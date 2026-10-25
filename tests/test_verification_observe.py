"""engineering/verification/observe.py (B16, B31): WCS features, exploded INSERTs, units.

Review Focus 6: a circle inside a mirrored, non-uniformly scaled nested block is
observed as an ellipse with the approximation note; a mirrored arc keeps its
true sweep. Every expected value is worked by hand below.
"""

from __future__ import annotations

import json
import math

import ezdxf
import pytest

from engineering.understand.snapshot import records_from_doc
from engineering.verification.observe import NON_UNIFORM, TILTED, observe


def _doc(units: int = 4):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = units
    return doc, doc.modelspace()


def _by_type(observed, kind):
    return [f for f in observed.features if f.type == kind]


def test_plain_geometry_is_observed_in_wcs_with_its_units_and_document():
    doc, msp = _doc()
    line = msp.add_line((0, 0), (30, 40))
    circle = msp.add_circle((5, 5), 2.5)
    arc = msp.add_arc((0, 0), 10, 30, 120)
    point = msp.add_point((7, 8))
    poly = msp.add_lwpolyline([(0, 0, 0, 0, 0.5), (10, 0, 0, 0, 0)], format="xyseb")
    snap = records_from_doc(doc, source="memory:t", block_defs=True)
    observed = observe(snap, document={"document_id": "abc", "revision": 3})
    assert observed.declared_unit == "mm" and observed.inferred_unit == "mm"
    assert observed.document_id == "abc" and observed.revision == 3
    assert observed.source == "memory:t"
    geometry = {f.handle: dict(f.geometry) for f in observed.features}
    assert geometry[line.dxf.handle] == {"start": (0.0, 0.0), "end": (30.0, 40.0)}
    assert geometry[circle.dxf.handle] == {"center": (5.0, 5.0), "radius": 2.5}
    arc_geometry = geometry[arc.dxf.handle]
    assert arc_geometry["center"] == (0.0, 0.0) and arc_geometry["radius"] == 10.0
    assert arc_geometry["start_angle"] == pytest.approx(30.0, abs=1e-9)
    assert arc_geometry["end_angle"] == pytest.approx(120.0, abs=1e-9)
    assert geometry[point.dxf.handle] == {"location": (7.0, 8.0)}
    assert geometry[poly.dxf.handle] == {
        "points": ((0.0, 0.0), (10.0, 0.0)),
        "bulges": (0.5, 0.0),
        "closed": False,
    }
    assert all(f.block_path == () and f.approximation is None for f in observed.features)


def test_a_circle_in_a_mirrored_non_uniformly_scaled_nested_block_is_an_ellipse():
    doc, msp = _doc()
    hole = doc.blocks.new("HOLE")
    circle = hole.add_circle((0, 0), 5)
    plate = doc.blocks.new("PLATE")
    inner = plate.add_blockref("HOLE", (10, 0), dxfattribs={"xscale": 2, "yscale": 1})
    outer = msp.add_blockref("PLATE", (100, 50), dxfattribs={"xscale": -1, "rotation": 30})
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    (feature,) = observed.features
    assert feature.type == "ellipse"
    assert feature.handle == circle.dxf.handle
    assert feature.block_path == (outer.dxf.handle, inner.dxf.handle)
    assert NON_UNIFORM in feature.approximation
    g = dict(feature.geometry)
    # M = R(30) @ diag(-1, 1) @ diag(2, 1) = R(30) @ diag(-2, 1); the circle of
    # radius 5 maps to semi-axes 10 (along R(30) @ x) and 5.
    c30, s30 = math.cos(math.radians(30.0)), math.sin(math.radians(30.0))
    # centre: (100, 50) + R(30) @ diag(-1, 1) @ (10, 0) = (100 - 10 c30, 50 - 10 s30)
    assert g["center"] == pytest.approx((100.0 - 10.0 * c30, 50.0 - 10.0 * s30), abs=1e-9)
    assert g["major_axis"] == pytest.approx((10.0 * c30, 10.0 * s30), abs=1e-9)
    assert g["ratio"] == pytest.approx(0.5, abs=1e-12)
    assert (g["start_param"], g["end_param"]) == (0.0, 2.0 * math.pi)


def test_a_mirrored_insert_keeps_the_arc_s_true_sweep():
    doc, msp = _doc()
    block = doc.blocks.new("ARCB")
    block.add_arc((0, 0), 5, 0, 90)  # counter-clockwise 0 -> 90
    by_scale = msp.add_blockref("ARCB", (100, 0), dxfattribs={"xscale": -1})
    by_normal = msp.add_blockref("ARCB", (-100, 0))  # OCS x -100 is WCS x +100
    by_normal.dxf.extrusion = (0.0, 0.0, -1.0)
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    arcs = {f.block_path: dict(f.geometry) for f in _by_type(observed, "arc")}
    # x -> -x maps 0 deg to 180 deg and 90 deg to 90 deg: the image runs
    # counter-clockwise from 90 to 180, still a quarter turn.
    for path in ((by_scale.dxf.handle,), (by_normal.dxf.handle,)):
        g = arcs[path]
        assert g["center"] == pytest.approx((100.0, 0.0), abs=1e-9)
        assert g["radius"] == pytest.approx(5.0, abs=1e-12)
        assert g["start_angle"] == pytest.approx(90.0, abs=1e-9)
        assert g["end_angle"] == pytest.approx(180.0, abs=1e-9)


def test_a_mirrored_insert_reverses_polyline_bulges():
    doc, msp = _doc()
    block = doc.blocks.new("PB")
    block.add_lwpolyline([(0, 0, 0, 0, 1.0), (10, 0, 0, 0, 0)], format="xyseb")
    msp.add_blockref("PB", (0, 0), dxfattribs={"xscale": -1})
    (feature,) = observe(records_from_doc(doc, source="t", block_defs=True)).features
    g = dict(feature.geometry)
    assert g["points"] == ((0.0, 0.0), (-10.0, 0.0))
    assert g["bulges"] == (-1.0, 0.0)
    assert feature.approximation is None


def test_a_tilted_circle_is_reported_and_marked_tilted():
    doc, msp = _doc()
    circle = msp.add_circle((0, 0), 1)
    circle.dxf.extrusion = (0.5, 0.0, -math.sqrt(3.0) / 2.0)
    (feature,) = observe(records_from_doc(doc, source="t")).features
    assert feature.type == "circle" and feature.approximation == TILTED


def test_layer_zero_inside_a_block_takes_the_insert_s_layer():
    doc, msp = _doc()
    block = doc.blocks.new("B")
    block.add_line((0, 0), (1, 0), dxfattribs={"layer": "0"})
    block.add_line((0, 1), (1, 1), dxfattribs={"layer": "OWN"})
    msp.add_blockref("B", (0, 0), dxfattribs={"layer": "PARTS"})
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    assert sorted(f.layer for f in observed.features) == ["OWN", "PARTS"]


def test_tags_dimensions_and_splines_are_observed():
    doc, msp = _doc()
    doc.appids.add("ACADMCP_MECH")
    circle = msp.add_circle((0, 0), 5)
    payload = {"v": 1, "kind": "feature", "part": "P", "feature": "bore", "view": "front"}
    circle.set_xdata("ACADMCP_MECH", [(1000, json.dumps(payload))])
    dim = msp.add_diameter_dim(center=(0, 0), radius=5, angle=0, dimstyle="Standard")
    dim.render()
    msp.add_spline(fit_points=[(0, 0), (10, 5), (20, 0)])
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    (tagged,) = _by_type(observed, "circle")
    assert json.loads(dict(tagged.tags)["ACADMCP_MECH"]) == payload
    (dimension,) = _by_type(observed, "dimension")
    g = dict(dimension.geometry)
    assert g["dimtype"] == 3 and g["measurement"] == 10.0 and g["dimlfac"] == 1.0
    assert g["defpoints"][0] == (5.0, 0.0) and g["defpoints"][3] == (-5.0, 0.0)
    (spline,) = _by_type(observed, "spline")
    assert dict(spline.geometry)["fit_points"] == ((0.0, 0.0), (10.0, 5.0), (20.0, 0.0))


def test_a_snapshot_without_block_definitions_is_refused_not_half_read():
    doc, msp = _doc()
    doc.blocks.new("B").add_line((0, 0), (1, 0))
    msp.add_blockref("B", (0, 0))
    msp.add_line((5, 5), (6, 6))
    snap = records_from_doc(doc, source="t")
    with pytest.raises(ValueError, match="block_defs=True"):
        observe(snap)
    (only,) = observe(snap, explode_inserts=False).features
    assert only.type == "line"


def test_unitless_drawing_declares_no_unit():
    doc, msp = _doc(units=0)
    msp.add_line((0, 0), (1, 0))
    assert observe(records_from_doc(doc, source="t")).declared_unit is None


def test_a_block_that_contains_itself_is_refused():
    doc, msp = _doc()
    loop = doc.blocks.new("LOOP")
    loop.add_blockref("LOOP", (1, 0))
    msp.add_blockref("LOOP", (0, 0))
    with pytest.raises(ValueError, match="contains itself"):
        observe(records_from_doc(doc, source="t", block_defs=True))


def test_layer_zero_inherits_through_a_layer_zero_nested_insert():
    # AutoCAD's rule: an entity on layer 0 takes the layer of the INSERT that
    # places it; when that INSERT is itself on 0 inside a block, the layer
    # comes from the next INSERT up.
    doc, msp = _doc()
    inner = doc.blocks.new("INNER")
    inner.add_line((0, 0), (1, 0), dxfattribs={"layer": "0"})
    outer = doc.blocks.new("OUTER")
    outer.add_blockref("INNER", (0, 0), dxfattribs={"layer": "0"})
    outer.add_blockref("INNER", (0, 5), dxfattribs={"layer": "NESTED"})
    msp.add_blockref("OUTER", (0, 0), dxfattribs={"layer": "PARTS"})
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    assert sorted(f.layer for f in observed.features) == ["NESTED", "PARTS"]
