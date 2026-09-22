# tests/test_pid_lines.py
"""Routing is geometry, so it is tested without a backend."""

from __future__ import annotations

import math

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


# ── Track A hardening (track E wave 0, Task 7): bulge flattening ─────────────


@pytest.mark.parametrize("bulge", [1.0, -1.0, 0.5, -0.3, 2.0, 0.2])
def test_flatten_bulges_matches_ezdxf_flattening(bulge):
    """Pinned against ezdxf's own arc flattening: same vertex count, same points."""
    from ezdxf.math import ConstructionArc, Vec2, bulge_to_arc

    from engineering.pid.lines import FLATTEN_SAGITTA, flatten_bulges

    a, b = (0.0, 0.0), (10.0, 0.0)
    mine = flatten_bulges([a, b], [bulge])
    centre, start_rad, end_rad, radius = bulge_to_arc(Vec2(a), Vec2(b), bulge)
    arc = ConstructionArc(
        center=centre,
        radius=radius,
        start_angle=math.degrees(start_rad),
        end_angle=math.degrees(end_rad),
    )
    reference = [(p.x, p.y) for p in arc.flattening(FLATTEN_SAGITTA)]
    assert len(mine) == len(reference)
    assert mine[0] == a and mine[-1] == b
    assert all(math.hypot(p[0] - centre.x, p[1] - centre.y) == pytest.approx(radius) for p in mine)
    assert mine[len(mine) // 2] == pytest.approx(reference[len(reference) // 2])


def test_crossings_test_the_exact_arc_so_a_flattened_vertex_hides_nothing():
    """(45,0)→(55,0) bulge -1 is the r=5 jump; its apex (50,5) is a vertex of
    the flattened chain (12 chords, even), so the chord chain drops a line at
    x=50 from both neighbours. The exact arc counts it once — as it does at
    x=50.3 — and keeps the junction rule everywhere it belongs."""
    from engineering.pid.lines import count_crossings, flatten_bulges, segment_arc_crossings

    jump, bulges = [(45.0, 0.0), (55.0, 0.0)], [-1.0, 0.0]
    flat = flatten_bulges(jump, bulges)
    assert (50.0, 5.0) in flat, "the apex is a flattened vertex — the case that was missed"
    assert count_crossings([(50, -20), (50, 20)], [flat]) == 0, "chords: the hit is a vertex"
    assert count_crossings([(50, -20), (50, 20)], [jump], bulges=[bulges]) == 1
    assert count_crossings([(50.3, -20), (50.3, 20)], [jump], bulges=[bulges]) == 1
    assert count_crossings([(50, 20), (50, 60)], [[(0, 0), (100, 0)]], bulges=[[-1.0, 0.0]]) == 1
    # Junctions stay junctions: the segment ending on the arc, a line through
    # the arc's own endpoint (the polyline's vertex), the empty chord side,
    # and a tangent along the apex that touches without crossing.
    assert segment_arc_crossings((50, 5), (50, 20), *jump, -1.0) == 0
    assert segment_arc_crossings((45, -5), (45, 5), *jump, -1.0) == 0
    assert segment_arc_crossings((50, -20), (50, -1), *jump, -1.0) == 0
    assert segment_arc_crossings((40, 5), (60, 5), *jump, -1.0) == 0
    assert segment_arc_crossings((40, 4.9), (60, 4.9), *jump, -1.0) == 2, "in and out again"
    assert segment_arc_crossings((50, -20), (50, 20), *jump, 1.0) == 1, "bulge +1 bows to -Y"
    assert segment_arc_crossings((50, -20), (50, 20), *jump, 0.0) == 1, "bulge 0 is the chord"
    # A short or missing bulge list is zero-padded: straight edges as before.
    assert count_crossings([(5, -5), (5, 5)], [[(0, 0), (10, 0), (10, 10)]], bulges=[[]]) == 1
    assert count_crossings([(5, -5), (5, 5)], [[(0, 0), (10, 0)]], bulges=None) == 1


@pytest.mark.parametrize("seed", range(4))
def test_segment_arc_crossings_match_ezdxf_arc_line_intersection(seed):
    """Pinned against ezdxf's own ``ConstructionArc.intersect_line`` (hits
    strictly inside the segment) over random segments and bulges of either
    sign, on both sides of a semicircle."""
    import random

    from ezdxf.math import ConstructionArc, ConstructionLine, Vec2, bulge_to_arc

    from engineering.pid.lines import segment_arc_crossings

    rng = random.Random(seed)
    checked = 0
    for _ in range(400):
        a = (rng.uniform(-50, 50), rng.uniform(-50, 50))
        b = (rng.uniform(-50, 50), rng.uniform(-50, 50))
        if math.dist(a, b) < 1.0:
            continue
        magnitude = rng.choice([0.2, 0.5, 1.0, 1.5, 2.5, rng.uniform(0.05, 3.0)])
        bulge = rng.choice([-1.0, 1.0]) * magnitude
        p1 = (rng.uniform(-80, 80), rng.uniform(-80, 80))
        p2 = (rng.uniform(-80, 80), rng.uniform(-80, 80))
        centre, s_rad, e_rad, radius = bulge_to_arc(Vec2(a), Vec2(b), bulge)
        arc = ConstructionArc(
            center=centre,
            radius=radius,
            start_angle=math.degrees(s_rad),
            end_angle=math.degrees(e_rad),
        )
        d = Vec2(p2) - Vec2(p1)
        hits = arc.intersect_line(ConstructionLine(Vec2(p1), Vec2(p2)), abs_tol=1e-9)
        expected = sum(1 for h in hits if 1e-6 < (h - Vec2(p1)).dot(d) / d.dot(d) < 1 - 1e-6)
        assert segment_arc_crossings(p1, p2, a, b, bulge) == expected, (p1, p2, a, b, bulge)
        checked += 1
    assert checked > 350


def test_flatten_bulges_passes_straight_edges_through_and_pads_short_bulge_lists():
    from engineering.pid.lines import flatten_bulges

    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    assert flatten_bulges(pts, []) == pts
    assert flatten_bulges(pts, [0.0, 0.0, 0.0]) == pts
    flat = flatten_bulges(pts, [0.0, 1.0])
    assert flat[0] == (0.0, 0.0) and flat[1] == (10.0, 0.0) and flat[-1] == (10.0, 10.0)
    assert len(flat) > 3, "the second edge became a semicircle of chords"
    assert flatten_bulges([(1.0, 1.0)], [1.0]) == [(1.0, 1.0)]
