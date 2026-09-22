"""ISO 15 boundary dimensions and the ISO 8826 representation conventions."""

from __future__ import annotations

import pytest

from engineering.mech.standards import standards
from engineering.mech.standards.bearings import (
    BALL_SYMBOL_FRACTION,
    BEARING_SERIES,
    CROSS_FRACTION,
    DEEP_GROOVE,
    SOURCE_ISO_15,
    SOURCE_ISO_8826_1,
    SOURCE_ISO_8826_2,
    bearing,
    list_bearings,
)


@pytest.mark.parametrize(
    ("designation", "bore", "outer", "width"),
    [
        ("6000", 10.0, 26.0, 8.0),
        ("6004", 20.0, 42.0, 12.0),
        ("6006", 30.0, 55.0, 13.0),
        ("6010", 50.0, 80.0, 16.0),
        ("6200", 10.0, 30.0, 9.0),
        ("6203", 17.0, 40.0, 12.0),
        ("6205", 25.0, 52.0, 15.0),
        ("6208", 40.0, 80.0, 18.0),
        ("6210", 50.0, 90.0, 20.0),
        ("6300", 10.0, 35.0, 11.0),
        ("6305", 25.0, 62.0, 17.0),
        ("6309", 45.0, 100.0, 25.0),
        ("6310", 50.0, 110.0, 27.0),
    ],
)
def test_named_rows_match_iso15(designation, bore, outer, width):
    row = bearing(designation)
    assert (row["d"], row["D"], row["B"]) == (bore, outer, width)
    assert row["designation"] == designation
    assert row["source"] == SOURCE_ISO_15


def test_bore_number_rule_holds_across_every_series():
    """ISO 15: bore 00/01/02/03 = 10/12/15/17 mm; 04 and up = 5 x bore number."""
    for series in ("60", "62", "63"):
        assert bearing(f"{series}00")["d"] == 10.0
        assert bearing(f"{series}01")["d"] == 12.0
        assert bearing(f"{series}02")["d"] == 15.0
        assert bearing(f"{series}03")["d"] == 17.0
        for n in range(4, 11):
            assert bearing(f"{series}{n:02d}")["d"] == 5.0 * n


def test_outside_diameter_and_width_grow_with_the_series():
    for n in range(0, 11):
        light = bearing(f"60{n:02d}")
        medium = bearing(f"62{n:02d}")
        heavy = bearing(f"63{n:02d}")
        assert light["d"] == medium["d"] == heavy["d"]
        assert light["D"] < medium["D"] < heavy["D"]
        assert light["B"] < medium["B"] < heavy["B"]


def test_coverage_refusals_name_the_table():
    with pytest.raises(ValueError, match="ISO 15"):
        bearing("6011")
    with pytest.raises(ValueError, match="ISO 15"):
        bearing("6211")
    with pytest.raises(ValueError, match="ISO 15"):
        bearing("6311")
    with pytest.raises(ValueError, match="ISO 15"):
        bearing("61805")  # the 618x light series is not transcribed
    with pytest.raises(ValueError, match="ISO 15"):
        bearing("6205-2RS")  # a seal suffix is a variant this table does not carry


def test_representation_fractions_are_declared_conventions_not_table_values():
    assert 0.0 < CROSS_FRACTION < 1.0
    assert 0.0 < BALL_SYMBOL_FRACTION < 1.0
    assert "ISO 8826-1" in SOURCE_ISO_8826_1
    assert "ISO 8826-2" in SOURCE_ISO_8826_2
    assert "ISO 15:2017" in SOURCE_ISO_15


def test_the_element_symbol_always_fits_inside_the_ring_rectangle():
    """The declared fractions must never draw a symbol outside the envelope."""
    for name in DEEP_GROOVE:
        row = bearing(name)
        half_width = row["B"] / 2.0
        half_gap = (row["D"] - row["d"]) / 4.0
        assert BALL_SYMBOL_FRACTION * half_gap < half_width
        assert CROSS_FRACTION * half_width < half_width
        assert CROSS_FRACTION * half_gap < half_gap


def test_list_bearings_filters_by_series():
    assert len(DEEP_GROOVE) == 33
    assert len(list_bearings()) == 33
    assert BEARING_SERIES == ("600x", "620x", "630x")
    assert {row["designation"] for row in list_bearings("620x")} == {
        f"62{n:02d}" for n in range(11)
    }
    with pytest.raises(ValueError, match="620x"):
        list_bearings("622x")


def test_the_table_registers_itself():
    assert "ISO 15" in standards()
