"""The wall engine: outlines, L/T/X junctions, opening gaps and poche.

Every corner below is hand-computed and written out: two 200 mm walls centred
on their axes put their faces 100 mm either side, so an L at the origin has
its inner corner at (100, 100) and its outer one at (-100, -100), and a T or
an X leaves the faces of the crossing wall cut exactly 100 mm either side of
the stem's axis.
"""

from __future__ import annotations

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
