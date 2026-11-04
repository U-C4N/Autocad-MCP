"""1.8-10 gate - value history: ``drawing_history``, the checkpoint diff, a thin status.

The rival's H1: three rounds on one part, then "what was the diameter in
round 2?" - answered from the durable journal in at most two calls, also after
the server restarted. Here: the row / args / changes shapes, labels from
``_meta.label`` / ``cad_batch(label=)`` / ``checkpoint_create``, the change
summary on every mutating journal row and in the ``cad_batch`` result, the
feature-aware ``_changes`` of ``engineering/understand/diff.py``,
``drawing_diff`` by checkpoint and the thin ``system_status.operations``.

Every test runs on its own ``ACADMCP_STATE_DIR`` (tests/conftest.py). A
"restart" is a second ``Client(server.mcp)`` session after the first closed:
the lifespan runs again, a new backend and a new journal object open the same
sqlite file - what a new process sees.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

import server
from engineering.understand.diff import diff_snapshots, feature_diff
from engineering.understand.snapshot import EntityRecord, Snapshot

PLATE = {
    "kind": "prismatic",
    "name": "plate",
    "material": "aluminium",
    "thickness": 8.0,
    "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
    "features": [{"kind": "hole", "id": "h1", "x": 40.0, "y": 25.0, "diameter": 10.0}],
}


def _plate(diameter: float) -> dict:
    spec = json.loads(json.dumps(PLATE))
    spec["features"][0]["diameter"] = diameter
    return spec


def _data(result) -> dict:
    return result.structured_content or {}


async def _call(client, tool, args=None, **kw):
    return _data(await client.call_tool(tool, args or {}, **kw))


# ── the _changes feature diff (unit) ────────────────────────────────────────


def _rec(handle, xdata=(), radius=5.0, kind="CIRCLE", dimension=None, points=((40.0, 25.0),)):
    return EntityRecord(
        handle=handle,
        type=kind,
        layer="GEOMETRY",
        space="Model",
        points=points,
        radius=radius,
        xdata=tuple(xdata),
        dimension=dimension,
    )


def _snap(*records) -> Snapshot:
    return Snapshot(
        source="t",
        insunits=4,
        extmin=None,
        extmax=None,
        layers={},
        layouts=("Model",),
        records=tuple(records),
    )


def _mech(diameters) -> tuple:
    payload = {
        "features": [{"id": f"h{i}", "kind": "hole", "d": d} for i, d in enumerate(diameters)]
    }
    return (("ACADMCP_MECH", json.dumps(payload)),)


def test_feature_diff_is_a_sorted_json_path_diff_on_the_tol_grid():
    old = {"features": [{"d": 10}, {"d": 11}, {"d": 12, "id": "h3"}], "name": "plate"}
    new = {"features": [{"d": 10}, {"d": 11.004}, {"d": 16, "id": "h3"}], "name": "plate"}
    assert feature_diff(old, new) == {"features[2].d": {"from": 12, "to": 16}}
    # a key on one side only; a float reported on the 0.01 grid
    assert feature_diff({"a": 1.23456}, {"b": 2}) == {
        "a": {"from": 1.23, "to": None},
        "b": {"from": None, "to": 2},
    }
    assert list(feature_diff({"z": 1, "a": 1}, {"z": 2, "a": 2})) == ["a", "z"]


def test_changes_report_the_feature_payload_values_of_a_matched_pair():
    old = _snap(_rec("A1", _mech([10, 11, 12]), radius=6.0))
    new = _snap(_rec("A1", _mech([10, 11, 16]), radius=8.0))
    (row,) = diff_snapshots(old, new)["changed"]
    assert row["changes"]["feature"] == {"features[2].d": {"from": 12, "to": 16}}
    assert row["changes"]["radius"] == {"from": 6.0, "to": 8.0}


def test_an_exact_geometric_match_whose_payload_moved_is_an_edit():
    old = _snap(_rec("A1", _mech([10])), _rec("A2", points=((0.0, 0.0),)))
    new = _snap(_rec("A1", _mech([12])), _rec("A2", points=((0.0, 0.0),)))
    result = diff_snapshots(old, new)
    (row,) = result["changed"]
    assert row["matched_by"] == "exact"
    assert row["changes"] == {"feature": {"features[0].d": {"from": 10, "to": 12}}}
    assert result["summary"]["unchanged"] == 1 and result["summary"]["changed"] == 1


def test_a_dimension_reports_its_measurement_from_to():
    def dim(handle, value, x):
        return _rec(
            handle,
            kind="DIMENSION",
            radius=None,
            points=((0.0, 0.0), (x, 0.0)),
            dimension=(0, (), value, "", "ISO-25", 1.0, 0.0),
        )

    still = _rec("C1", points=((100.0, 100.0),))
    old, new = _snap(dim("D1", 20.0, 20.0), still), _snap(dim("D1", 24.0, 24.0), still)
    (row,) = diff_snapshots(old, new)["changed"]
    assert row["matched_by"] == "handle"
    assert row["changes"]["measurement"] == {"from": 20.0, "to": 24.0}


# ── change summaries on journal rows and in cad_batch ───────────────────────


@pytest.mark.asyncio
async def test_every_mutating_row_carries_its_change_summary():
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        line = await _call(client, "entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
        await client.call_tool("entity_delete", {"handle": line["handle"]})
        rows = (await _call(client, "system_status", {"detail": "operations"}))["operations"]
    created = [r for r in rows if r["tool"] == "entity_create_line"][-1]
    deleted = [r for r in rows if r["tool"] == "entity_delete"][-1]
    assert created["changes"] == {
        "added": [line["handle"]],
        "deleted": [],
        "counts": {"Model": {"added": 1}},
        "complete": True,
    }
    assert deleted["changes"]["deleted"] == [line["handle"]]
    assert deleted["changes"]["counts"] == {"Model": {"deleted": 1}}
    reads = [r for r in rows if r["tool"] == "system_status"]
    assert all("changes" not in r for r in reads), "a read carries no summary"


@pytest.mark.asyncio
async def test_cad_batch_sums_its_steps_and_a_failed_batch_says_what_it_undid():
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        ok = await _call(
            client,
            "cad_batch",
            {
                "steps": [
                    {"tool": "entity_create_line", "args": {"x1": 0, "y1": 0, "x2": 5, "y2": 0}},
                    {"tool": "entity_create_circle", "args": {"cx": 0, "cy": 0, "radius": 2}},
                ]
            },
        )
        handles = [row["result"]["handle"] for row in ok["results"]]
        failed = await _call(
            client,
            "cad_batch",
            {
                "steps": [
                    {"tool": "entity_create_line", "args": {"x1": 0, "y1": 9, "x2": 5, "y2": 9}},
                    {"tool": "entity_edit_text", "args": {"handle": handles[0], "text": "X"}},
                ]
            },
        )
        rows = (await _call(client, "system_status", {"detail": "operations"}))["operations"]
    assert ok["changes"]["added"] == handles and ok["changes"]["complete"] is True
    assert ok["changes"]["counts"] == {"Model": {"added": 2}}
    assert failed["ok"] is False and failed["atomicity"]["rolled_back"] is True
    assert failed["changes"]["added"] == [] and failed["changes"]["rolled_back"] is True
    assert len(failed["partial_changes"]["added"]) == 1
    batch_rows = [r for r in rows if r["tool"] == "cad_batch"]
    assert batch_rows[0]["changes"]["added"] == handles


# ── drawing_history ─────────────────────────────────────────────────────────


async def _three_rounds(client, tmp_path, *, label_round_2: bool = True) -> str:
    """Rounds 1-3 of the H1 probe: a plate with a 10, 12, 16 hole, saved each round."""
    path = str(tmp_path / "part.dxf")
    await client.call_tool("drawing_new", {})
    for index, diameter in enumerate((10.0, 12.0, 16.0), start=1):
        meta = {"label": "round 2"} if (index == 2 and label_round_2) else None
        await client.call_tool(
            "mech_part_draw",
            {"spec": _plate(diameter), "at_x": 200.0 * index, "dimension": False},
            meta=meta,
        )
        await client.call_tool(
            "drawing_save_as" if index == 1 else "drawing_save",
            {"path": path} if index == 1 else {},
        )
    return path


def _diameter(row) -> float:
    return row["args"]["spec"]["features"][0]["diameter"]


@pytest.mark.asyncio
async def test_the_round_2_value_in_one_call_and_after_a_restart_in_two(tmp_path):
    async with Client(server.mcp) as client:
        path = await _three_rounds(client, tmp_path)
        found = await _call(client, "drawing_history", {"label": "round 2", "detail": "args"})
    (row,) = found["rows"]
    assert row["tool"] == "mech_part_draw" and row["label"] == "round 2"
    assert _diameter(row) == 12.0

    async with Client(server.mcp) as restarted:  # a new lifespan: a new journal object
        calls = 0
        await restarted.call_tool("drawing_open", {"path": path})
        calls += 1
        again = await _call(restarted, "drawing_history", {"label": "round 2", "detail": "args"})
        calls += 1
    assert calls <= 2 and _diameter(again["rows"][0]) == 12.0


@pytest.mark.asyncio
async def test_a_label_no_row_carries_is_refused_by_name_never_guessed(tmp_path):
    async with Client(server.mcp) as client:
        await _three_rounds(client, tmp_path)
        refused = await client.call_tool(
            "drawing_history", {"label": "round 9"}, raise_on_error=False
        )
    payload = _data(refused)
    assert refused.is_error and payload["kind"] == "invalid_args"
    assert "'round 9'" in payload["message"] and "'round 2'" in payload["message"]


@pytest.mark.asyncio
async def test_rows_are_newest_first_without_reads_and_ten_fit_400_tokens(tmp_path):
    from benchmarks.token_suite import FORMAT_JSON_DATA, RatioTokenCounter

    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        for index in range(12):
            await client.call_tool(
                "entity_create_line", {"x1": 0, "y1": index, "x2": 5, "y2": index}
            )
            await client.call_tool("entity_list", {"limit": 1})
        result = await client.call_tool("drawing_history", {"limit": 10})
    payload = _data(result)
    rows = payload["rows"]
    assert len(rows) == 10 and payload["more"] is True
    assert [r["seq"] for r in rows] == sorted((r["seq"] for r in rows), reverse=True)
    assert {r["tool"] for r in rows} == {"entity_create_line"}, "reads are left out"
    assert set(rows[0]) == {"seq", "op", "tool", "state", "rev", "t"}
    text = "".join(block.text for block in result.content)
    tokens = RatioTokenCounter().count(text, FORMAT_JSON_DATA)
    assert tokens <= 400, f"drawing_history(limit=10) is {tokens} tokens ({len(text)} chars)"


@pytest.mark.asyncio
async def test_query_and_handle_filters(tmp_path):
    async with Client(server.mcp) as client:
        await _three_rounds(client, tmp_path)
        line = await _call(client, "entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
        await client.call_tool("entity_move", {"handle": line["handle"], "dx": 1, "dy": 0})
        by_feature = await _call(client, "drawing_history", {"query": "HOLE"})
        by_handle = await _call(client, "drawing_history", {"handle": line["handle"].lower()})
        by_tool = await _call(client, "drawing_history", {"tool": "entity_list"})
    assert [r["tool"] for r in by_feature["rows"]] == ["mech_part_draw"] * 3
    assert [r["tool"] for r in by_handle["rows"]] == ["entity_move", "entity_create_line"]
    assert by_tool["rows"] == [], "naming a read-only tool lists it - none ran here"


@pytest.mark.asyncio
async def test_cad_batch_and_checkpoint_labels_reach_the_rows():
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        await client.call_tool(
            "cad_batch",
            {
                "steps": [
                    {"tool": "entity_create_line", "args": {"x1": 0, "y1": 0, "x2": 5, "y2": 0}}
                ],
                "label": "sketch",
            },
        )
        await client.call_tool("checkpoint_create", {"label": "before edits"})
        sketch = await _call(client, "drawing_history", {"label": "sketch"})
        marked = await _call(client, "drawing_history", {"label": "before edits"})
        bad = await client.call_tool(
            "entity_create_line",
            {"x1": 0, "y1": 0, "x2": 1, "y2": 1},
            meta={"label": "x" * 201},
            raise_on_error=False,
        )
    assert {r["tool"] for r in sketch["rows"]} == {"cad_batch", "entity_create_line"}
    assert [r["tool"] for r in marked["rows"]] == ["checkpoint_create"]
    assert bad.is_error and _data(bad)["kind"] == "invalid_args"


@pytest.mark.asyncio
async def test_detail_changes_diffs_the_checkpoints_around_an_unlabelled_round(tmp_path):
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        hole = await _call(
            client, "entity_create_circle", {"cx": 50, "cy": 50, "radius": 5, "layer": "GEOMETRY"}
        )
        await client.call_tool("drawing_save_as", {"path": str(tmp_path / "r.dxf")})
        await client.call_tool("entity_edit_geometry", {"handle": hole["handle"], "radius": 6})
        await client.call_tool("drawing_save", {})
        await client.call_tool("entity_edit_geometry", {"handle": hole["handle"], "radius": 8})
        found = await _call(
            client,
            "drawing_history",
            {"tool": "entity_edit_geometry", "detail": "changes", "limit": 2},
        )
    newest, older = found["rows"]
    assert older["diff"]["available"] is True and older["diff"]["span"]["exact"] is True
    (changed,) = older["diff"]["changed"]
    assert changed["changes"]["radius"] == {"from": 5.0, "to": 6.0}
    # the newest edit is after the last save: diffed against the current drawing
    assert newest["diff"]["span"]["to"] == "current"
    assert newest["diff"]["changed"][0]["changes"]["radius"] == {"from": 6.0, "to": 8.0}


@pytest.mark.asyncio
async def test_drawing_diff_by_checkpoint_label_and_id(tmp_path):
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        hole = await _call(client, "entity_create_circle", {"cx": 0, "cy": 0, "radius": 5})
        first = await _call(client, "checkpoint_create", {"label": "r1"})
        await client.call_tool("entity_edit_geometry", {"handle": hole["handle"], "radius": 7})
        second = await _call(client, "checkpoint_create", {})
        diff = await _call(
            client, "drawing_diff", {"old_checkpoint": "r1", "new_checkpoint": second["cp"]}
        )
        both = await client.call_tool(
            "drawing_diff",
            {"old_path": "x.dxf", "old_checkpoint": first["cp"]},
            raise_on_error=False,
        )
        missing = await client.call_tool(
            "drawing_diff", {"old_checkpoint": "no such label"}, raise_on_error=False
        )
    (row,) = diff["changed"]
    assert row["changes"]["radius"] == {"from": 5.0, "to": 7.0}
    assert diff["old"] == f"checkpoint:{first['cp']}"
    assert both.is_error and _data(both)["kind"] == "invalid_args"
    assert missing.is_error and _data(missing)["kind"] == "checkpoint_not_found"


# ── the thin system_status ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_operations_is_a_summary_and_detail_operations_the_old_list():
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
        summary = (await _call(client, "system_status"))["operations"]
        listed = (await _call(client, "system_status", {"detail": "operations"}))["operations"]
    assert set(summary) == {"count", "last", "unknown_commit"}
    assert summary["last"]["tool"] == "entity_create_line" and summary["unknown_commit"] == 0
    assert set(summary["last"]) == {"op", "tool", "state", "rev"}
    assert isinstance(listed, list) and listed[-1]["tool"] == "system_status"


@pytest.mark.asyncio
async def test_status_with_1000_journal_rows_is_at_most_300_bytes_larger():
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {"bootstrap": False})
        empty = await client.call_tool("system_status", {})
        journal = server._FALLBACK_OPERATIONS
        for lifespan_journal in _lifespan_journals():
            journal = lifespan_journal
        document = {"document_id": "d-1", "revision": 1}
        for index in range(1000):
            record, _ = journal.begin("entity_create_line", {"i": index}, document=document)
            journal.finish(record, "committed", document={**document, "revision": index + 2})
        full = await client.call_tool("system_status", {})
    small = len(json.dumps(empty.structured_content))
    large = len(json.dumps(full.structured_content))
    assert full.structured_content["operations"]["count"] >= 1000
    assert large - small <= 300, (small, large)


def _lifespan_journals():
    """The journal the running server holds (the lifespan context of its session)."""
    context = getattr(server.mcp, "_lifespan_result", None)
    if isinstance(context, dict) and context.get("operations") is not None:
        yield context["operations"]


# ── the gates: the v9 task and the correctness check (real server processes) ─


@pytest.mark.asyncio
async def test_the_v9_history_value_lookup_task_passes_headlessly(tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v8 import task_by_id
    from benchmarks.tasks_v9 import HEADLESS_ONLY_V9

    assert task_by_id("history_value_lookup", "v9").category == "durability"
    assert "history_value_lookup" in HEADLESS_ONLY_V9
    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        passed, metrics, _artifacts = await adapter._task_history_value_lookup()
    finally:
        await adapter.cleanup()
    assert passed is True, metrics
    assert metrics["calls"] <= 2 and metrics["labelled"]["value"] == 12.0
    assert metrics["missing_label"]["kind"] == "invalid_args"
    assert metrics["unlabelled"]["value"] == 12.0


@pytest.mark.asyncio
async def test_the_history_round2_value_check_passes():
    from benchmarks.correctness_suite import CHECKS

    check, category = CHECKS["history_round2_value"]
    assert category == "Durability"
    assert list(CHECKS)[-1] == "history_round2_value", "appended at the end"
    assert await check() is True


# ── the summaries' building blocks (both engines) ───────────────────────────


def test_merge_changes_nets_a_batch_and_stays_honest_about_completeness():
    from backends.change_sets import merge_changes

    first = {
        "added": ["A", "B"],
        "deleted": [],
        "counts": {"Model": {"added": 2}},
        "complete": True,
    }
    second = {
        "added": ["C"],
        "deleted": ["A", "Z"],
        "counts": {"Model": {"added": 1, "deleted": 2}},
        "complete": True,
    }
    merged = merge_changes([first, second])
    assert merged["added"] == ["B", "C"] and merged["deleted"] == ["Z"]
    assert merged["counts"] == {"Model": {"added": 3, "deleted": 2}}
    assert merged["complete"] is True
    assert merge_changes([first, None])["complete"] is False, "a step with no summary"
    assert merge_changes([]) is None


def test_the_live_engine_is_summarised_from_its_returned_handles():
    """A backend without change_mark (ComBackend): the handles its result
    returned, complete:false - ActiveX has no handle seed to diff."""
    from types import SimpleNamespace

    class Live:
        name = "com"

    mark = server._change_mark(Live(), "entity_create_line")
    assert mark == "result"
    result = SimpleNamespace(structured_content={"handle": "2A1", "type": "LINE"})
    summary = server._change_summary(Live(), "entity_create_line", mark, result)
    assert summary == {
        "added": ["2A1"],
        "deleted": [],
        "counts": {"returned": {"added": 1}},
        "complete": False,
        "basis": "returned_handles",
    }
    assert server._change_mark(Live(), "entity_list") is None, "a read is never marked"
    refused = SimpleNamespace(structured_content={"ok": False, "handle": "2A1"})
    assert server._change_summary(Live(), "entity_create_line", mark, refused) is None


@pytest.mark.asyncio
async def test_a_call_whose_final_state_a_kill_lost_is_bounded_by_the_next_call(tmp_path):
    """The journal flushes final states in batches: a kill inside that window
    leaves revision_after unrecorded (unknown_commit). The diff still reads,
    bounded by where the next call began, and says it inferred the end."""
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        hole = await _call(client, "entity_create_circle", {"cx": 0, "cy": 0, "radius": 5})
        await client.call_tool("drawing_save_as", {"path": str(tmp_path / "k.dxf")})
        edit = await client.call_tool(
            "entity_edit_geometry", {"handle": hole["handle"], "radius": 6}
        )
        await client.call_tool("drawing_save", {})
        journal = next(_lifespan_journals())
        journal.flush()
        operation_id = (
            edit.structured_content.get("operation_id")
            or [r for r in journal.recent(50) if r["tool"] == "entity_edit_geometry"][-1][
                "operation_id"
            ]
        )
        journal._connection.execute(
            "UPDATE operations SET revision_after = NULL, state = 'unknown_commit' "
            "WHERE operation_id = ?",
            (operation_id,),
        )
        journal._records.pop(operation_id, None)
        found = await _call(
            client, "drawing_history", {"tool": "entity_edit_geometry", "detail": "changes"}
        )
    (row,) = found["rows"]
    assert row["state"] == "unknown_commit"
    assert row["diff"]["span"]["after_inferred"] is True and row["diff"]["span"]["exact"] is False
    assert row["diff"]["changed"][0]["changes"]["radius"] == {"from": 5.0, "to": 6.0}


@pytest.mark.asyncio
async def test_live_rows_of_an_earlier_bind_are_found_by_the_lineage():
    """The live engine's document_id is per bind; the lineage is what a reopened
    file keeps. Rows of an earlier process's bind are listed; their checkpoint
    diff is refused by reason - the revisions counted from another start."""
    from backends.journal_store import open_journal
    from engineering.history import history

    journal = open_journal()
    first = {"document_id": "bind-1", "revision": 3, "lineage_id": "L1"}
    record, _ = journal.begin("entity_create_line", {"x1": 0}, document=first, label="round 2")
    journal.finish(record, "committed", document={**first, "revision": 4})
    journal.close()  # the process ends

    class Live:
        name = "com"

        def document_identity(self):
            return {"document_id": "bind-2", "revision": 0}

        def active_lineage(self):
            return "L1"

        async def checkpoint_list(self, limit=1):
            return {"lineage_id": "L1"}

    restarted = open_journal()
    try:
        found = await history(Live(), restarted, label="round 2", detail="changes")
    finally:
        restarted.close()
    (row,) = found["rows"]
    assert row["tool"] == "entity_create_line" and row["label"] == "round 2"
    assert row["diff"]["available"] is False and "earlier binding" in row["diff"]["reason"]
