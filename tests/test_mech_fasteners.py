"""Provenance and coverage of the ISO fastener tables (the table rule, spec 5).

Every value asserted here is a named row of a named table, and the converse holds
too: the ``ISO*_ROWS`` literals below carry *every* dimension of *every* shipped
size, and a completeness test asserts each table's key set equals its literal's.
A row nobody pinned is exactly where a wrong across-flats would hide. A row that
could not be verified against the standard is not in the module at all, so the
coverage refusals below are as load-bearing as the values.
"""

from __future__ import annotations

import math

import pytest

from engineering.mech.standards import standards
from engineering.mech.standards.fasteners import (
    COARSE_PITCH,
    HEX_HEAD,
    HEX_NUT,
    SOCKET_HEAD,
    SOURCE_ISO_4014,
    WASHER,
    across_corners,
    hex_head,
    hex_nut,
    nominal_diameter,
    sizes,
    socket_head,
    thread_length,
    thread_pitch,
    washer,
)

#: ISO 4014:2011 / ISO 4017:2014 Table 1 - every transcribed size, (s, k).
ISO4014_ROWS: dict[str, tuple[float, float]] = {
    "M5": (8.0, 3.5),
    "M6": (10.0, 4.0),
    "M8": (13.0, 5.3),
    "M10": (16.0, 6.4),
    "M12": (18.0, 7.5),
    "M16": (24.0, 10.0),
    "M20": (30.0, 12.5),
    "M24": (36.0, 15.0),
    "M30": (46.0, 18.7),
    "M36": (55.0, 22.5),
}

#: ISO 4032:2012 Table 1 - every transcribed size, (s, m).
ISO4032_ROWS: dict[str, tuple[float, float]] = {
    "M5": (8.0, 4.7),
    "M6": (10.0, 5.2),
    "M8": (13.0, 6.8),
    "M10": (16.0, 8.4),
    "M12": (18.0, 10.8),
    "M16": (24.0, 14.8),
    "M20": (30.0, 18.0),
    "M24": (36.0, 21.5),
    "M30": (46.0, 25.6),
    "M36": (55.0, 31.0),
}

#: ISO 7089:2000 Table 1 - every transcribed size, (d1, d2, h).
ISO7089_ROWS: dict[str, tuple[float, float, float]] = {
    "M5": (5.3, 10.0, 1.0),
    "M6": (6.4, 12.0, 1.6),
    "M8": (8.4, 16.0, 1.6),
    "M10": (10.5, 20.0, 2.0),
    "M12": (13.0, 24.0, 2.5),
    "M16": (17.0, 30.0, 3.0),
    "M20": (21.0, 37.0, 3.0),
    "M24": (25.0, 44.0, 4.0),
    "M30": (31.0, 56.0, 4.0),
    "M36": (37.0, 66.0, 5.0),
}

#: ISO 4762:2004 Table 1 - every transcribed size, (dk, k, s, b).
ISO4762_ROWS: dict[str, tuple[float, float, float, float]] = {
    "M3": (5.5, 3.0, 2.5, 18.0),
    "M4": (7.0, 4.0, 3.0, 20.0),
    "M5": (8.5, 5.0, 4.0, 22.0),
    "M6": (10.0, 6.0, 5.0, 24.0),
    "M8": (13.0, 8.0, 6.0, 28.0),
    "M10": (16.0, 10.0, 8.0, 32.0),
    "M12": (18.0, 12.0, 10.0, 36.0),
    "M16": (24.0, 16.0, 14.0, 44.0),
    "M20": (30.0, 20.0, 17.0, 52.0),
    "M24": (36.0, 24.0, 19.0, 60.0),
}


@pytest.mark.parametrize(("size", "expected"), sorted(ISO4014_ROWS.items()))
def test_iso4014_named_rows_match_the_standard(size, expected):
    """ISO 4014:2011 Table 1 - width across flats s and head height k."""
    row = hex_head(size)
    assert (row["s"], row["k"]) == expected


def test_every_iso4014_row_is_pinned_by_a_literal():
    """No shipped size may escape the table above - that is where a wrong row hides."""
    assert set(HEX_HEAD) == set(ISO4014_ROWS)
    assert sizes("ISO 4014") == tuple(HEX_HEAD)


def test_iso4017_shares_the_iso4014_head():
    """ISO 4017 screws and ISO 4014 bolts have the same head at every size here."""
    assert sizes("ISO 4017") == sizes("ISO 4014")


@pytest.mark.parametrize(("size", "expected"), sorted(ISO4032_ROWS.items()))
def test_iso4032_named_rows_match_the_standard(size, expected):
    """ISO 4032:2012 Table 1 - width across flats s and nut height m (max)."""
    row = hex_nut(size)
    assert (row["s"], row["m"]) == expected


def test_every_iso4032_row_is_pinned_and_shares_the_bolt_across_flats():
    assert set(HEX_NUT) == set(ISO4032_ROWS)
    for size in HEX_NUT:
        assert hex_nut(size)["s"] == hex_head(size)["s"]


@pytest.mark.parametrize(("size", "expected"), sorted(ISO7089_ROWS.items()))
def test_iso7089_named_rows_match_the_standard(size, expected):
    """ISO 7089:2000 Table 1 - d1, d2, h (nominal), normal series, grade A."""
    row = washer(size)
    assert (row["d1"], row["d2"], row["h"]) == expected


def test_every_iso7089_row_is_pinned_by_a_literal():
    assert set(WASHER) == set(ISO7089_ROWS)


@pytest.mark.parametrize(("size", "expected"), sorted(ISO4762_ROWS.items()))
def test_iso4762_named_rows_match_the_standard(size, expected):
    """ISO 4762:2004 Table 1 - head dk (max), head height k (max), socket s, thread length b."""
    row = socket_head(size)
    assert (row["dk"], row["k"], row["s"], row["b"]) == expected


def test_every_iso4762_row_is_pinned_and_its_head_height_equals_the_thread_diameter():
    assert set(SOCKET_HEAD) == set(ISO4762_ROWS)
    # ISO 4762 head height equals the nominal thread diameter at every size here.
    for size in SOCKET_HEAD:
        assert socket_head(size)["k"] == nominal_diameter(size)


def test_iso261_coarse_pitch_column():
    assert COARSE_PITCH == {
        "M3": 0.5,
        "M4": 0.7,
        "M5": 0.8,
        "M6": 1.0,
        "M8": 1.25,
        "M10": 1.5,
        "M12": 1.75,
        "M16": 2.0,
        "M20": 2.5,
        "M24": 3.0,
        "M30": 3.5,
        "M36": 4.0,
    }
    assert thread_pitch("M12") == 1.75
    assert nominal_diameter("M12") == 12.0


def test_coverage_is_refused_at_both_ends_by_name():
    with pytest.raises(ValueError, match="ISO 4014"):
        hex_head("M3")
    with pytest.raises(ValueError, match="ISO 4014"):
        hex_head("M42")
    with pytest.raises(ValueError, match="ISO 4032"):
        hex_nut("M42")
    with pytest.raises(ValueError, match="ISO 7089"):
        washer("M3")
    with pytest.raises(ValueError, match="ISO 4762"):
        socket_head("M2")
    with pytest.raises(ValueError, match="ISO 4762"):
        socket_head("M30")


def test_second_choice_sizes_are_absent_not_interpolated():
    for absent in ("M14", "M18", "M22", "M27", "M33"):
        with pytest.raises(ValueError):
            hex_head(absent)
        with pytest.raises(ValueError):
            hex_nut(absent)


def test_thread_length_follows_the_standards_own_b_rule():
    assert thread_length("M12", 60.0) == pytest.approx(30.0)  # 2d + 6,  l <= 125
    assert thread_length("M12", 150.0) == pytest.approx(36.0)  # 2d + 12, 125 < l <= 200
    assert thread_length("M12", 260.0) == pytest.approx(49.0)  # 2d + 25, l > 200
    assert thread_length("M12", 25.0) == pytest.approx(25.0)  # never longer than the bolt
    assert thread_length("M12", 60.0, "ISO 4017") == pytest.approx(60.0)
    assert thread_length("M8", 30.0, "ISO 4762") == pytest.approx(28.0)
    assert thread_length("M8", 20.0, "ISO 4762") == pytest.approx(20.0)
    with pytest.raises(ValueError):
        thread_length("M12", 0.0)
    with pytest.raises(ValueError):
        thread_length("M12", float("inf"))
    with pytest.raises(ValueError, match="ISO 4014"):
        thread_length("M12", 60.0, "DIN 931")


def test_across_corners_is_derived_geometry_not_a_transcribed_column():
    assert across_corners(18.0) == pytest.approx(2.0 * 18.0 / math.sqrt(3.0))
    assert across_corners(18.0) == pytest.approx(20.7846, abs=1e-4)
    with pytest.raises(ValueError):
        across_corners(0.0)


def test_every_table_registers_itself_with_the_shared_registry():
    for name in ("ISO 4014", "ISO 4017", "ISO 4032", "ISO 7089", "ISO 4762"):
        assert name in standards()
    assert "ISO 4014:2011" in SOURCE_ISO_4014


def test_sizes_refuses_a_standard_this_module_does_not_carry():
    assert sizes("ISO 4014")[0] == "M5"
    with pytest.raises(ValueError, match="ISO 4014"):
        sizes("ISO 15")
