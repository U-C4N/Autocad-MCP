"""Document lineage across processes (1.8-04, durability-4 narrowed).

A saved file remembers which document it is. Every save (and every open)
writes ``<STATE_DIR>/lineage/<key>.json`` for the file's real path:
``{lineage_id, document_id, realpath, last_saved_sha256, revision}``. Nothing is
written into the DXF itself (an XRECORD stamp is 1.8.1).

``resolve_open`` decides what a ``drawing_open`` of that file is:

* no record - a new lineage, a new ``document_id``, revision 0;
* the file's sha256 is the one recorded - the same ``document_id`` and
  revision continue (a restart no longer resets either, so an
  ``expected_revision`` from an earlier process stays meaningful);
* another sha256 - ``external_change: true``: the same ``document_id``, the
  revision one past the recorded one (the server journals an
  ``external_edit`` row);
* the record's lineage is already open under another path in this process -
  a fork: a new lineage and ``document_id`` with ``lineage_parent``, because
  two open documents never share an id.

A revision is never issued twice for different content: when the process (or
the journal, through ``revision_high``) knows a higher revision was issued for
that ``document_id`` than the file carries - edits made after the last save
and never saved - the open continues one past it and says so
(``revision_floor``).

The live engine keeps its per-bind uuid (``ComBackend``): there the record
only carries the lineage and detects an external change, keyed by
``FullName`` and recorded only while ``DBMOD`` is 0.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from backends.atomic_write import _write_json, state_dir

log = logging.getLogger("autocad_mcp.lineage")

__all__ = ["LINEAGE_DIR", "read_record", "realpath_of", "resolve_open", "write_record"]

LINEAGE_DIR = "lineage"


def realpath_of(path: str | os.PathLike) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _record_path(folder: Path, path: str | os.PathLike) -> Path:
    key = os.path.normcase(realpath_of(path))
    return folder / LINEAGE_DIR / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}.json"


def read_record(path: str | os.PathLike, *, state: Any = None) -> dict | None:
    """The lineage record of the file at ``path``, or None (none, or no state folder)."""
    folder = state if state is not None else state_dir()
    if folder is None:
        return None
    try:
        record = json.loads(_record_path(Path(folder), path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("unreadable lineage record for %s: %s", path, exc)
        return None
    return record if isinstance(record, dict) and record.get("lineage_id") else None


def write_record(path: str | os.PathLike, record: Mapping, *, state: Any = None) -> bool:
    """Record ``path``'s lineage; False when there is no usable state folder."""
    folder = state if state is not None else state_dir()
    if folder is None:
        return False
    payload = {**record, "realpath": realpath_of(path), "recorded": time.time()}
    try:
        _write_json(_record_path(Path(folder), path), payload)
    except OSError as exc:
        log.warning("lineage record for %s not written: %s", path, exc)
        return False
    return True


def new_lineage_id() -> str:
    return uuid.uuid4().hex


def resolve_open(
    path: str | os.PathLike,
    sha256: str | None,
    *,
    open_elsewhere: Callable[[str], bool] = lambda _lineage: False,
    revision_high: Callable[[str], int | None] | None = None,
    state: Any = None,
) -> dict:
    """What opening ``path`` (whose bytes hash to ``sha256``) continues or starts.

    ``open_elsewhere(lineage_id)`` - is that lineage open under another path in
    this process; ``revision_high(document_id)`` - the highest revision known
    to have been issued for it (in this process or in the journal). Returns
    ``{lineage_id, document_id (None = mint one), revision, continued,
    external_change, previous_sha256, sha256}`` plus ``lineage_parent`` on a
    fork and ``revision_floor`` when unsaved revisions were skipped.
    """
    record = read_record(path, state=state)
    fresh = {
        "lineage_id": new_lineage_id(),
        "document_id": None,
        "revision": 0,
        "continued": False,
        "external_change": False,
        "previous_sha256": None,
        "sha256": sha256,
    }
    if record is None:
        return fresh
    recorded_sha = record.get("last_saved_sha256")
    changed = sha256 is not None and recorded_sha is not None and sha256 != recorded_sha
    if open_elsewhere(str(record["lineage_id"])) or not record.get("document_id"):
        return {
            **fresh,
            "lineage_parent": record["lineage_id"],
            "external_change": changed,
            "previous_sha256": recorded_sha,
        }
    revision = int(record.get("revision") or 0)
    base = revision + 1 if changed else revision
    out = {
        "lineage_id": record["lineage_id"],
        "document_id": record["document_id"],
        "revision": base,
        "continued": True,
        "external_change": changed,
        "previous_sha256": recorded_sha,
        "sha256": sha256,
    }
    for name in ("lineage_parent", "recovered_from"):
        if record.get(name):
            out[name] = record[name]
    high = None
    if revision_high is not None:
        try:
            high = revision_high(str(record["document_id"]))
        except Exception as exc:  # a lookup is evidence, never a reason to fail an open
            log.debug("revision_high(%s): %s", record["document_id"], exc)
    if high is not None and int(high) >= base and int(high) > revision:
        out["revision"] = int(high) + 1
        out["revision_floor"] = int(high)
    return out
