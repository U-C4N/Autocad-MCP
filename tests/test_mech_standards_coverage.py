"""The four tables this build does NOT transcribe, and how they refuse.

The track's table rule is absolute: a row that cannot be sourced against the
standard is not shipped, because a wrong relief depth or a wrong groove
diameter is a wrong workshop drawing, while a narrow table is merely narrow.
DIN 509, DIN 471/472, DIN 332-1 and ISO 3601-2 therefore ship with their
structure, their SOURCE and an empty row set, and every lookup is refused by
name and says what to transcribe.

These tests are the gate that keeps that honest in both directions: the
refusal must name the standard and the transcription route, and the shape gate
must fail the moment somebody adds a row that is missing a column the drawing
code reads.
"""

from __future__ import annotations

import pytest

from engineering.mech.standards import standards
from engineering.mech.standards.centres import (
    CENTRE_ROW_KEYS,
    ROWS_A,
    ROWS_B,
    centre_hole_dims,
)
from engineering.mech.standards.centres import (
    SOURCE as SOURCE_CENTRES,
)
from engineering.mech.standards.orings import (
    ORING_ROW_KEYS,
    oring_groove_dims,
)
from engineering.mech.standards.orings import (
    ROWS as ROWS_ORING,
)
from engineering.mech.standards.orings import (
    SOURCE as SOURCE_ORING,
)
from engineering.mech.standards.rings import (
    RING_ROW_KEYS,
    ROWS_471,
    ROWS_472,
    ring_groove_dims,
)
from engineering.mech.standards.rings import (
    SOURCE as SOURCE_RINGS,
)
from engineering.mech.standards.undercuts import (
    ROWS_E,
    ROWS_F,
    UNDERCUT_ROW_KEYS,
    undercut_profile,
)
from engineering.mech.standards.undercuts import (
    SOURCE as SOURCE_UNDERCUTS,
)


def test_every_table_is_registered_so_a_later_transcription_is_a_data_edit():
    registered = standards()
    for name in (
        "DIN 509-E",
        "DIN 509-F",
        "DIN 471",
        "DIN 472",
        "DIN 332-A",
        "DIN 332-B",
        "ISO 3601-2",
    ):
        assert name in registered


@pytest.mark.parametrize(
    ("source", "standard"),
    [
        (SOURCE_UNDERCUTS, "DIN 509"),
        (SOURCE_RINGS, "DIN 471"),
        (SOURCE_CENTRES, "DIN 332"),
        (SOURCE_ORING, "ISO 3601-2"),
    ],
)
def test_every_source_names_its_standard_and_edition(source, standard):
    assert standard in source
    assert any(ch.isdigit() for ch in source.split(":")[-1])


def test_no_row_is_shipped_in_this_build():
    assert ROWS_E == {} and ROWS_F == {}
    assert ROWS_471 == {} and ROWS_472 == {}
    assert ROWS_A == {} and ROWS_B == {}
    assert ROWS_ORING == {}


def test_the_undercut_lookup_is_refused_by_name_and_says_what_to_transcribe():
    with pytest.raises(ValueError) as excinfo:
        undercut_profile("E", 40.0)
    message = str(excinfo.value)
    assert "DIN 509" in message
    assert "form E" in message
    assert "40" in message
    assert "undercuts.py" in message


def test_an_unknown_undercut_form_is_refused_before_the_coverage_refusal():
    with pytest.raises(ValueError, match="forms E and F"):
        undercut_profile("G", 40.0)


def test_the_ring_groove_lookup_is_refused_by_name_for_shaft_and_bore():
    for standard in ("DIN 471", "DIN 472"):
        with pytest.raises(ValueError) as excinfo:
            ring_groove_dims(standard, 30.0)
        assert standard in str(excinfo.value)
        assert "rings.py" in str(excinfo.value)
    with pytest.raises(ValueError, match="DIN 471 or DIN 472"):
        ring_groove_dims("DIN 6799", 30.0)


def test_the_centre_hole_lookup_is_refused_by_name_and_form_r_is_refused_outright():
    with pytest.raises(ValueError) as excinfo:
        centre_hole_dims("A", 2.5)
    assert "DIN 332" in str(excinfo.value)
    assert "centres.py" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        centre_hole_dims("R", 2.5)
    assert "form R" in str(excinfo.value)


def test_the_oring_lookup_is_refused_by_name():
    with pytest.raises(ValueError) as excinfo:
        oring_groove_dims(3.53)
    assert "ISO 3601-2" in str(excinfo.value)
    assert "orings.py" in str(excinfo.value)


def test_the_row_key_contracts_are_the_ones_the_drawing_code_reads():
    assert UNDERCUT_ROW_KEYS == ("profile",)
    assert RING_ROW_KEYS == ("m", "d2")
    assert CENTRE_ROW_KEYS == ("d1", "d2", "d3", "t")
    assert ORING_ROW_KEYS == ("b", "h")


@pytest.mark.parametrize(
    ("rows", "keys"),
    [
        (ROWS_E, ("profile",)),
        (ROWS_F, ("profile",)),
        (ROWS_471, ("m", "d2")),
        (ROWS_472, ("m", "d2")),
        (ROWS_A, ("d1", "d2", "t")),
        (ROWS_B, ("d1", "d2", "d3", "t")),
        (ROWS_ORING, ("b", "h")),
    ],
)
def test_any_row_that_is_ever_added_must_carry_every_key_the_drawing_code_reads(rows, keys):
    """Empty today; this gate fires the moment a row lands without a column."""
    for size, row in rows.items():
        missing = [key for key in keys if key not in row]
        assert not missing, f"row {size} is missing {missing}"
