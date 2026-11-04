"""1.8-25 gate - the fault-injection harness kills a real server and proves it.

``benchmarks/fault_inject.py`` starts ``python server.py`` (stdio, headless, a
private temporary ``ACADMCP_STATE_DIR``), kills it with ``Popen.kill()``
(TerminateProcess on Windows: no atexit, no ``finally``) and starts a fresh
one on the same state folder. Its own self-test, here:

* the child really died - every pid of it (the venv launcher's and, behind it,
  the interpreter's) is gone, ``alive`` is False, the exit status is the
  kill's - and a second kill is a no-op;
* the restart sees what the kill left (a client operation id replays);
* a named barrier parks the child *inside* a call (``save.before_replace``)
  and the next server's startup finishes that save;
* the barrier cannot be enabled by the environment variable alone: without
  the unshipped marker module the same call runs through;
* retries happen only at spawn, never after a kill;
* the temporary folder is gone after ``close()``, every handle closed first.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from backends import fault_barrier
from benchmarks import fault_inject
from benchmarks.fault_inject import (
    KILLED_EXIT_CODE,
    MARKER_DIR,
    FaultHarness,
    ServerGone,
    SpawnError,
    pid_alive,
)

ROOT = Path(__file__).resolve().parents[1]
_LINE = {"x1": 0, "y1": 0, "x2": 10, "y2": 0}


@pytest.fixture
def harness():
    fh = FaultHarness()
    try:
        yield fh
    finally:
        fh.close()


def _dead(server) -> bool:
    return not server.alive and all(not pid_alive(pid) for pid in server.pids)


def test_a_killed_child_is_dead_and_the_restart_sees_what_it_left(harness):
    batch = {"steps": [{"tool": "entity_create_line", "args": _LINE}], "operation_id": "fi-1"}
    path = harness.work / "kept.dxf"

    first = harness.spawn()
    assert first.alive and all(pid_alive(pid) for pid in first.pids)
    first.call("drawing_new")
    assert first.call("cad_batch", batch)["ok"] is True
    assert first.call("drawing_save", {"path": str(path)})["is_error"] is False
    pids = list(first.pids)

    assert first.kill() is True, "this kill killed a live child"
    assert _dead(first), f"a process of the killed child still runs: {pids}"
    assert first.exit_code == KILLED_EXIT_CODE
    assert first.killed_at is not None and first.orphan_terminated is False
    assert first.kill() is False, "a second kill is a no-op"
    with pytest.raises((ServerGone, OSError, ValueError)):
        first.call("drawing_info", timeout=5)

    second = harness.spawn()
    assert second.pid not in pids and second.server_pid not in pids
    assert second.call("drawing_open", {"path": str(path)})["is_error"] is False
    replay = second.call("cad_batch", batch)
    assert replay["replayed"] is True and replay["operation_id"] == "fi-1"
    assert second.call("drawing_info")["entity_count"] == 1, "the replay drew nothing"
    store = second.call("system_status")["state_store"]
    assert store["mode"] == "durable" and store["path"].startswith(str(harness.state_dir))
    assert len(harness.servers) == 2, "nothing was respawned behind the caller's back"


def test_a_kill_parked_at_a_barrier_is_finished_by_the_next_startup(harness):
    target = harness.work / "interrupted.dxf"
    server = harness.spawn(barrier=f"save.before_replace:{target.name}")
    server.call("drawing_new")
    assert server.call("entity_create_line", _LINE)["is_error"] is False
    server.send("tools/call", {"name": "drawing_save_as", "arguments": {"path": str(target)}})

    parked = harness.wait_barrier("save.before_replace")
    assert parked["key"] == target.name
    assert parked["pid"] == server.server_pid, "the interpreter that runs server.py parked"
    markers = harness.pending_markers()
    assert len(markers) == 1 and markers[0]["expected_sha256"], "the marker says verified"
    assert not target.exists(), "parked before the replace"
    assert server.kill() is True and _dead(server)

    restarted = harness.spawn()  # no barrier: an ordinary server on the same state
    recovery = restarted.call("system_status")["save_recovery"]
    assert [row["outcome"] for row in recovery] == ["finished"]
    assert target.is_file() and not harness.pending_markers()
    from backends.atomic_write import file_sha256

    assert file_sha256(target) == markers[0]["expected_sha256"]
    opened = restarted.call("drawing_open", {"path": str(target)})
    assert opened["is_error"] is False
    assert restarted.call("drawing_info")["entity_count"] == 1


def test_the_variable_alone_never_parks_a_server(harness):
    # The production lock: ACADMCP_TEST_BARRIER set, the marker folder NOT on
    # the child's path. The save runs through and no signal is written.
    target = harness.work / "plain.dxf"
    env = harness.env(**{fault_barrier.BARRIER_ENV: "save.before_replace"})
    env[fault_barrier.BARRIER_DIR_ENV] = str(harness.signals)
    assert str(MARKER_DIR) not in env.get("PYTHONPATH", "")
    server = harness.spawn(env=env)
    server.call("drawing_new")
    saved = server.call("drawing_save_as", {"path": str(target)}, timeout=60)
    assert saved["is_error"] is False and target.is_file()
    assert not list(harness.signals.iterdir()), "no barrier fired"
    assert server.kill() is True


def test_the_marker_module_is_not_importable_outside_the_harness():
    assert importlib.util.find_spec(fault_barrier.MARKER_MODULE) is None
    assert str(MARKER_DIR) not in sys.path
    assert not (MARKER_DIR / "__init__.py").exists(), "not a package of the project"
    assert fault_barrier.marker_present() is False
    # armed() reads the variable only; barrier() returns at once without the marker
    environ = {fault_barrier.BARRIER_ENV: "journal.after_begin:op-9"}
    assert fault_barrier.armed("journal.after_begin", environ) is True
    assert fault_barrier.armed("save.before_replace", environ) is False
    with pytest.raises(ValueError, match="unknown fault barrier"):
        fault_barrier.armed("save.typo", environ)
    with pytest.raises(ValueError, match="unknown fault barrier"):
        fault_inject.barrier_env("journal.typo", ROOT, {})


def test_the_wheel_never_ships_the_marker():
    import tomllib

    wheel = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    included = wheel["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    assert not any(entry.startswith("tests") for entry in included)
    from scripts import check_dist_contents

    assert "tests/" in check_dist_contents.WHEEL_FORBIDDEN


def test_retries_happen_at_spawn_only(harness, monkeypatch, tmp_path):
    broken = tmp_path / "exits.py"
    broken.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(fault_inject, "SERVER", broken)
    monkeypatch.setattr(fault_inject, "_SPAWN_PAUSE_S", 0.0)
    with pytest.raises(SpawnError, match="did not start"):
        harness.spawn(attempts=2, startup_timeout=30)
    assert len(harness.servers) == 2 and len(harness.spawn_failures) == 2
    assert all(not server.alive for server in harness.servers)


def test_close_removes_the_state_folder_after_every_child_is_dead():
    fh = FaultHarness()
    server = fh.spawn()
    server.call("drawing_new")
    assert server.call("entity_create_line", _LINE)["is_error"] is False
    assert any(fh.state_dir.iterdir()), "the journal is on disk"
    base = fh.base
    assert fh.close() is True and fh.removed is True
    assert not base.exists()
    assert _dead(server)
    # what a report records about the child
    described = fault_inject.describe(server)
    assert described["alive"] is False and described["exit_code"] == KILLED_EXIT_CODE
    json.dumps(described)
