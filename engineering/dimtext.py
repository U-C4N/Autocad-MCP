"""What a DIMENSION shows: its text formatted the way the drawing formats it (1.8-15).

v1.7.0 reported a dimension's displayed text as ``f"{measurement * DIMLFAC:.10g}"``
(``engineering/verification/binding.py``): a dimension over 19.6 mm on a sheet
whose style has ``DIMDEC 0`` shows "20", and we reported "shows 19.6". This
module is the pure formatter that replaces it - no document, no backend: the
dimension's fields (``backends/entity_fields.py::dimension_fields``) and its
effective style variables (``dimension_style_vars``: the entity's own DSTYLE
override first, then its dimension style, then the renderer's fallback) in,
the text out.

What it applies, in AutoCAD's order:

* ``DIMLFAC`` scales a linear, radial or diameter value; an angle is never
  scaled (AutoCAD and ezdxf agree; v1.7.0 scaled it).
* ``DIMRND`` rounds to the nearest multiple; ``0`` means *no rounding* (the
  AutoCAD meaning - ezdxf's renderer would round a stored 0 to an integer,
  which is why the headless redraw neutralises it, see
  ``backends/ezdxf_backend.py::_rerender_dimension``).
* ``DIMDEC`` (``DIMADEC`` for an angle) decimal places, then ``DIMZIN``
  (``DIMAZIN``) zero suppression: bit 4 leading, bit 8 trailing zeros. Bits
  0-3 only concern feet-and-inch units, which this formatter does not render.
  **Live verification pending (spike S4):** the bit meaning follows the DXF
  reference and ezdxf's renderer; a live seat has not confirmed it.
* ``DIMDSEP`` the decimal marker.
* ``DIMPOST`` with ``<>`` standing for the value (``"<> mm"``); a DIMPOST
  without ``<>`` is a suffix (AutoCAD's rule).
* A diameter dimension (type 3) prefixes ``Ø`` and a radius (type 4) ``R``
  unless the formatted value already starts with it - the renderer's rule.
* An angle (type 2 / 5) in ``DIMAUNIT`` 0 (decimal degrees, ``°``), 1
  (degrees / minutes / seconds), 2 (grads, ``g``) or 3 (radians, ``r``).
  ``DIMAPOST`` is the *alternate-units* suffix in AutoCAD (it only shows with
  ``DIMALT`` on), not an angular one; with ``DIMALT`` on the value is not
  claimed (``source="alternate_units"``) rather than half-formatted.
* The text override: ``""`` or ``"<>"`` shows the value; ``" "`` (one space)
  suppresses the text; one with ``<>`` substitutes the formatted value for
  the first ``<>``; one without replaces the value, and its first number is
  what the sheet claims.
* ``DIMLIM`` replaces the value by its two limits: the value is not one number
  and ``source="limits"``.

``display_rounding`` is what the formatting itself moved the number:
``displayed value - measurement x DIMLFAC`` - 0.4 for 19.6 shown as "20". A
value shown from the measurement may differ from it by up to ``tolerance``
(half of the last shown unit, or half of ``DIMRND``) without being a lie.

``style_vars`` None means "the style is unknown" (a feature built by hand, a
record from before 1.8): the value is then shown at full precision without a
prefix - the v1.7.0 behaviour - and ``style_known`` is False.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = [
    "DIAMETER_SIGN",
    "DIAMETER_SIGNS",
    "RADIUS_SIGN",
    "Display",
    "StyleVars",
    "display",
    "displayed_text",
    "double_prefix",
    "format_value",
    "has_digit",
    "normalize",
    "rendered_matches",
    "style_dict",
]

#: The prefix a diameter dimension's value carries (ezdxf's ``PREFIX``; AutoCAD's ``%%c``).
DIAMETER_SIGN = "\u00d8"
#: Every character read as a diameter sign (``engineering/understand/labels.py``).
DIAMETER_SIGNS = "\u00d8\u00f8\u2205\u2300"
RADIUS_SIGN = "R"

_LINEAR_TYPES = (0, 1, 3, 4, 6)
_ANGULAR_TYPES = (2, 5)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_DIGIT = re.compile(r"\d")
#: A leading count ("4x", "6 X", "2×") followed by a sign, a thread or a space.
_COUNT = re.compile(r"^\s*\d+\s*[xX×]\s*(?=[\sØMR])")
_PERCENT = re.compile(r"%%([cCdDpP])")
_UNICODE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
_PERCENT_CHARS = {"c": DIAMETER_SIGN, "d": "\u00b0", "p": "\u00b1"}

#: The renderer's fallbacks for a variable neither the entity nor its style
#: stores (ezdxf ``render/dim_base.py``; AutoCAD writes every one explicitly).
DEFAULTS: dict[str, object] = {
    "dimdec": 2,
    "dimrnd": 0.0,
    "dimzin": 8,
    "dimdsep": ".",
    "dimpost": "",
    "dimlfac": 1.0,
    "dimadec": 2,
    "dimazin": 2,
    "dimaunit": 0,
    "dimalt": 0,
    "dimtol": 0,
    "dimlim": 0,
    "dimtxt": 1.0,
}


@dataclass(frozen=True)
class Display:
    """What a DIMENSION shows and the number it claims."""

    #: The plain text the sheet shows (``%%c`` as ``Ø``); None when not known.
    text: str | None
    #: The number the sheet claims; None when it claims none (limits, no digit).
    value: float | None
    #: measurement x DIMLFAC (an angle: the measurement), before any formatting.
    scaled_value: float | None
    #: ``value - scaled_value`` when the value is the formatted measurement.
    display_rounding: float | None
    #: How far a value formatted from the measurement may sit from it.
    tolerance: float
    #: ``measurement`` / ``override`` / ``suppressed`` / ``limits`` /
    #: ``alternate_units`` / ``unknown``.
    source: str
    style_known: bool

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "value": self.value,
            "scaled_value": self.scaled_value,
            "display_rounding": self.display_rounding,
            "tolerance": self.tolerance,
            "source": self.source,
            "style_known": self.style_known,
        }


@dataclass(frozen=True)
class StyleVars:
    """``((name, value), ...)`` held opaque: an observed feature's geometry
    carries it without its numbers (a colour index, a decimal count) reading
    as coordinates to ``engineering/verification/measure.py::default_epsilon``."""

    pairs: tuple


def style_dict(style_vars) -> dict | None:
    """``style_vars`` (a mapping, :class:`StyleVars` or ``((name, value), ...)``)
    as a dict, or None."""
    if style_vars is None:
        return None
    if isinstance(style_vars, StyleVars):
        style_vars = style_vars.pairs
    pairs = style_vars.items() if isinstance(style_vars, dict) else style_vars
    return {str(name).lower(): value for name, value in pairs}


def _get(style: dict, name: str):
    value = style.get(name)
    return DEFAULTS[name] if value is None else value


def _separator(value) -> str:
    """DIMDSEP as a character: a code (DXF group 278) or a one-character string.
    Code 0 renders as a comma - ezdxf's ``get_decimal_separator`` rule."""
    if isinstance(value, str):
        return value or "."
    code = int(value)
    return "," if code == 0 else chr(code)


def _suppress(text: str, leading: bool, trailing: bool) -> str:
    """Zero suppression on a ``"%.nf"`` string (ezdxf ``tools.suppress_zeros``)."""
    if not leading and not trailing:
        return text
    if float(text) == 0.0:
        return "0"
    sign = ""
    if text[0] in "-+":
        sign, text = text[0], text[1:]
    if leading:
        text = text.lstrip("0")
    if trailing and "." in text:
        text = text.rstrip("0")
    if text and text[-1] in ".,":
        text = text[:-1]
    return sign + (text or "0")


def _rounded(value: float, rounding: float) -> float:
    if rounding and rounding > 0:
        factor = 1.0 / float(rounding)
        return round(value * factor) / factor
    return value


def _decimal(value: float, places: int, zin: int, separator: str, rounding: float) -> str:
    value = _rounded(value, rounding)
    text = f"{value:.{max(0, int(places))}f}"
    text = _suppress(text, bool(zin & 4), bool(zin & 8))
    return text.replace(".", separator) if separator != "." else text


def _dms(degrees: float, places: int, zin: int, separator: str) -> str:
    total = abs(degrees)
    d = int(total)
    minutes_full = (total - d) * 60.0
    m = int(minutes_full)
    s = (minutes_full - m) * 60.0
    sign = "-" if degrees < 0 else ""
    if places > 4:
        return f"{sign}{d}\u00b0{m}'{_decimal(s, places - 5, zin, separator, 0.0)}\""
    if places > 2:
        return f"{sign}{d}\u00b0{m}'{s:.0f}\""
    if places > 0:
        return f"{sign}{d}\u00b0{m}'"
    return f"{sign}{d}\u00b0"


def _with_post(text: str, post: str) -> str:
    if not post:
        return text
    if "<>" in post:
        return post.replace("<>", text, 1)
    return text + post


def _tolerance(style: dict, angular: bool) -> float:
    places = int(_get(style, "dimadec" if angular else "dimdec"))
    half = 0.5 * 10.0 ** (-max(0, places))
    rounding = 0.0 if angular else float(_get(style, "dimrnd") or 0.0)
    return max(half, rounding / 2.0) if rounding > 0 else half


def format_value(value: float, dimtype: int, style_vars) -> str:
    """The formatted measurement (DIMLFAC already applied by the caller), with
    DIMPOST and the automatic Ø / R prefix of a diameter / radius dimension."""
    style = style_dict(style_vars) or {}
    if dimtype in _ANGULAR_TYPES:
        places = int(_get(style, "dimadec"))
        zin = int(_get(style, "dimazin")) << 2
        separator = _separator(_get(style, "dimdsep"))
        unit = int(_get(style, "dimaunit"))
        if unit == 1:
            text = _dms(value, places, zin, separator)
        elif unit == 2:
            text = _decimal(value * 400.0 / 360.0, places, zin, separator, 0.0) + "g"
        elif unit == 3:
            text = _decimal(math.radians(value), places, zin, separator, 0.0) + "r"
        else:
            text = _decimal(value, places, zin, separator, 0.0) + "\u00b0"
        return _with_post(text, str(_get(style, "dimpost") or ""))
    text = _decimal(
        value,
        int(_get(style, "dimdec")),
        int(_get(style, "dimzin")),
        _separator(_get(style, "dimdsep")),
        float(_get(style, "dimrnd") or 0.0),
    )
    text = _with_post(text, str(_get(style, "dimpost") or ""))
    prefix = {3: DIAMETER_SIGN, 4: RADIUS_SIGN}.get(dimtype)
    if prefix and text and text[0] != prefix:
        text = prefix + text
    return text


def normalize(text: str | None) -> str | None:
    """A shown text in one spelling: AutoCAD ``%%c`` / ``%%d`` / ``%%p`` and
    ``\\U+XXXX`` codes decoded, every diameter sign as ``Ø``, runs of white
    space as one space, the ends stripped. The comparison form of
    ``rendered_text`` against ``displayed_text``."""
    if text is None:
        return None
    out = _PERCENT.sub(lambda m: _PERCENT_CHARS[m.group(1).lower()], str(text))
    out = _UNICODE.sub(lambda m: chr(int(m.group(1), 16)), out)
    for sign in DIAMETER_SIGNS:
        out = out.replace(sign, DIAMETER_SIGN)
    return " ".join(out.split())


def has_digit(text: str | None) -> bool:
    return bool(text) and bool(_DIGIT.search(str(text)))


def double_prefix(dimtype: int, override: str | None) -> bool:
    """A diameter (radius) dimension whose override puts its own Ø (R) right
    before ``<>``: the automatic prefix lands after it and the sheet shows ØØ
    (RR). **S4 pending:** the live double sign is not measured yet."""
    if dimtype not in (3, 4) or not override or "<>" not in override:
        return False
    before = normalize(override.split("<>", 1)[0].replace("\\P", " ")) or ""
    before = before.rstrip()
    if not before:
        return False
    sign = DIAMETER_SIGN if dimtype == 3 else RADIUS_SIGN
    return before.endswith(sign)


def _fields(fields) -> tuple:
    dimtype, _defpoints, measurement, override, _style, dimlfac, _angle = tuple(fields)[:7]
    return int(dimtype), measurement, str(override or ""), dimlfac


def display(fields, style_vars) -> Display:
    """What the DIMENSION of ``fields`` shows under ``style_vars``."""
    dimtype, measurement, override, dimlfac = _fields(fields)
    style = style_dict(style_vars)
    known = style is not None
    angular = dimtype in _ANGULAR_TYPES
    if measurement is None:
        scaled = None
    elif angular:
        scaled = float(measurement)
    elif dimlfac is None:
        scaled = None
    else:
        scaled = float(measurement) * float(dimlfac)

    if override == " ":
        return Display("", None, scaled, None, 0.0, "suppressed", known)
    if override and override != "<>" and "<>" not in override:
        plain = normalize(override.replace("\\P", " ")) or ""
        # "4x Ø13.5": the leading count is not the size the sheet claims.
        found = _NUMBER.search(_COUNT.sub("", plain, count=1))
        value = float(found.group().replace(",", ".")) if found else None
        return Display(plain, value, scaled, None, 0.0, "override", known)

    if known and int(_get(style, "dimlim")) and not int(_get(style, "dimtol")):
        return Display(None, None, scaled, None, 0.0, "limits", known)
    if known and int(_get(style, "dimalt")):
        return Display(None, None, scaled, None, 0.0, "alternate_units", known)
    if scaled is None:
        return Display(None, None, None, None, 0.0, "unknown", known)

    if known:
        formatted = format_value(scaled, dimtype, style)
        rounding_step = 0.0 if angular else float(_get(style, "dimrnd") or 0.0)
        places = int(_get(style, "dimadec" if angular else "dimdec"))
        shown = round(_rounded(scaled, rounding_step), max(0, places))
        if angular and int(_get(style, "dimaunit")) != 0:
            shown = scaled  # DMS / grads / radians: the number is not a degree count
        tolerance = _tolerance(style, angular)
    else:
        formatted = f"{scaled:.10g}"
        shown = scaled
        tolerance = 0.0
    text = override.replace("<>", formatted, 1) if override and override != "<>" else formatted
    rounding = round(shown - scaled, 12)
    return Display(normalize(text), shown, scaled, rounding, tolerance, "measurement", known)


def displayed_text(fields, style_vars) -> str | None:
    """The plain text the sheet shows for ``fields`` under ``style_vars``."""
    return display(fields, style_vars).text


def rendered_matches(rendered: str | None, fields, style_vars) -> bool | None:
    """Does the ``*D`` block's text say what the fields and style say it must?

    None when either side is unknown: no rendered text, a value the formatter
    does not claim (limits, alternate units) or an unknown style. With
    ``DIMTOL`` on, the tolerance follows the value, so the rendered text must
    *start* with the displayed one (and not continue the number)."""
    shown = display(fields, style_vars)
    if rendered is None or shown.text is None or not shown.style_known:
        return None
    have, want = normalize(rendered), normalize(shown.text)
    if have == want:
        return True
    style = style_dict(style_vars) or {}
    if int(_get(style, "dimtol")) and have.startswith(want):
        rest = have[len(want) :]
        return not (rest[:1].isdigit() or rest[:1] in ".,")
    return False
