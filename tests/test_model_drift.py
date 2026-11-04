"""Model drift: the read-only face of ``mech_part_revise`` (plan item 1.8-19).

``mech_part_inspect(drift=True)`` and the mechanical ``model_drift`` critique
focus compare every tagged entity of a drawn part with the primitive its stored
model draws, within the verification epsilon. A fresh drawing is current; a
moved, deleted or copied tagged entity is drift; a layer or colour change is
not; a part drawn before 1.8.0 is ``unstamped``; a view the engine no longer
reproduces is ``model_drift``.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

import server
from backends.ezdxf_backend import EzdxfBackend
from engineering.critique import run_critique
from engineering.critique_catalog import FOCUS_CATALOG
from engineering.errors import ModelDriftError
from engineering.mech import drift as D
from engineering.mech.draw import add_view, draw_part, read_part
from engineering.mech.part import build_part
from engineering.mech.revise import revise_part
from engineering.mech.xdata import APP_ID, to_values
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.qa_coverage import LEDGER_KEY
from engineering.understand.snapshot import take_snapshot

pytestmark = pytest.mark.asyncio

PLATE = {
    "kind": "prismatic",
    "name": "plate",
    "material": "steel",
    "thickness": 8.0,
    "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
    "features": [
        {"kind": "hole", "id": "h1", "x": 20.0, "y": 25.0, "diameter": 10.0},
        {"kind": "hole", "id": "h2", "x": 60.0, "y": 25.0, "diameter": 10.0},
    ],
}
SHAFT = {
    "kind": "revolved",
    "name": "shaft",
    "material": "steel",
    "segments": [{"length": 20.0, "d_outer": 30.0}, {"length": 40.0, "d_outer": 20.0}],
    "features": [{"kind": "chamfer", "id": "c1", "at": "start", "size": 1.0}],
}


async def _drawn(spec=PLATE, views=("front", "side")):
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    drawn = await draw_part(b, build_part(spec), at=(10.0, 20.0), rotation=30.0, views=views)
    return b, drawn["part_id"]


async def _focus(b):
    shared: dict = {}
    issues = await run_critique(b, ["model_drift"], shared=shared)
    return issues, shared[LEDGER_KEY]


async def _tagged(b, pid, kind):
    snap = await take_snapshot(b)
    return [t for t in D.tagged_entities(snap.records, pid) if t.type == kind]


async def test_the_focus_is_registered_in_the_mech_domain():
    assert "model_drift" in ALL_CRITIQUE_FOCUSES
    assert FOCUS_CATALOG["model_drift"][0] == "mech"


async def test_a_fresh_part_is_current_even_rotated_and_in_three_views():
    b, pid = await _drawn(SHAFT, views=("front", "side"))
    await add_view(b, pid, "section", plane={"p1": [-5.0, 0.0], "p2": [70.0, 0.0], "label": "B"})
    report = await D.inspect_drift(b, pid)
    assert report.model_is_current and report.stamped and report.engine_drift == []
    assert report.counts()["ok"] == len(report.expected) > 0
    issues, ledger = await _focus(b)
    assert issues == []
    assert ledger.count("model_drift") == 1


async def test_a_drawing_without_a_part_is_domain_absent():
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    await b.entity_create_line(0, 0, 10, 0)
    issues, ledger = await _focus(b)
    assert issues == []
    rows = ledger.not_checked_rows()
    assert [(r.focus, r.reason) for r in rows] == [("model_drift", "domain_absent")]


async def test_moved_deleted_and_copied_tagged_entities_are_drift():
    b, pid = await _drawn()
    lines = await _tagged(b, pid, "LINE")
    circles = await _tagged(b, pid, "CIRCLE")
    await b.entity_move(lines[0].handle, 0.5, 0.0)
    await b.entity_delete(lines[1].handle)
    copy = await b.entity_copy(circles[0].handle, 100.0, 0.0)
    report = await D.inspect_drift(b, pid)
    by_status = {}
    for row in report.drifted:
        by_status.setdefault(row["status"], []).append(row)
    assert [r["handle"] for r in by_status["moved"]] == [lines[0].handle]
    assert by_status["moved"][0]["expected"] != by_status["moved"][0]["measured"]
    assert [r["handle"] for r in by_status["missing"]] == [None]
    assert by_status["missing"][0]["view"] == lines[1].view
    copied = copy.handle if hasattr(copy, "handle") else copy["handle"]
    assert [r["handle"] for r in by_status["duplicate"]] == [copied]
    assert not report.model_is_current

    issues, _ledger = await _focus(b)
    (issue,) = issues
    assert issue.severity == "warning" and issue.detail["code"] == "hand_edited_tagged_entity"
    assert issue.detail["repairable"] is False
    assert {lines[0].handle, copied} <= set(issue.handles)


async def test_a_layer_and_colour_edit_is_not_drift():
    b, pid = await _drawn()
    (circle, *_rest) = await _tagged(b, pid, "CIRCLE")
    await b.layer_create("MINE")
    await b.entity_set_properties(circle.handle, layer="MINE", color=3)
    report = await D.inspect_drift(b, pid)
    assert report.model_is_current
    assert (await _focus(b))[0] == []


async def test_a_part_drawn_before_1_8_is_unstamped_info():
    b, pid = await _drawn()
    payload = await read_part(b, pid)
    legacy = {k: v for k, v in payload.items() if k not in ("part_id", "gen")}
    await b.entity_set_xdata(pid, APP_ID, to_values(legacy))
    report = await D.inspect_drift(b, pid)
    assert report.stamped is False and report.model_is_current is False
    (issue,) = (await _focus(b))[0]
    assert issue.severity == "info" and issue.detail["code"] == "unstamped"


async def test_an_engine_that_draws_the_model_differently_is_model_drift():
    b, pid = await _drawn()
    payload = await read_part(b, pid)
    tampered = {k: v for k, v in payload.items() if k != "part_id"}
    tampered["views"] = [
        dict(r, sha="0" * 64) if n == 1 else r for n, r in enumerate(tampered["views"])
    ]
    await b.entity_set_xdata(pid, APP_ID, to_values(tampered))
    report = await D.inspect_drift(b, pid)
    assert report.engine_drift == ["side"]
    (issue,) = (await _focus(b))[0]
    assert issue.detail["code"] == "model_drift" and issue.detail["views"] == ["side"]
    with pytest.raises(ModelDriftError) as caught:
        await revise_part(b, pid, {"features.h1.diameter": 8.0})
    assert caught.value.fields()["reason"] == "engine_drift"


async def test_the_whole_critique_stays_clean_on_a_fresh_mechanical_sheet():
    b, _pid = await _drawn()
    issues = await run_critique(b, None)
    assert [i for i in issues if i.focus == "model_drift"] == []


async def test_inspect_drift_through_the_tool(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        drawn = (await client.call_tool("mech_part_draw", {"spec": PLATE})).structured_content
        pid = drawn["part_id"]
        clean = (
            await client.call_tool("mech_part_inspect", {"part_id": pid, "drift": True})
        ).structured_content
        assert clean["model_is_current"] is True and clean["drift"] == []
        circles = (
            await client.call_tool("entity_list", {"type_filter": "CIRCLE"})
        ).structured_content
        rows = circles.get("result", circles) if isinstance(circles, dict) else circles
        await client.call_tool("entity_edit_geometry", {"handle": rows[0]["handle"], "radius": 6.0})
        drifted = (
            await client.call_tool("mech_part_inspect", {"part_id": pid, "drift": True})
        ).structured_content
    assert drifted["model_is_current"] is False
    (row,) = drifted["drift"]
    assert set(row) >= {"feature", "view", "expected", "measured", "status"}
    assert row["status"] == "moved" and row["measured"]["radius"] == 6.0
    assert row["expected"]["radius"] == 5.0
