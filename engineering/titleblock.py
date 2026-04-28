"""ISO 7200 / A3 (420 x 297 mm) title block generator.

Deterministic — the title text is written verbatim from metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import AutoCADBackend


# A3 paper, ISO 7200 layout constants (mm)
SHEET_W = 420.0
SHEET_H = 297.0
LEFT_MARGIN = 20.0
RIGHT_MARGIN = 10.0
TOP_MARGIN = 10.0
BOTTOM_MARGIN = 10.0

TB_WIDTH = 180.0
TB_HEIGHT = 60.0

ROW1_H = 20.0  # title
ROW2_H = 15.0  # drawing_no | revision | sheet
ROW3_H = 15.0  # part_no | material | scale
ROW4_H = 10.0  # drawn_by | checked_by | date | company

LABEL_HEIGHT = 2.0
VALUE_HEIGHT = 3.5
TITLE_HEIGHT = 5.0

LAYER_BORDER = "TITLEBLOCK"
LAYER_TEXT = "TEXT"


@dataclass
class TitleBlockMetadata:
    """ISO 7200 title block fields (all strings — no LLM transformation)."""

    title: str
    drawing_no: str
    part_no: str = ""
    material: str = ""
    scale: str = "1:1"
    units: str = "mm"
    drawn_by: str = ""
    checked_by: str = ""
    date: str = ""
    sheet: str = "1/1"
    revision: str = "A"
    company: str = "Anka-Makine"


async def apply_iso_a3_titleblock(
    backend: "AutoCADBackend",
    *,
    metadata: TitleBlockMetadata,
    origin: tuple[float, float] = (0.0, 0.0),
) -> dict:
    """Draw a 420x297 ISO 7200 title block at `origin` (lower-left of sheet)."""

    ox, oy = float(origin[0]), float(origin[1])

    # ---- outer + inner borders -------------------------------------------------
    outer_pts = [
        [ox, oy],
        [ox + SHEET_W, oy],
        [ox + SHEET_W, oy + SHEET_H],
        [ox, oy + SHEET_H],
    ]
    outer = await backend.entity_create_polyline(
        points=outer_pts, closed=True, layer=LAYER_BORDER,
    )

    inner_x0 = ox + LEFT_MARGIN
    inner_y0 = oy + BOTTOM_MARGIN
    inner_x1 = ox + SHEET_W - RIGHT_MARGIN
    inner_y1 = oy + SHEET_H - TOP_MARGIN
    inner_pts = [
        [inner_x0, inner_y0],
        [inner_x1, inner_y0],
        [inner_x1, inner_y1],
        [inner_x0, inner_y1],
    ]
    inner = await backend.entity_create_polyline(
        points=inner_pts, closed=True, layer=LAYER_BORDER,
    )

    # ---- title block (lower-right corner of inner border) ----------------------
    tb_x0 = inner_x1 - TB_WIDTH
    tb_y0 = inner_y0
    tb_x1 = inner_x1
    tb_y1 = inner_y0 + TB_HEIGHT

    # Title block frame (closed polyline)
    tb_frame_pts = [
        [tb_x0, tb_y0],
        [tb_x1, tb_y0],
        [tb_x1, tb_y1],
        [tb_x0, tb_y1],
    ]
    tb_frame = await backend.entity_create_polyline(
        points=tb_frame_pts, closed=True, layer=LAYER_BORDER,
    )

    # Row baselines (from bottom up): row4_top, row3_top, row2_top
    row4_top = tb_y0 + ROW4_H
    row3_top = row4_top + ROW3_H
    row2_top = row3_top + ROW2_H  # == tb_y1 - ROW1_H

    titleblock_lines: list[str] = [tb_frame.handle]

    async def hline(y: float) -> str:
        ent = await backend.entity_create_line(
            tb_x0, y, tb_x1, y, layer=LAYER_BORDER,
        )
        return ent.handle

    async def vline(x: float, y0: float, y1: float) -> str:
        ent = await backend.entity_create_line(
            x, y0, x, y1, layer=LAYER_BORDER,
        )
        return ent.handle

    titleblock_lines.append(await hline(row4_top))
    titleblock_lines.append(await hline(row3_top))
    titleblock_lines.append(await hline(row2_top))

    # Row 2 splits: left half = drawing_no, then quarter rev, quarter sheet
    r2_split1 = tb_x0 + TB_WIDTH * 0.5
    r2_split2 = tb_x0 + TB_WIDTH * 0.75
    titleblock_lines.append(await vline(r2_split1, row3_top, row2_top))
    titleblock_lines.append(await vline(r2_split2, row3_top, row2_top))

    # Row 3 splits: thirds — part_no | material | scale
    r3_split1 = tb_x0 + TB_WIDTH / 3.0
    r3_split2 = tb_x0 + TB_WIDTH * 2.0 / 3.0
    titleblock_lines.append(await vline(r3_split1, row4_top, row3_top))
    titleblock_lines.append(await vline(r3_split2, row4_top, row3_top))

    # Row 4 splits: quarters — drawn_by | checked_by | date | company
    r4_split1 = tb_x0 + TB_WIDTH * 0.25
    r4_split2 = tb_x0 + TB_WIDTH * 0.50
    r4_split3 = tb_x0 + TB_WIDTH * 0.75
    titleblock_lines.append(await vline(r4_split1, tb_y0, row4_top))
    titleblock_lines.append(await vline(r4_split2, tb_y0, row4_top))
    titleblock_lines.append(await vline(r4_split3, tb_y0, row4_top))

    # ---- text content ----------------------------------------------------------
    label_pad_x = 1.5
    label_pad_y = 1.5
    value_pad_x = 3.0
    value_pad_y = 5.0

    async def label(text: str, x: float, y: float) -> str:
        ent = await backend.entity_create_text(
            text=text, x=x, y=y, height=LABEL_HEIGHT, layer=LAYER_TEXT,
        )
        return ent.handle

    async def value(text: str, x: float, y: float, height: float = VALUE_HEIGHT) -> str:
        ent = await backend.entity_create_text(
            text=text, x=x, y=y, height=height, layer=LAYER_TEXT,
        )
        return ent.handle

    # Row 1 — TITLE (verbatim)
    title_x = tb_x0 + TB_WIDTH * 0.5 - (len(metadata.title) * TITLE_HEIGHT * 0.3)
    title_y = row2_top + (ROW1_H - TITLE_HEIGHT) / 2.0
    title_text_handle = await value(metadata.title, title_x, title_y, height=TITLE_HEIGHT)

    value_texts: dict[str, str] = {}

    # Row 2: drawing_no (left half) | revision (3rd qtr) | sheet (4th qtr)
    await label("DWG NO", tb_x0 + label_pad_x, row2_top - LABEL_HEIGHT - label_pad_y)
    value_texts["drawing_no"] = await value(
        metadata.drawing_no,
        tb_x0 + value_pad_x,
        row3_top + value_pad_y,
    )
    await label("REV", r2_split1 + label_pad_x, row2_top - LABEL_HEIGHT - label_pad_y)
    value_texts["revision"] = await value(
        metadata.revision,
        r2_split1 + value_pad_x,
        row3_top + value_pad_y,
    )
    await label("SHEET", r2_split2 + label_pad_x, row2_top - LABEL_HEIGHT - label_pad_y)
    value_texts["sheet"] = await value(
        metadata.sheet,
        r2_split2 + value_pad_x,
        row3_top + value_pad_y,
    )

    # Row 3: part_no | material | scale
    await label("PART NO", tb_x0 + label_pad_x, row3_top - LABEL_HEIGHT - label_pad_y)
    value_texts["part_no"] = await value(
        metadata.part_no,
        tb_x0 + value_pad_x,
        row4_top + value_pad_y,
    )
    await label("MATERIAL", r3_split1 + label_pad_x, row3_top - LABEL_HEIGHT - label_pad_y)
    value_texts["material"] = await value(
        metadata.material,
        r3_split1 + value_pad_x,
        row4_top + value_pad_y,
    )
    await label("SCALE", r3_split2 + label_pad_x, row3_top - LABEL_HEIGHT - label_pad_y)
    value_texts["scale"] = await value(
        metadata.scale,
        r3_split2 + value_pad_x,
        row4_top + value_pad_y,
    )

    # Row 4: drawn_by | checked_by | date | company
    await label("DRAWN", tb_x0 + label_pad_x, row4_top - LABEL_HEIGHT - label_pad_y)
    value_texts["drawn_by"] = await value(
        metadata.drawn_by,
        tb_x0 + value_pad_x,
        tb_y0 + 1.5,
    )
    await label("CHECKED", r4_split1 + label_pad_x, row4_top - LABEL_HEIGHT - label_pad_y)
    value_texts["checked_by"] = await value(
        metadata.checked_by,
        r4_split1 + value_pad_x,
        tb_y0 + 1.5,
    )
    await label("DATE", r4_split2 + label_pad_x, row4_top - LABEL_HEIGHT - label_pad_y)
    value_texts["date"] = await value(
        metadata.date,
        r4_split2 + value_pad_x,
        tb_y0 + 1.5,
    )
    await label("COMPANY", r4_split3 + label_pad_x, row4_top - LABEL_HEIGHT - label_pad_y)
    value_texts["company"] = await value(
        metadata.company,
        r4_split3 + value_pad_x,
        tb_y0 + 1.5,
    )

    return {
        "outer_border": outer.handle,
        "inner_border": inner.handle,
        "titleblock_lines": titleblock_lines,
        "title_text": title_text_handle,
        "value_texts": value_texts,
        "metadata": asdict(metadata),
        "bbox": {
            "min": [ox, oy],
            "max": [ox + SHEET_W, oy + SHEET_H],
        },
    }
