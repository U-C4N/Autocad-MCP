"""std_feature_draw: the ISO 6410 thread, and grooves drawn from registry rows.

The DIN 471 / DIN 509 / DIN 332 / ISO 3601-2 tables themselves land with Task 3
(group M). These tests register **synthetic** tables under obviously fake
standard names, so what is pinned here is that the feature draws exactly the
dimensions the registry publishes - never a number this module invented.
"""

from __future__ import annotations

import math

import pytest

from engineering.mech.standards import Coverage, register, standards
from engineering.mech.stdparts import (
    STD_FEATURE_KINDS,
    draw_std_feature,
    feature_prims,
    feature_spec,
    place_prims,
)


@pytest.fixture(scope="module", autouse=True)
def synthetic_tables():
    """Round, obviously synthetic rows - NOT DIN or ISO values."""
    if "TEST 471" in standards():
        return
    register(
        "TEST 471",
        {30.0: {"m": 2.0, "d2": 27.0}},
        Coverage(8.0, 100.0),
        "synthetic test fixture - the real DIN 471 table lands with Task 3",
    )
    register(
        "TEST 472",
        {30.0: {"m": 2.0, "d2": 33.0}},
        Coverage(8.0, 100.0),
        "synthetic test fixture - the real DIN 472 table lands with Task 3",
    )
    register(
        "TEST 509",
        {30.0: {"profile": [[0.0, 0.0], [0.0, 0.4], [2.0, 0.4], [2.5, 0.0]]}},
        Coverage(18.0, 80.0),
        "synthetic test fixture - the real DIN 509 table lands with Task 3",
    )
    register(
        "TEST 332 A",
        {2.5: {"d1": 2.5, "d2": 5.3, "t": 5.0}},
        Coverage(1.0, 10.0),
        "synthetic test fixture - the real DIN 332-1 table lands with Task 3",
    )
    register(
        "TEST 332 B",
        {2.5: {"d1": 2.5, "d2": 5.3, "d3": 6.3, "t": 6.0}},
        Coverage(1.0, 10.0),
        "synthetic test fixture - the real DIN 332-1 table lands with Task 3",
    )
    register(
        "TEST 3601",
        {3.53: {"b": 4.7, "h": 2.7}},
        Coverage(1.0, 10.0),
        "synthetic test fixture - the real ISO 3601-2 table lands with Task 3",
    )


def test_feature_kinds_are_the_five_the_spec_names():
    assert STD_FEATURE_KINDS == (
        "thread",
        "undercut",
        "ring_groove",
        "centre_hole",
        "oring_groove",
    )
    with pytest.raises(ValueError, match="oring_groove"):
        feature_prims("spline", {})


def test_external_thread_is_the_iso6410_representation():
    spec = feature_spec("thread", {"d": 20.0, "length": 30.0, "size": "M20"})
    assert spec["dims"]["pitch"] == 2.5  # ISO 261 coarse, from the fastener table
    assert "ISO 6410" in spec["source"]
    assert spec["prims"] == (
        {"type": "line", "x1": 0.0, "y1": 8.0, "x2": 30.0, "y2": 8.0, "layer": "GEOMETRY"},
        {"type": "line", "x1": 0.0, "y1": -8.0, "x2": 30.0, "y2": -8.0, "layer": "GEOMETRY"},
        {"type": "line", "x1": 30.0, "y1": -10.0, "x2": 30.0, "y2": 10.0, "layer": "GEOMETRY"},
    )


def test_internal_thread_draws_the_major_diameter_instead():
    prims = feature_prims("thread", {"d": 20.0, "length": 30.0, "pitch": 2.5, "internal": True})
    assert prims[0]["y1"] == 10.0 and prims[1]["y1"] == -10.0


def test_a_thread_without_a_size_or_a_pitch_is_refused():
    with pytest.raises(ValueError, match="never guessed"):
        feature_prims("thread", {"d": 20.0, "length": 30.0})
    with pytest.raises(ValueError, match="ISO 261"):
        feature_prims("thread", {"d": 14.0, "length": 30.0, "size": "M14"})


def test_a_size_that_does_not_match_d_is_refused():
    """Otherwise the returned dims report M20's pitch on M8 geometry, citing ISO 261."""
    with pytest.raises(ValueError, match=r"params\['size'\]='M20' is a 20.0 mm thread"):
        feature_spec("thread", {"d": 8.0, "length": 20.0, "size": "M20"})
    matching = feature_spec("thread", {"d": 8.0, "length": 20.0, "size": "M8"})
    assert matching["dims"]["pitch"] == 1.25  # ISO 261 coarse pitch of M8


def test_ring_groove_is_drawn_from_the_registry_row():
    prims = feature_prims("ring_groove", {"d": 30.0, "kind": "shaft", "standard": "TEST 471"})
    assert len(prims) == 6
    assert prims[0] == {
        "type": "line",
        "x1": 0.0,
        "y1": 15.0,
        "x2": 0.0,
        "y2": 13.5,
        "layer": "GEOMETRY",
    }
    assert prims[1] == {
        "type": "line",
        "x1": 0.0,
        "y1": 13.5,
        "x2": 2.0,
        "y2": 13.5,
        "layer": "GEOMETRY",
    }
    assert prims[3]["y1"] == -15.0
    bore = feature_prims("ring_groove", {"d": 30.0, "kind": "bore", "standard": "TEST 472"})
    assert bore[1]["y1"] == 16.5


def test_ring_groove_refuses_a_row_that_is_the_wrong_way_round():
    with pytest.raises(ValueError, match="not a shaft groove"):
        feature_prims("ring_groove", {"d": 30.0, "kind": "shaft", "standard": "TEST 472"})
    with pytest.raises(ValueError, match="DIN 471"):
        feature_prims("ring_groove", {"d": 30.0, "kind": "shaft"})


def test_oring_groove_is_a_rectangular_groove_from_the_row():
    prims = feature_prims(
        "oring_groove", {"d": 30.0, "cord": 3.53, "kind": "shaft", "standard": "TEST 3601"}
    )
    assert len(prims) == 6
    assert prims[0]["y1"] == 15.0 and prims[0]["y2"] == pytest.approx(12.3)
    assert prims[1]["x2"] == pytest.approx(4.7)


def test_centre_hole_form_a_is_a_sixty_degree_countersink():
    spec = feature_spec("centre_hole", {"size": 2.5, "form": "A", "standard": "TEST 332 A"})
    cone = math.sqrt(3.0) * (5.3 - 2.5) / 2.0
    assert spec["dims"]["cone"] == pytest.approx(cone)
    prims = spec["prims"]
    assert len(prims) == 5
    assert prims[0] == {
        "type": "line",
        "x1": 0.0,
        "y1": 2.65,
        "x2": pytest.approx(cone),
        "y2": 1.25,
        "layer": "GEOMETRY",
    }
    assert prims[2]["x2"] == 5.0 and prims[2]["y1"] == 1.25
    assert prims[4] == {
        "type": "line",
        "x1": 5.0,
        "y1": -1.25,
        "x2": 5.0,
        "y2": 1.25,
        "layer": "GEOMETRY",
    }


def test_centre_hole_form_b_adds_the_protecting_chamfer():
    prims = feature_prims("centre_hole", {"size": 2.5, "form": "B", "standard": "TEST 332 B"})
    chamfer = (6.3 - 5.3) / (2.0 * math.sqrt(3.0))
    assert len(prims) == 7
    assert prims[0]["y1"] == 3.15  # d3 / 2
    assert prims[0]["x2"] == pytest.approx(chamfer)


def test_centre_hole_form_r_is_refused_not_approximated():
    with pytest.raises(ValueError, match="forms A and B"):
        feature_prims("centre_hole", {"size": 2.5, "form": "R", "standard": "TEST 332 A"})


def test_undercut_draws_the_profile_the_table_publishes():
    prims = feature_prims("undercut", {"d": 30.0, "form": "E", "standard": "TEST 509"})
    assert len(prims) == 2
    assert prims[0] == {
        "type": "polyline",
        "points": [[0.0, 15.0], [0.0, 14.6], [2.0, 14.6], [2.5, 15.0]],
        "closed": False,
        "layer": "GEOMETRY",
    }
    assert prims[1]["points"][1] == [0.0, -14.6]


def test_undercut_refuses_a_row_without_a_profile():
    with pytest.raises(ValueError, match="carries no profile"):
        feature_prims("undercut", {"d": 30.0, "form": "E", "standard": "TEST 471"})
    with pytest.raises(ValueError, match="DIN 509"):
        feature_prims("undercut", {"d": 30.0, "form": "E"})


def test_place_prims_rotates_about_the_insertion_point():
    prims = feature_prims("thread", {"d": 20.0, "length": 30.0, "pitch": 2.5})
    placed = place_prims(prims, (100.0, 50.0), 90.0)
    assert placed[0]["x1"] == pytest.approx(92.0)
    assert placed[0]["y1"] == pytest.approx(50.0)
    assert placed[0]["x2"] == pytest.approx(92.0)
    assert placed[0]["y2"] == pytest.approx(80.0)
    with pytest.raises(ValueError, match="finite"):
        place_prims(prims, (float("inf"), 0.0))


@pytest.mark.asyncio
async def test_draw_std_feature_writes_the_primitives(backend):
    result = await draw_std_feature(
        backend, "thread", (100.0, 50.0), {"d": 20.0, "length": 30.0, "size": "M20"}
    )
    assert result["ok"] is True
    assert result["primitive_count"] == 3
    assert len(result["handles"]) == 3
    assert result["layers"] == ["GEOMETRY"]
    assert result["dims"]["pitch"] == 2.5
    info = await backend.entity_get(result["handles"][0])
    assert info.layer == "GEOMETRY"


@pytest.mark.asyncio
async def test_draw_std_feature_refuses_before_writing_anything(backend):
    before = len(await backend.entity_list())
    with pytest.raises(ValueError, match="never guessed"):
        await draw_std_feature(backend, "thread", (0.0, 0.0), {"d": 20.0, "length": 30.0})
    with pytest.raises(ValueError, match="DIN 509"):
        await draw_std_feature(backend, "undercut", (0.0, 0.0), {"d": 30.0})
    assert len(await backend.entity_list()) == before
