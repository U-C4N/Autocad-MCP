"""drawing_understand's report: the synthetic plant pair, then hand-built snapshots.

The plant-pair facts come from the generator's truth dict (Task 2), never from
a real drawing; the hand-built cases compute their expectations in comments.
"""

from __future__ import annotations

import copy

import pytest

from engineering.understand.describe import describe, tag_occurrences, text_language
from engineering.understand.labels import parse_tag
from engineering.understand.snapshot import EntityRecord, Snapshot, read_snapshot
from engineering.understand.vocab import classify_layer, room_label
from tests.fixtures.plant_pair import build_plant_pair

EPS = 1e-9


def shoelace(polygon):
    total = 0.0
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


@pytest.fixture(scope="module")
def plant(tmp_path_factory):
    truth = build_plant_pair(tmp_path_factory.mktemp("plant"))
    return {
        "truth": truth,
        "pid": describe(read_snapshot(truth["pid"])),
        "layout": describe(read_snapshot(truth["layout"])),
    }


# ── the synthetic plant pair ────────────────────────────────────────────────


def test_the_layout_declares_inches_over_millimetre_geometry(plant):
    units = plant["layout"]["units"]
    assert units["declared"]["code"] == plant["truth"]["layout_insunits"]
    assert units["inferred"] == plant["truth"]["layout_true_unit"]
    assert units["agree"] is False
    assert any(w.startswith("INSUNITS declares Inches (1)") for w in plant["layout"]["warnings"])


def test_the_far_outlier_is_named_by_handle(plant):
    outliers = plant["layout"]["extents"]["outliers"]
    assert outliers[0]["handle"] == plant["truth"]["outlier_handle"]
    assert any(plant["truth"]["outlier_handle"] in w for w in plant["layout"]["warnings"])


def test_the_two_plan_copies_are_two_clusters(plant):
    assert len(plant["layout"]["clusters"]) == plant["truth"]["cluster_count"]


def test_service_layers_are_classified(plant):
    rows = {r["name"]: r for r in plant["layout"]["layers"]}
    rows.update({r["name"]: r for r in plant["pid"]["layers"]})
    for layer, service in plant["truth"]["layer_services"].items():
        assert rows[layer]["service"] == service, layer
        assert rows[layer]["confidence"] > 0.0, layer


def test_every_equipment_tag_of_the_truth_is_found_on_the_pid(plant):
    found = {t["tag"] for t in plant["pid"]["equipment_tags"]}
    expected = {tag for run in plant["truth"]["pipe_runs"] for tag in run["tags"]}
    expected |= {row["tag"] for row in plant["truth"]["cable_rows"]}
    assert expected <= found


def test_layout_rooms_carry_the_truth_numbers_names_and_measured_faces(plant):
    for room in plant["truth"]["rooms"]:
        rows = [
            r
            for r in plant["layout"]["rooms"]
            if r["number"] == room["number"] and r["name"] == room["name"]
        ]
        assert rows, room["number"]
        expected = shoelace([tuple(p) for p in room["polygon"]])
        for row in rows:
            assert row["face"] is not None, room["number"]
            assert abs(row["face"]["area"] - expected) <= 1e-9 * expected, room["number"]
            assert row["confidence"] == 0.9


def test_the_pid_texts_are_read_as_cyrillic(plant):
    assert plant["pid"]["languages"]["by_class"]["cyrillic"] > 0


# ── hand-built snapshots ────────────────────────────────────────────────────


def line(handle, a, b, layer="WALL", space="Model"):
    return EntityRecord(handle=handle, type="LINE", layer=layer, space=space, points=(a, b))


def text(handle, value, x, y, layer="TEXT", height=250.0):
    return EntityRecord(
        handle=handle,
        type="TEXT",
        layer=layer,
        space="Model",
        points=((x, y),),
        text=value,
        height=height,
    )


def insert(handle, block, x, y, attribs=(), space="Model"):
    return EntityRecord(
        handle=handle,
        type="INSERT",
        layer="0",
        space=space,
        points=((x, y),),
        block=block,
        attribs=tuple(attribs),
    )


def rect(prefix, x0, y0, x1, y1, layer="WALL"):
    return [
        line(f"{prefix}1", (x0, y0), (x1, y0), layer),
        line(f"{prefix}2", (x1, y0), (x1, y1), layer),
        line(f"{prefix}3", (x1, y1), (x0, y1), layer),
        line(f"{prefix}4", (x0, y1), (x0, y0), layer),
    ]


def snapshot(records, insunits=4, layers=None, layouts=("Model",)):
    return Snapshot(
        source="test",
        insunits=insunits,
        extmin=None,
        extmax=None,
        layers=layers or {},
        layouts=layouts,
        records=tuple(records),
    )


def test_a_room_label_inside_the_wall_faces_is_measured():
    assert classify_layer("WALL")["discipline"] == "architecture"
    parsed = room_label("ROOM 101 PROCESS")
    assert parsed is not None
    # Inner face (200,200)-(6200,4200): 6000 x 4000 = 24 000 000 unit^2.
    records = [
        *rect("O", 0.0, 0.0, 6400.0, 4400.0),
        *rect("I", 200.0, 200.0, 6200.0, 4200.0),
        text("R", "ROOM 101 PROCESS", 1000.0, 2000.0),
    ]
    report = describe(snapshot(records))
    (room,) = report["rooms"]
    assert room["number"] == parsed["number"]
    assert room["name"] == parsed["name"]
    assert room["face"]["area"] == pytest.approx(24_000_000.0, abs=EPS)
    assert room["face"]["polygon"] == [
        [200.0, 200.0],
        [6200.0, 200.0],
        [6200.0, 4200.0],
        [200.0, 4200.0],
    ]
    assert room["source"] == "label+face"
    assert room["confidence"] == 0.9
    # The wall ring between the outlines is the one unlabelled face.
    assert report["room_faces"]["faces"] == 2
    assert report["room_faces"]["unlabelled"] == 1


def frame(handle, x0, y0, x1, y1, layer="0"):
    return EntityRecord(
        handle=handle,
        type="LWPOLYLINE",
        layer=layer,
        space="Model",
        points=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        closed=True,
        bbox=(x0, y0, x1, y1),
    )


def test_a_room_drawn_as_a_frame_is_measured_by_its_frame():
    # A P&ID draws its rooms as closed frames on no wall layer. Each numbered
    # label takes the smallest frame around it that holds no other room label;
    # the sheet border (both rooms) and a box drawn tight around a label (under
    # ten text heights a side) are not rooms.
    records = [
        frame("B", 0.0, 0.0, 20000.0, 10000.0),  # the border: both labels inside
        frame("F1", 1000.0, 1000.0, 9000.0, 9000.0),  # room 1: 8000 x 8000
        frame("F2", 11000.0, 1000.0, 19000.0, 7000.0),  # room 2: 8000 x 6000
        frame("K", 1900.0, 7900.0, 4100.0, 8400.0),  # a box around room 1's label
        text("R1", "ROOM 1", 2000.0, 8000.0),
        text("R2", "ROOM 2", 12000.0, 6000.0),
        # an unnumbered mention inside room 2 ('hot room') does not deny its frame
        text("H", "HOT ROOM", 15000.0, 3000.0),
    ]
    report = describe(snapshot(records))
    rooms = {room["number"]: room for room in report["rooms"]}
    assert rooms["1"]["source"] == rooms["2"]["source"] == "label+frame"
    assert rooms["1"]["face"]["area"] == pytest.approx(64_000_000.0, abs=EPS)
    assert rooms["2"]["face"]["area"] == pytest.approx(48_000_000.0, abs=EPS)
    # a frame is weaker evidence than a face of wall layers
    assert rooms["1"]["confidence"] == rooms["2"]["confidence"] == 0.8


def test_a_cabinet_label_naming_its_room_is_not_a_room():
    # 'CONTROL CABINET / FILLING ROOM' labels the cabinet, not a room: it names
    # equipment and carries no room number. A numbered room named after its
    # equipment ('PUMP ROOM 3') is still a room.
    records = [
        text("C", "CONTROL CABINET FILLING ROOM", 1000.0, 1000.0),
        text("P", "PUMP ROOM 3", 5000.0, 1000.0),
    ]
    report = describe(snapshot(records))
    assert [(room["number"], room["name"]) for room in report["rooms"]] == [("3", "PUMP")]


def test_an_attribute_and_a_text_of_one_tag_are_one_occurrence_and_a_copy_is_two():
    assert parse_tag("T4100") == "T4100"
    records = [
        line("L", (0.0, 0.0), (20000.0, 0.0), "0"),
        insert("B1", "TANK", 1000.0, 0.0, attribs=[("TAG", "T4100")]),
        # 150 units away: inside 1 % of the 20 000 diagonal (200), so the same one.
        text("X1", "T4100", 1150.0, 0.0),
        # 18 000 away: a second occurrence.
        text("X2", "T4100", 19000.0, 0.0),
    ]
    snap = snapshot(records)
    occurrences = tag_occurrences(snap)
    assert [(o["handle"], o["source"]) for o in occurrences] == [
        ("B1", "attribute"),
        ("X2", "text"),
    ]
    report = describe(snap)
    (row,) = report["equipment_tags"]
    assert row["tag"] == "T4100"
    assert row["count"] == 2
    assert row["ambiguous"] is True
    assert row["confidence"] == 0.95
    assert any("appear more than once" in w for w in report["warnings"])


def test_text_languages_are_counted_by_character_class():
    assert text_language("ПОДАЧА CIP") == "cyrillic"
    assert text_language("DÖNÜŞ") == "turkish"
    assert text_language("DÖNÜS") == "latin"  # ö and ü alone are German too
    assert text_language("SUPPLY") == "latin"
    assert text_language("3,5") == "none"
    records = [
        text("A", "ПОДАЧА", 0.0, 0.0),
        text("B", "BESLEME DÖNÜŞ", 0.0, 500.0),
        text("C", "SUPPLY", 0.0, 1000.0),
        text("D", "12", 0.0, 1500.0),
    ]
    languages = describe(snapshot(records))["languages"]
    assert languages["by_class"] == {"latin": 1, "turkish": 1, "cyrillic": 1, "none": 1}
    assert languages["share"]["cyrillic"] == 0.25
    assert languages["samples"]["cyrillic"] == ["A"]


def test_blocks_are_split_named_and_anonymous_with_kinds_by_name():
    records = [
        insert("A1", "*U12", 0.0, 0.0),
        insert("A2", "*U12", 100.0, 0.0),
        insert("G1", "GATE_VALVE", 200.0, 0.0),
        insert("G2", "GATE_VALVE", 300.0, 0.0),
        insert("G3", "GATE_VALVE", 400.0, 0.0),
        insert("P1", "PUMP_A", 500.0, 0.0),
    ]
    blocks = describe(snapshot(records))["blocks"]
    assert blocks["inserts"] == 6
    assert [(r["name"], r["inserts"], r["kind"]) for r in blocks["named"]] == [
        ("GATE_VALVE", 3, "valve"),
        ("PUMP_A", 1, "equipment"),
    ]
    assert blocks["anonymous"] == {"names": 1, "inserts": 2}
    assert blocks["kinds"] == {"equipment": 1, "valve": 1}


def test_the_title_block_is_the_named_paper_space_block_with_its_attributes():
    title = insert(
        "TB",
        "A1_TITLE",
        0.0,
        0.0,
        attribs=[("DWG_NO", "001"), ("REV", "B"), ("SCALE", "1:100"), ("DATE", "2026-09-24")],
        space="Layout1",
    )
    other = insert(
        "M",
        "NOTE_BOX",
        0.0,
        0.0,
        attribs=[("A", "1"), ("B", "2"), ("C", "3"), ("D", "4"), ("E", "5")],
    )
    report = describe(snapshot([title, other], layouts=("Model", "Layout1")))
    block = report["title_block"]
    assert block["found"] is True
    assert block["handle"] == "TB"
    assert block["attributes"] == {
        "DWG_NO": "001",
        "REV": "B",
        "SCALE": "1:100",
        "DATE": "2026-09-24",
    }
    assert block["confidence"] == 0.9
    assert report["layouts"] == [
        {"name": "Model", "entities": 1},
        {"name": "Layout1", "entities": 1},
    ]


def test_no_title_block_is_said_not_invented():
    block = describe(snapshot([insert("M", "X", 0.0, 0.0, attribs=[("A", "1")])]))["title_block"]
    assert block == {"found": False, "reason": "no block reference with three or more attributes"}


def test_a_layer_without_a_keyword_is_classified_by_its_texts():
    expected = classify_layer("CIP SUPPLY")
    assert expected["service"] != "unknown"
    assert classify_layer("L-17")["service"] == "unknown"
    assert classify_layer("L-17")["discipline"] == "unknown"
    records = [
        line("P1", (0.0, 0.0), (3000.0, 0.0), "L-17"),
        line("P2", (3000.0, 0.0), (3000.0, 4000.0), "L-17"),
        text("T1", "CIP SUPPLY", 1000.0, 100.0, layer="L-17"),
        text("T2", "CIP SUPPLY", 3100.0, 2000.0, layer="L-17"),
    ]
    layers = {r["name"]: r for r in describe(snapshot(records))["layers"]}
    row = layers["L-17"]
    assert row["service"] == expected["service"]
    assert row["discipline"] == expected["discipline"]
    assert row["confidence"] == 0.6
    assert row["entities"] == 4
    assert row["length"] == pytest.approx(7000.0, abs=EPS)  # 3000 + 4000
    assert row["length_m"] == pytest.approx(7.0, abs=EPS)
    assert row["evidence"].startswith("2 of 2 texts on the layer read as")


def test_describe_leaves_the_snapshot_as_it_was():
    records = [*rect("O", 0.0, 0.0, 6400.0, 4400.0), text("R", "T4100", 100.0, 100.0)]
    snap = snapshot(records)
    before = copy.deepcopy(snap)
    describe(snap)
    assert snap == before
