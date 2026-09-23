"""ISO 7200 / A3 title block -- the historical entry point.

The layout moved to `engineering.sheet.titleblock`, which draws every ISO 5457
size from A4 to A0. What is left here is the A3 alias: `apply_iso_a3_titleblock`
forwards to `apply_titleblock(size="A3", projection=None, zones=False,
marks=False)`, which is bit-for-bit the geometry this module drew before -- no
projection-angle symbol, no zone grid, no trimming marks -- so every caller and
every test that pinned it keeps working. The module-level constants are
re-exported for the same reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engineering.sheet.frames import (
    EDGE_MARGIN,
    FILING_MARGIN,
    SHEET_LAYER,
    SHEET_TEXT_LAYER,
    sheet_size,
)
from engineering.sheet.titleblock import (
    LABEL_HEIGHT,
    ROW1_H,
    ROW2_H,
    ROW3_H,
    ROW4_H,
    TB_HEIGHT,
    TB_WIDTH,
    TITLE_HEIGHT,
    VALUE_HEIGHT,
    TitleBlockMetadata,
    apply_titleblock,
)

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = [
    "BOTTOM_MARGIN",
    "LABEL_HEIGHT",
    "LAYER_BORDER",
    "LAYER_TEXT",
    "LEFT_MARGIN",
    "RIGHT_MARGIN",
    "ROW1_H",
    "ROW2_H",
    "ROW3_H",
    "ROW4_H",
    "SHEET_H",
    "SHEET_W",
    "TB_HEIGHT",
    "TB_WIDTH",
    "TITLE_HEIGHT",
    "TOP_MARGIN",
    "VALUE_HEIGHT",
    "TitleBlockMetadata",
    "apply_iso_a3_titleblock",
]

SHEET_W, SHEET_H = sheet_size("A3", "landscape")
LEFT_MARGIN = FILING_MARGIN
RIGHT_MARGIN = EDGE_MARGIN
TOP_MARGIN = EDGE_MARGIN
BOTTOM_MARGIN = EDGE_MARGIN
LAYER_BORDER = SHEET_LAYER
LAYER_TEXT = SHEET_TEXT_LAYER


async def apply_iso_a3_titleblock(
    backend: AutoCADBackend,
    *,
    metadata: TitleBlockMetadata,
    origin: tuple[float, float] = (0.0, 0.0),
    layout: str | None = None,
) -> dict:
    """Draw a 420x297 ISO 7200 title block at `origin` (lower-left of sheet).

    With `layout`, the sheet is drawn on that paper-space layout -- which is
    where a title block belongs, since the border frames the printed sheet
    rather than the model. The caller's current space is restored afterwards, so
    asking for a sheet border does not silently move them onto the sheet.
    Without `layout` it draws in whatever space is current, as it always did.

    Equivalent to `titleblock_apply(size="A3")` with the projection-angle
    symbol, the zone grid and the trimming marks switched off. Reach for
    `titleblock_apply` for any other size, or for the symbol.
    """
    return await apply_titleblock(
        backend,
        size="A3",
        metadata=metadata,
        origin=origin,
        orientation="landscape",
        projection=None,
        frame=True,
        zones=False,
        marks=False,
        layout=layout,
    )
