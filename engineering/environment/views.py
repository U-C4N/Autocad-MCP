"""Named-view arguments, validated before either engine writes."""

from __future__ import annotations

import math


def _finite(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{where} must be finite")
    return number


def resolve_view_args(center, height, width) -> dict:
    """``{"center": (x, y) | None, "height": float | None, "width": float | None}``."""
    out: dict = {"center": None, "height": None, "width": None}
    if center is not None:
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            raise ValueError("center must be an [x, y] pair")
        out["center"] = (_finite(center[0], "center.x"), _finite(center[1], "center.y"))
    if height is not None:
        out["height"] = _finite(height, "height")
        if out["height"] <= 0:
            raise ValueError(f"height must be > 0, got {out['height']}")
    if width is not None:
        out["width"] = _finite(width, "width")
        if out["width"] <= 0:
            raise ValueError(f"width must be > 0, got {out['width']}")
    return out
