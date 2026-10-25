"""Re-measure the drawings a skill-eval run saved (spec §6 Task 22, B55).

    uv run --frozen --all-extras python benchmarks/skill_eval/check_artifacts.py \
        --root <a run's folder> [--case <name> ...] [--json out/skill-eval-artifacts.json]
    uv run --frozen --all-extras python benchmarks/skill_eval/check_artifacts.py \
        --case saved-not-in-memory --file <path/to/bracket.dxf>

A grader of ``claude plugin eval`` sees the transcript and the files a run left,
never what is inside a drawing: ``file_exists`` says ``out/bracket.dxf`` was
written, not whether its holes are 40 mm apart. This script opens each saved
drawing fresh with ``engineering.verification.artifact.verify_artifact`` - the
reader ``drawing_verify_requirements(artifact_path=...)`` uses - and measures it
against the requirements in ``artifact_checks.json``: what a correct run's file
holds. A case is

- ``as_expected``: the file's acceptance status is the one the check expects
  (and, for ``pass``, the report accepts it);
- ``not_as_expected``: it is not - the rows say what was measured;
- ``missing``: the run saved no such file (a failed case, never a skip);
- ``unreadable``: the file is there and cannot be read (the reason is kept).

Exit 0 when every checked case is ``as_expected``, 1 otherwise, 2 on a usage
error (an unknown case, ``--file`` with more than one case, no ``--root`` or
``--file``). The script reads; it never writes a drawing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS_FILE = Path(__file__).resolve().parent / "artifact_checks.json"

__all__ = ["CHECKS_FILE", "check_case", "load_checks", "main"]


def load_checks(path: Path = CHECKS_FILE) -> dict[str, dict]:
    """The artifact checks by case name (keys starting with ``_`` are notes)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {name: check for name, check in data.items() if not name.startswith("_")}


async def check_case(
    name: str,
    *,
    root: Path | None = None,
    file: Path | None = None,
    checks: dict[str, dict] | None = None,
) -> dict:
    """Measure case ``name``'s saved drawing: ``file``, or the check's path under ``root``."""
    from engineering.verification.artifact import verify_artifact
    from engineering.verification.requirements import parse_requirements

    checks = load_checks() if checks is None else checks
    if name not in checks:
        raise KeyError(f"no artifact check for case {name!r}; known: {', '.join(sorted(checks))}")
    check = checks[name]
    if file is None:
        if root is None:
            raise ValueError("give the run's folder (root) or the saved file (file)")
        file = Path(root) / check["path"]
    result = {"case": name, "path": str(file), "expect": check["expect"]}
    if not Path(file).is_file():
        return {**result, "status": "missing", "reason": f"{check['path']}: no such file"}
    requirements = parse_requirements(check["requirements"])
    try:
        report = await verify_artifact(str(file), requirements)
    except Exception as exc:  # an unreadable drawing is a finding, not a crash
        return {**result, "status": "unreadable", "reason": f"{type(exc).__name__}: {exc}"}
    met = report["status"] == check["expect"] and (check["expect"] != "pass" or report["accepted"])
    return {
        **result,
        "status": "as_expected" if met else "not_as_expected",
        "measured": {
            row["id"]: {
                "status": row["status"],
                "value": (row.get("measured") or {}).get("value"),
            }
            for row in report["rows"]
        },
        "report": report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--case", action="append", help="a case to check (repeatable; default: every check)"
    )
    parser.add_argument("--root", type=Path, help="the run's folder the saved paths are under")
    parser.add_argument("--file", type=Path, help="the saved drawing itself (one --case only)")
    parser.add_argument("--json", type=Path, help="write the full results here")
    args = parser.parse_args(argv)
    checks = load_checks()
    names = args.case or sorted(checks)
    unknown = [name for name in names if name not in checks]
    if unknown:
        parser.error(f"no artifact check for {', '.join(unknown)}; known: {', '.join(checks)}")
    if args.file is not None and len(names) != 1:
        parser.error("--file measures one drawing: give exactly one --case")
    if args.file is None and args.root is None:
        parser.error("give --root <run folder> or --file <drawing>")

    async def run() -> list[dict]:
        return [
            await check_case(name, root=args.root, file=args.file, checks=checks) for name in names
        ]

    results = asyncio.run(run())
    for item in results:
        detail = item.get("reason") or ", ".join(
            f"{rid} {row['status']} {row['value']}" for rid, row in item["measured"].items()
        )
        print(f"{item['case']}: {item['status']} (expect {item['expect']}) - {detail}")
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"cases": results}, indent=2, default=str) + "\n", encoding="utf-8"
        )
    return 0 if all(item["status"] == "as_expected" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
