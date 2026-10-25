"""B12 - the operation id reaches the worker threads and the call_outcomes rows.

ezdxf's ``_async`` runs its worker in ``contextvars.copy_context()`` (W1); the
COM executor copies no context, so ``_run`` carries the id into the STA job
explicitly. Every cancellation and abandonment row names the operation it
belongs to - that is what lets the middleware call a cancelled write
``unknown_commit`` and a cancelled queue entry ``cancelled``. No AutoCAD is
touched: the COM executor runs plain callables on a daemon thread.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import config
from backends.com_backend import ComBackend
from backends.operations import CURRENT_OPERATION
from backends.quarantine import DaemonExecutor

pytestmark = pytest.mark.asyncio


def _wait_on(entered: threading.Event, release: threading.Event) -> str:
    entered.set()
    assert release.wait(10), "the test never released the worker"
    return "late"


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.01)


@pytest.fixture
def com_backend():
    b = ComBackend()
    b._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    b._connected = True
    yield b
    if b._executor is not None:
        b._executor.shutdown(wait=False)
        b._executor = None


async def test_the_com_job_sees_the_callers_operation_id(com_backend):
    token = CURRENT_OPERATION.set("op-com-1")
    try:
        seen = await com_backend._run(CURRENT_OPERATION.get)
    finally:
        CURRENT_OPERATION.reset(token)
    assert seen == "op-com-1"
    assert await com_backend._run(CURRENT_OPERATION.get) is None, "nothing leaks into the next job"


async def test_a_cancelled_com_call_records_its_operation_id(com_backend, monkeypatch):
    monkeypatch.setattr(config.settings, "com_call_timeout", 30)
    entered, release = threading.Event(), threading.Event()
    token = CURRENT_OPERATION.set("op-com-2")
    try:
        task = asyncio.ensure_future(com_backend._run(_wait_on, entered, release))
    finally:
        CURRENT_OPERATION.reset(token)
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = com_backend.call_outcomes[-1]
        assert (row["call"], row["outcome"]) == ("_wait_on", "cancelled_running")
        assert row["operation_id"] == "op-com-2"
    finally:
        release.set()


async def test_a_com_deadline_overrun_leaves_an_abandoned_deadline_row(com_backend, monkeypatch):
    monkeypatch.setattr(config.settings, "com_call_timeout", 0.2)
    entered, release = threading.Event(), threading.Event()
    token = CURRENT_OPERATION.set("op-com-3")
    try:
        with pytest.raises(RuntimeError, match="did not respond"):
            await com_backend._run(_wait_on, entered, release)
    finally:
        CURRENT_OPERATION.reset(token)
        release.set()
    row = com_backend.call_outcomes[-1]
    assert (row["call"], row["outcome"], row["ended"]) == ("_wait_on", "abandoned_deadline", None)
    assert row["operation_id"] == "op-com-3"


async def test_a_cancelled_ezdxf_call_records_its_operation_id(backend):
    entered, release = threading.Event(), threading.Event()
    token = CURRENT_OPERATION.set("op-ezdxf-1")
    try:
        task = asyncio.ensure_future(backend._async(_wait_on, entered, release))
    finally:
        CURRENT_OPERATION.reset(token)
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = backend.call_outcomes[-1]
        assert (row["call"], row["outcome"]) == ("_wait_on", "cancelled_running")
        assert row["operation_id"] == "op-ezdxf-1"
    finally:
        release.set()
    await _until(lambda: backend.call_outcomes[-1]["ended"] is not None)


async def test_the_ezdxf_worker_sees_the_callers_operation_id(backend):
    token = CURRENT_OPERATION.set("op-ezdxf-2")
    try:
        assert await backend._async(CURRENT_OPERATION.get) == "op-ezdxf-2"
    finally:
        CURRENT_OPERATION.reset(token)
