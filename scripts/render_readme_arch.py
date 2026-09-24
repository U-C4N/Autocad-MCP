"""Render the floor plan the README shows, using only the server's own tools.

The plan is ``EXAMPLE_SPEC`` from ``engineering/arch/spec.py`` - the document
``arch_plan_from_spec`` draws in one call: a 9 x 6 m brick ring split by an AAC
wall, an entrance door, an interior door, two windows, a straight stair, two
rooms whose areas are measured from the faces the walls enclose, the exterior
dimension chains and the door / window / room schedules. The ``arch_roundtrip``
benchmark task gates on the same spec, so a regression changes both.

Reproduce:

    AUTOCAD_MCP_BACKEND=ezdxf uv run --frozen python scripts/render_readme_arch.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "arch-showcase.png"


async def render(output: Path) -> dict:
    from backends.ezdxf_backend import EzdxfBackend
    from engineering.arch.spec import EXAMPLE_SPEC, draw_plan_from_spec
    from engineering.critique import run_critique

    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_new()
        result = await draw_plan_from_spec(backend, EXAMPLE_SPEC)
        issues = await run_critique(backend, None)
        png = await backend.view_screenshot()
        if not png:
            raise RuntimeError("view_screenshot returned no PNG (is matplotlib installed?)")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        return {
            "output": str(output),
            "bytes": len(png),
            "omitted": len(result.get("omitted", [])),
            "critique": len(issues),
        }
    finally:
        await backend.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(asyncio.run(render(args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
