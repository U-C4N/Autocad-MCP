"""B01 — shutdown with a worker still running.

``disconnect()`` waits a bounded ``SHUTDOWN_GRACE`` for an in-flight worker and
then logs it; the server process still exits, because workers run on daemon
threads nothing joins at exit. Measured on 2026-10-24 before the fix: a 6 s
runaway held ``asyncio.run`` for 6.0 s and the process for 7.2 s after the call
had already been abandoned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import config
from backends.ezdxf_backend import EzdxfBackend

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[1]


async def test_shutdown_grace_defaults_to_five_seconds(monkeypatch):
    monkeypatch.delenv("SHUTDOWN_GRACE", raising=False)
    assert config.Settings().shutdown_grace == 5.0
    monkeypatch.setenv("SHUTDOWN_GRACE", "0.5")
    assert config.Settings().shutdown_grace == 0.5
    monkeypatch.setenv("SHUTDOWN_GRACE", "-3")
    assert config.Settings().shutdown_grace == 0.0


async def _cancel_running(backend, entered, release):
    def _sync():
        entered.set()
        assert release.wait(10)
        return "late"

    task = asyncio.ensure_future(backend._async(_sync))
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_disconnect_waits_for_a_worker_that_finishes_within_the_grace(monkeypatch, caplog):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    monkeypatch.setattr(config.settings, "shutdown_grace", 10)
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    entered, release = threading.Event(), threading.Event()
    await _cancel_running(backend, entered, release)

    closing = asyncio.ensure_future(backend.disconnect())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not closing.done(), "disconnect waits for the worker"
    release.set()
    with caplog.at_level(logging.WARNING, logger="backends.ezdxf_backend"):
        await asyncio.wait_for(closing, timeout=5)
    assert not [r for r in caplog.records if "SHUTDOWN_GRACE" in r.getMessage()]
    assert not backend._leases, "disconnect cancels pending deadline timers"


async def test_disconnect_gives_up_on_a_runaway_after_the_grace(monkeypatch, caplog):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    monkeypatch.setattr(config.settings, "shutdown_grace", 0.2)
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    entered, release = threading.Event(), threading.Event()
    try:
        await _cancel_running(backend, entered, release)
        (lease,) = backend._leases
        timer = lease.timer
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="backends.ezdxf_backend"):
            await backend.disconnect()
        assert time.monotonic() - started < 5
        assert timer.cancelled(), "the original-deadline timer must not fire later"
        assert any("SHUTDOWN_GRACE=0.2" in r.getMessage() for r in caplog.records)
    finally:
        release.set()


async def test_a_runaway_worker_does_not_hold_the_process_open(tmp_path):
    """RED: the process lived until the runaway finished (30 s here)."""
    script = tmp_path / "runaway.py"
    script.write_text(
        textwrap.dedent(
            """
            import asyncio, threading
            import config
            from backends.ezdxf_backend import EzdxfBackend

            async def main():
                config.settings.ezdxf_call_timeout = 0.2
                backend = EzdxfBackend()
                await backend.connect()
                await backend.drawing_new()
                try:
                    await backend._async(lambda: threading.Event().wait(30))
                except RuntimeError:
                    pass
                await backend.disconnect()

            asyncio.run(main())
            print("exited")
            """
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "SHUTDOWN_GRACE": "0.2"}
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "exited" in done.stdout
    assert time.monotonic() - started < 20
