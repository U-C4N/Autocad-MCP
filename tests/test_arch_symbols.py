"""The structural grid and the architectural symbols.

Pure geometry: the emitted primitives are asserted against coordinates worked
out by hand in each test (1:50 unless a test says otherwise, so a 5 mm paper
bubble is 250 drawing units). The last tests draw through the headless backend.
"""

from __future__ import annotations

import math

import pytest

from engineering.arch.lang import vocab
from engineering.arch.layers import ARCH_LAYERS, ARCH_ROLE_LAYER
from engineering.arch.symbols import (
    SYMBOL_KINDS,
    draw_grid,
    draw_symbol,
    fmt_level,
    grid_axes,
    grid_prims,
    symbol_prims,
)
from engineering.mech.primitives import Circle, Line, Poly, Text

TOL = 1e-9


def close(a, b) -> bool:
    return abs(a[0] - b[0]) <= TOL and abs(a[1] - b[1]) <= TOL


def of(prims, kind):
    return [p for p in prims if isinstance(p, kind)]


def centred_at(text: Text, cx: float, cy: float) -> bool:
    """The inverse of engineering.sheet.frames.centred_text."""
    h = text.height
    return close(text.at, (cx - 0.3 * h * len(text.text), cy - 0.5 * h))


# -- the grid ------------------------------------------------------------------


def test_grid_numbers_along_x_and_letters_along_y():
    axes = grid_axes([0.0, 5000.0, 9000.0], [0.0, 6000.0])
    assert axes == {
        "x": [
            {"label": "1", "position": 0.0},
            {"label": "2", "position": 5000.0},
            {"label": "3", "position": 9000.0},
        ],
        "y": [{"label": "A", "position": 0.0}, {"label": "B", "position": 6000.0}],
    }


def test_grid_lines_run_past_the_outer_axes_by_the_extension():
    prims = grid_prims([0.0, 5000.0, 9000.0], [0.0, 6000.0], extension=1500.0, scale=50)
    lines = of(prims, Line)
    assert len(lines) == 5
    assert all(line.role == "grid" for line in lines)
    # vertical axes: y from 0 - 1500 to 6000 + 1500
    assert close(lines[0].p1, (0.0, -1500.0)) and close(lines[0].p2, (0.0, 7500.0))
    assert close(lines[2].p1, (9000.0, -1500.0)) and close(lines[2].p2, (9000.0, 7500.0))
    # horizontal axes: x from 0 - 1500 to 9000 + 1500
    assert close(lines[3].p1, (-1500.0, 0.0)) and close(lines[3].p2, (10500.0, 0.0))
    assert close(lines[4].p1, (-1500.0, 6000.0)) and close(lines[4].p2, (10500.0, 6000.0))


def test_grid_bubbles_touch_both_ends_and_carry_the_labels():
    prims = grid_prims([0.0, 5000.0, 9000.0], [0.0, 6000.0], extension=1500.0, scale=50)
    bubbles = of(prims, Circle)
    texts = of(prims, Text)
    assert len(bubbles) == 10 and len(texts) == 10
    assert all(b.radius == 250.0 and b.role == "symbol" for b in bubbles)
    assert all(t.role == "symbol" and t.height == 175.0 for t in texts)
    # axis 1 (x = 0): bubbles at y = -1500 - 250 and y = 7500 + 250
    assert close(bubbles[0].center, (0.0, -1750.0))
    assert close(bubbles[1].center, (0.0, 7750.0))
    # axis B (y = 6000): bubbles at x = -1500 - 250 and x = 10500 + 250
    assert close(bubbles[8].center, (-1750.0, 6000.0))
    assert close(bubbles[9].center, (10750.0, 6000.0))
    assert [t.text for t in texts] == ["1", "1", "2", "2", "3", "3", "A", "A", "B", "B"]
    # "1" at (0, -1750), height 175: left baseline at (-52.5, -1837.5)
    assert close(texts[0].at, (-52.5, -1837.5))
    for bubble, text in zip(bubbles, texts, strict=True):
        assert centred_at(text, *bubble.center)


def test_grid_prims_come_line_then_bubble_label_pairs_per_axis():
    prims = grid_prims([0.0], [0.0], extension=1000.0, scale=100)
    assert [type(p).__name__ for p in prims] == [
        "Line",
        "Circle",
        "Text",
        "Circle",
        "Text",
        "Line",
        "Circle",
        "Text",
        "Circle",
        "Text",
    ]
    # 1:100 -> a 5 mm bubble is 500 units; the vertical axis spans y -1000 .. 1000
    assert close(prims[1].center, (0.0, -1500.0)) and prims[1].radius == 500.0


def test_grid_unsorted_positions_are_numbered_left_to_right():
    axes = grid_axes([9000.0, 0.0, 5000.0], [6000.0, 0.0])
    assert [(a["label"], a["position"]) for a in axes["x"]] == [
        ("1", 0.0),
        ("2", 5000.0),
        ("3", 9000.0),
    ]
    assert [(a["label"], a["position"]) for a in axes["y"]] == [("A", 0.0), ("B", 6000.0)]


def test_grid_given_labels_follow_their_positions():
    axes = grid_axes([5000.0, 0.0], [0.0, 6000.0], labels={"x": ["B2", "B1"], "y": ["K", "L"]})
    assert [(a["label"], a["position"]) for a in axes["x"]] == [("B1", 0.0), ("B2", 5000.0)]
    assert [a["label"] for a in axes["y"]] == ["K", "L"]


def test_grid_letters_continue_past_z():
    axes = grid_axes([0.0], [float(i) * 1000.0 for i in range(28)])
    assert [a["label"] for a in axes["y"]][24:] == ["Y", "Z", "AA", "AB"]


def test_grid_refusals_name_their_path():
    with pytest.raises(ValueError, match="y_axes: a grid needs at least one axis"):
        grid_prims([0.0, 5000.0], [])
    with pytest.raises(ValueError, match=r"x_axes: two axes at the same position 0"):
        grid_prims([0.0, 0.0], [0.0])
    with pytest.raises(ValueError, match=r"x_axes\[1\]: must be finite"):
        grid_prims([0.0, float("nan")], [0.0])
    with pytest.raises(ValueError, match=r"labels.x: 1 label\(s\) for 2 axis position\(s\)"):
        grid_prims([0.0, 5000.0], [0.0], labels={"x": ["1"]})
    with pytest.raises(ValueError, match="labels.y: every axis label must be unique"):
        grid_prims([0.0], [0.0, 10.0], labels={"y": ["A", "A"]})
    with pytest.raises(ValueError, match="labels: unknown key"):
        grid_prims([0.0], [0.0], labels={"z": ["1"]})
    # a string is not split into one label per character; a number is not a list
    with pytest.raises(ValueError, match=r"labels.x: expected a list of labels"):
        grid_prims([0.0, 1000.0], [0.0], labels={"x": "AB"})
    with pytest.raises(ValueError, match=r"labels.x: expected a list of labels"):
        grid_prims([0.0], [0.0], labels={"x": 5})
    with pytest.raises(ValueError, match=r"labels: expected \{'x'"):
        grid_prims([0.0], [0.0], labels=["x"])
    with pytest.raises(ValueError, match="extension: must be greater than zero"):
        grid_prims([0.0], [0.0], extension=0.0)
    with pytest.raises(ValueError, match="scale: must be greater than zero"):
        grid_prims([0.0], [0.0], scale=-50)


# -- the north arrow -------------------------------------------------------------


def test_north_arrow_points_up_at_zero_rotation():
    circle, arrow, label = symbol_prims("north_arrow", (1000.0, 2000.0), scale=50)
    # radius 8 mm x 50 = 400
    assert isinstance(circle, Circle) and circle.radius == 400.0
    assert close(circle.center, (1000.0, 2000.0))
    assert isinstance(arrow, Poly) and arrow.closed and arrow.role == "symbol"
    expected = ((1000.0, 2400.0), (840.0, 1760.0), (1000.0, 1880.0), (1160.0, 1760.0))
    assert all(close(p, q) for p, q in zip(arrow.points, expected, strict=True))
    assert label.text == vocab("en")["north"]
    # label centre: 400 + 175 above the centre
    assert centred_at(label, 1000.0, 2575.0)
    assert label.rotation == 0.0


def test_north_arrow_rotation_turns_the_tip_and_the_label_ccw():
    circle, arrow, label = symbol_prims("north_arrow", (1000.0, 2000.0), scale=50, rotation=90.0)
    # CCW 90: +Y becomes -X
    assert close(arrow.points[0], (600.0, 2000.0))
    # the left barb (-160, -240) turns to (240, -160)
    assert close(arrow.points[1], (1240.0, 1840.0))
    assert centred_at(label, 425.0, 2000.0)
    assert label.rotation == 0.0  # the letter stays upright


def test_north_arrow_speaks_turkish():
    *_, label = symbol_prims("north_arrow", (0.0, 0.0), lang="tr")
    assert label.text == vocab("tr")["north"]
    assert vocab("tr")["north"] != vocab("en")["north"]


# -- the section mark ------------------------------------------------------------


def test_section_mark_line_bubbles_and_arrows_looking_left():
    prims = symbol_prims(
        "section_mark", (0.0, 0.0), scale=50, p1=(0.0, 0.0), p2=(4000.0, 0.0), label="A"
    )
    (line,) = of(prims, Line)
    assert close(line.p1, (0.0, 0.0)) and close(line.p2, (4000.0, 0.0))
    bubbles = of(prims, Circle)
    assert [b.radius for b in bubbles] == [250.0, 250.0]
    assert close(bubbles[0].center, (-250.0, 0.0)) and close(bubbles[1].center, (4250.0, 0.0))
    texts = of(prims, Text)
    assert [t.text for t in texts] == ["A", "A"]
    assert centred_at(texts[0], -250.0, 0.0) and centred_at(texts[1], 4250.0, 0.0)
    first, second = of(prims, Poly)
    # arrow: 6 mm long, 2 mm half-width at 1:50 -> 300 and 100
    assert all(
        close(p, q)
        for p, q in zip(first.points, ((0.0, 0.0), (200.0, 0.0), (100.0, 300.0)), strict=True)
    )
    assert all(
        close(p, q)
        for p, q in zip(second.points, ((4000.0, 0.0), (3800.0, 0.0), (3900.0, 300.0)), strict=True)
    )


def test_section_mark_looking_right_and_relative_to_at():
    prims = symbol_prims(
        "section_mark",
        (100.0, 200.0),
        scale=50,
        p1=(0.0, 0.0),
        p2=(0.0, 3000.0),
        label="B",
        side="right",
    )
    (line,) = of(prims, Line)
    assert close(line.p1, (100.0, 200.0)) and close(line.p2, (100.0, 3200.0))
    first, _second = of(prims, Poly)
    # up the line, looking right is +X
    assert close(first.points[2], (100.0 + 300.0, 200.0 + 100.0))


def test_section_mark_refusals():
    with pytest.raises(ValueError, match="p1 and p2 are required"):
        symbol_prims("section_mark", (0.0, 0.0), p1=(0.0, 0.0))
    with pytest.raises(ValueError, match="side: must be one of left, right"):
        symbol_prims("section_mark", (0.0, 0.0), p1=(0, 0), p2=(1000, 0), side="up")
    with pytest.raises(ValueError, match="the cut line is 300 long"):
        symbol_prims("section_mark", (0.0, 0.0), p1=(0, 0), p2=(300, 0))
    with pytest.raises(ValueError, match="label must be a non-empty string"):
        symbol_prims("section_mark", (0.0, 0.0), p1=(0, 0), p2=(1000, 0), label="  ")


# -- the level mark --------------------------------------------------------------


def test_level_formats_the_value_in_both_languages():
    assert fmt_level(0.0, "en") == "±0.00"
    assert fmt_level(-0.004, "en") == "±0.00"
    assert fmt_level(3.0, "en") == "+3.00"
    assert fmt_level(-0.45, "en") == "-0.45"
    assert fmt_level(3.0, "tr") == "+3,00"
    assert fmt_level(0.0, "tr") == "±0,00"


def test_level_triangle_line_and_value():
    triangle, line, text = symbol_prims("level", (1000.0, 1000.0), scale=50, value=2.8)
    # 2.5 mm x 50 = 125: apex on the level point, the top edge 125 above it
    expected = ((1000.0, 1000.0), (875.0, 1125.0), (1125.0, 1125.0))
    assert triangle.closed and all(
        close(p, q) for p, q in zip(triangle.points, expected, strict=True)
    )
    # the line runs 15 mm x 50 = 750 to the right from the triangle's right corner
    assert close(line.p1, (1125.0, 1125.0)) and close(line.p2, (1875.0, 1125.0))
    # the value sits 1 mm x 50 = 50 right of and above that corner, 2.5 mm high
    assert text.text == "+2.80" and text.height == 125.0
    assert close(text.at, (1175.0, 1175.0))


# -- the elevation mark ----------------------------------------------------------


def test_elevation_mark_pointer_is_tangent_at_45_degrees():
    circle, label, pointer = symbol_prims("elevation_mark", (0.0, 0.0), scale=50, label="2")
    assert circle.radius == 250.0
    assert label.text == "2" and centred_at(label, 0.0, 0.0)
    s = 250.0 * math.sqrt(0.5)  # 176.776...
    expected = ((s, s), (0.0, 250.0 * math.sqrt(2.0)), (-s, s))
    assert not pointer.closed
    assert all(close(p, q) for p, q in zip(pointer.points, expected, strict=True))


def test_elevation_mark_one_pointer_per_direction():
    prims = symbol_prims(
        "elevation_mark", (0.0, 0.0), scale=50, label="1", directions=["up", "right", "down"]
    )
    pointers = of(prims, Poly)
    assert len(pointers) == 3
    apex = 250.0 * math.sqrt(2.0)
    assert close(pointers[1].points[1], (apex, 0.0))
    assert close(pointers[2].points[1], (0.0, -apex))
    with pytest.raises(ValueError, match=r"directions\[0\]: must be one of right, up, left, down"):
        symbol_prims("elevation_mark", (0.0, 0.0), directions=["north"])
    with pytest.raises(ValueError, match="each direction once"):
        symbol_prims("elevation_mark", (0.0, 0.0), directions=["up", "up"])


# -- refusals common to every kind ---------------------------------------------


def test_an_unknown_kind_is_refused_with_the_list():
    with pytest.raises(ValueError) as excinfo:
        symbol_prims("compass_rose", (0.0, 0.0))
    message = str(excinfo.value)
    assert "unknown symbol kind 'compass_rose'" in message
    for kind in SYMBOL_KINDS:
        assert kind in message


def test_a_parameter_the_kind_does_not_take_is_refused():
    with pytest.raises(ValueError, match="level: unknown parameter.*takes value"):
        symbol_prims("level", (0.0, 0.0), rotation=45.0)


def test_an_unknown_language_is_refused():
    with pytest.raises(ValueError, match="en, tr"):
        symbol_prims("north_arrow", (0.0, 0.0), lang="de")


# -- drawn through the headless backend -------------------------------------------


@pytest.mark.asyncio
async def test_draw_grid_puts_axes_on_the_grid_layer_and_bubbles_on_symbols(backend):
    result = await draw_grid(backend, [0.0, 6000.0], [0.0, 4500.0], scale=50)
    assert result["ok"] is True
    assert len(result["handles"]) == 4 * 5
    assert result["bubble_radius"] == 250.0
    grid_layer = ARCH_ROLE_LAYER["grid"]
    symbol_layer = ARCH_ROLE_LAYER["symbol"]
    lines = await backend.entity_list(type_filter="LINE", limit=100)
    circles = await backend.entity_list(type_filter="CIRCLE", limit=100)
    texts = await backend.entity_list(type_filter="TEXT", limit=100)
    assert len(lines) == 4 and {e.layer for e in lines} == {grid_layer}
    assert len(circles) == 8 and {e.layer for e in circles} == {symbol_layer}
    assert sorted(e.properties["text"] for e in texts) == ["1", "1", "2", "2", "A", "A", "B", "B"]
    row = next(row for row in ARCH_LAYERS if row[0] == grid_layer)
    info = next(info for info in await backend.layer_list() if info.name == grid_layer)
    assert info.linetype.upper() == row[2].upper()


@pytest.mark.asyncio
async def test_draw_symbol_writes_the_north_arrow_on_the_symbol_layer(backend):
    result = await draw_symbol(backend, "north_arrow", (0.0, 0.0), lang="tr", rotation=30.0)
    assert result["ok"] is True and result["kind"] == "north_arrow"
    assert result["counts"] == {"circle": 1, "poly": 1, "text": 1}
    (text,) = await backend.entity_list(type_filter="TEXT", limit=10)
    assert text.layer == ARCH_ROLE_LAYER["symbol"]
    assert text.properties["text"] == vocab("tr")["north"]


@pytest.mark.asyncio
async def test_draw_symbol_refuses_before_writing_anything(backend):
    before = len(await backend.entity_list())
    with pytest.raises(ValueError, match="kinds are north_arrow"):
        await draw_symbol(backend, "compass_rose", (0.0, 0.0))
    assert len(await backend.entity_list()) == before
