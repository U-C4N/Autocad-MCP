"""Render the source-reviewed AutoCAD MCP benchmark as a deterministic SVG."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "benchmarks" / "source_review.json"
DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "autocad-mcp-benchmark.svg"
REQUIRED_PROJECT_KEYS = {
    "repository",
    "url",
    "reviewed_version",
    "score",
    "evidence_grade",
    "note",
}


def load_benchmark(path: Path) -> dict[str, Any]:
    """Load and validate the benchmark snapshot used by docs and tests."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0":
        raise ValueError("unsupported benchmark schema_version")
    if data.get("evidence_boundary") != "source_review_not_shared_live_run":
        raise ValueError("benchmark evidence boundary must be explicit")

    rubric = data.get("rubric")
    if not isinstance(rubric, list) or sum(item.get("weight", 0) for item in rubric) != 100:
        raise ValueError("benchmark rubric weights must total 100")

    projects = data.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("benchmark must contain projects")

    seen: set[str] = set()
    for project in projects:
        missing = REQUIRED_PROJECT_KEYS - project.keys()
        if missing:
            raise ValueError(f"project is missing keys: {sorted(missing)}")
        repository = project["repository"]
        if repository in seen:
            raise ValueError(f"duplicate repository: {repository}")
        seen.add(repository)
        if not 0 <= project["score"] <= 100:
            raise ValueError(f"score outside 0..100: {repository}")
        if project["evidence_grade"] not in {"A", "B", "C"}:
            raise ValueError(f"invalid evidence grade: {repository}")
    return data


def render_chart(data: dict[str, Any], output: Path) -> None:
    """Render a GitHub-readable fixed-background horizontal bar chart."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit(
            'Matplotlib is required. Install it with: pip install -e ".[pdf]"'
        ) from exc

    matplotlib.rcParams["svg.hashsalt"] = "autocad-mcp-source-review-v1"
    projects = sorted(data["projects"], key=lambda item: item["score"])
    total = len(projects)
    # rank 1 = highest score (drawn topmost by barh with ascending sort)
    labels = [f"{total - index}. {item['repository']}" for index, item in enumerate(projects)]
    scores = [item["score"] for item in projects]
    grades = [item["evidence_grade"] for item in projects]
    # leaderboard palette by rank (rank 1 first), canvas/LLM-benchmark style
    rank_palette = [
        "#3fb950",  # 1 green
        "#58a6ff",  # 2 sky
        "#6e7ce0",  # 3 indigo
        "#d29922",  # 4 amber
        "#c4622d",  # 5 rust
        "#e3b341",  # 6 yellow
        "#bf4b8a",  # 7 magenta
        "#f0876e",  # 8 salmon
        "#8957e5",  # 9 purple
    ]
    colors = [rank_palette[(total - 1 - index) % len(rank_palette)] for index in range(total)]

    fig, ax = plt.subplots(figsize=(12, 0.62 * total + 2.4), constrained_layout=True)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")
    bars = ax.barh(labels, scores, color=colors, height=0.62)

    ax.set_xlim(0, 100)
    ax.set_xlabel("Weighted capability score / 100", color="#e6edf3", labelpad=10)
    ax.set_title(
        "Public AutoCAD MCP leaderboard — Source-reviewed capability benchmark",
        loc="left",
        fontsize=15,
        fontweight="bold",
        color="#e6edf3",
        pad=28,
    )
    ax.text(
        0,
        1.02,
        f"Fixed rubric · reviewed {data['reviewed_at']} · evidence grade shown per project",
        transform=ax.transAxes,
        color="#8b949e",
        fontsize=10,
    )

    for bar, score, grade in zip(bars, scores, grades, strict=True):
        ax.text(
            min(score + 1.2, 88),
            bar.get_y() + bar.get_height() / 2,
            f"{score} / 100  ·  {grade}",
            va="center",
            color="#e6edf3",
            fontweight="bold",
        )

    ax.text(
        0,
        -0.17,
        "Shared live-run results are not implied. Scores reflect public-source and recorded test evidence.",
        transform=ax.transAxes,
        color="#8b949e",
        fontsize=9,
    )
    ax.grid(axis="x", color="#30363d", linewidth=0.8, alpha=0.9)
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
        metadata={"Creator": "AutoCAD MCP Pro benchmark renderer", "Date": None},
    )
    plt.close(fig)

    svg = output.read_text(encoding="utf-8")
    normalized = "\n".join(line.rstrip() for line in svg.splitlines()) + "\n"
    output.write_text(normalized, encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    render_chart(load_benchmark(args.input), args.output)
    print(f"Rendered benchmark chart: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
