"""The mechanical part model: a profile plus a list of typed features.

Pure: no backend, no async, no DXF. A part is either **revolved** (a stack of
segments along +X, the axis at y = 0, the left end at x = 0) or **prismatic**
(a closed outline in part-local XY plus a thickness along +Z). Every coordinate
here is part-local; `engineering/mech/draw.py` is the only module that maps
part-local to WCS.

Everything is validated before anything is drawn (the repository's
validate-first rule), and every refusal names the offending path the way
`pid_from_spec` does: ``segments[2].d_outer: ...``.

The feature surface is duck-typed on purpose, so this module does not import
`features.py` at module scope and the two are testable independently:

    feature.id                       -> str
    feature.validate(part)           -> None, ValueError naming what is wrong
    feature.to_dict()                -> dict accepted by feature_from_dict
    feature.removal_box(part)        -> (x0, y0, x1, y1) | None   (optional)
    feature.expand()                 -> tuple[Feature, ...]       (optional)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from engineering.measure import is_self_intersecting
from engineering.mech.primitives import Pt
from engineering.mech.standards.materials import hatch_for

_EPS = 1e-9


class Segment(NamedTuple):
    """One turned step of a revolved part, left to right along +X."""

    length: float
    d_outer: float
    d_inner: float = 0.0
    taper_to: float | None = None


@dataclass(frozen=True)
class RevolvedPart:
    name: str
    segments: tuple[Segment, ...]
    features: tuple[Any, ...] = ()
    material: str = "steel"


@dataclass(frozen=True)
class PrismaticPart:
    name: str
    outline: tuple[Pt, ...]
    thickness: float
    features: tuple[Any, ...] = ()
    material: str = "steel"


Part = RevolvedPart | PrismaticPart

PART_KINDS: tuple[str, ...] = ("revolved", "prismatic")

_SEGMENT_KEYS = frozenset({"length", "d_outer", "d_inner", "taper_to"})


# -- number guards -----------------------------------------------------------


def _finite(value: Any, path: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{path}: expected a number, got {value!r}.") from None
    if not math.isfinite(number):
        raise ValueError(f"{path}: {value!r} is not a finite number.")
    return number


def _positive(value: Any, path: str) -> float:
    number = _finite(value, path)
    if number <= _EPS:
        raise ValueError(f"{path}: must be greater than zero, got {number}.")
    return number


def _non_negative(value: Any, path: str) -> float:
    number = _finite(value, path)
    if number < -_EPS:
        raise ValueError(f"{path}: must not be negative, got {number}.")
    return max(number, 0.0)


# -- geometry helpers (Tasks 3-7 read the part through these) ----------------


def segment_bounds(part: RevolvedPart) -> tuple[tuple[float, float], ...]:
    """``((x0, x1), ...)`` per segment, left to right from x = 0."""
    bounds: list[tuple[float, float]] = []
    x = 0.0
    for segment in part.segments:
        bounds.append((x, x + segment.length))
        x += segment.length
    return tuple(bounds)


def part_length(part: Part) -> float:
    if isinstance(part, RevolvedPart):
        return float(sum(segment.length for segment in part.segments))
    x0, _, x1, _ = outline_bbox(part)
    return x1 - x0


def part_max_diameter(part: RevolvedPart) -> float:
    diameters = [segment.d_outer for segment in part.segments]
    diameters += [s.taper_to for s in part.segments if s.taper_to is not None]
    return float(max(diameters))


def _segment_at(part: RevolvedPart, x: float) -> tuple[int, float, float, Segment]:
    bounds = zip(segment_bounds(part), part.segments, strict=True)  # one bound per segment
    for index, ((x0, x1), segment) in enumerate(bounds):
        if x0 - _EPS <= x <= x1 + _EPS:
            return index, x0, x1, segment
    raise ValueError(f"x={x} is outside the part (0 .. {part_length(part)} mm).")


def outer_radius_at(part: RevolvedPart, x: float) -> float:
    """Outer radius at an axial station, honouring a segment's taper."""
    _, x0, x1, segment = _segment_at(part, float(x))
    if segment.taper_to is None:
        return segment.d_outer / 2.0
    span = x1 - x0
    t = 0.0 if span <= _EPS else (float(x) - x0) / span
    return (segment.d_outer + t * (segment.taper_to - segment.d_outer)) / 2.0


def inner_radius_at(part: RevolvedPart, x: float) -> float:
    """Bore radius at an axial station; 0.0 where the part is solid."""
    _, _, _, segment = _segment_at(part, float(x))
    return segment.d_inner / 2.0


def outline_bbox(part: PrismaticPart) -> tuple[float, float, float, float]:
    xs = [p[0] for p in part.outline]
    ys = [p[1] for p in part.outline]
    return (min(xs), min(ys), max(xs), max(ys))


def point_in_outline(part: PrismaticPart, x: float, y: float) -> bool:
    """Even-odd ray cast against the closed outline; a point on an edge counts as in."""
    points = part.outline
    count = len(points)
    inside = False
    for i in range(count):
        ax, ay = points[i]
        bx, by = points[(i + 1) % count]
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        on_span = min(ax, bx) - _EPS <= x <= max(ax, bx) + _EPS
        on_span = on_span and min(ay, by) - _EPS <= y <= max(ay, by) + _EPS
        if abs(cross) < 1e-9 and on_span:
            return True
        if (ay > y) != (by > y):
            t = (y - ay) / (by - ay)
            if x < ax + t * (bx - ax):
                inside = not inside
    return inside


# -- construction ------------------------------------------------------------


def _segment_from(index: int, raw: Any) -> Segment:
    if not isinstance(raw, dict):
        raise ValueError(f"segments[{index}]: expected a dict, got {type(raw).__name__}.")
    unknown = set(raw) - _SEGMENT_KEYS
    if unknown:
        raise ValueError(
            f"segments[{index}]: unknown key(s) {sorted(unknown)}; "
            f"a segment takes {sorted(_SEGMENT_KEYS)}."
        )
    length = _positive(raw.get("length"), f"segments[{index}].length")
    d_outer = _positive(raw.get("d_outer"), f"segments[{index}].d_outer")
    d_inner = _non_negative(raw.get("d_inner") or 0.0, f"segments[{index}].d_inner")
    if d_inner >= d_outer - _EPS:
        raise ValueError(
            f"segments[{index}].d_inner: {d_inner} mm is not smaller than d_outer "
            f"{d_outer} mm. A bore that changes diameter is two segments."
        )
    taper_to = raw.get("taper_to")
    if taper_to is not None:
        taper_to = _positive(taper_to, f"segments[{index}].taper_to")
        if taper_to <= d_inner + _EPS:
            raise ValueError(
                f"segments[{index}].taper_to: {taper_to} mm is not larger than the bore "
                f"{d_inner} mm."
            )
    return Segment(length, d_outer, d_inner, taper_to)


def _outline_from(raw: Any) -> tuple[Pt, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("outline: expected a non-empty list of [x, y] points.")
    points: list[Pt] = []
    for index, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"outline[{index}]: expected [x, y], got {item!r}.")
        points.append(
            (
                _finite(item[0], f"outline[{index}].x"),
                _finite(item[1], f"outline[{index}].y"),
            )
        )
    if len(points) > 1 and math.dist(points[0], points[-1]) < _EPS:
        points = points[:-1]
    distinct: list[Pt] = []
    for point in points:
        if not distinct or math.dist(distinct[-1], point) > _EPS:
            distinct.append(point)
    if len(distinct) < 3:
        raise ValueError(
            f"outline: a closed outline needs at least 3 distinct vertices, got {len(distinct)}."
        )
    if is_self_intersecting(distinct) is True:
        raise ValueError(
            "outline: the closed outline crosses itself. A crossed loop has no single "
            "enclosed area, so neither the view engine nor the section hatch can be right."
        )
    return tuple(distinct)


def _features_from(raw: Any) -> tuple[Any, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("features: expected a list of feature dicts.")
    if not raw:
        # No feature dicts to build, so `features.py` is not imported at all:
        # a featureless part stays usable while Task 3 is still unwritten, and
        # the import stays local either way (features.py imports this module).
        return ()
    from engineering.mech.features import feature_from_dict  # local: avoids an import cycle

    built: list[Any] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"features[{index}]: expected a dict with a 'kind' key.")
        try:
            feature = feature_from_dict(item)
            expand = getattr(feature, "expand", None)
            expanded = tuple(expand()) if callable(expand) else (feature,)
        except ValueError as exc:
            raise ValueError(f"features[{index}]: {exc}") from exc
        for one in expanded:
            if one.id in seen:
                raise ValueError(f"features[{index}]: duplicate feature id {one.id!r}.")
            seen.add(one.id)
            built.append(one)
    return tuple(built)


def validate_part(part: Part) -> None:
    """Every feature validates against the part, and no two remove the same material."""
    for index, feature in enumerate(part.features):
        try:
            feature.validate(part)
        except ValueError as exc:
            raise ValueError(f"features[{index}] ({feature.id}): {exc}") from exc

    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for feature in part.features:
        getter = getattr(feature, "removal_box", None)
        box = getter(part) if callable(getter) else None
        if box is not None:
            x0, y0, x1, y1 = (float(v) for v in box)
            boxes.append((feature.id, (x0, y0, x1, y1)))

    for a in range(len(boxes)):
        id_a, (ax0, ay0, ax1, ay1) = boxes[a]
        for b in range(a + 1, len(boxes)):
            id_b, (bx0, by0, bx1, by1) = boxes[b]
            ox0, ox1 = max(ax0, bx0), min(ax1, bx1)
            oy0, oy1 = max(ay0, by0), min(ay1, by1)
            if ox1 - ox0 > _EPS and oy1 - oy0 > _EPS:
                raise ValueError(
                    f"features {id_a!r} and {id_b!r} remove the same material "
                    f"(overlap x {ox0:.3f}..{ox1:.3f}, y {oy0:.3f}..{oy1:.3f}). "
                    "Move one of them, or merge them into a single feature."
                )


def build_part(spec: dict) -> Part:
    """Validate a part spec and return the typed model. ValueError names the path."""
    if not isinstance(spec, dict):
        raise ValueError(f"spec: expected a dict describing the part, got {type(spec).__name__}.")
    kind = str(spec.get("kind", "")).strip().lower()
    if kind not in PART_KINDS:
        raise ValueError(f"kind: expected one of {PART_KINDS}, got {spec.get('kind')!r}.")
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name: a non-empty part name is required.")
    material = str(spec.get("material", "steel"))
    try:
        hatch_for(material)
    except ValueError as exc:
        raise ValueError(f"material: {exc}") from exc

    if kind == "revolved":
        raw_segments = spec.get("segments")
        if not isinstance(raw_segments, (list, tuple)) or not raw_segments:
            raise ValueError("segments: a revolved part needs at least one segment.")
        part: Part = RevolvedPart(
            name=name.strip(),
            segments=tuple(_segment_from(i, s) for i, s in enumerate(raw_segments)),
            material=material,
        )
    else:
        part = PrismaticPart(
            name=name.strip(),
            outline=_outline_from(spec.get("outline")),
            thickness=_positive(spec.get("thickness"), "thickness"),
            material=material,
        )

    part = replace(part, features=_features_from(spec.get("features")))
    validate_part(part)
    return part


def part_to_dict(part: Part) -> dict:
    """The dict `build_part` accepts back, unchanged."""
    common = {
        "name": part.name,
        "material": part.material,
        "features": [feature.to_dict() for feature in part.features],
    }
    if isinstance(part, RevolvedPart):
        return {
            "kind": "revolved",
            **common,
            "segments": [
                {
                    "length": s.length,
                    "d_outer": s.d_outer,
                    "d_inner": s.d_inner,
                    "taper_to": s.taper_to,
                }
                for s in part.segments
            ],
        }
    return {
        "kind": "prismatic",
        **common,
        "outline": [[x, y] for x, y in part.outline],
        "thickness": part.thickness,
    }
