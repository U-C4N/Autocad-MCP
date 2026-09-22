"""Provenance and coverage of the ISO fastener tables (the table rule, spec 5).

Every value asserted here is a named row of a named table. A row that could not
be verified against the standard is not in the module at all, so the coverage
refusals below are as load-bearing as the values.
"""

from __future__ import annotations

import math

import pytest

from engineering.mech.standards import standards
from engineering.mech.standards.fasteners import (
    COARSE_PITCH,
    HEX_NUT,
    SOCKET_HEAD,
    SOURCE_ISO_4014,
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


def test_iso4014_named_rows_match_the_standard():
    """ISO 4014:2011 Table 1 - width across flats s and head height k."""
    assert (hex_head("M6")["s"], hex_head("M6")["k"]) == (10.0, 4.0)
    assert (hex_head("M8")["s"], hex_head("M8")["k"]) == (13.0, 5.3)
    assert (hex_head("M10")["s"], hex_head("M10")["k"]) == (16.0, 6.4)
    assert (hex_head("M12")["s"], hex_head("M12")["k"]) == (18.0, 7.5)
    assert (hex_head("M16")["s"], hex_head("M16")["k"]) == (24.0, 10.0)
    assert (hex_head("M20")["s"], hex_head("M20")["k"]) == (30.0, 12.5)
    assert (hex_head("M24")["s"], hex_head("M24")["k"]) == (36.0, 15.0)
    assert (hex_head("M30")["s"], hex_head("M30")["k"]) == (46.0, 18.7)
    assert (hex_head("M36")["s"], hex_head("M36")["k"]) == (55.0, 22.5)


def test_iso4017_shares_the_iso4014_head():
    """ISO 4017 screws and ISO 4014 bolts have the same head at every size here."""
    assert sizes("ISO 4017") == sizes("ISO 4014")


def test_iso4032_named_rows_match_the_standard():
    """ISO 4032:2012 Table 1 - nut height m (max)."""
    assert hex_nut("M6")["m"] == 5.2
    assert hex_nut("M8")["m"] == 6.8
    assert hex_nut("M10")["m"] == 8.4
    assert hex_nut("M12")["m"] == 10.8
    assert hex_nut("M16")["m"] == 14.8
    assert hex_nut("M20")["m"] == 18.0
    assert hex_nut("M24")["m"] == 21.5
    assert hex_nut("M30")["m"] == 25.6
    assert hex_nut("M36")["m"] == 31.0
    for size in HEX_NUT:
        assert hex_nut(size)["s"] == hex_head(size)["s"]


def test_iso7089_named_rows_match_the_standard():
    """ISO 7089:2000 Table 1 - d1, d2, h (nominal), normal series, grade A."""
    assert (washer("M6")["d1"], washer("M6")["d2"], washer("M6")["h"]) == (6.4, 12.0, 1.6)
    assert (washer("M8")["d1"], washer("M8")["d2"], washer("M8")["h"]) == (8.4, 16.0, 1.6)
    assert (washer("M10")["d1"], washer("M10")["d2"], washer("M10")["h"]) == (10.5, 20.0, 2.0)
    assert (washer("M12")["d1"], washer("M12")["d2"], washer("M12")["h"]) == (13.0, 24.0, 2.5)
    assert (washer("M16")["d1"], washer("M16")["d2"], washer("M16")["h"]) == (17.0, 30.0, 3.0)
    assert (washer("M20")["d1"], washer("M20")["d2"], washer("M20")["h"]) == (21.0, 37.0, 3.0)
    assert (washer("M24")["d1"], washer("M24")["d2"], washer("M24")["h"]) == (25.0, 44.0, 4.0)
    assert (washer("M30")["d1"], washer("M30")["d2"], washer("M30")["h"]) == (31.0, 56.0, 4.0)
    assert (washer("M36")["d1"], washer("M36")["d2"], washer("M36")["h"]) == (37.0, 66.0, 5.0)


def test_iso4762_named_rows_match_the_standard():
    """ISO 4762:2004 Table 1 - head dk (max), socket s, thread length b."""
    assert (
        socket_head("M3")["dk"],
        socket_head("M3")["s"],
        socket_head("M3")["b"],
    ) == (5.5, 2.5, 18.0)
    assert (
        socket_head("M5")["dk"],
        socket_head("M5")["s"],
        socket_head("M5")["b"],
    ) == (8.5, 4.0, 22.0)
    assert (
        socket_head("M8")["dk"],
        socket_head("M8")["s"],
        socket_head("M8")["b"],
    ) == (13.0, 6.0, 28.0)
    assert (
        socket_head("M12")["dk"],
        socket_head("M12")["s"],
        socket_head("M12")["b"],
    ) == (18.0, 10.0, 36.0)
    assert (
        socket_head("M20")["dk"],
        socket_head("M20")["s"],
        socket_head("M20")["b"],
    ) == (30.0, 17.0, 52.0)
    assert (
        socket_head("M24")["dk"],
        socket_head("M24")["s"],
        socket_head("M24")["b"],
    ) == (36.0, 19.0, 60.0)
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
