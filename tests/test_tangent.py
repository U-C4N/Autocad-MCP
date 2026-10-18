"""engineering.tangent: circles tangent to two lines / circles / arcs (CIRCLE Ttr, FILLET)."""

from __future__ import annotations

import math

import pytest

from engineering.tangent import Circ, Line, choose, fillet_arc, solve_ttr


def _dist_to_line(pt, line: Line) -> float:
    (x1, y1), (x2, y2) = line.p, line.q
    return abs((x2 - x1) * (y1 - pt[1]) - (x1 - pt[0]) * (y2 - y1)) / math.dist(line.p, line.q)


def _assert_tangent(sol, a, b, r):
    for curve, tp in ((a, sol.ta), (b, sol.tb)):
        assert math.dist(sol.center, tp) == pytest.approx(r, abs=1e-7)
        if isinstance(curve, Line):
            assert _dist_to_line(sol.center, curve) == pytest.approx(r, abs=1e-7)
        else:
            assert math.dist(curve.c, tp) == pytest.approx(curve.r, abs=1e-7)
            d = math.dist(curve.c, sol.center)
            assert min(abs(d - (curve.r + r)), abs(d - abs(curve.r - r))) < 1e-7


def test_two_lines_give_four_circles_and_near_picks_the_corner():
    x_axis, y_axis = Line((0, 0), (20, 0)), Line((0, 0), (0, 20))
    sols = solve_ttr(x_axis, y_axis, 3.0)
    assert len(sols) == 4
    for s in sols:
        _assert_tangent(s, x_axis, y_axis, 3.0)
    sol = choose(sols, near=(5, 5))
    assert sol.center == pytest.approx((3.0, 3.0))
    assert sol.ext_a == pytest.approx(0.0) and sol.ext_b == pytest.approx(0.0)
    assert sol.ta == pytest.approx((3.0, 0.0)) and sol.tb == pytest.approx((0.0, 3.0))


def test_fillet_arc_of_a_right_corner_is_the_short_quarter():
    sols = solve_ttr(Line((0, 0), (20, 0)), Line((0, 0), (0, 20)), 3.0)
    sol = choose(sols, near=(5, 5))
    a0, a1 = fillet_arc(sol, 3.0)
    assert (a1 - a0) % 360 == pytest.approx(90.0)
    assert a0 % 360 == pytest.approx(180.0) and a1 % 360 == pytest.approx(270.0)


def test_line_and_circle_external_and_internal_tangency():
    line = Line((-20, 0), (40, 0))
    circ = Circ((10, 10), 6.0)
    sols = solve_ttr(line, circ, 2.0)
    assert sols
    for s in sols:
        _assert_tangent(s, line, circ, 2.0)
    # a circle of r=2 sitting on the axis, touching the big circle from outside, left of it
    sol = choose(sols, near=(2, 2))
    assert sol.center[1] == pytest.approx(2.0) and math.dist(sol.center, (10, 10)) == pytest.approx(
        8.0
    )


def test_two_circles_all_solutions_are_tangent():
    a, b = Circ((0, 0), 10.0), Circ((25, 0), 5.0)
    sols = solve_ttr(a, b, 6.0)  # the gap is 10, so a circle must be 2r >= 10 wide to bridge it
    assert len(sols) >= 2
    for s in sols:
        _assert_tangent(s, a, b, 6.0)
    assert solve_ttr(a, b, 4.0) == []  # 2r = 8 < 10: nothing bridges the gap from between them


def test_radius_larger_than_the_circle_encloses_it():
    a, b = Circ((0, 0), 2.0), Line((-50, -10), (50, -10))
    sols = solve_ttr(a, b, 10.0)
    enclosing = [s for s in sols if math.dist(s.center, (0, 0)) == pytest.approx(8.0)]
    assert enclosing
    s = enclosing[0]
    assert math.dist(s.ta, (0, 0)) == pytest.approx(2.0) and math.dist(
        s.ta, s.center
    ) == pytest.approx(10.0)


def test_extension_prefers_the_solution_on_the_real_entities():
    short = Line((0, 0), (10, 0))
    arc = Circ(
        (10, 8), 8.0, a0=180.0, a1=270.0
    )  # the lower-left quarter, ending at (10, 0)... starting (2, 8)
    sols = solve_ttr(short, arc, 1.5)
    assert sols
    best = choose(sols)
    assert best.ext_a + best.ext_b == pytest.approx(min(s.ext_a + s.ext_b for s in sols))


def test_no_solution_and_bad_radius_are_refused():
    parallel_a, parallel_b = Line((0, 0), (10, 0)), Line((0, 5), (10, 5))
    assert (
        solve_ttr(parallel_a, parallel_b, 1.0) == []
    )  # lines 5 apart, r=1: no circle touches both
    with pytest.raises(ValueError):
        choose([])
    with pytest.raises(ValueError):
        solve_ttr(parallel_a, parallel_b, 0.0)


def test_parallel_lines_with_the_right_radius_have_a_solution_family_of_none_but_midline_is_degenerate():
    # r = half the gap: both offsets coincide with the mid-line; the intersection is parallel -> no unique centre
    assert solve_ttr(Line((0, 0), (10, 0)), Line((0, 4), (10, 4)), 2.0) == []
