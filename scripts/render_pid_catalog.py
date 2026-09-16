"""Render every catalogue symbol on one labelled sheet (docs/assets/pid-catalog.png).

Each symbol is placed through ``insert_symbol`` -- the same path
``pid_symbol_insert`` takes -- so a regressed builder changes the picture.
The sheet is rendered through the same ezdxf/matplotlib frontend
``view_screenshot`` uses, only at a figure size a 10 x 11 grid of 8 mm
symbols stays legible on (the tool's 16 x 9 in screenshot is sized for a
single drawing, not a catalogue).

``render`` reports what was actually drawn, not what the catalogue lists:
``symbols`` counts the specs whose insert left geometry with a real extent
in the modelspace (an empty block or a builder that draws nothing is
``blank``), and ``ink`` is the share of the PNG's pixels that are not the
background colour -- the labels-only sheet is the baseline the test
compares against.

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


def ink_fraction(png: bytes) -> float:
    """The share of pixels that are not the sheet's background colour.

    The background is the most common colour, so the number does not depend
    on which background the frontend paints; a blank sheet reports 0.0.
    """
    import io

    import numpy as np
    from matplotlib.image import imread

    rgba = imread(io.BytesIO(png))
    pixels = (np.asarray(rgba).reshape(-1, rgba.shape[-1]) * 255).astype(np.uint32)
    packed = np.zeros(len(pixels), dtype=np.uint32)
    for channel in range(pixels.shape[1]):  # one integer per pixel; unique() stays 1-D
        packed = (packed << 8) | pixels[:, channel]
    _colours, counts = np.unique(packed, return_counts=True)
    return float(1.0 - counts.max() / len(packed))


def _has_extent(doc, handle: str) -> bool:
    """Whether the entity behind ``handle`` has any length once its block
    content is resolved -- an INSERT of an empty block has no extent, while
    a spec break (one vertical line) legitimately has none across x."""
    from ezdxf import bbox

    entity = doc.entitydb.get(handle)
    if entity is None:
        return False
    box = bbox.extents([entity], fast=True)
    return box.has_data and (box.size.x > 0 or box.size.y > 0)


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
        doc = backend._require_doc()
        specs = all_specs()
        blank: list[str] = []
        for index, spec in enumerate(specs):
            col, row = index % COLUMNS, index // COLUMNS
            x, y = col * CELL, -row * CELL
            layer = "PROCESS-PIPING-MAIN" if spec.family == "marker" else None
            before = len(doc.modelspace())
            placed = await insert_symbol(backend, spec, x, y, 0.0, 1.0, {}, layer)
            handles = [placed.get("handle")] if len(doc.modelspace()) > before else []
            if not any(_has_extent(doc, h) for h in handles if h):
                blank.append(spec.name)
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
        return {
            "output": str(output),
            "symbols": len(specs) - len(blank),
            "blank": blank,
            "bytes": len(png),
            "ink": await asyncio.to_thread(ink_fraction, png),
        }
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
