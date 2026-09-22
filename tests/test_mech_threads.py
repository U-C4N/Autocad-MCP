"""ISO 261 pitches, ISO 724 basic diameters and the ISO 6410 representation.

Provenance: every asserted pitch is a value the module transcribed from
ISO 261:2022; every asserted basic diameter is a published ISO 724 value that
the ISO 68-1 formula has to reproduce exactly. The coverage refusals are
asserted at both ends, because a size outside the transcribed table must be
refused by name and never interpolated.
"""

from __future__ import annotations

import math

import pytest

from engineering.mech.primitives import Arc, Line
from engineering.mech.standards import lookup
from engineering.mech.standards.threads import (
    COARSE,
    DIAMETERS,
    FINE,
    MINOR_RATIO,
    basic_minor_diameter,
    basic_pitch_diameter,
    parse_designation,
    pitch_for,
    thread_axial_prims,
    thread_end_prims,
)


@pytest.mark.parametrize(
    ("d", "pitch"),
    [
        (1.6, 0.35),
        (2.0, 0.40),
        (3.0, 0.50),
        (5.0, 0.80),
        (6.0, 1.00),
        (8.0, 1.25),
        (10.0, 1.50),
        (12.0, 1.75),
        (14.0, 2.00),
        (16.0, 2.00),
        (20.0, 2.50),
        (24.0, 3.00),
        (30.0, 3.50),
        (33.0, 3.50),
        (36.0, 4.00),
        (42.0, 4.50),
        (48.0, 5.00),
        (56.0, 5.50),
        (64.0, 6.00),
    ],
)
def test_iso261_coarse_pitches_are_the_transcribed_values(d, pitch):
    assert COARSE[d] == pitch


def test_the_transcribed_diameters_are_first_choice_plus_six_second_choice():
    assert DIAMETERS[0] == 1.6
    assert DIAMETERS[-1] == 64.0
    assert {14.0, 18.0, 22.0, 27.0, 33.0, 39.0} <= set(DIAMETERS)
    # third-choice diameters are deliberately absent
    assert 7.0 not in DIAMETERS
    assert 9.0 not in DIAMETERS
    assert 3.5 not in DIAMETERS


@pytest.mark.parametrize(
    ("d", "pitches"),
    [
        (8.0, (1.0, 0.75)),
        (10.0, (1.25, 1.0, 0.75)),
        (12.0, (1.5, 1.25, 1.0)),
        (16.0, (1.5, 1.0)),
        (20.0, (2.0, 1.5, 1.0)),
        (24.0, (2.0, 1.5, 1.0)),
        (36.0, (3.0, 2.0, 1.5)),
        (64.0, (4.0, 3.0, 2.0, 1.5)),
    ],
)
def test_iso261_fine_pitch_series(d, pitches):
    assert FINE[d] == pitches


def test_a_bare_designation_resolves_to_the_coarse_pitch():
    assert parse_designation("M20") == {
        "designation": "M20",
        "d": 20.0,
        "pitch": 2.5,
        "series": "coarse",
        "hand": "right",
    }


def test_a_fine_designation_resolves_and_keeps_its_hand():
    parsed = parse_designation("M20x1.5-LH")
    assert parsed["d"] == 20.0
    assert parsed["pitch"] == 1.5
    assert parsed["series"] == "fine"
    assert parsed["hand"] == "left"


def test_a_diameter_outside_the_transcribed_table_is_refused_by_name():
    with pytest.raises(ValueError) as excinfo:
        parse_designation("M7")
    message = str(excinfo.value)
    assert "ISO 261" in message
    assert "M7" in message
    with pytest.raises(ValueError, match="ISO 261"):
        parse_designation("M70")


def test_a_fine_pitch_that_is_not_in_the_series_is_refused_and_lists_the_series():
    with pytest.raises(ValueError) as excinfo:
        parse_designation("M20x1.25")
    message = str(excinfo.value)
    assert "M20" in message
    assert "2.0" in message and "1.5" in message and "1.0" in message


def test_a_malformed_designation_is_refused():
    with pytest.raises(ValueError, match="designation"):
        parse_designation("20mm")


def test_pitch_for_matches_parse_designation():
    assert pitch_for(20.0) == 2.5
    assert pitch_for(20.0, 1.5) == 1.5
    with pytest.raises(ValueError):
        pitch_for(20.0, 1.25)


@pytest.mark.parametrize(
    ("d", "pitch", "d1", "d2"),
    [
        (20.0, 2.50, 17.294, 18.376),
        (10.0, 1.50, 8.376, 9.026),
        (8.0, 1.25, 6.647, 7.188),
        (12.0, 1.75, 10.106, 10.863),
        (6.0, 1.00, 4.917, 5.350),
    ],
)
def test_iso68_1_reproduces_the_published_iso724_basic_diameters(d, pitch, d1, d2):
    assert basic_minor_diameter(d, pitch) == pytest.approx(d1, abs=5e-4)
    assert basic_pitch_diameter(d, pitch) == pytest.approx(d2, abs=5e-4)


def test_the_registry_carries_the_pitch_table_and_refuses_outside_its_coverage():
    assert lookup("ISO 261", 20.0)["pitch"] == 2.5
    with pytest.raises(ValueError, match="ISO 261"):
        lookup("ISO 261", 70.0)


def test_iso6410_axial_representation_of_an_external_thread():
    prims = thread_axial_prims(x0=10.0, length=30.0, d=20.0)
    lines = [p for p in prims if isinstance(p, Line)]
    assert len(prims) == 3 and len(lines) == 3
    minor = MINOR_RATIO * 20.0 / 2.0
    assert {round(line.p1[1], 9) for line in lines[:2]} == {round(minor, 9), round(-minor, 9)}
    assert lines[0].p1 == (10.0, minor) and lines[0].p2 == (40.0, minor)
    assert lines[1].p1 == (10.0, -minor) and lines[1].p2 == (40.0, -minor)
    # the thread-length limit line runs the full major diameter
    assert lines[2].p1 == (40.0, -10.0) and lines[2].p2 == (40.0, 10.0)
    assert {line.role for line in lines} == {"visible"}


def test_iso6410_axial_representation_of_an_internal_thread_is_hidden():
    prims = thread_axial_prims(x0=0.0, length=20.0, d=12.0, internal=True)
    assert {p.role for p in prims} == {"hidden"}
    assert prims[0].p1 == (0.0, 6.0) and prims[0].p2 == (20.0, 6.0)


def test_iso6410_end_view_is_three_quarters_of_a_circle():
    (arc,) = thread_end_prims(center=(0.0, 0.0), d=20.0)
    assert isinstance(arc, Arc)
    assert arc.radius == pytest.approx(8.0)
    assert (arc.end_deg - arc.start_deg) % 360.0 == pytest.approx(270.0)
    assert arc.start_deg == pytest.approx(90.0)


def test_the_end_view_gap_can_be_moved_and_stays_a_quarter():
    (arc,) = thread_end_prims(center=(0.0, 0.0), d=20.0, gap_center_deg=135.0)
    assert arc.start_deg == pytest.approx(180.0)
    assert (arc.end_deg - arc.start_deg) % 360.0 == pytest.approx(270.0)
    assert math.isclose(arc.radius, 8.0)
