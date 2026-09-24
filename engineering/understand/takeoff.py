"""Pipe takeoff rows: topology from the P&ID, lengths from the layout (spec §8).

A P&ID says which equipment a line joins, what it carries and how big it is;
it does not say how long the pipe is unless the scale check calls it to scale.
So each run found by `engineering.understand.network` is measured one of two
ways, and the rows say which:

* **layout** - the positions of the tags the run joins, on the layout, joined
  by the rectilinear minimum spanning tree (`route.rmst`), plus
  `vertical_allowance` metres per tree edge. The P&ID's drawn length is kept
  beside it as `schematic_m`, a comparison column, never the answer.
* **pid** - the drawn length on the P&ID: divided by the scale factor when the
  scale check calls it `to_scale`, taken in the P&ID's own unit when there is
  no layout (every row then carries `scale_verified: false`), and on a
  schematic P&ID only with `force=True`.

This module also carries the takeoff's own stand-ins for three readers another
branch of track H owns - the unit inference, the plan-copy clusters and the
scale check (group U's `units`, `clusters` and `scale`). They follow the
spec's rules (§5, §6) in the narrow form a takeoff needs; `pipe_rows` uses a
`scale` result it is given before its own, so the full scale check replaces
the stand-in wherever a caller passes it.

Every length leaves this module in metres, converted from the unit the
geometry implies - never blindly from `INSUNITS`. Nothing here touches a
drawing.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable

from engineering.arch.faces import DEFAULT_TOL, Face, face_containing, planar_faces
from engineering.understand.labels import normalize_panel, parse_electrical, wiring_target
from engineering.understand.network import (
    MODEL,
    TEXT_TYPES,
    label_search_default,
    tag_occurrences,
)
from engineering.understand.route import manhattan, rmst, route_legs
from engineering.understand.snapshot import Pt, Snapshot
from engineering.understand.vocab import SERVICES, classify_layer, room_label

__all__ = [
    "CABLE_SEARCH_SHARE",
    "DEFAULT_SERVICES",
    "LENGTH_SOURCES",
    "UNIT_METRES",
    "apportion",
    "cable_rows",
    "check_allowance",
    "check_pipe_options",
    "check_section_rules",
    "layout_tags",
    "pipe_rows",
    "room_at",
    "room_regions",
    "roundup_m",
    "scale_stand_in",
    "section_for",
    "unit_of",
]

#: The user's own takeoff scope; utilities only on request (spec §8).
DEFAULT_SERVICES = ("product", "cip_supply", "cip_return")
LENGTH_SOURCES = ("auto", "pid", "layout")
UNASSIGNED = "unassigned"

#: INSUNITS codes this module reads, and metres per unit of each.
INSUNITS_NAMES = {1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
UNIT_METRES = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}
#: This module's heuristic, not a standard: the model-space text of any plant
#: drawing - a P&ID sheet at 1:1 or a plan at 1:200 - stands between 0.5 mm and
#: 1 m tall, and the drawing spans between 0.1 m and 5 km. A declared unit that
#: puts the geometry outside either band is not believed.
TEXT_BAND_M = (0.0005, 1.0)
SPAN_BAND_M = (0.1, 5000.0)
#: Among the units that fit both bands, the one nearest (in log) a 50 m plant
#: with 0.25 m text is chosen.
TYPICAL_SPAN_M = 50.0
TYPICAL_TEXT_M = 0.25

#: Plan copies are told apart when the gap between them exceeds 2 % of the
#: drawing's robust span (5th-95th percentile).
CLUSTER_GAP_SHARE = 0.02
#: Spec §6: pairs of matched tags at least 2 m apart, a ±10 % band, 80 % / 50 %.
MIN_PAIR_MM = 2000.0
SCALE_BAND = 0.10
TO_SCALE_SHARE = 0.80
SCHEMATIC_SHARE = 0.50
#: Fewer matched tags than this and the verdict is `insufficient`.
MIN_TAGS = 3
#: A wall layer is one `classify_layer` files under architecture this surely.
WALL_CONFIDENCE = 0.9


# -- options -----------------------------------------------------------------


def _number(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: expected a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{name}: must be finite, got {value!r}")
    return number


def check_allowance(allowance) -> float:
    number = _number(allowance, "allowance")
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"allowance: must lie between 0 and 1 (0.20 is 20 %), got {number:g}")
    return number


def check_pipe_options(*, services, allowance, vertical_allowance, length_source) -> dict:
    """Refuse bad takeoff options by name, before a drawing is read."""
    chosen = DEFAULT_SERVICES if services is None else tuple(dict.fromkeys(services))
    if not chosen:
        raise ValueError("services: an empty list takes nothing off; omit it for the default")
    unknown = [s for s in chosen if s not in SERVICES]
    if unknown:
        raise ValueError(
            f"services: {', '.join(map(str, unknown))} unknown; choose from {', '.join(SERVICES)}"
        )
    vertical = _number(vertical_allowance, "vertical_allowance")
    if vertical < 0.0:
        raise ValueError(f"vertical_allowance: must be zero or more metres, got {vertical:g}")
    if length_source not in LENGTH_SOURCES:
        raise ValueError(
            f"length_source: {length_source!r} unknown; choose from {', '.join(LENGTH_SOURCES)}"
        )
    return {
        "services": chosen,
        "allowance": check_allowance(allowance),
        "vertical_allowance": vertical,
        "length_source": length_source,
    }


# -- units -------------------------------------------------------------------


def _model_points(snap: Snapshot) -> list[Pt]:
    out: list[Pt] = []
    for rec in snap.records:
        if rec.space != MODEL:
            continue
        out.extend(rec.points)
        if rec.bbox is not None:
            out.extend(((rec.bbox[0], rec.bbox[1]), (rec.bbox[2], rec.bbox[3])))
    return out


def _robust_box(points: list[Pt]) -> tuple[float, float, float, float] | None:
    """The 5th-95th percentile box of the points: one stray entity does not stretch it."""
    if len(points) < 2:
        return None
    qx = statistics.quantiles([p[0] for p in points], n=20, method="inclusive")
    qy = statistics.quantiles([p[1] for p in points], n=20, method="inclusive")
    return (qx[0], qy[0], qx[-1], qy[-1])


def _text_heights(snap: Snapshot) -> list[float]:
    return [
        rec.height
        for rec in snap.records
        if rec.space == MODEL and rec.type in TEXT_TYPES and rec.height and rec.height > 0.0
    ]


def unit_of(snap: Snapshot) -> dict:
    """The unit a drawing's geometry implies, against the one INSUNITS declares.

    ``{"declared", "inferred", "m_per_unit", "warning", "evidence"}``. The
    declared unit (mm when INSUNITS declares none) stands when it puts the
    median model-space text height between 0.5 mm and 1 m and the robust span
    (5th-95th percentile) between 0.1 m and 5 km - this module's heuristic.
    Otherwise the unit of mm / cm / m / in / ft that fits both bands and lies
    nearest a 50 m drawing with 0.25 m text is taken. A disagreement is a
    warning with the numbers - never a silent correction.
    """
    declared = INSUNITS_NAMES.get(snap.insunits)
    box = _robust_box(_model_points(snap))
    span = max(box[2] - box[0], box[3] - box[1]) if box else 0.0
    heights = _text_heights(snap)
    height = statistics.median(heights) if heights else 0.0

    def fits(unit: str) -> bool:
        m = UNIT_METRES[unit]
        return (span <= 0.0 or SPAN_BAND_M[0] <= span * m <= SPAN_BAND_M[1]) and (
            height <= 0.0 or TEXT_BAND_M[0] <= height * m <= TEXT_BAND_M[1]
        )

    def score(unit: str) -> float:
        m = UNIT_METRES[unit]
        total = abs(math.log(span * m / TYPICAL_SPAN_M)) if span > 0.0 else 0.0
        return total + (abs(math.log(height * m / TYPICAL_TEXT_M)) if height > 0.0 else 0.0)

    base = declared or "mm"
    inferred = base
    if not fits(base):
        candidates = [unit for unit in UNIT_METRES if fits(unit)]
        if candidates:
            inferred = min(candidates, key=lambda unit: (score(unit), unit))
    if declared is not None:
        said = declared
    elif snap.insunits:
        said = f"code {snap.insunits} (a unit this reader does not convert)"
    else:
        said = "no unit"
    warning = None
    if inferred != declared:
        warning = (
            f"{snap.source}: INSUNITS declares {said} but the geometry reads "
            f"as {inferred} (median text height {height:g}, robust span {span:g} drawing "
            f"units); lengths are taken in {inferred}"
        )
    return {
        "declared": declared,
        "inferred": inferred,
        "m_per_unit": UNIT_METRES[inferred],
        "warning": warning,
        "evidence": {"robust_span": span, "median_text_height": height},
    }


# -- plan copies and tag positions ---------------------------------------------


def _mark_segment(cells: set, a: Pt, b: Pt, gap: float, limit: float) -> None:
    length = math.dist(a, b)
    if length > limit:
        steps = 0  # a stray line across the world marks only its ends
    else:
        steps = int(length / (gap / 2.0))
    for i in range(steps + 1):
        t = i / steps if steps else 0.0
        x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        cells.add((math.floor(x / gap), math.floor(y / gap)))
    cells.add((math.floor(b[0] / gap), math.floor(b[1] / gap)))


def _clusters(snap: Snapshot) -> tuple[float, list[tuple[float, float, float, float]]]:
    """(cell size, cluster boxes): separate bodies of model-space content.

    Every record's geometry marks the cells of a grid whose cell is 2 % of the
    robust span; 8-connected cells form bodies; bodies whose boxes overlap are
    one cluster (a label in the middle of a room belongs to the plan around it).
    """
    points = _model_points(snap)
    box = _robust_box(points)
    if box is None:
        return 1.0, []
    span = max(box[2] - box[0], box[3] - box[1], 1.0)
    gap = CLUSTER_GAP_SHARE * span
    limit = 10.0 * span
    cells: set[tuple[int, int]] = set()
    for rec in snap.records:
        if rec.space != MODEL:
            continue
        pts = list(rec.points)
        if rec.type in ("LINE", "LWPOLYLINE", "POLYLINE") and len(pts) >= 2:
            ring = pts + ([pts[0]] if rec.closed else [])
            for a, b in zip(ring, ring[1:], strict=False):
                _mark_segment(cells, a, b, gap, limit)
        elif rec.bbox is not None:
            x0, y0, x1, y1 = rec.bbox
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            for a, b in zip(corners, corners[1:], strict=False):
                _mark_segment(cells, a, b, gap, limit)
        for p in pts[:1]:
            cells.add((math.floor(p[0] / gap), math.floor(p[1] / gap)))
    seen: set[tuple[int, int]] = set()
    bodies: list[list[float]] = []
    for start in sorted(cells):
        if start in seen:
            continue
        seen.add(start)
        stack, body = [start], [start[0], start[1], start[0], start[1]]
        while stack:
            cx, cy = stack.pop()
            body = [min(body[0], cx), min(body[1], cy), max(body[2], cx), max(body[3], cy)]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (cx + dx, cy + dy)
                    if n in cells and n not in seen:
                        seen.add(n)
                        stack.append(n)
        bodies.append(body)
    merged = True
    while merged:
        merged = False
        for i in range(len(bodies)):
            for j in range(i + 1, len(bodies)):
                a, b = bodies[i], bodies[j]
                if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                    bodies[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del bodies[j]
                    merged = True
                    break
            if merged:
                break
    boxes = [(b[0] * gap, b[1] * gap, (b[2] + 1) * gap, (b[3] + 1) * gap) for b in sorted(bodies)]
    return gap, boxes


def layout_tags(snap: Snapshot, wanted: Iterable[str] = ()) -> dict:
    """Tag positions on a drawing, using only tags that are unique where they are read.

    When no tag is written twice the whole model space is read. Otherwise the
    drawing is split into clusters (plan copies, sheets, a far outlier) and the
    cluster holding the most `wanted` tags is chosen (ties: the one furthest
    left, then lowest); a tag written more than once inside it is `ambiguous`,
    never picked arbitrarily. ``{"positions": {tag: Pt}, "ambiguous": [...],
    "cluster": box | None, "clusters": n}``.
    """
    occurrences = tag_occurrences(snap)
    wanted = set(wanted)
    if all(len(places) == 1 for places in occurrences.values()):
        return {
            "positions": {tag: places[0]["at"] for tag, places in occurrences.items()},
            "ambiguous": [],
            "cluster": None,
            "clusters": 1,
        }
    _gap, boxes = _clusters(snap)

    def inside(p: Pt, box) -> bool:
        return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]

    def weight(box) -> tuple:
        count = sum(
            1
            for tag, places in occurrences.items()
            if tag in wanted and any(inside(place["at"], box) for place in places)
        )
        return (-count, box[0], box[1])

    chosen = min(boxes, key=weight) if boxes else None
    positions, ambiguous = {}, []
    for tag, places in sorted(occurrences.items()):
        here = [place for place in places if chosen is None or inside(place["at"], chosen)]
        if len(here) == 1:
            positions[tag] = here[0]["at"]
        elif len(here) > 1:
            ambiguous.append(tag)
    return {
        "positions": positions,
        "ambiguous": ambiguous,
        "cluster": chosen,
        "clusters": len(boxes),
    }


def scale_stand_in(
    pid: Snapshot, layout: Snapshot, *, pid_tags=None, layout_found=None, layout_unit=None
) -> dict:
    """Spec §6 on the tags unique in both drawings - the takeoff's stand-in for scale_check.

    The result has `scale_check`'s shape (group U, Task 4): for every pair of
    matched tags at least 2 m apart on the layout, ``ratio`` is their P&ID
    distance over their layout distance, both in millimetres of each drawing's
    inferred unit; ``median``, ``q1``, ``q3``, ``iqr`` and ``within_10pct`` (the
    share within ±10 % of the median) describe them. Verdict ``to_scale`` at
    >= 80 %, ``schematic`` at <= 50 %, ``partly`` between, ``insufficient``
    with fewer than three matched tags or no pair; ``factor`` is the median
    only when ``to_scale``; a median that is not positive (every P&ID tag at
    one point) is ``insufficient``. There are no per-room ``groups`` here. Tag labels
    are not equipment centres: the band and the 2 m floor absorb that.
    """
    pid_occ = pid_tags if pid_tags is not None else tag_occurrences(pid)
    pid_positions = {tag: places[0]["at"] for tag, places in pid_occ.items() if len(places) == 1}
    found = layout_found if layout_found is not None else layout_tags(layout, pid_positions)
    unit = layout_unit if layout_unit is not None else unit_of(layout)
    mm_layout = unit["m_per_unit"] * 1000.0
    mm_pid = unit_of(pid)["m_per_unit"] * 1000.0
    tags = [tag for tag in sorted(pid_positions) if tag in found["positions"]]
    matched = {tag: (pid_positions[tag], found["positions"][tag]) for tag in tags}
    ambiguous = sorted(
        {tag for tag, places in pid_occ.items() if len(places) > 1} | set(found["ambiguous"])
    )
    pairs = []
    for i, a in enumerate(tags):
        for b in tags[i + 1 :]:
            d_layout = math.dist(matched[a][1], matched[b][1]) * mm_layout
            if d_layout >= MIN_PAIR_MM:
                d_pid = math.dist(matched[a][0], matched[b][0]) * mm_pid
                pairs.append(
                    {"tags": [a, b], "a_mm": d_pid, "b_mm": d_layout, "ratio": d_pid / d_layout}
                )
    out = {
        "verdict": "insufficient",
        "factor": None,
        "median": None,
        "q1": None,
        "q3": None,
        "iqr": None,
        "within_10pct": None,
        "pair_count": len(pairs),
        "pairs": pairs,
        "matched": [
            {"tag": tag, "a": list(matched[tag][0]), "b": list(matched[tag][1])} for tag in tags
        ],
        "ambiguous": ambiguous,
        "groups": [],
        "notes": ["takeoff stand-in for scale_check (spec §6): no per-room groups"],
    }
    ratios = [pair["ratio"] for pair in pairs]
    if len(tags) < MIN_TAGS or not ratios:
        return out
    median = statistics.median(ratios)
    if median <= 0.0:
        return out  # every P&ID tag at one point: no factor to divide by (as scale_check)
    q1, q3 = (
        statistics.quantiles(ratios, n=4, method="inclusive")[::2]
        if len(ratios) >= 2
        else (ratios[0], ratios[0])
    )
    within = sum(1 for r in ratios if abs(r - median) <= SCALE_BAND * median) / len(ratios)
    if within >= TO_SCALE_SHARE:
        verdict = "to_scale"
    elif within <= SCHEMATIC_SHARE:
        verdict = "schematic"
    else:
        verdict = "partly"
    out.update(
        verdict=verdict,
        factor=median if verdict == "to_scale" else None,
        median=median,
        q1=q1,
        q3=q3,
        iqr=q3 - q1,
        within_10pct=within,
    )
    return out


# -- rooms -------------------------------------------------------------------


def room_regions(snap: Snapshot, cluster=None) -> dict:
    """Room polygons and room labels of a drawing.

    Labels are texts `room_label` reads; polygons are the faces of the wall
    layers (`classify_layer` architecture, confidence >= 0.9, straight
    segments) that hold a label, found by track F's planar-face finder. Inside
    `cluster` only, when one is given. ``{"faces": [(key, Face)], "labels":
    [(key, Pt)]}``; the key is the room number, else its name.
    """

    def keep(p: Pt) -> bool:
        return cluster is None or (
            cluster[0] <= p[0] <= cluster[2] and cluster[1] <= p[1] <= cluster[3]
        )

    labels = []
    for rec in snap.records:
        if rec.space != MODEL or rec.type not in TEXT_TYPES or not rec.text or not rec.points:
            continue
        found = room_label(rec.text)
        if found and keep(rec.points[0]):
            key = found.get("number") or found.get("name") or rec.text
            labels.append((str(key), rec.points[0]))
    walls = set()
    for layer in {rec.layer for rec in snap.records}:  # classify each layer once
        info = classify_layer(layer)
        if info["discipline"] == "architecture" and info["confidence"] >= WALL_CONFIDENCE:
            walls.add(layer)
    segments = []
    for rec in snap.records:
        if rec.space != MODEL or rec.layer not in walls:
            continue
        if rec.type not in ("LINE", "LWPOLYLINE", "POLYLINE") or len(rec.points) < 2:
            continue
        pts = list(rec.points) + ([rec.points[0]] if rec.closed else [])
        bulges = list(rec.bulges) + [0.0] * len(pts)
        for i, (a, b) in enumerate(zip(pts, pts[1:], strict=False)):
            if bulges[i] == 0.0 and keep(a) and keep(b):
                segments.append((a, b))
    faces = planar_faces(segments, tol=DEFAULT_TOL) if segments else ()
    rooms: list[tuple[str, Face]] = []
    taken: set[int] = set()
    for key, at in sorted(labels):
        face = face_containing(faces, at)
        if face is not None and id(face) not in taken:
            taken.add(id(face))
            rooms.append((key, face))
    return {"faces": rooms, "labels": sorted(labels)}


def _rooms_mode(regions: dict) -> str:
    if regions["faces"]:
        return "polygons"
    return "nearest_label" if regions["labels"] else "none"


def room_at(regions: dict, p: Pt) -> str:
    """The room a point stands in; else the nearest room label; else ''."""
    face = face_containing([face for _key, face in regions["faces"]], p)
    if face is not None:
        return next(key for key, f in regions["faces"] if f is face)
    if regions["labels"]:
        return min(regions["labels"], key=lambda item: (math.dist(item[1], p), item[0]))[0]
    return ""


def apportion(legs, regions: dict) -> dict[str, float]:
    """Length of each orthogonal leg inside each room polygon (drawing units).

    A leg is cut at every crossing with a room outline or hole; each piece goes
    to the room its midpoint stands in, or to '' outside every room. Nothing is
    counted twice: the pieces add up to the leg.
    """
    faces = [face for _key, face in regions["faces"]]
    key_of = {id(face): key for key, face in regions["faces"]}
    out: dict[str, float] = {}
    for a, b in legs:
        length = math.dist(a, b)
        if length == 0.0:
            continue
        cuts = {0.0, 1.0}
        for face in faces:
            for ring in (face.loop, *face.holes):
                for i in range(len(ring)):
                    t = _cut(a, b, ring[i], ring[(i + 1) % len(ring)])
                    if t is not None:
                        cuts.add(t)
        ts = sorted(cuts)
        for t0, t1 in zip(ts, ts[1:], strict=False):
            if t1 - t0 <= 0.0:
                continue
            tm = (t0 + t1) / 2.0
            mid = (a[0] + (b[0] - a[0]) * tm, a[1] + (b[1] - a[1]) * tm)
            face = face_containing(faces, mid)
            key = key_of[id(face)] if face is not None else ""
            out[key] = out.get(key, 0.0) + (t1 - t0) * length
    return out


def _cut(a: Pt, b: Pt, c: Pt, d: Pt) -> float | None:
    """Parameter along a->b where it crosses segment c-d, or None."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denom = rx * sy - ry * sx
    if denom == 0.0:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = (qx * sy - qy * sx) / denom
    u = (qx * ry - qy * rx) / denom
    if 0.0 < t < 1.0 and 0.0 <= u <= 1.0:
        return t
    return None


# -- pipe rows ---------------------------------------------------------------


def _choose_source(requested: str, layout, scale: dict, force: bool) -> tuple[str, str]:
    verdict = scale.get("verdict")
    if requested == "layout":
        if layout is None:
            raise ValueError("length_source='layout' needs a layout drawing; none was given")
        return "layout", "requested"
    if requested == "pid":
        if layout is not None and verdict == "schematic" and not force:
            raise ValueError(
                "length_source='pid': the scale check calls this P&ID schematic - "
                f"{scale['within_10pct']:.0%} of {scale['pair_count']} tag pairs lie within "
                f"±10 % of the median ratio {scale['median']:.6g} (IQR {scale['iqr']:.6g}); "
                "a length measured on it is drawn line, not pipe. Pass force=True to measure "
                "on it anyway, or use length_source='layout'."
            )
        return "pid", "forced" if layout is not None and verdict == "schematic" else "requested"
    if layout is None:
        return "pid", "no_layout"
    if verdict == "to_scale":
        return "pid", "auto_to_scale"
    return "layout", f"auto_{verdict}"


def pipe_rows(
    network,
    pid: Snapshot,
    layout: Snapshot | None,
    *,
    scale=None,
    services=DEFAULT_SERVICES,
    allowance=0.20,
    vertical_allowance=0.0,
    length_source="auto",
    force=False,
) -> dict:
    """Takeoff rows by room x service x diameter, with the runs, checks and method behind them.

    `network` is `build_network(pid)`'s result. `scale` is a scale-check
    result (`scale_check`'s shape); omitted, the stand-in runs when a layout
    is given. Refused by name before anything is measured: an unknown service
    or length source, an allowance outside 0-1, a negative vertical
    allowance, `length_source='layout'` without a layout, and
    `length_source='pid'` on a P&ID the scale check calls schematic unless
    `force=True` (the refusal quotes the statistics).

    Status per run: **A** every end on a tag placed on the layout and every
    piece's diameter read directly; **B** a diameter carried by continuity or
    unassigned; **C** not routable - an end on no equipment, fewer than two
    tags, a tag missing from (or written twice on) the layout. A C run goes to
    `control` with its schematic length only, is counted in the totals and is
    flagged. A run holding several diameters shares its length among them in
    proportion to their drawn length on the P&ID.
    """
    options = check_pipe_options(
        services=services,
        allowance=allowance,
        vertical_allowance=vertical_allowance,
        length_source=length_source,
    )
    allowance = options["allowance"]
    vertical = options["vertical_allowance"]
    pid_unit = unit_of(pid)
    layout_unit = unit_of(layout) if layout is not None else None
    wanted = {tag for run in network["runs"] for tag in run.tags}
    found = layout_tags(layout, wanted) if layout is not None else None
    if scale is None:
        scale = (
            scale_stand_in(pid, layout, layout_found=found, layout_unit=layout_unit)
            if layout is not None
            else {"verdict": "not_checked", "pair_count": 0, "matched": [], "ambiguous": []}
        )
    source, reason = _choose_source(length_source, layout, scale, bool(force))
    scale_verified = source == "layout" or scale.get("verdict") == "to_scale"
    positions = found["positions"] if found else {}
    ambiguous = set(found["ambiguous"]) if found else set()
    regions = (
        room_regions(layout, found["cluster"])
        if found is not None and source == "layout"
        else room_regions(pid)
    )
    m_pid = pid_unit["m_per_unit"]
    m_layout = layout_unit["m_per_unit"] if layout_unit else None

    rows: dict[tuple[str, str, str], dict] = {}
    runs_out, control, excluded = [], [], {}
    for run in network["runs"]:
        if run.service not in options["services"]:
            excluded[run.service] = excluded.get(run.service, 0) + 1
            continue
        ends = network["attachments"].get(run.id, [])
        drawn = sum(edge["length"] for edge in run.edges)
        schematic_m = drawn * m_pid
        parts: dict[str, dict] = {}
        for edge in run.edges:
            part = parts.setdefault(
                edge["diameter"] or UNASSIGNED,
                {"drawn": 0.0, "direct": 0.0, "continuity": 0.0, "unassigned": 0.0},
            )
            part["drawn"] += edge["length"]
            part[edge["diameter_source"]] += edge["length"]
        why = None
        if any(end["tag"] is None for end in ends) or not ends:
            why = "end_not_on_equipment"
        elif len(run.tags) < 2:
            why = "single_tag"
        elif source == "layout":
            missing = [tag for tag in run.tags if tag not in positions]
            if missing:
                why = (
                    "tag_ambiguous" if any(t in ambiguous for t in missing) else "tag_not_on_layout"
                )
        detail = {
            "id": run.id,
            "service": run.service,
            "tags": list(run.tags),
            "diameters": {d: p["drawn"] * m_pid for d, p in sorted(parts.items())},
            "schematic_m": schematic_m,
            "layout_m": None,
            "vertical_m": 0.0,
            "length_m": None,
            "rooms": {},
            "status": "C",
            "reason": why,
        }
        runs_out.append(detail)
        for diameter, part in sorted(parts.items()):
            if diameter == UNASSIGNED:
                control.append(
                    {
                        "run": run.id,
                        "kind": "unassigned_diameter",
                        "service": run.service,
                        "tags": list(run.tags),
                        "schematic_m": part["drawn"] * m_pid,
                        "reason": "no_diameter_label",
                    }
                )
        if why is not None:
            control.append(
                {
                    "run": run.id,
                    "kind": "unroutable",
                    "service": run.service,
                    "tags": list(run.tags),
                    "schematic_m": schematic_m,
                    "reason": why,
                }
            )
            continue
        if source == "layout":
            points = [positions[tag] for tag in run.tags]
            length_units, tree = rmst(points)
            if regions["faces"]:
                legs = [leg for i, j in tree for leg in route_legs(points[i], points[j])]
                rooms = {key: value * m_layout for key, value in apportion(legs, regions).items()}
            else:
                centre = (
                    sum(p[0] for p in points) / len(points),
                    sum(p[1] for p in points) / len(points),
                )
                rooms = {room_at(regions, centre): length_units * m_layout}
            for _i, j in tree:
                key = room_at(regions, points[j])
                rooms[key] = rooms.get(key, 0.0) + vertical
            detail["layout_m"] = length_units * m_layout
            detail["vertical_m"] = vertical * len(tree)
            length_m = detail["layout_m"] + detail["vertical_m"]
        else:
            if layout is not None and scale.get("verdict") == "to_scale":
                length_m = schematic_m / scale["factor"]
            else:
                length_m = schematic_m
            centre = (
                sum(end["at"][0] for end in ends) / len(ends),
                sum(end["at"][1] for end in ends) / len(ends),
            )
            rooms = {room_at(regions, centre): length_m}
        detail["length_m"] = length_m
        detail["rooms"] = rooms
        status = (
            "A"
            if all(p["continuity"] == 0.0 and p["unassigned"] == 0.0 for p in parts.values())
            else "B"
        )
        detail["status"] = status
        for diameter, part in parts.items():
            share = part["drawn"] / drawn if drawn else 0.0
            for room, room_m in rooms.items():
                x = room_m * share
                row = rows.setdefault(
                    (room, run.service, diameter),
                    {
                        "room": room,
                        "service": run.service,
                        "diameter": diameter,
                        "direct_m": 0.0,
                        "continuity_m": 0.0,
                        "unassigned_m": 0.0,
                        "net_m": 0.0,
                        "schematic_m": 0.0,
                        "status": "A",
                        "runs": [],
                    },
                )
                row["net_m"] += x
                row["direct_m"] += x * part["direct"] / part["drawn"]
                row["continuity_m"] += x * part["continuity"] / part["drawn"]
                row["unassigned_m"] += x * part["unassigned"] / part["drawn"]
                row["schematic_m"] += schematic_m * share * (room_m / length_m if length_m else 0.0)
                if part["continuity"] or part["unassigned"]:
                    row["status"] = "B"
                if run.id not in row["runs"]:
                    row["runs"].append(run.id)

    order = {service: i for i, service in enumerate(SERVICES)}
    table = []
    for key in sorted(rows, key=lambda k: (k[0], order.get(k[1], len(order)), k[2])):
        row = rows[key]
        row["allowance"] = allowance
        row["allowance_m"] = row["net_m"] * allowance
        row["total_m"] = row["net_m"] + row["allowance_m"]
        table.append(row)

    routed = sum(row["net_m"] for row in table)
    unroutable = sum(c["schematic_m"] for c in control if c["kind"] == "unroutable")
    net_total = routed + unroutable
    summary = {
        name: [
            {
                "key": key,
                "net_m": sum(r["net_m"] for r in table if r[field] == key),
                "total_m": sum(r["total_m"] for r in table if r[field] == key),
            }
            for key in sorted({r[field] for r in table})
        ]
        for name, field in (
            ("by_service", "service"),
            ("by_diameter", "diameter"),
            ("by_room", "room"),
        )
    }
    warnings = [u["warning"] for u in (pid_unit, layout_unit) if u and u["warning"]]
    dropped = int(network.get("stats", {}).get("segments_below_tol") or 0)
    if dropped:
        warnings.append(
            f"{dropped} P&ID segment(s) shorter than the junction tolerance "
            f"({network['stats']['tol']:g} drawing units) collapsed to a point and are not "
            "counted"
        )
    if not scale_verified:
        warnings.append(
            "scale_verified: false - lengths are drawn lengths on a P&ID that no scale check "
            "has confirmed; they are not physical pipe lengths"
        )
    return {
        "kind": "pipe",
        "rows": table,
        "runs": runs_out,
        "control": control,
        "summary": summary,
        "totals": {
            "routed_m": routed,
            "unroutable_m": unroutable,
            "net_m": net_total,
            "allowance": allowance,
            "allowance_m": net_total * allowance,
            "total_m": net_total * (1.0 + allowance),
            "flagged_runs": sum(1 for r in runs_out if r["status"] == "C"),
        },
        "length_source": source,
        "scale": scale,
        "scale_verified": scale_verified,
        "units": {"pid": pid_unit, "layout": layout_unit},
        "warnings": warnings,
        "method": {
            "sources": {"pid": pid.source, "layout": layout.source if layout else None},
            "length_source": source,
            "requested": length_source,
            "reason": reason,
            "allowance": allowance,
            "vertical_allowance": vertical,
            "services": list(options["services"]),
            "excluded_services": excluded,
            "tol": network["stats"]["tol"],
            "label_search": network["stats"]["label_search"],
            "overlap_length_m": network["stats"]["overlap_length"] * m_pid,
            "route": "rectilinear MST of the tag positions; each connection x first, then y",
            "rooms": _rooms_mode(regions),
        },
    }


# -- cable rows ----------------------------------------------------------------

#: A load's power block stands under its symbol rather than on a line, so its
#: electrical texts and its wiring callout are searched twice as far as a
#: diameter label: 10 x the median text height.
CABLE_SEARCH_SHARE = 2.0
SECTION_RULE_KEYS = ("max_kw", "section", "phases")


def check_section_rules(section_rules) -> tuple[dict, ...]:
    """The caller's own cable-section rule table, validated and sorted by max_kw.

    Each rule is ``{"max_kw": > 0, "section": text, "phases": 1 | 3 (optional)}``;
    a load takes the first rule whose max_kw it does not exceed and whose
    phases, when the rule names them, equal the load's. No standard table exists
    in this repository: without rules no section is proposed.
    """
    if section_rules is None:
        return ()
    if not isinstance(section_rules, (list, tuple)):
        raise ValueError("section_rules: expected a list of {max_kw, section[, phases]}")
    rules = []
    for i, rule in enumerate(section_rules):
        where = f"section_rules[{i}]"
        if not isinstance(rule, dict):
            raise ValueError(f"{where}: expected {{max_kw, section[, phases]}}, got {rule!r}")
        unknown = sorted(set(rule) - set(SECTION_RULE_KEYS))
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(map(str, unknown))}")
        max_kw = _number(rule.get("max_kw"), f"{where}.max_kw")
        if max_kw <= 0.0:
            raise ValueError(f"{where}.max_kw: must be greater than zero, got {max_kw:g}")
        section = rule.get("section")
        if not isinstance(section, str) or not section.strip():
            raise ValueError(
                f"{where}.section: expected a non-empty text such as '5x2.5', got {section!r}"
            )
        phases = rule.get("phases")
        if phases is not None and phases not in (1, 3):
            raise ValueError(f"{where}.phases: 1 or 3, got {phases!r}")
        rules.append({"max_kw": max_kw, "section": section.strip(), "phases": phases})
    return tuple(sorted(rules, key=lambda r: (r["max_kw"], r["phases"] or 0)))


def section_for(kw: float | None, phases: int | None, rules) -> str | None:
    """The first rule the load fits, or None - never a guessed section."""
    if kw is None:
        return None
    for rule in rules:
        if kw <= rule["max_kw"] and (rule["phases"] is None or rule["phases"] == phases):
            return rule["section"]
    return None


def roundup_m(length_m: float, allowance: float) -> int:
    """ROUNDUP(length x (1 + allowance)) to the metre, on the value rounded to 1e-6 m first.

    The rounding keeps binary noise from buying a metre of cable: 50 m with a
    10 % allowance is 55.00000000000001 in floating point, and 55 m on the sheet.
    """
    return math.ceil(round(length_m * (1.0 + allowance), 6))


def _nearest_tag(at: Pt, places: dict[str, list[Pt]], search: float) -> str | None:
    found = [
        (math.dist(at, p), tag)
        for tag, points in places.items()
        for p in points
        if math.dist(at, p) <= search
    ]
    return min(found)[1] if found else None


def _inside_box(p: Pt, box) -> bool:
    return box is None or (box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3])


def cable_rows(
    pid: Snapshot, layout: Snapshot | None, *, allowance=0.20, section_rules=None
) -> dict:
    """One row per electrical load, the cable method made repeatable (spec §9).

    Loads are the P&ID tags with electrical texts near them (`parse_electrical`:
    kW, phases, voltage, + N, VFD) and the tags a wiring callout names. The
    panel comes from the layout's callouts (`wiring_target`: 'Wiring to CP1'
    -> CP-1), else the P&ID's. The length is the Manhattan distance on the
    layout from the load's tag to its panel's tag, and the cable is
    `roundup_m(length, allowance)`. **Power is never invented**: a load with no
    stated kW keeps an empty cell and an open item. With no layout, lengths
    stay empty and every row gets an open item - the P&ID is not a length
    source for cables. A load wired to a panel that itself states a power is
    part of that package: room and panel totals count the package's power and
    not its children's. Sections come only from `section_rules`.

    Refused by name before anything is read: an allowance outside 0-1 and a
    malformed section rule.
    """
    allowance = check_allowance(allowance)
    rules = check_section_rules(section_rules)
    pid_places = {tag: [p["at"] for p in places] for tag, places in tag_occurrences(pid).items()}
    pid_search = CABLE_SEARCH_SHARE * label_search_default(pid)

    loads: dict[str, list] = {}
    orphans: list[str] = []
    pid_callouts: dict[str, str] = {}
    for rec in pid.records:
        if rec.space != MODEL or rec.type not in TEXT_TYPES or not rec.text or not rec.points:
            continue
        at = rec.points[0]
        target = wiring_target(rec.text)
        if target is not None:
            tag = _nearest_tag(at, pid_places, pid_search)
            if tag is not None:
                pid_callouts.setdefault(tag, target)
            continue
        found = parse_electrical(rec.text)
        if found["kw"] is None and not (found["phases"] or found["voltage"] or found["vfd"]):
            continue
        tag = _nearest_tag(at, pid_places, pid_search)
        if tag is None:
            orphans.append(rec.handle)
            continue
        distance = min(math.dist(at, p) for p in pid_places[tag])
        loads.setdefault(tag, []).append((distance, rec.handle, found))

    positions: dict[str, Pt] = {}
    ambiguous: set[str] = set()
    panels: dict[str, tuple[str, str]] = {}
    layout_unit = unit_of(layout) if layout is not None else None
    cluster = None
    layout_search = None
    if layout is not None:
        placed = layout_tags(layout, set(loads) | set(pid_callouts))
        positions, ambiguous, cluster = (
            placed["positions"],
            set(placed["ambiguous"]),
            placed["cluster"],
        )
        layout_search = CABLE_SEARCH_SHARE * label_search_default(layout)
        layout_places = {tag: [p] for tag, p in positions.items()}
        for rec in layout.records:
            if rec.space != MODEL or rec.type not in TEXT_TYPES or not rec.text or not rec.points:
                continue
            target = wiring_target(rec.text)
            if target is None or not _inside_box(rec.points[0], cluster):
                continue
            tag = _nearest_tag(rec.points[0], layout_places, layout_search)
            if tag is not None:
                panels.setdefault(tag, (target, "layout"))
    for tag, target in pid_callouts.items():
        panels.setdefault(tag, (target, "pid"))
    panel_at = {}
    for tag, p in positions.items():
        name = normalize_panel(tag)
        if name is not None:
            panel_at[name] = p
    regions = room_regions(layout, cluster) if layout is not None else room_regions(pid)

    rows, items = [], []
    for tag in sorted(set(loads) | set(panels)):
        texts = sorted(loads.get(tag, []), key=lambda item: item[:2])
        powers = [(d, f["kw"]) for d, _h, f in texts if f["kw"] is not None]
        kw = powers[0][1] if powers else None
        if len({value for _d, value in powers}) > 1:
            items.append(
                {
                    "tag": tag,
                    "item": "conflicting_power",
                    "detail": ", ".join(f"{value:g} kW" for _d, value in powers),
                }
            )
        phases = next((f["phases"] for _d, _h, f in texts if f["phases"]), None)
        voltage = next((f["voltage"] for _d, _h, f in texts if f["voltage"]), None)
        panel, panel_source = panels.get(tag, (None, None))
        row = {
            "tag": tag,
            "room": "",
            "at": None,
            "kw": kw,
            "phases": phases,
            "voltage": voltage,
            "neutral": any(f["neutral"] for _d, _h, f in texts),
            "vfd": any(f["vfd"] for _d, _h, f in texts),
            "panel": panel,
            "panel_source": panel_source,
            "length_m": None,
            "allowance": allowance,
            "cable_m": None,
            "section": section_for(kw, phases, rules),
            "package": None,
        }
        if kw is None:
            items.append(
                {
                    "tag": tag,
                    "item": "missing_power",
                    "detail": "no power is stated near the tag on the P&ID",
                }
            )
        if panel is None:
            items.append(
                {
                    "tag": tag,
                    "item": "missing_panel",
                    "detail": "no wiring callout names a panel for this tag",
                }
            )
        if rules and kw is not None and row["section"] is None:
            items.append(
                {"tag": tag, "item": "no_section_rule", "detail": f"no rule covers {kw:g} kW"}
            )
        if layout is None:
            items.append(
                {
                    "tag": tag,
                    "item": "no_layout",
                    "detail": "no layout given: the P&ID is not a length source for cables",
                }
            )
            if pid_places.get(tag):
                row["room"] = room_at(regions, pid_places[tag][0])
        else:
            here = positions.get(tag)
            if here is None:
                items.append(
                    {
                        "tag": tag,
                        "item": "tag_ambiguous" if tag in ambiguous else "tag_not_on_layout",
                        "detail": "the load's tag cannot be placed on the layout",
                    }
                )
            else:
                row["at"] = here
                row["room"] = room_at(regions, here)
            if panel is not None and panel not in panel_at:
                items.append(
                    {
                        "tag": tag,
                        "item": "panel_not_on_layout",
                        "detail": f"{panel} is not written once on the layout",
                    }
                )
            if here is not None and panel in panel_at:
                row["length_m"] = manhattan(here, panel_at[panel]) * layout_unit["m_per_unit"]
                row["cable_m"] = roundup_m(row["length_m"], allowance)
        rows.append(row)

    stated = {
        (normalize_panel(r["tag"]) or r["tag"]): r["tag"] for r in rows if r["kw"] is not None
    }
    for row in rows:
        owner = stated.get(row["panel"]) if row["panel"] else None
        if owner is not None and owner != row["tag"]:
            row["package"] = owner

    def counted(row: dict) -> float:
        return row["kw"] if row["kw"] is not None and row["package"] is None else 0.0

    summary = {}
    for name, field in (("by_room", "room"), ("by_panel", "panel")):
        summary[name] = [
            {
                "key": key,
                "loads": sum(1 for r in rows if (r[field] or "") == key),
                "kw": sum(counted(r) for r in rows if (r[field] or "") == key),
                "cable_m": sum(r["cable_m"] or 0 for r in rows if (r[field] or "") == key),
            }
            for key in sorted({row[field] or "" for row in rows})
        ]
    units = {"pid": unit_of(pid), "layout": layout_unit}
    warnings = [unit["warning"] for unit in units.values() if unit and unit["warning"]]
    if orphans:
        warnings.append(f"electrical text(s) near no tag, not used: {', '.join(orphans)}")
    return {
        "kind": "cable",
        "rows": rows,
        "open_items": items,
        "summary": summary,
        "totals": {
            "loads": len(rows),
            "known_kw": sum(counted(r) for r in rows),
            "cable_m": sum(r["cable_m"] or 0 for r in rows),
            "open_items": len(items),
            "packages": sum(1 for r in rows if r["package"]),
        },
        "units": units,
        "warnings": warnings,
        "method": {
            "sources": {"pid": pid.source, "layout": layout.source if layout else None},
            "allowance": allowance,
            "search": {"pid": pid_search, "layout": layout_search},
            "section_rules": [dict(rule) for rule in rules],
            "rooms": _rooms_mode(regions),
        },
    }
