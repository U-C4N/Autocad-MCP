"""The v8 task matrix: v7 plus what 1.7.0 made provable, and the rivals' ground.

Spec §6 Task 20 (B56). Every new task runs here once, headlessly, and once on a
broken input, where its gate has to fail and say what failed - a gate that
cannot fail is not evidence (the v7 pattern). The nine verification checks of
the correctness suite (roadmap scenarios T05, T09-T13, T18, T25, T36) run here
too, so a regression fails the suite and not only the A/B report.

Rival lanes: a task a rival was never asked is ``not_run``, a lane whose
environment is absent is ``environment_blocked``, and neither is a zero in any
score. ``compare_versions.py --fail-on-regression`` exits 1 on a planted
regression and 0 otherwise.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import ezdxf
import pytest

from benchmarks.adapters.base import BenchmarkAdapter, EnvironmentBlocked, TaskResult
from benchmarks.tasks_v7 import TASKS_V7
from benchmarks.tasks_v8 import (
    DEFAULT_MATRIX,
    EVALUATOR_VERSION,
    FIXTURE_SOURCES,
    MATRICES,
    NEW_TASKS_V8,
    RIVAL_GROUND,
    TASKS_V8,
    task_by_id,
)

ROOT = Path(__file__).resolve().parents[1]

NEW_IDS = [
    "requirements_roundtrip",
    "cancel_safety",
    "qa_large_drawing",
    "export_safety",
    "stale_preview",
    "revision_guarded_edit",
    "pid_file_roundtrip",
    "foreign_edit_verified",
    "damaged_dxf_refusal",
    "dimension_binding",
]

#: The roadmap's acceptance scenarios, one correctness check each.
VERIFICATION_CHECKS = (
    "verify_t05_stale_preview_no_silent_mutation",
    "verify_t09_diameter_under_its_label",
    "verify_t10_moved_circle_against_its_model",
    "verify_t11_inner_hole_moved_caught_on_reopen",
    "verify_t12_equal_holes_ambiguous",
    "verify_t13_hole_count_cardinality",
    "verify_t18_dimension_override_and_dimlfac_visible",
    "verify_t25_handle_renumber_rebinds",
    "verify_t36_report_stale_after_file_change",
)


# -- the matrix ---------------------------------------------------------------------


def test_v8_extends_v7_without_touching_it():
    assert TASKS_V8[: len(TASKS_V7)] == TASKS_V7
    assert [t.task_id for t in NEW_TASKS_V8] == NEW_IDS
    assert len(TASKS_V8) == 31
    assert MATRICES["v7"] == TASKS_V7 and MATRICES["v8"] == TASKS_V8
    assert DEFAULT_MATRIX == "v8"
    assert task_by_id("requirements_roundtrip").category == "verify"
    assert task_by_id("pid_file_roundtrip").category == "pid"
    assert set(RIVAL_GROUND) <= set(NEW_IDS)
    assert RIVAL_GROUND["dimension_binding"] == "best-cad-mcp"


def test_the_runner_and_the_chart_read_the_current_matrix():
    from benchmarks import render_matrix_chart, run_competitors

    assert run_competitors.DEFAULT_MATRIX == "v8"
    assert run_competitors.MATRICES is MATRICES
    assert render_matrix_chart.CURRENT_TASKS == TASKS_V8
    assert "environment_blocked" in render_matrix_chart.STATUS_COLORS


def test_the_doc_number_gate_counts_the_v8_matrix():
    from scripts import check_doc_numbers

    assert check_doc_numbers.matrix_tasks() == len(TASKS_V8) == 31


def test_the_published_reference_report_covers_the_whole_current_matrix():
    # The release's reference run: every task of the current matrix, all passed,
    # with the manifest that names what it measured.
    published = ROOT / "benchmarks" / "results" / "published"
    report = json.loads((published / "autocad-mcp-pro.json").read_text(encoding="utf-8"))
    scored = {item["task_id"] for item in report["results"]}

    assert scored == {task.task_id for task in TASKS_V8}
    assert report["matrix"] == DEFAULT_MATRIX == "v8"
    assert report["summary"]["score"] == 100.0
    assert {item["status"] for item in report["results"]} == {"pass"}
    manifest = report["manifest"]
    assert manifest["evaluator_version"] == EVALUATOR_VERSION
    assert [row["path"] for row in manifest["fixtures"]] == list(FIXTURE_SOURCES)
    assert manifest["dependencies"]["ezdxf"] == ezdxf.__version__


def test_the_rival_lanes_of_v8_are_recorded_without_a_zero():
    lanes = ROOT / "benchmarks" / "results" / "published" / "lanes-v8"
    names = sorted(path.name for path in lanes.glob("*.json"))
    assert names == [
        "beiming183-autocad-mcp.json",
        "best-cad-mcp.json",
        "puran-water-autocad-mcp.json",
    ]
    for path in lanes.glob("*.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        statuses = {item["status"] for item in report["results"]}
        assert report["matrix"] == "v8"
        assert {item["task_id"] for item in report["results"]} == {t.task_id for t in TASKS_V8}
        if statuses & {"pass", "fail", "partial", "timeout", "unsupported"}:
            continue  # a lane that really ran is scored as it ran
        assert statuses <= {"not_run", "environment_blocked"}, path.name
        assert report["summary"]["score"] is None, path.name
        assert all(item["message"] for item in report["results"]), "every row says why"


def test_the_adapter_implements_every_v8_task():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    for task in TASKS_V8:
        assert hasattr(AutoCADMCPProAdapter, f"_task_{task.task_id}"), task.task_id


def test_the_suite_carries_the_nine_verification_checks():
    from benchmarks.correctness_suite import CHECKS

    for name in VERIFICATION_CHECKS:
        assert CHECKS[name][1] == "Verification"
    assert len(CHECKS) == 61


# -- the new tasks, headlessly --------------------------------------------------------


async def _run(task_id, tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        return await getattr(adapter, f"_task_{task_id}")()
    finally:
        await adapter.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", NEW_IDS)
async def test_each_new_task_passes_headlessly(task_id, tmp_path):
    passed, metrics, _artifacts = await _run(task_id, tmp_path)
    assert passed, metrics


@pytest.mark.asyncio
async def test_requirements_roundtrip_fails_exactly_the_moved_requirement(tmp_path):
    passed, metrics, artifacts = await _run("requirements_roundtrip", tmp_path)
    assert passed, metrics
    assert metrics["document"]["counts"]["pass"] == 4
    assert metrics["artifact"]["failed"] == ["R3"] == metrics["reopened"]["failed"]
    # what 1.6's delivery parity compared could not see the move
    assert metrics["parity_blind"] is True
    # sqrt(48^2 + 25^2): the bore 3 mm right of (60, 40), measured from (15, 15)
    assert abs(metrics["artifact"]["measured"]["R3"] - 2929**0.5) < 1e-9
    assert all(Path(p).is_file() for p in artifacts)


@pytest.mark.asyncio
async def test_requirements_roundtrip_fails_loudly_when_the_file_is_never_read(
    tmp_path, monkeypatch
):
    """The 1.6 shape of the bug: 'verified' on the in-memory document, never the
    saved file - the moved bore is invisible and the gate names the artifact."""
    from engineering.verification import artifact
    from engineering.verification.report import verify_requirements

    async def from_memory(path, rs, *, backend=None, binding_policy="strict"):
        return await verify_requirements(backend, rs, binding_policy=binding_policy)

    monkeypatch.setattr(artifact, "verify_artifact", from_memory)
    passed, metrics, _artifacts = await _run("requirements_roundtrip", tmp_path)
    assert passed is False
    assert metrics["artifact"]["failed"] == []
    assert metrics["reopened"]["failed"] == ["R3"], "the honest reopen still sees it"


@pytest.mark.asyncio
async def test_cancel_safety_fails_loudly_when_a_cancel_releases_the_drawing(tmp_path, monkeypatch):
    from backends.ezdxf_backend import EzdxfBackend

    async def releases_on_cancel(
        self, fn, *args, quarantine_exit=False, readonly=False, detached_read_exit=False, **kwargs
    ):
        # 1.6's shape: the awaiting task owns the drawing, so a cancel frees it
        # while the worker thread still writes
        return await asyncio.to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(EzdxfBackend, "_async", releases_on_cancel)
    passed, metrics, _artifacts = await _run("cancel_safety", tmp_path)
    assert passed is False
    assert metrics["overlapped"] is True


@pytest.mark.asyncio
async def test_qa_large_drawing_fails_loudly_when_the_scan_stops_at_5000(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "max_qa_entities", 5_000)
    passed, metrics, _artifacts = await _run("qa_large_drawing", tmp_path)
    assert passed is False
    assert "scan_incomplete" in metrics["incomplete"]
    assert metrics["missing"], metrics


@pytest.mark.asyncio
async def test_export_safety_fails_loudly_without_the_csv_prefix(tmp_path, monkeypatch):
    from engineering.export import cells

    monkeypatch.setattr(cells, "csv_text", lambda value: "" if value is None else str(value))
    passed, metrics, _artifacts = await _run("export_safety", tmp_path)
    assert passed is False
    assert metrics["formula_cells"], metrics
    assert {row["file"] for row in metrics["formula_cells"]} >= {"lines.csv", "bom.csv"}
    assert metrics["bom_xlsx"]["ok"] is True, "the XLSX writer never relied on the prefix"


@pytest.mark.asyncio
async def test_stale_preview_fails_loudly_when_the_fingerprint_is_blind(tmp_path, monkeypatch):
    from engineering.changes import preview

    monkeypatch.setattr(preview, "_digests", lambda doc, handles: dict.fromkeys(handles, "0" * 64))
    passed, metrics, _artifacts = await _run("stale_preview", tmp_path)
    assert passed is False
    assert metrics["refused_kind"] is None
    assert metrics["moved_over_a_stale_preview"] is True


@pytest.mark.asyncio
async def test_revision_guarded_edit_fails_loudly_when_the_revision_never_moves(
    tmp_path, monkeypatch
):
    from backends.ezdxf_backend import EzdxfBackend

    real = EzdxfBackend.document_identity

    def frozen(self):
        return {**real(self), "revision": 0}

    monkeypatch.setattr(EzdxfBackend, "document_identity", frozen)
    passed, metrics, _artifacts = await _run("revision_guarded_edit", tmp_path)
    assert passed is False
    assert metrics["stale_refused"] is False


@pytest.mark.asyncio
async def test_pid_file_roundtrip_fails_loudly_when_the_save_drops_a_line(tmp_path, monkeypatch):
    from backends.ezdxf_backend import EzdxfBackend

    real = EzdxfBackend.drawing_save_as

    async def lossy(self, path, *args, **kwargs):
        result = await real(self, path, *args, **kwargs)
        doc = ezdxf.readfile(path)
        msp = doc.modelspace()
        first = next(
            e
            for e in msp
            if e.dxftype() == "LWPOLYLINE" and e.dxf.layer.upper() == "PROCESS-PIPING-MAIN"
        )
        msp.delete_entity(first)
        doc.saveas(path)
        return result

    monkeypatch.setattr(EzdxfBackend, "drawing_save_as", lossy)
    passed, metrics, _artifacts = await _run("pid_file_roundtrip", tmp_path)
    assert passed is False
    assert metrics["edges_equal"] is False or metrics["line_numbers_equal"] is False


@pytest.mark.asyncio
async def test_foreign_edit_verified_fails_loudly_when_the_edit_is_a_no_op(tmp_path, monkeypatch):
    from backends.ezdxf_backend import EzdxfBackend

    async def no_op(self, handle, **kwargs):
        return await self.entity_get(handle)

    monkeypatch.setattr(EzdxfBackend, "entity_edit_geometry", no_op)
    passed, metrics, _artifacts = await _run("foreign_edit_verified", tmp_path)
    assert passed is False
    assert metrics["before"]["failed"] == ["H3"]
    assert metrics["after"]["failed"] == ["H3"], "the saved file still holds the Ø9 hole"


@pytest.mark.asyncio
async def test_damaged_dxf_refusal_names_the_fault_and_not_the_machine(tmp_path):
    passed, metrics, _artifacts = await _run("damaged_dxf_refusal", tmp_path)
    assert passed, metrics
    assert "ENDSEC" in metrics["cases"]["truncated"]["error"]
    assert "not_a_drawing.dxf" in metrics["cases"]["not_dxf"]["error"]
    assert str(tmp_path) not in json.dumps(metrics), "a published report carries no machine layout"


@pytest.mark.asyncio
async def test_damaged_dxf_refusal_fails_loudly_on_a_silent_empty_document(tmp_path, monkeypatch):
    """The failure 1.7.0 must not have: a damaged file 'opened' as an empty drawing."""
    from backends.ezdxf_backend import EzdxfBackend

    async def silent(self, path, *args, **kwargs):
        await self.drawing_new()
        return {"ok": True, "path": str(path)}

    monkeypatch.setattr(EzdxfBackend, "drawing_open", silent)
    passed, metrics, _artifacts = await _run("damaged_dxf_refusal", tmp_path)
    assert passed is False
    assert metrics["cases"]["truncated"]["refused"] is False
    assert metrics["document_kept"] is False


@pytest.mark.asyncio
async def test_dimension_binding_fails_loudly_when_the_override_is_believed(tmp_path, monkeypatch):
    from engineering.verification import binding

    def believe_the_geometry(override, measurement, dimlfac):
        return ("<>", measurement)

    monkeypatch.setattr(binding, "_displayed", believe_the_geometry)
    passed, metrics, _artifacts = await _run("dimension_binding", tmp_path)
    assert passed is False
    assert metrics["findings"] == {}


@pytest.mark.asyncio
async def test_a_live_lane_reports_the_headless_only_tasks_as_not_run():
    from benchmarks.adapters.autocad_mcp_pro import HEADLESS_ONLY, AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="com")
    for task_id in sorted(HEADLESS_ONLY):
        result = await adapter.run_task(task_by_id(task_id))
        assert result.status == "not_run", task_id
        assert "headless" in result.message


# -- the correctness checks -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", VERIFICATION_CHECKS)
async def test_each_verification_check_passes(name):
    from benchmarks.correctness_suite import CHECKS

    check, _category = CHECKS[name]
    assert await check() is True


# -- rival lanes and the score ------------------------------------------------------------


class _Fixed(BenchmarkAdapter):
    """An adapter that returns the statuses it is given, in order."""

    name = "fixed"

    def __init__(self, statuses):
        self.statuses = list(statuses)

    async def setup(self, artifact_dir):
        pass

    async def run_task(self, task):
        return TaskResult(task.task_id, self.statuses.pop(0), 100.0, "given")

    async def cleanup(self):
        pass


class _Blocked(_Fixed):
    name = "blocked"

    async def setup(self, artifact_dir):
        raise EnvironmentBlocked("no checkout of the competitor and fetching is off")


@pytest.mark.asyncio
async def test_a_task_not_run_is_never_a_zero_in_the_score(tmp_path):
    from benchmarks.run_competitors import run_tasks

    tasks = list(TASKS_V8[:3])
    report = await run_tasks(_Fixed(["pass", "pass", "not_run"]), tasks, tmp_path)
    summary = report["summary"]
    assert summary["score"] == 100.0, "two passes of two attempted, not 66.67"
    assert summary["attempted"] == 2 and summary["not_run"] == 1
    assert report["results"][2]["status"] == "not_run"


@pytest.mark.asyncio
async def test_a_lane_without_its_environment_is_environment_blocked(tmp_path):
    from benchmarks.run_competitors import run_tasks

    report = await run_tasks(_Blocked([]), list(TASKS_V8), tmp_path, matrix="v8")
    assert {item["status"] for item in report["results"]} == {"environment_blocked"}
    assert all("fetching is off" in item["message"] for item in report["results"])
    assert report["summary"]["score"] is None
    assert report["summary"]["coverage_percent"] is None
    assert report["summary"]["environment_blocked"] == len(TASKS_V8)
    assert report["summary"]["attempted"] == 0


@pytest.mark.asyncio
async def test_a_rival_with_no_checkout_and_no_fetch_is_blocked_not_cloned(tmp_path):
    from benchmarks.adapters.puran_water import PuranWaterAdapter
    from benchmarks.run_competitors import run_tasks

    adapter = PuranWaterAdapter(backend="ezdxf")
    adapter.fetch = False
    adapter.cache_root = tmp_path / "cache"
    report = await run_tasks(adapter, list(TASKS_V8[:2]), tmp_path / "out", matrix="v8")
    assert {item["status"] for item in report["results"]} == {"environment_blocked"}
    message = report["results"][0]["message"]
    assert "puran-water-autocad-mcp" in message and "no checkout" in message
    assert str(tmp_path) not in message, "a published report carries no machine layout"
    assert not (tmp_path / "cache").exists(), "nothing was cloned"


@pytest.mark.asyncio
async def test_a_rival_never_asked_a_later_task_reports_not_run(tmp_path):
    from benchmarks.adapters.puran_water import PuranWaterAdapter

    adapter = PuranWaterAdapter(backend="ezdxf")
    later = await adapter.run_task(task_by_id("revision_guarded_edit"))
    assert later.status == "not_run"
    assert "never asked" in later.message
    # a v2 task without a playbook keeps the v1.4 verdict: no documented equivalent
    v2 = await adapter.run_task(task_by_id("preflight"))
    assert v2.status == "unsupported"


def test_a_registry_entry_without_an_adapter_is_a_not_run_lane():
    from benchmarks.run_competitors import _load_registry, not_run_report

    registry = _load_registry()
    entry = registry["best-cad-mcp"]
    assert entry["adapter"] is None
    report = not_run_report(entry, list(TASKS_V8), matrix="v8")
    assert {item["status"] for item in report["results"]} == {"not_run"}
    assert all("COM-only" in item["message"] for item in report["results"])
    assert report["summary"]["score"] is None
    assert report["adapter"] == "best-cad-mcp"


@pytest.mark.asyncio
async def test_the_report_carries_a_run_manifest(tmp_path):
    from benchmarks.run_competitors import run_tasks

    report = await run_tasks(_Fixed(["pass"]), list(TASKS_V8[:1]), tmp_path)
    manifest = report["manifest"]
    assert manifest["evaluator_version"] == EVALUATOR_VERSION
    assert manifest["server_sha"] == report["git_sha"]
    assert isinstance(manifest["dirty"], (bool, type(None)))
    for row, relative in zip(manifest["fixtures"], FIXTURE_SOURCES, strict=True):
        assert row["path"] == relative
        expected = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert row["sha256"] == expected
    assert manifest["dependencies"]["ezdxf"] == ezdxf.__version__
    assert "fastmcp" in manifest["dependencies"]


# -- compare_versions --fail-on-regression ---------------------------------------------------


def _plant(monkeypatch, outcomes):
    """compare_versions with the checks, the worktree and each run replaced:
    ``outcomes`` maps a check name to its (old, new) result."""
    from benchmarks import compare_versions

    monkeypatch.setattr(
        compare_versions, "list_checks", lambda: [(name, "Core") for name in outcomes]
    )
    monkeypatch.setattr(compare_versions, "baseline_worktree", _no_worktree)

    def run_one(repo, suite, name):
        old, new = outcomes[name]
        return new if repo == compare_versions.REPO else old

    monkeypatch.setattr(compare_versions, "run_one", run_one)
    return compare_versions


class _no_worktree:
    def __init__(self, ref):
        self.ref = ref

    def __enter__(self):
        return "<baseline>"

    def __exit__(self, *exc):
        return False


def test_fail_on_regression_exits_1_on_a_planted_regression(monkeypatch, tmp_path, capsys):
    compare_versions = _plant(monkeypatch, {"core_a": ("pass", "pass"), "core_b": ("pass", "fail")})
    out = tmp_path / "ab.json"
    assert compare_versions.main(["v1.6.0", "--fail-on-regression", "--json", str(out)]) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["regressed"] == 1
    assert "!!REGRESS" in capsys.readouterr().out
    # without the flag the same run reports and exits 0, as it always did
    assert compare_versions.main(["v1.6.0"]) == 0


def test_fail_on_regression_exits_0_when_nothing_regressed(monkeypatch):
    compare_versions = _plant(monkeypatch, {"core_a": ("pass", "pass"), "core_b": ("fail", "pass")})
    assert compare_versions.main(["v1.6.0", "--fail-on-regression"]) == 0


def test_a_crash_or_timeout_on_the_new_side_is_a_regression(monkeypatch):
    compare_versions = _plant(
        monkeypatch, {"core_a": ("pass", "timeout"), "core_b": ("pass", "crash")}
    )
    assert compare_versions.main(["v1.6.0", "--fail-on-regression"]) == 1


def test_the_published_ab_report_is_over_the_whole_suite_with_nothing_regressed():
    from benchmarks.correctness_suite import CHECKS
    from scripts.check_doc_numbers import AB_REPORT

    report = json.loads((ROOT / AB_REPORT).read_text(encoding="utf-8"))
    rows = {row["check"]: row for row in report["rows"]}
    assert report["summary"]["baseline_ref"] == "v1.6.0"
    assert set(rows) == set(CHECKS)
    assert report["summary"]["regressed"] == 0
    for name in VERIFICATION_CHECKS:
        assert rows[name]["new"] == "pass", name
        assert rows[name]["old"] != "pass", f"{name} passed on 1.6.0: it proves nothing new"
