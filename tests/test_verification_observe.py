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
from engineering.verification.binding import bind, feature_of
from engineering.verification.measure import evaluate
from engineering.verification.observe import NON_UNIFORM, TILTED, observe
from engineering.verification.requirements import parse_requirements


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


# ── MINSERT: every cell of the grid is geometry ─────────────────────────────


def _minsert(
    doc,
    layout,
    *,
    at=(10, 10),
    rows=1,
    columns=4,
    row_spacing=0.0,
    column_spacing=20.0,
    rotation=0.0,
):
    if "HOLE" not in doc.blocks:
        doc.blocks.new("HOLE").add_circle((0, 0), 5)
    grid = layout.add_blockref("HOLE", at, dxfattribs={"rotation": rotation})
    grid.dxf.row_count = rows
    grid.dxf.column_count = columns
    grid.dxf.row_spacing = row_spacing
    grid.dxf.column_spacing = column_spacing
    return grid


def _evaluated(observed, items):
    """``{id: Measurement}`` for raw requirement items, parsed, bound and measured."""
    rs = parse_requirements({"units": "mm", "items": items})
    bindings = {b.requirement_id: b for b in bind(rs, observed)}
    return {r.id: evaluate(r, bindings[r.id], observed, units=rs.units) for r in rs.requirements}


def _circles(quantity, target=None, rid="n", **predicate):
    item = {
        "id": rid,
        "quantity": quantity,
        "features": [{"kind": "predicate", "type": "circle", **predicate}],
        "provenance": "explicit",
        "source": {"document": "t"},
    }
    if target is not None:
        item["target"] = target
    return item


def test_a_minsert_is_exploded_once_per_cell():
    # A 1 x 4 grid at (10, 10), columns 20 apart: holes at x = 10, 30, 50, 70
    # (the copies ezdxf's own multi_insert() yields).
    doc, msp = _doc()
    grid = _minsert(doc, msp)
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    circles = sorted(_by_type(observed, "circle"), key=lambda f: dict(f.geometry)["center"])
    assert [dict(f.geometry)["center"] for f in circles] == [
        (10.0, 10.0),
        (30.0, 10.0),
        (50.0, 10.0),
        (70.0, 10.0),
    ]
    h = grid.dxf.handle
    assert [f.block_path for f in circles] == [(f"{h}[0,{c}]",) for c in range(4)]
    # a 1.6 reader still sees one INSERT record at the MINSERT's own placement
    (record,) = [r for r in records_from_doc(doc, source="t").records if r.type == "INSERT"]
    assert record.transform == (1.0, 0.0, 0.0, 1.0, 10.0, 10.0)
    assert len(record.cells) == 4


def test_a_minsert_s_copies_fail_a_count_and_an_absence_they_would_have_passed():
    doc, msp = _doc()
    _minsert(doc, msp)
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    m = _evaluated(
        observed,
        [
            _circles("count", {"exact": 1}, rid="one"),
            _circles("count", {"exact": 4}, rid="four"),
            # the copies at x = 50 and x = 70 lie in the region
            _circles("absence", rid="none", region=[50, 0, 100, 40]),
        ],
    )
    assert (m["one"].status, m["one"].value) == ("fail", 4.0)
    assert (m["four"].status, m["four"].value) == ("pass", 4.0)
    assert (m["none"].status, m["none"].value) == ("fail", 2.0)


def test_a_rotated_two_by_two_minsert_turns_its_grid():
    # rotation 90: a column step of 20 runs along +y, a row step of 30 along -x
    # (the grid offset is rotated, not scaled - ezdxf's multi_insert rule).
    doc, msp = _doc()
    grid = _minsert(
        doc,
        msp,
        at=(0, 0),
        rows=2,
        columns=2,
        row_spacing=30.0,
        column_spacing=20.0,
        rotation=90.0,
    )
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    h = grid.dxf.handle
    centres = {
        f.block_path[0]: tuple(round(v, 9) + 0.0 for v in dict(f.geometry)["center"])
        for f in _by_type(observed, "circle")
    }
    assert centres == {
        f"{h}[0,0]": (0.0, 0.0),
        f"{h}[0,1]": (0.0, 20.0),
        f"{h}[1,0]": (-30.0, 0.0),
        f"{h}[1,1]": (-30.0, 20.0),
    }


def test_a_minsert_with_zero_spacing_places_one_copy():
    # ezdxf's mcount rule: a zero spacing repeats one location, yielded once
    doc, msp = _doc()
    _minsert(doc, msp, columns=4, column_spacing=0.0)
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    assert len(_by_type(observed, "circle")) == 1


def test_a_minsert_nested_in_a_block_is_exploded_per_cell_under_its_insert():
    doc, msp = _doc()
    plate = doc.blocks.new("PLATE")
    nested = _minsert(doc, plate, at=(0, 0), columns=3, column_spacing=10.0)
    outer = msp.add_blockref("PLATE", (100, 0))
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    circles = sorted(_by_type(observed, "circle"), key=lambda f: dict(f.geometry)["center"])
    assert [dict(f.geometry)["center"] for f in circles] == [
        (100.0, 0.0),
        (110.0, 0.0),
        (120.0, 0.0),
    ]
    n = nested.dxf.handle
    assert [f.block_path for f in circles] == [(outer.dxf.handle, f"{n}[0,{c}]") for c in range(3)]


def test_one_minsert_cell_is_bound_by_handle_and_its_cell_path():
    doc, msp = _doc()
    grid = _minsert(doc, msp)
    circle_handle = next(iter(doc.blocks.get("HOLE"))).dxf.handle
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    rs = parse_requirements(
        {
            "units": "mm",
            "items": [
                {
                    "id": "third",
                    "quantity": "radius",
                    "target": {"nominal": 5, "unit": "mm"},
                    # lower case on purpose: the parser normalises the handle part
                    "features": [
                        {
                            "kind": "handle",
                            "handle": circle_handle,
                            "block_path": [f"{grid.dxf.handle.lower()}[0,2]"],
                        }
                    ],
                    "provenance": "explicit",
                    "source": {"document": "t"},
                }
            ],
        }
    )
    (binding,) = bind(rs, observed)
    assert binding.status == "bound"
    assert dict(feature_of(binding.refs[0], observed).geometry)["center"] == (50.0, 10.0)


# ── units: the header is the claim the artifact is held to ──────────────────


def _plate(scale: float = 1.0):
    """A 100 x 60 plate (x ``scale``) whose corners are R25 quarter arcs, a
    R10 hole at its centre and a 3.5 text, declared in millimetres."""
    doc = ezdxf.new(setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    s = scale
    msp.add_line((25 * s, 0), (75 * s, 0))
    msp.add_line((100 * s, 25 * s), (100 * s, 35 * s))
    msp.add_line((75 * s, 60 * s), (25 * s, 60 * s))
    msp.add_line((0, 35 * s), (0, 25 * s))
    msp.add_arc((75 * s, 25 * s), 25 * s, 270, 360)
    msp.add_arc((75 * s, 35 * s), 25 * s, 0, 90)
    msp.add_arc((25 * s, 35 * s), 25 * s, 90, 180)
    msp.add_arc((25 * s, 25 * s), 25 * s, 180, 270)
    msp.add_circle((50 * s, 30 * s), 10 * s)
    msp.add_text("PLATE", dxfattribs={"height": 3.5, "insert": (5 * s, 5 * s)})
    return doc


def _plate_items(scale: float = 1.0):
    s = scale
    return [
        _circles("radius", {"nominal": 10, "unit": "mm"}, rid="hole"),
        {
            "id": "width",
            "quantity": "distance",
            "target": {"nominal": 60, "unit": "mm"},
            "features": [
                {"kind": "predicate", "type": "line", "region": [20 * s, -1, 80 * s, 1]},
                {
                    "kind": "predicate",
                    "type": "line",
                    "region": [20 * s, 59 * s - 1, 80 * s, 60 * s + 1],
                },
            ],
            "provenance": "explicit",
            "source": {"document": "t"},
        },
    ]


def test_a_millimetre_plate_with_quarter_fillets_is_measured_in_millimetres():
    # infer_units reads a 25 mm quarter arc as a door swing of 25 in = 0.635 m
    # and outweighs the header; the verification holds the drawing to its
    # declared mm (it used to report 254 mm and 1524 mm: x 25.4).
    observed = observe(records_from_doc(_plate(), source="p", block_defs=True))
    assert (observed.declared_unit, observed.inferred_unit) == ("mm", "in")
    m = _evaluated(observed, _plate_items())
    for rid, value in (("hole", 10.0), ("width", 60.0)):
        assert (m[rid].status, m[rid].unit) == ("pass", "mm")
        assert m[rid].value == pytest.approx(value, abs=1e-9)
        detail = dict(m[rid].detail)
        assert detail["drawing_unit"] == "mm" and detail["unit_source"] == "declared"
        assert detail["inferred_unit"] == "in"  # the disagreement is recorded, not applied


def test_a_millimetre_plate_drawn_25_4_times_too_small_fails():
    # the same plate drawn 1 / 25.4 of its size under a mm header: the hole is
    # R0.3937 mm and the width 2.3622 mm - a fail, never a pass rescued by an
    # inferred inch
    observed = observe(records_from_doc(_plate(1 / 25.4), source="p", block_defs=True))
    m = _evaluated(observed, _plate_items(1 / 25.4))
    assert m["hole"].status == "fail" and m["hole"].value == pytest.approx(10 / 25.4, abs=1e-9)
    assert m["width"].status == "fail" and m["width"].value == pytest.approx(60 / 25.4, abs=1e-9)
