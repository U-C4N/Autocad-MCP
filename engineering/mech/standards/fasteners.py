"""ISO metric fastener tables - transcribed from the standards, never derived.

SOURCE
------
* ``HEX_HEAD``    ISO 4014:2011 Table 1 (hexagon head bolts, product grades A
  and B) and ISO 4017:2014 Table 1 (hexagon head screws, fully threaded). The
  two standards carry the same width across flats ``s`` and head height ``k``
  for every size shipped here, so one table serves both and the standard is a
  property of the *part*, not a second copy of the table.
* ``HEX_NUT``     ISO 4032:2012 Table 1 (hexagon regular nuts, style 1):
  width across flats ``s``, nut height ``m`` (max).
* ``WASHER``      ISO 7089:2000 Table 1 (plain washers, normal series, product
  grade A): inside diameter ``d1``, outside diameter ``d2``, thickness ``h``.
* ``SOCKET_HEAD`` ISO 4762:2004 Table 1 (hexagon socket head cap screws): head
  diameter ``dk`` (max), head height ``k`` (max), hexagon socket width across
  flats ``s``, thread length ``b``.
* ``COARSE_PITCH`` ISO 261:1998, coarse pitch series.

COVERAGE (deliberately narrow - the table rule of the mech spec, section 5)
---------------------------------------------------------------------------
Only ISO 261 **first-choice** sizes are shipped:

* ISO 4014 / 4017 / 4032 / 7089 : M5 M6 M8 M10 M12 M16 M20 M24 M30 M36
* ISO 4762                      : M3 M4 M5 M6 M8 M10 M12 M16 M20 M24

Second-choice sizes (M14, M18, M22, M27, M33) and everything beyond those
ranges are **not** shipped and are refused by name. A narrow table is merely
narrow; a wrong across-flats ships a wrong workshop drawing.

Two columns are deliberately absent:

* across-corners ``e`` - :func:`across_corners` returns the *nominal*
  hexagon's ``2s/sqrt(3)`` instead. The standards' own ``e`` is a minimum
  computed from ``s`` min and is product-grade dependent; a drawing of the
  nominal part wants the nominal hexagon, so no ``e`` column is transcribed.
* the per-size nominal length ranges of ISO 4014 Table 1 - ``l`` is validated
  as a finite positive number and the useful thread length comes from the
  standard's own ``b`` rule in :func:`thread_length`.
"""

from __future__ import annotations

import math

from . import Coverage, register

SOURCE_ISO_4014 = "ISO 4014:2011 Table 1 - hexagon head bolts, product grades A and B"
SOURCE_ISO_4017 = "ISO 4017:2014 Table 1 - hexagon head screws, fully threaded"
SOURCE_ISO_4032 = "ISO 4032:2012 Table 1 - hexagon regular nuts, style 1"
SOURCE_ISO_7089 = "ISO 7089:2000 Table 1 - plain washers, normal series, product grade A"
SOURCE_ISO_4762 = "ISO 4762:2004 Table 1 - hexagon socket head cap screws"
SOURCE_ISO_261 = "ISO 261:1998 - ISO general purpose metric screw threads, coarse pitch series"

#: ISO 261:1998 coarse pitch, one entry per size any table below carries.
COARSE_PITCH: dict[str, float] = {
    "M3": 0.5,
    "M4": 0.7,
    "M5": 0.8,
    "M6": 1.0,
    "M8": 1.25,
    "M10": 1.5,
    "M12": 1.75,
    "M16": 2.0,
    "M20": 2.5,
    "M24": 3.0,
    "M30": 3.5,
    "M36": 4.0,
}

#: ISO 4014:2011 / ISO 4017:2014 Table 1 - s = across flats, k = head height.
HEX_HEAD: dict[str, dict[str, float]] = {
    "M5": {"s": 8.0, "k": 3.5},
    "M6": {"s": 10.0, "k": 4.0},
    "M8": {"s": 13.0, "k": 5.3},
    "M10": {"s": 16.0, "k": 6.4},
    "M12": {"s": 18.0, "k": 7.5},
    "M16": {"s": 24.0, "k": 10.0},
    "M20": {"s": 30.0, "k": 12.5},
    "M24": {"s": 36.0, "k": 15.0},
    "M30": {"s": 46.0, "k": 18.7},
    "M36": {"s": 55.0, "k": 22.5},
}

#: ISO 4032:2012 Table 1 - s = across flats, m = nut height (max).
HEX_NUT: dict[str, dict[str, float]] = {
    "M5": {"s": 8.0, "m": 4.7},
    "M6": {"s": 10.0, "m": 5.2},
    "M8": {"s": 13.0, "m": 6.8},
    "M10": {"s": 16.0, "m": 8.4},
    "M12": {"s": 18.0, "m": 10.8},
    "M16": {"s": 24.0, "m": 14.8},
    "M20": {"s": 30.0, "m": 18.0},
    "M24": {"s": 36.0, "m": 21.5},
    "M30": {"s": 46.0, "m": 25.6},
    "M36": {"s": 55.0, "m": 31.0},
}

#: ISO 7089:2000 Table 1 - d1 = bore, d2 = outside diameter, h = thickness.
WASHER: dict[str, dict[str, float]] = {
    "M5": {"d1": 5.3, "d2": 10.0, "h": 1.0},
    "M6": {"d1": 6.4, "d2": 12.0, "h": 1.6},
    "M8": {"d1": 8.4, "d2": 16.0, "h": 1.6},
    "M10": {"d1": 10.5, "d2": 20.0, "h": 2.0},
    "M12": {"d1": 13.0, "d2": 24.0, "h": 2.5},
    "M16": {"d1": 17.0, "d2": 30.0, "h": 3.0},
    "M20": {"d1": 21.0, "d2": 37.0, "h": 3.0},
    "M24": {"d1": 25.0, "d2": 44.0, "h": 4.0},
    "M30": {"d1": 31.0, "d2": 56.0, "h": 4.0},
    "M36": {"d1": 37.0, "d2": 66.0, "h": 5.0},
}

#: ISO 4762:2004 Table 1 - dk = head diameter (max), k = head height (max),
#: s = hexagon socket across flats, b = thread length.
SOCKET_HEAD: dict[str, dict[str, float]] = {
    "M3": {"dk": 5.5, "k": 3.0, "s": 2.5, "b": 18.0},
    "M4": {"dk": 7.0, "k": 4.0, "s": 3.0, "b": 20.0},
    "M5": {"dk": 8.5, "k": 5.0, "s": 4.0, "b": 22.0},
    "M6": {"dk": 10.0, "k": 6.0, "s": 5.0, "b": 24.0},
    "M8": {"dk": 13.0, "k": 8.0, "s": 6.0, "b": 28.0},
    "M10": {"dk": 16.0, "k": 10.0, "s": 8.0, "b": 32.0},
    "M12": {"dk": 18.0, "k": 12.0, "s": 10.0, "b": 36.0},
    "M16": {"dk": 24.0, "k": 16.0, "s": 14.0, "b": 44.0},
    "M20": {"dk": 30.0, "k": 20.0, "s": 17.0, "b": 52.0},
    "M24": {"dk": 36.0, "k": 24.0, "s": 19.0, "b": 60.0},
}

_TABLES: dict[str, tuple[dict[str, dict[str, float]], str]] = {
    "ISO 4014": (HEX_HEAD, SOURCE_ISO_4014),
    "ISO 4017": (HEX_HEAD, SOURCE_ISO_4017),
    "ISO 4032": (HEX_NUT, SOURCE_ISO_4032),
    "ISO 7089": (WASHER, SOURCE_ISO_7089),
    "ISO 4762": (SOCKET_HEAD, SOURCE_ISO_4762),
}

THREAD_LENGTH_STANDARDS = ("ISO 4014", "ISO 4017", "ISO 4762")


def _key(size) -> str:
    key = str(size).strip().upper()
    if not key:
        raise ValueError("fastener size must be a non-empty string such as 'M12'")
    return key


def _row(table: dict, standard: str, source: str, key: str) -> dict:
    if key not in table:
        raise ValueError(
            f"{standard} is transcribed here for {', '.join(table)} only; {key!r} is "
            f"outside that table and is never interpolated ({source})."
        )
    return dict(table[key])


def nominal_diameter(size) -> float:
    """Nominal thread diameter in mm, e.g. ``'M12' -> 12.0``."""
    key = _key(size)
    if key not in COARSE_PITCH:
        raise ValueError(
            f"{SOURCE_ISO_261} is transcribed here for {', '.join(COARSE_PITCH)}; "
            f"{key!r} is outside it."
        )
    return float(key[1:])


def thread_pitch(size) -> float:
    """ISO 261 coarse pitch in mm."""
    key = _key(size)
    if key not in COARSE_PITCH:
        raise ValueError(
            f"{SOURCE_ISO_261} is transcribed here for {', '.join(COARSE_PITCH)}; "
            f"{key!r} is outside it."
        )
    return COARSE_PITCH[key]


def hex_head(size) -> dict:
    """ISO 4014 / ISO 4017 head row plus the nominal diameter and pitch."""
    key = _key(size)
    row = _row(HEX_HEAD, "ISO 4014 / ISO 4017", SOURCE_ISO_4014, key)
    return {"size": key, "d": nominal_diameter(key), "p": thread_pitch(key), **row}


def hex_nut(size) -> dict:
    """ISO 4032 nut row plus the nominal diameter and pitch."""
    key = _key(size)
    row = _row(HEX_NUT, "ISO 4032", SOURCE_ISO_4032, key)
    return {"size": key, "d": nominal_diameter(key), "p": thread_pitch(key), **row}


def washer(size) -> dict:
    """ISO 7089 washer row plus the mating thread's nominal diameter and pitch."""
    key = _key(size)
    row = _row(WASHER, "ISO 7089", SOURCE_ISO_7089, key)
    return {"size": key, "d": nominal_diameter(key), "p": thread_pitch(key), **row}


def socket_head(size) -> dict:
    """ISO 4762 socket head cap screw row plus the nominal diameter and pitch."""
    key = _key(size)
    row = _row(SOCKET_HEAD, "ISO 4762", SOURCE_ISO_4762, key)
    return {"size": key, "d": nominal_diameter(key), "p": thread_pitch(key), **row}


def thread_length(size, length: float, standard: str = "ISO 4014") -> float:
    """Useful thread length ``b`` for a nominal length ``l``, per the part standard.

    ISO 4014 fixes ``b`` by rule rather than by table: ``2d + 6`` for
    ``l <= 125``, ``2d + 12`` for ``125 < l <= 200`` and ``2d + 25`` for
    ``l > 200``. ISO 4017 screws are threaded over their whole length. ISO 4762
    tabulates ``b`` per size. In every case a screw shorter than its own ``b``
    is threaded to the head, so the result is capped at ``l``.
    """
    key = _key(size)
    name = " ".join(str(standard).split())
    if name not in THREAD_LENGTH_STANDARDS:
        raise ValueError(
            f"thread_length: standard must be one of {', '.join(THREAD_LENGTH_STANDARDS)}, "
            f"got {standard!r}."
        )
    if isinstance(length, bool) or not isinstance(length, (int, float)):
        raise ValueError(f"thread_length: length must be a number, got {length!r}")
    l_mm = float(length)
    if not math.isfinite(l_mm) or l_mm <= 0.0:
        raise ValueError(f"thread_length: length must be finite and > 0, got {length!r}")
    if name == "ISO 4017":
        hex_head(key)
        return l_mm
    if name == "ISO 4762":
        return min(socket_head(key)["b"], l_mm)
    diameter = nominal_diameter(key)
    hex_head(key)  # ISO 4014 coverage: refuse a size the head table does not carry
    if l_mm <= 125.0:
        b = 2.0 * diameter + 6.0
    elif l_mm <= 200.0:
        b = 2.0 * diameter + 12.0
    else:
        b = 2.0 * diameter + 25.0
    return min(b, l_mm)


def across_corners(s: float) -> float:
    """Across-corners width of the *nominal* hexagon: ``2s/sqrt(3)``.

    Derived geometry, not a transcribed column - see the module docstring.
    """
    if isinstance(s, bool) or not isinstance(s, (int, float)):
        raise ValueError(f"across_corners: s must be a number, got {s!r}")
    width = float(s)
    if not math.isfinite(width) or width <= 0.0:
        raise ValueError(f"across_corners: s must be finite and > 0, got {s!r}")
    return 2.0 * width / math.sqrt(3.0)


def sizes(standard: str) -> tuple[str, ...]:
    """The transcribed sizes of one fastener standard, in table order."""
    name = " ".join(str(standard).split()).upper()
    if name not in _TABLES:
        raise ValueError(
            f"sizes: {standard!r} is not a fastener standard in this module; "
            f"have {', '.join(_TABLES)}."
        )
    return tuple(_TABLES[name][0])


register("ISO 4014", HEX_HEAD, Coverage(5.0, 36.0), SOURCE_ISO_4014)
register("ISO 4017", HEX_HEAD, Coverage(5.0, 36.0), SOURCE_ISO_4017)
register("ISO 4032", HEX_NUT, Coverage(5.0, 36.0), SOURCE_ISO_4032)
register("ISO 7089", WASHER, Coverage(5.0, 36.0), SOURCE_ISO_7089)
register("ISO 4762", SOCKET_HEAD, Coverage(3.0, 24.0), SOURCE_ISO_4762)
