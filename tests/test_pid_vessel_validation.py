"""B07 on parametric vessels: backlog rows W2-T10-04, -05, -06, -07 and -08.

Every expected coordinate is computed here from the capsule geometry: a
vertical capsule of width ``w`` and height ``h`` has heads of radius ``r = w/2``
centred at ``(0, ±(h/2 - r))``, so a horizontal line at height ``y`` inside a
head meets it at ``x = ±sqrt(r² - (|y| - (h/2 - r))²)``.
"""

from __future__ import annotations

import math

import pytest

import config
from engineering.pid.symbols_vessels import build_vessel, validate_vessel_params
from engineering.validation import SpecError

# ── W2-T10-04: NaN / inf sizes and fractions ────────────────────────────────


@pytest.mark.parametrize(
    "params, path",
    [
        ({"width": float("nan")}, "vertical_vessel: params.width"),
        ({"height": float("inf")}, "vertical_vessel: params.height"),
        ({"width": -4.0}, "vertical_vessel: params.width"),
        (
            {"nozzles": [{"name": "A", "side": "top", "fraction": float("nan")}]},
            r"vertical_vessel: nozzles\[0\]\.fraction",
        ),
        (
            {"nozzles": [{"name": "A", "side": "top", "fraction": float("-inf")}]},
            r"vertical_vessel: nozzles\[0\]\.fraction",
        ),
    ],
)
def test_non_finite_sizes_and_fractions_are_refused_by_path(params, path):
    """RED: width NaN was accepted and height inf died as a RuntimeError in _nozzles."""
    with pytest.raises(SpecError, match=f"^{path}: "):
        validate_vessel_params("vertical_vessel", params)


# ── W2-T10-07: the tray budget and spacing ──────────────────────────────────


def test_a_billion_trays_are_refused_naming_the_setting():
    """RED: trays=10**9 was accepted (10**6 took 6.3 s and 272 MB to build)."""
    with pytest.raises(SpecError) as excinfo:
        validate_vessel_params("column", {"trays": 10**9})
    message = str(excinfo.value)
    assert message.startswith("column: params.trays: 1000000000 is over the limit of 500")
    assert "MAX_VESSEL_TRAYS" in message


def test_the_tray_budget_follows_the_setting(monkeypatch):
    monkeypatch.setattr(config.settings, "max_vessel_trays", 4)
    with pytest.raises(SpecError, match="MAX_VESSEL_TRAYS"):
        validate_vessel_params("column", {"trays": 5})
    assert validate_vessel_params("column", {"trays": 4})["trays"] == 4


def test_trays_closer_than_a_millimetre_are_refused():
    # height 60 / (60 + 1) = 0.98 mm apart; 59 trays sit exactly 1 mm apart.
    with pytest.raises(SpecError, match=r"60 trays in a 60 mm shell sit 0\.984 mm apart"):
        validate_vessel_params("column", {"trays": 60})
    assert validate_vessel_params("column", {"trays": 59})["trays"] == 59


def test_trays_must_be_an_integer():
    with pytest.raises(SpecError, match=r"^column: params\.trays: expected an integer"):
        validate_vessel_params("column", {"trays": 3.0})


# ── W2-T10-05: trays clipped to the dished heads ────────────────────────────


def test_trays_stop_inside_the_dished_heads():
    """RED: every tray was drawn ±(w/2 - 1) wide, so the trays inside the heads
    poked through the outline (up to 3.254 mm measured on this shape)."""
    w, h, trays = 16.0, 20.0, 9
    r, body = w / 2, h / 2 - w / 2  # 8, 2
    spec = build_vessel("column", {"width": w, "height": h, "trays": trays, "nozzles": []})
    drawn = spec.primitives[4:]  # the shell is two walls and two heads
    assert len(drawn) == trays
    for i, tray in enumerate(drawn):
        y = -h / 2 + (i + 1) * h / (trays + 1)
        half = r if abs(y) <= body else math.sqrt(r * r - (abs(y) - body) ** 2)
        assert tray["y1"] == pytest.approx(y, abs=1e-9)
        assert tray["y2"] == pytest.approx(y, abs=1e-9)
        assert tray["x1"] == pytest.approx(-(half - 1.0), abs=1e-9)
        assert tray["x2"] == pytest.approx(half - 1.0, abs=1e-9)
    # y = ±8 is in the heads: sqrt(64 - 36) - 1 = 4.2915..., not the old 7.
    assert drawn[-1]["x2"] == pytest.approx(math.sqrt(28.0) - 1.0, abs=1e-9)


# ── W2-T10-06: a nozzle half way to the centre ──────────────────────────────


def test_a_side_nozzle_at_the_pole_is_refused():
    """RED: left at fraction 0 drew a 13 mm stub from the bottom pole (0, -20)."""
    with pytest.raises(SpecError) as excinfo:
        validate_vessel_params(
            "vertical_vessel", {"nozzles": [{"name": "L", "side": "left", "fraction": 0.0}]}
        )
    message = str(excinfo.value)
    assert message.startswith("vertical_vessel: nozzles[0] ('L') at left fraction 0")
    assert "half way or more to the centre" in message


def test_a_side_nozzle_just_off_the_pole_meets_the_head():
    # w 20, h 40: r 10, heads centred at (0, ±10). fraction 0.05 -> y = -18,
    # x = -sqrt(100 - 64) = -6: 4 mm inside the 10 mm half width (< 5), kept.
    spec = build_vessel(
        "vertical_vessel", {"nozzles": [{"name": "L", "side": "left", "fraction": 0.05}]}
    )
    stub = spec.primitives[-1]
    assert (stub["x1"], stub["y1"]) == (
        pytest.approx(-6.0, abs=1e-9),
        pytest.approx(-18.0, abs=1e-9),
    )
    assert (stub["x2"], stub["y2"]) == (
        pytest.approx(-13.0, abs=1e-9),
        pytest.approx(-18.0, abs=1e-9),
    )


def test_a_hopper_outlet_corner_is_refused():
    # hopper 24 x 24: bottom fraction 0 -> x = -12 meets only the top corner (-12, 12):
    # 24 mm above the bottom edge of a 12 mm half height.
    with pytest.raises(SpecError, match=r"hopper: nozzles\[0\] \('B'\) at bottom fraction 0"):
        validate_vessel_params(
            "hopper", {"nozzles": [{"name": "B", "side": "bottom", "fraction": 0.0}]}
        )


# ── W2-T10-08: one place, one nozzle; -0.0 is 0.0 ──────────────────────────


def test_two_nozzles_at_one_place_are_refused():
    """RED: both were drawn, one stub on top of the other."""
    nozzles = [
        {"name": "A", "side": "top", "fraction": 0.5},
        {"name": "B", "side": "top", "fraction": 0.5},
    ]
    with pytest.raises(SpecError) as excinfo:
        validate_vessel_params("vertical_vessel", {"nozzles": nozzles})
    assert str(excinfo.value).startswith(
        "vertical_vessel: nozzles[1] sits at top fraction 0.5, where nozzles[0] already is"
    )


def test_negative_zero_is_the_same_block_as_zero():
    """RED: fraction -0.0 hashed to a different block name than 0.0."""

    def name(fraction):
        nozzles = [{"name": "A", "side": "top", "fraction": fraction}]
        return build_vessel("tank", {"nozzles": nozzles}).name

    assert name(-0.0) == name(0.0)
    params = validate_vessel_params(
        "tank", {"nozzles": [{"name": "A", "side": "top", "fraction": -0.0}]}
    )
    assert math.copysign(1.0, params["nozzles"][0]["fraction"]) == 1.0
