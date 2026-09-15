"""Every catalogue symbol: valid primitives, honest bbox, ports on the boundary."""

from __future__ import annotations

import json
import math
import re

import pytest

from backends.block_specs import validate_attdef_specs, validate_entity_specs
from engineering.pid.geometry import bbox_of
from engineering.pid.symbols import (
    BUBBLE_RADIUS,
    CATALOG_VERSION,
    INSTRUMENT_LOCATIONS,
    INSTRUMENT_TYPES,
    Port,
    all_specs,
    list_symbols,
    resolve,
    transform_port,
)

NAME_RE = re.compile(r"^PID_[A-Z0-9_]+$")
SOURCE_RE = re.compile(r"^(ISO 10628-2|ISA-5\.1|PIP PIC001)")
EPS = 1e-6


def _ids():
    return [spec.name for spec in all_specs()]


@pytest.mark.parametrize("spec", all_specs(), ids=_ids())
def test_primitives_and_attdefs_validate(spec):
    validate_entity_specs(list(spec.primitives))
    validate_attdef_specs(list(spec.attdefs))


@pytest.mark.parametrize("spec", all_specs(), ids=_ids())
def test_declared_bbox_contains_every_primitive(spec):
    xmin, ymin, xmax, ymax = bbox_of(spec.primitives)
    dxmin, dymin, dxmax, dymax = spec.bbox
    assert dxmin - EPS <= xmin and dymin - EPS <= ymin
    assert xmax <= dxmax + EPS and ymax <= dymax + EPS
    assert dxmax > dxmin and dymax > dymin


@pytest.mark.parametrize("spec", all_specs(), ids=_ids())
def test_ports_sit_on_the_boundary_or_at_the_centre(spec):
    if spec.family == "marker":
        assert spec.ports == ()
        return
    assert spec.ports, "every non-marker symbol needs at least one port"
    xmin, ymin, xmax, ymax = spec.bbox
    for port in spec.ports:
        if port.direction_deg is None:
            assert port.radius > 0 and (port.x, port.y) == (0.0, 0.0)
            continue
        on_edge = (
            abs(port.x - xmin) < EPS
            or abs(port.x - xmax) < EPS
            or abs(port.y - ymin) < EPS
            or abs(port.y - ymax) < EPS
        )
        assert on_edge, (
            f"{spec.name}.{port.name} at ({port.x}, {port.y}) is inside bbox {spec.bbox}"
        )
        assert port.direction_deg in (0.0, 90.0, 180.0, 270.0)
        assert port.kind in ("process", "signal")


@pytest.mark.parametrize("spec", all_specs(), ids=_ids())
def test_naming_source_and_layer(spec):
    assert NAME_RE.match(spec.name)
    assert SOURCE_RE.match(spec.source), spec.source
    assert spec.layer_class in (
        "PROCESS-VALVES",
        "PROCESS-EQUIPMENT",
        "INSTRUMENT-SYMBOL",
        "line",
    )
    tags = [a["tag"] for a in spec.attdefs]
    assert len(tags) == len(set(tags))
    for prim in spec.primitives:
        assert prim.get("layer", "0") == "0"


def test_catalogue_names_are_unique_and_versioned():
    names = [s.name for s in all_specs()]
    assert len(names) == len(set(names))
    assert CATALOG_VERSION == "1"


def test_instrument_generator_covers_four_types_by_five_locations():
    specs = {
        (t, loc): resolve("instrument", type=t, location=loc)
        for t in INSTRUMENT_TYPES
        for loc in INSTRUMENT_LOCATIONS
    }
    assert len(specs) == 20
    field = specs[("discrete", "field")]
    assert field.name == "PID_INST_DISCRETE_FIELD"
    assert [a["tag"] for a in field.attdefs] == ["FUNC", "LOOP"]
    assert field.ports == (Port("signal", 0.0, 0.0, None, "signal", BUBBLE_RADIUS),)
    assert sum(1 for p in field.primitives if p["type"] == "circle") == 1
    primary = specs[("discrete", "primary")]
    assert sum(1 for p in primary.primitives if p["type"] == "line") == 1
    aux_rear = specs[("plc", "auxiliary_rear")]
    assert sum(1 for p in aux_rear.primitives if p["type"] == "line") > 2, "dashed pair"
    assert sum(1 for p in aux_rear.primitives if p["type"] == "polyline") == 2, "square + diamond"
    hexagon = next(p for p in specs[("computer", "field")].primitives if p["type"] == "polyline")
    assert len(hexagon["points"]) == 6


def test_instrument_outline_per_type_follows_isa_table_5_4_1():
    """Spec 4.4: discrete = circle; dcs = circle inside a square; computer = hexagon;
    plc = diamond inside a square -- no circle in the last two."""
    outline = {
        t: [p["type"] for p in resolve("instrument", type=t, location="field").primitives]
        for t in INSTRUMENT_TYPES
    }
    assert outline == {
        "discrete": ["circle"],
        "dcs": ["circle", "polyline"],
        "computer": ["polyline"],
        "plc": ["polyline", "polyline"],
    }
    plc = resolve("instrument", type="plc", location="field").primitives
    assert sorted(len(p["points"]) for p in plc) == [4, 4], "square + diamond"


def test_instrument_location_lines_end_on_the_outline():
    """A location line must not poke outside the bubble outline: chord of the circle,
    full width of the square, clipped to the flat-topped hexagon's slanted sides."""
    r = BUBBLE_RADIUS
    half_width = {
        "discrete": lambda y: math.sqrt(r * r - y * y),
        "dcs": lambda y: r,
        "plc": lambda y: r,
        "computer": lambda y: r - abs(y) / math.tan(math.radians(60.0)),
    }
    for t in INSTRUMENT_TYPES:
        for loc in INSTRUMENT_LOCATIONS:
            spec = resolve("instrument", type=t, location=loc)
            lines = [p for p in spec.primitives if p["type"] == "line"]
            ys = {p["y1"] for p in lines}
            expected_ys = {"field": set(), "primary": {0.0}, "auxiliary": {0.8, -0.8}}[
                loc.removesuffix("_rear")
            ]
            assert ys == expected_ys, (t, loc)
            for p in lines:
                assert p["y1"] == p["y2"], (t, loc)
                hw = half_width[t](p["y1"])
                assert -hw - 1e-9 <= min(p["x1"], p["x2"]), (t, loc, p)
                assert max(p["x1"], p["x2"]) <= hw + 1e-9, (t, loc, p)
            # a solid line spans the whole outline (not merely stays inside it); a dashed
            # one starts on it and its last dash may stop short of the far side
            if lines:
                hw = half_width[t](next(iter(ys)))
                assert min(p["x1"] for p in lines) == pytest.approx(-hw)
                if not loc.endswith("_rear"):
                    assert max(p["x2"] for p in lines) == pytest.approx(hw)


def test_unknown_symbol_or_option_names_the_valid_values():
    with pytest.raises(ValueError, match="instrument"):
        resolve("thermometer")
    with pytest.raises(ValueError, match="field"):
        resolve("instrument", type="dcs", location="ceiling")


def test_markers_and_connectors():
    arrow = resolve("arrow_flow")
    assert arrow.family == "marker" and arrow.layer_class == "line"
    assert any(p["type"] == "solid" for p in arrow.primitives)
    for which in ("mark_pneumatic", "mark_capillary", "mark_hydraulic", "mark_data"):
        assert resolve(which).family == "marker"
    out = resolve("offpage", direction="out")
    inn = resolve("offpage", direction="in")
    assert out.name == "PID_CONNECTOR_OFFPAGE_OUT" and inn.name == "PID_CONNECTOR_OFFPAGE_IN"
    assert out.ports[0] == Port("process", -8.0, 0.0, 180.0, "process")
    assert inn.ports[0] == Port("process", 8.0, 0.0, 0.0, "process")
    assert [a["tag"] for a in out.attdefs] == ["TAG", "LINK"]
    assert next(a for a in out.attdefs if a["tag"] == "LINK")["invisible"] is True


def test_transform_port_applies_rotation_and_scale_about_the_insertion():
    port = Port("out", 4.0, 0.0, 0.0, "process")
    moved = transform_port(port, 100.0, 50.0, 90.0, 2.0)
    assert moved["x"] == pytest.approx(100.0) and moved["y"] == pytest.approx(58.0)
    assert moved["direction_deg"] == pytest.approx(90.0)
    radial = transform_port(Port("signal", 0.0, 0.0, None, "signal", 5.0), 10.0, 10.0, 45.0, 3.0)
    assert radial["direction_deg"] is None and radial["radius"] == pytest.approx(15.0)


def test_list_symbols_reports_variants_and_ports():
    rows = {row["symbol"]: row for row in list_symbols()}
    assert rows["instrument"]["variants"] == {
        "type": list(INSTRUMENT_TYPES),
        "location": list(INSTRUMENT_LOCATIONS),
    }
    assert rows["offpage"]["variants"] == {"direction": ["in", "out"]}
    assert rows["arrow_flow"]["ports"] == []
    assert list_symbols(family="marker") and all(
        r["family"] == "marker" for r in list_symbols(family="marker")
    )


# ── valves (Task 8) ──────────────────────────────────────────────────────────

from engineering.pid.symbols_valves import (  # noqa: E402
    ACTUATORS,
    NO_ACTUATOR,
    STEM_CLEARANCE,
    VALVE_BODIES,
    build_valve,
)


def test_valve_bodies_and_actuators_enumerate():
    assert len(VALVE_BODIES) == 11 and len(ACTUATORS) == 6
    gate = build_valve("gate")
    assert gate.name == "PID_VALVE_GATE" and gate.bbox == (-4.0, -2.0, 4.0, 2.0)
    assert {p.name: (p.x, p.y, p.direction_deg) for p in gate.ports} == {
        "in": (-4.0, 0.0, 180.0),
        "out": (4.0, 0.0, 0.0),
    }
    assert [a["tag"] for a in gate.attdefs] == ["TAG", "DESC"]


def test_actuators_add_a_signal_port_and_a_fail_attribute():
    cv = build_valve("globe", "diaphragm")
    assert cv.name == "PID_VALVE_GLOBE_DIAPHRAGM" and cv.variant == "diaphragm"
    signal = next(p for p in cv.ports if p.name == "signal")
    assert (signal.x, signal.y, signal.direction_deg, signal.kind) == (0.0, 9.0, 90.0, "signal")
    assert cv.bbox[3] == 9.0
    assert "FAIL" in [a["tag"] for a in cv.attdefs]
    hand = build_valve("gate", "hand")
    assert all(p.name != "signal" for p in hand.ports) and "FAIL" not in [
        a["tag"] for a in hand.attdefs
    ]


def test_check_and_relief_refuse_actuators():
    for body in ("check", "relief"):
        with pytest.raises(ValueError, match="actuator"):
            build_valve(body, "motor")


def test_three_way_angle_and_relief_ports():
    assert {p.name for p in build_valve("three_way").ports} == {"in", "out", "branch"}
    angle = build_valve("angle")
    assert {p.name: p.direction_deg for p in angle.ports} == {"in": 180.0, "out": 270.0}
    relief = build_valve("relief")
    assert {p.name: p.direction_deg for p in relief.ports} == {"in": 270.0, "out": 0.0}


def _axis_top(primitives) -> float:
    """Highest y at which a body's own geometry touches the x = 0 axis."""
    ys = [0.0]
    for p in primitives:
        kind = p["type"]
        if kind == "line" and p["x1"] == 0.0 == p["x2"]:
            ys += [p["y1"], p["y2"]]
        elif kind == "circle" and p["cx"] == 0.0:
            ys.append(p["cy"] + p["r"])
        elif kind == "arc" and p["cx"] == 0.0:
            if (90.0 - p["start_deg"]) % 360.0 <= (p["end_deg"] - p["start_deg"]) % 360.0:
                ys.append(p["cy"] + p["r"])
        elif kind == "polyline":
            pts = list(p["points"]) + ([p["points"][0]] if p["closed"] else [])
            for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
                if x1 == 0.0 == x2:
                    ys += [y1, y2]
                elif min(x1, x2) <= 0.0 <= max(x1, x2):
                    ys.append(y1 + (y2 - y1) * (0.0 - x1) / (x2 - x1))
    return max(ys)


def _actuated():
    return [
        (body, actuator)
        for body in VALVE_BODIES
        if body not in NO_ACTUATOR
        for actuator in ACTUATORS
        if actuator != "none"
    ]


@pytest.mark.parametrize("body,actuator", _actuated(), ids=lambda v: v)
def test_actuator_glyph_is_connected_to_its_stem_and_clear_of_the_body(body, actuator):
    bare = build_valve(body)
    composed = build_valve(body, actuator)
    extra = list(composed.primitives[len(bare.primitives) :])
    assert composed.primitives[: len(bare.primitives)] == bare.primitives
    stem, glyph = extra[0], extra[1:]
    assert stem["type"] == "line" and stem["x1"] == 0.0 == stem["x2"]
    stem_bottom, stem_top = sorted((stem["y1"], stem["y2"]))
    # The stem starts where the body's own axial geometry ends — never
    # coincident with a body line (butterfly disc, needle stem) or inside a
    # body circle / dome.
    assert stem_bottom == pytest.approx(_axis_top(bare.primitives))
    # The glyph's lowest edge sits on the stem's top (no floating dome) ...
    assert bbox_of(glyph)[1] == pytest.approx(stem_top)
    # ... and the whole glyph is above the body, with the standard clearance.
    assert bbox_of(glyph)[1] >= bare.bbox[3] + STEM_CLEARANCE - EPS
    # The FAIL attribute sits beside the glyph, not on the body.
    if actuator != "hand":
        fail = next(a for a in composed.attdefs if a["tag"] == "FAIL")
        assert fail["y"] > bare.bbox[3]
    if actuator != "hand":
        signal = next(p for p in composed.ports if p.name == "signal")
        assert signal.y == pytest.approx(composed.bbox[3])


def test_hand_actuator_has_no_fail_attribute_or_signal_port():
    for body in VALVE_BODIES:
        if body in NO_ACTUATOR:
            continue
        hand = build_valve(body, "hand")
        assert all(a["tag"] != "FAIL" for a in hand.attdefs)
        assert all(p.name != "signal" for p in hand.ports)


@pytest.mark.parametrize("spec", all_specs(), ids=_ids())
def test_no_duplicate_primitives(spec):
    seen = [json.dumps(p, sort_keys=True) for p in spec.primitives]
    assert len(seen) == len(set(seen)), "duplicate overlapping geometry inside the block"


# ── rotating equipment, heat transfer, inline miscellany (Task 9) ────────────

from engineering.pid.symbols_equipment import HEAT, MISC, ROTATING  # noqa: E402


def test_equipment_families_register():
    assert len(ROTATING) == 6 and len(HEAT) == 4 and len(MISC) == 7
    pump = resolve("centrifugal_pump")
    assert pump.family == "rotating" and pump.layer_class == "PROCESS-EQUIPMENT"
    assert {p.name: p.direction_deg for p in pump.ports} == {"suction": 180.0, "discharge": 90.0}
    hx = resolve("shell_tube_hx")
    assert {p.name for p in hx.ports} == {"shell_in", "shell_out", "tube_in", "tube_out"}
    orifice = resolve("orifice_plate")
    assert next(p for p in orifice.ports if p.name == "signal").kind == "signal"
    ecc = resolve("reducer", shape="eccentric")
    assert ecc.name == "PID_MISC_REDUCER_ECCENTRIC"
    assert next(p for p in ecc.ports if p.name == "out").y == pytest.approx(-1.0)
    assert resolve("reducer").name == "PID_MISC_REDUCER_CONCENTRIC"
    assert resolve("agitator").ports[0] == Port("shaft", 0.0, 0.0, 270.0, "process")


# ── parametric vessels (Task 10) ─────────────────────────────────────────────

from engineering.pid.symbols_vessels import (  # noqa: E402
    VESSELS,
    build_vessel,
    validate_vessel_params,
)


def test_vessels_are_parametric_and_hash_named():
    assert len(VESSELS) == 6
    v = build_vessel("vertical_vessel")
    assert v.parametric is True and v.name.startswith("PID_VESSEL_VERTICAL_VESSEL_")
    assert len(v.name.rsplit("_", 1)[1]) == 8
    assert {p.name: p.direction_deg for p in v.ports} == {
        "N1": 90.0,
        "N2": 270.0,
        "N3": 180.0,
        "N4": 0.0,
    }
    same = build_vessel("vertical_vessel", {"width": 20, "height": 40})
    assert same.name == v.name, "identical parameters share one definition"
    other = build_vessel("vertical_vessel", {"width": 30})
    assert other.name != v.name


def test_vessel_nozzles_are_where_the_parameters_say():
    v = build_vessel(
        "vertical_vessel",
        {
            "width": 20,
            "height": 40,
            "nozzles": [{"name": "F", "side": "right", "fraction": 0.25}],
        },
    )
    port = v.ports[0]
    assert (port.name, port.x, port.y, port.direction_deg) == ("F", 13.0, -10.0, 0.0)
    assert v.bbox == (-10.0, -20.0, 13.0, 20.0)


def test_vessel_variants_draw_their_features():
    column = build_vessel("column", {"trays": 5})
    assert sum(1 for p in column.primitives if p["type"] == "line") >= 5 + 2
    cone = build_vessel("tank", {"roof": "cone"})
    assert any(p["type"] == "polyline" and not p["closed"] for p in cone.primitives)
    jacketed = build_vessel("reactor", {"jacketed": True})
    assert jacketed.bbox[2] == pytest.approx(12.0 + 5.0), "half width 12 + jacket 2 + stub 3"
    hopper = build_vessel("hopper")
    assert {p.direction_deg for p in hopper.ports} == {90.0, 270.0}


@pytest.mark.parametrize(
    "params, key",
    [
        ({"width": 0}, "width"),
        ({"height": -1}, "height"),
        ({"nozzles": [{"name": "A", "side": "up", "fraction": 0.5}]}, "side"),
        ({"nozzles": [{"name": "A", "side": "top", "fraction": 1.5}]}, "fraction"),
        (
            {
                "nozzles": [
                    {"name": "A", "side": "top", "fraction": 0.5},
                    {"name": "A", "side": "top", "fraction": 0.2},
                ]
            },
            "name",
        ),
        ({"trays": -2}, "trays"),
        ({"roof": "dome"}, "roof"),
        ({"bogus": 1}, "bogus"),
    ],
)
def test_vessel_params_are_validated(params, key):
    symbol = "column" if "trays" in params else ("tank" if "roof" in params else "vertical_vessel")
    with pytest.raises(ValueError, match=key):
        validate_vessel_params(symbol, params)


def _distance_to_outline(x: float, y: float, primitives) -> float:
    """Exact distance from a point to the nearest drawn line, arc or polyline edge."""

    def to_segment(x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        if dx == 0.0 == dy:
            return math.hypot(x - x1, y - y1)
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))

    best = math.inf
    for p in primitives:
        kind = p["type"]
        if kind == "line":
            best = min(best, to_segment(p["x1"], p["y1"], p["x2"], p["y2"]))
        elif kind == "arc":
            cx, cy, r = p["cx"], p["cy"], p["r"]
            sweep = (p["end_deg"] - p["start_deg"]) % 360.0 or 360.0
            rel = (math.degrees(math.atan2(y - cy, x - cx)) - p["start_deg"]) % 360.0
            if rel <= sweep + 1e-9:
                best = min(best, abs(math.hypot(x - cx, y - cy) - r))
            for deg in (p["start_deg"], p["end_deg"]):
                a = math.radians(deg)
                best = min(best, math.hypot(x - (cx + r * math.cos(a)), y - (cy + r * math.sin(a))))
        elif kind == "polyline":
            pts = list(p["points"]) + ([p["points"][0]] if p["closed"] else [])
            for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
                best = min(best, to_segment(x1, y1, x2, y2))
    return best


def _nozzle_stubs(spec):
    """(stub line, port) pairs — the stubs are the last primitives, one per port."""
    stubs = list(spec.primitives[-len(spec.ports) :])
    return list(zip(stubs, spec.ports, strict=True))


def _vessel_fuzz():
    fractions = (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)
    nozzles = [
        {"name": f"{side[0].upper()}{i}", "side": side, "fraction": f}
        for side in ("top", "bottom", "left", "right")
        for i, f in enumerate(fractions)
    ]
    cases = []
    for symbol in VESSELS:
        for w in (4.0, 10.0, 20.0, 40.0):
            for h in (2.0, 10.0, 25.0, 40.0):
                for extra in ({}, {"roof": "cone"}, {"jacketed": True}, {"trays": 3}):
                    params = {"width": w, "height": h, "nozzles": nozzles, **extra}
                    try:
                        validate_vessel_params(symbol, params)
                    except ValueError:
                        continue
                    cases.append(pytest.param(symbol, params, id=f"{symbol}-{w:g}x{h:g}-{extra}"))
    return cases


@pytest.mark.parametrize("symbol, params", _vessel_fuzz())
def test_every_accepted_vessel_shape_is_drawn_inside_its_bbox_with_stubs_on_the_wall(
    symbol, params
):
    spec = build_vessel(symbol, params)
    # The shell comes first: two walls + two heads, a box (+ cone roof), or the hopper.
    n_shell = {"tank": 2 if params.get("roof") == "cone" else 1, "hopper": 1}.get(symbol, 4)
    body = list(spec.primitives[:n_shell])
    xmin, ymin, xmax, ymax = bbox_of(spec.primitives)
    dxmin, dymin, dxmax, dymax = spec.bbox
    assert dxmin - EPS <= xmin and dymin - EPS <= ymin
    assert xmax <= dxmax + EPS and ymax <= dymax + EPS
    for stub, port in _nozzle_stubs(spec):
        assert stub["type"] == "line"
        outer = {(stub["x1"], stub["y1"]), (stub["x2"], stub["y2"])}
        assert (port.x, port.y) in outer, f"{port.name}: stub does not end at its port"
        (ix, iy) = next(pt for pt in outer if pt != (port.x, port.y))
        assert _distance_to_outline(ix, iy, body) < 1e-6, (
            f"{symbol} {params['width']:g}x{params['height']:g} {port.name}: stub inner end "
            f"({ix:.3f}, {iy:.3f}) floats {_distance_to_outline(ix, iy, body):.3f} mm off the outline"
        )
        assert math.hypot(port.x - ix, port.y - iy) >= 3.0 - EPS, "stub shorter than STUB"


def test_default_vessel_stubs_start_on_the_dished_head_not_the_bbox():
    # horizontal_vessel N3 (left, 0.7): y = 4 on a head of r = 10 centred (-10, 0).
    n3 = next(s for s, p in _nozzle_stubs(build_vessel("horizontal_vessel")) if p.name == "N3")
    assert n3["y1"] == n3["y2"] == pytest.approx(4.0)
    assert min(n3["x1"], n3["x2"]) == pytest.approx(-23.0), "port stays STUB outside the bbox"
    assert max(n3["x1"], n3["x2"]) == pytest.approx(-10.0 - math.sqrt(100.0 - 16.0))
    # reactor N3/N4 land on the heads too (|y| = 7.2 > cylindrical half-length 6).
    for stub, port in _nozzle_stubs(build_vessel("reactor")):
        if port.name in ("N3", "N4"):
            inner = min(abs(stub["x1"]), abs(stub["x2"]))
            assert inner == pytest.approx(math.sqrt(144.0 - (7.2 - 6.0) ** 2))
    # A jacketed reactor's stub passes through the jacket to the vessel wall.
    n1 = next(
        s for s, p in _nozzle_stubs(build_vessel("reactor", {"jacketed": True})) if p.name == "N1"
    )
    assert sorted((n1["y1"], n1["y2"])) == [pytest.approx(18.0), pytest.approx(23.0)]
    # Hopper side nozzle meets the sloped wall; cone-roof top nozzle meets the roof.
    hopper = build_vessel("hopper", {"nozzles": [{"name": "L", "side": "left", "fraction": 0.5}]})
    assert max(hopper.primitives[-1]["x1"], hopper.primitives[-1]["x2"]) == pytest.approx(-8.0)
    cone = build_vessel(
        "tank", {"roof": "cone", "nozzles": [{"name": "T", "side": "top", "fraction": 0.25}]}
    )
    assert min(cone.primitives[-1]["y1"], cone.primitives[-1]["y2"]) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "symbol, params, key",
    [
        ("vertical_vessel", {"width": 20, "height": 5}, "height"),
        ("vertical_vessel", {"width": 20, "height": 20}, "height"),
        ("column", {"width": 16, "height": 10}, "height"),
        ("reactor", {"width": 24, "height": 12}, "height"),
        ("horizontal_vessel", {"width": 20, "height": 40}, "width"),
        ("tank", {"roof": "cone", "width": 24, "height": 4}, "height"),
        (
            "vertical_vessel",
            {"nozzles": [{"name": "F", "side": "right", "fractoin": 0.25}]},
            "fractoin",
        ),
        (
            "hopper",
            {"nozzles": [{"name": "A", "side": "top", "fraction": 0.5, "colour": 1}]},
            r"nozzles\[0\]",
        ),
    ],
)
def test_vessel_shapes_that_cannot_be_drawn_and_nozzle_typos_are_refused(symbol, params, key):
    with pytest.raises(ValueError, match=key):
        validate_vessel_params(symbol, params)
    # Flat-roof and hopper proportions are free; the capsule limit is strict.
    assert validate_vessel_params("tank", {"width": 24, "height": 4})["height"] == 4.0
    assert validate_vessel_params("hopper", {"width": 40, "height": 4})["height"] == 4.0
