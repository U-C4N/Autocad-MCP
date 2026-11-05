"""Render the floor plan the README shows, using only the server's own tools.

An A2 sheet at 1:50: a duplex apartment drawn by ``arch_plan_from_spec`` in one
transaction - a 9 x 7 m ring of 250 mm brick split by 100 mm AAC partitions
into a living room, a kitchen, a hall, a bathroom and a bedroom; an entrance
door and four interior doors hosted in their walls, five windows, a straight
stair, room labels whose areas the engine measures off the wall faces and the
door / window / room schedules as tables. Around it: the structural grid
A-B / 1-2 on the brick ring (``arch_grid``), the exterior dimension chains on the top and left facades
(``arch_dimension_chains``), a north arrow and two section marks X and Y
(``arch_symbol``; letters the grid does not use), furniture and sanitary
blocks from the catalogue (``arch_catalogue_insert``), and - on a paper-space
layout looking at the plan through a 1:50 viewport - the ISO 5457 frame with
its zones and the ISO 7200 title block (``titleblock_apply``) with a revision
row (``revision_add``).

Nothing on the sheet is typed geometry: every line comes from a tool the
server exposes, so the picture changes when a tool does. The script ends with
``drawing_critique(focus=None)`` and refuses to write the PNG unless it is
empty, then reports the score the finalize gate computes over the saved sheet.

Reproduce:

    AUTOCAD_MCP_BACKEND=ezdxf uv run --frozen python scripts/render_readme_arch.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "arch-showcase.png"
SAVE_DIR = ROOT / "out"

SCALE = 50  # 1:50
SHEET = "A2"
SHEET_W, SHEET_H = 594.0, 420.0
LAYOUT = "A2 PLAN"
MAX_PX = 3200
MAX_BYTES = 700_000

#: The viewport covers the whole drawing area of the frame (ISO 5457: 20 mm
#: filing margin on the left, 10 mm elsewhere). Paper (20, 10) sees model
#: (MODEL_X0, MODEL_Y0); the plan sits left of centre, the schedules at the right.
VP_X0, VP_Y0, VP_X1, VP_Y1 = 20.0, 10.0, 584.0, 410.0
MODEL_X0, MODEL_Y0 = -5200.0, -6450.0

#: Grid axes on the load-bearing brick ring only: 1-2 along x, A-B along y.
#: The 100 mm AAC partitions are not structure, and an axis on one would run
#: through the centre of its own wall-thickness dimension text.
GRID_X = [0.0, 9000.0]
GRID_Y = [0.0, 7000.0]

#: Grid lines run 34 mm (paper) past the ring's axis, so their bubbles sit
#: beyond the outermost chain row and no chain extension line crosses a bubble.
GRID_EXTENSION = 34.0 * SCALE

#: Exterior chains on the top and left facades, rows 10 / 18 / 26 mm out. The
#: headless renderer centres a text too wide for its gap on the gap and shifts
#: it along the dimension line's +normal: outward on top and left, but inward -
#: among the row's own extension lines - on bottom and right. So the chains go
#: where a moved text lands in open space.
CHAINS_SIDES = ("top", "left")
CHAINS_FIRST = 10.0 * SCALE
CHAINS_STEP = 8.0 * SCALE

#: The plan. Exterior offsets run along the ring's axis from (0, 0):
#: bottom 0-9000 (x), right 9000-16000 (y = off - 9000), top 16000-25000
#: (x = 25000 - off), left 25000-32000 (y = 32000 - off).
SPEC: dict = {
    "sheet": {"intent": "Duplex apartment, ground floor plan", "sheet_size": SHEET, "scale": SCALE},
    "lang": "en",
    "walls": [
        {
            "id": "EXT",
            "axis": [[0, 0], [9000, 0], [9000, 7000], [0, 7000]],
            "thickness": 250,
            "closed": True,
            "material": "brick",
        },
        {"id": "P1", "axis": [[5800, 0], [5800, 7000]], "thickness": 100, "material": "aac"},
        {"id": "P2", "axis": [[0, 3400], [5800, 3400]], "thickness": 100, "material": "aac"},
        {"id": "P3", "axis": [[3000, 0], [3000, 3400]], "thickness": 100, "material": "aac"},
        {"id": "P4", "axis": [[5800, 2200], [9000, 2200]], "thickness": 100, "material": "aac"},
    ],
    "openings": [
        # entrance, bottom facade, into the hall
        {"id": "D1", "wall": "EXT", "kind": "door", "offset": 3800, "width": 1000, "tag": "D1"},
        # hall -> kitchen
        {"id": "D2", "wall": "P3", "kind": "door", "offset": 2400, "width": 800, "tag": "D2"},
        # hall -> living
        {"id": "D3", "wall": "P2", "kind": "door", "offset": 4100, "width": 900, "tag": "D3"},
        # hall -> bedroom
        {"id": "D4", "wall": "P1", "kind": "door", "offset": 2450, "width": 800, "tag": "D4"},
        # hall -> bathroom
        {"id": "D5", "wall": "P1", "kind": "door", "offset": 700, "width": 700, "tag": "D5"},
        # kitchen, bottom facade
        {
            "id": "W1",
            "wall": "EXT",
            "kind": "window",
            "offset": 900,
            "width": 1200,
            "sill": 900,
            "height": 1200,
            "tag": "W1",
        },
        # bathroom, bottom facade
        {
            "id": "W2",
            "wall": "EXT",
            "kind": "window",
            "offset": 7300,
            "width": 600,
            "sill": 1500,
            "height": 600,
            "tag": "W2",
        },
        # bedroom, right facade
        {
            "id": "W3",
            "wall": "EXT",
            "kind": "window",
            "offset": 13000,
            "width": 1400,
            "sill": 900,
            "height": 1400,
            "tag": "W3",
        },
        # living, top facade
        {
            "id": "W4",
            "wall": "EXT",
            "kind": "window",
            "offset": 19400,
            "width": 1200,
            "sill": 900,
            "height": 1400,
            "tag": "W4",
        },
        # living, left facade
        {
            "id": "W5",
            "wall": "EXT",
            "kind": "window",
            "offset": 26500,
            "width": 1200,
            "sill": 900,
            "height": 1400,
            "tag": "W5",
        },
    ],
    "stairs": [
        {
            "id": "S1",
            "start": [400, 6425],
            "direction_deg": 0,
            "width": 900,
            "risers": 15,
            "riser_height": 180,
            "going": 270,
        }
    ],
    "rooms": [
        {"id": "R1", "name": "LIVING", "number": "01", "at": [3650, 5100]},
        {"id": "R2", "name": "KITCHEN", "number": "02", "at": [1300, 2700]},
        {"id": "R3", "name": "HALL", "number": "03", "at": [3900, 1900]},
        {"id": "R4", "name": "BATHROOM", "number": "04", "at": [6200, 1500]},
        {"id": "R5", "name": "BEDROOM", "number": "05", "at": [6300, 4100]},
    ],
    # No "chains" here: they are drawn after the grid by the arch_dimension_chains
    # engine, which takes the row offsets this spec key does not (see CHAINS_*).
    "schedules": [
        {"kind": "doors", "at": [15200, 11400]},
        {"kind": "windows", "at": [15200, 7300]},
        {"kind": "rooms", "at": [15200, 2900]},
    ],
}

#: Catalogue items: (name, back-left corner, rotation). Rotation 0 puts the
#: item's back along +X with the item towards +Y; 180 hangs it from a top wall,
#: -90 stands it against a left wall.
FURNITURE = [
    # living: sofa on the partition, coffee table, armchair
    ("sofa_3_seat", (1100.0, 3450.0), 0.0),
    ("coffee_table", (1650.0, 4550.0), 0.0),
    ("armchair", (5750.0, 4400.0), 90.0),
    ("bookshelf", (125.0, 3450.0 + 800.0), -90.0),
    # kitchen: counter and fridge on the left wall, dining table with chairs
    ("kitchen_counter", (125.0, 3350.0), -90.0),
    ("fridge", (125.0, 950.0), -90.0),
    ("dining_table_4", (1150.0, 550.0), 0.0),
    # bathroom: WC, basin under the window, shower in the corner
    ("wc", (6700.0, 125.0), 0.0),
    ("wall_basin", (7325.0, 125.0), 0.0),
    ("shower_tray", (7975.0, 125.0), 0.0),
    ("washing_machine", (8875.0, 1500.0), 90.0),
    # bedroom: double bed from the top wall, nightstands, wardrobe on the partition
    ("double_bed", (8100.0, 6875.0), 180.0),
    ("nightstand", (6500.0, 6875.0), 180.0),
    ("nightstand", (8600.0, 6875.0), 180.0),
    ("wardrobe", (7600.0, 2250.0), 0.0),
]


async def build(backend) -> dict:
    from engineering.arch.catalogue import insert_item
    from engineering.arch.draw import draw_chains, read_plan
    from engineering.arch.spec import draw_plan_from_spec
    from engineering.arch.symbols import draw_grid, draw_symbol
    from engineering.sheet.revision import add_revision
    from engineering.sheet.titleblock import TitleBlockMetadata, apply_titleblock
    from engineering.standards.dimstyles import resolve_dimstyle

    await backend.drawing_new()
    # Dimension lettering at ISO 3098 row 3.5 on the sheet (DIMSCALE = 50 is
    # applied by the chain tool), decimal point, whole millimetres.
    await backend.dimstyle_create(
        "PLAN-50",
        resolve_dimstyle(
            "iso-25",
            {
                "DIMTXT": 3.5,
                "DIMASZ": 3.0,
                "DIMEXE": 1.75,
                "DIMEXO": 1.0,
                "DIMGAP": 0.9,
                "DIMDEC": 0,
                "DIMDSEP": ".",
                # Fit: a text too wide for its gap moves, arrows and text
                # stay as one; moved text gets a leader. AutoCAD honours
                # both; the headless renderer (ezdxf dim_linear) reads
                # DIMATFIT without using it and applies DIMTMOVE only to a
                # user-placed text, so headlessly they change nothing.
                "DIMATFIT": 3,
                "DIMTMOVE": 1,
            },
        ),
        set_current=True,
    )

    plan = await draw_plan_from_spec(backend, SPEC)
    grid = await draw_grid(backend, GRID_X, GRID_Y, extension=GRID_EXTENSION, scale=SCALE)
    # arch_dimension_chains: read the plan's records back, then the three rows.
    drawn = await read_plan(backend)
    plan["chains"] = await draw_chains(
        backend,
        drawn["walls"],
        drawn["openings"],
        sides=CHAINS_SIDES,
        scale=SCALE,
        first_offset=CHAINS_FIRST,
        step=CHAINS_STEP,
    )
    symbols = [
        await draw_symbol(backend, "north_arrow", (11900.0, 9600.0), scale=SCALE, rotation=0.0),
        await draw_symbol(
            backend,
            "section_mark",
            (3450.0, -3900.0),
            scale=SCALE,
            p1=[0.0, 0.0],
            p2=[0.0, 14900.0],
            label="X",  # the grid owns A-B and 1-2
            side="left",
        ),
        await draw_symbol(
            backend,
            "section_mark",
            (-3900.0, 5650.0),
            scale=SCALE,
            p1=[0.0, 0.0],
            p2=[16800.0, 0.0],
            label="Y",
            side="left",
        ),
    ]
    items = [await insert_item(backend, name, at=at, rotation=rot) for name, at, rot in FURNITURE]

    # The sheet: a paper layout, a 1:50 viewport over the frame's drawing area,
    # the ISO 5457 frame with zones and marks, the ISO 7200 title block.
    await backend.layout_create(LAYOUT)
    vw, vh = VP_X1 - VP_X0, VP_Y1 - VP_Y0
    viewport = await backend.viewport_create(
        LAYOUT,
        (VP_X0 + VP_X1) / 2.0,
        (VP_Y0 + VP_Y1) / 2.0,
        vw,
        vh,
        MODEL_X0 + vw * SCALE / 2.0,
        MODEL_Y0 + vh * SCALE / 2.0,
        scale=1.0 / SCALE,
    )
    await backend.layout_set_current(LAYOUT)
    await apply_titleblock(
        backend,
        size=SHEET,
        metadata=TitleBlockMetadata(
            title="APARTMENT PLAN",
            drawing_no="AMC-ARCH-0101",
            part_no="GF-01",
            material="BRICK 250 / AAC 100",
            scale=f"1:{SCALE}",
            drawn_by="autocad-mcp-pro",
            checked_by="drawing_critique",
            date="2026-11-05",
            revision="A",
        ),
        projection=None,
        frame=True,
        zones=True,
        marks=True,
        layout=LAYOUT,
    )
    revision = await add_revision(
        backend,
        rev="A",
        description="First issue",
        date="2026-11-05",
        by="autocad-mcp-pro",
        size=SHEET,
        layout=LAYOUT,
    )
    return {
        "plan": plan,
        "grid": grid,
        "symbols": symbols,
        "items": items,
        "viewport": viewport,
        "revision": revision,
    }


async def render(output: Path) -> dict:
    os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")
    from backends.ezdxf_backend import EzdxfBackend
    from engineering import DrawingValidator
    from engineering.critique import run_critique
    from engineering.scoring import combine

    backend = EzdxfBackend()
    await backend.connect()
    try:
        built = await build(backend)
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        sheet = SAVE_DIR / "arch-showcase.dxf"
        # drawing_finalize saves before it validates; so does this script.
        await backend.drawing_save_as(str(sheet))
        issues = await run_critique(backend, None)
        if issues:
            for issue in issues:
                print("critique:", issue.to_dict(), file=sys.stderr)
            raise SystemExit(f"drawing_critique(focus=None) returned {len(issues)} issue(s)")
        validation = await DrawingValidator().run(backend)
        score = combine(validation.summary, {"error": 0, "warning": 0, "info": 0})
        info = await backend.drawing_info()

        max_px = MAX_PX
        while True:
            shot = await backend.view_render(region=[0.0, 0.0, SHEET_W, SHEET_H], max_px=max_px)
            png = shot.get("png")
            if not png:
                raise RuntimeError(f"view_render returned no PNG: {shot.get('error')}")
            if len(png) <= MAX_BYTES or max_px <= 2000:
                break
            max_px -= 200
    finally:
        await backend.disconnect()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    plan = built["plan"]
    rooms = {
        f"{r['room']['number']} {r['room']['name']}": r["room"]["area_text"] for r in plan["rooms"]
    }
    return {
        "output": str(output),
        "bytes": len(png),
        "px": shot.get("px"),
        "saved": str(sheet),
        "entities": info.entity_count,
        "critique_issues": len(issues),
        "score": float(score["score"]),
        "grade": score["grade"],
        "validator_findings": sorted(f.code for f in validation.findings),
        "walls": len(plan["walls"]),
        "openings": len(plan["openings"]),
        "omitted": plan["omitted"],
        "rooms_m2": rooms,
        "chains": (plan.get("chains") or {}).get("count"),
        "schedule_rows": [len(s.get("rows") or ()) for s in plan["schedules"]],
        "catalogue_items": len(built["items"]),
        "plan_critique": plan["critique"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(asyncio.run(render(args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
