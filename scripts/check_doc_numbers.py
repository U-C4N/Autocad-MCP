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


def derived() -> dict[str, object]:
    """Every number a document is allowed to quote, computed from the tree."""
    sys.path.insert(0, str(ROOT))
    inventory = _json("docs/tool-inventory.json")["totals"]
    ab = _json("benchmarks/results/published/ab-v1.5.0-vs-v1.5.1.json")["summary"]
    perf = {
        w["workload_id"]: w["wall_ms"]
        for w in _json("benchmarks/results/published/perf-ezdxf.json")["workloads"]
    }

    from benchmarks import correctness_suite
    from benchmarks.tasks_v3 import TASKS_V3

    return {
        "collected_tests": _collected_tests(),
        "tools": inventory["tools"],
        "resources": inventory["resources"],
        "prompts": inventory["prompts"],
        "groups": inventory["groups"],
        "ab_total": ab["total"],
        "ab_old_pass": ab["old_pass"],
        "ab_old_pct": ab["old_pct"],
        "ab_fixed": ab["fixed"],
        "ab_regressed": ab["regressed"],
        "suite_checks": len(correctness_suite.CHECKS),
        "v3_tasks": len(TASKS_V3),
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
    ("README.md", "A/B baseline pass", r"v1\.5\.0 \*\(baseline\)\* \| (\d+) / \d+", "ab_old_pass"),
    ("README.md", "A/B suite denominator", r"v1\.5\.0 \*\(baseline\)\* \| \d+ / (\d+)", "ab_total"),
    ("benchmarks/README.md", "A/B suite size", r"(\d+) checks, ezdxf backend", "ab_total"),
    (
        "benchmarks/README.md",
        "A/B baseline pass",
        r"\*\*v1\.5\.0\*\* \(baseline\)\s*\| (\d+) / \d+",
        "ab_old_pass",
    ),
    (
        "benchmarks/README.md",
        "A/B suite denominator",
        r"\*\*v1\.5\.0\*\* \(baseline\)\s*\| \d+ / (\d+)",
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
        r"v1\.5\.0 \*\(baseline\)\* \| \d+ / \d+ \| ([\d.]+) %",
        "ab_old_pct",
    ),
    (
        "README.md",
        "A/B fixed count",
        r"v1\.5\.1\*\* \*\(this release\)\* \| \*\*\d+ / \d+\*\* \| \*\*\d+ %\*\* \| (\d+)",
        "ab_fixed",
    ),
    (
        "benchmarks/README.md",
        "A/B baseline pass rate",
        r"\*\*v1\.5\.0\*\* \(baseline\)\s*\| \d+ / \d+ \| ([\d.]+) %",
        "ab_old_pct",
    ),
    (
        "benchmarks/README.md",
        "A/B fixed count",
        r"\*\*v1\.5\.1\*\* \(this release\) \| \d+ / \d+ \| \d+ % \| (\d+)",
        "ab_fixed",
    ),
    ("CLAUDE.md", "A/B suite size", r"correctness_suite\.py` \((\d+) checks\)", "ab_total"),
    ("CLAUDE.md", "v3 task count", r"the current set \((\d+) tasks", "v3_tasks"),
]


def run(verbose: bool = False) -> int:
    values = derived()

    if values["suite_checks"] != values["ab_total"]:
        print(
            f"DRIFT  correctness_suite.py has {values['suite_checks']} checks but the published "
            f"A/B report was run over {values['ab_total']} -- regenerate the report:\n"
            f"       python benchmarks/compare_versions.py v1.5.0 "
            f"--json benchmarks/results/published/ab-v1.5.0-vs-v1.5.1.json"
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

    if failures:
        print(f"\n{failures} document number(s) disagree with the tree.")
        return 1
    print(f"ok: {len(CHECKS)} document numbers match the tree")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print every derived value")
    return run(verbose=parser.parse_args().list)


if __name__ == "__main__":
    raise SystemExit(main())
