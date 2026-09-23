"""The wall engine: outlines, L/T/X junctions, opening gaps and poche.

Every corner below is hand-computed and written out: two 200 mm walls centred
on their axes put their faces 100 mm either side, so an L at the origin has
its inner corner at (100, 100) and its outer one at (-100, -100), and a T or
an X leaves the faces of the crossing wall cut exactly 100 mm either side of
the stem's axis.
"""

from __future__ import annotations

import itertools
import math

import pytest

from engineering.arch.model import Opening, Wall
from engineering.arch.walls import (
    MIN_JOIN_ANGLE_DEG,
    WallGeometry,
    face_offsets,
    room_segments,
    wall_geometry,
    wall_geometry_by_wall,
    wall_segments,
)
from engineering.mech.primitives import HatchArea, Line


def seg(line) -> tuple:
    """An undirected, rounded segment: comparable as a set member."""
    a = (round(line.p1[0], 9) + 0.0, round(line.p1[1], 9) + 0.0)
    b = (round(line.p2[0], 9) + 0.0, round(line.p2[1], 9) + 0.0)
    return tuple(sorted((a, b)))


def segs(lines) -> set:
    return {seg(line) for line in lines}


def S(x1, y1, x2, y2) -> tuple:
    return tuple(sorted(((float(x1), float(y1)), (float(x2), float(y2)))))


def ring(points) -> tuple:
    """A polygon, rounded and rotated to start at its smallest vertex (order kept)."""
    pts = [(round(x, 9) + 0.0, round(y, 9) + 0.0) for x, y in points]
    k = pts.index(min(pts))
    return tuple(pts[k:] + pts[:k])


def signed_area(points) -> float:
    return (
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(points, list(points[1:]) + [points[0]], strict=True)
        )
        / 2.0
    )


def wall(id, *axis, thickness=200.0, **kw) -> Wall:
    return Wall(id=id, axis=tuple(axis), thickness=thickness, **kw)


# -- the wall's own numbers ------------------------------------------------------


def test_face_offsets_follow_the_justification():
    assert face_offsets(wall("c", (0, 0), (1, 0))) == (100.0, -100.0)
    assert face_offsets(wall("l", (0, 0), (1, 0), justification="left")) == (200.0, 0.0)
    assert face_offsets(wall("r", (0, 0), (1, 0), justification="right")) == (0.0, -200.0)


def test_wall_segments_close_a_closed_axis():
    square = wall("s", (0, 0), (4000, 0), (4000, 3000), (0, 3000), closed=True)
    assert wall_segments(square) == (
        ((0.0, 0.0), (4000.0, 0.0)),
        ((4000.0, 0.0), (4000.0, 3000.0)),
        ((4000.0, 3000.0), (0.0, 3000.0)),
        ((0.0, 3000.0), (0.0, 0.0)),
    )


# -- a free end ------------------------------------------------------------------


def test_a_single_wall_is_two_faces_closed_by_a_jamb_at_each_free_end():
    geo = wall_geometry([wall("a", (0, 0), (3000, 0), thickness=100.0, justification="left")])
    assert isinstance(geo, WallGeometry)
    assert segs(geo.faces) == {S(0, 100, 3000, 100), S(0, 0, 3000, 0)}
    assert segs(geo.jambs) == {S(0, 0, 0, 100), S(3000, 0, 3000, 100)}
    assert [ring(r) for r in geo.regions] == [ring([(0, 0), (3000, 0), (3000, 100), (0, 100)])]
    assert all(line.role == "wall" for line in (*geo.faces, *geo.jambs))


def test_right_justification_puts_the_wall_on_the_other_side_of_the_axis():
    geo = wall_geometry([wall("a", (0, 0), (3000, 0), thickness=100.0, justification="right")])
    assert segs(geo.faces) == {S(0, 0, 3000, 0), S(0, -100, 3000, -100)}


# -- L -----------------------------------------------------------------------------


L_FACES = {
    S(100, 100, 4000, 100),  # wall a, inner face
    S(-100, -100, 4000, -100),  # wall a, outer face
    S(100, 100, 100, 3000),  # wall b, inner face
    S(-100, -100, -100, 3000),  # wall b, outer face
}
L_JAMBS = {S(4000, -100, 4000, 100), S(-100, 3000, 100, 3000)}
L_REGIONS = {
    ring([(-100, -100), (4000, -100), (4000, 100), (100, 100)]),
    ring([(100, 100), (100, 3000), (-100, 3000), (-100, -100)]),
}


def test_an_l_between_two_walls_mitres_both_faces_to_their_intersections():
    geo = wall_geometry([wall("a", (0, 0), (4000, 0)), wall("b", (0, 0), (0, 3000))])
    assert segs(geo.faces) == L_FACES
    assert segs(geo.jambs) == L_JAMBS
    assert {ring(r) for r in geo.regions} == L_REGIONS
    assert len(geo.hatches) == 2


def test_an_l_inside_one_wall_is_the_same_corner():
    geo = wall_geometry([wall("a", (4000, 0), (0, 0), (0, 3000))])
    assert segs(geo.faces) == L_FACES
    assert segs(geo.jambs) == L_JAMBS
    assert {ring(r) for r in geo.regions} == L_REGIONS


def test_every_region_is_counter_clockwise():
    geo = wall_geometry([wall("a", (4000, 0), (0, 0), (0, 3000))])
    assert all(signed_area(r) > 0 for r in geo.regions)


# -- straight joins whose faces do not line up ---------------------------------------


def test_a_justification_change_on_a_straight_axis_steps_across_the_node():
    geo = wall_geometry(
        [
            wall("a", (0, 0), (3000, 0), justification="left"),
            wall("b", (3000, 0), (6000, 0), justification="right"),
        ]
    )
    assert segs(geo.faces) == {
        S(0, 0, 3000, 0),
        S(0, 200, 3000, 200),
        S(3000, 200, 3000, 0),  # the step: each face of a stops on the square cut
        S(3000, 0, 3000, -200),  # through the node, and so does b's
        S(3000, 0, 6000, 0),
        S(3000, -200, 6000, -200),
    }
    assert {ring(r) for r in geo.regions} == {
        ring([(0, 0), (3000, 0), (3000, 200), (0, 200)]),
        ring([(3000, -200), (6000, -200), (6000, 0), (3000, 0)]),
    }


def test_a_rounding_kink_steps_like_the_straight_join_instead_of_mitring_kilometres_away():
    # 0.5 mm over 3 m: the faces 200 mm apart would meet at x = 1 203 000
    geo = wall_geometry(
        [
            wall("a", (0, 0), (3000, 0), justification="left"),
            wall("b", (3000, 0), (6000, 0.5), justification="right"),
        ]
    )
    points = [p for line in geo.faces for p in (line.p1, line.p2)]
    # b's free end is square to its own axis: 200 * sin(0.5 / 3000) past x = 6000
    assert max(abs(x) for x, _y in points) <= 6000.0 + 0.04
    assert max(abs(y) for _x, y in points) <= 200.5 + 1e-6
    assert all(signed_area(r) > 0 for r in geo.regions)
    assert sorted(signed_area(r) for r in geo.regions) == pytest.approx([600000, 600000], abs=50)


def test_a_thickness_change_at_a_small_kink_steps_at_the_node_and_does_not_flare():
    kink = math.radians(2.0)
    geo = wall_geometry(
        [
            wall("a", (0, 0), (3000, 0), thickness=100.0),
            wall(
                "b",
                (3000, 0),
                (3000 + 3000 * math.cos(kink), 3000 * math.sin(kink)),
                thickness=300.0,
            ),
        ]
    )
    a_faces = [line for line in geo.faces if abs(line.p1[1] - line.p2[1]) < 1e-9]
    # a's two 100 mm faces stop at the node (the bisector of a 2 degree join), not
    # 2865 mm into b where they would meet b's 300 mm faces
    assert {round(y, 6) for line in a_faces for y in (line.p1[1], line.p2[1])} == {50.0, -50.0}
    assert max(x for line in a_faces for x in (line.p1[0], line.p2[0])) < 3002.0
    assert all(signed_area(r) > 0 for r in geo.regions)
    assert sorted(signed_area(r) for r in geo.regions) == pytest.approx([300000, 900000], abs=1)


def test_a_rounded_rotated_thickness_change_draws_the_same_step_as_the_exact_one():
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))

    def along(d, digits=None):
        x, y = d * c, d * s
        return (round(x, digits), round(y, digits)) if digits is not None else (x, y)

    exact = wall_geometry(
        [
            wall("a", along(0), along(3000), thickness=100.0),
            wall("b", along(3000), along(6000), thickness=300.0),
        ]
    )
    rounded = wall_geometry(
        [
            wall("a", along(0, 2), along(3000, 2), thickness=100.0),
            wall("b", along(3000, 2), along(6000, 2), thickness=300.0),
        ]
    )
    assert len(rounded.faces) == len(exact.faces) == 6
    for mine, theirs in zip(
        sorted(seg(line) for line in rounded.faces),
        sorted(seg(line) for line in exact.faces),
        strict=True,
    ):
        assert math.dist(mine[0], theirs[0]) < 0.05 and math.dist(mine[1], theirs[1]) < 0.05


@pytest.mark.parametrize("kink", [0.0, 0.01, 0.5, 2.0, 8.0, 20.0, 45.0, -2.0, -30.0])
@pytest.mark.parametrize("justs", list(itertools.product(("center", "left", "right"), repeat=2)))
@pytest.mark.parametrize("thicknesses", [(200.0, 200.0), (100.0, 300.0)])
def test_a_straight_or_kinked_join_stays_near_its_node_and_every_region_is_ccw(
    kink, justs, thicknesses
):
    turn = math.radians(30.0 + kink)
    p1 = (round(3000 * math.cos(math.radians(30)), 2), round(3000 * math.sin(math.radians(30)), 2))
    p2 = (round(p1[0] + 3000 * math.cos(turn), 2), round(p1[1] + 3000 * math.sin(turn), 2))
    geo = wall_geometry(
        [
            wall("a", (0, 0), p1, thickness=thicknesses[0], justification=justs[0]),
            wall("b", p1, p2, thickness=thicknesses[1], justification=justs[1]),
        ]
    )
    reach = 6000 + 2 * max(thicknesses)
    assert all(math.hypot(*p) < reach for line in geo.faces for p in (line.p1, line.p2))
    assert len(geo.regions) == 2
    assert all(signed_area(r) > 0 for r in geo.regions)


# -- T -----------------------------------------------------------------------------


def test_a_t_stops_the_stem_on_the_near_face_and_keeps_the_far_face_whole():
    geo = wall_geometry([wall("a", (-3000, 0), (3000, 0)), wall("b", (0, 0), (0, 2500))])
    assert segs(geo.faces) == {
        S(-3000, -100, 3000, -100),  # the far face: one line past the stem
        S(-3000, 100, -100, 100),  # the near face, cut where the stem meets it
        S(100, 100, 3000, 100),
        S(-100, 100, -100, 2500),  # the stem, stopped on the near face
        S(100, 100, 100, 2500),
    }
    assert segs(geo.jambs) == {
        S(-3000, -100, -3000, 100),
        S(3000, -100, 3000, 100),
        S(-100, 2500, 100, 2500),
    }
    assert {ring(r) for r in geo.regions} == {
        ring([(-3000, -100), (0, -100), (-100, 100), (-3000, 100)]),
        ring([(0, -100), (3000, -100), (3000, 100), (100, 100)]),
        ring([(100, 100), (100, 2500), (-100, 2500), (-100, 100)]),
        ring([(100, 100), (-100, 100), (0, -100)]),  # the hub the three pieces leave
    }


def test_a_stem_drawn_to_the_face_is_carried_to_the_axis():
    touching = wall_geometry([wall("a", (-3000, 0), (3000, 0)), wall("b", (0, 100), (0, 2500))])
    on_axis = wall_geometry([wall("a", (-3000, 0), (3000, 0)), wall("b", (0, 0), (0, 2500))])
    assert segs(touching.faces) == segs(on_axis.faces)


# c runs along y = 0; a is drawn to c's face (its start is carried from y = 100
# down to c's axis) and b is a T into a's middle at y = 2000. The T on a must not
# depend on whether a's start has been carried yet when b is resolved.
T_ON_A_CARRIED_WALL = {
    S(-1000, -100, 5000, -100),  # c's far face, whole
    S(-1000, 100, 1900, 100),  # c's near face, cut by a
    S(2100, 100, 5000, 100),
    S(1900, 100, 1900, 1900),  # a's near face to b, cut by b at 1900 / 2100
    S(1900, 2100, 1900, 4000),
    S(2100, 100, 2100, 4000),  # a's far face to b, whole
    S(0, 1900, 1900, 1900),  # b stops on a's near face
    S(0, 2100, 1900, 2100),
}


@pytest.mark.parametrize("order", list(itertools.permutations("cab")))
def test_a_t_into_a_carried_wall_does_not_depend_on_the_order_of_the_walls(order):
    walls = {
        "c": wall("c", (-1000, 0), (5000, 0)),
        "a": wall("a", (2000, 100), (2000, 4000)),
        "b": wall("b", (0, 2000), (2000, 2000)),
    }
    geo = wall_geometry([walls[name] for name in order])
    assert segs(geo.faces) == T_ON_A_CARRIED_WALL
    assert segs(geo.jambs) == {
        S(-1000, -100, -1000, 100),
        S(5000, -100, 5000, 100),
        S(1900, 4000, 2100, 4000),
        S(0, 1900, 0, 2100),  # b's free end - and no jamb inside wall a
    }
    assert {ring(r) for r in geo.regions} == {
        ring([(-1000, -100), (2000, -100), (1900, 100), (-1000, 100)]),
        ring([(2000, -100), (5000, -100), (5000, 100), (2100, 100)]),
        ring([(2100, 100), (1900, 100), (2000, -100)]),  # the hub on c
        ring([(2100, 100), (2100, 2000), (1900, 1900), (1900, 100)]),  # a, split at y = 2000
        ring([(2100, 2000), (2100, 4000), (1900, 4000), (1900, 2100)]),
        ring([(1900, 2100), (1900, 1900), (2100, 2000)]),  # the hub on a
        ring([(0, 1900), (1900, 1900), (1900, 2100), (0, 2100)]),
    }


# -- X -----------------------------------------------------------------------------


def test_an_x_cuts_all_four_faces():
    geo = wall_geometry([wall("a", (-3000, 0), (3000, 0)), wall("b", (0, -2500), (0, 2500))])
    assert segs(geo.faces) == {
        S(-3000, 100, -100, 100),
        S(100, 100, 3000, 100),
        S(-3000, -100, -100, -100),
        S(100, -100, 3000, -100),
        S(-100, -2500, -100, -100),
        S(-100, 100, -100, 2500),
        S(100, -2500, 100, -100),
        S(100, 100, 100, 2500),
    }
    assert len(geo.jambs) == 4
    assert ring([(100, 100), (-100, 100), (-100, -100), (100, -100)]) in {
        ring(r) for r in geo.regions
    }
    assert len(geo.regions) == 5  # four arms and the hub


# -- openings ------------------------------------------------------------------------


def test_a_door_removes_its_width_from_both_faces_and_is_closed_by_two_jambs():
    geo = wall_geometry(
        [wall("a", (0, 0), (5000, 0))],
        [Opening(id="d1", wall="a", kind="door", offset=1000.0, width=900.0)],
    )
    assert segs(geo.faces) == {
        S(0, 100, 1000, 100),
        S(1900, 100, 5000, 100),
        S(0, -100, 1000, -100),
        S(1900, -100, 5000, -100),
    }
    assert segs(geo.jambs) == {
        S(0, -100, 0, 100),
        S(5000, -100, 5000, 100),
        S(1000, -100, 1000, 100),
        S(1900, -100, 1900, 100),
    }
    assert segs(geo.thresholds) == {S(1000, 100, 1900, 100), S(1000, -100, 1900, -100)}
    assert {ring(r) for r in geo.regions} == {
        ring([(0, -100), (1000, -100), (1000, 100), (0, 100)]),
        ring([(1900, -100), (5000, -100), (5000, 100), (1900, 100)]),
    }
    assert [h.material for h in geo.hatches] == ["brick", "brick"]
    assert geo.omitted == ()


def test_an_opening_is_placed_along_the_whole_axis_not_its_first_segment():
    geo = wall_geometry(
        [wall("a", (0, 0), (4000, 0), (4000, 3000))],
        [Opening(id="w1", wall="a", kind="window", offset=4500.0, width=1200.0)],
    )
    assert {S(3900, 100, 3900, 500), S(3900, 1700, 3900, 3000)} <= segs(geo.faces)
    assert {S(3900, 500, 4100, 500), S(3900, 1700, 4100, 1700)} <= segs(geo.jambs)


def test_an_opening_that_runs_into_a_junction_is_omitted_by_name():
    geo = wall_geometry(
        [wall("a", (0, 0), (4000, 0), (4000, 3000))],
        [Opening(id="d9", wall="a", kind="door", offset=3500.0, width=450.0)],
    )
    ((entry,),) = [geo.omitted]
    assert entry["element"] == "d9"
    assert "junction" in entry["reason"]
    assert segs(geo.faces) == segs(
        wall_geometry([wall("a", (0, 0), (4000, 0), (4000, 3000))]).faces
    )


# -- poche -----------------------------------------------------------------------------


def test_one_hatch_per_region_in_the_wall_material_and_none_without_poche():
    walls = [wall("a", (-3000, 0), (3000, 0), material="concrete"), wall("b", (0, 0), (0, 2500))]
    geo = wall_geometry(walls)
    assert len(geo.hatches) == len(geo.regions) == 4
    assert all(isinstance(h, HatchArea) and len(h.loops) == 1 for h in geo.hatches)
    assert sorted(h.material for h in geo.hatches) == ["brick", "concrete", "concrete", "concrete"]
    bare = wall_geometry(walls, poche=False)
    assert bare.hatches == ()
    assert len(bare.regions) == 4


def test_by_wall_splits_every_element_to_its_owner():
    walls = [wall("a", (-3000, 0), (3000, 0)), wall("b", (0, 0), (0, 2500))]
    split = wall_geometry_by_wall(walls)
    whole = wall_geometry(walls)
    assert set(split) == {"a", "b"}
    assert segs(split["a"].faces) | segs(split["b"].faces) == segs(whole.faces)
    assert segs(split["b"].faces) == {S(-100, 100, -100, 2500), S(100, 100, 100, 2500)}
    assert len(split["a"].regions) == 3  # two arms and the hub (a is first)
    assert len(split["b"].regions) == 1


def test_room_segments_are_faces_jambs_and_thresholds():
    geo = wall_geometry(
        [wall("a", (0, 0), (5000, 0))],
        [Opening(id="d1", wall="a", kind="door", offset=1000.0, width=900.0)],
    )
    segments = room_segments(geo)
    assert len(segments) == len(geo.faces) + len(geo.jambs) + len(geo.thresholds) == 10
    assert {S(*p1, *p2) for p1, p2 in segments} == segs(geo.faces) | segs(geo.jambs) | segs(
        geo.thresholds
    )


# -- refusals -------------------------------------------------------------------------


def test_a_stem_meeting_a_wall_below_five_degrees_is_refused_by_name():
    with pytest.raises(ValueError) as excinfo:
        wall_geometry([wall("a", (0, 0), (5000, 0)), wall("b", (-2000, -150), (2000, 0))])
    message = str(excinfo.value)
    assert "'a'" in message and "'b'" in message
    assert f"{MIN_JOIN_ANGLE_DEG:g}°" in message


def test_two_ends_meeting_below_five_degrees_are_refused():
    with pytest.raises(ValueError, match="apart"):
        wall_geometry([wall("a", (0, 0), (5000, 0)), wall("b", (0, 0), (5000, 200))])


def test_duplicate_wall_ids_are_refused_with_both_paths():
    with pytest.raises(ValueError, match=r"walls\[1\]\.id: 'a' is already used by walls\[0\]"):
        wall_geometry([wall("a", (0, 0), (1000, 0)), wall("a", (0, 500), (1000, 500))])


def test_an_unknown_material_is_refused_by_path():
    with pytest.raises(ValueError, match=r"walls\[0\]\.material") as excinfo:
        wall_geometry([wall("a", (0, 0), (1000, 0), material="cheese")])
    assert "material: material" not in str(excinfo.value)


def test_a_corner_inside_another_wall_is_refused():
    with pytest.raises(ValueError, match="corner"):
        wall_geometry(
            [
                wall("a", (-3000, 0), (3000, 0)),
                wall("b", (-500, 2000), (0, 50), (500, 2000)),
            ]
        )


def test_an_end_whose_axis_misses_the_other_wall_is_refused():
    with pytest.raises(ValueError, match="beyond its end"):
        wall_geometry([wall("a", (0, 0), (3000, 0)), wall("b", (2990, 80), (2000, 2000))])


def test_a_stub_lying_inside_the_wall_it_joins_is_refused():
    with pytest.raises(ValueError, match="lies inside the wall it joins"):
        wall_geometry([wall("a", (-3000, 0), (3000, 0)), wall("b", (0, 0), (0, 80))])


def test_a_segment_too_short_for_its_corner_is_refused():
    # a 30 degree corner puts the inner mitre 100 / tan(15 deg) = 373.2 along the
    # second segment, which is only 300 long: its outline would cross itself
    with pytest.raises(ValueError, match="too short"):
        wall_geometry([wall("a", (1000, 0), (0, 0), (259.8, 150.0))])


def test_a_stub_ending_inside_its_own_corner_is_refused():
    # 60 long past a 200 mm L: its end lies inside the first segment's body
    with pytest.raises(ValueError, match="lies inside the wall it joins"):
        wall_geometry([wall("a", (0, 0), (1000, 0), (1000, 60))])


def test_a_bad_join_tolerance_is_refused():
    with pytest.raises(ValueError, match="join_tol"):
        wall_geometry([wall("a", (0, 0), (1000, 0))], join_tol=0.0)


def test_line_primitives_are_the_mech_line_type():
    geo = wall_geometry([wall("a", (0, 0), (1000, 0))])
    assert all(isinstance(line, Line) for line in (*geo.faces, *geo.jambs))


def test_a_closed_axis_is_a_ring_with_no_jamb_and_a_4_by_5_m_inner_face():
    geo = wall_geometry([wall("r", (0, 0), (4200, 0), (4200, 5200), (0, 5200), closed=True)])
    assert geo.jambs == ()
    assert segs(geo.faces) == {
        S(100, 100, 4100, 100),
        S(4100, 100, 4100, 5100),
        S(4100, 5100, 100, 5100),
        S(100, 5100, 100, 100),
        S(-100, -100, 4300, -100),
        S(4300, -100, 4300, 5300),
        S(4300, 5300, -100, 5300),
        S(-100, 5300, -100, -100),
    }
    assert len(geo.regions) == 4
