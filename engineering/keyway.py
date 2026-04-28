"""DIN 6885 keyway geometry — bore + keyway cutter for shafts and hubs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

log = logging.getLogger(__name__)


DIN6885_TABLE: list[tuple[float, float, float, float, float]] = [
    # (bore_min, bore_max, key_width, key_height, depth_in_shaft_t1)
    (6, 8, 2, 2, 1.2),
    (8, 10, 3, 3, 1.8),
    (10, 12, 4, 4, 2.5),
    (12, 17, 5, 5, 3.0),
    (17, 22, 6, 6, 3.5),
    (22, 30, 8, 7, 4.0),
    (30, 38, 10, 8, 5.0),
    (38, 44, 12, 8, 5.0),
    (44, 50, 14, 9, 5.5),
    (50, 58, 16, 10, 6.0),
    (58, 65, 18, 11, 7.0),
    (65, 75, 20, 12, 7.5),
    (75, 85, 22, 14, 9.0),
    (85, 95, 25, 14, 9.0),
    (95, 110, 28, 16, 10.0),
    (110, 130, 32, 18, 11.0),
]


def keyway_dimensions(bore_diameter: float) -> dict:
    """Look up DIN 6885 key width/height/depth for a given bore diameter (mm)."""
    if bore_diameter < DIN6885_TABLE[0][0]:
        raise ValueError(
            f"bore_diameter {bore_diameter} mm is below DIN 6885 minimum "
            f"({DIN6885_TABLE[0][0]} mm)."
        )
    if bore_diameter > DIN6885_TABLE[-1][1]:
        raise ValueError(
            f"bore_diameter {bore_diameter} mm exceeds DIN 6885 maximum "
            f"({DIN6885_TABLE[-1][1]} mm)."
        )
    for b_min, b_max, width, height, t1 in DIN6885_TABLE:
        if b_min < bore_diameter <= b_max or (bore_diameter == b_min and b_min == DIN6885_TABLE[0][0]):
            return {
                "width": float(width),
                "height": float(height),
                "depth_shaft": float(t1),
                "depth_hub": float(height) - float(t1),
            }
    raise ValueError(f"No DIN 6885 row matched bore_diameter={bore_diameter}.")


async def draw_keyed_bore(
    backend: "AutoCADBackend",
    *,
    center: tuple[float, float],
    bore_diameter: float,
    keyway_width: float | None = None,
    keyway_depth: float | None = None,
    layer: str = "GEOMETRY",
    center_layer: str = "CENTER",
) -> dict:
    """Draw bore + upward-opening keyway notch + horizontal/vertical centerlines."""
    cx, cy = float(center[0]), float(center[1])
    r = float(bore_diameter) / 2.0

    if keyway_width is None or keyway_depth is None:
        dims = keyway_dimensions(bore_diameter)
        if keyway_width is None:
            keyway_width = dims["width"]
        if keyway_depth is None:
            keyway_depth = dims["depth_hub"]

    bore = await backend.entity_create_circle(cx, cy, r, layer=layer)

    half_w = float(keyway_width) / 2.0
    depth = float(keyway_depth)
    bore_top_y = cy + r
    notch_top_y = bore_top_y + depth
    left_x = cx - half_w
    right_x = cx + half_w

    points = [
        [left_x, bore_top_y],
        [left_x, notch_top_y],
        [right_x, notch_top_y],
        [right_x, bore_top_y],
    ]
    keyway_poly = await backend.entity_create_polyline(points, closed=False, layer=layer)

    overshoot = max(r * 0.2, 5.0)
    horiz = await backend.entity_create_line(
        cx - r - overshoot, cy, cx + r + overshoot, cy, layer=center_layer,
    )
    vert = await backend.entity_create_line(
        cx, cy - r - overshoot, cx, cy + r + depth + overshoot, layer=center_layer,
    )

    return {
        "bore": bore.handle,
        "keyway_polyline": keyway_poly.handle,
        "centerline_h": horiz.handle,
        "centerline_v": vert.handle,
    }


async def draw_keyway_section(
    backend: "AutoCADBackend",
    *,
    center: tuple[float, float],
    bore_diameter: float,
    face_width: float,
    keyway_width: float | None = None,
    keyway_depth: float | None = None,
    layer: str = "GEOMETRY",
) -> dict:
    """Side cross-section: bore as two horizontal lines with rectangular keyway notch on top."""
    cx, cy = float(center[0]), float(center[1])
    r = float(bore_diameter) / 2.0
    half_face = float(face_width) / 2.0

    if keyway_width is None or keyway_depth is None:
        dims = keyway_dimensions(bore_diameter)
        if keyway_width is None:
            keyway_width = dims["width"]
        if keyway_depth is None:
            keyway_depth = dims["depth_hub"]

    half_kw = float(keyway_width) / 2.0
    depth = float(keyway_depth)
    top_y = cy + r
    bot_y = cy - r
    notch_top_y = top_y + depth
    x_left = cx - half_face
    x_right = cx + half_face
    notch_left = cx - half_kw
    notch_right = cx + half_kw

    bore_top = await backend.entity_create_line(
        x_left, top_y, x_right, top_y, layer=layer,
    )
    bore_bottom = await backend.entity_create_line(
        x_left, bot_y, x_right, bot_y, layer=layer,
    )

    notch_bottom = await backend.entity_create_line(
        notch_left, top_y, notch_right, top_y, layer=layer,
    )
    notch_left_wall = await backend.entity_create_line(
        notch_left, top_y, notch_left, notch_top_y, layer=layer,
    )
    notch_top = await backend.entity_create_line(
        notch_left, notch_top_y, notch_right, notch_top_y, layer=layer,
    )
    notch_right_wall = await backend.entity_create_line(
        notch_right, notch_top_y, notch_right, top_y, layer=layer,
    )

    return {
        "bore_top": bore_top.handle,
        "bore_bottom": bore_bottom.handle,
        "keyway": [
            notch_bottom.handle,
            notch_left_wall.handle,
            notch_top.handle,
            notch_right_wall.handle,
        ],
    }
