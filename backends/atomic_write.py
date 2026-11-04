"""Atomic DXF writes and the recovery of an interrupted one (1.8-04, durability-9).

Until 1.8.0 ``doc.saveas(target)`` wrote straight into the client's file: a
process killed half way left it truncated. ``atomic_write_dxf`` is the save
protocol every headless DXF write goes through (``drawing_save``,
``drawing_save_as``, ``drawing_export_dxf``, the finalize / deliver writes and
``drawing_template_save`` all reach it through the backend):

1. a pending marker ``{target, tmp, expected_sha256: null, previous_sha256}``
   in ``<STATE_DIR>/pending`` (phase ``writing``);
2. the document written to ``<target>.acadmcp-<pid>.tmp`` *in the target's own
   folder* (``os.replace`` is atomic only within one volume), flushed and
   fsynced;
3. the tmp checked (``verify="header"``): it starts with the HEADER section,
   ends with ``EOF``, carries the document's ``$ACADVER`` and ``$HANDSEED``,
   and its ENTITIES section holds exactly the records the document has;
4. the marker rewritten with ``expected_sha256`` (phase ``verified``);
5. with ``SAVE_BACKUP=1`` the previous file copied to ``<name>.bak``;
6. ``os.replace(tmp, target)``, tried again 3 x 50 ms on a sharing violation
   (a target with more than one hard link is written *into* from the verified
   tmp instead - a rename would split its names into two files - and a copy cut
   short is finished from the tmp at recovery);
7. the marker removed.

The plan orders the marker after the check; it is written first here (and
completed at step 4) so that a process dying *while it writes* leaves a marker
too - the plan's own gate ("the writer dies at 50 % -> the marker is there ->
``rolled_back`` at the next open") needs one. A failure inside the process
removes the tmp and the marker and leaves the target as it was.

A marker left by a dead process is resolved at startup and by a
``drawing_open`` of the same path (``resolve_pending``): ``completed`` (the
replace had happened), ``finished`` (the verified tmp is put in place now),
``rolled_back`` (the tmp was incomplete and is deleted; the target is the
previous file) or ``orphan_tmp`` (a tmp of a dead process with no marker,
deleted). The answer is reported as ``save_recovery``; there is no tool for it.

The live engine does not come here: ``SaveAs`` is AutoCAD's own protocol and
its export already writes into a private folder and renames.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backends import fault_barrier

log = logging.getLogger("autocad_mcp.atomic_write")

__all__ = [
    "PENDING_DIR",
    "VERIFY_MODES",
    "AtomicWriteError",
    "SaveLockedError",
    "SaveVerifyError",
    "atomic_write_dxf",
    "file_sha256",
    "resolve_pending",
    "state_dir",
    "tmp_path_for",
]

PENDING_DIR = "pending"
TMP_INFIX = ".acadmcp-"
TMP_SUFFIX = ".tmp"
VERIFY_MODES: tuple[str, ...] = ("header", "full", "none")
#: ``os.replace`` is tried this many more times on a sharing violation ...
REPLACE_RETRIES = 3
#: ... this long apart (an antivirus scan or an indexer holding the target).
REPLACE_PAUSE_S = 0.05
#: Windows errors that mean "somebody holds the file": access denied, sharing
#: and lock violation.
_SHARING_WINERRORS = frozenset({5, 32, 33})
#: Records written inside an entity's own export, not entities of the space.
_SUB_RECORDS = frozenset({b"VERTEX", b"SEQEND", b"ATTRIB"})


class AtomicWriteError(RuntimeError):
    """A save that did not complete; the target is the file it was before."""

    kind = "save_failed"


class SaveVerifyError(AtomicWriteError):
    """The written tmp did not hold what the document holds; nothing was replaced."""

    kind = "save_verify_failed"


class SaveLockedError(AtomicWriteError):
    """The target stayed locked by another program through every retry."""

    kind = "save_locked"


# ── the state folder ─────────────────────────────────────────────────────────

_STATE_CACHE: dict[tuple, Path | None] = {}


def state_dir(environ: Mapping[str, str] | None = None) -> Path | None:
    """The usable ``ACADMCP_STATE_DIR`` (1.8-03's folder), or None when refused.

    The same folder and the same refusals as the journal
    (``backends.journal_store.check_state_dir``): a folder inside a checkout or
    beside drawings is never written. None means no marker and no lineage - a
    save is still atomic, only its interruption is not resolved by a later run.
    """
    from backends.journal_store import StateDirRefusedError, check_state_dir, default_state_dir

    env = os.environ if environ is None else environ
    raw = (env.get("ACADMCP_STATE_DIR") or "").strip()
    key = (raw, env.get("LOCALAPPDATA"), env.get("XDG_STATE_HOME"))
    if key in _STATE_CACHE:
        return _STATE_CACHE[key]
    folder = Path(raw).expanduser() if raw else default_state_dir(env)
    try:
        resolved: Path | None = check_state_dir(folder)
    except (StateDirRefusedError, OSError) as exc:
        log.warning("Save markers and lineage are off: %s", exc)
        resolved = None
    _STATE_CACHE[key] = resolved
    return resolved


def file_sha256(path: str | os.PathLike) -> str | None:
    """The file's sha256 (hex), or None when there is no such file."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    return digest.hexdigest()


def tmp_path_for(target: str | os.PathLike, pid: int | None = None) -> str:
    """``<target>.acadmcp-<pid>.tmp`` - beside the target, on its volume."""
    return f"{os.path.abspath(target)}{TMP_INFIX}{pid or os.getpid()}{TMP_SUFFIX}"


def _target_key(target: str) -> str:
    spelled = os.path.normcase(os.path.abspath(target))
    return hashlib.sha256(spelled.encode("utf-8")).hexdigest()[:24]


def _write_json(path: Path, payload: dict) -> None:
    """A small JSON file written whole: tmp, replace.

    No fsync: a process kill loses nothing the OS already holds (the case
    these files exist for); only a power loss can, and then the DXF tmp it
    describes - which is fsynced - is resolved as an orphan. Measured: the two
    marker fsyncs were ~10 ms of a 2,000-entity save's ~35 ms protocol cost.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")
    with open(scratch, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    os.replace(scratch, path)


def _unlink(path: str | os.PathLike | None) -> None:
    if path is None:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)


def _fsync_folder(folder: str) -> None:
    """Make the rename itself durable where the OS allows it (not on Windows)."""
    if sys.platform == "win32":
        return
    try:
        handle = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def _copy_into(source: str, target: str) -> None:
    """Write ``source``'s bytes into ``target``'s own file (it keeps its inode), fsynced."""
    with open(source, "rb") as src, open(target, "r+b") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
        dst.truncate()
        dst.flush()
        os.fsync(dst.fileno())
    os.unlink(source)


def _replace(source: str, target: str, *, in_place: bool = False) -> None:
    """Put ``source`` in place of ``target``, with retries on a sharing violation.

    ``os.replace`` normally. A target with more than one hard link is written
    *into* instead (``in_place``): a rename would give the name being saved a
    new file and leave every other name on the old bytes - the registry treats
    all of them as one document. The verified tmp and its marker make that
    copy resumable: a copy cut short is finished from the tmp at recovery.
    """
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            if in_place:
                _copy_into(source, target)
            else:
                os.replace(source, target)
            return
        except PermissionError as exc:
            error = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in _SHARING_WINERRORS:
                raise
            error = exc
        if attempt < REPLACE_RETRIES:
            time.sleep(REPLACE_PAUSE_S)
    raise SaveLockedError(
        f"save: {target} stayed locked by another program through "
        f"{REPLACE_RETRIES} retries {int(REPLACE_PAUSE_S * 1000)} ms apart ({error}); the file "
        "is unchanged and nothing was written into it. Close the program holding it and save "
        "again, or save to another path."
    ) from error


# ── the check of a written tmp ───────────────────────────────────────────────


def _expected_entities(doc) -> int:
    """What ezdxf's ENTITIES section holds: model space plus the active paper space."""
    layouts = doc.layouts
    return sum(1 for _ in layouts.modelspace().entity_space) + sum(
        1 for _ in layouts.active_layout().entity_space
    )


def _header_value(data: bytes, nl: bytes, name: bytes) -> bytes | None:
    marker = b"  9" + nl + name + nl
    at = data.find(marker)
    if at < 0:
        return None
    rest = data[at + len(marker) :].split(nl, 2)
    return rest[1].strip() if len(rest) >= 2 else None


def _scan(data: bytes) -> dict:
    """What a header check needs from a written DXF, read in one pass."""
    nl = b"\r\n" if b"\r\n" in data[:64] else b"\n"
    found: dict[str, Any] = {
        "starts_with_header": data.startswith(
            b"  0" + nl + b"SECTION" + nl + b"  2" + nl + b"HEADER" + nl
        ),
        "ends_with_eof": data.endswith(b"  0" + nl + b"EOF" + nl),
        "acadver": _header_value(data, nl, b"$ACADVER"),
        "handseed": _header_value(data, nl, b"$HANDSEED"),
        "entities": None,
    }
    start = data.find(b"  2" + nl + b"ENTITIES" + nl)
    end = data.find(b"  0" + nl + b"ENDSEC" + nl, start) if start >= 0 else -1
    if start >= 0 and end > start:
        lines = data[start:end].split(nl)
        found["entities"] = sum(
            1
            for code, value in zip(lines[0::2], lines[1::2], strict=False)
            if code == b"  0" and value not in _SUB_RECORDS
        )
    return found


def _verify(doc, tmp: str, data: bytes, mode: str) -> int:
    """Raise ``SaveVerifyError`` unless the tmp holds the document; returns its entity count."""
    expected = _expected_entities(doc)
    if mode == "full":
        import ezdxf

        reread = ezdxf.readfile(tmp)
        got = sum(1 for _ in reread.modelspace()) + sum(1 for _ in reread.layouts.active_layout())
        if got != expected:
            raise SaveVerifyError(
                f"save: the written file reads back {got} entities, the document has "
                f"{expected}; nothing was replaced"
            )
        return got
    found = _scan(data)
    faults = []
    if not found["starts_with_header"]:
        faults.append("it does not start with the HEADER section")
    if not found["ends_with_eof"]:
        faults.append("it does not end with EOF")
    acadver = found["acadver"]
    if acadver is None or acadver.decode("ascii", "replace") != doc.dxfversion:
        faults.append(f"$ACADVER is {acadver!r}, the document is {doc.dxfversion}")
    seed = doc.header.get("$HANDSEED")
    if seed and found["handseed"] is not None:
        if found["handseed"].decode("ascii", "replace").upper() != str(seed).upper():
            faults.append(f"$HANDSEED is {found['handseed']!r}, the document's is {seed}")
    if found["entities"] != expected:
        faults.append(f"its ENTITIES section holds {found['entities']}, the document {expected}")
    if faults:
        raise SaveVerifyError(
            "save: the written file failed its check (" + "; ".join(faults) + "); the target "
            "was not replaced and is the file it was before"
        )
    return int(found["entities"])


# ── the protocol ─────────────────────────────────────────────────────────────

_DEFAULT = object()


def _backup_wanted(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get("SAVE_BACKUP") or "").strip().lower() in ("1", "on", "true", "yes")


def _marker_payload(
    target: str, tmp: str, previous: str | None, expected: str | None, in_place: bool
) -> dict:
    from backends.journal_store import process_start_token

    return {
        "target": target,
        "tmp": tmp,
        "expected_sha256": expected,
        "previous_sha256": previous,
        "pid": os.getpid(),
        "pid_started": process_start_token(os.getpid()),
        "phase": "verified" if expected else "writing",
        "mode": "in_place" if in_place else "replace",
        "started": time.time(),
    }


def _hard_linked(target: str) -> bool:
    try:
        return os.stat(target).st_nlink > 1
    except OSError:
        return False


def atomic_write_dxf(
    doc,
    target: str | os.PathLike,
    verify: str = "header",
    *,
    backup: bool | None = None,
    state: Any = _DEFAULT,
    keep_data: bool = False,
) -> dict:
    """Write ``doc`` as DXF onto ``target`` so that the target is never half written.

    Returns ``{path, sha256, previous_sha256, bytes, entities, verify, backup,
    recovery_marker}``. On any failure the target is untouched, the tmp and
    the marker are removed and the error (``SaveVerifyError``,
    ``SaveLockedError`` or the writer's own) is raised. ``verify``:
    ``header`` (default; one read of the written bytes), ``full`` (ezdxf reads
    the tmp back) or ``none``. ``backup`` defaults to ``SAVE_BACKUP``.
    ``doc.filename`` is not touched: re-pointing is the caller's decision.
    ``keep_data`` adds ``data``: the bytes written, already in memory for the
    check - 1.8-08 copies them into a checkpoint without serialising again.
    """
    if verify not in VERIFY_MODES:
        raise ValueError(f"verify must be one of {VERIFY_MODES}, got {verify!r}")
    target = os.path.abspath(os.fspath(target))
    folder = os.path.dirname(target)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"save: the folder {folder} does not exist")
    tmp = tmp_path_for(target)
    previous = file_sha256(target)
    in_place = previous is not None and _hard_linked(target)
    pending = state_dir() if state is _DEFAULT else state
    marker: Path | None = None
    if pending is not None:
        marker = Path(pending) / PENDING_DIR / f"{_target_key(target)}-{os.getpid()}.json"
        try:
            _write_json(marker, _marker_payload(target, tmp, previous, None, in_place))
        except OSError as exc:
            log.warning("save marker not written (%s); the save stays atomic", exc)
            marker = None
    if backup is None:
        backup = _backup_wanted()
    committed = False
    try:
        with open(tmp, "w", encoding=doc.output_encoding, errors="dxfreplace") as stream:
            doc.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        data = Path(tmp).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        entities = _verify(doc, tmp, data, verify) if verify != "none" else None
        if marker is not None:
            _write_json(marker, _marker_payload(target, tmp, previous, sha, in_place))
        # Test-only kill point (inert without the unshipped marker module): the
        # tmp is verified, the marker says so, the target is not replaced yet.
        fault_barrier.barrier("save.before_replace", key=os.path.basename(target))
        backup_path = None
        if backup and previous is not None:
            backup_path = os.path.splitext(target)[0] + ".bak"
            shutil.copy2(target, backup_path)
        _replace(tmp, target, in_place=in_place)
        committed = True
        _fsync_folder(folder)
    finally:
        if not committed:
            _unlink(tmp)
        if marker is not None:
            _unlink(marker)
    written = {
        "path": target,
        "sha256": sha,
        "previous_sha256": previous,
        "bytes": len(data),
        "entities": entities,
        "verify": verify,
        "backup": backup_path,
        "recovery_marker": marker is not None,
        "method": "in_place" if in_place else "replace",
    }
    if keep_data:
        written["data"] = data
    return written


# ── recovery ─────────────────────────────────────────────────────────────────


def _resolve_marker(data: dict) -> dict:
    target, tmp = data.get("target"), data.get("tmp")
    expected, previous = data.get("expected_sha256"), data.get("previous_sha256")
    current = file_sha256(target) if target else None
    staged = file_sha256(tmp) if tmp else None
    row = {
        "target": target,
        "tmp": tmp,
        "expected_sha256": expected,
        "previous_sha256": previous,
        "pid": data.get("pid"),
    }
    in_place = data.get("mode") == "in_place"
    if expected and current == expected:
        _unlink(tmp if staged is not None else None)
        return {**row, "outcome": "completed"}
    # An in-place copy cut short leaves the target neither previous nor
    # expected; the verified tmp still finishes it.
    if expected and staged == expected and (current == previous or in_place):
        _replace(tmp, target, in_place=in_place and current is not None)
        return {**row, "outcome": "finished", "sha256": expected}
    _unlink(tmp if staged is not None else None)
    row["outcome"] = "rolled_back"
    if current != previous:
        # Somebody wrote the target after the dead save began: it is left as it is.
        row["target_changed"] = True
    return row


def _owner_alive(data: dict) -> bool:
    from backends.journal_store import owner_alive

    pid = int(data.get("pid") or 0)
    if pid == os.getpid():
        return True  # this process's own save in flight
    return owner_alive(pid, data.get("pid_started"))


def _orphan_tmps(target: str, handled: set[str]) -> list[dict]:
    from backends.journal_store import owner_alive

    folder, name = os.path.split(os.path.abspath(target))
    prefix = f"{name}{TMP_INFIX}"
    rows = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return rows
    for entry in entries:
        if not (entry.startswith(prefix) and entry.endswith(TMP_SUFFIX)):
            continue
        path = os.path.join(folder, entry)
        if os.path.normcase(path) in handled:
            continue
        try:
            pid = int(entry[len(prefix) : -len(TMP_SUFFIX)])
        except ValueError:
            continue
        if pid == os.getpid() or owner_alive(pid, None):
            continue
        _unlink(path)
        rows.append(
            {"target": os.path.abspath(target), "tmp": path, "pid": pid, "outcome": "orphan_tmp"}
        )
    return rows


def resolve_pending(
    target: str | os.PathLike | None = None, *, state: Any = _DEFAULT
) -> list[dict]:
    """Resolve the markers dead processes left - all of them, or ``target``'s.

    With ``target`` a tmp of a dead process beside it that no marker names is
    deleted too (``orphan_tmp``). A marker of a live process is never touched.
    Every row: ``{target, tmp, outcome, expected_sha256, previous_sha256, pid}``.
    """
    pending = state_dir() if state is _DEFAULT else state
    rows: list[dict] = []
    handled: set[str] = set()
    if pending is not None:
        folder = Path(pending) / PENDING_DIR
        pattern = f"{_target_key(os.fspath(target))}-*.json" if target is not None else "*.json"
        for marker in sorted(folder.glob(pattern)) if folder.is_dir() else ():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("unreadable save marker %s (%s): removed", marker, exc)
                _unlink(marker)
                continue
            if _owner_alive(data):
                continue
            try:
                row = _resolve_marker(data)
            except OSError as exc:
                log.error("save marker %s could not be resolved: %s", marker, exc)
                continue
            if data.get("tmp"):
                handled.add(os.path.normcase(os.path.abspath(data["tmp"])))
            _unlink(marker)
            log.warning("interrupted save of %s: %s", row.get("target"), row["outcome"])
            rows.append(row)
    if target is not None:
        rows.extend(_orphan_tmps(os.fspath(target), handled))
    return rows
