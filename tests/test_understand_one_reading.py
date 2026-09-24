"""One reading of a drawing: the takeoffs see what drawing_understand and drawing_scale_check see.

Groups U and N were built side by side from one base, so Task 6 carried narrow
stand-ins: its own unit bands, a 2 % grid of plan copies, a scale statistic
without room groups, its own room faces. On main the takeoffs read units, plan
copies, scale and rooms through group U's readers, so the four tools never
give one drawing two units, two plan copies, two verdicts or two sets of rooms.
"""

from __future__ import annotations

import pytest

from engineering.understand import takeoff
from engineering.understand.describe import describe, drawing_units, prepare
from engineering.understand.network import build_network
from engineering.understand.scale import scale_check
from engineering.understand.snapshot import Snapshot, read_snapshot
from tests.fixtures.plant_pair import build_plant_pair


@pytest.fixture(scope="module")
def plant(tmp_path_factory):
    truth = build_plant_pair(tmp_path_factory.mktemp("one_reading"))
    return truth, read_snapshot(truth["pid"]), read_snapshot(truth["layout"])


def empty(insunits):
    return Snapshot(
        source="empty.dxf",
        insunits=insunits,
        extmin=None,
        extmax=None,
        layers={},
        layouts=(),
        records=(),
    )


def test_one_unit_per_drawing(plant):
    _truth, pid, layout = plant
    check = scale_check(pid, layout)
    for snap, side in ((pid, "a"), (layout, "b")):
        unit = takeoff.unit_of(snap)
        assert unit["inferred"] == describe(snap)["units"]["inferred"]
        assert unit["inferred"] == check["units"][side]["unit"]
    assert takeoff.unit_of(layout)["inferred"] == "mm"  # INSUNITS says inches: Review Focus 5


def test_the_takeoff_unit_carries_drawing_units_evidence_and_warning(plant):
    _truth, pid, layout = plant
    for snap in (pid, layout):
        mine, theirs = takeoff.unit_of(snap), drawing_units(snap)
        assert mine["declared"] == theirs["declared"]["name"]
        assert mine["m_per_unit"] == pytest.approx(theirs["mm_per_unit"] / 1000.0)
        assert mine["evidence"] == theirs["evidence"]
        expected = f"{snap.source}: {theirs['warning']}" if theirs["warning"] else None
        assert mine["warning"] == expected


def test_a_unit_nothing_measures_keeps_the_declared_one():
    unit = takeoff.unit_of(empty(5))
    assert (unit["declared"], unit["inferred"], unit["m_per_unit"], unit["warning"]) == (
        "cm",
        "cm",
        0.01,
        None,
    )


def test_a_unitless_drawing_nothing_measures_is_read_in_mm_and_says_so():
    unit = takeoff.unit_of(empty(0))
    assert (unit["declared"], unit["inferred"], unit["m_per_unit"]) == (None, "mm", 0.001)
    assert unit["warning"] == (
        "empty.dxf: INSUNITS declares Unitless (0) and no measurement settles the unit; "
        "the takeoff reads it as mm."
    )


def test_plan_copies_are_the_clusters_drawing_understand_reports(plant):
    truth, _pid, layout = plant
    found = takeoff.layout_tags(layout, truth["tag_positions"]["layout"])
    boxes = [tuple(cluster["box"]) for cluster in prepare(layout)["clusters"]]
    assert found["clusters"] == len(boxes) == truth["cluster_count"]
    assert tuple(found["cluster"]) in boxes


def test_the_takeoff_scale_is_drawing_scale_check(plant):
    _truth, pid, layout = plant
    result = takeoff.pipe_rows(build_network(pid), pid, layout)
    check = scale_check(pid, layout)
    for key in ("verdict", "factor", "median", "within_10pct", "pair_count", "groups"):
        assert result["scale"][key] == check[key], key


def test_rooms_are_the_rooms_drawing_understand_reports(plant):
    truth, _pid, layout = plant
    cluster = takeoff.layout_tags(layout, truth["tag_positions"]["layout"])["cluster"]
    regions = takeoff.room_regions(layout, cluster)
    assert sorted(key for key, _face in regions["faces"]) == sorted(
        str(room["number"]) for room in truth["rooms"]
    )
    reported = {
        (str(row["number"] or row["name"]), round(row["face"]["area"], 3))
        for row in describe(layout)["rooms"]
        if row["face"] is not None
    }
    for key, face in regions["faces"]:
        assert (key, round(face.area, 3)) in reported


def test_no_second_reader_is_left():
    for name in (
        "scale_stand_in",
        "INSUNITS_NAMES",
        "TEXT_BAND_M",
        "SPAN_BAND_M",
        "TYPICAL_SPAN_M",
        "TYPICAL_TEXT_M",
        "CLUSTER_GAP_SHARE",
        "MIN_PAIR_MM",
        "MIN_TAGS",
        "SCALE_BAND",
        "TO_SCALE_SHARE",
        "SCHEMATIC_SHARE",
        "_model_points",
        "_robust_box",
        "_text_heights",
        "_mark_segment",
        "_clusters",
    ):
        assert not hasattr(takeoff, name), name
