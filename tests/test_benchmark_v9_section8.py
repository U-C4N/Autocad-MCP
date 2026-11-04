"""1.8-25 - the v9 matrix carries every row of plan section 8, the kill gates included.

Design spec section 9.5 lists eighteen v9 rows and section 3 says the rows
named in an item's gate are authored by 1.8-25 into ``benchmarks/tasks_v9.py``.
Five of them had no earlier item to write them: ``replay_across_restart``
(B13, with the kill *inside* a call), ``interrupted_save_recover`` (the kill
between fsync and replace), ``damaged_dxf_recover`` (B09), ``bylayer_truth``
(the rival's section 4.5) and ``space_context`` (section 4.6). Without them the
default matrix reported 100 % while its in-call kill gates did not run.

Each of the five runs here through the reference adapter on the headless
engine and must pass; the two kill tasks drive ``benchmarks/fault_inject.py``
(real ``python server.py`` children, ``Popen.kill()``, the named barriers).
"""

from __future__ import annotations

import pytest

from benchmarks.tasks_v9 import (
    HEADLESS_ONLY_V9,
    KILL_POINTS,
    NEW_TASKS_V9,
    RIVAL_GROUND,
    RIVAL_SCENARIO,
    task_by_id,
)

#: Design spec section 9.5 (plan section 8), the v9 rows, in the table's order.
SECTION_8_ROWS: tuple[str, ...] = (
    "undo_across_restart",
    "replay_across_restart",
    "atomic_batch_default",
    "history_value_lookup",
    "interrupted_save_recover",
    "damaged_dxf_recover",
    "layer_cleanup_budget",
    "call_count_dimension_part",
    "bylayer_truth",
    "dimension_lie_detect",
    "space_context",
    "parametric_revise_keeps_handles",
    "pid_routing_obstacles",
    "pid_project_offpage",
    "policy_read_only",
    "lisp_read_tier",
    "std_tables_b40",
    "dwg_headless_roundtrip",
)

ADDED_BY_1_8_25 = (
    "replay_across_restart",
    "interrupted_save_recover",
    "damaged_dxf_recover",
    "bylayer_truth",
    "space_context",
)


def test_every_section_8_row_is_in_the_v9_matrix():
    ids = [task.task_id for task in NEW_TASKS_V9]
    missing = sorted(set(SECTION_8_ROWS) - set(ids))
    assert missing == [], f"section 8 rows the default matrix does not run: {missing}"
    assert sorted(ids) == sorted(SECTION_8_ROWS), "and nothing outside section 8"
    # appended at the end, after every item's own row
    assert ids[-len(ADDED_BY_1_8_25) :] == list(ADDED_BY_1_8_25)


def test_the_in_call_kill_gates_are_kill_points_of_the_matrix():
    for task_id in ("replay_across_restart", "interrupted_save_recover"):
        points = KILL_POINTS[task_id]
        assert any(point.startswith("inside ") for point in points), points
    assert any("journal.after_begin" in p for p in KILL_POINTS["replay_across_restart"])
    assert any("save.before_replace" in p for p in KILL_POINTS["interrupted_save_recover"])


def test_the_new_rows_are_headless_and_the_rivals_ground_is_mapped():
    assert set(ADDED_BY_1_8_25) <= HEADLESS_ONLY_V9
    for task_id, section in (
        ("bylayer_truth", "§4.5"),
        ("space_context", "§4.6"),
        ("lisp_read_tier", "§4.6"),
    ):
        assert RIVAL_GROUND[task_id] == "lizaixi01-agentcad"
        assert RIVAL_SCENARIO[task_id].startswith(section)
    assert task_by_id("replay_across_restart").category == "durability"
    assert task_by_id("damaged_dxf_recover").category == "reliability"


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", ADDED_BY_1_8_25)
async def test_the_task_passes_on_the_headless_engine(task_id, tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        result = await adapter.run_task(task_by_id(task_id))
    finally:
        await adapter.cleanup()
    assert result.status == "pass", (result.message, result.metrics)


@pytest.mark.asyncio
async def test_the_kill_tasks_are_not_run_on_the_live_lane(tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="com")  # no setup: answered before any engine
    for task_id in ADDED_BY_1_8_25:
        result = await adapter.run_task(task_by_id(task_id))
        assert result.status == "not_run", task_id
