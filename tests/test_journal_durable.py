"""B13 (1.8-03) gate - a real server killed and restarted keeps its operations.

``python server.py`` runs as a stdio subprocess with a temporary
``ACADMCP_STATE_DIR`` and the headless engine, through the fault-injection
harness (``benchmarks/fault_inject.py``): newline-framed JSON-RPC on the
child's pipes, so that ``Popen.kill()`` (TerminateProcess on Windows, SIGKILL
elsewhere) is a real, unannounced death - no shutdown hook runs.

(a) ``cad_batch(operation_id="op-1")`` -> kill -> restart -> ``drawing_open`` ->
    the same id and steps answer ``replayed: true`` and draw nothing;
(b) the same id with other steps is ``operation_conflict``;
(c) a call killed while it runs (the test-only barrier ``journal.after_begin``
    of ``backends/fault_barrier.py`` parks it after its ``running`` row is
    committed; the variable AND the unshipped marker module arm it) is
    ``requires_reconcile`` with ``error_kind: "process_exit"`` after the
    restart, and ``JOURNAL_ARGS=digest`` left no argument text in the files.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastmcp import Client

import server
from backends.journal_store import JOURNAL_FILE
from benchmarks.fault_inject import ServerProcess, barrier_env

ROOT = Path(__file__).resolve().parents[1]
_LINE = {"x1": 0, "y1": 0, "x2": 10, "y2": 0}


def StdioServer(env: dict, cwd: Path, log: Path) -> ServerProcess:  # noqa: N802 - the old class name
    """``python server.py`` over stdio, initialized (benchmarks/fault_inject.py)."""
    child = ServerProcess(env, cwd, log)
    try:
        child.initialize()
    except BaseException:
        child.kill()
        raise
    return child


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
    """The stored state of one operation, read through a fresh connection.

    A read right after ``Popen.kill()`` can meet a transient ``database is
    locked`` / ``disk I/O error`` while SQLite rebuilds the WAL index the dead
    process left behind; that is retried for a few seconds, so a transient lock
    is never mistaken for "no such row" (measured: 1 in 4 runs on 2026-11-03).
    A journal without the table yet answers ``None``.
    """
    deadline = time.monotonic() + 5.0
    while True:
        connection = sqlite3.connect(state_dir / JOURNAL_FILE, timeout=5)
        try:
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or time.monotonic() > deadline:
                return None
            time.sleep(0.1)
            continue
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
def folders(outside_repo_dir, tmp_path_factory) -> tuple[Path, Path]:
    return outside_repo_dir, tmp_path_factory.mktemp("drawings")


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

    signals = drawings / "barriers"
    signals.mkdir()
    first = StdioServer(
        barrier_env("journal.after_begin:op-3", signals, _env(state_dir, JOURNAL_ARGS="digest")),
        drawings,
        log,
    )
    # A client id's answer repeats its steps' arguments; under digest it must
    # not reach the file either (review 1.8-03), yet it still replays.
    echoed = {
        "steps": [
            {"tool": "layer_create", "args": {"name": marker + "-LAYER"}},
            {"tool": "entity_create_text", "args": {"text": marker, "x": 0, "y": 5, "height": 2.5}},
        ],
        "operation_id": "op-2",
    }
    try:
        first.call("drawing_new")
        typed = first.call("entity_create_text", {"text": marker, "x": 0, "y": 0, "height": 2.5})
        assert typed["is_error"] is False
        assert first.call("cad_batch", echoed)["ok"] is True
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
        replay = second.call("cad_batch", echoed)
        assert replay["replayed"] is True and replay["result_truncated"] is True
        assert replay["ok"] is True and replay["is_error"] is False
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
        status = (
            await client.call_tool("system_status", {"detail": "operations"})
        ).structured_content
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
