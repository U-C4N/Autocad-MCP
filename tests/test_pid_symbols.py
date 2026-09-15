"""Every catalogue symbol: valid primitives, honest bbox, ports on the boundary."""

from __future__ import annotations

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
