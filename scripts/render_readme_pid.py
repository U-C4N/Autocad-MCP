"""Render the P&ID the README shows, using only the server's own tools.

The sheet is ``EXAMPLE_SPEC`` from ``engineering/pid/spec.py`` -- the same
document ``pid_from_spec`` draws in one transaction -- placed and routed
through the same backend methods the MCP tools call, then rendered headlessly.
If a symbol builder or the router regresses, the picture changes with it.

Reproduce:

    AUTOCAD_MCP_BACKEND=ezdxf uv run --frozen python scripts/render_readme_pid.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "pid-showcase.png"


async def render(output: Path) -> dict:
    from backends.ezdxf_backend import EzdxfBackend
    from engineering.layers import apply_layer_set
    from engineering.pid.spec import EXAMPLE_SPEC, run_spec

    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_new()
        await apply_layer_set(backend, "pid")
        result = await run_spec(backend, EXAMPLE_SPEC)
        png = await backend.view_screenshot()
        if not png:
            raise RuntimeError("view_screenshot returned no PNG (is matplotlib installed?)")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        return {
            "output": str(output),
            "bytes": len(png),
            "dangling": result["graph"]["stats"]["dangling"],
            "critique": len(result["critique"]),
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
