"""ISO 261 metric thread pitches and the ISO 6410 drawing representation.

SOURCE
------
**Pitches.** ISO 261:2022, *ISO general purpose metric screw threads - General
plan*, Table 1 (coarse pitch series) and Table 2 (fine pitch series).

**Transcribed coverage.** The first-choice diameters M1.6 to M64, plus the
second-choice diameters M14, M18, M22, M27, M33, M39, M45, M52 and M60. The
small second-choice diameters (M1.8, M2.2, M3.5, M4.5, M5.5, M7, M9, M11),
every third-choice diameter, everything above M64 and the fine pitch series
below M8 are **not** transcribed and are refused by name rather than
interpolated - the table rule of this track. Where a diameter's fine series
here is shorter than the standard's, the pitch that is missing is refused by
name as well; widening it is a data edit in ``FINE`` with no code change.

**Basic diameters.** ISO 68-1:1998 fundamental triangle, H = (sqrt(3)/2) * P,
with ISO 724's basic minor diameter d1 = d - 1.25*H = d - 1.082532*P and basic
pitch diameter d2 = d - 0.75*H = d - 0.649519*P. That is the standard's own
formula rather than transcribed table data, which is why it can be pinned
against published ISO 724 values instead of trusted: M20x2.5 reproduces
17.294 / 18.376 exactly, M10x1.5 reproduces 8.376 / 9.026, M8x1.25 reproduces
6.647 / 7.188.

**Representation.** ISO 6410-1:1993, *Technical drawings - Screw threads and
threaded parts*: the minor diameter is drawn as a thin continuous line at
0.8 x the major diameter (`MINOR_RATIO`); in a view along the axis that same
circle is drawn as three quarters of a circle with the gap in one quadrant;
the thread run-out / thread-length limit is a line across the full major
diameter. An internal thread shown without a section is drawn hidden
throughout.
"""

from __future__ import annotations

import math
import re

from engineering.mech.primitives import Arc, Line, Prim
from engineering.mech.standards import Coverage, register

SOURCE_PITCH = (
    "ISO 261:2022 Table 1 (coarse) and Table 2 (fine); first-choice M1.6-M64 "
    "plus second-choice M14, M18, M22, M27, M33, M39, M45, M52, M60"
)
SOURCE_BASIC = "ISO 68-1:1998 fundamental triangle; values pinned against ISO 724 Table 1"
SOURCE_REPRESENTATION = "ISO 6410-1:1993"

#: ISO 6410-1: the minor diameter is represented at 0.8 x the major diameter.
MINOR_RATIO = 0.8

#: ISO 261 Table 1 - coarse pitch series, major diameter (mm) -> pitch (mm).
COARSE: dict[float, float] = {
    1.6: 0.35,
    2.0: 0.40,
    2.5: 0.45,
    3.0: 0.50,
    4.0: 0.70,
    5.0: 0.80,
    6.0: 1.00,
    8.0: 1.25,
    10.0: 1.50,
    12.0: 1.75,
    14.0: 2.00,
    16.0: 2.00,
    18.0: 2.50,
    20.0: 2.50,
    22.0: 2.50,
    24.0: 3.00,
    27.0: 3.00,
    30.0: 3.50,
    33.0: 3.50,
    36.0: 4.00,
    39.0: 4.00,
    42.0: 4.50,
    45.0: 4.50,
    48.0: 5.00,
    52.0: 5.00,
    56.0: 5.50,
    60.0: 5.50,
    64.0: 6.00,
}

#: ISO 261 Table 2 - fine pitch series, major diameter (mm) -> pitches (mm),
#: coarsest first. Transcribed from M8 upwards only.
FINE: dict[float, tuple[float, ...]] = {
    8.0: (1.0, 0.75),
    10.0: (1.25, 1.0, 0.75),
    12.0: (1.5, 1.25, 1.0),
    14.0: (1.5, 1.0),
    16.0: (1.5, 1.0),
    18.0: (2.0, 1.5, 1.0),
    20.0: (2.0, 1.5, 1.0),
    22.0: (2.0, 1.5, 1.0),
    24.0: (2.0, 1.5, 1.0),
    27.0: (2.0, 1.5, 1.0),
    30.0: (2.0, 1.5, 1.0),
    33.0: (2.0, 1.5),
    36.0: (3.0, 2.0, 1.5),
    39.0: (3.0, 2.0, 1.5),
    42.0: (3.0, 2.0, 1.5),
    45.0: (3.0, 2.0, 1.5),
    48.0: (3.0, 2.0, 1.5),
    52.0: (3.0, 2.0, 1.5),
    56.0: (4.0, 3.0, 2.0, 1.5),
    60.0: (4.0, 3.0, 2.0, 1.5),
    64.0: (4.0, 3.0, 2.0, 1.5),
}

DIAMETERS: tuple[float, ...] = tuple(sorted(COARSE))

COVERAGE = Coverage(DIAMETERS[0], DIAMETERS[-1], "mm")

_DESIGNATION_RE = re.compile(
    r"^\s*M\s*(\d+(?:[.,]\d+)?)\s*(?:[x×X]\s*(\d+(?:[.,]\d+)?))?\s*(?:-\s*(LH|RH))?\s*$",
    re.IGNORECASE,
)

register(
    "ISO 261",
    {d: {"pitch": COARSE[d], "fine": FINE.get(d, ())} for d in DIAMETERS},
    COVERAGE,
    SOURCE_PITCH,
)


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def _pitch_text(pitch: float) -> str:
    """A pitch in millimetres, always carrying a decimal: 2.5, 2.0, 0.75."""
    text = f"{float(pitch):g}"
    return text if "." in text else f"{text}.0"


def pitch_for(d: float, pitch: float | None = None) -> float:
    """The coarse pitch for ``d``, or validate an explicit fine pitch against the series."""
    diameter = float(d)
    if diameter not in COARSE:
        raise ValueError(
            f"ISO 261: M{diameter:g} is not in the transcribed table. "
            f"Coverage is M{DIAMETERS[0]:g}-M{DIAMETERS[-1]:g}, the first-choice diameters plus "
            "the second-choice M14, M18, M22, M27, M33, M39, M45, M52 and M60; the remaining "
            "second-choice diameters and every third-choice diameter are not transcribed and "
            "are not interpolated."
        )
    if pitch is None:
        return COARSE[diameter]
    value = float(pitch)
    if abs(value - COARSE[diameter]) < 1e-9:
        return COARSE[diameter]
    series = FINE.get(diameter, ())
    for candidate in series:
        if abs(value - candidate) < 1e-9:
            return candidate
    known = ", ".join(_pitch_text(p) for p in (COARSE[diameter], *series))
    raise ValueError(
        f"ISO 261: M{diameter:g} has no pitch {value:g} mm in the transcribed series. "
        f"Transcribed pitches for M{diameter:g}: {known}."
    )


def parse_designation(designation: str) -> dict:
    """``M20`` / ``M20x1.5`` / ``M20x1.5-LH`` -> d, pitch, series, hand."""
    match = _DESIGNATION_RE.match(str(designation or ""))
    if not match:
        raise ValueError(
            f"designation {designation!r}: expected an ISO metric thread such as "
            "'M20', 'M20x1.5' or 'M20x1.5-LH'."
        )
    diameter = _number(match.group(1))
    raw_pitch = _number(match.group(2)) if match.group(2) else None
    pitch = pitch_for(diameter, raw_pitch)
    hand = "left" if (match.group(3) or "RH").upper() == "LH" else "right"
    series = "coarse" if abs(pitch - COARSE[diameter]) < 1e-9 else "fine"
    text = f"M{diameter:g}" if raw_pitch is None else f"M{diameter:g}x{pitch:g}"
    if hand == "left":
        text += "-LH"
    return {
        "designation": text,
        "d": diameter,
        "pitch": pitch,
        "series": series,
        "hand": hand,
    }


def _h(pitch: float) -> float:
    """ISO 68-1 fundamental triangle height."""
    return math.sqrt(3.0) / 2.0 * float(pitch)


def basic_minor_diameter(d: float, pitch: float) -> float:
    """ISO 724 basic minor diameter d1 = d - 1.25 * H."""
    return float(d) - 1.25 * _h(pitch)


def basic_pitch_diameter(d: float, pitch: float) -> float:
    """ISO 724 basic pitch diameter d2 = d - 0.75 * H."""
    return float(d) - 0.75 * _h(pitch)


def thread_axial_prims(
    *,
    x0: float,
    length: float,
    d: float,
    internal: bool = False,
    minor_ratio: float = MINOR_RATIO,
    limit_line: bool = True,
) -> tuple[Prim, ...]:
    """ISO 6410 representation in a view along the axis (axis on y = 0, +X along it).

    External: the two minor-diameter lines at 0.8 x d plus the thread-length
    limit line across the full major diameter. Internal (not sectioned): the
    same pair at the *major* diameter, hidden, because the drawn bore already
    carries the minor.
    """
    x1 = float(x0) + float(length)
    role = "hidden" if internal else "visible"
    radius = float(d) / 2.0 if internal else float(minor_ratio) * float(d) / 2.0
    prims: list[Prim] = [
        Line((float(x0), radius), (x1, radius), role),
        Line((float(x0), -radius), (x1, -radius), role),
    ]
    if limit_line:
        major = float(d) / 2.0
        prims.append(Line((x1, -major), (x1, major), role))
    return tuple(prims)


def thread_end_prims(
    *,
    center: tuple[float, float],
    d: float,
    internal: bool = False,
    gap_center_deg: float = 45.0,
    minor_ratio: float = MINOR_RATIO,
) -> tuple[Prim, ...]:
    """ISO 6410 three-quarter circle for a view along the thread axis.

    External: three quarters of the *minor* circle. Internal: three quarters of
    the *major* circle. The quarter-circle gap is centred on ``gap_center_deg``
    (45 deg = the upper-right quadrant, the usual placement).
    """
    radius = float(d) / 2.0 if internal else float(minor_ratio) * float(d) / 2.0
    start = (float(gap_center_deg) + 45.0) % 360.0
    end = (start + 270.0) % 360.0
    return (Arc((float(center[0]), float(center[1])), radius, start, end, "visible"),)
