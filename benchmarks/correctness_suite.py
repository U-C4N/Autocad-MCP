"""Version-agnostic correctness checks for the AutoCAD MCP ezdxf backend.

Driven by ``compare_versions.py`` (the A/B runner). Run ONE check in isolation:

    python benchmarks/correctness_suite.py <check_name>
    python benchmarks/correctness_suite.py --list

Prints 'PASS' or 'FAIL' on the last line; a crash (e.g. SIGSEGV) or an
unsupported method => non-zero exit, which the runner counts as a miss.

The same file is executed against both the baseline worktree and the current
checkout (PYTHONPATH + cwd point at each repo), so a check that exercises a
method the old version lacks simply raises AttributeError -> miss.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile


async def _b():
    from backends.ezdxf_backend import EzdxfBackend

    be = EzdxfBackend()
    await be.connect()
    await be.drawing_new()
    return be


# ── Core capability (expected to pass on BOTH versions — sanity / not rigged) ──


async def core_line_length():
    b = await _b()
    ln = await b.entity_create_line(0, 0, 3, 4)
    info = await b.entity_get(ln.handle)
    return abs(float(info.properties["length"]) - 5.0) < 1e-6


async def core_circle_radius():
    b = await _b()
    c = await b.entity_create_circle(0, 0, 10)
    info = await b.entity_get(c.handle)
    return abs(float(info.properties["radius"]) - 10.0) < 1e-6


async def core_layer_create():
    b = await _b()
    await b.layer_create("BENCH", color=1)
    names = {lyr.name for lyr in await b.layer_list()}
    return "BENCH" in names


async def core_polyline_closed():
    b = await _b()
    p = await b.entity_create_polyline([[0, 0], [10, 0], [10, 10]], closed=True)
    info = await b.entity_get(p.handle)
    return bool(info.properties.get("closed"))


async def core_linear_dim():
    b = await _b()
    d = await b.dimension_linear(0, 0, 100, 0, 50, 20)
    info = await b.entity_get(d.handle)
    return "DIM" in info.type.upper()


async def core_save_dxf_roundtrip():
    b = await _b()
    await b.entity_create_line(0, 0, 10, 10)
    path = os.path.join(tempfile.mkdtemp(), "rt.dxf")
    await b.drawing_save_as(path, "dxf")
    import ezdxf

    ezdxf.readfile(path)  # raises if not a valid DXF
    return os.path.getsize(path) > 0


# ── Dimensions (I16) ──────────────────────────────────────────────────────────


async def dim_aligned_no_error():
    b = await _b()
    d = await b.dimension_aligned(0, 0, 50, 50, 30, 70)
    return d is not None


async def dim_angular_no_error():
    b = await _b()
    d = await b.dimension_angular(0, 0, 100, 0, 0, 100, 60, 60)
    return d is not None


# ── Polar array full circle (R9) ──────────────────────────────────────────────


async def array_polar_360_distinct():
    b = await _b()
    c = await b.entity_create_circle(10, 0, 1)
    res = await b.entity_array_polar(c.handle, 4, 360.0, 0, 0)
    # 3 copies + original = 4 distinct centers, none coincident with the original.
    centers = {(10.0, 0.0)}
    for e in res:
        info = await b.entity_get(e.handle if hasattr(e, "handle") else e)
        cx, cy = info.properties["center"]
        centers.add((round(cx, 3), round(cy, 3)))
    return len(centers) == 4


# ── Deterministic geometry (I9) ────────────────────────────────────────────────


async def point_intersection_line_line():
    b = await _b()
    l1 = await b.entity_create_line(0, 0, 10, 0)
    l2 = await b.entity_create_line(5, -5, 5, 5)
    pt = await b.point_intersection(l1.handle, l2.handle)
    return abs(pt[0] - 5.0) < 1e-6 and abs(pt[1] - 0.0) < 1e-6


async def point_tangent_external():
    b = await _b()
    c = await b.entity_create_circle(0, 0, 3)
    tx, ty = await b.point_tangent(c.handle, 5, 0, ref_y=10)
    # tangency: (T-F).(T-C) == 0 and |T-C| == r
    fx, fy = 5.0, 0.0
    dot = (tx - fx) * (tx - 0) + (ty - fy) * (ty - 0)
    return abs(dot) < 1e-4 and abs(math.hypot(tx, ty) - 3.0) < 1e-4


# ── Selection / property parity (N3, N5) ───────────────────────────────────────


async def arc_has_length():
    b = await _b()
    a = await b.entity_create_arc(0, 0, 10, 0, 90)
    info = await b.entity_get(a.handle)
    return abs(float(info.properties["length"]) - (10 * math.pi / 2)) < 1e-3


async def arc_select_by_length():
    b = await _b()
    await b.entity_create_arc(0, 0, 10, 0, 90)  # length ~15.7
    sel = await b.entity_select_smart({"type": "ARC", "length_range": [15, 16]})
    return len(sel) == 1


async def ezdxf_bounding_box():
    b = await _b()
    ln = await b.entity_create_line(0, 0, 30, 40)
    info = await b.entity_get(ln.handle)
    bb = info.properties.get("bounding_box")
    return isinstance(bb, dict) and set(bb) == {"min", "max"}


async def mtext_rotation_roundtrip():
    b = await _b()
    mt = await b.entity_create_mtext("r", 0, 0, width=50, height=2.5, rotation=30.0)
    info = await b.entity_get(mt.handle)
    return abs(float(info.properties.get("rotation", 0.0)) - 30.0) < 1e-6


# ── Rendering (screenshot Agg / SIGSEGV) ───────────────────────────────────────


async def screenshot_png():
    b = await _b()
    await b.entity_create_line(0, 0, 100, 100)
    png = await b.view_screenshot()
    return png is not None and png[:4] == b"\x89PNG"


# ── Quality gate / critique (R4, N4) ───────────────────────────────────────────


async def dim_overlap_critique_fires():
    b = await _b()
    await b.drawing_apply_iso_layers("mech")
    await b.dimension_linear(0, 0, 100, 0, 50, 20, layer="DIM")
    await b.dimension_linear(0, 0, 100, 0, 50, 20, layer="DIM")
    issues = await b.drawing_critique(focus=["dim_overlap"])
    return len(issues) >= 1


async def iso13567_dim_layer():
    b = await _b()
    await b.drawing_apply_iso_layers("iso13567")
    ln = await b.entity_create_line(0, 0, 80, 0, layer="M-GEOMET-E-N")
    dims = await b.dimension_auto([ln.handle], style="chain")
    return dims[0].layer == "M-DIMEN-T-N"


async def construction_left_iso_caught():
    b = await _b()
    await b.drawing_apply_iso_layers("iso13567")
    await b.construction_xline(0, 0, 90)
    issues = await b.drawing_critique(focus=["construction_left"])
    return len(issues) == 1


# ── Engineering (gear) ──────────────────────────────────────────────────────────


async def gear_no_self_overlap():
    from engineering.gear import generate_full_gear_outline

    pts = generate_full_gear_outline(2, 50, 20.0)  # z=50 -> root_r >= base_r
    root_r = (2 * 50 / 2.0) - 1.25 * 2
    return min(math.hypot(x, y) for x, y in pts) >= root_r - 1e-6


# ── Linetype on demand (R24) ───────────────────────────────────────────────────


async def center_linetype_applied():
    b = await _b()
    ln = await b.entity_create_line(0, 0, 50, 0, linetype="CENTER")
    info = await b.entity_get(ln.handle)
    return info.linetype.upper() == "CENTER"


# ── Measurement (v1.5.0) ───────────────────────────────────────────────────────
#
# These three are NEW capability, not repaired regressions: v1.4.0 has no
# `entity_measure` and no boundary tracing, so it misses them by not having the
# method rather than by getting it wrong. They are here because all three broke
# during v1.5.0 development and nothing outside their own unit tests would have
# noticed.


async def hatch_area_subtracts_its_island():
    """A section view is mostly holes; counting them is a 33% error here."""
    b = await _b()
    h = await b.entity_create_hatch("ANSI31", [[0, 0], [20, 0], [20, 20], [0, 20]])
    await b.hatch_add_boundary(
        h.handle,
        [
            {"type": "line", "start": [5, 5], "end": [15, 5]},
            {"type": "line", "start": [15, 5], "end": [15, 15]},
            {"type": "line", "start": [15, 15], "end": [5, 15]},
            {"type": "line", "start": [5, 15], "end": [5, 5]},
        ],
    )
    return abs((await b.entity_measure(h.handle))["area"] - 300.0) < 1e-6


async def boundary_area_agrees_with_measure():
    """Two tools, one shape, one number."""
    b = await _b()
    await b.entity_create_line(0, 0, 10, 0)
    await b.entity_create_line(10, 0, 10, 10)
    await b.entity_create_line(0, 10, 0, 0)
    await b.entity_create_arc(5, 10, 5, 0, 180)
    loop = await b.boundary_trace(5.0, 5.0)
    measured = await b.entity_measure(loop["handle"])
    true_area = 100.0 + math.pi * 25.0 / 2.0
    return abs(loop["area"] - measured["area"]) < 1e-9 and abs(measured["area"] - true_area) < 1e-5


async def two_vertex_circle_has_area():
    """DXF's most compact circle. The shoelace over its two points is 0."""
    from engineering.measure import polygon_area_perimeter

    area, _ = polygon_area_perimeter([(-10.0, 0.0, 1.0), (10.0, 0.0, 1.0)], closed=True)
    return abs(area - math.pi * 100.0) < 1e-9


async def diameter_dim_measures_the_diameter():
    """The number on the callout is the whole point of the callout.

    `add_diameter_dim` measures to `mpoint`; the leader is a text placement.
    Feeding it centre + (radius + leader) made every diameter come back
    2 x leader_length too large at DEFAULT settings - 60 on a true 40.
    """
    b = await _b()
    info = await b.dimension_diameter(-20, 0, 20, 0)
    dim = b._doc.entitydb.get(info.handle)
    return abs(dim.get_measurement() - 40.0) < 1e-6


async def radius_dim_ignores_the_leader_length():
    b = await _b()
    near = await b.dimension_radius(0, 0, 20, 0, leader_length=5)
    far = await b.dimension_radius(0, 0, 20, 0, leader_length=40)
    values = [
        b._doc.entitydb.get(near.handle).get_measurement(),
        b._doc.entitydb.get(far.handle).get_measurement(),
    ]
    return all(abs(v - 20.0) < 1e-6 for v in values)


# ── P&ID (v1.6, track A) ──────────────────────────────────────────────────────


async def pid_block_define_attdef_roundtrip():
    b = await _b()
    await b.block_define(
        "PID_T",
        [{"type": "line", "x1": 0, "y1": 0, "x2": 8, "y2": 0}],
        [{"tag": "TAG", "x": 0, "y": 3, "height": 2.5}],
    )
    ref = await b.block_insert("PID_T", 5, 5, attributes={"TAG": "HV-1"})
    return await b.block_get_attributes(ref.handle) == {"TAG": "HV-1"}


async def pid_tag_parse_fic():
    from engineering.pid.tags import parse_tag

    return parse_tag("FIC-101")["description"] == "Flow Indicating Controller"


async def pid_graph_edge_count():
    from engineering.pid.spec import EXAMPLE_SPEC, run_spec

    b = await _b()
    result = await run_spec(b, EXAMPLE_SPEC)
    return len(result["graph"]["edges"]) == 4 and result["graph"]["stats"]["dangling"] == 0


# ── Settings and environment (v1.6, track E) ─────────────────────────────────


async def settings_dimstyle_iso25_values():
    """The ISO-25 preset reaches the DIMSTYLE table with ISO 129-1's numbers.

    Read back through ezdxf directly, not through `dimstyle_list`, so the
    check cannot be satisfied by a list that echoes its own input.
    """
    from engineering.standards.dimstyles import resolve_dimstyle

    b = await _b()
    await b.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None), set_current=True)
    style = b._doc.dimstyles.get("ISO-25")
    listed = {row["name"]: row for row in await b.dimstyle_list()}
    return (
        abs(float(style.dxf.dimtxt) - 2.5) < 1e-9
        and abs(float(style.dxf.dimasz) - 2.5) < 1e-9
        and abs(float(style.dxf.dimexe) - 1.25) < 1e-9
        and abs(float(style.dxf.dimexo) - 0.625) < 1e-9
        and abs(float(style.dxf.dimgap) - 0.625) < 1e-9
        and int(style.dxf.dimtad) == 1
        and int(style.dxf.dimdsep) == ord(",")
        and str(style.dxf.dimtxsty) == "ISOCP"
        and b._doc.header["$DIMSTYLE"] == "ISO-25"
        and listed["ISO-25"]["current"] is True
        and listed["ISO-25"]["values"]["DIMDSEP"] == ","
    )


async def settings_layer_state_roundtrip():
    """Save → change → restore puts the table back, and the state survives a
    save/reopen because it is an XRECORD in the file, not server memory."""
    b = await _b()
    await b.layer_create("LS_A", color=1)
    await b.layer_create("LS_B", color=2)
    saved = await b.layer_state_save("bench", description="A/B round trip")
    await b.layer_modify("LS_A", color=5)
    await b.layer_freeze("LS_B")
    restored = await b.layer_state_restore("bench")
    layers = {layer.name: layer for layer in await b.layer_list()}
    path = os.path.join(tempfile.mkdtemp(), "ls.dxf")
    await b.drawing_save_as(path, "dxf")
    await b.drawing_open(path)
    names_after_reopen = {row["name"] for row in await b.layer_state_list()}
    return (
        saved["ok"] is True
        and restored["missing_layers"] == []
        and restored["new_layers"] == []
        and layers["LS_A"].color == 1
        and layers["LS_B"].is_frozen is False
        and "bench" in names_after_reopen
    )


def _sheet_near(size_mm, width_mm: float, height_mm: float) -> bool:
    """Within the spec's ±0.5 mm: COM measures ANSI B as 431.8 × 279.4."""
    try:
        w, h = (float(v) for v in size_mm)
    except (TypeError, ValueError):
        return False
    return abs(w - width_mm) < 0.5 and abs(h - height_mm) < 0.5


async def settings_pdf_mediabox_a3():
    """Page setup evidence is the PDF's own /MediaBox (spec §8.3).

    A fresh document's Layout1 is already ISO A3 landscape, so an A3-only
    read-back would pass with the setter stubbed out. The sheet is therefore
    moved to ANSI B first — 432 × 279, a size no fresh document has (spec §9
    names it) — and that PDF is read; then ISO A3 is applied and read again.
    The A3 apply must also report the layout moved from B to A3, so the PDF
    and the setter's own diff have to agree on where the sheet was.
    """
    from engineering.standards.papers import resolve_page_setup
    from engineering.standards.plot import batch_plot

    b = await _b()
    await b.page_setup_apply("Layout1", resolve_page_setup("ANSI_B"))
    out_dir = tempfile.mkdtemp()
    report_b = await batch_plot(b, ["Layout1"], out_dir, "{drawing}-{layout}-ansi_b.pdf")
    width_b, height_b = report_b["sheets"][0]["mediabox_mm"]
    applied = await b.page_setup_apply("Layout1", resolve_page_setup("ISO_A3"))
    report_a3 = await batch_plot(b, ["Layout1"], out_dir, "{drawing}-{layout}-iso_a3.pdf")
    width_a3, height_a3 = report_a3["sheets"][0]["mediabox_mm"]
    moved = applied["changed"].get("size_mm") or [[0.0, 0.0], [0.0, 0.0]]
    return (
        abs(width_b - 432.0) < 0.5
        and abs(height_b - 279.0) < 0.5
        and _sheet_near(moved[0], 432.0, 279.0)
        and _sheet_near(moved[1], 420.0, 297.0)
        and abs(width_a3 - 420.0) < 0.5
        and abs(height_a3 - 297.0) < 0.5
    )


# ── Mechanical parts and the sheet (tracks B + G) ───────────────────────────


def _field(row: dict, *names):
    """A row's value under whichever of these names it carries.

    The parts-list column spelling belongs to the sheet module; what this
    suite pins is the numbers, so it reads by meaning rather than by name.
    """
    for name in names:
        if name in row:
            return row[name]
    return None


def _inserted_handle(result: dict):
    return result.get("handle") or (result.get("handles") or [None])[0]


async def mech_part_roundtrip():
    """Draw a part, read it back from its own ACADMCP_MECH XDATA, compare.

    Segments and material only: a feature's parameter spelling belongs to
    `engineering/mech/features.py` and its own tests, and a check that guessed
    at it would fail for the wrong reason.
    """
    from engineering.mech.draw import draw_part, read_part
    from engineering.mech.part import build_part, part_to_dict

    b = await _b()
    spec = {
        "kind": "revolved",
        "name": "SHAFT",
        "material": "steel",
        "segments": [
            {"length": 40.0, "d_outer": 30.0},
            {"length": 20.0, "d_outer": 50.0, "d_inner": 10.0},
        ],
    }
    part = build_part(spec)
    drawn = await draw_part(b, part, at=(0.0, 0.0), views=("front",), dimension=False)
    back = await read_part(b, drawn["part_id"])
    return part_to_dict(build_part(back["part"])) == part_to_dict(part)


async def mech_section_hatch_area():
    """The hatched cut area of a full section, measured against a hand number.

    A plain sleeve: length 60, outside diameter 40, bore 20. A full
    longitudinal section cuts two faces, each 60 x 10, so the hatch fills
    60 * (40 - 20) = 1200 mm^2 exactly. The number is read out of the drawing
    through entity_measure (the tool's analysis_measure_entity), never from the
    code that drew it — which is why it cannot be faked.
    """
    from engineering.mech.draw import add_view, draw_part
    from engineering.mech.part import build_part

    b = await _b()
    part = build_part(
        {
            "kind": "revolved",
            "name": "SLEEVE",
            "material": "steel",
            "segments": [{"length": 60.0, "d_outer": 40.0, "d_inner": 20.0}],
        }
    )
    drawn = await draw_part(b, part, at=(0.0, 0.0), views=("front",), dimension=False)
    await add_view(
        b,
        drawn["part_id"],
        "section",
        plane={"p1": [0.0, 0.0], "p2": [60.0, 0.0], "label": "A"},
        style="full",
    )
    total = 0.0
    for ent in await b.entity_list(type_filter="HATCH", limit=500):
        total += float((await b.entity_measure(ent.handle))["area"])
    return abs(total - 1200.0) < 0.01


async def mech_iso286_on_dimension():
    """A 40 H7 bore dimension carries ISO 286's +0.025 / 0 through the layout.

    ISO 286-1 table 1: the 30-50 mm step has IT7 = 25 um, and H fixes the lower
    deviation at 0. The fit has to survive `layout_dimensions`, or the number
    is right in the table and wrong on the sheet.
    """
    from engineering.fits import fit_lookup
    from engineering.mech.dimension import layout_dimensions
    from engineering.mech.primitives import Circle, DimIntent
    from engineering.mech.views import View

    deviation = fit_lookup("H7", 40.0)
    if abs(deviation.upper_mm - 0.025) > 1e-9 or abs(deviation.lower_mm) > 1e-9:
        return False
    intent = DimIntent("diameter", (-20.0, 0.0), (20.0, 0.0), fit="H7", feature="bore")
    view = View(
        kind="front",
        prims=(Circle((0.0, 0.0), 20.0),),
        dims=(intent,),
        omitted=(),
        bbox=(-20.0, -20.0, 20.0, 20.0),
    )
    placed = layout_dimensions(view, [intent])
    return len(placed) == 1 and placed[0].fit == "H7" and placed[0].feature == "bore"


async def mech_thread_unrepresented_is_caught():
    """The ISO 6410 focus fires on a thread drawn as a plain circle.

    A quality gate that never fires is not a gate; this is the mechanical twin
    of `dim_overlap_critique_fires`.
    """
    from engineering.mech.critique import build_index, issues_for
    from engineering.mech.xdata import APP_ID, to_values

    b = await _b()
    payload = {
        "v": 1,
        "kind": "part",
        "id": "P1",
        "view": "side",
        "part": {
            "name": "STUD",
            "material": "steel",
            "segments": [{"length": 60.0, "d_outer": 20.0}],
            "features": [{"kind": "thread", "id": "t1", "designation": "M20x1.5"}],
        },
    }
    anchor = await b.entity_create_circle(0.0, 0.0, 0.5, layer="0")
    await b.entity_set_xdata(anchor.handle, APP_ID, to_values(payload))
    await b.entity_create_circle(0.0, 0.0, 10.0, layer="GEOMETRY")
    before = issues_for("mech_thread_unrepresented", await build_index(b))
    await b.entity_create_arc(0.0, 0.0, 8.0, 0.0, 270.0, layer="GEOMETRY")
    after = issues_for("mech_thread_unrepresented", await build_index(b))
    return len(before) == 1 and after == []


async def sheet_frame_iso5457_a3():
    """ISO 5457 A3: 420 x 297 trimmed, a 20 mm filing margin on the left and
    10 mm on the other three edges, so the frame runs (20, 10) to (410, 287)
    and the drawing space is 390 x 277. The trimmed sheet edge is drawn too
    (ISO 5457 4.1), so the outermost extent is the sheet, 0,0 to 420,297 -
    and nothing may reach past it."""
    from engineering.mech.primitives import points_of
    from engineering.sheet.frames import SHEETS, frame_prims

    if tuple(float(v) for v in SHEETS["A3"]) != (297.0, 420.0):
        return False
    prims = frame_prims("A3", orientation="landscape", zones=False, marks=False)
    points = {
        (round(float(x), 6), round(float(y), 6)) for prim in prims for x, y in points_of(prim)
    }
    frame = {(20.0, 10.0), (410.0, 10.0), (410.0, 287.0), (20.0, 287.0)}
    sheet = {(0.0, 0.0), (420.0, 0.0), (420.0, 297.0), (0.0, 297.0)}
    xs = [x for x, _y in points]
    ys = [y for _x, y in points]
    extent = (min(xs), min(ys), max(xs), max(ys))
    return frame <= points and sheet <= points and extent == (0.0, 0.0, 420.0, 297.0)


async def bom_balloon_link():
    """Two identical bolts collapse into one row of quantity 2, the item number
    is stable across a rerun, and the balloon drawn for that row carries it."""
    from engineering.mech.stdparts import insert_std_part
    from engineering.mech.xdata import APP_ID, decode
    from engineering.sheet.bom import balloon_prims, rows_from_records

    b = await _b()
    records = []
    for x in (0.0, 60.0):
        result = await insert_std_part(b, "ISO 4014 - M12x60", at=(x, 0.0), view="side")
        raw = await b.entity_get_xdata(_inserted_handle(result), APP_ID)
        records.append(decode(raw["xdata"][APP_ID]))
    rows = rows_from_records(records)
    again = rows_from_records(records)
    if len(rows) != 1 or len(again) != 1:
        return False
    item = _field(rows[0], "item", "no", "pos")
    if item is None or item != _field(again[0], "item", "no", "pos"):
        return False
    if int(_field(rows[0], "qty", "quantity") or 0) != 2:
        return False
    texts = [
        p.text for p in balloon_prims(int(item), (0.0, 40.0), (0.0, 5.0)) if hasattr(p, "text")
    ]
    return texts == [str(int(item))]


async def std_part_iso4014_m12():
    """ISO 4014's M12 row and the block it becomes.

    ISO 4014:2011 table 1, M12: width across flats s = 18 mm, head height
    k = 7.5 mm. The row is matched by value rather than by column name — the
    spelling is the catalogue's business, the numbers are the standard's — and
    the inserted block has to carry its designation back in ACADMCP_MECH.
    The catalogue nests a row's sizes one level down (``dims``), so the
    values are read one level deep.
    """
    from engineering.mech.stdparts import catalogue, insert_std_part
    from engineering.mech.xdata import APP_ID, decode

    rows = [row for row in catalogue("M12") if any("4014" in str(value) for value in row.values())]
    if not rows:
        return False
    values = [
        inner
        for row in rows
        for value in row.values()
        for inner in (value.values() if isinstance(value, dict) else (value,))
    ]
    numbers = {
        round(float(value), 3)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if not {18.0, 7.5} <= numbers:
        return False
    b = await _b()
    result = await insert_std_part(b, "ISO 4014 - M12x60", at=(0.0, 0.0), view="side")
    raw = await b.entity_get_xdata(_inserted_handle(result), APP_ID)
    payload = decode(raw["xdata"][APP_ID])
    return payload["kind"] == "std_part" and "M12" in str(payload["designation"])


CHECKS = {
    "core_line_length": (core_line_length, "Core"),
    "core_circle_radius": (core_circle_radius, "Core"),
    "core_layer_create": (core_layer_create, "Core"),
    "core_polyline_closed": (core_polyline_closed, "Core"),
    "core_linear_dim": (core_linear_dim, "Core"),
    "core_save_dxf_roundtrip": (core_save_dxf_roundtrip, "Core"),
    "dim_aligned_no_error": (dim_aligned_no_error, "Dimensions"),
    "dim_angular_no_error": (dim_angular_no_error, "Dimensions"),
    "array_polar_360_distinct": (array_polar_360_distinct, "Modify"),
    "point_intersection_line_line": (point_intersection_line_line, "Geometry"),
    "point_tangent_external": (point_tangent_external, "Geometry"),
    "arc_has_length": (arc_has_length, "Query"),
    "arc_select_by_length": (arc_select_by_length, "Query"),
    "ezdxf_bounding_box": (ezdxf_bounding_box, "Query"),
    "mtext_rotation_roundtrip": (mtext_rotation_roundtrip, "Entities"),
    "screenshot_png": (screenshot_png, "Render"),
    "dim_overlap_critique_fires": (dim_overlap_critique_fires, "Quality gate"),
    "iso13567_dim_layer": (iso13567_dim_layer, "Quality gate"),
    "construction_left_iso_caught": (construction_left_iso_caught, "Quality gate"),
    "gear_no_self_overlap": (gear_no_self_overlap, "Engineering"),
    "center_linetype_applied": (center_linetype_applied, "Entities"),
    "hatch_area_subtracts_its_island": (hatch_area_subtracts_its_island, "Measurement"),
    "boundary_area_agrees_with_measure": (boundary_area_agrees_with_measure, "Measurement"),
    "two_vertex_circle_has_area": (two_vertex_circle_has_area, "Measurement"),
    "diameter_dim_measures_the_diameter": (diameter_dim_measures_the_diameter, "Dimensions"),
    "radius_dim_ignores_the_leader_length": (radius_dim_ignores_the_leader_length, "Dimensions"),
    "pid_block_define_attdef_roundtrip": (pid_block_define_attdef_roundtrip, "P&ID"),
    "pid_tag_parse_fic": (pid_tag_parse_fic, "P&ID"),
    "pid_graph_edge_count": (pid_graph_edge_count, "P&ID"),
    "settings_dimstyle_iso25_values": (settings_dimstyle_iso25_values, "Settings"),
    "settings_layer_state_roundtrip": (settings_layer_state_roundtrip, "Settings"),
    "settings_pdf_mediabox_a3": (settings_pdf_mediabox_a3, "Settings"),
    "mech_part_roundtrip": (mech_part_roundtrip, "Mechanical"),
    "mech_section_hatch_area": (mech_section_hatch_area, "Mechanical"),
    "mech_iso286_on_dimension": (mech_iso286_on_dimension, "Mechanical"),
    "mech_thread_unrepresented_is_caught": (mech_thread_unrepresented_is_caught, "Mechanical"),
    "std_part_iso4014_m12": (std_part_iso4014_m12, "Mechanical"),
    "sheet_frame_iso5457_a3": (sheet_frame_iso5457_a3, "Sheet"),
    "bom_balloon_link": (bom_balloon_link, "Sheet"),
}


def main():
    if len(sys.argv) == 1 or sys.argv[1] in {"-h", "--help"}:
        print("usage: correctness_suite.py <check_name> | --list")
        print("Run one deterministic correctness check, or list available checks.")
        return
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        for k, (_fn, cat) in CHECKS.items():
            print(f"{k}\t{cat}")
        return
    name = sys.argv[1]
    if name not in CHECKS:
        print(f"Unknown check: {name}", file=sys.stderr)
        raise SystemExit(2)
    fn, _cat = CHECKS[name]
    ok = asyncio.run(fn())
    print("PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
