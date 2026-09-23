"""Rooms: labels that carry a measured area, and a reader for plans that are not ours.

A room's area is the floor its walls enclose. `label_room` never takes it from
the caller: it reads the wall faces off the drawing, finds the planar face the
label point lies in (`engineering.arch.faces`) and writes *that* area into the
label and into the ``room`` record of ``ACADMCP_ARCH`` - so a room schedule is
a read of the drawing, and the number on the sheet is the number the geometry
gives.

A door or a window cuts its wall, so the two rooms either side of a door would
read as one face through the gap. The opening records on the drawing close it:
for each opening, the wall's two face lines are continued across the gap - in
memory only, never drawn - which is the convention a net floor area is measured
to (the wall face, not the reveal). A plan without our records has no such
lines, and `rooms_detect` says so rather than guessing where a door was.

`rooms_detect` runs the same finder on any lines and polylines - a foreign
plan - and reports each face with a confidence, the way `pid_graph` does:
1.0 when every edge of the face lies on our wall layer, 0.6 when any edge is a
plain line from somewhere else. It never modifies the drawing.

DECLARED, not standard:

* The label text heights (3.5 mm name, 2.5 mm number and area, on paper) are
  two values of the ISO 3098-1 series; which line gets which is this module's
  choice. The line pitch of 1.6 x the text height is this module's choice too.
* ``MIN_ROOM_WIDTH``: a face whose mean width (2 x area / perimeter) is under
  600 mm is taken for a wall body or an opening reveal, not a room. That is
  this reader's heuristic, not a building rule - a 200 mm wall ring measures
  200, a 900 mm corridor 5 m long measures 763.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

from engineering.arch.faces import DEFAULT_TOL, Face, face_containing, planar_faces, tagged_faces
from engineering.arch.lang import LANGS, fmt_area_m2
from engineering.arch.layers import ARCH_ROLE_LAYER
from engineering.arch.model import (
    APP_ID,
    Opening,
    Room,
    Stair,
    Wall,
    decode,
    from_payload,
    room_from_dict,
    segment_of,
    to_payload,
    to_values,
)
from engineering.measure import polygon_area_perimeter
from engineering.mech.primitives import Pt, Text

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = [
    "CONFIDENCE_FOREIGN",
    "CONFIDENCE_OURS",
    "DEFAULT_MIN_AREA",
    "LINE_PITCH",
    "MIN_ROOM_WIDTH",
    "NAME_HEIGHT",
    "TEXT_HEIGHT",
    "WALL_LAYER_KEYWORDS",
    "detect_rooms",
    "label_room",
    "mean_width",
    "opening_closures",
    "read_records",
    "read_segments",
    "room_label_prims",
    "rooms_detect",
]

NAME_HEIGHT = 3.5  # mm on paper (ISO 3098-1 series value)
TEXT_HEIGHT = 2.5  # mm on paper (ISO 3098-1 series value)
LINE_PITCH = 1.6  # baseline step, x the height of the line being placed
MIN_ROOM_WIDTH = 600.0  # mm; the wall-body heuristic above
DEFAULT_MIN_AREA = 1.0e6  # mm^2 = 1 m^2
CONFIDENCE_OURS = 1.0
CONFIDENCE_FOREIGN = 0.6
#: A layer whose name contains one of these (any case) is read by default.
WALL_LAYER_KEYWORDS = ("WALL", "DUVAR")
#: Entity types that bound a region with a curve; the finder reads straight
#: segments only, so these are reported as skipped, never approximated.
CURVE_TYPES = frozenset({"ARC", "CIRCLE", "ELLIPSE", "SPLINE"})


def _check_lang(lang: str) -> str:
    if lang not in LANGS:
        raise ValueError(
            f"lang: {lang!r} is not a label language; languages are {', '.join(LANGS)}"
        )
    return lang


def _positive(value, where: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected a number, got {value!r}") from None
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{where}: must be a finite number greater than zero, got {value!r}")
    return number


def room_label_prims(room: Room, *, lang: str = "en", scale=50) -> tuple[Text, ...]:
    """Name, number (when there is one) and measured area, stacked down from ``room.at``.

    Heights are paper millimetres times the plot-scale denominator: at 1:50 the
    3.5 mm name is 175 drawing units high. The first baseline is ``room.at``;
    each next one is 1.6 x its own height lower.
    """
    _check_lang(lang)
    factor = _positive(scale, "scale")
    x, y = float(room.at[0]), float(room.at[1])
    lines = [(room.name, NAME_HEIGHT * factor)]
    if room.number:
        lines.append((str(room.number), TEXT_HEIGHT * factor))
    lines.append((fmt_area_m2(room.area, lang), TEXT_HEIGHT * factor))
    prims: list[Text] = []
    baseline = y
    for index, (text, height) in enumerate(lines):
        if index:
            baseline -= LINE_PITCH * height
        prims.append(Text(at=(x, baseline), text=text, height=height, rotation=0.0, role="room"))
    return tuple(prims)


def _face_offsets(wall: Wall) -> tuple[float, float]:
    """Signed distances of the wall's two faces from its axis, + to the left."""
    t = float(wall.thickness)
    if wall.justification == "left":
        return (0.0, t)
    if wall.justification == "right":
        return (-t, 0.0)
    return (-t / 2.0, t / 2.0)


def opening_closures(walls, openings) -> tuple[tuple[tuple[Pt, Pt], ...], tuple[dict, ...]]:
    """The wall-face lines continued across every opening, and what could not be closed.

    For an opening at ``offset`` of ``width`` on its wall, the segment of the
    axis it sits on gives the direction ``u`` and the left normal ``n``; each
    face line runs from ``start + n*d`` to ``start + u*width + n*d`` for the
    face's signed offset ``d``. An opening whose wall has no record is returned
    in ``omitted`` - its gap stays open - never guessed.
    """
    by_id = {wall.id: wall for wall in walls}
    closures: list[tuple[Pt, Pt]] = []
    omitted: list[dict] = []
    for opening in openings:
        wall = by_id.get(opening.wall)
        if wall is None:
            omitted.append(
                {
                    "element": opening.id,
                    "reason": f"host wall {opening.wall!r} has no record on this drawing; "
                    "its gap is left open",
                }
            )
            continue
        try:
            index, along = segment_of(wall, float(opening.offset))
        except ValueError as exc:
            # a stale record (its wall was shortened since): the reader is
            # read-only and answers for every other room, so it says so here
            omitted.append({"element": opening.id, "reason": f"{exc}; its gap is left open"})
            continue
        axis = [(float(p[0]), float(p[1])) for p in wall.axis]
        p0, p1 = axis[index], axis[(index + 1) % len(axis)]
        length = math.dist(p0, p1)
        ux, uy = (p1[0] - p0[0]) / length, (p1[1] - p0[1]) / length
        nx, ny = -uy, ux
        sx, sy = p0[0] + ux * along, p0[1] + uy * along
        ex, ey = sx + ux * float(opening.width), sy + uy * float(opening.width)
        for d in _face_offsets(wall):
            closures.append(((sx + nx * d, sy + ny * d), (ex + nx * d, ey + ny * d)))
    return tuple(closures), tuple(omitted)


def mean_width(face: Face) -> float:
    """2 x area / perimeter: the thickness of a long thin face, holes included."""
    perimeter = sum(
        polygon_area_perimeter([(x, y, 0.0) for x, y in ring], True)[1]
        for ring in (face.loop, *face.holes)
    )
    return 2.0 * face.area / perimeter if perimeter > 0.0 else 0.0


def detect_rooms(
    segments,
    *,
    min_area: float = DEFAULT_MIN_AREA,
    tol: float = DEFAULT_TOL,
    min_width: float = MIN_ROOM_WIDTH,
) -> tuple[dict, ...]:
    """Faces of at least ``min_area`` mm^2 and ``min_width`` mean width, with a confidence.

    ``segments`` are ``(p1, p2)`` or ``(p1, p2, layer)``. A face scores 1.0
    when every edge of it (holes included) is covered by a segment on
    ``ARCH_ROLE_LAYER["wall"]``, and 0.6 otherwise - an untagged segment is a
    plain line from somewhere else. The minimum of the inputs, never raised by
    agreement: one foreign edge is enough to make the face 0.6.
    """
    min_area = _positive(min_area, "min_area")
    min_width = _positive(min_width, "min_width")
    wall = ARCH_ROLE_LAYER["wall"].casefold()
    rooms: list[dict] = []
    for face, tags in tagged_faces(segments, tol=tol):
        if face.area < min_area or mean_width(face) < min_width:
            continue
        ours = all(any(isinstance(t, str) and t.casefold() == wall for t in edge) for edge in tags)
        rooms.append(
            {
                "loop": face.loop,
                "holes": face.holes,
                "area": face.area,
                "centroid": face.centroid,
                "confidence": CONFIDENCE_OURS if ours else CONFIDENCE_FOREIGN,
            }
        )
    return tuple(rooms)


async def read_records(backend: AutoCADBackend) -> dict:
    """Every ``ACADMCP_ARCH`` record on the drawing's architectural layers.

    The shape `read_plan` (Task 4) returns - ``walls``, ``openings``, ``stairs``,
    ``rooms`` as model dataclasses - plus ``room_handles`` (room id -> the
    handle of the label that carries it). The current space is paged through
    once, not sampled and not once per layer: a record past the 200th entity is
    still a record, and a live seat, which pays a cross-process call per entity
    per scan, scans once rather than once for each of the twelve arch layers.
    Entities on the ``ARCH_ROLE_LAYER`` layers (any case) are read in layer-name
    order, drawing order within a layer. XDATA that is not ours or does not
    decode is skipped - this reader never guesses a record - and counted in
    ``unreadable``. A record that appears on several entities (a wall on each
    of its outlines) is kept once, by kind and id.
    """
    from engineering.sheet.bom import all_entities  # lazy: sheet imports stay out of arch

    found: dict[str, list] = {"walls": [], "openings": [], "stairs": [], "rooms": []}
    room_handles: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    unreadable = 0
    buckets = {Wall: "walls", Opening: "openings", Stair: "stairs", Room: "rooms"}
    arch_layers = {name.casefold(): name for name in ARCH_ROLE_LAYER.values()}
    ours = [
        entity
        for entity in await all_entities(backend)
        if str(entity.layer or "").casefold() in arch_layers
    ]
    ours.sort(key=lambda entity: arch_layers[str(entity.layer).casefold()])  # stable
    for entity in ours:
        raw = await backend.entity_get_xdata(entity.handle, APP_ID)
        values = (raw.get("xdata") or {}).get(APP_ID) or []
        if not values:
            continue
        try:
            obj = from_payload(decode(values))
        except (ValueError, KeyError, TypeError):
            obj = None
        bucket = buckets.get(type(obj))
        if bucket is None:
            unreadable += 1
            continue
        key = (bucket, obj.id)
        if key in seen:
            continue
        seen.add(key)
        found[bucket].append(obj)
        if bucket == "rooms":
            room_handles[obj.id] = entity.handle
    return {**found, "room_handles": room_handles, "unreadable": unreadable}


async def read_segments(backend: AutoCADBackend, layers) -> tuple[list, list[dict]]:
    """Straight segments on ``layers`` as ``(p1, p2, layer)``, and what was skipped.

    LINE and (LW)POLYLINE are read by their properties, which is the same on
    both engines (the live one names a polyline POLYLINE, the headless one
    LWPOLYLINE). A bulged polyline edge or an ARC/CIRCLE/ELLIPSE/SPLINE is an
    arc, and the finder reads straight segments only: it is listed in the
    second return value with its handle, never flattened into chords.
    """
    from engineering.sheet.bom import all_entities  # lazy: sheet imports stay out of arch

    segments: list = []
    skipped: list[dict] = []
    for layer in layers:
        for entity in await all_entities(backend, layer=layer):
            props = entity.properties or {}
            kind = str(entity.type).upper()
            if "start" in props and "end" in props and "center" not in props:
                segments.append((tuple(props["start"][:2]), tuple(props["end"][:2]), entity.layer))
            elif "points" in props:
                points = [tuple(p[:2]) for p in props["points"]]
                bulges = list(props.get("bulges") or [])
                count = len(points)
                arcs = 0
                for i in range(count if props.get("closed") else count - 1):
                    if i < len(bulges) and abs(float(bulges[i])) > 1e-12:
                        arcs += 1
                        continue
                    segments.append((points[i], points[(i + 1) % count], entity.layer))
                if arcs:
                    skipped.append(
                        {
                            "handle": entity.handle,
                            "type": kind,
                            "reason": f"{arcs} arc segment(s): the face finder reads straight "
                            "segments only and does not approximate an arc",
                        }
                    )
            elif kind in CURVE_TYPES:
                skipped.append(
                    {
                        "handle": entity.handle,
                        "type": kind,
                        "reason": "a curve: the face finder reads straight segments only "
                        "and does not approximate an arc",
                    }
                )
    return segments, skipped


def _wcs_loop(loop) -> list[list[float]]:
    return [[float(x), float(y)] for x, y in loop]


async def label_room(
    backend: AutoCADBackend, room_dict: dict, *, lang: str = "en", scale=50
) -> dict:
    """Label the room around ``room_dict["at"]`` with its name, number and measured area.

    Refused before anything is drawn: an ``area`` in the request (areas are
    measured, never typed), an unknown ``lang``, a non-positive ``scale``, an
    ``id`` already labelled on the drawing, a point that lies in no closed face
    of the wall layer, and a point inside a wall body. ``id`` defaults to the
    first free ``R<n>``.
    """
    if not isinstance(room_dict, dict):
        raise ValueError(
            f"room: expected an object with name and at, got {type(room_dict).__name__}"
        )
    if "area" in room_dict:
        raise ValueError(
            "room.area: a room's area is measured from its walls, never typed; remove it"
        )
    _check_lang(lang)
    _positive(scale, "scale")
    records = await read_records(backend)
    taken = {room.id for room in records["rooms"]}
    data = dict(room_dict)
    data.setdefault("number", None)
    if not data.get("id"):
        number = len(taken) + 1
        while f"R{number}" in taken:
            number += 1
        data["id"] = f"R{number}"
    room = room_from_dict(data, "room")
    if room.id in taken:
        raise ValueError(
            f"room.id: {room.id!r} is already labelled on this drawing "
            f"(label {records['room_handles'][room.id]}); choose another id"
        )

    wall_layer = ARCH_ROLE_LAYER["wall"]
    segments, skipped = await read_segments(backend, [wall_layer])
    closures, omitted = opening_closures(records["walls"], records["openings"])
    faces = planar_faces(
        [*segments, *((p1, p2, wall_layer) for p1, p2 in closures)], tol=DEFAULT_TOL
    )
    x, y = float(room.at[0]), float(room.at[1])
    face = face_containing(faces, (x, y))
    if face is None:
        raise ValueError(
            f"room.at: ({x:g}, {y:g}) lies in no closed face of the {len(segments)} wall "
            f"segment(s) on {wall_layer}; close the walls around it first (arch_wall), or read "
            "a plan drawn by other means with arch_rooms_detect"
        )
    width = mean_width(face)
    if width < MIN_ROOM_WIDTH:
        raise ValueError(
            f"room.at: ({x:g}, {y:g}) lies inside a wall body (a face {width:.0f} mm wide on "
            f"average, under the {MIN_ROOM_WIDTH:.0f} mm a room is taken to need), not in a room"
        )

    measured = replace(room, area=face.area)
    from engineering.mech.draw import draw_prims  # lazy: mech.draw imports the arch layer set

    drawn = await draw_prims(
        backend, room_label_prims(measured, lang=lang, scale=scale), role_layer=ARCH_ROLE_LAYER
    )
    handle = drawn["handles"][0]
    await backend.entity_set_xdata(handle, APP_ID, to_values(to_payload(measured)))
    return {
        "ok": True,
        "room": {
            "id": measured.id,
            "name": measured.name,
            "number": measured.number,
            "at": [x, y],
            "area": measured.area,
            "area_m2": measured.area / 1.0e6,
            "area_text": fmt_area_m2(measured.area, lang),
        },
        "handle": handle,
        "handles": list(drawn["handles"]),
        "layer": ARCH_ROLE_LAYER["room"],
        "loop": _wcs_loop(face.loop),
        "holes": [_wcs_loop(hole) for hole in face.holes],
        "wall_segments": len(segments),
        "closures": len(closures),
        "skipped": skipped,
        "omitted": list(omitted),
    }


async def rooms_detect(
    backend: AutoCADBackend,
    *,
    layers=None,
    min_area: float = DEFAULT_MIN_AREA,
    tol: float = DEFAULT_TOL,
) -> dict:
    """Read the rooms of any plan: faces of the lines on ``layers``, measured, never drawn.

    ``layers=None`` reads every layer whose name contains WALL or DUVAR (any
    case). A named layer the drawing does not have is refused with the list of
    the ones it does. Our own opening records, when there are any, close door
    and window gaps exactly as `label_room` does; a foreign plan's gaps stay
    open, and two rooms joined by an open doorway read as one face.
    """
    min_area = _positive(min_area, "min_area")
    tol = _positive(tol, "tol")
    available = [layer.name for layer in await backend.layer_list()]
    if layers is None:
        chosen = [
            name for name in available if any(key in name.upper() for key in WALL_LAYER_KEYWORDS)
        ]
        if not chosen:
            raise ValueError(
                "layers: no layer name contains "
                f"{' or '.join(WALL_LAYER_KEYWORDS)}; name the layers the walls are on "
                f"(layers are {', '.join(sorted(available))})"
            )
    else:
        lookup = {name.casefold(): name for name in available}
        missing = [str(name) for name in layers if str(name).casefold() not in lookup]
        if missing:
            raise ValueError(
                f"layers: {', '.join(missing)} not in this drawing; "
                f"layers are {', '.join(sorted(available))}"
            )
        chosen = [lookup[str(name).casefold()] for name in layers]

    segments, skipped = await read_segments(backend, chosen)
    records = await read_records(backend)
    closures, omitted = opening_closures(records["walls"], records["openings"])
    wall_layer = ARCH_ROLE_LAYER["wall"]
    extra = [(p1, p2, wall_layer) for p1, p2 in closures]
    rooms = detect_rooms([*segments, *extra], min_area=min_area, tol=tol)

    out: list[dict] = []
    for room in rooms:
        face = Face(
            loop=room["loop"], area=room["area"], centroid=room["centroid"], holes=room["holes"]
        )
        label = next(
            (
                {"id": r.id, "name": r.name, "number": r.number, "area": r.area}
                for r in records["rooms"]
                if face_containing((face,), r.at) is not None
            ),
            None,
        )
        out.append(
            {
                "loop": _wcs_loop(room["loop"]),
                "holes": [_wcs_loop(hole) for hole in room["holes"]],
                "area": room["area"],
                "area_m2": room["area"] / 1.0e6,
                "centroid": [room["centroid"][0], room["centroid"][1]],
                "confidence": room["confidence"],
                "label": label,
            }
        )
    return {
        "ok": True,
        "modified": False,
        "layers": chosen,
        "segments": len(segments),
        "closures": len(extra),
        "count": len(out),
        "rooms": out,
        "confidence_min": min((room["confidence"] for room in out), default=None),
        "skipped": skipped,
        "omitted": list(omitted),
        "min_area": min_area,
        "tol": tol,
    }
