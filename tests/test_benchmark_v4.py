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
    # The stage that makes the task falsifiable: the sheet was ANSI B first.
    assert abs(metrics["mediabox_mm_before"][0] - 432.0) < 0.5
    assert abs(metrics["mediabox_mm_before"][1] - 279.0) < 0.5
    assert metrics["size_mm_changed"] == [[432.0, 279.0], [420.0, 297.0]]


async def _noop_page_setup_apply(self, layout, setup):
    """A setter that writes nothing and claims success."""
    return {
        "ok": True,
        "layout": layout,
        "applied": dict(setup),
        "changed": {},
        "warnings": [],
        "plot_style_known": True,
        "viewports_kept": True,
        "backend": "ezdxf",
    }


@pytest.mark.asyncio
async def test_page_setup_truth_fails_when_the_setter_writes_nothing(tmp_path, monkeypatch):
    """A fresh Layout1 is already ISO A3 landscape (ezdxf's R2018 default: 420 x 297,
    rotation 0), so a task that only reads A3 back from the PDF cannot tell a
    working setter from a stub. Both gate rows must fail with the stub."""
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    from backends.ezdxf_backend import EzdxfBackend
    from benchmarks import correctness_suite
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    monkeypatch.setattr(EzdxfBackend, "page_setup_apply", _noop_page_setup_apply)

    assert await correctness_suite.settings_pdf_mediabox_a3() is False

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        passed, metrics, _artifacts = await adapter._task_page_setup_truth()
    finally:
        await adapter.cleanup()
    assert passed is False, metrics
    # The stub's own evidence: the "ANSI B" PDF is still the default A3 sheet.
    assert abs(metrics["mediabox_mm_before"][0] - 420.0) < 0.5
    assert metrics["size_mm_changed"] is None


@pytest.mark.asyncio
async def test_correctness_check_settings_pdf_mediabox_a3_passes():
    """The A/B gate row itself, run once here: ANSI B plotted at 432 x 279,
    then ISO A3 at 420 x 297, each read from its own PDF."""
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    from benchmarks import correctness_suite

    assert await correctness_suite.settings_pdf_mediabox_a3() is True
