"""1.7.0 must not be slower than a base release: paired, alternating, calibrated.

Builds a ``git worktree`` of ``--base`` (a tag or commit), then measures every
workload on both trees in alternating order — base, head, head, base, ... — so
drift on the machine (thermal state, another process) lands on both sides
alike. Each measurement is one subprocess per tree that imports *that tree's*
packages (``PYTHONPATH`` = the tree, ``-P`` so this file's own directory is not
on the path) and runs the workloads of THIS checkout's ``perf_suite.py``, so
both trees run the same workload code.

Workloads: ``perf_suite``'s four (they drive the backend directly) plus
``server_create_lines_2k`` — 2,000 ``entity_create_line`` calls through an
in-memory FastMCP ``Client``, so the middleware and the tool layer are
measured too (about 7 s per measurement on the maintainer's machine).

``--aa`` adds an A/A calibration: the base tree is measured a second time in
every round, and the spread of base/base ratios is the noise floor. The gate
(spec §8.3): for every workload the median of the paired per-round ratios
head/base is at most ``1 + max(0.05, 2 × aa_spread)``. Without ``--aa`` the
spread is taken as 0 and the bound is 1.05; the phase gate runs with ``--aa``.

    python -m benchmarks.perf_compare --base v1.6.0 --aa --out out/perf/compare.json
    python -m benchmarks.perf_compare --aa      # --base auto: the previous release tag

``--base auto`` (the default since 1.8-14; it was a hard-coded ``v1.6.0``)
resolves to the newest tag reachable from HEAD, or from HEAD's parent when HEAD
is itself the tag being released.

**The same catalog on both sides.** Every measurement runs with
``DISCOVERY_MODE=off``, the 1.7.0 catalog. The in-memory client is ``mcp``'s
own session, and that session lists the catalog again on every call of a tool
it was not shown (``ClientSession._validate_tool_result``); under the search
default (1.8-11) every tool but the pinned ones is such a tool, so the server
workload would measure the client's listings, not the server's call path.
MEASURED 2026-11-05 (release tree 19ada05, 20 rounds): 8.5 x v1.7.0 under
the search default - six listings a call, three of them the server's own
definition-cache refreshes - against 0.96 x with ``DISCOVERY_MODE=off``.
The server's share is fixed (``server._install_tool_definition_cache``,
``discovery/transform.py`` "Built once"); the client's listings are the
client's, and the gate compares the call path under one catalog shape.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PERF_SUITE = HERE / "perf_suite.py"

SUITE_WORKLOADS = ("create_lines_2k", "roundtrip_10k", "region_query_10k", "premium_pass")
SERVER_WORKLOAD = "server_create_lines_2k"
WORKLOADS = (*SUITE_WORKLOADS, SERVER_WORKLOAD)
MIN_MARGIN = 0.05


# ── the gate arithmetic (pure) ──────────────────────────────────────────────


def paired_ratio(numerators: list[float], denominators: list[float]) -> float:
    """Median of the per-round ratios ``numerators[i] / denominators[i]``."""
    if not numerators or len(numerators) != len(denominators):
        raise ValueError("paired_ratio needs two non-empty lists of the same length")
    return statistics.median(n / d for n, d in zip(numerators, denominators, strict=True))


def bound_for(aa_spread: float) -> float:
    """``1 + max(0.05, 2 × aa_spread)`` — the spec §8.3 ceiling on head/base."""
    return 1.0 + max(MIN_MARGIN, 2.0 * aa_spread)


def summarize(
    base: dict[str, list[float]],
    head: dict[str, list[float]],
    base_again: dict[str, list[float]] | None,
    *,
    base_ref: str,
    head_ref: str,
    warmup: int,
    rounds: int,
) -> dict:
    """The JSON report from per-round wall times in milliseconds."""
    aa_spread = 0.0
    if base_again is not None:
        aa_spread = max(abs(paired_ratio(base_again[w], base[w]) - 1.0) for w in base)
    bound = bound_for(aa_spread)
    workloads = {
        name: {
            "base_ms": round(statistics.median(base[name]), 3),
            "head_ms": round(statistics.median(head[name]), 3),
            "ratio": round(paired_ratio(head[name], base[name]), 4),
        }
        for name in base
    }
    return {
        "schema_version": 1,
        "base": base_ref,
        "head": head_ref,
        "warmup": warmup,
        "rounds": rounds,
        "aa": base_again is not None,
        "workloads": workloads,
        "aa_spread": round(aa_spread, 4),
        "bound": round(bound, 4),
        "ok": all(row["ratio"] <= bound for row in workloads.values()),
    }


# ── one measurement, inside a tree (the --worker side) ──────────────────────


def _load_suite():
    """This checkout's perf_suite, importing the *tree's* backends."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("acadmcp_perf_suite", PERF_SUITE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


async def _server_create_lines_2k() -> float:
    os.environ["AUTOCAD_MCP_BACKEND"] = "ezdxf"
    import logging

    from fastmcp import Client

    import server

    logging.disable(logging.INFO)  # 4,000 audit lines would be measured too
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        started = time.perf_counter()
        for index in range(2_000):
            y = float(index % 100)
            await client.call_tool(
                "entity_create_line", {"x1": 0.0, "y1": y, "x2": 100.0, "y2": y + 1.0}
            )
        return (time.perf_counter() - started) * 1000.0


async def measure(workloads: list[str], scratch: Path) -> dict[str, float]:
    """Wall milliseconds per workload, measured in this process."""
    suite = _load_suite()
    out: dict[str, float] = {}
    backend = suite.EzdxfBackend()
    await backend.connect()
    try:
        runners = {
            "create_lines_2k": lambda: suite.workload_create_lines_2k(backend),
            "roundtrip_10k": lambda: suite.workload_roundtrip_10k(backend, scratch),
            "region_query_10k": lambda: suite.workload_region_query_10k(backend),
            "premium_pass": lambda: suite.workload_premium_pass(backend),
        }
        for name in workloads:
            if name == SERVER_WORKLOAD:
                continue
            out[name] = float((await runners[name]()).wall_ms)
    finally:
        await backend.disconnect()
    if SERVER_WORKLOAD in workloads:
        out[SERVER_WORKLOAD] = await _server_create_lines_2k()
    return out


def run_in_tree(tree: Path, workloads: list[str], scratch: Path) -> dict[str, float]:
    """One measurement of ``workloads`` with ``tree``'s packages, in a subprocess."""
    # DISCOVERY_MODE=off on both sides: the module docstring says why.
    env = {
        **os.environ,
        "PYTHONPATH": str(tree),
        "AUTOCAD_MCP_BACKEND": "ezdxf",
        "DISCOVERY_MODE": "off",
    }
    done = subprocess.run(
        [
            sys.executable,
            "-P",
            str(Path(__file__).resolve()),
            "--worker",
            "--workloads",
            ",".join(workloads),
            "--scratch",
            str(scratch),
        ],
        cwd=str(tree),
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if done.returncode != 0:
        raise RuntimeError(f"measurement in {tree} failed:\n{done.stderr[-4000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


# ── orchestration ───────────────────────────────────────────────────────────


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


#: ``--base`` value that resolves to the previous release tag (1.8-14).
AUTO_BASE = "auto"


def resolve_base(base: str, git=_git) -> str:
    """``auto`` -> the previous release tag; anything else is used as given.

    The previous tag is the newest tag reachable from HEAD, or - when HEAD is
    itself tagged (a release run) - the newest one reachable from its parent,
    so a tag is never compared with itself. No tag at all is refused by name.
    """
    if base != AUTO_BASE:
        return base
    try:
        exact = git("tag", "--points-at", "HEAD")
        start = "HEAD^" if exact.strip() else "HEAD"
        return git("describe", "--tags", "--abbrev=0", start)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "--base auto: no release tag is reachable from HEAD (git describe --tags failed); "
            "fetch the tags (actions/checkout fetch-depth: 0) or pass --base <tag>"
        ) from exc


def compare(
    base_ref: str, *, warmup: int, rounds: int, aa: bool, workloads: list[str], scratch: Path
) -> dict:
    head_ref = _git("rev-parse", "--short", "HEAD")
    tree = Path(tempfile.mkdtemp(prefix="acadmcp_perf_base_"))
    _git("worktree", "add", "--detach", "--quiet", str(tree), base_ref)
    samples: dict[str, dict[str, list[float]]] = {
        side: {w: [] for w in workloads} for side in ("base", "head", "base_again")
    }
    try:
        for index in range(warmup + rounds):
            order = [("base", tree), ("head", REPO)]
            if aa:
                order.append(("base_again", tree))
            if index % 2:
                order.reverse()
            measured = {side: run_in_tree(where, workloads, scratch) for side, where in order}
            if index < warmup:
                continue
            for side, values in measured.items():
                for name, value in values.items():
                    samples[side][name].append(value)
            print(f"round {index - warmup + 1}/{rounds} done", file=sys.stderr, flush=True)
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(tree)])
    return summarize(
        samples["base"],
        samples["head"],
        samples["base_again"] if aa else None,
        base_ref=base_ref,
        head_ref=head_ref,
        warmup=warmup,
        rounds=rounds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default=AUTO_BASE,
        help="git ref to compare against; 'auto' (default) = the previous release tag",
    )
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--aa", action="store_true", help="measure base twice per round")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--workloads", default=",".join(WORKLOADS))
    parser.add_argument("--scratch", type=Path, default=Path("out/perf-scratch"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workloads = [w for w in args.workloads.split(",") if w]
    unknown = sorted(set(workloads) - set(WORKLOADS))
    if unknown:
        raise SystemExit(f"unknown workload(s) {unknown}; known: {list(WORKLOADS)}")
    args.scratch.mkdir(parents=True, exist_ok=True)
    if args.worker:
        print(json.dumps(asyncio.run(measure(workloads, args.scratch.resolve()))))
        return 0
    base = resolve_base(args.base)
    print(f"base: {base}", file=sys.stderr, flush=True)
    report = compare(
        base,
        warmup=args.warmup,
        rounds=args.rounds,
        aa=args.aa,
        workloads=workloads,
        scratch=args.scratch.resolve(),
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
