"""The three architectural critique focuses (spec §9).

Every check is pure over one index built once per critique run and shared
through ``run_critique``'s context, the way the P&ID and mechanical focuses
are. A drawing with no ``ACADMCP_ARCH`` record on it yields no issue at all: a
mechanical sheet, a P&ID or a foreign plan of plain lines must not be scored
against architectural conventions, and ``focus=None`` has to stay cheap for
them.

What the index reads, and why only that: every plan record this server writes
(a wall on its outline, an opening on its leaf or frame, a room on its label)
lands on a layer of ``ARCH_ROLE_LAYER``, so XDATA is read on those layers only,
every entity of them (paged, not the first 200). A record carried by several
entities is kept once, by kind and id, with every handle that carries it. The
room faces come from ``rooms_detect`` on the wall layer - the same reader
``arch_rooms_detect`` runs, which closes door and window gaps from the opening
records - so the focus and the tool cannot disagree about what a room is.

* ``arch_room_unlabelled`` (warning): a face of at least ``rooms_detect``'s
  minimum area (1 m²) with no room record whose label point lies inside it.
* ``arch_wall_gap`` (error): a free wall end that comes within ``GAP_MM``
  (50 mm) of another wall - its end or its body - without joining it
  (``JOIN_TOL_MM``, 1 mm, the wall engine's ``join_tol``). The room leaks, and
  its area is measured through the gap. One issue per pair of walls.
* ``arch_opening_clash`` (error): two openings overlapping on one wall, an
  opening running past the end of its wall's axis or across an axis vertex,
  and an opening whose host wall has no record on the drawing.

The two thresholds are declared by the design spec, not taken from a
standard.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from engineering.plan_spec import Issue

from .layers import ARCH_ROLE_LAYER
from .model import APP_ID, Opening, Room, Stair, Wall, axis_segments, decode, from_payload

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

ARCH_FOCUSES = ("arch_room_unlabelled", "arch_wall_gap", "arch_opening_clash")
_KEY = "arch_index"

#: Two wall ends closer than this without joining are a gap (spec §9).
GAP_MM = 50.0
#: Within this a wall end joins the wall it touches - the wall engine's join_tol.
JOIN_TOL_MM = 1.0
#: Below this, two lengths along a wall are the same length.
EPS = 1e-9

_BUCKETS = {Wall: "walls", Opening: "openings", Stair: "stairs", Room: "rooms"}


# ── the index ───────────────────────────────────────────────────────────────


async def build_index(backend: AutoCADBackend) -> dict:
    """One read of the drawing: every ACADMCP_ARCH record, and the room faces.

    ``{"walls" | "openings" | "stairs" | "rooms": {id: {"obj", "handles"}},
    "faces": [{"loop", "area", ...}]}``. The faces are read only when there is
    a wall record to bound them; a ``rooms_detect`` refusal (no wall layer)
    reads as no faces.
    """
    from engineering.sheet.bom import all_entities

    index: dict = {"walls": {}, "openings": {}, "stairs": {}, "rooms": {}, "faces": []}
    for layer in sorted(set(ARCH_ROLE_LAYER.values())):
        try:
            entities = await all_entities(backend, layer=layer)
        except Exception:
            continue
        for entity in entities:
            try:
                raw = await backend.entity_get_xdata(entity.handle, APP_ID)
            except Exception:
                continue
            values = (raw.get("xdata") or {}).get(APP_ID) or []
            if not values:
                continue
            try:
                obj = from_payload(decode(values))
            except (ValueError, KeyError, TypeError):
                # Not ours, or corrupt: the codec refused it, and guessing at
                # it here would undo that.
                continue
            bucket = index[_BUCKETS[type(obj)]]
            entry = bucket.setdefault(obj.id, {"obj": obj, "handles": []})
            entry["handles"].append(entity.handle)
    if index["walls"]:
        from engineering.arch.rooms import rooms_detect

        try:
            detected = await rooms_detect(backend, layers=[ARCH_ROLE_LAYER["wall"]])
        except ValueError:
            detected = {"rooms": []}
        index["faces"] = list(detected.get("rooms") or [])
    return index


def has_arch_content(index: dict) -> bool:
    return any(index[bucket] for bucket in ("walls", "openings", "stairs", "rooms"))


# ── geometry ────────────────────────────────────────────────────────────────


def _band(wall: Wall) -> tuple[float, float]:
    """Where the two faces lie along the segment's left normal, low to high."""
    t = float(wall.thickness)
    if wall.justification == "left":
        return (0.0, t)
    if wall.justification == "right":
        return (-t, 0.0)
    return (-t / 2.0, t / 2.0)


def body_distance(point, a, b, band: tuple[float, float]) -> float:
    """Distance from ``point`` to the body of one straight wall segment.

    The body is the rectangle between the segment's two faces, from ``a`` to
    ``b``: 0 inside it or on its edge.
    """
    length = math.dist(a, b)
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    dx, dy = point[0] - a[0], point[1] - a[1]
    along = dx * ux + dy * uy
    across = -dx * uy + dy * ux
    out_along = max(0.0 - along, 0.0, along - length)
    out_across = max(band[0] - across, 0.0, across - band[1])
    return math.hypot(out_along, out_across)


def _ends(wall: Wall) -> list[tuple[str, tuple[float, float], int]]:
    """The free ends of a wall: (which end, the point, the segment it belongs to)."""
    if wall.closed:
        return []
    last = len(wall.axis) - 2
    return [("start", wall.axis[0], 0), ("end", wall.axis[-1], last)]


def wall_gaps(
    walls: Sequence[Wall], *, gap: float = GAP_MM, tol: float = JOIN_TOL_MM
) -> list[dict]:
    """Free wall ends within ``gap`` of another wall that do not join it.

    An end joins a wall when it lies within ``tol`` of that wall's body (an L
    corner's shared end, a T stem ending in the crossing wall). The walls an
    end is measured against are every other wall, and its own wall's segments
    other than the one it ends and that segment's neighbours - a wall that
    nearly closes on itself leaks too. One row per pair of walls, the smallest
    gap kept: ``{"walls": (a, b), "end": "W2.start", "other": "W1", "gap_mm", "at"}``.
    """
    found: dict[frozenset, dict] = {}
    for wall in walls:
        for end, point, own in _ends(wall):
            best: tuple[float, str] | None = None
            joined = False
            for other in walls:
                band = _band(other)
                segments = axis_segments(other)
                for index, (a, b) in enumerate(segments):
                    if other.id == wall.id and abs(index - own) <= 1:
                        continue
                    distance = body_distance(point, a, b, band)
                    if distance <= tol:
                        joined = True
                        break
                    if best is None or distance < best[0]:
                        best = (distance, other.id)
                if joined:
                    break
            if joined or best is None or best[0] > gap:
                continue
            pair = frozenset((wall.id, best[1]))
            row = {
                "walls": tuple(sorted(pair)) if len(pair) == 2 else (wall.id, wall.id),
                "end": f"{wall.id}.{end}",
                "other": best[1],
                "gap_mm": round(best[0], 3),
                "at": (float(point[0]), float(point[1])),
            }
            if pair not in found or row["gap_mm"] < found[pair]["gap_mm"]:
                found[pair] = row
    return sorted(found.values(), key=lambda row: row["walls"])


def opening_clashes(walls: Sequence[Wall], openings: Sequence[Opening]) -> list[dict]:
    """Every opening its wall cannot host: ``{"openings": (ids), "wall", "reason"}``.

    The same rules ``validate_openings`` refuses before a draw, collected
    rather than raised - the drawing already exists, and every clash on it is
    reported, not only the first.
    """
    by_id = {wall.id: wall for wall in walls}
    rows: list[dict] = []
    spans: dict[str, list[tuple[float, float, str]]] = {}
    for opening in openings:
        wall = by_id.get(opening.wall)
        if wall is None:
            rows.append(
                {
                    "openings": (opening.id,),
                    "wall": opening.wall,
                    "reason": f"its host wall {opening.wall!r} has no record on this drawing",
                }
            )
            continue
        start = float(opening.offset)
        end = start + float(opening.width)
        runs = []
        run = 0.0
        for a, b in axis_segments(wall):
            run += math.dist(a, b)
            runs.append(run)
        total = runs[-1]
        if end > total + EPS:
            rows.append(
                {
                    "openings": (opening.id,),
                    "wall": wall.id,
                    "reason": f"it runs {end - total:g} mm past the end of the wall "
                    f"({start:g}-{end:g} on an axis {total:g} long)",
                }
            )
        for vertex in runs[:-1]:
            if start + EPS < vertex < end - EPS:
                rows.append(
                    {
                        "openings": (opening.id,),
                        "wall": wall.id,
                        "reason": f"it runs across the axis vertex at {vertex:g} "
                        f"({start:g}-{end:g})",
                    }
                )
        for other_start, other_end, other_id in spans.get(wall.id, ()):
            if start < other_end - EPS and other_start < end - EPS:
                rows.append(
                    {
                        "openings": (other_id, opening.id),
                        "wall": wall.id,
                        "reason": f"they overlap ({other_start:g}-{other_end:g} and "
                        f"{start:g}-{end:g})",
                    }
                )
        spans.setdefault(wall.id, []).append((start, end, opening.id))
    return rows


def point_in_loop(point, loop) -> bool:
    """Even-odd ray cast; a point on an edge counts as inside."""
    x, y = float(point[0]), float(point[1])
    pts = [(float(p[0]), float(p[1])) for p in loop]
    inside = False
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if (
            abs(cross) <= 1e-9 * max(1.0, math.dist((x1, y1), (x2, y2)))
            and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9
            and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9
        ):
            return True
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def unlabelled_faces(faces: Sequence[dict], rooms: Sequence[Room]) -> list[dict]:
    """The faces no room record's label point lies in."""
    return [
        face for face in faces if not any(point_in_loop(room.at, face["loop"]) for room in rooms)
    ]


# ── the three checks ────────────────────────────────────────────────────────


def _objs(index: dict, bucket: str) -> list:
    return [entry["obj"] for entry in index[bucket].values()]


def _handles(index: dict, bucket: str, ids) -> list[str]:
    return [h for i in ids if i in index[bucket] for h in index[bucket][i]["handles"]]


def _room_unlabelled(index: dict) -> list[Issue]:
    out: list[Issue] = []
    for face in unlabelled_faces(index["faces"], _objs(index, "rooms")):
        centroid = face.get("centroid") or face["loop"][0]
        out.append(
            Issue(
                "warning",
                "arch_room_unlabelled",
                f"A closed face of {float(face['area']) / 1.0e6:.2f} m² around "
                f"({float(centroid[0]):.0f}, {float(centroid[1]):.0f}) carries no room label; "
                "label it with arch_room so its measured area reaches the room schedule.",
                [],
                {"area_mm2": float(face["area"]), "centroid": [float(c) for c in centroid]},
            )
        )
    return out


def _wall_gap(index: dict) -> list[Issue]:
    out: list[Issue] = []
    for row in wall_gaps(_objs(index, "walls")):
        a, b = row["walls"]
        out.append(
            Issue(
                "error",
                "arch_wall_gap",
                f"Wall end {row['end']} stops {row['gap_mm']:g} mm short of wall "
                f"{row['other']} without joining it: the room leaks and its area is measured "
                "through the gap. Extend the wall to the joint (arch_wall) or move it clear.",
                _handles(index, "walls", sorted({a, b})),
                {"walls": [a, b], "gap_mm": row["gap_mm"], "at": list(row["at"])},
            )
        )
    return out


def _opening_clash(index: dict) -> list[Issue]:
    out: list[Issue] = []
    for row in opening_clashes(_objs(index, "walls"), _objs(index, "openings")):
        names = " and ".join(row["openings"])
        noun = "Openings" if len(row["openings"]) > 1 else "Opening"
        out.append(
            Issue(
                "error",
                "arch_opening_clash",
                f"{noun} {names} on wall {row['wall']}: {row['reason']}.",
                _handles(index, "openings", row["openings"]),
                {"openings": list(row["openings"]), "wall": row["wall"]},
            )
        )
    return out


_CHECKS = {
    "arch_room_unlabelled": _room_unlabelled,
    "arch_wall_gap": _wall_gap,
    "arch_opening_clash": _opening_clash,
}


def issues_for(focus: str, index: dict) -> list[Issue]:
    if not has_arch_content(index):
        return []
    return _CHECKS[focus](index)


def _make(focus: str):
    async def check(backend: AutoCADBackend, shared: dict) -> list[Issue]:
        index = shared.get(_KEY)
        if index is None:
            index = shared[_KEY] = await build_index(backend)
        return issues_for(focus, index)

    check.needs_shared = True  # type: ignore[attr-defined]
    check.__name__ = f"check_{focus}"
    return check


ARCH_DISPATCH = {focus: _make(focus) for focus in ARCH_FOCUSES}
