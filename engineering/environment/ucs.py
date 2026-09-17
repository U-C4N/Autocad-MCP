"""UCS axes, validated and normalised before either engine writes.

Tool coordinates stay WCS on both engines (the repository rule): a UCS is
stored and made current for the operator's benefit, and nothing here starts
interpreting inputs in it.
"""

from __future__ import annotations

import math

WORLD = "world"
ORTHO_TOLERANCE_DEG = 1e-3

#: The WCS frame — what ``$UCSORG`` / ``$UCSXDIR`` / ``$UCSYDIR`` (and the
#: UCSORG / UCSXDIR / UCSYDIR system variables) hold while no UCS is active.
WORLD_ORIGIN = (0.0, 0.0, 0.0)
WORLD_X_AXIS = (1.0, 0.0, 0.0)
WORLD_Y_AXIS = (0.0, 1.0, 0.0)
WORLD_TOLERANCE = 1e-9


def is_world_axes(origin, x_axis, y_axis, *, tol: float = WORLD_TOLERANCE) -> bool:
    """True when the frame *is* the WCS — the test ``$UCSNAME == ""`` cannot make.

    An unnamed UCS (``UCS Origin`` / ``UCS 3P`` without saving — the common
    kind) also has an empty name, so "is the drawing in WCS" has to be
    answered from the frame itself: AutoCAD's own answer is WORLDUCS, and
    this is the same comparison for a header that has no such variable.
    """
    for got, want in (
        (origin, WORLD_ORIGIN),
        (x_axis, WORLD_X_AXIS),
        (y_axis, WORLD_Y_AXIS),
    ):
        coords = tuple(float(c) for c in got)
        if len(coords) != 3:
            return False
        if any(abs(a - b) > tol for a, b in zip(coords, want, strict=True)):
            return False
    return True


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
