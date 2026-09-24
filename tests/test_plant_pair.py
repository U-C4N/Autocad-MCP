"""The synthetic plant pair opens, holds what it claims, and its truth is right.

Every expected number is worked out by hand in the comment beside it, from the
positions listed in tests/fixtures/plant_pair.py - never read back from the
generator's own arithmetic.
"""

from __future__ import annotations

import math

import ezdxf
import pytest

from tests.fixtures import plant_pair
from tests.fixtures.plant_pair import build_plant_pair


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    return build_plant_pair(tmp_path_factory.mktemp("plant"))


def _count(msp, kind, layer=None):
    return sum(1 for e in msp if e.dxftype() == kind and (layer is None or e.dxf.layer == layer))


def _texts(msp):
    out = []
    for e in msp:
        if e.dxftype() == "TEXT":
            out.append(e.dxf.text)
        elif e.dxftype() == "MTEXT":
            out.append(e.text)
    return out


def test_both_files_open_with_their_declared_units(pair):
    pid = ezdxf.readfile(pair["pid"])
    layout = ezdxf.readfile(pair["layout"])
    assert pid.header["$INSUNITS"] == 4
    assert layout.header["$INSUNITS"] == pair["layout_insunits"] == 1
    assert pair["layout_true_unit"] == "mm"


def test_the_pid_holds_the_symbols_the_runs_and_the_labels(pair):
    msp = ezdxf.readfile(pair["pid"]).modelspace()
    # 8 tanks (T101-T105, T201-T203), 2 pumps, 2 panels, 2 valves, 1 reducer
    assert _count(msp, "INSERT") == 15
    assert _count(msp, "INSERT", "KLEPPEN") == 3
    assert _count(msp, "TEXT", "TAGS") == 12  # the 11 shared tags and T105
    assert _count(msp, "ARC", "P_product piping") == 1
    texts = _texts(msp)
    for spelling in (
        "{\\fArial|b0;Ø51}",
        "%%c38",
        "\\U+2205 38",
        "⌀38",
        "ø38",
        "SMS51",
        "DN20",
        "DN25",
        "20x27",
    ):
        assert spelling in texts, spelling
    assert texts.count("ПОДАЧА") == 2  # CS1, CS2
    assert texts.count("ВОЗВРАТ") == 2  # CR1, CR2
    assert _count(msp, "MTEXT", "E_power") == 4


def test_one_segment_is_drawn_twice_a_line_over_a_polyline(pair):
    msp = ezdxf.readfile(pair["pid"]).modelspace()
    start, end = (14150.0, 7000.0), (17250.0, 7000.0)
    lines = [
        e
        for e in msp.query("LINE")
        if (e.dxf.start.x, e.dxf.start.y) == start and (e.dxf.end.x, e.dxf.end.y) == end
    ]
    polylines = [
        e for e in msp.query("LWPOLYLINE") if [tuple(p) for p in e.get_points("xy")] == [start, end]
    ]
    assert len(lines) == 1 and len(polylines) == 1


def test_a_cip_return_is_drawn_on_the_supply_layer(pair):
    (cr2,) = [run for run in pair["pipe_runs"] if run["id"] == "CR2"]
    assert cr2["layer"] == "P_cipsupplyline" and cr2["service"] == "cip_return"


def test_the_layout_carries_two_copies_one_outlier_and_the_rooms(pair):
    doc = ezdxf.readfile(pair["layout"])
    msp = doc.modelspace()
    # per copy: 7 tank and 2 pump circles, 2 panel boxes, 11 tag texts,
    # 2 room labels, 4 callouts, 12 wall lines; plus one stray line
    assert _count(msp, "CIRCLE") == 18
    assert _count(msp, "LWPOLYLINE", "E-PANEL") == 4
    assert _count(msp, "TEXT", "TAGS") == 22
    assert _count(msp, "TEXT", "ROOMS") == 4
    assert _count(msp, "MULTILEADER") == 8
    assert _count(msp, "LINE", "WALLS") == 24
    tags = [e.dxf.text for e in msp.query("TEXT") if e.dxf.layer == "TAGS"]
    assert all(tags.count(tag) == 2 for tag in set(tags))
    assert "T105" not in tags
    outlier = doc.entitydb[pair["outlier_handle"]]
    assert outlier.dxftype() == "LINE" and outlier.dxf.start.x == -5.0e9
    assert doc.header["$EXTMIN"][0] == -5.0e9  # the declared extents include it
    assert pair["cluster_count"] == 2 and pair["copy_offset"] == [100000.0, 0.0]


def test_the_wiring_callouts_point_at_their_loads(pair):
    msp = ezdxf.readfile(pair["layout"]).modelspace()
    first_copy = {}
    for e in msp.query("MULTILEADER"):
        tip = e.context.leaders[0].lines[0].vertices[0]
        if tip.x < 50000.0:
            first_copy[e.context.mtext.default_content] = (tip.x, tip.y)
    assert first_copy == {
        "Wiring to CP1": (5500.0, 8200.0),  # M11
        "Wiring to CP 1": (8000.0, 3500.0),  # T102
        "Wiring to CP-2": (26300.0, 8400.0),  # M12
        "WIRING TO CP2": (28700.0, 3000.0),  # T202
    }


def test_the_layer_services_truth(pair):
    assert pair["layer_services"] == {
        "P_product piping": "product",
        "P_cipsupplyline": "cip_supply",
        "P_cipreturnline": "cip_return",
        "P_ijswater": "ice_water",
        "E_power": "electrical",
    }


def test_the_pid_is_schematic_by_its_own_placement(pair):
    scale = pair["scale"]
    assert pair["scale_verdict"] == "schematic"
    assert scale["overall"]["pairs"] == 55  # 11 shared tags, every pair over 2 m
    assert scale["overall"]["within_10pct"] <= 0.5
    assert scale["room_1"]["within_10pct"] == 1.0
    assert scale["room_1"]["median"] == pytest.approx(1.3, abs=1e-12)
    # T101 (3000, 3000) and T102 (8000, 3500) on the layout; x1.3 on the P&ID
    pid = pair["tag_positions"]["pid"]
    assert pid["T101"] == pytest.approx((3900.0, 3900.0))
    assert pid["T102"] == pytest.approx((10400.0, 4550.0))


def test_layout_lengths_are_manhattan_and_the_rectilinear_mst(pair):
    runs = {run["id"]: run for run in pair["pipe_runs"]}
    # PR1: T101 (3000, 3000) - T102 (8000, 3500): 5000 + 500
    assert runs["PR1"]["layout_length_mm"] == 5500.0
    # PR2: T102 (8000, 3500), T103 (13500, 3000), M11 (5500, 8200):
    # T102-T103 6000, T102-M11 7200, T103-M11 13200 -> MST 6000 + 7200
    assert runs["PR2"]["layout_length_mm"] == 13200.0
    # CS1: T201 (23500, 3200) - T101 (3000, 3000): 20500 + 200
    assert runs["CS1"]["layout_length_mm"] == 20700.0
    # PR4 ends on T105, which the layout does not have
    assert runs["PR4"]["layout_length_mm"] is None and runs["PR4"]["status"] == "C"
    assert [runs[i]["status"] for i in ("PR1", "PR2", "PR3", "CS2")] == ["A", "B", "A", "B"]


def test_schematic_lengths_count_the_double_drawn_segment_once(pair):
    runs = {run["id"]: run for run in pair["pipe_runs"]}
    # PR2: 1850 + 6700 + 3100 + 2200 + 3360 straight, and a quarter arc of r 300
    assert runs["PR2"]["pid_length_mm"] == pytest.approx(17210.0 + 150.0 * math.pi, abs=1e-9)
    # PR3 across the reducer: 1850 + 3100 at Ø51, 3750 + 1500 at Ø38
    assert runs["PR3"]["diameters"] == {"Ø51": 4950.0, "Ø38": 5250.0}


def test_cable_truth_is_manhattan_then_the_allowance_rounded_up(pair):
    rows = {row["tag"]: row for row in pair["cable_rows"]}
    # M11 (5500, 8200) to CP-1 (1200, 10800): 4300 + 2600 = 6900 mm;
    # 6.9 m x 1.2 = 8.28 -> 9
    assert (rows["M11"]["panel"], rows["M11"]["length_mm"], rows["M11"]["roundup_m"]) == (
        "CP-1",
        6900.0,
        9,
    )
    # M12 (26300, 8400) to CP-2 (35000, 1300): 8700 + 7100 = 15800; 18.96 -> 19
    assert (rows["M12"]["length_mm"], rows["M12"]["roundup_m"], rows["M12"]["vfd"]) == (
        15800.0,
        19,
        True,
    )
    assert rows["T202"]["kw"] is None, "the heater states no power; none is invented"
    assert [rows[t]["kw"] for t in ("M11", "T102", "M12")] == [5.5, 2.2, 7.5]


def test_the_rooms_truth(pair):
    assert [(r["number"], r["name"]) for r in pair["rooms"]] == [
        ("1", "PROCESS HALL"),
        ("2", "МОЙКА"),
    ]
    assert pair["rooms"][1]["polygon"] == [
        [20200.0, 0.0],
        [36000.0, 0.0],
        [36000.0, 12000.0],
        [20200.0, 12000.0],
    ]


def test_the_generator_refuses_a_pair_that_would_read_to_scale(tmp_path, monkeypatch):
    stretched = {
        tag: (1.3 * x, 1.3 * y)
        for tag, (x, y) in plant_pair.LAYOUT_TAGS.items()
        if tag not in plant_pair.ROOM1_TAGS
    }
    monkeypatch.setattr(plant_pair, "PID_ROOM2", stretched)
    with pytest.raises(AssertionError, match="schematic"):
        build_plant_pair(tmp_path)
    assert not (tmp_path / "pid.dxf").exists()


def test_every_label_reads_as_the_truth_says_through_the_task_1_parsers(pair):
    """The fixture's declared readings agree with engineering/understand's parsers."""
    from engineering.understand.labels import (
        parse_diameters,
        parse_electrical,
        parse_tag,
        wiring_target,
    )
    from engineering.understand.snapshot import read_snapshot
    from engineering.understand.vocab import classify_layer, room_label, supply_return

    for run in plant_pair.RUNS:
        for _kind, raw, _at, read in run.labels:
            assert parse_diameters(raw) == (read,), (run.id, raw)
        for word, _at in run.words:
            expected = "supply" if run.service.endswith("supply") else "return"
            assert supply_return(word) == expected, run.id
    for layer, service in pair["layer_services"].items():
        assert classify_layer(layer)["service"] == service, layer
    pid = read_snapshot(pair["pid"])
    tags = [parse_tag(r.text) for r in pid.records if r.type == "TEXT" and r.layer == "TAGS"]
    assert sorted(tags) == sorted([*pair["tag_positions"]["layout"], "T105"])
    electrical = [r.text for r in pid.records if r.layer == "E_power"]
    assert sorted(parse_electrical(t)["kw"] or 0.0 for t in electrical) == [0.0, 2.2, 5.5, 7.5]
    layout = read_snapshot(pair["layout"])
    rooms = [room_label(r.text) for r in layout.records if r.layer == "ROOMS"]
    assert {(r["number"], r["name"]) for r in rooms} == {
        (room["number"], room["name"]) for room in pair["rooms"]
    }
    panels = {wiring_target(r.text) for r in layout.records if r.type == "MULTILEADER"}
    assert panels == {"CP-1", "CP-2"}
