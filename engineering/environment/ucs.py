"""UCS axes, validated and normalised before either engine writes.

Tool coordinates stay WCS on both engines (the repository rule): a UCS is
stored and made current for the operator's benefit, and nothing here starts
interpreting inputs in it.
"""

from __future__ import annotations

import math

WORLD = "world"
ORTHO_TOLERANCE_DEG = 1e-3


def _vec3(value, where: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{where} must be an [x, y, z] triple")
    out = []
    for axis, item in zip("xyz", value, strict=True):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{where}.{axis} must be a number, got {item!r}")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError(f"{where}.{axis} must be finite")
        out.append(number)
    return out[0], out[1], out[2]


def _unit(vector, where: str) -> tuple[float, float, float]:
    length = math.sqrt(sum(c * c for c in vector))
    if length < 1e-12:
        raise ValueError(f"{where} must not be a zero vector")
    return vector[0] / length, vector[1] / length, vector[2] / length


def resolve_ucs_axes(origin, x_axis, y_axis) -> dict:
    """Unit axes plus the measured angle; non-orthogonal axes are refused by that angle."""
    o = _vec3(origin, "origin")
    x = _unit(_vec3(x_axis, "x_axis"), "x_axis")
    y = _unit(_vec3(y_axis, "y_axis"), "y_axis")
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(x, y, strict=True))))
    angle = math.degrees(math.acos(dot))
    if abs(angle - 90.0) > ORTHO_TOLERANCE_DEG:
        raise ValueError(
            f"ucs_set: x_axis and y_axis must be perpendicular; measured {angle:.2f}° "
            f"between them (tolerance {ORTHO_TOLERANCE_DEG}°)"
        )
    z = (
        x[1] * y[2] - x[2] * y[1],
        x[2] * y[0] - x[0] * y[2],
        x[0] * y[1] - x[1] * y[0],
    )
    return {"origin": o, "x_axis": x, "y_axis": y, "z_axis": z, "angle_deg": angle}
