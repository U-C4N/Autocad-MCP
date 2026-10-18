"""Plan a TRIM of a LINE, ARC or CIRCLE against a cutting edge, without a CAD engine.

``entity_trim`` used to take LINE+LINE only on the headless engine, and on the live engine it
sent ``_TRIM`` to AutoCAD and reported success whatever happened: measured on AutoCAD 2026, a
line crossed by a cutter came back unchanged (the keyword sequence no longer matches TRIM's
prompts). The plan is pure geometry, so both engines apply the same edit.

Contract (unchanged): the piece of the target that contains the ``keep`` point is kept; every
other piece is dropped. The cutter is an infinite line, or the whole circle of an ARC / CIRCLE
(AutoCAD's "extend" edge mode). The target is never extended: a cutter that crosses only the
target's *line* beyond its end is refused, because that is EXTEND's job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from engineering.corners import SourceArc, SourceCircle, SourceLine
from engineering.tangent import (
    Point,
    intersect_circle_circle,
    intersect_line_circle,
    intersect_line_line,
)

EPS = 1e-9

Source = SourceLine | SourceArc | SourceCircle


@dataclass
class TrimPlan:
    kind: str  # "line" | "arc" | "circle_to_arc"
    p: Point | None = None  # line: new start
    q: Point | None = None  # line: new end
    a0: float | None = None  # arc: new CCW start angle, degrees
    a1: float | None = None
    cut_points: tuple[Point, ...] = ()


def _cutter_hits(target: Source, cutter: Source) -> list[Point]:
    """Intersections of the target's own curve with the cutter (line infinite, arc/circle whole)."""
    if isinstance(target, SourceLine):
        from engineering.tangent import Line

        tl = Line(target.p, target.q)
        if isinstance(cutter, SourceLine):
            return intersect_line_line(tl, Line(cutter.p, cutter.q))
        return intersect_line_circle(tl, cutter.c, cutter.r)
    if isinstance(cutter, SourceLine):
        from engineering.tangent import Line

        return intersect_line_circle(Line(cutter.p, cutter.q), target.c, target.r)
    return intersect_circle_circle(target.c, target.r, cutter.c, cutter.r)


def _angle(c: Point, p: Point) -> float:
    return math.degrees(math.atan2(p[1] - c[1], p[0] - c[0]))


def _pieces(cuts: list[float], lo: float, hi: float) -> list[tuple[float, float]]:
    pts = [lo, *sorted(cuts), hi]
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1) if pts[i + 1] - pts[i] > EPS]


def plan_trim(target: Source, cutter: Source, keep: Point) -> TrimPlan:
    hits = _cutter_hits(target, cutter)
    if isinstance(target, SourceLine):
        length = math.dist(target.p, target.q)
        ux, uy = (target.q[0] - target.p[0]) / length, (target.q[1] - target.p[1]) / length
        params = []
        for h in hits:
            t = (h[0] - target.p[0]) * ux + (h[1] - target.p[1]) * uy
            if -1e-7 <= t <= length + 1e-7:
                params.append(min(max(t, 0.0), length))
        params = sorted({round(t, 9) for t in params})
        params = [t for t in params if EPS < t < length - EPS] or params
        if not params:
            raise ValueError(
                "target and cutter do not intersect on the target"
                if hits
                else "target and cutter are parallel or do not intersect"
            )
        tk = min(max((keep[0] - target.p[0]) * ux + (keep[1] - target.p[1]) * uy, 0.0), length)
        lo, hi = next((a, b) for a, b in _pieces(params, 0.0, length) if a - 1e-9 <= tk <= b + 1e-9)
        return TrimPlan(
            "line",
            p=(target.p[0] + ux * lo, target.p[1] + uy * lo),
            q=(target.p[0] + ux * hi, target.p[1] + uy * hi),
            cut_points=tuple((target.p[0] + ux * t, target.p[1] + uy * t) for t in params),
        )

    if isinstance(target, SourceArc):
        sweep = (target.a1 - target.a0) % 360.0 or 360.0
        params = []
        for h in hits:
            s = (_angle(target.c, h) - target.a0) % 360.0
            if s <= sweep + 1e-7:
                params.append(min(s, sweep))
        params = sorted({round(s, 9) for s in params})
        params = [s for s in params if EPS < s < sweep - EPS] or params
        if not params:
            raise ValueError("target and cutter do not intersect on the target")
        sk = (_angle(target.c, keep) - target.a0) % 360.0
        if sk > sweep:  # keep point off the arc: use the nearer end
            sk = sweep if (sk - sweep) < (360.0 - sk) else 0.0
        lo, hi = next((a, b) for a, b in _pieces(params, 0.0, sweep) if a - 1e-9 <= sk <= b + 1e-9)
        return TrimPlan("arc", a0=target.a0 + lo, a1=target.a0 + hi, cut_points=tuple(hits))

    # CIRCLE: needs two cuts to leave an arc
    angs = sorted({round(_angle(target.c, h) % 360.0, 9) for h in hits})
    if len(angs) < 2:
        raise ValueError(
            "a circle needs the cutter to cross it twice to leave an arc "
            f"(it crosses {len(angs)} time{'s' if len(angs) != 1 else ''})"
        )
    sk = _angle(target.c, keep) % 360.0
    for i, a in enumerate(angs):
        b = angs[(i + 1) % len(angs)]
        span = (b - a) % 360.0
        if (sk - a) % 360.0 <= span + 1e-9:
            return TrimPlan("circle_to_arc", a0=a, a1=b, cut_points=tuple(hits))
    raise ValueError("keep point does not select a piece of the circle")
