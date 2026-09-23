"""Door, window and room schedules: rows from the plan model, a TABLE from the drawing.

Records are written with `model.to_values` directly - in production the wall
engine (Task 4, another branch) and `arch_room` write them; the record shape is
pinned by the skeleton.
"""

from __future__ import annotations

import re

import pytest

from engineering.arch.lang import vocab
from engineering.arch.layers import ARCH_ROLE_LAYER
from engineering.arch.model import APP_ID, Opening, Room, Wall, to_payload, to_values
from engineering.arch.schedule import SCHEDULE_COLUMNS, draw_schedule, schedule_rows

WALLS = [
    Wall(id="w1", axis=((0.0, 0.0), (6000.0, 0.0)), thickness=200.0),
    Wall(id="w2", axis=((6000.0, 0.0), (6000.0, 4000.0)), thickness=200.0),
]


def plan():
    return {
        "walls": list(WALLS),
        "openings": [
            Opening(
                id="da",
                wall="w2",
                kind="door",
                offset=500.0,
                width=800.0,
                swing="out",
                hand="right",
            ),
            Opening(id="db", wall="w1", kind="door", offset=3000.0, width=912.5, height=2100.0),
            Opening(
                id="dc", wall="w1", kind="door", offset=1000.0, width=900.0, height=2100.0, tag="D2"
            ),
            Opening(
                id="wa",
                wall="w1",
                kind="window",
                offset=4000.0,
                width=1200.0,
                height=1200.0,
                sill=900.0,
            ),
        ],
        "rooms": [
            Room(id="R1", name="KITCHEN", number="2", at=(0.0, 0.0), area=15_000_000.0),
            Room(id="R2", name="LIVING", number="10", at=(0.0, 0.0), area=20_000_000.0),
            Room(id="R3", name="STORE", number=None, at=(0.0, 0.0), area=10_000_000.0),
        ],
    }


def test_door_rows_generate_missing_tags_in_wall_order_and_sort_by_tag():
    rows = schedule_rows("doors", plan(), lang="en")
    en = vocab("en")
    # wall order: w1 at 1000 (tagged D2), w1 at 3000 -> D1, w2 at 500 -> D3 (D2 is taken)
    assert [(r["id"], r["tag"], r["generated"]) for r in rows] == [
        ("db", "D1", True),
        ("dc", "D2", False),
        ("da", "D3", True),
    ]
    assert rows[0] == {
        "id": "db",
        "tag": "D1",
        "generated": True,
        "width": "912.5",
        "height": "2100",
        "swing": en["in"],
        "hand": en["left"],
        "wall": "w1",
    }
    # no height on the model: an empty cell, never a default
    assert rows[2]["height"] == ""
    assert (rows[2]["swing"], rows[2]["hand"]) == (en["out"], en["right"])
    assert set(SCHEDULE_COLUMNS["doors"]) <= set(rows[0])


def test_turkish_door_rows_use_k_tags_decimal_commas_and_turkish_words():
    rows = schedule_rows("doors", plan(), lang="tr")
    tr = vocab("tr")
    # explicit "D2" is never renamed; generated tags take the Turkish prefix
    assert [r["tag"] for r in rows] == ["D2", "K1", "K2"]
    k1 = next(r for r in rows if r["tag"] == "K1")
    assert k1["width"] == "912,5"
    assert (k1["swing"], k1["hand"]) == (tr["in"], tr["left"])


def test_window_rows_carry_sill_and_no_swing():
    (row,) = schedule_rows("windows", plan(), lang="en")
    assert row == {
        "id": "wa",
        "tag": "W1",
        "generated": True,
        "width": "1200",
        "height": "1200",
        "sill": "900",
        "wall": "w1",
    }
    assert [r["tag"] for r in schedule_rows("windows", plan(), lang="tr")] == ["P1"]


def test_room_rows_sort_by_number_with_the_unnumbered_last_and_end_in_a_total():
    rows = schedule_rows("rooms", plan(), lang="en")
    assert [(r["number"], r["name"], r["area"]) for r in rows] == [
        ("2", "KITCHEN", "15.00 m²"),
        ("10", "LIVING", "20.00 m²"),
        ("", "STORE", "10.00 m²"),
        ("", vocab("en")["total"], "45.00 m²"),  # 15 + 20 + 10
    ]
    assert rows[-1]["total"] is True
    assert rows[-1]["area_mm2"] == 45_000_000.0


def test_turkish_room_rows_write_the_total_with_a_decimal_comma():
    rows = schedule_rows("rooms", plan(), lang="tr")
    assert (rows[-1]["name"], rows[-1]["area"]) == (vocab("tr")["total"], "45,00 m²")
    assert rows[0]["area"] == "15,00 m²"


def test_an_empty_plan_has_no_rows_and_no_total():
    assert schedule_rows("rooms", {"walls": [], "openings": [], "rooms": []}) == ()
    assert schedule_rows("doors", {"walls": [], "openings": [], "rooms": []}) == ()


@pytest.mark.parametrize(
    ("kind", "lang", "message"),
    [
        ("stairs", "en", "kind: 'stairs' is not a schedule; schedules are doors, windows, rooms"),
        ("doors", "de", "lang: 'de' is not a schedule language; languages are en, tr"),
    ],
)
def test_an_unknown_kind_or_language_is_refused_with_the_list(kind, lang, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        schedule_rows(kind, plan(), lang=lang)


# ── async: the TABLE ────────────────────────────────────────────────────────


async def write_record(backend, obj, role):
    carrier = await backend.entity_create_line(0.0, 0.0, 1.0, 0.0, layer=ARCH_ROLE_LAYER[role])
    await backend.entity_set_xdata(carrier.handle, APP_ID, to_values(to_payload(obj)))


async def write_plan(backend):
    data = plan()
    for wall in data["walls"]:
        await write_record(backend, wall, "wall")
    for opening in data["openings"]:
        await write_record(backend, opening, opening.kind)
    for room in data["rooms"]:
        await write_record(backend, room, "room")


@pytest.mark.asyncio
async def test_a_door_schedule_is_a_table_with_title_header_and_one_row_per_door(backend):
    await write_plan(backend)
    result = await draw_schedule(backend, "doors", at=(10_000.0, 5_000.0), lang="en", scale=50)
    assert result["row_count"] == 3
    assert result["table_rows"] == 5  # title + header + 3 doors
    assert result["representation"] == "composite"
    assert result["layer"] == ARCH_ROLE_LAYER["symbol"]
    assert result["title"] == vocab("en")["door_schedule"]
    # 1:50: widths 15+20+20+20+20+20 = 115 mm -> 5750; rows 5 x 7 mm x 50 = 1750
    assert result["bbox"] == {"min": [10_000.0, 3_250.0], "max": [15_750.0, 5_000.0]}
    cells = {
        e.properties["text"]
        for e in await backend.entity_list(
            type_filter="MTEXT", layer_filter=ARCH_ROLE_LAYER["symbol"], limit=1000
        )
    }
    assert {"D1", "D2", "D3", "912.5", "w2"} <= cells


@pytest.mark.asyncio
async def test_a_room_schedule_carries_the_total_row(backend):
    await write_plan(backend)
    result = await draw_schedule(backend, "rooms", at=(0.0, 0.0), lang="tr")
    assert result["row_count"] == 4  # three rooms + the total
    assert result["table_rows"] == 6
    assert result["rows"][-1]["area"] == "45,00 m²"


@pytest.mark.asyncio
async def test_nothing_to_schedule_is_refused_before_anything_is_drawn(backend):
    before = await backend.entity_count()
    with pytest.raises(ValueError, match=re.escape("kind: this drawing carries no window records")):
        await draw_schedule(backend, "windows", at=(0.0, 0.0))
    with pytest.raises(
        ValueError, match=re.escape("scale: must be a finite number greater than zero")
    ):
        await draw_schedule(backend, "doors", at=(0.0, 0.0), scale=-1)
    assert await backend.entity_count() == before
