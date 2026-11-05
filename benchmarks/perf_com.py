"""Live COM speed, before and after 1.8-23, on one AutoCAD seat (plan 1.8-23 (5)).

§4.6 of the rival's report measured the live engine about 2x slower than its
own (370 s against 183 s). The code had mechanical causes - a viewport Regen
per created entity, an O(N) read around every corner op, and "bulk" tools
that were N STA jobs - and 1.8-23 removes them. This script measures the
result; it is run by hand before a release (not in CI) and its numbers are
the decision input of docs/adr/0001-native-worker.md.

**Targets, fixed before any measurement** (``TARGETS``, also written into the
JSON so a result cannot be judged against targets chosen after it):

* ``bulk_speedup_min`` - ``entity_batch_create(200)`` and
  ``entity_delete_many(200)`` are each at least **5x** faster than v1.7.0's
  200 single calls (``entity_create_circle`` / ``entity_delete``);
* ``single_create_median_drop_min`` - the median single ``entity_create_circle``
  is at least **30 %** lower than v1.7.0's;
* ``fillet_n_ratio_max`` - "fillet time independent of N": the median
  ``entity_fillet`` in a drawing of 10,000 lines is at most **1.2x** the
  median in one of 5,000.

**What runs.** On the live harness (scripts/live/harness.py: attach with
``GetActiveObject`` only, one ``Documents.Add()`` document of its own, closed
with ``Close(False)``, no event sink), through the server's own tools (an
in-process ``fastmcp.Client`` over ``server.mcp`` whose backend is the
harness's ``ComBackend``), in a document seeded with 5,000 lines:

    create_circle_singles   200 x entity_create_circle            (per call)
    batch_create_200        entity_batch_create(200 circles)      (x --reps)
    delete_singles_200      200 x entity_delete                   (per call)
    delete_many_200         entity_delete_many(200)               (x --reps)
    cad_batch_50            cad_batch of 50 entity_create_circle  (x --reps)
    fillet_50_n5000         50 x entity_fillet at 5,000 lines     (per call)
    fillet_50_n10000        50 x entity_fillet at 10,000 lines    (per call)

What a workload created is deleted again (untimed) so every workload runs at
the same N. Each workload reports n, median, p95, min, max and total ms and,
where the code has it (1.8-14 onward), the ``_run`` breakdown of its jobs
(queue / guard / work / refresh / counter ms; ``system_status(metrics=True)``).

**Before / after.** ``--base PATH`` names a second checkout of the v1.7.0 tag
(``git worktree add ../acadmcp-v1.7.0 v1.7.0``); each side runs in its own
child process with that side's ``backends`` / ``server`` first on
``sys.path`` and this script's harness loaded by file, so the two sides differ
only in the code under test. On v1.7.0 the bulk tools are the server's loops
over the single calls, which is exactly the comparison the targets name.
Without ``--base`` only the current side is measured and every target that
needs v1.7.0 is ``pending``.

Run (AutoCAD open, an empty session is fine; nothing of the operator's is
touched):

    uv run --frozen python -m benchmarks.perf_com --base ..\\acadmcp-v1.7.0
    uv run --frozen python -m benchmarks.perf_com --pending   # the skeleton, no AutoCAD

Writes ``benchmarks/results/live/perf_com.json`` (``--json`` to change it).
Exit codes: 0 every measured target met, 1 a target missed or a side failed,
2 no AutoCAD to attach to.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]

SCHEMA = "acadmcp-perf-com/1"
ITEM = "1.8-23"
DEFAULT_JSON = ROOT / "benchmarks" / "results" / "live" / "perf_com.json"

#: Fixed before the first live run (plan 1.8-23 "Kanıt / Gate").
TARGETS: dict[str, float] = {
    "bulk_speedup_min": 5.0,
    "single_create_median_drop_min": 0.30,
    "fillet_n_ratio_max": 1.2,
}

#: The seeded drawing and the workload sizes.
SEED_LINES = 5_000
SINGLES = 200
BULK = 200
FILLETS = 50
BATCH_STEPS = 50
DEFAULT_REPS = 5

WORKLOADS = (
    "create_circle_singles",
    "batch_create_200",
    "delete_singles_200",
    "delete_many_200",
    "cad_batch_50",
    "fillet_50_n5000",
    "fillet_50_n10000",
)

#: The result's shape, for a reader of the JSON (and the skeleton --pending writes).
RESULT_SCHEMA: dict[str, Any] = {
    "schema": SCHEMA,
    "item": ITEM,
    "status": "pending | measured | unavailable | failed",
    "targets": "TARGETS, fixed before the run",
    "verdict": {
        name: "{value, target, pass (true / false / null = pending)}"
        for name in (
            "bulk_create_speedup",
            "bulk_delete_speedup",
            "single_create_median_drop",
            "fillet_n_ratio",
        )
    },
    "sides": {
        "base": "the v1.7.0 side (--base) or null",
        "current": "this checkout's side",
        "<side>": {
            "code": "{root, version, sha, dirty}",
            "harness": "the live harness JSON (status, checks, environment, documents)",
            "workloads": {
                name: "{n, median_ms, p95_ms, min_ms, max_ms, total_ms, errors, run_breakdown}"
                for name in WORKLOADS
            },
        },
    },
}


# ── statistics ──────────────────────────────────────────────────────────────


def summarise(samples: list[float], errors: int = 0, breakdown: dict | None = None) -> dict:
    """``{n, median_ms, p95_ms, min_ms, max_ms, total_ms, errors, run_breakdown}``."""
    if not samples:
        return {"n": 0, "errors": errors, "run_breakdown": breakdown}
    ordered = sorted(samples)
    rank = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return {
        "n": len(ordered),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[rank], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "total_ms": round(sum(ordered), 3),
        "errors": errors,
        "run_breakdown": breakdown,
    }


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 3)


def _metric(side: dict | None, workload: str, key: str) -> float | None:
    if not side:
        return None
    row = (side.get("workloads") or {}).get(workload) or {}
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def verdict(base: dict | None, current: dict | None) -> dict:
    """Each target: ``{value, target, pass}``; ``pass`` None while a side is missing."""

    def row(value: float | None, target: float, at_least: bool) -> dict:
        passed = None if value is None else (value >= target if at_least else value <= target)
        return {"value": value, "target": target, "pass": passed}

    singles_base = _metric(base, "create_circle_singles", "median_ms")
    singles_now = _metric(current, "create_circle_singles", "median_ms")
    drop = None
    if singles_base and singles_now is not None:
        drop = round(1.0 - singles_now / singles_base, 3)
    return {
        "bulk_create_speedup": row(
            _ratio(
                _metric(base, "create_circle_singles", "total_ms"),
                _metric(current, "batch_create_200", "median_ms"),
            ),
            TARGETS["bulk_speedup_min"],
            True,
        ),
        "bulk_delete_speedup": row(
            _ratio(
                _metric(base, "delete_singles_200", "total_ms"),
                _metric(current, "delete_many_200", "median_ms"),
            ),
            TARGETS["bulk_speedup_min"],
            True,
        ),
        "single_create_median_drop": row(drop, TARGETS["single_create_median_drop_min"], True),
        "fillet_n_ratio": row(
            _ratio(
                _metric(current, "fillet_50_n10000", "median_ms"),
                _metric(current, "fillet_50_n5000", "median_ms"),
            ),
            TARGETS["fillet_n_ratio_max"],
            False,
        ),
    }


def skeleton(status: str = "pending", reason: str | None = None) -> dict:
    return {
        "schema": SCHEMA,
        "item": ITEM,
        "status": status,
        "reason": reason,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "targets": dict(TARGETS),
        "workloads": list(WORKLOADS),
        "sizes": {
            "seed_lines": SEED_LINES,
            "singles": SINGLES,
            "bulk": BULK,
            "fillets": FILLETS,
            "batch_steps": BATCH_STEPS,
        },
        "verdict": verdict(None, None),
        "sides": {"base": None, "current": None},
        "result_schema": RESULT_SCHEMA,
    }


# ── one side (a child process) ──────────────────────────────────────────────


def _load_harness():
    """This checkout's harness, loaded by file so the code under test cannot shadow it."""
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))  # the harness then leaves sys.path[0] alone
    spec = importlib.util.spec_from_file_location(
        "acadmcp_perf_harness", ROOT / "scripts" / "live" / "harness.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _code_build(code_root: Path) -> dict:
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(code_root), *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    # The checkout's own pyproject, never ``import version``: a side's
    # version.py answers from the installed package's metadata first, which is
    # the environment's release (a v1.7.0 checkout measured from a 1.8.0
    # environment read "1.8.0"), not the code under test.
    version = None
    try:
        import tomllib

        with (code_root / "pyproject.toml").open("rb") as stream:
            version = str(tomllib.load(stream)["project"]["version"])
    except (OSError, KeyError, ValueError):
        version = None
    status = git("status", "--porcelain", "--untracked-files=no")
    return {
        "root": code_root.name,
        "version": version,
        "sha": git("rev-parse", "HEAD"),
        "dirty": None if status is None else bool(status),
    }


def _point(x: float, y: float):
    import pythoncom
    import win32com.client

    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (x, y, 0.0))


def _find(app, name: str):
    for index in range(int(app.Documents.Count)):
        doc = app.Documents.Item(index)
        if str(doc.Name) == name:
            return doc
    raise LookupError(f"{name} is not open")


async def _seed_lines(h, name: str, count: int, y0: float) -> int:
    """``count`` lines added by raw COM in ONE job, far from the workloads; the new Count."""

    def _sync(app):
        space = _find(app, name).ModelSpace
        for index in range(count):
            y = y0 + float(index)
            space.AddLine(_point(1.0e6, y), _point(1.0e6 + 100.0, y))
        return int(space.Count)

    return await h.raw(_sync)


async def _corners(h, name: str, count: int, y: float) -> list[tuple[str, str]]:
    """``count`` L corners (two lines meeting at a point) to fillet; their handle pairs."""

    def _sync(app):
        space = _find(app, name).ModelSpace
        pairs = []
        for index in range(count):
            x = float(index) * 50.0
            a = space.AddLine(_point(x, y), _point(x + 20.0, y))
            b = space.AddLine(_point(x, y), _point(x, y + 20.0))
            pairs.append((str(a.Handle), str(b.Handle)))
        return pairs

    return await h.raw(_sync)


def _circles(count: int, y: float) -> list[dict]:
    return [{"type": "circle", "cx": float(i) * 3.0, "cy": y, "radius": 1.0} for i in range(count)]


class _Side:
    """The workloads of one side, through the server's tools on the harness's backend."""

    def __init__(self, h, client):
        self.h = h
        self.client = client
        self.backend = h.backend

    def _reset_breakdown(self) -> None:
        breakdown = getattr(self.backend, "_run_breakdown", None)
        if breakdown is not None:
            self.backend._run_breakdown = type(breakdown)()

    def _breakdown(self) -> dict | None:
        metrics = getattr(self.backend, "run_metrics", None)
        return metrics() if callable(metrics) else None  # absent before 1.8-14

    async def call(self, tool: str, args: dict) -> tuple[float, Any, bool]:
        started = time.perf_counter()
        result = await self.client.call_tool(tool, args, raise_on_error=False)
        elapsed = (time.perf_counter() - started) * 1000.0
        return elapsed, result.structured_content, bool(result.is_error)

    async def cleanup(self, handles: list[str]) -> None:
        if handles:
            await self.client.call_tool(
                "entity_delete_many", {"handles": handles}, raise_on_error=False
            )

    async def per_call(self, tool: str, arg_list: list[dict]) -> tuple[dict, list]:
        self._reset_breakdown()
        samples, errors, payloads = [], 0, []
        for args in arg_list:
            elapsed, payload, failed = await self.call(tool, args)
            samples.append(elapsed)
            errors += int(failed)
            payloads.append(payload)
        return summarise(samples, errors, self._breakdown()), payloads

    async def repeated(self, tool: str, make_args, reps: int, after=None) -> dict:
        """``reps`` timed calls after one untimed warm-up (the ADR's protocol)."""
        args = await make_args()
        _ms, payload, _failed = await self.call(tool, args)
        if after is not None:
            await after(payload)
        samples, errors, breakdowns = [], 0, []
        for _ in range(reps):
            args = await make_args()
            self._reset_breakdown()
            elapsed, payload, failed = await self.call(tool, args)
            breakdowns.append(self._breakdown())
            samples.append(elapsed)
            errors += int(failed)
            if after is not None:
                await after(payload)
        return summarise(samples, errors, breakdowns[-1] if breakdowns else None)

    async def created(self, count: int, y: float) -> list[str]:
        _ms, payload, _failed = await self.call(
            "entity_batch_create", {"entities": _circles(count, y)}
        )
        return [row["handle"] for row in (payload or {}).get("entities", [])]


def _handles_of(payload) -> list[str]:
    rows = (payload or {}).get("entities") or []
    return [row["handle"] for row in rows if isinstance(row, dict) and row.get("handle")]


async def _workloads(h, side: _Side, name: str, reps: int) -> dict:
    out: dict[str, dict] = {}

    h.stage("create_circle_singles")
    row, payloads = await side.per_call(
        "entity_create_circle",
        [{"cx": float(i) * 3.0, "cy": -100.0, "radius": 1.0} for i in range(SINGLES)],
    )
    out["create_circle_singles"] = row
    await side.cleanup([p["handle"] for p in payloads if isinstance(p, dict) and "handle" in p])

    h.stage("batch_create_200")

    async def batch_args():
        return {"entities": _circles(BULK, -200.0)}

    async def drop_created(payload):
        await side.cleanup(_handles_of(payload))

    out["batch_create_200"] = await side.repeated(
        "entity_batch_create", batch_args, reps, after=drop_created
    )

    h.stage("delete_singles_200")
    handles = await side.created(BULK, -300.0)
    row, _ = await side.per_call("entity_delete", [{"handle": x} for x in handles])
    out["delete_singles_200"] = row

    h.stage("delete_many_200")

    async def delete_args():
        return {"handles": await side.created(BULK, -400.0)}

    out["delete_many_200"] = await side.repeated("entity_delete_many", delete_args, reps)

    h.stage("cad_batch_50")

    async def cad_batch_args():
        steps = [
            {
                "tool": "entity_create_circle",
                "args": {"cx": float(i) * 3.0, "cy": -500.0, "radius": 1.0},
            }
            for i in range(BATCH_STEPS)
        ]
        return {"steps": steps}

    async def drop_steps(payload):
        rows = (payload or {}).get("results") or []
        found = []
        for row in rows:
            result = row.get("result") if isinstance(row, dict) else None
            handle = (result or {}).get("handle") if isinstance(result, dict) else None
            handle = handle or (row.get("handle") if isinstance(row, dict) else None)
            if handle:
                found.append(handle)
        await side.cleanup(found)

    out["cad_batch_50"] = await side.repeated("cad_batch", cad_batch_args, reps, after=drop_steps)

    for label, extra in (("fillet_50_n5000", 0), ("fillet_50_n10000", SEED_LINES)):
        h.stage(label)
        if extra:
            await _seed_lines(h, name, extra, float(SEED_LINES) + 10.0)
        pairs = await _corners(h, name, FILLETS, -1000.0 - float(extra))
        row, _ = await side.per_call(
            "entity_fillet",
            [{"handle1": a, "handle2": b, "radius": 2.0} for a, b in pairs],
        )
        out[label] = row
    return out


async def measure_side(code_root: Path, reps: int, json_path: Path) -> int:
    """One side: ``code_root``'s backends / server under this checkout's harness."""
    sys.path.insert(0, str(code_root))
    harness = _load_harness()

    async def body(h) -> dict:
        from fastmcp import Client

        import server

        name = await h.new_document()
        h.stage("seed")
        count = await _seed_lines(h, name, SEED_LINES, 0.0)
        h.check(
            "the document holds the seeded lines",
            count >= SEED_LINES,
            expected=SEED_LINES,
            observed=count,
        )
        backend = h.backend

        async def make():
            return backend

        async def keep_connected():  # the harness disconnects, not the lifespan
            return None

        original = server._make_backend
        server._make_backend = make
        backend.disconnect = keep_connected
        try:
            async with Client(server.mcp) as client:
                workloads = await _workloads(h, _Side(h, client), name, reps)
        finally:
            server._make_backend = original
            del backend.disconnect
        for label, row in workloads.items():
            h.check(
                f"{label} ran without a tool error",
                row.get("errors") == 0,
                expected=0,
                observed=row.get("errors"),
            )
        return {"code": _code_build(code_root), "workloads": workloads}

    return await harness.run_smoke("perf_com", body, json_path=str(json_path))


# ── the orchestrator ────────────────────────────────────────────────────────


def _run_child(code_root: Path, reps: int) -> tuple[int, dict | None]:
    with tempfile.TemporaryDirectory(prefix="acadmcp_perf_com_") as scratch:
        out = Path(scratch) / "side.json"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(code_root), env.get("PYTHONPATH", "")]).rstrip(
            os.pathsep
        )
        done = subprocess.run(
            [
                sys.executable,
                str(HERE),
                "--side",
                str(code_root),
                "--reps",
                str(reps),
                "--side-json",
                str(out),
            ],
            cwd=str(code_root),
            env=env,
            check=False,
        )
        if not out.is_file():
            return done.returncode, None
        return done.returncode, json.loads(out.read_text(encoding="utf-8"))


def _side_row(result: dict | None) -> dict | None:
    if result is None:
        return None
    report = result.get("report") or {}
    return {
        "code": report.get("code"),
        "workloads": report.get("workloads"),
        "harness": {key: value for key, value in result.items() if key != "report"},
    }


def orchestrate(base: Path | None, reps: int, json_path: Path) -> int:
    current_code, current = _run_child(ROOT, reps)
    base_code, base_result = (None, None) if base is None else _run_child(base, reps)
    payload = skeleton()
    payload["sides"] = {"base": _side_row(base_result), "current": _side_row(current)}
    payload["verdict"] = verdict(payload["sides"]["base"], payload["sides"]["current"])
    codes = [current_code] + ([] if base is None else [base_code])
    if any(code == 2 for code in codes):
        payload["status"], payload["reason"] = "unavailable", "no AutoCAD to attach to"
        exit_code = 2
    elif any(code != 0 for code in codes):
        payload["status"] = "failed"
        exit_code = 1
    else:
        payload["status"] = "measured"
        missed = [k for k, row in payload["verdict"].items() if row["pass"] is False]
        payload["missed"] = missed
        exit_code = 1 if missed else 0
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload["verdict"], indent=2))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", type=Path, help="a checkout of the v1.7.0 tag to compare with")
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS, help="repetitions of a bulk call")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="where the result goes")
    parser.add_argument("--pending", action="store_true", help="write the skeleton; no AutoCAD")
    parser.add_argument("--side", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--side-json", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.side is not None:
        return asyncio.run(measure_side(args.side.resolve(), args.reps, args.side_json))
    if args.pending:
        payload = skeleton(reason="not run: AutoCAD was not open when 1.8-23 was written")
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return 0
    base = args.base.resolve() if args.base is not None else None
    return orchestrate(base, max(1, args.reps), args.json)


if __name__ == "__main__":
    raise SystemExit(main())
