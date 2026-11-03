"""The two v9 call-count tasks (1.8-12 / 1.8-13): registered, and passing headlessly.

``layer_cleanup_budget`` and ``call_count_dimension_part`` run the least-call
oracles of ``benchmarks/call_budget.py`` through the real server on the
adapter's headless engine; the live lane reports them ``not_run``, never a
zero.
"""

from __future__ import annotations

import pytest

from benchmarks.adapters.autocad_mcp_pro import HEADLESS_ONLY, AutoCADMCPProAdapter
from benchmarks.tasks_v9 import DEFAULT_MATRIX, MATRICES, NEW_TASKS_V9, task_by_id

ECONOMY = ("layer_cleanup_budget", "call_count_dimension_part")


def test_the_tasks_are_in_v9_and_the_default_matrix_is_unchanged():
    ids = [task.task_id for task in MATRICES["v9"]]
    assert set(ECONOMY) <= {task.task_id for task in NEW_TASKS_V9}
    assert set(ECONOMY) <= set(ids)
    assert not set(ECONOMY) & {task.task_id for task in MATRICES["v8"]}
    assert DEFAULT_MATRIX == "v8", "1.8-25 moves the default"
    assert set(ECONOMY) <= HEADLESS_ONLY
    for task_id in ECONOMY:
        assert task_by_id(task_id, "v9").category == "ergonomics"


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", ECONOMY)
async def test_each_task_passes_headlessly_within_its_ceiling(task_id, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        result = await adapter.run_task(task_by_id(task_id, "v9"))
    finally:
        await adapter.cleanup()
    assert result.status == "pass", result.metrics
    assert result.metrics["calls"] <= result.metrics["ceiling"]


@pytest.mark.asyncio
async def test_the_live_lane_reports_them_not_run():
    adapter = AutoCADMCPProAdapter(backend="com")
    for task_id in ECONOMY:
        result = await adapter.run_task(task_by_id(task_id, "v9"))
        assert result.status == "not_run"
