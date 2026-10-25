"""backends/entity_fields.py: HATCH loops, DIMENSION fields and ACADMCP XDATA (B16).

Every expected number is hand geometry: squares, a semicircle, a full circle.
"""

from __future__ import annotations

import json
import math

import ezdxf
import pytest

from backends.entity_fields import (
    acadmcp_xdata,
    dimension_fields,
    dimension_props,
    filled_area,
    hatch_loops,
    hatch_props,
    loop_ring,
    nesting_depths,
)


def _msp():
    doc = ezdxf.new()
    return doc, doc.modelspace()


def test_three_nested_squares_have_depths_0_1_2_and_fill_by_island_style():
    _doc, msp = _msp()
    hatch = msp.add_hatch()
    hatch.paths.add_polyline_path([(0, 0), (20, 0), (20, 20), (0, 20)], is_closed=True, flags=1)
    hatch.paths.add_polyline_path([(4, 4), (16, 4), (16, 16), (4, 16)], is_closed=True, flags=0)
    hatch.paths.add_polyline_path([(8, 8), (12, 8), (12, 12), (8, 12)], is_closed=True, flags=0)
    loops = hatch_loops(hatch)
    assert [loop[2] for loop in loops] == [0, 1, 2]
    # normal: 20x20 - 12x12 + 4x4; outer: 20x20 - 12x12; ignore: 20x20
    assert filled_area(loops, 0) == pytest.approx(400.0 - 144.0 + 16.0, abs=1e-9)
    assert filled_area(loops, 1) == pytest.approx(400.0 - 144.0, abs=1e-9)
    assert filled_area(loops, 2) == pytest.approx(400.0, abs=1e-9)


def test_a_bulged_outer_loop_and_a_full_circle_island_are_exact():
    _doc, msp = _msp()
    hatch = msp.add_hatch()
    # 20 x 10 rectangle whose right edge bows out as a semicircle of radius 5
    hatch.paths.add_polyline_path(
        [(0, 0, 0), (20, 0, 1.0), (20, 10, 0), (0, 10, 0)], is_closed=True, flags=1
    )
    hatch.paths.add_edge_path(flags=16).add_arc((5, 5), 2, 0, 360)
    loops = hatch_loops(hatch)
    outer, island = loops
    assert outer[0] == "polyline" and outer[2] == 0
    assert outer[4] == (0.0, 1.0, 0.0, 0.0)
    assert island[0] == "edges" and island[2] == 1
    ring, exact = loop_ring(island)
    assert exact is True
    assert ring == [(7.0, 5.0, 1.0), (3.0, 5.0, 1.0)]  # two semicircles
    expected = 200.0 + 12.5 * math.pi - 4.0 * math.pi
    assert filled_area(loops, 0) == pytest.approx(expected, abs=1e-9)


def test_an_edge_loop_of_lines_and_a_ccw_arc_is_a_bulge_ring():
    _doc, msp = _msp()
    hatch = msp.add_hatch()
    edges = hatch.paths.add_edge_path(flags=1)
    edges.add_line((30, 0), (40, 0))
    edges.add_arc((40, 5), 5, 270, 90, ccw=True)
    edges.add_line((40, 10), (30, 10))
    edges.add_line((30, 10), (30, 0))
    (loop,) = hatch_loops(hatch)
    ring, exact = loop_ring(loop)
    assert exact is True
    assert [(x, y) for x, y, _b in ring] == [(30.0, 0.0), (40.0, 0.0), (40.0, 10.0), (30.0, 10.0)]
    assert ring[1][2] == pytest.approx(1.0, abs=1e-12)  # tan(180 deg / 4)
    assert filled_area((loop,), 0) == pytest.approx(100.0 + 12.5 * math.pi, abs=1e-9)


def test_a_mirrored_hatch_reports_wcs_loops_and_the_same_area():
    _doc, msp = _msp()
    hatch = msp.add_hatch()
    hatch.dxf.extrusion = (0.0, 0.0, -1.0)  # OCS x -> WCS -x
    edges = hatch.paths.add_edge_path(flags=1)
    edges.add_line((30, 0), (40, 0))
    edges.add_arc((40, 5), 5, 270, 90, ccw=True)
    edges.add_line((40, 10), (30, 10))
    edges.add_line((30, 10), (30, 0))
    (loop,) = hatch_loops(hatch)
    first, arc = loop[3][0], loop[3][1]
    assert first == ("line", (-30.0, 0.0), (-40.0, 0.0))
    # the arc's WCS image runs counter-clockwise 90 -> 270 and is walked clockwise
    assert arc == ("arc", (-40.0, 5.0), 5.0, 90.0, 270.0, False)
    assert filled_area((loop,), 0) == pytest.approx(100.0 + 12.5 * math.pi, abs=1e-9)


def test_a_clockwise_arc_edge_gets_a_negative_bulge():
    _doc, msp = _msp()
    hatch = msp.add_hatch()
    edges = hatch.paths.add_edge_path(flags=1)
    edges.add_arc((0, 0), 10, 0, 90, ccw=False)  # walked from 90 deg back to 0 deg
    edges.add_line((10, 0), (0, 0))
    edges.add_line((0, 0), (0, 10))
    (loop,) = hatch_loops(hatch)
    ring, _exact = loop_ring(loop)
    assert ring[0][0] == pytest.approx(0.0, abs=1e-12)
    assert ring[0][1] == pytest.approx(10.0, abs=1e-12)
    assert ring[0][2] == pytest.approx(-math.tan(math.radians(90.0) / 4.0), abs=1e-12)
    # quarter disc of radius 10
    assert filled_area((loop,), 0) == pytest.approx(25.0 * math.pi, abs=1e-9)


def test_an_ellipse_edge_is_sampled_and_says_it_is_not_exact():
    _doc, msp = _msp()
    hatch = msp.add_hatch()
    hatch.paths.add_edge_path(flags=1).add_ellipse((0, 0), (10, 0), 0.5)
    (loop,) = hatch_loops(hatch)
    _ring, exact = loop_ring(loop)
    assert exact is False
    assert filled_area((loop,), 0) is None
    props = hatch_props((loop,), 0)
    assert props["area"] is None and props["area_exact"] is False
    assert props["loops"][0]["edges"][0]["type"] == "ellipse"


def test_nesting_depths_skip_loops_whose_boxes_do_not_contain():
    left = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    right = [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0)]
    inner = [(22.0, 2.0), (28.0, 2.0), (28.0, 8.0), (22.0, 8.0)]
    assert nesting_depths([left, right, inner]) == [0, 0, 1]


def test_hatch_props_names_every_field_entity_get_returns():
    _doc, msp = _msp()
    hatch = msp.add_hatch()
    hatch.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True, flags=1)
    props = hatch_props(hatch_loops(hatch), 0)
    assert props["hatch_style"] == "normal"
    assert props["loop_count"] == 1
    assert props["area"] == pytest.approx(100.0, abs=1e-9)
    assert props["area_exact"] is True
    row = props["loops"][0]
    assert row["kind"] == "polyline" and row["external"] is True and row["outermost"] is False
    assert row["depth"] == 0
    assert row["points"] == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    assert row["bulges"] == [0.0, 0.0, 0.0, 0.0]
    assert row["area"] == pytest.approx(100.0, abs=1e-9)


def test_dimension_fields_keep_measurement_override_style_and_dimlfac_apart():
    doc, msp = _msp()
    dim = msp.add_linear_dim(
        base=(0, 5), p1=(0, 0), p2=(42, 0), dimstyle="Standard", override={"dimlfac": 2.0}
    )
    dim.render()
    dim.dimension.dxf.text = "40"
    fields = dimension_fields(dim.dimension)
    assert fields == (
        0,
        ((0.0, 5.0), (0.0, 0.0), (42.0, 0.0), None, None),
        42.0,
        "40",
        "Standard",
        2.0,
        0.0,
    )
    props = dimension_props(fields)
    assert props["dimtype"] == "linear"
    assert props["text_override"] == "40"
    assert props["defpoints"][2] == [42.0, 0.0]
    assert props["defpoints"][3] is None


def test_dimlfac_falls_back_to_the_style_value():
    doc, msp = _msp()
    doc.dimstyles.get("Standard").dxf.dimlfac = 3.0
    dim = msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(10, 0), dimstyle="Standard")
    dim.render()
    assert dimension_fields(dim.dimension)[5] == 3.0


def test_a_diameter_dimension_keeps_its_two_chord_points():
    _doc, msp = _msp()
    dim = msp.add_diameter_dim(center=(0, 0), radius=5, angle=0, dimstyle="Standard")
    dim.render()
    dimtype, defpoints, measurement, override, _style, dimlfac, _angle = dimension_fields(
        dim.dimension
    )
    assert dimtype == 3
    assert defpoints[0] == (5.0, 0.0) and defpoints[3] == (-5.0, 0.0)
    assert measurement == 10.0 and override == "<>" and dimlfac == 1.0
    assert dimension_props(dimension_fields(dim.dimension))["text_override"] is None


def test_acadmcp_xdata_keeps_our_applications_only():
    doc, msp = _msp()
    for app in ("ACADMCP_MECH", "ACADMCP_ARCH_H", "OTHER_APP"):
        doc.appids.add(app)
    line = msp.add_line((0, 0), (1, 1))
    payload = {"v": 1, "kind": "feature", "part": "1A", "feature": "h1", "view": "front"}
    line.set_xdata("ACADMCP_MECH", [(1000, json.dumps(payload))])
    line.set_xdata("ACADMCP_ARCH_H", [(1000, "31"), (1000, "32")])  # joins to the number 3132
    line.set_xdata("OTHER_APP", [(1000, "x")])
    rows = dict(acadmcp_xdata(line))
    assert set(rows) == {"ACADMCP_MECH", "ACADMCP_ARCH_H"}
    assert json.loads(rows["ACADMCP_MECH"]) == payload
    assert json.loads(rows["ACADMCP_ARCH_H"]) == ["31", "32"]
    assert acadmcp_xdata(msp.add_line((0, 0), (2, 2))) == ()


def test_a_square_island_inside_a_circular_boundary_is_an_island():
    # A full-circle edge is two vertices; its chord polygon contains nothing.
    # Depth is read off the walked arc, so the square is a hole, not a second
    # outer loop: 100 pi - 16, not 100 pi + 16.
    _doc, msp = _msp()
    hatch = msp.add_hatch()
    hatch.paths.add_edge_path(flags=1).add_arc((0, 0), 10, 0, 360)
    hatch.paths.add_polyline_path([(-2, -2), (2, -2), (2, 2), (-2, 2)], is_closed=True, flags=0)
    loops = hatch_loops(hatch)
    assert [loop[2] for loop in loops] == [0, 1]
    assert filled_area(loops, 0) == pytest.approx(100.0 * math.pi - 16.0, abs=1e-9)
    assert filled_area(loops, 2) == pytest.approx(100.0 * math.pi, abs=1e-9)
