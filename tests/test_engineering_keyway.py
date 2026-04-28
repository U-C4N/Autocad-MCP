"""Tests for engineering.keyway — DIN 6885 lookup + drawing helpers."""

from __future__ import annotations

import pytest

from engineering.keyway import (
    draw_keyed_bore,
    draw_keyway_section,
    keyway_dimensions,
)
from engineering.layers import (
    ensure_engineering_layers,
    ensure_standard_linetypes,
)


def test_din6885_lookup_22_30_range():
    dims = keyway_dimensions(25.0)
    assert dims["width"] == 8.0
    assert dims["height"] == 7.0
    assert dims["depth_shaft"] == 4.0


def test_din6885_lookup_38_44_range():
    dims = keyway_dimensions(40.0)
    assert dims["width"] == 12.0


def test_keyway_dimensions_below_min_raises_or_clamps():
    with pytest.raises(ValueError):
        keyway_dimensions(3.0)


@pytest.mark.asyncio
async def test_draw_keyed_bore_returns_expected_handles(backend):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    res = await draw_keyed_bore(
        backend, center=(0.0, 0.0), bore_diameter=25.0,
        keyway_width=8.0, keyway_depth=4.0,
    )
    for k in ("bore", "keyway_polyline", "centerline_h", "centerline_v"):
        assert k in res
        assert res[k]


@pytest.mark.asyncio
async def test_draw_keyway_section_geometry(backend):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    res = await draw_keyway_section(
        backend, center=(0.0, 0.0), bore_diameter=25.0, face_width=40.0,
        keyway_width=8.0, keyway_depth=4.0,
    )
    assert "bore_top" in res
    assert "bore_bottom" in res
    assert isinstance(res["keyway"], list)
    assert len(res["keyway"]) == 4
    # Verify the notch top sits above the bore top by reading entities back.
    bore_top = await backend.entity_get(res["bore_top"])
    bore_top_y = bore_top.properties["start"][1]
    notch_top_handle = res["keyway"][2]  # notch_top in our return order
    notch_top_ent = await backend.entity_get(notch_top_handle)
    notch_top_y = notch_top_ent.properties["start"][1]
    assert notch_top_y > bore_top_y
