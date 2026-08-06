"""Exact area and perimeter for DXF boundary geometry.

Pure arithmetic: no ezdxf, no COM, no I/O. Both backends call the same functions,
so the headless and live engines cannot drift apart on the numbers, and the tests
exercise the real implementation rather than a stand-in.

The thing that makes this exact rather than approximate is the **bulge**. A DXF
polyline edge carries a bulge factor, and a non-zero bulge means that edge is a
circular arc — always circular, never a spline — so the enclosed area is the
shoelace of the vertices plus each arc's signed circular segment, and the walked
length is arc length rather than chord length. Both are closed-form.

Getting this wrong is not a rounding matter. Measured on a 100x100 square with
one edge bulged into an outward semicircle: ignoring bulge reports 10000 against
a true 13926.9908, i.e. **28.2% low**, and a perimeter 12.5% low.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: Max chord deviation (drawing units) when geometry has to be flattened because
#: it is not closed-form — splines, partial ellipses.
DEFAULT_FLATTEN_TOLERANCE = 0.001

#: Above this vertex count the O(n^2) self-intersection test is skipped and the
#: answer is reported as unknown. A slow honest "I did not check" beats both a
#: hang and a confident False.
MAX_SELF_INTERSECT_VERTS = 2000

_EPS = 1e-12


def polygon_area_perimeter(
    vertices: Sequence[tuple[float, float, float]],
    closed: bool,
) -> tuple[float, float]:
    """Exact area and perimeter of a bulge-carrying polygon.

    ``vertices`` are ``(x, y, bulge)``; the bulge belongs to the edge *leaving*
    that vertex, which is the DXF convention.

    ``closed=False`` still returns the area of the implicitly closed region —
    that is what AutoCAD's AREA command does — but the perimeter is the length
    actually walked, without the closing segment. Callers must report the
    assumption rather than let it pass silently.

    Two vertices are not degenerate when they carry bulges: that is how DXF
    stores a circle built from two semicircular arcs, and the shoelace over its
    two points is genuinely 0 while the two circular segments are the entire
    shape. Only zero or one vertex bounds nothing.
    """
    count = len(vertices)
    if count < 2:
        return 0.0, _open_length(vertices)

    shoelace = 0.0
    for i in range(count):
        x1, y1, _ = vertices[i]
        x2, y2, _ = vertices[(i + 1) % count]
        shoelace += x1 * y2 - x2 * y1
    shoelace *= 0.5

    segments = 0.0
    perimeter = 0.0
    limit = count if closed else count - 1
    for i in range(count):
        x1, y1, bulge = vertices[i]
        x2, y2, _ = vertices[(i + 1) % count]
        chord = math.hypot(x2 - x1, y2 - y1)
        if abs(bulge) < _EPS or chord < _EPS:
            if i < limit:
                perimeter += chord
            continue
        theta = 4.0 * math.atan(bulge)  # signed sweep, radians
        radius = chord / (2.0 * math.sin(abs(theta) / 2.0))
        # The circular segment cut off by the chord. Signed: a positive bulge
        # bows the edge outward and adds material, a negative one removes it.
        segments += math.copysign(
            radius * radius / 2.0 * (abs(theta) - math.sin(abs(theta))), theta
        )
        if i < limit:
            perimeter += radius * abs(theta)

    return abs(shoelace + segments), perimeter


def _open_length(vertices: Sequence[tuple[float, float, float]]) -> float:
    return sum(
        math.dist(vertices[i][:2], vertices[i + 1][:2]) for i in range(max(0, len(vertices) - 1))
    )


def is_self_intersecting(vertices: Sequence[tuple[float, float]]) -> bool | None:
    """True when non-adjacent edges of the closed loop cross; None if not checked.

    Worth reporting because the shoelace *cancels* crossed lobes rather than
    failing: a bowtie made of two 2500-unit triangles measures 0.0, which is a
    confident wrong number of exactly the kind this release removes.

    A closed loop may arrive either with or without a repeated closing vertex —
    `ezdxf.path.flattening()` emits one, raw polyline points do not — and both
    spellings must give the same answer. They did not: the adjacency guard
    below excludes neighbours *by index*, so with the duplicate present the
    first and last real edges were cross-tested even though they share a point,
    and every plain hatch boundary came back flagged.
    """
    vertices = list(vertices)
    if len(vertices) > 1 and math.dist(vertices[0][:2], vertices[-1][:2]) < _EPS:
        vertices = vertices[:-1]
    count = len(vertices)
    if count < 4:
        return False
    if count > MAX_SELF_INTERSECT_VERTS:
        return None  # unknown beats a guess

    for i in range(count):
        a1, a2 = vertices[i], vertices[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i or (j + 1) % count == i or j == (i + 1) % count:
                continue  # adjacent edges share an endpoint by construction
            b1, b2 = vertices[j], vertices[(j + 1) % count]
            if _segments_cross(a1, a2, b1, b2):
                return True
    return False


def _orientation(p, q, r) -> float:
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _segments_cross(p1, p2, q1, q2) -> bool:
    """Proper crossing only — touching at an endpoint is not an intersection."""
    d1 = _orientation(q1, q2, p1)
    d2 = _orientation(q1, q2, p2)
    d3 = _orientation(p1, p2, q1)
    d4 = _orientation(p1, p2, q2)
    return ((d1 > _EPS) != (d2 > _EPS)) and ((d3 > _EPS) != (d4 > _EPS))


def ellipse_area(major: float, ratio: float) -> float:
    """pi*a*b — closed form, exact."""
    return math.pi * abs(float(major)) * abs(float(major) * float(ratio))


def ellipse_perimeter_ramanujan(major: float, ratio: float) -> float:
    """Ramanujan's second approximation.

    Relative error is around 1e-10 for ratio >= 0.1, but it is an approximation
    and not closed form, so anything reporting it must set ``perimeter_exact``
    to False. Exact for a circle (ratio == 1), which is the useful sanity check.
    """
    a = abs(float(major))
    b = abs(float(major) * float(ratio))
    if a < _EPS or b < _EPS:
        return 4.0 * max(a, b)
    h = ((a - b) ** 2) / ((a + b) ** 2)
    return math.pi * (a + b) * (1.0 + (3.0 * h) / (10.0 + math.sqrt(4.0 - 3.0 * h)))
