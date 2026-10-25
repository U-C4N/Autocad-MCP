"""EntityRecord's v1.7.0 fields and Snapshot.blocks (B16): defaults, hashing, values.

Readers that ignore the new fields see no change: the defaults are empty, the
record stays hashable and diff.signature is the 1.6 tuple.
"""

from __future__ import annotations

import json
import math

import ezdxf
import pytest

from engineering.understand.diff import signature
from engineering.understand.snapshot import (
    EntityRecord,
    read_snapshot,
    records_from_doc,
    take_snapshot,
)


def _doc():
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    return doc, doc.modelspace()


def test_new_fields_default_to_nothing_and_the_record_stays_hashable():
    record = EntityRecord(
        handle="1A", type="LINE", layer="0", space="Model", points=((0.0, 0.0), (3.0, 4.0))
    )
    assert record.tilted is False
    assert record.ellipse is None
    assert record.spline is None
    assert record.hatch_loops == ()
    assert record.hatch_style == 0
    assert record.dimension is None
    assert record.transform is None
    assert record.xdata == ()
    twin = EntityRecord(
        handle="1A", type="LINE", layer="0", space="Model", points=((0.0, 0.0), (3.0, 4.0))
    )
    assert hash(record) == hash(twin)
    # diff.signature reads none of the new fields: the tuple 1.6 built
    assert signature(record) == (
        "LINE",
        "0",
        "Model",
        ((0, 0), (300, 400)),
        (),
        False,
        None,
        None,
        None,
        None,
        None,
        (),
        0,
        (100, 100),
    )


def test_a_hatch_record_carries_its_loops_in_wcs_with_depth_and_style():
    doc, msp = _doc()
    hatch = msp.add_hatch()
    hatch.dxf.hatch_style = 1
    hatch.paths.add_polyline_path(
        [(0, 0, 0), (20, 0, 1.0), (20, 10, 0), (0, 10, 0)], is_closed=True, flags=1
    )
    hatch.paths.add_edge_path(flags=16).add_arc((5, 5), 2, 0, 360)
    (record,) = records_from_doc(doc, source="t").records
    assert record.hatch_style == 1
    outer, island = record.hatch_loops
    assert outer == (
        "polyline",
        3,
        0,
        ((0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)),
        (0.0, 1.0, 0.0, 0.0),
    )
    assert island == ("edges", 16, 1, (("arc", (5.0, 5.0), 2.0, 0.0, 360.0, True),), ())


def test_a_hatch_whose_loops_cannot_be_read_does_not_fail_the_whole_snapshot(monkeypatch):
    # The loop reader is new in 1.7; a hatch it cannot read must not turn a
    # drawing every 1.6 reader handled into "not a readable DXF file".
    import engineering.understand.snapshot as snapshot_module

    def _broken(_entity):
        raise ValueError("an edge this reader does not know")

    monkeypatch.setattr(snapshot_module, "hatch_loops", _broken)
    doc, msp = _doc()
    hatch = msp.add_hatch()
    hatch.paths.add_polyline_path([(0, 0), (1, 0), (1, 1)], is_closed=True, flags=1)
    msp.add_line((0, 0), (5, 0))
    records = records_from_doc(doc, source="t").records
    (record,) = [r for r in records if r.type == "HATCH"]
    assert record.hatch_loops == ()
    assert len(records) == 2


def test_a_dimension_record_keeps_the_16_fields_and_adds_the_dimension_tuple():
    doc, msp = _doc()
    dim = msp.add_linear_dim(
        base=(0, 5), p1=(0, 0), p2=(42, 0), dimstyle="Standard", override={"dimlfac": 2.0}
    )
    dim.render()
    dim.dimension.dxf.text = "40"
    record = next(r for r in records_from_doc(doc, source="t").records if r.type == "DIMENSION")
    assert record.dimension == (
        0,
        ((0.0, 5.0), (0.0, 0.0), (42.0, 0.0), None, None),
        42.0,
        "40",
        "Standard",
        2.0,
        0.0,
    )
    assert record.text == "40"
    assert record.points[0] == (0.0, 5.0)


def test_a_mirrored_ellipse_runs_counter_clockwise_seen_from_plus_z():
    doc, msp = _doc()
    ellipse = msp.add_ellipse((0, 0), major_axis=(2, 0), ratio=0.5, start_param=0.0, end_param=1.0)
    ellipse.dxf.extrusion = (0.0, 0.0, -1.0)
    (record,) = records_from_doc(doc, source="t").records
    centre, major, ratio, start, end = record.ellipse
    assert centre == (0.0, 0.0) and major == (2.0, 0.0) and ratio == 0.5
    assert start == pytest.approx((-1.0) % math.tau, abs=1e-12)
    assert end == pytest.approx(0.0, abs=1e-12)
    # the point at the old parameter 1.0 is where the new start parameter points
    x = 2.0 * math.cos(start)
    y = 1.0 * math.sin(start)
    assert (x, y) == pytest.approx((2.0 * math.cos(1.0), -math.sin(1.0)), abs=1e-12)


def test_a_spline_record_carries_degree_points_and_closure():
    doc, msp = _doc()
    msp.add_spline(fit_points=[(0, 0), (10, 5), (20, 0)])
    (record,) = records_from_doc(doc, source="t").records
    degree, control, fit, closed = record.spline
    assert degree == 3
    assert fit == ((0.0, 0.0), (10.0, 5.0), (20.0, 0.0))
    assert closed is False
    assert record.points == control


def test_tilted_is_set_only_off_the_wcs_xy_plane():
    doc, msp = _doc()
    flat = msp.add_circle((0, 0), 1)
    mirrored = msp.add_circle((5, 0), 1)
    mirrored.dxf.extrusion = (0.0, 0.0, -1.0)
    tilted = msp.add_circle((10, 0), 1)
    tilted.dxf.extrusion = (0.5, 0.0, -math.sqrt(3.0) / 2.0)
    by_handle = {r.handle: r for r in records_from_doc(doc, source="t").records}
    assert by_handle[flat.dxf.handle].tilted is False
    assert by_handle[mirrored.dxf.handle].tilted is False
    assert by_handle[tilted.dxf.handle].tilted is True


def test_an_insert_record_carries_its_block_to_wcs_affine():
    doc, msp = _doc()
    block = doc.blocks.new("B", base_point=(1, 2))
    block.add_circle((3, 2), 1)
    msp.add_blockref("B", (10, 10), dxfattribs={"xscale": 2, "yscale": -1, "rotation": 30})
    (record,) = records_from_doc(doc, source="t").records
    c, s = math.cos(math.radians(30.0)), math.sin(math.radians(30.0))
    xscale, yscale = 2.0, -1.0
    a, b = xscale * c, xscale * s  # R(30) @ (xscale, 0)
    cc, d = -yscale * s, yscale * c  # R(30) @ (0, yscale)
    e = 10.0 - (a * 1.0 + cc * 2.0)  # insert - M @ base point
    f = 10.0 - (b * 1.0 + d * 2.0)
    assert record.transform == pytest.approx((a, b, cc, d, e, f), abs=1e-9)


def test_xdata_of_our_applications_rides_on_the_record():
    doc, msp = _doc()
    doc.appids.add("ACADMCP_MECH")
    line = msp.add_line((0, 0), (1, 0))
    payload = {"v": 1, "kind": "feature", "part": "P", "feature": "f", "view": "front"}
    line.set_xdata("ACADMCP_MECH", [(1000, json.dumps(payload))])
    (record,) = records_from_doc(doc, source="t").records
    ((app, text),) = record.xdata
    assert app == "ACADMCP_MECH" and json.loads(text) == payload


def test_block_definitions_are_read_only_on_request_and_never_enter_records():
    doc, msp = _doc()
    block = doc.blocks.new("HOLE")
    block.add_circle((0, 0), 5)
    block.add_attdef("TAG", (0, 0))
    msp.add_blockref("HOLE", (10, 0))
    dim = msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(10, 0), dimstyle="Standard")
    dim.render()
    plain = records_from_doc(doc, source="t")
    full = records_from_doc(doc, source="t", block_defs=True)
    assert plain.blocks is None
    assert full.records == plain.records
    assert "HOLE" in full.blocks
    # no layout block, no *D block holding the dimension's rendering
    assert not [n for n in full.blocks if n.upper().startswith(("*D", "*MODEL", "*PAPER"))]
    (circle,) = full.blocks["HOLE"]
    assert circle.type == "CIRCLE" and circle.space == "HOLE" and circle.radius == 5.0


def test_read_snapshot_passes_block_defs_through(tmp_path):
    doc, msp = _doc()
    doc.blocks.new("B").add_line((0, 0), (1, 0))
    msp.add_blockref("B", (0, 0))
    path = tmp_path / "blocks.dxf"
    doc.saveas(path)
    assert read_snapshot(str(path)).blocks is None
    assert "B" in read_snapshot(str(path), block_defs=True).blocks


@pytest.mark.asyncio
async def test_take_snapshot_passes_block_defs_through_on_the_headless_engine(backend):
    backend._doc.blocks.new("B").add_line((0, 0), (1, 0))
    await backend.entity_create_block_ref("B", 0.0, 0.0)
    snap = await take_snapshot(backend, block_defs=True)
    assert "B" in snap.blocks
    assert (await take_snapshot(backend)).blocks is None
