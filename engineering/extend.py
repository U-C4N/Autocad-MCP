"""Plan an EXTEND of a LINE or ARC to a boundary, without a CAD engine.

``entity_extend`` was LINE+LINE only on the headless engine, and on the live engine it sent
``_EXTEND`` and reported success whatever happened: measured on AutoCAD 2026, a line stopping short
of a boundary came back unchanged. Both engines now apply this one plan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from engineering.corners import SourceArc, SourceCircle, SourceLine
from engineering.tangent import Point
from engineering.trim import EPS, Source, _angle, _cutter_hits


@dataclass
class ExtendPlan:
    kind: str  # "line" | "arc"
    p: Point | None = None  # line: new start
    q: Point | None = None  # line: new end
    a0: float | None = None  # arc: new CCW start angle, degrees
    a1: float | None = None
    end: str = "end"  # which end moved: "start" | "end"
    to: Point | None = None


def _ref_point(boundary: Source) -> Point:
    if isinstance(boundary, SourceLine):
        return ((boundary.p[0] + boundary.q[0]) / 2, (boundary.p[1] + boundary.q[1]) / 2)
    return boundary.c


def _end_order(near_start: bool, pick: Point | None) -> list[str]:
    order = ["start", "end"] if near_start else ["end", "start"]
    return order[:1] if pick is not None else order  # an explicit pick names the end; no fallback


def plan_extend(target: Source, boundary: Source, pick: Point | None = None) -> ExtendPlan:
    """Extend a LINE or ARC to meet ``boundary`` (an infinite line, or a whole circle).

    The end that moves is the one nearest ``pick``; without a pick it is the end nearest the boundary,
    and the other end is tried if the first has nothing to reach. An intersection behind the chosen end
    is not an extension and is never used.
    """
    if isinstance(target, SourceCircle):
        raise ValueError("a CIRCLE has no end to extend")
    hits = _cutter_hits(target, boundary)
    if not hits:
        if isinstance(target, SourceLine) and isinstance(boundary, SourceLine):
            raise ValueError("target and boundary are parallel; no extension possible")
        raise ValueError("target and boundary do not intersect")
    ref = pick if pick is not None else _ref_point(boundary)

    if isinstance(target, SourceLine):
        length = math.dist(target.p, target.q)
        ux, uy = (target.q[0] - target.p[0]) / length, (target.q[1] - target.p[1]) / length
        ts = [(h, (h[0] - target.p[0]) * ux + (h[1] - target.p[1]) * uy) for h in hits]
        for which in _end_order(math.dist(target.p, ref) <= math.dist(target.q, ref), pick):
            if which == "end":
                cand = [(h, t) for h, t in ts if t > length + EPS]
                best = min(cand, key=lambda ht: ht[1], default=None)
                if best:
                    return ExtendPlan("line", p=target.p, q=best[0], end="end", to=best[0])
            else:
                cand = [(h, t) for h, t in ts if t < -EPS]
                best = max(cand, key=lambda ht: ht[1], default=None)
                if best:
                    return ExtendPlan("line", p=best[0], q=target.q, end="start", to=best[0])
        raise ValueError("the boundary lies behind the end of the target; nothing to extend to")

    assert isinstance(target, SourceArc)
    sweep = (target.a1 - target.a0) % 360.0 or 360.0
    start = (
        target.c[0] + target.r * math.cos(math.radians(target.a0)),
        target.c[1] + target.r * math.sin(math.radians(target.a0)),
    )
    stop = (
        target.c[0] + target.r * math.cos(math.radians(target.a1)),
        target.c[1] + target.r * math.sin(math.radians(target.a1)),
    )
    for which in _end_order(math.dist(start, ref) <= math.dist(stop, ref), pick):
        if which == "end":
            cand = sorted(((_angle(target.c, h) - target.a0) % 360.0, h) for h in hits)
            cand = [(s, h) for s, h in cand if s > sweep + 1e-7]
            if cand:
                s, h = cand[0]
                return ExtendPlan("arc", a0=target.a0, a1=target.a0 + s, end="end", to=h)
        else:
            cand = sorted(((target.a0 - _angle(target.c, h)) % 360.0, h) for h in hits)
            cand = [(s, h) for s, h in cand if s > 1e-7 and sweep + s < 360.0 + 1e-7]
            if cand:
                s, h = cand[0]
                return ExtendPlan("arc", a0=target.a0 - s, a1=target.a1, end="start", to=h)
    raise ValueError("the boundary lies behind the end of the target; nothing to extend to")
