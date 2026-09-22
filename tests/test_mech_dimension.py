"""Dimension intents, ISO 286 fits, the hole table and the overlap-free layout.

The layout test is the one that matters: it asserts the placed text rectangles
are pairwise disjoint, computed by the same helper the layout uses, so "no
overlapping dimension text" is a measured property rather than a claim.
"""

from __future__ import annotations

import itertools

import pytest

from engineering.mech.dimension import (
    DIM_STYLES,
    dimension_intents,
    hole_table_rows,
    intent_text,
    layout_dimensions,
    measured_value,
    resolve_tolerance,
    text_rects,
)
from engineering.mech.features import Hole, Keyway
from engineering.mech.part import PrismaticPart, RevolvedPart, Segment
from engineering.mech.primitives import DimIntent
from engineering.mech.views import build_view


def shaft(*features) -> RevolvedPart:
    return RevolvedPart(
        name="shaft",
        segments=(Segment(20.0, 25.0), Segment(40.0, 30.0), Segment(15.0, 20.0)),
        features=tuple(features),
    )


def plate(*holes) -> PrismaticPart:
    return PrismaticPart(
        name="plate",
        outline=((0.0, 0.0), (80.0, 0.0), (80.0, 50.0), (0.0, 50.0)),
        thickness=8.0,
        features=tuple(holes),
    )


def grid(count: int, diameter: float = 6.0):
    return tuple(Hole(id=f"h{i}", x=5.0 + 7.0 * i, y=25.0, diameter=diameter) for i in range(count))


def test_the_three_styles_are_the_ones_the_spec_names():
    assert DIM_STYLES == ("chain", "baseline", "ordinate")


def test_measured_value_and_text_follow_the_intent():
    linear = DimIntent(kind="linear", p1=(0.0, 0.0), p2=(20.0, 0.0))
    assert measured_value(linear) == 20.0
    assert intent_text(linear) == "20"
    override = DimIntent(kind="diameter", p1=(-20.0, 0.0), p2=(20.0, 0.0), text_override="M20")
    assert measured_value(override) == 40.0
    assert intent_text(override) == "M20"


# -- ISO 286 fits and ISO 129 tolerances -------------------------------------


def test_a_h7_fit_on_a_40_mm_bore_resolves_to_plus_25_microns_over_zero():
    intent = DimIntent(kind="diameter", p1=(-20.0, 0.0), p2=(20.0, 0.0), fit="H7")
    resolved = resolve_tolerance(intent)
    assert resolved["tol_upper"] == pytest.approx(0.025)
    assert resolved["tol_lower"] == pytest.approx(0.0)
    assert resolved["tol_mode"] == "deviation"
    assert resolved["text_override"] == "<> H7"


def test_a_shaft_fit_keeps_the_build_dim_override_sign_convention():
    intent = DimIntent(kind="diameter", p1=(-10.0, 0.0), p2=(10.0, 0.0), fit="g6")
    resolved = resolve_tolerance(intent)
    # ISO 286 g6 at 20 mm: es = -0.007, ei = -0.020; build_dim_override wants the
    # lower deviation as a positive magnitude of the minus side.
    assert resolved["tol_upper"] == pytest.approx(-0.007)
    assert resolved["tol_lower"] == pytest.approx(0.020)


def test_an_explicit_tolerance_passes_through_and_is_validated():
    intent = DimIntent(
        kind="linear",
        p1=(0.0, 0.0),
        p2=(30.0, 0.0),
        tol={"mode": "symmetric", "upper": 0.1},
    )
    resolved = resolve_tolerance(intent)
    assert resolved["tol_mode"] == "symmetric"
    assert resolved["tol_upper"] == pytest.approx(0.1)
    bad = DimIntent(kind="linear", p1=(0.0, 0.0), p2=(30.0, 0.0), tol={"mode": "loose"})
    with pytest.raises(ValueError, match="tol_mode"):
        resolve_tolerance(bad)


def test_a_fit_and_an_explicit_tolerance_together_are_refused():
    intent = DimIntent(
        kind="diameter",
        p1=(-20.0, 0.0),
        p2=(20.0, 0.0),
        fit="H7",
        tol={"mode": "symmetric", "upper": 0.1},
    )
    with pytest.raises(ValueError, match="not both"):
        resolve_tolerance(intent)


def test_a_fit_outside_the_iso_286_tables_is_refused_by_name():
    intent = DimIntent(kind="diameter", p1=(-400.0, 0.0), p2=(400.0, 0.0), fit="H7")
    with pytest.raises(ValueError, match="ISO 286"):
        resolve_tolerance(intent)


def test_a_fit_with_no_measurable_nominal_is_refused_rather_than_guessed():
    intent = DimIntent(kind="diameter", p1=(5.0, 0.0), p2=(5.0, 0.0), fit="H7")
    with pytest.raises(ValueError, match="nominal"):
        resolve_tolerance(intent)


# -- collecting the intents --------------------------------------------------


def test_the_front_view_of_a_shaft_gets_its_overall_length_and_each_diameter_once():
    view = build_view(shaft(), "front")
    intents = dimension_intents(shaft(), view)
    lengths = [i for i in intents if i.kind == "linear"]
    diameters = [i for i in intents if i.kind == "diameter"]
    assert any(measured_value(i) == 75.0 for i in lengths)
    assert sorted({round(measured_value(i), 6) for i in diameters}) == [20.0, 25.0, 30.0]


def test_chain_and_baseline_place_the_same_stations_differently():
    part = shaft()
    view = build_view(part, "front")
    chain = [i for i in dimension_intents(part, view, style="chain") if i.kind == "linear"]
    baseline = [i for i in dimension_intents(part, view, style="baseline") if i.kind == "linear"]
    assert {round(measured_value(i), 6) for i in chain} >= {20.0, 40.0, 15.0}
    assert {round(measured_value(i), 6) for i in baseline} >= {20.0, 60.0, 75.0}


def test_ordinate_measures_every_station_from_the_origin():
    part = shaft()
    view = build_view(part, "front")
    intents = [i for i in dimension_intents(part, view, style="ordinate") if i.kind == "linear"]
    assert all(i.p1[0] == 0.0 for i in intents)


def test_a_feature_intent_survives_collection_and_keeps_its_feature_id():
    part = shaft(Keyway(id="kw", segment=1, length=20.0))
    view = build_view(part, "front")
    intents = dimension_intents(part, view)
    assert any(i.feature == "kw" for i in intents)


def test_iso129_the_same_measurement_is_not_dimensioned_twice():
    part = shaft()
    view = build_view(part, "front")
    intents = dimension_intents(part, view)
    keys = [
        (i.kind, round(i.p1[0], 6), round(i.p1[1], 6), round(i.p2[0], 6), round(i.p2[1], 6))
        for i in intents
    ]
    assert len(keys) == len(set(keys))


def test_a_prismatic_front_view_gets_its_two_overall_sizes():
    view = build_view(plate(), "front")
    intents = dimension_intents(plate(), view)
    assert sorted({round(measured_value(i), 6) for i in intents if i.kind == "linear"}) == [
        50.0,
        80.0,
    ]


# -- the hole table ----------------------------------------------------------


def test_under_the_threshold_the_holes_keep_their_own_diameters_and_there_is_no_table():
    part = plate(*grid(4))
    view = build_view(part, "front")
    intents = dimension_intents(part, view, hole_table_threshold=8)
    assert sum(1 for i in intents if i.kind == "diameter") == 4
    assert hole_table_rows(part, view) == ()


def test_over_the_threshold_the_hole_diameters_move_into_a_table():
    part = plate(*grid(10))
    view = build_view(part, "front")
    intents = dimension_intents(part, view, hole_table_threshold=8)
    assert not any(i.kind == "diameter" for i in intents)
    rows = hole_table_rows(part, view)
    assert len(rows) == 10
    assert {row["tag"] for row in rows} == {"A"}
    assert rows[0]["qty"] == 10
    assert rows[0]["d"] == 6.0
    assert set(rows[0]) == {"tag", "x", "y", "d", "qty", "note"}


def test_two_hole_sizes_get_two_tags():
    part = plate(
        *grid(6), *(Hole(id=f"b{i}", x=5.0 + 9.0 * i, y=40.0, diameter=10.0) for i in range(4))
    )
    view = build_view(part, "front")
    rows = hole_table_rows(part, view)
    assert {row["tag"] for row in rows} == {"A", "B"}
    by_tag = {row["tag"]: row["qty"] for row in rows}
    assert by_tag == {"A": 6, "B": 4}


def test_a_tapped_hole_carries_its_designation_in_the_note():
    part = plate(
        *(Hole(id=f"t{i}", x=5.0 + 9.0 * i, y=25.0, diameter=10.0, thread="M10") for i in range(9))
    )
    view = build_view(part, "front")
    rows = hole_table_rows(part, view)
    assert rows[0]["note"] == "M10"


# -- the layout, and its overlap proof ---------------------------------------


def test_the_layout_places_every_intent_and_the_text_rectangles_are_disjoint():
    part = shaft(Keyway(id="kw", segment=1, length=20.0))
    view = build_view(part, "front")
    intents = dimension_intents(part, view)
    placed = layout_dimensions(view, intents)
    assert len(placed) == len(intents)
    assert all(intent.text_at is not None for intent in placed)
    rects = text_rects(placed)
    for a, b in itertools.combinations(rects, 2):
        overlap_x = min(a[2], b[2]) - max(a[0], b[0])
        overlap_y = min(a[3], b[3]) - max(a[1], b[1])
        assert overlap_x <= 1e-9 or overlap_y <= 1e-9, f"{a} overlaps {b}"


def test_a_crowded_prismatic_view_still_lays_out_disjointly():
    part = plate(*grid(6))
    view = build_view(part, "front")
    placed = layout_dimensions(view, dimension_intents(part, view, hole_table_threshold=100))
    rects = text_rects(placed)
    for a, b in itertools.combinations(rects, 2):
        overlap_x = min(a[2], b[2]) - max(a[0], b[0])
        overlap_y = min(a[3], b[3]) - max(a[1], b[1])
        assert overlap_x <= 1e-9 or overlap_y <= 1e-9, f"{a} overlaps {b}"


def test_horizontal_dimensions_stack_below_the_view_and_vertical_ones_to_its_left():
    part = shaft()
    view = build_view(part, "front")
    placed = layout_dimensions(view, dimension_intents(part, view), offset=10.0, step=8.0)
    horizontal = [i for i in placed if i.kind == "linear" and abs(i.p1[1] - i.p2[1]) < 1e-9]
    assert horizontal and all(i.text_at[1] <= view.bbox[1] - 10.0 + 1e-9 for i in horizontal)
    diameters = [i for i in placed if i.kind == "diameter"]
    assert diameters and all(i.text_at[0] >= view.bbox[2] + 10.0 - 1e-9 for i in diameters)


def test_the_step_is_never_smaller_than_the_text_it_has_to_clear():
    part = shaft()
    view = build_view(part, "front")
    placed = layout_dimensions(view, dimension_intents(part, view), offset=10.0, step=1.0)
    rects = text_rects(placed)
    for a, b in itertools.combinations(rects, 2):
        overlap_x = min(a[2], b[2]) - max(a[0], b[0])
        overlap_y = min(a[3], b[3]) - max(a[1], b[1])
        assert overlap_x <= 1e-9 or overlap_y <= 1e-9


def test_an_unknown_style_is_refused():
    part = shaft()
    view = build_view(part, "front")
    with pytest.raises(ValueError, match="chain"):
        dimension_intents(part, view, style="freehand")
