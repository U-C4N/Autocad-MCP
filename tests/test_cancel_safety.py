"""B01 — the document lease follows the worker thread, never the awaiting coroutine.

``EzdxfBackend._async`` released the document lock in ``except BaseException``
when the awaiting task was cancelled, but nothing can stop the worker thread:
a second call then ran against the same ``Drawing`` while the first worker was
still writing — with ``EZDXF_CALL_TIMEOUT`` 120 and 0 alike, and with no
quarantine. The MCP SDK cancels a request by cancelling an anyio scope, so a
client pressing Esc was enough.

Every test here is ordered by ``threading.Event``s, never by a sleep race: the
worker blocks on ``release`` until the test has observed what it needs to.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
import pytest_asyncio

import config
from backends.quarantine import AbandonedCall, DocumentQuarantineError

pytestmark = pytest.mark.asyncio


class _Gate:
    """A worker body that blocks until the test releases it."""

    def __init__(self, *, fail: bool = False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.fail = fail

    def body(self, trace: list[str] | None = None):
        def _sync():
            self.entered.set()
            try:
                assert self.release.wait(10), "the test never released the worker"
                if trace is not None:
                    trace.append("A:end")
                if self.fail:
                    raise ValueError("died after the cancel")
                return "late"
            finally:
                self.finished.set()

        return _sync


async def _wait_for(event: threading.Event, timeout: float = 10.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout), "event never set"


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def loop_errors():
    """Every exception a loop callback raised — a double release lands here."""
    loop = asyncio.get_running_loop()
    seen: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    yield seen
    loop.set_exception_handler(previous)


@pytest.fixture
def gates():
    made: list[_Gate] = []

    def make(**kwargs) -> _Gate:
        gate = _Gate(**kwargs)
        made.append(gate)
        return gate

    yield make
    for gate in made:
        gate.release.set()
    for gate in made:
        if gate.entered.is_set():
            assert gate.finished.wait(10), "a gated worker outlived its test"


# ── AbandonedCall.set_on_finish ─────────────────────────────────────────────


async def test_on_finish_runs_once_on_the_worker_thread():
    ran: list[str] = []
    watched = AbandonedCall(lambda: "value")
    assert watched.set_on_finish(lambda: ran.append(threading.current_thread().name)) is True
    worker = threading.Thread(target=watched, name="the-worker")
    worker.start()
    worker.join(5)
    assert ran == ["the-worker"]
    assert watched.finished is True


async def test_on_finish_refuses_after_the_worker_finished():
    watched = AbandonedCall(lambda: "value")
    watched()
    ran: list[int] = []
    assert watched.set_on_finish(lambda: ran.append(1)) is False
    assert ran == [], "a hook registered too late is the caller's job, never run twice"


# ── cancelled while queued ──────────────────────────────────────────────────


@pytest.mark.parametrize("timeout", [30.0, 0.0])
async def test_cancel_while_queued_touches_no_lock(backend, monkeypatch, timeout):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", timeout)
    ran: list[str] = []
    held = backend._lock
    await held.acquire()  # a live call owns the document
    try:
        task = asyncio.ensure_future(backend._async(lambda: ran.append("queued")))
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert held.locked(), "the queued call must not release a lock it never took"
        assert ran == []
    finally:
        held.release()
    assert backend.call_outcomes[-1]["outcome"] == "cancelled_queued"
    assert await backend._async(lambda: "next") == "next"


# ── cancelled while the worker runs ─────────────────────────────────────────


@pytest.mark.parametrize("timeout", [30.0, 0.0])
async def test_cancel_while_running_keeps_the_lease_until_the_worker_ends(
    backend, monkeypatch, gates, loop_errors, timeout
):
    """RED: B ran at once, while A's worker was still inside the drawing."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", timeout)
    gate = gates()
    trace: list[str] = []
    first = asyncio.ensure_future(backend._async(gate.body(trace)))
    await _wait_for(gate.entered)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.ensure_future(backend._async(lambda: trace.append("B:run") or "B"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not second.done(), "B must wait for A's worker, not for A's coroutine"
    assert trace == []

    gate.release.set()
    assert await asyncio.wait_for(second, timeout=10) == "B"
    assert trace == ["A:end", "B:run"]
    row = backend.call_outcomes[-1]
    assert row["outcome"] == "completed_after_cancel"
    assert row["ended"] is not None and row["ended"] >= row["started"]
    assert not backend._lock.locked()
    assert backend._quarantine is None
    assert not backend._leases, "the deadline timer is cancelled when the worker finishes"
    assert loop_errors == [], "the lock was released more than once"


async def test_a_worker_failing_after_the_cancel_is_recorded(backend, monkeypatch, gates):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    gate = gates(fail=True)
    task = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release.set()
    await _until(lambda: backend.call_outcomes[-1]["outcome"] != "cancelled_running")
    assert backend.call_outcomes[-1]["outcome"] == "failed_after_cancel"
    assert await backend._async(lambda: "next") == "next"


async def test_cancel_as_the_worker_finishes_releases_exactly_once(
    backend, monkeypatch, gates, loop_errors
):
    """The worker has ended but the loop has not yet seen it: the cancellation
    lands on a finished future. The lease ends with the coroutine, once."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    gate = gates()
    task = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    gate.release.set()
    # Block the loop thread (deliberately, not a race) until the worker's
    # future is complete, so its result cannot reach the task first.
    deadline = time.monotonic() + 10
    while backend._in_flight and time.monotonic() < deadline:
        time.sleep(0.001)
    assert not backend._in_flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not backend._lock.locked(), "a finished worker leaves no lease behind"
    assert backend.call_outcomes[-1]["outcome"] == "completed_after_cancel"
    assert await backend._async(lambda: "next") == "next"
    assert loop_errors == []


# ── cancelled, then the original deadline passes ────────────────────────────


async def test_a_cancelled_worker_alive_at_its_deadline_quarantines(
    backend, monkeypatch, gates, loop_errors
):
    """RED: nothing was quarantined; the next call wrote into the drawing."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()
    orphan = backend._lock
    task = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _until(lambda: backend._quarantine is not None)
    assert backend._quarantine.cause == "cancelled_then_deadline"
    assert backend._lock is not orphan, "the lock is swapped at the deadline"
    assert backend.call_outcomes[-1]["outcome"] == "abandoned_deadline"
    with pytest.raises(DocumentQuarantineError):
        await backend._async(lambda: "refused")

    gate.release.set()
    await _wait_for(gate.finished)
    await _until(lambda: backend.call_outcomes[-1]["ended"] is not None)
    assert orphan.locked(), "the orphaned lock is never released to its waiters"
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    assert await backend._async(lambda: "lifted") == "lifted"  # completed cleanly: reaped
    assert loop_errors == []


async def test_waiters_past_the_gate_never_inherit_the_orphaned_lock(backend, monkeypatch, gates):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()
    ran: list[str] = []
    first = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    waiter = asyncio.ensure_future(backend._async(lambda: ran.append("waiter")))
    for _ in range(5):
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises((RuntimeError, DocumentQuarantineError)):
        await asyncio.wait_for(waiter, timeout=10)
    assert ran == [], "the waiter must never run while the abandoned worker is alive"
    gate.release.set()


# ── deadline overrun ────────────────────────────────────────────────────────


async def test_an_overrun_that_finishes_at_the_deadline_returns_its_value(
    backend, monkeypatch, gates
):
    """``work.done()`` decides, never ``cancelled()``: a worker that ended as
    the deadline fired is not abandoned and its value is the answer."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()
    task = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    gate.release.set()
    await _wait_for(gate.finished)
    deadline = time.monotonic() + 10
    while backend._in_flight and time.monotonic() < deadline:
        time.sleep(0.001)
    # Hold the loop thread past the deadline: when it runs again the result
    # and the expired deadline are both ready in the same iteration.
    time.sleep(0.4)
    assert await task == "late"
    assert backend._quarantine is None
    assert not backend._lock.locked()


async def test_a_readonly_overrun_keeps_the_lease_and_never_quarantines(
    backend, monkeypatch, gates
):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()
    held = backend._lock
    with pytest.raises(RuntimeError, match="read-only") as excinfo:
        await backend._async(gate.body(), readonly=True)
    assert "NOT quarantined" in str(excinfo.value)
    assert backend._quarantine is None
    assert backend._abandoned_calls == []
    assert backend._lock is held and held.locked(), "the read keeps its lease"
    with pytest.raises(RuntimeError, match="busy"):
        await backend._async(lambda: "write")

    gate.release.set()
    await _until(lambda: not held.locked())
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    assert await backend._async(lambda: "next") == "next"


async def test_the_obvious_reads_are_readonly(backend, monkeypatch):
    seen: dict[str, bool] = {}
    real = backend._async

    async def spy(func, *args, readonly=False, **kwargs):
        seen[func.__qualname__.split(".")[1]] = readonly
        return await real(func, *args, readonly=readonly, **kwargs)

    monkeypatch.setattr(backend, "_async", spy)
    line = await backend.entity_create_line(0, 0, 1, 1)
    await backend.entity_get(line.handle)
    await backend.entity_list()
    await backend.entity_count()
    await backend.drawing_info()
    assert seen == {
        "entity_create_line": False,
        "entity_get": True,
        "entity_list": True,
        "entity_count": True,
        "drawing_info": True,
    }


# ── what the call ran against ───────────────────────────────────────────────


async def test_the_quarantine_names_the_document_the_call_started_on(
    backend, monkeypatch, gates, tmp_path
):
    """RED: the record read ``self._doc_path`` when it was made, after the
    worker had already re-pointed the document."""
    await backend.drawing_save_as(str(tmp_path / "started.dxf"))
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()

    def _repoint():
        backend._state.path = str(tmp_path / "renamed.dxf")
        return gate.body()()

    with pytest.raises(RuntimeError, match="timed out"):
        await backend._async(_repoint)
    assert backend._quarantine.document_path == str(tmp_path / "started.dxf")
    assert backend._quarantine.cause == "deadline"
    assert backend.call_outcomes[-1]["document"] == str(tmp_path / "started.dxf")


# ── reporting ───────────────────────────────────────────────────────────────


async def test_call_outcomes_is_bounded_and_reported(backend, monkeypatch, gates):
    assert backend.call_outcomes.maxlen == 50
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    gate = gates()
    task = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = await backend.system_status()
    assert status["call_outcomes"][-1]["outcome"] == "cancelled_running"
    assert status["call_outcomes"][-1]["call"] == "body"
    gate.release.set()
