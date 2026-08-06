"""Tests for drawing operations via ezdxf backend."""

from __future__ import annotations

import pytest

import config

pytestmark = pytest.mark.asyncio


async def test_drawing_info(backend):
    info = await backend.drawing_info()
    assert info.backend == "ezdxf"
    assert info.entity_count >= 0


async def test_drawing_new(backend):
    result = await backend.drawing_new()
    assert result.get("ok") is True


async def test_drawing_save_and_open(backend, tmp_path):
    await backend.entity_create_line(0, 0, 100, 0)
    save_path = str(tmp_path / "test_save.dxf")
    await backend.drawing_save(save_path)
    await backend.drawing_open(save_path)
    info = await backend.drawing_info()
    assert info.entity_count >= 1


async def test_drawing_export_dxf(backend, tmp_path):
    await backend.entity_create_circle(0, 0, 50)
    export_path = str(tmp_path / "export.dxf")
    result = await backend.drawing_export_dxf(export_path)
    assert result.get("ok") is True
    import os

    assert os.path.exists(export_path)


async def test_drawing_purge(backend):
    await backend.layer_create("TEMP_LAYER")
    result = await backend.drawing_purge()
    assert result.get("ok") is True


async def test_drawing_audit(backend):
    result = await backend.drawing_audit()
    assert result.get("ok") is True
    # Asserting only `ok` is why the audit debt survived to 1.5.0: the tool
    # repaired documents and reported nothing about it, and this test was green
    # throughout. The contract is the whole key set (see backends/base.py).
    assert {"ok", "repaired", "fixes", "fix_count", "errors", "error_count"} <= result.keys()


async def test_drawing_undo(backend, monkeypatch):
    # `assert isinstance(result, dict)` used to be the whole test, and it passed
    # for years against an undo that could only ever fail. See
    # tests/test_undo_redo.py for the full contract.
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 4)
    await backend.drawing_new()
    await backend.entity_create_line(0, 0, 100, 0)

    result = await backend.drawing_undo()

    assert result["ok"] is True
    assert len(list(backend._doc.modelspace())) == 0


async def test_drawing_redo(backend, monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 4)
    await backend.drawing_new()
    await backend.entity_create_line(0, 0, 100, 0)
    await backend.drawing_undo()

    result = await backend.drawing_redo()

    assert result["ok"] is True
    assert len(list(backend._doc.modelspace())) == 1
