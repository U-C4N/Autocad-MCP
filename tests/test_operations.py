"""B12 - the operation journal: one record per call, honest states, client ids.

Pure: no backend and no server. The request hash's oracle is the canonical-JSON
string written out below; every state transition is driven by hand.
"""

from __future__ import annotations

import hashlib

import pytest

from backends.operations import (
    CURRENT_OPERATION,
    STATES,
    OperationConflictError,
    OperationJournal,
    OperationRecord,
    RequiresReconcileError,
    request_hash,
)

_LINE = {"x1": 0, "y1": 0, "x2": 10, "y2": 0}
_DOC = {
    "document_id": "a" * 32,
    "revision": 4,
    "name": "untitled-1",
    "path": None,
    "backend": "ezdxf",
}


def test_the_states_are_the_documented_vocabulary():
    assert STATES == (
        "running",
        "committed",
        "partial",
        "failed",
        "refused",
        "quarantined",
        "cancelled",
        "unknown_commit",
    )


def test_request_hash_is_the_sha256_of_the_canonical_json():
    canonical = '{"arguments":{"x1":0,"x2":10,"y1":0,"y2":0},"tool":"entity_create_line"}'
    expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert request_hash("entity_create_line", _LINE) == expected
    shuffled = {"y2": 0, "x2": 10, "y1": 0, "x1": 0}
    assert request_hash("entity_create_line", shuffled) == expected, "key order is no part of it"
    assert request_hash("entity_create_circle", _LINE) != expected


def test_a_server_minted_record_runs_and_finishes():
    journal = OperationJournal()
    record, replay = journal.begin("entity_create_line", _LINE, document=_DOC)
    assert isinstance(record, OperationRecord) and replay is None
    assert len(record.operation_id) == 32
    assert set(record.operation_id) <= set("0123456789abcdef")
    assert (record.state, record.client_supplied) == ("running", False)
    assert (record.document_id, record.revision_before, record.revision_after) == (
        "a" * 32,
        4,
        None,
    )
    assert record.request_hash == request_hash("entity_create_line", _LINE)
    journal.finish(record, "committed", document={**_DOC, "revision": 5})
    assert (record.state, record.revision_after, record.error_kind) == ("committed", 5, None)
    assert record.ended is not None and record.ended >= record.started
    assert journal.recent() == [record.to_dict()]
    assert set(record.to_dict()) == {
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
    }


def test_a_client_id_with_the_same_request_replays_the_recorded_result():
    journal = OperationJournal()
    record, _ = journal.begin("entity_create_line", _LINE, operation_id="op-1", document=_DOC)
    assert record.client_supplied is True and record.operation_id == "op-1"
    result = {"handle": "2A"}
    journal.finish(record, "committed", result=result, document=_DOC)
    again, replay = journal.begin("entity_create_line", dict(_LINE), operation_id="op-1")
    assert again is record and replay is result
    assert len(journal) == 1, "a replay records nothing new"


@pytest.mark.parametrize("state", ["partial", "failed", "refused", "quarantined"])
def test_every_finished_state_but_cancelled_replays(state):
    journal = OperationJournal()
    record, _ = journal.begin("cad_batch", {"steps": []}, operation_id="op-2")
    journal.finish(record, state, result="the recorded answer", error_kind="failed")
    assert (
        journal.begin("cad_batch", {"steps": []}, operation_id="op-2")[1] == "the recorded answer"
    )


def test_a_cancelled_operation_runs_again_under_its_id():
    """Cancelled while queued: nothing ran, so nothing can be replayed - or doubled."""
    journal = OperationJournal()
    record, _ = journal.begin("entity_create_line", _LINE, operation_id="op-3")
    journal.finish(record, "cancelled", error_kind="cancelled")
    rerun, replay = journal.begin("entity_create_line", _LINE, operation_id="op-3")
    assert replay is None and rerun is not record and rerun.state == "running"
    assert len(journal) == 1


def test_the_same_id_for_another_request_is_an_operation_conflict():
    journal = OperationJournal()
    record, _ = journal.begin("entity_create_line", _LINE, operation_id="op-4")
    journal.finish(record, "committed", result={"handle": "2A"})
    with pytest.raises(OperationConflictError) as conflict:
        journal.begin("entity_create_line", {**_LINE, "x2": 11}, operation_id="op-4")
    payload = conflict.value.to_dict()
    assert payload["ok"] is False and payload["kind"] == "operation_conflict"
    assert payload["operation_id"] == "op-4" and payload["reason"] == "different_request"


def test_an_id_still_running_is_an_operation_conflict():
    journal = OperationJournal()
    journal.begin("entity_create_line", _LINE, operation_id="op-5")
    with pytest.raises(OperationConflictError) as conflict:
        journal.begin("entity_create_line", _LINE, operation_id="op-5")
    assert conflict.value.to_dict()["reason"] == "in_progress"


def test_an_unknown_commit_must_be_reconciled():
    journal = OperationJournal()
    record, _ = journal.begin("entity_create_line", _LINE, operation_id="op-6")
    journal.finish(record, "unknown_commit", error_kind="cancelled")
    with pytest.raises(RequiresReconcileError) as reconcile:
        journal.begin("entity_create_line", _LINE, operation_id="op-6")
    payload = reconcile.value.to_dict()
    assert payload["kind"] == "requires_reconcile" and payload["operation_id"] == "op-6"
    assert payload["record"]["state"] == "unknown_commit"
    assert "new operation_id" in payload["error"]


def test_the_journal_is_bounded_and_forgets_the_oldest():
    journal = OperationJournal(maxlen=3)
    ids = []
    for index in range(4):
        record, _ = journal.begin(
            "entity_create_line", {**_LINE, "x2": index}, operation_id=f"op-{index}"
        )
        journal.finish(record, "committed", result=index)
        ids.append(record.operation_id)
    assert [row["operation_id"] for row in journal.recent(10)] == ids[1:]
    assert journal.get("op-0") is None
    _, replay = journal.begin("entity_create_line", {**_LINE, "x2": 0}, operation_id="op-0")
    assert replay is None, "an evicted id runs again: the journal is bounded memory (B13)"


@pytest.mark.parametrize(
    "bad, message",
    [
        ("", "must not be empty"),
        (" op", "leading or trailing"),
        ("x" * 129, "at most 128"),
        ("op\n1", "printable"),
        (17, "expected a string"),
    ],
)
def test_a_malformed_client_id_is_refused_by_name(bad, message):
    with pytest.raises(ValueError, match=message):
        OperationJournal().begin("entity_create_line", _LINE, operation_id=bad)


def test_finish_takes_only_a_final_state():
    journal = OperationJournal()
    record, _ = journal.begin("entity_create_line", _LINE)
    with pytest.raises(ValueError, match="state must be one of"):
        journal.finish(record, "running")


def test_the_current_operation_starts_unset():
    assert CURRENT_OPERATION.get() is None
