"""Mocked-COM regression tests for the Sprint-3 COM fixes.

These exercise the COM `_sync` bodies without a live AutoCAD by:
  * bypassing the STA executor (replace ``_run`` with a direct caller), and
  * patching the module-level ``_acad_app`` / ``_acad_doc`` / ``_entity_info``.

Covers: N6/R23 (run_lisp guard + USERS1 capture), set_variable coercion,
R16 (transaction flag cleared on error), R13 (offset honors side_x/side_y).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import backends.com_backend as cb
from backends.com_backend import ComBackend

pytestmark = pytest.mark.asyncio


def _backend_no_executor():
    b = ComBackend()

    async def _run(func, *args, **kwargs):  # bypass the ThreadPoolExecutor
        return func(*args, **kwargs)

    b._run = _run
    return b


class _Doc:
    """Idle doc: GetVariable returns 0 (CMDACTIVE clear) and ModelSpace is empty."""

    def __init__(self):
        self.sent: list[str] = []
        self._vars: dict = {}
        self.ModelSpace: list = []

    def GetVariable(self, name):
        return self._vars.get(name, 0)

    def SetVariable(self, name, val):
        self._vars[name] = val

    def SendCommand(self, cmd):
        self.sent.append(cmd)

    def EndUndoMark(self):
        pass


# ── NEW-com-set-variable-coercion ───────────────────────────────────────────


async def test_set_variable_coerces_string_to_sysvar_int_type(monkeypatch):
    # GetVariable / SetVariable are AcadDocument members (the Application has
    # neither), so the fake is the document that `_acad_doc()` returns.
    doc = MagicMock()
    doc.GetVariable.return_value = 4159  # OSMODE current value is an int
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    b = _backend_no_executor()
    out = await b.system_set_variable("OSMODE", "0")

    name, val = doc.SetVariable.call_args[0]
    assert name == "OSMODE"
    assert val == 0 and isinstance(val, int)  # coerced from "0" to int 0
    assert out["value"] == 0


# ── R23 / N6 — run_lisp guard + value capture ───────────────────────────────


async def test_run_lisp_refuses_when_cmdactive(monkeypatch):
    app = MagicMock()
    app.GetVariable.side_effect = lambda n: 1 if n == "CMDACTIVE" else 0
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(cb, "_acad_doc", lambda: _Doc())

    b = _backend_no_executor()
    with pytest.raises(RuntimeError):
        await b.system_run_lisp("(+ 1 2)")


async def test_run_lisp_captures_value_from_users1(monkeypatch):
    app = MagicMock()
    app.GetVariable.side_effect = lambda n: {"CMDACTIVE": 0, "USERS1": "3"}.get(n, 0)
    doc = _Doc()
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    b = _backend_no_executor()
    out = await b.system_run_lisp("(+ 1 2)")

    assert out["result"] == "3"  # captured, not the bogus "nil"
    assert any("USERS1" in c for c in doc.sent)  # value stashed out-of-band


# ── R16 — transaction flag cleared even on error ────────────────────────────


async def test_transaction_begin_sets_flag_and_marks_undo(monkeypatch):
    doc = _Doc()
    doc.StartUndoMark = MagicMock(side_effect=lambda: doc.sent.append("StartUndoMark"))
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    b = _backend_no_executor()
    out = await b.transaction_begin()
    assert out["ok"] is True
    assert b._transaction_active is True
    doc.StartUndoMark.assert_called_once()
    # The UNDO Mark goes in *before* the undo group: `_UNDO _B` in the rollback
    # returns to it, and a Mark inside the group would be undone with it.
    assert doc.sent == ["_.UNDO _M\n", "StartUndoMark"], doc.sent


async def test_transaction_begin_refuses_while_a_prompt_is_active(monkeypatch):
    """With CMDACTIVE set, SendCommand would feed `_UNDO _M` to the active
    command as input and no Mark would exist for the rollback to reach."""
    doc = _Doc()
    doc.SetVariable("CMDACTIVE", 1)
    doc.StartUndoMark = MagicMock()
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    b = _backend_no_executor()
    out = await b.transaction_begin()
    assert out["ok"] is False and "CMDACTIVE" in out["error"] and "ESC" in out["error"]
    assert b._transaction_active is False, "nothing reached AutoCAD"
    assert doc.sent == []
    doc.StartUndoMark.assert_not_called()


async def test_transaction_commit_clears_flag_on_error():
    b = _backend_no_executor()

    async def boom(func, *a, **k):
        raise RuntimeError("EndUndoMark failed")

    b._run = boom
    b._transaction_active = True
    with pytest.raises(RuntimeError):
        await b.transaction_commit()
    assert b._transaction_active is False  # not left stale


class _MarkingDoc(_Doc):
    """An idle doc that keeps AutoCAD's UNDO Mark bookkeeping.

    On the live AutoCAD 2026 `_UNDO B` with no Mark answers "This will undo
    everything. OK? <Y>" and `SendCommand` blocks at that prompt until a human
    presses ESC (measured: the rollback hit the 60 s COM timeout, the entities
    stayed, every COM client was rejected meanwhile). `StartUndoMark` /
    `EndUndoMark` form an undo *group*, not a Mark. This fake answers the
    unmarked Back the way the prompt would end up: as a failure, not a hang.
    """

    def __init__(self):
        super().__init__()
        self.marks = 0
        self.group_open = False

    def SendCommand(self, cmd):
        super().SendCommand(cmd)
        if cmd.strip() == "_.UNDO _M":
            self.marks += 1
        elif cmd.strip() == "_.UNDO _B":
            if self.marks == 0:
                raise RuntimeError("prompt: This will undo everything. OK? <Y>")
            self.marks -= 1

    def StartUndoMark(self):
        self.group_open = True

    def EndUndoMark(self):
        self.group_open = False


async def test_transaction_rollback_goes_back_to_the_mark_begin_set(monkeypatch):
    doc = _MarkingDoc()  # idle: CMDACTIVE=0 so _safe_send_command returns promptly
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    b = _backend_no_executor()
    assert (await b.transaction_begin())["ok"] is True
    out = await b.transaction_rollback()
    assert out["ok"] is True
    assert b._transaction_active is False
    # UNDO Back went through _safe_send_command (which appends a trailing
    # newline) and found the Mark begin set: no prompt, group closed, Mark consumed.
    assert [c.strip() for c in doc.sent] == ["_.UNDO _M", "_.UNDO _B"]
    assert doc.marks == 0 and doc.group_open is False


async def test_transaction_rollback_without_a_transaction_is_refused(monkeypatch):
    """No transaction means no Mark: an unmarked `_UNDO _B` is the prompt
    that strands the STA worker, so it is never sent."""
    doc = _MarkingDoc()
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    b = _backend_no_executor()
    out = await b.transaction_rollback()
    assert out == {"ok": False, "error": "No active transaction to rollback"}
    assert doc.sent == []


# ── R13 — offset honors side_x/side_y and deletes the unused copy ───────────


async def test_offset_picks_copy_nearest_side_point(monkeypatch):
    # ent.Offset(+d) -> copy centred at (0, +10); ent.Offset(-d) -> (0, -10).
    pos_copy = MagicMock(name="pos")
    pos_copy.GetBoundingBox.return_value = ((-1, 9), (1, 11))  # centre (0, 10)
    neg_copy = MagicMock(name="neg")
    neg_copy.GetBoundingBox.return_value = ((-1, -11), (1, -9))  # centre (0, -10)

    ent = MagicMock()
    ent.Offset.side_effect = lambda d: [pos_copy] if d > 0 else [neg_copy]
    doc = MagicMock()
    doc.HandleToObject.return_value = ent
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(cb, "_entity_info", lambda e: e)  # return the entity itself

    b = _backend_no_executor()
    # Side point near the negative copy -> keep neg, delete pos.
    kept = await b.entity_offset("AA", 5.0, side_x=0.0, side_y=-10.0)
    assert kept is neg_copy
    pos_copy.Delete.assert_called_once()
    neg_copy.Delete.assert_not_called()
