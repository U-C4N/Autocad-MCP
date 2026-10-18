"""Plan a FILLET between any two of LINE, ARC and CIRCLE, without a CAD engine.

``entity_fillet`` used to refuse everything but LINE+LINE ("V2 will add LINE+ARC"). The plan is
pure geometry so both engines can apply it: the fillet arc (centre, radius, CCW angles) and the
edits that trim - or extend - each source entity to its tangent point.

A CIRCLE is never trimmed (AutoCAD leaves it whole too). A LINE or ARC keeps the end farther
from the *other* tangent point, which is the side a drafter keeps at a convex or concave corner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from engineering.tangent import Circ, Line, Point, choose, fillet_arc, solve_ttr


@dataclass(frozen=True)
class SourceLine:
    p: Point
    q: Point


@dataclass(frozen=True)
class SourceArc:
    c: Point
    r: float
    a0: float  # CCW degrees
    a1: float


@dataclass(frozen=True)
class SourceCircle:
    c: Point
    r: float


Source = SourceLine | SourceArc | SourceCircle


@dataclass
class Edit:
    """New geometry for source ``index`` (0 or 1)."""

    index: int
    kind: str  # "line" | "arc"
    p: Point | None = None  # line: new start
    q: Point | None = None  # line: new end
    a0: float | None = None  # arc: new CCW start angle, degrees
    a1: float | None = None


@dataclass
class FilletPlan:
    center: Point
    radius: float
    start_deg: float
    end_deg: float
    tangent_a: Point
    tangent_b: Point
    extended: bool
    edits: list[Edit] = field(default_factory=list)


def _curve(src: Source):
    if isinstance(src, SourceLine):
        return Line(src.p, src.q)
    if isinstance(src, SourceArc):
        return Circ(src.c, src.r, src.a0, src.a1)
    return Circ(src.c, src.r)


def _angle(c: Point, p: Point) -> float:
    return math.degrees(math.atan2(p[1] - c[1], p[0] - c[0]))


def _edit(index: int, src: Source, tangent: Point, other_tangent: Point) -> Edit | None:
    if isinstance(src, SourceCircle):
        return None
    if isinstance(src, SourceLine):
        keep = src.p if math.dist(src.p, other_tangent) > math.dist(src.q, other_tangent) else src.q
        if keep == src.p:
            return Edit(index, "line", p=keep, q=tangent)
        return Edit(index, "line", p=tangent, q=keep)
    start = (
        src.c[0] + src.r * math.cos(math.radians(src.a0)),
        src.c[1] + src.r * math.sin(math.radians(src.a0)),
    )
    end = (
        src.c[0] + src.r * math.cos(math.radians(src.a1)),
        src.c[1] + src.r * math.sin(math.radians(src.a1)),
    )
    t_ang = _angle(src.c, tangent)
    if math.dist(start, other_tangent) > math.dist(end, other_tangent):
        return Edit(index, "arc", a0=src.a0, a1=t_ang)  # keep the start, cut back / extend the end
    return Edit(index, "arc", a0=t_ang, a1=src.a1)


def plan_fillet(
    a: Source, b: Source, radius: float, near: Point | None = None, trim: bool = True
) -> FilletPlan:
    if radius <= 0:
        raise ValueError("a fillet between an arc or circle needs a radius > 0")
    sol = choose(solve_ttr(_curve(a), _curve(b), radius), near)
    start, end = fillet_arc(sol, radius)
    plan = FilletPlan(
        center=sol.center,
        radius=radius,
        start_deg=start,
        end_deg=end,
        tangent_a=sol.ta,
        tangent_b=sol.tb,
        extended=(sol.ext_a + sol.ext_b) > 1e-9,
    )
    if trim:
        for idx, (src, t, other) in enumerate(((a, sol.ta, sol.tb), (b, sol.tb, sol.ta))):
            e = _edit(idx, src, t, other)
            if e is not None:
                plan.edits.append(e)
    return plan
