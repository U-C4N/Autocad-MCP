"""1.8-25 - a live claim in README / CLAUDE.md needs a result file behind it.

``scripts/check_doc_numbers.py`` reads every "executed live once by
`scripts/smoke_x_com.py` (AutoCAD 2026)" and looks for a ``pass`` result of
that smoke on that seat, from the documented release or an earlier one: a row
of ``benchmarks/results/live/<release>/matrix.json`` (``scripts/live/run_all.py``)
or a pre-matrix harness file. A claim without one is named.

Review 1.8-25a: that one phrasing was all the gate read, so README's "run
against a **live AutoCAD 2026**" and any "AutoCAD 2027 tested" passed
unchecked. The claims are now read sentence by sentence: a sentence saying a
run happened and naming a seat year or a smoke script is a claim, whatever its
wording, unless it says the run did not happen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_doc_numbers as cdn

ROOT = Path(__file__).resolve().parents[1]


def _claim(script: str, year: int = 2026, release: str | None = None) -> str:
    pinned = f", {release}" if release else ""
    return (
        f"The COM paths were executed live once by `scripts/{script}` (AutoCAD {year}{pinned}).\n"
    )


def _tree(tmp_path: Path, claude: str, readme: str = "") -> Path:
    (tmp_path / "CLAUDE.md").write_text(claude, encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    (tmp_path / cdn.LIVE_DIR).mkdir(parents=True)
    return tmp_path


def _harness_file(root: Path, smoke: str, status: str, version: str, acad: str = "25.1s"):
    data = {
        "schema": cdn.LIVE_HARNESS_SCHEMA,
        "smoke": smoke,
        "status": status,
        "environment": {"autocad": {"version": acad}, "server": {"version": version}},
    }
    (root / cdn.LIVE_DIR / f"{smoke}.json").write_text(json.dumps(data), encoding="utf-8")


def _matrix(root: Path, release: str, rows: list[tuple[str, str]], acad: str = "25.1s"):
    folder = root / cdn.LIVE_DIR / release
    folder.mkdir(parents=True)
    data = {
        "schema": "acadmcp-live-matrix/1",
        "release": release,
        "rows": [
            {
                "smoke": smoke,
                "status": status,
                "seat": None if status == "environment_blocked" else {"version": acad},
            }
            for smoke, status in rows
        ],
    }
    (folder / cdn.LIVE_MATRIX).write_text(json.dumps(data), encoding="utf-8")


def test_a_claim_without_evidence_is_named(tmp_path):
    root = _tree(tmp_path, _claim("smoke_pid_com.py") + _claim("smoke_space_com.py"))
    _harness_file(root, "pid", "pass", "1.7.0")
    problems, table = cdn.check_live_claims(root, release="1.8.0")
    assert len(problems) == 1
    assert "smoke_space_com.py" in problems[0] and "CLAUDE.md:2" in problems[0]
    assert problems[0].startswith("UNBACKED")
    assert {row["smoke"]: row["evidence"] for row in table} == {
        "pid": "benchmarks/results/live/pid.json",
        "space": None,
    }


def test_blocked_failed_newer_or_other_seat_results_back_nothing(tmp_path):
    root = _tree(
        tmp_path,
        _claim("smoke_edit_com.py")
        + _claim("smoke_render_com.py")
        + _claim("smoke_lisp_com.py")
        + _claim("smoke_verify_com.py", year=2027),
    )
    _matrix(root, "1.8.0", [("edit", "environment_blocked"), ("render", "fail")])
    _matrix(root, "1.9.0", [("lisp", "pass")])  # newer than the documented release
    _harness_file(root, "verify", "pass", "1.7.0", acad="25.1s")  # a 2026 seat
    problems, _table = cdn.check_live_claims(root, release="1.8.0")
    assert [p.split("`")[1] for p in problems] == [
        "scripts/smoke_edit_com.py",
        "scripts/smoke_render_com.py",
        "scripts/smoke_lisp_com.py",
        "scripts/smoke_verify_com.py",
    ]


def test_a_release_matrix_pass_row_backs_a_claim_and_a_pinned_claim_wants_its_release(tmp_path):
    root = _tree(
        tmp_path,
        _claim("smoke_settings_com.py", release="1.8.0") + _claim("smoke_space_com.py"),
        readme=_claim("smoke_accore.py"),
    )
    _harness_file(root, "settings", "pass", "1.7.0")  # an older run: not the pinned one
    _matrix(root, "1.8.0", [("space", "pass"), ("accore", "pass"), ("settings", "pending")])
    problems, table = cdn.check_live_claims(root, release="1.8.0")
    assert len(problems) == 1 and "smoke_settings_com.py" in problems[0]
    assert "release 1.8.0" in problems[0]
    evidence = {row["smoke"]: row["evidence"] for row in table}
    assert evidence["space"] == evidence["accore"] == "benchmarks/results/live/1.8.0/matrix.json"

    _matrix_rows = json.loads((root / cdn.LIVE_DIR / "1.8.0" / cdn.LIVE_MATRIX).read_text())
    _matrix_rows["rows"][2]["status"] = "pass"
    (root / cdn.LIVE_DIR / "1.8.0" / cdn.LIVE_MATRIX).write_text(json.dumps(_matrix_rows))
    assert cdn.check_live_claims(root, release="1.8.0")[0] == []


def test_pending_lists_the_documented_releases_rows_that_did_not_pass(tmp_path):
    root = _tree(tmp_path, "")
    _matrix(root, "1.8.0", [("pid", "environment_blocked"), ("accore", "pass")])
    pending = cdn.live_pending(root, release="1.8.0")
    assert [(row["smoke"], row["status"]) for row in pending] == [("pid", "environment_blocked")]


@pytest.mark.parametrize(
    ("autocad", "year"),
    [
        ({"version": "25.1s"}, 2026),
        ({"version": "25.0"}, 2025),
        ({"version": "W.60.0.0", "path": r"C:\Program Files\Autodesk\AutoCAD 2026\x.exe"}, 2026),
        ({"version": "99.9"}, None),
        (None, None),
    ],
)
def test_the_seat_year(autocad, year):
    assert cdn.seat_year(autocad) == year


def test_smoke_names_follow_the_harness():
    assert cdn.smoke_name("smoke_pid_com.py") == "pid"
    assert cdn.smoke_name("smoke_operator_edits_com.py") == "operator_edits"
    assert cdn.smoke_name("smoke_accore.py") == "accore"


def test_every_live_claim_of_this_tree_is_backed():
    problems, table = cdn.check_live_claims()
    assert problems == [], problems
    assert table, "the documents carry live claims; the pattern still finds them"


# -- review 1.8-25a: a live claim is not one phrasing -----------------------------------

#: Every wording below claims a run that no result file backs: the evidence is
#: one pid pass on a 2026 seat from 1.7.0 (and nothing for space, nothing on 2027).
_UNBACKED_WORDINGS = (
    "> The COM paths run against a **live AutoCAD 2027** through thirteen asserting smoke scripts.",
    "The COM paths were executed live by `scripts/smoke_space_com.py` (AutoCAD 2026)",
    "The COM paths were executed live once by `scripts/smoke_space_com.py` on AutoCAD 2026",
    "AutoCAD 2027 tested: `scripts/smoke_space_com.py` passed live.",
    "| **Evidence** | the live COM harness passing on AutoCAD 2027, and no workload slower |",
    "`scripts/smoke_space_com.py` passed live.",
    "Measured on AutoCAD 2027: an entity added moves the revision by one.",
    "The COM paths were executed live once by `scripts/smoke_pid_com.py` (AutoCAD 2027).",
)


@pytest.mark.parametrize("sentence", _UNBACKED_WORDINGS)
def test_any_wording_of_an_unbacked_live_run_is_named(tmp_path, sentence):
    root = _tree(tmp_path, "", readme="# Title\n\nSome prose.\n\n" + sentence + "\n")
    _harness_file(root, "pid", "pass", "1.7.0")
    problems, table = cdn.check_live_claims(root, release="1.8.0")
    assert len(problems) == 1 and problems[0].startswith("UNBACKED  README.md:5:"), problems
    assert all(row["evidence"] is None for row in table)


@pytest.mark.parametrize(
    "sentence",
    [
        "The live COM harness passed on AutoCAD 2026.",
        "> The COM paths run against a **live AutoCAD 2026** through one smoke script.",
        "The COM paths were executed live once by `scripts/smoke_pid_com.py` (AutoCAD 2026).",
        "`scripts/smoke_pid_com.py` passed live on AutoCAD 2026.",
    ],
)
def test_a_backed_wording_passes(tmp_path, sentence):
    root = _tree(tmp_path, "", readme=sentence + "\n")
    _harness_file(root, "pid", "pass", "1.7.0")
    problems, table = cdn.check_live_claims(root, release="1.8.0")
    assert problems == [] and table and all(row["evidence"] for row in table)


@pytest.mark.parametrize(
    "sentence",
    [
        "Four of them have not been run on a live seat yet: `smoke_space_com.py`, "
        "`smoke_edit_com.py` and `smoke_render_com.py`.",
        "The live check is `scripts/smoke_lisp_com.py` (written, not yet run on a seat).",
        "The live COM paths are checked on a running AutoCAD, never in CI: "
        "`python scripts/smoke_verify_com.py --json out/verify.json`.",
        "No 2027 seat was at hand: the AutoCAD 2027 lane is environment_blocked.",
        "The live assumption is unmeasured until the `set edits` stage of "
        "`scripts/smoke_settings_com.py` runs.",
        "The `scripts/smoke_*_com.py` scripts attach to a live seat.",
    ],
)
def test_a_sentence_that_says_the_run_did_not_happen_claims_nothing(tmp_path, sentence):
    root = _tree(tmp_path, "", readme=sentence + "\n")
    assert cdn.live_claims(root=root) == []


def test_a_seat_claim_is_backed_only_by_a_pass_on_that_years_seat(tmp_path):
    root = _tree(tmp_path, "", readme="The live COM harness passes on AutoCAD 2026.\n")
    _matrix(root, "1.8.0", [("space", "environment_blocked"), ("edit", "fail")])
    (problem,) = cdn.check_live_claims(root, release="1.8.0")[0]
    assert "a live AutoCAD 2026 run is claimed" in problem
    _harness_file(root, "pid", "pass", "1.7.0", acad="26.0")  # a 2027 seat
    assert len(cdn.check_live_claims(root, release="1.8.0")[0]) == 1
    _harness_file(root, "arch", "pass", "1.7.0", acad="25.1s")  # a 2026 seat
    assert cdn.check_live_claims(root, release="1.8.0")[0] == []


def test_the_readme_live_claims_of_this_tree_are_read():
    # review 1.8-25a: live_claims(files=("README.md",)) was [] on this tree
    readme = cdn.live_claims(files=("README.md",))
    assert readme, "README states live runs on AutoCAD 2026; the reader must find them"
    assert {claim["year"] for claim in readme} == {2026}
