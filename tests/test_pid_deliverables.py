"""Instrument index, line list and equipment list are derived from the graph."""

from __future__ import annotations

import csv

import pytest
from fastmcp.exceptions import ToolError

from engineering.pid.deliverables import (
    deliverable,
    equipment_list,
    instrument_index,
    line_list,
    natural_key,
)
from engineering.pid.drawlines import draw_line
from engineering.pid.graph import build_graph
from engineering.pid.insert import place_symbol

pytestmark = pytest.mark.asyncio

INDEX_KEYS = [
    "tag",
    "func",
    "loop",
    "description",
    "type",
    "location",
    "connected_to",
    "signal_lines",
    "handle",
    "confidence",
]
LINE_KEYS = [
    "line_number",
    "line_class",
    "size",
    "service",
    "spec",
    "insulation",
    "from_tag",
    "from_port",
    "to_tag",
    "to_port",
    "length_mm",
    "handle",
    "number_source",
]
EQUIP_KEYS = [
    "tag",
    "kind",
    "symbol",
    "description",
    "ports",
    "connected_lines",
    "unconnected_ports",
    "handle",
    "confidence",
]


async def _pid(backend):
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-101", desc="Feed pump")
    vessel = await place_symbol(backend, "vertical_vessel", 220, 120, tag="V-201")
    cv = await place_symbol(backend, "globe", 160, 106, tag="FCV-101", actuator="diaphragm")
    fic = await place_symbol(
        backend, "instrument", 160, 160, tag="FIC-101", type="dcs", location="primary"
    )
    await place_symbol(
        backend, "instrument", 100, 160, tag="PT-10", type="discrete", location="field"
    )
    await draw_line(
        backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": cv["handle"], "port": "in"},
        size="100",
        service="P",
        spec="CS1",
    )
    await draw_line(
        backend,
        {"handle": cv["handle"], "port": "out"},
        {"handle": vessel["handle"], "port": "N3"},
        size="100",
        service="P",
        spec="CS1",
    )
    await draw_line(
        backend,
        {"handle": fic["handle"]},
        {"handle": cv["handle"], "port": "signal"},
        line_class="pneumatic",
    )
    # A hand-drawn line off V-201's N4 nozzle: no XDATA, no label. The reader
    # takes it as an edge (it ends on a port) with ``line_number: None`` — the
    # untagged row spec 9.5 says sorts last.
    n4 = vessel["ports"]["N4"]
    await backend.entity_create_line(
        n4["x"], n4["y"], n4["x"] + 40, n4["y"], layer="PROCESS-PIPING-MAIN"
    )
    return await build_graph(backend)


async def test_instrument_index_rows(backend):
    graph = await _pid(backend)
    rows = instrument_index(graph)
    assert [list(r) for r in rows] == [INDEX_KEYS] * 2
    assert [r["tag"] for r in rows] == ["FIC-101", "PT-10"]
    fic = rows[0]
    assert fic["description"] == "Flow Indicating Controller"
    assert fic["type"] == "dcs" and fic["location"] == "primary"
    assert fic["connected_to"] == "FCV-101" and fic["signal_lines"] == 1
    assert rows[1]["signal_lines"] == 0 and rows[1]["connected_to"] == ""


async def test_line_list_rows(backend):
    graph = await _pid(backend)
    rows = line_list(graph)
    assert [list(r) for r in rows] == [LINE_KEYS] * 4
    numbered = [r for r in rows if r["line_number"]]
    # An ISA-5.1 signal line carries no pipe line number (track E, Task 6):
    # the pneumatic line is unnumbered and did not consume a sequence number,
    # so the two process lines are 1 and 2.
    assert [r["line_number"] for r in numbered] == ["100-P-1-CS1", "100-P-2-CS1"]
    signal = next(r for r in rows if r["line_class"] == "pneumatic")
    assert signal["line_number"] is None and signal["number_source"] is None
    assert signal["from_tag"] == "FIC-101" and signal["to_tag"] == "FCV-101"
    assert signal["to_port"] == "signal"
    first = numbered[0]
    assert first["from_tag"] == "P-101" and first["from_port"] == "discharge"
    assert first["to_tag"] == "FCV-101" and first["length_mm"] > 0
    unnumbered = [r for r in rows if r["line_number"] is None]
    assert rows[-2:] == unnumbered, "untagged rows sort last"
    hand_drawn = next(r for r in unnumbered if r["from_tag"] == "V-201")
    assert hand_drawn["from_port"] == "N4"
    assert hand_drawn["to_tag"] is None and hand_drawn["number_source"] is None


async def test_equipment_list_rows(backend):
    graph = await _pid(backend)
    rows = equipment_list(graph)
    assert [list(r) for r in rows] == [EQUIP_KEYS] * 3
    assert [r["tag"] for r in rows] == ["FCV-101", "P-101", "V-201"]
    pump = next(r for r in rows if r["tag"] == "P-101")
    assert pump["description"] == "Feed pump" and pump["ports"] == 2
    assert pump["connected_lines"] == 1
    assert pump["unconnected_ports"] == "suction"


def test_natural_sort():
    assert sorted(["P-10", "P-2", "P-1A"], key=natural_key) == ["P-1A", "P-2", "P-10"]
    assert natural_key(None) > natural_key("Z-999"), "untagged rows sort last"


async def test_csv_is_written_inside_allowed_paths(backend, tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "allowed_paths", [tmp_path.resolve()])
    await _pid(backend)
    target = tmp_path / "index.csv"
    result = await deliverable(backend, "instrument_index", csv_path=str(target))
    assert result["count"] == 2 and result["csv_path"] == str(target.resolve())
    with target.open(newline="", encoding="utf-8") as fh:
        reader = list(csv.reader(fh))
    assert reader[0] == INDEX_KEYS and reader[1][0] == "FIC-101"
    outside = tmp_path.parent / "outside.csv"
    with pytest.raises(ToolError, match="not inside any allowed directory"):
        await deliverable(backend, "line_list", csv_path=str(outside))
    assert not outside.exists(), "a refused path is never written"


async def test_instrument_tapping_a_pipe_reports_the_pipe_line_number(backend):
    # A transmitter whose signal line lands on the middle of a process line
    # (not on a port) — the far end is a junction, and spec 9.5 says the row
    # names the line number of what the junction joins.
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-101")
    vessel = await place_symbol(backend, "vertical_vessel", 300, 120, tag="V-201")
    pipe = await draw_line(
        backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": vessel["handle"], "port": "N3"},
        size="100",
        service="P",
        spec="CS1",
    )
    pt = await place_symbol(backend, "instrument", 193.5, 168, tag="PT-101", location="field")
    await draw_line(
        backend, {"handle": pt["handle"]}, {"x": 193.5, "y": 128}, line_class="pneumatic"
    )
    graph = await build_graph(backend)
    signal = next(e for e in graph["edges"] if e["line_class"] == "pneumatic")
    assert "junction" in (signal["to"] or {}), "the fixture must produce a junction end"
    rows = instrument_index(graph)
    row = next(r for r in rows if r["tag"] == "PT-101")
    assert row["connected_to"] == pipe["line_number"] == "100-P-1-CS1"
    assert row["signal_lines"] == 1


async def test_instrument_on_untagged_equipment_reports_its_handle(backend):
    # The vessel has no tag, so the only honest identity is its handle — never
    # the signal line's own number (which Task 13 always stamps, so a fallback
    # to it would silently mislabel every untagged mounting).
    vessel = await place_symbol(backend, "vertical_vessel", 300, 120)
    lt = await place_symbol(backend, "instrument", 300, 200, tag="LT-201", location="field")
    signal = await draw_line(
        backend,
        {"handle": lt["handle"]},
        {"handle": vessel["handle"], "port": "N1"},
        line_class="electric",
    )
    graph = await build_graph(backend)
    row = next(r for r in instrument_index(graph) if r["tag"] == "LT-201")
    assert row["connected_to"] == vessel["handle"]
    assert row["connected_to"] != signal["line_number"]
    assert row["signal_lines"] == 1
