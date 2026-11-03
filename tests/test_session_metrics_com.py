"""1.8-14: the live engine's ``_run`` breakdown, on a fake seat.

Each COM job is split into queue (submitted -> started on the STA thread),
guard (the bound-document check, without the operator-edit counter inside it),
work (the job's own code, without its display refreshes), refresh (``_regen``)
and counter (the operator-edit counter at the start and the end of the job).
``system_status(metrics=True)`` reports it; no AutoCAD is touched - ``_run`` is
the real one on a plain daemon thread, as in tests/test_com_space.py.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("win32com.client", reason="the COM backend module needs pywin32")

import server  # noqa: E402
from backends import com_backend as cb  # noqa: E402
from backends.quarantine import DaemonExecutor  # noqa: E402


class _Doc:
    def __init__(self):
        self.Name = "Sheet.dwg"
        self.FullName = "C:\\work\\Sheet.dwg"
        self.regens = 0

    def Regen(self, _which):
        time.sleep(0.02)
        self.regens += 1


class _Docs:
    def __init__(self, doc):
        self._docs = [doc]

    @property
    def Count(self):
        return len(self._docs)


class _Seat:
    HWND = 4242

    def __init__(self):
        self.doc = _Doc()
        self.Documents = _Docs(self.doc)
        self.ActiveDocument = self.doc


@pytest.fixture
def com(monkeypatch):
    seat = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: seat)

    def _slow_counter(self, app):
        time.sleep(0.03)

    monkeypatch.setattr(cb.ComBackend, "_edit_counter_start", _slow_counter)
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend, seat
    backend._executor.shutdown(wait=False)
    backend._executor = None


@pytest.mark.asyncio
async def test_the_run_breakdown_splits_a_job_into_its_parts(com):
    backend, seat = com

    def _job():
        cb._acad_doc()  # the guard (and, inside it, the operator-edit counter)
        time.sleep(0.04)  # the work
        cb._regen()  # the display refresh
        return "done"

    assert await backend._run(_job) == "done"
    assert seat.doc.regens == 1
    breakdown = backend.run_metrics()
    assert breakdown["jobs"] == 1
    assert set(breakdown["ms"]) == {"queue", "guard", "work", "refresh", "counter"}
    ms = {bucket: row["total"] for bucket, row in breakdown["ms"].items()}
    assert ms["work"] >= 35.0
    assert ms["refresh"] >= 15.0
    assert ms["counter"] >= 25.0
    # the counter is not counted again as guard, the refresh not again as work
    assert ms["guard"] < 25.0
    assert ms["work"] < 40.0 + 15.0
    for row in breakdown["ms"].values():
        assert row["p50"] <= row["p95"]


@pytest.mark.asyncio
async def test_the_breakdown_is_reported_under_the_metrics_flag(com):
    backend, _seat = com
    await backend._run(lambda: cb._acad_doc() and None)
    payload = server._metrics_payload(None, backend)
    assert payload["com_run"]["jobs"] == 1
    assert payload["com_run_reason"] is None


def test_a_job_that_fails_is_still_timed(com):
    backend, _seat = com

    def _boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        asyncio.run(backend._run(_boom))
    assert backend.run_metrics()["jobs"] == 1
