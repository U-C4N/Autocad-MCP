# tests/test_benchmark_v4.py
"""The v4 task matrix: v3 plus what 1.6 added.

Track A added ``pid_roundtrip``; track E adds ``page_setup_truth`` — apply an
ISO A3 landscape page setup, plot through ``batch_plot`` and read the sheet
size back from the PDF's own ``/MediaBox``. The published competitor reports
were never asked either question and the chart shows them ``not_run`` — the
same rule v3 set for its five.
"""

from __future__ import annotations

import pytest

from benchmarks.tasks_v3 import TASKS_V3
from benchmarks.tasks_v4 import DEFAULT_MATRIX, MATRICES, NEW_TASKS_V4, TASKS_V4, task_by_id


def test_v4_extends_v3_without_touching_it():
    assert TASKS_V4[: len(TASKS_V3)] == TASKS_V3
    assert [t.task_id for t in NEW_TASKS_V4] == ["pid_roundtrip", "page_setup_truth"]
    assert len(TASKS_V4) == 17
    assert MATRICES["v3"] == TASKS_V3 and MATRICES["v4"] == TASKS_V4 and DEFAULT_MATRIX == "v4"
    assert task_by_id("pid_roundtrip").category == "pid"
    assert task_by_id("page_setup_truth").category == "pagesetup"


def test_the_adapter_implements_every_v4_task():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    for task in TASKS_V4:
        assert hasattr(AutoCADMCPProAdapter, f"_task_{task.task_id}"), task.task_id


def test_correctness_suite_has_the_pid_checks():
    from benchmarks.correctness_suite import CHECKS

    for name in ("pid_block_define_attdef_roundtrip", "pid_tag_parse_fic", "pid_graph_edge_count"):
        assert CHECKS[name][1] == "P&ID"


def test_correctness_suite_has_the_settings_checks():
    from benchmarks.correctness_suite import CHECKS

    for name in (
        "settings_dimstyle_iso25_values",
        "settings_layer_state_roundtrip",
        "settings_pdf_mediabox_a3",
    ):
        assert CHECKS[name][1] == "Settings"
    assert len(CHECKS) == 32


@pytest.mark.asyncio  # asyncio_mode is strict; the four tests above are sync
async def test_page_setup_truth_passes_headlessly(tmp_path):
    """The benchmark task itself, run once here so a regression in page setup
    or the PDF read-back fails the suite and not only the matrix."""
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        passed, metrics, _artifacts = await adapter._task_page_setup_truth()
    finally:
        await adapter.cleanup()
    assert passed, metrics
    assert abs(metrics["mediabox_mm"][0] - 420.0) < 0.5
    assert abs(metrics["mediabox_mm"][1] - 297.0) < 0.5
