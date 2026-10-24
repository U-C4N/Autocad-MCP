"""B01 on the COM backend: a cancelled ``_run`` records whether its call ran.

One STA thread runs every COM call in FIFO order, so a cancelled call can never
overlap the next one — that part was already true and is pinned here. What was
missing is the record: Task 10 reports ``unknown_commit`` for a mutating call
that was cancelled while it ran, and needs to tell that apart from a call that
never left the queue. No AutoCAD is touched: the executor runs plain callables.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

import backends.com_backend as cb
import config
from backends.com_backend import ComBackend
from backends.quarantine import DaemonExecutor

pytestmark = pytest.mark.asyncio


@pytest.fixture
def com_backend():
    b = ComBackend()
    b._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    b._connected = True
    yield b
    if b._executor is not None:
        b._executor.shutdown(wait=False)
        b._executor = None


async def _wait_for(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 10), "event never set"


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.01)


async def test_cancelled_queued_and_running_calls_are_recorded_in_order(com_backend, monkeypatch):
    monkeypatch.setattr(config.settings, "com_call_timeout", 30)
    entered = threading.Event()
    release = threading.Event()
    trace: list[str] = []

    def first():
        entered.set()
        assert release.wait(10)
        trace.append("first:end")

    def second():
        trace.append("second:ran")

    running = asyncio.ensure_future(com_backend._run(first))
    await _wait_for(entered)
    queued = asyncio.ensure_future(com_backend._run(second))
    for _ in range(5):
        await asyncio.sleep(0)
    queued.cancel()
    running.cancel()
    for task in (queued, running):
        with pytest.raises(asyncio.CancelledError):
            await task

    rows = list(com_backend.call_outcomes)
    assert [r["outcome"] for r in rows] == ["cancelled_queued", "cancelled_running"]
    assert rows[0]["ended"] is not None
    assert rows[1]["ended"] is None, "the running call has not returned yet"

    third = asyncio.ensure_future(com_backend._run(lambda: trace.append("third:ran")))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not third.done(), "FIFO: the next call waits for the cancelled one's worker"
    release.set()
    await asyncio.wait_for(third, timeout=10)
    assert trace == ["first:end", "third:ran"], "the queued call never ran"
    await _until(lambda: rows[1]["ended"] is not None)


async def test_system_status_reports_the_last_outcomes(com_backend, monkeypatch):
    def _no_autocad():
        raise RuntimeError("no AutoCAD in this test")

    monkeypatch.setattr(cb, "_acad_app", _no_autocad)
    com_backend.call_outcomes.append(
        {"call": "x", "outcome": "cancelled_queued", "started": 1.0, "ended": 2.0, "document": None}
    )
    status = await com_backend.system_status()
    assert status["connected"] is False
    assert status["call_outcomes"][-1]["outcome"] == "cancelled_queued"


async def test_disconnect_waits_a_bounded_grace_for_a_stuck_call(com_backend, monkeypatch, caplog):
    monkeypatch.setattr(config.settings, "com_call_timeout", 30)
    monkeypatch.setattr(config.settings, "shutdown_grace", 0.2)
    entered = threading.Event()
    release = threading.Event()

    def stuck():
        entered.set()
        release.wait(10)

    task = asyncio.ensure_future(com_backend._run(stuck))
    await _wait_for(entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="backends.com_backend"):
        await com_backend.disconnect()
    assert time.monotonic() - started < 5
    assert any("SHUTDOWN_GRACE=0.2" in r.getMessage() for r in caplog.records)
    release.set()


async def test_connect_runs_com_on_one_daemon_thread(monkeypatch):
    pytest.importorskip("pythoncom")
    b = ComBackend()
    await b.connect()  # lazy: no AutoCAD contact until the first tool call
    try:
        assert isinstance(b._executor, DaemonExecutor)
        assert await b._run(threading.current_thread) is not threading.current_thread()
        assert [t.daemon for t in b._executor.threads] == [True]
    finally:
        await b.disconnect()
