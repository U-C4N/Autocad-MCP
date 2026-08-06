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
        dimscale, text_size, point_mode, point_size, osmode, fillet_radius.
        `units` accepts mm/cm/m/inch/feet (mapped to the INSUNITS code)."""
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
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
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
