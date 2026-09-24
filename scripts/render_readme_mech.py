"""Render the mechanical sheet the README shows, using only the server's own tools.

The sheet is the one the ``mech_assembly`` benchmark task gates on - a flange
coupling: a hub with a full section, a flange with its bolt-circle end view, two
ISO 4014 bolts with ISO 4032 nuts, an ISO 7573 parts list read off the drawing,
linked ISO 6433 balloons, an ISO 5457 frame and an ISO 7200 title block. It is
built by the benchmark's own task, so the picture and the gate cannot drift
apart: if the view engine or the sheet standard regresses, both change.

Reproduce:

    AUTOCAD_MCP_BACKEND=ezdxf uv run --frozen python scripts/render_readme_mech.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "mech-showcase.png"


async def render(output: Path) -> dict:
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    with tempfile.TemporaryDirectory(prefix="readme_mech_") as folder:
        await adapter.setup(Path(folder))
        try:
            await adapter.backend.drawing_new()  # the harness opens one before every task
            passed, metrics = (await adapter._task_mech_assembly())[:2]
            png = await adapter.backend.view_screenshot()
        finally:
            await adapter.cleanup()
    if not png:
        raise RuntimeError("view_screenshot returned no PNG (is matplotlib installed?)")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    return {
        "output": str(output),
        "bytes": len(png),
        "gate_passed": passed,
        "critique_issues": metrics["critique_issues"],
        "score": metrics["score"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(asyncio.run(render(args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
