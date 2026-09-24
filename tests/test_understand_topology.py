"""The topology engine: ends, joins, near misses, crossings - by hand-computed geometry.

Every record is built by hand, so every coordinate in an assertion is the
answer. The network layer is called PIPE here; which layers are network layers
by default is ``network_layers``' job and is tested on the synthetic plant.
"""

from __future__ import annotations

import pytest

from engineering.understand.snapshot import EntityRecord, Snapshot
from engineering.understand.topology import (
    Piece,
    cap_findings,
    entity_pieces,
    network_layers,
    topology_findings,
)
from engineering.understand.vocab import classify_layer
from tests.fixtures.plant_pair import build_plant_pair

L = "PIPE"


def rec(handle, type_, layer=L, points=(), **kw):
    space = kw.pop("space", "Model")
    return EntityRecord(
        handle=handle, type=type_, layer=layer, space=space, points=tuple(points), **kw
    )


def line(handle, x1, y1, x2, y2, layer=L, space="Model"):
    return rec(handle, "LINE", layer, ((x1, y1), (x2, y2)), space=space)


def snap(*records):
    return Snapshot(
        source="test",
        insunits=4,
        extmin=None,
        extmax=None,
        layers={r.layer: {"color": 7, "linetype": "Continuous"} for r in records},
        layouts=("Layout1",),
        records=tuple(records),
    )


def run(*records, gap=5.0, layers=(L,)):
    return topology_findings(snap(*records), layers=list(layers), gap=gap)


def ends(rows):
    return {(row["handle"], row["end"]) for row in rows}


# -- pieces ----------------------------------------------------------------------------


def test_a_bulge_of_one_is_the_semicircle_below_its_chord():
    # bulge +1 from (0,0) to (200,0): counter-clockwise, centre (100,0), radius 100,
    # through (100,-100)
    pieces, entity_ends = entity_pieces(
        rec("PL", "LWPOLYLINE", L, ((0.0, 0.0), (200.0, 0.0)), bulges=(1.0, 0.0))
    )
    (arc,) = pieces
    assert arc.centre == (pytest.approx(100.0), pytest.approx(0.0, abs=1e-9))
    assert arc.radius == pytest.approx(100.0)
    foot = arc.nearest((100.0, -150.0))
    assert foot == (pytest.approx(100.0), pytest.approx(-100.0))
    assert entity_ends == ((0.0, 0.0), (200.0, 0.0))


def test_a_closed_polyline_has_no_ends_and_a_closing_piece():
    pieces, entity_ends = entity_pieces(
        rec("SQ", "LWPOLYLINE", L, ((0, 0), (10, 0), (10, 10), (0, 10)), closed=True)
    )
    assert entity_ends is None and len(pieces) == 4
    assert (pieces[3].a, pieces[3].b) == ((0.0, 10.0), (0.0, 0.0))


def test_a_straight_piece_nearest_point_is_clamped_to_its_ends():
    piece = Piece("A", L, 0, (0.0, 0.0), (10.0, 0.0))
    assert piece.nearest((5.0, 3.0)) == (5.0, 0.0)
    assert piece.nearest((-4.0, 3.0)) == (0.0, 0.0)


# -- dangling and joined ---------------------------------------------------------------


def test_a_lone_line_dangles_at_both_ends():
    result = run(line("A", 0, 0, 1000, 0))
    assert ends(result["dangling"]) == {("A", "start"), ("A", "end")}
    assert result["near_miss"] == [] and result["crossing"] == []


def test_a_t_junction_joins_the_stem():
    result = run(line("A", 0, 0, 1000, 0), line("B", 500, 0, 500, 500))
    assert ends(result["dangling"]) == {("A", "start"), ("A", "end"), ("B", "end")}


def test_a_closed_square_of_lines_has_no_finding():
    square = [
        line("A", 0, 0, 1000, 0),
        line("B", 1000, 0, 1000, 1000),
        line("C", 1000, 1000, 0, 1000),
        line("D", 0, 1000, 0, 0),
    ]
    result = run(*square)
    assert (result["dangling"], result["near_miss"], result["crossing"]) == ([], [], [])


def test_an_end_on_an_arc_and_on_an_arc_body_is_joined():
    # ARC centre (0,0) r 100 from 0 to 90 degrees: ends (100,0) and (0,100)
    arc = rec("ARC1", "ARC", L, ((0.0, 0.0),), radius=100.0, angles=(0.0, 90.0))
    on_end = line("L1", 100, 0, 500, 0)
    on_body = line("L2", 70.71067811865476, 70.71067811865476, 300, 300)
    result = run(arc, on_end, on_body)
    assert ends(result["dangling"]) == {("ARC1", "end"), ("L1", "end"), ("L2", "end")}


def test_an_end_on_a_bulged_segment_is_joined():
    arc = rec("PL", "LWPOLYLINE", L, ((0.0, 0.0), (200.0, 0.0)), bulges=(1.0, 0.0))
    result = run(arc, line("L", 100, -100, 100, -500))
    assert ends(result["dangling"]) == {("PL", "start"), ("PL", "end"), ("L", "end")}


def test_an_end_inside_an_insert_box_is_attached():
    valve = rec("V1", "INSERT", "VALVES", ((1000, 0),), block="VALVE", bbox=(995, -5, 1005, 5))
    result = run(line("A", 0, 0, 995, 0), valve)
    assert ends(result["dangling"]) == {("A", "start")}


def test_an_end_on_a_curve_of_another_layer_is_attached():
    tank = rec(
        "TK",
        "LWPOLYLINE",
        "EQUIPMENT",
        ((1000, -500), (2000, -500), (2000, 500), (1000, 500)),
        closed=True,
    )
    result = run(line("A", 0, 0, 1000, 0), tank)
    assert ends(result["dangling"]) == {("A", "start")}


def test_model_space_and_a_layout_never_join():
    result = run(line("A", 0, 0, 100, 0), line("B", 100, 0, 200, 0, space="Layout1"))
    assert ends(result["dangling"]) == {("A", "start"), ("A", "end"), ("B", "start"), ("B", "end")}


# -- lines drawn twice -----------------------------------------------------------------


def test_a_pipe_drawn_twice_still_dangles_at_all_four_ends():
    result = run(line("A", 0, 0, 1000, 0), line("A2", 0, 0, 1000, 0))
    assert ends(result["dangling"]) == {
        ("A", "start"),
        ("A", "end"),
        ("A2", "start"),
        ("A2", "end"),
    }
    assert result["near_miss"] == [] and result["crossing"] == []


def test_a_line_over_a_polyline_joins_only_where_the_polyline_turns():
    polyline = rec("PL", "LWPOLYLINE", L, ((0, 0), (1000, 0), (1000, 1000)))
    result = run(polyline, line("OV", 0, 0, 1000, 0))
    assert ends(result["dangling"]) == {("OV", "start"), ("PL", "end"), ("PL", "start")}


def test_an_arc_drawn_twice_still_dangles_at_all_four_ends():
    # the arc form of a pipe drawn twice: a bend drawn twice must not join itself
    first = rec("A1", "ARC", L, ((0.0, 0.0),), radius=100.0, angles=(0.0, 90.0))
    second = rec("A2", "ARC", L, ((0.0, 0.0),), radius=100.0, angles=(0.0, 90.0))
    result = run(first, second)
    assert ends(result["dangling"]) == {
        ("A1", "start"),
        ("A1", "end"),
        ("A2", "start"),
        ("A2", "end"),
    }
    assert result["near_miss"] == []


def test_two_arcs_of_one_circle_butted_end_to_end_join():
    # 0-90 then 90-180 on the same circle: continuing the other way is a join
    first = rec("A1", "ARC", L, ((0.0, 0.0),), radius=100.0, angles=(0.0, 90.0))
    second = rec("A2", "ARC", L, ((0.0, 0.0),), radius=100.0, angles=(90.0, 180.0))
    assert ends(run(first, second)["dangling"]) == {("A1", "start"), ("A2", "end")}


def test_a_bulge_of_minus_one_is_the_semicircle_above_its_chord():
    ((arc,), _ends) = entity_pieces(
        rec("PL", "LWPOLYLINE", L, ((0.0, 0.0), (200.0, 0.0)), bulges=(-1.0, 0.0))
    )
    assert arc.radius == pytest.approx(100.0)
    assert arc.nearest((100.0, 150.0)) == (pytest.approx(100.0), pytest.approx(100.0))


def test_a_side_drawn_twice_inside_a_closed_loop_is_harmless():
    square = [
        line("A", 0, 0, 1000, 0),
        line("A2", 0, 0, 1000, 0),
        line("B", 1000, 0, 1000, 1000),
        line("C", 1000, 1000, 0, 1000),
        line("D", 0, 1000, 0, 0),
    ]
    result = run(*square)
    assert (result["dangling"], result["near_miss"], result["crossing"]) == ([], [], [])


# -- near misses -----------------------------------------------------------------------


def test_two_ends_3_apart_are_one_end_end_near_miss():
    result = run(line("A", 0, 0, 1000, 0), line("B", 1003, 0, 2000, 0))
    (row,) = result["near_miss"]
    assert row["handles"] == ["A", "B"] and row["kind"] == "end_end"
    assert row["gap"] == pytest.approx(3.0)
    assert (row["at"], row["to"]) == ([1000.0, 0.0], [1003.0, 0.0])
    assert ends(result["dangling"]) == {("A", "start"), ("B", "end")}


def test_an_end_4_above_a_line_is_an_end_segment_near_miss():
    result = run(line("A", 0, 0, 1000, 0), line("B", 500, 4, 500, 500))
    (row,) = result["near_miss"]
    assert row["handles"] == ["B", "A"] and row["kind"] == "end_segment"
    assert row["gap"] == pytest.approx(4.0) and row["to"] == [500.0, 0.0]


def test_an_end_beyond_the_gap_dangles_instead():
    result = run(line("A", 0, 0, 1000, 0), line("B", 500, 6, 500, 500))
    assert result["near_miss"] == []
    assert ("B", "start") in ends(result["dangling"])


def test_a_polyline_that_nearly_closes_on_itself_is_one_near_miss():
    ring = rec("RING", "LWPOLYLINE", L, ((0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 3)))
    result = run(ring)
    (row,) = result["near_miss"]
    assert row["handles"] == ["RING", "RING"] and row["kind"] == "end_end"
    assert row["gap"] == pytest.approx(3.0)
    assert result["dangling"] == []


# -- crossings -------------------------------------------------------------------------


def test_two_lines_of_one_layer_crossing_mid_span_are_a_crossing():
    result = run(line("A", 0, 0, 1000, 0), line("B", 500, -500, 500, 500))
    assert result["crossing"] == [
        {"handles": ["A", "B"], "layer": L, "at": [500.0, 0.0], "space": "Model"}
    ]


def test_two_layers_crossing_and_a_t_junction_are_not_crossings():
    other = line("B", 500, -500, 500, 500, layer="CABLE")
    assert run(line("A", 0, 0, 1000, 0), other, layers=(L, "CABLE"))["crossing"] == []
    assert run(line("A", 0, 0, 1000, 0), line("B", 500, 0, 500, 500))["crossing"] == []


def test_a_bow_tie_polyline_crosses_itself():
    bow = rec("BOW", "LWPOLYLINE", L, ((0, 0), (100, 100), (100, 0), (0, 100)))
    (row,) = run(bow)["crossing"]
    assert row["handles"] == ["BOW", "BOW"] and row["at"] == [50.0, 50.0]


# -- defaults and refusals -------------------------------------------------------------


def test_the_default_gap_is_0_2_percent_of_the_network_diagonal():
    # box 0..3000 x 0..4000: diagonal 5000, gap 10
    result = topology_findings(snap(line("A", 0, 0, 3000, 0), line("B", 0, 0, 0, 4000)), layers=[L])
    assert result["gap"] == pytest.approx(10.0)
    assert result["stats"] == {"entities": 2, "ends": 4, "skipped": {}}


def test_an_arc_without_a_centre_is_skipped_and_counted():
    result = run(line("A", 0, 0, 10, 0), rec("ARC9", "ARC", L, (), radius=5.0, angles=(0, 90)))
    assert result["stats"]["skipped"] == {"ARC": 1}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"layers": []}, r"^layers: name at least one layer to check"),
        ({"layers": [L], "tol": 0}, r"^tol must be a positive number; got 0"),
        ({"layers": [L], "gap": -1}, r"^gap must be a positive number; got -1"),
        ({"layers": [L], "gap": 0.005}, r"^gap \(0\.005\) must exceed tol \(0\.01\)"),
    ],
)
def test_refusals_come_before_any_work(kwargs, message):
    with pytest.raises(ValueError, match=message):
        topology_findings(snap(line("A", 0, 0, 1, 0)), **kwargs)


def test_cap_findings_counts_everything_and_cuts_the_lists():
    lines = [line(f"L{i}", 0, 1000 * i, 100, 1000 * i) for i in range(3)]
    capped = cap_findings(run(*lines), limit=2)
    assert capped["counts"] == {"dangling": 6, "near_miss": 0, "crossing": 0}
    assert capped["truncated"] == {"dangling": 4, "near_miss": 0, "crossing": 0}
    assert len(capped["dangling"]) == 2


# -- the default layers on the synthetic plant -----------------------------------------


def test_the_plant_piping_layers_are_network_layers(tmp_path):
    from engineering.understand.snapshot import read_snapshot

    truth = build_plant_pair(tmp_path)
    pid = read_snapshot(truth["pid"])
    found = {row["layer"] for row in network_layers(pid)}
    for layer, service in truth["layer_services"].items():
        if layer in pid.layers and service in ("product", "cip_supply", "cip_return"):
            assert layer in found, (layer, classify_layer(layer))
    assert all(row["confidence"] >= 0.9 for row in network_layers(pid))
