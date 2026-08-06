"""ISO 286-1/2 limits-and-fits lookup (authored table data, not formulas).

`fit_lookup("H7", 20.0)` resolves a fit code into upper/lower deviations in
millimetres, ready to feed `tolerances.build_dim_override` (tol_mode
"deviation"). Values are the published ISO 286 table values — the standard's
rounding rules make naive formula evaluation drift by 1 µm or a full step in
places, so the tables are authored and pinned by tests instead.

Scope (deliberate, documented):

- Nominal sizes over 1 mm up to and including 500 mm.
- IT grades 4-11 (IT4 is carried for the shaft-k rule and future hole mirrors).
- Shaft letters: d, e, f, g, h, js, k (IT4-7), m, n, p, and the interference
  shafts r, s, t, u (t from 24 mm, where ISO 286 begins defining it).
- Hole letters:  D, E, F, G, H, JS (mirrored clearance letters + H + JS), and
  K, M, N, P via the ISO 286 delta rule ``ES = -ei + Δ``, ``Δ = IT(n) −
  IT(n−1)``. That is a rule over tables already here, not new table data, which
  is why it can be pinned against published values rather than trusted.

r/s/t/u *are* new table data, sub-stepped more finely than the main size steps,
so they are cross-checked in the tests against ISO 286-1's own derivation
formulae — an independent source. r reproduces as the geometric mean of p and s
on 24/24 steps (with p pinned before r existed); s, t and u track their
IT7-based formulae within 1 µm on every step from 18 mm up, and the five places
where the standard's rounding puts a published value 2-3 µm off its own formula
are listed by name in the tests rather than hidden.

This covers the preferred general-mechanical fits (H7/g6, H7/h6, H7/k6,
H7/n6, H7/p6, H8/f7, H9/d9, G7/h6, F8/h7, JS-symmetric, ...), the hole-basis
transition and interference pairs (K7/h6, M7/h6, N7/h6, P7/h6), and the heavy
press/drive fits (H7/r6, H7/s6, H7/t6, H7/u6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Size steps: (over, up_to_incl) in mm — ISO 286-1 main steps, 1..500 mm.
_SIZE_STEPS: tuple[tuple[float, float], ...] = (
    (1, 3),
    (3, 6),
    (6, 10),
    (10, 18),
    (18, 30),
    (30, 50),
    (50, 80),
    (80, 120),
    (120, 180),
    (180, 250),
    (250, 315),
    (315, 400),
    (400, 500),
)

# Standard tolerance values IT4..IT11 in µm per size step (ISO 286-1 Table 1).
_IT_GRADES = (4, 5, 6, 7, 8, 9, 10, 11)
_IT_TABLE: tuple[tuple[int, ...], ...] = (
    #  IT4  IT5  IT6  IT7  IT8  IT9  IT10  IT11
    (3, 4, 6, 10, 14, 25, 40, 60),  # >1-3
    (4, 5, 8, 12, 18, 30, 48, 75),  # >3-6
    (4, 6, 9, 15, 22, 36, 58, 90),  # >6-10
    (5, 8, 11, 18, 27, 43, 70, 110),  # >10-18
    (6, 9, 13, 21, 33, 52, 84, 130),  # >18-30
    (7, 11, 16, 25, 39, 62, 100, 160),  # >30-50
    (8, 13, 19, 30, 46, 74, 120, 190),  # >50-80
    (10, 15, 22, 35, 54, 87, 140, 220),  # >80-120
    (12, 18, 25, 40, 63, 100, 160, 250),  # >120-180
    (14, 20, 29, 46, 72, 115, 185, 290),  # >180-250
    (16, 23, 32, 52, 81, 130, 210, 320),  # >250-315
    (18, 25, 36, 57, 89, 140, 230, 360),  # >315-400
    (20, 27, 40, 63, 97, 155, 250, 400),  # >400-500
)

# Fundamental deviations for shafts in µm per size step (ISO 286-1 Table 2/3).
# d..g carry the upper deviation es (negative); k..p carry the lower
# deviation ei (positive). h is es=0; js is symmetric (±IT/2).
_SHAFT_ES: dict[str, tuple[int, ...]] = {
    "d": (-20, -30, -40, -50, -65, -80, -100, -120, -145, -170, -190, -210, -230),
    "e": (-14, -20, -25, -32, -40, -50, -60, -72, -85, -100, -110, -125, -135),
    "f": (-6, -10, -13, -16, -20, -25, -30, -36, -43, -50, -56, -62, -68),
    "g": (-2, -4, -5, -6, -7, -9, -10, -12, -14, -15, -17, -18, -20),
}
_SHAFT_EI: dict[str, tuple[int, ...]] = {
    # k applies to IT4..IT7 only (other grades have ei = 0 per the standard).
    "k": (1, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4, 5),
    "m": (2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21, 23),
    "n": (4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37, 40),
    "p": (6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62, 68),
}

# Interference shafts r/s/t/u are sub-stepped more finely than the main steps:
# ISO 286 splits >18-30 into 18-24/24-30, >30-50 into 30-40/40-50, and every step
# above 50 mm in two or three. Flattening them onto the main steps is a silent
# error of tens of microns at the sizes these fits are actually used at.
_FINE_SIZE_STEPS: tuple[tuple[float, float], ...] = (
    (1, 3),
    (3, 6),
    (6, 10),
    (10, 18),
    (18, 24),
    (24, 30),
    (30, 40),
    (40, 50),
    (50, 65),
    (65, 80),
    (80, 100),
    (100, 120),
    (120, 140),
    (140, 160),
    (160, 180),
    (180, 200),
    (200, 225),
    (225, 250),
    (250, 280),
    (280, 315),
    (315, 355),
    (355, 400),
    (400, 450),
    (450, 500),
)

# Fundamental deviations ei (µm) for the interference shafts, one per fine step.
#
# PROVENANCE — this is table data, not a rule, so it is cross-checked in
# tests/test_fits.py against ISO 286-1's own derivation formulae rather than
# trusted on its own:
#   r  = geometric mean of p and s      -> 24/24 steps agree within 1 µm, and p
#                                          was already pinned before r existed
#   s  (>50 mm) = IT7 + 0.40*D          -> agrees within 1 µm
#   s  (<=50 mm) = IT8 + 0..4           -> monotone inside the standard's band
#   t  = IT7 + 0.63*D                   -> 17/19 within 1 µm
#   u  = IT7 + 1.00*D                   -> 17/20 within 1 µm
# D is the geometric mean of the step bounds. The handful of steps where the
# standard's rounding puts the published value 2-3 µm off its own formula are
# listed by name in KNOWN_FORMULA_DIVERGENCES, so a reader holding ISO 286 has
# five numbers to check rather than ninety-five.
#
# t is undefined below 24 mm in the standard and is None there — refused, not
# extrapolated.
_SHAFT_EI_FINE: dict[str, tuple[int | None, ...]] = {
    "r": (
        10,
        15,
        19,
        23,
        28,
        28,
        34,
        34,
        41,
        43,
        51,
        54,
        63,
        65,
        68,
        77,
        80,
        84,
        94,
        98,
        108,
        114,
        126,
        132,
    ),
    "s": (
        14,
        19,
        23,
        28,
        35,
        35,
        43,
        43,
        53,
        59,
        71,
        79,
        92,
        100,
        108,
        122,
        130,
        140,
        158,
        170,
        190,
        208,
        232,
        252,
    ),
    "t": (
        None,
        None,
        None,
        None,
        None,
        41,
        48,
        54,
        66,
        75,
        91,
        104,
        122,
        134,
        146,
        166,
        180,
        196,
        218,
        240,
        268,
        294,
        330,
        360,
    ),
    "u": (
        18,
        23,
        28,
        33,
        41,
        48,
        60,
        70,
        87,
        102,
        124,
        144,
        170,
        190,
        210,
        236,
        258,
        284,
        315,
        350,
        390,
        435,
        490,
        540,
    ),
}


def _fine_size_index(nominal_mm: float) -> int:
    for index, (over, up_to) in enumerate(_FINE_SIZE_STEPS):
        if over < nominal_mm <= up_to:
            return index
    raise ValueError(  # pragma: no cover - _size_index rejects out-of-range first
        f"No ISO 286 sub-step covers {nominal_mm} mm"
    )


SUPPORTED_SHAFT_LETTERS = ("d", "e", "f", "g", "h", "js", "k", "m", "n", "p", "r", "s", "t", "u")
SUPPORTED_HOLE_LETTERS = ("D", "E", "F", "G", "H", "JS", "K", "M", "N", "P")

_FIT_CODE_RE = re.compile(r"^\s*(JS|js|[A-Ha-h]|[K-Pk-p]|[r-ur-u])(\d{1,2})\s*$")

#: Holes derived from the same-letter shaft by the ISO 286 delta rule. No new
#: table data: ES = -ei + Δ, with Δ = IT(n) − IT(n−1) taken from _IT_TABLE.
_DELTA_RULE_HOLES = ("K", "M", "N", "P")

#: The highest IT grade the delta still applies at. Above it ISO 286 sets Δ = 0,
#: so the hole is the exact mirror of the shaft.
_DELTA_MAX_GRADE = {"K": 8, "M": 8, "N": 8, "P": 7}


def _delta_rule_es(letter: str, grade: int, size_index: int, it_um: int) -> float:
    """Upper deviation of a transition/interference hole (ISO 286 delta rule).

    ``ES = -ei + Δ`` where ``ei`` is the same-letter shaft's fundamental
    deviation and ``Δ = IT(n) − IT(n−1)``. Both inputs are already authored in
    this module, so this adds a *rule* rather than a table — and the rule is
    pinned against published values in the tests, which is what makes it
    checkable at all.
    """
    if grade <= _IT_GRADES[0]:
        raise ValueError(
            f"Fit {letter}{grade}: the ISO 286 delta rule needs IT{grade - 1}, which is "
            f"below the authored IT{_IT_GRADES[0]}-IT{_IT_GRADES[-1]} range. Use IT5 or "
            "coarser for a transition/interference hole."
        )
    ei = float(_SHAFT_EI[letter.lower()][size_index])
    if grade > _DELTA_MAX_GRADE[letter]:
        return -ei  # ISO 286: above this grade the delta is zero
    previous_it = _IT_TABLE[size_index][_IT_GRADES.index(grade) - 1]
    return -ei + float(it_um - previous_it)


@dataclass(frozen=True)
class FitDeviation:
    """Resolved fit: deviations in mm relative to the nominal size."""

    code: str
    nominal: float
    upper_mm: float
    lower_mm: float
    it_grade: int
    it_value_mm: float

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "nominal": self.nominal,
            "upper_mm": self.upper_mm,
            "lower_mm": self.lower_mm,
            "it_grade": self.it_grade,
            "it_value_mm": self.it_value_mm,
        }


def parse_fit_code(code: str) -> tuple[str, int]:
    """Split e.g. 'H7' -> ('H', 7) or 'js6' -> ('js', 6)."""
    match = _FIT_CODE_RE.match(code or "")
    if not match:
        raise ValueError(
            f"Invalid fit code {code!r}. Expected letter + IT grade, e.g. H7, g6, js9."
        )
    return match.group(1), int(match.group(2))


def _size_index(nominal_mm: float) -> int:
    if not 1.0 < nominal_mm <= 500.0:
        raise ValueError(
            f"Nominal size {nominal_mm} mm is outside the authored ISO 286 range "
            "(over 1 mm up to and including 500 mm)."
        )
    for index, (over, up_to) in enumerate(_SIZE_STEPS):
        if over < nominal_mm <= up_to:
            return index
    raise ValueError(f"No ISO 286 size step covers {nominal_mm} mm")  # pragma: no cover


def _it_um(grade: int, size_index: int) -> int:
    if grade not in _IT_GRADES:
        raise ValueError(f"IT grade {grade} is outside the authored range IT4-IT11.")
    return _IT_TABLE[size_index][_IT_GRADES.index(grade)]


def fit_lookup(code: str, nominal_mm: float) -> FitDeviation:
    """Resolve a fit code for a nominal size into mm deviations.

    Holes use uppercase letters, shafts lowercase (ISO 286 convention).
    Raises ValueError for codes outside the authored scope.
    """
    letter, grade = parse_fit_code(code)
    index = _size_index(float(nominal_mm))
    it_um = _it_um(grade, index)

    if letter in ("js", "JS"):
        upper_um = it_um / 2.0
        lower_um = -it_um / 2.0
    elif letter == "h":
        upper_um, lower_um = 0.0, float(-it_um)
    elif letter == "H":
        upper_um, lower_um = float(it_um), 0.0
    elif letter in _SHAFT_ES:  # d, e, f, g — clearance shafts
        es = float(_SHAFT_ES[letter][index])
        upper_um, lower_um = es, es - it_um
    elif letter in ("D", "E", "F", "G"):  # mirrored clearance holes: EI = -es
        ei = float(-_SHAFT_ES[letter.lower()][index])
        upper_um, lower_um = ei + it_um, ei
    elif letter in _SHAFT_EI:  # k, m, n, p — transition/interference shafts
        if letter == "k" and not 4 <= grade <= 7:
            ei = 0.0  # ISO 286: k has ei = 0 outside IT4-IT7
        else:
            ei = float(_SHAFT_EI[letter][index])
        upper_um, lower_um = ei + it_um, ei
    elif letter in _DELTA_RULE_HOLES:  # K, M, N, P — transition/interference holes
        es = _delta_rule_es(letter, grade, index, it_um)
        upper_um, lower_um = es, es - it_um
    elif letter in _SHAFT_EI_FINE:  # r, s, t, u — interference shafts
        ei_um = _SHAFT_EI_FINE[letter][_fine_size_index(float(nominal_mm))]
        if ei_um is None:
            raise ValueError(
                f"Fit {letter}{grade}: ISO 286 does not define shaft {letter!r} below "
                f"24 mm, and {nominal_mm} mm is under it. Use s (defined from 1 mm) "
                "for a lighter interference fit, or explicit tol_upper/tol_lower."
            )
        ei = float(ei_um)
        upper_um, lower_um = ei + it_um, ei
    else:
        raise ValueError(
            f"Fit letter {letter!r} is outside the authored scope. Supported: "
            f"shafts {SUPPORTED_SHAFT_LETTERS}, holes {SUPPORTED_HOLE_LETTERS}."
        )

    return FitDeviation(
        code=f"{letter}{grade}",
        nominal=float(nominal_mm),
        upper_mm=round(upper_um / 1000.0, 6),
        lower_mm=round(lower_um / 1000.0, 6),
        it_grade=grade,
        it_value_mm=round(it_um / 1000.0, 6),
    )
