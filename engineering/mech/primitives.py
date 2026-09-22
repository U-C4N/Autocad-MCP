"""Typed primitive descriptions emitted by the pure mechanical modules.

`part.py`, `features.py`, `views.py`, `dimension.py` and everything under
`standards/` never touch a backend: they return these tuples, and
`engineering/mech/draw.py` is the only module that turns them into entities.
That is what makes the view engine testable without a drawing — a test asserts
the emitted primitives, not a screenshot.

Coordinates here are part-local; `draw.py` maps them to WCS through the part's
placement. `role` is the ISO 128 line role, and `ROLE_LAYER` is the one place a
role becomes a layer name, so no module hardcodes a layer.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Literal, NamedTuple

Role = Literal["visible", "hidden", "center", "phantom", "hatch", "dim", "text"]
Pt = tuple[float, float]

#: ISO 128 line role -> the engineering layer `drawing_new` already bootstraps.
ROLE_LAYER: dict[str, str] = {
    "visible": "GEOMETRY",
    "hidden": "HIDDEN",
    "center": "CENTER",
    "phantom": "PHANTOM",
    "hatch": "HATCH",
    "dim": "DIM",
    "text": "TEXT",
}
ROLES: tuple[str, ...] = tuple(ROLE_LAYER)


class Line(NamedTuple):
    p1: Pt
    p2: Pt
    role: Role = "visible"


class Arc(NamedTuple):
    """Counter-clockwise from ``start_deg`` to ``end_deg``, degrees from +X."""

    center: Pt
    radius: float
    start_deg: float
    end_deg: float
    role: Role = "visible"


class Circle(NamedTuple):
    center: Pt
    radius: float
    role: Role = "visible"


class Poly(NamedTuple):
    points: tuple[Pt, ...]
    closed: bool = False
    role: Role = "visible"


class HatchArea(NamedTuple):
    """``loops[0]`` is the outer boundary; ``loops[1:]`` are islands.

    It carries no ``role``: a hatch is always on ``ROLE_LAYER["hatch"]`` and its
    pattern comes from ``standards.materials.hatch_for(material)``.
    """

    loops: tuple[tuple[Pt, ...], ...]
    material: str = "steel"


class Text(NamedTuple):
    at: Pt
    text: str
    height: float = 3.5
    rotation: float = 0.0
    role: Role = "text"


class DimIntent(NamedTuple):
    """What a feature wants dimensioned, before any placement decision."""

    kind: Literal["linear", "aligned", "diameter", "radius", "angular"]
    p1: Pt
    p2: Pt
    text_at: Pt | None = None
    text_override: str | None = None
    fit: str | None = None
    tol: dict | None = None
    feature: str = ""


Prim = Line | Arc | Circle | Poly | HatchArea | Text


def layer_for(prim: Prim) -> str:
    """The layer a primitive belongs on. A HATCH has no role of its own."""
    if isinstance(prim, HatchArea):
        return ROLE_LAYER["hatch"]
    role = getattr(prim, "role", "visible")
    try:
        return ROLE_LAYER[role]
    except KeyError:
        raise ValueError(f"unknown line role {role!r}; roles are {', '.join(ROLES)}") from None


def _on_circle(center: Pt, radius: float, deg: float) -> Pt:
    rad = math.radians(deg)
    return (center[0] + radius * math.cos(rad), center[1] + radius * math.sin(rad))


def _arc_points(arc: Arc) -> tuple[Pt, ...]:
    """Endpoints plus every axis extreme the sweep actually passes through.

    Without this a 0-180 arc reports a bbox that stops at its endpoints, and
    the view layout then puts the next view on top of it.
    """
    start = arc.start_deg % 360.0
    sweep = (arc.end_deg - arc.start_deg) % 360.0
    if sweep == 0.0:
        sweep = 360.0
    points = [
        _on_circle(arc.center, arc.radius, arc.start_deg),
        _on_circle(arc.center, arc.radius, arc.end_deg),
    ]
    for quadrant in (0.0, 90.0, 180.0, 270.0):
        if ((quadrant - start) % 360.0) <= sweep:
            points.append(_on_circle(arc.center, arc.radius, quadrant))
    return tuple(points)


def points_of(prim: Prim) -> tuple[Pt, ...]:
    """Every point that bounds a primitive.

    A ``Text`` reports only its anchor: the rendered width depends on the style
    and the engine, and a guessed box is a silent wrong number.
    """
    if isinstance(prim, Line):
        return (prim.p1, prim.p2)
    if isinstance(prim, Arc):
        return _arc_points(prim)
    if isinstance(prim, Circle):
        cx, cy = prim.center
        r = prim.radius
        return ((cx - r, cy), (cx + r, cy), (cx, cy - r), (cx, cy + r))
    if isinstance(prim, Poly):
        return tuple(prim.points)
    if isinstance(prim, HatchArea):
        return tuple(pt for loop in prim.loops for pt in loop)
    if isinstance(prim, Text):
        return (prim.at,)
    raise TypeError(f"not a primitive: {prim!r}")


def bbox(prims: Iterable[Prim]) -> tuple[float, float, float, float]:
    """``(minx, miny, maxx, maxy)`` over every primitive. Empty is refused."""
    xs: list[float] = []
    ys: list[float] = []
    for prim in prims:
        for x, y in points_of(prim):
            xs.append(float(x))
            ys.append(float(y))
    if not xs:
        raise ValueError("bbox: no primitives to bound")
    return (min(xs), min(ys), max(xs), max(ys))


def _map_points(prim: Prim, fn) -> Prim:
    if isinstance(prim, Line):
        return prim._replace(p1=fn(prim.p1), p2=fn(prim.p2))
    if isinstance(prim, (Arc, Circle)):
        return prim._replace(center=fn(prim.center))
    if isinstance(prim, Poly):
        return prim._replace(points=tuple(fn(p) for p in prim.points))
    if isinstance(prim, HatchArea):
        return prim._replace(loops=tuple(tuple(fn(p) for p in loop) for loop in prim.loops))
    if isinstance(prim, Text):
        return prim._replace(at=fn(prim.at))
    raise TypeError(f"not a primitive: {prim!r}")


def translate(prims: Iterable[Prim], dx: float, dy: float) -> tuple[Prim, ...]:
    def move(p: Pt) -> Pt:
        return (p[0] + dx, p[1] + dy)

    return tuple(_map_points(prim, move) for prim in prims)


def rotate(prims: Iterable[Prim], deg: float, about: Pt = (0.0, 0.0)) -> tuple[Prim, ...]:
    cos = math.cos(math.radians(deg))
    sin = math.sin(math.radians(deg))
    ox, oy = about

    def turn(p: Pt) -> Pt:
        dx = p[0] - ox
        dy = p[1] - oy
        return (ox + dx * cos - dy * sin, oy + dx * sin + dy * cos)

    out: list[Prim] = []
    for prim in prims:
        moved = _map_points(prim, turn)
        if isinstance(moved, Arc):
            moved = moved._replace(start_deg=moved.start_deg + deg, end_deg=moved.end_deg + deg)
        elif isinstance(moved, Text):
            moved = moved._replace(rotation=moved.rotation + deg)
        out.append(moved)
    return tuple(out)


def scale(prims: Iterable[Prim], factor: float, about: Pt = (0.0, 0.0)) -> tuple[Prim, ...]:
    if not math.isfinite(factor) or factor <= 0.0:
        raise ValueError(f"scale factor must be finite and positive, got {factor!r}")
    ox, oy = about

    def grow(p: Pt) -> Pt:
        return (ox + (p[0] - ox) * factor, oy + (p[1] - oy) * factor)

    out: list[Prim] = []
    for prim in prims:
        moved = _map_points(prim, grow)
        if isinstance(moved, (Arc, Circle)):
            moved = moved._replace(radius=moved.radius * factor)
        elif isinstance(moved, Text):
            moved = moved._replace(height=moved.height * factor)
        out.append(moved)
    return tuple(out)
