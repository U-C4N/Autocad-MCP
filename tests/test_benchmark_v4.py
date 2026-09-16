"""Track A — the v4 task matrix.

v4 adds one task, ``pid_roundtrip``: draw a P&ID through the same code the
tools call and read it back through the graph builder, so the edge count, the
dangling count and the instrument index are each derived independently of the
drawing code. The published competitor reports were never asked this question
and the chart shows them ``not_run`` — the same rule v3 set for its five.
"""

from __future__ import annotations

from benchmarks.tasks_v3 import TASKS_V3
from benchmarks.tasks_v4 import DEFAULT_MATRIX, MATRICES, NEW_TASKS_V4, TASKS_V4, task_by_id


def test_v4_extends_v3_without_touching_it():
    assert TASKS_V4[: len(TASKS_V3)] == TASKS_V3
    assert [t.task_id for t in NEW_TASKS_V4] == ["pid_roundtrip"]
    assert MATRICES["v3"] == TASKS_V3 and MATRICES["v4"] == TASKS_V4 and DEFAULT_MATRIX == "v4"
    assert task_by_id("pid_roundtrip").category == "pid"


def test_the_adapter_implements_every_v4_task():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    for task in TASKS_V4:
        assert hasattr(AutoCADMCPProAdapter, f"_task_{task.task_id}"), task.task_id


def test_correctness_suite_has_the_pid_checks():
    from benchmarks.correctness_suite import CHECKS

    for name in ("pid_block_define_attdef_roundtrip", "pid_tag_parse_fic", "pid_graph_edge_count"):
        assert CHECKS[name][1] == "P&ID"
    assert len(CHECKS) == 29
