"""Every catalogue symbol: valid primitives, honest bbox, ports on the boundary."""

from __future__ import annotations

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

from engineering.pid.symbols_valves import ACTUATORS, VALVE_BODIES, build_valve  # noqa: E402


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
