"""The headless engine's half of 1.8-08: checkpoints anchored on saves.

``EzdxfBackend`` mixes this in. Every helper named ``_cp_*`` runs inside a
worker job that already holds the document lock (``EzdxfBackend._async``);
the three tool methods take the lock themselves. The store is the pure
``backends/checkpoints.py``; this module decides *when* a checkpoint is taken
and what an undo or a restore does with one.

When (zero cost on an ordinary call - nothing here runs per mutation):

* ``drawing_new``: S0, a blank row (no file - the engine rebuilds the blank
  drawing on restore), or the template's bytes; re-anchored on the
  bootstrapped drawing when the ``drawing_new`` tool's bootstrap ran
  (``checkpoint_anchor_new``); held in memory until the lineage writes its
  first checkpoint, so a drawing that is never saved writes nothing, and no
  journal row names it until then;
* ``drawing_open``: S0, the bytes just read - skipped when the lineage's
  newest checkpoint already holds them (a reopen after a save);
* ``drawing_save`` / ``drawing_save_as``: the bytes the atomic save just wrote,
  gzip level 1, no second serialisation;
* ``transaction_commit`` / ``transaction_rollback`` (and so
  ``cad_batch(on_error="rollback")``): the snapshot ``transaction_begin``
  wrote is moved into the store instead of deleted;
* ``checkpoint_create(label)``; before an in-place restore, the state it
  replaces (``pre_restore`` / ``pre_undo``), so a restore is undoable.

Undo. ``drawing_undo`` (after ``EZDXF_UNDO_DEPTH``'s in-memory steps, when
set) restores the newest checkpoint whose revision is below the current one -
below the *effective* one, when the current content was itself reached by an
undo (``undo_base``), so a second undo keeps walking back and an undo after a
restart does not step forward onto the state the first undo left. A row whose
bytes are the current content is skipped (a reopen right after a save would
otherwise "undo" to itself). It reports ``granularity`` (``save`` / ``batch``
/ ``label``): the store holds states at saves, batches and labels, not after
every call. ``drawing_redo`` restores what an undo replaced, in the session.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from pathlib import Path

from backends.checkpoints import (
    GRANULARITY,
    UNDO_SKIPS,
    CheckpointStore,
    open_store,
    public_row,
)

log = logging.getLogger("autocad_mcp.checkpoints")

RESTORE_MODES = ("in_place", "new_document")


class EzdxfCheckpoints:
    """Checkpoint hooks and tools for ``EzdxfBackend`` (see the module docstring)."""

    # ── plumbing ──

    def _cp_store(self) -> CheckpointStore | None:
        return open_store()

    def latest_checkpoint(self) -> str | None:
        if self._active_key is None:
            return None
        return self._state.latest_cp

    def active_lineage(self) -> str | None:
        """The active document's lineage (1.8-04), what a journal row and
        ``drawing_history`` (1.8-10) key on; None before it has one."""
        if self._active_key is None:
            return None
        return self._state.lineage_id or None

    @staticmethod
    def _cp_effective(state) -> int:
        """The revision an undo steps back from (see the module docstring)."""
        if state.undo_base is not None and state.undo_base_rev == state.revision:
            return int(state.undo_base)
        return int(state.revision)

    @staticmethod
    def _cp_doc_bytes(doc) -> bytes:
        """The document as DXF bytes, read without changing it.

        ``Drawing.write`` first runs ``update_all``, which on a drawing never
        written creates two APPIDs and a DICTIONARYVAR - three handles - so a
        checkpoint would shift every handle drawn after it (a fresh drawing's
        first entity would no longer be ``3E``). The sections are exported
        directly instead; the next save adds those objects as it always does.
        """
        from ezdxf.lldxf.const import DXF12
        from ezdxf.lldxf.tagwriter import TagWriter

        doc.commit_pending_changes()
        handles = True
        if doc.dxfversion == DXF12:
            handles = bool(doc.header.get("$HANDLING", 0))
        else:
            doc.classes.add_required_classes(doc.dxfversion)
        buffer = io.StringIO()
        doc.export_sections(TagWriter(buffer, write_handles=handles, dxfversion=doc.dxfversion))
        return buffer.getvalue().encode(doc.output_encoding, errors="dxfreplace")

    def _cp_blank_doc(self):
        import ezdxf

        from backends.ezdxf_backend import _apply_iso_dimstyle, _arial_standard_style

        doc = ezdxf.new(dxfversion="R2010")
        _apply_iso_dimstyle(doc)
        _arial_standard_style(doc)
        return doc

    def _cp_load(self, data: bytes | None):
        """A Drawing from checkpoint bytes (None: the blank drawing)."""
        if data is None:
            return self._cp_blank_doc()
        import ezdxf

        fd, scratch = tempfile.mkstemp(suffix=".dxf", prefix="acadmcp_cp_")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            return ezdxf.readfile(scratch)
        finally:
            try:
                os.unlink(scratch)
            except OSError:
                pass

    def _cp_require_store(self, operation: str) -> CheckpointStore:
        from backends.base import UnsupportedCapabilityError

        store = self._cp_store()
        if store is None:
            raise UnsupportedCapabilityError(
                "checkpoints",
                f"{operation}: checkpoints live in the state folder, and there is none - "
                "JOURNAL_DURABLE=off, or ACADMCP_STATE_DIR was refused (see "
                "system_status.state_store). Unset JOURNAL_DURABLE or point "
                "ACADMCP_STATE_DIR at a private folder; transaction_begin / "
                "transaction_rollback still give one checkpoint in memory.",
            )
        return store

    def _cp_add(
        self,
        state,
        *,
        kind: str,
        tool: str,
        data: bytes | None = None,
        source: str | os.PathLike | None = None,
        blank: bool = False,
        label: str | None = None,
        sha256: str | None = None,
        rev: int | None = None,
        base: int | None = None,
        strict: bool = False,
    ) -> dict | None:
        """One checkpoint of ``state``'s lineage; None (logged) when it cannot be written.

        ``strict`` raises instead - a tool whose whole job is the checkpoint.
        A save or a transaction never fails because its checkpoint did.
        """
        store = self._cp_require_store(tool) if strict else self._cp_store()
        if store is None:
            if source is not None:
                _unlink(source)
            return None
        if not state.lineage_id:
            from backends import lineage

            state.lineage_id = lineage.new_lineage_id()
        try:
            self._cp_materialize_s0(store, state)
            row = store.add(
                state.lineage_id,
                kind=kind,
                tool=tool,
                rev=state.revision if rev is None else rev,
                base=self._cp_effective(state) if base is None else base,
                data=data,
                source=source,
                blank=blank,
                label=label,
                sha256=sha256,
                document_id=state.document_id,
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:  # a damaged index too
            if source is not None:
                _unlink(source)
            if strict:
                raise
            log.warning("checkpoint (%s, %s) not written: %s", kind, tool, exc)
            return None
        state.latest_cp = row["cp"]
        return row

    def _cp_materialize_s0(self, store: CheckpointStore, state) -> None:
        """Write ``drawing_new``'s S0 before the lineage's first other checkpoint."""
        pending, state.s0 = state.s0, None
        if pending is not None and not store.exists(state.lineage_id):
            store.add(state.lineage_id, document_id=state.document_id, **pending)

    def _cp_rows(self, store: CheckpointStore | None, state) -> list[dict]:
        """Every row of the lineage, oldest first, with a pending S0 in front."""
        rows = store.records(state.lineage_id) if (store and state.lineage_id) else []
        if state.s0 is not None and not rows:
            pending = state.s0
            rows = [
                {
                    "cp": "cp-0",
                    "seq": 0,
                    "kind": pending["kind"],
                    "granularity": GRANULARITY[pending["kind"]],
                    "tool": pending["tool"],
                    "rev": pending["rev"],
                    "base": pending["rev"],
                    "label": None,
                    "sha256": None,
                    "blank": bool(pending.get("blank")),
                    "evicted": False,
                    "kb": 0.0,
                    "pending": True,
                }
            ]
        return rows

    def _cp_read(self, store: CheckpointStore | None, state, row: dict) -> bytes | None:
        if row.get("pending"):
            return state_s0_data(state)
        return store.read(state.lineage_id, row)

    def _cp_note_content(self, state, sha: str | None) -> None:
        state.content_sha = sha
        state.content_sha_rev = state.revision if sha else None

    def _cp_current_sha(self, state) -> str | None:
        return state.content_sha if state.content_sha_rev == state.revision else None

    # ── hooks the engine calls ──

    def _cp_on_new(self, state, template: str | None) -> None:
        """S0 of a new drawing, held until the lineage's first checkpoint.

        ``latest_cp`` stays unset: a pending S0 is not a checkpoint yet (a
        drawing that is never saved writes nothing), so no journal row may name
        it. Without a store there is no S0 at all.
        """
        from backends import lineage

        if self._cp_store() is None:
            return
        if not state.lineage_id:
            state.lineage_id = lineage.new_lineage_id()
        s0: dict = {"kind": "new", "tool": "drawing_new", "rev": int(state.revision)}
        if template and Path(template).is_file():
            try:
                s0["data"] = Path(template).read_bytes()
            except OSError as exc:
                log.warning("S0 of the template %s not kept: %s", template, exc)
                return
        else:
            s0["blank"] = True
        state.s0 = s0

    async def checkpoint_anchor_new(self) -> None:
        """Re-anchor a pending ``drawing_new`` S0 on the drawing the tool handed back.

        The ``drawing_new`` tool bootstraps the engineering linetypes and layers
        after the engine created the drawing, and those calls move the
        revision. S0 must be that bootstrapped drawing - the blank one is a
        state no tool ever returned, and restoring it would strip the layers.
        Serialised once, here, and held in memory like the blank row was.
        """

        def _sync():
            state = self._state
            pending = state.s0
            if pending is None or pending.get("kind") != "new" or self._doc is None:
                return
            if int(pending.get("rev", -1)) == state.revision:
                return  # nothing ran after the engine's drawing_new
            state.s0 = {
                "kind": "new",
                "tool": "drawing_new",
                "rev": int(state.revision),
                "data": self._cp_doc_bytes(self._doc),
            }

        await self._async(_sync, readonly=True)

    def _cp_on_open(
        self, state, path: str, sha: str | None, *, doc=None, unchanged: bool = False
    ) -> None:
        """S0 of an opened file: its bytes, unless the newest checkpoint holds them.

        ``unchanged``: the open continued the lineage at the revision its record
        names (no external change, no unsaved revisions skipped) - the newest
        checkpoint at that revision is this content even when a later save of
        the same content wrote other timestamps.
        """
        store = self._cp_store()
        if store is None or not state.lineage_id:
            return
        rows = [r for r in store.records(state.lineage_id) if not r.get("evicted")]
        newest = rows[-1] if rows else None
        same = newest is not None and (
            (sha and newest.get("sha256") == sha)
            or (unchanged and int(newest.get("rev", -1)) == state.revision)
        )
        if doc is None and same:
            state.latest_cp = newest["cp"]
            if int(newest.get("rev", -1)) == state.revision:
                state.undo_base = int(newest.get("base", newest["rev"]))
                state.undo_base_rev = state.revision
            self._cp_note_content(state, sha)
            return
        try:
            data = self._cp_doc_bytes(doc) if doc is not None else Path(path).read_bytes()
        except OSError as exc:
            log.warning("S0 of %s not kept: %s", path, exc)
            return
        row = self._cp_add(
            state, kind="open", tool="drawing_open", data=data, sha256=None if doc else sha
        )
        if row is not None:
            self._cp_note_content(state, row["sha256"])

    def _cp_on_save(self, state, written: dict, tool: str) -> str | None:
        """The bytes a save just wrote become a checkpoint; returns its id."""
        data = written.pop("data", None)
        if data is None:
            return None
        store = self._cp_store()
        if store is None:
            return None
        sha = written.get("sha256")
        try:
            rows = [r for r in self._cp_rows(store, state) if not r.get("evicted")]
            newest = rows[-1] if rows else None
            same = (
                newest is not None
                and not newest.get("pending")
                and int(newest.get("rev", -1)) == state.revision
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # The file is already written: a save never fails because of its
            # checkpoint (a damaged index row is the store's problem).
            log.warning("checkpoint of %s not written: %s", tool, exc)
            return None
        if same:
            # The same revision is the same content (a revision is never issued
            # twice); the bytes differ only by the header's save timestamps.
            self._cp_note_content(state, sha)
            return newest["cp"]
        row = self._cp_add(state, kind="save", tool=tool, data=data, sha256=sha)
        self._cp_note_content(state, sha)
        return row["cp"] if row else None

    def _cp_transaction_opened(self, state, snapshot: Path) -> None:
        state.transaction_revs[str(snapshot)] = (int(state.revision), self._cp_effective(state))

    def _cp_transaction_closed(self, state, snapshot: Path, tool: str) -> dict | None:
        """Move the transaction's snapshot into the store (instead of deleting it)."""
        rev, base = state.transaction_revs.pop(str(snapshot), (None, None))
        if rev is None:
            _unlink(snapshot)
            return None
        return self._cp_add(
            state, kind="transaction", tool=tool, source=snapshot, rev=rev, base=base
        )

    # ── restore ──

    def _cp_capture_current(self, state, kind: str, tool: str) -> str | None:
        """The current content as a checkpoint, unless a row already holds it."""
        store = self._cp_store()
        for row in reversed(self._cp_rows(store, state)):
            if row.get("evicted") or row.get("pending"):
                continue
            if int(row.get("rev", -1)) == state.revision:
                return row["cp"]
        row = self._cp_add(
            state, kind=kind, tool=tool, data=self._cp_doc_bytes(self._require_doc()), strict=True
        )
        self._cp_note_content(state, row["sha256"])
        return row["cp"]

    def _cp_replace_document(self, state, row: dict, data: bytes | None) -> None:
        """Swap the active document's contents for ``row``'s - one content change."""
        from backends.ezdxf_backend import _normalise_dimstyle_rounding

        doc = self._cp_load(data)
        _normalise_dimstyle_rounding(doc)
        if state.path:
            doc.filename = state.path
        self._doc = doc
        self._current_layer = str(doc.header.get("$CLAYER", "0"))
        self._dirty = True
        # The in-memory per-call history described the replaced document (an
        # open transaction's snapshot is kept: its rollback still means "back
        # to the begin").
        self._discard(state.undo_stack)
        self._discard(state.redo_stack)
        self._push_undo_snapshot()
        self._cp_note_content(state, row.get("sha256"))

    def _cp_undo_sync(self) -> dict:
        state = self._state
        self._require_doc()
        store = self._cp_store()
        effective = self._cp_effective(state)
        current_sha = self._cp_current_sha(state)
        rows = self._cp_rows(store, state)
        candidates = [
            row
            for row in rows
            if not row.get("evicted")
            and row.get("kind") not in UNDO_SKIPS
            and int(row.get("rev", 0)) < effective
            and not (current_sha and row.get("sha256") == current_sha)
        ]
        if not candidates:
            self._cp_redo_stack(state)  # drops a redo branch a later edit made stale
            evicted = [r for r in rows if r.get("evicted") and int(r.get("rev", 0)) < effective]
            message = f"Nothing to undo - no checkpoint below revision {effective}" + (
                f" is kept ({len(evicted)} older were evicted by the CHECKPOINT_* budget)."
                if evicted
                else "; the store holds states at saves, batches and checkpoint_create."
            )
            return {
                "ok": False,
                "error": message,
                "source": "checkpoint",
                "undo_depth": 0,
                "redo_depth": self._cp_redo_depth(state),
            }
        target = max(candidates, key=lambda r: (int(r.get("rev", 0)), int(r.get("seq", 0))))
        data = self._cp_read(store, state, target)
        redo = self._cp_redo_stack(state)
        before = self._cp_capture_current(state, "pre_undo", "drawing_undo")
        # The in-memory steps run first, so an undo that crosses into the store
        # can leave a memory redo branch behind (the states those undos
        # replaced, possibly never saved). It is set aside, not discarded: the
        # redo of `before` hands it back, so redo retraces every undo.
        branch = list(state.redo_stack)
        del state.redo_stack[:]
        try:
            self._cp_replace_document(state, target, data)
        except BaseException:
            state.redo_stack[:0] = branch
            raise
        if branch:
            stale = state.parked_redo.pop(before, None)
            if stale:
                self._discard(stale)
            state.parked_redo[before] = branch
        state.undo_base = int(target.get("base", target["rev"]))
        state.undo_base_rev = state.revision
        redo.append(before)
        state.store_redo = redo
        remaining = sum(1 for r in candidates if int(r.get("rev", 0)) < state.undo_base)
        return {
            "ok": True,
            "verified": True,
            "message": (
                f"Stepped back to checkpoint {target['cp']} (revision {target['rev']}, "
                f"{target.get('granularity')})."
            ),
            "source": "checkpoint",
            "checkpoint": target["cp"],
            "granularity": target.get("granularity") or GRANULARITY.get(target.get("kind"), "save"),
            "restored_revision": int(target["rev"]),
            "revision": state.revision,
            "checkpoint_before": before,
            "undo_depth": remaining,
            "redo_depth": self._cp_redo_depth(state),
        }

    def _cp_redo_stack(self, state) -> list[str]:
        """The undo-replaced states, valid while nothing else changed the document."""
        if (state.store_redo or state.parked_redo) and state.undo_base_rev != state.revision:
            self._cp_drop_redo(state)
        return list(state.store_redo)

    def _cp_drop_redo(self, state) -> None:
        """Abandon the store's redo branch and the memory branches it set aside."""
        state.store_redo = []
        for branch in state.parked_redo.values():
            self._discard(branch)
        state.parked_redo.clear()

    @staticmethod
    def _cp_redo_depth(state) -> int:
        """Redo steps left: the store's, the memory ones set aside, the live memory ones."""
        parked = sum(len(branch) for branch in state.parked_redo.values())
        return len(state.store_redo) + parked + len(state.redo_stack)

    def _cp_redo_sync(self) -> dict:
        state = self._state
        self._require_doc()
        redo = self._cp_redo_stack(state)
        if not redo:
            return {
                "ok": False,
                "error": "Nothing to redo — nothing has been undone, or a later edit "
                "discarded the branch.",
                "source": "checkpoint",
                "redo_depth": 0,
            }
        store = self._cp_require_store("drawing_redo")
        cp = redo.pop()
        row = store.resolve(state.lineage_id, cp)
        data = store.read(state.lineage_id, row)
        self._cp_replace_document(state, row, data)
        state.undo_base = int(row.get("base", row["rev"]))
        state.undo_base_rev = state.revision
        state.store_redo = redo
        # The memory redo branch the crossing undo set aside: the next redo
        # continues there (drawing_redo tries the memory stack first).
        branch = state.parked_redo.pop(cp, None)
        if branch:
            state.redo_stack.extend(branch)
        return {
            "ok": True,
            "verified": True,
            "message": f"Reapplied the state an undo replaced ({cp}).",
            "source": "checkpoint",
            "checkpoint": cp,
            "revision": state.revision,
            "redo_depth": self._cp_redo_depth(state),
        }

    # ── the three tools ──

    async def checkpoint_create(self, label: str | None = None) -> dict:
        """A checkpoint of the active document now (``kind: label``)."""
        if label is not None and (not isinstance(label, str) or len(label) > 200):
            raise ValueError("checkpoint_create: label must be a string of at most 200 characters")

        def _sync():
            state = self._state
            doc = self._require_doc()
            row = self._cp_add(
                state,
                kind="label",
                tool="checkpoint_create",
                data=self._cp_doc_bytes(doc),
                label=label or None,
                strict=True,
            )
            self._cp_note_content(state, row["sha256"])
            return {
                "ok": True,
                **public_row(row),
                "granularity": row["granularity"],
                "document_id": state.document_id,
                "lineage_id": state.lineage_id,
            }

        return await self._async(_sync)

    async def checkpoint_list(self, limit: int = 10) -> dict:
        def _sync():
            state = self._state
            self._require_doc()
            store = self._cp_require_store("checkpoint_list")
            rows = self._cp_rows(store, state)
            shown = [public_row(r) for r in reversed(rows)][: max(0, int(limit))]
            return {
                "ok": True,
                "document_id": state.document_id,
                "lineage_id": state.lineage_id or None,
                "revision": state.revision,
                "count": len(rows),
                "evicted": sum(1 for r in rows if r.get("evicted")),
                "checkpoints": shown,
            }

        return await self._async(_sync, readonly=True)

    async def checkpoint_restore(self, checkpoint, mode: str = "in_place") -> dict:
        if mode not in RESTORE_MODES:
            raise ValueError(
                f"checkpoint_restore: mode must be one of {RESTORE_MODES}, got {mode!r}"
            )

        def _resolve():
            state = self._state
            self._require_doc()
            store = self._cp_require_store("checkpoint_restore")
            rows = self._cp_rows(store, state)
            pending = rows[0] if rows and rows[0].get("pending") else None
            if pending is not None and str(checkpoint) in ("0", "cp-0"):
                return state, store, pending
            return state, store, store.resolve(state.lineage_id, checkpoint)

        if mode == "new_document":

            def _write():
                state, store, row = _resolve()
                data = self._cp_read(store, state, row)
                if data is None:
                    data = self._cp_doc_bytes(self._cp_blank_doc())
                stem = Path(state.path).stem if state.path else "untitled"
                folder = tempfile.mkdtemp(prefix="acadmcp-cp-")
                target = os.path.join(folder, f"{stem}-cp{row['seq']}.dxf")
                with open(target, "wb") as stream:
                    stream.write(data)
                return row, target, state.lineage_id

            row, target, source_lineage = await self._async(_write)
            opened = await self.drawing_open(target)
            return {
                **opened,
                "ok": True,
                "mode": "new_document",
                "restored": public_row(row),
                "granularity": row.get("granularity"),
                "source_lineage": source_lineage,
            }

        def _sync():
            state, store, row = _resolve()
            data = self._cp_read(store, state, row)
            before = self._cp_capture_current(state, "pre_restore", "checkpoint_restore")
            self._cp_replace_document(state, row, data)
            # A restore is undone by drawing_undo: the next undo steps back to
            # `before`, not past it, so no undo base is carried over.
            state.undo_base = None
            state.undo_base_rev = None
            self._cp_drop_redo(state)
            return {
                "ok": True,
                "mode": "in_place",
                "restored": public_row(row),
                "granularity": row.get("granularity"),
                "revision": state.revision,
                "checkpoint_before": before,
                "undo": f"drawing_undo returns to {before}, the state this restore replaced",
            }

        return await self._async(_sync)


def state_s0_data(state) -> bytes | None:
    pending = state.s0 or {}
    return pending.get("data")


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)
