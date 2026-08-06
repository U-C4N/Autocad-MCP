"""Regression tests for the Sprint-4 ezdxf-backend fixes.

  * R24 — _apply_attrs loads CENTER/HIDDEN on demand so entity_create_line(
           linetype=...) actually renders the dashed linetype instead of
           silently falling back to Continuous.
  * S3  — block_list surfaces the block definition's description instead of "".
  * R26/N9 — view_zoom_extents / view_zoom_window return a consistent
           {"ok": True, "applied": False, ...} shape (no fake framing success).
  * NEW-undo-1 — drawing_undo / transaction_* serialize _undo_stack access
           (no read/mutate outside self._lock); behaviour still correct.

All tests are backend-agnostic ezdxf and use the shared `backend` fixture.
"""

from __future__ import annotations

import pytest

import config

pytestmark = pytest.mark.asyncio


# ── R24 — CENTER/HIDDEN linetype actually loaded + applied ──────────────────


async def test_create_line_loads_center_linetype(backend):
    # Fresh doc: CENTER is not loaded yet.
    assert "CENTER" not in backend._doc.linetypes

    line = await backend.entity_create_line(0, 0, 100, 0, linetype="CENTER")

    # The linetype must now be defined in the document (a real dashed pattern),
    # not silently dropped — otherwise it renders as Continuous.
    assert "CENTER" in backend._doc.linetypes

    # And it must be assigned to the entity.
    info = await backend.entity_get(line.handle)
    assert info.linetype == "CENTER"


async def test_create_line_loads_hidden_linetype(backend):
    assert "HIDDEN" not in backend._doc.linetypes
    line = await backend.entity_create_line(0, 0, 50, 50, linetype="HIDDEN")
    assert "HIDDEN" in backend._doc.linetypes
    info = await backend.entity_get(line.handle)
    assert info.linetype == "HIDDEN"


async def test_create_line_continuous_does_not_error(backend):
    # Built-in linetypes are a no-op for the loader — must not raise.
    line = await backend.entity_create_line(0, 0, 10, 0, linetype="Continuous")
    info = await backend.entity_get(line.handle)
    assert info.linetype == "Continuous"


# ── S3 — block description surfaced from the definition ─────────────────────


async def test_block_list_surfaces_description(backend):
    # Create a block from an entity, then stamp a description on its definition.
    seg = await backend.entity_create_line(0, 0, 10, 0)
    await backend.block_create_from_entities("WIDGET", [seg.handle], 0.0, 0.0)
    backend._doc.blocks.get("WIDGET").block.dxf.description = "Mechanical widget"

    blocks = await backend.block_list()
    widget = next(b for b in blocks if b.name == "WIDGET")
    assert widget.description == "Mechanical widget"


async def test_block_list_description_defaults_empty(backend):
    # A block with no description must still surface "" (no crash, default).
    seg = await backend.entity_create_line(0, 0, 5, 0)
    await backend.block_create_from_entities("PLAIN", [seg.handle], 0.0, 0.0)

    blocks = await backend.block_list()
    plain = next(b for b in blocks if b.name == "PLAIN")
    assert plain.description == ""


# ── R26 / N9 — honest zoom payload shape ────────────────────────────────────


async def test_zoom_extents_reports_not_applied(backend):
    res = await backend.view_zoom_extents()
    assert res["ok"] is True
    assert res["applied"] is False
    assert "message" in res


async def test_zoom_window_reports_not_applied(backend):
    res = await backend.view_zoom_window(0, 0, 100, 100)
    assert res["ok"] is True
    assert res["applied"] is False
    assert "message" in res


# ── NEW-undo-1 — undo/transaction stack behaviour under lock ────────────────


async def test_undo_empty_stack_returns_error(backend, monkeypatch):
    """With history on but nothing done yet, undo declines rather than raising."""
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 4)
    await backend.drawing_new()

    res = await backend.drawing_undo()

    assert res["ok"] is False
    assert "undo" in res["error"].lower()


async def test_commit_without_transaction_returns_error(backend):
    res = await backend.transaction_commit()
    assert res["ok"] is False
    assert "transaction" in res["error"].lower()


async def test_rollback_without_transaction_returns_error(backend):
    res = await backend.transaction_rollback()
    assert res["ok"] is False
    assert "transaction" in res["error"].lower()


async def test_transaction_begin_commit_roundtrip(backend):
    begin = await backend.transaction_begin()
    assert begin["ok"] is True
    assert len(backend._transaction_stack) == 1
    assert len(backend._undo_stack) == 0

    commit = await backend.transaction_commit()
    assert commit["ok"] is True
    assert len(backend._transaction_stack) == 0


async def test_transaction_rollback_restores_snapshot(backend):
    # Snapshot an empty doc, add an entity, then roll back to the snapshot.
    await backend.transaction_begin()
    line = await backend.entity_create_line(0, 0, 10, 0)
    before = await backend.entity_get(line.handle)
    assert before.handle  # entity exists

    res = await backend.transaction_rollback()
    assert res["ok"] is True
    assert len(backend._transaction_stack) == 0

    # The line is gone after rollback.
    listed = await backend.entity_list(type_filter="LINE")
    assert listed == []
