# engineering/pid/geometry.py
"""Primitive-spec helpers for authoring symbols (block-local millimetres).

Each helper returns a ``block_define`` entity/ATTDEF spec (see
``backends/block_specs.py``), so a symbol builder composes plain dicts and the
same spec is validated and written identically on both engines.
"""

from __future__ import annotations

import math


def line(x1: float, y1: float, x2: float, y2: float) -> dict:
    return {"type": "line", "x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2)}


def circle(cx: float, cy: float, r: float) -> dict:
    return {"type": "circle", "cx": float(cx), "cy": float(cy), "r": float(r)}


def arc(cx: float, cy: float, r: float, start_deg: float, end_deg: float) -> dict:
    return {
        "type": "arc",
        "cx": float(cx),
        "cy": float(cy),
        "r": float(r),
        "start_deg": float(start_deg),
        "end_deg": float(end_deg),
    }


def polyline(points, closed: bool = True, bulges=None) -> dict:
    spec = {
        "type": "polyline",
        "points": [[float(x), float(y)] for x, y in points],
        "closed": closed,
    }
    if bulges is not None:
        spec["bulges"] = [float(b) for b in bulges]
    return spec


def text(
    t: str,
    x: float,
    y: float,
    height: float,
    align: str = "middle_center",
    rotation_deg: float = 0.0,
) -> dict:
    return {
        "type": "text",
        "text": t,
        "x": float(x),
        "y": float(y),
        "height": float(height),
        "align": align,
        "rotation_deg": float(rotation_deg),
    }


def solid(points) -> dict:
    return {"type": "solid", "points": [[float(x), float(y)] for x, y in points]}


def dashed_line(
    x1: float, y1: float, x2: float, y2: float, on: float = 1.0, off: float = 1.0
) -> list[dict]:
    """A dashed line as short LINE primitives, so the block needs no linetype."""
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        raise ValueError("dashed_line: zero-length line")
    if on <= 0 or off < 0:
        raise ValueError("dashed_line: 'on' must be > 0 and 'off' >= 0")
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    out: list[dict] = []
    pos = 0.0
    while pos < length - 1e-9:
        end = min(pos + on, length)
        out.append(line(x1 + ux * pos, y1 + uy * pos, x1 + ux * end, y1 + uy * end))
        pos = end + off
    return out


def regular_polygon(
    cx: float, cy: float, r: float, n: int, start_deg: float = 0.0
) -> list[tuple[float, float]]:
    return [
        (
            cx + r * math.cos(math.radians(start_deg + 360.0 * i / n)),
            cy + r * math.sin(math.radians(start_deg + 360.0 * i / n)),
        )
        for i in range(n)
    ]


def attdef(
    tag: str,
    x: float,
    y: float,
    height: float,
    align: str = "center",
    invisible: bool = False,
    default: str = "",
) -> dict:
    return {
        "tag": tag,
        "prompt": tag,
        "default": default,
        "x": float(x),
        "y": float(y),
        "height": float(height),
        "align": align,
        "invisible": invisible,
    }


def _arc_extent(cx, cy, r, start_deg, end_deg):
    """Points along the arc at 1° steps plus both ends (CCW from start to end)."""
    pts = []
    sweep = (end_deg - start_deg) % 360.0 or 360.0
    for k in range(0, int(sweep) + 1):
        a = math.radians(start_deg + k)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    a = math.radians(end_deg)
    pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def scan_hits(primitives, axis: str, c: float, eps: float = 1e-9) -> list[float]:
    """Where the scan line ``x = c`` (``axis="x"``) or ``y = c`` (``axis="y"``)
    meets the drawn geometry: the *other* coordinate of every crossing.

    A segment lying on the scan line contributes both of its ends; a tangent
    arc contributes its touching point. Text has no outline and is skipped;
    bulged polylines are refused rather than silently flattened to chords.
    """
    if axis not in ("x", "y"):
        raise ValueError(f"scan_hits: axis must be 'x' or 'y', got {axis!r}")
    along_x = axis == "x"
    hits: list[float] = []

    def segment(x1: float, y1: float, x2: float, y2: float) -> None:
        # a = scanned coordinate, b = reported coordinate
        a1, b1, a2, b2 = (x1, y1, x2, y2) if along_x else (y1, x1, y2, x2)
        if abs(a2 - a1) <= eps:
            if abs(a1 - c) <= eps:
                hits.extend((b1, b2))
            return
        t = (c - a1) / (a2 - a1)
        if -eps <= t <= 1.0 + eps:
            hits.append(b1 + t * (b2 - b1))

    def circle_arc(cx: float, cy: float, r: float, start: float, end: float) -> None:
        ca, cb = (cx, cy) if along_x else (cy, cx)
        d2 = r * r - (c - ca) ** 2
        if d2 < -eps:
            return
        s = math.sqrt(max(d2, 0.0))
        sweep = (end - start) % 360.0 or 360.0
        for b in {cb - s, cb + s}:
            x, y = (c, b) if along_x else (b, c)
            rel = (math.degrees(math.atan2(y - cy, x - cx)) - start) % 360.0
            if rel <= sweep + 1e-6 or rel >= 360.0 - 1e-6:
                hits.append(b)

    for p in primitives:
        kind = p["type"]
        if kind == "line":
            segment(p["x1"], p["y1"], p["x2"], p["y2"])
        elif kind == "circle":
            circle_arc(p["cx"], p["cy"], p["r"], 0.0, 360.0)
        elif kind == "arc":
            circle_arc(p["cx"], p["cy"], p["r"], p["start_deg"], p["end_deg"])
        elif kind in ("polyline", "solid"):
            if kind == "polyline" and p.get("bulges"):
                raise ValueError("scan_hits: bulged polylines are not supported")
            pts = list(p["points"])
            if kind == "solid" or p["closed"]:
                pts.append(pts[0])
            for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
                segment(x1, y1, x2, y2)
    return hits


def bbox_of(primitives) -> tuple[float, float, float, float]:
    """Extent of the drawn geometry (text counts by its insertion point only)."""
    xs: list[float] = []
    ys: list[float] = []
    for p in primitives:
        kind = p["type"]
        if kind == "line":
            xs += [p["x1"], p["x2"]]
            ys += [p["y1"], p["y2"]]
        elif kind == "circle":
            xs += [p["cx"] - p["r"], p["cx"] + p["r"]]
            ys += [p["cy"] - p["r"], p["cy"] + p["r"]]
        elif kind == "arc":
            for x, y in _arc_extent(p["cx"], p["cy"], p["r"], p["start_deg"], p["end_deg"]):
                xs.append(x)
                ys.append(y)
        elif kind in ("polyline", "solid"):
            for x, y in p["points"]:
                xs.append(x)
                ys.append(y)
        elif kind == "text":
            xs.append(p["x"])
            ys.append(p["y"])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))
