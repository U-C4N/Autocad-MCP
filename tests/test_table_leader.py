from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backends.base import EntityInfo
from backends.com_backend import ComBackend

pytestmark = pytest.mark.asyncio


async def test_ezdxf_table_returns_composite_contract(backend):
    result = await backend.entity_create_table(
        10,
        100,
        rows=[["P-01", "4"], ["P-02", "2"]],
        headers=["Part", "Qty"],
        title="BOM",
        layer="TEXT",
    )

    assert result.type == "TABLE"
    assert result.layer == "TEXT"
    assert result.properties["representation"] == "composite"
    assert result.properties["logical_group_id"].startswith("table:")
    assert len(result.properties["child_handles"]) >= 10
    assert result.properties["bounds"] == {"min": [10.0, 72.0], "max": [30.0, 100.0]}


async def test_ezdxf_table_title_row_is_merged_across_the_table(backend):
    # AutoCAD's TABLE merges its title row. Drawn as one column's cell, a title
    # such as 'DOOR SCHEDULE' wrapped inside the first column and ran over the
    # header row below it. The title spans the table, and only the table's outer
    # edges cross its row.
    result = await backend.entity_create_table(
        0,
        100,
        rows=[["D1", "900"]],
        headers=["TAG", "WIDTH"],
        column_widths=[20, 30],
        row_height=10,
        text_height=2.5,
        title="DOOR SCHEDULE",
    )
    children = [backend._doc.entitydb[h] for h in result.properties["child_handles"]]
    (title,) = [e for e in children if e.dxftype() == "MTEXT" and e.text == "DOOR SCHEDULE"]
    assert title.dxf.width == pytest.approx(50.0 - 2 * 1.0)  # padding min(1.0, 10 x 0.15)
    verticals = [e for e in children if e.dxftype() == "LINE" and e.dxf.start.x == e.dxf.end.x]
    inner = [v for v in verticals if 0.0 < v.dxf.start.x < 50.0]
    assert inner and all(max(v.dxf.start.y, v.dxf.end.y) == pytest.approx(90.0) for v in inner)
    outer = [v for v in verticals if v.dxf.start.x in (0.0, 50.0)]
    assert len(outer) == 2 and all(max(v.dxf.start.y, v.dxf.end.y) == 100.0 for v in outer)


async def test_ezdxf_table_cell_margin_scales_with_the_lettering(backend):
    # A 1:50 schedule letters at 125 in rows of 350. A flat 1.0 margin is 0.02 mm
    # on that sheet: the text sat on the cell line, and 'LEFT' read as 'FT'.
    result = await backend.entity_create_table(
        0,
        0,
        rows=[["D1", "LEFT"]],
        column_widths=[750, 1000],
        row_height=350,
        text_height=125,
    )
    children = [backend._doc.entitydb[h] for h in result.properties["child_handles"]]
    (left,) = [e for e in children if e.dxftype() == "MTEXT" and e.text == "LEFT"]
    assert left.dxf.insert.x == pytest.approx(750 + 50.0)  # 0.4 x 125
    assert left.dxf.insert.y == pytest.approx(-50.0)
    assert left.dxf.width == pytest.approx(1000 - 2 * 50.0)


async def test_ezdxf_table_survives_save_and_reopen(backend, tmp_path):
    result = await backend.entity_create_table(0, 30, rows=[["A", "1"]], headers=["Name", "Qty"])
    path = tmp_path / "table.dxf"
    await backend.drawing_save_as(str(path), fmt="dxf")
    await backend.drawing_open(str(path))

    entities = await backend.entity_list(limit=100)
    handles = {entity.handle for entity in entities}
    texts = {
        entity.properties.get("text") for entity in entities if entity.type in {"TEXT", "MTEXT"}
    }
    assert set(result.properties["child_handles"]).issubset(handles)
    assert {"Name", "Qty", "A", "1"}.issubset(texts)


async def test_table_rejects_ragged_rows_and_size_limit(backend):
    with pytest.raises(RuntimeError, match="same number of columns"):
        await backend.entity_create_table(0, 0, rows=[["A", "B"], ["C"]])

    with pytest.raises(RuntimeError, match="200 rows"):
        await backend.entity_create_table(0, 0, rows=[["A"]] * 201)


async def test_ezdxf_mleader_returns_composite_contract(backend):
    result = await backend.leader_create_mleader(
        [[0, 0], [10, 5], [30, 5]],
        "SURFACE A",
        layer="DIM",
    )

    assert result.type == "MLEADER"
    assert result.properties["representation"] == "composite"
    assert result.properties["logical_group_id"].startswith("mleader:")
    assert result.properties["text"] == "SURFACE A"
    assert len(result.properties["child_handles"]) == 3


async def test_mleader_requires_two_points(backend):
    with pytest.raises(RuntimeError, match="at least two points"):
        await backend.leader_create_mleader([[0, 0]], "note")


async def test_com_table_uses_native_add_table():
    backend = ComBackend()

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    backend._run = run_inline
    mspace = MagicMock()
    table = MagicMock()
    table.Handle = "A1"
    mspace.AddTable.return_value = table
    info = EntityInfo("A1", "ACAD_TABLE", "TEXT", 256, "ByLayer", True, {})

    with (
        patch("backends.com_backend._msp", return_value=mspace),
        patch("backends.com_backend._apoint", side_effect=lambda *p: tuple(p)),
        patch("backends.com_backend._entity_info", return_value=info),
        patch("backends.com_backend._apply_entity_attrs"),
        patch("backends.com_backend._regen"),
    ):
        result = await backend.entity_create_table(
            0, 50, [["A", "1"]], headers=["Name", "Qty"], title="BOM"
        )

    mspace.AddTable.assert_called_once()
    assert mspace.AddTable.call_args[0][1:3] == (3, 2)
    assert table.SetText.call_count == 6
    assert result.type == "TABLE"
    assert result.properties["representation"] == "native"


async def test_com_mleader_uses_native_add_mleader():
    backend = ComBackend()

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    backend._run = run_inline
    mspace = MagicMock()
    leader = MagicMock()
    leader.Handle = "B1"
    mspace.AddMLeader.return_value = leader
    info = EntityInfo("B1", "ACAD_MLEADER", "DIM", 256, "ByLayer", True, {})

    with (
        patch("backends.com_backend._msp", return_value=mspace),
        patch("backends.com_backend._av", side_effect=lambda values: values),
        patch("backends.com_backend._entity_info", return_value=info),
        patch("backends.com_backend._apply_entity_attrs"),
        patch("backends.com_backend._regen"),
    ):
        result = await backend.leader_create_mleader([[0, 0], [20, 5]], "NOTE")

    mspace.AddMLeader.assert_called_once_with([0.0, 0.0, 0.0, 20.0, 5.0, 0.0], 0)
    assert leader.TextString == "NOTE"
    assert result.type == "MLEADER"
    assert result.properties["representation"] == "native"


async def test_server_registers_table_and_mleader_tools():
    import server

    names = {tool.name for tool in await server._registered_tools()}
    assert "entity_create_table" in names
    assert "leader_create_mleader" in names


class _MeasuredTable:
    """The IAcadTable members a live AutoCAD 2026 seat exposes (measured).

    MEASURED on AutoCAD 2026 (track F task 6): the table has no ``TextHeight``
    property - pywin32 answers ``table.TextHeight = h`` with AttributeError -
    and a table left alone keeps the Standard style's heights (title 6.0, header
    and data 4.5). ``SetCellTextHeight(row, col, h)`` is the member that sets it.
    A fake that accepted ``TextHeight`` would hide exactly that defect.
    """

    def __init__(self, rows, columns):
        self.Handle = "A1"
        self.Rows, self.Columns = rows, columns
        self._heights = {
            (r, c): (6.0 if r == 0 else 4.5) for r in range(rows) for c in range(columns)
        }

    def __setattr__(self, name, value):
        if name == "TextHeight":
            raise AttributeError(f"object has no attribute {name!r}")
        object.__setattr__(self, name, value)

    def SetColumnWidth(self, column, width):
        pass

    def SetRowHeight(self, row, height):
        pass

    def SetText(self, row, column, value):
        pass

    def SetCellTextHeight(self, row, column, height):
        self._heights[(row, column)] = float(height)

    def GetCellTextHeight(self, row, column):
        return self._heights[(row, column)]


async def test_com_table_sets_every_cell_text_height_through_the_measured_member():
    backend = ComBackend()

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    backend._run = run_inline
    mspace = MagicMock()
    made: list[_MeasuredTable] = []

    def add_table(_point, rows, columns, _row_height, _column_width):
        made.append(_MeasuredTable(rows, columns))
        return made[-1]

    mspace.AddTable.side_effect = add_table
    info = EntityInfo("A1", "ACAD_TABLE", "TEXT", 256, "ByLayer", True, {})

    with (
        patch("backends.com_backend._msp", return_value=mspace),
        patch("backends.com_backend._apoint", side_effect=lambda *p: tuple(p)),
        patch("backends.com_backend._entity_info", return_value=info),
        patch("backends.com_backend._apply_entity_attrs"),
        patch("backends.com_backend._regen"),
    ):
        await backend.entity_create_table(
            0, 50, [["A", "1"]], headers=["Name", "Qty"], title="BOM", text_height=125.0
        )

    (table,) = made
    assert {
        (row, column): table.GetCellTextHeight(row, column)
        for row in range(table.Rows)
        for column in range(table.Columns)
    } == {(row, column): 125.0 for row in range(3) for column in range(2)}
