"""1.8-09: the destructive list tools are all or nothing, and check every handle first.

``entity_delete_many`` deleted what it could and answered ``ok: True`` with an
``errors`` list (plan §2: partial delete, ``ok: True``). Now every handle is
checked in one fingerprint read before anything is deleted - a missing one
refuses the call with kind ``unknown_handle`` and nothing is deleted - and the
deletes run inside a transaction: a delete that fails mid-loop rolls the
others back and the answer is ``ok: false``. ``entity_batch_modify`` gets the
same pre-check. The live engine is covered on the fake seat of
tests/test_com_space.py: a delete that fails mid-loop sends ``_UNDO _B`` back
to the mark and the entities are back.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def _line(client, y: float = 0.0) -> str:
    result = await client.call_tool("entity_create_line", {"x1": 0, "y1": y, "x2": 10, "y2": y})
    return result.structured_content["handle"]


async def _exists(client, handle: str) -> bool:
    result = await client.call_tool("entity_get", {"handle": handle}, raise_on_error=False)
    return not result.is_error


async def _revision(client) -> int:
    status = (await client.call_tool("system_status", {})).structured_content or {}
    return int(status["revision"])


# ── entity_delete_many ──────────────────────────────────────────────────────


async def test_a_missing_handle_refuses_the_whole_delete_and_deletes_nothing(client):
    valid = await _line(client)
    revision = await _revision(client)
    result = await client.call_tool(
        "entity_delete_many", {"handles": [valid, "DEAD"]}, raise_on_error=False
    )
    payload = result.structured_content
    assert result.is_error is True
    assert payload["kind"] == "unknown_handle"
    assert payload["category"] == "input"
    assert payload["missing"] == [{"index": 1, "handle": "DEAD"}]
    assert payload["handles"] == ["DEAD"]
    assert payload["deleted"] == 0
    assert await _exists(client, valid), "the valid handle must not be deleted"
    assert await _revision(client) == revision, "nothing was written"


async def test_a_failure_mid_loop_rolls_every_delete_back(client, monkeypatch):
    from backends.ezdxf_backend import EzdxfBackend

    handles = [await _line(client, y) for y in (0.0, 5.0, 10.0)]
    real = EzdxfBackend.entity_delete

    async def flaky(self, handle):
        if handle == handles[1]:
            raise RuntimeError("the entity is on a locked layer")
        return await real(self, handle)

    monkeypatch.setattr(EzdxfBackend, "entity_delete", flaky)
    result = await client.call_tool("entity_delete_many", {"handles": handles})
    payload = result.structured_content
    assert payload["ok"] is False
    assert payload["deleted"] == 0
    assert payload["rolled_back"] is True
    assert payload["verified"] is True
    assert [row["handle"] for row in payload["errors"]] == [handles[1]]
    for handle in handles:
        assert await _exists(client, handle), f"{handle} must be back after the rollback"


async def test_a_clean_delete_commits_and_answers_ok(client):
    handles = [await _line(client, y) for y in (0.0, 5.0)]
    payload = (
        await client.call_tool("entity_delete_many", {"handles": handles})
    ).structured_content
    assert payload == {"ok": True, "deleted": 2, "errors": []}
    for handle in handles:
        assert not await _exists(client, handle)
    status = (await client.call_tool("system_status", {})).structured_content or {}
    assert status.get("transaction_depth", 0) == 0, "the delete's transaction is closed"


async def test_a_handle_listed_twice_is_deleted_once(client):
    handle = await _line(client)
    payload = (
        await client.call_tool("entity_delete_many", {"handles": [handle, handle.lower()]})
    ).structured_content
    assert payload["ok"] is True
    assert payload["deleted"] == 1
    assert payload["duplicates_ignored"] == 1


async def test_an_empty_list_deletes_nothing_and_opens_no_transaction(client, monkeypatch):
    from backends.ezdxf_backend import EzdxfBackend

    async def never(self):  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("no transaction for an empty list")

    monkeypatch.setattr(EzdxfBackend, "transaction_begin", never)
    payload = (await client.call_tool("entity_delete_many", {"handles": []})).structured_content
    assert payload == {"ok": True, "deleted": 0, "errors": []}


# ── entity_batch_modify ─────────────────────────────────────────────────────


async def test_batch_modify_refuses_a_missing_handle_before_moving_anything(client):
    valid = await _line(client)
    result = await client.call_tool(
        "entity_batch_modify",
        {
            "operations": [
                {"handle": valid, "action": "move", "dx": 5, "dy": 0},
                {"handle": "DEAD", "action": "delete"},
            ]
        },
        raise_on_error=False,
    )
    payload = result.structured_content
    assert result.is_error is True
    assert payload["kind"] == "unknown_handle"
    assert payload["missing"] == [{"index": 1, "handle": "DEAD"}]
    line = (await client.call_tool("entity_get", {"handle": valid})).structured_content
    assert line["properties"]["start"][:2] == [0.0, 0.0], "the move must not have run"


# ── the live engine, on a fake seat ─────────────────────────────────────────


class _Ctx:
    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, message):
        pass

    async def warning(self, message):
        pass

    async def report_progress(self, progress, total=None, message=None):
        pass


async def test_a_live_delete_failing_mid_loop_is_undone_back_to_the_mark(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 builds the point VARIANTs")
    from backends import com_backend as cb
    from backends.quarantine import DaemonExecutor
    from engineering.changes import precheck
    from tests.test_com_space import _Seat

    seat = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: seat)
    doc = seat.doc
    space = doc.ModelSpace
    entities = [space.add("AcDbLine") for _ in range(3)]
    handles = [entity.Handle for entity in entities]

    def _locked():
        raise RuntimeError("Object is on a locked layer")

    entities[1].Delete = _locked  # the second delete fails, after the first landed

    marks: list[list] = []

    def on_command(doc_, text):
        # What AutoCAD does with the two commands the transaction sends.
        if text.startswith("_.UNDO _M"):
            marks.append(list(space._items))
        elif text.startswith("_.UNDO _B") and marks:
            space._items[:] = marks.pop()

    doc.on_command = on_command

    async def fingerprints(backend, wanted):
        # The fake seat has no Export; read presence through the seat itself.
        found = {}
        for handle in wanted:
            try:
                doc.HandleToObject(handle)
                found[handle] = "present-" + handle
            except KeyError:
                found[handle] = precheck.MISSING
        return found

    monkeypatch.setattr(precheck, "entity_fingerprints", fingerprints)

    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    try:
        payload = await server.entity_delete_many(handles, ctx=_Ctx(backend))
    finally:
        backend._executor.shutdown(wait=False)
        backend._executor = None

    assert payload["ok"] is False
    assert payload["rolled_back"] is True
    assert payload["deleted"] == 0
    assert payload["verified"] is True
    assert [row["handle"] for row in payload["errors"]] == [handles[1]]
    assert [entity.Handle for entity in space._items] == handles, "every entity is back"
    assert any(text.startswith("_.UNDO _B") for text in doc.sent), "rolled back to the mark"
    assert "start" in doc.undo_marks and backend.transaction_depth() == 0


async def test_a_live_delete_refuses_a_missing_handle_before_any_mark(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 builds the point VARIANTs")
    from backends import com_backend as cb
    from backends.quarantine import DaemonExecutor
    from engineering.changes import precheck
    from engineering.errors import UnknownHandleError
    from tests.test_com_space import _Seat

    seat = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: seat)
    doc = seat.doc
    line = doc.ModelSpace.add("AcDbLine")

    async def fingerprints(backend, wanted):
        return {h: ("x" if h == line.Handle else precheck.MISSING) for h in wanted}

    monkeypatch.setattr(precheck, "entity_fingerprints", fingerprints)
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")
    backend._connected = True
    try:
        with pytest.raises(UnknownHandleError) as refused:
            await server.entity_delete_many([line.Handle, "DEAD"], ctx=_Ctx(backend))
    finally:
        backend._executor.shutdown(wait=False)
        backend._executor = None
    assert refused.value.missing == [{"index": 1, "handle": "DEAD"}]
    assert doc.sent == [] and doc.undo_marks == [], "no mark, no command: nothing ran"
    assert doc.ModelSpace.Count == 1


# ── the gates: the v9 task and the correctness checks ───────────────────────


async def _run_v9(task_id, tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        return await getattr(adapter, f"_task_{task_id}")()
    finally:
        await adapter.cleanup()


async def test_the_v9_atomic_batch_default_task_passes_headlessly(tmp_path):
    from benchmarks.tasks_v8 import task_by_id
    from benchmarks.tasks_v9 import HEADLESS_ONLY_V9

    assert task_by_id("atomic_batch_default", "v9").category == "durability"
    assert "atomic_batch_default" in HEADLESS_ONLY_V9
    passed, metrics, _artifacts = await _run_v9("atomic_batch_default", tmp_path)
    assert passed is True, metrics
    assert metrics["probe"]["kind"] == "unknown_handle"
    assert metrics["runtime_twin"]["atomicity"]["verified"] is True


async def test_the_v9_task_fails_loudly_without_the_pre_check(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_batch_handle_refs", lambda plans: [])
    passed, metrics, _artifacts = await _run_v9("atomic_batch_default", tmp_path)
    assert passed is False
    assert metrics["probe"]["summary_unchanged"] is True, "auto still rolled the move back"
    assert metrics["probe"]["kind"] != "unknown_handle"


@pytest.mark.parametrize("name", ["atomic_batch_default", "delete_many_all_or_nothing"])
async def test_the_correctness_checks_pass(name):
    from benchmarks.correctness_suite import CHECKS

    check, category = CHECKS[name]
    assert category == "Durability"
    assert await check() is True
