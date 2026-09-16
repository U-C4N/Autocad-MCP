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

from engineering.standards.names import check_name

__all__ = [
    "ANSI",
    "ARROWHEAD_BLOCKS",
    "DIM_VARIABLE_RANGES",
    "DIM_VARIABLE_WHITELIST",
    "ISO_25",
    "LINEWEIGHT_CODES",
    "PRESETS",
    "PRESET_SOURCES",
    "PRESET_VARIABLES",
    "canonical_arrowhead",
    "check_dim_value",
    "describe_preset",
    "ezdxf_arrowhead",
    "reported_arrowhead",
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

#: AutoCAD's nineteen named built-in arrowheads, spelled the way the DIMBLK
#: documentation, the ``$DIMBLK`` header, ActiveX and a loaded ezdxf DIMSTYLE
#: all spell them: the block name, underscore first. The twentieth, closed
#: filled, has no block name and is ``""`` (the spec §4.1 default).
#: ``tests/test_standards_presets.py`` pins this set equal to ezdxf's
#: ``ARROWS.__acad__`` so the two vocabularies cannot drift apart unseen.
ARROWHEAD_BLOCKS: frozenset[str] = frozenset(
    "_ARCHTICK _BOXBLANK _BOXFILLED _CLOSED _CLOSEDBLANK _DATUMBLANK _DATUMFILLED "
    "_DOT _DOTBLANK _DOTSMALL _INTEGRAL _NONE _OBLIQUE _OPEN _OPEN30 _OPEN90 "
    "_ORIGIN _ORIGIN2 _SMALL".split()
)

#: The spellings of the closed filled default a caller may send: AutoCAD's own
#: "enter a single period to return to closed filled", and the block name
#: ezdxf gives the arrow it draws for it.
_CLOSED_FILLED_ALIASES = frozenset({".", "CLOSEDFILLED", "_CLOSEDFILLED"})

#: What ``dimstyle_create`` / ``dimstyle_modify`` will write, and the range each
#: value must sit in. The shape of each entry:
#:
#: * ``("float", low, high)`` — a number (bool refused) within the inclusive
#:   bounds; a ``low`` of ``1e-9`` is the repo's spelling of "strictly positive"
#:   (``backends/contracts/settings.py::_SETTING_RANGES``).
#: * ``("int", low, high)`` — an integer (a float with no fraction is accepted).
#: * ``("nonzero_float", low, high)`` — as ``float`` but ``0`` is refused.
#: * ``("char",)`` — exactly one character.
#: * ``("arrow",)`` — an arrowhead: ``""`` (closed filled), one of
#:   :data:`ARROWHEAD_BLOCKS` given with or without its underscore in any case,
#:   or a user block name under the symbol-name rule; see :func:`canonical_arrowhead`.
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
    "DIMBLK": ("arrow",),
    "DIMTXSTY": ("name",),
    "DIMLWD": LINEWEIGHT_CODES,
    "DIMLWE": LINEWEIGHT_CODES,
    "DIMSCALE": ("float", 0.0, 1e6),  # 0 means "scale to the viewport"
    # tolerances, rounding, fit, colours, arrowheads — the rest of the whitelist
    "DIMTOL": {0, 1},
    # Both signed, as AutoCAD documents them: the displayed lower deviation is
    # -DIMTM, so a double-positive fit (ISO 286 p6, +0.035/+0.022) stores
    # DIMTM -0.022 and a double-negative one (f7, -0.020/-0.041) stores DIMTP
    # -0.020 — exactly what engineering/tolerances.py::build_dim_override emits
    # per dimension; a style must be able to hold the same numbers.
    "DIMTP": ("float", -1e6, 1e6),
    "DIMTM": ("float", -1e6, 1e6),
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
    "DIMBLK1": ("arrow",),
    "DIMBLK2": ("arrow",),
    "DIMCEN": ("float", -1e6, 1e6),  # negative draws centre lines, 0 none
}

DIM_VARIABLE_WHITELIST: frozenset[str] = frozenset(DIM_VARIABLE_RANGES)


def canonical_arrowhead(key: str, value: Any) -> str:
    """``value`` as the arrowhead name DIMBLK stores, or ``TypeError`` /
    ``ValueError`` naming ``key``.

    ``""``, ``"."``, ``CLOSEDFILLED`` and ``_CLOSEDFILLED`` are the closed
    filled default and come back as ``""``. A built-in is accepted with or
    without its underscore in any case (``oblique``, ``_Dot``) and comes back
    as its block name (``_OBLIQUE``, ``_DOT``) — AutoCAD's documented spelling,
    what ActiveX takes verbatim and what ezdxf reports after a load. Anything
    else is a user block name and must obey the symbol-name rule; whether the
    block exists is the backend's question, asked against the open drawing.
    """
    if not isinstance(value, str):
        raise TypeError(f"{key}: expected a string, got {type(value).__name__}")
    if value == "":
        return ""
    text = value.strip()
    if text.upper() in _CLOSED_FILLED_ALIASES:
        return ""
    if not text:
        raise ValueError(
            f'{key}: {value!r} is blank; use "" for the closed filled arrowhead or a block name'
        )
    upper = text.upper()
    candidate = upper if upper.startswith("_") else "_" + upper
    if candidate in ARROWHEAD_BLOCKS:
        return candidate
    return check_name(key, text, what="block name")


def reported_arrowhead(raw: Any) -> str:
    """A *stored* arrowhead name in the canonical spelling, never raising.

    The read-side twin of :func:`canonical_arrowhead`: ezdxf reports the name
    the writer stored (``OBLIQUE``) in-session and the block name
    (``_OBLIQUE``) after a reload, so a raw read would spell one style two
    ways and make re-setting a value to itself look like a change (spec §8.3).
    A built-in in any spelling comes back as its block name and the closed
    filled aliases as ``""``; ``None`` is ``""``; anything else — a user block,
    or a foreign drawing's name the symbol-name rule would refuse — passes
    through unchanged, because a read must not raise.
    """
    if raw is None:
        return ""
    text = str(raw)
    try:
        return canonical_arrowhead("DIMBLK", text)
    except (TypeError, ValueError):
        return text


def ezdxf_arrowhead(canonical: str) -> str:
    """The name ezdxf's DIMSTYLE takes for a :func:`canonical_arrowhead` value.

    ezdxf's ``ARROWS`` vocabulary drops the underscore (``_DOT`` → ``DOT``,
    ``""`` stays ``""``) and its export raises ``DXFValueError`` on ``_DOT``
    because no block of that name exists until it creates one from ``DOT``. A
    user block is passed through unchanged. The COM engine needs no mapping:
    AutoCAD takes the canonical spelling.
    """
    if canonical in ARROWHEAD_BLOCKS:
        return canonical[1:]
    return canonical


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
    if kind == "arrow":
        return canonical_arrowhead(key, value)
    if kind == "name":
        return check_name(key, value, what="style name")
    if not isinstance(value, str):
        raise TypeError(f"{key}: expected a string, got {type(value).__name__}")
    if len(value) != 1:  # "char"
        raise ValueError(f"{key}: {value!r} must be exactly one character")
    return value


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
