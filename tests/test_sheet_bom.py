"""ISO 7573 parts lists and ISO 6433 item references (balloons)."""

from __future__ import annotations

import pytest

from engineering.mech.primitives import Circle, Line, Text
from engineering.sheet.bom import (
    COLUMN_LABELS,
    DEFAULT_COLUMNS,
    LIST_WIDTH,
    ROW_HEIGHT,
    balloon_prims,
    rows_from_records,
    table_layout,
    table_prims,
)

EPS = 1e-9


def _records():
    return [
        {
            "handle": "A1",
            "designation": "ISO 4014 - M12x60",
            "standard": "ISO 4014",
            "material": "8.8",
            "qty": 1,
        },
        {
            "handle": "A2",
            "designation": "ISO 4014 - M12x60",
            "standard": "ISO 4014",
            "material": "8.8",
            "qty": 1,
        },
        {
            "handle": "B1",
            "designation": "SHAFT 40h6",
            "standard": "",
            "material": "C45",
            "qty": 1,
        },
        {
            "handle": "C1",
            "designation": "ISO 4032 - M12",
            "standard": "ISO 4032",
            "material": "8",
            "qty": 1,
        },
    ]


def test_default_columns_are_the_iso_7573_minimum_plus_material():
    assert DEFAULT_COLUMNS == ("item", "qty", "designation", "standard", "material")
    for column in DEFAULT_COLUMNS:
        assert column in COLUMN_LABELS


def test_rows_group_by_designation_sum_the_quantity_and_number_from_one():
    rows = rows_from_records(_records())
    assert [r["item"] for r in rows] == [1, 2, 3]
    by_designation = {r["designation"]: r for r in rows}
    assert by_designation["ISO 4014 - M12x60"]["qty"] == 2
    assert by_designation["ISO 4014 - M12x60"]["handles"] == ("A1", "A2")
    assert by_designation["SHAFT 40h6"]["qty"] == 1
    assert set(rows[0]) >= set(DEFAULT_COLUMNS) | {"handles"}


def test_rows_are_ordered_deterministically_whatever_order_they_arrive_in():
    forward = rows_from_records(_records())
    backward = rows_from_records(list(reversed(_records())))
    assert [r["designation"] for r in forward] == [r["designation"] for r in backward]
    assert [r["item"] for r in forward] == [r["item"] for r in backward]


def test_group_by_none_keeps_one_row_per_insert():
    rows = rows_from_records(_records(), group_by=None)
    assert [r["item"] for r in rows] == [1, 2, 3, 4]
    assert all(r["qty"] == 1 for r in rows)


def test_an_unknown_column_is_refused_with_the_list():
    with pytest.raises(ValueError) as excinfo:
        rows_from_records(_records(), columns=("item", "colour"))
    message = str(excinfo.value)
    assert "colour" in message
    assert "designation" in message


def test_the_list_is_the_title_block_width_and_the_default_shares_sum_to_it():
    assert LIST_WIDTH == 180.0
    assert ROW_HEIGHT == 7.0
    layout = table_layout(rows_from_records(_records()), at=(230.0, 70.0))
    assert layout["width"] == pytest.approx(180.0, abs=EPS)
    assert sum(layout["widths"]) == pytest.approx(180.0, abs=EPS)
    assert layout["height"] == pytest.approx(4 * 7.0, abs=EPS)  # 3 items + the heading


def test_direction_up_puts_the_heading_at_the_bottom_and_item_1_above_it():
    rows = rows_from_records(_records())
    layout = table_layout(rows, at=(230.0, 70.0), direction="up")
    assert layout["cells"][-1][0] == COLUMN_LABELS["item"]
    assert layout["cells"][-2][0] == "1"
    assert layout["cells"][0][0] == "3"
    # the table is drawn downwards from its insertion point, so the insertion
    # sits a full table height above the anchor and the list grows upwards
    assert layout["insert"] == (230.0, 70.0 + 4 * 7.0)


def test_direction_down_puts_the_heading_at_the_top():
    rows = rows_from_records(_records())
    layout = table_layout(rows, at=(230.0, 200.0), direction="down")
    assert layout["cells"][0][0] == COLUMN_LABELS["item"]
    assert layout["cells"][1][0] == "1"
    assert layout["insert"] == (230.0, 200.0)


def test_an_unknown_direction_is_refused():
    with pytest.raises(ValueError) as excinfo:
        table_layout(rows_from_records(_records()), at=(0.0, 0.0), direction="sideways")
    assert "sideways" in str(excinfo.value)


def test_table_prims_draw_the_same_rectangle_the_layout_declares():
    rows = rows_from_records(_records())
    layout = table_layout(rows, at=(230.0, 70.0))
    prims = table_prims(rows, at=(230.0, 70.0))
    xs = [p[0] for prim in prims if isinstance(prim, Line) for p in (prim.p1, prim.p2)]
    ys = [p[1] for prim in prims if isinstance(prim, Line) for p in (prim.p1, prim.p2)]
    assert min(xs) == pytest.approx(230.0, abs=EPS)
    assert max(xs) == pytest.approx(230.0 + layout["width"], abs=EPS)
    assert min(ys) == pytest.approx(70.0, abs=EPS)
    assert max(ys) == pytest.approx(70.0 + layout["height"], abs=EPS)
    assert any(isinstance(p, Text) and p.text == "ISO 4014 - M12x60" for p in prims)


def test_an_unknown_parts_list_standard_is_refused_by_name():
    with pytest.raises(ValueError) as excinfo:
        table_prims(rows_from_records(_records()), at=(0.0, 0.0), standard="ASME Y14.34")
    message = str(excinfo.value)
    assert "ASME Y14.34" in message
    assert "ISO 7573" in message


def test_a_balloon_is_a_circle_a_leader_a_dot_and_the_numeral():
    prims = balloon_prims(7, (100.0, 100.0), (80.0, 90.0))
    circles = [p for p in prims if isinstance(p, Circle)]
    lines = [p for p in prims if isinstance(p, Line)]
    texts = [p for p in prims if isinstance(p, Text)]
    assert len(circles) == 2 and len(lines) == 1 and len(texts) == 1
    balloon = max(circles, key=lambda c: c.radius)
    dot = min(circles, key=lambda c: c.radius)
    assert balloon.center == (100.0, 100.0) and balloon.radius == 4.0
    assert dot.center == (80.0, 90.0)
    assert texts[0].text == "7"
    # ISO 6433: the leader starts on the balloon's boundary and ends on the item
    start_distance = ((lines[0].p1[0] - 100.0) ** 2 + (lines[0].p1[1] - 100.0) ** 2) ** 0.5
    assert start_distance == pytest.approx(4.0, abs=1e-9)
    assert lines[0].p2 == (80.0, 90.0)


def test_a_leader_target_inside_the_balloon_is_refused():
    with pytest.raises(ValueError) as excinfo:
        balloon_prims(3, (100.0, 100.0), (101.0, 100.0))
    assert "inside" in str(excinfo.value)


def test_a_non_positive_item_number_is_refused():
    with pytest.raises(ValueError) as excinfo:
        balloon_prims(0, (0.0, 0.0), (20.0, 0.0))
    assert "0" in str(excinfo.value)


# ── reading and drawing ─────────────────────────────────────────────────────

from engineering.mech.xdata import APP_ID, to_values  # noqa: E402
from engineering.sheet.bom import (  # noqa: E402
    add_balloon,
    draw_bom_table,
    extract_records,
    read_balloons,
)


async def _place_bolt(backend, tag: str, x: float) -> str:
    await backend.block_define(
        "ISO4014_M12",
        [{"type": "circle", "cx": 0.0, "cy": 0.0, "r": 6.0}],
        [{"tag": "DESIGNATION", "x": 0.0, "y": -10.0, "height": 2.5, "default": ""}],
        overwrite=True,
    )
    insert = await backend.block_insert(
        "ISO4014_M12", x, 0.0, attributes={"DESIGNATION": "ISO 4014 - M12x60"}
    )
    await backend.entity_set_xdata(
        insert.handle,
        APP_ID,
        to_values(
            {
                "v": 1,
                "kind": "std_part",
                "designation": "ISO 4014 - M12x60",
                "standard": "ISO 4014",
                "size": "M12x60",
                "qty": 1,
                "material": "8.8",
            }
        ),
    )
    return insert.handle


@pytest.mark.asyncio
async def test_extract_reads_the_xdata_payload_and_never_touches_the_drawing(backend):
    handles = [await _place_bolt(backend, "a", 0.0), await _place_bolt(backend, "b", 50.0)]
    before = len(await backend.entity_list())
    records = await extract_records(backend)
    assert len(await backend.entity_list()) == before, "bom_extract must not modify the drawing"
    assert {r["handle"] for r in records} == set(handles)
    assert all(r["designation"] == "ISO 4014 - M12x60" for r in records)
    assert all(r["standard"] == "ISO 4014" for r in records)
    assert all(r["source"] == "xdata" for r in records)


@pytest.mark.asyncio
async def test_extract_falls_back_to_the_block_attributes_when_there_is_no_payload(backend):
    await backend.block_define(
        "PLATE",
        [{"type": "circle", "cx": 0.0, "cy": 0.0, "r": 3.0}],
        [{"tag": "DESIGNATION", "x": 0.0, "y": -6.0, "height": 2.5, "default": ""}],
        overwrite=True,
    )
    await backend.block_insert("PLATE", 10.0, 10.0, attributes={"DESIGNATION": "PLATE 10"})
    records = await extract_records(backend)
    plate = next(r for r in records if r["designation"] == "PLATE 10")
    assert plate["source"] == "attributes"
    assert plate["standard"] == ""


@pytest.mark.asyncio
async def test_the_table_is_a_real_table_entity_sitting_on_its_anchor(backend):
    await _place_bolt(backend, "a", 0.0)
    rows = rows_from_records(await extract_records(backend))
    result = await draw_bom_table(backend, rows, at=(230.0, 70.0), direction="up")
    assert result["ok"] is True
    assert result["rows"] == 1
    assert result["insert"] == (230.0, 70.0 + 2 * 7.0)
    assert result["bbox"] == {"min": [230.0, 70.0], "max": [410.0, 84.0]}
    # Measured: `entity_create_table` returns EntityInfo(type="TABLE") whose
    # `handle` is the FIRST CHILD of a composite on the headless engine -- so
    # `entity_get(handle)` reads back a rule LINE, never an ACAD_TABLE. The
    # contract is pinned by tests/test_table_leader.py; `representation` says
    # which of the two renderings a caller got.
    assert result["entity_type"] == "TABLE"
    assert result["representation"] == "composite"
    assert result["handle"] in result["child_handles"]
    table = await backend.entity_get(result["handle"])
    assert table.layer == "TEXT"


@pytest.mark.asyncio
async def test_a_balloon_records_its_targets_and_a_rerun_renumbers_instead_of_duplicating(backend):
    target = await _place_bolt(backend, "a", 0.0)
    first = await add_balloon(
        backend, item=1, at=(40.0, 40.0), leader_to=(0.0, 0.0), targets=(target,)
    )
    assert first["renumbered"] is False
    circles_after_first = len(await backend.entity_list(type_filter="CIRCLE"))

    second = await add_balloon(
        backend, item=5, at=(40.0, 40.0), leader_to=(0.0, 0.0), targets=(target,)
    )
    assert second["renumbered"] is True
    assert second["handle"] == first["handle"]
    assert len(await backend.entity_list(type_filter="CIRCLE")) == circles_after_first

    balloons = await read_balloons(backend)
    assert len(balloons) == 1
    assert balloons[0]["item"] == 5
    assert balloons[0]["targets"] == [target]
    numeral = await backend.entity_get(balloons[0]["text"])
    assert numeral.properties["text"] == "5"


@pytest.mark.asyncio
async def test_reusing_an_item_number_for_different_targets_is_refused(backend):
    one = await _place_bolt(backend, "a", 0.0)
    two = await _place_bolt(backend, "b", 60.0)
    await add_balloon(backend, item=1, at=(40.0, 40.0), leader_to=(0.0, 0.0), targets=(one,))
    before = len(await backend.entity_list())
    with pytest.raises(ValueError) as excinfo:
        await add_balloon(backend, item=1, at=(100.0, 40.0), leader_to=(60.0, 0.0), targets=(two,))
    assert "1" in str(excinfo.value)
    assert len(await backend.entity_list()) == before


@pytest.mark.asyncio
async def test_a_second_balloon_with_the_same_number_and_no_targets_is_refused(backend):
    """`targets` is optional in the tool, so the clash test used to compare
    `() != ()` -- False -- and never fire. Two balloons carrying item 1."""
    first = await add_balloon(backend, item=1, at=(40.0, 40.0), leader_to=(0.0, 0.0))
    before = len(await backend.entity_list())
    with pytest.raises(ValueError) as excinfo:
        await add_balloon(backend, item=1, at=(90.0, 40.0), leader_to=(60.0, 0.0))
    assert first["handle"] in str(excinfo.value)
    assert len(await backend.entity_list()) == before
    assert [b["item"] for b in await read_balloons(backend)] == [1]


@pytest.mark.asyncio
async def test_the_balloon_guards_read_past_the_entity_list_page(backend):
    """A routine hole pattern fills `entity_list`'s 200-entity default, which
    used to make `read_balloons` return nothing and every guard pass."""
    for index in range(200):
        await backend.entity_create_circle(float(index) * 10.0, 0.0, 2.0)
    await add_balloon(backend, item=1, at=(40.0, 400.0), leader_to=(0.0, 380.0), targets=("AAA",))
    assert [b["item"] for b in await read_balloons(backend)] == [1]
    again = await add_balloon(
        backend, item=9, at=(40.0, 400.0), leader_to=(0.0, 380.0), targets=("AAA",)
    )
    assert again["renumbered"] is True, "the guard must see a balloon past the 200th circle"
    with pytest.raises(ValueError):
        await add_balloon(
            backend, item=9, at=(200.0, 400.0), leader_to=(160.0, 380.0), targets=("BBB",)
        )


@pytest.mark.asyncio
async def test_extract_reads_every_insert_not_the_first_page(backend):
    """250 bolts must report QTY 250. The 200-entity `entity_list` default used
    to print 200 into a real TABLE with nothing saying it had been cut."""
    await backend.block_define("BOLT", [{"type": "circle", "cx": 0.0, "cy": 0.0, "r": 3.0}], [])
    for index in range(250):
        insert = await backend.block_insert("BOLT", float(index) * 10.0, 0.0)
        await backend.entity_set_xdata(
            insert.handle,
            APP_ID,
            to_values(
                {
                    "v": 1,
                    "kind": "std_part",
                    "designation": "ISO 4014 - M12x60",
                    "standard": "ISO 4014",
                    "size": "M12x60",
                    "qty": 1,
                    "material": "8.8",
                }
            ),
        )
    records = await extract_records(backend)
    assert len(records) == 250
    rows = rows_from_records(records)
    assert [(r["designation"], r["qty"]) for r in rows] == [("ISO 4014 - M12x60", 250)]
    # A cap is still honoured when one is asked for, explicitly.
    assert len(await extract_records(backend, limit=17)) == 17
    with pytest.raises(ValueError):
        await extract_records(backend, limit=0)


@pytest.mark.asyncio
async def test_the_balloon_guards_read_the_layout_they_draw_on(backend):
    """`read_balloons` follows the current space, so reading it before
    `enter_layout` inspected Model while the balloons went on the layout."""
    await backend.layout_create("SHEET1")
    first = await add_balloon(
        backend, item=1, at=(40.0, 40.0), leader_to=(0.0, 0.0), targets=("AAA",), layout="SHEET1"
    )
    again = await add_balloon(
        backend, item=5, at=(40.0, 40.0), leader_to=(0.0, 0.0), targets=("AAA",), layout="SHEET1"
    )
    assert again["renumbered"] is True
    assert again["handle"] == first["handle"]
    with pytest.raises(ValueError):
        await add_balloon(
            backend,
            item=5,
            at=(90.0, 40.0),
            leader_to=(60.0, 0.0),
            targets=("BBB",),
            layout="SHEET1",
        )
    assert (await backend.layout_list())["current"] == "Model", "the caller's tab must come back"
    await backend.layout_set_current("SHEET1")
    try:
        assert len(await backend.entity_list(type_filter="CIRCLE", limit=10_000)) == 2
    finally:
        await backend.layout_set_current("Model")
