"""Save-anchored durable checkpoints (1.8-08, durability-2 as the two reviewers narrowed it).

A pure store: no backend, no server import. A checkpoint is the bytes of one
DXF, gzip level 1, at ``<STATE_DIR>/docs/<lineage_id>/cp/cp-<seq>.dxf.gz``,
described by one row of ``<STATE_DIR>/docs/<lineage_id>/index.json``. The
lineage is 1.8-04's (``backends/lineage.py``), so a checkpoint written by one
process is found by the next one that opens the same file.

Where the bytes come from - never a second serialisation of a save:

* ``save``: the bytes ``drawing_save`` / ``drawing_save_as`` just wrote
  (``atomic_write_dxf(keep_data=True)``);
* ``open``: the file ``drawing_open`` just read (skipped when the lineage's
  newest checkpoint already holds those bytes);
* ``new``: ``drawing_new``'s blank drawing - a row with no file (``blank``),
  rebuilt by the engine on restore; with a template, the template's bytes;
* ``transaction``: the DXF snapshot headless ``transaction_begin`` already
  writes for its rollback, moved into the store when the transaction closes
  instead of being deleted (``cad_batch(on_error="rollback")`` is one);
* ``label``: ``checkpoint_create(label)``; ``pre_restore`` / ``pre_undo``: the
  state a restore replaces, so the restore itself can be undone; the live
  engine's ``Document.Export`` at its boundaries (``batch``, ``destructive``).

A row is ``{cp, seq, kind, tool, rev, base, kb, bytes, label, sha256,
created, document_id, blank, evicted}``. ``rev`` is the document's content
revision the bytes hold; ``base`` the revision ``drawing_undo`` steps back
from when that content is current (smaller than ``rev`` when the content was
itself reached by an undo). ``granularity`` maps the kind onto what an undo
reports: ``save`` / ``batch`` / ``label``.

Eviction (1.8-03's settings, consumed here): per lineage, the newest
``CHECKPOINT_KEEP`` rows besides S0 (seq 0, always kept by this rule) and at
most ``CHECKPOINT_MAX_DOC_MB`` of files; across lineages, the least recently
written lineage first, oldest row first, until the folder holds at most
``CHECKPOINT_BUDGET_MB`` - never the row just written, so the disk stays at or
under the budget plus one checkpoint. An evicted row stays in the index with
``evicted: true`` and its file gone, and is refused by name
(``checkpoint_evicted``), never mistaken for one that never existed.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("autocad_mcp.checkpoints")

__all__ = [
    "CHECKPOINT_KINDS",
    "DOCS_DIR",
    "GRANULARITY",
    "CheckpointError",
    "CheckpointEvictedError",
    "CheckpointNotFoundError",
    "CheckpointSettings",
    "CheckpointStore",
    "open_store",
    "public_row",
]

DOCS_DIR = "docs"
INDEX_FILE = "index.json"
CP_DIR = "cp"
GZIP_LEVEL = 1
_MB = 1024 * 1024

#: kind -> the granularity an undo reports for a checkpoint of that kind.
GRANULARITY: dict[str, str] = {
    "save": "save",
    "open": "save",
    "new": "save",
    "transaction": "batch",
    "batch": "batch",
    "destructive": "batch",
    "pre_restore": "batch",
    "pre_undo": "batch",
    "label": "label",
}
CHECKPOINT_KINDS: tuple[str, ...] = tuple(GRANULARITY)
#: Rows ``drawing_undo`` never steps onto: the state an undo itself replaced
#: (``drawing_redo`` and ``checkpoint_restore`` reach it).
UNDO_SKIPS: frozenset[str] = frozenset({"pre_undo"})


class CheckpointError(RuntimeError):
    """A checkpoint reference the store cannot honour, typed by ``kind``."""

    kind = "checkpoint_not_found"


class CheckpointNotFoundError(CheckpointError):
    kind = "checkpoint_not_found"

    def __init__(self, ref: Any, lineage_id: str | None, known: list[str] | None = None):
        self.ref = ref
        shown = ", ".join((known or [])[-5:]) or "none"
        super().__init__(
            f"checkpoint {ref!r} is not a checkpoint of this document (lineage "
            f"{lineage_id or 'none'}); checkpoint_list names them - newest: {shown}"
        )


class CheckpointEvictedError(CheckpointError):
    kind = "checkpoint_evicted"

    def __init__(self, row: Mapping):
        self.row = dict(row)
        why = row.get("evicted_by") or "budget"
        super().__init__(
            f"checkpoint {row.get('cp')} (revision {row.get('rev')}) was evicted by the "
            f"CHECKPOINT_* budget ({why}); its bytes are gone. checkpoint_list shows what is "
            "kept; raise CHECKPOINT_KEEP / CHECKPOINT_BUDGET_MB / CHECKPOINT_MAX_DOC_MB to "
            "keep more."
        )


@dataclass(frozen=True)
class CheckpointSettings:
    keep: int = 20
    budget_mb: int = 512
    max_doc_mb: int = 32

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CheckpointSettings:
        from backends.journal_store import JournalSettingError, JournalSettings

        try:
            parsed = JournalSettings.from_env(environ)
        except JournalSettingError as exc:
            log.warning("checkpoint settings: %s - the defaults apply", exc)
            return cls()
        return cls(
            keep=parsed.checkpoint_keep,
            budget_mb=parsed.checkpoint_budget_mb,
            max_doc_mb=parsed.checkpoint_max_doc_mb,
        )


def public_row(row: Mapping) -> dict:
    """What ``checkpoint_list`` shows: ``{cp, seq, tool, rev, kb, label, evicted}``."""
    return {
        "cp": row.get("cp"),
        "seq": row.get("seq"),
        "tool": row.get("tool"),
        "rev": row.get("rev"),
        "kb": row.get("kb"),
        "label": row.get("label"),
        "evicted": bool(row.get("evicted")),
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")
    with open(scratch, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    os.replace(scratch, path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("checkpoint file %s not removed: %s", path, exc)


class CheckpointStore:
    """The checkpoints of every lineage under one state folder."""

    def __init__(self, root: str | os.PathLike, settings: CheckpointSettings | None = None):
        self.root = Path(root)
        self.settings = settings or CheckpointSettings()

    # ── layout ──

    def lineage_dir(self, lineage_id: str) -> Path:
        if not lineage_id or any(c in lineage_id for c in "\\/:.") or len(lineage_id) > 64:
            raise ValueError(f"not a lineage id: {lineage_id!r}")
        return self.root / DOCS_DIR / lineage_id

    def _index_path(self, lineage_id: str) -> Path:
        return self.lineage_dir(lineage_id) / INDEX_FILE

    def _file(self, lineage_id: str, row: Mapping) -> Path:
        return self.lineage_dir(lineage_id) / CP_DIR / str(row["file"])

    def load(self, lineage_id: str) -> dict:
        """The lineage's index (an empty one when it has none)."""
        empty = {"lineage_id": lineage_id, "next_seq": 0, "checkpoints": []}
        try:
            data = json.loads(self._index_path(lineage_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        except (OSError, ValueError) as exc:
            log.warning("unreadable checkpoint index of %s: %s", lineage_id, exc)
            return empty
        if not isinstance(data, dict) or not isinstance(data.get("checkpoints"), list):
            return empty
        return data

    def exists(self, lineage_id: str) -> bool:
        return self._index_path(lineage_id).is_file()

    def records(self, lineage_id: str) -> list[dict]:
        """Every row of the lineage, oldest first (evicted rows included)."""
        return list(self.load(lineage_id)["checkpoints"])

    # ── write ──

    def add(
        self,
        lineage_id: str,
        *,
        kind: str,
        tool: str,
        rev: int,
        base: int | None = None,
        data: bytes | None = None,
        source: str | os.PathLike | None = None,
        blank: bool = False,
        label: str | None = None,
        sha256: str | None = None,
        document_id: str | None = None,
    ) -> dict:
        """One checkpoint: ``data`` (DXF bytes), a ``source`` file moved in, or ``blank``."""
        if kind not in GRANULARITY:
            raise ValueError(f"unknown checkpoint kind {kind!r}")
        if not blank and data is None and source is None:
            raise ValueError("a checkpoint needs data, a source file or blank=True")
        index = self.load(lineage_id)
        seq = int(index.get("next_seq") or 0)
        folder = self.lineage_dir(lineage_id) / CP_DIR
        row: dict[str, Any] = {
            "cp": f"cp-{seq}",
            "seq": seq,
            "kind": kind,
            "granularity": GRANULARITY[kind],
            "tool": tool,
            "rev": int(rev),
            "base": int(rev if base is None else min(int(base), int(rev))),
            "label": label,
            "sha256": sha256,
            "document_id": document_id,
            "created": time.time(),
            "blank": bool(blank),
            "evicted": False,
            "bytes": 0,
            "kb": 0.0,
            "file": None,
        }
        if not blank:
            if data is None:
                data = Path(source).read_bytes()
            if sha256 is None:
                import hashlib

                row["sha256"] = hashlib.sha256(data).hexdigest()
            packed = gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)
            folder.mkdir(parents=True, exist_ok=True)
            name = f"cp-{seq}.dxf.gz"
            scratch = folder / f"{name}.{uuid.uuid4().hex[:8]}.part"
            with open(scratch, "wb") as stream:
                stream.write(packed)
            os.replace(scratch, folder / name)
            row.update(file=name, bytes=len(packed), kb=round(len(packed) / 1024, 1))
            if source is not None:
                _unlink(Path(source))
        index["next_seq"] = seq + 1
        index["checkpoints"].append(row)
        index["updated"] = time.time()
        self._evict_lineage(lineage_id, index, keep_seq=seq)
        _write_json(self._index_path(lineage_id), index)
        self._evict_budget(lineage_id, keep_seq=seq)
        return dict(row)

    # ── read ──

    def resolve(self, lineage_id: str | None, ref: Any) -> dict:
        """The row ``ref`` names: a seq (int or digits), ``cp-<seq>`` or a label."""
        rows = self.records(lineage_id) if lineage_id else []
        found = None
        if isinstance(ref, bool):
            ref = str(ref)
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            seq = int(ref)
            found = next((r for r in rows if r.get("seq") == seq), None)
        elif isinstance(ref, str) and ref.strip():
            text = ref.strip()
            found = next((r for r in rows if r.get("cp") == text), None)
            if found is None:
                labelled = [r for r in rows if r.get("label") == text]
                found = labelled[-1] if labelled else None
        if found is None:
            raise CheckpointNotFoundError(ref, lineage_id, [str(r.get("cp")) for r in rows])
        if found.get("evicted"):
            raise CheckpointEvictedError(found)
        return dict(found)

    def read(self, lineage_id: str, row: Mapping) -> bytes | None:
        """The DXF bytes of ``row`` (None for a blank row); evicted -> refused by name."""
        if row.get("evicted"):
            raise CheckpointEvictedError(row)
        if row.get("blank"):
            return None
        path = self._file(lineage_id, row)
        try:
            return gzip.decompress(path.read_bytes())
        except FileNotFoundError:
            raise CheckpointEvictedError({**row, "evicted_by": "file_missing"}) from None

    def listing(self, lineage_id: str | None, limit: int = 10) -> list[dict]:
        """The newest ``limit`` rows, newest first, as ``checkpoint_list`` shows them."""
        rows = self.records(lineage_id) if lineage_id else []
        return [public_row(r) for r in reversed(rows)][: max(0, int(limit))]

    # ── eviction ──

    def _evict_row(self, lineage_id: str, row: dict, why: str) -> None:
        if row.get("file"):
            _unlink(self._file(lineage_id, row))
        row["evicted"] = True
        row["evicted_by"] = why
        row["evicted_at"] = time.time()

    def _evict_lineage(self, lineage_id: str, index: dict, *, keep_seq: int) -> None:
        live = [r for r in index["checkpoints"] if not r.get("evicted") and r.get("seq") != 0]
        excess = len(live) - max(1, int(self.settings.keep))
        for row in live:
            if excess <= 0:
                break
            if row.get("seq") == keep_seq:
                continue
            self._evict_row(lineage_id, row, "keep")
            excess -= 1
        cap = int(self.settings.max_doc_mb) * _MB
        live = [r for r in index["checkpoints"] if not r.get("evicted")]
        size = sum(int(r.get("bytes") or 0) for r in live)
        for row in live:
            if size <= cap:
                break
            if row.get("seq") == keep_seq or not row.get("bytes"):
                continue
            size -= int(row.get("bytes") or 0)
            self._evict_row(lineage_id, row, "max_doc_mb")

    def _lineage_usage(self) -> list[tuple[float, str, int]]:
        """``(last written, lineage, bytes on disk)`` for every lineage folder."""
        docs = self.root / DOCS_DIR
        out: list[tuple[float, str, int]] = []
        try:
            entries = list(os.scandir(docs))
        except FileNotFoundError:
            return out
        for entry in entries:
            if not entry.is_dir():
                continue
            size = 0
            try:
                for item in os.scandir(Path(entry.path) / CP_DIR):
                    if item.name.endswith(".dxf.gz"):
                        size += item.stat().st_size
            except FileNotFoundError:
                pass
            try:
                stamp = os.stat(Path(entry.path) / INDEX_FILE).st_mtime
            except OSError:
                stamp = 0.0
            out.append((stamp, entry.name, size))
        return out

    def total_bytes(self) -> int:
        return sum(size for _, _, size in self._lineage_usage())

    def count(self) -> int:
        """Checkpoint files across every lineage (``system_status.state_store``).

        Directory entries only, no index parsed: ``system_status`` is the most
        called diagnostic. A blank S0 row (no file) is not counted.
        """
        docs = self.root / DOCS_DIR
        total = 0
        try:
            entries = list(os.scandir(docs))
        except FileNotFoundError:
            return 0
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                total += sum(
                    1
                    for item in os.scandir(Path(entry.path) / CP_DIR)
                    if item.name.endswith(".dxf.gz")
                )
            except FileNotFoundError:
                pass
        return total

    def _evict_budget(self, current: str, *, keep_seq: int) -> None:
        budget = int(self.settings.budget_mb) * _MB
        usage = self._lineage_usage()
        total = sum(size for _, _, size in usage)
        if total <= budget:
            return
        # The least recently written lineage first; the one being written last.
        order = sorted(usage, key=lambda item: (item[1] == current, item[0]))
        for _stamp, lineage_id, _size in order:
            if total <= budget:
                break
            index = self.load(lineage_id)
            changed = False
            for row in index["checkpoints"]:
                if total <= budget:
                    break
                if row.get("evicted") or not row.get("bytes"):
                    continue
                if lineage_id == current and row.get("seq") == keep_seq:
                    continue
                total -= int(row.get("bytes") or 0)
                self._evict_row(lineage_id, row, "budget_mb")
                changed = True
            if changed:
                _write_json(self._index_path(lineage_id), index)


def open_store(environ: Mapping[str, str] | None = None) -> CheckpointStore | None:
    """The server's checkpoint store, or None when there is no usable state folder.

    The folder is 1.8-03's ``ACADMCP_STATE_DIR`` with its refusals
    (``backends.atomic_write.state_dir``); ``JOURNAL_DURABLE=off`` keeps the
    1.7 behaviour - no checkpoints, nothing written to the state folder by them.
    """
    from backends.atomic_write import state_dir

    env = os.environ if environ is None else environ
    if (env.get("JOURNAL_DURABLE") or "").strip().lower() in ("off", "false", "0", "no"):
        return None
    folder = state_dir(environ)
    if folder is None:
        return None
    return CheckpointStore(folder, CheckpointSettings.from_env(environ))
