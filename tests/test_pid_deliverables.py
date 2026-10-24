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


async def test_line_list_keeps_a_signal_line_unnumbered_beside_a_labelled_pipe(backend):
    """The line list prints what the reader publishes, and the reader must not
    hand a 12 mm-parallel signal run the header's label (its own XDATA says
    ``number: null``)."""
    proc = await draw_line(
        backend, {"x": 20, "y": 100}, {"x": 200, "y": 100}, size="100", service="P"
    )
    ft = await place_symbol(
        backend, "instrument", 40, 112, tag="FT-1", type="dcs", location="primary"
    )
    fic = await place_symbol(
        backend, "instrument", 180, 112, tag="FIC-1", type="dcs", location="primary"
    )
    sig = await draw_line(backend, {"handle": ft["handle"]}, {"handle": fic["handle"]}, "electric")
    rows = {r["handle"]: r for r in line_list(await build_graph(backend))}
    assert (rows[proc["handle"]]["line_number"], rows[proc["handle"]]["number_source"]) == (
        "100-P-1",
        "xdata",
    )
    signal = rows[sig["handle"]]
    assert (signal["line_number"], signal["number_source"]) == (None, None)
    assert (signal["from_tag"], signal["to_tag"]) == ("FT-1", "FIC-1")


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


# ── 1.7.0 Task 3: the deliverables pass (triage rows W3-T15-04 .. -10) ─────────


async def test_natural_key_survives_a_digit_that_is_not_decimal():
    """W3-T15-04: ``'²'.isdigit()`` is True and ``int('²')`` raises."""
    assert sorted(["FT-10", "FT-1²", "FT-2"], key=natural_key) == ["FT-1²", "FT-2", "FT-10"]
    assert natural_key("①") == (0, ["①"])


async def _tapped_signal_line(backend):
    """PT-101's signal line runs down onto the P-101 -> V-201 pipe; TT-102's
    electric line lands on the MIDDLE of PT-101's signal line."""
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
    sig = await draw_line(
        backend, {"handle": pt["handle"]}, {"x": 193.5, "y": 128}, line_class="pneumatic"
    )
    tt = await place_symbol(backend, "instrument", 240, 158, tag="TT-102", location="field")
    tee = await draw_line(
        backend, {"handle": tt["handle"]}, {"x": 193.5, "y": 140}, line_class="electric"
    )
    graph = await build_graph(backend)
    on_line = [j for j in graph["junctions"] if set(j["edges"]) == {sig["handle"], tee["handle"]}]
    assert on_line, "the fixture must put a junction on the middle of PT-101's line"
    return graph, pipe, sig, tee


async def test_a_junction_on_the_instruments_line_is_reported(backend):
    """W3-T15-05: a line teed into the middle of the signal line reaches the
    instrument too, not only what the line's own two ends touch."""
    graph, pipe, _sig, tee = await _tapped_signal_line(backend)
    row = next(r for r in instrument_index(graph) if r["tag"] == "PT-101")
    assert row["connected_to"] == f"{pipe['line_number']}, {tee['handle']}"
    assert row["signal_lines"] == 1


async def test_a_line_list_end_names_lines_and_handles_never_a_junction_id(backend):
    """W3-T15-06: ``J1`` is the reader's internal id; the tag column names what
    a reader can find in the drawing, the same way the instrument index does."""
    graph, pipe, sig, tee = await _tapped_signal_line(backend)
    rows = {r["handle"]: r for r in line_list(graph)}
    assert (rows[sig["handle"]]["to_tag"], rows[sig["handle"]]["to_port"]) == (
        pipe["line_number"],
        None,
    )
    assert rows[tee["handle"]]["to_tag"] == sig["handle"]
    ends = {r["from_tag"] for r in rows.values()} | {r["to_tag"] for r in rows.values()}
    assert not ends & {j["id"] for j in graph["junctions"]}


async def test_an_untagged_node_end_is_its_handle_in_the_line_list(backend):
    """W3-T15-06: the index names an untagged node by its handle; so does the line list."""
    vessel = await place_symbol(backend, "vertical_vessel", 300, 120)
    lt = await place_symbol(backend, "instrument", 300, 200, tag="LT-201", location="field")
    signal = await draw_line(
        backend,
        {"handle": lt["handle"]},
        {"handle": vessel["handle"], "port": "N1"},
        line_class="electric",
    )
    graph = await build_graph(backend)
    row = next(r for r in line_list(graph) if r["handle"] == signal["handle"])
    assert (row["from_tag"], row["to_tag"], row["to_port"]) == ("LT-201", vessel["handle"], "N1")
    index = next(r for r in instrument_index(graph) if r["tag"] == "LT-201")
    assert index["connected_to"] == row["to_tag"]


async def test_signal_lines_counts_only_signal_classes(backend):
    """W3-T15-10: a process line drawn onto a bubble is a connection, not a signal line."""
    pt = await place_symbol(backend, "instrument", 193.5, 168, tag="PT-101", location="field")
    await draw_line(
        backend, {"handle": pt["handle"]}, {"x": 193.5, "y": 128}, line_class="pneumatic"
    )
    await backend.entity_create_line(198.5, 168, 260, 168, layer="PROCESS-PIPING-MAIN")
    graph = await build_graph(backend)
    on_bubble = [
        e
        for e in graph["edges"]
        if any((ref or {}).get("node") == pt["handle"] for ref in (e["from"], e["to"]))
    ]
    assert {e["line_class"] for e in on_bubble} == {"pneumatic", "process_major"}, (
        "the fixture must put a process line on the bubble"
    )
    row = next(r for r in instrument_index(graph) if r["tag"] == "PT-101")
    assert row["signal_lines"] == 1


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


async def test_zero_rows_still_write_the_header(backend, tmp_path):
    """W3-T15-07: a drawing with no instruments gives a header-only CSV, not 0 bytes."""
    target = tmp_path / "index.csv"
    result = await deliverable(backend, "instrument_index", csv_path=str(target))
    assert result["count"] == 0
    assert read_rows(target) == [INDEX_KEYS]


@pytest.mark.parametrize(
    ("make_path", "message"),
    [
        (lambda tmp: "", "File path cannot be empty"),
        (lambda tmp: str(tmp), "is a directory"),
        (lambda tmp: str(tmp / "missing" / "index.csv"), "Parent directory does not exist"),
    ],
)
async def test_a_bad_csv_path_is_refused_before_the_graph_is_read(
    backend, tmp_path, monkeypatch, make_path, message
):
    """W3-T15-07: ``csv_path=''`` is a path (refused), not "no CSV"; a directory is
    refused by name instead of escaping as PermissionError; a missing parent is
    refused by the path check (the old ``mkdir`` could never run); all of it
    before the drawing is read."""
    import engineering.pid.deliverables as deliverables

    async def no_graph(*args, **kwargs):
        raise AssertionError("the graph was built before the path was validated")

    monkeypatch.setattr(deliverables, "build_graph", no_graph)
    with pytest.raises(ToolError, match=message):
        await deliverable(backend, "line_list", csv_path=make_path(tmp_path))
    assert not (tmp_path / "missing").exists()


async def test_write_csv_without_columns_or_rows_is_refused(tmp_path):
    from engineering.pid.deliverables import write_csv

    with pytest.raises(ValueError, match="columns"):
        write_csv([], str(tmp_path / "x.csv"))
    assert not (tmp_path / "x.csv").exists()


async def test_write_csv_prefixes_formula_text(tmp_path):
    """W3-T15-08: ``=1+1`` and ``@x`` never reach a spreadsheet as formulas."""
    from engineering.pid.deliverables import write_csv

    target = tmp_path / "x.csv"
    write_csv([{"tag": "=1+1"}, {"tag": "@x"}, {"tag": "FT-1"}], str(target))
    assert read_rows(target) == [["tag"], ["'=1+1"], ["'@x"], ["FT-1"]]


# ── the three list tools over MCP ───────────────────────────────────────────

LIST_TOOLS = ("pid_instrument_index", "pid_line_list", "pid_equipment_list")


async def _tools():
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        return {t.name: t for t in await client.list_tools()}


async def test_the_list_tools_expose_the_graph_reading_knobs():
    """W3-T15-09: label_search and include_foreign reach the graph the lists read."""
    tools = await _tools()
    for name in LIST_TOOLS:
        properties = tools[name].inputSchema["properties"]
        assert {"label_search", "include_foreign"} <= set(properties), name


async def test_the_knobs_are_passed_through_to_the_graph(monkeypatch):
    from fastmcp import Client

    import engineering.pid.deliverables as deliverables
    import server

    seen = []

    async def fake_graph(backend, **kwargs):
        seen.append(kwargs)
        return {"nodes": [], "edges": [], "junctions": [], "stats": {}}

    monkeypatch.setattr(deliverables, "build_graph", fake_graph)
    async with Client(server.mcp) as client:
        for name in LIST_TOOLS:
            await client.call_tool(name, {"label_search": 30.0, "include_foreign": False})
    expected = {
        "tolerance": 0.5,
        "label_search": 30.0,
        "scope": "current_space",
        "include_foreign": False,
    }
    assert seen == [expected] * 3


async def test_the_list_tools_are_not_read_only_because_they_write_a_csv():
    """Spec §7: given `csv_path` they write a file, so `readOnlyHint` is false and
    the cost is `safe`; `cad_batch` and the search risk filter follow the annotation."""
    import server
    from discovery.transform import tool_risk

    tools = await _tools()
    read_only = server._batch_read_only_tools()
    registered = {t.name: t for t in await server._registered_tools()}
    for name in LIST_TOOLS:
        annotations = tools[name].annotations
        assert annotations.readOnlyHint is False and annotations.destructiveHint is False
        assert (registered[name].meta or {})["cad"]["cost"] == "safe"
        assert name not in read_only
        assert tool_risk(registered[name]) == "write"
