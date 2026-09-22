"""pid_graph reads the drawing back: our symbols exactly, foreign ones with confidence."""

from __future__ import annotations

import math

import pytest

from engineering.pid.drawlines import draw_line
from engineering.pid.graph import build_graph
from engineering.pid.insert import place_symbol

pytestmark = pytest.mark.asyncio


async def _small_pid(backend):
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-101", desc="Feed pump")
    hv = await place_symbol(backend, "gate", 160, 104, tag="HV-101", actuator="hand")
    vessel = await place_symbol(backend, "vertical_vessel", 220, 120, tag="V-201")
    fic = await place_symbol(
        backend, "instrument", 160, 160, tag="FIC-101", type="dcs", location="primary"
    )
    l1 = await draw_line(
        backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": hv["handle"], "port": "in"},
        size="100",
        service="P",
        spec="CS1",
    )
    l2 = await draw_line(
        backend,
        {"handle": hv["handle"], "port": "out"},
        {"handle": vessel["handle"], "port": "N3"},
        line_number=l1["line_number"],
    )
    sig = await draw_line(
        backend, {"handle": fic["handle"]}, {"x": 160, "y": 112}, line_class="electric"
    )
    return {"pump": pump, "hv": hv, "vessel": vessel, "fic": fic, "l1": l1, "l2": l2, "sig": sig}


async def test_tool_drawn_pid_is_read_back_exactly(backend):
    ids = await _small_pid(backend)
    graph = await build_graph(backend)
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert set(nodes) == {
        ids["pump"]["handle"],
        ids["hv"]["handle"],
        ids["vessel"]["handle"],
        ids["fic"]["handle"],
    }
    assert nodes[ids["pump"]["handle"]]["kind"] == "equipment"
    assert nodes[ids["pump"]["handle"]]["tag"] == "P-101"
    assert nodes[ids["pump"]["handle"]]["description"] == "Feed pump"
    assert nodes[ids["fic"]["handle"]]["kind"] == "instrument"
    assert nodes[ids["fic"]["handle"]]["tag"] == "FIC-101"
    assert nodes[ids["fic"]["handle"]]["tag_parsed"]["description"] == "Flow Indicating Controller"
    assert all(n["source"] == "catalog" and n["confidence"] == 1.0 for n in graph["nodes"])
    edges = {e["id"]: e for e in graph["edges"]}
    assert set(edges) == {ids["l1"]["handle"], ids["l2"]["handle"], ids["sig"]["handle"]}
    l1 = edges[ids["l1"]["handle"]]
    assert l1["from"] == {"node": ids["pump"]["handle"], "port": "discharge"}
    assert l1["to"] == {"node": ids["hv"]["handle"], "port": "in"}
    assert l1["line_number"] == "100-P-1-CS1"
    assert l1["number_source"] == "xdata" and l1["class_source"] == "xdata"
    assert nodes[ids["hv"]["handle"]]["ports"]["in"]["edges"] == [ids["l1"]["handle"]]
    sig = edges[ids["sig"]["handle"]]
    assert sig["from"] == {"node": ids["fic"]["handle"], "port": "signal"}
    assert sig["line_class"] == "electric"
    assert len(graph["dangling"]) == 1 and graph["dangling"][0]["edge"] == ids["sig"]["handle"]
    assert graph["stats"]["nodes_by_kind"] == {"equipment": 2, "instrument": 1, "valve": 1}
    assert graph["stats"]["confidence_min"] == 1.0 and graph["stats"]["dangling"] == 1
    assert graph["junctions"] == [] and graph["unclassified"] == []


async def test_a_nudged_symbol_makes_its_line_dangle_with_a_hint(backend):
    ids = await _small_pid(backend)
    await backend.entity_move(ids["hv"]["handle"], 0.0, 1.0)
    graph = await build_graph(backend, tolerance=0.5)
    dangling = {d["edge"]: d for d in graph["dangling"]}
    assert ids["l1"]["handle"] in dangling and ids["l2"]["handle"] in dangling
    hint = dangling[ids["l1"]["handle"]]["nearest"]
    assert hint["node"] == ids["hv"]["handle"] and hint["port"] == "in"
    assert hint["distance"] == pytest.approx(1.0)
    assert await _no_dangling_after(backend, tolerance=1.5)


async def _no_dangling_after(backend, tolerance):
    graph = await build_graph(backend, tolerance=tolerance)
    return graph["stats"]["dangling"] == 1  # only the deliberately open signal line


async def test_branch_onto_a_line_becomes_a_junction(backend):
    ids = await _small_pid(backend)
    # l1 runs (100,106)->(100,111)->(151,111)->(151,104)->(156,104); (130,111) is on its
    # second segment
    branch = await draw_line(
        backend, {"x": 130, "y": 111}, {"x": 130, "y": 60}, line_class="process_minor"
    )
    graph = await build_graph(backend)
    edge = next(e for e in graph["edges"] if e["id"] == branch["handle"])
    assert edge["from"] and "junction" in edge["from"]
    junction = next(j for j in graph["junctions"] if j["id"] == edge["from"]["junction"])
    assert (junction["x"], junction["y"]) == pytest.approx((130.0, 111.0))
    assert set(junction["edges"]) == {branch["handle"], ids["l1"]["handle"]}


async def test_rotated_and_scaled_symbols_are_measured_not_assumed(backend):
    await place_symbol(backend, "centrifugal_pump", 0, 0, rotation=90.0, scale=2.0, tag="P-1")
    graph = await build_graph(backend)
    node = graph["nodes"][0]
    assert node["ports"]["discharge"]["x"] == pytest.approx(-12.0)
    assert node["ports"]["discharge"]["direction_deg"] == pytest.approx(180.0)
    assert node["rotation_deg"] == pytest.approx(90.0) and node["scale"] == pytest.approx(2.0)


async def test_foreign_blocks_classify_with_reduced_confidence_and_inferred_ports(backend):
    blk = backend._doc.blocks.new(name="GATE_VALVE")
    blk.add_lwpolyline([(-4, -2), (-4, 2), (0, 0)], close=True)
    blk.add_lwpolyline([(4, -2), (4, 2), (0, 0)], close=True)
    blk.add_attdef("TAG", insert=(0, 4), text="", dxfattribs={"height": 2.5})
    logo = backend._doc.blocks.new(name="COMPANY_LOGO")
    logo.add_circle((0, 0), 5)
    await backend.block_insert("GATE_VALVE", 50, 50, attributes={"TAG": "HV-9"})
    await backend.block_insert("COMPANY_LOGO", 300, 300)
    await backend.entity_create_line(10, 50, 46, 50, layer="PROCESS-PIPING-MAIN")
    graph = await build_graph(backend)
    assert len(graph["nodes"]) == 1
    node = graph["nodes"][0]
    assert node["kind"] == "valve" and node["source"] == "heuristic" and node["confidence"] == 0.6
    assert node["tag"] == "HV-9" and node["tag_source"] == "attribute"
    port = next(iter(node["ports"].values()))
    assert port["inferred"] is True and (port["x"], port["y"]) == pytest.approx((46.0, 50.0))
    assert graph["stats"]["confidence_min"] == 0.6
    edge = graph["edges"][0]
    assert edge["line_class"] == "process_major" and edge["class_source"] == "layer"
    assert graph["stats"]["entities_scanned"] >= 3


async def test_a_foreign_line_ending_on_the_body_behind_the_tag_text_attaches(backend):
    """The INSERT's ``bounding_box`` takes the ATTRIB with it, so the TAG
    lettered above the valve pushed the box to x~60 and a line ending on the
    body's real right edge (54,50) was *inside* the box — and dangled."""
    blk = backend._doc.blocks.new(name="GATE_VALVE")
    blk.add_lwpolyline([(-4, -2), (-4, 2), (0, 0)], close=True)
    blk.add_lwpolyline([(4, -2), (4, 2), (0, 0)], close=True)
    blk.add_attdef("TAG", insert=(0, 4), text="", dxfattribs={"height": 2.5})
    valve = (await backend.block_insert("GATE_VALVE", 50, 50, attributes={"TAG": "HV-9"})).handle
    props = (await backend.entity_get(valve)).properties
    assert props["geometry_bbox"]["min"] == pytest.approx([46.0, 48.0])
    assert props["geometry_bbox"]["max"] == pytest.approx([54.0, 52.0])
    assert props["bounding_box"]["max"][0] > 54.0  # the attribute-inclusive box
    line = (await backend.entity_create_line(54, 50, 90, 50, layer="PROCESS-PIPING-MAIN")).handle
    graph = await build_graph(backend)
    edge = next(e for e in graph["edges"] if e["id"] == line)
    assert edge["from"] == {"node": valve, "port": "p1"}
    node = next(n for n in graph["nodes"] if n["id"] == valve)
    port = node["ports"]["p1"]
    assert port["inferred"] is True and (port["x"], port["y"]) == pytest.approx((54.0, 50.0))
    assert (line, "from") not in {(d["edge"], d["end"]) for d in graph["dangling"]}


async def test_a_foreign_vessel_takes_lines_on_every_edge_regardless_of_where_its_tag_sits(
    backend,
):
    blk = backend._doc.blocks.new(name="VESSEL_V")
    blk.add_lwpolyline([(-5, -10), (5, -10), (5, 10), (-5, 10)], close=True)
    blk.add_attdef("TAG", insert=(0, 13), text="", dxfattribs={"height": 2.5})
    vessel = (await backend.block_insert("VESSEL_V", 100, 100, attributes={"TAG": "V-1"})).handle
    props = (await backend.entity_get(vessel)).properties
    assert props["geometry_bbox"]["min"] == pytest.approx([95.0, 90.0])
    assert props["geometry_bbox"]["max"] == pytest.approx([105.0, 110.0])
    top = (await backend.entity_create_line(100, 140, 100, 110, layer="PROCESS-PIPING-MAIN")).handle
    side = (
        await backend.entity_create_line(140, 100, 105, 100, layer="PROCESS-PIPING-MAIN")
    ).handle
    graph = await build_graph(backend)
    edges = {e["id"]: e for e in graph["edges"]}
    assert edges[top]["to"]["node"] == vessel and edges[side]["to"]["node"] == vessel
    node = next(n for n in graph["nodes"] if n["id"] == vessel)
    assert node["kind"] == "equipment" and node["tag"] == "V-1"
    ends = {(round(p["x"], 6), round(p["y"], 6)) for p in node["ports"].values()}
    assert ends == {(100.0, 110.0), (105.0, 100.0)}
    assert {(d["edge"], d["end"]) for d in graph["dangling"]} == {(top, "from"), (side, "from")}


async def test_a_line_drawn_into_a_foreign_body_is_dangling_unless_tolerance_reaches_the_edge(
    backend,
):
    """Spec §9.2 step 3: a foreign port is an end within ``tolerance`` of the
    block's bounding-box *boundary*. The interior is not a port — a border
    INSERT's interior is the whole sheet."""
    small = backend._doc.blocks.new(name="GATE_VALVE")
    small.add_lwpolyline([(-4, -2), (4, -2), (4, 2), (-4, 2)], close=True)
    valve = (await backend.block_insert("GATE_VALVE", 20, 0, attributes={"TAG": "HV-2"})).handle
    # body x in [16, 24]; the line overshoots 2 mm into it
    line = (await backend.entity_create_line(60, 0, 22, 0, layer="PROCESS-PIPING-MAIN")).handle
    graph = await build_graph(backend, tolerance=0.5)
    edge = next(e for e in graph["edges"] if e["id"] == line)
    assert edge["to"] is None
    assert {(d["edge"], d["end"]) for d in graph["dangling"]} == {(line, "from"), (line, "to")}
    assert next(n for n in graph["nodes"] if n["id"] == valve)["ports"] == {}
    graph = await build_graph(backend, tolerance=2.0)
    edge = next(e for e in graph["edges"] if e["id"] == line)
    assert edge["to"] == {"node": valve, "port": "p1"}
    assert {(d["edge"], d["end"]) for d in graph["dangling"]} == {(line, "from")}


async def test_two_foreign_boundaries_sharing_an_end_go_to_the_nearest_insertion(backend):
    big = backend._doc.blocks.new(name="VESSEL_BIG")
    big.add_lwpolyline([(-30, -30), (30, -30), (30, 30), (-30, 30)], close=True)
    small = backend._doc.blocks.new(name="GATE_VALVE")
    small.add_lwpolyline([(-4, -2), (4, -2), (4, 2), (-4, 2)], close=True)
    vessel = (await backend.block_insert("VESSEL_BIG", 0, 0, attributes={"TAG": "V-2"})).handle
    # the valve sits flush with the vessel's right wall: both boundaries hold (30, 0)
    valve = (await backend.block_insert("GATE_VALVE", 26, 0, attributes={"TAG": "HV-2"})).handle
    line = (await backend.entity_create_line(60, 0, 30, 0, layer="PROCESS-PIPING-MAIN")).handle
    graph = await build_graph(backend)
    edge = next(e for e in graph["edges"] if e["id"] == line)
    assert edge["to"] == {"node": valve, "port": "p1"}
    assert next(n for n in graph["nodes"] if n["id"] == vessel)["ports"] == {}


async def test_a_sheet_border_insert_never_swallows_junctions_or_dangling_ends(backend):
    """The border is an INSERT whose box encloses the whole sheet. Accepting
    ends strictly inside a foreign box made every end that missed a port a
    port of the border: no junction, no dangling end, the signal line
    'connected' to A3_BORDER."""
    border = backend._doc.blocks.new(name="A3_BORDER")
    border.add_lwpolyline([(0, 0), (420, 0), (420, 297), (0, 297)], close=True)
    sheet = (await backend.block_insert("A3_BORDER", 0, 0)).handle
    ids = await _small_pid(backend)
    branch = await draw_line(
        backend, {"x": 130, "y": 111}, {"x": 130, "y": 60}, line_class="process_minor"
    )
    graph = await build_graph(backend)
    assert graph["unclassified"] == [{"handle": sheet, "block_name": "A3_BORDER"}]
    assert sheet not in {n["id"] for n in graph["nodes"]}
    assert [(j["id"], j["x"], j["y"]) for j in graph["junctions"]] == [("J1", 130.0, 111.0)]
    assert set(graph["junctions"][0]["edges"]) == {branch["handle"], ids["l1"]["handle"]}
    dangling = {(d["edge"], d["end"]) for d in graph["dangling"]}
    assert dangling == {(ids["sig"]["handle"], "to"), (branch["handle"], "to")}
    assert graph["stats"]["dangling"] == 2 and graph["stats"]["unclassified"] == 1
    edges = {e["id"]: e for e in graph["edges"]}
    assert edges[ids["sig"]["handle"]]["to"] is None
    assert edges[branch["handle"]]["from"] == {"junction": "J1"}


async def test_a_foreign_title_block_does_not_connect_the_valve_or_adopt_loose_lines(backend):
    title = backend._doc.blocks.new(name="TITLEBLOCK_A3")
    title.add_lwpolyline([(0, 0), (420, 0), (420, 297), (0, 297)], close=True)
    title.add_lwpolyline([(240, 0), (240, 60), (420, 60)])
    blk = backend._doc.blocks.new(name="GATE_VALVE")
    blk.add_lwpolyline([(-4, -2), (-4, 2), (0, 0)], close=True)
    blk.add_lwpolyline([(4, -2), (4, 2), (0, 0)], close=True)
    blk.add_attdef("TAG", insert=(0, 4), text="", dxfattribs={"height": 2.5})
    sheet = (await backend.block_insert("TITLEBLOCK_A3", 0, 0)).handle
    valve = (await backend.block_insert("GATE_VALVE", 50, 50, attributes={"TAG": "HV-9"})).handle
    left = (await backend.entity_create_line(10, 50, 46, 50, layer="PROCESS-PIPING-MAIN")).handle
    right = (await backend.entity_create_line(54, 50, 120, 50, layer="PROCESS-PIPING-MAIN")).handle
    loose = (await backend.entity_create_line(200, 200, 260, 200, layer="0")).handle
    graph = await build_graph(backend)
    edges = {e["id"]: e for e in graph["edges"]}
    assert set(edges) == {left, right}
    assert loose not in edges and "unknown" not in graph["stats"]["edges_by_class"]
    assert edges[left]["to"] == {"node": valve, "port": "p1"}
    assert edges[right]["from"] == {"node": valve, "port": "p2"}
    assert edges[left]["from"] is None and edges[right]["to"] is None
    assert {(d["edge"], d["end"]) for d in graph["dangling"]} == {(left, "from"), (right, "to")}
    assert graph["stats"]["dangling"] == 2
    assert graph["unclassified"] == [{"handle": sheet, "block_name": "TITLEBLOCK_A3"}]
    assert {n["id"] for n in graph["nodes"]} == {valve}


async def test_include_foreign_false_ignores_untagged_blocks(backend):
    blk = backend._doc.blocks.new(name="GATE_VALVE")
    blk.add_line((0, 0), (1, 0))
    await backend.block_insert("GATE_VALVE", 50, 50)
    graph = await build_graph(backend, include_foreign=False)
    assert graph["nodes"] == [] and graph["edges"] == []


async def test_line_number_and_tags_from_nearby_text(backend):
    await backend.entity_create_line(0, 0, 100, 0, layer="PROCESS-PIPING-MAIN")
    await backend.entity_create_text('6"-P-1234-CS1', 40, 2, 2.5, layer="PROCESS-LINE-TEXT")
    graph = await build_graph(backend)
    edge = graph["edges"][0]
    assert edge["line_number"] == '6"-P-1234-CS1' and edge["number_source"] == "label"


async def test_a_signal_line_next_to_a_pipe_label_never_adopts_the_pipes_number(backend):
    """The label search is a fallback for a line with no record of its own.

    A transmitter-to-controller run 12 mm above a labelled header (the
    ordinary spacing on a dense sheet) is well inside ``label_search``; its
    own payload says ``number: null`` and a signal line has no pipe line
    number, so the reader must report None — not the header's number under
    ``number_source="label"``.
    """
    proc = await draw_line(
        backend, {"x": 20, "y": 100}, {"x": 200, "y": 100}, size="100", service="P"
    )
    assert proc["line_number"] == "100-P-1"
    ft = await place_symbol(
        backend, "instrument", 40, 112, tag="FT-1", type="dcs", location="primary"
    )
    fic = await place_symbol(
        backend, "instrument", 180, 112, tag="FIC-1", type="dcs", location="primary"
    )
    sig = await draw_line(backend, {"handle": ft["handle"]}, {"handle": fic["handle"]}, "electric")
    assert sig["line_number"] is None
    graph = await build_graph(backend)
    by_handle = {e["id"]: e for e in graph["edges"]}
    pipe, signal = by_handle[proc["handle"]], by_handle[sig["handle"]]
    assert (pipe["line_number"], pipe["number_source"]) == ("100-P-1", "xdata")
    assert (signal["line_number"], signal["number_source"]) == (None, None)
    assert signal["from"]["node"] == ft["handle"] and signal["to"]["node"] == fic["handle"]


async def test_a_verbatim_number_on_a_signal_line_is_still_read_back(backend):
    ft = await place_symbol(
        backend, "instrument", 40, 112, tag="FT-1", type="dcs", location="primary"
    )
    fic = await place_symbol(
        backend, "instrument", 180, 112, tag="FIC-1", type="dcs", location="primary"
    )
    sig = await draw_line(
        backend,
        {"handle": ft["handle"]},
        {"handle": fic["handle"]},
        "electric",
        line_number="SIG-7",
    )
    graph = await build_graph(backend)
    edge = next(e for e in graph["edges"] if e["id"] == sig["handle"])
    assert (edge["line_number"], edge["number_source"]) == ("SIG-7", "xdata")


async def test_a_foreign_signal_line_next_to_a_pipe_label_stays_unnumbered(backend):
    """A layer-classified signal line (no payload) is a signal all the same:
    LINE_NUMBER_RE is the pipe grammar, so a match near it is a neighbour's.
    The pipe on the same sheet still takes its label."""
    await backend.entity_create_line(0, 0, 100, 0, layer="PROCESS-PIPING-MAIN")
    await backend.entity_create_text('6"-P-1234-CS1', 40, 2, 2.5, layer="PROCESS-LINE-TEXT")
    await backend.entity_create_line(0, 6, 100, 6, layer="ELECTRICAL-LINE")
    await backend.entity_create_line(0, 10, 100, 10, layer="INSTRUMENT-LINE-SIGNAL")
    graph = await build_graph(backend)
    by_class = {e["line_class"]: e for e in graph["edges"]}
    assert set(by_class) == {"process_major", "electric", "signal_unknown"}
    pipe = by_class["process_major"]
    assert (pipe["line_number"], pipe["number_source"]) == ('6"-P-1234-CS1', "label")
    for cls in ("electric", "signal_unknown"):
        assert (by_class[cls]["line_number"], by_class[cls]["number_source"]) == (None, None), cls


async def test_scope_all_walks_every_layout_and_restores_the_current_one(backend):
    await place_symbol(backend, "gate", 0, 0, tag="HV-1")
    await backend.layout_create("SHEET-2")
    await backend.layout_set_current("SHEET-2")
    await place_symbol(backend, "gate", 0, 0, tag="HV-2")
    await backend.layout_set_current("Model")
    graph = await build_graph(backend, scope="all")
    assert sorted(n["tag"] for n in graph["nodes"]) == ["HV-1", "HV-2"]
    assert sorted(n["space"] for n in graph["nodes"]) == ["Model", "SHEET-2"]
    assert (await backend.layout_list())["current"] == "Model"


async def test_offpage_links_pair_by_link_id(backend):
    out = await place_symbol(backend, "offpage", 0, 0, tag="TO 002", link="L-17", direction="out")
    inn = await place_symbol(
        backend, "offpage", 100, 0, tag="FROM 001", link="L-17", direction="in"
    )
    graph = await build_graph(backend)
    assert graph["offpage_links"] == [
        {"link": "L-17", "nodes": sorted([out["handle"], inn["handle"]])}
    ]


async def test_geometry_is_optional(backend):
    await _small_pid(backend)
    lean = await build_graph(backend)
    assert "vertices" not in lean["edges"][0]
    full = await build_graph(backend, include_geometry=True)
    assert len(full["edges"][0]["vertices"]) >= 2


async def test_a_line_ending_on_a_discarded_foreign_line_dangles(backend):
    """A foreign line is an edge only when an end touches a node (spec §9.2 4c).

    Junctions used to be resolved before that decision, so a P&ID line ending
    on a foreign line that was then dropped kept ``{"junction": "J1"}`` while
    ``junctions`` was empty and ``stats.dangling`` was one short — an id
    Task 15's line list would dereference into nothing."""
    main = (await backend.entity_create_line(0, 0, 50, 0, layer="PROCESS-PIPING-MAIN")).handle
    await backend.entity_create_line(50, -20, 50, 20, layer="0")
    graph = await build_graph(backend)
    edge = graph["edges"][0]
    assert edge["id"] == main and edge["from"] is None and edge["to"] is None
    assert graph["junctions"] == []
    assert {(d["edge"], d["end"]) for d in graph["dangling"]} == {(main, "from"), (main, "to")}
    assert graph["stats"]["dangling"] == 2


async def test_a_foreign_line_ending_on_another_discarded_foreign_line_dangles(backend):
    blk = backend._doc.blocks.new(name="GATE_VALVE")
    blk.add_lwpolyline([(-4, -2), (-4, 2), (0, 0)], close=True)
    blk.add_lwpolyline([(4, -2), (4, 2), (0, 0)], close=True)
    valve = (await backend.block_insert("GATE_VALVE", 50, 50)).handle
    kept = (await backend.entity_create_line(46, 50, 10, 50, layer="0")).handle  # on the bbox
    await backend.entity_create_line(10, 50, 10, 0, layer="0")  # touches nothing: not an edge
    graph = await build_graph(backend)
    assert [e["id"] for e in graph["edges"]] == [kept]
    edge = graph["edges"][0]
    assert edge["from"] == {"node": valve, "port": "p1"} and edge["to"] is None
    assert graph["junctions"] == []
    assert [(d["edge"], d["end"]) for d in graph["dangling"]] == [(kept, "to")]
    assert graph["stats"]["dangling"] == 1


async def test_edge_length_is_the_engines_measurement_not_a_chord_walk(backend):
    """A curved crossing "jump" is an arc in the drawing; the chord walk over
    its vertices was 5.7 units short and said nothing about it. The engine
    measures the real geometry, and a line ending *on the arc* is a junction."""
    msp = backend._doc.modelspace()
    pipe = msp.add_lwpolyline(
        [(-20, 0, 0), (0, 0, 1), (10, 0, 0), (30, 0, 0)],
        format="xyb",
        dxfattribs={"layer": "PROCESS-PIPING-MAIN"},
    )
    pipe = pipe.dxf.handle
    measured = (await backend.entity_get(pipe)).properties["length"]
    assert measured == pytest.approx(20 + 5 * math.pi + 20)
    # bulge 1 from (0,0) to (10,0) bows to -y: centre (5,0), radius 5 → apex (5,-5)
    branch = await backend.entity_create_line(5, -5, 5, -30, layer="PROCESS-PIPING-SECONDARY")
    branch = branch.handle
    graph = await build_graph(backend)
    edges = {e["id"]: e for e in graph["edges"]}
    assert edges[pipe]["length"] == pytest.approx(measured)
    assert "length_approximate" not in edges[pipe]
    assert edges[branch]["from"] and "junction" in edges[branch]["from"]
    junction = graph["junctions"][0]
    assert (junction["x"], junction["y"]) == pytest.approx((5.0, -5.0))
    assert set(junction["edges"]) == {pipe, branch}
    full = await build_graph(backend, include_geometry=True)
    assert {e["id"]: e for e in full["edges"]}[pipe]["bulges"] == [0.0, 1.0, 0.0, 0.0]


def test_point_to_arc_distance_follows_the_bulge():
    from engineering.pid.graph import _point_segment_distance

    a, b = (0.0, 0.0), (10.0, 0.0)
    assert _point_segment_distance((5, -5), a, b, 1.0) == pytest.approx(0.0)  # CCW apex
    assert _point_segment_distance((5, 5), a, b, -1.0) == pytest.approx(0.0)  # CW apex
    assert _point_segment_distance((5, 5), a, b, 1.0) == pytest.approx(math.hypot(5, 5))
    assert _point_segment_distance((5, -2.5), a, b, 0.5) == pytest.approx(0.0)  # sagitta
    assert _point_segment_distance((5, 2.5), a, b, 0.5) == pytest.approx(5.0)
    assert _point_segment_distance((5, 0), a, b, 0.0) == pytest.approx(0.0)  # straight


def _arc_distance_sampled(p, a, b, bulge, samples=20000):
    """Nearest distance from ``p`` to the bulge arc, measured on ezdxf's own
    arc (centre, angles, radius) by dense sampling — an independent answer."""
    from ezdxf.math import Vec2, bulge_to_arc

    centre, start, end, radius = bulge_to_arc(Vec2(a), Vec2(b), bulge)
    if end < start:
        end += 2.0 * math.pi
    return min(
        math.dist(
            p,
            (
                centre.x + radius * math.cos(start + (end - start) * i / samples),
                centre.y + radius * math.sin(start + (end - start) * i / samples),
            ),
        )
        for i in range(samples + 1)
    )


def test_point_to_arc_distance_past_a_semicircle_keeps_the_centre_on_the_arcs_side():
    """For |bulge| > 1 the centre crosses the chord (``radius - sagitta`` goes
    negative). ``copysign`` threw that sign away, so a bulge-2 arc was tested
    as its own reflection: the apex read as far away and empty paper as on it."""
    from ezdxf.math import bulge_center

    from engineering.pid.graph import _point_segment_distance

    a, b = (0.0, 0.0), (10.0, 0.0)
    assert tuple(bulge_center(a, b, 2.0)) == pytest.approx((5.0, -3.75))
    assert tuple(bulge_center(a, b, -2.0)) == pytest.approx((5.0, 3.75))
    # centre (5,-3.75), r 6.25 -> apex (5,-10); the mirror image is (5,10)
    assert _point_segment_distance((5, -10), a, b, 2.0) == pytest.approx(0.0)
    assert _point_segment_distance((5, 10), a, b, 2.0) == pytest.approx(
        _arc_distance_sampled((5, 10), a, b, 2.0), abs=1e-6
    )
    assert _point_segment_distance((5, 10), a, b, 2.0) == pytest.approx(math.hypot(5, 10))
    assert _point_segment_distance((5, 10), a, b, -2.0) == pytest.approx(0.0)
    assert _point_segment_distance((5, -10), a, b, -2.0) == pytest.approx(
        _arc_distance_sampled((5, -10), a, b, -2.0), abs=1e-6
    )
    assert _point_segment_distance((5, -10), a, b, -2.0) == pytest.approx(math.hypot(5, 10))


async def test_a_line_ending_on_the_apex_of_an_arc_past_a_semicircle_is_a_junction(backend):
    msp = backend._doc.modelspace()
    pipe = msp.add_lwpolyline(
        [(-20, 0, 0), (0, 0, 2), (10, 0, 0), (30, 0, 0)],
        format="xyb",
        dxfattribs={"layer": "PROCESS-PIPING-MAIN"},
    ).dxf.handle
    on_arc = (
        await backend.entity_create_line(5, -10, 5, -30, layer="PROCESS-PIPING-SECONDARY")
    ).handle
    on_paper = (
        await backend.entity_create_line(5, 10, 5, 30, layer="PROCESS-PIPING-SECONDARY")
    ).handle
    graph = await build_graph(backend)
    edges = {e["id"]: e for e in graph["edges"]}
    assert edges[on_arc]["from"] and "junction" in edges[on_arc]["from"]
    assert edges[on_paper]["from"] is None
    assert [j["id"] for j in graph["junctions"]] == [edges[on_arc]["from"]["junction"]]
    junction = graph["junctions"][0]
    assert (junction["x"], junction["y"]) == pytest.approx((5.0, -10.0))
    assert set(junction["edges"]) == {pipe, on_arc}
    dangling = {(d["edge"], d["end"]) for d in graph["dangling"]}
    assert (on_paper, "from") in dangling and (on_arc, "from") not in dangling


async def test_a_mirrored_arc_is_tested_on_the_side_the_drawing_holds(backend):
    """The bulge is signed in the polyline's own frame, and ``entity_mirror``
    leaves that frame a reflection (extrusion -Z). Handing the stored sign
    out next to WCS ``points`` described the arc's mirror image: a line ending
    on the true apex dangled and one ending on empty paper made a junction,
    with ``length`` (frame-invariant) saying nothing about it."""
    msp = backend._doc.modelspace()
    pipe = msp.add_lwpolyline(
        [(-20, 0, 0), (0, 0, 1), (10, 0, 0), (30, 0, 0)],
        format="xyb",
        dxfattribs={"layer": "PROCESS-PIPING-MAIN"},
    )
    # Mirror across the X axis: the arc that bowed to -y now bows to +y, which
    # the engine's own bounding box confirms.
    pipe = (await backend.entity_mirror(pipe.dxf.handle, 0, 0, 1, 0, delete_original=True)).handle
    info = (await backend.entity_get(pipe)).properties
    assert info["bounding_box"]["max"][1] == pytest.approx(5.0)
    assert info["bounding_box"]["min"][1] == pytest.approx(0.0)
    assert info["bulges"] == [0.0, -1.0, 0.0, 0.0]
    on_arc = (
        await backend.entity_create_line(5, 5, 5, 30, layer="PROCESS-PIPING-SECONDARY")
    ).handle
    on_paper = (
        await backend.entity_create_line(5, -5, 5, -30, layer="PROCESS-PIPING-SECONDARY")
    ).handle
    graph = await build_graph(backend)
    edges = {e["id"]: e for e in graph["edges"]}
    assert edges[pipe]["length"] == pytest.approx(20 + 5 * math.pi + 20)
    assert edges[on_arc]["from"] and "junction" in edges[on_arc]["from"]
    assert edges[on_paper]["from"] is None
    assert [j["id"] for j in graph["junctions"]] == [edges[on_arc]["from"]["junction"]]
    junction = graph["junctions"][0]
    assert (junction["x"], junction["y"]) == pytest.approx((5.0, 5.0))
    assert set(junction["edges"]) == {pipe, on_arc}
    assert {(d["edge"], d["end"]) for d in graph["dangling"]} >= {(on_paper, "from")}
    assert (on_arc, "from") not in {(d["edge"], d["end"]) for d in graph["dangling"]}


def test_com_polyline_bulges_follow_points_into_wcs():
    """ActiveX ``GetBulge`` answers in the same OCS as ``Coordinates``. Once
    the points are reflected into WCS the arc's sense reverses with them, so
    the bulge sign flips — the same polyline as the test above, as AutoCAD
    would hand it over after MIRROR."""
    from types import SimpleNamespace

    from backends.com_backend import _entity_info
    from engineering.pid.graph import _point_segment_distance

    ent = SimpleNamespace(
        ObjectName="AcDbPolyline",
        Coordinates=(20.0, 0.0, 0.0, 0.0, -10.0, 0.0, -30.0, 0.0),
        Normal=(0.0, 0.0, -1.0),
        Elevation=0.0,
        Handle="2F",
        Layer="PROCESS-PIPING-MAIN",
        Color=256,
        Linetype="ByLayer",
        Visible=True,
        Closed=False,
        Length=40 + 5 * math.pi,
        GetBoundingBox=lambda: ((-20.0, 0.0, 0.0), (30.0, 5.0, 0.0)),
        GetBulge=lambda i: [0.0, 1.0, 0.0, 0.0][i],
    )
    props = _entity_info(ent).properties
    assert props["points"] == [[-20.0, 0.0], [0.0, 0.0], [10.0, 0.0], [30.0, 0.0]]
    assert props["bulges"] == [0.0, -1.0, 0.0, 0.0]
    a, b, bulge = props["points"][1], props["points"][2], props["bulges"][1]
    assert _point_segment_distance((5.0, 5.0), a, b, bulge) == pytest.approx(0.0)
    assert _point_segment_distance((5.0, -5.0), a, b, bulge) == pytest.approx(math.hypot(5, 5))


def test_com_polyline_bulges_are_read_only_when_length_says_an_arc_exists():
    """The COM engine pays one round trip per vertex for GetBulge, so it asks
    only when ActiveX ``Length`` exceeds the chord walk — an arc is always
    longer than its chord, so equality proves every bulge is zero."""
    from types import SimpleNamespace

    from backends.com_backend import _entity_info

    def fake(coords, length, bulges):
        calls = []

        def get_bulge(i):
            calls.append(i)
            return bulges[i]

        ent = SimpleNamespace(
            ObjectName="AcDbPolyline",
            Coordinates=coords,
            Normal=(0.0, 0.0, 1.0),
            Elevation=0.0,
            Handle="2F",
            Layer="0",
            Color=256,
            Linetype="ByLayer",
            Visible=True,
            Closed=False,
            Length=length,
            GetBoundingBox=lambda: ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
            GetBulge=get_bulge,
        )
        return ent, calls

    straight, calls = fake((0.0, 0.0, 10.0, 0.0, 10.0, 10.0), 20.0, [0.0, 0.0, 0.0])
    assert _entity_info(straight).properties["bulges"] == [0.0, 0.0, 0.0]
    assert calls == []
    curved, calls = fake((0.0, 0.0, 10.0, 0.0, 10.0, 10.0), 10 + 5 * math.pi, [0.0, 1.0, 0.0])
    assert _entity_info(curved).properties["bulges"] == [0.0, 1.0, 0.0]
    assert calls == [0, 1, 2]


def test_an_approximate_geometry_bbox_is_tightened_by_the_attribute_inclusive_box():
    """The COM engine marks a rotated INSERT's box ``approximate`` when a
    member could only be carried by its corners: the box then overshoots the
    body, and a line ending on the body's true edge would sit *inside* it and
    dangle. The drawn geometry lies inside ``bounding_box`` too, so every
    wall takes the tighter of the two; an exact box is used as it is."""
    from backends.base import EntityInfo
    from engineering.pid.graph import _bbox, _on_bbox_boundary

    def info(**props):
        return EntityInfo(
            handle="1",
            type="INSERT",
            layer="0",
            color=256,
            linetype="ByLayer",
            visible=True,
            properties=props,
        )

    exact = {"min": [95.0, 95.0], "max": [105.0, 105.0]}
    outer = {"min": [95.0, 95.0], "max": [105.0, 118.0]}  # the TAG above the body
    assert _bbox(info(geometry_bbox=exact, bounding_box=outer)) == (95.0, 95.0, 105.0, 105.0)
    loose = {"min": [92.93, 92.93], "max": [107.07, 107.07], "approximate": True}
    assert _bbox(info(geometry_bbox=loose, bounding_box=outer)) == (95.0, 95.0, 105.0, 107.07)
    assert _bbox(info(geometry_bbox=loose)) == (92.93, 92.93, 107.07, 107.07)
    assert _bbox(info(bounding_box=outer)) == (95.0, 95.0, 105.0, 118.0)
    assert _bbox(info()) is None
    # a line ending on the body's true left edge attaches through the tightened box
    assert _on_bbox_boundary(
        (95.0, 100.0), _bbox(info(geometry_bbox=loose, bounding_box=outer)), 0.5
    )
    assert not _on_bbox_boundary((95.0, 100.0), _bbox(info(geometry_bbox=loose)), 0.5)


# ── per-space resolution, mirrored inserts, tags above the box, per-axis scale ──


async def test_scope_all_resolves_each_line_in_its_own_space(backend):
    """The grids, box candidates and texts were pooled across layouts, so a
    line in SHEET-2 attached to the Model gate that shares its coordinates,
    and a line under a Model-only pump 'connected' across sheets."""
    hv1 = (await place_symbol(backend, "gate", 0, 0, tag="HV-1"))["handle"]
    await backend.layout_create("SHEET-2")
    await backend.layout_set_current("SHEET-2")
    hv2 = (await place_symbol(backend, "gate", 0, 0, tag="HV-2"))["handle"]
    to_valve = (await backend.entity_create_line(-40, 0, -4, 0, layer="PROCESS-PIPING-MAIN")).handle
    await backend.layout_set_current("Model")
    pump = (await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-1"))["handle"]
    await backend.layout_set_current("SHEET-2")
    under_pump = (
        await backend.entity_create_line(100, 150, 100, 106, layer="PROCESS-PIPING-MAIN")
    ).handle
    await backend.layout_set_current("Model")
    graph = await build_graph(backend, scope="all")
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = {e["id"]: e for e in graph["edges"]}
    assert nodes[hv1]["space"] == "Model" and nodes[hv2]["space"] == "SHEET-2"
    assert edges[to_valve]["space"] == "SHEET-2"
    assert edges[to_valve]["to"] == {"node": hv2, "port": "in"}
    assert nodes[hv2]["ports"]["in"]["edges"] == [to_valve]
    assert nodes[hv1]["ports"]["in"]["edges"] == []
    assert edges[under_pump]["to"] is None
    assert nodes[pump]["ports"]["discharge"]["edges"] == []
    dangling = {(d["edge"], d["end"]): d for d in graph["dangling"]}
    assert set(dangling) == {(to_valve, "from"), (under_pump, "from"), (under_pump, "to")}
    assert dangling[(under_pump, "to")]["nearest"] is None  # the pump is on another sheet


async def test_scope_all_keeps_junctions_and_tags_within_a_space(backend):
    """A Model line's vertex must not make a junction for a SHEET-2 line
    ending on the same coordinates, and a Model tag must not label a
    SHEET-2 symbol."""
    await backend.entity_create_line(0, 0, 50, 0, layer="PROCESS-PIPING-MAIN")
    await backend.entity_create_text("P-9", 100, 113, 2.5)
    await backend.layout_create("SHEET-2")
    await backend.layout_set_current("SHEET-2")
    branch = (await backend.entity_create_line(25, 0, 25, -30, layer="PROCESS-PIPING-MAIN")).handle
    blk = backend._doc.blocks.new(name="CENTRIFUGAL_PUMP")
    blk.add_circle((0, 0), 5)
    pump = (await backend.block_insert("CENTRIFUGAL_PUMP", 100, 100)).handle
    await backend.layout_set_current("Model")
    graph = await build_graph(backend, scope="all")
    edge = next(e for e in graph["edges"] if e["id"] == branch)
    assert edge["from"] is None and graph["junctions"] == []
    node = next(n for n in graph["nodes"] if n["id"] == pump)
    assert node["tag"] is None and node["space"] == "SHEET-2"


async def test_a_mirrored_insert_has_its_ports_on_the_mirror_image(backend):
    """MIRROR3D and foreign DXFs store a mirror as extrusion -Z with the
    stored insertion x unnegated. The reader took the catalogue ports on
    the unmirrored image at confidence 1.0."""
    from ezdxf.math import Vec3

    pump = (await place_symbol(backend, "centrifugal_pump", 50, 20, rotation=30.0, tag="P-1"))[
        "handle"
    ]
    ent = backend._doc.entitydb.get(pump)
    ent.dxf.extrusion = (0, 0, -1)
    ent.dxf.insert = (-50, 20, 0)  # the same WCS insertion point
    info = (await backend.entity_get(pump)).properties
    assert info["insertion"] == pytest.approx([50.0, 20.0])
    assert info["mirrored"] is True
    m = ent.matrix44()
    local = {"discharge": (0.0, 6.0), "suction": (-4.0, 0.0)}
    truth = {name: m.transform(Vec3(lx, ly, 0)) for name, (lx, ly) in local.items()}
    graph = await build_graph(backend)
    node = next(n for n in graph["nodes"] if n["id"] == pump)
    assert node["mirrored"] is True and node["confidence"] == 1.0
    ports = node["ports"]
    assert (ports["discharge"]["x"], ports["discharge"]["y"]) == pytest.approx(
        (53.0, 25.196), abs=1e-3
    )
    assert (ports["suction"]["x"], ports["suction"]["y"]) == pytest.approx((53.464, 18.0), abs=1e-3)
    for name, p in truth.items():
        assert (ports[name]["x"], ports[name]["y"]) == pytest.approx((p.x, p.y), abs=1e-9)
        assert ports[name]["inferred"] is False
    # 90° discharge turned to 120° reads as 60° in the mirror image
    assert ports["discharge"]["direction_deg"] == pytest.approx(60.0)
    assert ports["suction"]["direction_deg"] == pytest.approx((180.0 - 210.0) % 360.0)
    line = (
        await backend.entity_create_line(53.0, 40.0, 53.0, 25.196, layer="PROCESS-PIPING-MAIN")
    ).handle
    graph = await build_graph(backend)
    edge = next(e for e in graph["edges"] if e["id"] == line)
    assert edge["to"] == {"node": pump, "port": "discharge"}


async def test_an_unmirrored_insert_reports_mirrored_false(backend):
    pump = (await place_symbol(backend, "centrifugal_pump", 50, 20, tag="P-1"))["handle"]
    assert (await backend.entity_get(pump)).properties["mirrored"] is False
    graph = await build_graph(backend)
    assert graph["nodes"][0]["mirrored"] is False and graph["nodes"][0]["notes"] == []


async def test_equipment_tag_is_the_text_above_the_box_not_the_line_label_below(backend):
    """Spec §9.2 step 6: the tag is the nearest text *above* the box. A full
    circle around the top-centre let a line number lettered under the pump
    win over the tag above it."""
    blk = backend._doc.blocks.new(name="CENTRIFUGAL_PUMP")
    blk.add_circle((0, 0), 5)
    pump = (await backend.block_insert("CENTRIFUGAL_PUMP", 100, 100)).handle
    await backend.entity_create_text("P-101", 100, 113, 2.5)
    await backend.entity_create_text('4"-P-100-CS1', 100, 99, 2.5)
    graph = await build_graph(backend)
    node = next(n for n in graph["nodes"] if n["id"] == pump)
    assert node["tag"] == "P-101" and node["tag_source"] == "text"


async def test_valve_tag_is_the_text_above_the_box_not_the_note_below(backend):
    blk = backend._doc.blocks.new(name="GATE_VALVE")
    blk.add_lwpolyline([(-4, -2), (4, -2), (4, 2), (-4, 2)], close=True)
    valve = (await backend.block_insert("GATE_VALVE", 50, 50)).handle
    await backend.entity_create_text("HV-9", 50, 58, 2.5)
    await backend.entity_create_text("NC", 50, 47, 2.5)
    graph = await build_graph(backend)
    node = next(n for n in graph["nodes"] if n["id"] == valve)
    assert node["tag"] == "HV-9" and node["tag_source"] == "text"


async def test_a_line_number_above_the_box_is_never_an_equipment_tag(backend):
    blk = backend._doc.blocks.new(name="GATE_VALVE")
    blk.add_lwpolyline([(-4, -2), (4, -2), (4, 2), (-4, 2)], close=True)
    valve = (await backend.block_insert("GATE_VALVE", 50, 50)).handle
    await backend.entity_create_text('4"-P-100-CS1', 50, 55, 2.5)
    await backend.entity_create_text("HV-9", 50, 60, 2.5)
    graph = await build_graph(backend)
    node = next(n for n in graph["nodes"] if n["id"] == valve)
    assert node["tag"] == "HV-9"


async def test_a_text_flush_on_the_box_top_still_counts_as_above_it(backend):
    """Insertion y >= top - 0.5 * height: a label whose baseline sits a hair
    under the box top (lettered flush on the body) is still above it, while
    one clearly inside the body is not."""
    blk = backend._doc.blocks.new(name="GATE_VALVE")
    blk.add_lwpolyline([(-4, -2), (4, -2), (4, 2), (-4, 2)], close=True)
    valve = (await backend.block_insert("GATE_VALVE", 50, 50)).handle
    # top is 52, so the threshold is 50.75: HV-9 (1.56 from the top-centre)
    # counts as above it, NC (1.3 away, nearer) does not
    await backend.entity_create_text("HV-9", 51.0, 50.8, 2.5)
    await backend.entity_create_text("NC", 50.0, 50.7, 2.5)
    graph = await build_graph(backend)
    node = next(n for n in graph["nodes"] if n["id"] == valve)
    assert node["tag"] == "HV-9"


async def test_a_non_uniformly_scaled_symbol_is_measured_per_axis_not_inferred(backend):
    """The reader re-ran the transform with the X factor for both axes and
    marked the port `inferred` — a flag spec §9.1 reserves for foreign-block
    ports. Per-axis scale is exact for a point port."""
    pump = (await place_symbol(backend, "centrifugal_pump", 0, 0, tag="P-1"))["handle"]
    backend._doc.entitydb.get(pump).dxf.yscale = 2.0
    line = (await backend.entity_create_line(0, 40, 0, 12, layer="PROCESS-PIPING-MAIN")).handle
    graph = await build_graph(backend)
    node = next(n for n in graph["nodes"] if n["id"] == pump)
    discharge = node["ports"]["discharge"]
    assert (discharge["x"], discharge["y"]) == pytest.approx((0.0, 12.0))
    assert discharge["inferred"] is False and discharge["direction_deg"] == pytest.approx(90.0)
    assert node["confidence"] == 1.0 and node["notes"] == []
    assert node["scale"] == pytest.approx(1.0) and node["y_scale"] == pytest.approx(2.0)
    suction = node["ports"]["suction"]
    assert (suction["x"], suction["y"]) == pytest.approx((-4.0, 0.0))
    edge = next(e for e in graph["edges"] if e["id"] == line)
    assert edge["to"] == {"node": pump, "port": "discharge"}
    assert discharge["edges"] == [line]


async def test_a_stretched_bubble_keeps_the_smaller_radius_and_says_so(backend):
    """A stretched instrument bubble is an ellipse; no circle is exact, so the
    radial port keeps min(sx, sy)·r and the node's confidence drops to 0.6
    with a note — never `inferred`, which is for foreign-block ports."""
    fic = (
        await place_symbol(backend, "instrument", 0, 0, tag="FIC-1", type="dcs", location="primary")
    )["handle"]
    graph = await build_graph(backend)
    radius = graph["nodes"][0]["ports"]["signal"]["radius"]
    assert radius > 0
    backend._doc.entitydb.get(fic).dxf.xscale = 2.0
    graph = await build_graph(backend)
    node = graph["nodes"][0]
    port = node["ports"]["signal"]
    assert port["radius"] == pytest.approx(radius) and port["inferred"] is False
    assert node["confidence"] == 0.6 and node["notes"] == ["non-uniform scale"]
    assert node["source"] == "catalog"
    assert graph["stats"]["confidence_min"] == 0.6


def test_com_block_reference_reports_mirrored_from_its_normal():
    from types import SimpleNamespace

    from backends.com_backend import _entity_info

    def refuse(_name):
        raise RuntimeError("no block table in this fake")

    def blockref(**extra):
        return SimpleNamespace(
            ObjectName="AcDbBlockReference",
            Name="PID_X",
            InsertionPoint=(50.0, 20.0, 0.0),
            XScaleFactor=1.0,
            YScaleFactor=1.0,
            Rotation=0.0,
            Handle="B1",
            Layer="0",
            Color=256,
            Linetype="ByLayer",
            Visible=True,
            Document=SimpleNamespace(Blocks=SimpleNamespace(Item=refuse)),
            GetBoundingBox=lambda: ((45.0, 15.0, 0.0), (55.0, 25.0, 0.0)),
            **extra,
        )

    assert _entity_info(blockref(Normal=(0.0, 0.0, -1.0))).properties["mirrored"] is True
    assert _entity_info(blockref(Normal=(0.0, 0.0, 1.0))).properties["mirrored"] is False
    assert _entity_info(blockref()).properties["mirrored"] is False
