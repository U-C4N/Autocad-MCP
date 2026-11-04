"""1.8-12: set-based edits - ``entity_modify_where``, ``layer_rename``, ``layer_merge``.

§4.4 H4 of the rival's report: a layer clean-up took us 124 calls (the rival
11) because the verbs did not exist and ``entity_batch_modify`` wants a row per
entity. These tests hold the three verbs on the headless engine (the live one
is tests/test_set_edits_com.py, on a fake seat): what ``where`` matches, what
``set`` writes, the dry run that writes nothing, ``expect_count`` refusing
``stale_revision`` with nothing changed, the refusals by name, and the
reassignment reaching model space, paper space and block definitions.
"""

from __future__ import annotations

import ezdxf
import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends import set_edits as se
from discovery.aliases import aliases_for, tools_for_command
from engineering.changes.preview import StaleRevisionError, entity_fingerprints
from engineering.errors import InvalidArgsError, classify

pytestmark = pytest.mark.asyncio


# ── fixtures ────────────────────────────────────────────────────────────────


def _doc(backend):
    return backend._require_doc()


def _revision(backend) -> int:
    return int(backend.document_identity()["revision"])


async def _lines(backend, layer: str, n: int, *, y: float = 0.0, **props) -> list[str]:
    handles = []
    for i in range(n):
        info = await backend.entity_create_line(i * 10.0, y, i * 10.0 + 5.0, y + 5.0, layer=layer)
        handles.append(info.handle)
        if props:
            await backend.entity_set_properties(info.handle, **props)
    return handles


@pytest_asyncio.fixture
async def drawn(backend):
    """Three layers' worth of lines, two overridden, plus a paper-space line."""
    await backend.layer_create("WALL_old", color=1)
    await backend.layer_create("Walls", color=2)
    await backend.layer_create("NOTES", color=3)
    walls = await _lines(backend, "WALL_old", 3, color=5, lineweight=-3)
    more = await _lines(backend, "Walls", 2, y=50.0)
    notes = await _lines(backend, "NOTES", 2, y=100.0)
    doc = _doc(backend)
    paper = doc.layouts.get("Layout1")
    sheet_line = paper.add_line((0, 0), (10, 10), dxfattribs={"layer": "WALL_old"})
    return {"walls": walls, "more": more, "notes": notes, "paper": sheet_line.dxf.handle}


# ── entity_modify_where ─────────────────────────────────────────────────────


async def test_a_glob_list_restyles_every_match_in_model_space_and_says_what_changed(
    backend, drawn
):
    before = _revision(backend)
    result = await backend.entity_modify_where(
        {"layer": ["wall*"]}, {"layer": "A-WALL", "color": 256, "lineweight": "ByLayer"}
    )
    expected = sorted(drawn["walls"] + drawn["more"], key=lambda h: int(h, 16))
    assert result["ok"] is True
    assert result["matched"] == 5 and result["changed"] == 5 and result["unchanged"] == 0
    assert result["handles"] == expected, "sorted by handle, every changed one"
    assert result["truncated"] is False
    assert result["layers_created"] == ["A-WALL"], "a missing target layer is created, and said"
    assert result["set"] == {"layer": "A-WALL", "color": 256, "lineweight": -1}
    assert result["changes"] == {
        "added": [],
        "deleted": [],
        "counts": {"added": 0, "deleted": 0, "modified": 5},
        "complete": True,
    }
    doc = _doc(backend)
    for handle in expected:
        entity = doc.entitydb[handle]
        assert entity.dxf.layer == "A-WALL"
        assert entity.dxf.color == 256 and entity.dxf.lineweight == -1
    assert doc.entitydb[drawn["paper"]].dxf.layer == "WALL_old", "space='model' leaves the sheet"
    assert all(doc.entitydb[h].dxf.layer == "NOTES" for h in drawn["notes"])
    assert _revision(backend) > before


async def test_space_paper_and_all_reach_the_sheets(backend, drawn):
    sheet = _doc(backend).layouts.get("Layout1")
    overall = sheet.add_viewport((100, 100), (200, 150), (0, 0), 100)
    overall.dxf.id = 1
    overall.dxf.layer = "WALL_old"
    paper = await backend.entity_modify_where({"layer": "WALL_old"}, {"color": 1}, space="paper")
    assert paper["matched"] == 1 and paper["handles"] == [drawn["paper"]], "never the sheet's id 1"
    every = await backend.entity_modify_where({"layer": "WALL_old"}, {"color": 4}, space="all")
    assert every["matched"] == 4


async def test_resetting_a_value_to_itself_is_not_a_change(backend, drawn):
    await backend.entity_modify_where({"layer": "NOTES"}, {"color": 3})
    before = _revision(backend)
    again = await backend.entity_modify_where({"layer": "NOTES"}, {"color": 3})
    assert again["matched"] == 2 and again["changed"] == 0 and again["unchanged"] == 2
    assert again["changes"]["counts"]["modified"] == 0
    assert _revision(backend) == before, "nothing written, so the revision stays"


async def test_each_where_key_filters(backend, drawn):
    doc = _doc(backend)
    circle = doc.modelspace().add_circle((500, 500), 5, dxfattribs={"layer": "NOTES"})
    block = doc.blocks.new("BOLT")
    block.add_circle((0, 0), 1)
    insert = doc.modelspace().add_blockref("BOLT", (600, 600), dxfattribs={"layer": "NOTES"})
    dashed = doc.modelspace().add_line(
        (700, 700), (710, 700), dxfattribs={"layer": "NOTES", "linetype": "DASHED"}
    )
    query = backend.entity_select_where

    async def handles(where):
        return (await query(where, {"color": 7}))["handles"]

    assert await handles({"type": "circle"}) == [circle.dxf.handle]
    assert await handles({"block": "BO*"}) == [insert.dxf.handle]
    assert await handles({"linetype": "dashed"}) == [dashed.dxf.handle]
    assert set(await handles({"color": 5})) == set(drawn["walls"])
    assert set(await handles({"lineweight": "Default"})) == set(drawn["walls"])
    assert await handles({"region": [495, 495, 505, 505]}) == [circle.dxf.handle]
    assert await handles({"type": ["LINE"], "layer": "NOTES", "region": [-1, 99, 6, 106]}) == [
        drawn["notes"][0]
    ]
    assert await handles({"layer": "W?lls"}) == drawn["more"], "? is one character"
    assert await handles({"layer": "NO_SUCH"}) == []


async def test_the_dry_run_writes_nothing_and_counts_what_would_change(backend, drawn):
    before = _revision(backend)
    prints = await entity_fingerprints(backend, drawn["walls"] + drawn["more"])
    result = await backend.entity_select_where({"layer": ["WALL_old", "Walls"]}, {"color": 5})
    assert result["dry_run"] is True and result["count"] == 5
    assert result["would_change"] == 2, "the three walls are colour 5 already"
    assert _revision(backend) == before
    assert await entity_fingerprints(backend, drawn["walls"] + drawn["more"]) == prints


async def test_the_first_twenty_handles_and_truncated(backend):
    await backend.layer_create("MANY")
    made = await _lines(backend, "MANY", 25)
    result = await backend.entity_select_where({"layer": "MANY"}, {"color": 1})
    assert result["count"] == 25 and result["handles"] == made[:20] and result["truncated"]


async def test_expect_count_mismatch_is_stale_revision_and_nothing_changes(backend, drawn):
    before = _revision(backend)
    prints = await entity_fingerprints(backend, drawn["walls"])
    with pytest.raises(StaleRevisionError) as caught:
        await backend.entity_modify_where(
            {"layer": "WALL_old"}, {"layer": "A-WALL", "color": 256}, expect_count=2
        )
    assert classify(caught.value)["kind"] == "stale_revision"
    assert caught.value.changed == [{"count": "where", "expected": 2, "actual": 3}]
    assert await entity_fingerprints(backend, drawn["walls"]) == prints
    assert not _doc(backend).layers.has_entry("A-WALL"), "not even the layer was created"
    assert _revision(backend) == before
    ok = await backend.entity_modify_where({"layer": "WALL_old"}, {"color": 256}, expect_count=3)
    assert ok["changed"] == 3


async def test_a_locked_layer_is_skipped_and_reported(backend, drawn):
    await backend.layer_lock("Walls")
    result = await backend.entity_modify_where({"layer": "W*"}, {"color": 1})
    assert result["matched"] == 3
    assert result["skipped_locked"] == {"count": 2, "layers": ["Walls"]}
    doc = _doc(backend)
    assert all(doc.entitydb[h].dxf.get("color", 256) == 256 for h in drawn["more"])


async def test_setting_a_colour_drops_a_true_colour(backend, drawn):
    entity = _doc(backend).entitydb[drawn["notes"][0]]
    entity.rgb = (10, 20, 30)
    result = await backend.entity_modify_where({"layer": "NOTES"}, {"color": 256})
    assert result["changed"] == 1, "the true colour is an override even at ACI 256"
    assert result["unchanged"] == 1, "the other note already follows its layer"
    assert not entity.dxf.hasattr("true_color")


async def test_set_layer_is_spelt_as_the_table_stores_it(backend, drawn):
    result = await backend.entity_modify_where({"layer": "NOTES"}, {"layer": "walls"})
    assert result["layers_created"] == [] and result["set"]["layer"] == "Walls"
    assert all(_doc(backend).entitydb[h].dxf.layer == "Walls" for h in drawn["notes"])


@pytest.mark.parametrize(
    ("where", "set_", "space", "needle"),
    [
        ({}, {"color": 1}, "model", "where: name at least one"),
        ({"colour": 1}, {"color": 1}, "model", "unknown key(s) colour"),
        ({"color": 300}, {"color": 1}, "model", "where.color"),
        ({"region": [5, 0, 0, 5]}, {"color": 1}, "model", "where.region"),
        ({"layer": ""}, {"color": 1}, "model", "where.layer[0]"),
        ({"layer": "X"}, {}, "model", "set: name at least one"),
        ({"layer": "X"}, {"lineweight": "thick"}, "model", "set.lineweight"),
        ({"layer": "X"}, {"visible": "yes"}, "model", "set.visible"),
        ({"layer": "X"}, {"layer": "A|B"}, "model", "set.layer"),
        ({"layer": "X"}, {"color": 1}, "sheet", "space"),
    ],
)
async def test_malformed_arguments_are_invalid_args_by_path(backend, where, set_, space, needle):
    with pytest.raises(InvalidArgsError) as caught:
        await backend.entity_modify_where(where, set_, space)
    assert needle in str(caught.value)
    assert classify(caught.value)["kind"] == "invalid_args"


async def test_wcmatch_filters_escape_everything_but_star_and_question_mark():
    assert se.wcmatch_any(("A-WALL", "B.*", "C#?")) == "A`-WALL,B`.*,C`#?"
    assert se.glob_matches(("a-w*",), "A-WALL") and not se.glob_matches(("A.B",), "AxB")


# ── layer_rename ────────────────────────────────────────────────────────────


async def test_rename_reaches_model_paper_blocks_attributes_viewports_and_current(backend, drawn):
    doc = _doc(backend)
    block = doc.blocks.new("TAG")
    member = block.add_line((0, 0), (1, 1), dxfattribs={"layer": "WALL_old"})
    block.add_attdef("T", (0, 0), dxfattribs={"layer": "WALL_old"})
    ref = doc.modelspace().add_blockref("TAG", (0, 0))
    attrib = ref.add_attrib("T", "x", dxfattribs={"layer": "WALL_old"})
    viewport = doc.layouts.get("Layout1").add_viewport((50, 50), (40, 40), (0, 0), 50)
    viewport.frozen_layers = ["WALL_old", "NOTES"]
    await backend.layer_set_current("WALL_old")
    result = await backend.layer_rename("wall_old", "A-WALL")
    assert result["ok"] is True and result["old"] == "WALL_old" and result["new"] == "A-WALL"
    assert result["entities"] == 7, "3 lines + the sheet line + block line, ATTDEF, ATTRIB"
    assert result["blocks_touched"] == ["TAG"] and "every INSERT" in result["note"]
    assert result["current_layer"] == "A-WALL" and doc.header["$CLAYER"] == "A-WALL"
    assert result["changes"]["counts"]["layers_renamed"] == 1
    assert not doc.layers.has_entry("WALL_old") and doc.layers.has_entry("A-WALL")
    for entity in (member, attrib, doc.entitydb[drawn["paper"]], doc.entitydb[drawn["walls"][0]]):
        assert entity.dxf.layer == "A-WALL"
    assert viewport.frozen_layers == ["A-WALL", "NOTES"]


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("0", "ZERO", "cannot be renamed"),
        ("Defpoints", "DP", "cannot be renamed"),
        ("NOTES", "Walls", "already exists"),
        ("NOTES", "0", "reserved"),
        ("NOPE", "X", "no layer named 'NOPE'"),
        ("NOTES", "notes", "differ only in case"),
        ("XR|NOTES", "X", "xref-dependent"),
        ("NOTES", "BAD;NAME", "cannot contain"),
    ],
)
async def test_rename_refusals_change_nothing(backend, drawn, old, new, needle):
    if not _doc(backend).layers.has_entry("Defpoints"):
        _doc(backend).layers.add("Defpoints")
    before = _revision(backend)
    with pytest.raises(Exception) as caught:
        await backend.layer_rename(old, new)
    assert needle in str(caught.value)
    assert classify(caught.value)["kind"] in {"refused", "invalid_args"}
    assert _revision(backend) == before
    assert _doc(backend).layers.has_entry("NOTES")


# ── layer_merge ─────────────────────────────────────────────────────────────


async def test_merge_moves_everything_reports_the_blocks_and_deletes_the_sources(backend, drawn):
    doc = _doc(backend)
    block = doc.blocks.new("DOOR")
    block.add_line((0, 0), (1, 0), dxfattribs={"layer": "Walls"})
    block.add_attdef("N", (0, 0))
    ref = doc.modelspace().add_blockref("DOOR", (0, 0))
    attrib = ref.add_attrib("N", "1", dxfattribs={"layer": "WALL_old"})
    poly = doc.modelspace().add_polyline2d([(0, 0), (5, 5), (9, 0)], dxfattribs={"layer": "Walls"})
    viewport = doc.layouts.get("Layout1").add_viewport((50, 50), (40, 40), (0, 0), 50)
    viewport.frozen_layers = ["Walls", "NOTES"]
    await backend.layer_set_current("Walls")
    await backend.layer_create("A-WALL", lineweight=50)

    result = await backend.layer_merge(["WALL_old", "walls"], "A-WALL")
    assert result["ok"] is True
    assert result["sources"] == ["WALL_old", "Walls"] and result["target"] == "A-WALL"
    assert result["blocks_touched"] == ["DOOR"] and "every INSERT of DOOR" in result["note"]
    assert result["by_space"]["Layout1"] == 1 and result["by_space"]["(block definitions)"] == 1
    assert result["layers_deleted"] == ["WALL_old", "Walls"]
    assert result["current_layer"] == "A-WALL"
    assert result["changes"]["counts"]["layers_deleted"] == 2
    assert result["changes"]["counts"]["modified"] == result["reassigned"]
    assert attrib.dxf.layer == "A-WALL"
    assert all(v.dxf.layer == "A-WALL" for v in poly.vertices), "a VERTEX names its layer too"
    assert viewport.frozen_layers == ["NOTES"], "a deleted layer's viewport freeze goes with it"
    dangling = [
        e.dxf.handle
        for e in doc.entitydb.values()
        if e.is_alive
        and e.dxftype() != "LAYER"
        and e.dxf.hasattr("layer")
        and e.dxf.layer.lower() in {"wall_old", "walls"}
    ]
    assert dangling == [], "nothing names a deleted layer"


async def test_merge_without_deleting_keeps_the_empty_sources(backend, drawn):
    result = await backend.layer_merge(["Walls"], "NOTES", delete_sources=False)
    assert result["layers_deleted"] == [] and _doc(backend).layers.has_entry("Walls")
    assert result["reassigned"] == 2


@pytest.mark.parametrize(
    ("sources", "target", "needle"),
    [
        (["WALL_old"], "MISSING", "no layer named 'MISSING'"),
        (["GHOST"], "NOTES", "no layer named 'GHOST'"),
        (["0"], "NOTES", "cannot be merged away"),
        (["NOTES"], "notes", "cannot merge into itself"),
        (["X|Y"], "NOTES", "xref-dependent"),
        ([], "NOTES", "sources"),
        (["Walls", "WALLS"], "NOTES", "names a layer twice"),
    ],
)
async def test_merge_refusals_change_nothing(backend, drawn, sources, target, needle):
    before = _revision(backend)
    with pytest.raises(Exception) as caught:
        await backend.layer_merge(sources, target)
    assert needle in str(caught.value)
    assert classify(caught.value)["kind"] in {"refused", "invalid_args"}
    assert _revision(backend) == before
    assert _doc(backend).layers.has_entry("WALL_old")


# ── through the server ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {"bootstrap": False})
        yield connected


async def _call(client, tool, args):
    return (await client.call_tool(tool, args)).structured_content


async def test_the_three_tools_are_registered_with_their_cost_pack_and_aliases(full_catalog):
    tools = {t.name: t for t in await server.mcp.list_tools()}
    costs = {
        "entity_modify_where": "destructive",
        "layer_rename": "mutate",
        "layer_merge": "destructive",
    }
    for name, cost in costs.items():
        assert tools[name].meta["cad"]["cost"] == cost, name
        assert server._tool_pack(name) == "core", name
        assert aliases_for(name) is not None, name
    assert tools_for_command("LAYMRG") == ("layer_merge",)
    assert "layer_rename" in tools_for_command("RENAME")
    assert "entity_modify_where" in tools_for_command("QSELECT")
    assert "entity_modify_where" in tools_for_command("CHPROP")
    assert "entity_modify_where" in server.LEAN_TOOL_NAMES


async def test_the_tool_dry_run_carries_a_preview_and_the_region_fingerprint(client):
    for x in (0.0, 20.0, 400.0):
        await _call(client, "entity_create_line", {"x1": x, "y1": 0, "x2": x + 5, "y2": 5})
    plain = await _call(
        client,
        "entity_modify_where",
        {"where": {"type": "LINE"}, "set": {"color": 1}, "dry_run": True},
    )
    assert plain["count"] == 3 and plain["preview"]["preconditions"] == []
    assert isinstance(plain["preview"]["revision"], int)
    boxed = await _call(
        client,
        "entity_modify_where",
        {"where": {"region": [-1, -1, 30, 10]}, "set": {"color": 1}, "dry_run": True},
    )
    assert boxed["count"] == 2
    (row,) = boxed["preview"]["preconditions"]
    assert row["region"] == [-1.0, -1.0, 30.0, 10.0]
    assert boxed["region_fingerprint"] == row["fingerprint"] and len(row["fingerprint"]) == 64
    info = await _call(client, "drawing_info", {})
    assert info["revision"] == plain["preview"]["revision"], "a dry run is not a revision"


async def test_a_direct_call_with_a_stale_count_is_a_typed_refusal(client):
    made = await _call(client, "entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 5})
    result = await client.call_tool(
        "entity_modify_where",
        {"where": {"type": "LINE"}, "set": {"color": 1}, "expect_count": 4},
        raise_on_error=False,
    )
    assert result.is_error is True
    payload = result.structured_content
    assert payload["kind"] == "stale_revision" and payload["ok"] is False
    entity = await _call(client, "entity_get", {"handle": made["handle"]})
    assert entity["color"] == 256


async def test_the_three_are_cad_batch_steps_and_carry_changes(client):
    steps = [
        {"tool": "layer_create", "args": {"name": "old"}},
        {"tool": "layer_create", "args": {"name": "older"}},
        {"tool": "layer_create", "args": {"name": "A-WALL", "lineweight": 50}},
        {
            "tool": "entity_create_line",
            "args": {"x1": 0, "y1": 0, "x2": 5, "y2": 0, "layer": "old"},
        },
        {
            "tool": "entity_create_line",
            "args": {"x1": 0, "y1": 9, "x2": 5, "y2": 9, "layer": "older"},
        },
        {"tool": "layer_rename", "args": {"old": "old", "new": "A-WALL-OLD"}},
        {"tool": "layer_merge", "args": {"sources": ["A-WALL-OLD", "older"], "target": "A-WALL"}},
        {
            "tool": "entity_modify_where",
            "args": {"where": {"layer": "A-WALL"}, "set": {"color": 3}, "expect_count": 2},
        },
    ]
    batch = await _call(client, "cad_batch", {"steps": steps})
    assert batch["ok"] is True, batch
    rename, merge, modify = (row["result"] for row in batch["results"][-3:])
    assert rename["changes"]["counts"]["layers_renamed"] == 1
    assert merge["changes"]["counts"]["layers_deleted"] == 2 and merge["reassigned"] == 2
    assert modify["changes"]["counts"]["modified"] == 2 and modify["changes"]["complete"] is True


async def test_messy_layers_reach_the_target_state_in_two_calls(client, outside_repo_dir):
    """The call_budget scenario (1.8-12 / 1.8-13 writes the CI oracle): six
    misnamed layers, 300 entities with colour overrides and lineweight -3,
    cleaned in one cad_batch, then a critique that comes back empty."""
    doc = ezdxf.new("R2018")
    doc.header["$DIMDSEP"] = ord(".")  # ISO 129's decimal point: the fixture is about layers
    layers = {"wall": 50, "Walls_OLD": 50, "door 1": 25, "Door": 25, "win": 25, "furn": 18}
    for name, weight in layers.items():
        doc.layers.add(name, color=7, lineweight=weight)
    msp = doc.modelspace()
    for i in range(300):
        layer = list(layers)[i % 6]
        msp.add_line(
            (i, 0), (i, 10), dxfattribs={"layer": layer, "color": 1 + i % 6, "lineweight": -3}
        )
    path = outside_repo_dir / "messy_layers.dxf"
    doc.saveas(path)
    await _call(client, "drawing_open", {"path": str(path)})

    steps = [
        {"tool": "layer_rename", "args": {"old": "wall", "new": "A-WALL"}},
        {"tool": "layer_merge", "args": {"sources": ["Walls_OLD"], "target": "A-WALL"}},
        {"tool": "layer_rename", "args": {"old": "door 1", "new": "A-DOOR"}},
        {"tool": "layer_merge", "args": {"sources": ["Door"], "target": "A-DOOR"}},
        {"tool": "layer_rename", "args": {"old": "win", "new": "A-GLAZ"}},
        {"tool": "layer_rename", "args": {"old": "furn", "new": "A-FURN"}},
        {
            "tool": "entity_modify_where",
            "args": {
                "where": {"type": "*"},
                "set": {"color": 256, "lineweight": "ByLayer"},
                "space": "all",
                "expect_count": 300,
            },
        },
    ]
    batch = await _call(client, "cad_batch", {"steps": steps})  # call 1
    assert batch["ok"] is True, batch
    issues = await _call(  # call 2
        client, "drawing_critique", {"focus": ["iso128", "layer_color", "property_override"]}
    )
    assert issues in ([], {"result": []}), issues
    listed = await _call(client, "layer_list", {})
    rows = listed["result"] if isinstance(listed, dict) else listed
    names = {row["name"] for row in rows} - {"0", "Defpoints"}
    assert names == {"A-WALL", "A-DOOR", "A-GLAZ", "A-FURN"}, "no empty layer is left"
