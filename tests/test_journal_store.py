"""B13 (1.8-03) - the durable operation journal, in process.

``DurableJournal`` keeps the in-memory journal's API (``begin`` / ``finish`` /
``get`` / ``recent``) and writes every record through to
``<STATE_DIR>/journal.sqlite3``: a mutating call is one INSERT (``running``,
committed before the call) and one UPDATE (its final state, queued for the
background flush unless the id is the client's). Two journals on
one file stand in for a server and its restart; a row's owner is a pid plus
that process's start time. The subprocess gate (a real ``python server.py``
killed with ``Popen.kill``) is ``tests/test_journal_durable.py``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastmcp.tools.tool import ToolResult

from backends import journal_store
from backends.journal_store import (
    ARGS_CAP_BYTES,
    JOURNAL_FILE,
    DurableJournal,
    JournalSettingError,
    JournalSettings,
    StateDirRefusedError,
    check_state_dir,
    default_state_dir,
    open_journal,
    process_start_token,
    state_store_status,
)
from backends.operations import (
    OperationConflictError,
    OperationJournal,
    RequiresReconcileError,
    request_hash,
)

ROOT = Path(__file__).resolve().parents[1]
_LINE = {"x1": 0, "y1": 0, "x2": 10, "y2": 0}
_DOC = {"document_id": "d" * 32, "revision": 3}
_READ_ONLY = {"entity_list", "system_status", "drawing_info"}


def _is_read_only(tool: str) -> bool:
    return tool in _READ_ONLY


def _settings(state_dir: Path, **overrides) -> JournalSettings:
    env = {"ACADMCP_STATE_DIR": str(state_dir)}
    env.update(overrides)
    return JournalSettings.from_env(env)


def _journal(state_dir: Path, **kwargs) -> DurableJournal:
    settings = kwargs.pop("settings", None) or _settings(state_dir)
    return DurableJournal(state_dir, settings=settings, is_read_only=_is_read_only, **kwargs)


def _rows(state_dir: Path) -> list[dict]:
    connection = sqlite3.connect(state_dir / JOURNAL_FILE)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM operations ORDER BY seq")]
    finally:
        connection.close()


def _result(payload: dict) -> ToolResult:
    return ToolResult(content=json.dumps(payload), structured_content=payload)


def _dead_owner() -> tuple[int, str | None]:
    """A pid whose process has exited, with the start token it had."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    token = process_start_token(child.pid)
    child.communicate(b"")
    return child.pid, token


@pytest.fixture
def state_dir(outside_repo_dir) -> Path:
    return outside_repo_dir


@pytest.fixture
def slow_flush(monkeypatch):
    """The background flush waits long enough for a test to see the queue."""
    monkeypatch.setattr(journal_store, "FLUSH_DELAY_S", 60.0)


# ── settings and the state folder ────────────────────────────────────────────


def test_the_settings_default_to_the_plan_table(tmp_path):
    settings = JournalSettings.from_env({"LOCALAPPDATA": str(tmp_path)})
    assert settings.durable is True
    assert settings.args == "bounded" and settings.sync == "normal"
    assert settings.retain_days == 30 and settings.result_max_kb == 64
    assert (settings.checkpoint_keep, settings.checkpoint_budget_mb) == (20, 512)
    assert settings.checkpoint_max_doc_mb == 32
    if sys.platform == "win32":
        assert settings.state_dir == tmp_path / "autocad-mcp-pro" / "state"
    assert default_state_dir({"LOCALAPPDATA": str(tmp_path)}) == settings.state_dir


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("JOURNAL_ARGS", "everything"),
        ("JOURNAL_DURABLE", "maybe"),
        ("JOURNAL_SYNC", "extra"),
        ("JOURNAL_RETAIN_DAYS", "0"),
        ("JOURNAL_RESULT_MAX_KB", "-1"),
        ("CHECKPOINT_KEEP", "many"),
    ],
)
def test_a_bad_setting_is_refused_by_its_name(tmp_path, name, value):
    with pytest.raises(JournalSettingError, match=name):
        JournalSettings.from_env({"ACADMCP_STATE_DIR": str(tmp_path), name: value})


def test_journal_durable_off_is_the_1_7_in_memory_journal(tmp_path):
    journal = open_journal({"ACADMCP_STATE_DIR": str(tmp_path), "JOURNAL_DURABLE": "off"})
    assert type(journal) is OperationJournal
    assert state_store_status(journal)["mode"] == "off"
    assert list(tmp_path.iterdir()) == []


def test_the_repository_root_is_refused_as_the_state_dir():
    with pytest.raises(StateDirRefusedError, match="repository"):
        check_state_dir(ROOT)
    journal = open_journal({"ACADMCP_STATE_DIR": str(ROOT)})
    try:
        status = state_store_status(journal)
        assert status["degraded"]["kind"] == "state_dir_refused"
        assert "ACADMCP_STATE_DIR" in status["degraded"]["reason"]
        assert not (ROOT / JOURNAL_FILE).exists()
        record, _ = journal.begin("entity_create_line", _LINE)
        journal.finish(record, "committed")
        assert journal.get(record.operation_id).state == "committed", "memory still records"
    finally:
        journal.close()


def test_any_git_checkout_and_a_drawing_folder_are_refused(outside_repo_dir):
    tmp_path = outside_repo_dir
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    with pytest.raises(StateDirRefusedError, match="repository"):
        check_state_dir(checkout)
    drawings = tmp_path / "drawings"
    drawings.mkdir()
    (drawings / "plan.DXF").write_text("0\nEOF\n", encoding="ascii")
    with pytest.raises(StateDirRefusedError, match="drawing"):
        check_state_dir(drawings)
    assert check_state_dir(tmp_path / "fresh") == (tmp_path / "fresh").resolve()


@pytest.mark.parametrize(
    "inside", ["state", "backends", "tests/fixtures", ".acadmcp", "out/x/deeper"]
)
def test_a_folder_anywhere_inside_the_repository_is_refused(inside):
    """``git add -A`` would commit a journal left in any of these (review 1.8-03)."""
    with pytest.raises(StateDirRefusedError, match="inside .* repository checkout"):
        check_state_dir(ROOT / inside)
    journal = open_journal({"ACADMCP_STATE_DIR": str(ROOT / inside)})
    try:
        assert state_store_status(journal)["degraded"]["kind"] == "state_dir_refused"
    finally:
        journal.close()
    assert not (ROOT / inside / JOURNAL_FILE).exists()


def test_a_folder_inside_any_checkout_is_refused(outside_repo_dir):
    checkout = outside_repo_dir / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: elsewhere\n", encoding="ascii")  # a worktree
    with pytest.raises(StateDirRefusedError, match=r"inside .*checkout"):
        check_state_dir(checkout / "state" / "journal")
    assert check_state_dir(outside_repo_dir / "beside") == (outside_repo_dir / "beside").resolve()


# ── write-through, replay across a restart ───────────────────────────────────


def test_a_client_result_replays_from_the_file_after_a_restart(state_dir):
    first = _journal(state_dir)
    record, _ = first.begin("cad_batch", {"steps": [1]}, operation_id="op-1", document=_DOC)
    first.finish(
        record, "committed", result=_result({"ok": True, "n": 1}), document={**_DOC, "revision": 4}
    )
    first.close()

    second = _journal(state_dir)
    try:
        prior, replay = second.begin("cad_batch", {"steps": [1]}, operation_id="op-1")
        assert isinstance(replay, ToolResult)
        assert replay.structured_content == {"ok": True, "n": 1}
        assert (prior.state, prior.revision_after, prior.client_supplied) == ("committed", 4, True)
        assert second.get("op-1").request_hash == request_hash("cad_batch", {"steps": [1]})
        with pytest.raises(OperationConflictError) as conflict:
            second.begin("cad_batch", {"steps": [2]}, operation_id="op-1")
        assert conflict.value.reason == "different_request"
    finally:
        second.close()


def test_a_recorded_failure_replays_as_a_failure(state_dir):
    first = _journal(state_dir)
    record, _ = first.begin("entity_create_line", _LINE, operation_id="op-f")
    first.finish(record, "failed", result=RuntimeError("the layer is locked"), error_kind="failed")
    first.close()
    second = _journal(state_dir)
    try:
        _, replay = second.begin("entity_create_line", _LINE, operation_id="op-f")
        assert isinstance(replay, BaseException) and "the layer is locked" in str(replay)
    finally:
        second.close()


def test_a_result_over_the_cap_replays_as_its_digest(state_dir):
    settings = _settings(state_dir, JOURNAL_RESULT_MAX_KB="1")
    first = _journal(state_dir, settings=settings)
    big = {"ok": True, "rows": ["x" * 100] * 40}
    record, _ = first.begin("cad_batch", {"steps": []}, operation_id="op-big", document=_DOC)
    first.finish(record, "committed", result=_result(big), document={**_DOC, "revision": 9})
    first.close()
    (row,) = _rows(state_dir)
    assert row["result_sha256"].startswith("sha256:")
    stub = json.loads(row["result_json"])
    assert (stub["type"], stub["reason"], stub["is_error"]) == ("omitted", "over_cap", False)
    assert "x" * 100 not in row["result_json"]

    second = _journal(state_dir, settings=settings)
    try:
        _, replay = second.begin("cad_batch", {"steps": []}, operation_id="op-big")
        payload = replay.structured_content
        assert payload["result_truncated"] is True and payload["revision_after"] == 9
        assert payload["result_sha256"] == row["result_sha256"]
        assert "rows" not in payload and replay.is_error is False
    finally:
        second.close()


def test_a_truncated_error_replays_as_an_error(state_dir):
    """Review 1.8-03: a truncated replay used to lose ``is_error``."""
    settings = _settings(state_dir, JOURNAL_RESULT_MAX_KB="0")
    first = _journal(state_dir, settings=settings)
    refused = ToolResult(
        content="refused", structured_content={"ok": False, "kind": "not_found"}, is_error=True
    )
    record, _ = first.begin("entity_move", {"handle": "FFFFFF"}, operation_id="op-e")
    first.finish(record, "refused", result=refused, error_kind="not_found")
    raised, _ = first.begin("entity_move", {"handle": "EEEEEE"}, operation_id="op-x")
    first.finish(raised, "failed", result=RuntimeError("no such handle"), error_kind="failed")
    first.close()
    second = _journal(state_dir, settings=settings)
    try:
        _, replay = second.begin("entity_move", {"handle": "FFFFFF"}, operation_id="op-e")
        assert replay.is_error is True and replay.structured_content["ok"] is False
        assert replay.structured_content["result_truncated"] is True
        assert replay.structured_content["kind"] == "not_found"
        _, failure = second.begin("entity_move", {"handle": "EEEEEE"}, operation_id="op-x")
        assert isinstance(failure, BaseException) and "not available" in str(failure)
    finally:
        second.close()


@pytest.mark.parametrize("mode", ["digest", "off"])
def test_digest_and_off_keep_no_answer_text_for_a_client_id(state_dir, mode):
    """Review 1.8-03: a client id's answer repeats its arguments (a ``cad_batch``
    echoes its steps' layer names and texts), so under ``JOURNAL_ARGS=digest`` /
    ``off`` it is kept as its sha256 and replays truncated after a restart."""
    marker = "ZQX-CUSTOMER-SECRET-7781"
    settings = _settings(state_dir, JOURNAL_ARGS=mode)
    first = _journal(state_dir, settings=settings)
    steps = {"steps": [{"tool": "layer_create", "args": {"name": marker}}]}
    answer = {"ok": True, "results": [{"tool": "layer_create", "name": marker}]}
    record, _ = first.begin("cad_batch", steps, operation_id="op-leak", document=_DOC)
    first.finish(record, "committed", result=_result(answer), document={**_DOC, "revision": 4})
    failed, _ = first.begin("layer_set_current", {"name": marker}, operation_id="op-leak2")
    first.finish(failed, "failed", result=RuntimeError(f"no layer {marker}"), error_kind="failed")
    first.close()
    blob = b"".join(path.read_bytes() for path in state_dir.iterdir() if path.is_file())
    assert marker.encode() not in blob

    second = _journal(state_dir, settings=settings)
    try:
        _, replay = second.begin("cad_batch", steps, operation_id="op-leak")
        payload = replay.structured_content
        assert payload["ok"] is True and payload["result_truncated"] is True
        assert payload["revision_after"] == 4 and f"JOURNAL_ARGS={mode}" in payload["note"]
        _, failure = second.begin("layer_set_current", {"name": marker}, operation_id="op-leak2")
        assert isinstance(failure, BaseException) and marker not in str(failure)
    finally:
        second.close()


def test_a_running_row_of_a_dead_server_becomes_unknown_commit(state_dir):
    owner = _dead_owner()
    first = _journal(state_dir, owner=owner)
    first.begin("entity_create_circle", {"r": 5}, operation_id="op-3")
    first.begin("entity_list", {}, operation_id="op-read")  # a read: nothing to reconcile
    first._connection.close()  # killed: no finish, no close

    second = _journal(state_dir)
    try:
        with pytest.raises(RequiresReconcileError) as reconcile:
            second.begin("entity_create_circle", {"r": 5}, operation_id="op-3")
        assert reconcile.value.record.error_kind == "process_exit"
        assert "server process ended" in str(reconcile.value)
        read, replay = second.begin("entity_list", {}, operation_id="op-read")
        assert replay is None and read.state == "running", "a cancelled read runs again"
        swept = {row["operation_id"]: row for row in _rows(state_dir)}
        assert swept["op-3"]["state"] == "unknown_commit"
        assert swept["op-3"]["ended"] is not None
    finally:
        second.close()


def test_a_server_that_dies_after_this_one_opened_is_reconciled_at_lookup(state_dir):
    """Review 1.8-03: the open's sweep alone left ``in_progress`` forever."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        first = _journal(state_dir, owner=(child.pid, process_start_token(child.pid)))
        first.begin("entity_create_circle", {"r": 5}, operation_id="op-H")
        second = _journal(state_dir)  # opens while the owner still runs
    finally:
        child.communicate(b"")
    try:
        with pytest.raises(RequiresReconcileError) as reconcile:
            second.begin("entity_create_circle", {"r": 5}, operation_id="op-H")
        assert reconcile.value.record.error_kind == "process_exit"
        (row,) = _rows(state_dir)
        assert (row["state"], row["error_kind"]) == ("unknown_commit", "process_exit")
    finally:
        second.close()
        first._connection.close()
        first._connection = None


def test_a_failed_open_is_tried_again(state_dir, monkeypatch):
    """Review 1.8-03: an open right after a kill can fail once with an I/O error."""
    real_connect = sqlite3.connect
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(journal_store, "OPEN_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(journal_store.sqlite3, "connect", flaky)
    journal = _journal(state_dir)
    try:
        assert journal.degraded is None and len(calls) == 2
    finally:
        journal.close()


def test_a_live_second_server_keeps_its_running_rows(state_dir):
    first = _journal(state_dir)  # this very process: alive
    first.begin("entity_create_circle", {"r": 5}, operation_id="op-live")
    second = _journal(state_dir)
    try:
        assert _rows(state_dir)[0]["state"] == "running"
        with pytest.raises(OperationConflictError) as conflict:
            second.begin("entity_create_circle", {"r": 5}, operation_id="op-live")
        assert conflict.value.reason == "in_progress"
    finally:
        second.close()
        first.close()


# ── what is written, and how much ────────────────────────────────────────────


def test_a_mutating_call_is_one_commit_on_its_hot_path(state_dir, slow_flush):
    """Review 1.8-03 (B10): the call itself pays one single-statement commit.

    Its running row is committed before it runs; its final UPDATE and a read's
    row wait for the background flush, which commits them together.
    """
    journal = _journal(state_dir)
    statements: list[str] = []
    journal._connection.set_trace_callback(statements.append)
    try:
        record, _ = journal.begin("entity_create_line", _LINE, document=_DOC)
        assert [s.split()[0] for s in statements] == ["INSERT"], "committed before the call"
        assert _rows(state_dir)[0]["state"] == "running"
        journal.finish(record, "committed", document={**_DOC, "revision": 4})
        read, _ = journal.begin("entity_list", {}, document=_DOC)
        journal.finish(read, "committed", document=_DOC)
        assert [s.split()[0] for s in statements] == ["INSERT"], "nothing else on the hot path"
        journal.flush()
        assert [s.split()[0] for s in statements] == [
            "INSERT",
            "BEGIN",
            "UPDATE",
            "INSERT",
            "COMMIT",
        ]
    finally:
        journal._connection.set_trace_callback(None)
        journal.close()
    rows = _rows(state_dir)
    assert [(r["tool"], r["state"]) for r in rows] == [
        ("entity_create_line", "committed"),
        ("entity_list", "committed"),
    ]
    assert rows[0]["revision_before"] == 3 and rows[0]["revision_after"] == 4
    assert rows[0]["pid"] == os.getpid() and rows[0]["server_instance"]
    assert rows[0]["result_json"] is None, "only a client id's answer is kept"


def test_a_server_id_row_touches_no_index(state_dir):
    """The only index is the partial one over client ids (unique there)."""
    journal = _journal(state_dir)
    try:
        indexes = journal._connection.execute("PRAGMA index_list(operations)").fetchall()
        assert [(row[1], row[2], row[4]) for row in indexes] == [("operations_client_id", 1, 1)]
        record, _ = journal.begin("cad_batch", {"n": 1}, operation_id="op-u")
        journal.finish(record, "cancelled", error_kind="cancelled")
        rerun, replay = journal.begin("cad_batch", {"n": 1}, operation_id="op-u")
        assert replay is None and rerun.state == "running"
    finally:
        journal.close()
    assert [row["operation_id"] for row in _rows(state_dir)] == ["op-u"], "replaced in place"


def test_the_background_flush_writes_the_queue(state_dir):
    journal = _journal(state_dir)
    try:
        record, _ = journal.begin("entity_create_line", _LINE)
        journal.finish(record, "committed")
        deadline = time.monotonic() + 30
        while _rows(state_dir)[0]["state"] != "committed":
            assert time.monotonic() < deadline, "the queued UPDATE was never flushed"
            time.sleep(0.02)
    finally:
        journal.close()


def test_a_client_id_final_state_is_committed_before_finish_returns(state_dir, slow_flush):
    journal = _journal(state_dir)
    try:
        record, _ = journal.begin("cad_batch", {"n": 1}, operation_id="op-now")
        journal.finish(record, "committed", result=_result({"ok": True}))
        (row,) = _rows(state_dir)
        assert row["state"] == "committed" and json.loads(row["result_json"])["type"]
    finally:
        journal.close()


def test_a_queued_final_state_lost_to_a_kill_is_unknown_commit(state_dir, slow_flush):
    """Conservative: a kill before the flush never leaves a false ``committed``."""
    first = _journal(state_dir, owner=_dead_owner())
    record, _ = first.begin("entity_create_line", _LINE)
    first.finish(record, "committed")
    first._stopped.set()
    first._wake.set()
    first._connection.close()  # killed: the queue is lost
    first._connection = None
    second = _journal(state_dir)
    try:
        (row,) = _rows(state_dir)
        assert (row["state"], row["error_kind"]) == ("unknown_commit", "process_exit")
        assert second.get(record.operation_id).state == "unknown_commit"
    finally:
        second.close()


def test_the_arguments_are_bounded_by_default(state_dir):
    journal = _journal(state_dir)
    try:
        small, _ = journal.begin("entity_create_line", _LINE)
        big_args = {"text": "y" * (ARGS_CAP_BYTES * 2)}
        big, _ = journal.begin("entity_create_text", big_args)
    finally:
        journal.close()
    rows = {row["operation_id"]: row for row in _rows(state_dir)}
    assert json.loads(rows[small.operation_id]["args_json"]) == _LINE
    wrapped = json.loads(rows[big.operation_id]["args_json"])
    assert wrapped["truncated"] is True and wrapped["bytes"] > ARGS_CAP_BYTES
    assert wrapped["sha256"].startswith("sha256:")
    assert len(wrapped["head"].encode("utf-8")) <= ARGS_CAP_BYTES


@pytest.mark.parametrize("mode", ["digest", "off"])
def test_digest_and_off_leave_no_argument_text_in_the_file(state_dir, mode):
    marker = "ZQX-CUSTOMER-TEXT-7731"
    journal = _journal(state_dir, settings=_settings(state_dir, JOURNAL_ARGS=mode))
    try:
        record, _ = journal.begin("entity_create_text", {"text": marker})
        journal.finish(record, "committed")
        read, _ = journal.begin("entity_list", {"layer_filter": marker})
        journal.finish(read, "committed")
    finally:
        journal.close()
    blob = b"".join(path.read_bytes() for path in state_dir.iterdir() if path.is_file())
    assert marker.encode() not in blob
    stored = _rows(state_dir)[0]["args_json"]
    if mode == "off":
        assert stored is None
    else:
        assert set(json.loads(stored)) == {"sha256"}


def test_journal_sync_full_is_opt_in(state_dir):
    normal = _journal(state_dir)
    try:
        assert normal._connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert normal._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        normal.close()
    full = _journal(state_dir, settings=_settings(state_dir, JOURNAL_SYNC="full"))
    try:
        assert full._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        full.close()


# ── retention and eviction ───────────────────────────────────────────────────


def test_rows_older_than_the_retention_are_dropped_at_open(state_dir):
    journal = _journal(state_dir)
    for name in ("op-old", "op-new"):
        record, _ = journal.begin("cad_batch", {"n": name}, operation_id=name)
        journal.finish(record, "committed", result=_result({"ok": True}))
    journal.close()
    connection = sqlite3.connect(state_dir / JOURNAL_FILE)
    day = 86400.0
    with connection:
        connection.execute(
            "UPDATE operations SET started = ?, ended = ? WHERE operation_id = 'op-old'",
            (time.time() - 31 * day, time.time() - 31 * day),
        )
        connection.execute(
            "UPDATE operations SET started = ?, ended = ? WHERE operation_id = 'op-new'",
            (time.time() - 29 * day, time.time() - 29 * day),
        )
    connection.close()

    reopened = _journal(state_dir)
    try:
        assert reopened.get("op-old") is None
        rerun, replay = reopened.begin("cad_batch", {"n": "op-old"}, operation_id="op-old")
        assert replay is None and rerun.state == "running", "a forgotten id runs again"
        _, kept = reopened.begin("cad_batch", {"n": "op-new"}, operation_id="op-new")
        assert kept.structured_content == {"ok": True}
    finally:
        reopened.close()


def test_an_id_evicted_from_memory_still_replays_from_the_file(state_dir):
    journal = _journal(state_dir, maxlen=2)
    try:
        for index in range(4):
            record, _ = journal.begin("cad_batch", {"i": index}, operation_id=f"op-{index}")
            journal.finish(record, "committed", result=_result({"ok": True, "i": index}))
        assert len(journal) == 2, "memory stays bounded"
        _, replay = journal.begin("cad_batch", {"i": 0}, operation_id="op-0")
        assert replay.structured_content == {"ok": True, "i": 0}
    finally:
        journal.close()


def test_recent_shows_an_earlier_run_before_this_one(state_dir):
    first = _journal(state_dir)
    record, _ = first.begin("entity_create_line", _LINE)
    first.finish(record, "committed")
    first.close()
    second = _journal(state_dir)
    try:
        now, _ = second.begin("entity_create_circle", {"r": 1})
        rows = second.recent()
        assert [r["tool"] for r in rows] == ["entity_create_line", "entity_create_circle"]
        assert rows[0] == record.to_dict()
        assert rows[1] == now.to_dict()
    finally:
        second.close()


# ── degrading, never failing a call ──────────────────────────────────────────


def test_a_locked_file_degrades_to_memory_and_says_so(state_dir):
    journal = _journal(state_dir, busy_timeout_ms=50)
    blocker = sqlite3.connect(state_dir / JOURNAL_FILE, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        record, replay = journal.begin("entity_create_line", _LINE, operation_id="op-busy")
        journal.finish(record, "committed", result=_result({"ok": True}))
        assert replay is None and journal.get("op-busy").state == "committed"
        status = journal.state_store()
        assert status["degraded"]["kind"] == "write_failed"
        assert "locked" in status["degraded"]["reason"]
        assert status["write_failures"] == 1
        _, again = journal.begin("entity_create_line", _LINE, operation_id="op-busy")
        assert again.structured_content == {"ok": True}, "memory still replays"
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        journal.close()


def test_the_state_store_status_counts_what_it_holds(state_dir):
    journal = _journal(state_dir)
    try:
        for revision in (1, 2):
            record, _ = journal.begin(
                "entity_create_line", _LINE, document={**_DOC, "revision": revision}
            )
            journal.finish(record, "committed")
        status = journal.state_store()
    finally:
        journal.close()
    assert status["mode"] == "durable" and status["degraded"] is False
    assert status["path"] == str(state_dir / JOURNAL_FILE)
    assert status["bytes"] > 0 and status["documents"] == 1 and status["checkpoints"] == 0
    assert status["operations"] == 2 and status["write_failures"] == 0
