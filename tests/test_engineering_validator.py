"""Tests for engineering.validator — 8-step pre-completion validator."""

import os

import pytest

from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes
from engineering.validator import DrawingValidator

pytestmark = pytest.mark.asyncio


def _codes(result) -> set[str]:
    return {f.code for f in result.findings}


async def _ensure_hidden_linetype(backend) -> None:
    """ezdxf doesn't ship a HIDDEN pattern; add a synthetic one so the validator passes."""
    doc = backend._require_doc()
    if "HIDDEN" not in doc.linetypes:
        doc.linetypes.add(
            "HIDDEN",
            pattern=[6.35, 5.0, -1.27],
            description="Hidden _ _ _ _ _ _",
        )


async def _setup_clean(backend, tmp_path):
    """Bootstrap layers/linetypes, draw a bore + a real DIMENSION, save to disk."""
    await ensure_standard_linetypes(backend)
    await _ensure_hidden_linetype(backend)
    await ensure_engineering_layers(backend)
    # Bore on GEOMETRY layer
    await backend.entity_create_circle(0, 0, 12.5, layer="GEOMETRY")
    # Real dimension entity
    await backend.dimension_linear(0, 0, 25, 0, dim_x=12.5, dim_y=20, layer="DIM")
    save_path = str(tmp_path / "clean.dxf")
    await backend.drawing_save_as(save_path)
    return save_path


async def test_validator_passes_clean_drawing(backend, tmp_path):
    await _setup_clean(backend, tmp_path)
    result = await DrawingValidator().run(backend)
    # Should have zero errors; warnings/info okay (e.g. screenshot info)
    assert result.ok is True, f"unexpected findings: {[f.code for f in result.findings]}"
    assert result.summary["error"] == 0


async def test_validator_catches_unsaved_drawing(backend):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    await backend.entity_create_circle(0, 0, 10, layer="GEOMETRY")
    # Do not save
    result = await DrawingValidator().run(backend)
    assert "not_saved" in _codes(result)
    assert result.ok is False


async def test_validator_catches_missing_file(backend, tmp_path):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    save_path = str(tmp_path / "vanish.dxf")
    await backend.drawing_save_as(save_path)
    # Delete the file out from under the backend
    os.remove(save_path)
    result = await DrawingValidator().run(backend)
    assert "file_missing" in _codes(result)
    assert result.ok is False


async def test_validator_catches_helical_spur_title(backend, tmp_path):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    await backend.entity_create_text(
        text="HELICAL SPUR GEAR", x=0, y=0, height=5, layer="TEXT",
    )
    await backend.drawing_save_as(str(tmp_path / "tb.dxf"))
    result = await DrawingValidator().run(backend, expected={"helix_angle": 20})
    codes = _codes(result)
    assert "title_helical_spur" in codes or "title_says_spur_for_helical" in codes
    assert result.ok is False


async def test_validator_catches_fake_dimension_text(backend, tmp_path):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    await backend.entity_create_text(
        text="Ø78", x=10, y=10, height=3.5, layer="DIM",
    )
    await backend.drawing_save_as(str(tmp_path / "fake.dxf"))
    result = await DrawingValidator().run(backend)
    assert "fake_dimension_text" in _codes(result)


async def test_validator_catches_hidden_orphans(backend, tmp_path):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    info = await backend.entity_create_circle(0, 0, 10, layer="GEOMETRY")
    await backend.entity_set_properties(info.handle, visible=False)
    await backend.drawing_save_as(str(tmp_path / "hidden.dxf"))
    result = await DrawingValidator().run(backend)
    assert "hidden_orphans" in _codes(result)
    assert result.ok is False


async def test_validator_catches_missing_linetypes(backend, tmp_path):
    """Fresh drawing without ensure_standard_linetypes lacks CENTER/HIDDEN/PHANTOM."""
    # Skip the bootstrap step entirely
    await backend.entity_create_line(0, 0, 100, 0)
    await backend.drawing_save_as(str(tmp_path / "bare.dxf"))
    result = await DrawingValidator().run(backend)
    codes = _codes(result)
    assert "linetypes_missing" in codes
    # And the missing detail names them
    missing_finding = next(f for f in result.findings if f.code == "linetypes_missing")
    missing_set = set(missing_finding.detail.get("missing", []))
    assert {"center", "hidden", "phantom"}.issubset(missing_set)


async def test_validator_bore_missing_when_expected(backend, tmp_path):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    # No circle drawn at all
    await backend.drawing_save_as(str(tmp_path / "nobore.dxf"))
    result = await DrawingValidator().run(
        backend, expected={"must_have_bore": True},
    )
    assert "bore_missing" in _codes(result)


async def test_validation_result_to_dict_serializes(backend):
    result = await DrawingValidator().run(backend)
    d = result.to_dict()
    assert "ok" in d
    assert "summary" in d
    assert "findings" in d
    assert isinstance(d["findings"], list)
