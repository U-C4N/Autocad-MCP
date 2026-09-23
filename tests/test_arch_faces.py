"""The planar-face finder: segments in, closed faces out, areas measured.

Every expected coordinate and area below is computed by hand in the comment
beside it; nothing is read back from the implementation.
"""

from __future__ import annotations

import re

import pytest

from engineering.arch.faces import Face, face_containing, planar_faces, tagged_faces

EPS = 1e-9


def rect(x0, y0, x1, y1):
    """The four sides of an axis-aligned rectangle as segments."""
    return [
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ]


def two_rooms():
    """Two 4000 x 5000 rooms between 200 mm walls, as clean outlines.

    Outer outline (0,0)-(8600,5400). Inner face of the exterior walls
    (200,200)-(8400,5200), with the shared wall's two faces x=4200 and x=4400
    stopping on it at T-junctions - exactly what a clean wall engine draws.
    Room A is (200,200)-(4200,5200), room B (4400,200)-(8400,5200).
    """
    return [
        *rect(0.0, 0.0, 8600.0, 5400.0),
        *rect(200.0, 200.0, 8400.0, 5200.0),
        ((4200.0, 200.0), (4200.0, 5200.0)),
        ((4400.0, 200.0), (4400.0, 5200.0)),
    ]


def test_one_rectangle_is_one_ccw_face_starting_low_left():
    (face,) = planar_faces(rect(0.0, 0.0, 4000.0, 3000.0))
    assert face.loop == ((0.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0), (0.0, 3000.0))
    assert abs(face.area - 12_000_000.0) < EPS  # 4000 x 3000
    assert face.centroid == pytest.approx((2000.0, 1500.0), abs=EPS)
    assert face.holes == ()


def test_the_same_rectangle_drawn_clockwise_is_still_a_ccw_face():
    clockwise = [
        ((0.0, 0.0), (0.0, 3000.0)),
        ((0.0, 3000.0), (4000.0, 3000.0)),
        ((4000.0, 3000.0), (4000.0, 0.0)),
        ((4000.0, 0.0), (0.0, 0.0)),
    ]
    (face,) = planar_faces(clockwise)
    assert face.loop == ((0.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0), (0.0, 3000.0))


def test_two_adjacent_rooms_are_two_room_faces_plus_the_wall_bodies():
    faces = planar_faces(two_rooms())
    # room A, room B, the shared wall body, and the exterior ring
    assert len(faces) == 4
    room_a = face_containing(faces, (2200.0, 2700.0))
    room_b = face_containing(faces, (6400.0, 2700.0))
    assert abs(room_a.area - 20_000_000.0) < EPS  # 4000 x 5000
    assert abs(room_b.area - 20_000_000.0) < EPS
    assert room_a.loop == ((200.0, 200.0), (4200.0, 200.0), (4200.0, 5200.0), (200.0, 5200.0))
    assert room_b.loop == ((4400.0, 200.0), (8400.0, 200.0), (8400.0, 5200.0), (4400.0, 5200.0))
    assert room_a.centroid == pytest.approx((2200.0, 2700.0), abs=EPS)
    shared = face_containing(faces, (4300.0, 2700.0))
    assert abs(shared.area - 1_000_000.0) < EPS  # 200 x 5000
    ring = face_containing(faces, (100.0, 2700.0))
    # 8600 x 5400 = 46 440 000 minus the inner outline 8200 x 5000 = 41 000 000
    assert abs(ring.area - 5_440_000.0) < EPS
    # the hole is the inner outline, clockwise, split where the shared wall meets it
    assert ring.holes == (
        (
            (200.0, 200.0),
            (200.0, 5200.0),
            (4200.0, 5200.0),
            (4400.0, 5200.0),
            (8400.0, 5200.0),
            (8400.0, 200.0),
            (4400.0, 200.0),
            (4200.0, 200.0),
        ),
    )
    # the ring's net region is symmetric about the outline's centre
    assert ring.centroid == pytest.approx((4300.0, 2700.0), abs=1e-6)


def test_a_4000_by_5000_room_between_200_mm_walls_measures_20_m2_not_the_axis_area():
    # walls on axes (-100,-100)-(4100,5100); the axis rectangle is 4200 x 5200
    faces = planar_faces([*rect(-200.0, -200.0, 4200.0, 5200.0), *rect(0.0, 0.0, 4000.0, 5000.0)])
    room = face_containing(faces, (2000.0, 2500.0))
    assert abs(room.area - 20_000_000.0) < EPS  # 20.00 m2, not 21.84 m2
    wall = face_containing(faces, (-100.0, 2500.0))
    # 4400 x 5400 = 23 760 000 minus 20 000 000
    assert abs(wall.area - 3_760_000.0) < EPS


def test_a_t_junction_splits_the_line_it_touches():
    # a partition from (2000,0) to (2000,3000) stops on both long sides
    faces = planar_faces([*rect(0.0, 0.0, 4000.0, 3000.0), ((2000.0, 0.0), (2000.0, 3000.0))])
    assert len(faces) == 2
    left, right = faces
    assert left.loop == ((0.0, 0.0), (2000.0, 0.0), (2000.0, 3000.0), (0.0, 3000.0))
    assert right.loop == ((2000.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0), (2000.0, 3000.0))
    assert abs(left.area - 6_000_000.0) < EPS  # 2000 x 3000
    assert abs(right.area - 6_000_000.0) < EPS


def test_a_t_touch_within_tol_still_splits():
    # the partition stops 0.5 mm short of the bottom side: within tol=1.0
    faces = planar_faces(
        [*rect(0.0, 0.0, 4000.0, 3000.0), ((2000.0, 0.5), (2000.0, 3000.0))], tol=1.0
    )
    assert len(faces) == 2
    # the split vertex is the partition's own end (2000, 0.5), which bends the
    # bottom side by 0.5 mm; each half is 2000 x 3000 minus a 0.5 mm-high sliver
    # triangle of base 2000: 6 000 000 - 0.5 * 2000 * 0.5 = 5 999 500
    assert [round(f.area, 6) for f in faces] == [5_999_500.0, 5_999_500.0]


def test_crossing_lines_split_each_other_and_their_overshoots_are_pruned():
    segments = [
        *rect(0.0, 0.0, 4000.0, 3000.0),
        ((-500.0, 1500.0), (4500.0, 1500.0)),  # overshoots 500 at both ends
        ((2000.0, -500.0), (2000.0, 3500.0)),
    ]
    faces = planar_faces(segments)
    assert len(faces) == 4
    assert all(abs(f.area - 3_000_000.0) < EPS for f in faces)  # 2000 x 1500 each
    assert faces[0].loop == ((0.0, 0.0), (2000.0, 0.0), (2000.0, 1500.0), (0.0, 1500.0))


def test_a_dangling_segment_is_ignored():
    segments = [
        *rect(0.0, 0.0, 4000.0, 3000.0),
        ((4000.0, 3000.0), (5000.0, 4000.0)),  # a spur off a corner
        ((1000.0, 1000.0), (1500.0, 1200.0)),  # a stray line inside the room
    ]
    (face,) = planar_faces(segments)
    assert abs(face.area - 12_000_000.0) < EPS


def test_endpoints_within_tol_merge_and_a_wider_gap_leaves_the_loop_open():
    closing = ((0.0, 3000.4), (0.0, 0.0))  # starts 0.4 mm off the corner (0, 3000)
    sides = [
        ((0.0, 0.0), (4000.0, 0.0)),
        ((4000.0, 0.0), (4000.0, 3000.0)),
        ((4000.0, 3000.0), (0.0, 3000.0)),
    ]
    (face,) = planar_faces([*sides, closing], tol=1.0)
    # (0, 3000) was seen first, so it is the vertex: the area is exact
    assert abs(face.area - 12_000_000.0) < EPS
    open_gap = ((0.0, 2995.0), (0.0, 0.0))  # stops 5 mm short of (0, 3000)
    assert planar_faces([*sides, open_gap], tol=1.0) == ()


def test_an_overshoot_through_a_corner_still_closes_the_loop():
    sides = [
        ((0.0, 0.0), (4000.0, 0.0)),
        ((4000.0, 0.0), (4000.0, 3000.0)),
        ((4000.0, 3000.0), (0.0, 3000.0)),
    ]
    # the closing side runs 5 mm past the corner (0, 3000): the corner splits it
    # and the 5 mm stub is a dangling edge
    (face,) = planar_faces([*sides, ((0.0, 3005.0), (0.0, 0.0))])
    assert face.loop == ((0.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0), (0.0, 3000.0))
    assert abs(face.area - 12_000_000.0) < EPS


def test_collinear_overlaps_become_one_edge():
    segments = [
        ((0.0, 0.0), (3000.0, 0.0)),
        ((1000.0, 0.0), (4000.0, 0.0)),  # overlaps the first from 1000 to 3000
        ((4000.0, 0.0), (4000.0, 3000.0)),
        ((4000.0, 3000.0), (0.0, 3000.0)),
        ((0.0, 3000.0), (0.0, 0.0)),
    ]
    (face,) = planar_faces(segments)
    assert face.loop == (
        (0.0, 0.0),
        (1000.0, 0.0),
        (3000.0, 0.0),
        (4000.0, 0.0),
        (4000.0, 3000.0),
        (0.0, 3000.0),
    )
    assert abs(face.area - 12_000_000.0) < EPS


def test_a_column_inside_a_room_is_a_hole_in_the_room():
    segments = [*rect(0.0, 0.0, 4000.0, 3000.0), *rect(1000.0, 1000.0, 1400.0, 1400.0)]
    faces = planar_faces(segments)
    room = face_containing(faces, (3000.0, 2000.0))
    column = face_containing(faces, (1200.0, 1200.0))
    assert abs(room.area - 11_840_000.0) < EPS  # 12 000 000 - 400 x 400
    assert abs(column.area - 160_000.0) < EPS
    assert len(room.holes) == 1
    # net centroid: (12e6 * (2000, 1500) - 160 000 * (1200, 1200)) / 11 840 000
    assert room.centroid == pytest.approx(
        (
            (12e6 * 2000.0 - 160_000.0 * 1200.0) / 11_840_000.0,
            (12e6 * 1500.0 - 160_000.0 * 1200.0) / 11_840_000.0,
        ),
        abs=1e-6,
    )


def test_a_point_in_no_face_answers_none():
    faces = planar_faces(rect(0.0, 0.0, 4000.0, 3000.0))
    assert face_containing(faces, (5000.0, 5000.0)) is None


def test_edge_tags_ride_along_to_every_face_edge():
    tagged = [(p1, p2, "WALL") for p1, p2 in rect(0.0, 0.0, 4000.0, 3000.0)]
    tagged[0] = (tagged[0][0], tagged[0][1], "0")
    ((face, tags),) = tagged_faces(tagged)
    assert tags == (frozenset({"0"}), frozenset({"WALL"}), frozenset({"WALL"}), frozenset({"WALL"}))
    assert isinstance(face, Face)


@pytest.mark.parametrize(
    ("segments", "tol", "message"),
    [
        ([((0.0, 0.0),)], 1.0, "segments[0]: expected (p1, p2) or (p1, p2, tag), got 1 items"),
        ([((0.0, "x"), (1.0, 1.0))], 1.0, "segments[0][0][1]: expected a number, got 'x'"),
        ([((0.0, float("nan")), (1.0, 1.0))], 1.0, "segments[0][0][1]: must be finite"),
        (rect(0.0, 0.0, 1.0, 1.0), 0.0, "tol: must be greater than zero, got 0"),
    ],
)
def test_bad_input_is_refused_by_path(segments, tol, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        planar_faces(segments, tol=tol)
