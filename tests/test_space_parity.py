"""1.8-05 on the headless engine: the same answers the live engine now gives.

The headless engine always drew where ``layout_set_current`` pointed it and
started in model space; what 1.8-05 adds on both engines is the report - an
entity on a sheet names its ``space``, and ``drawing_undo`` / ``drawing_redo``
say ``verified``. (tests/test_com_space.py holds the live engine's half.)
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import config
from backends.ezdxf_backend import EzdxfBackend


@pytest_asyncio.fixture
async def headless(monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 5)
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    yield backend
    await backend.disconnect()


@pytest.mark.asyncio
async def test_a_sheet_entity_names_its_space_and_a_model_one_does_not(headless):
    model = await headless.entity_create_circle(0.0, 0.0, 5.0)
    assert "space" not in model.properties
    assert (await headless.layout_set_current("Layout1"))["ok"] is True
    sheet = await headless.entity_create_circle(0.0, 0.0, 5.0)
    assert sheet.properties["space"] == "Layout1"
    assert (await headless.entity_get(sheet.handle)).properties["space"] == "Layout1"


@pytest.mark.asyncio
async def test_undo_and_redo_report_verified(headless):
    await headless.entity_create_circle(0.0, 0.0, 5.0)
    undo = await headless.drawing_undo()
    assert undo["ok"] is True and undo["verified"] is True
    redo = await headless.drawing_redo()
    assert redo["ok"] is True and redo["verified"] is True
