"""Exterior dimension chains: openings, walls, overall - three rows per side.

An architectural plan carries its dimensions outside the building, in rows
parallel to each facade, nearest first:

1. **openings** - the facade's extremes and both jambs of every opening in the
   exterior wall on that side;
2. **walls** - the extremes and both faces of every wall that meets that
   exterior wall, so the chain reads wall thickness, clear room width, wall
   thickness, ...;
3. **overall** - the building's extreme faces.

The "exterior wall on a side" is every axis segment parallel to that side
whose outer face no other parallel segment overlapping it lies beyond. Points
come from the wall engine's own faces, so a chain can never disagree with the
outline it measures.

ISO 129-1: each measurement appears once. A row whose points are exactly those
of a farther row (a facade with no opening, a side no wall meets) is not
emitted; the rows that are keep their places, so the overall row is always at
the same distance.

Offsets are paper millimetres times the plot-scale denominator: the first row
10 mm from the building and 8 mm between rows. Those two numbers are this
repository's drafting defaults, not standard values; pass ``first_offset`` /
``step`` in drawing units to override them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from engineering.arch.model import Wall, segment_of
from engineering.arch.walls import face_offsets, wall_geometry, wall_segments
from engineering.mech.primitives import DimIntent

SIDES: tuple[str, ...] = ("bottom", "top", "left", "right")
ROWS: tuple[str, ...] = ("openings", "walls", "overall")
FIRST_OFFSET_PAPER_MM = 10.0
STEP_PAPER_MM = 8.0
_TOL = 1.0


def _positive(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: expected a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{where}: must be a finite number above zero, got {value!r}")
    return number


def _sides(sides) -> tuple[str, ...]:
    if isinstance(sides, str) or not isinstance(sides, (list, tuple)) or not sides:
        raise ValueError(f"sides: name at least one of {', '.join(SIDES)}, got {sides!r}")
    chosen: list[str] = []
    for i, side in enumerate(sides):
        if side not in SIDES:
            raise ValueError(f"sides[{i}]: {side!r}; sides are {', '.join(SIDES)}")
        if side in chosen:
            raise ValueError(f"sides[{i}]: {side!r} is named twice")
        chosen.append(side)
    return tuple(chosen)


def _unit(a, b) -> tuple[float, float]:
    length = math.dist(a, b)
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _exterior(walls: Sequence[Wall], ai: int, sign: float) -> list[dict]:
    """The segments parallel to a side whose outer face nothing parallel lies beyond."""
    ci = 1 - ai
    parallel: list[dict] = []
    for wall in walls:
        left, right = face_offsets(wall)
        for index, (a, b) in enumerate(wall_segments(wall)):
            u = _unit(a, b)
            if abs(u[ci]) > 1e-9:
                continue
            n = (-u[1], u[0])
            faces = (a[ci] + n[ci] * left, a[ci] + n[ci] * right)
            parallel.append(
                {
                    "wall": wall,
                    "index": index,
                    "a": a,
                    "b": b,
                    "span": (min(a[ai], b[ai]), max(a[ai], b[ai])),
                    "outer": min(faces) if sign < 0 else max(faces),
                }
            )
    exterior = []
    for item in parallel:
        lo, hi = item["span"]
        covered = any(
            other is not item
            and min(hi, other["span"][1]) - max(lo, other["span"][0]) > _TOL
            and (
                other["outer"] < item["outer"] - _TOL
                if sign < 0
                else other["outer"] > item["outer"] + _TOL
            )
            for other in parallel
        )
        if not covered:
            exterior.append(item)
    return exterior


def _inside(point, item: dict) -> bool:
    a, b = item["a"], item["b"]
    left, right = face_offsets(item["wall"])
    u = _unit(a, b)
    s = (point[0] - a[0]) * u[0] + (point[1] - a[1]) * u[1]
    d = (point[0] - a[0]) * -u[1] + (point[1] - a[1]) * u[0]
    return -_TOL <= s <= math.dist(a, b) + _TOL and right - _TOL <= d <= left + _TOL


def _points(values: set[float]) -> list[float]:
    out: list[float] = []
    for value in sorted(values):
        if not out or value - out[-1] > 1e-6:
            out.append(value)
    return out


def exterior_chains(
    walls,
    openings=(),
    *,
    sides=("bottom", "left"),
    first_offset=None,
    step=None,
    scale=50,
) -> tuple[DimIntent, ...]:
    """Linear ``DimIntent`` rows outside each named side, nearest first.

    ``feature`` names the row as ``"<side>:<row>"`` (``"bottom:openings"``);
    ``text_at`` is the point the dimension line passes through.
    """
    walls = list(walls)
    openings = list(openings)
    chosen = _sides(sides)
    factor = _positive(scale, "scale")
    first = (
        FIRST_OFFSET_PAPER_MM * factor
        if first_offset is None
        else _positive(first_offset, "first_offset")
    )
    gap = STEP_PAPER_MM * factor if step is None else _positive(step, "step")
    geometry = wall_geometry(walls, openings, poche=False)
    if not geometry.faces:
        raise ValueError("walls: there is no wall to dimension")
    xs = [p[0] for line in geometry.faces for p in (line.p1, line.p2)]
    ys = [p[1] for line in geometry.faces for p in (line.p1, line.p2)]
    box = {"bottom": min(ys), "top": max(ys), "left": min(xs), "right": max(xs)}
    skipped = {entry["element"] for entry in geometry.omitted}
    by_id = {wall.id: wall for wall in walls}

    intents: list[DimIntent] = []
    for side in chosen:
        ai = 0 if side in ("bottom", "top") else 1
        sign = -1.0 if side in ("bottom", "left") else 1.0
        edge = box[side]
        lo, hi = (box["left"], box["right"]) if ai == 0 else (box["bottom"], box["top"])
        exterior = _exterior(walls, ai, sign)
        hosts = {(item["wall"].id, item["index"]) for item in exterior}

        jambs = {lo, hi}
        for opening in openings:
            if opening.id in skipped:
                continue
            wall = by_id[opening.wall]
            index, along = segment_of(wall, float(opening.offset))
            if (wall.id, index) not in hosts:
                continue
            a, b = wall_segments(wall)[index]
            u = _unit(a, b)
            jambs.add(a[ai] + u[ai] * along)
            jambs.add(a[ai] + u[ai] * (along + float(opening.width)))

        faces = {lo, hi}
        for wall in walls:
            left, right = face_offsets(wall)
            for a, b in wall_segments(wall):
                u = _unit(a, b)
                if abs(u[1 - ai]) <= 1e-9:
                    continue
                n = (-u[1], u[0])
                for end in (a, b):
                    if any(_inside(end, item) for item in exterior):
                        faces.add(end[ai] + n[ai] * left)
                        faces.add(end[ai] + n[ai] * right)

        rows = [_points(jambs), _points(faces), _points({lo, hi})]
        for r, points in enumerate(rows):
            if any(points == later for later in rows[r + 1 :]):
                continue
            offset = edge + sign * (first + r * gap)
            for p, q in zip(points, points[1:], strict=False):
                if ai == 0:
                    p1, p2, at = (p, edge), (q, edge), ((p + q) / 2.0, offset)
                else:
                    p1, p2, at = (edge, p), (edge, q), (offset, (p + q) / 2.0)
                intents.append(DimIntent("linear", p1, p2, at, feature=f"{side}:{ROWS[r]}"))
    return tuple(intents)
