"""B01 — what a cancellation leaves behind above the backend.

A cancelled call must leave no half-applied checkpoint (``cad_batch``,
``drawing_refine``), a shielded rollback must wait for a worker that is still
writing instead of racing it (``pid_from_spec``), the audit log must say a call
was cancelled, and the server's shutdown must release the backend.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import types

import anyio
import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import McpError

import config
import server
from backends import ezdxf_backend
from engineering.pid import spec as spec_module
from engineering.pid.spec import EXAMPLE_SPEC, run_spec
from engineering.plan_spec import Issue
from engineering.refiner import RepairRegistry, refine_drawing

pytestmark = pytest.mark.asyncio


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.02)


# ── AuditMiddleware ─────────────────────────────────────────────────────────


async def test_the_audit_log_records_a_cancelled_call(caplog):
    """RED: it caught ``Exception`` only, so a cancellation left no line at all."""

    async def call_next(_context):
        raise asyncio.CancelledError

    context = types.SimpleNamespace(message=types.SimpleNamespace(name="entity_create_line"))
    with caplog.at_level(logging.INFO, logger="autocad_mcp"):
        with pytest.raises(asyncio.CancelledError):
            await server.AuditMiddleware().on_call_tool(context, call_next)
    lines = [r.getMessage() for r in caplog.records if r.name == "autocad_mcp"]
    assert any(line.startswith("TOOL CANCELLED") and "entity_create_line" in line for line in lines)


# ── the lifespan ────────────────────────────────────────────────────────────


class _FakeBackend:
    name = "fake"

    def __init__(self, *, fail: bool = False):
        self.disconnected = 0
        self.fail = fail

    async def disconnect(self):
        self.disconnected += 1
        if self.fail:
            raise RuntimeError("disconnect blew up")


async def test_shutdown_disconnects_the_backend(monkeypatch):
    """RED: the lifespan's ``finally`` only logged; the backend was never released."""
    fake = _FakeBackend()

    async def make():
        return fake

    monkeypatch.setattr(server, "_make_backend", make)
    async with Client(server.mcp):
        assert fake.disconnected == 0
    assert fake.disconnected == 1


async def test_a_failing_disconnect_does_not_break_shutdown(monkeypatch, caplog):
    fake = _FakeBackend(fail=True)

    async def make():
        return fake

    monkeypatch.setattr(server, "_make_backend", make)
    with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
        async with Client(server.mcp):
            pass
    assert fake.disconnected == 1
    assert any("disconnect blew up" in r.getMessage() for r in caplog.records)


async def test_a_backend_that_never_started_is_survivable(monkeypatch):
    async def make():
        raise RuntimeError("no engine today")

    monkeypatch.setattr(server, "_make_backend", make)
    async with Client(server.mcp) as client:
        status = (await client.call_tool("system_status", {})).structured_content
    assert status["backend"] == "none"
    assert "no engine today" in status["error"]


# ── cad_batch ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client():
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


async def _depth(client) -> int:
    return (await client.call_tool("system_status", {})).structured_content["transaction_depth"]


async def _entities(client) -> int:
    return (await client.call_tool("analysis_entity_stats", {})).structured_content[
        "total_entities"
    ]


def _line(y: float = 0.0) -> dict:
    return {"tool": "entity_create_line", "args": {"x1": 0, "y1": y, "x2": 10, "y2": y}}


async def test_a_cancelled_batch_rolls_its_checkpoint_back(client, monkeypatch):
    """RED: the cancellation escaped the step loop, past the rollback."""
    in_step = asyncio.Event()

    async def stuck_circle(self, *args, **kwargs):
        in_step.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "entity_create_circle", stuck_circle)
    before = await _entities(client)
    request_id = client.session._request_id
    call = asyncio.ensure_future(
        client.call_tool(
            "cad_batch",
            {
                "steps": [
                    _line(),
                    {"tool": "entity_create_circle", "args": {"cx": 0, "cy": 0, "radius": 5}},
                ],
                "on_error": "rollback",
            },
        )
    )
    await in_step.wait()
    await client.cancel(request_id)
    with pytest.raises(McpError, match="cancelled"):
        await call

    async def clean() -> bool:
        return await _depth(client) == 0 and await _entities(client) == before

    await _until(clean)


async def test_a_batch_cancelled_during_its_commit_still_commits(client, monkeypatch):
    """RED: the commit's own await was cancelled, leaving the checkpoint open."""
    committing = asyncio.Event()
    proceed = asyncio.Event()
    real_commit = ezdxf_backend.EzdxfBackend.transaction_commit

    async def slow_commit(self):
        committing.set()
        await proceed.wait()
        return await real_commit(self)

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "transaction_commit", slow_commit)
    before = await _entities(client)
    request_id = client.session._request_id
    call = asyncio.ensure_future(
        client.call_tool("cad_batch", {"steps": [_line()], "on_error": "rollback"})
    )
    await committing.wait()
    await client.cancel(request_id)
    with pytest.raises(McpError, match="cancelled"):
        await call
    proceed.set()

    async def committed() -> bool:
        return await _depth(client) == 0

    await _until(committed)
    assert await _entities(client) == before + 1, "the committed line stays"
    # The session survived: a cancelled request that returned normally would
    # have been answered twice.
    assert (await client.call_tool("system_status", {})).structured_content["backend"] == "ezdxf"


# ── drawing_refine ──────────────────────────────────────────────────────────


async def test_a_cancelled_refine_round_rolls_back(backend, monkeypatch):
    """RED: ``except Exception`` let the cancellation leave the repair applied
    inside an open transaction."""
    issue = Issue("error", "construction_left", "left over", ["1"])

    async def critique(focus=None):
        return [issue]

    in_repair = asyncio.Event()

    async def repair(backend_arg, _issue):
        await backend_arg.entity_create_line(0, 0, 5, 5)
        in_repair.set()
        await asyncio.Event().wait()
        return {}

    registry = RepairRegistry()
    registry.register("construction_left", repair)
    monkeypatch.setattr(backend, "drawing_critique", critique)
    before = await backend.entity_count()

    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: refine_drawing(backend, min_score=100.0, registry=registry))
        await in_repair.wait()
        tg.cancel_scope.cancel()

    assert await backend.entity_count() == before
    assert (await backend.system_status())["transaction_depth"] == 0


# ── pid_from_spec: the shielded rollback waits for the orphaned worker ──────


async def test_the_pid_rollback_waits_for_the_cancelled_worker(backend, monkeypatch):
    """RED: the cancelled coroutine released the lock, so the rollback body ran
    while the worker was still alive and the worker's write landed afterwards."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    entered = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()
    rollback_entered = asyncio.Event()
    rollback_body_saw_worker_done: list[bool] = []

    def worker():
        entered.set()
        try:
            assert release.wait(10)
            backend._msp().add_line((0, 0), (5, 5))
            return "late"
        finally:
            worker_done.set()

    async def blocking_draw_line(backend_arg, *args, **kwargs):
        await backend_arg._async(worker)
        return {"crossings": 0}

    real_async = backend._async

    async def spying_async(func, *args, **kwargs):
        if func.__qualname__.startswith("EzdxfBackend.transaction_rollback."):
            rollback_entered.set()

            def body(*a, **k):
                rollback_body_saw_worker_done.append(worker_done.is_set())
                return func(*a, **k)

            body.__qualname__ = func.__qualname__
            return await real_async(body, *args, **kwargs)
        return await real_async(func, *args, **kwargs)

    monkeypatch.setattr(spec_module, "draw_line", blocking_draw_line)
    monkeypatch.setattr(backend, "_async", spying_async)

    async def release_when_the_rollback_is_blocked_or_ran():
        # Release the worker only once the rollback has either run its body
        # (the bug: then it saw a live worker) or is queued on the lock (the
        # fix). `_waiters` is asyncio.Lock's own queue (CPython 3.13).
        await rollback_entered.wait()
        lock = backend._lock
        deadline = time.monotonic() + 10
        while not rollback_body_saw_worker_done and not (lock.locked() and lock._waiters):
            assert time.monotonic() < deadline, "the rollback neither ran nor waited"
            await asyncio.sleep(0.005)
        release.set()

    releaser = asyncio.ensure_future(release_when_the_rollback_is_blocked_or_ran())
    before = await backend.entity_count()
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_spec, backend, EXAMPLE_SPEC)
        assert await anyio.to_thread.run_sync(entered.wait, 10)
        tg.cancel_scope.cancel()
    await releaser
    assert await anyio.to_thread.run_sync(worker_done.wait, 10)

    assert rollback_body_saw_worker_done == [True], "the rollback raced the live worker"
    assert await backend.entity_count() == before
    assert (await backend.system_status())["transaction_depth"] == 0
