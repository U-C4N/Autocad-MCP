"""Cable takeoff rows: loads from the P&ID, panels and Manhattan lengths from the layout.

The hand-built pair. On the P&ID: M82 with 'P = 18,5 кВт' and '3 ф. 400 В +
N + PE' under it, M87 with only '3 ф. 400 В + N + PE' (no power stated), the
package panel CP-M88 with 'P = 15 кВт' and its child motor M88 with
'P = 4 кВт'. On the layout (mm): two rooms split at x = 20000, the panel CP-1
at (15000, 1000), M82 (2000, 3000), M87 (4000, 3000), CP-M88 (25000, 5000),
M88 (26000, 8000), each with a 'Wiring to ...' callout 300 mm below it.
Every expected length is worked in the comment beside it. The last two tests
read the synthetic plant pair of Task 2 and compare every cable row with the
generator's own Manhattan lengths.
"""

from __future__ import annotations

import csv

import pytest

from engineering.understand.labels import normalize_panel
from engineering.understand.report import write_workbook
from engineering.understand.snapshot import EntityRecord, Snapshot, read_snapshot
from engineering.understand.takeoff import cable_rows, roundup_m
from engineering.understand.vocab import classify_layer, room_label
from tests.fixtures.plant_pair import build_plant_pair

EPS = 1e-9
WALLS = "WALLS"


def rec(handle, kind, layer, points=(), **extra):
    return EntityRecord(
        handle=handle, type=kind, layer=layer, space="Model", points=tuple(points), **extra
    )


def text(handle, value, at, height, layer="TEXT"):
    x, y = at
    return rec(
        handle,
        "TEXT",
        layer,
        (at,),
        text=value,
        height=height,
        bbox=(x, y, x + height * len(value) * 0.6, y + height),
    )


def snap(records, insunits, source):
    layers = {r.layer: {"color": 7, "linetype": "Continuous"} for r in records}
    return Snapshot(
        source=source,
        insunits=insunits,
        extmin=None,
        extmax=None,
        layers=layers,
        layouts=(),
        records=tuple(records),
    )


def pid_records():
    return [
        text("a", "M82", (0.0, 0.0), 50.0),
        text("a1", "P = 18,5 кВт", (0.0, -80.0), 50.0),
        text("a2", "3 ф. 400 В + N + PE", (0.0, -160.0), 50.0),
        text("b", "M87", (1000.0, 0.0), 50.0),
        text("b1", "3 ф. 400 В + N + PE", (1000.0, -80.0), 50.0),
        text("c", "CP-M88", (2000.0, 0.0), 50.0),
        text("c1", "P = 15 кВт", (2000.0, -80.0), 50.0),
        text("d", "M88", (3000.0, 0.0), 50.0),
        text("d1", "P = 4 кВт", (3000.0, -80.0), 50.0),
    ]


def layout_records(skip=()):
    places = {
        "M82": (2000.0, 3000.0),
        "M87": (4000.0, 3000.0),
        "CP-M88": (25000.0, 5000.0),
        "M88": (26000.0, 8000.0),
    }
    panels = {"M82": "CP1", "M87": "CP 1", "CP-M88": "CP-1", "M88": "CP-M88"}
    records = [
        rec(
            "W",
            "LWPOLYLINE",
            WALLS,
            ((0, 0), (40000, 0), (40000, 20000), (0, 20000)),
            closed=True,
            bbox=(0.0, 0.0, 40000.0, 20000.0),
        ),
        rec("X", "LINE", WALLS, ((20000, 0), (20000, 20000))),
        text("R1", "ROOM 101", (5000.0, 10000.0), 250.0),
        text("R2", "ROOM 102", (30000.0, 10000.0), 250.0),
        text("P1", "CP-1", (15000.0, 1000.0), 250.0),
    ]
    for tag, (x, y) in places.items():
        if tag in skip:
            continue
        records.append(text("t" + tag, tag, (x, y), 250.0))
        records.append(text("w" + tag, f"Wiring to {panels[tag]}", (x, y - 300.0), 250.0))
    return records


def room_key(label):
    found = room_label(label)
    assert found is not None, f"the vocabulary must read {label!r} as a room label"
    return str(found.get("number") or found.get("name"))


@pytest.fixture
def pair():
    assert classify_layer(WALLS)["discipline"] == "architecture", "WALLS must be a wall layer"
    return snap(pid_records(), 4, "pid.dxf"), snap(layout_records(), 4, "layout.dxf")


def by_tag(result):
    return {row["tag"]: row for row in result["rows"]}


def test_every_load_gets_its_panel_length_and_rounded_cable(pair):
    pid, layout = pair
    rows = by_tag(cable_rows(pid, layout))
    assert set(rows) == {"M82", "M87", "CP-M88", "M88"}
    expected = {
        # tag: (panel, Manhattan mm, ROUNDUP(m x 1.2))
        "M82": ("CP-1", 15000.0, 18),  # |15000-2000| + |1000-3000| = 13000 + 2000
        "M87": ("CP-1", 13000.0, 16),  # 11000 + 2000 -> 15.6 -> 16
        "CP-M88": ("CP-1", 14000.0, 17),  # 10000 + 4000 -> 16.8 -> 17
        "M88": ("CP-M88", 4000.0, 5),  # 1000 + 3000 -> 4.8 -> 5
    }
    for tag, (panel, mm, cable) in expected.items():
        row = rows[tag]
        assert row["panel"] == panel  # 'CP1', 'CP 1' and 'CP-1' are one panel
        assert row["panel_source"] == "layout"
        assert row["length_m"] * 1000.0 == pytest.approx(mm, abs=1e-6)
        assert row["cable_m"] == cable


def test_power_and_supply_are_read_from_the_pid_texts(pair):
    pid, layout = pair
    m82 = by_tag(cable_rows(pid, layout))["M82"]
    assert m82["kw"] == pytest.approx(18.5)
    assert (m82["phases"], m82["voltage"], m82["neutral"]) == (3, 400, True)


def test_power_is_never_invented(pair):
    pid, layout = pair
    result = cable_rows(pid, layout)
    m87 = by_tag(result)["M87"]
    assert m87["kw"] is None  # the P&ID states phases and voltage, no kW
    assert m87["cable_m"] == 16  # the cable is still measured
    assert {
        "tag": "M87",
        "item": "missing_power",
        "detail": "no power is stated near the tag on the P&ID",
    } in result["open_items"]
    assert all(item["tag"] == "M87" for item in result["open_items"])


def test_room_totals_count_a_package_and_not_its_children(pair):
    pid, layout = pair
    result = cable_rows(pid, layout)
    rows = by_tag(result)
    assert rows["M88"]["package"] == "CP-M88"
    assert rows["M82"]["room"] == room_key("ROOM 101")
    assert rows["CP-M88"]["room"] == room_key("ROOM 102")
    rooms = {item["key"]: item for item in result["summary"]["by_room"]}
    assert rooms[room_key("ROOM 101")]["kw"] == pytest.approx(18.5)  # M82; M87 has none
    assert rooms[room_key("ROOM 102")]["kw"] == pytest.approx(15.0)  # CP-M88, not + M88's 4
    assert rooms[room_key("ROOM 102")]["loads"] == 2
    assert result["totals"]["known_kw"] == pytest.approx(33.5)
    assert result["totals"]["cable_m"] == 18 + 16 + 17 + 5


def test_roundup_does_not_buy_a_metre_of_binary_noise():
    assert 50.0 * 1.1 > 55.0  # 55.00000000000001 in floating point
    assert roundup_m(50.0, 0.10) == 55
    assert roundup_m(15.0, 0.20) == 18
    assert roundup_m(15.001, 0.20) == 19  # 18.0012 -> 19


def test_without_a_layout_lengths_stay_empty_with_an_open_item(pair):
    pid, _layout = pair
    result = cable_rows(pid, None)
    assert all(row["length_m"] is None and row["cable_m"] is None for row in result["rows"])
    no_layout = [item["tag"] for item in result["open_items"] if item["item"] == "no_layout"]
    assert sorted(no_layout) == ["CP-M88", "M82", "M87", "M88"]
    # no callout on the P&ID either: the panel is open, never guessed
    assert all(row["panel"] is None for row in result["rows"])


def test_a_tag_missing_from_the_layout_is_an_open_item(pair):
    pid, _layout = pair
    layout = snap(layout_records(skip=("M87",)), 4, "layout.dxf")
    result = cable_rows(pid, layout)
    items = {(item["tag"], item["item"]) for item in result["open_items"]}
    assert ("M87", "tag_not_on_layout") in items
    assert ("M87", "missing_panel") in items  # its callout went with it
    assert by_tag(result)["M87"]["cable_m"] is None


def test_sections_come_only_from_the_callers_rules(pair):
    pid, layout = pair
    assert all(row["section"] is None for row in cable_rows(pid, layout)["rows"])
    rules = [{"max_kw": 40, "section": "5x10"}, {"max_kw": 5, "section": "5x2.5"}]
    rows = by_tag(cable_rows(pid, layout, section_rules=rules))
    assert rows["M82"]["section"] == "5x10"  # 18.5 kW
    assert rows["M88"]["section"] == "5x2.5"  # 4 kW: the smaller rule is tried first
    assert rows["M87"]["section"] is None  # no power, no section


def test_a_load_beyond_every_rule_is_an_open_item(pair):
    pid, layout = pair
    result = cable_rows(pid, layout, section_rules=[{"max_kw": 10, "section": "5x4"}])
    assert {"tag": "M82", "item": "no_section_rule", "detail": "no rule covers 18.5 kW"} in (
        result["open_items"]
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"allowance": 2.0}, "allowance: must lie between 0 and 1"),
        ({"section_rules": {"max_kw": 5}}, "section_rules: expected a list"),
        (
            {"section_rules": [{"max_kw": 0, "section": "x"}]},
            r"section_rules\[0\]\.max_kw: must be",
        ),
        ({"section_rules": [{"max_kw": 5}]}, r"section_rules\[0\]\.section: expected a non-empty"),
        (
            {"section_rules": [{"max_kw": 5, "section": "x", "phases": 2}]},
            r"section_rules\[0\]\.phases: 1 or 3",
        ),
        (
            {"section_rules": [{"max_kw": 5, "section": "x", "iec": True}]},
            r"section_rules\[0\]: unknown key\(s\) iec",
        ),
    ],
)
def test_refusals_name_the_option(pair, kwargs, message):
    pid, layout = pair
    with pytest.raises(ValueError, match="^" + message):
        cable_rows(pid, layout, **kwargs)


CABLE_HEADERS = {
    "tr": [
        "Etiket",
        "Mahal",
        "Güç (kW)",
        "Faz",
        "Gerilim (V)",
        "Nötr",
        "Sürücü (VFD)",
        "Pano",
        "Mesafe (m)",
        "Pay oranı",
        "Kablo (m)",
        "Kesit",
        "Paket",
    ],
    "en": [
        "Tag",
        "Room",
        "Power (kW)",
        "Phases",
        "Voltage (V)",
        "Neutral",
        "VFD",
        "Panel",
        "Distance (m)",
        "Allowance",
        "Cable (m)",
        "Section",
        "Package",
    ],
    "ru": [
        "Тег",
        "Помещение",
        "Мощность (кВт)",
        "Фазы",
        "Напряжение (В)",
        "Нейтраль",
        "ЧРП",
        "Шкаф",
        "Расстояние (м)",
        "Запас",
        "Кабель (м)",
        "Сечение",
        "Комплект",
    ],
}


@pytest.mark.parametrize("lang", ["tr", "en", "ru"])
def test_the_cable_workbook_speaks_three_languages(pair, tmp_path, lang):
    pid, layout = pair
    files = write_workbook(
        cable_rows(pid, layout), kind="cable", lang=lang, path=str(tmp_path / "cables.xlsx")
    )
    with open(files["csv"][0], encoding="utf-8-sig", newline="") as handle:
        metraj = list(csv.reader(handle))
    assert metraj[0] == CABLE_HEADERS[lang]
    assert metraj[-1][10] == str(18 + 16 + 17 + 5)
    with open(files["csv"][2], encoding="utf-8-sig", newline="") as handle:
        kontrol = list(csv.reader(handle))
    assert [row[0] for row in kontrol[1:]] == ["M87"]  # the one open item: no power


def test_the_method_sheet_states_the_package_rule_and_that_power_is_never_invented(pair, tmp_path):
    pid, layout = pair
    files = write_workbook(
        cable_rows(pid, layout), kind="cable", lang="en", path=str(tmp_path / "cables.xlsx")
    )
    with open(files["csv"][3], encoding="utf-8-sig", newline="") as handle:
        method = {row[0]: row[1] for row in list(csv.reader(handle))[1:]}
    assert "never invented" in method["Rule: power"]
    assert "counted twice" in method["Rule: panel packages"]
    assert method["Section rules (the caller's)"] == "—"


# -- the synthetic plant pair (Task 2) -------------------------------------------


@pytest.fixture(scope="module")
def plant(tmp_path_factory):
    truth = build_plant_pair(tmp_path_factory.mktemp("plant"))
    return truth, read_snapshot(truth["pid"]), read_snapshot(truth["layout"])


def inside(point, polygon):
    """Even-odd ray cast over the generator's own room polygon."""
    x, y = point
    hit = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=False):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def test_every_synthetic_cable_row_equals_the_generators_truth(plant):
    truth, pid, layout = plant
    result = cable_rows(pid, layout)
    rows = by_tag(result)
    assert set(rows) == {expected["tag"] for expected in truth["cable_rows"]}
    for expected in truth["cable_rows"]:
        row = rows[expected["tag"]]
        assert row["panel"] == expected["panel"], expected["tag"]
        if expected["kw"] is None:
            assert row["kw"] is None, expected["tag"]
            assert {
                "tag": expected["tag"],
                "item": "missing_power",
                "detail": "no power is stated near the tag on the P&ID",
            } in result["open_items"]
        else:
            assert row["kw"] == pytest.approx(expected["kw"]), expected["tag"]
        assert row["length_m"] * 1000.0 == pytest.approx(expected["length_mm"], abs=1e-6)
        assert row["cable_m"] == expected["roundup_m"], expected["tag"]


def test_the_synthetic_room_totals_count_each_power_once(plant):
    truth, pid, layout = plant
    result = cable_rows(pid, layout)
    rooms = [
        (str(room["number"] or room["name"]), [tuple(p) for p in room["polygon"]])
        for room in truth["rooms"]
    ]
    for row in result["rows"]:
        homes = [key for key, polygon in rooms if row["at"] and inside(row["at"], polygon)]
        if homes:
            assert row["room"] == homes[0], row["tag"]
    stated = {
        normalize_panel(r["tag"]) or r["tag"] for r in truth["cable_rows"] if r["kw"] is not None
    }

    def child(r):
        return r["panel"] in stated and (normalize_panel(r["tag"]) or r["tag"]) != r["panel"]

    expected_kw = sum(r["kw"] for r in truth["cable_rows"] if r["kw"] is not None and not child(r))
    assert result["totals"]["known_kw"] == pytest.approx(expected_kw)
    by_room = sum(item["kw"] for item in result["summary"]["by_room"])
    assert by_room == pytest.approx(expected_kw)
