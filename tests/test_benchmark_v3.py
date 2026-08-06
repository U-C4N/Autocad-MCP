"""M9 — the v3 task matrix.

v2 returned 10/10 for this server. That was not a good sign: every task in it
tested something the server was designed around, so the score could only ever
have been full marks. The five tasks added here were chosen because they can
fail, and three of them did fail while being written — a hatch could not be
measured at all, and the boundary tools disagreed with the measurement tool
about the same shape.

Two assertions in this file exist purely to keep the matrix honest rather than
to test behaviour:

* the plan's ``tool_discovery_bilingual`` and ``measurement_massprops`` must
  **not** appear, because neither capability shipped; and
* the published competitor reports must not carry the new task ids, because
  those servers were never run against them and a zero we invented would read
  exactly like a zero we measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backends.base import UnsupportedCapabilityError
from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
from benchmarks.adapters.base import BenchmarkAdapter
from benchmarks.run_competitors import run_tasks
from benchmarks.tasks_v2 import TASKS_V2
from benchmarks.tasks_v3 import MATRICES, NEW_TASKS_V3, TASKS_V3, task_by_id

PUBLISHED = Path(__file__).resolve().parents[1] / "benchmarks" / "results" / "published"


# ── shape of the matrix ─────────────────────────────────────────────────────


def test_v3_extends_v2_without_disturbing_it():
    """A renumbered or reweighted v2 task would silently invalidate the
    published v1.4 reports, which are keyed by task id."""
    assert TASKS_V3[: len(TASKS_V2)] == TASKS_V2
    assert len(NEW_TASKS_V3) == 5
    assert len(TASKS_V3) == 15


def test_every_task_id_is_unique():
    ids = [task.task_id for task in TASKS_V3]
    assert len(ids) == len(set(ids))


def test_the_two_capabilities_that_did_not_ship_are_not_advertised_as_tasks():
    ids = {task.task_id for task in TASKS_V3}

    assert "tool_discovery_bilingual" not in ids, (
        "the Turkish normalizer was built and removed; this repo is English "
        "throughout, so a bilingual task would be a benchmark for nothing"
    )
    assert "measurement_massprops" not in ids, (
        "no centroid or moment-of-inertia tool ships — F2 was cut to entity_measure"
    )
    assert "tool_discovery" in ids
    assert "measure_from_handle" in ids


def test_task_by_id_can_still_address_the_v2_matrix():
    assert task_by_id("core_geometry", "v2").category == "creation"
    with pytest.raises(KeyError):
        task_by_id("hatch_islands", "v2")


def test_matrices_registry_exposes_both_sets():
    assert MATRICES["v2"] == TASKS_V2
    assert MATRICES["v3"] == TASKS_V3


# ── the reference adapter actually answers them ─────────────────────────────


def test_the_reference_adapter_implements_every_v3_task():
    """An unimplemented task scores `unsupported` and is easy to not notice."""
    missing = [
        task.task_id
        for task in TASKS_V3
        if not hasattr(AutoCADMCPProAdapter, f"_task_{task.task_id}")
    ]
    assert missing == []


@pytest.mark.asyncio
@pytest.mark.parametrize("task", NEW_TASKS_V3, ids=lambda t: t.task_id)
async def test_each_new_task_passes_on_the_headless_backend(task, tmp_path):
    report = await run_tasks(AutoCADMCPProAdapter(), [task], tmp_path, timeout=120.0)

    result = report["results"][0]
    assert result["status"] == "pass", result["message"]
    assert result["metrics"], "a task that proves nothing reports no evidence"


@pytest.mark.asyncio
async def test_the_full_v3_matrix_runs_green(tmp_path):
    report = await run_tasks(AutoCADMCPProAdapter(), TASKS_V3, tmp_path, timeout=180.0)

    assert report["summary"]["attempted"] == 15
    assert report["summary"]["passed"] == 15, [
        (item["task_id"], item["status"], item["message"]) for item in report["results"]
    ]
    assert report["matrix"] == "v3"


# ── a capability refusal is not a failure ───────────────────────────────────


class RefusingAdapter(BenchmarkAdapter):
    name = "refusing"

    async def setup(self, artifact_dir):
        return None

    async def run_task(self, task):
        raise UnsupportedCapabilityError("chspace", "no change-space member on this engine")

    async def cleanup(self):
        return None


@pytest.mark.asyncio
async def test_a_capability_refusal_is_reported_unsupported_not_failed(tmp_path):
    """`fail` and `unsupported` are different claims about a competitor.

    "Your server got this wrong" and "your engine cannot reach this" are not
    the same finding, and the runner used to collapse the second into the
    first because a refusal is still an exception.
    """
    report = await run_tasks(RefusingAdapter(), [NEW_TASKS_V3[0]], tmp_path)

    result = report["results"][0]
    assert result["status"] == "unsupported"
    assert "chspace" in result["message"]
    assert report["summary"]["unsupported"] == 1
    assert report["summary"]["score"] == 0.0


# ── nothing was invented for the competitors ────────────────────────────────


@pytest.mark.parametrize(
    "filename", ["beiming183-autocad-mcp.json", "puran-water-autocad-mcp.json"]
)
def test_competitor_reports_carry_no_result_for_the_new_tasks(filename):
    report = json.loads((PUBLISHED / filename).read_text(encoding="utf-8"))
    scored = {item["task_id"] for item in report["results"]}
    new_ids = {task.task_id for task in NEW_TASKS_V3}

    assert scored & new_ids == set(), (
        "these servers were pinned and run against the v2 matrix at v1.4; a "
        "result here would be fabricated"
    )
    assert report.get("matrix", "v2") == "v2"


def test_the_published_reference_report_covers_the_whole_v3_matrix():
    report = json.loads((PUBLISHED / "autocad-mcp-pro.json").read_text(encoding="utf-8"))
    scored = {item["task_id"] for item in report["results"]}

    assert scored == {task.task_id for task in TASKS_V3}
    assert report["matrix"] == "v3"
    assert report["summary"]["score"] == 100.0
