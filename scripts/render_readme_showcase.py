"""Render the drawing the README shows, using only the server's own tools.

A CAD README that shows nothing but bar charts is asking to be taken on faith.
This produces the one asset that is not a claim: an actual sheet, built through
the same backend methods the MCP tools call, in the order CLAUDE.md's premium
workflow prescribes -- ISO layers, deterministic gear geometry, a keyed bore,
ISO 129 dimensions, an ISO 7200 title block -- and rendered headlessly.

Reproduce:

    AUTOCAD_MCP_BACKEND=ezdxf python scripts/render_readme_showcase.py

Nothing here is decoration: every line on the sheet comes from a tool the
server exposes, so if a tool regresses the picture changes with it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "autocad-mcp-showcase.png"

#: A 24-tooth, module-6 spur gear. Sized to fill the A3 drawing area, because
#: the render crops to the sheet border: geometry that fills the border gets
#: the pixels, and involute flanks that do not resolve prove nothing.
GEAR_MODULE = 6.0
GEAR_TEETH = 24
BORE_DIAMETER = 40.0
KEYWAY_WIDTH = 12.0
KEYWAY_DEPTH = 3.3
FACE_WIDTH = 46.0
GEAR_CENTER = (135.0, 172.0)
SECTION_X = 300.0

#: No DIMSCALE here on purpose. `drawing_settings({"dimscale": ...})` sets the
#: header variable and reports success, but ezdxf renders dimensions from the
#: dimstyle table entry, so headlessly it changes nothing (measured). Setting it
#: in this script would put a call in the README's own showcase that does not do
#: what it looks like it does.


async def build(backend) -> dict:
    from engineering.gear import draw_gear_section_aa, draw_spur_gear_front_view
    from engineering.titleblock import TitleBlockMetadata, apply_iso_a3_titleblock

    await backend.drawing_new()
    await backend.drawing_apply_iso_layers("mech")
    await backend.drawing_settings({"units": "mm", "linear_precision": 2})

    gear = await draw_spur_gear_front_view(
        backend,
        module=GEAR_MODULE,
        teeth=GEAR_TEETH,
        center=GEAR_CENTER,
        bore_diameter=BORE_DIAMETER,
        keyway_width=KEYWAY_WIDTH,
        keyway_depth=KEYWAY_DEPTH,
    )

    # `gear_metadata` is the geometry record, not the handle map the front view
    # returns -- the section needs radii, not handles.
    await draw_gear_section_aa(
        backend,
        gear_metadata=gear["metadata"],
        x_offset=SECTION_X,
        face_width=FACE_WIDTH,
    )

    # ISO 129 dimensions, and a real ISO 286 fit on the bore -- "H7" is the
    # whole reason a drafter trusts a generated hole. `fit=` is resolved by the
    # tool layer, so the deviations are looked up here the same way it does
    # rather than typed in as text.
    from engineering.fits import fit_lookup

    cx, cy = GEAR_CENTER
    pitch_radius = GEAR_MODULE * GEAR_TEETH / 2.0
    outer_radius = pitch_radius + GEAR_MODULE
    await backend.dimension_linear(
        cx - pitch_radius,
        cy,
        cx + pitch_radius,
        cy,
        cx,
        cy - outer_radius - 22.0,
        layer="DIM",
    )
    # dimension_diameter takes the two ENDS OF A DIAMETER, not the centre and a
    # point on the circle. Passing the latter halves the measured size, which is
    # how the first draft of this sheet ended up calling a 40 mm bore 20.
    bore_fit = fit_lookup("H7", BORE_DIAMETER)
    await backend.dimension_diameter(
        cx - BORE_DIAMETER / 2.0,
        cy,
        cx + BORE_DIAMETER / 2.0,
        cy,
        leader_length=34.0,
        layer="DIM",
        tol_upper=bore_fit.upper_mm,
        tol_lower=-bore_fit.lower_mm,
        tol_mode="deviation",
        text_override=f"<> {bore_fit.code}",
    )

    await apply_iso_a3_titleblock(
        backend,
        metadata=TitleBlockMetadata(
            title=f"SPUR GEAR m{GEAR_MODULE:g} z{GEAR_TEETH}",
            drawing_no="AMC-1500-001",
            part_no="1500-001",
            material="C45E",
            scale="1:1",
            drawn_by="autocad-mcp-pro",
            checked_by="drawing_critique",
            date="2026-08-06",
            revision="A",
        ),
        origin=(0.0, 0.0),
    )

    issues = await backend.drawing_critique(focus=None)
    info = await backend.drawing_info()
    return {"issues": len(issues), "entities": info.entity_count, "gear": gear}


async def main(output: Path) -> int:
    os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")
    from backends.ezdxf_backend import EzdxfBackend

    backend = EzdxfBackend()
    await backend.connect()
    try:
        summary = await build(backend)
        png = await backend.view_screenshot(overlay_handles=False)
    finally:
        await backend.disconnect()

    if not png:
        print("render produced no image", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    print(
        f"wrote {output} ({len(png)} bytes) - "
        f"{summary['entities']} entities, {summary['issues']} critique issues"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(build_parser().parse_args().output)))
