"""M5 — undo/redo on the headless backend, and what it costs.

The plan called this "add ezdxf redo". The larger half was that **undo did not
work either**: `_undo_stack` was declared, read and cleaned up, but nothing ever
appended to it, so `drawing_undo` could only ever answer "Nothing to undo". Four
places promised otherwise — the `@cad_tool` summary ("Step back one operation"),
the tool title, the docstring, and the alias corpus, which routes `UNDO` and
"take that back" straight at it. The existing test asserted
``isinstance(result, dict)``, which a permanent failure satisfies.

Why it is opt-in. ezdxf has no journal; the only way to restore a previous state
is a full DXF snapshot. Measured on this machine:

    10 entities ->   3.1 ms,  19 KB
   100 entities ->   5.6 ms,  31 KB
  1000 entities ->  28.4 ms, 146 KB
  5000 entities -> 129.7 ms, 658 KB

Snapshotting on every mutation would make each new entity in a 5000-entity
drawing cost 130 ms. So `EZDXF_UNDO_DEPTH` defaults to 0 (off) and the tools
refuse with a capability that names the setting, rather than the server quietly
either lying or getting slow.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import config
from backends.base import UnsupportedCapabilityError
from backends.ezdxf_backend import EzdxfBackend

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def undoable(monkeypatch):
    """A backend with undo switched on, as a user opting in would have it."""
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 8)
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    yield backend
    await backend.disconnect()


async def _count(backend) -> int:
    return len(list(backend._doc.modelspace()))


# ── the promise, kept ───────────────────────────────────────────────────────


async def test_undo_actually_removes_the_last_entity(undoable):
    await undoable.entity_create_line(0, 0, 10, 0)
    await undoable.entity_create_circle(5, 5, 2)
    assert await _count(undoable) == 2

    result = await undoable.drawing_undo()

    assert result["ok"] is True
    assert await _count(undoable) == 1, "the circle must be gone"


async def test_redo_puts_it_back(undoable):
    await undoable.entity_create_line(0, 0, 10, 0)
    await undoable.entity_create_circle(5, 5, 2)
    await undoable.drawing_undo()

    result = await undoable.drawing_redo()

    assert result["ok"] is True
    assert await _count(undoable) == 2


async def test_undo_and_redo_walk_the_whole_history(undoable):
    for i in range(4):
        await undoable.entity_create_line(0, i, 10, i)
    assert await _count(undoable) == 4

    for expected in (3, 2, 1):
        await undoable.drawing_undo()
        assert await _count(undoable) == expected
    for expected in (2, 3, 4):
        await undoable.drawing_redo()
        assert await _count(undoable) == expected


async def test_undo_stops_at_the_start_without_claiming_success(undoable):
    await undoable.entity_create_line(0, 0, 10, 0)
    await undoable.drawing_undo()

    result = await undoable.drawing_undo()
    assert result["ok"] is False
    assert await _count(undoable) == 0


async def test_redo_with_nothing_to_redo_says_so(undoable):
    await undoable.entity_create_line(0, 0, 10, 0)
    result = await undoable.drawing_redo()
    assert result["ok"] is False


# ── the branch that must not be reachable ───────────────────────────────────


async def test_drawing_after_an_undo_discards_the_redo_branch(undoable):
    """Redoing onto a timeline the user left would restore a state that never
    existed — geometry they had deleted reappearing beside geometry they drew
    afterwards."""
    await undoable.entity_create_line(0, 0, 10, 0)
    await undoable.entity_create_circle(5, 5, 2)
    await undoable.drawing_undo()  # circle gone
    await undoable.entity_create_arc(0, 0, 5, 0, 90)  # new branch

    result = await undoable.drawing_redo()

    assert result["ok"] is False, "the circle's future is no longer reachable"
    types = sorted(e.dxftype() for e in undoable._doc.modelspace())
    assert types == ["ARC", "LINE"], f"the circle must not come back: {types}"


async def test_history_is_bounded_by_the_configured_depth(monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 2)
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    try:
        for i in range(5):
            await backend.entity_create_line(0, i, 10, i)

        undone = 0
        while (await backend.drawing_undo())["ok"]:
            undone += 1
        assert undone == 2, f"depth 2 must buy exactly two steps back, got {undone}"
    finally:
        await backend.disconnect()


# ── off by default, and honest about it ─────────────────────────────────────


async def test_undo_is_off_by_default_and_refuses_with_the_setting_named(backend):
    assert config.settings.ezdxf_undo_depth == 0

    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.drawing_undo()

    assert excinfo.value.capability == "undo_history"
    assert "EZDXF_UNDO_DEPTH" in str(excinfo.value), "the refusal must name the way in"


async def test_redo_refuses_the_same_way_when_history_is_off(backend):
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.drawing_redo()
    assert excinfo.value.capability == "undo_history"


async def test_the_capability_is_declared_on_both_backends():
    from backends.com_backend import ComBackend

    ezdxf_feature = EzdxfBackend().capabilities().features["undo_history"]
    assert ezdxf_feature.supported is False
    assert "EZDXF_UNDO_DEPTH" in (ezdxf_feature.reason or "")

    # AutoCAD keeps its own undo journal, so the live backend needs no setting.
    assert ComBackend().capabilities().features["undo_history"].supported is True


async def test_switching_it_on_flips_the_capability(monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 4)
    assert EzdxfBackend().capabilities().features["undo_history"].supported is True
