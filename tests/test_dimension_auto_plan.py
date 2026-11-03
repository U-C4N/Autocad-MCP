"""1.8-13: the ``dimension_auto`` v2 planner (``engineering/dimension_auto.py``), per type.

Pure geometry, no engine: which dimension each entity gets, drawn to which
points and standing where - outline edges chained per side, oblique edges
aligned on their outward side, diameters with the first chord point facing out
of the part box, radii at the middle of the sweep, the overall size never drawn
twice - and the refusals and ``omitted`` rows by name. The engines are
``tests/test_dimension_auto_v2.py``.
"""

from __future__ import annotations

import math

import ezdxf
import pytest

from backends.base import EntityInfo
from benchmarks.call_budget import (
    PLATE_ARCS,
    PLATE_HOLE_RADIUS,
    PLATE_HOLES,
    PLATE_OUTLINE,
    build_plate,
)
from engineering import dimension_auto as da
from engineering.critique import DIM_OVERLAP_TOL_MM
from engineering.errors import InvalidArgsError, classify

BOX = (0.0, 0.0, 120.0, 80.0)


def _info(handle, kind, layer="GEOMETRY", **props) -> EntityInfo:
    return EntityInfo(handle, kind, layer, 256, "ByLayer", True, props)


def _plate_infos() -> list[EntityInfo]:
    rows = [
        _info(
            "P1",
            "LWPOLYLINE",
            points=[list(p) for p in PLATE_OUTLINE],
            bulges=[0.0] * len(PLATE_OUTLINE),
            closed=True,
        )
    ]
    for i, (x, y) in enumerate(PLATE_HOLES):
        rows.append(_info(f"C{i}", "CIRCLE", center=[x, y], radius=PLATE_HOLE_RADIUS))
    for i, (cx, cy, r, a0, a1) in enumerate(PLATE_ARCS):
        rows.append(_info(f"A{i}", "ARC", center=[cx, cy], radius=r, start_angle=a0, end_angle=a1))
    return rows


def _plan(infos, **kwargs) -> da.Plan:
    include = da.parse_include(kwargs.pop("include", None))
    sources, omitted = da.normalize(infos)
    plan = da.plan_dimensions(sources, include=include, **kwargs)
    plan.omitted[:0] = omitted
    return plan


def _outside(point, box=BOX) -> bool:
    x0, y0, x1, y1 = box
    return not (x0 < point[0] < x1 and y0 < point[1] < y1)


# -- arguments -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("include", "match"),
    [(["linear", "angles"], "unknown kind"), ([], "name at least one")],
)
def test_include_is_checked_by_name(include, match):
    with pytest.raises(InvalidArgsError, match=match) as caught:
        da.parse_include(include)
    assert classify(caught.value)["kind"] == "invalid_args"
    assert "dimension_auto.include" in str(caught.value)


@pytest.mark.parametrize(
    ("selector", "match"),
    [
        ({"layer": "GEOMETRY", "colour": 1}, "unknown key"),
        ({}, "name at least one"),
        ([], "object"),
    ],
)
def test_the_selector_is_checked_by_name(selector, match):
    with pytest.raises(InvalidArgsError, match=match):
        da.parse_selector(selector)


def test_the_text_clearance_is_twice_the_dim_overlap_tolerance():
    assert da.CLEARANCE == 2.0 * DIM_OVERLAP_TOL_MM


# -- the planner, per type ------------------------------------------------------------


def test_a_closed_outline_is_chained_per_side_outside_the_part_box():
    plan = _plan(_plate_infos(), include=["linear"])
    edges = [d for d in plan.dims if d.feature == "edge"]
    assert len(edges) == 4 and all(d.kind == "linear" for d in edges)
    rows = {(d.rotation, d.location[1] if d.rotation == 0.0 else d.location[0]) for d in edges}
    # bottom and top rows at y = -10 / 90, left and right rows at x = -10 / 130
    assert rows == {(0.0, -10.0), (0.0, 90.0), (90.0, -10.0), (90.0, 130.0)}
    assert sorted(d.value for d in edges) == [80.0, 80.0, 120.0, 120.0]
    assert all(_outside(d.text_point) for d in edges)


def test_a_clockwise_outline_faces_the_same_way():
    cw = _info("P1", "LWPOLYLINE", points=[list(p) for p in reversed(PLATE_OUTLINE)], closed=True)
    edges = _plan([cw], include=["linear"]).dims
    assert {round(d.location[1], 6) for d in edges if d.rotation == 0.0} == {-10.0, 90.0}
    assert {round(d.location[0], 6) for d in edges if d.rotation == 90.0} == {-10.0, 130.0}


def test_an_oblique_edge_is_aligned_on_its_outward_side():
    triangle = _info("T", "LWPOLYLINE", points=[[0, 0], [40, 0], [0, 30]], closed=True)
    plan = _plan([triangle], include=["linear"])
    (slope,) = [d for d in plan.dims if d.kind == "aligned"]
    assert slope.value == pytest.approx(50.0)
    # the hypotenuse (40,0)-(0,30) faces up and right, normal (0.6, 0.8): its
    # dimension line stands 10 out from the middle (20, 15)
    assert slope.location == pytest.approx((26.0, 23.0))
    (a, b) = slope.p1, slope.p2
    left = (-(b[1] - a[1]), b[0] - a[0])
    centroid = (40.0 / 3.0, 10.0)
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    assert left[0] * (mid[0] - centroid[0]) + left[1] * (mid[1] - centroid[1]) > 0.0, (
        "the aligned dimension stands on the left of p1 -> p2, which must be outward"
    )


def test_a_bulged_segment_is_omitted_with_its_index():
    slot = _info(
        "S", "LWPOLYLINE", points=[[0, 0], [40, 0], [40, 20], [0, 20]],
        bulges=[0.0, 1.0, 0.0, 0.0], closed=True,
    )  # fmt: skip
    plan = _plan([slot], include=["linear"])
    assert [d.segment for d in plan.dims] == [0, 2, 3]
    (row,) = plan.omitted
    assert (row["handle"], row["segment"]) == ("S", 1)
    assert "arc segment" in row["reason"]


def test_circles_get_diameters_with_the_first_chord_point_facing_out():
    plan = _plan(_plate_infos(), include=["diameter"])
    diameters = [d for d in plan.dims if d.kind == "diameter"]
    assert len(diameters) == len(PLATE_HOLES)
    centre = (60.0, 40.0)
    for dim in diameters:
        c = ((dim.p1[0] + dim.p2[0]) / 2.0, (dim.p1[1] + dim.p2[1]) / 2.0)
        assert math.dist(dim.p1, dim.p2) == pytest.approx(2.0 * PLATE_HOLE_RADIUS)
        assert math.dist(dim.p1, centre) > math.dist(dim.p2, centre), "first chord point outward"
        assert dim.value == pytest.approx(2.0 * PLATE_HOLE_RADIUS)
        assert _outside(dim.text_point), (dim.source, dim.text_point)
        r = math.dist(c, dim.text_point) - PLATE_HOLE_RADIUS
        assert r == pytest.approx(dim.leader)


def test_arcs_get_radii_at_the_middle_of_their_sweep():
    plan = _plan(_plate_infos(), include=["radius"])
    radii = [d for d in plan.dims if d.kind == "radius"]
    assert [d.source for d in radii] == ["A0", "A1"]
    up, down = radii
    assert up.p1 == (40.0, 40.0) and up.p2 == pytest.approx((40.0, 48.0))
    assert down.p2 == pytest.approx((80.0, 32.0))
    assert all(d.value == 8.0 and _outside(d.text_point) for d in radii)


def test_radial_texts_are_pushed_clear_of_the_edge_rows():
    plan = _plan(_plate_infos())
    texts = [d.text_point for d in plan.dims]
    for i, a in enumerate(texts):
        for b in texts[i + 1 :]:
            assert math.dist(a, b) >= da.CLEARANCE, (a, b)


def test_the_overall_size_is_not_drawn_twice():
    plan = _plan(_plate_infos())
    assert [d for d in plan.dims if d.feature == "overall"] == []
    assert {(r["axis"], r["covered_by"]["kind"]) for r in plan.redundant} == {
        ("x", "linear"),
        ("y", "linear"),
    }
    lone = _plan([_info("C", "CIRCLE", center=[0, 0], radius=5.0)])
    assert [d.kind for d in lone.dims] == ["diameter"]
    assert {r["covered_by"]["kind"] for r in lone.redundant} == {"diameter"}


def test_a_stepped_part_gets_its_overall_size_one_row_further_out():
    # a step on both sides: no single vertical edge spans the 50 mm height
    step = _info(
        "L", "LWPOLYLINE",
        points=[[0, 0], [100, 0], [100, 30], [60, 30], [60, 50], [20, 50], [20, 20], [0, 20]],
        closed=True,
    )  # fmt: skip
    hole = _info("H", "CIRCLE", center=[40, 25], radius=4.0)
    plan = _plan([step, hole])
    overall = {d.axis: d for d in plan.dims if d.feature == "overall"}
    # the width: the 100 mm bottom edge covers it
    assert set(overall) == {"y"}
    assert [r["axis"] for r in plan.redundant] == ["x"]
    height = overall["y"]
    assert height.value == pytest.approx(50.0)
    assert height.location[0] == pytest.approx(100.0 + 2 * 10.0)  # the right row is used
    assert height.rotation == 90.0
    # drawn between real feature points, so a binder can bind it
    corners = {tuple(p) for p in step.properties["points"]}
    assert {height.p1, height.p2} <= corners


def test_group_equal_draws_one_diameter_with_the_count_read_off_the_selection():
    plan = _plan(_plate_infos(), include=["diameter"], group_equal=True)
    (dim,) = [d for d in plan.dims if d.kind == "diameter"]
    assert dim.text_override == f"{len(PLATE_HOLES)}× <>"
    assert dim.members == tuple(f"C{i}" for i in range(len(PLATE_HOLES)))
    unequal = _plate_infos() + [_info("C9", "CIRCLE", center=[60, 40], radius=10.0)]
    grouped = [d for d in _plan(unequal, group_equal=True).dims if d.kind == "diameter"]
    assert sorted(len(d.members) for d in grouped) == [1, 6]


def test_lines_alone_are_the_17_run_point_for_point():
    lines = [
        _info("L1", "LINE", start=[0, 0], end=[30, 0]),
        _info("L2", "LINE", start=[30, 0], end=[70, 0]),
    ]
    chain = _plan(lines).dims
    assert [(d.p1, d.p2, d.location, d.rotation) for d in chain] == [
        ((0.0, 0.0), (30.0, 0.0), (15.0, 10.0), 0.0),
        ((30.0, 0.0), (70.0, 0.0), (50.0, 10.0), 0.0),
    ]
    assert not _plan(lines).redundant, "no overall on a LINE-only selection (1.7 behaviour)"
    baseline = _plan(lines, style="baseline").dims
    assert [d.p1 for d in baseline] == [(0.0, 0.0), (0.0, 0.0)]


@pytest.mark.parametrize(
    ("kind", "props", "reason"),
    [
        ("ELLIPSE", {"center": [0, 0], "major_axis": [5, 0], "ratio": 0.5}, "ellipse"),
        ("SPLINE", {}, "spline"),
        ("DIMENSION", {}, "annotation"),
        ("ROTATEDDIMENSION", {}, "annotation"),
        ("TEXT", {"text": "A"}, "is not one of them"),
        ("CIRCLE", {"center": [0, 0], "plane_normal": [0, 0.7, 0.7]}, "tilted"),
    ],
)
def test_what_cannot_be_dimensioned_is_omitted_by_name(kind, props, reason):
    sources, omitted = da.normalize([_info("X", kind, **props)])
    assert sources == []
    (row,) = omitted
    assert row["handle"] == "X" and row["type"] == kind and reason in row["reason"]


def test_include_leaves_out_by_name_never_silently():
    plan = _plan(_plate_infos(), include=["radius"])
    assert [d.source for d in plan.dims] == ["A0", "A1"]
    assert {row["handle"] for row in plan.omitted} == {"P1", *(f"C{i}" for i in range(6))}
    assert all("include leaves out" in row["reason"] for row in plan.omitted)


def test_com_spellings_are_read_as_their_dxf_types():
    pline = _info("P", "POLYLINE", points=[[0, 0], [10, 0], [10, 5]], closed=False)
    sources, omitted = da.normalize([pline])
    assert [s.kind for s in sources] == ["polyline"] and omitted == []


def test_an_existing_dimension_on_the_same_feature_is_matched():
    plan = _plan(_plate_infos())
    first = {d.source: d for d in plan.dims if d.kind == "diameter"}["C0"]
    edge = [d for d in plan.dims if d.feature == "edge"][0]
    existing = [
        {"handle": "D1", "dimtype": 3, "refs": {"C0"}, "points": None, "value": 11.0},
        {
            "handle": "D2",
            "dimtype": 0,
            "refs": {"P1"},
            "points": (edge.p2, edge.p1),
            "value": 120.0,
        },
        # the same two points measured along another direction is another dimension
        {"handle": "D3", "dimtype": 0, "refs": {"P1"}, "points": (edge.p1, edge.p2), "value": 1.0},
    ]
    keep, skipped = da.match_existing(plan.dims, existing, 1e-9)
    assert first not in keep and edge not in keep
    assert {row["existing"] for row in skipped} == {"D1", "D2"}
    assert len(keep) == len(plan.dims) - 2


def test_com_the_17_names_still_parse_as_a_plate():
    # the live engine reports AcDbPolyline as POLYLINE; its circles and arcs as on DXF
    infos = _plate_infos()
    infos[0] = _info(
        "P1", "POLYLINE", points=[list(p) for p in PLATE_OUTLINE], bulges=[0.0] * 4, closed=True
    )
    plan = _plan(infos)
    assert len(plan.dims) == 12 and plan.omitted == []


def test_a_dimension_dxf_fixture_reads_back_the_same_plate(tmp_path):
    doc = ezdxf.readfile(build_plate(tmp_path / "p.dxf"))
    assert doc.header["$INSUNITS"] == 4, "the plate is a millimetre part"
