"""Friendly facade over system variables.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.

v1.6 (track E) grows the facade from 15 keys to 29: drawing limits, grid and
snap, ortho and polar tracking, PSLTSCALE, the annotation scale, linear and
angular unit formats, and the current dimension / text style. Every key still
goes through ``system_get_variable`` / ``system_set_variable`` so the facade
stays backend-agnostic; the *engines* are where a variable's real home is
known (header, VPORT, the variable dictionary, or — headlessly — nowhere).
"""

from __future__ import annotations

import math
import re
from typing import Any


class SettingsContract:
    # A user-facing wrapper over system_get_variable / system_set_variable that
    # maps memorable names ("units", "dimscale", …) to AutoCAD system variables.
    # Concrete + backend-agnostic: both engines accept the bare sysvar name, so
    # the same call reads/writes on live COM and headless ezdxf alike.

    async def drawing_settings(self, settings: dict | None = None) -> dict:
        """Read (no args) or apply (with args) common drawing settings.

        With ``settings=None`` returns a snapshot of every known setting. With a
        dict, applies each provided key and returns ``{applied, changed,
        errors}`` — ``changed`` holds ``[old, new]`` only for keys whose value
        actually moved (re-setting a value to itself is not a change).

        Friendly keys: units, linear_precision, angular_precision, ltscale,
        dimscale, dim_text_height, dim_arrow_size, dim_decimals,
        decimal_separator, zero_suppression, text_size, point_mode, point_size,
        osmode, fillet_radius, limits, grid, grid_spacing, snap, snap_spacing,
        ortho, polar, polar_angle, psltscale, annotation_scale, linear_units,
        angular_units, dimstyle, textstyle.

        Refusals (per key, in ``errors``; the other keys still apply): an
        unknown key; a value outside ``_SETTING_RANGES``; a malformed
        ``limits`` / ``annotation_scale``; ``annotation_scale`` on an R12 file
        headlessly (no OBJECTS section, so the value would vanish at save —
        save as R2000 or newer first); ``osmode`` / ``polar`` / ``polar_angle``
        on the headless engine (registry-saved — ``capability: registry_sysvar``;
        a snapshot reports them as ``None`` there, a file holds no value);
        ``dimstyle`` / ``textstyle`` when the backend has no styles contract.

        Note that a no-argument snapshot is one ``system_get_variable`` per key
        (two for ``limits``). Free headless; a COM round trip each on a live
        seat, and a write costs a read before and after it for ``changed``."""
        if not settings:
            snapshot: dict = {}
            for key, spec in _SETTING_MAP.items():
                try:
                    snapshot[key] = await self._read_setting(key, spec)
                except Exception as exc:
                    snapshot[key] = {"error": str(exc)}
            return {"ok": True, "settings": snapshot}

        applied: dict = {}
        changed: dict = {}
        errors: dict = {}
        for key, value in settings.items():
            spec = _SETTING_MAP.get(key)
            if spec is None:
                errors[key] = f"unknown setting (valid: {sorted(_SETTING_MAP)})"
                continue
            try:
                before = await self._read_setting(key, spec)
                await self._write_setting(key, spec, value)
                after = await self._read_setting(key, spec)
                applied[key] = value
                if after != before:
                    changed[key] = [before, after]
            except Exception as exc:
                errors[key] = str(exc)

        result = {"ok": not errors, "applied": applied, "changed": changed}
        if errors:
            result["errors"] = errors
        return result

    async def _read_setting(self, key: str, spec: tuple[str, str]) -> Any:
        var, kind = spec
        if kind == "limits":
            low = await self.system_get_variable("LIMMIN")
            high = await self.system_get_variable("LIMMAX")
            if low is None or high is None:
                return None
            return [_point2(low), _point2(high)]
        return _decode_setting(key, kind, await self.system_get_variable(var))

    async def _write_setting(self, key: str, spec: tuple[str, str], value: Any) -> None:
        var, kind = spec
        if kind == "limits":
            low, high = _encode_limits(key, value)  # validated before either write
            await self.system_set_variable("LIMMIN", low)
            await self.system_set_variable("LIMMAX", high)
            return
        if kind == "name":
            name = _encode_name(key, value)
            setter = getattr(self, _STYLE_SETTERS[key], None)
            if not callable(setter):
                raise ValueError(
                    f"{key}: this backend has no {_STYLE_SETTERS[key]} (styles contract "
                    f"absent); the current {key} can be read but not changed here."
                )
            await setter(name)
            return
        if kind == "bit":
            flag = _encode_bool(key, value)
            raw = await self.system_get_variable(var)
            current = int(raw) if raw is not None else 0
            mask = _BIT_MASKS[key]
            await self.system_set_variable(var, (current | mask) if flag else (current & ~mask))
            return
        await self.system_set_variable(var, _encode_setting(key, kind, value))


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
    # OSMODE is registry-saved (a DXF R2000+ file has no $OSMODE), so the
    # headless engine refuses it like the polar pair below.
    "osmode": ("OSMODE", "int"),
    "fillet_radius": ("FILLETRAD", "float"),
    # ── track E: the environment a drawing lives in ──────────────────────
    # `limits` is one friendly key over two variables; `_read_setting` and
    # `_write_setting` special-case the kind, the variable named here is the
    # first of the pair.
    "limits": ("LIMMIN", "limits"),
    # GRIDMODE / GRIDUNIT / SNAPMODE / SNAPUNIT are not header variables in a
    # DXF file — AutoCAD keeps them on the active VPORT — so the headless engine
    # routes them there (see `EzdxfBackend.system_set_variable`).
    "grid": ("GRIDMODE", "bool"),
    "grid_spacing": ("GRIDUNIT", "spacing"),
    "snap": ("SNAPMODE", "bool"),
    "snap_spacing": ("SNAPUNIT", "spacing"),
    "ortho": ("ORTHOMODE", "bool"),
    # Polar tracking on/off is AUTOSNAP bit 8 (the F10 toggle), not POLARMODE
    # — POLARMODE's bits choose *how* polar angles are measured. Both AUTOSNAP
    # and POLARANG are registry-saved, so a headless file cannot hold them.
    "polar": ("AUTOSNAP", "bit"),
    "polar_angle": ("POLARANG", "degrees"),
    "psltscale": ("PSLTSCALE", "bool"),
    # CANNOSCALE is a DICTIONARYVAR in AcDbVariableDictionary, not a header
    # variable; CANNOSCALEVALUE is derived from it and read-only.
    "annotation_scale": ("CANNOSCALE", "scale"),
    "linear_units": ("LUNITS", "lunits"),
    "angular_units": ("AUNITS", "aunits"),
    # The current style names read through the header / GetVariable; a write
    # delegates to the styles contract (`dimstyle_set_current` /
    # `textstyle_set_current`) because DIMSTYLE is a read-only variable.
    "dimstyle": ("DIMSTYLE", "name"),
    "textstyle": ("TEXTSTYLE", "name"),
}

_STYLE_SETTERS = {"dimstyle": "dimstyle_set_current", "textstyle": "textstyle_set_current"}

#: AUTOSNAP bit 8 = polar tracking on. (1 marker, 2 magnet, 4 tooltip, 16
#: object snap tracking, 32 tracking tooltips, 64 dynamic input aperture.)
_BIT_MASKS = {"polar": 8}

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

# LUNITS (linear unit format) and AUNITS (angular unit format) code tables.
_LUNITS_TO_CODE: dict[str, int] = {
    "scientific": 1,
    "decimal": 2,
    "engineering": 3,
    "architectural": 4,
    "fractional": 5,
}
_CODE_TO_LUNITS: dict[int, str] = {code: name for name, code in _LUNITS_TO_CODE.items()}
_AUNITS_TO_CODE: dict[str, int] = {
    "degrees": 0,
    "dms": 1,
    "grads": 2,
    "radians": 3,
    "surveyor": 4,
}
_CODE_TO_AUNITS: dict[int, str] = {code: name for name, code in _AUNITS_TO_CODE.items()}

#: DIMDSEP is a character *code*, not a character — ezdxf raises DXFValueError
#: on a string in the header — so the friendly key is where the translation
#: belongs. Only two markers are in real use: ISO 129's point and the comma most
#: of continental Europe drafts with. Anything else lands an arbitrary glyph in
#: the middle of every number on the sheet, so it is refused rather than stored.
_DECIMAL_MARKERS = (".", ",")

#: "1:50", "2:1", "1:2.5" — paper units : drawing units, both strictly positive.
SCALE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$")


def parse_scale(text: Any) -> tuple[str, float, float]:
    """``"1:50"`` → ``("1:50", 1.0, 50.0)``; raises ``ValueError`` naming the input.

    Returns the canonical name (whitespace stripped, integers without a
    trailing ``.0``), the paper units and the drawing units. The annotation
    scale value AutoCAD reports as CANNOSCALEVALUE is ``paper / drawing``.
    """
    match = SCALE_RE.match(str(text)) if not isinstance(text, bool) else None
    if match is None:
        raise ValueError(
            f"annotation_scale: {text!r} is not a scale — use 'paper:drawing' such as "
            "'1:1', '1:50' or '2:1'."
        )
    paper, drawing = float(match.group(1)), float(match.group(2))
    if paper <= 0 or drawing <= 0:
        raise ValueError(f"annotation_scale: both sides of {text!r} must be greater than 0.")
    return f"{_trim(paper)}:{_trim(drawing)}", paper, drawing


def scale_value(text: Any) -> float:
    """CANNOSCALEVALUE for a scale name: ``"1:50"`` → ``0.02``."""
    _, paper, drawing = parse_scale(text)
    return paper / drawing


def _trim(number: float) -> str:
    return str(int(number)) if number == int(number) else f"{number:g}"


def _point2(raw: Any) -> list[float]:
    """A 2D point from whatever the engine returned (tuple, list, Vec2/Vec3)."""
    try:
        return [float(raw[0]), float(raw[1])]
    except (TypeError, IndexError, ValueError) as exc:
        raise ValueError(f"expected a point, got {raw!r}") from exc


def _finite(key: str, value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key}: {what} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{key}: {what} must be finite")
    return number


def _encode_limits(key: str, value: Any) -> tuple[tuple[float, float], tuple[float, float]]:
    """``[[xmin, ymin], [xmax, ymax]]`` → two 2D tuples; refused before any write."""
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(not isinstance(corner, (list, tuple)) or len(corner) != 2 for corner in value)
    ):
        raise ValueError(f"{key}: expected [[xmin, ymin], [xmax, ymax]], got {value!r}")
    xmin = _finite(key, value[0][0], "xmin")
    ymin = _finite(key, value[0][1], "ymin")
    xmax = _finite(key, value[1][0], "xmax")
    ymax = _finite(key, value[1][1], "ymax")
    if xmax <= xmin or ymax <= ymin:
        raise ValueError(
            f"{key}: the upper-right corner must be above and right of the lower-left "
            f"(got {[[xmin, ymin], [xmax, ymax]]})"
        )
    return (xmin, ymin), (xmax, ymax)


_TRUE_WORDS = {"1", "on", "true", "yes"}
_FALSE_WORDS = {"0", "off", "false", "no"}


def _encode_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise ValueError(f"{key}: expected true/false (or 1/0, on/off), got {value!r}")


def _encode_name(key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key}: expected a non-empty style name, got {value!r}")
    return value.strip()


def _encode_enum(key: str, value: Any, table: dict[str, int]) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        code = int(value)
        if code in table.values():
            return code
        raise ValueError(f"{key}: code {code} is not one of {sorted(table.values())}")
    name = str(value).strip().lower()
    if name in table:
        return table[name]
    raise ValueError(f"{key}: unknown value {value!r}. Use one of {sorted(table)}.")


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
    if kind == "lunits":
        return _encode_enum(key, value, _LUNITS_TO_CODE)
    if kind == "aunits":
        return _encode_enum(key, value, _AUNITS_TO_CODE)
    if kind == "bool":
        return 1 if _encode_bool(key, value) else 0
    if kind == "scale":
        name, _paper, _drawing = parse_scale(value)
        return name
    if kind == "spacing":
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError(f"{key}: expected a number or [x, y], got {value!r}")
            x = _in_range(key, _finite(key, value[0], "x"))
            y = _in_range(key, _finite(key, value[1], "y"))
            return (x, y)
        step = _in_range(key, _finite(key, value, "spacing"))
        return (step, step)
    if kind == "degrees":
        degrees = _in_range(key, _finite(key, value, "angle"))
        return math.radians(degrees)
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
    # track E
    "grid_spacing": (1e-9, 1e9),  # GRIDUNIT, strictly positive per axis
    "snap_spacing": (1e-9, 1e9),  # SNAPUNIT, strictly positive per axis
    "polar_angle": (1e-9, 360),  # POLARANG increment, degrees at the facade
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
    if kind == "lunits":
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return raw
        return {"code": code, "name": _CODE_TO_LUNITS.get(code, "unknown")}
    if kind == "aunits":
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return raw
        return {"code": code, "name": _CODE_TO_AUNITS.get(code, "unknown")}
    if kind == "bool":
        try:
            return bool(int(raw))
        except (TypeError, ValueError):
            return raw
    if kind == "bit":
        try:
            return bool(int(raw) & _BIT_MASKS[key])
        except (TypeError, ValueError):
            return raw
    if kind == "degrees":
        try:
            return round(math.degrees(float(raw)), 6)
        except (TypeError, ValueError):
            return raw
    if kind == "spacing":
        try:
            x, y = _point2(raw)
        except ValueError:
            return raw
        return x if x == y else [x, y]
    if kind == "scale":
        try:
            name, paper, drawing = parse_scale(raw)
        except ValueError:
            return {"name": str(raw), "value": None}
        return {"name": name, "value": paper / drawing}
    if kind == "name":
        return str(raw)
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


# ---------------------------------------------------------------------------
# Document properties (DWGPROPS) — validation shared by both engines
# ---------------------------------------------------------------------------

#: The SummaryInfo fields AutoCAD's DWGPROPS dialog shows on its Summary tab.
SUMMARY_FIELDS = ("title", "subject", "author", "keywords", "comments")


#: The printable characters AutoCAD's ``SummaryInfo.AddCustomInfo`` rejects
#: anywhere inside a custom key with ``Invalid key``. Measured on AutoCAD 2026
#: by sweeping every character 0x20-0x7E through ``AddCustomInfo`` on a scratch
#: document: exactly these thirteen are refused; every other printable ASCII
#: character, every control character (0x01-0x1F, including tab), internal
#: spaces, a no-break space and other unicode are accepted. Leading or
#: trailing whitespace (space, tab or no-break space) is refused separately.
_CUSTOM_KEY_FORBIDDEN = frozenset('"*,/:;<=>?\\`|')


def custom_key_fold(key: str) -> str:
    """Fold a custom-property key the way AutoCAD compares them.

    AutoCAD's ``AddCustomInfo`` / ``SetCustomByKey`` / ``RemoveCustomByKey``
    match keys by a *simple* one-to-one case compare, not Unicode full case
    folding. Measured on AutoCAD 2026 (``AddCustomInfo`` of the second
    spelling over the first): ``Project``/``PROJECT``, ``Grün``/``GRÜN``,
    ``é``/``É``, ``ÿ``/``Ÿ``, ``ł``/``Ł``, ``σ``/``Σ``, ``я``/``Я`` and
    ``ǆ``/``Ǆ`` are the same key (``Duplicate key``), while ``Straße``/
    ``STRASSE``, ``ẞ``/``ß``, ``kelvin``/``Kelvin`` (Kelvin sign), ``µ``/``Μ``,
    ``σς``/``ΣΣ`` and ``ǅ``/``Ǆ`` are distinct (both stored) — every one of
    which Python's ``str.casefold()`` calls equal. So this folds each character
    to its lowercase only when the mapping is a single character that
    round-trips (``lower().upper() == upper()``, ``upper().lower() == lower()``
    and the character is one of the two); anything else — ``ß`` (uppercases
    to ``SS``), the final sigma ``ς``, the micro sign ``µ``, the Kelvin sign,
    the title-case digraph ``ǅ``, dotless ``ı`` and dotted ``İ`` — is kept as
    is. The result never changes length. This is what the headless engine and
    the validator match on; the live engine lets AutoCAD decide (see
    ``ComBackend.drawing_properties_set``).
    """
    out: list[str] = []
    for c in key:
        lo, up = c.lower(), c.upper()
        if (
            len(lo) == 1
            and len(up) == 1
            and lo.upper() == up
            and up.lower() == lo
            and c in (lo, up)
        ):
            out.append(lo)
        else:
            out.append(c)
    return "".join(out)


def _check_custom_text(key: str, text: str, what: str) -> None:
    """A newline in a custom key or value corrupts the DXF on save.

    ezdxf writes custom properties verbatim as ``9 / $CUSTOMPROPERTYTAG / 1 /
    {key}`` and ``9 / $CUSTOMPROPERTY / 1 / {value}`` header lines — a line
    break inside either text starts a new (invalid) group and the reopened
    file fails with ``DXFStructureError``. Refused here so neither engine
    writes it.
    """
    if "\n" in text or "\r" in text:
        raise ValueError(
            f"custom[{key!r}]: the {what} contains a line break, which corrupts the "
            "DXF on save (the whole drawing would fail to reopen, not just the "
            "property). Custom properties are single-line."
        )


def _check_custom_key_for_write(key: str) -> None:
    """Mirror AutoCAD's ``AddCustomInfo`` key syntax so the live engine cannot
    refuse mid-write. Measured on AutoCAD 2026: ``Invalid key`` for a key with
    leading/trailing whitespace (space, tab, no-break space) or any of
    ``_CUSTOM_KEY_FORBIDDEN`` anywhere in it — raised after the summary fields
    and the earlier keys were already applied, while the headless engine
    writes the same key without complaint. Only a *write* goes through
    ``AddCustomInfo``; ``RemoveCustomByKey`` does not validate syntax (measured:
    ``Key not found``, never ``Invalid key``), so a delete is not held to these
    rules — a key that reached the drawing by other means (a DXF written by
    ezdxf, say) must stay removable."""
    if key != key.strip():
        raise ValueError(
            f"custom: key {key!r} has leading or trailing whitespace — AutoCAD's "
            "AddCustomInfo rejects it as 'Invalid key'. Strip the key."
        )
    bad = sorted(set(key) & _CUSTOM_KEY_FORBIDDEN)
    if bad:
        shown = " ".join(repr(c) for c in bad)
        raise ValueError(
            f"custom: key {key!r} contains {shown} — AutoCAD's AddCustomInfo rejects "
            f"it as 'Invalid key' (refused characters: {''.join(sorted(_CUSTOM_KEY_FORBIDDEN))}). "
            "Use another separator."
        )


def validate_drawing_properties(
    summary: dict | None, custom: dict | None
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """→ (summary fields to write, custom keys to write, custom keys to delete).

    Refuses the whole request by name before either engine writes anything:
    an unknown summary field is a ``ValueError``; a non-string summary value,
    a non-string or empty custom key, or a custom value that is neither a
    string nor ``None`` (``None`` deletes) is a ``TypeError``. Values are never
    coerced with ``str()`` — a number the caller meant as text is theirs to
    format.

    Custom keys and values are also held to the rules the *other* engine would
    enforce or the file format would break on, so the same call cannot succeed
    on one engine and half-apply on the other (``ValueError`` naming the key):

    * a key to *write* with leading or trailing whitespace, or containing any
      of the thirteen characters ``" * , / : ; < = > ? \\ ` |`` — AutoCAD's
      ``AddCustomInfo`` rejects exactly these as ``Invalid key`` mid-write
      (measured on AutoCAD 2026 over every printable ASCII character), after
      the summary fields and the earlier keys were already applied; the
      headless engine writes them without complaint, and AutoCAD then loads
      them from the DXF. A *delete* (``None``) is exempt: ``RemoveCustomByKey``
      never validates syntax, and such keys do reach drawings by other routes,
      so they must stay removable;
    * two keys in one request that AutoCAD would treat as the same key — its
      key compare is a simple one-to-one case compare (``custom_key_fold``;
      measured: ``AddCustomInfo("PROJECT")`` over an existing ``Project``
      raises ``Duplicate key``, while ``Straße`` and ``STRASSE`` are two
      keys), so the second would fail mid-write on the live engine and write
      a second tag headlessly;
    * a key or value containing a line break (LF or CR) — ezdxf emits custom
      properties unescaped, one header line per text, so the saved DXF is
      corrupt and the whole drawing fails to reopen (``DXFStructureError``),
      not just the property.
    """
    if summary is not None and not isinstance(summary, dict):
        raise TypeError("summary: must be an object of {field: text}")
    written: dict[str, str] = {}
    for field, value in (summary or {}).items():
        if field not in SUMMARY_FIELDS:
            raise ValueError(f"summary: unknown field {field!r} (valid: {list(SUMMARY_FIELDS)})")
        if value is None:
            continue
        if not isinstance(value, str):
            raise TypeError(f"summary.{field}: must be a string, got {type(value).__name__}")
        written[field] = value
    if custom is not None and not isinstance(custom, dict):
        raise TypeError("custom: must be an object of {key: text | null}")
    to_write: dict[str, str] = {}
    to_delete: list[str] = []
    seen: dict[str, str] = {}  # folded key (AutoCAD's rule) -> the spelling seen first
    for key, value in (custom or {}).items():
        if not isinstance(key, str) or not key.strip():
            raise TypeError(f"custom: key {key!r} must be a non-empty string")
        _check_custom_text(key, key, "key")
        folded = custom_key_fold(key)
        if folded in seen:
            raise ValueError(
                f"custom: keys {seen[folded]!r} and {key!r} differ only by case — "
                "AutoCAD compares custom keys with a simple per-character case "
                "compare and calls these the same key (AddCustomInfo raises "
                "'Duplicate key'). Mention the key once."
            )
        seen[folded] = key
        if value is None:
            to_delete.append(key)
        elif isinstance(value, str):
            _check_custom_key_for_write(key)
            _check_custom_text(key, value, "value")
            to_write[key] = value
        else:
            raise TypeError(
                f"custom[{key!r}]: must be a string or null (null deletes the key), "
                f"got {type(value).__name__}"
            )
    return written, to_write, to_delete
