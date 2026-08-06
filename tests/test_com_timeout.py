"""``ComBackend._run`` deadline honesty — the STA executor must only be rebuilt
when *our* deadline actually expired.

On Python 3.11+ ``TimeoutError`` **is** ``asyncio.TimeoutError`` (and an
``OSError`` subclass), so a wrapped COM call that raises ``TimeoutError`` itself
— a socket read against a dead network share, a helper that re-raises one —
arrives at ``except TimeoutError`` looking exactly like a blown deadline. Taking
it at face value told the user AutoCAD "did not respond within 60s" for a call
that failed instantly, and (far worse than the ezdxf equivalent) threw away the
live single-thread STA executor and the cached AutoCAD connection with it.

These tests exercise the real ``_run`` — a plain ``ThreadPoolExecutor`` stands in
for the COM apartment, no AutoCAD and no win32 calls involved.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import backends.com_backend as cb
import config
from backends.com_backend import ComBackend

pytestmark = pytest.mark.asyncio


@pytest.fixture
def com_backend():
    """A ComBackend wired to a real (AutoCAD-free) single-thread executor."""
    b = ComBackend()
    # No initializer: _com_init would CoInitialize a real STA apartment.
    b._executor = ThreadPoolExecutor(max_workers=1)
    b._connected = True
    yield b
    if b._executor is not None:
        b._executor.shutdown(wait=False)
        b._executor = None


def _dead_share():
    """Stand-in for a COM/file call that raises TimeoutError of its own accord."""
    raise TimeoutError("the network share went away")


async def test_call_raising_TimeoutError_is_not_blamed_on_the_deadline(com_backend, monkeypatch):
    """A TimeoutError *from the call* is an ordinary failure, not our deadline."""
    monkeypatch.setattr(config.settings, "com_call_timeout", 30)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="network share") as excinfo:
        await com_backend._run(_dead_share)

    assert time.monotonic() - started < 5, "it failed instantly; nothing waited 30s"
    assert "did not respond" not in str(excinfo.value)
    assert not isinstance(excinfo.value, RuntimeError), "the original error must survive"


async def test_call_raising_TimeoutError_keeps_the_sta_executor(com_backend, monkeypatch):
    """Rebuilding the apartment on an ordinary error drops a live AutoCAD session."""
    monkeypatch.setattr(config.settings, "com_call_timeout", 30)
    monkeypatch.setitem(cb._COM_STATE, "app", object())
    live = com_backend._executor

    with pytest.raises(TimeoutError):
        await com_backend._run(_dead_share)

    assert com_backend._executor is live, "the STA executor must not be rebuilt"
    assert com_backend._connected is True, "the AutoCAD connection must not be dropped"
    assert "app" in cb._COM_STATE, "the cached COM app must survive an ordinary error"
    assert await asyncio.wait_for(com_backend._run(lambda: "next"), timeout=5) == "next"


async def test_disabled_timeout_still_surfaces_a_callable_TimeoutError(com_backend, monkeypatch):
    """With COM_CALL_TIMEOUT=0 there is no deadline at all to blame."""
    monkeypatch.setattr(config.settings, "com_call_timeout", 0)
    live = com_backend._executor

    with pytest.raises(TimeoutError, match="network share"):
        await com_backend._run(_dead_share)

    assert com_backend._executor is live
    assert com_backend._connected is True


async def test_genuine_deadline_still_rebuilds_the_executor(com_backend, monkeypatch):
    """The real overrun path is deliberate and must keep working: the worker is
    stuck inside an uncancellable COM call, so it is abandoned and replaced."""
    monkeypatch.setattr(config.settings, "com_call_timeout", 0.2)
    monkeypatch.setitem(cb._COM_STATE, "app", object())
    stuck = com_backend._executor
    entered = threading.Event()

    def _hang():
        entered.set()
        time.sleep(2.0)
        return "too late"

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="did not respond") as excinfo:
        await com_backend._run(_hang)

    assert entered.is_set()
    assert time.monotonic() - started < 1.5, "must abandon the call, not wait it out"
    assert isinstance(excinfo.value.__cause__, TimeoutError)
    assert com_backend._executor is not stuck, "the stuck apartment must be replaced"
    assert com_backend._connected is False
    assert "app" not in cb._COM_STATE, "the cached app belongs to the abandoned thread"

    # The anti-deadlock guarantee: the next call runs on the fresh executor
    # instead of queueing behind the still-sleeping worker.
    monkeypatch.setattr(config.settings, "com_call_timeout", 10)
    assert await asyncio.wait_for(com_backend._run(lambda: "fresh"), timeout=3) == "fresh"


async def test_ordinary_errors_are_unchanged(com_backend, monkeypatch):
    """Sanity: non-timeout failures never touched the executor and still don't."""
    monkeypatch.setattr(config.settings, "com_call_timeout", 5)
    live = com_backend._executor

    def _boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await com_backend._run(_boom)

    assert com_backend._executor is live
    assert await com_backend._run(lambda x: x * 2, 21) == 42
