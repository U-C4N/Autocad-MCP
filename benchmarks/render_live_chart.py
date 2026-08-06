"""Render the live-run benchmark lane (v2 fixed-task matrix) as a deterministic SVG.

Input: the published per-server reports under ``benchmarks/results/published/``
(produced by ``benchmarks.run_competitors``, artifact paths sanitized to
filenames). Output: a GitHub-readable horizontal bar chart showing the
weighted score and task coverage per server.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "benchmarks" / "results" / "published"
DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "autocad-mcp-livebench.svg"


def load_reports(input_dir: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(input_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != "2.0":
            continue  # other lanes (e.g. perf/1.0) share the published folder
        summary = data.get("summary") or {}
        if "score" not in summary or "coverage_percent" not in summary:
            raise ValueError(f"{path.name}: summary is missing score/coverage")
        reports.append(data)
    if not reports:
        raise SystemExit(f"no published reports found in {input_dir}")
    return reports


def render_chart(reports: list[dict[str, Any]], output: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit(
            'Matplotlib is required. Install it with: pip install -e ".[pdf]"'
        ) from exc

    matplotlib.rcParams["svg.hashsalt"] = "autocad-mcp-livebench-v2"
    reports = sorted(reports, key=lambda item: item["summary"]["score"])
    labels = [item["adapter"] for item in reports]
    scores = [item["summary"]["score"] for item in reports]
    coverage = [item["summary"]["coverage_percent"] for item in reports]
    passed = [item["summary"]["passed"] for item in reports]
    attempted = [item["summary"]["attempted"] for item in reports]
    # Which task set each server actually ran. Not decoration: since v1.5.0
    # the reference adapter runs the 15-task v3 matrix while the pinned
    # competitor reports are v2 runs from v1.4, so the bars do not share a
    # denominator and a chart that implies they do is the lie this lane
    # exists to avoid.
    matrices = [item.get("matrix", "v2") for item in reports]
    colors = ["#58a6ff" if label == "autocad-mcp-pro" else "#484f58" for label in labels]

    fig, ax = plt.subplots(figsize=(12, 4.8), constrained_layout=True)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")
    bars = ax.barh(labels, scores, color=colors, height=0.58)

    ax.set_xlim(0, 100)
    ax.set_xlabel(
        "Weighted live-run score / 100, within each server's own task matrix",
        color="#e6edf3",
        labelpad=10,
    )
    ax.set_title(
        "Live-run benchmark - headless ezdxf lane",
        loc="left",
        fontsize=18,
        fontweight="bold",
        color="#e6edf3",
        pad=28,
    )
    ax.text(
        0,
        1.02,
        "Same harness, pinned competitor commits; artifacts verified by re-opening the DXF. "
        "Matrices differ - read the per-bar label.",
        transform=ax.transAxes,
        color="#8b949e",
        fontsize=10,
    )

    for bar, score, cov, ok, total, matrix in zip(
        bars, scores, coverage, passed, attempted, matrices, strict=True
    ):
        ax.text(
            min(score + 1.2, 76),
            bar.get_y() + bar.get_height() / 2,
            f"{score:g}  ({ok}/{total} pass, {cov:g}% coverage, matrix {matrix})",
            va="center",
            color="#e6edf3",
            fontweight="bold",
        )

    ax.text(
        0,
        -0.22,
        "Unsupported tasks score 0 within a server's own matrix. The five v3 tasks were "
        "added after the competitor runs and were never put to them; per-task statuses are "
        "in benchmarks/results/published/.",
        transform=ax.transAxes,
        color="#8b949e",
        fontsize=9,
    )
    ax.grid(axis="x", color="#30363d", linewidth=0.8, alpha=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", colors="#8b949e")
    ax.tick_params(axis="y", colors="#e6edf3", length=0, pad=8)
    for spine in ax.spines.values():
        spine.set_visible(False)

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        output,
        format="svg",
        facecolor=fig.get_facecolor(),
        metadata={"Creator": "AutoCAD MCP Pro live benchmark renderer", "Date": None},
    )
    plt.close(fig)

    svg = output.read_text(encoding="utf-8")
    normalized = "\n".join(line.rstrip() for line in svg.splitlines()) + "\n"
    output.write_text(normalized, encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    render_chart(load_reports(args.input_dir), args.output)
    print(f"Rendered live benchmark chart: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
