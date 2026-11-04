"""Hold the numbers in the documents equal to the numbers in the tree.

Every figure this checks has been wrong at least once, and the expensive one was
wrong in three files at the same time: `benchmarks/README.md` and `CLAUDE.md`
described the A/B suite as 24 checks at 87.5% while the suite held 26 and the
published report said 80.8%. Nothing caught it because each file was edited on
its own and nobody re-read the other two.

The rule is the one the release snapshot already follows: a number a human
types is a number that rots, so derive it and fail when the prose disagrees.

    python scripts/check_doc_numbers.py          # report and exit non-zero on drift
    python scripts/check_doc_numbers.py --list   # show every derived value

`tests/test_release_consistency.py` runs this as a gate.

Since 1.8-25 it also holds the live claims ("executed live once by
`scripts/smoke_x_com.py` (AutoCAD 2026)") of README.md and CLAUDE.md to the
result files under `benchmarks/results/live/`: a claim with no pass result of
that smoke, on that seat, from the documented release or an earlier one, is
named (`UNBACKED`). The mechanism - the per-release matrix written by
`scripts/live/run_all.py`, the pre-matrix harness files, the pinned form
`(AutoCAD 2026, 1.8.0)` - is described above `LIVE_DIR`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The published A/B report the README and benchmarks/README quote. Bump this,
#: the two row labels below and the prose together, or the gate reports drift.
AB_REPORT = "benchmarks/results/published/ab-v1.7.0-vs-v1.8.0.json"
AB_BASELINE_LABEL = r"v1\.7\.0"
AB_CURRENT_LABEL = r"v1\.8\.0"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _json(relative: str) -> dict:
    return json.loads(_read(relative))


def _collected_tests() -> int:
    """How many tests pytest actually collects.

    On the same README line as tools/resources/prompts, and the only number on
    it that nothing derived -- so it drifted by 95 while its neighbours stayed
    exact. Collection only; running the suite here would make the gate cost a
    minute.
    """
    env = dict(os.environ, AUTOCAD_MCP_BACKEND="ezdxf")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    match = re.search(r"(\d+) tests collected", result.stdout)
    if not match:
        raise SystemExit(
            "check_doc_numbers: could not read a collected-test count from pytest.\n"
            + result.stdout[-2000:]
        )
    return int(match.group(1))


def matrix_tasks() -> int:
    """How many tasks the current benchmark matrix holds (the runner's
    ``DEFAULT_MATRIX``: v8 in 1.7.0, v9 since 1.8-25) - the number CLAUDE.md
    quotes for "the current set". Read from ``tasks_v9``, the module the runner
    imports, so the count follows the matrix the runner actually runs."""
    sys.path.insert(0, str(ROOT))
    from benchmarks.tasks_v9 import DEFAULT_MATRIX, MATRICES

    return len(MATRICES[DEFAULT_MATRIX])


# ── live claims (1.8-25) ─────────────────────────────────────────────────────
#
# "The COM paths were executed live once by `scripts/smoke_pid_com.py`
# (AutoCAD 2026)" is a claim about a run, and a run leaves a file. The claims
# are read from the documents and each must be backed by a ``pass`` result of
# that smoke, on a seat of that AutoCAD year, from the release being documented
# or an earlier one:
#
# * the live matrix of a release, ``benchmarks/results/live/<release>/matrix.json``
#   (written by ``scripts/live/run_all.py``: one row per smoke, ``pass`` /
#   ``fail`` / ``environment_blocked``, the seat each ran on); or
# * a harness result written before the matrix existed (1.7.0 and earlier):
#   ``benchmarks/results/live/<smoke>.json`` with ``schema``
#   ``acadmcp-live-harness/1``, ``status: "pass"``; its release is the server
#   version the file records (``environment.server.version``).
#
# A release newer than the one documented (``version.py``) never backs a claim,
# and an ``environment_blocked`` or ``fail`` row backs nothing. A claim may pin
# the release that must back it - ``(AutoCAD 2026, 1.8.0)`` - and then only
# that release's evidence counts: this is how the numbers pass wires a 1.8.0
# claim (a smoke that ran in 1.7.0 but gained stages since). Until the seat
# runs, such a claim is unbacked and named; a smoke of the documented release
# with no pass row is listed as ``pending`` by ``--list`` but is not a failure
# as long as the documents do not claim it.

LIVE_DIR = "benchmarks/results/live"
LIVE_MATRIX = "matrix.json"
LIVE_HARNESS_SCHEMA = "acadmcp-live-harness/1"

#: The documents whose live claims are held to evidence.
LIVE_CLAIM_FILES: tuple[str, ...] = ("README.md", "CLAUDE.md")

#: ``executed live once by `scripts/smoke_x_com.py` (AutoCAD 2026)`` and the
#: pinned form ``(AutoCAD 2026, 1.8.0)``. Group 1 the script, 2 the AutoCAD
#: year, 3 the pinned release (optional).
LIVE_CLAIM_PATTERN = re.compile(
    r"executed live once by `scripts/(smoke_[A-Za-z0-9_]+\.py)`\s*"
    r"\(AutoCAD (\d{4})(?:[,;]\s*(?:release\s+)?v?(\d+\.\d+\.\d+))?\)"
)

#: ``Application.Version`` (the harness drops the licence text) -> AutoCAD year.
ACAD_RELEASE_YEARS: dict[str, int] = {
    "24.0": 2021,
    "24.1": 2022,
    "24.2": 2023,
    "24.3": 2024,
    "25.0": 2025,
    "25.1": 2026,
    "26.0": 2027,
}


def _release_key(release: str | None) -> tuple[int, ...] | None:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(release or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def smoke_name(script: str) -> str:
    """``smoke_pid_com.py`` -> ``pid``; ``smoke_accore.py`` -> ``accore`` (the
    name the harness writes into a result's ``smoke``)."""
    stem = Path(script).name
    stem = stem[: -len(".py")] if stem.endswith(".py") else stem
    stem = stem[len("smoke_") :] if stem.startswith("smoke_") else stem
    return stem[: -len("_com")] if stem.endswith("_com") else stem


def seat_year(autocad: dict | None) -> int | None:
    """The AutoCAD year a result's ``environment.autocad`` names, or None.

    The install path wins (``...\\AutoCAD 2026\\accoreconsole.exe``); otherwise
    ``Application.Version`` through ``ACAD_RELEASE_YEARS``.
    """
    if not isinstance(autocad, dict):
        return None
    if isinstance(autocad.get("year"), int):
        return autocad["year"]
    found = re.search(r"AutoCAD (\d{4})", str(autocad.get("path") or ""))
    if found:
        return int(found.group(1))
    version = re.match(r"(\d+\.\d+)", str(autocad.get("version") or ""))
    return ACAD_RELEASE_YEARS.get(version.group(1)) if version else None


def documented_release() -> str:
    """The release the documents describe: ``version.py`` of this tree."""
    sys.path.insert(0, str(ROOT))
    from version import __version__

    return str(__version__)


def live_claims(files: tuple[str, ...] = LIVE_CLAIM_FILES, root: Path | None = None) -> list[dict]:
    """Every live claim in ``files``: ``{file, line, script, smoke, year, release}``."""
    base = ROOT if root is None else Path(root)
    claims = []
    for relative in files:
        path = base / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in LIVE_CLAIM_PATTERN.finditer(text):
            claims.append(
                {
                    "file": relative,
                    "line": text.count("\n", 0, match.start()) + 1,
                    "script": match.group(1),
                    "smoke": smoke_name(match.group(1)),
                    "year": int(match.group(2)),
                    "release": match.group(3),
                }
            )
    return claims


def live_evidence(root: Path | None = None) -> list[dict]:
    """Every pass / fail / blocked result on disk: ``{smoke, status, release,
    year, source}`` - matrix rows first, then the pre-matrix harness files."""
    root_path = ROOT if root is None else Path(root)
    base = root_path / LIVE_DIR
    rows: list[dict] = []
    if not base.is_dir():
        return rows
    for matrix in sorted(base.glob(f"*/{LIVE_MATRIX}")):
        try:
            data = json.loads(matrix.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        release = str(data.get("release") or matrix.parent.name)
        for row in data.get("rows", []):
            rows.append(
                {
                    "smoke": row.get("smoke"),
                    "status": row.get("status"),
                    "release": release,
                    "year": seat_year(row.get("seat")),
                    "source": matrix.relative_to(root_path).as_posix(),
                }
            )
    for single in sorted(base.glob("*.json")):
        try:
            data = json.loads(single.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema") != LIVE_HARNESS_SCHEMA:
            continue
        environment = data.get("environment") or {}
        rows.append(
            {
                "smoke": data.get("smoke"),
                "status": data.get("status"),
                "release": (environment.get("server") or {}).get("version"),
                "year": seat_year(environment.get("autocad")),
                "source": single.relative_to(root_path).as_posix(),
            }
        )
    return rows


def backing(claim: dict, evidence: list[dict], release: str) -> dict | None:
    """The first pass result that backs ``claim`` when ``release`` is documented."""
    ceiling = _release_key(release)
    for row in evidence:
        if row["smoke"] != claim["smoke"] or row["status"] != "pass":
            continue
        if row["year"] != claim["year"]:
            continue
        key = _release_key(row["release"])
        if key is None or (ceiling is not None and key > ceiling):
            continue
        if claim["release"] and key != _release_key(claim["release"]):
            continue
        return row
    return None


def check_live_claims(
    root: Path | None = None, release: str | None = None
) -> tuple[list[str], list[dict]]:
    """``(problems, table)``: one problem line per claim with no evidence, and
    one table row per claim (with the file that backs it, or None)."""
    release = documented_release() if release is None else release
    evidence = live_evidence(root)
    problems, table = [], []
    for claim in live_claims(root=root):
        row = backing(claim, evidence, release)
        table.append({**claim, "evidence": None if row is None else row["source"]})
        if row is None:
            pinned = f", release {claim['release']}" if claim["release"] else ""
            problems.append(
                f"UNBACKED  {claim['file']}:{claim['line']}: `scripts/{claim['script']}` is "
                f"claimed live (AutoCAD {claim['year']}{pinned}) but no pass result of "
                f"'{claim['smoke']}' on that seat exists for release {release} or earlier "
                f"({LIVE_DIR}/<release>/{LIVE_MATRIX} or a {LIVE_HARNESS_SCHEMA} file)"
            )
    return problems, table


def live_pending(root: Path | None = None, release: str | None = None) -> list[dict]:
    """The documented release's matrix rows that are not ``pass`` (``pending``
    claims: nothing may claim them until the seat runs)."""
    release = documented_release() if release is None else release
    return [
        row
        for row in live_evidence(root)
        if row["source"].endswith(f"/{release}/{LIVE_MATRIX}") and row["status"] != "pass"
    ]


def derived() -> dict[str, object]:
    """Every number a document is allowed to quote, computed from the tree."""
    sys.path.insert(0, str(ROOT))
    inventory = _json("docs/tool-inventory.json")["totals"]
    ab = _json(AB_REPORT)["summary"]
    perf = {
        w["workload_id"]: w["wall_ms"]
        for w in _json("benchmarks/results/published/perf-ezdxf.json")["workloads"]
    }

    from benchmarks import correctness_suite

    return {
        "collected_tests": _collected_tests(),
        "tools": inventory["tools"],
        "resources": inventory["resources"],
        "prompts": inventory["prompts"],
        "groups": inventory["groups"],
        "ab_baseline_ref": ab["baseline_ref"],
        "ab_total": ab["total"],
        "ab_old_pass": ab["old_pass"],
        "ab_old_pct": ab["old_pct"],
        "ab_fixed": ab["fixed"],
        "ab_regressed": ab["regressed"],
        "suite_checks": len(correctness_suite.CHECKS),
        "matrix_tasks": matrix_tasks(),
        "perf_ms": perf,
    }


#: (relative path, human label, regex over the file, callable -> expected value)
#:
#: Each pattern captures the number as group 1. A pattern that matches nothing is
#: itself a failure: the sentence it was written for has moved, and a silent
#: no-op gate is worse than no gate.
CHECKS: list[tuple[str, str, str, str]] = [
    ("README.md", "release snapshot tools", r"release snapshot:\*\*\s*(\d+) tools", "tools"),
    (
        "README.md",
        "release snapshot resources",
        r"release snapshot:.*?(\d+) resources",
        "resources",
    ),
    (
        "README.md",
        "release snapshot prompts",
        r"release snapshot:.*?(\d+) prompt templates",
        "prompts",
    ),
    ("README.md", "A/B suite size", r"(\d+) deterministic headless checks", "ab_total"),
    (
        "README.md",
        "A/B baseline pass",
        AB_BASELINE_LABEL + r" \*\(baseline\)\* \| (\d+) / \d+",
        "ab_old_pass",
    ),
    (
        "README.md",
        "A/B suite denominator",
        AB_BASELINE_LABEL + r" \*\(baseline\)\* \| \d+ / (\d+)",
        "ab_total",
    ),
    ("benchmarks/README.md", "A/B suite size", r"(\d+) checks, ezdxf backend", "ab_total"),
    (
        "benchmarks/README.md",
        "A/B baseline pass",
        r"\*\*" + AB_BASELINE_LABEL + r"\*\* \(baseline\)\s*\| (\d+) / \d+",
        "ab_old_pass",
    ),
    (
        "benchmarks/README.md",
        "A/B suite denominator",
        r"\*\*" + AB_BASELINE_LABEL + r"\*\* \(baseline\)\s*\| \d+ / (\d+)",
        "ab_total",
    ),
    (
        "README.md",
        "release snapshot collected tests",
        r"release snapshot:.*?(\d+) collected tests",
        "collected_tests",
    ),
    (
        "README.md",
        "A/B baseline pass rate",
        AB_BASELINE_LABEL + r" \*\(baseline\)\* \| \d+ / \d+ \| ([\d.]+) %",
        "ab_old_pct",
    ),
    (
        "README.md",
        "A/B fixed count",
        AB_CURRENT_LABEL
        + r"\*\* \*\(this release\)\* \| \*\*\d+ / \d+\*\* \| \*\*\d+ %\*\* \| (\d+)",
        "ab_fixed",
    ),
    (
        "benchmarks/README.md",
        "A/B baseline pass rate",
        r"\*\*" + AB_BASELINE_LABEL + r"\*\* \(baseline\)\s*\| \d+ / \d+ \| ([\d.]+) %",
        "ab_old_pct",
    ),
    (
        "benchmarks/README.md",
        "A/B fixed count",
        r"\*\*" + AB_CURRENT_LABEL + r"\*\* \(this release\) \| \d+ / \d+ \| \d+ % \| (\d+)",
        "ab_fixed",
    ),
    ("CLAUDE.md", "A/B suite size", r"correctness_suite\.py` \((\d+) checks\)", "ab_total"),
    ("CLAUDE.md", "matrix task count", r"the current set \((\d+) tasks", "matrix_tasks"),
]


def run(verbose: bool = False) -> int:
    values = derived()

    if values["suite_checks"] != values["ab_total"]:
        print(
            f"DRIFT  correctness_suite.py has {values['suite_checks']} checks but the published "
            f"A/B report was run over {values['ab_total']} -- regenerate the report:\n"
            f"       python benchmarks/compare_versions.py {values['ab_baseline_ref']} "
            f"--json {AB_REPORT}"
        )
        return 1

    if verbose:
        for key, value in values.items():
            print(f"  {key:20s} {value}")
        print()

    failures = 0
    for relative, label, pattern, key in CHECKS:
        text = _read(relative)
        match = re.search(pattern, text, re.DOTALL)
        expected = values[key]
        if match is None:
            print(
                f"DRIFT  {relative}: the sentence carrying '{label}' no longer matches its pattern"
            )
            failures += 1
            continue
        found = type(expected)(match.group(1))
        if found != expected:
            print(f"DRIFT  {relative}: {label} says {found}, the tree says {expected}")
            failures += 1
        elif verbose:
            print(f"  ok  {relative:24s} {label:24s} {found}")

    problems, table = check_live_claims()
    for line in problems:
        print(line)
    if verbose:
        for row in table:
            print(
                f"  live  {row['file']}:{row['line']} {row['script']} "
                f"(AutoCAD {row['year']}) <- {row['evidence']}"
            )
        for row in live_pending():
            print(f"  pending  {row['smoke']}: {row['status']} in {row['source']}")

    if failures or problems:
        if failures:
            print(f"\n{failures} document number(s) disagree with the tree.")
        if problems:
            print(f"\n{len(problems)} live claim(s) have no evidence file.")
        return 1
    print(
        f"ok: {len(CHECKS)} document numbers match the tree; "
        f"{len(table)} live claim(s) backed by a result file"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print every derived value")
    return run(verbose=parser.parse_args().list)


if __name__ == "__main__":
    raise SystemExit(main())
