"""Operation records: one per tool call, held in memory (v1.7.0, B12).

Every tool call gets an ``operation_id``: minted by ``AuditMiddleware`` into
``CURRENT_OPERATION`` (a call that re-enters the middleware - the search
proxy's ``call_tool`` - reuses the id already set), one per step by
``cad_batch`` (its steps skip the middleware), and carried into the COM
executor explicitly because a thread pool copies no context. A record says
what happened, honestly:

    committed       the call returned its result
    partial         a batch whose steps partly landed (on_error stop/continue)
    failed          attempted and broke, or a read that outran its deadline
    refused         declined before doing its work (capability, guard, args)
    quarantined     refused because the document is quarantined
    cancelled       cancelled before its worker ran - nothing happened
    unknown_commit  a mutating call cancelled or timed out while its worker ran:
                    it may or may not have landed

A client may bring its own id (``_meta.operation_id`` on any request, or
``cad_batch(operation_id=...)``). The same id with the same request hash gets
the recorded result back without running again (``replayed: true``); after an
``unknown_commit`` it is refused with ``requires_reconcile``; with a different
request it is refused with ``operation_conflict``. The journal is in memory and
bounded: a server restart forgets every id (B13 makes it durable in 1.8.0),
and an id pushed out of the bound runs again.

Pure: no backend, no server import.
"""

from __future__ import annotations

import collections
import contextvars
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

__all__ = [
    "CURRENT_OPERATION",
    "FINAL_STATES",
    "REPLAYABLE_STATES",
    "STATES",
    "OperationConflictError",
    "OperationJournal",
    "OperationRecord",
    "RequiresReconcileError",
    "request_hash",
    "validate_operation_id",
]

STATES: tuple[str, ...] = (
    "running",
    "committed",
    "partial",
    "failed",
    "refused",
    "quarantined",
    "cancelled",
    "unknown_commit",
)
FINAL_STATES: tuple[str, ...] = STATES[1:]
#: A finished operation whose recorded answer is final. ``cancelled`` is not:
#: nothing ran, so the same id runs again. ``unknown_commit`` is not either: it
#: is refused with ``requires_reconcile``.
REPLAYABLE_STATES: tuple[str, ...] = ("committed", "partial", "failed", "refused", "quarantined")
MAX_OPERATION_ID_CHARS = 128

#: The operation the running code belongs to. Set by AuditMiddleware (one per
#: request), by cad_batch (one per step) and inside the COM job (explicitly).
CURRENT_OPERATION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "acadmcp_operation_id", default=None
)


@dataclass
class OperationRecord:
    operation_id: str
    tool: str
    request_hash: str
    state: str
    document_id: str | None
    revision_before: int | None
    revision_after: int | None
    started: float
    ended: float | None = None
    error_kind: str | None = None
    client_supplied: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def request_hash(tool: str, arguments: dict | None) -> str:
    """``sha256:`` + the hex digest of the canonical JSON of tool + arguments."""
    canonical = json.dumps(
        {"tool": str(tool), "arguments": arguments or {}},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_operation_id(value: Any) -> str:
    """A client's id: 1-128 printable characters, no surrounding whitespace."""
    if not isinstance(value, str):
        raise ValueError(f"operation_id: expected a string, got {type(value).__name__}")
    if not value:
        raise ValueError("operation_id: must not be empty")
    if len(value) > MAX_OPERATION_ID_CHARS:
        raise ValueError(
            f"operation_id: at most {MAX_OPERATION_ID_CHARS} characters, got {len(value)}"
        )
    if value != value.strip():
        raise ValueError("operation_id: no leading or trailing whitespace")
    if not value.isprintable():
        raise ValueError("operation_id: printable characters only")
    return value


class OperationConflictError(RuntimeError):
    """A client id already names another request, or a request still running."""

    kind = "operation_conflict"

    def __init__(self, operation_id: str, *, reason: str, recorded_tool: str):
        self.operation_id = operation_id
        self.reason = reason
        if reason == "in_progress":
            message = (
                f"operation {operation_id!r} ({recorded_tool}) is still running; wait for it, "
                "then send the same id again to get its result"
            )
        else:
            message = (
                f"operation {operation_id!r} was already used for a different request "
                f"({recorded_tool}); an operation id names one request - send a new id"
            )
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "kind": self.kind,
            "error": str(self),
            "operation_id": self.operation_id,
            "reason": self.reason,
        }


class RequiresReconcileError(RuntimeError):
    """The id's first run ended ``unknown_commit``: whether it landed is unknown."""

    kind = "requires_reconcile"

    def __init__(self, record: OperationRecord):
        self.record = record
        super().__init__(
            f"operation {record.operation_id!r} ({record.tool}) ended unknown_commit: it was "
            "cancelled or timed out while its worker was writing, so whether it landed is "
            "unknown. Check the drawing (system_status revision, entity_list) before doing it "
            "again, and retry under a new operation_id."
        )

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "kind": self.kind,
            "error": str(self),
            "operation_id": self.record.operation_id,
            "record": self.record.to_dict(),
        }


class OperationJournal:
    """The bounded, in-memory record of recent operations, oldest first."""

    def __init__(self, maxlen: int = 1000):
        if int(maxlen) < 1:
            raise ValueError(f"maxlen must be at least 1, got {maxlen!r}")
        self._maxlen = int(maxlen)
        self._records: collections.OrderedDict[str, OperationRecord] = collections.OrderedDict()
        self._results: dict[str, Any] = {}

    def __len__(self) -> int:
        return len(self._records)

    def begin(
        self,
        tool: str,
        arguments: dict | None,
        *,
        operation_id: str | None = None,
        document: dict | None = None,
    ) -> tuple[OperationRecord, Any | None]:
        """Open a record; ``(record, replayed result or None)``.

        A client id (``operation_id`` given) is checked against what the
        journal holds: a different request is ``OperationConflictError``
        (``different_request``), one still running is ``OperationConflictError``
        (``in_progress``), an ``unknown_commit`` is ``RequiresReconcileError``,
        and a finished one in ``REPLAYABLE_STATES`` returns its recorded result
        with the record it belongs to - nothing new is recorded then.
        """
        digest = request_hash(tool, arguments)
        identity = document or {}
        client_supplied = operation_id is not None
        if client_supplied:
            operation_id = validate_operation_id(operation_id)
            prior = self._records.get(operation_id)
            if prior is not None:
                if prior.request_hash != digest:
                    raise OperationConflictError(
                        operation_id, reason="different_request", recorded_tool=prior.tool
                    )
                if prior.state == "running":
                    raise OperationConflictError(
                        operation_id, reason="in_progress", recorded_tool=prior.tool
                    )
                if prior.state == "unknown_commit":
                    raise RequiresReconcileError(prior)
                if prior.state in REPLAYABLE_STATES and operation_id in self._results:
                    return prior, self._results[operation_id]
        else:
            operation_id = uuid.uuid4().hex
        record = OperationRecord(
            operation_id=operation_id,
            tool=str(tool),
            request_hash=digest,
            state="running",
            document_id=identity.get("document_id"),
            revision_before=identity.get("revision"),
            revision_after=None,
            started=time.time(),
            client_supplied=client_supplied,
        )
        self._store(record)
        return record, None

    def finish(
        self,
        record: OperationRecord,
        state: str,
        *,
        result: Any = None,
        error_kind: str | None = None,
        document: dict | None = None,
    ) -> None:
        """Close ``record``; a client id's ``result`` is kept for replay."""
        if state not in FINAL_STATES:
            raise ValueError(f"state must be one of {FINAL_STATES}, got {state!r}")
        record.state = state
        record.error_kind = error_kind
        record.ended = time.time()
        if document:
            record.revision_after = document.get("revision")
            if record.document_id is None:
                record.document_id = document.get("document_id")
        if result is not None and record.client_supplied:
            if self._records.get(record.operation_id) is record:
                self._results[record.operation_id] = result

    def get(self, operation_id: str) -> OperationRecord | None:
        return self._records.get(operation_id)

    def recent(self, n: int = 20) -> list[dict]:
        """The last ``n`` records as dicts, newest last."""
        if n <= 0:
            return []
        return [record.to_dict() for record in list(self._records.values())[-int(n) :]]

    def _store(self, record: OperationRecord) -> None:
        self._records.pop(record.operation_id, None)
        self._results.pop(record.operation_id, None)
        self._records[record.operation_id] = record
        while len(self._records) > self._maxlen:
            evicted, _ = self._records.popitem(last=False)
            self._results.pop(evicted, None)
