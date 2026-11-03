"""B13 (1.8-03) gate - a real server killed and restarted keeps its operations.

``python server.py`` runs as a stdio subprocess with a temporary
``ACADMCP_STATE_DIR`` and the headless engine; the test speaks newline-framed
JSON-RPC to it directly so that ``Popen.kill()`` (TerminateProcess on Windows,
SIGKILL elsewhere) is a real, unannounced death - no shutdown hook runs.

(a) ``cad_batch(operation_id="op-1")`` -> kill -> restart -> ``drawing_open`` ->
    the same id and steps answer ``replayed: true`` and draw nothing;
(b) the same id with other steps is ``operation_conflict``;
(c) a call killed while it runs (the test-only ``ACADMCP_TEST_HANG_AFTER_BEGIN``
    hook parks it after its ``running`` row is committed) is
    ``requires_reconcile`` with ``error_kind: "process_exit"`` after the
    restart, and ``JOURNAL_ARGS=digest`` left no argument text in the files.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastmcp import Client

import server
from backends.journal_store import JOURNAL_FILE

ROOT = Path(__file__).resolve().parents[1]
_LINE = {"x1": 0, "y1": 0, "x2": 10, "y2": 0}
_STARTUP_S = 120.0
_CALL_S = 60.0


class StdioServer:
    """``python server.py`` over stdio, driven with raw JSON-RPC lines."""

    def __init__(self, env: dict, cwd: Path, log: Path):
        self._log = log.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            env=env,
            cwd=str(cwd),
        )
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "journal-durable-test", "version": "0"},
            },
            timeout=_STARTUP_S,
        )
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _pump(self) -> None:
        for raw in self.proc.stdout:
            line = raw.strip()
            if line:
                self._lines.put(json.loads(line))
        self._lines.put(None)

    def _write(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def send(self, method: str, params: dict) -> int:
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._next_id

    def wait(self, request_id: int, timeout: float = _CALL_S) -> dict:
        deadline = time.monotonic() + timeout
        while request_id not in self._pending:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no answer to request {request_id} within {timeout:.0f}s")
            message = self._lines.get(timeout=left)
            if message is None:
                raise AssertionError("the server closed its stdout")
            if "id" in message and ("result" in message or "error" in message):
                self._pending[message["id"]] = message
        message = self._pending.pop(request_id)
        assert "error" not in message, message
        return message["result"]

    def request(self, method: str, params: dict, timeout: float = _CALL_S) -> dict:
        return self.wait(self.send(method, params), timeout)

    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return {"is_error": bool(result.get("isError")), **(result.get("structuredContent") or {})}

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=30)
        self.proc.stdin.close()
        self.proc.stdout.close()
        self._log.close()


def _env(state_dir: Path, **extra: str) -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("JOURNAL_", "CHECKPOINT_", "ACADMCP_"))
    }
    env.update(
        {"AUTOCAD_MCP_BACKEND": "ezdxf", "ACADMCP_STATE_DIR": str(state_dir), "PYTHONUTF8": "1"}
    )
    env.update(extra)
    return env


def _state_of(state_dir: Path, operation_id: str) -> str | None:
    connection = sqlite3.connect(state_dir / JOURNAL_FILE, timeout=5)
    try:
        row = connection.execute(
            "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
    except sqlite3.OperationalError:  # the table is not there yet
        return None
    finally:
        connection.close()
    return row[0] if row else None


def _until(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.0f}s")
        time.sleep(0.05)


@pytest.fixture
def folders(tmp_path_factory) -> tuple[Path, Path]:
    return tmp_path_factory.mktemp("state"), tmp_path_factory.mktemp("drawings")


def test_a_client_id_replays_after_the_server_is_killed(folders):
    state_dir, drawings = folders
    log = drawings / "server.log"
    steps = [{"tool": "entity_create_line", "args": _LINE}]
    batch = {"steps": steps, "operation_id": "op-1"}
    path = drawings / "replay.dxf"

    first = StdioServer(_env(state_dir), drawings, log)
    try:
        first.call("drawing_new")
        ran = first.call("cad_batch", batch)
        assert ran["ok"] is True and ran["operation_id"] == "op-1"
        assert first.call("drawing_save", {"path": str(path)})["is_error"] is False
    finally:
        first.kill()

    second = StdioServer(_env(state_dir), drawings, log)
    try:
        assert second.call("drawing_open", {"path": str(path)})["is_error"] is False
        assert second.call("drawing_info")["entity_count"] == 1
        replay = second.call("cad_batch", batch)
        assert replay["replayed"] is True and replay["operation_id"] == "op-1"
        assert replay["ok"] is True and replay["succeeded"] == 1
        assert second.call("drawing_info")["entity_count"] == 1, "the replay drew nothing"

        other = second.call(
            "cad_batch",
            {
                "steps": [{"tool": "entity_create_line", "args": {**_LINE, "x2": 20}}],
                "operation_id": "op-1",
            },
        )
        assert other["is_error"] is True
        assert (other["kind"], other["reason"]) == ("operation_conflict", "different_request")
        assert second.call("drawing_info")["entity_count"] == 1

        store = second.call("system_status")["state_store"]
        assert store["mode"] == "durable" and store["degraded"] is False
        assert store["path"] == str(state_dir / JOURNAL_FILE)
    finally:
        second.kill()


def test_a_call_killed_mid_run_must_be_reconciled_after_the_restart(folders):
    state_dir, drawings = folders
    log = drawings / "server.log"
    marker = "ZQX-CUSTOMER-TEXT-4419"
    batch = {
        "steps": [{"tool": "entity_create_circle", "args": {"cx": 0, "cy": 0, "radius": 5}}],
        "operation_id": "op-3",
    }

    first = StdioServer(
        _env(state_dir, JOURNAL_ARGS="digest", ACADMCP_TEST_HANG_AFTER_BEGIN="op-3"),
        drawings,
        log,
    )
    try:
        first.call("drawing_new")
        typed = first.call("entity_create_text", {"text": marker, "x": 0, "y": 0, "height": 2.5})
        assert typed["is_error"] is False
        first.send("tools/call", {"name": "cad_batch", "arguments": batch})
        _until(lambda: _state_of(state_dir, "op-3") == "running")
    finally:
        first.kill()

    blob = b"".join(p.read_bytes() for p in state_dir.iterdir() if p.is_file())
    assert blob, "the journal is on disk"
    assert marker.encode() not in blob, "JOURNAL_ARGS=digest keeps no argument text"

    second = StdioServer(_env(state_dir), drawings, log)
    try:
        second.call("drawing_new")
        retry = second.call("cad_batch", batch)
        assert retry["is_error"] is True and retry["kind"] == "requires_reconcile"
        assert retry["operation_id"] == "op-3"
        assert retry["record"]["state"] == "unknown_commit"
        assert retry["record"]["error_kind"] == "process_exit"
        assert second.call("drawing_info")["entity_count"] == 0, "nothing ran again"
    finally:
        second.kill()
    assert _state_of(state_dir, "op-3") == "unknown_commit"


@pytest.mark.asyncio
async def test_a_refused_state_dir_degrades_and_every_call_still_runs(monkeypatch):
    monkeypatch.setenv("ACADMCP_STATE_DIR", str(ROOT))
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        await client.call_tool("entity_create_line", _LINE)
        status = (await client.call_tool("system_status", {})).structured_content
    store = status["state_store"]
    assert store["mode"] == "durable" and store["degraded"]["kind"] == "state_dir_refused"
    assert any(row["tool"] == "entity_create_line" for row in status["operations"])
    assert not (ROOT / JOURNAL_FILE).exists()


@pytest.mark.asyncio
async def test_journal_durable_off_reports_the_in_memory_journal(monkeypatch):
    monkeypatch.setenv("JOURNAL_DURABLE", "off")
    async with Client(server.mcp) as client:
        status = (await client.call_tool("system_status", {})).structured_content
    assert status["state_store"]["mode"] == "off"
    assert status["state_store"]["degraded"] is False
