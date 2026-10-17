"""An OLE message filter for the COM thread: retry what AutoCAD refuses while busy.

When AutoCAD is busy — a command still running, a document switching, a regen,
an operator half-way through a prompt — its message filter answers an incoming
COM call with ``SERVERCALL_RETRYLATER``. With no filter on the *calling*
thread, COM turns that straight into ``RPC_E_CALL_REJECTED`` ("Call was
rejected by callee"): the call did not run, and nothing retried it.

That was the root of two field failures (report of 2026-10-18, AutoCAD 2026):

* 1–6 of 72 ``cad_batch`` steps failed at random on an idle seat;
* a step reported ``failed`` *after* its entity existed, because the refused
  call was the property write that followed ``AddLine`` / ``AddHatch``; a
  retry of the step drew a duplicate, and one hatch was left on layer 0.

The remedy Microsoft documents for automation clients is
``CoRegisterMessageFilter`` with an ``IMessageFilter`` whose
``RetryRejectedCall`` returns a delay: COM then re-sends *that one call*
itself, so ``AddLine`` and the ``Layer =`` write after it are retried
separately and nothing is ever half-done by a rejection. pywin32 exposes no
``CoRegisterMessageFilter`` (``pythoncom`` has no such member, checked on
pywin32 311), so the filter is a minimal COM object built with ``ctypes``:
a vtable of the six ``IUnknown`` + ``IMessageFilter`` methods.

A filter belongs to one STA thread. :func:`register` runs on the COM
executor thread right after ``CoInitialize`` (``_com_init``) and
:func:`revoke` before ``CoUninitialize``. The retry budget is
``COM_REJECTED_RETRY_S`` (default 15 s); ``0`` disables the filter.

The policy (:func:`retry_delay_ms`) is pure, so it is tested on every
platform; the registration needs Windows and is tested there.
"""

from __future__ import annotations

import logging
import sys
import threading
from contextlib import contextmanager

log = logging.getLogger(__name__)

#: ``SERVERCALL`` — what the callee's filter answered.
SERVERCALL_ISHANDLED = 0
SERVERCALL_REJECTED = 1
SERVERCALL_RETRYLATER = 2

#: ``PENDINGMSG`` — what to do with window messages while a call is pending.
PENDINGMSG_WAITDEFPROCESS = 2

#: ``RetryRejectedCall`` return that tells COM to give up on the call.
CANCEL_CALL = 0xFFFFFFFF


def retry_delay_ms(tick_count_ms: int, reject_type: int, budget_ms: int) -> int:
    """How long COM should wait before re-sending a refused call, or ``CANCEL_CALL``.

    ``tick_count_ms`` is the time since the call was first sent. Either reject
    type means the call did not run, so both are retried while the budget
    lasts: 100 ms apart for the first second (a busy seat usually frees up
    within a few hundred), then 250 ms. A value of 100 or more is a wait in
    milliseconds to COM; below 100 would mean "retry immediately", which is
    never wanted here.
    """
    if reject_type not in (SERVERCALL_REJECTED, SERVERCALL_RETRYLATER):
        return CANCEL_CALL
    if budget_ms <= 0 or tick_count_ms < 0 or tick_count_ms >= budget_ms:
        return CANCEL_CALL
    return 100 if tick_count_ms < 1000 else 250


class _FilterState:
    """What one registration holds alive: the ctypes object and its callbacks."""

    def __init__(self, budget_ms: int):
        self.budget_ms = budget_ms
        self.retries = 0
        self.cancelled = 0
        self.refs = 1
        self.obj = None
        self.vtable = None
        self.callbacks: tuple = ()
        self.previous = None


#: thread id → its filter state. Only the owning thread touches its entry.
_STATES: dict[int, _FilterState] = {}
_LOCK = threading.Lock()


def stats() -> dict:
    """Retries and give-ups counted by the filters registered in this process."""
    with _LOCK:
        states = list(_STATES.values())
    return {
        "registered_threads": len(states),
        "retried_calls": sum(s.retries for s in states),
        "cancelled_calls": sum(s.cancelled for s in states),
        "budget_ms": max((s.budget_ms for s in states), default=0),
    }


@contextmanager
def budget_override(budget_ms: int):
    """Use a different retry budget on this thread for the duration of the block.

    For diagnostics: ``system_status`` asks a seat that may be refusing calls
    whether it is busy, and should answer in about a second rather than wait
    out the full budget on each probe. A thread without a filter is unaffected.
    """
    state = _STATES.get(threading.get_ident())
    if state is None:
        yield
        return
    saved = state.budget_ms
    state.budget_ms = int(budget_ms)
    try:
        yield
    finally:
        state.budget_ms = saved


def register(budget_s: float) -> bool:
    """Install the filter on the calling (STA) thread. True when it is installed.

    Never raises: a filter that cannot be installed leaves the thread exactly
    as it was before v1.7 (every rejection fails at once), and says so in the
    log. Calling it twice on one thread is a no-op.
    """
    if sys.platform != "win32" or budget_s <= 0:
        return False
    tid = threading.get_ident()
    with _LOCK:
        if tid in _STATES:
            return True
    try:
        state = _build_filter(int(budget_s * 1000))
        import ctypes

        previous = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoRegisterMessageFilter(
            ctypes.byref(state.obj), ctypes.byref(previous)
        )
        if hr != 0:
            log.warning(
                "CoRegisterMessageFilter failed (hr=%#010x); no retry filter", hr & 0xFFFFFFFF
            )
            return False
        state.previous = previous
    except Exception as exc:  # pragma: no cover - depends on the host's COM
        log.warning("could not install the COM retry filter: %s", exc)
        return False
    with _LOCK:
        _STATES[tid] = state
    log.debug("COM retry filter installed on thread %s (budget %d ms)", tid, state.budget_ms)
    return True


def revoke() -> None:
    """Remove the calling thread's filter (restoring whatever was there before)."""
    if sys.platform != "win32":
        return
    tid = threading.get_ident()
    with _LOCK:
        state = _STATES.pop(tid, None)
    if state is None:
        return
    try:
        import ctypes

        restore = state.previous if state.previous else None
        ctypes.windll.ole32.CoRegisterMessageFilter(restore, None)
    except Exception as exc:  # pragma: no cover - teardown must never raise
        log.debug("CoRegisterMessageFilter revoke failed: %s", exc)


def _build_filter(budget_ms: int) -> _FilterState:
    """A COM object implementing IMessageFilter, as ctypes structures."""
    import ctypes
    from ctypes import wintypes

    HRESULT = ctypes.c_long

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def _iid(data1: int) -> bytes:
        guid = GUID(data1, 0, 0, (ctypes.c_ubyte * 8)(0xC0, 0, 0, 0, 0, 0, 0, 0x46))
        return bytes(guid)

    iid_unknown = _iid(0x00000000)
    iid_message_filter = _iid(0x00000016)

    QI = ctypes.WINFUNCTYPE(
        HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
    )
    REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    INCOMING = ctypes.WINFUNCTYPE(
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    RETRY = ctypes.WINFUNCTYPE(
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD
    )
    PENDING = ctypes.WINFUNCTYPE(
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD
    )

    class VTable(ctypes.Structure):
        _fields_ = [
            ("QueryInterface", QI),
            ("AddRef", REF),
            ("Release", REF),
            ("HandleInComingCall", INCOMING),
            ("RetryRejectedCall", RETRY),
            ("MessagePending", PENDING),
        ]

    class FilterObject(ctypes.Structure):
        _fields_ = [("lpVtbl", ctypes.POINTER(VTable))]

    state = _FilterState(budget_ms)
    E_NOINTERFACE = -2147467262  # 0x80004002

    def query_interface(this, riid, ppv):
        try:
            wanted = bytes(riid.contents)
            if wanted in (iid_unknown, iid_message_filter):
                ppv[0] = this
                state.refs += 1
                return 0
            ppv[0] = None
        except Exception:  # pragma: no cover - never let an exception cross COM
            pass
        return E_NOINTERFACE

    def add_ref(this):
        state.refs += 1
        return state.refs

    def release(this):
        # Python owns the memory (module state keeps it alive); the count is
        # kept only so COM sees a well-behaved object.
        state.refs = max(1, state.refs - 1)
        return state.refs

    def handle_incoming_call(this, call_type, caller, tick_count, interface_info):
        return SERVERCALL_ISHANDLED

    def retry_rejected_call(this, callee, tick_count, reject_type):
        try:
            delay = retry_delay_ms(int(tick_count), int(reject_type), state.budget_ms)
        except Exception:  # pragma: no cover - a raising callback must cancel, not spin
            delay = CANCEL_CALL
        if delay == CANCEL_CALL:
            state.cancelled += 1
        else:
            state.retries += 1
        return delay

    def message_pending(this, callee, tick_count, pending_type):
        return PENDINGMSG_WAITDEFPROCESS

    callbacks = (
        QI(query_interface),
        REF(add_ref),
        REF(release),
        INCOMING(handle_incoming_call),
        RETRY(retry_rejected_call),
        PENDING(message_pending),
    )
    vtable = VTable(*callbacks)
    obj = FilterObject(ctypes.pointer(vtable))
    state.callbacks = callbacks
    state.vtable = vtable
    state.obj = obj
    return state
