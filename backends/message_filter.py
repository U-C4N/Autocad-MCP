"""A client-side COM message filter for the live engine's STA thread.

**Why.** AutoCAD's own message filter answers a call it cannot take now -
right after ``Documents.Add`` / ``Open`` / ``Close``, during a long regen,
while a FILLET finishes - with "retry later", and a client without a message
filter turns that at once into ``RPC_E_CALL_REJECTED``: the call did not run.
pywin32 has no ``CoRegisterMessageFilter``. Without one, every rejection
reached the code as an error: a read was waited out by hand
(``_wait_out_rejected_call``), but a rejection in the middle of a write could
only fail the whole call, because a job that may have written cannot be run
again. MEASURED on AutoCAD 2026 (2026-11-04): ``entity_create_line`` inside
``apply_titleblock`` (smoke ``mech``) and one ``entity_fillet`` in a
5,000-line drawing (``benchmarks/perf_com.py``) failed that way.

**What.** ``IMessageFilter`` implemented with ``ctypes`` and registered on the
STA thread by ``install()`` (``com_backend._com_init``). COM then calls
``RetryRejectedCall`` for a rejected outgoing call, and the filter asks for
the SAME call again after ``RETRY_PAUSE_MS`` while the callee answers
``SERVERCALL_RETRYLATER`` and less than ``RETRY_BUDGET_MS`` has passed since
the first attempt. Retrying is safe: a rejected call never ran. An outright
``SERVERCALL_REJECTED`` is not retried (it is not a busy moment), and once the
budget is spent the caller gets ``RPC_E_CALL_REJECTED`` exactly as before, so
every existing handler keeps its meaning. Incoming calls are accepted
(``SERVERCALL_ISHANDLED``) and pending messages get the default processing
(``PENDINGMSG_WAITDEFPROCESS``) - the filter changes nothing but the retry.

``stats`` counts what the filter saw (calls retried, refusals passed on), for
evidence; nothing reads it to decide anything.

``com_backend._com_init`` installs the filter on the STA thread right after
``CoInitialize`` and ``com_backend._com_teardown`` removes it (``uninstall()``)
before ``CoUninitialize``; off Windows, or when registration fails, both are
no-ops and every call behaves as it did before.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading

log = logging.getLogger(__name__)

__all__ = [
    "RETRY_BUDGET_MS",
    "RETRY_PAUSE_MS",
    "install",
    "installed",
    "retry_decision",
    "stats",
    "uninstall",
]

#: How long one rejected call is retried, in ms since its first attempt (COM's
#: ``dwTickCount``). Well inside ``COM_CALL_TIMEOUT`` (60 s).
RETRY_BUDGET_MS = 15_000
#: The pause before each retry; >= 100 means "wait this long, then retry".
RETRY_PAUSE_MS = 100

SERVERCALL_ISHANDLED = 0
SERVERCALL_REJECTED = 1
SERVERCALL_RETRYLATER = 2
PENDINGMSG_WAITDEFPROCESS = 2
_CANCEL = 0xFFFFFFFF  # (DWORD)-1: give the call up

S_OK = 0
E_NOINTERFACE = -2147467262  # 0x80004002

#: What the filter saw, per process: ``{"retried": n, "given_up": n, "rejected": n}``.
stats: dict[str, int] = {"retried": 0, "given_up": 0, "rejected": 0}

_installed = threading.local()
#: Every filter ever registered, kept for the life of the process: COM may
#: call ``Release`` on a filter it no longer uses at any later point (when it
#: is replaced, at ``CoUninitialize``), and a ctypes callback that has been
#: freed is a jump into unmapped memory. A filter is a few hundred bytes and a
#: process installs one per STA thread, so nothing is ever freed on purpose.
_retired: list = []


def retry_decision(reject_type: int, tick_count: int) -> int:
    """``RetryRejectedCall``'s answer: a pause in ms to retry, or ``_CANCEL``."""
    if reject_type != SERVERCALL_RETRYLATER:
        stats["rejected"] += 1
        return _CANCEL
    if tick_count >= RETRY_BUDGET_MS:
        stats["given_up"] += 1
        return _CANCEL
    stats["retried"] += 1
    return RETRY_PAUSE_MS


def _build():  # pragma: no cover - Windows only, exercised by install()
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def guid(text: str) -> GUID:
        value = GUID()
        ctypes.oledll.ole32.IIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value))
        return value

    iid_unknown = bytes(guid("{00000000-0000-0000-C000-000000000046}"))
    iid_filter = bytes(guid("{00000016-0000-0000-C000-000000000046}"))

    query = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
    )
    count = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    incoming = ctypes.WINFUNCTYPE(
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    retry = ctypes.WINFUNCTYPE(
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD
    )
    pending = ctypes.WINFUNCTYPE(
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD
    )

    class Vtbl(ctypes.Structure):
        _fields_ = [
            ("QueryInterface", query),
            ("AddRef", count),
            ("Release", count),
            ("HandleInComingCall", incoming),
            ("RetryRejectedCall", retry),
            ("MessagePending", pending),
        ]

    class Filter(ctypes.Structure):
        _fields_ = [("lpVtbl", ctypes.POINTER(Vtbl))]

    def _query(this, riid, ppv):
        if bytes(riid.contents) in (iid_unknown, iid_filter):
            ppv[0] = this
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(this):
        return 1  # a static object: it lives as long as the thread's reference

    def _release(this):
        return 1

    def _incoming(this, call_type, caller, tick_count, info):
        return SERVERCALL_ISHANDLED

    def _retry(this, callee, tick_count, reject_type):
        try:
            return retry_decision(int(reject_type), int(tick_count))
        except Exception:  # noqa: BLE001 - never raise into COM
            return _CANCEL

    def _pending(this, callee, tick_count, pending_type):
        return PENDINGMSG_WAITDEFPROCESS

    vtbl = Vtbl(
        query(_query),
        count(_add_ref),
        count(_release),
        incoming(_incoming),
        retry(_retry),
        pending(_pending),
    )
    instance = Filter(ctypes.pointer(vtbl))
    return instance, vtbl


def install() -> bool:
    """Register the filter on the calling (STA) thread; True when it is in place.

    Idempotent per thread. Off Windows, or when registration fails, nothing
    changes and the caller keeps today's behaviour (False).
    """
    if sys.platform != "win32":
        return False
    if getattr(_installed, "keep", None) is not None:
        return True
    try:
        instance, vtbl = _build()
        previous = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoRegisterMessageFilter(
            ctypes.byref(instance), ctypes.byref(previous)
        )
    except Exception as exc:  # noqa: BLE001 - the filter is an improvement, not a requirement
        log.debug("COM message filter not installed: %s", exc)
        return False
    if hr != S_OK:
        log.debug("CoRegisterMessageFilter answered %#010x", hr & 0xFFFFFFFF)
        return False
    _installed.keep = (instance, vtbl)  # the callbacks must outlive every call
    return True


def installed() -> bool:
    """True while this thread's filter is registered."""
    return getattr(_installed, "keep", None) is not None


def uninstall() -> bool:
    """Remove the calling thread's filter again; True when one was in place.

    Called by ``com_backend._com_teardown`` before ``CoUninitialize``. A thread
    that never installed one (or already removed it) answers False and nothing
    is touched.
    """
    if not installed():
        return False
    # Unregister first, and keep the objects afterwards (``_retired``): the
    # unregistration itself, and the apartment's teardown later, may call the
    # filter's ``Release`` - through a vtable that must still be there.
    try:
        previous = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoRegisterMessageFilter(None, ctypes.byref(previous))
    except Exception as exc:  # noqa: BLE001 - the apartment is going away anyway
        log.debug("COM message filter not removed: %s", exc)
        return False
    finally:
        _retired.append(_installed.keep)
        _installed.keep = None
    if hr != S_OK:
        log.debug("CoRegisterMessageFilter(NULL) answered %#010x", hr & 0xFFFFFFFF)
        return False
    return True
