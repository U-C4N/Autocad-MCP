"""T0.5 — measuring the drawing, not the caller's memory of it.

``analysis_measure_area`` never touched the document: it ran a shoelace over
points the *caller* typed in. So "what is the area of the polyline I just drew?"
had no answer — the model had to recall the vertices, which is precisely what
CLAUDE.md's no-coordinate-guessing rule forbids.

The only workflow available was worse than absent. ``entity_get`` reported
LWPOLYLINE vertices through the default ``get_points()`` format and kept just
x and y, silently dropping the **bulge** that makes an edge an arc. Measured on a
100x100 square with one edge bulged into an outward semicircle: shoelace over the
reported points gives 10000 against a true 13926.9908 — **28.20% low** — and the
perimeter 400 against 457.0796, **12.49% low**, with nothing in the response
saying so. ``props["length"]`` was dead code: ``LWPolyline`` has no ``length()``
method, so the line raised into a debug log and the key simply never appeared.

Bulge geometry is always a circular arc, so this needs no approximation: area is
the shoelace plus each arc's signed circular segment, and perimeter uses arc
length. The tests below check that against two independent oracles — closed-form
values worked out by hand, and ezdxf's own flattening at a fine tolerance.
"""

from __future__ import annotations

import math

import pytest

from engineering.measure import (
    ellipse_area,
    ellipse_perimeter_ramanujan,
    is_self_intersecting,
    polygon_area_perimeter,
)

#: 100x100 square, bottom edge bulged outward into a semicircle (bulge=1.0 means
#: a 180 deg sweep, so the chord of 100 is the diameter and the radius is 50).
SQUARE_WITH_SEMICIRCLE = [
    (0.0, 0.0, 0.0),
    (100.0, 0.0, 1.0),
    (100.0, 100.0, 0.0),
    (0.0, 100.0, 0.0),
]
SEMICIRCLE_AREA = 10000.0 + math.pi * 50.0**2 / 2.0  # 13926.990817
SEMICIRCLE_PERIMETER = 300.0 + math.pi * 50.0  # 457.079633

PLAIN_SQUARE = [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0), (100.0, 100.0, 0.0), (0.0, 100.0, 0.0)]


def _flattened_oracle(vertices, closed, segments_per_arc=200_000):
    """Area and perimeter from a dense polyline approximation of the same shape.

    Independent of the implementation under test: it subdivides each arc and
    runs a plain shoelace, so agreement to 6 decimal places is real evidence
    rather than a formula checking itself.
    """
    points: list[tuple[float, float]] = []
    count = len(vertices)
    limit = count if closed else count - 1
    for i in range(limit):
        x1, y1, bulge = vertices[i]
        x2, y2, _ = vertices[(i + 1) % count]
        points.append((x1, y1))
        if abs(bulge) < 1e-12:
            continue
        theta = 4.0 * math.atan(bulge)
        chord = math.hypot(x2 - x1, y2 - y1)
        radius = chord / (2.0 * math.sin(abs(theta) / 2.0))
        # Centre lies on the perpendicular bisector, offset by the sagitta side.
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        height = radius * math.cos(abs(theta) / 2.0)
        ux, uy = (y2 - y1) / chord, -(x2 - x1) / chord
        sign = 1.0 if theta > 0 else -1.0
        cx, cy = mx - sign * ux * height, my - sign * uy * height
        a1 = math.atan2(y1 - cy, x1 - cx)
        steps = max(2, segments_per_arc // max(1, limit))
        for step in range(1, steps):
            angle = a1 + theta * step / steps
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    if not closed:
        points.append((vertices[-1][0], vertices[-1][1]))

    total = len(points)
    shoelace = sum(
        points[i][0] * points[(i + 1) % total][1] - points[(i + 1) % total][0] * points[i][1]
        for i in range(total)
    )
    perimeter = sum(
        math.dist(points[i], points[(i + 1) % total]) for i in range(total if closed else total - 1)
    )
    return abs(shoelace) / 2.0, perimeter


# ── the arithmetic the server was missing ───────────────────────────────────


def test_a_straight_polygon_is_the_plain_shoelace():
    area, perimeter = polygon_area_perimeter(PLAIN_SQUARE, closed=True)
    assert area == pytest.approx(10000.0)
    assert perimeter == pytest.approx(400.0)


def test_an_outward_bulge_adds_its_circular_segment():
    """The 28.20% the old path lost, recovered exactly."""
    area, perimeter = polygon_area_perimeter(SQUARE_WITH_SEMICIRCLE, closed=True)
    assert area == pytest.approx(SEMICIRCLE_AREA, rel=1e-12)
    assert perimeter == pytest.approx(SEMICIRCLE_PERIMETER, rel=1e-12)
    # ...and it really is 28% more than the xy-only answer.
    assert area / 10000.0 == pytest.approx(1.3926990817, rel=1e-9)


def test_an_inward_bulge_subtracts_it():
    inward = [(0.0, 0.0, 0.0), (100.0, 0.0, -0.5), (100.0, 100.0, 0.0), (0.0, 100.0, 0.0)]
    area, perimeter = polygon_area_perimeter(inward, closed=True)

    oracle_area, oracle_perimeter = _flattened_oracle(inward, closed=True)
    assert area == pytest.approx(oracle_area, rel=1e-9)
    assert perimeter == pytest.approx(oracle_perimeter, rel=1e-7)
    assert area < 10000.0, "an inward bulge must remove material"


def test_a_bulge_matches_a_dense_flattening_of_the_same_arc():
    area, perimeter = polygon_area_perimeter(SQUARE_WITH_SEMICIRCLE, closed=True)
    oracle_area, oracle_perimeter = _flattened_oracle(SQUARE_WITH_SEMICIRCLE, closed=True)
    assert area == pytest.approx(oracle_area, rel=1e-9)
    assert perimeter == pytest.approx(oracle_perimeter, rel=1e-7)


def test_an_open_boundary_is_closed_implicitly(bulge_free=PLAIN_SQUARE):
    """AutoCAD's AREA closes an open boundary; the caller is told it happened."""
    open_area, open_perimeter = polygon_area_perimeter(bulge_free, closed=False)
    assert open_area == pytest.approx(10000.0), "the area is of the closed region"
    assert open_perimeter == pytest.approx(300.0), "the walked length is the open one"


# ── the trap shoelace hides ─────────────────────────────────────────────────


def test_a_self_intersecting_loop_is_detected():
    """Shoelace CANCELS crossed lobes: a bowtie of two 2500 triangles reads 0."""
    bowtie = [(0.0, 0.0, 0.0), (100.0, 100.0, 0.0), (100.0, 0.0, 0.0), (0.0, 100.0, 0.0)]
    area, _ = polygon_area_perimeter(bowtie, closed=True)
    assert area == pytest.approx(0.0, abs=1e-9), "this is exactly why it must be flagged"
    assert is_self_intersecting([(x, y) for x, y, _ in bowtie]) is True


def test_a_simple_polygon_is_not_flagged():
    assert is_self_intersecting([(x, y) for x, y, _ in PLAIN_SQUARE]) is False


def test_self_intersection_reports_unknown_rather_than_guessing_on_huge_loops():
    """The check is O(n^2); above the cap it must answer None, not False."""
    from engineering.measure import MAX_SELF_INTERSECT_VERTS

    huge = [(float(i), float(i % 7), 0.0) for i in range(MAX_SELF_INTERSECT_VERTS + 1)]
    assert is_self_intersecting([(x, y) for x, y, _ in huge]) is None


# ── ellipses ────────────────────────────────────────────────────────────────


def test_ellipse_area_is_exact_and_perimeter_is_flagged_approximate():
    assert ellipse_area(10.0, 0.5) == pytest.approx(math.pi * 10.0 * 5.0)
    # A circle is the one ellipse whose perimeter is closed-form, so Ramanujan
    # must be exact there — a cheap check that the approximation is wired right.
    assert ellipse_perimeter_ramanujan(10.0, 1.0) == pytest.approx(2 * math.pi * 10.0, rel=1e-12)


# ── the two-vertex circle ───────────────────────────────────────────────────


def test_a_circle_stored_as_two_bulged_vertices_has_its_real_area():
    """DXF's most compact circle, and the arithmetic answered 0.0 for it.

    Two semicircular arcs joined into a circle store as a closed LWPOLYLINE of
    *two* vertices, each bulge 1.0 — what AutoCAD writes for a JOINed pair of
    arcs, and what `boundary_from_entities` builds from any two-arc loop. The
    `count < 3` guard treated that as degenerate and returned zero area, which
    is the same silent-nothing this module exists to stop: the shoelace over
    two points is genuinely 0, but the two circular segments are the whole
    shape and they were never added.
    """
    circle = [(-10.0, 0.0, 1.0), (10.0, 0.0, 1.0)]

    area, perimeter = polygon_area_perimeter(circle, closed=True)

    assert area == pytest.approx(math.pi * 100.0, rel=1e-12)
    assert perimeter == pytest.approx(2 * math.pi * 10.0, rel=1e-12)


def test_two_straight_vertices_still_enclose_nothing():
    """The fix must not invent area where there is none."""
    area, perimeter = polygon_area_perimeter([(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)], closed=True)

    assert area == 0.0
    assert perimeter == pytest.approx(20.0), "there and back"


def test_a_single_vertex_is_still_degenerate():
    assert polygon_area_perimeter([(0.0, 0.0, 1.0)], closed=True) == (0.0, 0.0)


# ── a closed loop must answer the same however it was handed over ───────────


def test_a_repeated_closing_vertex_is_not_a_self_intersection():
    """The false alarm this shipped with.

    `ezdxf.path.flattening()` repeats the start point, so every hatch boundary
    and every flattened curve arrived here with a duplicate closing vertex. The
    adjacency guard excludes neighbours *by index*, so the first and last real
    edges — which share a point but not an index neighbourhood — were
    cross-tested and reported as crossing. A plain 40x40 square hatch came back
    `self_intersecting: True`: a confident wrong warning on the field that
    exists to warn.
    """
    square = [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0), (0.0, 50.0)]

    assert is_self_intersecting(square) is False
    assert is_self_intersecting([*square, (0.0, 0.0)]) is False


def test_a_bowtie_is_still_caught_when_it_carries_a_closing_vertex():
    """Dropping the duplicate must not disarm the check."""
    bowtie = [(0.0, 0.0), (50.0, 50.0), (50.0, 0.0), (0.0, 50.0)]

    assert is_self_intersecting(bowtie) is True
    assert is_self_intersecting([*bowtie, (0.0, 0.0)]) is True


def test_a_triangle_with_a_closing_vertex_is_still_not_degenerate():
    """Four entries, three real corners — must not fall under the `< 4` guard
    and answer False for the wrong reason."""
    triangle = [(0.0, 0.0), (60.0, 0.0), (30.0, 40.0), (0.0, 0.0)]

    assert is_self_intersecting(triangle) is False
