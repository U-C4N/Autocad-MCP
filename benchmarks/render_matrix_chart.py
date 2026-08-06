"""Render the live-run task matrix (tasks x servers) as a deterministic SVG.

Input: the published per-server reports under ``benchmarks/results/published/``
(the same files the score chart uses). Output: a status heatmap — every task
against every server — so coverage gaps are visible per capability instead of
being folded into one number.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from benchmarks.tasks_v3 import TASKS_V3

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "benchmarks" / "results" / "published"
DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "autocad-mcp-taskmatrix.svg"

STATUS_COLORS = {
    "pass": "#238636",
    "partial": "#d29922",
    "fail": "#da3633",
    "timeout": "#a40e26",
    "unsupported": "#30363d",
    "not_run": "#161b22",
}

STATUS_GLYPHS = {
    "pass": "P",
    "partial": "~",
    "fail": "F",
    "timeout": "T",
    "unsupported": "-",
    "not_run": "?",
}


def load_reports(input_dir: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(input_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != "2.0":
            continue  # perf reports and other lanes live in the same folder
        reports.append(data)
    if not reports:
        raise SystemExit(f"no task-matrix reports found in {input_dir}")
    # our server first, then alphabetical
    reports.sort(key=lambda item: (item["adapter"] != "autocad-mcp-pro", item["adapter"]))
    return reports


def render_chart(reports: list[dict[str, Any]], output: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch, Rectangle
    except ImportError as exc:
        raise SystemExit(
            'Matplotlib is required. Install it with: pip install -e ".[pdf]"'
        ) from exc

    matplotlib.rcParams["svg.hashsalt"] = "autocad-mcp-taskmatrix-v1"

    task_ids = [task.task_id for task in TASKS_V3]
    task_labels = [f"{task.task_id}  ({task.category})" for task in TASKS_V3]
    servers = [report["adapter"] for report in reports]
    status_by_server = {
        report["adapter"]: {item["task_id"]: item["status"] for item in report["results"]}
        for report in reports
    }

    fig, ax = plt.subplots(
        figsize=(3.4 + 2.4 * len(servers), 1.8 + 0.52 * len(task_ids)),
        constrained_layout=True,
    )
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    for row, task_id in enumerate(task_ids):
        for col, server in enumerate(servers):
            status = status_by_server[server].get(task_id, "not_run")
            color = STATUS_COLORS.get(status, "#f6f8fa")
            ax.add_patch(
                Rectangle(
                    (col + 0.06, row + 0.08),
                    0.88,
                    0.84,
                    facecolor=color,
                    edgecolor="#30363d",
                    linewidth=0.8,
                )
            )
            glyph_color = "#ffffff" if status in ("pass", "fail", "timeout") else "#e6edf3"
            ax.text(
                col + 0.5,
                row + 0.5,
                STATUS_GLYPHS.get(status, "?"),
                ha="center",
                va="center",
                fontsize=11,
                fontweight="bold",
                color=glyph_color,
            )

    ax.set_xlim(0, len(servers))
    ax.set_ylim(len(task_ids), 0)
    ax.set_xticks([index + 0.5 for index in range(len(servers))])
    ax.set_xticklabels(servers, color="#e6edf3", fontsize=10, fontweight="bold")
    ax.xaxis.set_ticks_position("top")
    ax.set_yticks([index + 0.5 for index in range(len(task_ids))])
    ax.set_yticklabels(task_labels, color="#e6edf3", fontsize=9)
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_title(
        "Task matrix — live headless lane",
        loc="left",
        fontsize=17,
        fontweight="bold",
        color="#e6edf3",
        pad=34,
    )

    legend_handles = [
        Patch(facecolor=STATUS_COLORS["pass"], label="pass"),
        Patch(facecolor=STATUS_COLORS["partial"], label="partial"),
        Patch(facecolor=STATUS_COLORS["fail"], label="fail"),
        Patch(facecolor=STATUS_COLORS["unsupported"], label="unsupported (no equivalent)"),
        # Real rows now, not a theoretical state: the competitor reports were
        # pinned and run against the v2 matrix, so the five v3 tasks were
        # never put to them. An invented zero would look identical to a
        # measured one, so they stay blank and say why.
        Patch(facecolor=STATUS_COLORS["not_run"], edgecolor="#30363d", label="not run"),
    ]
    ax.legend(
        handles=legend_handles,
        loc="upper left",
        bbox_to_anchor=(0, -0.02),
        ncol=5,
        frameon=False,
        fontsize=9,
        labelcolor="#e6edf3",
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        output,
        format="svg",
        facecolor=fig.get_facecolor(),
        metadata={"Creator": "AutoCAD MCP Pro task-matrix renderer", "Date": None},
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
    print(f"Rendered task-matrix chart: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
