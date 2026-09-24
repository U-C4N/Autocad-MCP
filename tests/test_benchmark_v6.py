"""The v6 task matrix: v5 plus what track F added.

``arch_roundtrip`` is track F's evidence - a two-room plan drawn from one
spec, read back off the drawing by the room reader that never sees the spec,
and gated on the numbers the design spec fixed in advance: every room's
detected area within 0.1 % of its label's and of the hand-computed net floor,
the schedules listing the drawn tags, ``drawing_critique(focus=None)`` at
zero and the finalize score at least 90. The published competitor reports
were never asked it and the chart shows them ``not_run``.
"""

from __future__ import annotations

import pytest

from benchmarks.tasks_v5 import TASKS_V5
from benchmarks.tasks_v6 import DEFAULT_MATRIX, MATRICES, NEW_TASKS_V6, TASKS_V6, task_by_id


def test_v6_extends_v5_without_touching_it():
    assert TASKS_V6[: len(TASKS_V5)] == TASKS_V5
    assert [t.task_id for t in NEW_TASKS_V6] == ["arch_roundtrip"]
    assert len(TASKS_V6) == 19
    assert MATRICES["v5"] == TASKS_V5 and MATRICES["v6"] == TASKS_V6
    assert DEFAULT_MATRIX == "v6"
    assert task_by_id("arch_roundtrip").category == "arch"


def test_the_adapter_implements_every_v6_task():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    for task in TASKS_V6:
        assert hasattr(AutoCADMCPProAdapter, f"_task_{task.task_id}"), task.task_id


def test_the_hand_areas_are_the_net_floor_not_the_axis_area():
    from benchmarks.adapters.autocad_mcp_pro import ROUNDTRIP_AREAS_MM2

    assert ROUNDTRIP_AREAS_MM2 == {"01": 27_743_750.0, "02": 21_993_750.0}
    # the axis areas the rooms would report if the wall thickness were ignored
    assert 5000.0 * 6000.0 not in ROUNDTRIP_AREAS_MM2.values()
    assert 4000.0 * 6000.0 not in ROUNDTRIP_AREAS_MM2.values()


def test_the_suite_carries_the_four_architectural_checks():
    from benchmarks.correctness_suite import CHECKS

    for name in (
        "arch_junction_l_t_x",
        "arch_room_area_net",
        "arch_opening_cuts_wall",
        "arch_rooms_detect_foreign",
    ):
        assert CHECKS[name][1] == "Architecture"
    assert len(CHECKS) == 48


@pytest.mark.asyncio
async def test_arch_roundtrip_passes_headlessly(tmp_path):
    """The benchmark task itself, run once here so a regression in the wall
    engine, the room reader, the schedules or the critique fails the suite and
    not only the matrix."""
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        passed, metrics, _artifacts = await adapter._task_arch_roundtrip()
    finally:
        await adapter.cleanup()
    assert passed, metrics
    assert metrics["critique_issues"] == 0, metrics["critique_focuses"]
    assert metrics["score"] >= 90.0
    assert metrics["door_tags"] == ["D1", "D2"] and metrics["window_tags"] == ["W1", "W2"]
    for number in ("01", "02"):
        assert metrics["rooms"][number]["agrees"], metrics["rooms"]


@pytest.mark.asyncio
async def test_arch_roundtrip_fails_loudly_when_a_room_label_is_removed(tmp_path, monkeypatch):
    """The gate has to be able to fail. One room left without its label is
    enough: the reader still finds the face, the critique names
    arch_room_unlabelled, and the room is missing from the area comparison."""
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from engineering.arch import rooms

    real = rooms.label_room

    async def skip_the_hall(backend, room_dict, **kw):
        if room_dict.get("number") == "02":
            return {"ok": True, "skipped": True}
        return await real(backend, room_dict, **kw)

    monkeypatch.setattr(rooms, "label_room", skip_the_hall)
    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        passed, metrics, _artifacts = await adapter._task_arch_roundtrip()
    finally:
        await adapter.cleanup()
    assert passed is False
    assert "arch_room_unlabelled" in metrics["critique_focuses"]
    assert "02" not in metrics["rooms"]


@pytest.mark.asyncio
async def test_the_net_area_check_measures_the_label():
    from benchmarks import correctness_suite

    assert await correctness_suite.arch_room_area_net() is True


@pytest.mark.asyncio
async def test_the_foreign_plan_check_passes():
    from benchmarks import correctness_suite

    assert await correctness_suite.arch_rooms_detect_foreign() is True
