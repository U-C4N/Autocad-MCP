"""Command line runner for the fixed-task benchmark adapter contract.

Every report carries a run manifest - the server's commit (and whether the
tree was dirty), the sha256 of the files that generate the fixtures, the
evaluator version and the versions of the libraries the measurement ran on -
so two reports can be compared only when they measured the same thing.

A task a server was never asked is ``not_run`` and a lane whose environment is
absent is ``environment_blocked``; neither enters the score (spec §6 Task 20).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path

from backends.capability import UnsupportedCapabilityError
from benchmarks.adapters.base import (
    NOT_ATTEMPTED,
    BenchmarkAdapter,
    EnvironmentBlocked,
    TaskResult,
)
from benchmarks.tasks_v8 import (
    DEFAULT_MATRIX,
    EVALUATOR_VERSION,
    FIXTURE_SOURCES,
    MATRICES,
    TaskSpec,
    task_by_id,
)

REPO = Path(__file__).resolve().parents[1]

#: Libraries a measurement depends on; the manifest records each installed
#: version (None when the distribution is absent).
DEPENDENCIES: tuple[str, ...] = (
    "ezdxf",
    "fastmcp",
    "mcp",
    "numpy",
    "openpyxl",
    "matplotlib",
    "pywin32",
)


def _load_registry() -> dict[str, dict]:
    """Read competitors.yaml (JSON content) into an id -> entry registry."""
    path = Path(__file__).with_name("competitors.yaml")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {entry["id"]: entry for entry in data.get("competitors", [])}


def _make_adapter(entry: dict, backend: str) -> BenchmarkAdapter:
    module_name, _, attr = entry["adapter"].partition(":")
    adapter_cls = getattr(importlib.import_module(module_name), attr)
    return adapter_cls(backend=backend)


def _git(*args: str) -> str | None:
    """``git <args>`` in this repository (never the caller's working
    directory), stripped; None when git cannot answer."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _git_sha() -> str | None:
    return _git("rev-parse", "HEAD") or None


def _version(distribution: str) -> str | None:
    try:
        return importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError:
        return None


def run_manifest() -> dict:
    """What a report measured: the server's commit, the fixture generators'
    hashes, the evaluator version and the dependency versions."""
    status = _git("status", "--porcelain", "--untracked-files=no")
    fixtures = []
    for relative in FIXTURE_SOURCES:
        path = REPO / relative
        if path.is_file():
            data = path.read_bytes()
            fixtures.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            )
        else:
            fixtures.append({"path": relative, "missing": True})
    return {
        "server_sha": _git_sha(),
        # tracked files edited after the commit: the sha alone would not name the tree
        "dirty": None if status is None else bool(status),
        "evaluator_version": EVALUATOR_VERSION,
        "fixtures": fixtures,
        "dependencies": {name: _version(name) for name in DEPENDENCIES},
    }


def _hash_file(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path),
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def _normalize_artifacts(result: TaskResult) -> None:
    normalized: list[dict] = []
    for artifact in result.artifacts:
        if isinstance(artifact, dict):
            normalized.append(dict(artifact))
            continue
        path = Path(artifact).resolve()
        if path.is_file():
            normalized.append(_hash_file(path))
        else:
            normalized.append({"path": str(path), "missing": True})
    result.artifacts = normalized


def _summary(tasks, results: list[TaskResult]) -> dict:
    """Pass counts and the weighted score over the tasks that were attempted.

    ``not_run`` and ``environment_blocked`` stay out of every denominator: a
    task nobody asked is not a task that failed. A lane that attempted nothing
    has no score (None), never 0.
    """
    attempted = [item for item in results if item.status not in NOT_ATTEMPTED]
    supported = [item for item in attempted if item.status != "unsupported"]
    weight_by_id = {task.task_id: task.weight for task in tasks}
    total_weight = sum(weight_by_id[item.task_id] for item in attempted)
    supported_weight = sum(weight_by_id[item.task_id] for item in supported)
    weighted_score = sum(weight_by_id[item.task_id] * item.score for item in supported)
    return {
        "attempted": len(attempted),
        "passed": sum(item.status == "pass" for item in attempted),
        "supported": len(supported),
        "unsupported": sum(item.status == "unsupported" for item in attempted),
        "not_run": sum(item.status == "not_run" for item in results),
        "environment_blocked": sum(item.status == "environment_blocked" for item in results),
        "coverage_percent": (
            round(supported_weight / total_weight * 100, 2) if total_weight else None
        ),
        "score": round(weighted_score / total_weight, 2) if total_weight else None,
    }


def _report(
    name: str,
    adapter_metadata: dict,
    tasks,
    results: list[TaskResult],
    *,
    timeout: float | None,
    matrix: str,
) -> dict:
    manifest = run_manifest()
    return {
        # The *report* schema keeps every v1.4 key, so the chart renderers still
        # read the published v1.4 files; `summary` gained `not_run` /
        # `environment_blocked` and `manifest` is new in v8.
        "schema_version": "2.0",
        "matrix": matrix,
        "adapter": name,
        "adapter_metadata": adapter_metadata,
        "git_sha": manifest["server_sha"],
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "timeout_seconds": timeout,
        "manifest": manifest,
        "summary": _summary(tasks, results),
        "results": [item.to_dict() for item in results],
    }


async def run_tasks(
    adapter: BenchmarkAdapter,
    tasks: list[TaskSpec] | tuple[TaskSpec, ...],
    artifact_dir: str | Path,
    *,
    timeout: float = 30.0,
    matrix: str = DEFAULT_MATRIX,
) -> dict:
    destination = Path(artifact_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    results: list[TaskResult] = []
    try:
        await adapter.setup(destination)
    except EnvironmentBlocked as exc:
        # The lane cannot start here. Every task says so, by name, and none is
        # scored: the server was not asked, so there is nothing to grade.
        results = [TaskResult(task.task_id, "environment_blocked", 0.0, str(exc)) for task in tasks]
        try:
            await adapter.cleanup()
        except Exception:  # noqa: BLE001 - a half-set-up adapter may not clean up
            pass
        return _report(
            adapter.name,
            {"environment_blocked": str(exc)},
            tasks,
            results,
            timeout=timeout,
            matrix=matrix,
        )
    try:
        for task in tasks:
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(adapter.run_task(task), timeout=timeout)
            except TimeoutError:
                result = TaskResult(task.task_id, "timeout", 0.0, f"Exceeded {timeout}s")
            except UnsupportedCapabilityError as exc:
                # "Your server got this wrong" and "your engine cannot reach
                # this" are different findings about a competitor. The enum has
                # carried `unsupported` since v2 for exactly this, but a
                # refusal is still an exception and used to land as `fail`.
                result = TaskResult(task.task_id, "unsupported", 0.0, f"[{exc.capability}] {exc}")
            except Exception as exc:
                result = TaskResult(task.task_id, "fail", 0.0, str(exc), stderr_summary=str(exc))
            result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            _normalize_artifacts(result)
            results.append(result)
    finally:
        await adapter.cleanup()
    return _report(adapter.name, adapter.metadata(), tasks, results, timeout=timeout, matrix=matrix)


def not_run_report(entry: dict, tasks, *, matrix: str = DEFAULT_MATRIX) -> dict:
    """The report of a lane with no adapter (``"adapter": null`` in the
    registry): every task ``not_run`` with the registry's reason, no score."""
    reason = entry.get("notes") or "no adapter exists for this server"
    results = [TaskResult(task.task_id, "not_run", 0.0, reason) for task in tasks]
    metadata = {key: entry[key] for key in ("repo", "pinned_sha", "backends") if key in entry}
    return _report(entry["id"], metadata, tasks, results, timeout=None, matrix=matrix)


def _parser(registry: dict[str, dict]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List the fixed benchmark tasks")
    parser.add_argument(
        "--matrix",
        default=DEFAULT_MATRIX,
        choices=sorted(MATRICES),
        help="Task set to run. v2 stays addressable so a v1.4 report can be reproduced.",
    )
    parser.add_argument(
        "--server",
        default="autocad-mcp-pro",
        choices=sorted(registry),
        help="Adapter id from benchmarks/competitors.yaml",
    )
    parser.add_argument("--backend", default="ezdxf", choices=["ezdxf", "com"])
    parser.add_argument("--task", action="append", help="Run only this task id (repeatable)")
    parser.add_argument("--artifact-dir", default="benchmarks/results/latest")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help=(
            "Never download or install a competitor: a lane whose pinned sources "
            "and venv are not already cached is reported environment_blocked."
        ),
    )
    parser.add_argument(
        "--publish",
        type=Path,
        help=(
            "Also write the report here with artifact paths reduced to filenames. "
            "How results/published/*.json are produced — it was a manual edit before."
        ),
    )
    return parser


def _sanitize_for_publication(report: dict) -> dict:
    """Drop absolute paths from artifacts, keep the hash that identifies them.

    A published report should not carry the run machine's directory layout;
    the sha256 is what makes the artifact checkable, and that stays.
    """
    published = json.loads(json.dumps(report))
    for result in published["results"]:
        for artifact in result["artifacts"]:
            artifact.pop("path", None)
    return published


def main() -> None:
    registry = _load_registry()
    args = _parser(registry).parse_args()
    if args.list:
        for task in MATRICES[args.matrix]:
            print(f"{task.task_id}\t{task.category}\t{task.description}")
        return

    entry = registry[args.server]
    tasks = (
        [task_by_id(task_id, args.matrix) for task_id in args.task]
        if args.task
        else list(MATRICES[args.matrix])
    )
    if entry.get("adapter") is None:
        report = not_run_report(entry, tasks, matrix=args.matrix)
    else:
        supported_backends = entry.get("backends", ["ezdxf"])
        if args.backend not in supported_backends:
            raise SystemExit(
                f"{args.server} supports backends {supported_backends}, not {args.backend!r}"
            )
        adapter = _make_adapter(entry, args.backend)
        adapter.fetch = not args.no_fetch
        report = asyncio.run(
            run_tasks(adapter, tasks, args.artifact_dir, timeout=args.timeout, matrix=args.matrix)
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.publish:
        args.publish.parent.mkdir(parents=True, exist_ok=True)
        args.publish.write_text(
            json.dumps(
                _sanitize_for_publication(report), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"Published: {args.publish}", file=sys.stderr)
    print(rendered)


if __name__ == "__main__":
    main()
