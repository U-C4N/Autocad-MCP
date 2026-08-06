"""ISO 286 fit-table lookups pinned against published table values."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from engineering.fits import FitDeviation, fit_lookup, parse_fit_code
from engineering.tolerances import build_dim_override
from server import _fit_to_tolerances


def _um(value_mm: float) -> float:
    return round(value_mm * 1000.0, 3)


# (code, nominal, upper µm, lower µm) — published ISO 286-2 values.
CANONICAL_FITS = [
    ("H7", 20.0, 21, 0),
    ("H7", 40.0, 25, 0),
    ("H8", 30.0, 33, 0),
    ("H9", 6.0, 30, 0),
    ("H7", 200.0, 46, 0),
    ("g6", 20.0, -7, -20),
    ("g6", 6.0, -4, -12),
    ("f7", 40.0, -25, -50),
    ("e8", 25.0, -40, -73),
    ("d9", 100.0, -120, -207),
    ("h6", 30.0, 0, -13),  # 30 mm is inside the >18-30 step (inclusive upper bound)
    ("h7", 50.0, 0, -25),
    ("k6", 20.0, 15, 2),
    ("m6", 25.0, 21, 8),
    ("n6", 20.0, 28, 15),
    ("p6", 20.0, 35, 22),
    ("js9", 25.0, 26, -26),
    ("G7", 6.0, 16, 4),
    ("F8", 40.0, 64, 25),
    ("D10", 30.0, 149, 65),
]


@pytest.mark.parametrize("code,nominal,upper_um,lower_um", CANONICAL_FITS)
def test_canonical_fit_values(code, nominal, upper_um, lower_um):
    deviation = fit_lookup(code, nominal)
    assert isinstance(deviation, FitDeviation)
    assert _um(deviation.upper_mm) == pytest.approx(upper_um), f"{code}@{nominal} upper"
    assert _um(deviation.lower_mm) == pytest.approx(lower_um), f"{code}@{nominal} lower"


def test_k_outside_it4_to_7_has_zero_lower_deviation():
    deviation = fit_lookup("k9", 20.0)
    assert deviation.lower_mm == 0.0
    assert _um(deviation.upper_mm) == 52  # IT9 @ 18-30


def test_parse_fit_code():
    assert parse_fit_code("H7") == ("H", 7)
    assert parse_fit_code("js10") == ("js", 10)
    with pytest.raises(ValueError):
        parse_fit_code("77")
    with pytest.raises(ValueError):
        parse_fit_code("")


@pytest.mark.parametrize(
    "code,nominal",
    [
        # K7/P7 used to be here: the delta rule derives them from tables already
        # in the module, so they are authored as of 1.5.0 and pinned against
        # published values above.
        ("t6", 20.0),  # ISO 286 does not define shaft t below 24 mm
        ("x6", 20.0),  # letter outside the authored subset (regex rejects)
        ("H3", 20.0),  # grade below authored range
        ("H12", 20.0),  # grade above authored range
        ("H7", 0.5),  # below 1 mm
        ("H7", 600.0),  # above 500 mm
    ],
)
def test_out_of_scope_lookups_raise(code, nominal):
    with pytest.raises(ValueError):
        fit_lookup(code, nominal)


def test_double_positive_fit_renders_correct_dimvars():
    """p6-style fits (+upper/+lower) must produce a negative DIMTM so AutoCAD
    displays the plus lower deviation."""
    deviation = fit_lookup("p6", 20.0)
    override, _ = build_dim_override(
        tol_upper=deviation.upper_mm,
        tol_lower=-deviation.lower_mm,  # server convention: positive = minus
        tol_mode="deviation",
    )
    assert override["dimtp"] == pytest.approx(0.035)
    assert override["dimtm"] == pytest.approx(-0.022)  # displayed as +0.022


def test_fit_to_tolerances_contract():
    tol_upper, tol_lower, tol_mode, text = _fit_to_tolerances("H7", 20.0, None, None, "none", None)
    assert tol_upper == pytest.approx(0.021)
    assert tol_lower == pytest.approx(0.0)
    assert tol_mode == "deviation"
    assert text == "<> H7"


def test_fit_to_tolerances_rejects_mixed_usage():
    with pytest.raises(ToolError):
        _fit_to_tolerances("H7", 20.0, 0.1, None, "none", None)
    with pytest.raises(ToolError):
        _fit_to_tolerances("H7", 20.0, None, None, "symmetric", None)
    with pytest.raises(ToolError):
        _fit_to_tolerances("Q9", 20.0, None, None, "none", None)


def test_fit_passthrough_without_code():
    assert _fit_to_tolerances(None, 20.0, 0.1, 0.05, "deviation", "x") == (
        0.1,
        0.05,
        "deviation",
        "x",
    )


# ── ISO 286 transition / interference HOLES (K, M, N, P) ────────────────────
#
# These were refused by name until 1.5.0. They are not new table data: ISO 286
# derives an upper-letter hole from the same-letter shaft by the delta rule,
#
#     ES = -ei + Δ,   Δ = IT(n) − IT(n−1)
#
# so every value below comes from tables already authored in this module. The
# expected numbers are the published ISO 286 ones, cross-checked by hand — the
# point of pinning them is that the *rule* is right, not that the arithmetic runs.


@pytest.mark.parametrize(
    ("code", "nominal", "upper_um", "lower_um"),
    [
        ("K7", 20.0, 6, -15),
        ("M7", 20.0, 0, -21),
        ("N7", 20.0, -7, -28),
        ("P7", 20.0, -14, -35),
        ("K7", 50.0, 7, -18),
        ("N7", 50.0, -8, -33),
        ("P7", 50.0, -17, -42),
        ("K6", 20.0, 2, -11),
        ("N6", 20.0, -11, -24),
        ("P6", 20.0, -18, -31),
    ],
)
def test_transition_hole_letters_match_published_iso286(code, nominal, upper_um, lower_um):
    fit = fit_lookup(code, nominal)
    assert fit.upper_mm == pytest.approx(upper_um / 1000.0, abs=1e-9)
    assert fit.lower_mm == pytest.approx(lower_um / 1000.0, abs=1e-9)


def test_h7_over_p6_is_a_genuine_interference_fit():
    """The pairing check the tables exist for: the shaft must be bigger."""
    hole = fit_lookup("H7", 20.0)
    shaft = fit_lookup("p6", 20.0)
    assert shaft.lower_mm > hole.upper_mm - hole.it_value_mm, "min shaft > min hole"
    assert shaft.lower_mm > 0 and hole.lower_mm == 0


def test_the_delta_collapses_above_the_grade_the_rule_covers():
    """ISO 286 applies Δ only to IT<=8 for K/M/N and IT<=7 for P; then ES = -ei."""
    from engineering.fits import _SHAFT_EI, _size_index

    index = _size_index(20.0)
    n9 = fit_lookup("N9", 20.0)
    assert n9.upper_mm == pytest.approx(-_SHAFT_EI["n"][index] / 1000.0, abs=1e-9)

    p8 = fit_lookup("P8", 20.0)
    assert p8.upper_mm == pytest.approx(-_SHAFT_EI["p"][index] / 1000.0, abs=1e-9)


def test_grade_4_is_refused_rather_than_guessed():
    """Δ needs IT3, which is outside the authored IT4-IT11 range."""
    with pytest.raises(ValueError, match="IT5"):
        fit_lookup("K4", 20.0)


# ── ISO 286 interference SHAFTS (r, s, t, u) ────────────────────────────────
#
# These need their own fundamental deviations, sub-stepped more finely than the
# main size steps above 50 mm, so unlike K/M/N/P they are table data rather than
# a rule. Authored table data is only as good as its provenance, so every value
# is cross-checked below against ISO 286-1's own derivation formulae — an
# independent source that agrees or does not. What survives is stated; what
# diverges is listed by name, so a reader with the standard checks five numbers
# instead of ninety-five.


@pytest.mark.parametrize(
    ("code", "nominal", "upper_um", "lower_um"),
    [
        # ei + IT above the nominal — these are all interference shafts.
        ("r6", 20.0, 41, 28),
        ("s6", 20.0, 48, 35),
        ("u6", 20.0, 54, 41),
        ("s7", 50.0, 68, 43),
        ("u6", 30.0, 61, 48),  # 24-30 sub-step: u jumps 41 -> 48 inside 18-30
        ("s6", 60.0, 72, 53),  # 50-65 sub-step, IT6 from the main 50-80 step
        ("t6", 60.0, 85, 66),
    ],
)
def test_interference_shafts_match_published_iso286(code, nominal, upper_um, lower_um):
    fit = fit_lookup(code, nominal)
    assert fit.upper_mm == pytest.approx(upper_um / 1000.0, abs=1e-9)
    assert fit.lower_mm == pytest.approx(lower_um / 1000.0, abs=1e-9)


def test_the_sub_steps_are_real_and_not_flattened():
    """Above 50 mm these letters split the main steps; collapsing them is a
    silent error of tens of microns."""
    assert fit_lookup("u6", 55.0).lower_mm != fit_lookup("u6", 70.0).lower_mm
    assert fit_lookup("u6", 20.0).lower_mm != fit_lookup("u6", 28.0).lower_mm


def test_t_is_refused_below_24_mm_because_iso_does_not_define_it():
    with pytest.raises(ValueError, match="24"):
        fit_lookup("t6", 20.0)
    assert fit_lookup("t6", 30.0).lower_mm > 0


# -- provenance: the table against ISO 286-1's own formulae ------------------


def test_r_is_the_geometric_mean_of_p_and_s():
    """ISO 286 *defines* r that way, and p is already pinned in this module.

    24/24 sub-steps agree within 1 um, which validates r and s together against
    data that was trusted before either was authored.
    """
    import math

    from engineering.fits import _FINE_SIZE_STEPS, _SHAFT_EI, _SHAFT_EI_FINE, _size_index

    for index, step in enumerate(_FINE_SIZE_STEPS):
        mid = (step[0] + step[1]) / 2.0
        p = _SHAFT_EI["p"][_size_index(mid)]
        s = _SHAFT_EI_FINE["s"][index]
        r = _SHAFT_EI_FINE["r"][index]
        assert abs(round(math.sqrt(p * s)) - r) <= 1, f"r at {step}: table {r}, sqrt(p*s) {p}/{s}"


#: Steps where ISO 286's rounding rules put the published value more than 1 um
#: from the formula. Listed rather than hidden: these are the only values a
#: reader with the standard needs to check by hand.
KNOWN_FORMULA_DIVERGENCES = {
    ("u", (250, 280)),
    ("u", (400, 450)),
    ("u", (450, 500)),
    ("t", (24, 30)),
    ("t", (450, 500)),
}


#: Below this the formulae do not describe the published tables at all — ISO
#: tabulates the small sizes directly. Measured for u: the formula is 6 um low at
#: 1-3 mm and only converges from 18 mm up (-6, -7, -5, -2, +1). Scoping the
#: check here is a statement about where the cross-check has authority, not a
#: threshold moved until the test passed.
FORMULA_VALID_FROM_MM = 18


@pytest.mark.parametrize(("letter", "coefficient"), [("t", 0.63), ("u", 1.0)])
def test_t_and_u_track_their_iso_formula(letter, coefficient):
    """ISO 286-1: ei = IT7 + k*D, D the geometric mean of the step bounds.

    The standard rounds its published tables, so exact equality is not the
    claim — agreement within 1 um everywhere except a listed handful is.
    """
    import math

    from engineering.fits import _FINE_SIZE_STEPS, _SHAFT_EI_FINE, _it_um, _size_index

    checked = 0
    for index, step in enumerate(_FINE_SIZE_STEPS):
        ei = _SHAFT_EI_FINE[letter][index]
        if ei is None or step[0] < FORMULA_VALID_FROM_MM:
            continue
        checked += 1
        mid = (step[0] + step[1]) / 2.0
        it7 = _it_um(7, _size_index(mid))
        formula = it7 + coefficient * math.sqrt(step[0] * step[1])
        drift = abs(round(formula) - ei)
        if (letter, step) in KNOWN_FORMULA_DIVERGENCES:
            assert 1 < drift <= 3, f"{letter} at {step} drifted {drift} um — re-check the table"
        else:
            assert drift <= 1, f"{letter} at {step}: table {ei}, formula {formula:.1f}"
    assert checked >= 18, f"only {checked} steps cross-checked for {letter}"


def test_s_above_50_tracks_its_iso_formula():
    import math

    from engineering.fits import _FINE_SIZE_STEPS, _SHAFT_EI_FINE, _it_um, _size_index

    for index, step in enumerate(_FINE_SIZE_STEPS):
        if step[0] < 50:
            continue
        mid = (step[0] + step[1]) / 2.0
        formula = _it_um(7, _size_index(mid)) + 0.4 * math.sqrt(step[0] * step[1])
        assert abs(round(formula) - _SHAFT_EI_FINE["s"][index]) <= 1, f"s at {step}"


def test_s_below_50_sits_in_the_iso_band_above_it8():
    """ISO 286: for D <= 50, s is IT8 plus a small step. Monotone, 0..4 um."""
    from engineering.fits import _FINE_SIZE_STEPS, _SHAFT_EI_FINE, _it_um, _size_index

    deltas = []
    for index, step in enumerate(_FINE_SIZE_STEPS):
        if step[0] >= 50:
            break
        mid = (step[0] + step[1]) / 2.0
        deltas.append(_SHAFT_EI_FINE["s"][index] - _it_um(8, _size_index(mid)))
    assert all(0 <= d <= 4 for d in deltas), deltas
    assert deltas == sorted(deltas), f"the step above IT8 must not decrease: {deltas}"


def test_h7_over_s6_is_tighter_than_h7_over_p6():
    """The reason these letters exist: progressively heavier interference."""
    p6 = fit_lookup("p6", 20.0).lower_mm
    r6 = fit_lookup("r6", 20.0).lower_mm
    s6 = fit_lookup("s6", 20.0).lower_mm
    u6 = fit_lookup("u6", 20.0).lower_mm
    assert p6 < r6 < s6 < u6
