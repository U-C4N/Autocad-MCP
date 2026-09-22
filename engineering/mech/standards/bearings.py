"""ISO 15 deep groove ball bearings, plus the ISO 8826 representation rules.

SOURCE
------
* ``DEEP_GROOVE`` - ISO 15:2017 Table 4: boundary dimensions ``d`` (bore),
  ``D`` (outside diameter) and ``B`` (width) for the radial bearings of
  dimension series 10 (600x), 02 (620x) and 03 (630x).
* ``SOURCE_ISO_8826_1`` - ISO 8826-1:1989, general simplified representation:
  the bearing is drawn as its envelope outline with an upright cross inside
  that does not touch the outline.
* ``SOURCE_ISO_8826_2`` - ISO 8826-2:1994, particular simplified
  representation: the envelope outline plus a symbol for the rolling element.

COVERAGE
--------
600x, 620x and 630x, bore numbers 00..10 (d = 10..50 mm) - 33 rows. Bore
numbers 00/01/02/03 are 10/12/15/17 mm by the ISO 15 rule; 04 and up are
``5 x bore number``. Deliberately **not** transcribed, and refused by name:
bore numbers above 10, the wide series 622x/623x, the light series 618x/619x
and 16xx, the inch series, and every suffixed variant (``6205-2RS``,
``6205-Z``) - a suffix changes width and sealing, not a number this module may
guess.

The chamfer dimensions ``r_s`` are not transcribed either: neither simplified
representation needs them, so no row carries a value nobody checked.

CROSS_FRACTION and BALL_SYMBOL_FRACTION are **declared drawing conventions**,
not transcribed dimensions. ISO 8826-1 fixes the *form* of the cross and
requires only that it not touch the outline; ISO 15 carries no ball diameter
at all, so the element symbol is sized as a fraction of the ring gap rather
than invented as a real ball.
"""

from __future__ import annotations

from . import Coverage, register

SOURCE_ISO_15 = (
    "ISO 15:2017 Table 4 - rolling bearings, radial bearings, boundary dimensions "
    "(dimension series 10, 02, 03)"
)
SOURCE_ISO_8826_1 = (
    "ISO 8826-1:1989 - technical drawings, rolling bearings, general simplified representation"
)
SOURCE_ISO_8826_2 = (
    "ISO 8826-2:1994 - technical drawings, rolling bearings, particular simplified representation"
)

#: Fraction of the half-width / half-gap used by the ISO 8826-1 upright cross.
#: A declared convention (see the module docstring), never a table value.
CROSS_FRACTION = 0.6

#: Fraction of the half-gap used by the ISO 8826-2 rolling-element symbol.
#: A declared convention, never a ball diameter.
BALL_SYMBOL_FRACTION = 0.6

BEARING_SERIES = ("600x", "620x", "630x")
_SERIES_PREFIX = {"60": "600x", "62": "620x", "63": "630x"}

#: ISO 15:2017 Table 4 - d, D, B in millimetres.
DEEP_GROOVE: dict[str, dict[str, float]] = {
    # dimension series 10 (600x)
    "6000": {"d": 10.0, "D": 26.0, "B": 8.0},
    "6001": {"d": 12.0, "D": 28.0, "B": 8.0},
    "6002": {"d": 15.0, "D": 32.0, "B": 9.0},
    "6003": {"d": 17.0, "D": 35.0, "B": 10.0},
    "6004": {"d": 20.0, "D": 42.0, "B": 12.0},
    "6005": {"d": 25.0, "D": 47.0, "B": 12.0},
    "6006": {"d": 30.0, "D": 55.0, "B": 13.0},
    "6007": {"d": 35.0, "D": 62.0, "B": 14.0},
    "6008": {"d": 40.0, "D": 68.0, "B": 15.0},
    "6009": {"d": 45.0, "D": 75.0, "B": 16.0},
    "6010": {"d": 50.0, "D": 80.0, "B": 16.0},
    # dimension series 02 (620x)
    "6200": {"d": 10.0, "D": 30.0, "B": 9.0},
    "6201": {"d": 12.0, "D": 32.0, "B": 10.0},
    "6202": {"d": 15.0, "D": 35.0, "B": 11.0},
    "6203": {"d": 17.0, "D": 40.0, "B": 12.0},
    "6204": {"d": 20.0, "D": 47.0, "B": 14.0},
    "6205": {"d": 25.0, "D": 52.0, "B": 15.0},
    "6206": {"d": 30.0, "D": 62.0, "B": 16.0},
    "6207": {"d": 35.0, "D": 72.0, "B": 17.0},
    "6208": {"d": 40.0, "D": 80.0, "B": 18.0},
    "6209": {"d": 45.0, "D": 85.0, "B": 19.0},
    "6210": {"d": 50.0, "D": 90.0, "B": 20.0},
    # dimension series 03 (630x)
    "6300": {"d": 10.0, "D": 35.0, "B": 11.0},
    "6301": {"d": 12.0, "D": 37.0, "B": 12.0},
    "6302": {"d": 15.0, "D": 42.0, "B": 13.0},
    "6303": {"d": 17.0, "D": 47.0, "B": 14.0},
    "6304": {"d": 20.0, "D": 52.0, "B": 15.0},
    "6305": {"d": 25.0, "D": 62.0, "B": 17.0},
    "6306": {"d": 30.0, "D": 72.0, "B": 19.0},
    "6307": {"d": 35.0, "D": 80.0, "B": 21.0},
    "6308": {"d": 40.0, "D": 90.0, "B": 23.0},
    "6309": {"d": 45.0, "D": 100.0, "B": 25.0},
    "6310": {"d": 50.0, "D": 110.0, "B": 27.0},
}


def bearing(designation) -> dict:
    """Boundary dimensions of one deep groove ball bearing.

    Returns ``{"designation", "series", "source", "d", "D", "B"}``. A
    designation outside the transcribed table is refused by name; nothing is
    interpolated between series or bore numbers.
    """
    key = " ".join(str(designation).split()).upper()
    if key not in DEEP_GROOVE:
        raise ValueError(
            "ISO 15 deep groove ball bearings are transcribed here for the 600x, 620x "
            f"and 630x series, bore numbers 00-10 (d = 10-50 mm); {key!r} is outside "
            f"that table and is never interpolated ({SOURCE_ISO_15})."
        )
    return {
        "designation": key,
        "series": _SERIES_PREFIX[key[:2]],
        "source": SOURCE_ISO_15,
        **DEEP_GROOVE[key],
    }


def list_bearings(series: str | None = None) -> tuple[dict, ...]:
    """Every transcribed bearing, optionally restricted to one series."""
    rows = tuple(bearing(name) for name in DEEP_GROOVE)
    if series is None:
        return rows
    name = " ".join(str(series).split()).lower()
    if name not in BEARING_SERIES:
        raise ValueError(
            f"list_bearings: series must be one of {', '.join(BEARING_SERIES)}, got {series!r}."
        )
    return tuple(row for row in rows if row["series"] == name)


register("ISO 15", DEEP_GROOVE, Coverage(10.0, 50.0), SOURCE_ISO_15)
