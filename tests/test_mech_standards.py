"""The standards registry: one row or a named refusal, never an interpolation."""

from __future__ import annotations

import pytest

import engineering.mech.standards as standards_mod
from engineering.mech.standards import (
    Coverage,
    coverage_of,
    lookup,
    register,
    source_of,
    standards,
)

SOURCE = "TEST 1 (2026), table 1 — fixture rows, not a real standard"


@pytest.fixture(autouse=True)
def _fixture_tables():
    """Swap the process-wide registry for three fixture tables, then restore it."""
    saved = dict(standards_mod._REGISTRY)
    standards_mod._REGISTRY.clear()
    register(
        "TEST 1",
        {10: {"d": 10.0, "w": 3.0}, 20: {"d": 20.0, "w": 6.0}},
        Coverage(10.0, 20.0),
        SOURCE,
    )
    register(
        "TEST 2",
        {18: ({"form": "E", "r": 0.6}, {"form": "F", "r": 0.8})},
        Coverage(18.0, 18.0),
        SOURCE,
    )
    register("TEST 3", {"M12": {"pitch": 1.75}}, Coverage(12.0, 12.0), SOURCE)
    yield
    standards_mod._REGISTRY.clear()
    standards_mod._REGISTRY.update(saved)


def test_registered_standards_are_listed_and_carry_their_source():
    assert standards() == ("TEST 1", "TEST 2", "TEST 3")
    assert source_of("TEST 1") == SOURCE
    assert coverage_of("TEST 1") == Coverage(10.0, 20.0, "mm")


def test_a_listed_size_returns_a_copy_of_its_row():
    row = lookup("TEST 1", 10)
    assert row == {"d": 10.0, "w": 3.0}
    row["w"] = 99.0
    assert lookup("TEST 1", 10)["w"] == 3.0


def test_a_size_outside_the_coverage_is_refused_by_name():
    with pytest.raises(ValueError, match="TEST 1 covers 10-20 mm; 95 mm is outside the table"):
        lookup("TEST 1", 95)


def test_a_size_inside_the_coverage_but_not_in_the_table_is_never_interpolated():
    with pytest.raises(ValueError, match="no row for 15"):
        lookup("TEST 1", 15)


def test_a_variant_is_selected_by_keyword():
    assert lookup("TEST 2", 18, form="E")["r"] == 0.6
    assert lookup("TEST 2", 18, form="F")["r"] == 0.8


def test_an_unknown_variant_is_refused_and_an_ambiguous_one_too():
    with pytest.raises(ValueError, match="no form='G' row"):
        lookup("TEST 2", 18, form="G")
    with pytest.raises(ValueError, match="ambiguous"):
        lookup("TEST 2", 18)


def test_a_designation_key_is_case_insensitive():
    assert lookup("TEST 3", "m12")["pitch"] == 1.75


def test_an_unregistered_standard_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="DIN 9999.*TEST 1"):
        lookup("DIN 9999", 10)


def test_a_source_that_does_not_name_its_table_is_refused():
    with pytest.raises(ValueError, match="SOURCE"):
        register("TEST 4", {1: {"a": 1}}, Coverage(1.0, 1.0), "made up")


def test_a_table_may_register_its_structure_before_its_rows_are_transcribed():
    """DIN 509, DIN 471/472, DIN 332-1 and ISO 3601-2 ship exactly this way."""
    register("TEST 5", {}, Coverage(0.0, 0.0), SOURCE)
    assert "TEST 5" in standards()
    with pytest.raises(ValueError, match="TEST 5"):
        lookup("TEST 5", 12)


def test_a_rows_argument_that_is_not_a_dict_is_refused():
    with pytest.raises(ValueError, match="rows dict"):
        register("TEST 6", [{"d": 1.0}], Coverage(1.0, 1.0), SOURCE)


def test_registering_the_same_standard_twice_is_refused():
    with pytest.raises(ValueError, match="already registered"):
        register("TEST 1", {10: {"d": 10.0}}, Coverage(10.0, 10.0), SOURCE)
