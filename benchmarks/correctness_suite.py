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
