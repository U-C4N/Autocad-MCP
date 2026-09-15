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
