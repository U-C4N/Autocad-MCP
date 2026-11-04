"""``drawing_history`` and the checkpoint side of ``drawing_diff`` (1.8-10).

The durable journal (1.8-03) keeps one row per call: tool, state, revisions,
checkpoints (1.8-08), the caller's label, the bounded arguments
(``JOURNAL_ARGS=bounded``), the document's lineage and, for a mutating call,
its change summary (``backends/change_sets.py``). This module reads the rows of
the active document's ``document_id`` or lineage - every run's rows: a
reopened file continues its lineage (1.8-04; headless its ``document_id``
too, live the id is per bind), so history survives a server restart - and, for
``detail="changes"``, diffs the checkpoints around an operation with
``engineering/understand/diff.py::diff_snapshots``.

Three shapes, smallest first:

* ``row``: ``{seq, op, tool, state, rev, cp, label, t}`` - ``rev`` the
  revision the call left (before, when it recorded none), ``cp`` the newest
  checkpoint when it ended and ``label`` only when there is one, ``t`` its
  start in UTC. Ten rows stay under 400 tokens.
* ``args``: each row plus the arguments the journal kept (``args_kept`` says
  how: ``bounded`` JSON up to 4 KB, ``digest`` a sha256 only, ``off`` /
  ``memory`` nothing).
* ``changes``: each row plus its change summary, and for at most
  ``DIFF_OPERATIONS`` rows the geometry diff between the newest checkpoint at
  or before the call's starting revision and the oldest one at or after its
  ending revision (the current document when no checkpoint is that new).
  ``span.exact`` is false when those checkpoints also hold other calls' work:
  the store keeps states at saves, batches and labels, not after every call.
  A call whose final state a kill lost (``unknown_commit``, ``process_exit``:
  the journal flushes final states in batches) is bounded by the revision the
  next call began at (``span.after_inferred``).

Refused by name, never guessed: a ``label`` no row of the document carries
(``invalid_args``, naming the labels it has), an unknown ``detail``, and no
open document (``refused``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import tempfile
import time
from typing import Any

from fastmcp.exceptions import ToolError

from engineering.errors import InvalidArgsError

__all__ = [
    "DETAILS",
    "DIFF_OPERATIONS",
    "DIFF_ROWS",
    "checkpoint_revision",
    "history",
]

DETAILS = ("row", "args", "changes")
#: ``detail="changes"`` diffs at most this many operations (each reads two DXFs).
DIFF_OPERATIONS = 3
#: Rows kept per diff list (changed / added / removed).
DIFF_ROWS = 20


def _stamp(started: float | None) -> str | None:
    if started is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(started)))


def _row(raw: dict) -> dict:
    after = raw.get("revision_after")
    out: dict[str, Any] = {
        "seq": raw.get("seq"),
        "op": raw.get("operation_id"),
        "tool": raw.get("tool"),
        "state": raw.get("state"),
        "rev": after if after is not None else raw.get("revision_before"),
    }
    if raw.get("checkpoint_after"):
        out["cp"] = raw["checkpoint_after"]
    if raw.get("label"):
        out["label"] = raw["label"]
    out["t"] = _stamp(raw.get("started"))
    return out


def _json(text: str | None):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _args_mode(journal) -> str:
    settings = getattr(journal, "settings", None)
    if settings is None or getattr(journal, "_connection", None) is None:
        return "memory"
    return str(settings.args)


async def history(
    backend,
    journal,
    *,
    limit: int = 10,
    since_seq: int | None = None,
    tool: str | None = None,
    query: str | None = None,
    handle: str | None = None,
    label: str | None = None,
    detail: str = "row",
    read_only=None,
) -> dict:
    """The active document's operations, newest first (module docstring)."""
    if detail not in DETAILS:
        raise InvalidArgsError(
            f"invalid_args: detail must be one of {', '.join(DETAILS)}; got {detail!r}"
        )
    identity = backend.document_identity() if backend is not None else {}
    document_id = (identity or {}).get("document_id")
    if not document_id:
        raise ToolError(
            "drawing_history: no document is open. drawing_open the file - a reopened "
            "file continues its history across server restarts - then ask again."
        )
    lineage_id = None
    reader = getattr(backend, "active_lineage", None)
    if callable(reader):
        try:
            lineage_id = reader()
        except Exception:  # a lineage widens the search; never a reason to fail it
            lineage_id = None
    label = label.strip() if isinstance(label, str) and label.strip() else None
    if label is not None:
        known = journal.labels(document_id, lineage_id)
        if label not in known:
            shown = ", ".join(repr(k) for k in known[-10:]) or "none"
            raise InvalidArgsError(
                f"invalid_args: label {label!r} names no operation of this document; "
                f"labels it has: {shown}. A label is given with _meta.label, "
                "cad_batch(label=) or checkpoint_create(label)."
            )
    raw = journal.history(
        document_id,
        limit=int(limit) + 1,
        since_seq=since_seq,
        tool=tool or None,
        label=label,
        query=query or None,
        handle=handle or None,
        skip=read_only,
        lineage_id=lineage_id,
    )
    more = len(raw) > int(limit)
    raw = raw[: int(limit)]
    rows = [_row(r) for r in raw]
    out: dict[str, Any] = {"document_id": document_id, "rows": rows, "more": more}
    if detail == "args":
        out["args_kept"] = _args_mode(journal)
        for row, source in zip(rows, raw, strict=True):
            row["args"] = _json(source.get("args_json"))
    elif detail == "changes":
        for row, source in zip(rows, raw, strict=True):
            changes = _json(source.get("changes_json"))
            if changes is not None:
                row["changes"] = changes
        context = await _diff_context(backend)
        for index, (row, source) in enumerate(zip(rows, raw, strict=True)):
            if index >= DIFF_OPERATIONS:
                row["diff"] = {
                    "available": False,
                    "reason": f"diffs are read for the first {DIFF_OPERATIONS} rows only; "
                    "narrow with label / tool / handle / since_seq",
                }
                continue
            if source.get("document_id") not in (None, document_id):
                # Live: the same file under an earlier bind, whose revisions
                # (and its checkpoints' `rev`) counted from another start.
                row["diff"] = {
                    "available": False,
                    "reason": "the call ran on an earlier binding of this file, whose "
                    "revisions are not this one's; compare checkpoints with "
                    "drawing_diff(old_checkpoint=..., new_checkpoint=...)",
                }
                continue
            if source.get("revision_after") is None and source.get("revision_before") is not None:
                # Its final state never reached the file (a kill inside the
                # journal's flush window): bounded by where the next call began.
                bound = journal.next_revision(document_id, int(source["seq"]))
                if bound is None:
                    bound = identity.get("revision")
                source = {**source, "revision_after": bound, "after_inferred": True}
            row["diff"] = await _operation_diff(backend, context, source, identity)
    return out


# ── checkpoints ──────────────────────────────────────────────────────────────


async def _diff_context(backend) -> dict:
    """The store and the active document's lineage, or why there are none."""
    from backends.base import UnsupportedCapabilityError
    from backends.checkpoints import open_store

    store = open_store()
    if store is None:
        return {"error": "no state folder: checkpoints are off (JOURNAL_DURABLE=off?)"}
    try:
        listing = await backend.checkpoint_list(limit=1)
    except UnsupportedCapabilityError as exc:
        return {"error": str(exc)}
    lineage_id = (listing or {}).get("lineage_id")
    rows = store.records(lineage_id) if lineage_id else []
    return {"store": store, "lineage_id": lineage_id, "rows": rows}


def _blank_snapshot(source: str):
    import ezdxf

    from engineering.understand.snapshot import records_from_doc

    return records_from_doc(ezdxf.new(dxfversion="R2010"), source=source)


def _read_bytes(data: bytes | None, source: str, tol: float):
    """``(snapshot, block signatures)`` of checkpoint bytes (None: a blank S0)."""
    from engineering.understand.diff import read_revision

    if data is None:
        return _blank_snapshot(source), None
    fd, scratch = tempfile.mkstemp(suffix=".dxf", prefix="acadmcp_hist_")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        snap, blocks = read_revision(scratch, tol=tol)
        return dataclasses.replace(snap, source=source), blocks
    finally:
        try:
            os.unlink(scratch)
        except OSError:
            pass


async def checkpoint_revision(backend, ref, *, tol: float, role: str):
    """``(snapshot, block signatures)`` of the active document's checkpoint ``ref``
    (its id ``cp-3``, its seq, or its label) - ``drawing_diff``'s
    ``old_checkpoint`` / ``new_checkpoint``. Refused by kind
    (``checkpoint_not_found`` / ``checkpoint_evicted``) or without a store
    (``checkpoints``)."""
    from backends.base import UnsupportedCapabilityError

    context = await _diff_context(backend)
    if "error" in context:
        raise UnsupportedCapabilityError("checkpoints", f"{role}: {context['error']}")
    store, lineage_id = context["store"], context["lineage_id"]
    row = store.resolve(lineage_id, ref)
    data = store.read(lineage_id, row)
    return await asyncio.to_thread(_read_bytes, data, f"checkpoint:{row['cp']}", tol)


async def _operation_diff(backend, context: dict, source: dict, identity: dict) -> dict:
    from engineering.understand.diff import DEFAULT_TOL, cap_report, diff_snapshots
    from engineering.understand.snapshot import take_snapshot

    if "error" in context:
        return {"available": False, "reason": context["error"]}
    before = source.get("revision_before")
    after = source.get("revision_after")
    if before is None or after is None:
        return {"available": False, "reason": "the operation recorded no revision"}
    if int(after) == int(before):
        return {"available": False, "reason": "the operation left the content revision as it was"}
    rows = [r for r in context["rows"] if not r.get("evicted")]
    older = [r for r in rows if int(r.get("rev", -1)) <= int(before)]
    newer = [r for r in rows if int(r.get("rev", -1)) >= int(after)]
    if not older:
        return {
            "available": False,
            "reason": f"no kept checkpoint at or before revision {before}; the store holds "
            "states at saves, batches and checkpoint_create",
        }
    start = max(older, key=lambda r: (int(r["rev"]), int(r["seq"])))
    current = identity.get("revision")
    end = min(newer, key=lambda r: (int(r["rev"]), int(r["seq"]))) if newer else None
    store, lineage_id = context["store"], context["lineage_id"]
    try:
        old_snap, _ = await asyncio.to_thread(
            _read_bytes, store.read(lineage_id, start), f"checkpoint:{start['cp']}", DEFAULT_TOL
        )
        if end is not None:
            new_snap, _ = await asyncio.to_thread(
                _read_bytes, store.read(lineage_id, end), f"checkpoint:{end['cp']}", DEFAULT_TOL
            )
            to, to_rev = end["cp"], int(end["rev"])
        elif current is not None and int(current) >= int(after):
            new_snap = await take_snapshot(backend, None)
            to, to_rev = "current", int(current)
        else:
            return {
                "available": False,
                "reason": f"no kept checkpoint at or after revision {after}",
            }
    except Exception as exc:  # an unreadable checkpoint is reported, never fatal
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    report = cap_report(diff_snapshots(old_snap, new_snap), DIFF_ROWS)
    return {
        "available": True,
        "span": {
            "from": start["cp"],
            "from_rev": int(start["rev"]),
            "to": to,
            "to_rev": to_rev,
            "exact": int(start["rev"]) == int(before)
            and to_rev == int(after)
            and not source.get("after_inferred"),
            **({"after_inferred": True} if source.get("after_inferred") else {}),
        },
        "summary": report["summary"],
        "changed": report["changed"],
        "added": report["added"],
        "removed": report["removed"],
        "truncated": {k: v for k, v in report["truncated"].items() if v and k != "clusters"},
    }
