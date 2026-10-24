"""B07: the mechanical part model and the arch plan spec use the shared guards."""

from __future__ import annotations

import copy

import pytest

import config
from engineering import validation
from engineering.arch.spec import EXAMPLE_SPEC as ARCH_EXAMPLE
from engineering.arch.spec import validate_spec as validate_arch_spec
from engineering.mech import part as mech_part
from engineering.mech.part import build_part


def test_the_part_model_uses_the_shared_guards():
    assert mech_part._finite is validation.finite
    assert mech_part._positive is validation.positive
    assert mech_part._non_negative is validation.non_negative


@pytest.mark.parametrize(
    "segment, message",
    [
        (
            {"length": True, "d_outer": 20},
            "segments[0].length: expected a number, got True.",
        ),
        (
            {"length": float("nan"), "d_outer": 20},
            "segments[0].length: nan is not a finite number.",
        ),
        (
            {"length": 10, "d_outer": -20},
            "segments[0].d_outer: must be greater than zero, got -20.0.",
        ),
    ],
)
def test_a_part_refuses_by_path(segment, message):
    """RED for True: float(True) == 1.0 was accepted as a 1 mm segment."""
    with pytest.raises(validation.SpecError) as excinfo:
        build_part({"kind": "revolved", "name": "shaft", "segments": [segment]})
    assert str(excinfo.value) == message


def test_an_arch_list_over_the_budget_is_refused_naming_the_setting(monkeypatch):
    """RED: no list of an arch spec had a bound."""
    monkeypatch.setattr(config.settings, "max_spec_items", 3)
    spec = copy.deepcopy(ARCH_EXAMPLE)  # four openings
    with pytest.raises(validation.SpecError) as excinfo:
        validate_arch_spec(spec)
    assert str(excinfo.value).startswith("openings: 4 is over the limit of 3")
    assert "MAX_SPEC_ITEMS" in str(excinfo.value)


def test_the_arch_example_is_within_the_default_budget():
    assert validate_arch_spec(copy.deepcopy(ARCH_EXAMPLE))["scale"] == 50.0
