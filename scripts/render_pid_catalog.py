"""Render every catalogue symbol on one labelled sheet (docs/assets/pid-catalog.png).

Each symbol is placed through ``insert_symbol`` -- the same path
``pid_symbol_insert`` takes -- so a regressed builder changes the picture.
The sheet is rendered through the same ezdxf/matplotlib frontend
``view_screenshot`` uses, only at a figure size a 10 x 11 grid of 8 mm
symbols stays legible on (the tool's 16 x 9 in screenshot is sized for a
single drawing, not a catalogue).

    uv run --frozen python scripts/render_pid_catalog.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "pid-catalog.png"
CELL = 80.0  # the tallest vessel spans 66 mm with its nozzle stubs
COLUMNS = 10
FIGSIZE = (20.0, 22.0)  # inches; the grid is 800 x 880 mm
DPI = 110


def _render_png(backend, figsize: tuple[float, float], dpi: int) -> bytes:
    """The document as a PNG, at a size of our choosing."""
    import io

    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    from backends.ezdxf_backend import _new_agg_figure

    doc = backend._require_doc()
    fig = _new_agg_figure(figsize=figsize, dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    out = MatplotlibBackend(ax, adjust_figure=False)  # keep FIGSIZE; the default shrinks it to 5 in
    Frontend(RenderContext(doc), out).draw_layout(doc.modelspace(), finalize=True)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=dpi)
    return buf.getvalue()


async def _render(output: Path) -> dict:
    from backends.ezdxf_backend import EzdxfBackend
    from engineering.layers import apply_layer_set
    from engineering.pid.insert import insert_symbol
    from engineering.pid.symbols import all_specs

    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_new()
        await apply_layer_set(backend, "pid")
        specs = all_specs()
        for index, spec in enumerate(specs):
            col, row = index % COLUMNS, index // COLUMNS
            x, y = col * CELL, -row * CELL
            layer = "PROCESS-PIPING-MAIN" if spec.family == "marker" else None
            await insert_symbol(backend, spec, x, y, 0.0, 1.0, {}, layer)
            await backend.entity_create_text(
                spec.name.replace("PID_", ""),
                x - CELL / 2 + 2,
                y - CELL / 2 + 2,
                1.6,
                layer="TEXT-NOTES",
            )
        png = await asyncio.to_thread(_render_png, backend, FIGSIZE, DPI)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        return {"output": str(output), "symbols": len(specs), "bytes": len(png)}
    finally:
        await backend.disconnect()


def render(output: Path) -> dict:
    return asyncio.run(_render(output))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    print(render(parser.parse_args().output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
