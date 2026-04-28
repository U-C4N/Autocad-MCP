"""Tests for engineering.gear — pure-math + drawing orchestration."""

from __future__ import annotations

import math

import pytest

from engineering.gear import (
    draw_gear_section_aa,
    draw_helical_gear_front_view,
    generate_full_gear_outline,
    generate_involute_flank,
    involute_xy,
)
from engineering.layers import (
    ensure_engineering_layers,
    ensure_standard_linetypes,
)


# ---------------------------------------------------------------------------
# Pure math (sync)
# ---------------------------------------------------------------------------


def test_involute_xy_at_zero_returns_base_point():
    x, y = involute_xy(10.0, 0.0)
    assert math.isclose(x, 10.0, abs_tol=1e-9)
    assert math.isclose(y, 0.0, abs_tol=1e-9)


def test_generate_involute_flank_min_30_points():
    flank = generate_involute_flank(base_r=20.0, root_r=18.0, outer_r=25.0, n_points=5)
    assert len(flank) >= 30


def test_generate_involute_flank_radius_progression():
    base_r, root_r, outer_r = 20.0, 18.0, 25.0
    flank = generate_involute_flank(base_r=base_r, root_r=root_r, outer_r=outer_r)
    first_r = math.hypot(*flank[0])
    last_r = math.hypot(*flank[-1])
    assert math.isclose(first_r, root_r, abs_tol=1e-6)
    assert math.isclose(last_r, outer_r, rel_tol=1e-3)


def test_generate_full_gear_outline_closed():
    pts = generate_full_gear_outline(module=3.0, teeth=24)
    assert pts[0] == pts[-1]


def test_generate_full_gear_outline_point_count():
    teeth = 24
    pts = generate_full_gear_outline(module=3.0, teeth=teeth)
    # At minimum: teeth * (40 right + 40 left) = 1920 unique flank points,
    # plus tip and root arc samples. Closing duplicate adds 1.
    assert len(pts) >= teeth * 40


# ---------------------------------------------------------------------------
# Drawing orchestration (async, ezdxf backend)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helical_gear_front_view_returns_metadata(backend):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    res = await draw_helical_gear_front_view(
        backend, module=3, teeth=24, helix_angle=20.0, hand="RH",
        center=(0.0, 0.0),
    )
    expected_keys = {
        "outline", "pitch_circle", "base_circle", "outer_circle", "root_circle",
        "helix_symbol", "helix_label", "bore", "keyway", "metadata",
    }
    assert expected_keys.issubset(res.keys())
    md = res["metadata"]
    for k in (
        "module", "teeth", "pressure_angle", "helix_angle", "hand", "center",
        "bore_diameter", "keyway_width", "keyway_depth",
        "pitch_radius", "outer_radius", "base_radius", "root_radius",
    ):
        assert k in md, f"metadata missing key {k!r}"


@pytest.mark.asyncio
async def test_helical_gear_with_bore_and_keyway_handles_present(backend):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    res = await draw_helical_gear_front_view(
        backend, module=3, teeth=24, helix_angle=20.0,
        bore_diameter=25.0, keyway_width=8.0, keyway_depth=4.0,
    )
    assert res["bore"] is not None
    assert res["keyway"] is not None
    assert "bore" in res["keyway"]
    assert "keyway_polyline" in res["keyway"]


@pytest.mark.asyncio
async def test_helical_gear_metadata_radii_correct(backend):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    res = await draw_helical_gear_front_view(
        backend, module=3, teeth=24, helix_angle=20.0, pressure_angle=20.0,
    )
    md = res["metadata"]
    assert math.isclose(md["pitch_radius"], 36.0, abs_tol=1e-6)
    assert math.isclose(md["outer_radius"], 39.0, abs_tol=1e-6)
    assert math.isclose(md["base_radius"], 36.0 * math.cos(math.radians(20.0)), abs_tol=1e-3)
    assert math.isclose(md["root_radius"], 32.25, abs_tol=1e-6)


@pytest.mark.asyncio
async def test_section_aa_does_not_draw_helix_lines(backend):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    front = await draw_helical_gear_front_view(
        backend, module=3, teeth=24, helix_angle=20.0,
        bore_diameter=25.0, keyway_width=8.0, keyway_depth=4.0,
    )
    section = await draw_gear_section_aa(
        backend, gear_metadata=front["metadata"],
        x_offset=200.0, face_width=40.0,
    )
    assert "helix" not in section
    assert "helix_symbol" not in section
