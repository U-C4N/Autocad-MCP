"""1.8-08 gate - save-anchored durable checkpoints and undo after a restart.

The store (``backends/checkpoints.py``) keeps the bytes a save just wrote,
gzip level 1, at ``<STATE_DIR>/docs/<lineage_id>/cp/cp-<seq>.dxf.gz``; S0 at
``drawing_new`` / ``drawing_open``; a rollback-mode batch's snapshot is moved
in instead of deleted; ``checkpoint_create(label)`` adds one by hand.
``drawing_undo`` steps back to the newest checkpoint below the current
revision - after a restart too - and a restore is itself undoable. An evicted
checkpoint is refused ``checkpoint_evicted``, never "not found".

Every test runs on its own ``ACADMCP_STATE_DIR`` (tests/conftest.py); the
subprocess test kills a real ``python server.py`` with ``Popen.kill()``.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import ezdxf
import pytest
from fastmcp import Client

import server
from backends.base import UnsupportedCapabilityError
from backends.checkpoints import (
    CheckpointEvictedError,
    CheckpointNotFoundError,
    CheckpointSettings,
    CheckpointStore,
    open_store,
)
from backends.ezdxf_backend import EzdxfBackend
from backends.operations import OperationJournal
from engineering.errors import ERROR_KINDS, classify

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[1]


def _summary(doc) -> list[tuple]:
    """What a checkpoint must give back: every model-space entity, by handle."""
    rows = []
    for e in doc.modelspace():
        if e.dxftype() == "LINE":
            geometry = (
                tuple(round(v, 6) for v in e.dxf.start),
                tuple(round(v, 6) for v in e.dxf.end),
            )
        elif e.dxftype() == "CIRCLE":
            geometry = (tuple(round(v, 6) for v in e.dxf.center), round(e.dxf.radius, 6))
        else:
            geometry = ()
        rows.append((e.dxf.handle, e.dxftype(), e.dxf.layer, geometry))
    return sorted(rows)


def _file_summary(path) -> list[tuple]:
    return _summary(ezdxf.readfile(str(path)))


def _store() -> CheckpointStore:
    store = open_store()
    assert store is not None, "conftest gives every test a state folder"
    return store


# ── the store ───────────────────────────────────────────────────────────────


async def test_the_store_keeps_gzip_level_1_bytes_under_the_lineage(outside_repo_dir):
    store = CheckpointStore(outside_repo_dir)
    data = b"  0\nSECTION\n" * 200
    row = store.add("a" * 32, kind="save", tool="drawing_save", rev=3, data=data)
    path = outside_repo_dir / "docs" / ("a" * 32) / "cp" / "cp-0.dxf.gz"
    assert path.is_file() and gzip.decompress(path.read_bytes()) == data
    assert (row["cp"], row["seq"], row["rev"], row["granularity"]) == ("cp-0", 0, 3, "save")
    assert store.read("a" * 32, store.resolve("a" * 32, "cp-0")) == data
    assert store.resolve("a" * 32, 0)["cp"] == "cp-0" and store.resolve("a" * 32, "0")["seq"] == 0
    listed = store.listing("a" * 32)
    assert listed == [
        {
            "cp": "cp-0",
            "seq": 0,
            "tool": "drawing_save",
            "rev": 3,
            "kb": row["kb"],
            "label": None,
            "evicted": False,
        }
    ]
    with pytest.raises(CheckpointNotFoundError) as excinfo:
        store.resolve("a" * 32, "cp-9")
    assert excinfo.value.kind == "checkpoint_not_found" and "cp-0" in str(excinfo.value)


async def test_keep_evicts_the_oldest_but_never_s0_and_eviction_is_refused_by_name(
    outside_repo_dir,
):
    store = CheckpointStore(outside_repo_dir, CheckpointSettings(keep=2))
    lineage = "b" * 32
    for rev in range(5):
        store.add(
            lineage, kind="save", tool="drawing_save", rev=rev, data=b"x" * 100 + bytes([rev])
        )
    rows = store.records(lineage)
    assert [r["evicted"] for r in rows] == [False, True, True, False, False], "S0 + newest 2"
    assert not (outside_repo_dir / "docs" / lineage / "cp" / "cp-1.dxf.gz").exists()
    with pytest.raises(CheckpointEvictedError) as excinfo:
        store.resolve(lineage, "cp-1")
    assert excinfo.value.kind == "checkpoint_evicted"
    assert classify(excinfo.value)["kind"] == "checkpoint_evicted"
    assert {"checkpoint_evicted", "checkpoint_not_found"} <= set(ERROR_KINDS)
    assert [r["evicted"] for r in store.listing(lineage, 10)] == [False, False, True, True, False]


async def test_the_budget_evicts_the_oldest_lineage_first_and_holds_budget_plus_one(
    outside_repo_dir,
):
    store = CheckpointStore(outside_repo_dir, CheckpointSettings(keep=50, budget_mb=1))
    blob = os.urandom(400 * 1024)  # incompressible: ~400 KB on disk each
    store.add("c" * 32, kind="save", tool="t", rev=1, data=blob)
    store.add("c" * 32, kind="save", tool="t", rev=2, data=os.urandom(400 * 1024))
    os.utime(outside_repo_dir / "docs" / ("c" * 32) / "index.json", (1, 1))  # the older lineage
    store.add("d" * 32, kind="save", tool="t", rev=1, data=os.urandom(400 * 1024))
    newest = store.add("d" * 32, kind="save", tool="t", rev=2, data=os.urandom(400 * 1024))
    old_rows = store.records("c" * 32)
    assert all(r["evicted"] for r in old_rows), "the least recently written lineage goes first"
    assert not any(r["evicted"] for r in store.records("d" * 32))
    assert store.total_bytes() <= 1024 * 1024 + newest["bytes"]


# ── the headless engine ─────────────────────────────────────────────────────


async def test_a_save_copies_the_bytes_it_wrote(backend, tmp_path):
    await backend.entity_create_line(0, 0, 10, 0)
    target = tmp_path / "part.dxf"
    saved = await backend.drawing_save_as(str(target))
    store = _store()
    lineage = backend._state.lineage_id
    row = store.resolve(lineage, saved["checkpoint"])
    assert row["tool"] == "drawing_save_as" and row["rev"] == backend._state.revision
    assert store.read(lineage, row) == target.read_bytes(), "no second serialisation"
    assert row["sha256"] == saved["sha256"]
    # S0 of the new drawing was written first (a blank row, no file).
    assert store.resolve(lineage, "cp-0")["blank"] is True
    again = await backend.drawing_save()
    assert again["checkpoint"] == saved["checkpoint"], "an unchanged save adds nothing"


async def test_restore_gives_back_the_checkpoint_and_is_undoable(backend, tmp_path):
    line = await backend.entity_create_line(0, 0, 10, 0)
    target = tmp_path / "part.dxf"
    first = await backend.drawing_save_as(str(target))
    expected = _file_summary(target)
    await backend.entity_move(line.handle, 5, 5)
    await backend.entity_create_circle(0, 0, 3)
    moved = _summary(backend._doc)
    assert moved != expected

    restored = await backend.checkpoint_restore(first["checkpoint"])
    assert restored["ok"] is True and restored["mode"] == "in_place"
    assert _summary(backend._doc) == expected, "entity summaries equal the checkpoint"
    assert restored["revision"] == backend._state.revision
    assert restored["checkpoint_before"]

    undone = await backend.drawing_undo()
    assert undone["ok"] is True and undone["checkpoint"] == restored["checkpoint_before"]
    assert _summary(backend._doc) == moved, "the restore itself was undone"


async def test_undo_walks_back_across_saves_and_reports_granularity(backend, tmp_path):
    target = tmp_path / "walk.dxf"
    a = await backend.entity_create_line(0, 0, 10, 0)
    await backend.drawing_save_as(str(target))
    one = _summary(backend._doc)
    await backend.entity_move(a.handle, 0, 5)
    await backend.drawing_save()
    two = _summary(backend._doc)
    await backend.entity_create_circle(0, 0, 1)  # unsaved

    first = await backend.drawing_undo()
    assert first["ok"] and first["granularity"] == "save" and first["source"] == "checkpoint"
    assert _summary(backend._doc) == two
    second = await backend.drawing_undo()
    assert second["ok"] and _summary(backend._doc) == one
    third = await backend.drawing_undo()
    assert third["ok"] and _summary(backend._doc) == [], "S0: the blank drawing"
    assert (await backend.drawing_undo())["ok"] is False

    redone = await backend.drawing_redo()
    assert redone["ok"] is True and _summary(backend._doc) == one


async def test_a_rollback_batch_boundary_and_a_label_are_checkpoints(backend):
    await backend.entity_create_line(0, 0, 10, 0)
    before = _summary(backend._doc)
    assert (await backend.transaction_begin())["ok"]
    await backend.entity_create_circle(0, 0, 4)
    committed = await backend.transaction_commit()
    assert committed["checkpoint"], "the begin's snapshot is kept, not deleted"
    undone = await backend.drawing_undo()
    assert undone["granularity"] == "batch" and _summary(backend._doc) == before

    labelled = await backend.checkpoint_create("round 2")
    assert labelled["label"] == "round 2" and labelled["granularity"] == "label"
    await backend.entity_create_line(0, 9, 9, 9)
    back = await backend.drawing_undo()
    assert back["granularity"] == "label" and _summary(backend._doc) == before
    found = await backend.checkpoint_list()
    assert found["checkpoints"][0]["seq"] > found["checkpoints"][-1]["seq"], "newest first"
    assert {"cp", "seq", "tool", "rev", "kb", "label", "evicted"} == set(found["checkpoints"][0])


async def test_undo_after_a_restart_steps_back_before_the_last_saved_edit(tmp_path):
    """Two engines on one state folder - the second is the restarted server."""
    target = tmp_path / "restart.dxf"
    first = EzdxfBackend()
    await first.connect()
    try:
        await first.drawing_new()
        line = await first.entity_create_line(0, 0, 10, 0)
        await first.drawing_save_as(str(target))
        before_move = _file_summary(target)
        await first.entity_move(line.handle, 7, 0)
        await first.drawing_save()
    finally:
        await first.disconnect()  # nothing carried over in memory

    second = EzdxfBackend()
    await second.connect()
    try:
        opened = await second.drawing_open(str(target))
        assert opened["lineage"]["continued"] is True
        undone = await second.drawing_undo()
        assert undone["ok"] is True and undone["granularity"] == "save"
        assert _summary(second._doc) == before_move
    finally:
        await second.disconnect()


async def test_without_a_state_folder_checkpoints_refuse_by_capability(backend, monkeypatch):
    monkeypatch.setenv("JOURNAL_DURABLE", "off")
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.checkpoint_create("x")
    assert excinfo.value.capability == "checkpoints"
    assert backend.capabilities().features["checkpoints"].supported is False


async def test_the_in_place_capability_is_declared_on_both_engines():
    from backends.com_backend import ComBackend

    headless = EzdxfBackend().capabilities().to_dict()["features"]
    live = ComBackend().capabilities().to_dict()["features"]
    assert headless["checkpoint_restore_in_place"] == {
        "supported": True,
        "mode": "snapshot",
        "reason": None,
    }
    assert live["checkpoint_restore_in_place"]["supported"] is False
    assert live["checkpoint_restore_in_place"]["reason"] == (
        "activex_cannot_replace_database_contents;use_new_document"
    )
    assert "checkpoints" in headless and "checkpoints" in live


async def test_new_document_mode_opens_the_checkpoint_as_its_own_document(backend, tmp_path):
    await backend.entity_create_line(0, 0, 10, 0)
    saved = await backend.drawing_save_as(str(tmp_path / "plate.dxf"))
    await backend.entity_create_circle(0, 0, 2)
    result = await backend.checkpoint_restore(saved["checkpoint"], mode="new_document")
    assert result["mode"] == "new_document" and Path(result["path"]).name == (
        f"plate-cp{result['restored']['seq']}.dxf"
    )
    assert _summary(backend._doc) == _file_summary(tmp_path / "plate.dxf")
    assert result["open_documents"] == 2, "the original stays open"


# ── the journal ─────────────────────────────────────────────────────────────


async def test_lost_since_save_counts_the_calls_past_the_saved_revision():
    journal = OperationJournal()
    doc = {"document_id": "d1", "revision": 4}
    saved, _ = journal.begin("drawing_save", {}, document=doc)
    journal.finish(saved, "committed", document=doc)
    move, _ = journal.begin("entity_move", {}, document=doc)
    journal.finish(move, "committed", document={"document_id": "d1", "revision": 5})
    read, _ = journal.begin("entity_list", {}, document={"document_id": "d1", "revision": 5})
    journal.finish(read, "committed", document={"document_id": "d1", "revision": 5})
    lost = journal.lost_since_save("d1", 4)
    assert lost["count"] == 1 and lost["last_operation"]["tool"] == "entity_move"
    assert journal.lost_since_save("d1", 5) == {"count": 0, "last_operation": None}


async def test_journal_rows_carry_the_checkpoint_before_and_after(tmp_path):
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        line = await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
        saved = await client.call_tool("drawing_save_as", {"path": str(tmp_path / "j.dxf")})
        await client.call_tool(
            "entity_move", {"handle": line.structured_content["handle"], "dx": 1, "dy": 0}
        )
        again = await client.call_tool("drawing_save", {})
        status = (
            await client.call_tool("system_status", {"detail": "operations"})
        ).structured_content
    first_save = [r for r in status["operations"] if r["tool"] == "drawing_save_as"][-1]
    # Before the first save the S0 is still pending (nothing written): no row
    # may name a checkpoint that does not exist yet.
    assert "checkpoint_before" not in first_save
    assert first_save["checkpoint_after"] == saved.structured_content["checkpoint"]
    second_save = [r for r in status["operations"] if r["tool"] == "drawing_save"][-1]
    assert second_save["checkpoint_before"] == saved.structured_content["checkpoint"]
    assert second_save["checkpoint_after"] == again.structured_content["checkpoint"]


async def test_without_a_store_no_journal_row_names_a_checkpoint(tmp_path, monkeypatch):
    """JOURNAL_DURABLE=off is the 1.7 mode: no checkpoints, so no checkpoint fields."""
    monkeypatch.setenv("JOURNAL_DURABLE", "off")
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {"bootstrap": False})
        await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
        status = (
            await client.call_tool("system_status", {"detail": "operations"})
        ).structured_content
        listed = await client.call_tool("checkpoint_list", {}, raise_on_error=False)
    row = [r for r in status["operations"] if r["tool"] == "entity_create_line"][-1]
    assert "checkpoint_before" not in row and "checkpoint_after" not in row, row
    assert listed.is_error and listed.structured_content["kind"] == "unsupported"


async def test_a_pending_s0_is_not_the_latest_checkpoint_until_written(backend, tmp_path):
    assert backend.latest_checkpoint() is None, "S0 of a never-saved drawing is not written"
    await backend.entity_create_line(0, 0, 1, 0)
    saved = await backend.drawing_save_as(str(tmp_path / "s0.dxf"))
    assert backend.latest_checkpoint() == saved["checkpoint"] != "cp-0"
    assert _store().resolve(backend._state.lineage_id, "cp-0")["kind"] == "new"


async def test_a_damaged_index_row_never_fails_the_save(backend, tmp_path):
    target = tmp_path / "damaged.dxf"
    await backend.entity_create_line(0, 0, 1, 0)
    await backend.drawing_save_as(str(target))
    index = _store()._index_path(backend._state.lineage_id)
    payload = json.loads(index.read_text(encoding="utf-8"))
    for row in payload["checkpoints"]:
        row["rev"] = None
    index.write_text(json.dumps(payload), encoding="utf-8")
    await backend.entity_create_circle(0, 0, 2)
    saved = await backend.drawing_save()
    assert saved["ok"] is True and "CIRCLE" in target.read_text(encoding="utf-8")


# ── drawing_new's S0 is the drawing the tool hands back ───────────────────


async def test_undo_to_s0_keeps_the_layers_and_linetypes_drawing_new_bootstrapped():
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        before = (await client.call_tool("layer_list", {})).structured_content
        await client.call_tool(
            "entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0, "layer": "GEOMETRY"}
        )
        undone = (await client.call_tool("drawing_undo", {})).structured_content
        after = (await client.call_tool("layer_list", {})).structured_content
        lines = (await client.call_tool("entity_list", {})).structured_content
        restored = (
            await client.call_tool("checkpoint_restore", {"checkpoint": "cp-0"})
        ).structured_content
        again = (await client.call_tool("layer_list", {})).structured_content

    def names(payload):
        rows = payload.get("layers", payload.get("result", payload))
        return sorted(r["name"] for r in rows)

    assert undone["ok"] is True and undone["checkpoint"] == "cp-0", undone
    assert {"GEOMETRY", "DIM", "CENTER", "HIDDEN", "TITLEBLOCK"} <= set(names(before))
    assert names(after) == names(before), "the undo stripped what drawing_new bootstrapped"
    assert names(again) == names(before)
    assert restored["ok"] is True
    assert not [e for e in _rows(lines) if e.get("type") == "LINE"], "the line is undone"


def _rows(payload) -> list[dict]:
    for key in ("entities", "result", "items"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise AssertionError(f"no entity rows in {sorted(payload)}")


async def test_anchoring_s0_does_not_shift_the_handles_drawn_after_it(monkeypatch):
    """A checkpoint is a read: ``Drawing.write``'s ``update_all`` would spend three handles."""

    async def first_handle() -> str:
        async with Client(server.mcp) as client:
            await client.call_tool("drawing_new", {})
            made = await client.call_tool(
                "entity_create_line", {"x1": 0, "y1": 0, "x2": 1, "y2": 0}
            )
        return made.structured_content["handle"]

    with_store = await first_handle()
    monkeypatch.setenv("JOURNAL_DURABLE", "off")  # no store: nothing is anchored
    assert with_store == await first_handle()


async def test_a_never_bootstrapped_drawing_keeps_its_blank_s0(backend):
    await backend.checkpoint_anchor_new()  # nothing ran after drawing_new: unchanged
    assert backend._state.s0 == {
        "kind": "new",
        "tool": "drawing_new",
        "rev": backend._state.revision,
        "blank": True,
    }


async def test_s0_re_anchored_after_the_bootstrap_restores_its_layers(backend):
    await backend.layer_create("GEOMETRY")  # what the tool's bootstrap does
    await backend.checkpoint_anchor_new()
    await backend.entity_create_line(0, 0, 1, 0, layer="GEOMETRY")
    undone = await backend.drawing_undo()
    assert undone["ok"] and undone["checkpoint"] == "cp-0"
    assert "GEOMETRY" in backend._doc.layers and _summary(backend._doc) == []
    assert (await backend.drawing_undo())["ok"] is False, "nothing below the bootstrap"


# ── the in-memory steps and the store, one history ────────────────────────


async def test_redo_retraces_an_undo_that_crossed_from_memory_into_the_store(backend, monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 2)
    await backend.drawing_new()  # the memory baseline under the new depth

    def count() -> int:
        return sum(1 for e in backend._doc.modelspace() if e.dxftype() == "LINE")

    for i in range(5):
        await backend.entity_create_line(0, i, 10, i)
    trail = []
    for _ in range(3):
        r = await backend.drawing_undo()
        trail.append((r["ok"], r.get("source", "memory"), count()))
    assert trail == [(True, "memory", 4), (True, "memory", 3), (True, "checkpoint", 0)]
    redo_trail = []
    for _ in range(4):
        r = await backend.drawing_redo()
        redo_trail.append((r["ok"], count()))
    assert redo_trail == [(True, 3), (True, 4), (True, 5), (False, 5)], (
        "the two lines undone in memory were never saved; redo must bring them back"
    )


async def test_an_edit_after_the_crossing_abandons_the_set_aside_branch(backend, monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 1)
    await backend.drawing_new()
    for i in range(3):
        await backend.entity_create_line(0, i, 10, i)
    await backend.drawing_undo()  # memory -> 2 lines
    crossed = await backend.drawing_undo()  # store -> S0
    assert crossed["source"] == "checkpoint" and backend._state.parked_redo
    parked = [p for branch in backend._state.parked_redo.values() for p in branch]
    await backend.entity_create_circle(0, 0, 1)  # a new branch
    assert (await backend.drawing_redo())["ok"] is False
    assert not backend._state.parked_redo and not any(p.exists() for p in parked)


async def test_an_evicted_checkpoint_is_refused_by_kind_through_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_KEEP", "1")
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {"bootstrap": False})
        path = str(tmp_path / "e.dxf")
        for y in range(3):
            await client.call_tool("entity_create_line", {"x1": 0, "y1": y, "x2": 5, "y2": y})
            await client.call_tool("drawing_save_as", {"path": path})
        listed = (await client.call_tool("checkpoint_list", {})).structured_content
        evicted = [r["cp"] for r in listed["checkpoints"] if r["evicted"]]
        assert evicted, listed
        refused = await client.call_tool(
            "checkpoint_restore", {"checkpoint": evicted[0]}, raise_on_error=False
        )
        missing = await client.call_tool(
            "checkpoint_restore", {"checkpoint": "no such label"}, raise_on_error=False
        )
    assert refused.is_error and refused.structured_content["kind"] == "checkpoint_evicted"
    assert missing.is_error and missing.structured_content["kind"] == "checkpoint_not_found"


# ── a real kill ─────────────────────────────────────────────────────────────


async def test_undo_after_popen_kill_and_the_unsaved_variant(outside_repo_dir, tmp_path):
    """v9 ``undo_across_restart`` as a test: no undo setting in the environment."""
    from benchmarks.correctness_suite import _server_env, _StdioServer

    def StdioServer(script, env, cwd, log):  # noqa: N802 - reads like the class
        return _StdioServer(script, env, cwd, log, timeout=120.0)

    env = {**_server_env(str(outside_repo_dir)), "PYTHONPATH": str(ROOT)}
    env.pop("EZDXF_UNDO_DEPTH", None)
    path = str(tmp_path / "killed.dxf")
    log = str(tmp_path / "server.log")

    first = StdioServer(str(ROOT / "server.py"), env, str(tmp_path), log)
    try:
        first.call("drawing_new")
        handle = first.call("entity_create_line", {"x1": 0, "y1": 0, "x2": 10, "y2": 0})["handle"]
        first.call("drawing_save_as", {"path": path})
        first.call("entity_move", {"handle": handle, "dx": 5, "dy": 5})
        first.call("drawing_save", {})
    finally:
        first.kill()

    second = StdioServer(str(ROOT / "server.py"), env, str(tmp_path), log)
    try:
        assert second.call("drawing_open", {"path": path})["is_error"] is False
        undone = second.call("drawing_undo")
        line = second.call("entity_get", {"handle": handle})
        # the unsaved variant: one move after the save, then a kill
        second.call("entity_move", {"handle": handle, "dx": 1, "dy": 0})
        second.call("drawing_save", {})
        saved_bytes = Path(path).read_bytes()
        second.call("entity_move", {"handle": handle, "dx": 3, "dy": 0})
    finally:
        second.kill()
    assert undone["ok"] is True and undone["granularity"] == "save", json.dumps(undone)
    props = line["properties"]
    assert props["start"][:2] == [0.0, 0.0] and props["end"][:2] == [10.0, 0.0]

    third = StdioServer(str(ROOT / "server.py"), env, str(tmp_path), log)
    try:
        reopened = third.call("drawing_open", {"path": path})
    finally:
        third.kill()
    assert Path(path).read_bytes() == saved_bytes, "the unsaved move never reached the file"
    assert reopened["lost_since_save"]["count"] == 1
    assert reopened["lost_since_save"]["last_operation"]["tool"] == "entity_move"
