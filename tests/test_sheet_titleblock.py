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


def test_first_angle_puts_the_circles_to_the_right_of_the_cone():
    first = projection_symbol_prims((0.0, 0.0), "first")
    third = projection_symbol_prims((0.0, 0.0), "third")
    cone_first = next(p for p in first if isinstance(p, Poly))
    circles_first = [p for p in first if isinstance(p, Circle)]
    assert max(x for x, _ in cone_first.points) < min(c.center[0] for c in circles_first)
    cone_third = next(p for p in third if isinstance(p, Poly))
    circles_third = [p for p in third if isinstance(p, Circle)]
    assert min(x for x, _ in cone_third.points) > max(c.center[0] for c in circles_third)
    assert {round(c.radius, 6) for c in circles_first} == {4.0, 2.0}
    assert any(isinstance(p, Line) and p.role == "center" for p in first)


def test_the_cone_s_small_end_faces_the_circles_side_in_first_angle():
    """The derivation only holds if the view drawn as circles really is the
    view from the cone's small end."""
    cone = next(p for p in projection_symbol_prims((0.0, 0.0), "first") if isinstance(p, Poly))
    left_x = min(x for x, _ in cone.points)
    right_x = max(x for x, _ in cone.points)
    left_height = max(y for x, y in cone.points if x == left_x) * 2
    right_height = max(y for x, y in cone.points if x == right_x) * 2
    assert left_height == pytest.approx(4.0, abs=EPS)
    assert right_height == pytest.approx(8.0, abs=EPS)


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
