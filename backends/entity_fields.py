"""Exact HATCH loops, DIMENSION fields and ACADMCP_* XDATA, read once for both engines.

Both engines answer ``entity_get`` for a HATCH or a DIMENSION from this one
reader: the headless engine runs it on the live ``ezdxf`` entity, the live
engine on the record its measured ``Document.Export`` snapshot carries
(``engineering/understand/snapshot.py``). One reader, so the two engines
cannot disagree about a loop or a defpoint (v1.7.0, B16).

* A loop is ``(kind, flags, depth, vertices_or_edges, bulges)`` in WCS.
  ``kind`` "polyline" carries ``((x, y), ...)`` and the parallel bulges;
  ``kind`` "edges" carries edge tuples and no bulges -
  ``("line", start, end)``, ``("arc", centre, radius, start_deg, end_deg,
  ccw)`` (the arc is the counter-clockwise one from start to end; ``ccw`` is
  the direction the loop walks it), ``("ellipse", centre, major_axis, ratio,
  start_param, end_param, ccw)`` and ``("spline", degree, control_points)``.
* ``depth`` is how many other loops a loop sits inside, read off containment
  - the boundary-path flags mark a path external or outermost, never "two
  levels down", and depth is exactly where AutoCAD's three island styles stop
  agreeing. Hatch loops do not cross, so one vertex of the inner loop that is
  not on the other loop's edge settles it; a box test runs first, so a hatch
  with many loops does not pay a point-in-polygon walk per pair. Containment
  is tested on the loop with its arcs walked in short chords, never on its
  bare vertices (a full circle is two of them).
* A loop of lines and arcs is exact: its ring (vertices with bulges) IS the
  boundary and its area is closed-form (``engineering.measure``). An ellipse
  edge is sampled and a spline edge is its control polygon; such a loop says
  ``exact: False`` and reports no area.
* A mirrored frame (extrusion ``(0, 0, -1)``) reflects x: every point and
  arc is translated with ``backends/ocs.py``, and the walking direction of an
  arc or ellipse edge flips.
"""

from __future__ import annotations

import json
import math

from backends.ocs import is_flat_frame, to_wcs_2d, wcs_arc_angles, wcs_bulge
from engineering.measure import polygon_area_perimeter

__all__ = [
    "DIMTYPE_NAMES",
    "HATCH_STYLE_NAMES",
    "acadmcp_xdata",
    "dimension_fields",
    "dimension_props",
    "filled_area",
    "hatch_loops",
    "hatch_props",
    "loop_ring",
    "nesting_depths",
]

#: AutoCAD's HPISLANDDETECTION, by the code DXF stores in group 75.
HATCH_STYLE_NAMES = {0: "normal", 1: "outer", 2: "ignore"}
#: DXF group 70's low bits (the flags 32 / 64 / 128 are masked off).
DIMTYPE_NAMES = {
    0: "linear",
    1: "aligned",
    2: "angular",
    3: "diameter",
    4: "radius",
    5: "angular_3p",
    6: "ordinate",
}
#: Samples per full turn of an ellipse edge (its loop then says it is not exact).
_ELLIPSE_SAMPLES = 72
_TAU = 2.0 * math.pi


def _xy(point) -> tuple[float, float]:
    return (float(point[0]), float(point[1]))


def _ocs_point(extrusion, x, y, z) -> tuple[float, float]:
    wx, wy = to_wcs_2d(extrusion, float(x), float(y), float(z))
    return (float(wx), float(wy))


def _edge(edge, extrusion, elevation: float, mirrored: bool) -> tuple:
    name = edge.type.name.lower()
    if name == "line":
        return (
            "line",
            _ocs_point(extrusion, edge.start[0], edge.start[1], elevation),
            _ocs_point(extrusion, edge.end[0], edge.end[1], elevation),
        )
    if name == "arc":
        centre = (float(edge.center[0]), float(edge.center[1]), elevation)
        start, end = wcs_arc_angles(
            extrusion, centre, float(edge.radius), float(edge.start_angle), float(edge.end_angle)
        )
        return (
            "arc",
            _ocs_point(extrusion, *centre),
            float(edge.radius),
            float(start),
            float(end),
            bool(edge.ccw) != mirrored,
        )
    if name == "ellipse":
        centre = _ocs_point(extrusion, edge.center[0], edge.center[1], elevation)
        tip = _ocs_point(
            extrusion,
            edge.center[0] + edge.major_axis[0],
            edge.center[1] + edge.major_axis[1],
            elevation,
        )
        major = (tip[0] - centre[0], tip[1] - centre[1])
        t0, t1 = float(edge.start_param), float(edge.end_param)
        if mirrored:  # the minor axis flips, so the parameter runs the other way
            t0, t1 = (-t1) % _TAU, (-t0) % _TAU
        return ("ellipse", centre, major, float(edge.ratio), t0, t1, bool(edge.ccw) != mirrored)
    points = tuple(_ocs_point(extrusion, p[0], p[1], elevation) for p in edge.control_points)
    return ("spline", int(edge.degree), points)


def _arc_point(centre, radius, deg) -> tuple[float, float]:
    rad = math.radians(deg)
    return (centre[0] + radius * math.cos(rad), centre[1] + radius * math.sin(rad))


def _edges_ring(edges) -> tuple[list[tuple[float, float, float]], bool]:
    ring: list[tuple[float, float, float]] = []
    exact = True
    for edge in edges:
        kind = edge[0]
        if kind == "line":
            ring.append((edge[1][0], edge[1][1], 0.0))
        elif kind == "arc":
            _kind, centre, radius, start, end, ccw = edge
            sweep = (end - start) % 360.0
            if sweep == 0.0:
                sweep = 360.0
            if sweep >= 360.0 - 1e-12:  # a full circle: two semicircles
                first = _arc_point(centre, radius, start)
                second = _arc_point(centre, radius, start + 180.0)
                sign = 1.0 if ccw else -1.0
                ring.append((first[0], first[1], sign))
                ring.append((second[0], second[1], sign))
                continue
            bulge = math.tan(math.radians(sweep) / 4.0)
            if ccw:
                x, y = _arc_point(centre, radius, start)
                ring.append((x, y, bulge))
            else:
                x, y = _arc_point(centre, radius, end)
                ring.append((x, y, -bulge))
        elif kind == "ellipse":
            exact = False
            _kind, centre, major, ratio, t0, t1, ccw = edge
            span = (t1 - t0) % _TAU or _TAU
            count = max(3, int(_ELLIPSE_SAMPLES * span / _TAU))
            minor = (-major[1] * ratio, major[0] * ratio)
            if ccw:
                params = [t0 + span * i / count for i in range(count)]
            else:
                params = [t1 - span * i / count for i in range(count)]
            for t in params:
                ring.append(
                    (
                        centre[0] + major[0] * math.cos(t) + minor[0] * math.sin(t),
                        centre[1] + major[1] * math.cos(t) + minor[1] * math.sin(t),
                        0.0,
                    )
                )
        else:  # a spline edge: its control polygon, said to be approximate
            exact = False
            ring.extend((x, y, 0.0) for x, y in edge[2])
    return ring, exact


def loop_ring(loop) -> tuple[list[tuple[float, float, float]], bool]:
    """``([(x, y, bulge), ...], exact)`` - the loop as a bulge ring in WCS."""
    kind, _flags, _depth, items, bulges = loop
    if kind == "polyline":
        return [(p[0], p[1], b) for p, b in zip(items, bulges, strict=True)], True
    return _edges_ring(items)


def _containment_ring(ring) -> list[tuple[float, float]]:
    """The bulge ring with every arc walked in short chords - for containment only.

    A loop's vertices alone are not its outline: a full-circle edge is two
    vertices (two semicircles), whose chord polygon contains nothing and whose
    box is a line, so a square island inside a circular boundary would read
    as a second outer loop and be ADDED to the filled area (measured before
    this walk: 330.16 for a radius-10 disc with a 4 x 4 hole, against the true
    100 pi - 16 = 298.16). Areas stay exact - they come from the bulges.
    """
    points: list[tuple[float, float]] = []
    count = len(ring)
    for index, (x, y, bulge) in enumerate(ring):
        points.append((x, y))
        if abs(bulge) <= 1e-12 or count < 2:
            continue
        nx, ny, _b = ring[(index + 1) % count]
        chord = math.hypot(nx - x, ny - y)
        if chord <= 1e-12:
            continue
        sweep = 4.0 * math.atan(bulge)  # signed: counter-clockwise when positive
        ux, uy = (nx - x) / chord, (ny - y) / chord
        offset = (chord / 2.0) / math.tan(sweep / 2.0)  # centre left of the chord
        cx, cy = (x + nx) / 2.0 - uy * offset, (y + ny) / 2.0 + ux * offset
        radius = math.hypot(x - cx, y - cy)
        start = math.atan2(y - cy, x - cx)
        steps = max(2, math.ceil(abs(sweep) / (math.pi / 16.0)))
        for step in range(1, steps):
            angle = start + sweep * step / steps
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _box(points) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def nesting_depths(rings: list[list[tuple[float, float]]]) -> list[int]:
    """How many other rings each ring sits inside (see the module docstring)."""
    from ezdxf import edgesmith
    from ezdxf.math import Vec2

    boxes = [_box(ring) if ring else None for ring in rings]
    polygons = [[Vec2(x, y) for x, y in ring] for ring in rings]
    depths = []
    for index, ring in enumerate(rings):
        depth = 0
        mine = boxes[index]
        for other, polygon in enumerate(polygons):
            theirs = boxes[other]
            if other == index or mine is None or theirs is None:
                continue
            if not (
                theirs[0] <= mine[0]
                and theirs[1] <= mine[1]
                and mine[2] <= theirs[2]
                and mine[3] <= theirs[3]
            ):
                continue
            for x, y in ring:
                verdict = edgesmith.is_point_in_polygon_2d(Vec2(x, y), polygon)
                if verdict != 0:
                    depth += verdict > 0
                    break
        depths.append(depth)
    return depths


def hatch_loops(entity) -> tuple:
    """Every boundary path of an ``ezdxf`` HATCH as a WCS loop tuple, with its depth."""
    extrusion = tuple(entity.dxf.get("extrusion", (0.0, 0.0, 1.0)))
    elevation = float(entity.dxf.get("elevation", (0.0, 0.0, 0.0))[2])
    mirrored = is_flat_frame(extrusion) and extrusion[2] < 0
    raw = []
    for path in entity.paths:
        flags = int(path.path_type_flags)
        if path.type.name.lower() == "polyline":
            vertices = [
                (float(v[0]), float(v[1]), float(v[2]) if len(v) > 2 else 0.0)
                for v in path.vertices
            ]
            points = tuple(_ocs_point(extrusion, x, y, elevation) for x, y, _b in vertices)
            bulges = tuple(float(wcs_bulge(extrusion, b)) for _x, _y, b in vertices)
            raw.append(("polyline", flags, points, bulges))
        else:
            edges = tuple(_edge(e, extrusion, elevation, mirrored) for e in path.edges)
            raw.append(("edges", flags, edges, ()))
    rings = [loop_ring((kind, flags, 0, items, bulges))[0] for kind, flags, items, bulges in raw]
    depths = nesting_depths([_containment_ring(ring) for ring in rings])
    return tuple(
        (kind, flags, depth, items, bulges)
        for (kind, flags, items, bulges), depth in zip(raw, depths, strict=True)
    )


def _loop_area(loop) -> tuple[float | None, bool]:
    ring, exact = loop_ring(loop)
    if len(ring) < 2:
        return None, exact
    return abs(polygon_area_perimeter(ring, True)[0]), exact


def filled_area(loops, style: int = 0) -> float | None:
    """Outer loops minus their islands by AutoCAD's island style - the rule
    ``analysis_measure_entity`` applies. None when a counted loop is not exact."""
    name = HATCH_STYLE_NAMES.get(int(style), "normal")
    total = 0.0
    for loop in loops:
        depth = loop[2]
        if name == "ignore" and depth > 0:
            continue
        if name == "outer" and depth > 1:
            continue
        area, exact = _loop_area(loop)
        if area is None or not exact:
            return None
        total += -area if depth % 2 else area
    return abs(total)


def _edge_dict(edge) -> dict:
    kind = edge[0]
    if kind == "line":
        return {"type": "line", "start": list(edge[1]), "end": list(edge[2])}
    if kind == "arc":
        return {
            "type": "arc",
            "center": list(edge[1]),
            "radius": edge[2],
            "start_angle": edge[3],
            "end_angle": edge[4],
            "ccw": edge[5],
        }
    if kind == "ellipse":
        return {
            "type": "ellipse",
            "center": list(edge[1]),
            "major_axis": list(edge[2]),
            "ratio": edge[3],
            "start_param": edge[4],
            "end_param": edge[5],
            "ccw": edge[6],
        }
    return {"type": "spline", "degree": edge[1], "control_points": [list(p) for p in edge[2]]}


def hatch_props(loops, style: int = 0) -> dict:
    """``entity_get``'s HATCH fields: every loop in WCS with its depth and area,
    and the filled area by the hatch's own island style."""
    rows = []
    exact_all = True
    for loop in loops:
        kind, flags, depth, items, _bulges = loop
        ring, exact = loop_ring(loop)
        area, _ = _loop_area(loop)
        exact_all = exact_all and exact
        row = {
            "kind": kind,
            "flags": flags,
            "external": bool(flags & 1),
            "outermost": bool(flags & 16),
            "depth": depth,
            "points": [[x, y] for x, y, _b in ring],
            "bulges": [b for _x, _y, b in ring],
            "exact": exact,
            "area": area if exact else None,
        }
        if kind == "edges":
            row["edges"] = [_edge_dict(edge) for edge in items]
        rows.append(row)
    area = filled_area(loops, style)
    return {
        "hatch_style": HATCH_STYLE_NAMES.get(int(style), "normal"),
        "loop_count": len(rows),
        "loops": rows,
        "area": area,
        "area_exact": area is not None and exact_all,
    }


def dimension_fields(entity) -> tuple:
    """``(dimtype, defpoints, measurement, text_override, dimstyle, dimlfac, angle)``.

    ``defpoints`` are DXF groups 10, 13, 14, 15 (WCS) and 16 (OCS, translated),
    None where the dimension has none. ``measurement`` is what the dimension
    measures between its own defpoints (``get_measurement``), BEFORE DIMLFAC;
    ``dimlfac`` is the entity's override, else its style's value (None when
    the style cannot be read - an unknown factor is not 1). ``angle`` is DXF
    group 50, the direction a rotated linear dimension measures along: it is
    not recoverable from the defpoints.
    """
    from ezdxf.entities import DimStyleOverride

    dxf = entity.dxf
    extrusion = tuple(dxf.get("extrusion", (0.0, 0.0, 1.0)))
    points = []
    for name in ("defpoint", "defpoint2", "defpoint3", "defpoint4"):
        value = dxf.get(name)
        points.append(None if value is None else _xy(value))
    arc = dxf.get("defpoint5")
    points.append(None if arc is None else _ocs_point(extrusion, arc[0], arc[1], arc[2]))
    try:
        measured = entity.get_measurement()
        measurement = float(measured) if isinstance(measured, (int, float)) else None
    except Exception:  # an ordinate or a malformed dimension measures nothing here
        measurement = None
    try:
        dimlfac = float(DimStyleOverride(entity).get("dimlfac", 1.0))
    except Exception:  # the style cannot be read: the factor is unknown, not 1
        dimlfac = None
    return (
        int(dxf.get("dimtype", 0)) & 0x0F,
        tuple(points),
        measurement,
        str(dxf.get("text", "") or ""),
        str(dxf.get("dimstyle", "Standard")),
        dimlfac,
        float(dxf.get("angle", 0.0)),
    )


def dimension_props(fields) -> dict:
    """``entity_get``'s DIMENSION fields, from :func:`dimension_fields`."""
    dimtype, defpoints, measurement, override, dimstyle, dimlfac, angle = fields
    return {
        "dimtype": DIMTYPE_NAMES.get(dimtype, f"unknown:{dimtype}"),
        "dimtype_code": dimtype,
        "defpoints": [None if p is None else [p[0], p[1]] for p in defpoints],
        "measurement": measurement,
        "text_override": override if override not in ("", "<>") else None,
        "dimstyle": dimstyle,
        "dimlfac": dimlfac,
        "angle": angle,
    }


def acadmcp_xdata(entity) -> tuple[tuple[str, str], ...]:
    """Every ``ACADMCP_*`` application on the entity as ``(app, json)``.

    A payload whose 1000 chunks join to a JSON object (``ACADMCP_MECH``,
    ``_ARCH``, ``_PID``, ...) is kept joined; anything else - a list of plain
    values such as ``ACADMCP_ARCH_H``'s owned handles, which could join into a
    JSON *number* ("31" + "32") - is kept as the JSON list of its values.
    """
    if entity.xdata is None:
        return ()
    rows = []
    for app in sorted(entity.xdata.data):
        if not str(app).startswith("ACADMCP_"):
            continue
        values = [str(tag.value) for tag in entity.get_xdata(app) if tag.code == 1000]
        joined = "".join(values)
        try:
            is_object = isinstance(json.loads(joined), dict)
        except ValueError:
            is_object = False
        rows.append((str(app), joined if is_object else json.dumps(values)))
    return tuple(rows)
