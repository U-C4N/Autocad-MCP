# engineering/pid/lines.py
"""Line classes (ISA-5.1 Table 5.3.1), orthogonal routing, labels, markers, crossings."""

from __future__ import annotations

import math
import string
from dataclasses import dataclass


@dataclass(frozen=True)
class LineClass:
    layer: str
    linetype: str | None  # None = ByLayer
    marker: str | None  # marker symbol placed on long segments
    arrow: bool  # flow arrow at the end by default
    kind: str  # "process" | "signal"


LINE_CLASSES: dict[str, LineClass] = {
    "process_major": LineClass("PROCESS-PIPING-MAIN", None, None, True, "process"),
    "process_minor": LineClass("PROCESS-PIPING-SECONDARY", None, None, True, "process"),
    "utility": LineClass("UTILITY-LINE", None, None, True, "process"),
    "pneumatic": LineClass(
        "INSTRUMENT-LINE-SIGNAL", "Continuous", "mark_pneumatic", False, "signal"
    ),
    "electric": LineClass("INSTRUMENT-LINE-SIGNAL", None, None, False, "signal"),
    "hydraulic": LineClass(
        "INSTRUMENT-LINE-SIGNAL", "Continuous", "mark_hydraulic", False, "signal"
    ),
    "capillary": LineClass(
        "INSTRUMENT-LINE-SIGNAL", "Continuous", "mark_capillary", False, "signal"
    ),
    "data": LineClass("INSTRUMENT-LINE-SIGNAL", "Continuous", "mark_data", False, "signal"),
}
#: Layer → class when a line carries no payload. ``None`` = decide by linetype.
LAYER_TO_CLASS: dict[str, str | None] = {
    "PROCESS-PIPING-MAIN": "process_major",
    "PROCESS-PIPING-SECONDARY": "process_minor",
    "UTILITY-LINE": "utility",
    "ELECTRICAL-LINE": "electric",
    "INSTRUMENT-LINE-SIGNAL": None,
}
PID_LINE_LAYERS = frozenset(LAYER_TO_CLASS)
AXIS = {0.0: (1.0, 0.0), 90.0: (0.0, 1.0), 180.0: (-1.0, 0.0), 270.0: (0.0, -1.0)}
DEFAULT_NUMBER_FORMAT = "{size}-{service}-{seq}-{spec}-{insulation}"
_EPS = 1e-9
_DIR_TOL = 1e-6  # degrees of drift still read as an axis

Point = tuple[float, float]


def snap_axis(dx: float, dy: float) -> float:
    if abs(dx) >= abs(dy):
        return 0.0 if dx >= 0 else 180.0
    return 90.0 if dy >= 0 else 270.0


def axis_direction(name: str, direction_deg) -> float:
    """Normalise ``direction_deg`` to one of the four AXIS keys, or raise ``ValueError``.

    Tolerates the float drift an engine round trip can add (1e-6 deg); anything
    else — 45, 90.5, nan — is a genuinely non-orthogonal port and is refused."""
    value = float(direction_deg)
    if math.isfinite(value):
        wrapped = value % 360.0
        for key in AXIS:
            if abs(wrapped - key) < _DIR_TOL or abs(wrapped - key - 360.0) < _DIR_TOL:
                return key
    raise ValueError(
        f"{name} {value} is not an axis direction (0/90/180/270); "
        "rotate the symbol to a multiple of 90 or pass waypoints"
    )


def _unit(direction_deg: float) -> Point:
    return AXIS[axis_direction("direction", direction_deg)]


def _finite_point(name: str, p) -> Point:
    q = (float(p[0]), float(p[1]))
    for axis, value in zip("xy", q, strict=True):
        if not math.isfinite(value):
            raise ValueError(f"{name}.{axis} is {value}; coordinates must be finite")
    return q


def _dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def path_length(points) -> float:
    return sum(_dist(a, b) for a, b in zip(points, points[1:], strict=False))


def simplify(points) -> list[Point]:
    """Drop repeated points and merge collinear consecutive segments."""
    pts: list[Point] = []
    for p in points:
        q = (float(p[0]), float(p[1]))
        if not pts or _dist(pts[-1], q) > _EPS:
            pts.append(q)
    out: list[Point] = []
    for p in pts:
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            cross = (b[0] - a[0]) * (p[1] - b[1]) - (b[1] - a[1]) * (p[0] - b[0])
            dot = (b[0] - a[0]) * (p[0] - b[0]) + (b[1] - a[1]) * (p[1] - b[1])
            if abs(cross) < _EPS and dot > 0:
                out[-1] = p
                continue
        out.append(p)
    return out


def _direction(a: Point, b: Point) -> Point:
    length = _dist(a, b)
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _same(u: Point, v: Point) -> bool:
    return abs(u[0] - v[0]) < 1e-6 and abs(u[1] - v[1]) < 1e-6


def _validate(candidate, ds: float | None, de: float | None, stub: float) -> list[Point] | None:
    pts = simplify(candidate)
    if len(pts) < 2:
        return None
    segs = list(zip(pts, pts[1:], strict=False))
    straight = len(segs) == 1
    if ds is not None and not _same(_direction(*segs[0]), _unit(ds)):
        return None
    if de is not None:
        ue = _unit(de)
        if not _same(_direction(*segs[-1]), (-ue[0], -ue[1])):
            return None
    minimum = 0.01 if straight else max(stub, 0.01)
    if any(_dist(a, b) < minimum - _EPS for a, b in segs):
        return None
    for (a1, b1), (a2, b2) in zip(segs, segs[1:], strict=False):
        u, v = _direction(a1, b1), _direction(a2, b2)
        if abs(u[0] * v[0] + u[1] * v[1]) > 1e-6:
            return None
    return pts


def route(start, start_dir, end, end_dir, stub: float = 5.0, mode="auto") -> list[Point]:
    """Orthogonal path from ``start`` (leaving along ``start_dir``) to ``end``
    (arriving against ``end_dir``). ``None`` directions are radial: the axis
    towards the other end. Raises ``ValueError`` when no candidate honours the stub."""
    s = _finite_point("start", start)
    e = _finite_point("end", end)
    if isinstance(mode, (list, tuple)):
        path = [s] + [_finite_point(f"waypoints[{i}]", p) for i, p in enumerate(mode)] + [e]
        for a, b in zip(path, path[1:], strict=False):
            if _dist(a, b) < 0.01:
                raise ValueError("waypoints repeat a point (segment shorter than 0.01 mm)")
        return path
    if _dist(s, e) < 0.01:
        raise ValueError("zero-length line: both ends are the same point")
    if mode == "direct":
        return [s, e]
    if mode != "auto":
        raise ValueError("route must be 'auto', 'direct' or a list of waypoints")
    stub = float(stub)
    if not math.isfinite(stub) or stub < 0:
        raise ValueError(f"stub must be a finite length >= 0, not {stub}")
    ds = (
        snap_axis(e[0] - s[0], e[1] - s[1])
        if start_dir is None
        else axis_direction("start_dir", start_dir)
    )
    de = (
        snap_axis(s[0] - e[0], s[1] - e[1])
        if end_dir is None
        else axis_direction("end_dir", end_dir)
    )
    us, ue = _unit(ds), _unit(de)
    s1 = (s[0] + us[0] * stub, s[1] + us[1] * stub)
    e1 = (e[0] + ue[0] * stub, e[1] + ue[1] * stub)
    xm, ym = (s1[0] + e1[0]) / 2.0, (s1[1] + e1[1]) / 2.0
    candidates = [
        [s, e],
        [s, (e[0], s[1]), e],
        [s, (s[0], e[1]), e],
        [s, (xm, s[1]), (xm, e[1]), e],
        [s, (s[0], ym), (e[0], ym), e],
        [s, s1, (s1[0], e1[1]), e1, e],
        [s, s1, (e1[0], s1[1]), e1, e],
    ]
    valid = [p for p in (_validate(c, ds, de, stub) for c in candidates) if p]
    if not valid:
        raise ValueError(
            f"no orthogonal route from {s} to {e} honours a {stub} mm stub on both ports; "
            "shorten `stub` or pass waypoints"
        )
    valid.sort(key=lambda p: (len(p) - 2, path_length(p)))
    return valid[0]


def aim_points(start, end, mode) -> tuple[Point, Point]:
    """What each end of a line aims at: the other end, or — with waypoints —
    the first waypoint for the start and the last one for the end.

    A radial (bubble) port leaves its circle along the axis towards its aim
    point. Aiming at the far end while the route is a waypoint list put the
    exit on the wrong side of the bubble, so the first segment cut through it.
    """
    if isinstance(mode, (list, tuple)) and len(mode) > 0:
        return _finite_point("waypoints[0]", mode[0]), _finite_point(
            f"waypoints[{len(mode) - 1}]", mode[-1]
        )
    return _finite_point("end", end), _finite_point("start", start)


def label_placement(vertices, offset: float = 1.5) -> tuple[float, float, float, int]:
    """Midpoint of the longest segment, offset to its left, readable rotation."""
    pts = [(float(x), float(y)) for x, y in vertices]
    segs = list(zip(pts, pts[1:], strict=False))
    index = max(range(len(segs)), key=lambda i: _dist(*segs[i]))
    (x0, y0), (x1, y1) = segs[index]
    angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360.0
    if 90.0 < angle <= 270.0:
        angle = (angle - 180.0) % 360.0
    ux, uy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return (mx - uy * offset, my + ux * offset, round(angle, 6), index)


def marker_positions(vertices, min_len: float = 15.0) -> list[tuple[float, float, float]]:
    pts = [(float(x), float(y)) for x, y in vertices]
    out = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if _dist((x0, y0), (x1, y1)) < min_len:
            continue
        angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360.0
        out.append(((x0 + x1) / 2.0, (y0 + y1) / 2.0, round(angle, 6)))
    return out


#: Max chord deviation (mm) when a bulged polyline edge is flattened into
#: chords (``flatten_bulges``) — a tenth of the thinnest ISO 128 lineweight.
#: The crossing count does **not** flatten: it tests the exact arc.
FLATTEN_SAGITTA = 0.05


def _arc_of_bulge(a: Point, b: Point, bulge: float) -> tuple[float, float, float, float, float]:
    """``(cx, cy, radius, start, theta)`` of the arc a→b with the DXF bulge
    ``tan(sweep/4)``: ``theta`` is the signed sweep in radians (positive
    counter-clockwise) and ``start`` the angle of ``a`` about the centre.

    Same arithmetic as ``engineering/measure.py`` (sweep ``4·atan(bulge)``,
    radius ``chord / 2·sin(|sweep|/2)``) and ``graph._point_segment_distance``
    (centre on the chord's left normal, ``radius - sagitta`` carrying it across
    the chord past a semicircle). The caller guarantees a non-zero bulge and a
    non-degenerate chord.
    """
    chord = _dist(a, b)
    theta = 4.0 * math.atan(bulge)  # signed sweep, radians
    radius = chord / (2.0 * math.sin(abs(theta) / 2.0))
    sag = abs(bulge) * chord / 2.0
    nx, ny = -(b[1] - a[1]) / chord, (b[0] - a[0]) / chord  # left of a→b
    offset = (radius - sag) * (1.0 if bulge > 0 else -1.0)
    cx = (a[0] + b[0]) / 2.0 + offset * nx
    cy = (a[1] + b[1]) / 2.0 + offset * ny
    start = math.atan2(a[1] - cy, a[0] - cx)
    return cx, cy, radius, start, theta


def flatten_bulges(points, bulges, sagitta: float = FLATTEN_SAGITTA) -> list[Point]:
    """The polyline as straight segments, every bulged edge replaced by chords
    that deviate from the arc by at most ``sagitta``.

    Arc arithmetic from ``_arc_of_bulge``. Checked against
    ``ezdxf.math.bulge_to_arc`` + ``ConstructionArc.flattening`` in
    ``tests/test_pid_lines.py``: identical vertex count and positions to 1e-12
    for bulges 0.2 … 2.0 of either sign. A straight edge (bulge 0) is passed
    through; ``bulges`` shorter than the edge count is padded with zeros.

    Not used by the crossing count: a flattened chain has artificial vertices,
    and ``segments_cross`` treats a hit on any vertex as a junction — a
    semicircle flattens to an even chord count, so a line through its apex
    (the axis of symmetry, exactly where a P&ID line meets a crossing jump)
    would be dropped by both chords. ``count_crossings`` tests the exact arc.
    """
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 2:
        return pts
    out: list[Point] = []
    for i, (a, b) in enumerate(zip(pts, pts[1:], strict=False)):
        out.append(a)
        bulge = float(bulges[i]) if i < len(bulges) and bulges[i] else 0.0
        if abs(bulge) < 1e-12 or _dist(a, b) < 1e-12:
            continue
        cx, cy, radius, start, theta = _arc_of_bulge(a, b, bulge)
        if sagitta >= radius:
            segments = 1
        else:
            segments = max(1, math.ceil(abs(theta) / (2.0 * math.acos(1.0 - sagitta / radius))))
        for k in range(1, segments):
            angle = start + theta * k / segments
            out.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    out.append(pts[-1])
    return out


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1, p2, q1, q2) -> bool:
    """Proper crossing only: shared endpoints and T-touches are junctions, not crossings."""
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    return d1 * d2 < -_EPS and d3 * d4 < -_EPS


def segment_arc_crossings(p1, p2, a, b, bulge: float) -> int:
    """Proper crossings (0, 1 or 2) of the straight segment p1→p2 with the
    exact arc a→b of DXF ``bulge``; a zero bulge is the chord test.

    The same junction rule as ``segments_cross``: a hit within ``_EPS`` (mm)
    of either end of the segment or of the arc is a T-touch or a shared
    vertex, not a crossing, and a tangent (the line reaches no deeper than
    ``_EPS`` into the circle) touches without crossing. Only the arc's own
    two ends are junction points — nothing along the sweep is a vertex.
    """
    if abs(bulge) < 1e-12 or _dist(a, b) < 1e-12:
        return 1 if segments_cross(p1, p2, a, b) else 0
    cx, cy, radius, start, theta = _arc_of_bulge(a, b, bulge)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-24:
        return 0
    length = math.sqrt(length_sq)
    fx, fy = p1[0] - cx, p1[1] - cy
    # |p1 + t·d − c|² = r²  →  A t² + B t + C = 0 with A = |d|²
    coeff_b = 2.0 * (fx * dx + fy * dy)
    coeff_c = fx * fx + fy * fy - radius * radius
    disc = coeff_b * coeff_b - 4.0 * length_sq * coeff_c
    if disc <= 0.0:
        return 0
    root = math.sqrt(disc)
    if root / (2.0 * length) <= _EPS:
        return 0  # tangent: the half-chord inside the circle is nothing
    sweep = abs(theta)
    sign = 1.0 if theta > 0 else -1.0
    count = 0
    for numerator in (-coeff_b - root, -coeff_b + root):
        t = numerator / (2.0 * length_sq)
        if t * length <= _EPS or (1.0 - t) * length <= _EPS:
            continue  # the segment ends on the arc: a T-touch
        x, y = p1[0] + t * dx, p1[1] + t * dy
        along = (sign * (math.atan2(y - cy, x - cx) - start)) % (2.0 * math.pi)
        if along * radius <= _EPS or abs(sweep - along) * radius <= _EPS:
            continue  # the arc's own end: the polyline's vertex, a junction
        if along > sweep:
            continue  # on the circle but outside the arc
        count += 1
    return count


def count_crossings(vertices, others, bulges=None) -> int:
    """Proper crossings of the straight chain ``vertices`` with every chain in
    ``others``. ``bulges[i]`` is the per-vertex DXF bulge list of ``others[i]``
    (the arc leaving each vertex; short lists are zero-padded, ``None`` means
    every chain is straight): a bulged edge is tested as its exact arc, never
    as its chord or as flattened chords.
    """
    pts = [(float(x), float(y)) for x, y in vertices]
    count = 0
    for index, other in enumerate(others):
        opts = [(float(x), float(y)) for x, y in other]
        obulges = list(bulges[index]) if bulges is not None and index < len(bulges) else []
        for a, b in zip(pts, pts[1:], strict=False):
            for j, (c, d) in enumerate(zip(opts, opts[1:], strict=False)):
                bulge = float(obulges[j]) if j < len(obulges) and obulges[j] else 0.0
                count += segment_arc_crossings(a, b, c, d, bulge)
    return count


_KNOWN_FIELDS = {"size", "service", "seq", "spec", "insulation", "area", "unit"}


def _parse_token(token: str) -> list[tuple[str, str | None, str | None, str | None]]:
    """``string.Formatter`` parse of one ``-``-separated token, with every malformed
    or unsupported placeholder refused by name — nothing braced is ever copied through."""
    try:
        parts = list(string.Formatter().parse(token))
    except ValueError as exc:
        raise ValueError(f"line-number format token {token!r} is malformed: {exc}") from exc
    fields = [part for part in parts if part[1] is not None]
    if len(fields) > 1:
        raise ValueError(
            f"line-number format token {token!r} holds {len(fields)} fields; "
            "a token may hold at most one field"
        )
    for _literal, name, _spec, conversion in fields:
        if name == "" or name.isdigit():
            raise ValueError(
                f"line-number format token {token!r} uses a positional field; "
                f"name one of: {', '.join(sorted(_KNOWN_FIELDS))}"
            )
        if name not in _KNOWN_FIELDS:
            raise ValueError(
                f"unknown line-number field {{{name}}}; known: {', '.join(sorted(_KNOWN_FIELDS))}"
            )
        if conversion is not None:
            raise ValueError(
                f"line-number format token {token!r} uses a conversion (!{conversion}); "
                "only a format spec such as {seq:04d} is supported"
            )
    return parts


def _render_token(token: str, fields: dict) -> str | None:
    """The token with its field filled, or ``None`` when that field is empty."""
    out: list[str] = []
    for literal, name, spec, _conversion in _parse_token(token):
        out.append(literal)
        if name is None:
            continue
        value = fields.get(name)
        if value is None or value == "":
            return None
        try:
            out.append(format(value, spec or ""))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"line-number format token {token!r} cannot format {name}={value!r}: {exc}"
            ) from exc
    return "".join(out)


def format_line_number(fmt: str, **fields) -> str:
    """Fill ``fmt`` (``-``-separated tokens) and drop the empty fields with their separators.

    A token is literal text, or one ``{field}`` (optionally ``{field:spec}``) with
    literal text around it; a token whose field is empty drops out whole."""
    rendered = (_render_token(token, fields) for token in fmt.split("-"))
    return "-".join(text for text in rendered if text is not None)
