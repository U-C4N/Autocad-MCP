"""Tests for engineering.titleblock — ISO 7200 A3 layout."""

import pytest

from engineering.titleblock import TitleBlockMetadata, apply_iso_a3_titleblock

pytestmark = pytest.mark.asyncio


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


async def test_titleblock_outer_border_dimensions(backend):
    """Outer border bbox must equal A3 sheet (420 x 297 mm)."""
    result = await apply_iso_a3_titleblock(backend, metadata=_meta())
    bbox = result["bbox"]
    width = bbox["max"][0] - bbox["min"][0]
    height = bbox["max"][1] - bbox["min"][1]
    assert width == pytest.approx(420.0)
    assert height == pytest.approx(297.0)


async def test_title_text_exact_match(backend):
    """Title text in the drawing must equal metadata.title verbatim."""
    result = await apply_iso_a3_titleblock(
        backend, metadata=_meta(title="HELICAL GEAR"),
    )
    title_ent = await backend.entity_get(result["title_text"])
    assert title_ent.type == "TEXT"
    assert title_ent.properties["text"] == "HELICAL GEAR"


async def test_titleblock_creates_text_for_each_metadata_field(backend):
    """value_texts must contain a handle for every metadata field."""
    result = await apply_iso_a3_titleblock(backend, metadata=_meta())
    expected_keys = {
        "drawing_no", "revision", "sheet",
        "part_no", "material", "scale",
        "drawn_by", "checked_by", "date", "company",
    }
    assert set(result["value_texts"].keys()) == expected_keys
    # All handles must resolve to TEXT entities
    for key, handle in result["value_texts"].items():
        ent = await backend.entity_get(handle)
        assert ent.type == "TEXT", f"{key} expected TEXT, got {ent.type}"


async def test_titleblock_origin_offset(backend):
    """Passing origin=(100,50) must shift the bbox by that amount."""
    result = await apply_iso_a3_titleblock(
        backend, metadata=_meta(), origin=(100.0, 50.0),
    )
    bbox = result["bbox"]
    assert bbox["min"][0] == pytest.approx(100.0)
    assert bbox["min"][1] == pytest.approx(50.0)
    assert bbox["max"][0] == pytest.approx(520.0)
    assert bbox["max"][1] == pytest.approx(347.0)


async def test_titleblock_borders_on_titleblock_layer(backend):
    """Outer + inner borders + grid lines must be on TITLEBLOCK layer."""
    result = await apply_iso_a3_titleblock(backend, metadata=_meta())
    outer = await backend.entity_get(result["outer_border"])
    inner = await backend.entity_get(result["inner_border"])
    assert outer.layer == "TITLEBLOCK"
    assert inner.layer == "TITLEBLOCK"
    for handle in result["titleblock_lines"]:
        ent = await backend.entity_get(handle)
        assert ent.layer == "TITLEBLOCK"


async def test_titleblock_text_on_text_layer(backend):
    """Title and all value texts must be on TEXT layer."""
    result = await apply_iso_a3_titleblock(backend, metadata=_meta())
    title = await backend.entity_get(result["title_text"])
    assert title.layer == "TEXT"
    for handle in result["value_texts"].values():
        ent = await backend.entity_get(handle)
        assert ent.layer == "TEXT"


async def test_titleblock_metadata_in_result(backend):
    """Returned metadata dict must round-trip the input metadata."""
    md = _meta(title="SPUR GEAR", drawing_no="AM-2026-002")
    result = await apply_iso_a3_titleblock(backend, metadata=md)
    assert result["metadata"]["title"] == "SPUR GEAR"
    assert result["metadata"]["drawing_no"] == "AM-2026-002"
    assert result["metadata"]["company"] == "Anka-Makine"
