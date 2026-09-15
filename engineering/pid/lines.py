# engineering/pid/lines.py
"""Line classes (ISA-5.1 Table 5.3.1), orthogonal routing, labels, markers, crossings."""

from __future__ import annotations

import math
import re
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

Point = tuple[float, float]


def snap_axis(dx: float, dy: float) -> float:
    if abs(dx) >= abs(dy):
        return 0.0 if dx >= 0 else 180.0
    return 90.0 if dy >= 0 else 270.0


def _unit(direction_deg: float) -> Point:
    return AXIS[float(direction_deg) % 360.0]


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
    s: Point = (float(start[0]), float(start[1]))
    e: Point = (float(end[0]), float(end[1]))
    if isinstance(mode, (list, tuple)):
        path = [s] + [(float(p[0]), float(p[1])) for p in mode] + [e]
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
    if stub < 0:
        raise ValueError("stub must be >= 0")
    ds = snap_axis(e[0] - s[0], e[1] - s[1]) if start_dir is None else float(start_dir) % 360.0
    de = snap_axis(s[0] - e[0], s[1] - e[1]) if end_dir is None else float(end_dir) % 360.0
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


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1, p2, q1, q2) -> bool:
    """Proper crossing only: shared endpoints and T-touches are junctions, not crossings."""
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    return d1 * d2 < -_EPS and d3 * d4 < -_EPS


def count_crossings(vertices, others) -> int:
    pts = [(float(x), float(y)) for x, y in vertices]
    count = 0
    for a, b in zip(pts, pts[1:], strict=False):
        for other in others:
            opts = [(float(x), float(y)) for x, y in other]
            for c, d in zip(opts, opts[1:], strict=False):
                if segments_cross(a, b, c, d):
                    count += 1
    return count


_FIELD_RE = re.compile(r"^\{(\w+)\}$")
_KNOWN_FIELDS = {"size", "service", "seq", "spec", "insulation", "area", "unit"}


def format_line_number(fmt: str, **fields) -> str:
    """Fill ``fmt`` (``-``-separated tokens) and drop the empty fields with their separators."""
    kept: list[str] = []
    for token in fmt.split("-"):
        match = _FIELD_RE.match(token)
        if not match:
            kept.append(token)
            continue
        name = match.group(1)
        if name not in _KNOWN_FIELDS:
            raise ValueError(
                f"unknown line-number field {{{name}}}; known: {', '.join(sorted(_KNOWN_FIELDS))}"
            )
        value = fields.get(name)
        if value is None or value == "":
            continue
        kept.append(str(value))
    return "-".join(kept)
