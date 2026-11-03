"""One error taxonomy for every tool call (plan 1.8-02).

Before 1.8.0 only ``cad_batch`` typed its failures (``server._classify_batch_error``);
a direct call that failed reached the client as an English sentence behind
FastMCP's ``Internal error:`` envelope, so "this handle does not exist", "your
path is outside ALLOWED_PATHS" and "AutoCAD is showing a prompt" could be told
apart only by substring matching. This module is the shared half: the closed
set of kinds, each kind's category / retryable default / hint, the typed
exceptions the backends and the server raise, and ``classify(exc)``, which the
refusal middleware (a direct call) and ``cad_batch`` (a step) both call - so the
two routes cannot disagree about a kind.

Rules:

* **By type, never by text.** ``classify`` walks the ``__cause__`` chain
  (``FastMCP.call_tool`` re-wraps a tool's exception as ``ToolError(...) from
  e``) and matches exception *types*; a RuntimeError that happens to say "not
  found" stays ``failed``.
* **retryable is about what was written.** A kind's ``retryable`` is a default;
  :func:`retryable_for` makes it true only when the operation record is
  ``refused`` or ``cancelled`` - nothing was written, so sending the same request
  again cannot double-apply it.
* **The prose is unchanged.** A typed exception keeps the class its call site
  raised before (``EntityNotFoundError`` is a ``RuntimeError``,
  ``PathRefusedError`` a ``ToolError``), so ``except`` clauses and the text a
  prose-only client reads stay byte-for-byte what they were.

Free of backends at import time: the backend refusal types are imported lazily,
exactly as ``server.py`` did, so this module imports on a box without ezdxf's
extras or pywin32.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastmcp.exceptions import ToolError

# Defined beside the checks that raise them (security.py imports nothing of
# this package, so this import cannot cycle); re-exported here.
from security import CommandRefusedError, PathRefusedError

log = logging.getLogger("autocad_mcp.errors")

__all__ = [
    "BATCH_KINDS_1_7",
    "CATEGORIES",
    "CHECKPOINT_KINDS",
    "ERROR_KINDS",
    "ERROR_TABLE",
    "FILE_KINDS",
    "GATE_KINDS",
    "ApplicationBusyError",
    "CommandIncompleteError",
    "CommandRefusedError",
    "EntityNotFoundError",
    "GateFailure",
    "InvalidArgsError",
    "KindSpec",
    "PathRefusedError",
    "annotate",
    "capability_of",
    "cause_chain",
    "classify",
    "compact_message",
    "describe",
    "document_changed_of",
    "gate_failure_payload",
    "quarantine_of",
    "refusal_kind",
    "refusal_message",
    "retryable_for",
    "short_message",
    "stale_of",
    "typed_message",
]

#: The rival's eight categories (plan 1.8-02), in its order.
CATEGORIES = (
    "input",
    "spec",
    "geometry",
    "file",
    "policy",
    "validation",
    "dependency",
    "internal",
)

#: The step-row kinds 1.7.0 emitted, in their 1.7.0 order (``BATCH_ERROR_KINDS``).
BATCH_KINDS_1_7 = (
    "denied",
    "malformed_step",
    "unknown_tool",
    "invalid_args",
    "unresolved_ref",
    "unsupported",
    "refused",
    "failed",
    "quarantined",
    "busy",
    "stale_revision",
    "active_document_changed",
    "scan_incomplete",
    "unknown_commit",
    "timed_out",
    "cancelled",
    "operation_conflict",
)

#: The finalize / deliver gate kinds (:class:`GateFailure`).
GATE_KINDS = ("validation_failed", "critique_gate_failed", "failed_acceptance")

#: The save / open kinds of 1.8-04 (``backends/dxf_recover.py``,
#: ``backends/atomic_write.py``): each is the ``kind`` attribute of the
#: exception class that raises it.
FILE_KINDS = (
    "damaged_drawing",
    "recover_unsupported",
    "recovered_source_protected",
    "save_locked",
    "save_verify_failed",
    "save_failed",
)

#: The checkpoint refusals of 1.8-08 (``backends/checkpoints.py``): a reference
#: that names no checkpoint of the document, and one the CHECKPOINT_* budget
#: evicted - told apart, so an evicted state is never reported as one that
#: never existed.
CHECKPOINT_KINDS = ("checkpoint_not_found", "checkpoint_evicted")

#: Every kind an error envelope or a step row can carry. Closed: a client that
#: branches on the taxonomy needs it enumerable. ``requires_reconcile`` is the
#: operation journal's refusal (B12), already on the wire in 1.7.0;
#: ``process_exit`` is the ``error_kind`` the durable journal (1.8-03) writes on
#: the row of a call whose server died, which a ``requires_reconcile`` answer
#: carries in its ``record``.
ERROR_KINDS = BATCH_KINDS_1_7 + (
    "not_found",
    "unknown_handle",
    "path_refused",
    "policy_refused",
    "file_not_found",
    "application_busy",
    "command_incomplete",
    "internal",
    "requires_reconcile",
    *GATE_KINDS,
    *FILE_KINDS,
    "process_exit",
    *CHECKPOINT_KINDS,
)


@dataclass(frozen=True)
class KindSpec:
    """What a kind means to a client: its category, whether a retry of the
    same request may succeed once nothing was written, and where to go next."""

    category: str
    retryable: bool
    hint: str


#: kind -> (category, retryable default, hint). Hints are one short clause:
#: every error envelope carries one, inside a 60-token budget.
ERROR_TABLE: dict[str, KindSpec] = {
    "denied": KindSpec("policy", False, "Call the tool directly; cad_batch never runs it."),
    "malformed_step": KindSpec("input", False, "A step is {tool, args, bind}."),
    "unknown_tool": KindSpec(
        "input", False, "Not a callable tool here; search_tools or the active TOOL_PACKS."
    ),
    "invalid_args": KindSpec("input", False, "Fix the arguments; describe_tool shows the schema."),
    "unresolved_ref": KindSpec("input", False, "Bind the name in an earlier step."),
    "unsupported": KindSpec(
        "dependency", False, "This engine cannot; system_capabilities lists what it can."
    ),
    "refused": KindSpec("policy", False, "The tool declined; its message says why."),
    "failed": KindSpec("geometry", False, "Attempted and broke; check the drawing before a retry."),
    "quarantined": KindSpec(
        "dependency", False, "The document is untrusted after an abandoned call; reopen it."
    ),
    "busy": KindSpec("dependency", True, "A detached read holds the document; retry shortly."),
    "stale_revision": KindSpec(
        "spec", False, "The drawing moved; take a new preview and apply that."
    ),
    "active_document_changed": KindSpec(
        "dependency", True, "Switch AutoCAD back, or document_activate the other drawing."
    ),
    "scan_incomplete": KindSpec("validation", False, "The read was partial; see the reason."),
    "unknown_commit": KindSpec(
        "dependency", False, "It may have landed; inspect the drawing before a retry."
    ),
    "timed_out": KindSpec("dependency", False, "AutoCAD did not answer in time."),
    "cancelled": KindSpec("input", True, "The request was cancelled."),
    "operation_conflict": KindSpec(
        "input",
        False,
        "The id names another request (send a new id) or one still running (wait).",
    ),
    "requires_reconcile": KindSpec(
        "input", False, "Inspect the drawing, then send a new operation_id."
    ),
    "not_found": KindSpec("input", False, "No such entity; entity_list returns live handles."),
    "unknown_handle": KindSpec("input", False, "No such entity; entity_list returns live handles."),
    "path_refused": KindSpec("policy", False, "Use a path inside ALLOWED_PATHS, not a system one."),
    "policy_refused": KindSpec("policy", False, "Use a typed tool from `alternatives`."),
    "file_not_found": KindSpec("file", False, "No file at that path."),
    "application_busy": KindSpec(
        "dependency", True, "AutoCAD is at a prompt or dialog; press ESC, then retry."
    ),
    "command_incomplete": KindSpec(
        "dependency", False, "The command stopped at a prompt; see last_prompt."
    ),
    "internal": KindSpec("internal", False, "A server bug; report it with the operation_id."),
    "validation_failed": KindSpec(
        "validation", False, "Fix the blocking findings and finalize again."
    ),
    "critique_gate_failed": KindSpec(
        "validation", False, "Fix the blocking issues (drawing_refine) and finalize again."
    ),
    "failed_acceptance": KindSpec(
        "validation", False, "A requirement does not pass on the geometry; fix the drawing."
    ),
    "damaged_drawing": KindSpec(
        "file", False, "Not a readable DXF; drawing_open(path, recover=True) repairs what it can."
    ),
    "recover_unsupported": KindSpec(
        "file", False, "recover=True reads DXF only; use AutoCAD's own RECOVER on a DWG."
    ),
    "recovered_source_protected": KindSpec(
        "policy", False, "Save to another path, or pass overwrite_source=True to replace it."
    ),
    "save_locked": KindSpec(
        "file", True, "Another program holds the file; close it, then save again."
    ),
    "save_verify_failed": KindSpec(
        "file", False, "The written copy failed its check; the target file is unchanged."
    ),
    "save_failed": KindSpec(
        "file", False, "The save did not complete; the target is the file it was before."
    ),
    "process_exit": KindSpec(
        "dependency", False, "The server ended mid-call; inspect the drawing before a retry."
    ),
    "checkpoint_not_found": KindSpec(
        "input", False, "Not a checkpoint of this document; checkpoint_list names them."
    ),
    "checkpoint_evicted": KindSpec(
        "file", False, "Evicted by the CHECKPOINT_* budget; pick a kept one from checkpoint_list."
    ),
}

#: Record states in which nothing was written (``backends/operations.py``).
_NOTHING_WRITTEN = frozenset({"refused", "cancelled"})


def describe(kind: str) -> KindSpec:
    """The table row of ``kind``; an undeclared kind is ``internal``'s row."""
    spec = ERROR_TABLE.get(kind)
    if spec is None:
        log.warning("undeclared error kind %r", kind)
        return ERROR_TABLE["internal"]
    return spec


def retryable_for(kind: str, state: str | None) -> bool:
    """The kind's default, but true only when the record wrote nothing."""
    return describe(kind).retryable and state in _NOTHING_WRITTEN


def annotate(error: dict, state: str | None) -> dict:
    """Add ``category`` / ``retryable`` / ``hint`` to a classified error, in place."""
    spec = describe(str(error.get("kind")))
    error["category"] = spec.category
    error["retryable"] = retryable_for(str(error.get("kind")), state)
    error["hint"] = spec.hint
    return error


# ── typed exceptions ────────────────────────────────────────────────────────


class EntityNotFoundError(RuntimeError):
    """A handle that names no live entity in the document.

    A ``RuntimeError`` because that is what both engines raised for it before
    1.8.0: every ``except RuntimeError`` and the client's prose are unchanged.
    """

    kind = "not_found"

    def __init__(self, handle, message: str | None = None):
        self.handle = str(handle)
        super().__init__(message or f"Entity with handle '{handle}' not found.")


class ApplicationBusyError(RuntimeError):
    """AutoCAD refused the call (``RPC_E_CALL_REJECTED``) before the job had
    touched the document: nothing was written, the same call may be sent again."""

    kind = "application_busy"


class CommandIncompleteError(RuntimeError):
    """A command sent to AutoCAD's command line was still at a prompt when its
    deadline ran out (1.8-05): ``last_prompt`` is AutoCAD's own ``LASTPROMPT``,
    read before the ESCs that cancelled it, verbatim (it is localised).

    A ``RuntimeError`` with the 1.7 sentence ("did not finish within ..."), so
    ``except RuntimeError`` and a prose-only client are unchanged. Not
    retryable: the command was sent, so part of it may have landed.
    """

    kind = "command_incomplete"

    def __init__(self, command: str, last_prompt: str | None, deadline_s: float):
        self.command = str(command)
        self.last_prompt = None if last_prompt is None else str(last_prompt)
        self.deadline_s = float(deadline_s)
        stopped = "" if self.last_prompt is None else f" It stopped at: {self.last_prompt!r}."
        super().__init__(
            f"AutoCAD command did not finish within {self.deadline_s:.1f}s: "
            f"{self.command!r}.{stopped} ESC was sent three times to cancel it."
        )


class GateFailure(ToolError):
    """``drawing_finalize``'s gate failed; ``payload`` is what it computed.

    A ``ToolError`` with the same message as before, so the prose and
    ``pytest.raises(ToolError, match=...)`` are unchanged; the refusal
    middleware and ``cad_batch`` read ``payload``.
    """

    def __init__(self, message: str, payload: dict):
        super().__init__(message)
        self.payload = dict(payload)
        self.kind = str(self.payload.get("kind") or "validation_failed")


class InvalidArgsError(ToolError):
    """A tool refusing an argument value its schema cannot express (a closed
    set checked in the body, such as ``detail``).

    A ``ToolError`` with the sentence the call site raised before
    (``"invalid_args: ..."``), so the prose and ``except ToolError`` are
    unchanged; typed so that the kind is ``invalid_args`` - an unwrapped
    ``ToolError`` alone reads as the tool declining (``refused``).
    """

    kind = "invalid_args"


#: How many blocking rows a gate failure carries unless ``detail="full"``.
GATE_BLOCKING_ROWS = 10
#: How many handles each blocking row carries unless ``detail="full"``.
GATE_HANDLES_PER_ROW = 5


def _gate_row(code: str, severity: str, handles, message: str, full: bool) -> dict:
    handles = [str(h) for h in (handles or [])]
    row = {
        "code": str(code),
        "severity": str(severity),
        "handles": handles if full else handles[:GATE_HANDLES_PER_ROW],
        "message": str(message),
    }
    if not full and len(handles) > GATE_HANDLES_PER_ROW:
        row["handle_count"] = len(handles)
    return row


def gate_failure_payload(
    kind: str,
    *,
    score: dict | None,
    findings=(),
    issues=(),
    requirements=(),
    other=(),
    qa_coverage: dict | None = None,
    detail: str = "summary",
) -> dict:
    """The ``GateFailure`` payload: ``{kind, score, blocking, blocking_count, qa_coverage}``.

    ``findings`` are the validator's blocking ``ValidationFinding`` rows,
    ``issues`` the critique's blocking ``Issue`` rows, ``other`` any further
    ``(code, message)`` reasons (a delivery's parity, score or export) and
    ``requirements`` the acceptance report's blocking strings - every reason, in
    that order, whichever gate failed first. ``detail="full"`` keeps every row and every
    handle; the default keeps the first :data:`GATE_BLOCKING_ROWS` rows with
    :data:`GATE_HANDLES_PER_ROW` handles each (``handle_count`` says how many).
    """
    if kind not in GATE_KINDS:
        raise ValueError(f"gate kind must be one of {GATE_KINDS}, got {kind!r}")
    full = detail == "full"
    rows: list[dict] = []
    for finding in findings:
        detail_map = getattr(finding, "detail", None) or {}
        handles = detail_map.get("handles") if isinstance(detail_map, dict) else None
        rows.append(_gate_row(finding.code, finding.severity, handles, finding.message, full))
    for issue in issues:
        code = (issue.detail or {}).get("code") if isinstance(issue.detail, dict) else None
        rows.append(
            _gate_row(code or issue.focus, issue.severity, issue.handles, issue.message, full)
        )
    for code, text in other:
        rows.append(_gate_row(code, "error", (), text, full))
    for text in requirements:
        rows.append(_gate_row("requirement", "error", (), text, full))
    return {
        "kind": kind,
        "score": dict(score or {}),
        "blocking": rows if full else rows[:GATE_BLOCKING_ROWS],
        "blocking_count": len(rows),
        "qa_coverage": qa_coverage,
    }


# ── the cause chain ─────────────────────────────────────────────────────────
#
# `ctx.fastmcp.call_tool` re-raises FastMCP's own exceptions untouched but wraps
# everything else as `ToolError(f"Error calling tool {name!r}: {e}") from e`. The
# `from e` is what makes a typed taxonomy possible: the real exception is still
# on the `__cause__` chain, so nothing here ever has to read message text. It
# must not, either - "this backend cannot write DWG" and a RuntimeError that
# happens to say the same words are different facts, and only the type knows.

_CAUSE_CHAIN_LIMIT = 8

#: Step-row messages are compacted to this many characters. A pydantic failure
#: prints four paragraphs; re-emitting that per step would hand back the tokens
#: the batch just saved.
MESSAGE_CHARS = 240

_capability_error_types: tuple[type, ...] | None = None
_quarantine_error_type: type | None = None

#: Builtin exception types that mean a bug in this server, not a refusal or a
#: failed CAD operation: a call whose innermost cause is exactly one of them is
#: ``internal``. Only types the server never raises on purpose: ``TypeError``
#: is how block_specs / xdata_specs / the style and preference whitelists refuse
#: a caller's wrong-typed value, and ``KeyError`` / ``IndexError`` /
#: ``ZeroDivisionError`` are what a caller's missing key, short list or
#: degenerate geometry raises - an agent's own argument, which must never read
#: "a server bug". Those stay ``failed``, as they were in 1.7.
_BUG_TYPES = frozenset(
    {
        AttributeError,
        NameError,
        AssertionError,
        RecursionError,
        UnboundLocalError,
    }
)


def cause_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and its ``__cause__`` ancestors, outermost first, bounded."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < _CAUSE_CHAIN_LIMIT:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    return chain


def _capability_refusal_types() -> tuple[type, ...]:
    """Backend exception classes meaning "this engine cannot do that"."""
    global _capability_error_types
    if _capability_error_types is None:
        found: list[type] = []
        try:
            from backends.base import UnsupportedCapabilityError

            found.append(UnsupportedCapabilityError)
        except Exception as exc:  # pragma: no cover - a core module
            log.debug("capability refusal type unavailable: %s", exc)
        _capability_error_types = tuple(found)
    return _capability_error_types


def capability_of(exc: BaseException) -> str | None:
    """The backend capability ``exc`` refuses, or None.

    Recognised by type, and - because a backend may ship its own refusal class -
    also by the contract every such refusal implements: a non-empty
    ``capability`` string plus the ``to_dict()`` that carries the
    ``{ok, error, capability}`` payload. Structural, not textual.
    """
    types = _capability_refusal_types()
    for error in cause_chain(exc):
        if types and isinstance(error, types):
            return str(getattr(error, "capability", "") or "unknown")
        capability = getattr(error, "capability", None)
        if isinstance(capability, str) and capability and callable(getattr(error, "to_dict", None)):
            return capability
    return None


_file_error_types: tuple[type, ...] | None = None


def _file_refusal_types() -> tuple[type, ...]:
    """The 1.8-04 open / save exception classes, each carrying its ``kind``.

    ``AtomicWriteError`` is the base of ``SaveVerifyError`` and
    ``SaveLockedError``; the matched instance's own ``kind`` is the one reported.
    """
    global _file_error_types
    if _file_error_types is None:
        found: list[type] = []
        try:
            from backends.dxf_recover import (
                DrawingParseError,
                RecoveredSourceError,
                RecoverUnsupportedError,
            )

            found += [DrawingParseError, RecoveredSourceError, RecoverUnsupportedError]
        except Exception as exc:  # pragma: no cover - a core module
            log.debug("recover refusal types unavailable: %s", exc)
        try:
            from backends.atomic_write import AtomicWriteError

            found.append(AtomicWriteError)
        except Exception as exc:  # pragma: no cover - a core module
            log.debug("atomic write error type unavailable: %s", exc)
        _file_error_types = tuple(found)
    return _file_error_types


def _file_error_of(exc: BaseException) -> BaseException | None:
    types = _file_refusal_types()
    return _first_of(exc, types) if types else None


def _checkpoint_error_of(exc: BaseException) -> BaseException | None:
    """The 1.8-08 checkpoint refusal on the chain (``backends/checkpoints.py``), or None."""
    try:
        from backends.checkpoints import CheckpointError
    except Exception as exc_import:  # pragma: no cover - a core module
        log.debug("checkpoint error type unavailable: %s", exc_import)
        return None
    return _first_of(exc, CheckpointError)


def typed_message(exc: BaseException) -> str | None:
    """The full sentence of a 1.8-04 open / save error on the chain, on one line.

    Those exceptions are raised ``from`` the parser's or the OS's own error,
    whose text (``missing ENDSEC tag``) is the most specific on the chain but
    not the refusal: the typed sentence is the one that says nothing was
    opened or written and names the way out (``recover=True``,
    ``overwrite_source=True``). None for every other exception.
    """
    found = _file_error_of(exc)
    if found is None:
        return None
    return " ".join(str(found).split()) or None


def _quarantine_refusal_type() -> type:
    global _quarantine_error_type
    if _quarantine_error_type is None:
        from backends.quarantine import DocumentQuarantineError

        _quarantine_error_type = DocumentQuarantineError
    return _quarantine_error_type


def quarantine_of(exc: BaseException) -> dict | None:
    """The abandoned-call record ``exc`` refuses on behalf of, or None.

    By type: ``DocumentQuarantineError`` deliberately has no ``capability``
    attribute, so it is never reported as ``unsupported``.
    """
    try:
        quarantine_type = _quarantine_refusal_type()
    except Exception as exc_import:  # pragma: no cover - a core module
        log.debug("quarantine refusal type unavailable: %s", exc_import)
        return None
    for error in cause_chain(exc):
        if isinstance(error, quarantine_type):
            return getattr(error, "quarantine", None) or {}
    return None


def refusal_kind(exc: BaseException) -> str:
    """``"busy"`` for a detached read's ``DocumentBusyError``, else ``"quarantined"``."""
    try:
        quarantine_type = _quarantine_refusal_type()
    except Exception:  # pragma: no cover - a core module
        return "quarantined"
    for error in cause_chain(exc):
        if isinstance(error, quarantine_type):
            return str(getattr(error, "kind", "quarantined"))
    return "quarantined"


def document_changed_of(exc: BaseException) -> dict | None:
    """The ``ActiveDocumentChangedError`` payload on the chain, or None (B11)."""
    from backends.document_guard import ActiveDocumentChangedError

    for error in cause_chain(exc):
        if isinstance(error, ActiveDocumentChangedError):
            return error.to_dict()
    return None


def stale_of(exc: BaseException) -> dict | None:
    """The ``StaleRevisionError.to_dict()`` refusal on the chain, or None."""
    from engineering.changes.preview import StaleRevisionError

    for error in cause_chain(exc):
        if isinstance(error, StaleRevisionError):
            return error.to_dict()
    return None


def _first_of(exc: BaseException, kind: type):
    for error in cause_chain(exc):
        if isinstance(error, kind):
            return error
    return None


def refusal_message(exc: BaseException) -> str:
    """The most specific message on the cause chain, in full, on one line."""
    for error in reversed(cause_chain(exc)):
        text = " ".join(str(error).split())
        if text:
            return text
    return type(exc).__name__


def compact_message(text: str) -> str:
    """One short line, for a step row that sits alongside dozens of others."""
    text = " ".join(text.split())
    if len(text) > MESSAGE_CHARS:
        text = text[: MESSAGE_CHARS - 3].rstrip() + "..."
    return text


def short_message(exc: BaseException) -> str:
    """The most specific message on the chain, collapsed onto one short line."""
    chain = cause_chain(exc)
    text = ""
    for error in reversed(chain):
        text = str(error).strip()
        if text:
            break
    return compact_message(text) or type(chain[0]).__name__


def classify(exc: BaseException) -> dict:
    """Type an exception from a tool call: ``{"kind", "message", ...extras}``.

    The same function types a direct call (the refusal middleware) and a
    ``cad_batch`` step, so both routes give one kind. ``message`` is the short
    form; the extras are the kind's own fields (``capability``, ``quarantine``,
    ``bound`` / ``active``, the stale revisions, ``verbs`` / ``alternatives``,
    a gate's ``score`` / ``blocking``).
    """
    from fastmcp.exceptions import NotFoundError
    from fastmcp.exceptions import ValidationError as FastMCPValidationError
    from pydantic import ValidationError as PydanticValidationError

    message = short_message(exc)
    changed = document_changed_of(exc)
    if changed is not None:
        # B11: refused before anything was written; every later step is refused
        # the same way until the operator switches back or document_activate.
        return {
            "kind": "active_document_changed",
            "bound": changed["bound"],
            "active": changed["active"],
            "application_changed": changed.get("application_changed", False),
            "message": message,
        }
    capability = capability_of(exc)
    if capability:
        return {"kind": "unsupported", "capability": capability, "message": message}
    quarantine = quarantine_of(exc)
    if quarantine is not None:
        if refusal_kind(exc) == "busy":
            return {"kind": "busy", "detached_read": quarantine, "message": message}
        return {"kind": "quarantined", "quarantine": quarantine, "message": message}
    stale = stale_of(exc)
    if stale is not None:
        return {
            "kind": "stale_revision",
            "expected_revision": stale["expected_revision"],
            "actual_revision": stale["actual_revision"],
            "changed": stale["changed"],
            "message": message,
        }
    gate = _first_of(exc, GateFailure)
    if gate is not None:
        extras = {k: v for k, v in gate.payload.items() if k != "kind"}
        return {"kind": gate.kind, **extras, "message": message}
    refused = _first_of(exc, CommandRefusedError)
    if refused is not None:
        row = {
            "kind": "policy_refused",
            "channel": refused.channel,
            "verbs": list(refused.verbs),
            "alternatives": list(refused.alternatives),
            "message": message,
        }
        if refused.symbols:
            row["symbols"] = list(refused.symbols)
        return row
    for typed in (PathRefusedError, ApplicationBusyError, EntityNotFoundError, InvalidArgsError):
        found = _first_of(exc, typed)
        if found is not None:
            row = {"kind": typed.kind, "message": message}
            if typed is EntityNotFoundError:
                row["handle"] = found.handle
            return row
    found = _file_error_of(exc)
    if found is not None:
        # 1.8-04: a damaged or protected drawing, a save that did not land. By
        # type; the instance's own ``kind`` (a SaveLockedError is an
        # AtomicWriteError whose kind is save_locked), and its own sentence.
        return {"kind": str(found.kind), "message": compact_message(str(found))}
    found = _checkpoint_error_of(exc)
    if found is not None:
        # 1.8-08: by type; the instance's own kind (not_found vs evicted).
        return {"kind": str(found.kind), "message": compact_message(str(found))}
    incomplete = _first_of(exc, CommandIncompleteError)
    if incomplete is not None:
        return {
            "kind": "command_incomplete",
            "command": incomplete.command,
            "last_prompt": incomplete.last_prompt,
            "message": message,
        }
    chain = cause_chain(exc)
    if any(isinstance(error, NotFoundError) for error in chain):
        return {"kind": "unknown_tool", "message": message}
    if any(isinstance(error, (FastMCPValidationError, PydanticValidationError)) for error in chain):
        return {"kind": "invalid_args", "message": message}
    if any(isinstance(error, FileNotFoundError) for error in chain):
        return {"kind": "file_not_found", "message": message}
    if isinstance(chain[-1], ToolError):
        # call_tool re-raises a FastMCPError untouched, so an *unwrapped*
        # ToolError is the tool itself declining on purpose.
        return {"kind": "refused", "message": message}
    if type(chain[-1]) in _BUG_TYPES:
        # Exact builtins only: a library's own KeyError subclass (ezdxf's
        # DXFKeyError for a missing table entry) is a failed operation, not a bug.
        return {"kind": "internal", "message": message}
    return {"kind": "failed", "message": message}
