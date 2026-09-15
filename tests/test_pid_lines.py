# tests/test_pid_lines.py
"""Routing is geometry, so it is tested without a backend."""

from __future__ import annotations

import pytest

from engineering.pid.lines import (
    DEFAULT_NUMBER_FORMAT,
    LINE_CLASSES,
    count_crossings,
    format_line_number,
    label_placement,
    marker_positions,
    route,
    snap_axis,
)


def test_line_classes_map_to_layers_and_markers():
    assert set(LINE_CLASSES) == {
        "process_major",
        "process_minor",
        "utility",
        "pneumatic",
        "electric",
        "hydraulic",
        "capillary",
        "data",
    }
    assert LINE_CLASSES["process_major"].layer == "PROCESS-PIPING-MAIN"
    assert (
        LINE_CLASSES["electric"].layer == "INSTRUMENT-LINE-SIGNAL"
        and LINE_CLASSES["electric"].linetype is None
    )
    assert (
        LINE_CLASSES["pneumatic"].linetype == "Continuous"
        and LINE_CLASSES["pneumatic"].marker == "mark_pneumatic"
    )
    assert LINE_CLASSES["process_major"].arrow is True and LINE_CLASSES["pneumatic"].arrow is False
    assert LINE_CLASSES["utility"].kind == "process" and LINE_CLASSES["data"].kind == "signal"


def test_snap_axis_picks_the_dominant_component():
    assert snap_axis(10, 1) == 0.0 and snap_axis(-10, 1) == 180.0
    assert snap_axis(1, 10) == 90.0 and snap_axis(1, -10) == 270.0


def test_straight_when_aligned_and_facing():
    assert route((0, 0), 0.0, (50, 0), 180.0) == [(0.0, 0.0), (50.0, 0.0)]


def test_l_route_when_perpendicular():
    path = route((0, 0), 0.0, (50, 30), 270.0)
    assert path == [(0.0, 0.0), (50.0, 0.0), (50.0, 30.0)]


def test_z_route_when_parallel_and_offset():
    path = route((0, 0), 0.0, (60, 20), 180.0, stub=5.0)
    assert path == [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (60.0, 20.0)]


def test_ports_facing_away_on_one_axis_need_waypoints():
    with pytest.raises(ValueError, match="waypoints"):
        route((0, 0), 180.0, (40, 0), 0.0, stub=5.0)
    path = route((0, 0), 180.0, (40, 0), 0.0, mode=[(-5, 0), (-5, -10), (45, -10), (45, 0)])
    assert len(path) == 6


def test_stub_is_honoured_or_refused():
    with pytest.raises(ValueError, match="stub"):
        route((0, 0), 0.0, (3, 8), 180.0, stub=5.0)
    assert route((0, 0), 0.0, (3, 0), 180.0, stub=5.0) == [(0.0, 0.0), (3.0, 0.0)], (
        "a straight run is exempt"
    )


def test_radial_ends_take_the_axis_towards_the_other_end():
    path = route((0, 0), None, (0, 40), None)
    assert path == [(0.0, 0.0), (0.0, 40.0)]


def test_direct_and_waypoints():
    assert route((0, 0), 0.0, (10, 7), 90.0, mode="direct") == [(0.0, 0.0), (10.0, 7.0)]
    assert route((0, 0), 0.0, (10, 7), 90.0, mode=[(10, 0)]) == [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 7.0),
    ]
    with pytest.raises(ValueError, match="repeat"):
        route((0, 0), 0.0, (10, 7), 90.0, mode=[(0, 0)])
    with pytest.raises(ValueError, match="zero"):
        route((0, 0), 0.0, (0, 0), 90.0)


def test_label_sits_left_of_the_longest_segment_and_never_upside_down():
    x, y, rot, index = label_placement([(0, 0), (10, 0), (10, 40), (0, 40)])
    assert index == 1 and rot == 90.0
    assert (x, y) == pytest.approx((8.5, 20.0))
    x, y, rot, index = label_placement([(50, 0), (0, 0)])
    assert rot == 0.0 and y == pytest.approx(1.5)


def test_markers_only_on_segments_long_enough():
    marks = marker_positions([(0, 0), (10, 0), (10, 40)], min_len=15.0)
    assert marks == [(10.0, 20.0, 90.0)]
    assert marker_positions([(0, 0), (30, 0)]) == [(15.0, 0.0, 0.0)]


def test_crossings_count_proper_intersections_only():
    others = [[(5, -5), (5, 5)], [(20, 0), (20, 10)], [(0, 3), (8, 3)]]
    assert count_crossings([(0, 0), (10, 0)], others) == 1
    assert count_crossings([(0, 0), (20, 0)], [[(20, 0), (20, 10)]]) == 0, (
        "touching an endpoint is a junction"
    )


def test_line_number_format_drops_empty_fields():
    assert DEFAULT_NUMBER_FORMAT == "{size}-{service}-{seq}-{spec}-{insulation}"
    assert (
        format_line_number(
            DEFAULT_NUMBER_FORMAT, size="100", service="P", seq=1001, spec="CS1", insulation="IH"
        )
        == "100-P-1001-CS1-IH"
    )
    assert (
        format_line_number(DEFAULT_NUMBER_FORMAT, size="100", service="P", seq=1001) == "100-P-1001"
    )
    assert format_line_number("L-{seq}", seq=7) == "L-7"
    with pytest.raises(ValueError, match="unknown"):
        format_line_number("{bogus}", seq=1)


def test_line_number_format_fills_specs_and_refuses_what_it_cannot_fill():
    # A format spec is honoured, and literal text around a field survives with it.
    assert format_line_number("P-{seq:04d}", seq=7) == "P-0007"
    assert format_line_number('{size}"-{service}-{seq}', size=4, service="P", seq=1) == '4"-P-1'
    # A token whose field is empty drops out with its literal decoration.
    assert format_line_number('{size}"-{service}-{seq}', service="P", seq=1) == "P-1"
    # Nothing with a brace in it is ever copied verbatim into the number.
    with pytest.raises(ValueError, match="unknown"):
        format_line_number("{bogus}x-{seq}", seq=7)
    with pytest.raises(ValueError, match="one field"):
        format_line_number("{area}{unit}-{seq}", area="1", unit="2", seq=7)
    with pytest.raises(ValueError, match="conversion"):
        format_line_number("{seq!r}", seq=7)
    with pytest.raises(ValueError, match="positional"):
        format_line_number("{}-{seq}", seq=7)
    with pytest.raises(ValueError, match=r"'\{seq'"):
        format_line_number("{seq", seq=7)
    with pytest.raises(ValueError, match=r"'\{seq:04d\}'"):
        format_line_number("{seq:04d}", seq="abc")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_route_refuses_non_finite_coordinates(bad):
    with pytest.raises(ValueError, match=r"end\.x"):
        route((0, 0), 0.0, (bad, 0), 180.0)
    with pytest.raises(ValueError, match=r"end\.x"):
        route((0, 0), 0.0, (bad, 0), 180.0, mode="direct")
    with pytest.raises(ValueError, match=r"start\.y"):
        route((0, bad), 0.0, (50, 0), 180.0)
    with pytest.raises(ValueError, match=r"waypoints\[1\]\.x"):
        route((0, 0), 0.0, (10, 7), 90.0, mode=[(10, 0), (bad, 0)])
    with pytest.raises(ValueError, match="stub"):
        route((0, 0), 0.0, (50, 0), 180.0, stub=bad)


def test_route_refuses_non_axis_port_directions_with_a_value_error():
    with pytest.raises(ValueError, match=r"start_dir 45\.0 is not an axis direction"):
        route((0, 0), 45.0, (50, 50), 225.0)
    with pytest.raises(ValueError, match=r"end_dir 90\.5 is not an axis direction"):
        route((0, 0), 0.0, (50, 30), 90.5)
    with pytest.raises(ValueError, match="start_dir nan"):
        route((0, 0), float("nan"), (50, 0), 180.0)
    # Equivalent angles and engine float drift still resolve to the axis.
    assert route((0, 0), -360.0, (50, 0), 540.0) == [(0.0, 0.0), (50.0, 0.0)]
    assert route((0, 0), 1e-9, (50, 0), 180.0 - 1e-9) == [(0.0, 0.0), (50.0, 0.0)]
