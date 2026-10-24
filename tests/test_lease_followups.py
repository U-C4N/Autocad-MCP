"""W1 review follow-ups to B01 (Task 1, findings 1 and 2) - nothing needs a restart.

Finding 1: a read-only call that overran wrote no ``call_outcomes`` row, a
cancelled read-only call got no original-deadline timer, and the overrunning
read kept the lock: every later call answered a bare "busy", the quarantine
exits included, and only a server restart recovered. Now the read is
*detached* at its deadline: recorded as ``readonly_overrun``, never
quarantined, every other call on its document refused by name, and the exits
(drawing_open / drawing_new / drawing_close / document_activate /
document_close) work while it still runs - they replace the document object
the read holds, so nothing races it.

Finding 2: after a deadline swapped the lock, calls already queued on the
orphaned lock waited their own full EZDXF_CALL_TIMEOUT (forever at 0) and got
"busy"; a cancelled ``cad_batch``'s shielded rollback was one of them. Now the
swap wakes them at once and the new state answers them.

Ordering is by ``threading.Event`` and ``asyncio`` futures, never by a sleep
race. The only clocks are the deadlines under test; the "at once" bounds are
measured with ``time.perf_counter`` and stated with their margin.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import McpError

import config
import server
from backends import ezdxf_backend
from backends.quarantine import DocumentBusyError, DocumentQuarantineError

pytestmark = pytest.mark.asyncio

#: "At once": the woken call is answered within this many seconds of the swap.
#: The work in between is a handful of loop iterations (well under 10 ms on
#: this machine); the bound is the 1 s the orchestrator set, a 100x margin.
AT_ONCE = 1.0
#: The deadline of a call that something else must queue behind (or cancel)
#: first. That takes a few milliseconds; 1 s keeps the order certain on a
#: loaded runner (0.3 s was seen to lose it once in a 64-test run), and every
#: test asserts the order it relies on rather than assuming it.
RACED_DEADLINE = 1.0


class _Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def body(self):
        def body():
            self.entered.set()
            try:
                assert self.release.wait(30), "the test never released the worker"
                return "late"
            finally:
                self.finished.set()

        return body


async def _wait_for(event: threading.Event, timeout: float = 10.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout), "event never set"


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.01)


@pytest.fixture
def gates():
    made: list[_Gate] = []

    def make() -> _Gate:
        gate = _Gate()
        made.append(gate)
        return gate

    yield make
    for gate in made:
        gate.release.set()
    for gate in made:
        if gate.entered.is_set():
            assert gate.finished.wait(10), "a gated worker outlived its test"


def _queued_on(lock) -> int:
    return sum(1 for waiter in (lock._waiters or ()) if not waiter.done())


# ── finding 2: waiters on an orphaned lock ──────────────────────────────────


@pytest.mark.parametrize("waiter_timeout", [30.0, 0.0])
async def test_waiters_on_a_swapped_lock_are_answered_at_once(
    backend, monkeypatch, gates, waiter_timeout
):
    """RED: the waiter sat out its own 30 s (forever at 0) and then said 'busy'."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", RACED_DEADLINE)
    gate = gates()
    orphan = backend._lock
    ran: list[str] = []
    runaway = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", waiter_timeout)
    waiter = asyncio.ensure_future(backend._async(lambda: ran.append("waiter")))
    await _until(lambda: _queued_on(orphan) == 1)

    with pytest.raises(RuntimeError, match="timed out"):
        await runaway  # the deadline swaps the lock here
    swapped = time.perf_counter()
    assert backend._lock is not orphan
    with pytest.raises(DocumentQuarantineError):
        await asyncio.wait_for(waiter, timeout=5)
    assert time.perf_counter() - swapped < AT_ONCE
    assert ran == []
    assert orphan.locked(), "the orphaned lock itself is still never released"


@pytest.mark.parametrize("waiter_timeout", [30.0, 0.0])
async def test_a_cancelled_calls_deadline_wakes_its_waiters(
    backend, monkeypatch, gates, waiter_timeout
):
    """The same rule when the swap comes from a cancelled call's original
    deadline (``_lease_deadline``), not from an awaited overrun."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", RACED_DEADLINE)
    gate = gates()
    orphan = backend._lock
    swaps: list[tuple[float, int]] = []
    real_deadline = backend._lease_deadline

    def timed_deadline(*args, **kwargs):
        queued = _queued_on(orphan)
        real_deadline(*args, **kwargs)
        swaps.append((time.perf_counter(), queued))

    monkeypatch.setattr(backend, "_lease_deadline", timed_deadline)
    task = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", waiter_timeout)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    waiter = asyncio.ensure_future(backend._async(lambda: "never"))
    await _until(lambda: _queued_on(orphan) == 1)
    with pytest.raises(DocumentQuarantineError):
        await asyncio.wait_for(waiter, timeout=5)
    answered = time.perf_counter()
    assert len(swaps) == 1 and backend._quarantine is not None
    swapped, queued = swaps[0]
    assert queued == 1, "the waiter was queued on the lock when it was swapped"
    assert answered - swapped < AT_ONCE


async def test_a_quarantine_exit_woken_from_an_orphan_runs_on_the_new_lock(
    backend, monkeypatch, gates
):
    """An exit that was queued on the orphaned lock is not refused: it
    retries on the fresh lock and rebinds the document while the runaway lives."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", RACED_DEADLINE)
    gate = gates()
    orphan = backend._lock
    runaway = asyncio.ensure_future(backend._async(gate.body()))
    await _wait_for(gate.entered)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    fresh = asyncio.ensure_future(backend.drawing_new())
    await _until(lambda: _queued_on(orphan) == 1)
    with pytest.raises(RuntimeError, match="timed out"):
        await runaway
    swapped = time.perf_counter()
    result = await asyncio.wait_for(fresh, timeout=5)
    assert time.perf_counter() - swapped < AT_ONCE
    assert result["quarantine_cleared"]["call"] == "body"
    assert not gate.finished.is_set(), "the runaway is still alive"
    assert await backend._async(lambda: "next") == "next"


# ── finding 2: the cancelled cad_batch's shielded rollback ──────────────────


@pytest.mark.parametrize("waiter_timeout", [30.0, 0.0])
async def test_a_cancelled_batch_rollback_returns_at_the_swap(
    monkeypatch, gates, caplog, waiter_timeout
):
    """RED: the shielded rollback queued on the orphaned lock and held the
    cancellation for its own 30 s - forever at 0, with lifespan shutdown
    waiting behind it."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 1.0)
    monkeypatch.setattr(config.settings, "shutdown_grace", 0.2)
    gate = gates()
    swaps: list[tuple[float, int]] = []
    orphans: list = []
    rollbacks: list[float] = []
    rolled_back = asyncio.Event()
    real_deadline = ezdxf_backend.EzdxfBackend._lease_deadline
    real_rollback = ezdxf_backend.EzdxfBackend.transaction_rollback

    def timed_deadline(self, lease, *args, **kwargs):
        queued = _queued_on(lease.lock)
        orphans.append(lease.lock)
        real_deadline(self, lease, *args, **kwargs)
        swaps.append((time.perf_counter(), queued))

    async def timed_rollback(self):
        try:
            return await real_rollback(self)
        finally:
            rollbacks.append(time.perf_counter())
            rolled_back.set()

    async def stuck_circle(self, *args, **kwargs):
        return await self._async(gate.body())

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "_lease_deadline", timed_deadline)
    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "transaction_rollback", timed_rollback)
    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "entity_create_circle", stuck_circle)

    loop = asyncio.get_running_loop()

    def unblock_a_hung_red_run():
        # Only matters before the fix, where the rollback never returns at
        # 0 s: cancel what waits on the orphan so the test fails, not hangs.
        for lock in orphans:
            for waiter in list(lock._waiters or ()):
                waiter.cancel()
        gate.release.set()

    watchdog = loop.call_later(8.0, unblock_a_hung_red_run)
    stack = contextlib.AsyncExitStack()
    try:
        client = await stack.enter_async_context(Client(server.mcp))
        await client.call_tool("drawing_new", {})
        request_id = client.session._request_id
        call = asyncio.ensure_future(
            client.call_tool(
                "cad_batch",
                {
                    "steps": [
                        {"tool": "entity_create_circle", "args": {"cx": 0, "cy": 0, "radius": 5}}
                    ],
                    "on_error": "rollback",
                },
            )
        )
        await _wait_for(gate.entered)
        monkeypatch.setattr(config.settings, "ezdxf_call_timeout", waiter_timeout)
        with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
            await client.cancel(request_id)
            with pytest.raises(McpError, match="cancelled"):
                await call
            await asyncio.wait_for(rolled_back.wait(), timeout=4)
        assert len(swaps) == 1
        swapped, queued = swaps[0]
        assert queued == 1, "the rollback was queued on the lock when it was swapped"
        assert rollbacks[0] - swapped < AT_ONCE
        assert not gate.finished.is_set(), "the runaway is still alive"
        closing = time.perf_counter()
        await stack.aclose()
        # SHUTDOWN_GRACE (0.2 s) for the still-running worker, then exit.
        assert time.perf_counter() - closing < 0.2 + AT_ONCE
    finally:
        watchdog.cancel()
        unblock_a_hung_red_run()  # a failed run must not hang the shutdown below
        await stack.aclose()


# ── finding 1: a read-only overrun is detached, never a restart ─────────────


async def test_a_readonly_overrun_is_recorded_and_detached(backend, monkeypatch, gates, tmp_path):
    """RED: no call_outcomes row, and every later call - the exits included -
    answered a bare 'busy' until the read ended or the server restarted."""
    path = str(tmp_path / "read.dxf")
    await backend.drawing_save_as(path)
    read_doc = backend._doc
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()
    with pytest.raises(RuntimeError, match="read-only") as excinfo:
        await backend._async(gate.body(), readonly=True)
    assert "NOT quarantined" in str(excinfo.value)
    assert backend._quarantine is None

    row = backend.call_outcomes[-1]
    assert row["outcome"] == "readonly_overrun" and row["call"] == "body"
    assert row["deadline"] == pytest.approx(row["started"] + 0.3, abs=0.05)
    assert row["ended"] is None and row["document"] == path

    # Any other call on the read's document: refused at once, by name.
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    asked = time.perf_counter()
    with pytest.raises(DocumentBusyError, match="busy") as refused:
        await backend._async(lambda: "write")
    assert time.perf_counter() - asked < AT_ONCE
    message = str(refused.value)
    for word in ("'body'", "running", "drawing_open", "drawing_new", "drawing_close"):
        assert word in message, word
    assert refused.value.to_dict()["kind"] == "busy"
    status = await backend.system_status()
    [detached] = status["detached_reads"]
    assert detached["call"] == "body" and detached["thread_alive"] is True
    assert detached["document_active"] is True

    # The exits work while the read still runs.
    await asyncio.wait_for(backend.drawing_new(), timeout=AT_ONCE)
    assert backend._doc is not read_doc
    assert await backend._async(lambda: "write") == "write"
    assert not gate.finished.is_set(), "the read is still running"
    # Back to the read's document: refused again, nothing races the read.
    await asyncio.wait_for(backend.document_activate(path), timeout=AT_ONCE)
    with pytest.raises(DocumentBusyError):
        await backend.entity_create_line(0, 0, 1, 1)

    gate.release.set()
    await _wait_for(gate.finished)
    await _until(lambda: row["ended"] is not None)
    assert row["outcome"] == "readonly_overrun"
    assert await backend._async(lambda: "after") == "after"
    assert (await backend.system_status())["detached_reads"] == []


async def test_a_read_queued_behind_a_switch_is_filed_on_the_document_it_reads(
    backend, monkeypatch, gates
):
    """RED: ``_async`` took the document at entry. A read queued behind a
    document_activate ran on the new document but was detached against the
    old one, so the gate refused the free document and admitted writes on the
    one still being read."""
    key_a = (await backend.drawing_new())["document"]
    doc_a = backend._doc
    key_b = (await backend.drawing_new())["document"]
    doc_b = backend._doc
    await backend.document_activate(key_a)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", RACED_DEADLINE)
    writer, reader = gates(), gates()
    seen = {}
    read_gate = reader.body()

    def read_body():
        seen["doc"] = backend._doc
        return read_gate()

    held = backend._lock
    first = asyncio.ensure_future(backend._async(writer.body()))
    await _wait_for(writer.entered)
    switch = asyncio.ensure_future(backend.document_activate(key_b))
    read = asyncio.ensure_future(backend._async(read_body, readonly=True))
    await _until(lambda: _queued_on(held) == 2)  # the switch first, then the read
    writer.release.set()
    assert await first == "late"
    await switch
    await _wait_for(reader.entered)
    assert seen["doc"] is doc_b
    with pytest.raises(RuntimeError, match="read-only"):
        await read

    [detached] = backend._detached_reads
    assert detached.document is doc_b, "filed against the document it reads"
    assert (await backend.system_status())["detached_reads"][0]["document_active"] is True
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    with pytest.raises(DocumentBusyError):
        await backend.entity_create_line(0, 0, 1, 1)
    # The document the read does NOT hold is free.
    await asyncio.wait_for(backend.document_activate(key_a), timeout=AT_ONCE)
    assert backend._doc is doc_a
    assert await backend._async(lambda: "write on A") == "write on A"
    assert not reader.finished.is_set(), "the read of B is still running"
    reader.release.set()
    await _wait_for(reader.finished)


async def test_a_cancelled_read_keeps_its_original_deadline(backend, monkeypatch, gates, tmp_path):
    """RED: a cancelled read-only call got no deadline timer, so it held the
    lock with nothing ever recorded or detached."""
    path = str(tmp_path / "read.dxf")
    await backend.drawing_save_as(path)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", RACED_DEADLINE)
    gate = gates()
    task = asyncio.ensure_future(backend._async(gate.body(), readonly=True))
    await _wait_for(gate.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = backend.call_outcomes[-1]
    assert row["outcome"] == "cancelled_running"
    await _until(lambda: row["outcome"] != "cancelled_running", timeout=3)
    assert row["outcome"] == "readonly_overrun"
    assert row["deadline"] == pytest.approx(row["started"] + RACED_DEADLINE, abs=0.05)
    assert backend._quarantine is None

    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    with pytest.raises(DocumentBusyError):
        await backend._async(lambda: "write")
    result = await asyncio.wait_for(backend.drawing_open(path), timeout=AT_ONCE)
    assert result["ok"] is True
    assert await backend._async(lambda: "write") == "write"
    gate.release.set()
    await _until(lambda: row["ended"] is not None)


async def test_waiters_behind_a_read_are_answered_when_it_detaches(backend, monkeypatch, gates):
    """Calls already queued behind the read when its deadline passes get the
    named refusal at once instead of their own timeout's 'busy'."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", RACED_DEADLINE)
    gate = gates()
    held = backend._lock
    read = asyncio.ensure_future(backend._async(gate.body(), readonly=True))
    await _wait_for(gate.entered)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    waiter = asyncio.ensure_future(backend._async(lambda: "write"))
    await _until(lambda: _queued_on(held) == 1)
    with pytest.raises(RuntimeError, match="read-only"):
        await read
    detached = time.perf_counter()
    with pytest.raises(DocumentBusyError):
        await asyncio.wait_for(waiter, timeout=5)
    assert time.perf_counter() - detached < AT_ONCE
    gate.release.set()
    await _wait_for(gate.finished)
    assert await backend._async(lambda: "after") == "after"


async def test_closing_the_reads_document_is_an_exit(backend, monkeypatch, gates):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()
    await backend.drawing_new()  # a second document to fall back to
    with pytest.raises(RuntimeError, match="read-only"):
        await backend._async(gate.body(), readonly=True)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    closed = await asyncio.wait_for(backend.document_close(discard=True), timeout=AT_ONCE)
    assert closed["ok"] is True
    assert await backend._async(lambda: "other document") == "other document"
    assert not gate.finished.is_set()


async def test_a_readonly_overrun_at_timeout_zero_is_never_detached(backend, monkeypatch, gates):
    """EZDXF_CALL_TIMEOUT=0: no deadline, so a cancelled read keeps its lease
    until its worker ends, as Task 1 specified - nothing is detached."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0)
    gate = gates()
    task = asyncio.ensure_future(backend._async(gate.body(), readonly=True))
    await _wait_for(gate.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend._detached_reads == []
    gate.release.set()
    await _until(lambda: backend.call_outcomes[-1]["outcome"] == "completed_after_cancel")
    assert await backend._async(lambda: "next") == "next"


async def test_disconnect_forgets_a_detached_read(backend, monkeypatch, gates):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    monkeypatch.setattr(config.settings, "shutdown_grace", 0.05)
    gate = gates()
    with pytest.raises(RuntimeError, match="read-only"):
        await backend._async(gate.body(), readonly=True)
    started = time.perf_counter()
    await backend.disconnect()
    assert time.perf_counter() - started < 0.05 + AT_ONCE
    assert backend._detached_reads == []
    await backend.connect()
    await backend.drawing_new()
    assert await backend._async(lambda: "fresh") == "fresh"


async def test_anyio_cancel_scope_reaches_a_detached_read_refusal(backend, monkeypatch, gates):
    """The refusal is an ordinary exception: a caller inside an anyio scope
    gets it, not a hang."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()
    with pytest.raises(RuntimeError, match="read-only"):
        await backend._async(gate.body(), readonly=True)
    with anyio.fail_after(AT_ONCE):
        with pytest.raises(DocumentBusyError):
            await backend.entity_count()


async def test_a_client_sees_a_busy_refusal_as_its_own_kind(monkeypatch, gates):
    """Through the server: the detached read is reported as ``kind: "busy"``
    (not as an untrusted, quarantined document), in a tool result and in a
    cad_batch step row, and system_status names it."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    gate = gates()

    async def slow_info(self):
        return await self._async(gate.body(), readonly=True)

    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "drawing_info", slow_info)
        first = await client.call_tool("drawing_info", {}, raise_on_error=False)
        assert first.is_error
        monkeypatch.undo()
        monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
        line = {"x1": 0, "y1": 0, "x2": 1, "y2": 1}
        refused = await client.call_tool("entity_create_line", line, raise_on_error=False)
        assert refused.is_error
        payload = refused.structured_content
        assert payload["kind"] == "busy" and payload["detached_read"]["call"] == "body"
        batch = await client.call_tool(
            "cad_batch", {"steps": [{"tool": "entity_create_line", "args": line}]}
        )
        [row] = batch.structured_content["results"]
        assert row["error"]["kind"] == "busy", row
        status = (await client.call_tool("system_status", {})).structured_content
        assert [read["call"] for read in status["detached_reads"]] == ["body"]
        gate.release.set()
        await _wait_for(gate.finished)


# ── finding 3: the cancelled cad_batch's rollback answer ───────────────────


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": False, "error": "No active transaction to rollback"},
        RuntimeError("rollback blew up"),
    ],
    ids=["declined", "raised"],
)
async def test_a_cancelled_batchs_rollback_answer_is_checked(monkeypatch, caplog, answer):
    """RED: the shielded rollback in the cancellation path discarded the
    backend's answer, so a cancelled batch whose rollback did nothing left no
    trace. Checked like ``_batch_transaction_ok`` checks the normal path:
    logged as a warning and recorded in call_outcomes with the request id."""
    in_step = asyncio.Event()

    async def stuck_circle(self, *args, **kwargs):
        in_step.set()
        await asyncio.Event().wait()

    async def answering_rollback(self):
        if isinstance(answer, BaseException):
            raise answer
        return dict(answer)

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "entity_create_circle", stuck_circle)
    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "transaction_rollback", answering_rollback)
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        request_id = client.session._request_id
        call = asyncio.ensure_future(
            client.call_tool(
                "cad_batch",
                {
                    "steps": [
                        {"tool": "entity_create_circle", "args": {"cx": 0, "cy": 0, "radius": 5}}
                    ],
                    "on_error": "rollback",
                },
            )
        )
        with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
            await in_step.wait()
            await client.cancel(request_id)
            with pytest.raises(McpError, match="cancelled"):
                await call

            async def recorded():
                status = (await client.call_tool("system_status", {})).structured_content
                return [r for r in status["call_outcomes"] if r["call"] == "cad_batch"]

            deadline = time.monotonic() + 10
            rows = await recorded()
            while not rows and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
                rows = await recorded()
        [row] = rows
        expected = "rollback_declined" if isinstance(answer, dict) else "rollback_failed"
        assert row["outcome"] == expected
        assert row["request_id"] == str(request_id)
        detail = "No active transaction" if isinstance(answer, dict) else "rollback blew up"
        assert detail in row["error"]
        assert row["started"] <= row["ended"]
        lines = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "cad_batch" in line and str(request_id) in line and detail in line for line in lines
        ), lines
