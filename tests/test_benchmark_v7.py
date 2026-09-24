"""The v7 task matrix: v6 plus what track H added.

``takeoff_roundtrip`` and ``understand_foreign`` are track H's evidence, both
gated on the synthetic plant pair (tests/fixtures/plant_pair.py) whose truth
is computed from the positions it placed. Each gate is shown to fail on a
broken input - a gate that cannot fail is not evidence. The five correctness
checks run here once too, so a regression fails the suite and not only the
A/B report.
"""

from __future__ import annotations

import ezdxf
import pytest

from benchmarks.tasks_v6 import TASKS_V6
from benchmarks.tasks_v7 import DEFAULT_MATRIX, MATRICES, NEW_TASKS_V7, TASKS_V7, task_by_id
from tests.fixtures import plant_pair

UNDERSTANDING_CHECKS = (
    "scale_check_detects_schematic",
    "pipe_takeoff_rmst_exact",
    "cable_takeoff_roundup",
    "diff_detects_known_edits",
    "topology_known_defects",
)


def test_v7_extends_v6_without_touching_it():
    assert TASKS_V7[: len(TASKS_V6)] == TASKS_V6
    assert [t.task_id for t in NEW_TASKS_V7] == ["takeoff_roundtrip", "understand_foreign"]
    assert len(TASKS_V7) == 21
    assert MATRICES["v6"] == TASKS_V6 and MATRICES["v7"] == TASKS_V7
    assert DEFAULT_MATRIX == "v7"
    assert task_by_id("takeoff_roundtrip").category == "plant"
    assert task_by_id("understand_foreign").category == "understand"


def test_the_adapter_implements_every_v7_task():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    for task in TASKS_V7:
        assert hasattr(AutoCADMCPProAdapter, f"_task_{task.task_id}"), task.task_id


def test_the_suite_carries_the_five_understanding_checks():
    from benchmarks.correctness_suite import CHECKS

    for name in UNDERSTANDING_CHECKS:
        assert CHECKS[name][1] == "Understanding"
    assert len(CHECKS) == 48


async def _run(task_id, tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        return await getattr(adapter, f"_task_{task_id}")()
    finally:
        await adapter.cleanup()


def _broken(edit):
    """build_plant_pair, then ``edit(truth)`` on the files it wrote."""
    real = plant_pair.build_plant_pair

    def build(directory):
        truth = real(directory)
        edit(truth)
        return truth

    return build


@pytest.mark.asyncio
async def test_takeoff_roundtrip_passes_headlessly(tmp_path):
    passed, metrics, _artifacts = await _run("takeoff_roundtrip", tmp_path)
    assert passed, metrics
    assert metrics["scale_verdict"] == "schematic"
    assert metrics["runs_checked"] == 9 and metrics["loads_checked"] == 4


@pytest.mark.asyncio
async def test_takeoff_roundtrip_fails_loudly_without_the_wiring_callouts(tmp_path, monkeypatch):
    """Every callout gone from the layout: no load has a panel any more, so no
    cable length can be measured - and the gate names all four loads, while the
    pipe half, untouched, still agrees."""

    def drop_callouts(truth):
        doc = ezdxf.readfile(truth["layout"])
        msp = doc.modelspace()
        for entity in list(msp.query("MULTILEADER")):
            msp.delete_entity(entity)
        doc.saveas(truth["layout"])

    monkeypatch.setattr(plant_pair, "build_plant_pair", _broken(drop_callouts))
    passed, metrics, _artifacts = await _run("takeoff_roundtrip", tmp_path)
    assert passed is False
    assert sorted(row["tag"] for row in metrics["cable_mismatch"]) == ["M11", "M12", "T102", "T202"]
    assert metrics["pipe_mismatch"] == []


@pytest.mark.asyncio
async def test_understand_foreign_passes_headlessly(tmp_path):
    passed, metrics, _artifacts = await _run("understand_foreign", tmp_path)
    assert passed, metrics
    assert metrics["clusters"] == 2 and metrics["misread_layers"] == {}


@pytest.mark.asyncio
async def test_understand_foreign_fails_loudly_when_the_units_tell_the_truth(tmp_path, monkeypatch):
    """The same layout declaring millimetres: there is no disagreement left to
    flag, so the units gate fails - and only that gate."""

    def honest_units(truth):
        doc = ezdxf.readfile(truth["layout"])
        doc.header["$INSUNITS"] = 4
        doc.saveas(truth["layout"])

    monkeypatch.setattr(plant_pair, "build_plant_pair", _broken(honest_units))
    passed, metrics, _artifacts = await _run("understand_foreign", tmp_path)
    assert passed is False
    assert metrics["units_flagged"] is False
    assert metrics["outlier_found"] and metrics["copies_separated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", UNDERSTANDING_CHECKS)
async def test_each_understanding_check_passes(name):
    from benchmarks.correctness_suite import CHECKS

    check, _category = CHECKS[name]
    assert await check() is True
