"""1.7.0 review minors 1 and 2 - how ``cad_batch`` closes its own checkpoint.

Minor 1: a step that calls ``transaction_begin`` pushed a checkpoint above the
batch's. The closing rollback (or commit) popped *that* one, answered
``ok: True``, and the batch's own checkpoint stayed open with the steps before
the inner begin still in the drawing - on the cancelled path without any
trace, because that path read only the rollback's answer and never compared
fingerprints the way the normal path does. Now the close unwinds every
checkpoint a step opened above the batch's, and the cancelled path checks the
fingerprint and records ``rollback_unverified`` when it differs.

Minor 2: a non-cancelled exception escaping the step loop was logged and
recorded as "the rollback after cancelled request". The record now names what
ended the batch.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import McpError, ToolError

import server
from backends import ezdxf_backend
from engineering.changes import preview as preview_module

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


async def _status(client) -> dict:
    return (await client.call_tool("system_status", {})).structured_content


async def _entities(client) -> int:
    return (await client.call_tool("analysis_entity_stats", {})).structured_content[
        "total_entities"
    ]


async def _batch_rows(client) -> list[dict]:
    return [r for r in (await _status(client))["call_outcomes"] if r["call"] == "cad_batch"]


def _line(y: float = 0.0) -> dict:
    return {"tool": "entity_create_line", "args": {"x1": 0, "y1": y, "x2": 10, "y2": y}}


_BEGIN = {"tool": "transaction_begin", "args": {}}
_CIRCLE = {"tool": "entity_create_circle", "args": {"cx": 0, "cy": 0, "radius": 5}}


async def _cancel_stuck_batch(client, monkeypatch, steps) -> None:
    in_step = asyncio.Event()

    async def stuck_circle(self, *args, **kwargs):
        in_step.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "entity_create_circle", stuck_circle)
    request_id = client.session._request_id
    call = asyncio.ensure_future(
        client.call_tool("cad_batch", {"steps": steps, "on_error": "rollback"})
    )
    await in_step.wait()
    await client.cancel(request_id)
    with pytest.raises(McpError, match="cancelled"):
        await call


async def _settle(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.02)


# ── minor 1: a step's own transaction_begin ────────────────────────────────


async def test_a_cancelled_batch_unwinds_a_checkpoint_a_step_opened(client, monkeypatch):
    """RED (measured by the review): transaction_depth 1 afterwards, the line
    still drawn, and no cad_batch row in call_outcomes."""
    before = await _entities(client)
    await _cancel_stuck_batch(client, monkeypatch, [_line(), _BEGIN, _CIRCLE])

    async def closed() -> bool:
        return (await _status(client))["transaction_depth"] == 0

    await _settle(closed)
    assert await _entities(client) == before, "the line drawn before the inner begin stays"
    assert await _batch_rows(client) == [], "a verified rollback is not a failure to record"


async def test_a_failed_batch_unwinds_a_checkpoint_a_step_opened(client):
    before = await _entities(client)
    result = (
        await client.call_tool(
            "cad_batch",
            {
                "steps": [
                    _line(),
                    _BEGIN,
                    _line(5),
                    {"tool": "entity_delete", "args": {"handle": "FFFFFF"}},
                ],
                "on_error": "rollback",
            },
        )
    ).structured_content
    atomicity = result["atomicity"]
    assert atomicity["rolled_back"] is True
    assert atomicity["verified"] is True, atomicity
    assert atomicity["unwound"] == 1
    assert "transaction_begin" in atomicity["note"]
    assert (await _status(client))["transaction_depth"] == 0
    assert await _entities(client) == before


async def test_a_committed_batch_closes_a_checkpoint_a_step_opened(client):
    before = await _entities(client)
    result = (
        await client.call_tool(
            "cad_batch",
            {"steps": [_line(), _BEGIN, _line(5)], "on_error": "rollback"},
        )
    ).structured_content
    assert result["ok"] is True
    assert result["atomicity"]["committed"] is True
    assert result["atomicity"]["unwound"] == 1
    assert (await _status(client))["transaction_depth"] == 0
    assert await _entities(client) == before + 2


async def test_an_ordinary_batch_reports_no_unwinding(client):
    result = (
        await client.call_tool("cad_batch", {"steps": [_line()], "on_error": "rollback"})
    ).structured_content
    assert "unwound" not in result["atomicity"]


async def test_a_cancelled_batchs_unverified_rollback_is_recorded(client, monkeypatch, caplog):
    """RED: the cancelled path trusted ``ok: True``. A rollback that answers ok
    but leaves the drawing changed is recorded as ``rollback_unverified``."""
    real_commit = ezdxf_backend.EzdxfBackend.transaction_commit

    async def hollow_rollback(self):
        await real_commit(self)  # pops the checkpoint, restores nothing
        return {"ok": True, "message": "Transaction rolled back to snapshot"}

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "transaction_rollback", hollow_rollback)
    with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
        await _cancel_stuck_batch(client, monkeypatch, [_line(), _CIRCLE])

        async def recorded() -> bool:
            return bool(await _batch_rows(client))

        await _settle(recorded)
    [row] = await _batch_rows(client)
    assert row["outcome"] == "rollback_unverified"
    assert row["cause"] == "cancelled"
    assert "fingerprint" in row["error"]
    assert any(
        "cancelled" in r.getMessage() and "fingerprint" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )


# ── minor 2: a batch that ended without a cancellation ──────────────────────


async def test_a_non_cancelled_escape_is_not_labelled_a_cancellation(client, monkeypatch, caplog):
    """RED: logged as 'the rollback after cancelled request' and recorded with
    no cause, though nothing was cancelled."""

    async def drifting_before(self, label):
        raise RuntimeError("drift in the watch")

    async def declining_rollback(self):
        return {"ok": False, "error": "No active transaction to rollback"}

    monkeypatch.setattr(preview_module.RevisionWatch, "before", drifting_before)
    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "transaction_rollback", declining_rollback)
    revision = (await _status(client))["revision"]
    with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
        with pytest.raises(ToolError):
            await client.call_tool(
                "cad_batch",
                {"steps": [_line()], "on_error": "rollback", "expected_revision": revision},
            )
    [row] = await _batch_rows(client)
    assert row["outcome"] == "rollback_declined"
    assert row["cause"] == "RuntimeError: drift in the watch"
    lines = [r.getMessage() for r in caplog.records if "cad_batch: the rollback" in r.getMessage()]
    assert lines, "the declined rollback is still logged"
    assert all("cancelled" not in line for line in lines), lines
    assert any("RuntimeError: drift in the watch" in line for line in lines), lines
