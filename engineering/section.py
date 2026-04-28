"""Generic cutting-plane and section-hatch helpers."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import AutoCADBackend


async def cutting_plane_line(
    backend: "AutoCADBackend",
    *,
    p1: tuple[float, float],
    p2: tuple[float, float],
    label: str = "A",
    layer: str = "PHANTOM",
) -> dict:
    """Draw a section line, arrowheads at each end, and 'X-X' labels."""
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])

    line = await backend.entity_create_line(x1, y1, x2, y2, layer=layer)

    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux

    arrow_len = max(length * 0.05, 4.0)
    arrow_w = arrow_len * 0.4
    arrows: list[str] = []

    for tip_x, tip_y, dir_x, dir_y in (
        (x1, y1, -ux, -uy),
        (x2, y2, ux, uy),
    ):
        bx = tip_x + dir_x * arrow_len
        by = tip_y + dir_y * arrow_len
        wing1_x = bx + nx * arrow_w
        wing1_y = by + ny * arrow_w
        wing2_x = bx - nx * arrow_w
        wing2_y = by - ny * arrow_w
        a1 = await backend.entity_create_line(tip_x, tip_y, wing1_x, wing1_y, layer=layer)
        a2 = await backend.entity_create_line(tip_x, tip_y, wing2_x, wing2_y, layer=layer)
        arrows.extend([a1.handle, a2.handle])

    text = f"{label}-{label}"
    text_h = max(arrow_len, 3.5)
    offset = arrow_len * 1.5
    labels: list[str] = []
    for tip_x, tip_y, dir_x, dir_y in (
        (x1, y1, -ux, -uy),
        (x2, y2, ux, uy),
    ):
        tx = tip_x + dir_x * offset + nx * offset * 0.5
        ty = tip_y + dir_y * offset + ny * offset * 0.5
        t = await backend.entity_create_text(text, tx, ty, height=text_h, layer="TEXT")
        labels.append(t.handle)

    return {"line": line.handle, "arrows": arrows, "labels": labels}


async def apply_section_hatch(
    backend: "AutoCADBackend",
    *,
    boundary_points: list[list[float]],
    pattern: str = "ANSI31",
    angle: float = 45.0,
    scale: float = 1.0,
    layer: str = "HATCH",
) -> str:
    """Wrapper around backend.entity_create_hatch returning the new handle."""
    info = await backend.entity_create_hatch(
        pattern=pattern,
        boundary_points=boundary_points,
        scale=scale,
        angle=angle,
        layer=layer,
    )
    return info.handle
