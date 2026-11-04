"""Process-kill fault injection for the durability gates (1.8-25, durability-8).

A durability claim ("the journal survives the server", "undo works after a
restart", "an interrupted save is finished") is only evidence when a real
server really dies. This harness starts ``python server.py`` of this tree on
stdio, headless, on a private temporary ``ACADMCP_STATE_DIR``; drives tool
calls; kills the child with ``Popen.kill()`` - TerminateProcess on Windows,
SIGKILL elsewhere, so no atexit handler, no ``finally`` and no shutdown hook of
the server runs - optionally while it is parked at a named barrier; and starts
a fresh server on the same state folder, which must see what the kill left::

    with FaultHarness() as fh:
        first = fh.spawn()                          # retried only here, at spawn
        first.call("drawing_new")
        first.call("cad_batch", {"steps": [...], "operation_id": "op-1"})
        first.kill()                                # True: it was alive
        assert not first.alive and first.kill() is False   # a second kill is a no-op
        second = fh.spawn()                         # the same state folder
        second.call("cad_batch", {...})             # replayed: true

    with FaultHarness() as fh:                      # a kill *inside* a call
        server = fh.spawn(barrier="save.before_replace")
        ...
        server.send("tools/call", {"name": "drawing_save_as", ...})
        fh.wait_barrier("save.before_replace")      # the child is parked there
        server.kill()

**The wire.** fastmcp's ``StdioTransport`` speaks newline-framed JSON-RPC to
the child it starts; its client hands out neither the ``Popen`` nor the pid,
and leaving its context terminates the child politely - which is exactly the
death a kill test must not have. So the harness starts the child itself
(``subprocess.Popen``) and speaks the same framing over its pipes, as
``tests/test_journal_durable.py`` and ``benchmarks/correctness_suite.py`` did
before it.

**Barriers.** ``backends/fault_barrier.py``: a barrier parks the child only when
``ACADMCP_TEST_BARRIER`` names it AND the test-only marker module
``acadmcp_fault_marker`` (``tests/fault_marker/``, never in the wheel) is
importable. ``spawn(barrier=...)`` sets both for that child alone - the
variable, the marker folder on its ``PYTHONPATH`` and a signal folder the
barrier writes ``<name>.json`` into - so a production host that sets the
variable by mistake parks nothing.

**Retries.** Only at spawn: a child that exits or does not answer
``initialize`` within the startup deadline is killed and started again
(``SPAWN_ATTEMPTS``). A tool call is never retried, and nothing a killed child
was asked is ever sent again by the harness.

**Windows.** Every pipe and log handle of a child is closed when it is killed,
the harness opens no SQLite connection of its own, and the temporary folder is
removed only after every child is dead - with retries, because a just-killed
process's files can stay locked for a moment (an antivirus scan, the SQLite
WAL index the dead process left behind).
"""

from __future__ import annotations

import gc
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
#: The folder that holds the test-only marker module (``backends/fault_barrier.py``).
MARKER_DIR = ROOT / "tests" / "fault_marker"

STARTUP_S = 120.0
CALL_S = 60.0
#: How many times a child that fails to start is started again (spawn only).
SPAWN_ATTEMPTS = 3
_SPAWN_PAUSE_S = 0.5
#: How long removing the temporary folder keeps trying on a locked file.
REMOVE_PATIENCE_S = 10.0

#: Popen.kill()'s exit status: TerminateProcess(handle, 1) on Windows, SIGKILL elsewhere.
KILLED_EXIT_CODE = 1 if sys.platform == "win32" else -signal.SIGKILL

_STATE_PREFIXES = ("JOURNAL_", "CHECKPOINT_", "ACADMCP_")


class SpawnError(RuntimeError):
    """The child server could not be started (after every spawn attempt)."""


class ServerGone(RuntimeError):
    """The child closed its stdout (it exited or was killed) before answering."""


def pid_alive(pid: int, token: str | None = None) -> bool:
    """Whether process ``pid`` (started at ``token``, when given) still runs.

    The journal's own probe (``backends/journal_store.py``): no such process,
    or a pid reused by a process with another start time, is dead.
    """
    from backends.journal_store import owner_alive

    return owner_alive(int(pid), token)


def child_pids(pid: int) -> list[int]:
    """The processes whose parent is ``pid`` (Windows; elsewhere ``[]``).

    A venv's ``python.exe`` on Windows is a launcher that starts the base
    interpreter as its child (measured here: ``Popen.pid`` 31492, the server's
    ``os.getpid()`` 28244). The launcher holds the child in a kill-on-close job,
    so ``Popen.kill()`` of the launcher terminates the interpreter too - but the
    pid that must be proven gone is the interpreter's, found here.
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class _Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    found: list[int] = []
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if int(entry.th32ParentProcessID) == int(pid) and int(entry.th32ProcessID) != int(pid):
                found.append(int(entry.th32ProcessID))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def server_env(state_dir: str | os.PathLike, **extra: str) -> dict[str, str]:
    """The parent's environment minus every journal / checkpoint / ``ACADMCP_``
    setting (a test barrier in the parent never leaks into a child), headless,
    the full advertised surface, ``state_dir`` as ``ACADMCP_STATE_DIR``."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(_STATE_PREFIXES)}
    env.update(
        {
            "AUTOCAD_MCP_BACKEND": "ezdxf",
            "ACADMCP_STATE_DIR": str(state_dir),
            "PYTHONUTF8": "1",
            "TOOL_PROFILE": "full",
            "TOOL_PACKS": "all",
            "DISCOVERY_MODE": "off",
        }
    )
    env.pop("EZDXF_UNDO_DEPTH", None)
    env.update(extra)
    return env


def barrier_env(barrier: str, signal_dir: str | os.PathLike, env: dict[str, str]) -> dict[str, str]:
    """``env`` with ``barrier`` armed: the variable, the signal folder and the
    marker folder first on ``PYTHONPATH`` (``backends/fault_barrier.py``)."""
    from backends.fault_barrier import BARRIER_DIR_ENV, BARRIER_ENV, BARRIERS

    names = {part.split(":", 1)[0].strip() for part in barrier.split(",") if part.strip()}
    unknown = sorted(names - BARRIERS)
    if unknown:
        raise ValueError(f"unknown fault barrier(s) {unknown}; known: {sorted(BARRIERS)}")
    path = [str(MARKER_DIR), str(ROOT)]
    if env.get("PYTHONPATH"):
        path.append(env["PYTHONPATH"])
    return {
        **env,
        BARRIER_ENV: barrier,
        BARRIER_DIR_ENV: str(signal_dir),
        "PYTHONPATH": os.pathsep.join(path),
    }


class ServerProcess:
    """One ``python server.py`` child over stdio, driven with raw JSON-RPC lines."""

    def __init__(self, env: dict, cwd: str | os.PathLike, log: str | os.PathLike):
        from backends.journal_store import process_start_token

        self._log = open(log, "ab")  # noqa: SIM115 - closed in kill()
        try:
            self.proc = subprocess.Popen(
                [sys.executable, str(SERVER)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                env=env,
                cwd=str(cwd),
            )
        except BaseException:
            self._log.close()
            raise
        self.pid = self.proc.pid
        self.started_token = process_start_token(self.pid)
        #: The interpreter that runs server.py: ``pid`` itself, or on Windows the
        #: child a venv launcher started (``child_pids``); set by ``initialize``.
        self.server_pid: int = self.pid
        self.server_token: str | None = self.started_token
        self.killed_at: float | None = None
        self.exit_code: int | None = None
        #: True when the interpreter outlived its launcher's kill and was
        #: terminated by pid (never expected: the launcher's job kills it).
        self.orphan_terminated = False
        self._closed = False
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True, name=f"fault-pump-{self.pid}").start()
        self._next_id = 0
        self._answers: dict[int, dict] = {}

    # -- the wire ------------------------------------------------------------------

    def _pump(self) -> None:
        stream = self.proc.stdout
        try:
            for raw in stream:
                line = raw.strip()
                if not line:
                    continue
                try:
                    self._lines.put(json.loads(line))
                except ValueError:
                    continue  # not a JSON-RPC line
        except (OSError, ValueError):
            pass  # the pipe was closed under the reader by kill()
        self._lines.put(None)

    def _write(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def initialize(self, timeout: float = STARTUP_S) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "acadmcp-fault-inject", "version": "0"},
            },
            timeout,
        )
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        from backends.journal_store import process_start_token

        children = child_pids(self.pid)  # it answered, so its interpreter exists
        if children:
            self.server_pid = children[0]
            self.server_token = process_start_token(self.server_pid)
        return result

    def send(self, method: str, params: dict) -> int:
        """Send one request without waiting (a call the harness will kill inside)."""
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._next_id

    def wait(self, request_id: int, timeout: float = CALL_S) -> dict:
        """The answer to ``request_id``: its ``result``, or ``{isError, rpc_error}``."""
        deadline = time.monotonic() + timeout
        while request_id not in self._answers:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no answer to request {request_id} within {timeout:.0f}s")
            try:
                message = self._lines.get(timeout=left)
            except queue.Empty:
                continue
            if message is None:
                self._lines.put(None)  # every later wait sees it too
                raise ServerGone(f"server pid {self.pid} closed its stdout")
            if "id" in message and ("result" in message or "error" in message):
                self._answers[message["id"]] = message
        message = self._answers.pop(request_id)
        if "error" in message:
            return {"isError": True, "rpc_error": message["error"]}
        return message["result"]

    def request(self, method: str, params: dict, timeout: float = CALL_S) -> dict:
        return self.wait(self.send(method, params), timeout)

    def call(self, name: str, arguments: dict | None = None, timeout: float = CALL_S) -> dict:
        """``tools/call`` -> ``{"is_error": bool, **structuredContent}`` (never retried)."""
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        return {"is_error": bool(result.get("isError")), **(result.get("structuredContent") or {})}

    # -- death -----------------------------------------------------------------------

    @property
    def pids(self) -> list[int]:
        """Every process this child is: the Popen's and, behind a launcher, the server's."""
        return [self.pid] if self.server_pid == self.pid else [self.pid, self.server_pid]

    @property
    def alive(self) -> bool:
        """Whether any process of this child still runs (the OS's answer, not the Popen's)."""
        launcher = self.proc.poll() is None and pid_alive(self.pid, self.started_token)
        if self.server_pid == self.pid:
            return launcher
        return launcher or pid_alive(self.server_pid, self.server_token)

    def kill(self) -> bool:
        """``Popen.kill()`` and reap; True when this call killed a live child.

        A second kill (or a kill of a child that already exited) is a no-op that
        returns False. Every pipe and the log handle are closed either way. The
        interpreter behind a Windows venv launcher dies with the launcher's job;
        should it not within a few seconds it is terminated by pid and
        ``orphan_terminated`` says so.
        """
        if self._closed:
            return False
        killed = False
        try:
            if self.proc.poll() is None:
                self.proc.kill()
                killed = True
                self.killed_at = time.time()
            self.exit_code = self.proc.wait(timeout=30)
            if self.server_pid != self.pid:
                deadline = time.monotonic() + 5.0
                while pid_alive(self.server_pid, self.server_token):
                    if time.monotonic() > deadline:
                        os.kill(self.server_pid, signal.SIGTERM)  # TerminateProcess
                        self.orphan_terminated = True
                        break
                    time.sleep(0.05)
        finally:
            self._close_streams()
        return killed

    def _close_streams(self) -> None:
        self._closed = True
        for stream in (self.proc.stdin, self.proc.stdout, self._log):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class FaultHarness:
    """A private state folder, a work folder and the server children started on them."""

    def __init__(self, prefix: str = "acadmcp-fault-", *, keep: bool = False):
        self.base = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        if self.base == ROOT or ROOT in self.base.parents:
            shutil.rmtree(self.base, ignore_errors=True)
            raise RuntimeError(
                f"the temporary folder {self.base} is inside the repository; the journal "
                "refuses a state folder there - point TMP/TEMP outside it"
            )
        self.state_dir = self.base / "state"
        self.work = self.base / "work"
        self.signals = self.base / "barriers"
        for folder in (self.state_dir, self.work, self.signals):
            folder.mkdir()
        self.log = self.work / "server.log"
        self.keep = keep
        self.servers: list[ServerProcess] = []
        self.spawn_failures: list[str] = []
        self.removed: bool | None = None

    def __enter__(self) -> FaultHarness:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def env(self, barrier: str | None = None, **extra: str) -> dict[str, str]:
        env = server_env(self.state_dir, **extra)
        return barrier_env(barrier, self.signals, env) if barrier else env

    def spawn(
        self,
        *,
        barrier: str | None = None,
        env: dict[str, str] | None = None,
        cwd: str | os.PathLike | None = None,
        startup_timeout: float = STARTUP_S,
        attempts: int = SPAWN_ATTEMPTS,
    ) -> ServerProcess:
        """A fresh, initialized server on this harness's state folder.

        Retried (``attempts``) only while the child cannot start - it exited or
        did not answer ``initialize`` in time; each failed child is killed and
        its handles closed first. ``env`` replaces the harness's environment
        (``barrier`` then arms on top of it).
        """
        child_env = dict(env) if env is not None else self.env()
        if barrier and env is not None:
            child_env = barrier_env(barrier, self.signals, child_env)
        elif barrier:
            child_env = self.env(barrier)
        errors: list[str] = []
        for attempt in range(1, max(1, attempts) + 1):
            server = ServerProcess(child_env, cwd or self.work, self.log)
            self.servers.append(server)
            try:
                server.initialize(startup_timeout)
            except (TimeoutError, ServerGone, OSError) as exc:
                server.kill()
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                self.spawn_failures.append(errors[-1])
                time.sleep(_SPAWN_PAUSE_S)
                continue
            return server
        tail = ""
        try:
            tail = self.log.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            pass
        raise SpawnError("server.py did not start: " + "; ".join(errors) + "\n" + tail)

    def wait_barrier(self, name: str, timeout: float = CALL_S) -> dict:
        """The signal a parked child wrote at barrier ``name`` (``{name, key, pid, at}``)."""
        target = self.signals / f"{name}.json"
        deadline = time.monotonic() + timeout
        while True:
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"no server reached barrier {name!r} within {timeout:.0f}s")
            time.sleep(0.05)

    def pending_markers(self) -> list[dict]:
        """The interrupted-save markers a dead child left in the state folder."""
        from backends.atomic_write import PENDING_DIR

        rows = []
        for path in sorted((self.state_dir / PENDING_DIR).glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return rows

    def close(self) -> bool:
        """Kill every child still alive, close every handle, remove the folder.

        Returns whether the folder is gone (``keep`` leaves it and returns False).
        """
        for server in self.servers:
            try:
                server.kill()
            except Exception:  # noqa: BLE001 - one stuck child must not keep the others
                pass
        gc.collect()  # drop any file object a caller left unreferenced
        if self.keep:
            self.removed = False
            return False
        self.removed = remove_tree(self.base)
        return self.removed


def remove_tree(path: str | os.PathLike, patience_s: float = REMOVE_PATIENCE_S) -> bool:
    """``shutil.rmtree`` that waits out a file a just-killed process still holds."""
    path = Path(path)
    deadline = time.monotonic() + patience_s

    def _writable(func, target, _info):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass  # the outer loop tries again

    while True:
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_writable)
            else:  # pragma: no cover - 3.11
                shutil.rmtree(path, onerror=_writable)
        except FileNotFoundError:
            return True
        except OSError:
            pass
        if not path.exists():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)


def describe(server: ServerProcess) -> dict[str, Any]:
    """What a report records about one child: pid, whether it is alive, how it ended."""
    return {
        "pid": server.pid,
        "server_pid": server.server_pid,
        "alive": server.alive,
        "killed_at": server.killed_at,
        "exit_code": server.exit_code,
        "orphan_terminated": server.orphan_terminated,
    }
