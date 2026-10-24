"""B07 — ``engineering/validation.py``: one set of spec guards, refusing by path."""

from __future__ import annotations

import math

import pytest

import config
from engineering.validation import (
    SpecError,
    finite,
    int_in_range,
    non_negative,
    positive,
    short_repr,
    within_budget,
)


def test_spec_error_is_a_value_error():
    assert issubclass(SpecError, ValueError)


@pytest.mark.parametrize(
    "value, expected",
    [(0, 0.0), (-2.5, -2.5), (10**20, 1e20), (3, 3.0)],
)
def test_finite_accepts_real_numbers(value, expected):
    assert finite(value, "x") == expected


@pytest.mark.parametrize(
    "value, message",
    [
        (float("nan"), "equipment[2].x: nan is not a finite number."),
        (float("inf"), "equipment[2].x: inf is not a finite number."),
        (float("-inf"), "equipment[2].x: -inf is not a finite number."),
        (True, "equipment[2].x: expected a number, got True."),
        ("5", "equipment[2].x: expected a number, got '5'."),
        (None, "equipment[2].x: expected a number, got None."),
    ],
)
def test_finite_refuses_by_path(value, message):
    with pytest.raises(SpecError) as excinfo:
        finite(value, "equipment[2].x")
    assert str(excinfo.value) == message


def test_a_huge_integer_is_not_a_float():
    with pytest.raises(SpecError, match=r"^lines\[0\]\.stub: .* is not a finite number\.$"):
        finite(10**400, "lines[0].stub")


def test_positive_names_the_path_and_the_value():
    assert positive(0.5, "s") == 0.5
    with pytest.raises(SpecError) as excinfo:
        positive(-1, "equipment[2].scale")
    assert str(excinfo.value) == "equipment[2].scale: must be greater than zero, got -1.0."
    with pytest.raises(SpecError, match="must be greater than zero, got 0.0"):
        positive(0, "s")
    with pytest.raises(SpecError, match="greater than zero"):
        positive(1e-10, "s")  # inside the 1e-9 tolerance mech/part.py always used


def test_non_negative_folds_the_tolerance_band_to_zero():
    assert non_negative(0, "stub") == 0.0
    assert non_negative(-1e-10, "stub") == 0.0
    assert non_negative(4.0, "stub") == 4.0
    with pytest.raises(SpecError) as excinfo:
        non_negative(-0.5, "lines[1].stub")
    assert str(excinfo.value) == "lines[1].stub: must not be negative, got -0.5."


def test_int_in_range():
    assert int_in_range(3, "trays", 0, 10) == 3
    with pytest.raises(SpecError, match=r"^trays: expected an integer, got 3\.0\.$"):
        int_in_range(3.0, "trays", 0, 10)
    with pytest.raises(SpecError, match=r"^trays: expected an integer, got True\.$"):
        int_in_range(True, "trays", 0, 10)
    with pytest.raises(SpecError, match=r"^trays: must be from 0 to 10, got 11\.$"):
        int_in_range(11, "trays", 0, 10)


def test_within_budget_names_the_setting():
    within_budget(5, 5, "equipment", "MAX_SPEC_ITEMS")
    within_budget(10**9, 0, "equipment", "MAX_SPEC_ITEMS")  # 0 disables
    with pytest.raises(SpecError) as excinfo:
        within_budget(6, 5, "equipment", "MAX_SPEC_ITEMS")
    message = str(excinfo.value)
    assert message.startswith("equipment: 6 is over the limit of 5")
    assert "MAX_SPEC_ITEMS" in message


def test_echoed_values_are_bounded():
    huge = "x" * 2_000_000
    assert len(short_repr(huge)) <= 90
    with pytest.raises(SpecError) as excinfo:
        finite(huge, "entities[0].x1")
    assert len(str(excinfo.value)) < 200


def test_the_budgets_have_defaults_and_env_overrides(monkeypatch):
    monkeypatch.delenv("MAX_SPEC_ITEMS", raising=False)
    monkeypatch.delenv("MAX_VESSEL_TRAYS", raising=False)
    fresh = config.Settings()
    assert fresh.max_spec_items == 2000
    assert fresh.max_vessel_trays == 500
    monkeypatch.setenv("MAX_SPEC_ITEMS", "7")
    monkeypatch.setenv("MAX_VESSEL_TRAYS", "3")
    fresh = config.Settings()
    assert (fresh.max_spec_items, fresh.max_vessel_trays) == (7, 3)


def test_finite_returns_a_float_not_the_input_type():
    value = finite(7, "x")
    assert isinstance(value, float) and math.isclose(value, 7.0, abs_tol=1e-9)
