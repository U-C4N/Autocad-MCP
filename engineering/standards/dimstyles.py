"""ISO-25 and ANSI dimension style presets as data.

Every value in ``PRESETS`` is pinned by name in
``tests/test_standards_presets.py`` against the table in §4.1 of
``docs/superpowers/specs/2026-09-16-v1.6-settings-design.md``:

* ``iso-25`` — ISO 129-1:2018 dimensioning at the 2.5 mm lettering row of
  ISO 3098-1: 2.5 mm text and arrowheads, text above and aligned with the
  dimension line, decimal comma (ISO 80000-1 allows either marker; AutoCAD's
  own ISO-25 uses the comma).
* ``ansi`` — ASME Y14.2-2014 line conventions and lettering (3 mm / .12 in
  lettering, .125 in arrowheads) in metric values: 3 mm text and arrowheads,
  text centred in a gap and horizontal, decimal point.

``DIMDSEP`` is a one-character *string* here. DXF stores the character code and
ezdxf raises on a string in the header, so the translation belongs to the
backends, not to the data.

Pure data plus validation: no I/O, no ezdxf, no COM, so the backends, the
tools and the provenance tests all read one copy.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "ANSI",
    "DIM_VARIABLE_RANGES",
    "DIM_VARIABLE_WHITELIST",
    "ISO_25",
    "LINEWEIGHT_CODES",
    "PRESETS",
    "PRESET_SOURCES",
    "PRESET_VARIABLES",
    "check_dim_value",
    "describe_preset",
    "resolve_dimstyle",
    "validate_overrides",
]

#: The seventeen variables every preset sets, in the order the spec table
#: lists them. ``dimstyle_list`` reports exactly these.
PRESET_VARIABLES: tuple[str, ...] = (
    "DIMTXT",
    "DIMASZ",
    "DIMEXE",
    "DIMEXO",
    "DIMGAP",
    "DIMTAD",
    "DIMTIH",
    "DIMTOH",
    "DIMDEC",
    "DIMDSEP",
    "DIMLUNIT",
    "DIMZIN",
    "DIMBLK",
    "DIMTXSTY",
    "DIMLWD",
    "DIMLWE",
    "DIMSCALE",
)

ISO_25: dict[str, Any] = {
    "DIMTXT": 2.5,  # text height, mm — ISO 3098-1 lettering row 2.5
    "DIMASZ": 2.5,  # arrowhead length, matched to the text
    "DIMEXE": 1.25,  # extension line beyond the dimension line
    "DIMEXO": 0.625,  # extension line offset from the feature
    "DIMGAP": 0.625,  # gap between dimension line and text
    "DIMTAD": 1,  # text above the dimension line
    "DIMTIH": 0,  # inside text aligned with the dimension line
    "DIMTOH": 0,  # outside text aligned with the dimension line
    "DIMDEC": 2,  # linear decimals
    "DIMDSEP": ",",  # decimal comma
    "DIMLUNIT": 2,  # decimal linear units
    "DIMZIN": 8,  # trailing zeros suppressed
    "DIMBLK": "",  # closed filled arrowhead
    "DIMTXSTY": "ISOCP",  # ISO 3098 type B lettering (created if missing)
    "DIMLWD": -2,  # dimension line lineweight ByBlock
    "DIMLWE": -2,  # extension line lineweight ByBlock
    "DIMSCALE": 1.0,
}

ANSI: dict[str, Any] = {
    "DIMTXT": 3.0,  # 3 mm / .12 in lettering, ASME Y14.2-2014
    "DIMASZ": 3.0,  # .125 in arrowheads, metric
    "DIMEXE": 1.5,
    "DIMEXO": 1.5,
    "DIMGAP": 1.0,
    "DIMTAD": 0,  # text centred in a gap in the dimension line
    "DIMTIH": 1,  # inside text horizontal
    "DIMTOH": 1,  # outside text horizontal
    "DIMDEC": 2,
    "DIMDSEP": ".",  # decimal point
    "DIMLUNIT": 2,
    "DIMZIN": 8,
    "DIMBLK": "",
    "DIMTXSTY": "ROMANS",  # AutoCAD's simplex roman, the ASME Gothic look
    "DIMLWD": -2,
    "DIMLWE": -2,
    "DIMSCALE": 1.0,
}

PRESETS: dict[str, dict[str, Any]] = {"iso-25": ISO_25, "ansi": ANSI}

PRESET_SOURCES: dict[str, str] = {
    "iso-25": (
        "ISO 129-1:2018 (dimensioning) with the 2.5 mm lettering row of "
        "ISO 3098-1 (text and arrow sizes); decimal comma per ISO 80000-1"
    ),
    "ansi": (
        "ASME Y14.2-2014 line conventions and lettering: 3 mm / .12 in lettering, "
        ".125 in arrowheads, metric values"
    ),
}

#: The lineweights AutoCAD accepts, in hundredths of a millimetre, plus the
#: ByLayer / ByBlock / Default sentinels. Every ISO 128 weight is in here.
_LINEWEIGHT_TEXT = (
    "-3 -2 -1 0 5 9 13 15 18 20 25 30 35 40 50 53 60 70 80 90 100 106 120 140 158 200 211"
)
LINEWEIGHT_CODES: frozenset[int] = frozenset(int(code) for code in _LINEWEIGHT_TEXT.split())

#: What ``dimstyle_create`` / ``dimstyle_modify`` will write, and the range each
#: value must sit in. The shape of each entry:
#:
#: * ``("float", low, high)`` — a number (bool refused) within the inclusive
#:   bounds; a ``low`` of ``1e-9`` is the repo's spelling of "strictly positive"
#:   (``backends/contracts/settings.py::_SETTING_RANGES``).
#: * ``("int", low, high)`` — an integer (a float with no fraction is accepted).
#: * ``("nonzero_float", low, high)`` — as ``float`` but ``0`` is refused.
#: * ``("char",)`` — exactly one character.
#: * ``("str",)`` — any string (arrowhead block names; ``""`` is the closed filled arrow).
#: * ``("name",)`` — a non-empty DXF symbol-table name.
#: * a ``set`` — one of its members.
DIM_VARIABLE_RANGES: dict[str, tuple | set] = {
    # the seventeen the presets set
    "DIMTXT": ("float", 1e-9, 1e6),
    "DIMASZ": ("float", 1e-9, 1e6),
    "DIMEXE": ("float", 0.0, 1e6),
    "DIMEXO": ("float", 0.0, 1e6),
    "DIMGAP": ("float", -1e6, 1e6),  # negative draws the basic-dimension box
    "DIMTAD": ("int", 0, 4),
    "DIMTIH": {0, 1},
    "DIMTOH": {0, 1},
    "DIMDEC": ("int", 0, 8),
    "DIMDSEP": ("char",),
    "DIMLUNIT": ("int", 1, 6),
    "DIMZIN": ("int", 0, 15),  # a bitmask
    "DIMBLK": ("str",),
    "DIMTXSTY": ("name",),
    "DIMLWD": LINEWEIGHT_CODES,
    "DIMLWE": LINEWEIGHT_CODES,
    "DIMSCALE": ("float", 0.0, 1e6),  # 0 means "scale to the viewport"
    # tolerances, rounding, fit, colours, arrowheads — the rest of the whitelist
    "DIMTOL": {0, 1},
    "DIMTP": ("float", 0.0, 1e6),
    "DIMTM": ("float", 0.0, 1e6),
    "DIMTOLJ": ("int", 0, 2),
    "DIMTFAC": ("float", 1e-9, 1e6),
    "DIMLFAC": ("nonzero_float", -1e6, 1e6),
    "DIMRND": ("float", 0.0, 1e6),  # 0 = no rounding; the backends discard it, never store it
    "DIMATFIT": ("int", 0, 3),
    "DIMTMOVE": ("int", 0, 2),
    "DIMCLRD": ("int", 0, 256),
    "DIMCLRE": ("int", 0, 256),
    "DIMCLRT": ("int", 0, 256),
    "DIMSAH": {0, 1},
    "DIMBLK1": ("str",),
    "DIMBLK2": ("str",),
    "DIMCEN": ("float", -1e6, 1e6),  # negative draws centre lines, 0 none
}

DIM_VARIABLE_WHITELIST: frozenset[str] = frozenset(DIM_VARIABLE_RANGES)

#: DXF forbids these in a symbol-table name; the same rule ``security.py``
#: applies to block and layer names, restated here so this module stays free
#: of fastmcp.
_ILLEGAL_NAME_CHARS = frozenset('<>/\\":;?*|,=`')


def _number(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key}: expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{key}: {value!r} is not a finite number")
    return number


def check_dim_value(name: str, value: Any) -> Any:
    """``value`` coerced to what ``name`` stores, or ``TypeError``/``ValueError`` naming it.

    Nothing here writes; the backends call this before touching a table.
    """
    key = str(name).strip().upper()
    spec = DIM_VARIABLE_RANGES.get(key)
    if spec is None:
        raise ValueError(
            f"{name}: not a dimension variable this tool writes; allowed: "
            f"{sorted(DIM_VARIABLE_WHITELIST)}"
        )
    if isinstance(spec, (set, frozenset)):
        number = _number(key, value)
        if number != int(number) or int(number) not in spec:
            raise ValueError(f"{key}: {value!r} is not one of {sorted(spec)}")
        return int(number)
    kind = spec[0]
    if kind in ("float", "nonzero_float"):
        number = _number(key, value)
        low, high = spec[1], spec[2]
        if not low <= number <= high:
            low_text = "greater than 0" if 0 < low < 1e-6 else f"at least {low:g}"
            raise ValueError(
                f"{key}: {number:g} is out of range - must be {low_text} and at most {high:g}."
            )
        if kind == "nonzero_float" and number == 0.0:
            raise ValueError(f"{key}: 0 is refused (AutoCAD treats it as no factor at all)")
        return number
    if kind == "int":
        number = _number(key, value)
        if number != int(number):
            raise ValueError(f"{key}: {value!r} must be a whole number")
        low, high = spec[1], spec[2]
        if not low <= number <= high:
            raise ValueError(f"{key}: {int(number)} is out of range {low}..{high}")
        return int(number)
    if not isinstance(value, str):
        raise TypeError(f"{key}: expected a string, got {type(value).__name__}")
    if kind == "char":
        if len(value) != 1:
            raise ValueError(f"{key}: {value!r} must be exactly one character")
        return value
    if kind == "name":
        text = value.strip()
        if not text:
            raise ValueError(f"{key}: a style name cannot be empty")
        bad = sorted(set(text) & _ILLEGAL_NAME_CHARS)
        if bad:
            raise ValueError(f"{key}: {text!r} contains characters DXF forbids in a name: {bad}")
        return text
    return value  # "str"


def validate_overrides(overrides: dict | None) -> dict[str, Any]:
    """``overrides`` as a typed, upper-cased dict — every key checked, nothing written."""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise TypeError(f"overrides: expected a mapping, got {type(overrides).__name__}")
    typed: dict[str, Any] = {}
    for raw_key, value in overrides.items():
        key = str(raw_key).strip().upper()
        typed[key] = check_dim_value(key, value)
    return typed


def resolve_dimstyle(preset: str | None, overrides: dict | None) -> dict[str, Any]:
    """The preset's seventeen values with ``overrides`` applied on top.

    ``preset`` ``None`` means the ISO-25 base. An unknown preset, an override
    key outside :data:`DIM_VARIABLE_WHITELIST` or a value outside its range
    raises ``ValueError`` (``TypeError`` for the wrong type) naming the key —
    before anything is written, because nothing here writes.
    """
    key = "iso-25" if preset is None else str(preset).strip().lower()
    if key not in PRESETS:
        raise ValueError(f"preset: unknown value {preset!r}; use one of {sorted(PRESETS)}")
    values = dict(PRESETS[key])
    values.update(validate_overrides(overrides))
    return values


def describe_preset(name: str) -> dict:
    """``{name, source, values, textstyle}`` for a preset, or ``ValueError``."""
    key = str(name).strip().lower()
    if key not in PRESETS:
        raise ValueError(f"preset: unknown value {name!r}; use one of {sorted(PRESETS)}")
    return {
        "name": key,
        "source": PRESET_SOURCES[key],
        "values": dict(PRESETS[key]),
        "textstyle": PRESETS[key]["DIMTXSTY"],
    }
