"""The line-network engine: runs, duplicates once, fittings, diameters, services, equipment.

Every drawing below is built record by record, so each expected length is the
hand sum written beside it; nothing is read back from the implementation. The
last three tests read the synthetic plant pair of Task 2 and compare every run
with the generator's own truth.
"""

from __future__ import annotations

import pytest

from engineering.understand.network import (
    Run,
    build_network,
    default_layers,
    is_reducer,
    label_search_default,
)
from engineering.understand.snapshot import EntityRecord, Snapshot, read_snapshot
from engineering.understand.vocab import classify_layer, equipment_kind
from tests.fixtures.plant_pair import build_plant_pair

EPS = 1e-9
PIPE = "PRODUCT"


def rec(handle, kind, layer=PIPE, points=(), **extra):
    return EntityRecord(
        handle=handle, type=kind, layer=layer, space="Model", points=tuple(points), **extra
    )


def line(handle, a, b, layer=PIPE):
    return rec(handle, "LINE", layer, (a, b))


def text(handle, value, at, height=5.0, layer="TEXT"):
    x, y = at
    return rec(
        handle,
        "TEXT",
        layer,
        (at,),
        text=value,
        height=height,
        bbox=(x, y, x + height * len(value) * 0.6, y + height),
    )


def tank(handle, tag, box, layer="EQUIPMENT"):
    """A closed rectangle with its tag written inside it."""
    x0, y0, x1, y1 = box
    outline = rec(
        handle, "LWPOLYLINE", layer, ((x0, y0), (x1, y0), (x1, y1), (x0, y1)), closed=True, bbox=box
    )
    return [outline, text(handle + "T", tag, ((x0 + x1) / 2.0 - 10.0, (y0 + y1) / 2.0))]


def insert(handle, block, box, layer=PIPE):
    return rec(
        handle,
        "INSERT",
        layer,
        (((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0),),
        block=block,
        bbox=box,
    )


def snap(*records, insunits=4):
    flat = []
    for item in records:
        flat.extend(item if isinstance(item, list) else [item])
    layers = {r.layer: {"color": 7, "linetype": "Continuous"} for r in flat}
    return Snapshot(
        source="test",
        insunits=insunits,
        extmin=None,
        extmax=None,
        layers=layers,
        layouts=(),
        records=tuple(flat),
    )


def total(run: Run) -> float:
    return sum(edge["length"] for edge in run.edges)


def net(*records, **kw):
    return build_network(snap(*records), layers=[PIPE], **kw)


# -- topology ------------------------------------------------------------------


def test_ends_within_tol_merge_into_one_run():
    result = net(line("1", (0.0, 0.0), (1000.0, 0.0)), line("2", (1000.4, 0.0), (2000.0, 0.0)))
    (run,) = result["runs"]
    # the second line starts at the merged vertex (1000, 0): 1000 + 1000
    assert abs(total(run) - 2000.0) < EPS
    assert run.ends == ((0.0, 0.0), (2000.0, 0.0))


def test_a_branch_ending_on_a_header_is_a_t_junction():
    result = net(line("1", (0.0, 0.0), (2000.0, 0.0)), line("2", (1000.0, 0.0), (1000.0, 800.0)))
    (run,) = result["runs"]
    assert abs(total(run) - 2800.0) < EPS  # 2000 header + 800 branch
    assert len(run.edges) == 3  # header split at the tee
    assert run.ends == ((0.0, 0.0), (1000.0, 800.0), (2000.0, 0.0))


def test_two_lines_crossing_without_an_end_do_not_connect():
    result = net(line("1", (0.0, 0.0), (2000.0, 0.0)), line("2", (1000.0, -500.0), (1000.0, 500.0)))
    assert len(result["runs"]) == 2


# -- Review Focus 4: double-drawn pipe counts once ----------------------------


def test_a_line_drawn_twice_is_counted_once():
    result = net(line("1", (0.0, 0.0), (2000.0, 0.0)), line("2", (0.0, 0.0), (2000.0, 0.0)))
    (run,) = result["runs"]
    assert abs(total(run) - 2000.0) < EPS  # not 4000
    assert abs(result["stats"]["overlap_length"] - 2000.0) < EPS
    assert run.edges[0]["handles"] == ("1", "2")


def test_a_line_drawn_over_a_polyline_is_counted_once():
    poly = rec("P", "LWPOLYLINE", PIPE, ((0.0, 0.0), (1000.0, 0.0), (3000.0, 0.0)))
    result = net(line("L", (0.0, 0.0), (3000.0, 0.0)), poly)
    (run,) = result["runs"]
    # the line splits at the polyline's vertex (1000, 0): pieces 1000 + 2000, each once
    assert abs(total(run) - 3000.0) < EPS
    assert abs(result["stats"]["overlap_length"] - 3000.0) < EPS
    assert all(edge["handles"] == ("L", "P") for edge in run.edges)


def test_a_partial_collinear_overlap_counts_the_shared_stretch_once():
    result = net(line("1", (0.0, 0.0), (2000.0, 0.0)), line("2", (1500.0, 0.0), (3000.0, 0.0)))
    (run,) = result["runs"]
    assert abs(total(run) - 3000.0) < EPS  # 0..3000 once; 1500..2000 was drawn twice
    assert abs(result["stats"]["overlap_length"] - 500.0) < EPS


def test_an_arc_keeps_its_true_length():
    # a quarter circle of radius 1000 centred on the origin, from 0 deg to 90 deg
    arc = rec("A", "ARC", PIPE, ((0.0, 0.0),), radius=1000.0, angles=(0.0, 90.0))
    (run,) = net(arc)["runs"]
    assert abs(total(run) - 500.0 * 3.141592653589793) < 1e-6  # 2*pi*1000/4
    ends = [c for point in run.ends for c in point]
    assert ends == pytest.approx([0.0, 1000.0, 1000.0, 0.0], abs=1e-9)


# -- fittings ----------------------------------------------------------------


def test_an_inline_valve_joins_the_two_lines_and_adds_no_length():
    result = net(
        line("1", (0.0, 0.0), (1000.0, 0.0)),
        line("2", (1100.0, 0.0), (2000.0, 0.0)),
        insert("V", "GATE_VALVE", (1000.0, -50.0, 1100.0, 50.0)),
    )
    (run,) = result["runs"]
    assert abs(total(run) - 1900.0) < EPS  # 1000 + 900; the valve body is not pipe
    assert run.ends == ((0.0, 0.0), (2000.0, 0.0))
    assert result["stats"]["fittings"] == 1


@pytest.mark.parametrize(
    ("block", "reducer"),
    [
        ("REDUCER_51_38", True),
        ("ПЕРЕХОД_51", True),
        ("REDÜKSİYON-51", True),
        ("verloop 51x38", True),
        ("REDUZIERSTÜCK_51", True),
        ("GATE_VALVE", False),
        (None, False),
    ],
)
def test_reducer_words_in_four_languages(block, reducer):
    assert is_reducer(block) is reducer


def test_reducer_words_agree_with_the_vocabulary():
    # one reducer vocabulary: every block equipment_kind calls a reducer is one here
    for block in ("REDUCER_51_38", "ПЕРЕХОД_51", "REDÜKSİYON-51", "VERLOOPSTUK", "REDUZIERSTÜCK"):
        assert equipment_kind(block)["kind"] == "reducer"
        assert is_reducer(block), block


def test_an_insert_holding_more_than_four_free_ends_is_not_a_fitting():
    # a title-block sized INSERT over four separate lines: eight free ends inside
    result = net(
        *[line(str(n), (0.0, 100.0 * n), (500.0, 100.0 * n)) for n in range(4)],
        insert("TB", "TITLE", (-10.0, -10.0, 510.0, 310.0)),
    )
    assert len(result["runs"]) == 4
    assert result["stats"]["fittings"] == 0
    assert result["stats"]["fittings_skipped"] == ["TB"]


# -- diameters ---------------------------------------------------------------


def test_a_label_is_direct_and_carries_by_continuity_across_a_tee():
    result = net(
        line("1", (0.0, 0.0), (1000.0, 0.0)),
        line("2", (1000.0, 0.0), (2000.0, 0.0)),
        line("3", (1000.0, 0.0), (1000.0, 800.0)),
        text("D", "Ø51", (400.0, 5.0)),
    )
    (run,) = result["runs"]
    by_handle = {edge["handles"]: edge for edge in run.edges}
    assert by_handle[("1",)]["diameter_source"] == "direct"
    assert by_handle[("1",)]["label"] == "D"
    for handle in (("2",), ("3",)):
        assert by_handle[handle]["diameter"] == "Ø51"
        assert by_handle[handle]["diameter_source"] == "continuity"
    assert result["unassigned"] == []


def test_a_label_holds_for_every_piece_of_its_drawn_entity():
    poly = rec("P", "LWPOLYLINE", PIPE, ((0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0)))
    (run,) = net(poly, text("D", "DN20", (400.0, 5.0)))["runs"]
    assert [edge["diameter_source"] for edge in run.edges] == ["direct", "direct"]
    assert {edge["diameter"] for edge in run.edges} == {"DN20"}


def test_continuity_stops_at_a_reducer():
    result = net(
        line("1", (0.0, 0.0), (1000.0, 0.0)),
        line("2", (1060.0, 0.0), (2000.0, 0.0)),
        insert("R", "REDUCER_51_38", (1000.0, -30.0, 1060.0, 30.0)),
        text("D", "Ø51", (400.0, 5.0)),
    )
    (run,) = result["runs"]
    by_handle = {edge["handles"]: edge for edge in run.edges}
    assert by_handle[("1",)]["diameter"] == "Ø51"
    assert by_handle[("2",)]["diameter"] is None
    assert by_handle[("2",)]["diameter_source"] == "unassigned"
    assert result["unassigned"][0]["reason"].startswith("no diameter label")
    assert result["stats"]["reducers"] == 1


def test_continuity_stops_at_a_reducer_the_line_is_broken_at():
    # BREAK-at-point: two LINEs share a vertex inside the reducer's box, no free end there
    result = net(
        line("1", (0.0, 0.0), (1030.0, 0.0)),
        line("2", (1030.0, 0.0), (2000.0, 0.0)),
        insert("R", "REDUCER_51_38", (1000.0, -30.0, 1060.0, 30.0)),
        text("D", "Ø51", (400.0, 5.0)),
    )
    by_handle = {edge["handles"]: edge for edge in result["runs"][0].edges}
    assert by_handle[("1",)]["diameter"] == "Ø51"
    assert by_handle[("2",)]["diameter_source"] == "unassigned"
    assert (result["stats"]["fittings"], result["stats"]["reducers"]) == (1, 1)


def test_a_polyline_drawn_through_a_reducer_is_cut_there():
    poly = rec("P", "LWPOLYLINE", PIPE, ((0.0, 0.0), (2000.0, 0.0)))
    result = net(
        poly,
        line("BR", (1500.0, 0.0), (1500.0, 800.0)),
        insert("R", "REDUCER_51_38", (1000.0, -30.0, 1060.0, 30.0)),
        text("D", "Ø51", (400.0, 5.0)),
    )
    (run,) = result["runs"]
    # cut at the foot of the box centre (1030, 0): 1030 + 470 + 500 + 800 branch
    assert abs(total(run) - 2800.0) < EPS
    by_span = {(edge["a"][0], edge["b"][0], edge["handles"]): edge for edge in run.edges}
    labelled = by_span[(0.0, 1030.0, ("P",))]
    assert (labelled["diameter"], labelled["diameter_source"]) == ("Ø51", "direct")
    # the label holds for the polyline only up to the reducer; nothing crosses it
    others = [edge for edge in run.edges if edge is not labelled]
    assert len(others) == 3
    assert {edge["diameter_source"] for edge in others} == {"unassigned"}
    assert result["stats"]["reducers"] == 1


def test_a_pipe_running_beside_a_reducer_is_not_cut():
    # a parallel pipe grazing the reducer's box edge does not pass through its middle
    result = net(
        line("1", (0.0, 30.0), (2000.0, 30.0)),
        insert("R", "REDUCER_51_38", (1000.0, -30.0, 1060.0, 30.0)),
        text("D", "Ø51", (400.0, 35.0)),
    )
    (run,) = result["runs"]
    assert len(run.edges) == 1
    assert run.edges[0]["diameter_source"] == "direct"
    assert result["stats"]["reducers"] == 0


@pytest.mark.parametrize("order", ["ABC", "BAC", "CAB", "ACB", "BCA", "CBA"])
def test_a_three_way_valve_guesses_nothing_whatever_the_record_order(order):
    # C leaves the valve 1000 from A (Ø51) and 1000 from B (DN20): equally near
    records = {
        "A": line("A", (0.0, 0.0), (1000.0, 0.0)),
        "B": line("B", (1100.0, 0.0), (2100.0, 0.0)),
        "C": line("C", (1050.0, 50.0), (1050.0, 1050.0)),
    }
    result = net(
        *[records[name] for name in order],
        insert("V", "3WAY_VALVE", (1000.0, -50.0, 1100.0, 50.0)),
        text("TA", "Ø51", (400.0, 5.0)),
        text("TB", "DN20", (1500.0, 5.0)),
    )
    (run,) = result["runs"]
    c = next(edge for edge in run.edges if edge["handles"] == ("C",))
    assert (c["diameter"], c["diameter_source"]) == (None, "unassigned")
    assert [u["reason"] for u in result["unassigned"]] == ["two different diameters equally near"]
    assert run.ends == ((0.0, 0.0), (1050.0, 1050.0), (2100.0, 0.0))


def test_a_label_nearer_to_a_pipe_on_an_unread_layer_is_not_taken():
    # the label stands 5 below the WATER line and 22.5 above PRODUCT (search 25)
    drawing = snap(
        line("1", (0.0, 0.0), (1000.0, 0.0), PIPE),
        line("W", (0.0, 30.0), (1000.0, 30.0), "WATER"),
        text("D", "DN20", (400.0, 20.0)),
    )
    assert "WATER" not in default_layers(drawing)
    result = build_network(drawing)
    assert result["runs"][0].edges[0]["diameter_source"] == "unassigned"
    assert result["stats"]["labels_skipped"] == [
        {
            "handle": "D",
            "text": "DN20",
            "reason": "nearer to a line on layer WATER, which is not read",
        }
    ]


def test_an_equipment_outline_does_not_take_a_pipe_label():
    # closed outlines are shapes, not pipes: a label beside the pipe end stays the pipe's
    result = net(
        tank("A", "T4100", (-500.0, -500.0, 0.0, 500.0)),
        line("1", (0.0, 0.0), (1000.0, 0.0)),
        text("D", "Ø51", (2.0, 5.0)),
    )
    edge = result["runs"][0].edges[0]
    assert (edge["diameter"], edge["diameter_source"]) == ("Ø51", "direct")


def test_continuity_crosses_a_valve():
    result = net(
        line("1", (0.0, 0.0), (1000.0, 0.0)),
        line("2", (1100.0, 0.0), (2000.0, 0.0)),
        insert("V", "GATE_VALVE", (1000.0, -50.0, 1100.0, 50.0)),
        text("D", "Ø51", (400.0, 5.0)),
    )
    edge = next(e for e in result["runs"][0].edges if e["handles"] == ("2",))
    assert (edge["diameter"], edge["diameter_source"]) == ("Ø51", "continuity")


def test_a_conflicting_label_stops_continuity_and_the_nearer_one_wins():
    result = net(
        line("1", (0.0, 0.0), (1000.0, 0.0)),
        line("2", (1000.0, 0.0), (2000.0, 0.0)),
        line("3", (2000.0, 0.0), (3000.0, 0.0)),
        text("A", "Ø51", (400.0, 5.0)),
        text("B", "DN20", (1400.0, 5.0)),
    )
    by_handle = {edge["handles"]: edge for edge in result["runs"][0].edges}
    assert by_handle[("1",)]["diameter"] == "Ø51"
    assert by_handle[("2",)]["diameter"] == "DN20"
    # piece 3 is next to piece 2 (DN20), one piece further from piece 1 (Ø51)
    assert (by_handle[("3",)]["diameter"], by_handle[("3",)]["diameter_source"]) == (
        "DN20",
        "continuity",
    )


def test_two_diameters_equally_near_leave_the_piece_unassigned():
    result = net(
        line("1", (0.0, 0.0), (1000.0, 0.0)),
        line("2", (1000.0, 0.0), (2000.0, 0.0)),
        line("3", (2000.0, 0.0), (3000.0, 0.0)),
        text("A", "Ø51", (400.0, 5.0)),
        text("B", "DN20", (2400.0, 5.0)),
    )
    middle = next(e for e in result["runs"][0].edges if e["handles"] == ("2",))
    assert middle["diameter_source"] == "unassigned"
    assert result["unassigned"][0]["reason"] == "two different diameters equally near"


def test_labels_are_kept_exactly_as_drawn():
    result = net(line("1", (0.0, 0.0), (1000.0, 0.0)), text("D", "SMS51", (400.0, 5.0)))
    assert result["runs"][0].edges[0]["diameter"] == "SMS51"


def test_a_label_beyond_the_search_distance_is_not_read():
    # median text height 5 -> label_search 25; the label's centre stands 200 away
    result = net(line("1", (0.0, 0.0), (1000.0, 0.0)), text("D", "Ø51", (400.0, 200.0)))
    assert result["stats"]["label_search"] == 25.0
    assert result["runs"][0].edges[0]["diameter_source"] == "unassigned"


# -- services ----------------------------------------------------------------


def test_a_return_word_overrides_the_layer_for_the_whole_run():
    # the layer says supply; only the word says this run returns
    layer = "CIP_SUPPLY"
    base = classify_layer(layer)["service"]
    assert base in ("cip_supply", "cip_return"), "the vocabulary must file CIP_SUPPLY as CIP"
    records = [
        line("1", (0.0, 0.0), (1000.0, 0.0), layer),
        line("2", (1000.0, 0.0), (1000.0, 1000.0), layer),
        text("W", "ВОЗВРАТ", (400.0, 5.0)),
    ]
    result = build_network(snap(*records), layers=[layer])
    (run,) = result["runs"]
    assert run.service == "cip_return"
    assert {edge["service"] for edge in run.edges} == {"cip_return"}


def test_a_cip_layer_naming_no_direction_takes_it_from_the_word():
    # classify_layer files a directionless CIP layer as `unknown` on purpose:
    # the layer is not trusted with the direction, so the word must decide it
    layer = "CIP_LINES"
    found = classify_layer(layer)
    assert (found["service"], found["keyword"]) == ("unknown", "cip")
    records = [
        line("1", (0.0, 0.0), (1000.0, 0.0), layer),
        line("2", (1000.0, 0.0), (1000.0, 1000.0), layer),
        text("W", "ВОЗВРАТ", (400.0, 5.0)),
    ]
    result = build_network(snap(*records), layers=[layer])
    (run,) = result["runs"]
    assert run.service == "cip_return"
    assert result["stats"]["service_conflicts"] == []


def test_a_directionless_cip_layer_without_a_word_stays_unknown():
    (run,) = build_network(
        snap(line("1", (0.0, 0.0), (1000.0, 0.0), "CIP_LINES")), layers=["CIP_LINES"]
    )["runs"]
    assert run.service == "unknown"


def test_without_a_word_the_run_keeps_its_layer_service():
    (run,) = net(line("1", (0.0, 0.0), (1000.0, 0.0)))["runs"]
    assert run.service == classify_layer(PIPE)["service"]


# -- equipment ---------------------------------------------------------------


def test_run_ends_attach_to_the_tags_whose_geometry_holds_them():
    result = net(
        tank("A", "T4100", (-500.0, -500.0, 0.0, 500.0)),
        tank("B", "T4101", (2000.0, -500.0, 2500.0, 500.0)),
        line("1", (0.0, 0.0), (2000.0, 0.0)),
    )
    (run,) = result["runs"]
    assert run.tags == ("T4100", "T4101")
    assert result["attachments"]["R1"] == [
        {"at": (0.0, 0.0), "tag": "T4100"},
        {"at": (2000.0, 0.0), "tag": "T4101"},
    ]


def test_a_tagged_insert_is_equipment_not_a_fitting():
    pump = rec(
        "PU",
        "INSERT",
        "EQUIPMENT",
        ((1050.0, 0.0),),
        block="*U12",
        attribs=(("TAG", "M82"),),
        bbox=(1000.0, -50.0, 1100.0, 50.0),
    )
    result = net(
        line("1", (0.0, 0.0), (1000.0, 0.0)), line("2", (1100.0, 0.0), (2000.0, 0.0)), pump
    )
    assert len(result["runs"]) == 2
    assert all("M82" in run.tags for run in result["runs"])
    assert result["stats"]["fittings"] == 0


def test_a_room_outline_around_two_tags_is_not_equipment():
    room = rec(
        "ROOM",
        "LWPOLYLINE",
        "ROOMS",
        ((-5000.0, -5000.0), (9000.0, -5000.0), (9000.0, 5000.0), (-5000.0, 5000.0)),
        closed=True,
        bbox=(-5000.0, -5000.0, 9000.0, 5000.0),
    )
    outline = rec(
        "A",
        "LWPOLYLINE",
        "EQUIPMENT",
        ((-500.0, -500.0), (0.0, -500.0), (0.0, 500.0), (-500.0, 500.0)),
        closed=True,
        bbox=(-500.0, -500.0, 0.0, 500.0),
    )
    # T4100 is written 30 above its tank, inside the room: the room holds T4100
    # and T4101, so it is not equipment, and the tank is the nearest shape
    # within 2 x label_search = 50
    result = net(
        room,
        outline,
        text("AT", "T4100", (-400.0, 530.0)),
        tank("B", "T4101", (2000.0, -500.0, 2500.0, 500.0)),
        line("1", (0.0, 0.0), (2000.0, 0.0)),
    )
    assert result["equipment"]["T4100"][0]["handle"] == "A"
    assert result["runs"][0].tags == ("T4100", "T4101")


def test_a_shape_far_larger_than_the_others_is_still_found():
    # the grid keeps a box covering many cells aside; it must still be found
    small = [
        rec(
            f"C{n}",
            "CIRCLE",
            "EQUIPMENT",
            ((5000.0 + 300.0 * n, 5000.0),),
            radius=50.0,
            bbox=(4950.0 + 300.0 * n, 4950.0, 5050.0 + 300.0 * n, 5050.0),
        )
        for n in range(5)
    ]
    big = rec(
        "BIG",
        "LWPOLYLINE",
        "EQUIPMENT",
        ((-500.0, -500.0), (-500.0 + 40000.0, -500.0), (39500.0, 39500.0), (-500.0, 39500.0)),
        closed=True,
        bbox=(-500.0, -500.0, 39500.0, 39500.0),
    )
    result = net(
        *small, big, text("AT", "T4100", (-400.0, 0.0)), line("1", (0.0, 0.0), (0.0, -2000.0))
    )
    assert result["equipment"]["T4100"][0]["handle"] == "BIG"
    assert result["runs"][0].tags == ("T4100",)


def test_a_free_end_attaches_to_nothing():
    result = net(
        tank("A", "T4100", (-500.0, -500.0, 0.0, 500.0)), line("1", (0.0, 0.0), (2000.0, 0.0))
    )
    assert result["attachments"]["R1"][1] == {"at": (2000.0, 0.0), "tag": None}


# -- layer choice and refusals -------------------------------------------------


def test_default_layers_are_the_confidently_classified_network_layers():
    drawing = snap(line("1", (0.0, 0.0), (1.0, 0.0)), line("2", (0.0, 0.0), (1.0, 0.0), "MISC"))
    expected = tuple(
        name
        for name in ("MISC", PIPE)
        if classify_layer(name)["discipline"] in ("piping", "electrical")
        and classify_layer(name)["confidence"] >= 0.9
    )
    assert default_layers(drawing) == expected


def test_label_search_defaults_to_five_median_text_heights():
    drawing = snap(
        text("a", "x", (0, 0), 2.0), text("b", "y", (0, 0), 4.0), text("c", "z", (0, 0), 9.0)
    )
    assert label_search_default(drawing) == 20.0  # 5 x median(2, 4, 9)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tol": 0.0}, "tol: must be a finite number greater than zero"),
        ({"tol": float("nan")}, "tol: must be a finite number greater than zero"),
        ({"label_search": -1.0}, "label_search: must be a finite number greater than zero"),
        ({"layers": ["NOPE"]}, "layers: NOPE not in the drawing; it has: PRODUCT"),
        ({"layers": []}, "layers: an empty list reads nothing"),
    ],
)
def test_refusals_name_the_parameter(kwargs, message):
    drawing = snap(line("1", (0.0, 0.0), (1.0, 0.0)))
    kwargs = {"layers": [PIPE], **kwargs}
    with pytest.raises(ValueError, match="^" + message.replace("(", r"\(")):
        build_network(drawing, **kwargs)


# -- the synthetic plant pair (Task 2) -------------------------------------------


@pytest.fixture(scope="module")
def plant(tmp_path_factory):
    truth = build_plant_pair(tmp_path_factory.mktemp("plant"))
    return truth, read_snapshot(truth["pid"])


def run_key(tags, service):
    return (tuple(sorted(tags)), service)


def test_the_generator_names_each_run_by_its_tags_and_service(plant):
    truth, _pid = plant
    keys = [run_key(run["tags"], run["service"]) for run in truth["pipe_runs"]]
    assert len(keys) == len(set(keys))


def test_every_synthetic_run_has_its_service_diameter_and_tags(plant):
    truth, pid = plant
    found: dict = {}
    for run in build_network(pid)["runs"]:
        found.setdefault(run_key(run.tags, run.service), []).append(run)
    for expected in truth["pipe_runs"]:
        key = run_key(expected["tags"], expected["service"])
        runs = found.get(key, [])
        assert len(runs) == 1, f"{key}: {len(runs)} runs carry these tags and this service"
        if expected["status"] == "C":
            continue
        edges = runs[0].edges
        assert expected["diameter"] in {edge["diameter"] for edge in edges}, key
        sources = {edge["diameter_source"] for edge in edges}
        if expected["status"] == "A":
            assert sources == {"direct"}, key
        else:
            assert sources & {"continuity", "unassigned"}, key


def test_the_synthetic_service_layers_are_read_by_default(plant):
    truth, pid = plant
    layers = set(build_network(pid)["stats"]["layers"])
    service_layers = {
        layer
        for layer, service in truth["layer_services"].items()
        if service not in ("electrical", "unknown")
    }
    assert service_layers <= layers
