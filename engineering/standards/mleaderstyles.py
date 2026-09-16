"""Multileader style presets: arrow, landing gap, text style and height.

Sized to match the dimension presets they sit beside (ISO-25: 2.5 mm arrows
and lettering; ANSI: 3 mm), so a leader note and a dimension on the same
sheet read at one size. Only ezdxf can author an MLEADERSTYLE object;
ActiveX exposes no collection for it (see ``backends/contracts/styles.py``).
"""

from __future__ import annotations

import math
from typing import Any

from engineering.standards.names import check_name

__all__ = ["MLEADER_KEYS", "MLEADER_PRESETS", "resolve_mleaderstyle"]

MLEADER_PRESETS: dict[str, dict[str, Any]] = {
    "iso": {"arrow_size": 2.5, "landing_gap": 1.0, "text_style": "ISOCP", "text_height": 2.5},
    "ansi": {"arrow_size": 3.0, "landing_gap": 1.5, "text_style": "ROMANS", "text_height": 3.0},
}

MLEADER_KEYS: tuple[str, ...] = ("arrow_size", "landing_gap", "text_style", "text_height")


def _positive(key: str, value: Any, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key}: expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{key}: {value!r} is not a finite number")
    if number < 0.0 or (number == 0.0 and not allow_zero):
        raise ValueError(f"{key}: {value!r} must be greater than 0")
    return number


def resolve_mleaderstyle(preset: str, overrides: dict | None = None) -> dict[str, Any]:
    """The preset's four values with ``overrides`` applied; refusals name the key."""
    key = str(preset).strip().lower() if preset is not None else ""
    if key not in MLEADER_PRESETS:
        raise ValueError(f"preset: unknown value {preset!r}; use one of {sorted(MLEADER_PRESETS)}")
    values = dict(MLEADER_PRESETS[key])
    if overrides is None:
        return values
    if not isinstance(overrides, dict):
        raise TypeError(f"overrides: expected a mapping, got {type(overrides).__name__}")
    for raw_key, value in overrides.items():
        name = str(raw_key).strip().lower()
        if name not in MLEADER_KEYS:
            raise ValueError(
                f"overrides: {raw_key!r} is not a leader style key; allowed: {list(MLEADER_KEYS)}"
            )
        if name == "text_style":
            values[name] = check_name(name, value, what="text style name")
        elif name == "landing_gap":
            values[name] = _positive(name, value, allow_zero=True)
        else:
            values[name] = _positive(name, value)
    return values
