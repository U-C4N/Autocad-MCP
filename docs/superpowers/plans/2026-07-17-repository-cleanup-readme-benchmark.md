# Repository Cleanup, README, and Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove private/generated repository noise and publish a concise English README with an honest, reproducible Matplotlib benchmark graphic naming major AutoCAD MCP alternatives.

**Architecture:** Keep benchmark evidence, rendering, and presentation separate: a schema-versioned JSON snapshot is the source of truth, a focused Python module validates and renders it, and README/benchmark documentation explains the evidence boundary. Repository cleanup uses explicit, workspace-confined PowerShell targets so current version 1.3 source work remains untouched.

**Tech Stack:** Python 3.11+, JSON, Matplotlib SVG backend, pytest, Ruff, PowerShell, Markdown.

---

## File Map

- Modify `.gitignore`: remove private-draft names; add general cache/test/output rules.
- Delete untracked `b.md`, `new.md`, and `p.md` only when present.
- Delete ignored/generated workspace artifacts listed in Task 1; no tracked product code.
- Create `benchmarks/source_review.json`: dated source-review snapshot and rubric metadata.
- Create `benchmarks/render_chart.py`: validate the snapshot and render deterministic SVG.
- Create `tests/test_benchmark_chart.py`: data-contract and SVG-rendering coverage.
- Create `docs/assets/autocad-mcp-benchmark.svg`: generated benchmark image.
- Modify `benchmarks/README.md`: explain runtime versus source-review evidence and reproduction.
- Replace `README.md`: concise public product narrative and embedded benchmark.

### Task 1: Clean the workspace without touching product work

**Files:**
- Modify: `.gitignore:1-44`
- Delete if present: `b.md`
- Delete if present: `new.md`
- Delete if present: `p.md`
- Delete local artifacts: `.codex-pytest-temp/`, `.pytest_cache/`, `.release-*/`, `.tdd-*/`, `.ruff_cache/`, `**/__pycache__/`, `dist/`, `out/`, `.coverage`, and an empty `.firecrawl/`

- [ ] **Step 1: Capture the pre-cleanup product status**

Run:

```powershell
git status --porcelain=v1 -uall
```

Expected: existing version 1.3 modifications and untracked product files are visible. Keep this output available for comparison; do not stage or reset any of them.

- [ ] **Step 2: Replace draft-specific ignores with general generated-output rules**

Apply this exact `.gitignore` structure while preserving the existing environment, IDE, and OS sections:

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
*.egg-info/
*.egg
dist/
build/
.eggs/

# Virtual environments
.venv/
venv/
env/

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# Testing and tooling
.pytest_cache/
.ruff_cache/
.codex-pytest-temp/
.release-*/
.tdd-*/
.coverage
coverage.xml
htmlcov/

# Environment
.env

# OS
.DS_Store
Thumbs.db
desktop.ini

# Generated CAD and benchmark output
out/
benchmarks/results/*
!benchmarks/results/.gitkeep
*.dxf
*.dwg
*.bak
```

Expected: `b.md` and `new.md` no longer appear in `.gitignore`; generated artifacts remain ignored by category.

- [ ] **Step 3: Resolve and print every deletion target before deleting**

Run this PowerShell from the repository root:

```powershell
$repoRoot = (Resolve-Path -LiteralPath '.').Path.TrimEnd('\')
$rootNames = @(
    '.codex-pytest-temp', '.pytest_cache', '.ruff_cache',
    'dist', 'out'
)

$targets = @()
foreach ($name in $rootNames) {
    $candidate = Join-Path $repoRoot $name
    if (Test-Path -LiteralPath $candidate) {
        $targets += Get-Item -Force -LiteralPath $candidate
    }
}

$targets += Get-ChildItem -Force -Directory -LiteralPath $repoRoot |
    Where-Object { $_.Name -like '.release-*' -or $_.Name -like '.tdd-*' }
$targets += Get-ChildItem -Force -Directory -Recurse -LiteralPath $repoRoot -Filter '__pycache__' -ErrorAction SilentlyContinue
$targets = $targets | Sort-Object FullName -Unique

foreach ($target in $targets) {
    $full = [IO.Path]::GetFullPath($target.FullName)
    $insideRepo = $full.StartsWith(
        $repoRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
    if (-not $insideRepo -or $full -eq $repoRoot) {
        throw "Unsafe deletion target: $full"
    }
    [pscustomobject]@{ Type = 'directory'; Path = $full }
}

foreach ($name in @('b.md', 'new.md', 'p.md', '.coverage')) {
    $candidate = Join-Path $repoRoot $name
    if (Test-Path -LiteralPath $candidate) {
        $full = [IO.Path]::GetFullPath($candidate)
        [pscustomobject]@{ Type = 'file'; Path = $full }
    }
}

$firecrawl = Join-Path $repoRoot '.firecrawl'
if (Test-Path -LiteralPath $firecrawl) {
    $children = @(Get-ChildItem -Force -LiteralPath $firecrawl)
    if ($children.Count -eq 0) {
        [pscustomobject]@{ Type = 'empty-directory'; Path = $firecrawl }
    } else {
        Write-Warning '.firecrawl is not empty and will be preserved.'
    }
}
```

Expected: every printed path begins with the resolved repository root; no `backends`, `engineering`, `benchmarks`, `tests`, or source file is listed.

- [ ] **Step 4: Delete only the verified targets**

In the same PowerShell session, after Step 3 has printed a safe list:

```powershell
$targets |
    Sort-Object { $_.FullName.Length } -Descending |
    ForEach-Object { Remove-Item -Force -Recurse -LiteralPath $_.FullName }

foreach ($name in @('b.md', 'new.md', 'p.md', '.coverage')) {
    $candidate = Join-Path $repoRoot $name
    if (Test-Path -LiteralPath $candidate) {
        Remove-Item -Force -LiteralPath $candidate
    }
}

$firecrawl = Join-Path $repoRoot '.firecrawl'
if (Test-Path -LiteralPath $firecrawl) {
    $children = @(Get-ChildItem -Force -LiteralPath $firecrawl)
    if ($children.Count -eq 0) {
        Remove-Item -Force -LiteralPath $firecrawl
    }
}
```

Expected: all intended local artifacts are absent; a non-empty `.firecrawl/` is preserved.

- [ ] **Step 5: Verify hygiene and preservation**

Run:

```powershell
git status --porcelain=v1 -uall
git check-ignore -v -- .ruff_cache/sample .codex-pytest-temp/sample .release-check/sample .tdd-check/sample out/sample.dxf
rg -n '^(b\.md|new\.md|p\.md)$' .gitignore
```

Expected: pre-existing product changes remain; the first five generated paths report matching ignore rules; `rg` returns exit code 1 because no private draft name is ignored.

- [ ] **Step 6: Commit only repository hygiene**

```powershell
git add -- .gitignore
git commit -m "chore: remove local artifact noise"
```

Expected: the commit contains only `.gitignore`. The ignored/untracked deletions do not accidentally stage product files.

### Task 2: Define the benchmark contract with failing tests

**Files:**
- Create: `tests/test_benchmark_chart.py`
- Test: `tests/test_benchmark_chart.py`

- [ ] **Step 1: Write data-contract and renderer tests**

Create `tests/test_benchmark_chart.py` with:

```python
import json
from pathlib import Path

import pytest

from benchmarks.render_chart import load_benchmark, render_chart


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "benchmarks" / "source_review.json"


def test_source_review_contract_is_complete() -> None:
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data = load_benchmark(DATA_PATH)

    assert data == raw
    assert data["schema_version"] == "1.0"
    assert sum(item["weight"] for item in data["rubric"]) == 100
    assert data["evidence_boundary"] == "source_review_not_shared_live_run"

    projects = data["projects"]
    repositories = [item["repository"] for item in projects]
    assert len(repositories) == len(set(repositories))
    assert "U-C4N/Autocad-MCP" in repositories
    assert "varavista/autocad-mcp" in repositories
    assert "beiming183-cloud/AutoCAD-MCP" in repositories
    assert "AnCode666/multiCAD-mcp" in repositories
    assert "puran-water/autocad-mcp" in repositories
    assert "NCO-1986/AutoCAD_mcp" in repositories
    assert all(0 <= item["score"] <= 100 for item in projects)
    assert all(item["evidence_grade"] in {"A", "B", "C"} for item in projects)


def test_load_benchmark_rejects_duplicate_repositories(tmp_path: Path) -> None:
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    payload["projects"].append(payload["projects"][0].copy())
    invalid = tmp_path / "duplicate.json"
    invalid.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate repository"):
        load_benchmark(invalid)


def test_render_chart_writes_readable_svg(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    data = load_benchmark(DATA_PATH)
    output = tmp_path / "benchmark.svg"

    render_chart(data, output)

    svg = output.read_text(encoding="utf-8")
    assert svg.lstrip().startswith("<?xml")
    assert "Source-reviewed capability benchmark" in svg
    assert "U-C4N/Autocad-MCP" in svg
    assert "Shared live-run results are not implied" in svg
```

- [ ] **Step 2: Run the tests to verify the contract is missing**

Run:

```powershell
python -m pytest tests/test_benchmark_chart.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'benchmarks.render_chart'`.

- [ ] **Step 3: Commit the failing contract test**

```powershell
git add -- tests/test_benchmark_chart.py
git commit -m "test: define benchmark chart contract"
```

Expected: only the new test is committed.

### Task 3: Add reviewed benchmark data and deterministic Matplotlib rendering

**Files:**
- Create: `benchmarks/source_review.json`
- Create: `benchmarks/render_chart.py`
- Create: `docs/assets/autocad-mcp-benchmark.svg`
- Test: `tests/test_benchmark_chart.py`

- [ ] **Step 1: Add the reviewed source snapshot**

Create `benchmarks/source_review.json` with the six-project snapshot below. The AutoCAD MCP Pro score is the prior audited 88 plus five rubric points for version 1.3's TABLE/MLEADER surface, closed-loop refiner and delivery verification, and capability reporting. The other totals retain the 2026-07-16 audited snapshot; the file explicitly records the evidence boundary.

```json
{
  "schema_version": "1.0",
  "reviewed_at": "2026-07-17",
  "title": "AutoCAD MCP source-reviewed capability benchmark",
  "evidence_boundary": "source_review_not_shared_live_run",
  "methodology": "Public source, documented behavior, and locally recorded test evidence scored with a fixed 100-point rubric. Repository stars and raw tool counts do not score points.",
  "rubric": [
    {"category": "Functional CAD coverage", "weight": 25},
    {"category": "Correctness and delivery", "weight": 20},
    {"category": "Backends and platforms", "weight": 15},
    {"category": "Engineering production", "weight": 15},
    {"category": "Tests and maintenance", "weight": 15},
    {"category": "Security and operations", "weight": 10}
  ],
  "projects": [
    {
      "repository": "U-C4N/Autocad-MCP",
      "url": "https://github.com/U-C4N/Autocad-MCP",
      "reviewed_version": "1.3.0 working tree",
      "score": 93,
      "evidence_grade": "A",
      "note": "Dual COM/ezdxf backends, standards-aware quality gates, deterministic geometry, closed-loop refinement, and auditable delivery."
    },
    {
      "repository": "varavista/autocad-mcp",
      "url": "https://github.com/varavista/autocad-mcp",
      "reviewed_version": "2026-07-16 snapshot",
      "score": 84,
      "evidence_grade": "A",
      "note": "Broad backend and CAD coverage with layout and xref depth."
    },
    {
      "repository": "beiming183-cloud/AutoCAD-MCP",
      "url": "https://github.com/beiming183-cloud/AutoCAD-MCP",
      "reviewed_version": "2026-07-16 snapshot",
      "score": 83,
      "evidence_grade": "A",
      "note": "Atomic operations, postconditions, 3D tools, and verified artifact delivery."
    },
    {
      "repository": "AnCode666/multiCAD-mcp",
      "url": "https://github.com/AnCode666/multiCAD-mcp",
      "reviewed_version": "2026-07-16 snapshot",
      "score": 73,
      "evidence_grade": "A",
      "note": "TABLE/MLEADER, data exchange, multi-document workflows, and alternative CAD support."
    },
    {
      "repository": "puran-water/autocad-mcp",
      "url": "https://github.com/puran-water/autocad-mcp",
      "reviewed_version": "2026-07-16 snapshot",
      "score": 70,
      "evidence_grade": "A",
      "note": "AutoCAD LT file IPC, ezdxf, P&ID workflows, leaders, and screenshots."
    },
    {
      "repository": "NCO-1986/AutoCAD_mcp",
      "url": "https://github.com/NCO-1986/AutoCAD_mcp",
      "reviewed_version": "2026-07-16 snapshot",
      "score": 68,
      "evidence_grade": "B",
      "note": ".NET main-thread integration with a broad live AutoCAD operation surface."
    }
  ]
}
```

- [ ] **Step 2: Implement validation and SVG rendering**

Create `benchmarks/render_chart.py` with:

```python
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
    labels = [item["repository"] for item in projects]
    scores = [item["score"] for item in projects]
    grades = [item["evidence_grade"] for item in projects]
    colors = ["#0969da" if label == "U-C4N/Autocad-MCP" else "#6e7781" for label in labels]

    fig, ax = plt.subplots(figsize=(12, 6.4), constrained_layout=True)
    fig.patch.set_facecolor("#f6f8fa")
    ax.set_facecolor("#f6f8fa")
    bars = ax.barh(labels, scores, color=colors, height=0.62)

    ax.set_xlim(0, 100)
    ax.set_xlabel("Weighted capability score / 100", color="#24292f", labelpad=10)
    ax.set_title(
        "Source-reviewed capability benchmark",
        loc="left",
        fontsize=18,
        fontweight="bold",
        color="#24292f",
        pad=28,
    )
    ax.text(
        0,
        1.02,
        f"Fixed rubric · reviewed {data['reviewed_at']} · evidence grade shown per project",
        transform=ax.transAxes,
        color="#57606a",
        fontsize=10,
    )

    for bar, score, grade in zip(bars, scores, grades, strict=True):
        ax.text(
            min(score + 1.2, 96),
            bar.get_y() + bar.get_height() / 2,
            f"{score}  ·  {grade}",
            va="center",
            color="#24292f",
            fontweight="bold",
        )

    ax.text(
        0,
        -0.17,
        "Shared live-run results are not implied. Scores reflect public-source and recorded test evidence.",
        transform=ax.transAxes,
        color="#57606a",
        fontsize=9,
    )
    ax.grid(axis="x", color="#d0d7de", linewidth=0.8, alpha=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", colors="#57606a")
    ax.tick_params(axis="y", colors="#24292f", length=0, pad=8)
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
```

- [ ] **Step 3: Run the focused tests**

Run:

```powershell
python -m pytest tests/test_benchmark_chart.py -q
```

Expected: `3 passed` when Matplotlib is installed; otherwise the render test is skipped and the two contract tests pass.

- [ ] **Step 4: Render the committed SVG**

Run:

```powershell
python -m benchmarks.render_chart
```

Expected: `Rendered benchmark chart:` followed by `docs\assets\autocad-mcp-benchmark.svg`; the file begins with an XML declaration and contains all six project labels.

- [ ] **Step 5: Commit the benchmark data, renderer, and asset**

```powershell
git add -- benchmarks/source_review.json benchmarks/render_chart.py docs/assets/autocad-mcp-benchmark.svg
git commit -m "feat: add reproducible benchmark visualization"
```

Expected: only the dataset, renderer, and SVG are committed.

### Task 4: Document both benchmark evidence layers

**Files:**
- Modify: `benchmarks/README.md:1-67`
- Test: `tests/test_benchmark_chart.py`

- [ ] **Step 1: Add a source-review section before the runtime runner section**

Add this content after the `# Benchmarks` heading:

```markdown
## Two evidence layers

This repository publishes two deliberately separate forms of evidence:

1. **Source-reviewed capability benchmark.** Six named AutoCAD MCP projects are
   scored with a fixed 100-point rubric covering CAD breadth, correctness and
   delivery, backend reach, engineering production, tests, and security. The
   dated data lives in [`source_review.json`](source_review.json), and the README
   graphic is generated—not hand-edited—from that file.
2. **Fixed-task runtime benchmark.** Adapters execute the same ten tasks and
   return `pass`, `partial`, `unsupported`, `fail`, `timeout`, or `not_run`.
   AutoCAD MCP Pro currently has the reference adapter; other projects do not
   receive runtime scores until an adapter actually runs their public interface.

Regenerate the source-review chart:

```bash
pip install -e ".[pdf]"
python -m benchmarks.render_chart
```

The source-review chart is not presented as a shared live AutoCAD run. Review
dates, evidence grades, project URLs, and the boundary are stored with the data.
```

Keep the existing fixed-task and correctness-suite commands below it. Change the existing `## Fixed-task competitor runner v2` heading to `## Fixed-task runtime runner v2` so the distinction is visible in navigation.

- [ ] **Step 2: Verify benchmark documentation references**

Run:

```powershell
rg -n 'source_review\.json|benchmarks\.render_chart|runtime benchmark|not presented as a shared live' benchmarks/README.md
```

Expected: all four concepts appear in the updated benchmark documentation.

- [ ] **Step 3: Commit benchmark documentation**

```powershell
git add -- benchmarks/README.md
git commit -m "docs: explain benchmark evidence boundaries"
```

Expected: the commit contains only `benchmarks/README.md`.

### Task 5: Replace the public README with concise, evidence-led copy

**Files:**
- Modify: `README.md:1-585`
- Reference: `.env.example`
- Reference: `pyproject.toml`
- Reference: `version.py`
- Reference: `server.py`

- [ ] **Step 1: Collect authoritative local claims before writing**

Run:

```powershell
python -m pytest --collect-only -q
rg -c '^@mcp\.tool' server.py
rg -c '^@mcp\.resource' server.py
rg -c '^@mcp\.prompt' server.py
Get-Content -LiteralPath pyproject.toml -Raw
Get-Content -LiteralPath .env.example -Raw
```

Expected: fresh test collection and decorator counts are available. Use exact counts only in prose tied to this release; describe `system_about` as runtime-authoritative.

- [ ] **Step 2: Replace the hero, quickstart, and benchmark opening**

Use this exact opening structure and copy, substituting only the fresh test count if it is mentioned later:

```markdown
# AutoCAD MCP Pro

Production-grade AutoCAD automation for AI agents—live through COM on Windows,
or headless through ezdxf on any platform.

[![License: MIT](https://img.shields.io/badge/license-MIT-3fb950)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-1f6feb?logo=python&logoColor=white)](pyproject.toml)
[![FastMCP 3](https://img.shields.io/badge/MCP-FastMCP%203-8957e5)](https://github.com/jlowin/fastmcp)
[![Stars](https://img.shields.io/github/stars/U-C4N/Autocad-MCP?style=flat&color=e3b341)](https://github.com/U-C4N/Autocad-MCP/stargazers)

One typed MCP contract controls two execution engines. Build and edit drawings,
apply engineering standards, inspect exact geometry, refine quality issues inside
transactions, and deliver hashed artifacts with validation evidence.

## Start in 60 seconds

```bash
git clone https://github.com/U-C4N/Autocad-MCP.git
cd Autocad-MCP
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e .
python server.py
```

That starts the headless-capable stdio server. For live AutoCAD control on
Windows, install the COM extra and select the backend:

```powershell
pip install -e ".[com]"
$env:AUTOCAD_MCP_BACKEND = "com"
python server.py
```

## Evidence, not adjectives

![Source-reviewed AutoCAD MCP capability benchmark](docs/assets/autocad-mcp-benchmark.svg)

The chart is generated with Matplotlib from
[`benchmarks/source_review.json`](benchmarks/source_review.json). It applies the
same public 100-point rubric to six named projects. This is a dated source review,
not a claim that every project completed the shared live AutoCAD task runner.

```bash
pip install -e ".[pdf]"
python -m benchmarks.render_chart
python -m benchmarks.run_competitors --server autocad-mcp-pro --backend ezdxf --json
```

The second command regenerates the graphic. The third runs AutoCAD MCP Pro's
ten-task reference adapter; cross-project runtime scores will appear only after
equivalent adapters execute the same contract. See the full
[`benchmarks/` methodology](benchmarks/README.md).
```

- [ ] **Step 3: Add four proof-led differentiators and the backend table**

Use four compact subsections—`One contract, two engines`, `Geometry agents do not have to guess`, `Quality is a loop`, and `Delivery carries evidence`. Name concrete tools such as `point_from_snap`, `drawing_critique`, `drawing_refine`, `drawing_finalize`, and `drawing_deliver`; avoid comparative superlatives.

Include this backend table:

```markdown
| Capability | COM backend | ezdxf backend |
|---|:---:|:---:|
| Live AutoCAD document control | ✓ | — |
| Headless DXF creation and editing | — | ✓ |
| Cross-platform execution | — | ✓ |
| Deterministic entity/query contract | ✓ | ✓ |
| Transactions and rollback | ✓ | ✓ |
| TABLE and MLEADER semantics | Native | Portable composite |
| Screenshots | AutoCAD window | Matplotlib render |
| Raw AutoCAD commands / AutoLISP | Opt-in | — |
```

Follow it with: `Use system_capabilities at runtime instead of assuming backend support.`

- [ ] **Step 4: Add installation, MCP configuration, workflow, security, and development sections**

Keep the existing valid installation extras from `pyproject.toml`. Include one Claude Desktop JSON example using the absolute path placeholder `C:\\path\\to\\Autocad-MCP\\server.py`, and state that Cursor/Cline/Continue/Goose use the same stdio command/args shape.

Include one representative workflow, not an exhaustive catalog:

```text
drawing_preflight → drawing_plan → drawing_apply_iso_layers
→ deterministic create/edit tools → dimension_auto
→ drawing_critique → drawing_refine → drawing_finalize → drawing_deliver
```

Explain that `system_about` and `system_capabilities` provide the authoritative runtime inventory. Preserve concise configuration and security facts backed by `.env.example`, `config.py`, and `security.py`: allowed paths, HTTP bearer auth, non-loopback bind guard, dangerous command opt-in, AutoLISP allowlist, audit logging, and COM timeouts.

Use these development commands:

```bash
pip install -e ".[full]"
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build
```

End with compact `Contributing`, `Author`, and `License` sections. Link Umutcan Edizsalan and Anka-Makine only where an existing public URL is known; otherwise use plain text. Link the MIT license to `LICENSE`.

- [ ] **Step 5: Remove README anti-patterns**

Run:

```powershell
rg -ni 'the best|best AutoCAD|only one|without a single misstep|table of contents|public benchmark suite is on the way' README.md
```

Expected: no matches. Also verify the README does not reproduce the long private source-review report or claim live cross-project execution.

- [ ] **Step 6: Commit the README rewrite**

```powershell
git add -- README.md
git commit -m "docs: rebuild readme around verified evidence"
```

Expected: the commit contains only `README.md`.

### Task 6: Verify generated evidence, documentation, code quality, and workspace hygiene

**Files:**
- Modify if verification exposes a defect: `benchmarks/source_review.json`
- Modify if verification exposes a defect: `benchmarks/render_chart.py`
- Modify if verification exposes a defect: `benchmarks/README.md`
- Modify if verification exposes a defect: `README.md`
- Test: `tests/test_benchmark_chart.py`

- [ ] **Step 1: Prove chart generation is deterministic**

Run:

```powershell
$first = Join-Path $env:TEMP 'autocad-mcp-benchmark-first.svg'
$second = Join-Path $env:TEMP 'autocad-mcp-benchmark-second.svg'
python -m benchmarks.render_chart --output $first
python -m benchmarks.render_chart --output $second
$hash1 = (Get-FileHash -Algorithm SHA256 -LiteralPath $first).Hash
$hash2 = (Get-FileHash -Algorithm SHA256 -LiteralPath $second).Hash
if ($hash1 -ne $hash2) { throw "Benchmark SVG is not deterministic." }
Compare-Object (Get-Content -LiteralPath $first) (Get-Content -LiteralPath 'docs\assets\autocad-mcp-benchmark.svg')
```

Expected: hashes match and `Compare-Object` produces no output.

- [ ] **Step 2: Verify all local README links resolve**

Run:

```powershell
@'
import re
from pathlib import Path

root = Path.cwd()
missing = []
for document in (root / "README.md", root / "benchmarks" / "README.md"):
    text = document.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path_text = target.split("#", 1)[0]
        if path_text and not (document.parent / path_text).resolve().exists():
            missing.append(f"{document.relative_to(root)} -> {target}")
if missing:
    raise SystemExit("Missing local links:\n" + "\n".join(missing))
print("All local README links resolve.")
'@ | python -
```

Expected: `All local README links resolve.`

- [ ] **Step 3: Run focused and full verification**

Run:

```powershell
python -m pytest tests/test_benchmark_chart.py -q
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m build
```

Expected: focused tests pass; the full suite passes; Ruff check and format check exit 0; sdist and wheel build successfully. If the environment lacks the `build` module, install the development build tool and rerun rather than claiming a package build passed.

- [ ] **Step 4: Verify the final diff contains no generated noise or lost product work**

Run:

```powershell
git status --short --ignored
git diff --check
git log --oneline -6
```

Expected: ignored entries contain no lingering cache/output directories; the current version 1.3 product modifications visible before Task 1 remain present unless separately committed by their owner; no whitespace errors are reported; recent commits correspond to the plan's focused changes.

- [ ] **Step 5: Commit verification-only fixes if needed**

If Steps 1–4 require changes, stage only the affected benchmark/README files and commit them:

```powershell
git add -- README.md benchmarks/README.md benchmarks/source_review.json benchmarks/render_chart.py docs/assets/autocad-mcp-benchmark.svg tests/test_benchmark_chart.py
git commit -m "fix: align benchmark docs with verification"
```

Expected: no commit is created when verification needed no fixes; otherwise the commit contains only the listed task files.
