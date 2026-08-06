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

import time
from typing import Any

__all__ = [
    "AbandonedCall",
    "DocumentQuarantineError",
    "QuarantineRecord",
    "quarantine_refusal",
    "real_call_name",
]

#: Longest ``repr`` kept for a return value nobody received. A late
#: ``entity_list`` can return thousands of entities; the diagnostic is that the
#: value was lost, not what was in it.
_MAX_RESULT_REPR = 200


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
    the *await* ending, never the thread — which ``asyncio.to_thread`` cannot
    cancel. The only place the thread's outcome is observable is inside the
    thread, so ``_async`` wraps every timed call in one of these.

    Deliberately no ``threading.Event``: this is allocated on every ``_async``
    call and an Event carries a Condition and a Lock. ``finished`` is a plain
    bool written once by the worker and read from the event loop; a bool store is
    atomic under the GIL and nothing here ever waits on it.
    """

    __slots__ = ("func", "finished", "result", "error")

    def __init__(self, func):
        self.func = func
        self.finished = False
        self.result: Any = None
        self.error: BaseException | None = None

    def __call__(self, *args, **kwargs):
        try:
            self.result = self.func(*args, **kwargs)
            return self.result
        except BaseException as exc:
            self.error = exc
            raise
        finally:
            self.finished = True


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
    ):
        self.call = call
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

    def __init__(self, record: QuarantineRecord, message: str):
        super().__init__(message)
        self.quarantine = record.to_dict()

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "kind": "quarantined",
            "error": str(self),
            "quarantine": self.quarantine,
        }
