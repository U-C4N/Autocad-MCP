"""drawing_scale_check: tag matching and ratio statistics.

The hand-built plans put six tags at coordinates written below; every pair
distance and ratio in the comments is worked out from them.
"""

from __future__ import annotations

import math
from itertools import combinations

import pytest

from engineering.understand.scale import match_tags, scale_check
from engineering.understand.snapshot import EntityRecord, Snapshot, read_snapshot
from tests.fixtures.plant_pair import build_plant_pair

EPS = 1e-12

#: Six tags of one plant, in millimetres. The closest pair is T4100-T4500,
#: hypot(6000, 4000) = 7211.1, so every pair clears the 2 m floor.
PLAN = {
    "T4100": (0.0, 0.0),
    "T4200": (12000.0, 0.0),
    "T4300": (12000.0, 9000.0),
    "T4400": (0.0, 9000.0),
    "T4500": (6000.0, 4000.0),
    "T4600": (20000.0, 15000.0),
}


def text(handle, value, x, y):
    return EntityRecord(
        handle=handle, type="TEXT", layer="TAGS", space="Model", points=((x, y),), text=value
    )


def outline(handle, x0, y0, x1, y1):
    return EntityRecord(
        handle=handle,
        type="LWPOLYLINE",
        layer="0",
        space="Model",
        points=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        bulges=(0.0, 0.0, 0.0, 0.0),
        closed=True,
    )


def snapshot(records):
    return Snapshot(
        source="test",
        insunits=4,
        extmin=None,
        extmax=None,
        layers={},
        layouts=("Model",),
        records=tuple(records),
    )


def tags(positions, prefix="H", factor=1.0, dx=0.0):
    return [
        text(f"{prefix}{i}", tag, x * factor + dx, y * factor)
        for i, (tag, (x, y)) in enumerate(sorted(positions.items()))
    ]


def test_a_drawing_at_exactly_half_scale_is_to_scale_with_factor_one_half():
    result = scale_check(snapshot(tags(PLAN, "A", 0.5)), snapshot(tags(PLAN, "B")))
    assert result["verdict"] == "to_scale"
    assert result["factor"] == pytest.approx(0.5, abs=EPS)
    assert result["median"] == pytest.approx(0.5, abs=EPS)
    assert result["iqr"] == pytest.approx(0.0, abs=EPS)
    assert result["within_10pct"] == 1.0
    assert result["pair_count"] == 15  # C(6, 2)
    assert result["pairs_below_floor"] == 0
    assert [m["tag"] for m in result["matched"]] == sorted(PLAN)
    assert result["ambiguous"] == []
    assert result["units"]["a"]["unit"] == result["units"]["b"]["unit"] == "mm"


def test_a_rearranged_drawing_is_schematic():
    # The same six tags, shuffled onto each other's places and one moved off.
    rearranged = {
        "T4100": (12000.0, 9000.0),
        "T4200": (0.0, 0.0),
        "T4300": (20000.0, 15000.0),
        "T4400": (6000.0, 4000.0),
        "T4500": (12000.0, 0.0),
        "T4600": (0.0, 20000.0),
    }
    result = scale_check(snapshot(tags(rearranged, "A")), snapshot(tags(PLAN, "B")))
    ratios = sorted(
        math.dist(rearranged[i], rearranged[j]) / math.dist(PLAN[i], PLAN[j])
        for i, j in combinations(sorted(PLAN), 2)
    )
    median = ratios[7]  # 15 ratios: the 8th
    within = sum(1 for r in ratios if abs(r - median) <= 0.1 * median) / 15
    assert result["median"] == pytest.approx(median, abs=EPS)
    assert result["within_10pct"] == pytest.approx(within, abs=EPS)
    assert within <= 0.5
    assert result["verdict"] == "schematic"
    assert result["factor"] is None


def test_a_tag_found_twice_is_ambiguous_and_takes_no_part():
    # T4400 appears twice on the reference: neither copy is matched.
    reference = [*tags(PLAN, "B"), text("DUP", "T4400", 3000.0, 12000.0)]
    matched = match_tags(snapshot(tags(PLAN, "A", 0.5)), snapshot(reference))
    assert "T4400" not in matched["matched"]
    assert matched["ambiguous"] == ["T4400"]
    assert matched["occurrences"] == {"T4400": {"a": 1, "b": 2}}
    assert matched["matched"]["T4200"] == ((6000.0, 0.0), (12000.0, 0.0))

    result = scale_check(snapshot(tags(PLAN, "A", 0.5)), snapshot(reference))
    assert result["ambiguous"] == ["T4400"]
    assert "T4400" not in [m["tag"] for m in result["matched"]]
    assert result["pair_count"] == 10  # C(5, 2): the ambiguous tag is in no pair
    assert all("T4400" not in row["tags"] for row in result["pairs"])
    assert result["verdict"] == "to_scale"
    assert result["factor"] == pytest.approx(0.5, abs=EPS)


def test_two_plan_copies_on_the_reference_resolve_to_one_copy():
    # The reference holds the plan twice, 60 000 apart, each inside its own
    # outline: over the whole drawing every tag is ambiguous, inside one copy
    # every tag is unique.
    reference = [
        outline("O1", -1000.0, -1000.0, 21000.0, 16000.0),
        *tags(PLAN, "L"),
        outline("O2", 59000.0, -1000.0, 81000.0, 16000.0),
        *tags(PLAN, "R", dx=60000.0),
    ]
    whole = match_tags(snapshot(tags(PLAN, "A", 0.5)), snapshot(reference))
    assert whole["matched"] == {}
    assert whole["ambiguous"] == sorted(PLAN)
    result = scale_check(snapshot(tags(PLAN, "A", 0.5)), snapshot(reference))
    assert result["scope"] == {"cluster_a": None, "cluster_b": "C1"}
    assert len(result["matched"]) == 6
    assert result["ambiguous"] == []
    assert result["verdict"] == "to_scale"
    assert result["factor"] == pytest.approx(0.5, abs=EPS)


def test_cyrillic_look_alike_tags_match_their_latin_twins():
    # "Т4100" typed with a Cyrillic Т (U+0422) is the layout's Latin T4100.
    cyrillic = {("Т" + tag[1:] if tag == "T4100" else tag): xy for tag, xy in PLAN.items()}
    matched = match_tags(snapshot(tags(cyrillic, "A")), snapshot(tags(PLAN, "B")))
    assert "T4100" in matched["matched"]
    assert len(matched["matched"]) == 6


def test_fewer_than_three_matched_tags_is_insufficient():
    two = {k: PLAN[k] for k in ("T4100", "T4200")}
    result = scale_check(snapshot(tags(two, "A")), snapshot(tags(PLAN, "B")))
    assert result["verdict"] == "insufficient"
    assert result["factor"] is None
    assert result["pair_count"] == 1


def test_pairs_closer_than_the_floor_on_the_reference_are_left_out():
    # With a 13 m floor, the pairs of PLAN at least 13 000 apart on the
    # reference are T4100-T4300 (15 000), T4100-T4600 (25 000), T4200-T4600
    # (hypot(8000, 15000) = 17 000), T4400-T4600 (hypot(20000, 6000) = 20 881),
    # T4500-T4600 (hypot(14000, 11000) = 17 804), T4200-T4400 (15 000): six.
    result = scale_check(
        snapshot(tags(PLAN, "A", 0.5)), snapshot(tags(PLAN, "B")), min_pair_mm=13000.0
    )
    assert result["pair_count"] == 6
    assert result["pairs_below_floor"] == 9
    assert result["verdict"] == "to_scale"


@pytest.mark.parametrize("floor", [0.0, -5.0, float("nan"), float("inf")])
def test_a_floor_that_is_not_a_positive_number_is_refused(floor):
    with pytest.raises(ValueError, match="min_pair_mm: must be a positive number"):
        scale_check(snapshot(tags(PLAN, "A")), snapshot(tags(PLAN, "B")), min_pair_mm=floor)


def test_an_unknown_cluster_is_refused_with_the_ids_the_drawing_has():
    with pytest.raises(ValueError, match=r"cluster_b: no cluster 'C99'"):
        match_tags(snapshot(tags(PLAN, "A")), snapshot(tags(PLAN, "B")), cluster_b="C99")


def test_the_synthetic_pid_is_schematic_against_its_layout(tmp_path):
    truth = build_plant_pair(tmp_path)
    result = scale_check(read_snapshot(truth["pid"]), read_snapshot(truth["layout"]))
    assert result["verdict"] == truth["scale_verdict"]
    assert len(result["matched"]) >= 3
    # The layout's two plan copies: the check must pick one of them.
    assert result["scope"]["cluster_b"] is not None


def test_a_wiring_callout_is_not_a_second_place_for_its_panel():
    """``Wiring to CP-2`` names the panel it points at; it is not written at the
    panel. Read as a tag it would make every panel with a callout ambiguous
    (and ``Wiring to CP1`` a phantom tag ``CP1``), so the callout takes no part
    - the same rule the line-network reader (`network.tag_occurrences`) keeps."""
    plan = {**PLAN, "CP-2": (3000.0, 15000.0)}
    callouts = [
        text("W1", "Wiring to CP-2", 12000.0, 0.0),
        text("W2", "Wiring to CP1", 0.0, 9000.0),
    ]
    result = scale_check(snapshot(tags(plan, "A")), snapshot(tags(plan, "B") + callouts))
    assert result["ambiguous"] == []
    assert "CP-2" in {row["tag"] for row in result["matched"]}
    assert result["only_b"] == []
    assert result["pair_count"] == math.comb(len(plan), 2)


def test_the_synthetic_pair_reproduces_the_generators_own_statistics(tmp_path):
    """The generator computes its truth from the positions it placed: 11 shared
    tags, 55 pairs, 21.8 % within +/-10 % of the median. The check must see the
    same tags - a wiring callout counted as a panel would drop to 45."""
    truth = build_plant_pair(tmp_path)
    result = scale_check(read_snapshot(truth["pid"]), read_snapshot(truth["layout"]))
    assert result["pair_count"] == truth["scale"]["overall"]["pairs"] == 55
    assert result["within_10pct"] == pytest.approx(truth["scale"]["overall"]["within_10pct"])
