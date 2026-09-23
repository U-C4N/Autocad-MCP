"""Exterior dimension chains: three rows per side, from the wall engine's own faces.

The plan: a closed 8200 x 6200 axis rectangle of 200 mm walls (outer faces
-100..8300 x -100..6300), a 100 mm partition on x = 4100 teed into the bottom
and top walls (faces x = 4050 / 4150), a 1200 window at 1500 and a 1000 door
at 5500 in the bottom wall. Every point below is hand-computed from those
numbers; at 1:50 the rows sit 500, 900 and 1300 mm outside the building.
"""

from __future__ import annotations

import pytest

from engineering.arch.dimension import (
    FIRST_OFFSET_PAPER_MM,
    ROWS,
    SIDES,
    STEP_PAPER_MM,
    exterior_chains,
)
from engineering.arch.model import Opening, Wall
from engineering.mech.primitives import DimIntent

SHELL = Wall(
    id="shell",
    axis=((0.0, 0.0), (8200.0, 0.0), (8200.0, 6200.0), (0.0, 6200.0)),
    thickness=200.0,
    closed=True,
)
PARTITION = Wall(id="p", axis=((4100.0, 0.0), (4100.0, 6200.0)), thickness=100.0)
WINDOW = Opening(id="w1", wall="shell", kind="window", offset=1500.0, width=1200.0)
DOOR = Opening(id="d1", wall="shell", kind="door", offset=5500.0, width=1000.0)


def row(intents, feature) -> list[tuple]:
    out = []
    for intent in intents:
        if intent.feature == feature:
            out.append(
                (
                    tuple(round(v, 9) + 0.0 for v in intent.p1),
                    tuple(round(v, 9) + 0.0 for v in intent.p2),
                    tuple(round(v, 9) + 0.0 for v in intent.text_at),
                )
            )
    return out


def test_the_rows_and_the_offsets_are_declared():
    assert SIDES == ("bottom", "top", "left", "right")
    assert ROWS == ("openings", "walls", "overall")
    assert (FIRST_OFFSET_PAPER_MM, STEP_PAPER_MM) == (10.0, 8.0)


def test_the_bottom_side_carries_openings_walls_and_overall():
    intents = exterior_chains([SHELL, PARTITION], [WINDOW, DOOR], sides=("bottom",), scale=50)
    assert all(isinstance(i, DimIntent) and i.kind == "linear" for i in intents)
    assert row(intents, "bottom:openings") == [
        ((-100.0, -100.0), (1500.0, -100.0), (700.0, -600.0)),
        ((1500.0, -100.0), (2700.0, -100.0), (2100.0, -600.0)),
        ((2700.0, -100.0), (5500.0, -100.0), (4100.0, -600.0)),
        ((5500.0, -100.0), (6500.0, -100.0), (6000.0, -600.0)),
        ((6500.0, -100.0), (8300.0, -100.0), (7400.0, -600.0)),
    ]
    assert row(intents, "bottom:walls") == [
        ((-100.0, -100.0), (100.0, -100.0), (0.0, -1000.0)),
        ((100.0, -100.0), (4050.0, -100.0), (2075.0, -1000.0)),
        ((4050.0, -100.0), (4150.0, -100.0), (4100.0, -1000.0)),
        ((4150.0, -100.0), (8100.0, -100.0), (6125.0, -1000.0)),
        ((8100.0, -100.0), (8300.0, -100.0), (8200.0, -1000.0)),
    ]
    assert row(intents, "bottom:overall") == [
        ((-100.0, -100.0), (8300.0, -100.0), (4100.0, -1400.0)),
    ]
    assert len(intents) == 11


def test_a_row_that_repeats_a_farther_one_is_not_emitted():
    intents = exterior_chains([SHELL, PARTITION], [WINDOW, DOOR], sides=("left",), scale=50)
    # no opening in the left wall: that row would only restate the overall one
    assert row(intents, "left:openings") == []
    assert row(intents, "left:walls") == [
        ((-100.0, -100.0), (-100.0, 100.0), (-1000.0, 0.0)),
        ((-100.0, 100.0), (-100.0, 6100.0), (-1000.0, 3100.0)),
        ((-100.0, 6100.0), (-100.0, 6300.0), (-1000.0, 6200.0)),
    ]
    assert row(intents, "left:overall") == [((-100.0, -100.0), (-100.0, 6300.0), (-1400.0, 3100.0))]


def test_explicit_offsets_override_the_plot_scale_defaults():
    intents = exterior_chains(
        [SHELL], [], sides=("top",), first_offset=300.0, step=250.0, scale=100
    )
    # the top: the extremes only, so the walls row is the corner walls' faces
    assert row(intents, "top:overall") == [((-100.0, 6300.0), (8300.0, 6300.0), (4100.0, 7100.0))]
    assert {i.text_at[1] for i in intents if i.feature == "top:walls"} == {6850.0}


def test_bad_sides_and_scales_are_refused_by_name():
    with pytest.raises(ValueError, match=r"sides\[0\]: 'north'"):
        exterior_chains([SHELL], [], sides=("north",))
    with pytest.raises(ValueError, match="named twice"):
        exterior_chains([SHELL], [], sides=("left", "left"))
    with pytest.raises(ValueError, match="scale"):
        exterior_chains([SHELL], [], scale=0)
    with pytest.raises(ValueError, match="sides"):
        exterior_chains([SHELL], [], sides="bottom")
