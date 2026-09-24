"""Declared versus inferred units, and robust extents, on hand-built snapshots.

Every expected number is worked out in the comment beside it from the
coordinates written here; nothing is read back from the implementation.
"""

from __future__ import annotations

import math

import pytest

from engineering.understand.snapshot import EntityRecord, Snapshot
from engineering.understand.units import entity_box, infer_units, robust_extents

EPS = 1e-9


def line(handle, a, b, layer="WALL"):
    return EntityRecord(handle=handle, type="LINE", layer=layer, space="Model", points=(a, b))


def text(handle, value, x, y, height, layer="TEXT"):
    return EntityRecord(
        handle=handle,
        type="TEXT",
        layer=layer,
        space="Model",
        points=((x, y),),
        text=value,
        height=height,
    )


def rect(prefix, x0, y0, x1, y1, layer="WALL"):
    return [
        line(f"{prefix}1", (x0, y0), (x1, y0), layer),
        line(f"{prefix}2", (x1, y0), (x1, y1), layer),
        line(f"{prefix}3", (x1, y1), (x0, y1), layer),
        line(f"{prefix}4", (x0, y1), (x0, y0), layer),
    ]


def snapshot(records, insunits):
    return Snapshot(
        source="test",
        insunits=insunits,
        extmin=None,
        extmax=None,
        layers={},
        layouts=("Model",),
        records=tuple(records),
    )


def room_in_mm():
    """One 6000 x 4000 room between 200 mm double-line walls, labelled with
    250-unit text: outer outline (0,0)-(6400,4400), inner (200,200)-(6200,4200)."""
    return [
        *rect("O", 0.0, 0.0, 6400.0, 4400.0),
        *rect("I", 200.0, 200.0, 6200.0, 4200.0),
        text("T1", "ROOM 101 PROCESS", 1000.0, 2000.0, 250.0),
        text("T2", "T4100", 3000.0, 1000.0, 250.0),
    ]


def test_inches_declared_over_millimetre_geometry_is_flagged():
    # Inner room face 6000 x 4000 = 2.4e7 unit^2: 24 m2 as mm (plausible),
    # 15 484 m2 as inches (out of the 1-5000 m2 band). Wall pairs 200 apart:
    # 0.2 m as mm, 5.08 m as inches. Text 250: 0.25 m as mm, 6.35 m as inches.
    result = infer_units(snapshot(room_in_mm(), insunits=1), room_areas=[6000.0 * 4000.0])
    assert result["declared"] == {"code": 1, "name": "in", "label": "Inches"}
    assert result["inferred"] == "mm"
    assert result["mm_per_unit"] == 1.0
    assert result["agree"] is False
    assert result["warning"].startswith("INSUNITS declares Inches (1) but the geometry reads as mm")
    kinds = {e["kind"]: e for e in result["evidence"]}
    assert kinds["wall_thickness"]["value"] == pytest.approx(200.0, abs=EPS)
    assert kinds["wall_thickness"]["plausible"] == ["mm"]
    assert kinds["room_area"]["as"]["mm"] == pytest.approx(24.0, abs=EPS)
    assert kinds["room_area"]["as"]["in"] == pytest.approx(24_000_000.0 * 0.0254**2, abs=EPS)
    assert "in" not in kinds["text_height"]["plausible"]  # 6.35 m of lettering


def test_millimetres_declared_over_the_same_geometry_agree():
    result = infer_units(snapshot(room_in_mm(), insunits=4), room_areas=[24_000_000.0])
    assert result["inferred"] == "mm"
    assert result["agree"] is True
    assert result["warning"] is None
    assert 0.0 < result["confidence"] <= 0.9


def test_one_weak_item_never_overrules_the_header():
    # Only an overall size: 20 000 units is 20 m as mm, 200 m as cm, 508 m as
    # inches (all in the 0.01-2000 m band) and 20 km as metres (out of it).
    # mm, cm and in score the size's 0.25 each; "m" scores the header's 0.5
    # alone and still wins - the declared metre stands.
    records = [
        line("A", (0.0, 0.0), (20000.0, 0.0), "0"),
        line("B", (0.0, 0.0), (0.0, 5000.0), "0"),
    ]
    result = infer_units(snapshot(records, insunits=6))
    assert result["inferred"] == "m"
    assert result["agree"] is True


def test_no_evidence_keeps_the_declared_unit_at_low_confidence():
    result = infer_units(snapshot([], insunits=4))
    assert result["inferred"] == "mm"
    assert result["confidence"] == 0.4
    assert result["evidence"] == []


def test_unitless_with_no_evidence_settles_nothing():
    result = infer_units(snapshot([], insunits=0))
    assert result["inferred"] is None
    assert result["mm_per_unit"] is None
    assert result["warning"].endswith("lengths stay in drawing units.")


def test_a_far_outlier_is_found_by_handle_and_left_out_of_the_robust_box():
    records = [
        line(f"L{i}", (i * 1000.0, 0.0), (i * 1000.0 + 500.0, 800.0), "0") for i in range(12)
    ]
    far = line("FAR", (-5_000_000_000.0, 0.0), (-5_000_000_000.0 + 10.0, 0.0), "0")
    result = robust_extents([*records, far])
    assert [o["handle"] for o in result["outliers"]] == ["FAR"]
    # Robust box: x from 0 to 11 * 1000 + 500 = 11500, y from 0 to 800.
    assert result["robust"] == pytest.approx([0.0, 0.0, 11500.0, 800.0], abs=EPS)
    assert result["computed"][0] == pytest.approx(-5_000_000_000.0, abs=EPS)
    # The outlier's centre (-4 999 999 995, 0) is 4 999 999 995 from the box.
    assert result["outliers"][0]["distance"] == pytest.approx(4_999_999_995.0, abs=1e-3)
    # Stretch: hypot(5 000 011 500, 800) / hypot(11 500, 800) - the computed
    # diagonal over the robust one.
    expected = math.hypot(5_000_011_500.0, 800.0) / math.hypot(11500.0, 800.0)
    assert result["stretch"] == pytest.approx(expected, rel=1e-12)


def test_the_edge_walls_of_a_plan_are_not_outliers():
    # The room's centres crowd its middle (the vertical walls centre on
    # y 2200, the label on y 2000), so a per-axis fence on centres would cut
    # the top and bottom walls off; grown from the middle, every wall joins.
    result = robust_extents(room_in_mm())
    assert result["outliers"] == []
    assert result["robust"] == pytest.approx([0.0, 0.0, 6400.0, 4400.0], abs=EPS)
    assert result["stretch"] == pytest.approx(1.0, abs=EPS)


def test_fewer_than_four_entities_have_no_outliers():
    records = [line("A", (0.0, 0.0), (1.0, 0.0), "0"), line("B", (1e9, 0.0), (1e9 + 1.0, 0.0), "0")]
    assert robust_extents(records)["outliers"] == []


def test_entity_box_grows_a_circle_by_its_radius():
    circle = EntityRecord(
        handle="C", type="CIRCLE", layer="0", space="Model", points=((10.0, 20.0),), radius=5.0
    )
    assert entity_box(circle) == (5.0, 15.0, 15.0, 25.0)
    boxed = EntityRecord(handle="B", type="INSERT", layer="0", space="Model", bbox=(1, 2, 3, 4))
    assert entity_box(boxed) == (1.0, 2.0, 3.0, 4.0)
    assert entity_box(EntityRecord(handle="N", type="HATCH", layer="0", space="Model")) is None
