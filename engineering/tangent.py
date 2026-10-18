"""Tangent-tangent-radius (TTR) geometry for lines, circles and arcs. Pure maths, no CAD engine.

AutoCAD's ``CIRCLE`` > ``Ttr`` and ``FILLET`` both reduce to one question: where are the centres
of a circle of radius ``r`` that touches two given curves? A line is touched by a circle whose
centre lies ``r`` from it (two offset lines); a circle ``(C, R)`` by a circle whose centre lies
``R + r`` or ``|R - r|`` from ``C``. Intersecting those loci gives every solution; the caller
picks one with a reference point, or the solver prefers the one that needs no extension of the
source entities.

Lines are treated as infinite for tangency (FILLET extends them); ``extension`` reports how far
the tangent point lies outside the real segment or arc, so a caller can tell a corner from a
projection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]

EPS = 1e-9


@dataclass(frozen=True)
class Line:
    p: Point
    q: Point


@dataclass(frozen=True)
class Circ:
    """A circle, or an arc of it: ``a0``/``a1`` are the CCW extent in degrees (``None`` = full circle)."""

    c: Point
    r: float
    a0: float | None = None
    a1: float | None = None


Curve = Line | Circ


@dataclass(frozen=True)
class Solution:
    center: Point
    ta: Point  # tangent point on curve a
    tb: Point  # tangent point on curve b
    ext_a: float  # distance the tangent point lies outside curve a's real extent (0 = on it)
    ext_b: float


def _unit(dx: float, dy: float) -> Point:
    n = math.hypot(dx, dy)
    if n < EPS:
        raise ValueError("degenerate (zero-length) geometry")
    return dx / n, dy / n


def _line_offsets(line: Line, r: float):
    ux, uy = _unit(line.q[0] - line.p[0], line.q[1] - line.p[1])
    nx, ny = -uy, ux
    return [
        Line((line.p[0] + nx * r, line.p[1] + ny * r), (line.q[0] + nx * r, line.q[1] + ny * r)),
        Line((line.p[0] - nx * r, line.p[1] - ny * r), (line.q[0] - nx * r, line.q[1] - ny * r)),
    ]


def _int_line_line(a: Line, b: Line) -> list[Point]:
    d1 = (a.q[0] - a.p[0], a.q[1] - a.p[1])
    d2 = (b.q[0] - b.p[0], b.q[1] - b.p[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < EPS * max(1.0, math.hypot(*d1) * math.hypot(*d2)):
        return []
    t = ((b.p[0] - a.p[0]) * d2[1] - (b.p[1] - a.p[1]) * d2[0]) / den
    return [(a.p[0] + d1[0] * t, a.p[1] + d1[1] * t)]


def _int_line_circle(line: Line, c: Point, rad: float) -> list[Point]:
    ux, uy = _unit(line.q[0] - line.p[0], line.q[1] - line.p[1])
    fx, fy = line.p[0] - c[0], line.p[1] - c[1]
    b = fx * ux + fy * uy
    disc = b * b - (fx * fx + fy * fy - rad * rad)
    if disc < -EPS:
        return []
    s = math.sqrt(max(disc, 0.0))
    ts = [-b] if s < 1e-12 else [-b - s, -b + s]
    return [(line.p[0] + ux * t, line.p[1] + uy * t) for t in ts]


def _int_circle_circle(c1: Point, r1: float, c2: Point, r2: float) -> list[Point]:
    d = math.dist(c1, c2)
    if d < EPS or d > r1 + r2 + 1e-9 or d < abs(r1 - r2) - 1e-9:
        return []
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h = math.sqrt(max(r1 * r1 - a * a, 0.0))
    ux, uy = (c2[0] - c1[0]) / d, (c2[1] - c1[1]) / d
    mx, my = c1[0] + a * ux, c1[1] + a * uy
    if h < 1e-12:
        return [(mx, my)]
    return [(mx - h * uy, my + h * ux), (mx + h * uy, my - h * ux)]


def _locus_radii(circ: Circ, r: float) -> list[float]:
    out = [circ.r + r]
    inner = abs(circ.r - r)
    if inner > EPS:
        out.append(inner)
    return out


def _tangent_point(curve: Curve, center: Point, r: float) -> Point:
    if isinstance(curve, Line):
        ux, uy = _unit(curve.q[0] - curve.p[0], curve.q[1] - curve.p[1])
        t = (center[0] - curve.p[0]) * ux + (center[1] - curve.p[1]) * uy
        return curve.p[0] + ux * t, curve.p[1] + uy * t
    d = math.dist(curve.c, center)
    if d < EPS:
        raise ValueError("concentric circles have no tangent circle")
    ex, ey = (center[0] - curve.c[0]) / d, (center[1] - curve.c[1]) / d
    # collinear centres: the tangent point is the one at distance r from the new centre
    best = None
    for sign in (1.0, -1.0):
        pt = (curve.c[0] + sign * ex * curve.r, curve.c[1] + sign * ey * curve.r)
        err = abs(math.dist(pt, center) - r)
        if best is None or err < best[0]:
            best = (err, pt)
    return best[1]


def _extension(curve: Curve, pt: Point) -> float:
    if isinstance(curve, Line):
        ux, uy = _unit(curve.q[0] - curve.p[0], curve.q[1] - curve.p[1])
        length = math.dist(curve.p, curve.q)
        t = (pt[0] - curve.p[0]) * ux + (pt[1] - curve.p[1]) * uy
        if t < 0:
            return -t
        return max(t - length, 0.0)
    if curve.a0 is None or curve.a1 is None:
        return 0.0
    sweep = (curve.a1 - curve.a0) % 360.0 or 360.0
    ang = math.degrees(math.atan2(pt[1] - curve.c[1], pt[0] - curve.c[0]))
    rel = (ang - curve.a0) % 360.0
    if rel <= sweep:
        return 0.0
    outside = min(rel - sweep, 360.0 - rel)
    return math.radians(outside) * curve.r


def solve_ttr(a: Curve, b: Curve, r: float) -> list[Solution]:
    """Every circle of radius ``r`` tangent to both curves (duplicates removed)."""
    if not (r > EPS):
        raise ValueError("radius must be positive")
    centers: list[Point] = []
    if isinstance(a, Line) and isinstance(b, Line):
        for la in _line_offsets(a, r):
            for lb in _line_offsets(b, r):
                centers += _int_line_line(la, lb)
    elif isinstance(a, Line) or isinstance(b, Line):
        line, circ = (a, b) if isinstance(a, Line) else (b, a)
        for off in _line_offsets(line, r):
            for rad in _locus_radii(circ, r):
                centers += _int_line_circle(off, circ.c, rad)
    else:
        for r1 in _locus_radii(a, r):
            for r2 in _locus_radii(b, r):
                centers += _int_circle_circle(a.c, r1, b.c, r2)
    out: list[Solution] = []
    for c in centers:
        if any(math.dist(c, s.center) < 1e-7 for s in out):
            continue
        try:
            ta, tb = _tangent_point(a, c, r), _tangent_point(b, c, r)
        except ValueError:
            continue
        out.append(Solution(c, ta, tb, _extension(a, ta), _extension(b, tb)))
    return out


def choose(solutions: list[Solution], near: Point | None = None) -> Solution:
    """The caller's pick: nearest centre to ``near``; else the one needing the least extension."""
    if not solutions:
        raise ValueError("no circle of that radius is tangent to both entities")
    if near is not None:
        return min(solutions, key=lambda s: math.dist(s.center, near))
    return min(
        solutions,
        key=lambda s: (
            round(s.ext_a + s.ext_b, 7),
            math.dist(s.center, s.ta) + math.dist(s.center, s.tb),
        ),
    )


def fillet_arc(sol: Solution, r: float) -> tuple[float, float]:
    """CCW (start_deg, end_deg) of the short arc between the two tangent points."""
    a0 = math.degrees(math.atan2(sol.ta[1] - sol.center[1], sol.ta[0] - sol.center[0]))
    a1 = math.degrees(math.atan2(sol.tb[1] - sol.center[1], sol.tb[0] - sol.center[0]))
    ccw = (a1 - a0) % 360.0
    return (a0, a1) if ccw <= 180.0 else (a1, a0)


# Public names for the intersection helpers (used by engineering.trim).
def intersect_line_line(a: Line, b: Line) -> list[Point]:
    return _int_line_line(a, b)


def intersect_line_circle(line: Line, c: Point, radius: float) -> list[Point]:
    return _int_line_circle(line, c, radius)


def intersect_circle_circle(c1: Point, r1: float, c2: Point, r2: float) -> list[Point]:
    return _int_circle_circle(c1, r1, c2, r2)
