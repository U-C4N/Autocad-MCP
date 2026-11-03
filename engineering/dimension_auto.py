"""``dimension_auto`` v2 (1.8.0, plan item 1.8-13): a part's dimensions, planned.

The pure half of ``PremiumContract.dimension_auto``: it turns the selected
entities into a list of :class:`PlannedDim` - which dimension, drawn to which
points, standing where - and the backend half draws each one through the
existing ``dimension_*`` contracts, so both engines draw the same plan.

What each entity gets:

* **LINE** - exactly what 1.7 drew (the ``chain`` / ``baseline`` /
  ``ordinate`` run, :func:`plan_line_run`); a selection of LINEs alone is
  dimensioned the 1.7 way and nothing more.
* **LWPOLYLINE** - one dimension per straight segment. Horizontal segments
  stand on one row below or above the part box, vertical ones on one row left
  or right of it (the side the segment faces: outward of a closed outline, away
  from the selection's centre for an open one) - a chain per side. An oblique
  segment gets an aligned dimension ``offset`` out along its outward normal. A
  bulged (arc) segment is not dimensioned and is listed in ``omitted`` with its
  segment index.
* **CIRCLE** - a diameter. The first chord point faces away from the part
  box's centre, so the text stands beyond it (the first-chord rule both engines
  share), outside the part box by ``offset``.
* **ARC** - a radius, its chord point at the middle of the sweep, the text
  outside the part box the same way.
* **overall** - the selection's width and height, one row further out than
  the edge rows, drawn between real feature points (line ends, vertices,
  quadrant points) so a binder can bind it. An overall size that one planned
  dimension already measures (the bottom edge of a rectangle, the diameter of
  a lone circle) is not drawn twice: it is listed in ``redundant``.

``group_equal=True`` gives equal circles one diameter, ``"<n>× <>"`` (the
count read off the selection, the value still the measured one).

Text placement is rows, not an optimiser (the plan's stated risk): a radial
text that would stand within :data:`CLEARANCE` of a text already placed is
pushed out by one ``offset`` at a time, at most :data:`MAX_BUMPS` times. The
backend reports what the drawing then holds through the ``dim_overlap`` check
itself, never a prediction.

Nothing is dropped silently: an entity that cannot be dimensioned (ELLIPSE,
SPLINE, a DIMENSION, a TEXT, a circle in a tilted plane ...) or that ``include``
leaves out is a row of ``omitted`` with its handle, type and the reason.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from engineering.errors import InvalidArgsError

__all__ = [
    "CLEARANCE",
    "INCLUDE",
    "MAX_BUMPS",
    "SELECTOR_KEYS",
    "STYLES",
    "DimensionAutoResult",
    "Plan",
    "PlannedDim",
    "Source",
    "match_existing",
    "normalize",
    "parse_include",
    "parse_selector",
    "plan_dimensions",
    "plan_line_run",
]

STYLES: tuple[str, ...] = ("chain", "baseline", "ordinate")
INCLUDE: tuple[str, ...] = ("linear", "diameter", "radius", "overall")
#: The keys ``entity_select_smart`` reads.
SELECTOR_KEYS: tuple[str, ...] = ("type", "layer", "near", "length_range", "color")
#: Two dimension texts closer than this are moved apart: twice
#: ``engineering.critique.DIM_OVERLAP_TOL_MM``, the distance the ``dim_overlap``
#: focus calls an overlap.
CLEARANCE = 10.0
#: How many times one radial text is pushed out by ``offset`` before it is left
#: where it is (the self-check then reports it).
MAX_BUMPS = 20
#: A segment whose direction is within this many radians of an axis is drawn on
#: that axis's row.
AXIS_TOLERANCE_RAD = 1e-9
#: The ``×`` of a grouped count, ISO 129-1's "6× Ø11".
TIMES = "×"

Point = tuple[float, float]


# -- arguments -------------------------------------------------------------------


def _invalid(path: str, message: str) -> InvalidArgsError:
    return InvalidArgsError(f"invalid_args: dimension_auto.{path}: {message}")


def parse_include(include: Iterable[str] | None) -> tuple[str, ...]:
    """The kinds to draw, in :data:`INCLUDE` order; None is every kind."""
    if include is None:
        return INCLUDE
    if isinstance(include, str):
        include = [include]
    given = list(include)
    unknown = [str(item) for item in given if item not in INCLUDE]
    if unknown:
        raise _invalid(
            "include", f"unknown kind(s) {', '.join(unknown)}; kinds: {', '.join(INCLUDE)}"
        )
    if not given:
        raise _invalid("include", f"name at least one of {', '.join(INCLUDE)}")
    return tuple(kind for kind in INCLUDE if kind in given)


def parse_selector(selector: Any) -> dict:
    """An ``entity_select_smart`` predicate, its keys checked by name."""
    if not isinstance(selector, dict):
        raise _invalid("selector", f"expected an object, got {type(selector).__name__}")
    unknown = sorted(str(key) for key in selector if key not in SELECTOR_KEYS)
    if unknown:
        raise _invalid(
            "selector", f"unknown key(s) {', '.join(unknown)}; known: {', '.join(SELECTOR_KEYS)}"
        )
    if not selector:
        raise _invalid("selector", f"name at least one of {', '.join(SELECTOR_KEYS)}")
    return dict(selector)


# -- the selection ---------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One selected entity, reduced to the geometry dimension_auto reads."""

    handle: str
    kind: str  # "line" | "polyline" | "circle" | "arc"
    type: str  # the type the engine reported
    points: tuple[Point, ...] = ()
    bulges: tuple[float, ...] = ()
    closed: bool = False
    center: Point | None = None
    radius: float | None = None
    start_angle: float | None = None
    end_angle: float | None = None


def _pt(value) -> Point:
    return (float(value[0]), float(value[1]))


#: Why each type is not dimensioned (the engines' spellings; COM reports
#: AcDbPolyline as POLYLINE and every dimension class as ...DIMENSION).
_NOT_DIMENSIONED: dict[str, str] = {
    "ELLIPSE": (
        "an ellipse has no diameter or radius to dimension; dimension its axes with "
        "dimension_linear"
    ),
    "SPLINE": (
        "a spline has no size a drafting standard dimensions; dimension its fit points "
        "with dimension_linear"
    ),
}


def _omit(handle: str, kind: str, reason: str, **extra) -> dict:
    return {"handle": str(handle), "type": str(kind), "reason": reason, **extra}


#: The ``include`` kind each source is drawn under.
_KIND_OF = {"line": "linear", "polyline": "linear", "circle": "diameter", "arc": "radius"}


def normalize(infos: Iterable[Any]) -> tuple[list[Source], list[dict]]:
    """``(sources, omitted)`` for the selected ``EntityInfo`` rows: every entity
    dimension_auto can read, and a row for every one it cannot."""
    sources: list[Source] = []
    omitted: list[dict] = []
    seen: set[str] = set()
    for info in infos:
        handle = str(info.handle)
        if handle.upper() in seen:
            continue
        seen.add(handle.upper())
        kind = str(info.type or "").upper()
        props = dict(info.properties or {})
        if kind in _NOT_DIMENSIONED:
            omitted.append(_omit(handle, kind, _NOT_DIMENSIONED[kind]))
            continue
        if kind.endswith("DIMENSION"):
            omitted.append(_omit(handle, kind, "a dimension is annotation, not a feature"))
            continue
        tilted = props.get("plane_normal") is not None
        if kind == "LINE" and props.get("start") is not None and props.get("end") is not None:
            source = Source(handle, "line", kind, points=(_pt(props["start"]), _pt(props["end"])))
        elif kind in ("LWPOLYLINE", "POLYLINE") and props.get("points") and not tilted:
            points = tuple(_pt(p) for p in props["points"])
            bulges = tuple(float(b) for b in (props.get("bulges") or [0.0] * len(points)))
            source = Source(
                handle, "polyline", kind, points, bulges, closed=bool(props.get("closed"))
            )
        elif kind in ("CIRCLE", "ARC") and props.get("radius") is not None and not tilted:
            source = Source(
                handle,
                "circle" if kind == "CIRCLE" else "arc",
                kind,
                center=_pt(props["center"]),
                radius=float(props["radius"]),
                start_angle=float(props["start_angle"]) if kind == "ARC" else None,
                end_angle=float(props["end_angle"]) if kind == "ARC" else None,
            )
        elif kind in ("LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC"):
            omitted.append(
                _omit(
                    handle,
                    kind,
                    "it lies in a plane tilted out of WCS XY (or its geometry was not "
                    "readable); its xy is a projection, not its size",
                )
            )
            continue
        else:
            omitted.append(
                _omit(
                    handle,
                    kind,
                    "dimension_auto measures LINE, LWPOLYLINE, CIRCLE and ARC; "
                    f"a {kind or 'entity'} is not one of them",
                )
            )
            continue
        sources.append(source)
    return sources, omitted


# -- the plan --------------------------------------------------------------------


@dataclass
class PlannedDim:
    """One dimension to draw.

    ``kind`` is the contract that draws it: ``linear`` (``dimension_linear``,
    measured along ``rotation``), ``aligned``, ``diameter`` (``p1`` the first
    chord point - the text side - and ``p2`` the far one) or ``radius``
    (``p1`` the centre, ``p2`` the chord point). ``location`` is where a linear
    or aligned dimension line runs; ``leader`` how far a radial text stands
    beyond its chord point; ``text_point`` where its text is expected.
    """

    kind: str
    feature: str  # "line" | "edge" | "circle" | "arc" | "overall"
    source: str  # the entity it measures ("" for an overall size)
    p1: Point
    p2: Point
    value: float
    location: Point | None = None
    rotation: float = 0.0
    leader: float = 0.0
    text_point: Point | None = None
    segment: int | None = None
    axis: str | None = None
    text_override: str | None = None
    members: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "kind": self.kind,
            "feature": self.feature,
            "value": round(self.value, 6),
            "points": [_round(self.p1), _round(self.p2)],
        }
        if self.source:
            out["source"] = self.source
        if self.segment is not None:
            out["segment"] = self.segment
        if self.axis:
            out["axis"] = self.axis
        if self.text_override:
            out["text"] = self.text_override
        if len(self.members) > 1:
            out["members"] = list(self.members)
        if self.text_point is not None:
            out["text_at"] = _round(self.text_point)
        return out


def _round(point: Point) -> list[float]:
    return [round(point[0], 6), round(point[1], 6)]


@dataclass
class Plan:
    dims: list[PlannedDim] = field(default_factory=list)
    omitted: list[dict] = field(default_factory=list)
    redundant: list[dict] = field(default_factory=list)
    box: tuple[float, float, float, float] | None = None


def _linear_value(p1: Point, p2: Point, rotation_deg: float) -> float:
    u = (math.cos(math.radians(rotation_deg)), math.sin(math.radians(rotation_deg)))
    return abs((p2[0] - p1[0]) * u[0] + (p2[1] - p1[1]) * u[1])


def _linear_text(p1: Point, p2: Point, location: Point, rotation_deg: float) -> Point:
    """Where a linear dimension's text stands: the dimension line's middle."""
    u = (math.cos(math.radians(rotation_deg)), math.sin(math.radians(rotation_deg)))
    mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    t = (mid[0] - location[0]) * u[0] + (mid[1] - location[1]) * u[1]
    return (location[0] + t * u[0], location[1] + t * u[1])


def _linear(feature, source, p1, p2, location, rotation, *, segment=None, axis=None) -> PlannedDim:
    return PlannedDim(
        "linear",
        feature,
        source,
        p1,
        p2,
        _linear_value(p1, p2, rotation),
        location=location,
        rotation=rotation,
        text_point=_linear_text(p1, p2, location, rotation),
        segment=segment,
        axis=axis,
    )


def plan_line_run(lines: Sequence[Source], style: str, offset: float) -> list[PlannedDim]:
    """The 1.7 ``dimension_auto`` run over LINEs, point for point."""
    segs = [(s.handle, *s.points[0], *s.points[1]) for s in lines]
    off = float(offset)
    out: list[PlannedDim] = []
    if not segs:
        return out
    if style == "chain":
        # each line its own dimension, offset along its own left normal
        for handle, sx, sy, ex, ey in segs:
            mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
            dx, dy = ex - sx, ey - sy
            length = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / length, dx / length
            rotation = math.degrees(math.atan2(dy, dx))
            out.append(
                _linear(
                    "line", handle, (sx, sy), (ex, ey), (mx + nx * off, my + ny * off), rotation
                )
            )
    elif style == "baseline":
        _h0, sx0, sy0, ex0, ey0 = segs[0]
        dx, dy = ex0 - sx0, ey0 - sy0
        length0 = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length0, dy / length0
        base_rot = math.degrees(math.atan2(dy, dx))
        for i, (handle, _sx, _sy, ex, ey) in enumerate(segs):
            location = (sx0 - uy * (off + i * off), sy0 + ux * (off + i * off))
            out.append(_linear("line", handle, (sx0, sy0), (ex, ey), location, base_rot))
    else:  # ordinate: stacked X / Y distances, each feature on its own rung
        for i, (handle, sx, sy, ex, ey) in enumerate(segs):
            rung = off + i * off
            if abs(ex - sx) > 1e-9:
                location = (sx + (ex - sx) / 2.0, max(sy, ey) + rung)
                out.append(_linear("line", handle, (sx, sy), (ex, sy), location, 0.0))
            if abs(ey - sy) > 1e-9:
                location = (max(sx, ex) + rung, (sy + ey) / 2.0)
                out.append(_linear("line", handle, (ex, sy), (ex, ey), location, 90.0))
    return out


def _arc_contains(start: float, end: float, angle: float) -> bool:
    sweep = (end - start) % 360.0 or 360.0
    return (angle - start) % 360.0 <= sweep + 1e-12


def feature_points(source: Source) -> list[Point]:
    """The points of ``source`` a dimension may be drawn to: ends, vertices,
    a circle's quadrants, an arc's ends and the quadrants inside its sweep."""
    if source.kind in ("line", "polyline"):
        return list(source.points)
    cx, cy = source.center  # type: ignore[misc]
    r = float(source.radius)  # type: ignore[arg-type]
    quadrants = [
        (0.0, (cx + r, cy)),
        (90.0, (cx, cy + r)),
        (180.0, (cx - r, cy)),
        (270.0, (cx, cy - r)),
    ]
    if source.kind == "circle":
        return [p for _a, p in quadrants]
    start, end = float(source.start_angle), float(source.end_angle)  # type: ignore[arg-type]
    ends = [
        (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
        for a in (start, end)
    ]
    return ends + [p for a, p in quadrants if _arc_contains(start, end, a)]


def _box(sources: Sequence[Source]) -> tuple[float, float, float, float] | None:
    points = [p for s in sources for p in feature_points(s)]
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _exit_distance(point: Point, u: Point, box) -> float:
    """How far along ``u`` from ``point`` the part box ends (0 when outside)."""
    x0, y0, x1, y1 = box
    ts = []
    if u[0] > 1e-12:
        ts.append((x1 - point[0]) / u[0])
    elif u[0] < -1e-12:
        ts.append((x0 - point[0]) / u[0])
    if u[1] > 1e-12:
        ts.append((y1 - point[1]) / u[1])
    elif u[1] < -1e-12:
        ts.append((y0 - point[1]) / u[1])
    return max(0.0, min(ts)) if ts else 0.0


def _signed_area(points: Sequence[Point]) -> float:
    return 0.5 * sum(
        points[i][0] * points[(i + 1) % len(points)][1]
        - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))
    )


def _plan_polyline(source: Source, box, centre: Point, offset: float, rows: dict, plan: Plan):
    points = list(source.points)
    count = len(points) if source.closed else len(points) - 1
    area = _signed_area(points) if source.closed and len(points) >= 3 else 0.0
    x0, y0, x1, y1 = box
    for i in range(max(count, 0)):
        p, q = points[i], points[(i + 1) % len(points)]
        bulge = source.bulges[i] if i < len(source.bulges) else 0.0
        if abs(bulge) > 1e-12:
            plan.omitted.append(
                _omit(
                    source.handle,
                    source.type,
                    "an arc segment of a polyline is not dimensioned; dimension_radius "
                    "draws its radius",
                    segment=i,
                )
            )
            continue
        dx, dy = q[0] - p[0], q[1] - p[1]
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            plan.omitted.append(
                _omit(source.handle, source.type, "a zero-length segment", segment=i)
            )
            continue
        if area > 0.0:  # counter-clockwise: outward is the right-hand side
            n = (dy / length, -dx / length)
        elif area < 0.0:
            n = (-dy / length, dx / length)
        else:  # open: away from the selection's centre
            n = (-dy / length, dx / length)
            mid = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
            if (mid[0] - centre[0]) * n[0] + (mid[1] - centre[1]) * n[1] < 0.0:
                n = (-n[0], -n[1])
        angle = math.atan2(dy, dx)
        horizontal = abs(math.sin(angle)) <= AXIS_TOLERANCE_RAD
        vertical = abs(math.cos(angle)) <= AXIS_TOLERANCE_RAD
        if horizontal:
            side = "bottom" if n[1] < 0.0 else "top"
            row = y0 - offset if side == "bottom" else y1 + offset
            rows[side] = True
            location = ((p[0] + q[0]) / 2.0, row)
            plan.dims.append(_linear("edge", source.handle, p, q, location, 0.0, segment=i))
        elif vertical:
            side = "left" if n[0] < 0.0 else "right"
            row = x0 - offset if side == "left" else x1 + offset
            rows[side] = True
            location = (row, (p[1] + q[1]) / 2.0)
            plan.dims.append(_linear("edge", source.handle, p, q, location, 90.0, segment=i))
        else:
            # an aligned dimension stands on the left of p1 -> p2 (measured on
            # ezdxf; AddDimAligned takes the point itself): order the ends so
            # that the left is outward
            left = (-dy / length, dx / length)
            a, b = (p, q) if left[0] * n[0] + left[1] * n[1] > 0.0 else (q, p)
            mid = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
            location = (mid[0] + n[0] * offset, mid[1] + n[1] * offset)
            plan.dims.append(
                PlannedDim(
                    "aligned",
                    "edge",
                    source.handle,
                    a,
                    b,
                    length,
                    location=location,
                    text_point=location,
                    segment=i,
                )
            )


def _radial(source: Source, box, centre: Point, offset: float, placed: list[Point], **extra):
    cx, cy = source.center  # type: ignore[misc]
    r = float(source.radius)  # type: ignore[arg-type]
    if source.kind == "circle":
        vx, vy = cx - centre[0], cy - centre[1]
        norm = math.hypot(vx, vy)
        u = (vx / norm, vy / norm) if norm > 1e-9 else (math.sqrt(0.5), math.sqrt(0.5))
    else:
        start, end = float(source.start_angle), float(source.end_angle)  # type: ignore[arg-type]
        middle = math.radians(start + ((end - start) % 360.0 or 360.0) / 2.0)
        u = (math.cos(middle), math.sin(middle))
    chord = (cx + r * u[0], cy + r * u[1])
    leader = _exit_distance(chord, u, box) + offset
    for _ in range(MAX_BUMPS):
        text = (cx + (r + leader) * u[0], cy + (r + leader) * u[1])
        if all(math.dist(text, q) >= CLEARANCE for q in placed):
            break
        leader += offset
    text = (cx + (r + leader) * u[0], cy + (r + leader) * u[1])
    placed.append(text)
    if source.kind == "circle":
        far = (cx - r * u[0], cy - r * u[1])
        return PlannedDim(
            "diameter", "circle", source.handle, chord, far, 2.0 * r,
            leader=leader, text_point=text, **extra,
        )  # fmt: skip
    return PlannedDim(
        "radius", "arc", source.handle, (cx, cy), chord, r, leader=leader, text_point=text, **extra
    )


def _covered(axis: str, box, dims: Sequence[PlannedDim], eps: float) -> PlannedDim | None:
    """The planned dimension that already measures the box's width (``x``) or height."""
    x0, y0, x1, y1 = box
    lo, hi = (x0, x1) if axis == "x" else (y0, y1)
    k = 0 if axis == "x" else 1
    want = 0.0 if axis == "x" else 90.0
    for dim in dims:
        span = sorted((dim.p1[k], dim.p2[k]))
        if dim.kind == "linear":
            measured_on_axis = abs(((dim.rotation - want + 90.0) % 180.0) - 90.0) <= 1e-9
            if measured_on_axis and abs(span[0] - lo) <= eps and abs(span[1] - hi) <= eps:
                return dim
        elif dim.kind == "aligned":
            other = 1 - k
            if abs(dim.p1[other] - dim.p2[other]) <= eps and (
                abs(span[0] - lo) <= eps and abs(span[1] - hi) <= eps
            ):
                return dim
        elif dim.kind == "diameter":
            c = (dim.p1[k] + dim.p2[k]) / 2.0
            r = dim.value / 2.0
            if abs(c - r - lo) <= eps and abs(c + r - hi) <= eps:
                return dim
    return None


def _extreme(points: Sequence[Point], k: int, smallest: bool) -> Point:
    other = 1 - k
    key = (lambda p: (p[k], p[other])) if smallest else (lambda p: (-p[k], p[other]))
    return min(points, key=key)


def plan_dimensions(
    sources: Sequence[Source],
    *,
    style: str = "chain",
    offset: float = 10.0,
    include: Sequence[str] = INCLUDE,
    group_equal: bool = False,
) -> Plan:
    """The dimensions of ``sources``, in drawing order: the LINE run, the edges,
    the overall sizes, the diameters and the radii.

    The part box is the whole selection's, whatever ``include`` draws; a source
    whose kind ``include`` leaves out is listed in ``omitted`` by name."""
    plan = Plan()
    off = float(offset)
    drawn = []
    for source in sources:
        wanted = _KIND_OF[source.kind]
        if wanted in include:
            drawn.append(source)
        else:
            plan.omitted.append(_omit(source.handle, source.type, f"include leaves out '{wanted}'"))
    lines = [s for s in drawn if s.kind == "line"]
    plan.dims.extend(plan_line_run(lines, style, off))
    if all(s.kind == "line" for s in sources):
        return plan  # a selection of LINEs alone is dimensioned as 1.7 did
    others = [s for s in drawn if s.kind != "line"]
    box = _box(sources)
    plan.box = box
    if box is None:
        return plan
    centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    scale = max(1.0, *(abs(v) for v in box))
    eps = 1e-9 * scale
    rows: dict[str, bool] = {}
    for source in others:
        if source.kind == "polyline":
            _plan_polyline(source, box, centre, off, rows, plan)

    circles = [s for s in others if s.kind == "circle"]
    arcs = [s for s in others if s.kind == "arc"]
    groups: list[list[Source]] = []
    for circle in circles:
        for group in groups if group_equal else ():
            if abs(float(group[0].radius) - float(circle.radius)) <= eps:  # type: ignore[arg-type]
                group.append(circle)
                break
        else:
            groups.append([circle])

    if "overall" in include:
        # a lone circle's diameter is its width and its height
        diameters = [
            PlannedDim(
                "diameter",
                "circle",
                s.handle,
                (s.center[0] - s.radius, s.center[1]),  # type: ignore[index,operator]
                (s.center[0] + s.radius, s.center[1]),  # type: ignore[index,operator]
                2.0 * float(s.radius),  # type: ignore[arg-type]
            )
            for s in circles
        ]
        points = [p for s in sources for p in feature_points(s)]
        for axis, k, side in (("x", 0, "bottom"), ("y", 1, "right")):
            covering = _covered(axis, box, plan.dims + diameters, eps)
            if covering is not None:
                by = {"kind": covering.kind, "source": covering.source}
                if covering.segment is not None:
                    by["segment"] = covering.segment
                plan.redundant.append({"kind": "overall", "axis": axis, "covered_by": by})
                continue
            low, high = _extreme(points, k, True), _extreme(points, k, False)
            if abs(high[k] - low[k]) <= eps:
                continue  # no extent along this axis
            step = off * (2.0 if rows.get(side) else 1.0)
            if axis == "x":
                location = ((low[0] + high[0]) / 2.0, box[1] - step)
                dim = _linear("overall", "", low, high, location, 0.0, axis="x")
            else:
                location = (box[2] + step, (low[1] + high[1]) / 2.0)
                dim = _linear("overall", "", low, high, location, 90.0, axis="y")
            plan.dims.append(dim)

    placed = [d.text_point for d in plan.dims if d.text_point is not None]
    for group in groups:
        extra: dict[str, Any] = {"members": tuple(s.handle for s in group)}
        if len(group) > 1:
            extra["text_override"] = f"{len(group)}{TIMES} <>"
        plan.dims.append(_radial(group[0], box, centre, off, placed, **extra))
    for arc in arcs:
        plan.dims.append(_radial(arc, box, centre, off, placed, members=(arc.handle,)))
    return plan


# -- existing dimensions ------------------------------------------------------------


def match_existing(
    planned: Sequence[PlannedDim], existing: Sequence[dict], eps: float
) -> tuple[list[PlannedDim], list[dict]]:
    """``(to draw, skipped)``: a planned dimension is skipped when an existing
    DIMENSION, bound to the drawing by ``bind_dimension``, measures the same
    feature - a diameter or radius on the same circle or arc, or a linear /
    aligned one between the same two points with the same measured value.

    ``existing`` rows: ``{"handle", "dimtype", "refs": set of handles,
    "points": (p1, p2) or None, "value"}``.
    """
    keep: list[PlannedDim] = []
    skipped: list[dict] = []
    for dim in planned:
        hit = None
        for row in existing:
            if dim.kind in ("diameter", "radius"):
                if row["dimtype"] in (3, 4) and set(dim.members or (dim.source,)) & row["refs"]:
                    hit = row
            elif row["dimtype"] in (0, 1) and row.get("points") and row.get("value") is not None:
                a, b = row["points"]
                same = (math.dist(a, dim.p1) <= eps and math.dist(b, dim.p2) <= eps) or (
                    math.dist(a, dim.p2) <= eps and math.dist(b, dim.p1) <= eps
                )
                if same and abs(float(row["value"]) - dim.value) <= eps:
                    hit = row
            if hit is not None:
                break
        if hit is None:
            keep.append(dim)
            continue
        entry = {"kind": dim.kind, "feature": dim.feature, "existing": hit["handle"]}
        if dim.source:
            entry["source"] = dim.source
        if dim.segment is not None:
            entry["segment"] = dim.segment
        if dim.axis:
            entry["axis"] = dim.axis
        skipped.append(entry)
    return keep, skipped


# -- the result ------------------------------------------------------------------


class DimensionAutoResult(list):
    """What ``dimension_auto`` drew - a list of ``EntityInfo``, as 1.7 returned
    - and the report beside it. ``to_dict`` is the tool's answer."""

    def __init__(
        self,
        created: Iterable[Any] = (),
        *,
        skipped_existing: Sequence[dict] = (),
        omitted: Sequence[dict] = (),
        redundant: Sequence[dict] = (),
        overlap: dict | None = None,
        planned: Sequence[PlannedDim] = (),
        dry_run: bool = False,
        layer: str | None = None,
    ):
        super().__init__(created)
        self.skipped_existing = list(skipped_existing)
        self.omitted = list(omitted)
        self.redundant = list(redundant)
        self.overlap = overlap
        self.planned = list(planned)
        self.dry_run = bool(dry_run)
        self.layer = layer

    @property
    def created(self) -> list[str]:
        return [str(info.handle) for info in self]

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "ok": True,
            "created": self.created,
            "skipped_existing": self.skipped_existing,
            "omitted": self.omitted,
        }
        if self.redundant:
            out["redundant"] = self.redundant
        if self.layer:
            out["layer"] = self.layer
        if self.overlap is not None:
            out["dim_overlap"] = self.overlap
        if self.dry_run:
            out["dry_run"] = True
            out["planned"] = [dim.to_dict() for dim in self.planned]
        return out
