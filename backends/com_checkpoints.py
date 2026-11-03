"""The live engine's half of 1.8-08: checkpoints through ``Document.Export``.

``ComBackend`` mixes this in. A live checkpoint is the bound document exported
as DXF (``engineering/understand/snapshot.py::_com_export``: one selection set
of everything, ``Document.Export(<tmp>/snap, "DXF", ...)``, which never
renames the document and carries model space, paper space and block
definitions), gzip level 1 into the same store the headless engine writes
(``backends/checkpoints.py``), under the bound document's lineage (1.8-04's
``lineage_id`` for a saved file; minted for an unsaved one).

When: ``checkpoint_create(label)``; at ``transaction_begin`` (so at every
``cad_batch(on_error="rollback")`` boundary) and before the destructive tools
(``entity_delete_many``, ``drawing_purge``, ``text_find_replace`` and the
three ``*_from_spec``; ``checkpoint_before``). Never per call.

Restore. ActiveX has no member that replaces a database's contents in place,
so ``mode="in_place"`` is refused with capability
``checkpoint_restore_in_place`` (reason
``activex_cannot_replace_database_contents;use_new_document``) - AutoCAD's own
UNDO is the live in-place history. ``mode="new_document"`` writes the
checkpoint as ``<stem>-cp<seq>.dxf`` in a private folder and opens it the way
``drawing_open`` does, binding the new document. Neither is a mutation of the
bound document, so no method here is in ``_MUTATING_METHODS``.

Every path here runs its COM work inside ``self._run`` jobs through
``_acad_doc()`` (the bound-document guard); the AutoCAD steps are covered by
fake-COM tests and by ``scripts/smoke_durability_com.py`` on a live seat.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from backends.checkpoints import CheckpointStore, open_store, public_row

log = logging.getLogger("autocad_mcp.checkpoints")

RESTORE_MODES = ("in_place", "new_document")
IN_PLACE_REASON = "activex_cannot_replace_database_contents;use_new_document"


class ComCheckpoints:
    """Checkpoint tools for ``ComBackend`` (see the module docstring)."""

    def _cp_store(self) -> CheckpointStore | None:
        return open_store()

    def latest_checkpoint(self) -> str | None:
        entry = self._bound_entry()
        return entry.get("latest_cp") if entry else None

    def _cp_lineage(self) -> tuple[dict, str]:
        """The bound document's entry and lineage id (minted for an unsaved document)."""
        from backends import lineage

        entry = self._bound_entry()
        if entry is None:
            raise RuntimeError(
                "No document is bound yet; call drawing_new / drawing_open (or any "
                "drawing call) first."
            )
        return entry, entry.setdefault("lineage_id", lineage.new_lineage_id())

    def _cp_require_store(self, operation: str) -> CheckpointStore:
        from backends.base import UnsupportedCapabilityError

        store = self._cp_store()
        if store is None:
            raise UnsupportedCapabilityError(
                "checkpoints",
                f"{operation}: checkpoints live in the state folder, and there is none - "
                "JOURNAL_DURABLE=off, or ACADMCP_STATE_DIR was refused (see "
                "system_status.state_store). AutoCAD's own UNDO still works on the live "
                "engine.",
            )
        return store

    async def _cp_export_bytes(self) -> bytes:
        """The bound document as DXF bytes, exported inside one guarded job."""

        def _sync():
            from engineering.understand.snapshot import _com_export

            path, folder, _source = _com_export(self, None)
            try:
                return Path(path).read_bytes()
            finally:
                shutil.rmtree(folder, ignore_errors=True)

        return await self._run(_sync)

    async def _cp_take(
        self, kind: str, tool: str, *, label: str | None = None, strict: bool = False
    ) -> dict | None:
        """Export the bound document into the store; None (logged) unless ``strict``."""
        store = self._cp_require_store(tool) if strict else self._cp_store()
        if store is None:
            return None
        try:
            await self._ensure_bound_for_checkpoint()
            entry, lineage_id = self._cp_lineage()
            data = await self._cp_export_bytes()
            identity = self.document_identity()
            row = store.add(
                lineage_id,
                kind=kind,
                tool=tool,
                rev=int(identity.get("revision") or 0),
                data=data,
                label=label,
                document_id=identity.get("document_id"),
            )
        except Exception as exc:
            if strict:
                raise
            # A checkpoint is a safety net: a boundary never fails because of it.
            log.warning("live checkpoint (%s, %s) not taken: %s", kind, tool, exc)
            return None
        entry["latest_cp"] = row["cp"]
        return row

    async def _ensure_bound_for_checkpoint(self) -> None:
        """Bind the active document when nothing is bound yet (a first call)."""
        if self._bound_entry() is None:
            await self._run(self._check_bound_sync)

    async def checkpoint_before(self, tool: str) -> str | None:
        """Before a destructive tool: one checkpoint of the bound document."""
        kind = "batch" if tool == "transaction_begin" else "destructive"
        row = await self._cp_take(kind, tool)
        return row["cp"] if row else None

    # ── the three tools ──

    async def checkpoint_create(self, label: str | None = None) -> dict:
        if label is not None and (not isinstance(label, str) or len(label) > 200):
            raise ValueError("checkpoint_create: label must be a string of at most 200 characters")
        row = await self._cp_take("label", "checkpoint_create", label=label or None, strict=True)
        entry, lineage_id = self._cp_lineage()
        return {
            "ok": True,
            **public_row(row),
            "granularity": row["granularity"],
            "document_id": self.document_identity().get("document_id"),
            "lineage_id": lineage_id,
            "method": "Document.Export",
        }

    async def checkpoint_list(self, limit: int = 10) -> dict:
        store = self._cp_require_store("checkpoint_list")
        await self._ensure_bound_for_checkpoint()
        entry = self._bound_entry() or {}
        lineage_id = entry.get("lineage_id")
        rows = store.records(lineage_id) if lineage_id else []
        identity = self.document_identity()
        return {
            "ok": True,
            "document_id": identity.get("document_id"),
            "lineage_id": lineage_id,
            "revision": identity.get("revision"),
            "count": len(rows),
            "evicted": sum(1 for r in rows if r.get("evicted")),
            "checkpoints": [public_row(r) for r in reversed(rows)][: max(0, int(limit))],
        }

    async def checkpoint_restore(self, checkpoint, mode: str = "in_place") -> dict:
        from backends.base import UnsupportedCapabilityError

        if mode not in RESTORE_MODES:
            raise ValueError(
                f"checkpoint_restore: mode must be one of {RESTORE_MODES}, got {mode!r}"
            )
        if mode == "in_place":
            raise UnsupportedCapabilityError(
                "checkpoint_restore_in_place",
                "checkpoint_restore(mode='in_place'): ActiveX has no member that replaces "
                "an open drawing's database with another's, so the live engine cannot "
                "restore in place. Use mode='new_document' (opens the checkpoint as "
                "<stem>-cp<seq> and binds it), AutoCAD's own drawing_undo, or the "
                "headless ezdxf engine, which restores in place.",
            )
        store = self._cp_require_store("checkpoint_restore")
        await self._ensure_bound_for_checkpoint()
        entry = self._bound_entry() or {}
        lineage_id = entry.get("lineage_id")
        row = store.resolve(lineage_id, checkpoint)
        data = store.read(lineage_id, row)
        if data is None:  # a blank S0 is a headless row; the live engine never writes one
            raise ValueError(f"checkpoint {row['cp']} holds no bytes to open")
        name = (self._bound or {}).get("full_name") or (self._bound or {}).get("name") or "untitled"
        stem = Path(str(name)).stem or "untitled"
        folder = tempfile.mkdtemp(prefix="acadmcp-cp-")
        target = os.path.join(folder, f"{stem}-cp{row['seq']}.dxf")
        with open(target, "wb") as stream:
            stream.write(data)
        opened = await self.drawing_open(target)
        return {
            **opened,
            "ok": True,
            "mode": "new_document",
            "restored": public_row(row),
            "granularity": row.get("granularity"),
            "source_lineage": lineage_id,
        }
