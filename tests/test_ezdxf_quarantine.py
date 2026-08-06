"""R32 — a timed-out ezdxf call must quarantine the DOCUMENT, not just the lock.

``EzdxfBackend._async`` cannot cancel an ``asyncio.to_thread`` worker. When a
call overruns ``EZDXF_CALL_TIMEOUT`` the handler swaps in a fresh
``asyncio.Lock`` so later callers do not deadlock behind the orphaned one — and
that swap is precisely what removes the mutual exclusion the lock existed for.
The runaway thread still holds the one shared ``ezdxf.Drawing``, which is
explicitly not thread-safe.

Measured against the unfixed backend, with a runaway ``_sync`` that really
appends LINEs to ``backend._doc``:

* ``entity_create_circle`` after the abandonment returned handle ``A1`` —
  full success, no warning.
* ``drawing_info`` reported ``entity_count=116`` for a modelspace observed at
  113 -> 118 -> 302 under the same caller, finishing at 310.
* ``drawing_save`` returned ``{"ok": True}`` and wrote a valid DXF holding
  15200 of the 60000 entities the abandoned call would go on to write. It reads
  back fine. It is a considered document of a state that never existed.
* ``drawing_info`` iterating a container another thread appends to does not
  tear loudly — it ran unbounded, blew its own 2.0s deadline at 2.03s and
  abandoned a SECOND worker with a SECOND orphaned lock. That is why reads are
  refused too.

Only the timed-out caller was ever warned. Every later caller was told nothing.

Every runaway helper here is bounded AND sets its ``finished`` Event in a
``finally``: asyncio joins the default ``to_thread`` executor at loop shutdown,
so a still-spinning worker hangs the pytest process rather than failing a test.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import ezdxf
import pytest
import pytest_asyncio
from fastmcp import Client

import config
import server
from backends.ezdxf_backend import EzdxfBackend
from backends.quarantine import DocumentQuarantineError, QuarantineRecord, real_call_name

pytestmark = pytest.mark.asyncio


# ── runaway helpers ─────────────────────────────────────────────────────────


class _Runaway:
    """A ``_sync`` that outlives its deadline, with a guaranteed way to stop it."""

    def __init__(self):
        self.started = threading.Event()
        self.finished = threading.Event()
        self.stop = threading.Event()
        self.written = 0

    def writer(self, backend, *, delay: float = 0.0005, limit: int = 20_000):
        """Really appends LINEs to the shared Drawing, like a real ``_sync`` would.

        The existing honesty test used ``time.sleep(2.0)`` as the stand-in,
        which touches no document — which is exactly why the race it was
        written to cover was never caught.
        """

        def _sync():
            try:
                msp = backend._doc.modelspace()
                self.started.set()
                while self.written < limit and not self.stop.is_set():
                    msp.add_line((0, 0), (1, 1))
                    self.written += 1
                    time.sleep(delay)  # releases the GIL so the event loop runs
            finally:
                self.finished.set()
            return {"ok": True, "written": self.written}

        return _sync

    def slow_but_complete(self, extra: float = 0.5, result: str = "value nobody received"):
        """A legitimately slow call that finishes cleanly just past the deadline."""

        def _sync():
            try:
                self.started.set()
                self.stop.wait(extra)
            finally:
                self.finished.set()
            return result

        return _sync

    def slow_then_raises(self, extra: float = 0.5):
        def _sync():
            try:
                self.started.set()
                self.stop.wait(extra)
                raise ValueError("died mid-write")
            finally:
                self.finished.set()

        return _sync


@pytest.fixture
def runaways():
    """Factory for runaway calls; every one is stopped and joined on teardown."""
    made: list[_Runaway] = []

    def make() -> _Runaway:
        r = _Runaway()
        made.append(r)
        return r

    yield make

    for r in made:
        r.stop.set()
    for r in made:
        if r.started.is_set():
            assert r.finished.wait(15), "a runaway worker outlived its test"


async def _abandon(backend, monkeypatch, func, *, timeout: float = 0.3) -> RuntimeError:
    """Run ``func`` past a short deadline and return the deadline error."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", timeout)
    with pytest.raises(RuntimeError) as excinfo:
        await backend._async(func)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    return excinfo.value


# ── the quarantine ──────────────────────────────────────────────────────────


async def test_a_timed_out_write_quarantines_the_document(backend, monkeypatch, runaways):
    """RED: this returned handle 'A1' off a modelspace another thread was growing."""
    r = runaways()
    await _abandon(backend, monkeypatch, r.writer(backend))
    assert r.started.is_set()

    with pytest.raises(DocumentQuarantineError) as refusal:
        await backend.entity_create_circle(0, 0, 5)

    payload = refusal.value.to_dict()
    assert payload["ok"] is False
    assert payload["kind"] == "quarantined"
    assert payload["quarantine"]["call"] == "writer"
    assert payload["quarantine"]["thread_alive"] is True
    assert "drawing_open" in payload["error"], "a refusal that names no way out is not help"
    assert r.written > 0, "the abandoned thread really was mutating the document"


async def test_reads_are_refused_too(backend, monkeypatch, runaways):
    """RED: drawing_info reported entity_count=116 for a space at 302 and climbing."""
    r = runaways()
    await _abandon(backend, monkeypatch, r.writer(backend))

    with pytest.raises(DocumentQuarantineError):
        await backend.drawing_info()
    with pytest.raises(DocumentQuarantineError):
        await backend.entity_list()
    with pytest.raises(DocumentQuarantineError):
        await backend.analysis_stats()


async def test_a_read_does_not_cascade_a_second_abandonment(backend, monkeypatch, runaways, caplog):
    """RED: a read on the growing document blew its own deadline and abandoned again.

    Measured at 2.03s against a 2.0s deadline, with a second lock swap. Reads on
    a container another thread appends to do not tear loudly — they never finish.
    """
    r = runaways()
    await _abandon(backend, monkeypatch, r.writer(backend))
    quarantined_lock = backend._lock

    caplog.clear()  # the first abandonment is expected; a second one is the defect
    with caplog.at_level(logging.ERROR, logger="backends.ezdxf_backend"):
        with pytest.raises(DocumentQuarantineError):
            await backend.drawing_info()

    assert backend._lock is quarantined_lock, "a refusal must not swap the lock again"
    assert not [rec for rec in caplog.records if "abandoning the worker" in rec.getMessage()]
    assert len(backend._abandoned_calls) == 1


async def test_save_is_refused_while_quarantined(backend, monkeypatch, runaways, tmp_path):
    """RED: this wrote a valid DXF of 15200 of 60000 entities and said {'ok': True}."""
    r = runaways()
    await _abandon(backend, monkeypatch, r.writer(backend))

    target = tmp_path / "torn.dxf"
    with pytest.raises(DocumentQuarantineError):
        await backend.drawing_save(str(target))
    assert not target.exists(), "a refusal must not leave a torn document on disk"


# ── the ways back ───────────────────────────────────────────────────────────


async def test_drawing_open_is_the_way_back(backend, monkeypatch, runaways, tmp_path):
    good = tmp_path / "known_good.dxf"
    doc = ezdxf.new("R2010")
    for i in range(7):
        doc.modelspace().add_line((0, i), (1, i))
    doc.saveas(str(good))

    r = runaways()
    await _abandon(backend, monkeypatch, r.writer(backend))

    # Reopen WHILE the runaway is still writing: that is the whole point.
    assert not r.finished.is_set()
    result = await backend.drawing_open(str(good))
    assert result["ok"] is True
    assert result["quarantine_cleared"]["call"] == "writer"
    assert backend._quarantine is None

    written_before = r.written
    info = await backend.drawing_info()
    assert info.entity_count == 7

    # Let the runaway write more, then prove none of it landed here.
    while r.written < written_before + 20 and not r.finished.is_set():
        await asyncio.sleep(0.01)
    assert (await backend.drawing_info()).entity_count == 7
    assert (await backend.entity_create_circle(0, 0, 5)).handle


async def test_drawing_new_and_drawing_close_also_clear_it(backend, monkeypatch, runaways):
    first = runaways()
    await _abandon(backend, monkeypatch, first.writer(backend))
    assert (await backend.drawing_new())["quarantine_cleared"]["call"] == "writer"
    assert backend._quarantine is None
    assert (await backend.entity_create_line(0, 0, 1, 1)).handle

    second = runaways()
    await _abandon(backend, monkeypatch, second.writer(backend))
    closed = await backend.drawing_close(save=True)
    assert closed["ok"] is True
    assert closed["saved"] is False, "a quarantined document must not be saved on the way out"
    assert backend._quarantine is None
    await backend.drawing_new()
    assert (await backend.entity_create_line(0, 0, 1, 1)).handle


async def test_undo_and_rollback_are_not_a_way_back(backend, monkeypatch, runaways):
    """They rebind ``self._doc`` too, but from a snapshot that may itself be torn.

    ``_snapshot_doc`` does ``self._doc.saveas(tmp)`` on the LIVE document, so any
    snapshot pushed during the runaway window was serialised mid-mutation.
    """
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 5)
    r = runaways()
    await _abandon(backend, monkeypatch, r.writer(backend))

    with pytest.raises(DocumentQuarantineError):
        await backend.drawing_undo()
    with pytest.raises(DocumentQuarantineError):
        await backend.drawing_redo()
    with pytest.raises(DocumentQuarantineError):
        await backend.transaction_rollback()
    assert backend._quarantine is not None


# ── the free exit ───────────────────────────────────────────────────────────


async def test_a_slow_but_complete_call_lifts_the_quarantine(backend, monkeypatch, runaways):
    """The common real trigger is a slow drawing_audit, not a hang.

    If the ``_sync`` body ran to completion the document holds exactly the state
    that call intended — and everybody else was refused in the meantime, which
    is the only reason that inference is sound.
    """
    r = runaways()
    await _abandon(backend, monkeypatch, r.slow_but_complete(0.5))
    assert backend._quarantine is not None
    assert r.finished.wait(5)

    circle = await backend.entity_create_circle(0, 0, 5)
    assert circle.handle, "no reopen should be needed for a call that simply ran long"

    status = await backend.system_status()
    assert status["quarantine"] is None
    (record,) = status["abandoned_calls"]
    assert record["outcome"] == "completed_after_deadline"
    assert record["result_lost"] is True, "the caller never received the return value"
    assert "value nobody received" in record["lost_return_value"]


async def test_a_call_that_raises_after_the_deadline_stays_quarantined(
    backend, monkeypatch, runaways
):
    r = runaways()
    await _abandon(backend, monkeypatch, r.slow_then_raises(0.5))
    assert r.finished.wait(5)

    with pytest.raises(DocumentQuarantineError):
        await backend.entity_create_circle(0, 0, 5)
    status = await backend.system_status()
    assert status["quarantine"]["outcome"] == "failed_after_deadline"
    assert "died mid-write" in status["quarantine"]["detail"]


# ── the diagnostic channel ──────────────────────────────────────────────────


async def test_system_status_reports_the_quarantine_and_is_never_itself_refused(
    backend, monkeypatch, runaways
):
    """Otherwise the quarantine's first act is to close the only way to ask why."""
    r = runaways()
    await _abandon(backend, monkeypatch, r.writer(backend))

    status = await backend.system_status()
    record = status["quarantine"]
    assert record["call"] == "writer"
    assert record["timeout"] == pytest.approx(0.3)
    assert record["age_seconds"] >= 0
    assert record["thread_alive"] is True
    assert record["outcome"] == "running"


# ── the message ─────────────────────────────────────────────────────────────


async def test_the_timeout_message_names_the_real_call(backend, monkeypatch, runaways, tmp_path):
    """RED: every real backend method passes a closure literally named ``_sync``.

    Measured: ``__name__='_sync'``, ``__qualname__='EzdxfBackend.drawing_save.
    <locals>._sync'`` — so in production the error always read
    ``ezdxf call '_sync' timed out``. The qualname carries the name the user
    typed and it was being discarded.
    """
    r = runaways()

    def _slow_saveas(path, *args, **kwargs):
        r.started.set()
        try:
            r.stop.wait(5)
        finally:
            r.finished.set()

    monkeypatch.setattr(backend._doc, "saveas", _slow_saveas)
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.3)
    with pytest.raises(RuntimeError) as excinfo:
        await backend.drawing_save(str(tmp_path / "part.dxf"))

    message = str(excinfo.value)
    assert "drawing_save" in message
    assert "_sync" not in message
    assert "drawing_open" in message, "the message must name the way back"
    assert backend._quarantine.call == "drawing_save"


async def test_real_call_name_falls_back_when_there_is_no_enclosing_method():
    assert real_call_name(len) == "len"
    assert real_call_name(object()) == "call"


# ── it is not a capability refusal ──────────────────────────────────────────


async def test_the_refusal_is_not_reported_as_a_capability():
    """``_batch_capability_of`` classifies structurally, so a borrowed
    ``.capability`` here would tell the client the ezdxf engine cannot draw a
    circle. The engine is fine; the document is not."""
    record = QuarantineRecord("drawing_save", 120.0, _FinishedWatcher())
    exc = DocumentQuarantineError(record, "quarantined")

    assert not hasattr(exc, "capability")
    assert server._batch_capability_of(exc) is None
    assert server._classify_batch_error(exc)["kind"] == "quarantined"


class _FinishedWatcher:
    finished = True
    result = None
    error = None


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def test_the_wire_payload_carries_kind_quarantined(client, monkeypatch):
    record = QuarantineRecord("drawing_audit", 120.0, _FinishedWatcher())

    async def _refuse(self, *args, **kwargs):
        raise DocumentQuarantineError(record, "document quarantined; use drawing_open()")

    monkeypatch.setattr(EzdxfBackend, "entity_create_circle", _refuse)

    result = await client.call_tool(
        "entity_create_circle", {"cx": 0, "cy": 0, "radius": 5}, raise_on_error=False
    )
    assert result.is_error is True
    payload = result.structured_content
    assert payload["kind"] == "quarantined"
    assert "capability" not in payload
    assert payload["quarantine"]["call"] == "drawing_audit"
    assert payload["tool"] == "entity_create_circle"
    assert payload["backend"] == "ezdxf"
