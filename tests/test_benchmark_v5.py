# tests/test_benchmark_v5.py
"""The v5 task matrix: v4 plus what tracks B and G added.

``mech_assembly`` is roadmap criterion 3 — a flange-coupling sheet built from
the tools, gated on two numbers that cannot be negotiated:
``drawing_critique(focus=None)`` returns zero issues and ``drawing_finalize``
scores at least 90. The published competitor reports were never asked it and
the chart shows them ``not_run``, the rule v3 set for its five.
"""

from __future__ import annotations

import pytest

from benchmarks.tasks_v4 import TASKS_V4
from benchmarks.tasks_v5 import DEFAULT_MATRIX, MATRICES, NEW_TASKS_V5, TASKS_V5, task_by_id


def test_v5_extends_v4_without_touching_it():
    assert TASKS_V5[: len(TASKS_V4)] == TASKS_V4
    assert [t.task_id for t in NEW_TASKS_V5] == ["mech_assembly"]
    assert len(TASKS_V5) == 18
    assert MATRICES["v4"] == TASKS_V4 and MATRICES["v5"] == TASKS_V5
    assert DEFAULT_MATRIX == "v5"
    assert task_by_id("mech_assembly").category == "mech"


def test_the_adapter_implements_every_v5_task():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    for task in TASKS_V5:
        assert hasattr(AutoCADMCPProAdapter, f"_task_{task.task_id}"), task.task_id


def test_the_suite_carries_the_mechanical_and_sheet_checks():
    """Six from spec 14, plus mech_thread_unrepresented_is_caught: a quality
    gate that never fires is not a gate, the way dim_overlap_critique_fires
    already guards the dimension focus."""
    from benchmarks.correctness_suite import CHECKS

    for name in (
        "mech_part_roundtrip",
        "mech_section_hatch_area",
        "mech_iso286_on_dimension",
        "mech_thread_unrepresented_is_caught",
        "std_part_iso4014_m12",
    ):
        assert CHECKS[name][1] == "Mechanical"
    assert CHECKS["sheet_frame_iso5457_a3"][1] == "Sheet"
    assert CHECKS["bom_balloon_link"][1] == "Sheet"
    assert len(CHECKS) == 39


@pytest.mark.asyncio
async def test_mech_assembly_passes_headlessly(tmp_path):
    """The benchmark task itself, run once here so a regression in the part
    drawer, the sheet or the critique fails the suite and not only the matrix."""
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        passed, metrics, _artifacts = await adapter._task_mech_assembly()
    finally:
        await adapter.cleanup()
    assert passed, metrics
    assert metrics["critique_issues"] == 0, metrics["critique_focuses"]
    assert metrics["score"] >= 90.0


@pytest.mark.asyncio
async def test_mech_assembly_fails_loudly_when_the_critique_finds_anything(tmp_path, monkeypatch):
    """The gate has to be able to fail. One parts-list row left without its
    balloon is enough: ISO 7573 gives every row an item reference, and the
    critique names the focus that fired rather than only shading the score."""
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from engineering.sheet import bom

    real = bom.add_balloon

    async def only_the_first(backend, *, item, **kw):
        if int(item) == 1:
            return await real(backend, item=item, **kw)
        return {"ok": True, "skipped": True}

    monkeypatch.setattr(bom, "add_balloon", only_the_first)
    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        passed, metrics, _artifacts = await adapter._task_mech_assembly()
    finally:
        await adapter.cleanup()
    assert passed is False
    assert "mech_bom_balloon_mismatch" in metrics["critique_focuses"]


@pytest.mark.asyncio
async def test_the_section_hatch_area_check_measures_the_drawing():
    """1200 mm^2 is computed by hand from the model, and read back through
    analysis_measure_entity — a number the drawing code cannot fake."""
    from benchmarks import correctness_suite

    assert await correctness_suite.mech_section_hatch_area() is True


@pytest.mark.asyncio
async def test_the_iso5457_frame_check_passes():
    from benchmarks import correctness_suite

    assert await correctness_suite.sheet_frame_iso5457_a3() is True
