"""The STA thread's COM message filter retries a "retry later" answer, and nothing else.

MEASURED on AutoCAD 2026 (2026-11-04): ``entity_create_line`` inside
``apply_titleblock`` (live smoke ``mech``) and one ``entity_fillet`` in a
5,000-line drawing (``benchmarks/perf_com.py``) failed with
``RPC_E_CALL_REJECTED`` in the middle of a write. AutoCAD's own message filter
had answered SERVERCALL_RETRYLATER, and a client without a message filter of
its own turns that into an error at once, so the call never ran. pywin32 has
no ``CoRegisterMessageFilter``; ``backends/message_filter.py`` registers one
with ``ctypes`` on the STA thread. These tests hold its decision table, its
registration on a COM-initialised thread (Windows only, no AutoCAD touched)
and its wiring into the STA thread's init and teardown.
"""

from __future__ import annotations

import sys
import threading

import pytest

import config
from backends import com_backend as cb
from backends import message_filter as mf


@pytest.fixture
def counts():
    """``stats`` before the test, so each assertion is a delta."""
    return dict(mf.stats)


def _delta(before: dict[str, int]) -> dict[str, int]:
    return {key: mf.stats[key] - before[key] for key in before}


def test_retry_later_is_retried_after_the_pause(counts):
    assert mf.retry_decision(mf.SERVERCALL_RETRYLATER, 0) == mf.RETRY_PAUSE_MS
    assert _delta(counts) == {"retried": 1, "given_up": 0, "rejected": 0}


def test_the_pause_asks_com_to_wait_not_to_retry_at_once():
    """``RetryRejectedCall``: a value below 100 means "retry immediately" -
    which would spin against a busy AutoCAD; 100 or more is a wait in ms."""
    assert mf.RETRY_PAUSE_MS >= 100


def test_the_budget_ends_the_retries(counts):
    assert mf.retry_decision(mf.SERVERCALL_RETRYLATER, mf.RETRY_BUDGET_MS - 1) == mf.RETRY_PAUSE_MS
    assert mf.retry_decision(mf.SERVERCALL_RETRYLATER, mf.RETRY_BUDGET_MS) == mf._CANCEL
    assert _delta(counts) == {"retried": 1, "given_up": 1, "rejected": 0}


def test_the_budget_sits_inside_the_call_timeout():
    """Once the budget is spent the caller gets ``RPC_E_CALL_REJECTED`` as before;
    the STA job's own ``COM_CALL_TIMEOUT`` must not fire first."""
    assert mf.RETRY_BUDGET_MS / 1000 < config.settings.com_call_timeout


def test_an_outright_rejection_is_not_retried(counts):
    """SERVERCALL_REJECTED is not a busy moment (a modal dialog, a command
    prompt): it surfaces at once, so ``application_busy`` and the ESC advice
    keep their meaning."""
    assert mf.retry_decision(mf.SERVERCALL_REJECTED, 0) == mf._CANCEL
    assert mf.retry_decision(mf.SERVERCALL_ISHANDLED, 0) == mf._CANCEL
    assert _delta(counts) == {"retried": 0, "given_up": 0, "rejected": 2}


def test_off_windows_nothing_is_registered(monkeypatch):
    monkeypatch.setattr(mf.sys, "platform", "linux")
    assert mf.install() is False
    assert mf.installed() is False
    assert mf.uninstall() is False


@pytest.mark.skipif(sys.platform != "win32", reason="CoRegisterMessageFilter is COM")
def test_the_filter_registers_on_a_com_thread_and_is_removed_again():
    """The vtable is one COM accepts: registration answers S_OK on a thread
    that initialised COM, a second ``install`` is a no-op, ``uninstall``
    drops it and a second ``uninstall`` has nothing to drop. On its own
    thread, so pytest's main thread is left as it was."""
    pythoncom = pytest.importorskip("pythoncom")
    seen: dict[str, object] = {}

    def body():
        pythoncom.CoInitialize()
        try:
            seen["first"] = mf.install()
            seen["while"] = mf.installed()
            seen["again"] = mf.install()
            seen["removed"] = mf.uninstall()
            seen["after"] = mf.installed()
            seen["removed_again"] = mf.uninstall()
        except BaseException as exc:  # noqa: BLE001 - reported below
            seen["error"] = exc
        finally:
            pythoncom.CoUninitialize()

    thread = threading.Thread(target=body, name="test-com-message-filter")
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in seen, seen.get("error")
    assert seen == {
        "first": True,
        "while": True,
        "again": True,
        "removed": True,
        "after": False,
        "removed_again": False,
    }


def test_the_sta_thread_installs_the_filter_after_coinitialize_and_removes_it_before_couninitialize(
    monkeypatch,
):
    calls: list[str] = []

    class _Pythoncom:
        @staticmethod
        def CoInitialize():  # noqa: N802 - pywin32's name
            calls.append("CoInitialize")

        @staticmethod
        def CoUninitialize():  # noqa: N802
            calls.append("CoUninitialize")

    monkeypatch.setattr(cb, "_COM_IMPORTS_OK", True)
    monkeypatch.setattr(cb, "pythoncom", _Pythoncom, raising=False)
    monkeypatch.setattr(cb, "_drop_app", lambda: calls.append("drop_app"))
    monkeypatch.setattr(mf, "install", lambda: calls.append("install") or True)
    monkeypatch.setattr(mf, "uninstall", lambda: calls.append("uninstall") or True)

    cb._com_init()
    cb._com_teardown()

    assert calls == ["CoInitialize", "install", "drop_app", "uninstall", "CoUninitialize"]


def test_a_failed_registration_is_logged_and_changes_nothing(monkeypatch, caplog):
    class _Pythoncom:
        @staticmethod
        def CoInitialize():  # noqa: N802
            pass

    monkeypatch.setattr(cb, "_COM_IMPORTS_OK", True)
    monkeypatch.setattr(cb, "pythoncom", _Pythoncom, raising=False)
    monkeypatch.setattr(mf, "install", lambda: False)
    with caplog.at_level("INFO", logger=cb.log.name):
        cb._com_init()
    assert "message filter not registered" in caplog.text
