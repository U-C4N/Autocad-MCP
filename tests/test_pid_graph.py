"""pid_graph reads the drawing back: our symbols exactly, foreign ones with confidence."""

from __future__ import annotations

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
