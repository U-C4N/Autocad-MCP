"""ISO 7200 title blocks for A4-A0, and the A3 geometry that must not move."""

from __future__ import annotations

import pytest

from engineering.mech.primitives import Circle, Line, Poly, Text
from engineering.sheet.titleblock import (
    TB_HEIGHT,
    TB_WIDTH,
    TITLE_TEXT_INDEX,
    TITLEBLOCK_FIELDS,
    TITLEBLOCK_RULE_COUNT,
    VALUE_FIELDS,
    TitleBlockMetadata,
    apply_titleblock,
    projection_symbol_prims,
    titleblock_origin,
    titleblock_prims,
    value_text_indices,
)

EPS = 1e-9


def _meta(**overrides) -> TitleBlockMetadata:
    base = dict(
        title="HELICAL GEAR",
        drawing_no="AM-2026-001",
        part_no="GR-24T-M3",
        material="C45",
        scale="1:1",
        units="mm",
        drawn_by="Umutcan",
        checked_by="QA",
        date="2026-04-28",
        sheet="1/1",
        revision="A",
        company="Anka-Makine",
    )
    base.update(overrides)
    return TitleBlockMetadata(**base)


def test_the_block_is_the_iso_7200_180_mm_wide_and_sits_in_the_frame_corner():
    assert TB_WIDTH == 180.0 and TB_HEIGHT == 60.0
    assert titleblock_origin("A3") == (230.0, 10.0)
    assert titleblock_origin("A4", orientation="portrait") == (20.0, 10.0)
    assert titleblock_origin("A0") == (999.0, 10.0)


def test_every_declared_field_is_a_metadata_attribute():
    meta = _meta()
    for field in TITLEBLOCK_FIELDS:
        assert hasattr(meta, field), field


def test_the_a3_rules_are_exactly_where_they_have_always_been():
    prims = titleblock_prims("A3", _meta(), projection=None)
    rules = prims[:TITLEBLOCK_RULE_COUNT]
    assert isinstance(rules[0], Poly) and rules[0].closed
    assert rules[0].points == ((230.0, 10.0), (410.0, 10.0), (410.0, 70.0), (230.0, 70.0))
    assert [(r.p1[1], r.p1[0], r.p2[0]) for r in rules[1:4]] == [
        (20.0, 230.0, 410.0),
        (35.0, 230.0, 410.0),
        (50.0, 230.0, 410.0),
    ]
    assert [(r.p1[0], r.p1[1], r.p2[1]) for r in rules[4:]] == [
        (320.0, 35.0, 50.0),
        (365.0, 35.0, 50.0),
        (290.0, 20.0, 35.0),
        (350.0, 20.0, 35.0),
        (275.0, 10.0, 20.0),
        (320.0, 10.0, 20.0),
        (365.0, 10.0, 20.0),
    ]


def test_the_a3_value_texts_are_exactly_where_they_have_always_been():
    prims = titleblock_prims("A3", _meta(), projection=None)
    index = value_text_indices()
    assert prims[index["drawing_no"]] == Text(at=(233.0, 40.0), text="AM-2026-001", height=3.5)
    assert prims[index["revision"]] == Text(at=(323.0, 40.0), text="A", height=3.5)
    assert prims[index["sheet"]] == Text(at=(368.0, 40.0), text="1/1", height=3.5)
    assert prims[index["part_no"]] == Text(at=(233.0, 25.0), text="GR-24T-M3", height=3.5)
    assert prims[index["material"]] == Text(at=(293.0, 25.0), text="C45", height=3.5)
    assert prims[index["scale"]] == Text(at=(353.0, 25.0), text="1:1", height=3.5)
    assert prims[index["drawn_by"]] == Text(at=(233.0, 11.5), text="Umutcan", height=3.5)
    assert prims[index["checked_by"]] == Text(at=(278.0, 11.5), text="QA", height=3.5)
    assert prims[index["date"]] == Text(at=(323.0, 11.5), text="2026-04-28", height=3.5)
    assert prims[index["company"]] == Text(at=(368.0, 11.5), text="Anka-Makine", height=3.5)


def test_the_title_sits_at_the_pinned_index_and_carries_the_text_verbatim():
    prims = titleblock_prims("A3", _meta(title="SPUR GEAR / M3 Z24"), projection=None)
    title = prims[TITLE_TEXT_INDEX]
    assert isinstance(title, Text)
    assert title.text == "SPUR GEAR / M3 Z24"
    assert title.height == 5.0
    assert title.at[1] == pytest.approx(57.5, abs=EPS)


def test_projection_none_draws_no_symbol_and_first_draws_one():
    plain = titleblock_prims("A3", _meta(), projection=None)
    first = titleblock_prims("A3", _meta(), projection="first")
    assert len(plain) == TITLE_TEXT_INDEX + 1 + 2 * len(VALUE_FIELDS)
    assert len(first) == len(plain) + 4


#: The ISO 5456-2 / ISO 128-30 projection symbol, transcribed from two
#: independent reproductions of the standard's figure (ISO's own is paywalled
#: and was not read). Each entry is that source's OWN coordinates, measured off
#: the file, and the landmark order left-to-right is what the symbol means.
#:
#: 1. FreeCAD's ISO 5457 sheet template, first-angle symbol --
#:    src/Mod/TechDraw/Templates/ISO/A3_Landscape_ISO5457_advanced.svg,
#:    ids `first_angle_trapezoid`, `first_angle_inner circle`,
#:    `first_angle_outer circle`.
#: 2. Wikimedia Commons `Convention placement vues dessin technique.svg`, which
#:    draws both symbols side by side, labelled FR (first) and US (third).
#:
#: A test that read these numbers out of `projection_symbol_prims` instead
#: would pass whichever way round the symbol was drawn, which is exactly how
#: the mirrored third-angle cone shipped in the first place.
REFERENCE_FIGURES = {
    "first": (
        # source, short-side x, long-side x, end-view (circles) x
        ("FreeCAD A3_Landscape_ISO5457_advanced.svg", 379.0, 389.0, 396.0),
        ("Commons Convention placement vues dessin technique.svg (FR)", 13.343, 81.869, 148.005),
    ),
    "third": (
        ("Commons Convention placement vues dessin technique.svg (US)", 380.275, 448.801, 314.140),
    ),
}


#: The three landmarks, in the order `_landmarks` and REFERENCE_FIGURES list them.
LANDMARKS = ("short", "long", "circles")


def _landmarks(angle: str) -> tuple[float, float, float]:
    """(short-side x, long-side x, end-view x) of the symbol this module draws."""
    prims = projection_symbol_prims((0.0, 0.0), angle)
    cone = next(p for p in prims if isinstance(p, Poly))
    circles = [p for p in prims if isinstance(p, Circle)]
    left_x = min(x for x, _ in cone.points)
    right_x = max(x for x, _ in cone.points)
    left_h = max(y for x, y in cone.points if x == left_x) * 2
    right_h = max(y for x, y in cone.points if x == right_x) * 2
    short_x, long_x = (left_x, right_x) if left_h < right_h else (right_x, left_x)
    assert len({c.center[0] for c in circles}) == 1
    return short_x, long_x, circles[0].center[0]


@pytest.mark.parametrize("angle", ["first", "third"])
def test_the_symbol_lands_the_landmarks_in_the_order_the_reference_figures_do(angle):
    """The meaning of the symbol is the left-to-right order of three landmarks:
    the cone's short side, its long side and the end view. Assert that order
    against the measured figures, not against what the module computes."""
    mine = _landmarks(angle)
    for source, *reference in REFERENCE_FIGURES[angle]:
        expected = [name for _x, name in sorted(zip(reference, LANDMARKS, strict=True))]
        got = [name for _x, name in sorted(zip(mine, LANDMARKS, strict=True))]
        assert got == expected, f"{angle}-angle symbol disagrees with {source}"


def test_the_short_side_points_away_from_the_circles_in_first_and_towards_in_third():
    """The mirror-invariant half of the rule, and the one every source states
    in words: Wikipedia's Multiview orthographic projection, Symbol --
    "the first-angle symbol shows the trapezoid with its shortest side away
    from the circles", "the third-angle symbol shows the trapezoid with its
    shortest side towards the circles"."""
    short_x, long_x, circles_x = _landmarks("first")
    assert abs(short_x - circles_x) > abs(long_x - circles_x)
    short_x, long_x, circles_x = _landmarks("third")
    assert abs(short_x - circles_x) < abs(long_x - circles_x)


def test_the_symbol_keeps_the_reference_figures_proportions_and_its_axis():
    """Both reference figures draw the end view as two circles of ratio 1:2
    (FreeCAD r 2.5 / r 5) on a centre line; the absolute size is this
    module's."""
    first = projection_symbol_prims((0.0, 0.0), "first")
    circles = [p for p in first if isinstance(p, Circle)]
    assert {round(c.radius, 6) for c in circles} == {4.0, 2.0}
    assert any(isinstance(p, Line) and p.role == "center" for p in first)


def test_the_two_symbols_are_not_the_same_picture():
    assert _landmarks("first") != _landmarks("third")


def test_an_unknown_projection_angle_is_refused():
    with pytest.raises(ValueError) as excinfo:
        titleblock_prims("A3", _meta(), projection="second")
    assert "second" in str(excinfo.value)


def test_a_sheet_narrower_than_the_title_block_is_refused_by_name():
    """There is no such size in A4-A0; the guard exists so a future size cannot
    ship a title block hanging off the paper."""
    from engineering.sheet import frames

    original = dict(frames.SHEETS)
    frames.SHEETS["A9"] = (100.0, 140.0)
    try:
        with pytest.raises(ValueError) as excinfo:
            titleblock_origin("A9", orientation="portrait")
        assert "180" in str(excinfo.value)
    finally:
        frames.SHEETS.clear()
        frames.SHEETS.update(original)


@pytest.mark.asyncio
async def test_apply_titleblock_returns_the_payload_the_a3_tool_always_returned(backend):
    result = await apply_titleblock(backend, size="A3", metadata=_meta(), projection=None)
    assert result["bbox"] == {"min": [0.0, 0.0], "max": [420.0, 297.0]}
    assert set(result["value_texts"]) == {
        "drawing_no",
        "revision",
        "sheet",
        "part_no",
        "material",
        "scale",
        "drawn_by",
        "checked_by",
        "date",
        "company",
    }
    assert len(result["titleblock_lines"]) == TITLEBLOCK_RULE_COUNT
    title = await backend.entity_get(result["title_text"])
    assert title.type == "TEXT" and title.properties["text"] == "HELICAL GEAR"
    assert title.layer == "TEXT"
    for handle in result["titleblock_lines"]:
        assert (await backend.entity_get(handle)).layer == "TITLEBLOCK"
    assert result["projection_symbol"] == []


@pytest.mark.asyncio
async def test_apply_titleblock_draws_every_size_with_its_symbol(backend):
    for size in ("A4", "A3", "A2", "A1", "A0"):
        orientation = "portrait" if size == "A4" else "landscape"
        result = await apply_titleblock(
            backend, size=size, metadata=_meta(), orientation=orientation
        )
        assert result["ok"] is True and result["size"] == size
        assert len(result["projection_symbol"]) == 4


@pytest.mark.asyncio
async def test_the_titleblock_puts_the_caller_back_on_the_tab_it_found_them_on(
    backend, backend_without_private_state
):
    """`apply_titleblock` routes through the same `enter_layout`, so the live
    engine dropped the caller onto Model here too."""
    await backend.layout_create("SheetA")
    await backend.layout_create("SheetB")
    await backend.layout_set_current("SheetB")
    result = await apply_titleblock(
        backend_without_private_state, size="A3", metadata=_meta(), layout="SheetA"
    )
    assert result["layout"] == "SheetA"
    assert (await backend.layout_list())["current"] == "SheetB"
