"""Command line runner for the fixed-task benchmark v2 adapter contract."""

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
from pathlib import Path

from backends.capability import UnsupportedCapabilityError
from benchmarks.adapters.base import BenchmarkAdapter, TaskResult
from benchmarks.tasks_v3 import DEFAULT_MATRIX, MATRICES, TaskSpec, task_by_id


def _load_registry() -> dict[str, dict]:
    """Read competitors.yaml (JSON content) into an id -> entry registry."""
    path = Path(__file__).with_name("competitors.yaml")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {entry["id"]: entry for entry in data.get("competitors", [])}


def _make_adapter(entry: dict, backend: str) -> BenchmarkAdapter:
    module_name, _, attr = entry["adapter"].partition(":")
    adapter_cls = getattr(importlib.import_module(module_name), attr)
    return adapter_cls(backend=backend)


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


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
    await adapter.setup(destination)
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

    attempted = len(results)
    passed = sum(item.status == "pass" for item in results)
    supported = [item for item in results if item.status != "unsupported"]
    weight_by_id = {task.task_id: task.weight for task in tasks}
    total_weight = sum(weight_by_id.values())
    supported_weight = sum(weight_by_id[item.task_id] for item in supported)
    weighted_score = sum(
        weight_by_id[item.task_id] * item.score for item in results if item.status != "unsupported"
    )
    return {
        # The *report* schema is unchanged — same keys, same enum — so the
        # chart renderers still read v1.4's published files. `matrix` says
        # which task set ran, which is the thing that actually moved.
        "schema_version": "2.0",
        "matrix": matrix,
        "adapter": adapter.name,
        "adapter_metadata": adapter.metadata(),
        "git_sha": _git_sha(),
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "timeout_seconds": timeout,
        "summary": {
            "attempted": attempted,
            "passed": passed,
            "supported": len(supported),
            "unsupported": sum(item.status == "unsupported" for item in results),
            "coverage_percent": (
                round(supported_weight / total_weight * 100, 2) if total_weight else 0.0
            ),
            "score": round(weighted_score / total_weight, 2) if total_weight else 0.0,
        },
        "results": [item.to_dict() for item in results],
    }


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
    supported_backends = entry.get("backends", ["ezdxf"])
    if args.backend not in supported_backends:
        raise SystemExit(
            f"{args.server} supports backends {supported_backends}, not {args.backend!r}"
        )
    tasks = (
        [task_by_id(task_id, args.matrix) for task_id in args.task]
        if args.task
        else list(MATRICES[args.matrix])
    )
    adapter = _make_adapter(entry, args.backend)
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
