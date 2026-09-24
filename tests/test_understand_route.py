"""Orthogonal lengths: Manhattan distance, the rectilinear MST, the x-then-y route.

Every expected value is worked by hand in the comment beside it.
"""

from __future__ import annotations

from engineering.understand.route import manhattan, rmst, route_legs

EPS = 1e-9


def test_manhattan_is_the_sum_of_the_axis_distances():
    assert manhattan((0.0, 0.0), (3.0, -4.0)) == 7.0  # 3 + 4, not the diagonal 5


def test_fewer_than_two_points_have_no_tree():
    assert rmst([]) == (0.0, ())
    assert rmst([(5.0, 5.0)]) == (0.0, ())


def test_a_two_ended_run_is_dx_plus_dy():
    length, edges = rmst([(1000.0, 3000.0), (29000.0, 15000.0)])
    assert abs(length - 40000.0) < EPS  # 28000 + 12000
    assert edges == ((0, 1),)


def test_a_square_of_four_tags_is_three_sides():
    # Prim from point 0: (0,1) at 10 (tie with 3 goes to the lower index),
    # then (1,2) at 10, then (0,3) at 10 -> 30, never the 40 of the full ring
    length, edges = rmst([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])
    assert abs(length - 30.0) < EPS
    assert edges == ((0, 1), (1, 2), (0, 3))


def test_three_tags_in_a_row_chain_instead_of_a_star():
    length, edges = rmst([(0.0, 0.0), (100.0, 0.0), (200.0, 0.0)])
    assert abs(length - 200.0) < EPS  # 100 + 100, not 100 + 200
    assert edges == ((0, 1), (1, 2))


def test_the_route_runs_along_x_first_then_y():
    assert route_legs((0.0, 0.0), (30.0, 40.0)) == (
        ((0.0, 0.0), (30.0, 0.0)),
        ((30.0, 0.0), (30.0, 40.0)),
    )
    assert route_legs((0.0, 0.0), (0.0, 40.0)) == (((0.0, 0.0), (0.0, 40.0)),)
    assert route_legs((1.0, 1.0), (1.0, 1.0)) == ()
