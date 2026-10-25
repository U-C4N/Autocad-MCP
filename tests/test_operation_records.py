"""B12 through the server: every call leaves a record; client ids replay or refuse.

``AuditMiddleware`` mints the id into ``CURRENT_OPERATION`` (re-entry through
the search proxy reuses it), ``cad_batch`` mints one per step, and a client's
own id - ``_meta.operation_id`` on any request, ``cad_batch(operation_id=...)``
- replays the recorded result (``replayed: true``), is refused with
``operation_conflict`` for another request and with ``requires_reconcile``
after an ``unknown_commit``. Everything is read back through
``system_status``'s ``operations``.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import McpError, ToolError

import server
from backends import ezdxf_backend
from backends.document_guard import ActiveDocumentChangedError
from backends.operations import CURRENT_OPERATION

_LINE = {"x1": 0, "y1": 0, "x2": 10, "y2": 0}


@pytest_asyncio.fixture
async def client():
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


async def _status(client) -> dict:
    return (await client.call_tool("system_status", {})).structured_content


async def _operations(client) -> list[dict]:
    return (await _status(client))["operations"]


async def _entities(client) -> int:
    info = (await client.call_tool("drawing_info", {})).structured_content
    return int(info["entity_count"])


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_every_call_leaves_a_committed_record_with_its_revisions(client):
    before = await _status(client)
    await client.call_tool("entity_create_line", _LINE)
    rows = [r for r in await _operations(client) if r["tool"] == "entity_create_line"]
    assert len(rows) == 1
    (row,) = rows
    assert row["state"] == "committed" and row["client_supplied"] is False
    assert len(row["operation_id"]) == 32
    assert row["document_id"] == before["document_id"]
    assert (row["revision_before"], row["revision_after"]) == (
        before["revision"],
        before["revision"] + 1,
    )
    assert row["error_kind"] is None and row["ended"] >= row["started"]


@pytest.mark.asyncio
async def test_a_client_id_replays_instead_of_drawing_twice(client):
    first = await client.call_tool("entity_create_line", _LINE, meta={"operation_id": "line-op-1"})
    second = await client.call_tool("entity_create_line", _LINE, meta={"operation_id": "line-op-1"})
    assert second.structured_content["replayed"] is True
    assert second.structured_content["operation_id"] == "line-op-1"
    assert second.structured_content["handle"] == first.structured_content["handle"]
    assert await _entities(client) == 1
    (row,) = [r for r in await _operations(client) if r["operation_id"] == "line-op-1"]
    assert row["client_supplied"] is True and row["state"] == "committed"


@pytest.mark.asyncio
async def test_the_same_client_id_for_another_request_is_refused(client):
    await client.call_tool("entity_create_line", _LINE, meta={"operation_id": "line-op-2"})
    other = await client.call_tool(
        "entity_create_line",
        {**_LINE, "x2": 20},
        meta={"operation_id": "line-op-2"},
        raise_on_error=False,
    )
    assert other.is_error is True
    payload = other.structured_content
    assert payload["kind"] == "operation_conflict" and payload["operation_id"] == "line-op-2"
    assert payload["reason"] == "different_request" and payload["tool"] == "entity_create_line"
    assert await _entities(client) == 1


@pytest.mark.asyncio
async def test_a_malformed_client_id_is_refused_before_anything_runs(client):
    result = await client.call_tool(
        "entity_create_line", _LINE, meta={"operation_id": "op\n1"}, raise_on_error=False
    )
    assert result.is_error is True
    assert result.structured_content["kind"] == "invalid_args"
    assert "printable" in result.structured_content["error"]
    assert await _entities(client) == 0


@pytest.mark.asyncio
async def test_a_batch_with_a_client_id_replays_and_records_its_steps(client):
    steps = [
        {"tool": "entity_create_line", "args": _LINE},
        {"tool": "entity_create_line", "args": {**_LINE, "y1": 5, "y2": 5}},
    ]
    payload = {"steps": steps, "operation_id": "batch-op-1"}
    first = (await client.call_tool("cad_batch", payload)).structured_content
    assert first["ok"] is True and first["operation_id"] == "batch-op-1"
    second = (await client.call_tool("cad_batch", payload)).structured_content
    assert second["replayed"] is True and second["operation_id"] == "batch-op-1"
    assert await _entities(client) == 2, "the replay drew nothing"
    operations = await _operations(client)
    (batch,) = [r for r in operations if r["operation_id"] == "batch-op-1"]
    assert (batch["tool"], batch["state"], batch["client_supplied"]) == (
        "cad_batch",
        "committed",
        True,
    )
    step_rows = [r for r in operations if r["tool"] == "entity_create_line"]
    assert len(step_rows) == 2
    assert all(r["state"] == "committed" and r["operation_id"] != "batch-op-1" for r in step_rows)
    assert step_rows[1]["revision_before"] == step_rows[0]["revision_after"]


@pytest.mark.asyncio
async def test_a_failed_step_names_its_operation_and_the_batch_is_partial(client):
    result = (
        await client.call_tool(
            "cad_batch",
            {
                "steps": [
                    {"tool": "entity_get", "args": {"handle": "NOSUCH"}},
                    {"tool": "entity_create_line", "args": _LINE},
                ],
                "on_error": "continue",
            },
        )
    ).structured_content
    error = result["results"][0]["error"]
    assert error["kind"] == "failed"
    operations = await _operations(client)
    (step,) = [r for r in operations if r["operation_id"] == error["operation_id"]]
    assert (step["tool"], step["state"], step["error_kind"]) == ("entity_get", "failed", "failed")
    (batch,) = [r for r in operations if r["tool"] == "cad_batch"]
    assert batch["operation_id"] == result["operation_id"]
    assert (batch["state"], batch["error_kind"]) == ("partial", "failed")


@pytest.mark.asyncio
async def test_a_cancelled_write_is_unknown_commit_and_its_retry_must_reconcile(
    client, monkeypatch
):
    entered, release = threading.Event(), threading.Event()

    async def stuck_circle(self, *args, **kwargs):
        def _sync():
            entered.set()
            assert release.wait(30), "the test never released the worker"
            return "late"

        return await self._async(_sync)

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "entity_create_circle", stuck_circle)
    args = {"cx": 0, "cy": 0, "radius": 5}
    request_id = client.session._request_id
    call = asyncio.ensure_future(
        client.call_tool("entity_create_circle", args, meta={"operation_id": "circle-op-1"})
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        await client.cancel(request_id)
        with pytest.raises(McpError, match="cancelled"):
            await call

        async def recorded() -> bool:
            return any(
                r["operation_id"] == "circle-op-1" and r["state"] == "unknown_commit"
                for r in await _operations(client)
            )

        await _until(recorded)
        (row,) = [r for r in await _operations(client) if r["operation_id"] == "circle-op-1"]
        assert row["error_kind"] == "cancelled"
        retry = await client.call_tool(
            "entity_create_circle",
            args,
            meta={"operation_id": "circle-op-1"},
            raise_on_error=False,
        )
        assert retry.is_error is True
        assert retry.structured_content["kind"] == "requires_reconcile"
        assert retry.structured_content["operation_id"] == "circle-op-1"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_refusal_names_the_operation_it_ended(client, tmp_path):
    result = await client.call_tool(
        "drawing_save_as", {"path": str(tmp_path / "part.dwg")}, raise_on_error=False
    )
    payload = result.structured_content
    assert payload["kind"] == "unsupported"
    (row,) = [r for r in await _operations(client) if r["operation_id"] == payload["operation_id"]]
    assert (row["tool"], row["state"], row["error_kind"]) == (
        "drawing_save_as",
        "refused",
        "unsupported",
    )


@pytest.mark.asyncio
async def test_the_search_proxy_reuses_the_operation_it_runs_under(monkeypatch):
    seen: list[str | None] = []
    original = ezdxf_backend.EzdxfBackend.entity_create_line

    async def spy(self, *args, **kwargs):
        seen.append(CURRENT_OPERATION.get())
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "entity_create_line", spy)
    server._apply_discovery_mode("search")
    try:
        async with Client(server.mcp) as client:
            await client.call_tool("call_tool", {"name": "drawing_new", "arguments": {}})
            await client.call_tool("call_tool", {"name": "entity_create_line", "arguments": _LINE})
            status = (
                await client.call_tool("call_tool", {"name": "system_status", "arguments": {}})
            ).structured_content
    finally:
        server._apply_discovery_mode("off")
    (operation_id,) = seen
    rows = [r for r in status["operations"] if r["operation_id"] == operation_id]
    assert len(rows) == 1 and (rows[0]["tool"], rows[0]["state"]) == ("call_tool", "committed")
    assert not any(r["tool"] == "entity_create_line" for r in status["operations"]), (
        "one request, one record"
    )


def test_the_batch_taxonomy_declares_every_kind_1_7_emits():
    assert server.BATCH_ERROR_KINDS == (
        "denied",
        "malformed_step",
        "unknown_tool",
        "invalid_args",
        "unresolved_ref",
        "unsupported",
        "refused",
        "failed",
        "quarantined",
        "busy",
        "stale_revision",
        "active_document_changed",
        "scan_incomplete",
        "unknown_commit",
        "timed_out",
        "cancelled",
        "operation_conflict",
    )


def test_a_document_switch_is_its_own_kind_in_a_batch_row():
    cause = ActiveDocumentChangedError(
        {"document_id": "a" * 32, "name": "First.dwg", "path": None},
        {"document_id": None, "name": "Second.dwg", "path": None},
    )
    wrapped = ToolError("Error calling tool 'entity_create_line': switched")
    wrapped.__cause__ = cause
    row = server._classify_batch_error(wrapped)
    assert row["kind"] == "active_document_changed"
    assert row["bound"]["name"] == "First.dwg" and row["active"]["name"] == "Second.dwg"
