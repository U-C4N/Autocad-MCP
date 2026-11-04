"""1.8.0 W1: the six correctness checks the A/B gate adds for the W1 items.

``benchmarks/correctness_suite.py`` is executed against the v1.7.0 tree too
(``compare_versions.py``), so these tests hold, besides each check passing
here: the suite's module-level imports are the standard library only (a new
module imported at the top would break every check on the baseline, not fail
the new ones), a check that drives the server finds ``server.py`` in the tree
under test (the working directory), and a check can fail - a write straight
into the target, which is what 1.7.0 did, does not pass the atomic-save check.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "benchmarks" / "correctness_suite.py"

#: name -> category, plan 1.8.0 W1 integration.
W1_CHECKS = {
    "lineweight_truth": "Entities",
    "error_envelope_routes_agree": "Reliability",
    "journal_replay_after_kill": "Durability",
    "atomic_save_untouched_on_failure": "Durability",
    "lineage_revision_continues": "Durability",
    "region_render_applied": "Render",
}

#: What the suite may import at module level: present on every baseline tree.
_STDLIB_TOP_LEVEL = {
    "__future__",
    "asyncio",
    "atexit",
    "contextlib",
    "math",
    "os",
    "shutil",
    "sys",
    "tempfile",
}


def test_the_suite_carries_the_six_w1_checks():
    from benchmarks.correctness_suite import CHECKS

    for name, category in W1_CHECKS.items():
        assert CHECKS[name][1] == category, name
    # Appended in one block, nothing reordered; W2 items append theirs after it.
    names = list(CHECKS)
    start = names.index(next(iter(W1_CHECKS)))
    assert names[start : start + len(W1_CHECKS)] == list(W1_CHECKS), "appended, nothing reordered"
    assert start == 61, "the W1 block follows the 61 checks of 1.7.0"
    assert len(CHECKS) == 78


def test_the_published_ab_report_shows_the_six_w1_checks_fixed():
    """The 1.8.0 A/B report: every W1 check fails or misses on v1.7.0 and passes now."""
    from scripts.check_doc_numbers import AB_REPORT

    report = json.loads((ROOT / AB_REPORT).read_text(encoding="utf-8"))
    rows = {row["check"]: row for row in report["rows"]}
    assert report["summary"]["baseline_ref"] == "v1.7.0"
    assert report["summary"]["regressed"] == 0
    assert report["summary"]["fixed"] >= len(W1_CHECKS)
    for name in W1_CHECKS:
        assert rows[name]["new"] == "pass", name
        assert rows[name]["old"] != "pass", f"{name} passed on v1.7.0: it proves nothing new"


def test_the_suite_imports_only_the_standard_library_at_module_level():
    tree = ast.parse(SUITE.read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= _STDLIB_TOP_LEVEL, imported - _STDLIB_TOP_LEVEL


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(W1_CHECKS))
async def test_each_w1_check_passes(name, monkeypatch):
    import server
    from benchmarks.correctness_suite import CHECKS

    monkeypatch.chdir(ROOT)  # the journal check runs the server.py of the tree under test
    state_dir = os.environ.get("ACADMCP_STATE_DIR")
    factory = server._make_backend
    check, _category = CHECKS[name]
    assert await check() is True
    assert os.environ.get("ACADMCP_STATE_DIR") == state_dir, "the check restored the environment"
    assert server._make_backend is factory, "the check restored the backend factory"


@pytest.mark.asyncio
async def test_the_journal_check_finds_the_server_in_the_working_directory(tmp_path, monkeypatch):
    from benchmarks import correctness_suite

    monkeypatch.chdir(tmp_path)  # no server.py here: the check fails, it never borrows one
    assert await correctness_suite.journal_replay_after_kill() is False


@pytest.mark.asyncio
async def test_a_save_written_straight_into_the_target_fails_the_atomic_check(monkeypatch):
    """1.7.0's save: ``doc.saveas(target)`` - the held target is rewritten."""
    from backends import ezdxf_backend as eb
    from backends.atomic_write import file_sha256
    from benchmarks import correctness_suite

    def straight_into(doc, target, *args, **kwargs):
        doc.saveas(target)
        return {
            "path": str(target),
            "sha256": file_sha256(target),
            "bytes": os.path.getsize(target),
            "backup": None,
        }

    monkeypatch.setattr(eb, "atomic_write_dxf", straight_into)
    assert await correctness_suite.atomic_save_untouched_on_failure() is False
