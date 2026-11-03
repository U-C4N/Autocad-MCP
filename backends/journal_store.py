"""The durable operation journal and its state folder (B13, 1.8.0).

``DurableJournal`` is ``backends/operations.py``'s ``OperationJournal`` with
the same ``begin`` / ``finish`` / ``get`` / ``recent`` API - ``AuditMiddleware``
and the ``cad_batch`` step loop do not change - writing every record through to
``<STATE_DIR>/journal.sqlite3`` (stdlib sqlite3). Measured in spike S2
(``docs/analysis/spikes-1.8.0.json``) and applied here:

* WAL, ``synchronous=NORMAL``: a process kill loses no committed row; only an
  OS crash or a power loss can lose the last ones (``JOURNAL_SYNC=full`` covers
  that at ~30x the cost per commit). ``journal_mode`` must answer ``wal`` at
  open, or the store is not used.
* ``busy_timeout=5000``, autocommit mode (``isolation_level=None``) with an
  explicit ``BEGIN IMMEDIATE`` wherever a read decides a write (a client id's
  check-then-insert), so a lock upgrade cannot fail half way.
* The hot path of a mutating call is one INSERT (its ``running`` row, committed
  before the call runs) and one UPDATE (its final state). A read-only call
  without a client id is one INSERT when it ends.
* At open, a ``running`` row whose owner is dead - a pid plus that process's
  start time, both on the row, so a reused pid is not mistaken for the owner -
  becomes ``unknown_commit`` with ``error_kind: "process_exit"`` (a read-only
  tool's becomes ``cancelled``: nothing was written, so it runs again). Rows of
  a live second server sharing the file are never touched. No
  ``integrity_check`` at open.
* A write that still finds the file locked after the timeout (or any other
  sqlite error) is recorded - ``write_failures``, a logged error and
  ``state_store.degraded`` - and the journal carries on in memory: no tool call
  ever fails because of its journal.

What a row keeps: the request hash always; the arguments by ``JOURNAL_ARGS``
(``bounded``: the canonical JSON up to 4096 bytes, longer ones cut with the
full length and sha256; ``digest``: the sha256 only; ``off``: nothing); a
client-supplied id's answer, so the same id replays after a restart (an answer
over ``JOURNAL_RESULT_MAX_KB`` keeps only its sha256 and replays as
``result_truncated: true`` with ``revision_after``). ``lineage_id``,
``checkpoint_*``, ``label`` and ``changes_json`` are columns for 1.8-04 / -08 /
-10; ``CHECKPOINT_*`` settings are read and reported here and consumed by
1.8-08.

The folder is ``ACADMCP_STATE_DIR`` (default ``%LOCALAPPDATA%\\autocad-mcp-pro\\
state``; elsewhere ``$XDG_STATE_HOME/autocad-mcp-pro``). A repository checkout
or a folder holding drawings is refused at startup - the journal holds tool
arguments and answers, which belong beside neither - and the server runs on in
memory, saying so in ``system_status.state_store``.

Test-only: ``ACADMCP_TEST_HANG_AFTER_BEGIN=<operation id>`` parks the server
right after that id's ``running`` row is committed, so a test can kill it
mid-call (``tests/test_journal_durable.py``). Unset, it costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backends.operations import OperationJournal, OperationRecord

__all__ = [
    "ARGS_CAP_BYTES",
    "ARGS_MODES",
    "JOURNAL_FILE",
    "DurableJournal",
    "JournalSettingError",
    "JournalSettings",
    "ReplayedFailure",
    "StateDirRefusedError",
    "check_state_dir",
    "default_state_dir",
    "open_journal",
    "owner_alive",
    "process_start_token",
    "state_store_status",
]

log = logging.getLogger("autocad_mcp.journal")

JOURNAL_FILE = "journal.sqlite3"
ARGS_CAP_BYTES = 4096
ARGS_MODES: tuple[str, ...] = ("bounded", "digest", "off")
SYNC_MODES: tuple[str, ...] = ("normal", "full")
BUSY_TIMEOUT_MS = 5000
SCHEMA_VERSION = "1"
DRAWING_SUFFIXES = frozenset({".dxf", ".dwg", ".dwt", ".dxb", ".dws"})
TEST_HANG_ENV = "ACADMCP_TEST_HANG_AFTER_BEGIN"
_DAY_S = 86400.0
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    tool TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    document_id TEXT,
    lineage_id TEXT,
    revision_before INTEGER,
    revision_after INTEGER,
    started REAL NOT NULL,
    ended REAL,
    error_kind TEXT,
    client_supplied INTEGER NOT NULL DEFAULT 0,
    server_instance TEXT NOT NULL,
    pid INTEGER NOT NULL,
    pid_started TEXT,
    args_json TEXT,
    result_json TEXT,
    result_sha256 TEXT,
    checkpoint_before TEXT,
    checkpoint_after TEXT,
    label TEXT,
    changes_json TEXT
);
CREATE INDEX IF NOT EXISTS operations_state ON operations(state);
CREATE INDEX IF NOT EXISTS operations_ended ON operations(ended);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_COLUMNS = (
    "operation_id",
    "tool",
    "request_hash",
    "state",
    "document_id",
    "revision_before",
    "revision_after",
    "started",
    "ended",
    "error_kind",
    "client_supplied",
    "server_instance",
    "pid",
    "pid_started",
    "args_json",
    "result_json",
    "result_sha256",
)
#: One statement for a new row and for a client id that runs again (its earlier
#: run was ``cancelled``): the row is replaced in place.
_UPSERT = (
    f"INSERT INTO operations ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
    "ON CONFLICT(operation_id) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in _COLUMNS[1:])
)
_FINISH = (
    "UPDATE operations SET state = ?, error_kind = ?, ended = ?, revision_after = ?, "
    "document_id = ?, result_json = ?, result_sha256 = ? "
    "WHERE operation_id = ? AND server_instance = ?"
)
_RECORD_COLUMNS = (
    "operation_id, tool, request_hash, state, document_id, revision_before, revision_after, "
    "started, ended, error_kind, client_supplied"
)


# ── settings ─────────────────────────────────────────────────────────────────


class JournalSettingError(ValueError):
    """A journal setting the server cannot honour, named by its variable."""

    kind = "invalid_setting"


class StateDirRefusedError(ValueError):
    """``ACADMCP_STATE_DIR`` points somewhere the journal must not write."""

    kind = "state_dir_refused"


def default_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%\\autocad-mcp-pro\\state``; elsewhere the XDG state home."""
    env = os.environ if environ is None else environ
    if sys.platform == "win32":
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "autocad-mcp-pro" / "state"
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "autocad-mcp-pro"


def _switch(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("on", "true", "1", "yes"):
        return True
    if raw in ("off", "false", "0", "no"):
        return False
    raise JournalSettingError(f"{name}={raw!r}: expected on or off")


def _choice(env: Mapping[str, str], name: str, choices: tuple[str, ...], default: str) -> str:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw not in choices:
        raise JournalSettingError(f"{name}={raw!r}: expected one of {', '.join(choices)}")
    return raw


def _count(env: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise JournalSettingError(f"{name}={raw!r}: expected a whole number") from None
    if value < minimum:
        raise JournalSettingError(f"{name}={value}: must be at least {minimum}")
    return value


@dataclass(frozen=True)
class JournalSettings:
    """The B13 settings table (plan 1.8-03), read from the environment."""

    state_dir: Path
    durable: bool = True
    args: str = "bounded"
    sync: str = "normal"
    retain_days: int = 30
    result_max_kb: int = 64
    #: Read and reported here; consumed by 1.8-08's checkpoints.
    checkpoint_keep: int = 20
    checkpoint_budget_mb: int = 512
    checkpoint_max_doc_mb: int = 32

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> JournalSettings:
        env = os.environ if environ is None else environ
        raw_dir = (env.get("ACADMCP_STATE_DIR") or "").strip()
        return cls(
            state_dir=Path(raw_dir).expanduser() if raw_dir else default_state_dir(env),
            durable=_switch(env, "JOURNAL_DURABLE", True),
            args=_choice(env, "JOURNAL_ARGS", ARGS_MODES, "bounded"),
            sync=_choice(env, "JOURNAL_SYNC", SYNC_MODES, "normal"),
            retain_days=_count(env, "JOURNAL_RETAIN_DAYS", 30, minimum=1),
            result_max_kb=_count(env, "JOURNAL_RESULT_MAX_KB", 64, minimum=0),
            checkpoint_keep=_count(env, "CHECKPOINT_KEEP", 20, minimum=1),
            checkpoint_budget_mb=_count(env, "CHECKPOINT_BUDGET_MB", 512, minimum=1),
            checkpoint_max_doc_mb=_count(env, "CHECKPOINT_MAX_DOC_MB", 32, minimum=1),
        )

    def to_dict(self) -> dict:
        return {
            "args": self.args,
            "sync": self.sync,
            "retain_days": self.retain_days,
            "result_max_kb": self.result_max_kb,
            "checkpoint_keep": self.checkpoint_keep,
            "checkpoint_budget_mb": self.checkpoint_budget_mb,
            "checkpoint_max_doc_mb": self.checkpoint_max_doc_mb,
        }


def check_state_dir(path: str | os.PathLike) -> Path:
    """The resolved folder, or ``StateDirRefusedError`` naming why not.

    Refused: this server's own source tree, any git checkout (a ``.git`` entry -
    a worktree's is a file), and a folder that already holds drawings. The
    journal keeps tool arguments and answers; neither belongs in a repository
    or beside a client's drawings.
    """
    resolved = Path(path).expanduser().resolve()
    hint = (
        f"Point ACADMCP_STATE_DIR at a private folder (default {default_state_dir()}), "
        "or set JOURNAL_DURABLE=off."
    )
    if resolved == _PROJECT_ROOT or (resolved / ".git").exists():
        raise StateDirRefusedError(
            f"ACADMCP_STATE_DIR={resolved} is a repository checkout: the operation journal "
            f"keeps tool arguments and answers, which must not land in a repository. {hint}"
        )
    if resolved.is_dir():
        drawings = sorted(
            child.name
            for child in resolved.iterdir()
            if child.suffix.lower() in DRAWING_SUFFIXES and child.is_file()
        )
        if drawings:
            shown = ", ".join(drawings[:3]) + (" ..." if len(drawings) > 3 else "")
            raise StateDirRefusedError(
                f"ACADMCP_STATE_DIR={resolved} holds drawing files ({shown}): the operation "
                f"journal must not sit beside a client's drawings. {hint}"
            )
    return resolved


# ── process identity ─────────────────────────────────────────────────────────


def _probe(pid: int) -> tuple[str, str | None]:
    """``("alive" | "gone" | "unknown", start token or None)`` for ``pid``."""
    if pid <= 0:
        return "gone", None
    if sys.platform == "win32":
        return _probe_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone", None
    except PermissionError:
        return "alive", None
    except OSError:
        return "unknown", None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        return "alive", stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return "alive", None


_KERNEL32 = None


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        filetime = ctypes.POINTER(wintypes.FILETIME)
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            filetime,
            filetime,
            filetime,
            filetime,
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = kernel32
    return _KERNEL32


def _probe_windows(pid: int) -> tuple[str, str | None]:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        return ("alive" if error == error_access_denied else "gone"), None
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != still_active:
            return "gone", None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return "alive", None
        created = times[0]
        return "alive", str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def process_start_token(pid: int) -> str | None:
    """An opaque token for ``pid``'s start time (None when it cannot be read)."""
    try:
        return _probe(int(pid))[1]
    except Exception:  # a token is evidence, never a reason to fail
        return None


def owner_alive(pid: int, token: str | None) -> bool:
    """Whether the process that wrote a row still runs.

    Dead means: no such process, or a process whose start time differs from
    the one on the row (the pid was reused). When either cannot be read the
    owner counts as alive - a live server's rows are never touched on a guess.
    """
    try:
        state, now = _probe(int(pid))
    except Exception:
        return True
    if state == "gone":
        return False
    if token and now and token != now:
        return False
    return True


# ── results ──────────────────────────────────────────────────────────────────


class ReplayedFailure(RuntimeError):
    """A failure recorded by an earlier server process, replayed from the file."""


def _deepest_message(exc: BaseException) -> str:
    message = ""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = " ".join(str(current).split())
        if text:
            message = text
        current = current.__cause__ or current.__context__
    return message or type(exc).__name__


def _serialise(result: Any) -> dict:
    if isinstance(result, BaseException):
        return {
            "type": "exception",
            "class": type(result).__name__,
            "message": _deepest_message(result),
        }
    if hasattr(result, "structured_content") and hasattr(result, "content"):
        texts = (getattr(block, "text", None) for block in (result.content or ()))
        text = "\n".join(str(t) for t in texts if t is not None)
        return {
            "type": "tool_result",
            "is_error": bool(getattr(result, "is_error", False)),
            "structured_content": result.structured_content,
            "text": text,
        }
    return {"type": "value", "value": result}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_of(row: sqlite3.Row | tuple) -> OperationRecord:
    (
        operation_id,
        tool,
        digest,
        state,
        document_id,
        revision_before,
        revision_after,
        started,
        ended,
        error_kind,
        client_supplied,
    ) = tuple(row)[:11]
    return OperationRecord(
        operation_id=operation_id,
        tool=tool,
        request_hash=digest,
        state=state,
        document_id=document_id,
        revision_before=revision_before,
        revision_after=revision_after,
        started=started,
        ended=ended,
        error_kind=error_kind,
        client_supplied=bool(client_supplied),
    )


def _rehydrate(record: OperationRecord, result_json: str | None, result_sha: str | None) -> Any:
    from fastmcp.tools.tool import ToolResult

    if result_json is None:
        payload = {
            "ok": record.state == "committed",
            "state": record.state,
            "result_truncated": True,
            "result_sha256": result_sha,
            "revision_after": record.revision_after,
            "note": (
                "The recorded answer was larger than JOURNAL_RESULT_MAX_KB, so the journal "
                "kept only its sha256. Nothing ran again; read the drawing for the details."
            ),
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False), structured_content=payload
        )
    data = json.loads(result_json)
    kind = data.get("type")
    if kind == "exception":
        return ReplayedFailure(str(data.get("message") or data.get("class") or "failed"))
    if kind == "tool_result":
        return ToolResult(
            content=str(data.get("text") or ""),
            structured_content=data.get("structured_content"),
            is_error=bool(data.get("is_error")),
        )
    return data.get("value")


# ── the journal ──────────────────────────────────────────────────────────────


class DurableJournal(OperationJournal):
    """``OperationJournal`` written through to ``<state_dir>/journal.sqlite3``.

    Memory stays the first place a record is looked up (and keeps the live
    result objects of this process); the file answers for an id this process
    never saw or has evicted. When the file is unusable the journal runs as the
    in-memory one and ``degraded`` says why.
    """

    def __init__(
        self,
        state_dir: str | os.PathLike | None,
        *,
        settings: JournalSettings | None = None,
        is_read_only: Callable[[str], bool] | None = None,
        maxlen: int = 1000,
        owner: tuple[int, str | None] | None = None,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        degraded: dict | None = None,
    ):
        super().__init__(maxlen)
        self.settings = settings
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.path = self.state_dir / JOURNAL_FILE if self.state_dir is not None else None
        self.server_instance = uuid.uuid4().hex
        if owner is None:
            owner = (os.getpid(), process_start_token(os.getpid()))
        self._pid, self._pid_started = int(owner[0]), owner[1]
        self._is_read_only = is_read_only
        self._read_only_cache: dict[str, bool] = {}
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._deferred: dict[str, str | None] = {}
        self._hang = os.environ.get(TEST_HANG_ENV) or None
        self.degraded: dict | None = degraded
        self.write_failures = 0
        self.swept_at_open = 0
        self.retired_at_open = 0
        if self.degraded is None:
            if self.path is None or self.settings is None:
                self.degraded = {"kind": "unavailable", "reason": "no state folder was given"}
            else:
                self._open()

    @classmethod
    def unavailable(
        cls,
        kind: str,
        reason: str,
        *,
        settings: JournalSettings | None = None,
        is_read_only: Callable[[str], bool] | None = None,
        maxlen: int = 1000,
    ) -> DurableJournal:
        """An in-memory journal that reports why the file is not used."""
        log.error("Operation journal kept in memory only (%s): %s", kind, reason)
        return cls(
            None,
            settings=settings,
            is_read_only=is_read_only,
            maxlen=maxlen,
            degraded={"kind": kind, "reason": reason},
        )

    # ── open ──

    def _open(self) -> None:
        connection = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path),
                timeout=self._busy_timeout_ms / 1000.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            if mode != "wal":
                connection.close()
                self._degrade(
                    "journal_mode",
                    f"sqlite answered journal_mode={mode!r}, not 'wal' (a network or "
                    f"read-only drive?) for {self.path}",
                )
                return
            sync = "FULL" if self.settings.sync == "full" else "NORMAL"
            connection.execute(f"PRAGMA synchronous = {sync}")
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            stored = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            if stored != SCHEMA_VERSION:
                connection.close()
                self._degrade(
                    "schema_mismatch",
                    f"{self.path} has journal schema {stored}, this server writes {SCHEMA_VERSION}",
                )
                return
            self._sweep(connection)
            self._retire(connection)
        except (sqlite3.Error, OSError) as exc:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            self._degrade("open_failed", f"{type(exc).__name__}: {exc} ({self.path})")
            return
        self._connection = connection
        log.info(
            "Operation journal: %s (args=%s, sync=%s; %d dead-owner row(s) reconciled, "
            "%d past retention dropped)",
            self.path,
            self.settings.args,
            self.settings.sync,
            self.swept_at_open,
            self.retired_at_open,
        )

    def _sweep(self, connection: sqlite3.Connection) -> None:
        owners = connection.execute(
            "SELECT DISTINCT server_instance, pid, pid_started FROM operations "
            "WHERE state = 'running'"
        ).fetchall()
        now = time.time()
        for instance, pid, token in owners:
            if instance == self.server_instance or owner_alive(pid, token):
                continue
            running = connection.execute(
                "SELECT operation_id, tool FROM operations "
                "WHERE state = 'running' AND server_instance = ?",
                (instance,),
            ).fetchall()
            connection.execute("BEGIN IMMEDIATE")
            try:
                for operation_id, tool in running:
                    state = "cancelled" if self._read_only(tool) else "unknown_commit"
                    connection.execute(
                        "UPDATE operations SET state = ?, error_kind = 'process_exit', "
                        "ended = ? WHERE operation_id = ? AND state = 'running'",
                        (state, now, operation_id),
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            self.swept_at_open += len(running)
            log.warning(
                "Operation journal: %d running row(s) of a dead server (pid %s) marked "
                "unknown_commit (process_exit)",
                len(running),
                pid,
            )

    def _retire(self, connection: sqlite3.Connection) -> None:
        cutoff = time.time() - self.settings.retain_days * _DAY_S
        cursor = connection.execute(
            "DELETE FROM operations WHERE ended IS NOT NULL AND ended < ?", (cutoff,)
        )
        self.retired_at_open = max(0, cursor.rowcount or 0)

    # ── failures ──

    def _degrade(self, kind: str, reason: str) -> None:
        self.degraded = {"kind": kind, "reason": reason, "at": time.time()}
        log.error(
            "Operation journal kept in memory only from now on (%s): %s - see "
            "system_status.state_store",
            kind,
            reason,
        )

    def _failed(self, exc: BaseException, during: str) -> None:
        """A write (or read) the file refused: recorded, then memory only."""
        self.write_failures += 1
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._degrade("write_failed", f"{during}: {type(exc).__name__}: {exc}")

    # ── helpers ──

    def _read_only(self, tool: str) -> bool:
        cached = self._read_only_cache.get(tool)
        if cached is None:
            try:
                cached = bool(self._is_read_only(tool)) if self._is_read_only else False
            except Exception:
                cached = False
            self._read_only_cache[tool] = cached
        return cached

    def _args_text(self, arguments: dict | None) -> str | None:
        mode = self.settings.args if self.settings is not None else "off"
        if mode == "off":
            return None
        canonical = _canonical(arguments or {})
        if mode == "digest":
            return json.dumps({"sha256": _sha(canonical)})
        encoded = canonical.encode("utf-8")
        if len(encoded) <= ARGS_CAP_BYTES:
            return canonical
        head = encoded[:ARGS_CAP_BYTES].decode("utf-8", errors="ignore")
        return json.dumps(
            {"truncated": True, "bytes": len(encoded), "sha256": _sha(canonical), "head": head},
            ensure_ascii=False,
        )

    def _result_text(self, result: Any) -> tuple[str | None, str]:
        text = json.dumps(_serialise(result), ensure_ascii=False, default=str)
        digest = _sha(text)
        cap = self.settings.result_max_kb * 1024 if self.settings is not None else 0
        if len(text.encode("utf-8")) > cap:
            return None, digest
        return text, digest

    def _upsert(self, record: OperationRecord, args_text: str | None, result=(None, None)):
        self._connection.execute(
            _UPSERT,
            (
                record.operation_id,
                record.tool,
                record.request_hash,
                record.state,
                record.document_id,
                record.revision_before,
                record.revision_after,
                record.started,
                record.ended,
                record.error_kind,
                int(record.client_supplied),
                self.server_instance,
                self._pid,
                self._pid_started,
                args_text,
                result[0],
                result[1],
            ),
        )

    def _row(self, operation_id: str):
        try:
            return self._connection.execute(
                f"SELECT {_RECORD_COLUMNS}, result_json, result_sha256 FROM operations "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            self._failed(exc, "read")
            return None

    def _park(self, operation_id: str) -> None:
        log.warning("%s=%s: parked after its running row (test hook)", TEST_HANG_ENV, operation_id)
        while True:
            time.sleep(1.0)

    # ── the OperationJournal API ──

    def begin(
        self,
        tool: str,
        arguments: dict | None,
        *,
        operation_id: str | None = None,
        document: dict | None = None,
    ) -> tuple[OperationRecord, Any | None]:
        if self._connection is None:
            return super().begin(tool, arguments, operation_id=operation_id, document=document)
        client = operation_id is not None
        eager = client or not self._read_only(tool)
        with self._lock:
            if client:
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    self._failed(exc, "begin")
                    return super().begin(
                        tool, arguments, operation_id=operation_id, document=document
                    )
            try:
                record, replay = super().begin(
                    tool, arguments, operation_id=operation_id, document=document
                )
            except BaseException:
                if client and self._connection is not None:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error as exc:
                        self._failed(exc, "begin")
                raise
            if replay is None and self._connection is not None:
                args_text = self._args_text(arguments)
                if eager:
                    try:
                        self._upsert(record, args_text)
                    except sqlite3.Error as exc:
                        self._failed(exc, "begin")
                else:
                    self._deferred[record.operation_id] = args_text
            if client and self._connection is not None:
                try:
                    self._connection.execute("COMMIT")
                except sqlite3.Error as exc:
                    self._failed(exc, "begin")
        if replay is None and self._hang is not None and record.operation_id == self._hang:
            self._park(record.operation_id)
        return record, replay

    def finish(
        self,
        record: OperationRecord,
        state: str,
        *,
        result: Any = None,
        error_kind: str | None = None,
        document: dict | None = None,
    ) -> None:
        super().finish(record, state, result=result, error_kind=error_kind, document=document)
        with self._lock:
            deferred = record.operation_id in self._deferred
            args_text = self._deferred.pop(record.operation_id, None)
            if self._connection is None:
                return
            stored = (None, None)
            if record.client_supplied and result is not None:
                stored = self._result_text(result)
            try:
                if deferred:
                    self._upsert(record, args_text, stored)
                else:
                    self._connection.execute(
                        _FINISH,
                        (
                            record.state,
                            record.error_kind,
                            record.ended,
                            record.revision_after,
                            record.document_id,
                            stored[0],
                            stored[1],
                            record.operation_id,
                            self.server_instance,
                        ),
                    )
            except sqlite3.Error as exc:
                self._failed(exc, "finish")

    def _lookup(self, operation_id: str) -> tuple[OperationRecord, bool, Any] | None:
        found = super()._lookup(operation_id)
        if found is not None or self._connection is None:
            return found
        row = self._row(operation_id)
        if row is None:
            return None
        record = _record_of(row)
        result_json, result_sha = row[11], row[12]
        if result_json is None and result_sha is None:
            return record, False, None
        return record, True, _rehydrate(record, result_json, result_sha)

    def get(self, operation_id: str) -> OperationRecord | None:
        record = super().get(operation_id)
        if record is not None or self._connection is None:
            return record
        with self._lock:
            row = self._row(operation_id)
        return _record_of(row) if row is not None else None

    def recent(self, n: int = 20) -> list[dict]:
        """This run's last ``n`` records, preceded by earlier runs' when fewer."""
        mine = super().recent(n)
        if n <= 0 or len(mine) >= n or self._connection is None:
            return mine
        with self._lock:
            try:
                rows = self._connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM operations WHERE server_instance != ? "
                    "ORDER BY seq DESC LIMIT ?",
                    (self.server_instance, int(n) - len(mine)),
                ).fetchall()
            except sqlite3.Error as exc:
                self._failed(exc, "read")
                return mine
        return [_record_of(row).to_dict() for row in reversed(rows)] + mine

    # ── status and shutdown ──

    def state_store(self) -> dict:
        """``system_status.state_store``: where, how big, and whether degraded."""
        size = 0
        if self.path is not None:
            for suffix in ("", "-wal", "-shm"):
                try:
                    size += os.path.getsize(f"{self.path}{suffix}")
                except OSError:
                    pass
        documents = operations = None
        if self._connection is not None:
            with self._lock:
                try:
                    documents, operations = self._connection.execute(
                        "SELECT COUNT(DISTINCT document_id), COUNT(*) FROM operations"
                    ).fetchone()
                except sqlite3.Error as exc:
                    self._failed(exc, "read")
        return {
            "mode": "durable",
            "path": str(self.path) if self.path is not None else None,
            "degraded": dict(self.degraded) if self.degraded else False,
            "bytes": size,
            "documents": documents,
            "operations": operations,
            "checkpoints": 0,
            "write_failures": self.write_failures,
            "reconciled_at_open": self.swept_at_open,
            "settings": self.settings.to_dict() if self.settings is not None else None,
        }

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error as exc:
                    log.warning("Operation journal close failed: %s", exc)


def open_journal(
    environ: Mapping[str, str] | None = None,
    *,
    is_read_only: Callable[[str], bool] | None = None,
    maxlen: int = 1000,
) -> OperationJournal:
    """The server's journal: durable by default, in memory with ``JOURNAL_DURABLE=off``.

    Never raises: a bad setting or a refused folder gives an in-memory
    ``DurableJournal`` whose ``degraded`` names the variable.
    """
    env = os.environ if environ is None else environ
    try:
        settings = JournalSettings.from_env(env)
    except JournalSettingError as exc:
        return DurableJournal.unavailable(
            exc.kind, str(exc), is_read_only=is_read_only, maxlen=maxlen
        )
    if not settings.durable:
        return OperationJournal(maxlen)
    try:
        state_dir = check_state_dir(settings.state_dir)
    except StateDirRefusedError as exc:
        return DurableJournal.unavailable(
            exc.kind, str(exc), settings=settings, is_read_only=is_read_only, maxlen=maxlen
        )
    except OSError as exc:
        return DurableJournal.unavailable(
            "open_failed",
            f"{type(exc).__name__}: {exc}",
            settings=settings,
            is_read_only=is_read_only,
            maxlen=maxlen,
        )
    return DurableJournal(state_dir, settings=settings, is_read_only=is_read_only, maxlen=maxlen)


def state_store_status(journal: OperationJournal) -> dict:
    """``system_status.state_store`` for any journal the server may hold."""
    reader = getattr(journal, "state_store", None)
    if callable(reader):
        return reader()
    return {
        "mode": "off",
        "path": None,
        "degraded": False,
        "bytes": 0,
        "documents": None,
        "operations": None,
        "checkpoints": 0,
        "note": "JOURNAL_DURABLE=off: operations are held in memory; a restart forgets them.",
    }
