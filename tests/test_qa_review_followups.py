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
from engineering.critique import CHECK_FAILED, SCAN_INCOMPLETE
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
