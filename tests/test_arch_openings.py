"""Door and window symbols: hinge, leaf, swing arc, frame and glazing.

The wall runs along +X from the origin, 200 mm thick and centred, so its left
face is y = +100 and its right face y = -100. The door is 900 wide at offset
1000: its jambs are x = 1000 and x = 1900. For each swing/hand pair the hinge,
the open leaf and the arc angles are written out below by hand.
"""

from __future__ import annotations

import pytest

from engineering.arch.model import Opening, Wall
from engineering.arch.openings import opening_prims
from engineering.mech.primitives import Arc, Line

WALL = Wall(id="a", axis=((0.0, 0.0), (5000.0, 0.0)), thickness=200.0)


def door(swing: str, hand: str) -> Opening:
    return Opening(
        id="d1", wall="a", kind="door", offset=1000.0, width=900.0, swing=swing, hand=hand
    )


def pt(p) -> tuple[float, float]:
    return (round(p[0], 9) + 0.0, round(p[1], 9) + 0.0)


@pytest.mark.parametrize(
    ("swing", "hand", "hinge", "leaf_end", "start_deg", "end_deg"),
    [
        # in = the axis's left (+Y). Facing the wall from +Y, the viewer's left is +X.
        ("in", "left", (1900.0, 100.0), (1900.0, 1000.0), 90.0, 180.0),
        ("in", "right", (1000.0, 100.0), (1000.0, 1000.0), 0.0, 90.0),
        # out = the axis's right (-Y). Facing the wall from -Y, the viewer's left is -X.
        ("out", "left", (1000.0, -100.0), (1000.0, -1000.0), 270.0, 0.0),
        ("out", "right", (1900.0, -100.0), (1900.0, -1000.0), 180.0, 270.0),
    ],
)
def test_a_door_hinges_on_its_hand_and_swings_a_quarter_arc_to_its_side(
    swing, hand, hinge, leaf_end, start_deg, end_deg
):
    leaf, arc = opening_prims(WALL, door(swing, hand))
    assert isinstance(leaf, Line) and leaf.role == "door"
    assert (pt(leaf.p1), pt(leaf.p2)) == (hinge, leaf_end)
    assert isinstance(arc, Arc) and arc.role == "door"
    assert pt(arc.center) == hinge
    assert arc.radius == 900.0
    assert (round(arc.start_deg, 9), round(arc.end_deg, 9)) == (start_deg, end_deg)


def test_the_leaf_is_as_long_as_the_opening_is_wide_and_square_to_the_wall():
    leaf, _arc = opening_prims(WALL, door("in", "left"))
    dx = leaf.p2[0] - leaf.p1[0]
    dy = leaf.p2[1] - leaf.p1[1]
    assert (dx, dy) == (0.0, 900.0)


def test_a_door_on_a_second_segment_is_placed_along_the_whole_axis():
    bent = Wall(id="b", axis=((0.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0)), thickness=200.0)
    op = Opening(
        id="d2", wall="b", kind="door", offset=4500.0, width=800.0, swing="in", hand="right"
    )
    leaf, arc = opening_prims(bent, op)
    # segment 1 runs +Y from (4000, 0); its left normal is -X, so "in" is x < 4000
    # and its left face is x = 3900. Facing +X from the left side, the viewer's
    # left is +Y, so a right hand hinges at the near jamb, y = 500.
    assert pt(leaf.p1) == (3900.0, 500.0)
    assert pt(leaf.p2) == (3100.0, 500.0)
    assert (round(arc.start_deg, 9), round(arc.end_deg, 9)) == (90.0, 180.0)


def test_a_window_is_two_frame_lines_and_two_glazing_lines():
    window = Opening(id="w1", wall="a", kind="window", offset=2500.0, width=1200.0)
    prims = opening_prims(WALL, window)
    assert len(prims) == 4
    assert all(isinstance(p, Line) and p.role == "window" for p in prims)
    assert [(pt(p.p1), pt(p.p2)) for p in prims] == [
        ((2500.0, 100.0), (3700.0, 100.0)),  # frame, left face
        ((2500.0, -100.0), (3700.0, -100.0)),  # frame, right face
        ((2500.0, round(-100.0 + 200.0 / 3.0, 9)), (3700.0, round(-100.0 + 200.0 / 3.0, 9))),
        ((2500.0, round(-100.0 + 400.0 / 3.0, 9)), (3700.0, round(-100.0 + 400.0 / 3.0, 9))),
    ]


def test_an_opening_of_another_wall_is_refused():
    with pytest.raises(ValueError, match="hosted in wall 'z'"):
        opening_prims(WALL, Opening(id="d1", wall="z", kind="door", offset=0.0, width=900.0))


def test_an_unknown_swing_is_refused_with_the_list():
    with pytest.raises(ValueError, match="swings are in, out"):
        opening_prims(WALL, door("sideways", "left"))
