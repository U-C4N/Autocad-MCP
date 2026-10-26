"""Document quarantine — the vocabulary for a call that was abandoned mid-write.

``EzdxfBackend._async`` cannot cancel an ``asyncio.to_thread`` worker. When a
call overruns ``EZDXF_CALL_TIMEOUT`` the handler swaps in a fresh
``asyncio.Lock`` so later callers do not deadlock behind the orphaned one — and
that swap is precisely what removes the mutual exclusion the lock existed for.
The runaway thread keeps a live reference to the one shared ``ezdxf.Drawing``,
which is explicitly not thread-safe, through the closure it was handed.

Measured, with a runaway ``_sync`` that really appends LINEs to ``_doc``:
``entity_create_circle`` after the abandonment returned handle ``A1``;
``drawing_info`` reported 116 entities for a modelspace observed at 302 and
climbing; ``drawing_save`` returned ``{"ok": True}`` and wrote a valid DXF of
15200 of the 60000 entities the runaway would go on to write — a considered
document of a state that never existed. Only the timed-out caller was warned.

So the abandonment quarantines the DOCUMENT, not the backend. The document stays
untrusted until it is REPLACED by an object the abandoned thread has no
reference to (``drawing_open`` / ``drawing_new`` / ``drawing_close``), or until
the abandoned call is seen to finish cleanly — see ``AbandonedCall``.

This module deliberately has no ezdxf import: ``server.py`` needs the exception
type to classify a refusal on a box where a backend's dependencies are missing.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

__all__ = [
    "AbandonedCall",
    "DaemonExecutor",
    "DetachedRead",
    "DocumentBusyError",
    "DocumentQuarantineError",
    "QuarantineRecord",
    "detached_read_refusal",
    "is_marked_readonly",
    "mark_readonly",
    "quarantine_refusal",
    "real_call_name",
]

#: Longest ``repr`` kept for a return value nobody received. A late
#: ``entity_list`` can return thousands of entities; the diagnostic is that the
#: value was lost, not what was in it.
_MAX_RESULT_REPR = 200

log = logging.getLogger(__name__)


#: The attribute :func:`mark_readonly` sets on a function handed to
#: ``EzdxfBackend._async``.
_READONLY_MARK = "__acadmcp_readonly__"


def mark_readonly(func: Callable) -> Callable:
    """Mark ``func`` as a read for ``EzdxfBackend._async`` and return it.

    The ``readonly=True`` keyword is one argument a wrapper around ``_async``
    (a monkeypatch, a decorator, a tracer) can drop; the mark travels on the
    function itself, which every wrapper has to pass on, and ``_async`` treats
    a marked function exactly as ``readonly=True``.
    """
    setattr(func, _READONLY_MARK, True)
    return func


def is_marked_readonly(func: Any) -> bool:
    return getattr(func, _READONLY_MARK, False) is True


def real_call_name(func: Any) -> str:
    """The backend method ``func`` belongs to, not the closure's own name.

    Every real backend method hands ``_async`` a closure literally named
    ``_sync`` — measured: ``__name__='_sync'``,
    ``__qualname__='EzdxfBackend.drawing_save.<locals>._sync'`` — so
    ``__name__`` made every production timeout read ``ezdxf call '_sync' timed
    out``. The qualname carries the name the user typed and was being discarded.
    """
    qualname = getattr(func, "__qualname__", "") or ""
    parts = qualname.split(".")
    if "<locals>" in parts:
        index = parts.index("<locals>")
        if index:
            return parts[index - 1]
    return getattr(func, "__name__", None) or "call"


class AbandonedCall:
    """Observation post for a worker thread ``_async`` can no longer await.

    The obvious hook does not work, and this was checked before being relied on:
    ``asyncio.timeout_at`` **cancels** the Future ``_async`` awaits, so by the
    time the abandon branch runs that Future is already done and
    ``add_done_callback`` fires immediately with ``cancelled()`` True. It reports
    the *await* ending, never the thread — which nothing can cancel. The only
    place the thread's outcome is observable is inside the thread, so
    ``_async`` wraps every call in one of these.

    v1.7.0 (B01): the document lease follows the worker thread, not the
    awaiting coroutine. When the coroutine is cancelled while the worker runs,
    ``_async`` registers a one-shot hook with ``set_on_finish`` and the worker
    calls it as it ends. ``_hook_lock`` closes the race with the worker
    finishing at that moment: either ``set_on_finish`` sees ``finished`` and
    returns False (the caller releases at once), or the worker takes the hook
    and runs it — never both, never neither. ``finished`` stays a plain bool for
    the readers that only poll it.
    """

    __slots__ = ("func", "finished", "result", "error", "_hook", "_hook_lock")

    def __init__(self, func):
        self.func = func
        self.finished = False
        self.result: Any = None
        self.error: BaseException | None = None
        self._hook: Callable[[], None] | None = None
        self._hook_lock = threading.Lock()

    def set_on_finish(self, hook: Callable[[], None]) -> bool:
        """Register ``hook`` for the worker thread to call when it ends.

        Returns False when the worker has already finished: the hook is not
        stored and the caller does the hook's work itself, now. A registered
        hook runs exactly once, on the worker thread.
        """
        with self._hook_lock:
            if self.finished:
                return False
            self._hook = hook
            return True

    def __call__(self, *args, **kwargs):
        try:
            self.result = self.func(*args, **kwargs)
            return self.result
        except BaseException as exc:
            self.error = exc
            raise
        finally:
            with self._hook_lock:
                self.finished = True
                hook, self._hook = self._hook, None
            if hook is not None:
                try:
                    hook()
                except Exception:  # a hook failure must not replace the call's outcome
                    log.exception("on_finish hook of %r failed", real_call_name(self.func))


class DaemonExecutor(concurrent.futures.Executor):
    """Worker threads that never hold the interpreter open.

    ``asyncio.to_thread`` runs on the loop's default ``ThreadPoolExecutor``,
    whose threads ``asyncio.run`` (``shutdown_default_executor``) and
    ``concurrent.futures``' exit hook both join. A worker nothing can cancel
    therefore kept the server process alive until it finished — measured on
    2026-10-24: a 6 s runaway held ``asyncio.run`` for 6.0 s and the process for
    7.2 s after the call had been abandoned. These threads are daemon threads
    that nothing joins at exit; ``disconnect()`` waits a bounded
    ``SHUTDOWN_GRACE`` for them instead.

    Reuse follows ``ThreadPoolExecutor`` (CPython 3.13): a worker marks itself
    idle only when the queue is empty, and ``submit`` claims an idle worker
    before spawning a new one. ``max_workers=1`` keeps one strictly FIFO thread
    (the COM STA apartment).
    """

    def __init__(
        self,
        max_workers: int | None = None,
        *,
        name: str = "acadmcp-worker",
        initializer: Callable[[], None] | None = None,
    ):
        if max_workers is not None and max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._max_workers = max_workers
        self._name = name
        self._initializer = initializer
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._idle = threading.Semaphore(0)
        self._guard = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._shutdown = False

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        return tuple(self._threads)

    def submit(self, fn, /, *args, **kwargs) -> concurrent.futures.Future:
        with self._guard:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: concurrent.futures.Future = concurrent.futures.Future()
            self._queue.put((future, fn, args, kwargs))
            if not self._idle.acquire(timeout=0) and (
                self._max_workers is None or len(self._threads) < self._max_workers
            ):
                thread = threading.Thread(
                    target=self._work, name=f"{self._name}-{len(self._threads)}", daemon=True
                )
                self._threads.append(thread)
                thread.start()
            return future

    def _work(self) -> None:
        if self._initializer is not None:
            try:
                self._initializer()
            except BaseException:
                log.exception("%s: worker initializer failed", self._name)
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                self._idle.release()
                item = self._queue.get()
            if item is None:
                return
            future, fn, args, kwargs = item
            del item
            if future.set_running_or_notify_cancel():
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
                    del result
            del future, fn, args, kwargs

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._guard:
            self._shutdown = True
            threads = list(self._threads)
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
        for _ in threads:
            self._queue.put(None)
        if wait:
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join()


class QuarantineRecord:
    """One abandoned call, and what became of it.

    Holds the identity of the ``Drawing`` that was live at abandon time as well
    as its path: rebinding ``self._doc`` isolates a ``_sync`` that captured
    doc/msp once at entry (the shape of nearly every closure in the backend), but
    one that re-reads ``self._msp()`` inside its own loop would follow the rebind
    into the fresh document. ``_async`` cannot prevent that, so recovery says
    when the runaway was still alive instead of implying it was fenced off.
    """

    def __init__(
        self,
        call: str,
        timeout: float,
        watcher: Any,
        *,
        document_path: str | None = None,
        document_id: int | None = None,
        cause: str = "deadline",
    ):
        self.call = call
        self.cause = cause
        self.timeout = float(timeout)
        self.watcher = watcher
        self.document_path = document_path
        self.document_id = document_id
        self.stamp = time.monotonic()
        self.outcome = "running"
        self.detail = ""
        self.released_by: str | None = None
        self.result_lost = False
        self.result_repr: str | None = None

    def age(self) -> float:
        return time.monotonic() - self.stamp

    def thread_alive(self) -> bool:
        return not self.watcher.finished

    def capture_lost_result(self) -> None:
        """Record that the return value never reached the caller.

        A late-completing ``entity_create_*`` leaves an entity in the document
        whose handle nobody holds. That is not recoverable, so it is at least
        reported rather than dropped.
        """
        self.result_lost = True
        try:
            text = repr(self.watcher.result)
        except Exception:  # a __repr__ that raises must not break the report
            text = f"<unreprable {type(self.watcher.result).__name__}>"
        if len(text) > _MAX_RESULT_REPR:
            text = text[: _MAX_RESULT_REPR - 3] + "..."
        self.result_repr = text

    def to_dict(self) -> dict:
        return {
            "call": self.call,
            "timeout": self.timeout,
            "age_seconds": round(self.age(), 3),
            "thread_alive": self.thread_alive(),
            "outcome": self.outcome,
            "detail": self.detail,
            "released_by": self.released_by,
            "result_lost": self.result_lost,
            "lost_return_value": self.result_repr,
            "document_path": self.document_path,
            "cause": self.cause,
        }


def quarantine_refusal(record: QuarantineRecord) -> DocumentQuarantineError:
    """The refusal every later call on a quarantined document gets."""
    thread = "still running" if record.thread_alive() else "has since stopped"
    return DocumentQuarantineError(
        record,
        f"ezdxf document quarantined: '{record.call}' overran "
        f"EZDXF_CALL_TIMEOUT={record.timeout:g}s {record.age():.1f}s ago and its worker "
        f"thread ({thread}) cannot be cancelled, so it may still be mutating this "
        "drawing. Every call on it is refused, reads included - a read of a container "
        "another thread is appending to does not fail loudly, it never finishes. "
        "Recover with drawing_open(path), drawing_new() or drawing_close(): those "
        "rebind the document to a freshly constructed object the abandoned thread "
        "cannot reach. Undo and rollback are not a way back - their snapshots may "
        "themselves have been serialised mid-write. system_status() stays answerable "
        "and reports the abandoned call.",
    )


class DocumentQuarantineError(RuntimeError):
    """The open document is untrusted because a call was abandoned on it.

    Carries **no** ``capability`` attribute, on purpose.
    ``server._batch_capability_of`` classifies refusals structurally — any
    exception with a non-empty ``capability`` str plus a callable ``to_dict()``
    is reported to the client as ``kind: "unsupported"`` and filed by
    ``cad_batch`` as a capability step row. Borrowing one here would tell the
    client the ezdxf engine cannot draw a circle. The engine is fine; the
    document is not. This needs its own kind, not a borrowed one.
    """

    #: The refusal ``kind`` a client sees (``server.CapabilityRefusalMiddleware``
    #: and ``cad_batch``'s step rows read it).
    kind = "quarantined"

    def __init__(self, record: QuarantineRecord, message: str):
        super().__init__(message)
        self.quarantine = record.to_dict()

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "kind": self.kind,
            "error": str(self),
            "quarantine": self.quarantine,
        }


class DetachedRead:
    """A read-only call that overran its deadline and is still running.

    W1 review follow-up to B01. A read cannot change the drawing, so its
    overrun never quarantines the document; but its worker thread still holds
    the ``Drawing`` it started on, and a write racing it is what the lock
    exists to prevent. From the call's ORIGINAL deadline on, the read is
    *detached*: every other call on that document is refused by name (a
    ``DocumentBusyError``) until the read ends, and the exits that replace the
    document object - ``drawing_open`` / ``drawing_new`` / ``drawing_close`` /
    ``document_activate`` / ``document_close`` - work at once, so nothing ever
    needs a restart.

    ``document`` is the ``Drawing`` object itself, not its ``id()``: the record
    keeps it alive until the read ends anyway, and an id could be reused by a
    fresh document after the old one is closed. ``lock`` is the document lock
    the read holds.
    """

    __slots__ = ("call", "timeout", "watcher", "document", "document_path", "started", "lock")

    def __init__(
        self,
        call: str,
        timeout: float,
        watcher: Any,
        *,
        document: Any,
        document_path: str | None,
        started: float,
        lock: Any,
    ):
        self.call = call
        self.timeout = float(timeout)
        self.watcher = watcher
        self.document = document
        self.document_path = document_path
        self.started = started
        self.lock = lock

    @property
    def deadline(self) -> float:
        """The original deadline, wall-clock seconds."""
        return self.started + self.timeout

    def age(self) -> float:
        return time.time() - self.started

    def thread_alive(self) -> bool:
        return not self.watcher.finished

    def to_dict(self) -> dict:
        return {
            "call": self.call,
            "timeout": self.timeout,
            "started": self.started,
            "deadline": self.deadline,
            "age_seconds": round(self.age(), 3),
            "thread_alive": self.thread_alive(),
            "document_path": self.document_path,
        }


#: The calls that end a detached read's hold on the session. Named in the
#: refusal so a client never has to guess that a restart is not needed.
DETACHED_READ_EXITS = (
    "drawing_open(path)",
    "drawing_new()",
    "drawing_close()",
    "document_activate(<another document>)",
    "document_close()",
)


class DocumentBusyError(DocumentQuarantineError):
    """The open document is held by a detached read-only call.

    A subclass of ``DocumentQuarantineError`` on purpose: every QA reader that
    lets a quarantine refusal propagate (instead of turning it into a
    ``check_failed`` finding) must let this one through too - neither document
    can be read right now. ``kind`` differs, because the document is NOT
    untrusted: nothing wrote to it.
    """

    kind = "busy"

    def __init__(self, record: DetachedRead, message: str):
        RuntimeError.__init__(self, message)
        self.quarantine = record.to_dict()

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "kind": self.kind,
            "error": str(self),
            "detached_read": self.quarantine,
        }


def detached_read_refusal(record: DetachedRead) -> DocumentBusyError:
    """The refusal every other call on a detached read's document gets."""
    return DocumentBusyError(
        record,
        f"ezdxf document busy: the read-only call '{record.call}' overran "
        f"EZDXF_CALL_TIMEOUT={record.timeout:g}s and has been running {record.age():.1f}s; "
        "its worker thread cannot be cancelled and still reads this drawing. A read "
        "cannot change it, so the document is NOT quarantined, but every other call on "
        "it is refused until the read ends - the refusal then lifts by itself. To go on "
        f"now, use {', '.join(DETACHED_READ_EXITS)}: they replace the document object "
        "the read holds, so nothing races it; document_list() names the other "
        "documents and also answers. No restart is needed; system_status() reports "
        "the read.",
    )
