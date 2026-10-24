"""W1 review follow-ups of Task 2 (complete QA scans).

Five findings the adversarial review of Task 2 measured on the pre-merge
branch, each reproduced on release/1.7.0 before it was fixed:

1. ``drawing_refine`` sent a ``check_failed`` / ``scan_incomplete`` issue - not
   a finding: no handles, no layer - to its focus's repair handler, which
   raised and rolled the whole round back, valid repairs included.
2. ``construction_left`` reads every space; ``construction_clear`` read the
   current one, 5,000 entities at most, so the remedy the finding names could
   not clear it. ``entity_delete`` of a paper-space handle crashed headlessly
   (``ValueError: list.remove(x): x not in list``).
3. The QA read computed every INSERT's box through ``virtual_entities()``
   (5,000 INSERTs of a 20-line block: 4.7 s on this machine, 9.1 s in the
   review) though the only QA consumer of INSERT boxes is the topology lane.
4. ``_close_pairs`` stored every pair within the tolerance, the coincident
   ones ``untrimmed_corner`` throws away included: 4,000 zero-length LINEs at
   the origin took 3.45 GB in the review.
5. ``MAX_QA_ENTITIES=-1`` silently disabled the ceiling.

Drawings are built straight into the ezdxf document where a tool call per
entity would take minutes; every expected handle is one the test placed.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import config
from engineering import critique
from engineering.critique import CHECK_FAILED, SCAN_INCOMPLETE, run_critique
from engineering.refiner import refine_drawing

pytestmark = pytest.mark.asyncio


async def _wait_for(event: threading.Event, timeout: float = 30.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout), "event never set"


async def _until(predicate, timeout: float = 30.0) -> None:
    """Poll a condition the worker thread sets; the deadline only fails the test."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.01)


# -- finding 1: the refiner and the issues that are not findings ------------------------


async def test_a_scan_incomplete_issue_is_manual_and_the_rounds_repairs_stand(backend, monkeypatch):
    """The review's repro: a 2-entity ceiling over 3 LINEs makes
    duplicate_entities report scan_incomplete beside a real layer_color
    finding. Before: 'repair_failed', the round rolled back, DIM still 1."""
    await backend.layer_create("DIM", color=1)
    for i in range(3):
        await backend.entity_create_line(0, i * 10, 50, i * 10, layer="GEOMETRY")
    monkeypatch.setattr(config.settings, "max_qa_entities", 2)

    result = await refine_drawing(backend, focus=["layer_color", "duplicate_entities"])

    first = result.rounds[0]
    assert first.transaction == "committed", result.to_dict()
    by_focus = {action.focus: action for action in first.actions}
    assert by_focus["layer_color"].status == "applied"
    assert by_focus["duplicate_entities"].status == "manual_required"
    assert by_focus["duplicate_entities"].detail["code"] == SCAN_INCOMPLETE
    assert [layer.color for layer in await backend.layer_list() if layer.name == "DIM"] == [2]
    # The incomplete scan is still there, so the drawing is not called clean.
    assert result.status == "manual_required"
    assert [i["detail"]["code"] for i in result.remaining_issues] == [SCAN_INCOMPLETE]


async def test_a_check_failed_issue_never_reaches_its_repair_handler(backend, monkeypatch):
    """Before: repair_iso128 read detail['layer'] of a check_failed issue and
    raised KeyError, rolling back the layer_color repair beside it."""
    await backend.layer_create("DIM", color=1)

    async def _broken(_backend):
        raise RuntimeError("the iso128 check is having a bad day")

    monkeypatch.setitem(critique._FOCUS_DISPATCH, "iso128", _broken)

    planned = await refine_drawing(backend, focus=["layer_color", "iso128"], dry_run=True)
    assert {a.focus: a.status for a in planned.rounds[0].actions} == {
        "layer_color": "planned",
        "iso128": "manual_required",
    }

    result = await refine_drawing(backend, focus=["layer_color", "iso128"])

    first = result.rounds[0]
    assert first.transaction == "committed", result.to_dict()
    by_focus = {action.focus: action for action in first.actions}
    assert by_focus["layer_color"].status == "applied"
    assert by_focus["iso128"].status == "manual_required"
    assert by_focus["iso128"].detail["code"] == CHECK_FAILED
    assert [layer.color for layer in await backend.layer_list() if layer.name == "DIM"] == [2]


# -- finding 2: construction_clear covers what construction_left reads ------------------


async def test_construction_clear_reaches_every_space_and_every_entity(backend):
    """A CONSTRUCTION LINE on Layout1 and one at position 5,001 of model space
    are both deleted; nothing on another layer is touched. Before: deleted 0
    of the paper one and the 5,001st was past the 5,000 cap."""
    await backend.layer_create("CONSTRUCTION", color=250)
    doc = backend._doc
    msp = doc.modelspace()
    paper = doc.layouts.get("Layout1")
    kept = [
        msp.add_line(
            (0.0, 10.0 * i), (100.0, 10.0 * i), dxfattribs={"layer": "GEOMETRY"}
        ).dxf.handle
        for i in range(5_000)
    ]
    late = msp.add_line((-300.0, 0.0), (-200.0, 0.0), dxfattribs={"layer": "CONSTRUCTION"})
    on_paper = paper.add_line((0.0, 0.0), (10.0, 0.0), dxfattribs={"layer": "CONSTRUCTION"})
    kept.append(
        paper.add_line((0.0, 5.0), (10.0, 5.0), dxfattribs={"layer": "GEOMETRY"}).dxf.handle
    )
    victims = {late.dxf.handle, on_paper.dxf.handle}
    assert len(msp) == 5_001 and list(msp)[5_000].dxf.handle == late.dxf.handle

    (before,) = await run_critique(backend, ["construction_left"])
    assert set(before.handles) == victims and before.detail["spaces"] == ["Layout1", "Model"]

    cleared = await backend.construction_clear()

    assert cleared["deleted"] == 2 and set(cleared["deleted_handles"]) == victims
    assert cleared["failed"] == []
    assert await run_critique(backend, ["construction_left"]) == []
    left = {e.dxf.handle for space in (msp, paper) for e in space}
    assert left == set(kept)


async def test_entity_delete_removes_a_paper_space_entity(backend):
    """Before: ValueError: list.remove(x): x not in list - it deleted from the
    current space whatever space the entity lived in."""
    paper = backend._doc.layouts.get("Layout1")
    line = paper.add_line((0.0, 0.0), (10.0, 0.0))
    handle = line.dxf.handle

    assert (await backend.entity_delete(handle))["deleted_handle"] == handle
    assert [e.dxf.handle for e in paper] == []


async def test_the_refiner_clears_a_paper_space_construction_finding(backend):
    """repair_construction_left deletes by handle; with the paper-space delete
    fixed the remedy the finding names actually clears it."""
    await backend.layer_create("CONSTRUCTION", color=250)
    backend._doc.layouts.get("Layout1").add_line(
        (0.0, 0.0), (10.0, 0.0), dxfattribs={"layer": "CONSTRUCTION"}
    )

    result = await refine_drawing(backend, focus=["construction_left"])

    assert result.status == "threshold_met", result.to_dict()
    assert await run_critique(backend, ["construction_left"]) == []


# -- finding 2, the live engine: every Layout.Block, against measured members -----------


class _Entity:
    def __init__(self, doc, handle: str, layer: str, locked: bool = False):
        self._doc, self.Handle, self.Layer, self._locked = doc, handle, layer, locked

    def Delete(self):  # noqa: N802 - the ActiveX member name
        if self._locked:
            raise RuntimeError("On locked layer")
        for block in self._doc._blocks:
            if self in block._items:
                block._items.remove(self)


class _Block:
    def __init__(self, items):
        self._items = items

    @property
    def Count(self):  # noqa: N802
        return len(self._items)

    def Item(self, index):  # noqa: N802
        return self._items[index]


class _Layout:
    def __init__(self, block):
        self.Block = block


class _Layouts:
    def __init__(self, layouts):
        self._layouts = layouts

    @property
    def Count(self):  # noqa: N802
        return len(self._layouts)

    def Item(self, index):  # noqa: N802
        return self._layouts[index]


class _Doc:
    """Only the members measured on AutoCAD 2026 (Layouts.Count / Item(i),
    Layout.Block, Block.Count / Item(j), entity Layer / Handle / Delete,
    Document.HandleToObject)."""

    def __init__(self, spaces):
        self._blocks = [_Block([]) for _ in spaces]
        for block, rows in zip(self._blocks, spaces, strict=True):
            block._items.extend(_Entity(self, *row) for row in rows)
        self.Layouts = _Layouts([_Layout(block) for block in self._blocks])

    def HandleToObject(self, handle):  # noqa: N802
        for block in self._blocks:
            for entity in block._items:
                if entity.Handle == handle:
                    return entity
        raise KeyError(handle)


async def test_the_live_walk_covers_every_layout_block_and_reports_a_locked_one():
    from backends.com_backend import _delete_on_layer_all_layouts

    doc = _Doc(
        [
            [("A1", "construction"), ("A2", "GEOMETRY")],  # Layout1 (collection order)
            [],  # Layout2
            [(f"M{i}", "GEOMETRY") for i in range(5_000)]
            + [("M5001", "CONSTRUCTION"), ("L1", "CONSTRUCTION", True)],  # Model
        ]
    )

    result = _delete_on_layer_all_layouts(doc, "CONSTRUCTION")

    assert result["deleted"] == ["A1", "M5001"]
    assert result["failed"] == [{"handle": "L1", "error": "On locked layer"}]
    left = [e.Handle for block in doc._blocks for e in block._items]
    assert left == ["A2"] + [f"M{i}" for i in range(5_000)] + ["L1"]
