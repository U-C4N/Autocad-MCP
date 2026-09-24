"""Pipe takeoff rows: layout lengths, statuses, rooms, units and the scale rule.

The hand-built pair: a P&ID (mm) with three tanks and two runs, and a layout
whose INSUNITS says inches over millimetre geometry - two rooms split by a
wall at x = 20000, the tags at T4100 (2000, 3000), T4101 (30000, 3000) and
T4102 (30000, 15000). Every expected length is worked in the comment beside it.
The last four tests read the synthetic plant pair of Task 2 and compare every
run with the generator's own hand-computed rectilinear MST.
"""

from __future__ import annotations

import pytest

from engineering.understand.network import build_network
from engineering.understand.scale import scale_check
from engineering.understand.snapshot import EntityRecord, Snapshot, read_snapshot
from engineering.understand.takeoff import (
    DEFAULT_SERVICES,
    layout_tags,
    pipe_rows,
    unit_of,
)
from engineering.understand.vocab import classify_layer, room_label
from tests.fixtures.plant_pair import build_plant_pair

EPS = 1e-9
PIPE = "PRODUCT"
WALLS = "WALLS"


def rec(handle, kind, layer, points=(), **extra):
    return EntityRecord(
        handle=handle, type=kind, layer=layer, space="Model", points=tuple(points), **extra
    )


def text(handle, value, at, height, layer="TEXT"):
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


def snap(records, insunits, source):
    layers = {r.layer: {"color": 7, "linetype": "Continuous"} for r in records}
    return Snapshot(
        source=source,
        insunits=insunits,
        extmin=None,
        extmax=None,
        layers=layers,
        layouts=(),
        records=tuple(records),
    )


def tank(handle, tag, x0, y0):
    box = (x0, y0, x0 + 500.0, y0 + 500.0)
    return [
        rec(
            handle,
            "LWPOLYLINE",
            "EQUIPMENT",
            ((x0, y0), (x0 + 500.0, y0), (x0 + 500.0, y0 + 500.0), (x0, y0 + 500.0)),
            closed=True,
            bbox=box,
        ),
        text(handle + "T", tag, (x0 + 150.0, y0 + 225.0), 50.0),
    ]


def pid_records():
    return [
        *tank("A", "T4100", 0.0, 0.0),
        *tank("B", "T4101", 3000.0, 0.0),
        *tank("C", "T4102", 3000.0, 2000.0),
        rec("L1", "LINE", PIPE, ((500.0, 250.0), (3000.0, 250.0))),
        rec("L2", "LINE", PIPE, ((3250.0, 500.0), (3250.0, 2000.0))),
        text("D1", "Ø51", (1500.0, 260.0), 50.0),
    ]


def layout_records(dx=0.0, extra=()):
    def at(x, y):
        return (x + dx, y)

    return [
        rec(
            f"W{dx}",
            "LWPOLYLINE",
            WALLS,
            (at(0, 0), at(40000, 0), at(40000, 20000), at(0, 20000)),
            closed=True,
            bbox=(dx, 0.0, dx + 40000.0, 20000.0),
        ),
        rec(f"X{dx}", "LINE", WALLS, (at(20000, 0), at(20000, 20000))),
        text(f"R1{dx}", "ROOM 101", at(5000, 10000), 250.0),
        text(f"R2{dx}", "ROOM 102", at(30000, 10000), 250.0),
        text(f"T0{dx}", "T4100", at(2000, 3000), 250.0),
        text(f"T1{dx}", "T4101", at(30000, 3000), 250.0),
        text(f"T2{dx}", "T4102", at(30000, 15000), 250.0),
        *extra,
    ]


def room_key(label):
    found = room_label(label)
    assert found is not None, f"the vocabulary must read {label!r} as a room label"
    return str(found.get("number") or found.get("name"))


@pytest.fixture
def pair():
    assert classify_layer(PIPE)["service"] == "product", "PRODUCT must be a product layer"
    assert classify_layer(WALLS)["discipline"] == "architecture", "WALLS must be a wall layer"
    pid = snap(pid_records(), 4, "pid.dxf")
    layout = snap(layout_records(), 1, "layout.dxf")  # INSUNITS 1 = inches, geometry in mm
    return pid, layout, build_network(pid, layers=[PIPE])


def run_by_tags(result, *tags):
    (run,) = [r for r in result["runs"] if r["tags"] == list(tags)]
    return run


def test_layout_lengths_are_manhattan_between_the_tags(pair):
    pid, layout, network = pair
    result = pipe_rows(network, pid, layout, length_source="layout")
    a = run_by_tags(result, "T4100", "T4101")
    b = run_by_tags(result, "T4101", "T4102")
    assert abs(a["layout_m"] - 28.0) < EPS  # |30000 - 2000| + 0 mm
    assert abs(b["layout_m"] - 12.0) < EPS  # 0 + |15000 - 3000| mm
    assert (a["status"], b["status"]) == ("A", "B")  # b has no diameter label
    assert result["length_source"] == "layout"


def test_review_focus_5_inches_declared_over_millimetres(pair):
    pid, layout, network = pair
    result = pipe_rows(network, pid, layout, length_source="layout")
    units = result["units"]["layout"]
    assert (units["declared"], units["inferred"], units["m_per_unit"]) == ("in", "mm", 0.001)
    a = run_by_tags(result, "T4100", "T4101")
    assert abs(a["layout_m"] - 28.0) < EPS  # not 28000 x 0.0254 = 711.2
    assert any(
        "INSUNITS declares Inches (1)" in w and "reads as mm" in w for w in result["warnings"]
    )
    assert unit_of(pid)["warning"] is None  # the P&ID's mm is what its geometry says


def test_rooms_share_the_route_x_first(pair):
    pid, layout, network = pair
    result = pipe_rows(network, pid, layout, length_source="layout")
    a = run_by_tags(result, "T4100", "T4101")
    # (2000, 3000) -> (30000, 3000): 18000 mm left of the wall at x = 20000, 10000 right
    assert a["rooms"] == pytest.approx(
        {room_key("ROOM 101"): 18.0, room_key("ROOM 102"): 10.0}, abs=EPS
    )
    rows = {(r["room"], r["diameter"]): r for r in result["rows"]}
    assert rows[(room_key("ROOM 101"), "Ø51")]["net_m"] == pytest.approx(18.0, abs=EPS)
    assert rows[(room_key("ROOM 101"), "Ø51")]["direct_m"] == pytest.approx(18.0, abs=EPS)
    assert rows[(room_key("ROOM 102"), "unassigned")]["unassigned_m"] == pytest.approx(
        12.0, abs=EPS
    )


def test_allowance_is_its_own_column_and_totals_add_up(pair):
    pid, layout, network = pair
    result = pipe_rows(network, pid, layout, length_source="layout", allowance=0.25)
    for row in result["rows"]:
        assert row["allowance"] == 0.25
        assert row["allowance_m"] == pytest.approx(row["net_m"] * 0.25, abs=EPS)
        assert row["total_m"] == pytest.approx(row["net_m"] * 1.25, abs=EPS)
    totals = result["totals"]
    assert totals["net_m"] == pytest.approx(40.0, abs=EPS)  # 28 + 12
    assert totals["total_m"] == pytest.approx(50.0, abs=EPS)
    assert totals["flagged_runs"] == 0


def test_vertical_allowance_is_added_per_connection(pair):
    pid, layout, network = pair
    result = pipe_rows(network, pid, layout, length_source="layout", vertical_allowance=0.5)
    a = run_by_tags(result, "T4100", "T4101")
    assert a["vertical_m"] == 0.5  # one tree edge
    assert a["length_m"] == pytest.approx(28.5, abs=EPS)


def test_the_schematic_length_is_kept_beside_the_answer(pair):
    pid, layout, network = pair
    result = pipe_rows(network, pid, layout, length_source="layout")
    a = run_by_tags(result, "T4100", "T4101")
    assert a["schematic_m"] == pytest.approx(2.5, abs=EPS)  # the 2500 mm line on the P&ID


def test_a_tag_missing_from_the_layout_makes_the_run_c(pair):
    pid, _layout, network = pair
    layout = snap([r for r in layout_records() if r.text != "T4102"], 1, "layout.dxf")
    result = pipe_rows(network, pid, layout, length_source="layout")
    b = run_by_tags(result, "T4101", "T4102")
    assert (b["status"], b["reason"]) == ("C", "tag_not_on_layout")
    (row,) = [c for c in result["control"] if c["kind"] == "unroutable"]
    assert row["schematic_m"] == pytest.approx(1.5, abs=EPS)  # the 1500 mm line, drawn length only
    assert result["totals"]["unroutable_m"] == pytest.approx(1.5, abs=EPS)
    assert result["totals"]["flagged_runs"] == 1


def test_a_tag_written_twice_in_the_plan_is_ambiguous_not_picked(pair):
    pid, _layout, network = pair
    extra = [text("dup", "T4101", (35000.0, 18000.0), 250.0)]
    layout = snap(layout_records(extra=extra), 1, "layout.dxf")
    result = pipe_rows(network, pid, layout, length_source="layout")
    assert {r["reason"] for r in result["runs"]} == {"tag_ambiguous"}


def test_of_two_plan_copies_one_is_chosen_and_nothing_is_ambiguous(pair):
    pid, _layout, network = pair
    layout = snap(layout_records() + layout_records(dx=100000.0), 1, "layout.dxf")
    found = layout_tags(layout, ["T4100", "T4101", "T4102"])
    assert found["clusters"] == 2
    assert found["cluster"][2] < 100000.0  # the left copy: ties go to the leftmost
    assert found["ambiguous"] == []
    result = pipe_rows(network, pid, layout, length_source="layout")
    assert run_by_tags(result, "T4100", "T4101")["layout_m"] == pytest.approx(28.0, abs=EPS)


def test_a_run_with_a_free_end_is_c(pair):
    pid, layout, _network = pair
    records = pid_records() + [rec("L3", "LINE", PIPE, ((0.0, 500.0), (0.0, 1500.0)))]
    drawing = snap(records, 4, "pid.dxf")
    result = pipe_rows(
        build_network(drawing, layers=[PIPE]), drawing, layout, length_source="layout"
    )
    reasons = {r["reason"] for r in result["runs"] if r["status"] == "C"}
    assert reasons == {"end_not_on_equipment"}


def test_a_layout_takeoff_says_how_much_it_could_not_route(pair):
    # the free-ended run is not routed: the warning names the count, the drawn
    # length it leaves to the control rows and the P&ID alternative
    pid, layout, _network = pair
    records = pid_records() + [rec("L3", "LINE", PIPE, ((0.0, 600.0), (0.0, 1600.0)))]
    drawing = snap(records, 4, "pid.dxf")
    result = pipe_rows(
        build_network(drawing, layers=[PIPE]), drawing, layout, length_source="layout"
    )
    (warning,) = [w for w in result["warnings"] if "could not be routed" in w]
    assert warning.startswith("1 of 3 runs could not be routed on the layout")
    assert "1.0 m drawn on the P&ID" in warning  # 1000 mm
    assert "length_source='pid'" in warning


def test_pid_lengths_need_no_equipment_at_the_ends():
    # A product line that ends in the open - no tag, no equipment - still has a
    # drawn length: measured on the P&ID it is a row, and each piece goes to the
    # P&ID room it lies in (the wall at x = 4000 cuts the second line).
    walls = [
        rec(
            "W",
            "LWPOLYLINE",
            WALLS,
            ((0, 0), (8000, 0), (8000, 4000), (0, 4000)),
            closed=True,
            bbox=(0.0, 0.0, 8000.0, 4000.0),
        ),
        rec("X", "LINE", WALLS, ((4000, 0), (4000, 4000))),
        text("R1", "ROOM 101", (1000.0, 3000.0), 250.0),
        text("R2", "ROOM 102", (5000.0, 3000.0), 250.0),
    ]
    pipes = [
        rec("p1", "LINE", PIPE, ((1000.0, 1000.0), (3000.0, 1000.0))),  # 2000, room 101
        rec("p2", "LINE", PIPE, ((3000.0, 1000.0), (6000.0, 1000.0))),  # 1000 + 2000
        text("d", "Ø51", (2000.0, 1100.0), 50.0),
    ]
    drawing = snap(walls + pipes, 4, "pid.dxf")
    result = pipe_rows(build_network(drawing, layers=[PIPE]), drawing, None, services=["product"])
    rows = {(row["room"], row["diameter"]): row for row in result["rows"]}
    assert rows[(room_key("ROOM 101"), "Ø51")]["net_m"] == pytest.approx(3.0, abs=EPS)
    assert rows[(room_key("ROOM 102"), "Ø51")]["net_m"] == pytest.approx(2.0, abs=EPS)
    assert result["totals"]["unroutable_m"] == 0.0
    (run,) = result["runs"]
    assert run["tags"] == [] and run["status"] in ("A", "B")


def test_without_room_faces_a_piece_goes_to_the_nearest_numbered_room():
    # No walls: pieces go to the nearest room label. 'TO ROOM STORAGE' is a note
    # pointing at a room, not a room: when the drawing numbers its rooms, only
    # the numbered labels are candidates, so the line at x 8500-9500 is room 102.
    records = [
        text("R1", "ROOM 101", (0.0, 3000.0), 250.0),
        text("R2", "ROOM 102", (12000.0, 3000.0), 250.0),
        text("N", "TO ROOM STORAGE", (9000.0, 1200.0), 250.0),
        rec("p", "LINE", PIPE, ((8500.0, 1000.0), (9500.0, 1000.0))),
        text("d", "Ø51", (9000.0, 1100.0), 50.0),
    ]
    drawing = snap(records, 4, "pid.dxf")
    result = pipe_rows(build_network(drawing, layers=[PIPE]), drawing, None, services=["product"])
    assert [(row["room"], row["net_m"]) for row in result["rows"]] == [
        (room_key("ROOM 102"), pytest.approx(1.0, abs=EPS))
    ]


def detail_pid():
    # three tanks spread over the plant, and a compact detail drawing of the same
    # three tanks at x = 60 m: the detail repeats their pipes
    return [
        *tank("A", "T4100", 0.0, 0.0),
        *tank("B", "T4101", 20000.0, 0.0),
        *tank("C", "T4102", 20000.0, 20000.0),
        rec("L1", "LINE", PIPE, ((500.0, 250.0), (20000.0, 250.0))),  # 19.5 m
        rec("L2", "LINE", PIPE, ((20250.0, 500.0), (20250.0, 20000.0))),  # 19.5 m
        *tank("a", "T4100", 60000.0, 0.0),
        *tank("b", "T4101", 61000.0, 0.0),
        *tank("c", "T4102", 61000.0, 1000.0),
        rec("L3", "LINE", PIPE, ((60500.0, 250.0), (61000.0, 250.0))),  # 0.5 m
        rec("L4", "LINE", PIPE, ((61250.0, 500.0), (61250.0, 1000.0))),  # 0.5 m
        text("D1", "Ø51", (10000.0, 260.0), 50.0),
    ]


def test_a_detail_drawn_twice_is_reported_and_left_out_on_request():
    drawing = snap(detail_pid(), 4, "pid.dxf")
    network = build_network(drawing, layers=[PIPE])
    counted = pipe_rows(network, drawing, None, services=["product"])
    (region,) = counted["detail_copies"]
    assert (region["id"], region["tags"]) == ("D1", ["T4100", "T4101", "T4102"])
    assert (region["kind"], region["base"]) == ("repeated", None)
    assert region["box"][0] > 55000.0  # the compact copy, not the spread-out plant
    assert counted["totals"]["net_m"] == pytest.approx(40.0, abs=EPS)  # 19.5 + 19.5 + 0.5 + 0.5
    assert any("possible detail cop" in w and "D1" in w for w in counted["warnings"])
    left_out = pipe_rows(network, drawing, None, services=["product"], exclude_regions=["D1"])
    assert left_out["totals"]["net_m"] == pytest.approx(39.0, abs=EPS)
    assert left_out["totals"]["excluded_m"] == pytest.approx(1.0, abs=EPS)
    excluded = [c for c in left_out["control"] if c["kind"] == "excluded_region"]
    assert {c["reason"] for c in excluded} == {"D1"} and len(excluded) == 2
    with pytest.raises(ValueError, match="^exclude_regions: 'D9' unknown; the P&ID has D1"):
        pipe_rows(network, drawing, None, services=["product"], exclude_regions=["D9"])


def test_a_package_of_one_equipments_parts_is_reported_as_a_region():
    # A skid drawn in detail: P200's own parts P200A / P200B / P200C sit close
    # together at x = 60 m, far from the plant's tanks. Their tags are written
    # once each - no copy - but the family is one package whose internal pipes
    # a site takeoff may leave to its vendor: reported, left out on request.
    records = [
        *tank("A", "T4100", 0.0, 0.0),
        *tank("B", "T4101", 20000.0, 0.0),
        rec("L1", "LINE", PIPE, ((500.0, 250.0), (20000.0, 250.0))),  # 19.5 m
        *tank("p", "P200A", 60000.0, 0.0),
        *tank("q", "P200B", 61000.0, 0.0),
        *tank("r", "P200C", 61000.0, 1000.0),
        rec("L3", "LINE", PIPE, ((60500.0, 250.0), (61000.0, 250.0))),  # 0.5 m
        rec("L4", "LINE", PIPE, ((61250.0, 500.0), (61250.0, 1000.0))),  # 0.5 m
    ]
    drawing = snap(records, 4, "pid.dxf")
    network = build_network(drawing, layers=[PIPE])
    counted = pipe_rows(network, drawing, None, services=["product"])
    (region,) = counted["detail_copies"]
    assert (region["kind"], region["base"]) == ("family", "P200")
    assert region["tags"] == ["P200A", "P200B", "P200C"]
    left_out = pipe_rows(network, drawing, None, services=["product"], exclude_regions=["D1"])
    assert left_out["totals"]["net_m"] == pytest.approx(19.5, abs=EPS)
    assert left_out["totals"]["excluded_m"] == pytest.approx(1.0, abs=EPS)


def test_other_services_are_left_out_unless_asked(pair):
    pid, layout, network = pair
    assert DEFAULT_SERVICES == ("product", "cip_supply", "cip_return")
    result = pipe_rows(network, pid, layout, length_source="layout", services=["steam"])
    assert result["rows"] == []
    assert result["method"]["excluded_services"] == {"product": 2}


# -- the scale rule ----------------------------------------------------------

SCHEMATIC = {
    "verdict": "schematic",
    "within_10pct": 0.25,
    "pair_count": 12,
    "median": 1.3,
    "iqr": 0.8,
    "factor": None,
    "matched": [],
    "ambiguous": [],
}


def test_auto_measures_a_schematic_pid_on_the_layout(pair):
    pid, layout, network = pair
    result = pipe_rows(network, pid, layout, scale=SCHEMATIC)
    assert result["length_source"] == "layout"
    assert result["method"]["reason"] == "auto_schematic"
    assert result["scale_verified"] is True


def test_pid_on_a_schematic_pid_is_refused_with_the_statistics(pair):
    pid, layout, network = pair
    with pytest.raises(ValueError) as excinfo:
        pipe_rows(network, pid, layout, scale=SCHEMATIC, length_source="pid")
    message = str(excinfo.value)
    assert message.startswith("length_source='pid': the scale check calls this P&ID schematic")
    assert "25% of 12 tag pairs" in message and "median ratio 1.3" in message
    assert "force=True" in message


def test_force_measures_on_the_pid_and_says_it_is_unverified(pair):
    pid, layout, network = pair
    result = pipe_rows(network, pid, layout, scale=SCHEMATIC, length_source="pid", force=True)
    assert result["length_source"] == "pid"
    assert result["method"]["reason"] == "forced"
    assert result["scale_verified"] is False
    assert run_by_tags(result, "T4100", "T4101")["length_m"] == pytest.approx(2.5, abs=EPS)
    assert any(w.startswith("scale_verified: false") for w in result["warnings"])


def test_a_to_scale_pid_is_measured_through_its_factor(pair):
    pid, layout, network = pair
    to_scale = dict(SCHEMATIC, verdict="to_scale", within_10pct=0.9, factor=0.1)
    result = pipe_rows(network, pid, layout, scale=to_scale)
    assert result["length_source"] == "pid"
    # 2.5 m drawn on the P&ID / 0.1 P&ID mm per layout mm = 25 m
    assert run_by_tags(result, "T4100", "T4101")["length_m"] == pytest.approx(25.0, abs=EPS)


def test_without_a_layout_auto_measures_the_pid_unverified(pair):
    pid, _layout, network = pair
    result = pipe_rows(network, pid, None)
    assert (result["length_source"], result["scale_verified"]) == ("pid", False)
    assert result["method"]["reason"] == "no_layout"
    with pytest.raises(ValueError, match="^length_source='layout' needs a layout drawing"):
        pipe_rows(network, pid, None, length_source="layout")


def test_scale_check_recognises_a_uniform_scale_and_a_rearranged_one():
    places = {
        "T4100": (2000.0, 3000.0),
        "T4101": (30000.0, 3000.0),
        "T4102": (30000.0, 15000.0),
        "T4103": (8000.0, 17000.0),
    }
    layout = snap(
        [text(t, t, p, 250.0) for t, p in places.items()]
        + [rec("W", "LINE", WALLS, ((0.0, 0.0), (40000.0, 20000.0)))],
        4,
        "l",
    )
    half = snap([text(t, t, (x * 0.5, y * 0.5), 50.0) for t, (x, y) in places.items()], 4, "p")
    uniform = scale_check(half, layout)
    assert uniform["verdict"] == "to_scale"
    assert uniform["factor"] == pytest.approx(0.5, abs=EPS)  # P&ID mm per layout mm
    assert uniform["pair_count"] == 6  # four tags, every pair at least 2 m apart
    shuffled = {
        "T4100": (30000.0, 15000.0),
        "T4101": (2000.0, 3000.0),
        "T4102": (8000.0, 17000.0),
        "T4103": (30000.0, 3000.0),
    }
    other = snap([text(t, t, p, 50.0) for t, p in shuffled.items()], 4, "p")
    assert scale_check(other, layout)["verdict"] == "schematic"


def test_scale_check_calls_coinciding_pid_tags_insufficient_not_to_scale():
    # every P&ID tag at one point: each ratio is 0, the median 0 - a factor of 0
    # would divide every length by zero, so the verdict is insufficient (the
    # same rule as group U's scale_check: a median that is not positive)
    places = {"T4100": (2000.0, 3000.0), "T4101": (30000.0, 3000.0), "T4102": (30000.0, 15000.0)}
    layout = snap([text(t, t, p, 250.0) for t, p in places.items()], 4, "l")
    pid = snap([text(t, t, (100.0, 100.0), 50.0) for t in places], 4, "p")
    result = scale_check(pid, layout)
    assert result["pair_count"] == 3
    assert (result["verdict"], result["factor"]) == ("insufficient", None)


def test_an_insunits_code_this_reader_does_not_convert_is_named_not_called_none():
    drawing = snap(layout_records(), 14, "layout.dxf")  # 14 = decimetres
    found = unit_of(drawing)
    assert (found["declared"], found["inferred"]) == (None, "mm")
    assert "INSUNITS declares Decimeters (14) but the geometry reads as mm" in found["warning"]
    assert "no unit" not in found["warning"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"allowance": 1.5}, "allowance: must lie between 0 and 1"),
        ({"allowance": -0.1}, "allowance: must lie between 0 and 1"),
        ({"vertical_allowance": -1.0}, "vertical_allowance: must be zero or more metres"),
        ({"length_source": "tape"}, "length_source: 'tape' unknown"),
        ({"services": ["lava"]}, "services: lava unknown"),
        ({"services": []}, "services: an empty list takes nothing off"),
    ],
)
def test_refusals_name_the_option(pair, kwargs, message):
    pid, layout, network = pair
    with pytest.raises(ValueError, match="^" + message):
        pipe_rows(network, pid, layout, **kwargs)


# -- the synthetic plant pair (Task 2) -------------------------------------------


@pytest.fixture(scope="module")
def plant(tmp_path_factory):
    truth = build_plant_pair(tmp_path_factory.mktemp("plant"))
    pid, layout = read_snapshot(truth["pid"]), read_snapshot(truth["layout"])
    return truth, pid, layout, build_network(pid)


def truth_services(truth):
    return sorted({run["service"] for run in truth["pipe_runs"]})


def test_every_synthetic_layout_length_is_the_hand_computed_rmst(plant):
    truth, pid, layout, network = plant
    result = pipe_rows(network, pid, layout, services=truth_services(truth), length_source="layout")
    runs = {(tuple(sorted(r["tags"])), r["service"]): r for r in result["runs"]}
    for expected in truth["pipe_runs"]:
        run = runs[(tuple(sorted(expected["tags"])), expected["service"])]
        assert run["status"] == expected["status"], run["id"]
        if expected["status"] != "C":
            assert run["layout_m"] * 1000.0 == pytest.approx(expected["layout_length_mm"], abs=1e-6)
            assert sum(run["rooms"].values()) == pytest.approx(run["length_m"], abs=1e-9)


def test_auto_calls_the_synthetic_pid_schematic_and_measures_the_layout(plant):
    truth, pid, layout, network = plant
    result = pipe_rows(network, pid, layout, services=truth_services(truth))
    assert result["scale"]["verdict"] == truth["scale_verdict"] == "schematic"
    assert result["length_source"] == "layout"
    with pytest.raises(ValueError, match="the scale check calls this P&ID schematic"):
        pipe_rows(network, pid, layout, services=truth_services(truth), length_source="pid")


def test_review_focus_5_the_synthetic_layout_is_read_in_millimetres(plant):
    truth, pid, layout, network = plant
    assert layout.insunits == truth["layout_insunits"] == 1
    result = pipe_rows(network, pid, layout, services=truth_services(truth), length_source="layout")
    assert result["units"]["layout"]["inferred"] == truth["layout_true_unit"] == "mm"
    assert result["units"]["layout"]["m_per_unit"] == 0.001
    assert any("INSUNITS declares Inches (1)" in w for w in result["warnings"])


def test_routed_lengths_land_in_the_synthetic_rooms(plant):
    truth, pid, layout, network = plant
    result = pipe_rows(network, pid, layout, services=truth_services(truth), length_source="layout")
    known = {str(room["number"] or room["name"]) for room in truth["rooms"]} | {""}
    for run in result["runs"]:
        assert set(run["rooms"]) <= known, run["id"]
