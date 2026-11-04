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
request it is refused with ``operation_conflict``. This journal is in memory and
bounded: a restart forgets every id, and an id pushed out of the bound runs
again. Since 1.8.0 (B13) the server runs ``backends/journal_store.py``'s
``DurableJournal`` instead, which keeps the same records in sqlite so a client
id replays across restarts (``JOURNAL_DURABLE=off`` restores this one).

1.8-10: a record also carries the call's ``label`` and, for a mutating call,
its change summary (``changes``); ``history`` is what ``drawing_history``
reads and ``summary`` the thin ``system_status.operations``.

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
    "history_row_matches",
    "request_hash",
    "validate_label",
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
#: 1.8-10: the longest label a call may carry (``_meta.label``, ``cad_batch``,
#: ``checkpoint_create``'s 200 as well).
MAX_LABEL_CHARS = 200

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
    #: 1.8-08: the document's newest checkpoint when the call began and when it
    #: ended (``backends/checkpoints.py``); the same id when the call wrote none.
    checkpoint_before: str | None = None
    checkpoint_after: str | None = None
    #: 1.8-10: the caller's label (``_meta.label``, ``cad_batch(label=)``,
    #: ``checkpoint_create(label)``) and a mutating call's change summary
    #: ``{added, deleted, counts, complete}`` (``backends/change_sets.py``).
    label: str | None = None
    changes: dict | None = None
    #: 1.8-10: the document's lineage (1.8-04) - what ``drawing_history``
    #: filters on, so the rows of an earlier process (and, live, an earlier
    #: bind of the same file, whose ``document_id`` differs) are found.
    lineage_id: str | None = None

    def to_dict(self) -> dict:
        row = asdict(self)
        # Shown only when present: a record without them stays the 1.7 row,
        # byte for byte.
        for key in ("checkpoint_before", "checkpoint_after", "label", "changes", "lineage_id"):
            if row[key] is None:
                del row[key]
        return row


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


def validate_label(value: Any, *, source: str = "label") -> str | None:
    """A call's label (1.8-10): None, or 1-200 printable characters, trimmed."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{source}: expected a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_LABEL_CHARS:
        raise ValueError(f"{source}: at most {MAX_LABEL_CHARS} characters, got {len(text)}")
    if not text.isprintable():
        raise ValueError(f"{source}: printable characters only")
    return text


def history_row_matches(
    row: dict,
    *,
    tool: str | None = None,
    label: str | None = None,
    query: str | None = None,
    handle: str | None = None,
) -> bool:
    """Whether one history row (``tool``, ``label``, ``args_json``,
    ``changes_json`` keys) passes ``drawing_history``'s filters.

    ``query`` is case-insensitive over the tool name, the label and the stored
    argument text (keys and values - a part spec's feature ``id`` and ``kind``
    among them); ``handle`` matches a handle named in the arguments or in the
    change summary (added / deleted), case-insensitively.
    """
    if tool is not None and row.get("tool") != tool:
        return False
    if label is not None and row.get("label") != label:
        return False
    if query:
        needle = query.casefold()
        haystack = " ".join(
            str(row.get(key) or "") for key in ("tool", "label", "args_json")
        ).casefold()
        if needle not in haystack:
            return False
    if handle:
        wanted = f'"{handle.upper()}"'
        texts = (str(row.get("args_json") or ""), str(row.get("changes_json") or ""))
        if not any(wanted in text.upper() for text in texts):
            return False
    return True


def _lost(row: dict, document_id: str, saved_revision: int, saved_at: float | None = None) -> bool:
    if row.get("document_id") != document_id:
        return False
    state = row.get("state")
    after, before = row.get("revision_after"), row.get("revision_before")
    if state in ("committed", "partial"):
        return (
            after is not None and before is not None and after > before and after > saved_revision
        )
    if state == "unknown_commit":
        # No revision_after: the server died before the row was closed. It
        # counts when it started at the saved revision *after* the save was
        # recorded - the save's own row, killed before its final state was
        # flushed, started before that and wrote nothing the file lacks.
        if before is None or before < saved_revision:
            return False
        return saved_at is None or float(row.get("started") or 0.0) > float(saved_at)
    return False


def _lost_summary(rows: list[dict]) -> dict:
    """``{count, last_operation}`` over the rows ``_lost`` kept, oldest first."""
    if not rows:
        return {"count": 0, "last_operation": None}
    last = max(rows, key=lambda r: r.get("started") or 0.0)
    return {
        "count": len(rows),
        "last_operation": {
            "operation_id": last.get("operation_id"),
            "tool": last.get("tool"),
            "state": last.get("state"),
            "revision_after": last.get("revision_after"),
        },
    }


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
        if record.error_kind == "process_exit":
            # B13: the durable journal found the row still running after the
            # server that owned it had died.
            cause = "the server process ended while it was running"
        else:
            cause = "it was cancelled or timed out while its worker was writing"
        super().__init__(
            f"operation {record.operation_id!r} ({record.tool}) ended unknown_commit: "
            f"{cause}, so whether it landed is unknown. Check the drawing (system_status "
            "revision, entity_list) before doing it again, and retry under a new operation_id."
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
        #: 1.8-10: an in-memory journal's sequence numbers (the durable one
        #: uses its file's ``seq``), so ``drawing_history`` rows are ordered
        #: and ``since_seq`` works without a state folder.
        self._seqs: dict[str, int] = {}
        self._next_seq = 0

    def __len__(self) -> int:
        return len(self._records)

    def begin(
        self,
        tool: str,
        arguments: dict | None,
        *,
        operation_id: str | None = None,
        document: dict | None = None,
        label: str | None = None,
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
            found = self._lookup(operation_id)
            if found is not None:
                prior, has_result, stored = found
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
                if prior.state in REPLAYABLE_STATES and has_result:
                    return prior, stored
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
            checkpoint_before=identity.get("checkpoint"),
            label=label,
            lineage_id=identity.get("lineage_id"),
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
        changes: dict | None = None,
    ) -> None:
        """Close ``record``; a client id's ``result`` is kept for replay."""
        if state not in FINAL_STATES:
            raise ValueError(f"state must be one of {FINAL_STATES}, got {state!r}")
        record.state = state
        record.error_kind = error_kind
        record.ended = time.time()
        if changes is not None:
            record.changes = changes
        if document:
            record.revision_after = document.get("revision")
            record.checkpoint_after = document.get("checkpoint")
            if record.document_id is None:
                record.document_id = document.get("document_id")
            if record.lineage_id is None and record.document_id == document.get("document_id"):
                record.lineage_id = document.get("lineage_id")
        if result is not None and record.client_supplied:
            if self._records.get(record.operation_id) is record:
                self._results[record.operation_id] = result

    def lost_since_save(
        self, document_id: str, saved_revision: int, saved_at: float | None = None
    ) -> dict:
        """Operations on ``document_id`` past ``saved_revision`` (1.8-08).

        What a reopen of the saved file no longer holds: every call that moved
        the revision beyond the one the file was saved at (``committed`` /
        ``partial``), and every call left ``unknown_commit`` that started at that
        revision after the save was recorded (``saved_at``) - its write may have
        landed in the document that is gone. ``{count, last_operation}``;
        ``last_operation`` is None when count is 0.
        """
        return _lost_summary(
            [
                record.to_dict()
                for record in list(self._records.values())
                if _lost(record.to_dict(), document_id, saved_revision, saved_at)
            ]
        )

    def get(self, operation_id: str) -> OperationRecord | None:
        return self._records.get(operation_id)

    def recent(self, n: int = 20) -> list[dict]:
        """The last ``n`` records as dicts, newest last."""
        if n <= 0:
            return []
        return [record.to_dict() for record in list(self._records.values())[-int(n) :]]

    # ── 1.8-10: history and the thin status summary ──

    def history(
        self,
        document_id: str,
        *,
        limit: int = 10,
        since_seq: int | None = None,
        tool: str | None = None,
        label: str | None = None,
        query: str | None = None,
        handle: str | None = None,
        skip: Any = None,
        lineage_id: str | None = None,
    ) -> list[dict]:
        """``drawing_history``'s rows of ``document_id`` - or of its ``lineage_id``
        (an earlier process, an earlier live bind) - newest first.

        Each row: ``seq, operation_id, tool, state, revision_before,
        revision_after, checkpoint_before, checkpoint_after, label, started,
        args_json, changes_json``. ``skip(tool)`` drops a row (the server
        passes its read-only test); a running record is never a row. The
        in-memory journal keeps no arguments (``args_json`` None).
        """
        rows = []
        for record in reversed(list(self._records.values())):
            if not _mine(record, document_id, lineage_id) or record.state == "running":
                continue
            seq = self._seqs.get(record.operation_id, 0)
            if since_seq is not None and seq <= since_seq:
                continue
            row = _history_row(record, seq, None)
            if skip is not None and tool is None and skip(record.tool):
                continue
            if not history_row_matches(row, tool=tool, label=label, query=query, handle=handle):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def next_revision(self, document_id: str, seq: int) -> int | None:
        """The revision the next recorded call on ``document_id`` after ``seq``
        started from: where a call whose own ending was never recorded (a kill
        before its final state was flushed) left the document, at the latest."""
        found = None
        for record in self._records.values():
            if record.document_id != document_id or record.revision_before is None:
                continue
            if self._seqs.get(record.operation_id, 0) > seq:
                if found is None or record.revision_before < found:
                    found = record.revision_before
        return found

    def labels(self, document_id: str, lineage_id: str | None = None) -> list[str]:
        """The distinct labels of the document's (or its lineage's) records, oldest first."""
        seen: dict[str, None] = {}
        for record in self._records.values():
            if _mine(record, document_id, lineage_id) and record.label:
                seen.setdefault(record.label, None)
        return list(seen)

    def summary(self, *, exclude: str | None = None) -> dict:
        """``system_status.operations`` (1.8-10): ``{count, last, unknown_commit}``.

        ``last`` is the newest record other than ``exclude`` (the status call's
        own), as ``{op, tool, state, rev}``; None when there is none.
        """
        last = None
        unknown = 0
        for record in self._records.values():
            if record.state == "unknown_commit":
                unknown += 1
        for record in reversed(list(self._records.values())):
            if record.operation_id != exclude:
                last = _summary_last(record)
                break
        return {"count": len(self._records), "last": last, "unknown_commit": unknown}

    def _lookup(self, operation_id: str) -> tuple[OperationRecord, bool, Any] | None:
        """``(record, has_result, result)`` for a known id, else None.

        The hook a durable journal (``backends/journal_store.py``) extends to
        find an id this process never saw.
        """
        prior = self._records.get(operation_id)
        if prior is None:
            return None
        has_result = operation_id in self._results
        return prior, has_result, self._results.get(operation_id)

    def _store(self, record: OperationRecord) -> None:
        self._records.pop(record.operation_id, None)
        self._results.pop(record.operation_id, None)
        self._records[record.operation_id] = record
        self._next_seq += 1
        self._seqs[record.operation_id] = self._next_seq
        while len(self._records) > self._maxlen:
            evicted, _ = self._records.popitem(last=False)
            self._results.pop(evicted, None)
            self._seqs.pop(evicted, None)


def _mine(record: OperationRecord, document_id: str, lineage_id: str | None) -> bool:
    return record.document_id == document_id or (
        lineage_id is not None and record.lineage_id == lineage_id
    )


def _summary_last(record: OperationRecord) -> dict:
    return {
        "op": record.operation_id,
        "tool": record.tool,
        "state": record.state,
        "rev": record.revision_after
        if record.revision_after is not None
        else record.revision_before,
    }


def _history_row(record: OperationRecord, seq: int, args_json: str | None) -> dict:
    return {
        "seq": seq,
        "operation_id": record.operation_id,
        "document_id": record.document_id,
        "tool": record.tool,
        "state": record.state,
        "revision_before": record.revision_before,
        "revision_after": record.revision_after,
        "checkpoint_before": record.checkpoint_before,
        "checkpoint_after": record.checkpoint_after,
        "label": record.label,
        "started": record.started,
        "args_json": args_json,
        "changes_json": None
        if record.changes is None
        else json.dumps(record.changes, sort_keys=True, separators=(",", ":"), default=str),
    }
