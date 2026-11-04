"""Change summaries of mutating calls (1.8-10): ``{added, deleted, counts, complete}``.

Every mutating operation's journal row, and the ``cad_batch`` result, say what
the call added and deleted. ``modified`` is 1.8.1 (a first-touch hook on the
99 dirty points works against the B10 hot path); the checkpoint diff of
``drawing_history(detail="changes")`` answers "what changed" in between.

Headless (``EzdxfChangeSets``, mixed into ``EzdxfBackend``): a handle-set diff
per layout and block record, computed once per operation and kept cheap - the
B10 bound is per call:

* ``change_mark()`` reads the entity database's next handle and the summed
  length of the tracked entity spaces (one ``len`` per layout and named
  block);
* ``change_summary(mark)`` walks only the handles issued since the mark (ezdxf
  hands them out in order, ``EntityDB.next_handle``) for ``added``, and walks
  the cached handle -> owner map for ``deleted`` only when the spaces say
  something went (``before + added - after > 0``: ezdxf destroys a deleted
  entity but keeps its database row until a purge, so the database's own size
  says nothing);
* the map is built once per document object (a full walk of the layouts and
  named block definitions) and kept current by every summary; a revision the
  map did not see (a direct backend call, outside any journalled operation)
  rebuilds it at the next mark.

A call that replaces the document object - an undo, a checkpoint restore, a
rollback - is diffed in full against the map (both are handle sets), so it
stays complete; a call that switches to another document (``drawing_open``,
``drawing_new``, ``document_activate``) has no meaningful handle diff and says
so (``document_switched: true``, ``complete: false``).

``added`` / ``deleted`` list model- and paper-space handles (at most
``LIST_CAP`` each, ``truncated`` counting the rest); ``counts`` covers every
layout and named block definition (``"block:NAME"``). Anonymous blocks
(``*D`` dimension renderings, ``*U``, ``*X``) are left out: their names are
not stable and they are a rendering of the entity that owns them.

Live (``summary_from_result``): from the handles the call returned -
``handle``, ``handles``, ``created`` - with ``complete: false``: ActiveX
offers no handle seed to diff, and a deletion or a side effect (a dimension's
block) is not in a returned handle.

The mark and the summary run on the event loop thread, outside the document
lock, between a call's middleware and its worker: under concurrent clients a
second call's entities may be counted under the first. Never a reason to fail
a call - every reader here returns None on any error.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

log = logging.getLogger("autocad_mcp.changes")

__all__ = ["LIST_CAP", "EzdxfChangeSets", "empty_changes", "merge_changes", "summary_from_result"]

#: Handles listed per side; ``truncated`` counts what a list leaves out.
LIST_CAP = 50


def empty_changes(*, complete: bool = True, **extra: Any) -> dict:
    return {"added": [], "deleted": [], "counts": {}, "complete": complete, **extra}


def _finish(
    added: list[str], deleted: list[str], counts: Mapping[str, dict], complete: bool, **extra
) -> dict:
    out: dict[str, Any] = {
        "added": added[:LIST_CAP],
        "deleted": deleted[:LIST_CAP],
        "counts": {
            name: {k: v for k, v in row.items() if v}
            for name, row in sorted(counts.items())
            if any(row.values())
        },
        "complete": bool(complete),
    }
    truncated = {
        side: len(rows) - LIST_CAP
        for side, rows in (("added", added), ("deleted", deleted))
        if len(rows) > LIST_CAP
    }
    if truncated:
        out["truncated"] = truncated
    out.update(extra)
    return out


def _bump(counts: dict, owner: str, side: str, n: int = 1) -> None:
    row = counts.setdefault(owner, {"added": 0, "deleted": 0})
    row[side] = row.get(side, 0) + n


def merge_changes(parts: Iterable[dict | None]) -> dict | None:
    """One summary for several operations in order (a batch's steps).

    A handle added then deleted inside the run is neither; a handle deleted
    then added again (a restore) is both. ``complete`` only when every part
    was; None when no part had a summary.
    """
    parts = list(parts)
    seen = [p for p in parts if isinstance(p, dict)]
    if not seen:
        return None
    added: dict[str, None] = {}
    deleted: dict[str, None] = {}
    counts: dict[str, dict] = {}
    truncated = False
    for part in seen:
        for handle in part.get("deleted") or ():
            if handle in added:
                del added[handle]
            else:
                deleted.setdefault(handle, None)
        for handle in part.get("added") or ():
            added.setdefault(handle, None)
        for owner, row in (part.get("counts") or {}).items():
            for side in ("added", "deleted"):
                if row.get(side):
                    _bump(counts, owner, side, int(row[side]))
        truncated = truncated or bool(part.get("truncated"))
    complete = len(seen) == len(parts) and all(p.get("complete") for p in seen) and not truncated
    return _finish(list(added), list(deleted), counts, complete)


def summary_from_result(result: Any) -> dict | None:
    """The live engine's summary: the handles a call returned (``complete: false``)."""
    payload = getattr(result, "structured_content", result)
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return None
    handles: list[str] = []
    single = payload.get("handle")
    if isinstance(single, str) and single:
        handles.append(single)
    for key in ("handles", "created"):
        rows = payload.get(key)
        if isinstance(rows, list):
            for row in rows:
                value = row.get("handle") if isinstance(row, dict) else row
                if isinstance(value, str) and value and value not in handles:
                    handles.append(value)
    if not handles:
        return None
    counts = {"returned": {"added": len(handles), "deleted": 0}}
    return _finish(handles, [], counts, False, basis="returned_handles")


# ── the headless engine ─────────────────────────────────────────────────────


def _owner_name(db, owner_handle: str | None, names: dict) -> str | None:
    """``Model``, a paper layout's name, ``block:NAME`` - or None (anonymous / unknown)."""
    if owner_handle is None:
        return None
    if owner_handle in names:
        return names[owner_handle]
    record = db.get(owner_handle)
    name = None
    if record is not None and record.dxftype() == "BLOCK_RECORD":
        raw = str(record.dxf.get("name", ""))
        upper = raw.upper()
        if upper == "*MODEL_SPACE":
            name = "Model"
        elif upper.startswith("*PAPER_SPACE"):
            layout = db.get(record.dxf.get("layout", "0"))
            name = str(layout.dxf.get("name")) if layout is not None else raw
        elif not raw.startswith("*"):
            name = f"block:{raw}"
    names[owner_handle] = name
    return name


def _is_layout(name: str) -> bool:
    return not name.startswith("block:")


class EzdxfChangeSets:
    """``change_mark`` / ``change_summary`` for ``EzdxfBackend`` (module docstring)."""

    def _change_map_build(self, doc) -> dict:
        """handle -> owner name for every entity of every layout and named block."""
        db = doc.entitydb
        names: dict = {}
        owners: dict[str, str] = {}
        for block in doc.blocks:
            owner = _owner_name(db, block.block_record_handle, names)
            if owner is None:
                continue
            for entity in block:
                owners[entity.dxf.handle] = owner
        return {"owners": owners, "names": names, **self._change_spaces(doc, names)}

    @staticmethod
    def _change_spaces(doc, names: dict) -> dict:
        """The tracked entity spaces (layouts, named blocks) and the record count
        they were listed at. ezdxf destroys a deleted entity but leaves its row in
        the database until a purge, so a deletion shows as a shorter space."""
        db = doc.entitydb
        spaces = [
            block for block in doc.blocks if _owner_name(db, block.block_record_handle, names)
        ]
        return {"spaces": spaces, "n_records": len(doc.block_records)}

    @staticmethod
    def _change_total(doc, cache: dict) -> int:
        if len(doc.block_records) != cache["n_records"]:
            cache.update(EzdxfChangeSets._change_spaces(doc, cache["names"]))
        return sum(len(block) for block in cache["spaces"])

    def change_mark(self) -> dict | None:
        try:
            doc = self._doc
            if doc is None:
                return None
            state = self._state
            cache = getattr(self, "_change_cache", None)
            if (
                cache is None
                or cache["doc"] is not doc
                or cache["key"] != self._active_key
                or cache["rev"] != state.revision
            ):
                built = self._change_map_build(doc)
                cache = {"doc": doc, "key": self._active_key, "rev": state.revision, **built}
                self._change_cache = cache
            return {
                "doc": doc,
                "key": self._active_key,
                "seed": int(doc.entitydb.handles._handle),
                "total": self._change_total(doc, cache),
            }
        except Exception as exc:  # a summary is evidence, never a reason to fail a call
            log.debug("change_mark: %s", exc)
            return None

    def change_summary(self, mark: dict | None) -> dict | None:
        if not mark:
            return None
        try:
            return self._change_summary(mark)
        except Exception as exc:  # evidence only
            log.debug("change_summary: %s", exc)
            return None

    def _change_summary(self, mark: dict) -> dict | None:
        doc = self._doc
        cache = getattr(self, "_change_cache", None)
        if cache is None or cache["doc"] is not mark["doc"]:
            return None
        state = self._state
        if self._active_key != mark["key"]:
            # Another document: drawing_open / drawing_new / document_activate.
            self._change_cache = None
            return empty_changes(complete=False, document_switched=True)
        if doc is None:
            return empty_changes(complete=False, document_switched=True)
        if doc is not mark["doc"]:
            return self._change_replaced(doc, cache, state)
        db = doc.entitydb
        owners: dict[str, str] = cache["owners"]
        names: dict = cache["names"]
        counts: dict[str, dict] = {}
        added: list[str] = []
        seed_after = int(db.handles._handle)
        for value in range(int(mark["seed"]), seed_after):
            handle = f"{value:X}"
            entity = db.get(handle)
            if entity is None or not entity.is_alive:
                continue
            owner = _owner_name(db, entity.dxf.get("owner"), names)
            if owner is None:
                continue
            owners[handle] = owner
            _bump(counts, owner, "added")
            if _is_layout(owner):
                added.append(handle)
        deleted: list[str] = []
        new_total = sum(row["added"] for row in counts.values())
        if int(mark["total"]) + new_total - self._change_total(doc, cache) > 0:
            for handle, owner in list(owners.items()):
                entity = db.get(handle)
                if entity is None or not entity.is_alive:
                    del owners[handle]
                    _bump(counts, owner, "deleted")
                    if _is_layout(owner):
                        deleted.append(handle)
        cache["rev"] = state.revision
        return _finish(added, deleted, counts, True)

    def _change_replaced(self, doc, cache: dict, state) -> dict:
        """The document object was replaced in place (undo, restore, rollback)."""
        before: dict[str, str] = cache["owners"]
        built = self._change_map_build(doc)
        after: dict[str, str] = built["owners"]
        counts: dict[str, dict] = {}
        added, deleted = [], []
        for handle, owner in after.items():
            if handle not in before:
                _bump(counts, owner, "added")
                if _is_layout(owner):
                    added.append(handle)
        for handle, owner in before.items():
            if handle not in after:
                _bump(counts, owner, "deleted")
                if _is_layout(owner):
                    deleted.append(handle)
        self._change_cache = {"doc": doc, "key": self._active_key, "rev": state.revision, **built}
        return _finish(added, deleted, counts, True, document_replaced=True)
