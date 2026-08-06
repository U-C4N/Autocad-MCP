"""Friendly facade over system variables.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from typing import Any


class SettingsContract:
    # A user-facing wrapper over system_get_variable / system_set_variable that
    # maps memorable names ("units", "dimscale", …) to AutoCAD system variables.
    # Concrete + backend-agnostic: both engines accept the bare sysvar name, so
    # the same call reads/writes on live COM and headless ezdxf alike.

    async def drawing_settings(self, settings: dict | None = None) -> dict:
        """Read (no args) or apply (with args) common drawing settings.

        With ``settings=None`` returns a snapshot of every known setting. With a
        dict, applies each provided key and returns ``{applied, errors, current}``.
        Friendly keys: units, linear_precision, angular_precision, ltscale,
        dimscale, dim_text_height, dim_arrow_size, dim_decimals,
        decimal_separator, zero_suppression, text_size, point_mode, point_size,
        osmode, fillet_radius. `units` accepts mm/cm/m/inch/feet (mapped to the
        INSUNITS code) and `decimal_separator` accepts "." or ",".

        Note that a no-argument snapshot is one ``system_get_variable`` per key.
        Free headless; a COM round trip each on a live seat."""
        if not settings:
            snapshot: dict = {}
            for key, (var, kind) in _SETTING_MAP.items():
                try:
                    raw = await self.system_get_variable(var)
                except Exception as exc:
                    snapshot[key] = {"error": str(exc)}
                    continue
                snapshot[key] = _decode_setting(key, kind, raw)
            return {"ok": True, "settings": snapshot}

        applied: dict = {}
        errors: dict = {}
        for key, value in settings.items():
            spec = _SETTING_MAP.get(key)
            if spec is None:
                errors[key] = f"unknown setting (valid: {sorted(_SETTING_MAP)})"
                continue
            var, kind = spec
            try:
                encoded = _encode_setting(key, kind, value)
                await self.system_set_variable(var, encoded)
                applied[key] = value
            except Exception as exc:
                errors[key] = str(exc)

        result = {"ok": not errors, "applied": applied}
        if errors:
            result["errors"] = errors
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Friendly setting name -> (AutoCAD system variable, value kind).
_SETTING_MAP: dict[str, tuple[str, str]] = {
    "units": ("INSUNITS", "units"),
    "linear_precision": ("LUPREC", "int"),
    "angular_precision": ("AUPREC", "int"),
    "ltscale": ("LTSCALE", "float"),
    "dimscale": ("DIMSCALE", "float"),
    # The dimension variables had no friendly name until v1.5.2, so the only
    # way to reach dimension lettering was a raw `system_set_variable` — which
    # is precisely the path that silently did nothing on the headless engine.
    # `text_size` is TEXTSIZE, the height of a standalone TEXT entity; it has
    # never had anything to do with the numbers on a dimension.
    "dim_text_height": ("DIMTXT", "float"),
    "dim_arrow_size": ("DIMASZ", "float"),
    "dim_decimals": ("DIMDEC", "int"),
    "decimal_separator": ("DIMDSEP", "char"),
    "zero_suppression": ("DIMZIN", "int"),
    "text_size": ("TEXTSIZE", "float"),
    "point_mode": ("PDMODE", "int"),
    "point_size": ("PDSIZE", "float"),
    "osmode": ("OSMODE", "int"),
    "fillet_radius": ("FILLETRAD", "float"),
}

# INSUNITS code table (AutoCAD $INSUNITS): friendly name <-> integer code.
_UNIT_TO_CODE: dict[str, int] = {
    "unitless": 0,
    "inch": 1,
    "inches": 1,
    "in": 1,
    "feet": 2,
    "ft": 2,
    "foot": 2,
    "mm": 4,
    "millimeter": 4,
    "millimeters": 4,
    "cm": 5,
    "centimeter": 5,
    "centimeters": 5,
    "m": 6,
    "meter": 6,
    "meters": 6,
}
_CODE_TO_UNIT: dict[int, str] = {0: "unitless", 1: "inch", 2: "feet", 4: "mm", 5: "cm", 6: "m"}

#: DIMDSEP is a character *code*, not a character — ezdxf raises DXFValueError
#: on a string in the header — so the friendly key is where the translation
#: belongs. Only two markers are in real use: ISO 129's point and the comma most
#: of continental Europe drafts with. Anything else lands an arbitrary glyph in
#: the middle of every number on the sheet, so it is refused rather than stored.
_DECIMAL_MARKERS = (".", ",")


def _encode_setting(key: str, kind: str, value: Any) -> Any:
    """Coerce a friendly value into the raw system-variable value."""
    if kind == "units":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        code = _UNIT_TO_CODE.get(str(value).strip().lower())
        if code is None:
            raise ValueError(
                f"units: unknown value {value!r}. Use one of "
                f"{sorted(set(_UNIT_TO_CODE))} or an INSUNITS integer."
            )
        return code
    if kind == "char":
        if isinstance(value, bool):
            marker = ""
        elif isinstance(value, (int, float)):
            code = int(value)
            marker = chr(code) if 0 < code < 0x110000 else ""
        else:
            marker = str(value).strip()
        if marker not in _DECIMAL_MARKERS:
            raise ValueError(
                f"{key}: unknown value {value!r}. Use one of {list(_DECIMAL_MARKERS)}."
            )
        return ord(marker)
    if kind == "int":
        return _in_range(key, int(float(value)))
    if kind == "float":
        return _in_range(key, float(value))
    return value


#: AutoCAD's own ranges for the settings that now reach the renderer.
#:
#: Before the DIM* header fold, an out-of-range value died harmlessly in the
#: header. Folding it into the per-dimension override is what gave it teeth:
#: `dim_decimals=-1` reported `ok: True` and then killed the *next* dimension
#: with `ValueError: Format specifier missing precision` raised from inside
#: ezdxf's formatter, nowhere near the call responsible; `dim_decimals=40`
#: printed forty digits of float noise onto the sheet; and a non-positive
#: length was reported applied and silently discarded. `applied` has to mean
#: applied.
_SETTING_RANGES: dict[str, tuple[float, float]] = {
    "dim_decimals": (0, 8),  # DIMDEC
    "zero_suppression": (0, 15),  # DIMZIN is a bitmask
    "linear_precision": (0, 8),  # LUPREC
    "angular_precision": (0, 8),  # AUPREC
    "dim_text_height": (1e-9, 1e6),  # DIMTXT, strictly positive
    "dim_arrow_size": (1e-9, 1e6),  # DIMASZ, strictly positive
    "text_size": (1e-9, 1e6),  # TEXTSIZE
    "dimscale": (0, 1e6),  # 0 means "scale to the viewport" and is legal
    "ltscale": (1e-9, 1e6),
    "point_size": (-100, 1e6),  # negative is a percentage of the screen
    "fillet_radius": (0, 1e6),
}


def _in_range(key: str, value: float) -> float:
    """Refuse a value the renderer cannot use, naming the range."""
    bounds = _SETTING_RANGES.get(key)
    if bounds is None:
        return value
    low, high = bounds
    if not low <= value <= high:
        low_text = "greater than 0" if 0 < low < 1e-6 else f"at least {low:g}"
        raise ValueError(
            f"{key}: {value:g} is out of range - must be {low_text} and at most {high:g}."
        )
    return value


def _decode_setting(key: str, kind: str, raw: Any) -> Any:
    """Present a raw system-variable value in friendly form."""
    if raw is None:
        return None
    if kind == "units":
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return raw
        return {"code": code, "name": _CODE_TO_UNIT.get(code, "unknown")}
    if kind == "char":
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return raw
        # ezdxf's renderer reads DIMDSEP 0 as a comma rather than as "unset",
        # so report the marker that would actually be drawn.
        return "," if code == 0 else chr(code)
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    return raw
