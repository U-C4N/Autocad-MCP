"""Every feature of the part model: validation, projected geometry, dimensions.

Pure: no backend, no async, no DXF. A feature answers four questions about the
part it sits on - *is my placement legal*, *what do I contribute to this view*,
*what do I contribute to a cut face*, *what dimension do I want* - and five
structural ones the view engine asks:

``removal_box(part)``
    the axis-aligned box of material this feature removes, **in the front
    view's plane** (revolved: x along the axis, y radial; prismatic: the
    outline's own XY). ``None`` opts out - used by features whose removal is
    not a box (an annulus, an axisymmetric bore).
``radial_edit(part)``
    ``{"kind": "inner"|"outer", "x0", "x1", "radius"}`` records that change the
    revolved radial profile, which is what makes both the silhouette and the
    section cut face come out of one calculation.
``corner_edit(part)``
    ``{"corner", "before", "after", "arc"}`` - replace one silhouette corner
    with a chamfer line or a tangent fillet arc.
``outline_edit(part)``
    the prismatic equivalent, keyed by outline vertex index.
``omission(part, view)``
    why this feature has no geometry in that view, for the honesty rule: a
    feature that contributes nothing and edits nothing is reported in the
    view's ``omitted`` list rather than silently dropped.

Two features reuse the repository rather than restating a table:
``Keyway`` takes its width and depth from ``engineering.keyway`` (DIN 6885-1)
and ``GearTeeth`` takes its outline from ``engineering.gear``'s involute
generator. ``Thread`` takes its pitch from ``standards/threads.py`` (ISO 261)
and its geometry from ISO 6410. ``Undercut``, ``RetainingGroove``,
``CentreHole`` and ``ORingGroove`` take their dimensions from the registered
standards tables, which are not transcribed in this build - so each of them
either receives explicit dimensions or is refused by name. None of them
invents a number.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Protocol, runtime_checkable

from engineering.gear import generate_full_gear_outline
from engineering.keyway import keyway_dimensions
from engineering.mech.part import (
    PrismaticPart,
    RevolvedPart,
    inner_radius_at,
    outer_radius_at,
    outline_bbox,
    part_length,
    point_in_outline,
    segment_bounds,
)
from engineering.mech.primitives import Arc, Circle, DimIntent, HatchArea, Line, Poly, Prim, Text
from engineering.mech.standards.centres import centre_hole_dims
from engineering.mech.standards.orings import KINDS as ORING_KINDS
from engineering.mech.standards.orings import oring_groove_dims
from engineering.mech.standards.rings import ring_groove_dims
from engineering.mech.standards.threads import (
    MINOR_RATIO,
    parse_designation,
    thread_axial_prims,
    thread_end_prims,
)
from engineering.mech.standards.undercuts import undercut_profile

_EPS = 1e-9


@runtime_checkable
class Feature(Protocol):
    """The surface the part model and the view engine rely on."""

    id: str

    def validate(self, part) -> None: ...
    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]: ...
    def cut_prims(self, part, plane, ctx: dict) -> tuple[Prim, ...]: ...
    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]: ...


# -- shared helpers ----------------------------------------------------------


def mirror_about_x(prims: Iterable[Prim]) -> tuple[Prim, ...]:
    """Mirror primitives about the y = 0 axis (the revolved part's axis)."""
    out: list[Prim] = []
    for prim in prims:
        if isinstance(prim, Line):
            out.append(Line((prim.p1[0], -prim.p1[1]), (prim.p2[0], -prim.p2[1]), prim.role))
        elif isinstance(prim, Arc):
            out.append(
                Arc(
                    (prim.center[0], -prim.center[1]),
                    prim.radius,
                    (-prim.end_deg) % 360.0,
                    (-prim.start_deg) % 360.0,
                    prim.role,
                )
            )
        elif isinstance(prim, Circle):
            out.append(Circle((prim.center[0], -prim.center[1]), prim.radius, prim.role))
        elif isinstance(prim, Poly):
            out.append(Poly(tuple((x, -y) for x, y in prim.points), prim.closed, prim.role))
        elif isinstance(prim, HatchArea):
            out.append(
                HatchArea(
                    tuple(tuple((x, -y) for x, y in loop) for loop in prim.loops),
                    prim.material,
                )
            )
        elif isinstance(prim, Text):
            out.append(
                Text((prim.at[0], -prim.at[1]), prim.text, prim.height, prim.rotation, prim.role)
            )
        else:  # pragma: no cover - the union is closed
            raise ValueError(f"mirror_about_x: unknown primitive {type(prim).__name__}.")
    return tuple(out)


# -- cutting-line chords -----------------------------------------------------
#
# A section plane meets a feature along a *chord*, not along the feature's
# bounding box. The difference is not cosmetic: a plane 4.9 mm off the centre
# of a 10 mm hole cuts a 1.990 mm chord, and the box would punch the whole
# 10 mm out of the cut face - a wrong hatched area and a wrong drawn outline at
# the same time. Every function here returns parameters measured along ``unit``
# from ``p1``, on the infinite line; the caller clips them to its own leg.


def _box_around(center, radius: float) -> tuple[float, float, float, float]:
    """The axis-aligned box of a circle - what ``removal_box`` reports, and
    exactly why a cut face may not be built from it (see ``cut_spans``)."""
    x, y = float(center[0]), float(center[1])
    return (x - radius, y - radius, x + radius, y + radius)


def _merge_spans(spans) -> tuple[tuple[float, float], ...]:
    """Sorted, non-overlapping spans. A union, so a shape built from several
    pieces (a slot is two circles and a rectangle) reports one interval."""
    items = sorted((float(a), float(b)) for a, b in spans if float(b) - float(a) > _EPS)
    out: list[list[float]] = []
    for a, b in items:
        if out and a <= out[-1][1] + _EPS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return tuple((a, b) for a, b in out)


def _box_chord(box, p1, unit) -> tuple[tuple[float, float], ...]:
    """The exact chord of an axis-aligned rectangle (a slab clip, not a corner
    projection: projecting the four corners onto the line reports the box's
    whole shadow, which is only the chord when the line is parallel to a side)."""
    if box is None:
        return ()
    low, high = -math.inf, math.inf
    for axis in (0, 1):
        direction = float(unit[axis])
        lo, hi = float(box[axis]), float(box[axis + 2])
        origin = float(p1[axis])
        if abs(direction) < 1e-12:
            if origin < lo - _EPS or origin > hi + _EPS:
                return ()
            continue
        a, b = (lo - origin) / direction, (hi - origin) / direction
        if a > b:
            a, b = b, a
        low, high = max(low, a), min(high, b)
    return ((low, high),) if high - low > _EPS else ()


def _circle_chord(center, radius, p1, unit) -> tuple[tuple[float, float], ...]:
    """``2*sqrt(r**2 - d**2)`` about the foot of the perpendicular - exact, and
    empty when the line only grazes the circle."""
    dx, dy = float(center[0]) - float(p1[0]), float(center[1]) - float(p1[1])
    along = dx * unit[0] + dy * unit[1]
    off = -dx * unit[1] + dy * unit[0]
    inside = float(radius) * float(radius) - off * off
    if inside <= _EPS:
        return ()
    half = math.sqrt(inside)
    return ((along - half, along + half),)


def _rotated_box_chord(center, half_u, half_v, angle_deg, p1, unit):
    """The exact chord of a rectangle rotated about its own centre: the line is
    taken into the rectangle's frame and slab-clipped there."""
    theta = math.radians(float(angle_deg))
    cos, sin = math.cos(theta), math.sin(theta)
    dx, dy = float(p1[0]) - float(center[0]), float(p1[1]) - float(center[1])
    origin = (dx * cos + dy * sin, -dx * sin + dy * cos)
    direction = (unit[0] * cos + unit[1] * sin, -unit[0] * sin + unit[1] * cos)
    return _box_chord((-half_u, -half_v, half_u, half_v), origin, direction)


def _polygon_chord(points, p1, unit, *, what: str = "the removed outline"):
    """The intervals of the line that lie inside a simple polygon.

    Crossings are paired in order, exact for a polygon the line crosses
    transversally. A line through a vertex gives an odd count and is refused
    rather than paired wrong - the same rule the outline itself follows.
    """
    ts: list[float] = []
    count = len(points)
    for index in range(count):
        ax, ay = points[index]
        bx, by = points[(index + 1) % count]
        ex, ey = bx - ax, by - ay
        denominator = ex * unit[1] - ey * unit[0]
        if abs(denominator) < 1e-12:
            continue
        s = ((p1[0] - ax) * unit[1] - (p1[1] - ay) * unit[0]) / denominator
        if -1e-9 <= s <= 1.0 + 1e-9:
            px, py = ax + s * ex, ay + s * ey
            ts.append((px - p1[0]) * unit[0] + (py - p1[1]) * unit[1])
    ts = sorted({round(t, 9) for t in ts})
    if len(ts) % 2 != 0:
        raise ValueError(
            f"section: the cutting plane grazes a vertex of {what}, so its crossings cannot "
            "be paired. Move the plane off the vertex rather than accept a guessed cut face."
        )
    return tuple((ts[i], ts[i + 1]) for i in range(0, len(ts), 2))


def _number(value: Any, path: str, *, positive: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{path}: expected a number, got {value!r}.") from None
    if not math.isfinite(number):
        raise ValueError(f"{path}: {value!r} is not a finite number.")
    if positive and number <= _EPS:
        raise ValueError(f"{path}: must be greater than zero, got {number}.")
    return number


def _require_revolved(part, kind: str) -> RevolvedPart:
    if not isinstance(part, RevolvedPart):
        raise ValueError(f"{kind} applies to a revolved part, not a {type(part).__name__}.")
    return part


def _require_prismatic(part, kind: str) -> PrismaticPart:
    if not isinstance(part, PrismaticPart):
        raise ValueError(f"{kind} applies to a prismatic part, not a {type(part).__name__}.")
    return part


def _segment_index(part: RevolvedPart, index: Any, path: str = "segment") -> int:
    try:
        value = int(index)
    except (TypeError, ValueError):
        raise ValueError(f"{path}: expected a segment index, got {index!r}.") from None
    count = len(part.segments)
    if not 0 <= value < count:
        raise ValueError(
            f"{path} {value} is outside the {count}-segment part (segments 0-{count - 1})."
        )
    return value


def _parse_at(at: str, part: RevolvedPart, *, allow_step: bool = True) -> tuple[str, int]:
    text = str(at or "").strip().lower()
    if text in ("start", "end"):
        return text, 0
    if text.startswith("step:"):
        if not allow_step:
            raise ValueError(f"at {at!r}: this feature is placed at 'start' or 'end' only.")
        try:
            index = int(text.split(":", 1)[1])
        except ValueError:
            raise ValueError(f"at {at!r}: expected 'step:<index>'.") from None
        last = len(part.segments) - 2
        if last < 0 or not 0 <= index <= last:
            raise ValueError(
                f"at 'step:{index}': the part has {len(part.segments)} segment(s), so its steps "
                f"are step:0-step:{max(last, 0)}."
            )
        return "step", index
    raise ValueError(f"at {at!r}: expected 'start', 'end' or 'step:<index>'.")


def _radius_right_end(part: RevolvedPart, index: int) -> float:
    segment = part.segments[index]
    return (segment.taper_to if segment.taper_to is not None else segment.d_outer) / 2.0


def _radius_left_end(part: RevolvedPart, index: int) -> float:
    return part.segments[index].d_outer / 2.0


def _anchor(feature, part: RevolvedPart) -> tuple[float, float, int]:
    """(x, radius, direction) of the corner a chamfer/fillet/undercut sits on."""
    kind, index = _parse_at(feature.at, part)
    if kind == "start":
        return 0.0, _radius_left_end(part, 0), +1
    if kind == "end":
        last = len(part.segments) - 1
        return part_length(part), _radius_right_end(part, last), -1
    x = segment_bounds(part)[index][1]
    left = _radius_right_end(part, index)
    right = _radius_left_end(part, index + 1)
    return (x, left, -1) if left >= right else (x, right, +1)


def _anchor_segment(feature, part: RevolvedPart) -> int:
    """Index of the segment a chamfer at ``feature.at`` actually cuts into.

    ``_anchor`` gives the corner and the direction into the material; the leg
    runs that way, so at a step it belongs to whichever of the two adjacent
    segments carries the larger radius.
    """
    kind, index = _parse_at(feature.at, part)
    if kind == "start":
        return 0
    if kind == "end":
        return len(part.segments) - 1
    left = _radius_right_end(part, index)
    right = _radius_left_end(part, index + 1)
    return index if left >= right else index + 1


def _shoulder(feature, part: RevolvedPart) -> tuple[float, float, int]:
    """(x, radius of the SMALLER cylinder, direction into it) for a relief groove."""
    kind, index = _parse_at(feature.at, part)
    if kind == "start":
        return 0.0, _radius_left_end(part, 0), +1
    if kind == "end":
        last = len(part.segments) - 1
        return part_length(part), _radius_right_end(part, last), -1
    x = segment_bounds(part)[index][1]
    left = _radius_right_end(part, index)
    right = _radius_left_end(part, index + 1)
    return (x, right, +1) if left >= right else (x, left, -1)


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _tuplify(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_tuplify(item) for item in value)
    return value


# -- the base every feature shares -------------------------------------------


_REGISTRY: dict[str, type] = {}


def _register(cls):
    _REGISTRY[cls.KIND] = cls
    return cls


@dataclass(frozen=True, kw_only=True)
class _Base:
    id: str

    KIND: ClassVar[str] = ""
    #: ISO 128-3: a rib or web cut longitudinally is not hatched. Features that
    #: can be one override this as a real field.
    no_section_hatch = False
    #: True when the feature's *front* primitives describe only the +Y half of
    #: an axisymmetric cut, so the view engine mirrors them about the axis. A
    #: keyway is not axisymmetric (it sits on one face); a ring groove is.
    axisymmetric: ClassVar[bool] = False

    def validate(self, part) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("id: a non-empty feature id is required.")

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        return ()

    def cut_prims(self, part, plane, ctx: dict) -> tuple[Prim, ...]:
        return self.prims(part, "front", ctx)

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        return ()

    def removal_box(self, part) -> tuple[float, float, float, float] | None:
        return None

    def cut_spans(self, part, p1, unit) -> tuple[tuple[float, float], ...]:
        """Where a cutting line lies inside the material this feature removes.

        Parameters along ``unit`` measured from ``p1``, in the front view's own
        plane. The default is the exact chord of ``removal_box``, which is the
        truth for a feature whose removal really is a rectangle. A feature
        whose removal is a circle or an obround **must** override this: the
        chord of a hole shrinks to nothing as the plane leaves its centre,
        while its bounding box stays the full diameter wide, so a box-based cut
        face shows an 8 mm gap through solid material where the plane merely
        grazes the hole.
        """
        return _box_chord(self.removal_box(part), p1, unit)

    def cross_section(self, part, station: float) -> dict | None:
        """This feature's shape in a **transverse** cut face at ``station``.

        ``{"start_deg", "end_deg", "path", "radius"}`` - the outer boundary of
        the cut face keeps its arc from ``start_deg`` counter-clockwise to
        ``end_deg`` (the material that survives the feature) and is closed by
        ``path``, a polyline running from the point at ``start_deg`` to the
        point at ``end_deg``, which the engine walks backwards to close the
        loop. ``radius`` is the outside radius the record was built on, so the
        engine can refuse to graft it onto a face of another size rather than
        draw a notch that does not reach the surface.

        ``None`` means the feature has no transverse projection here. If the
        plane really passes through it, the honesty rule reports it instead of
        hatching over it.
        """
        return None

    def radial_edit(self, part) -> tuple[dict, ...]:
        return ()

    def corner_edit(self, part) -> dict | None:
        return None

    def outline_edit(self, part) -> dict | None:
        return None

    def omission(self, part, view: str) -> str | None:
        return f"{type(self).__name__} has no {view} projection"

    def to_dict(self) -> dict:
        payload = {"kind": self.KIND}
        for field_ in fields(self):
            payload[field_.name] = _jsonable(getattr(self, field_.name))
        return payload


def feature_from_dict(d: dict) -> Feature:
    """Build a feature from its dict form; the ``kind`` key selects the class."""
    if not isinstance(d, dict):
        raise ValueError(f"expected a dict with a 'kind' key, got {type(d).__name__}.")
    kind = str(d.get("kind", "")).strip().lower()
    if kind not in _REGISTRY:
        raise ValueError(f"unknown feature kind {d.get('kind')!r}. Known kinds: {FEATURE_KINDS}.")
    cls = _REGISTRY[kind]
    names = {field_.name for field_ in fields(cls)}
    payload = {key: value for key, value in d.items() if key != "kind"}
    unknown = sorted(set(payload) - names)
    if unknown:
        raise ValueError(f"{kind}: unknown key(s) {unknown}; it takes {sorted(names)}.")
    try:
        return cls(**{key: _tuplify(value) for key, value in payload.items()})
    except TypeError as exc:
        raise ValueError(f"{kind}: {exc}") from exc


# -- revolved features -------------------------------------------------------


@_register
@dataclass(frozen=True, kw_only=True)
class Chamfer(_Base):
    """ISO 128-3 corner chamfer at an end or a step. ``size`` is the axial leg."""

    KIND: ClassVar[str] = "chamfer"
    at: str
    size: float
    angle: float = 45.0

    def _legs(self) -> tuple[float, float]:
        """(axial leg, radial leg).

        45 degrees is special-cased to an exact 1.0 slope:
        ``math.tan(math.radians(45))`` is 0.9999999999999999, and the
        overwhelmingly common chamfer would otherwise carry that error into
        every coordinate it produces.
        """
        angle = float(self.angle)
        slope = 1.0 if abs(angle - 45.0) < 1e-12 else math.tan(math.radians(angle))
        return float(self.size), float(self.size) * slope

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "Chamfer")
        _number(self.size, "size")
        angle = _number(self.angle, "angle")
        if not 0.0 < angle < 90.0:
            raise ValueError(f"angle: a chamfer angle is between 0 and 90 degrees, got {angle}.")
        x, radius, direction = _anchor(self, part)
        axial, radial = self._legs()
        if radial >= radius:
            raise ValueError(
                f"size {self.size} at {self.at!r} cuts {radial:.3f} mm off a {radius:.3f} mm "
                "radius: the chamfer is larger than the material it sits on."
            )
        index = _anchor_segment(self, part)
        segment_length = part.segments[index].length
        if axial > segment_length + _EPS:
            far = x + direction * segment_length
            raise ValueError(
                f"size {self.size} at {self.at!r} runs {axial:g} mm along the axis, past "
                f"segment {index}'s {segment_length:g} mm: the chamfer would cross the step "
                f"at x={far:g} and be anchored at a radius that segment does not have."
            )
        del x

    def corner_edit(self, part) -> dict:
        x, radius, direction = _anchor(self, _require_revolved(part, "Chamfer"))
        axial, radial = self._legs()
        if direction > 0:
            before, after = (x, radius - radial), (x + axial, radius)
        else:
            before, after = (x - axial, radius), (x, radius - radial)
        return {"corner": (x, radius), "before": before, "after": after, "arc": None}

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        if view != "side":
            return ()
        part = _require_revolved(part, "Chamfer")
        _, radius, _ = _anchor(self, part)
        _, radial = self._legs()
        return (Circle((0.0, 0.0), radius - radial, "visible"),)

    def removal_box(self, part):
        edit = self.corner_edit(part)
        xs = (edit["before"][0], edit["after"][0])
        ys = (edit["before"][1], edit["after"][1])
        return (min(xs), min(ys), max(xs), max(ys))

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        edit = self.corner_edit(part)
        return (
            DimIntent(
                kind="aligned",
                p1=edit["before"],
                p2=edit["after"],
                text_override=f"{float(self.size):g}x{float(self.angle):g}°",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side") else f"a chamfer has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class Fillet(_Base):
    """A tangent fillet radius in the inside corner of a step."""

    KIND: ClassVar[str] = "fillet"
    at: str
    radius: float

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "Fillet")
        radius = _number(self.radius, "radius")
        kind, index = _parse_at(self.at, part)
        if kind != "step":
            raise ValueError(
                f"at {self.at!r}: a fillet is defined at a step ('step:<index>'); an end "
                "corner takes a Chamfer."
            )
        left = _radius_right_end(part, index)
        right = _radius_left_end(part, index + 1)
        if abs(left - right) < _EPS:
            raise ValueError(f"at {self.at!r}: the two segments have the same diameter, no step.")
        if radius >= abs(left - right):
            raise ValueError(
                f"radius {radius} is not smaller than the step height {abs(left - right):.3f} mm."
            )
        shortest = min(part.segments[index].length, part.segments[index + 1].length)
        if radius >= shortest:
            raise ValueError(
                f"radius {radius} is not smaller than the shortest segment {shortest}."
            )

    def corner_edit(self, part) -> dict:
        part = _require_revolved(part, "Fillet")
        _, index = _parse_at(self.at, part)
        x = segment_bounds(part)[index][1]
        left = _radius_right_end(part, index)
        right = _radius_left_end(part, index + 1)
        radius = float(self.radius)
        if left < right:  # step up going right: the small cylinder is on the left
            corner = (x, left)
            before = (x - radius, left)
            after = (x, left + radius)
            center = (x - radius, left + radius)
            arc = Arc(center, radius, 270.0, 360.0, "visible")
        else:  # step down going right: the small cylinder is on the right
            corner = (x, right)
            before = (x, right + radius)
            after = (x + radius, right)
            center = (x + radius, right + radius)
            arc = Arc(center, radius, 180.0, 270.0, "visible")
        return {"corner": corner, "before": before, "after": after, "arc": arc}

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        edit = self.corner_edit(part)
        return (
            DimIntent(
                kind="radius",
                p1=edit["arc"].center,
                p2=edit["before"],
                text_override=f"R{float(self.radius):g}",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        if view == "front":
            return None
        return "a step fillet projects onto the end view as a circle that coincides with the step"


@_register
@dataclass(frozen=True, kw_only=True)
class Keyway(_Base):
    """DIN 6885-1 keyway on the +Y face of a segment.

    Width and depth come from ``engineering.keyway.keyway_dimensions`` unless
    both are given. Because the slot is modelled on the +Y face, the front view
    shows its floor and its two end walls as visible lines; the width edges
    coincide with the silhouette and are not drawn twice.
    """

    KIND: ClassVar[str] = "keyway"
    segment: int
    length: float
    offset: float = 0.0
    from_end: str = "left"
    width: float | None = None
    depth: float | None = None

    def _table(self, part) -> dict:
        index = _segment_index(part, self.segment)
        diameter = part.segments[index].d_outer
        if self.width is not None and self.depth is not None:
            return {"width": float(self.width), "depth_shaft": float(self.depth)}
        try:
            table = keyway_dimensions(diameter)
        except ValueError as exc:
            raise ValueError(f"DIN 6885: {exc}") from exc
        return {
            "width": float(self.width) if self.width is not None else table["width"],
            "depth_shaft": float(self.depth) if self.depth is not None else table["depth_shaft"],
        }

    def _span(self, part) -> tuple[float, float, float, float, float]:
        index = _segment_index(part, self.segment)
        x0, x1 = segment_bounds(part)[index]
        table = self._table(part)
        offset = float(self.offset)
        length = float(self.length)
        if str(self.from_end).strip().lower() == "right":
            xa = x1 - offset - length
        else:
            xa = x0 + offset
        radius = part.segments[index].d_outer / 2.0
        return xa, xa + length, radius, table["width"], table["depth_shaft"]

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "Keyway")
        index = _segment_index(part, self.segment)
        _number(self.length, "length")
        _number(self.offset, "offset", positive=False)
        if str(self.from_end).strip().lower() not in ("left", "right"):
            raise ValueError(f"from_end: expected 'left' or 'right', got {self.from_end!r}.")
        segment_length = part.segments[index].length
        if float(self.offset) + float(self.length) > segment_length + _EPS:
            raise ValueError(
                f"length {self.length} + offset {self.offset} exceeds segment {index}'s "
                f"{segment_length} mm."
            )
        table = self._table(part)
        radius = part.segments[index].d_outer / 2.0
        if table["depth_shaft"] >= radius:
            raise ValueError(
                f"depth {table['depth_shaft']} mm is not smaller than the radius {radius} mm."
            )

    def _notch(self, part) -> dict:
        """The notch the slot cuts out of the round section, once.

        The end view draws it and a transverse cut face closes on it, so both
        come out of this one calculation and cannot disagree. ``Arc`` is CCW
        from start to end, so the shaft that SURVIVES the slot runs from the
        left notch wall the long way round to the right one; emitting
        (right -> left) would draw the ~31 degree cap over the opening instead
        of the ~329 degrees of material.
        """
        _xa, _xb, radius, width, depth = self._span(part)
        half = width / 2.0
        top = math.sqrt(max(radius * radius - half * half, 0.0))
        floor = radius - depth
        return {
            "radius": radius,
            "start_deg": math.degrees(math.atan2(top, -half)) % 360.0,
            "end_deg": math.degrees(math.atan2(top, half)) % 360.0,
            "path": ((-half, top), (-half, floor), (half, floor), (half, top)),
        }

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_revolved(part, "Keyway")
        xa, xb, radius, width, depth = self._span(part)
        if view == "front":
            floor = radius - depth
            return (
                Line((xa, radius), (xa, floor), "visible"),
                Line((xa, floor), (xb, floor), "visible"),
                Line((xb, floor), (xb, radius), "visible"),
            )
        if view == "side":
            notch = self._notch(part)
            path = notch["path"]
            return (
                Arc((0.0, 0.0), radius, notch["start_deg"], notch["end_deg"], "visible"),
                *(Line(path[index], path[index + 1], "visible") for index in range(len(path) - 1)),
            )
        return ()

    def cross_section(self, part, station: float) -> dict | None:
        """The notch, when the transverse plane falls inside the slot's length."""
        part = _require_revolved(part, "Keyway")
        xa, xb, _radius, _width, _depth = self._span(part)
        if not xa - _EPS <= float(station) <= xb + _EPS:
            return None
        return self._notch(part)

    def removal_box(self, part):
        xa, xb, radius, _, depth = self._span(part)
        return (xa, radius - depth, xb, radius)

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        xa, xb, radius, width, depth = self._span(part)
        if view == "front":
            return (DimIntent(kind="linear", p1=(xa, radius), p2=(xb, radius), feature=self.id),)
        if view == "side":
            floor = radius - depth
            return (
                DimIntent(
                    kind="linear",
                    p1=(-width / 2.0, floor),
                    p2=(width / 2.0, floor),
                    feature=self.id,
                ),
                DimIntent(
                    kind="linear",
                    p1=(0.0, -radius),
                    p2=(0.0, floor),
                    text_override=f"{radius + floor:g}",
                    feature=self.id,
                ),
            )
        return ()

    def omission(self, part, view: str) -> str | None:
        if view in ("front", "side"):
            return None
        return "a keyway on the +Y face coincides with the outline in the top view"


@_register
@dataclass(frozen=True, kw_only=True)
class Thread(_Base):
    """An ISO 261 thread drawn with the ISO 6410 representation."""

    KIND: ClassVar[str] = "thread"
    segment: int
    designation: str
    length: float
    offset: float = 0.0
    from_end: str = "left"
    internal: bool = False

    def _parsed(self) -> dict:
        return parse_designation(self.designation)

    def _span(self, part) -> tuple[float, float, float]:
        index = _segment_index(part, self.segment)
        x0, x1 = segment_bounds(part)[index]
        length = float(self.length)
        if str(self.from_end).strip().lower() == "right":
            xa = x1 - float(self.offset) - length
        else:
            xa = x0 + float(self.offset)
        return xa, xa + length, self._parsed()["d"]

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "Thread")
        index = _segment_index(part, self.segment)
        parsed = self._parsed()
        _number(self.length, "length")
        _number(self.offset, "offset", positive=False)
        segment = part.segments[index]
        if float(self.offset) + float(self.length) > segment.length + _EPS:
            raise ValueError(
                f"length {self.length} + offset {self.offset} exceeds segment {index}'s "
                f"{segment.length} mm."
            )
        if self.internal:
            # ISO 6410: a tapped hole is modelled with the *drilled* bore, i.e.
            # the minor diameter at 0.8 x major, and the major is what the
            # section hatch then runs to (radial_edit below).
            wanted = MINOR_RATIO * parsed["d"]
            if abs(wanted - segment.d_inner) > 0.01:
                raise ValueError(
                    f"{parsed['designation']} internal is drawn with its minor diameter at "
                    f"{wanted:g} mm (ISO 6410, 0.8 x major) but segment {index}'s bore is "
                    f"{segment.d_inner:g} mm. Drill the segment to the minor diameter, or "
                    "name the thread that fits it."
                )
        elif abs(parsed["d"] - segment.d_outer) > 0.01:
            raise ValueError(
                f"{parsed['designation']} has a major diameter of {parsed['d']:g} mm but "
                f"segment {index}'s outside is {segment.d_outer:g} mm. Cut the segment to the "
                "thread diameter, or name the thread that fits it."
            )

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_revolved(part, "Thread")
        xa, xb, d = self._span(part)
        if view == "front":
            return thread_axial_prims(x0=xa, length=xb - xa, d=d, internal=bool(self.internal))
        if view == "side":
            return thread_end_prims(center=(0.0, 0.0), d=d, internal=bool(self.internal))
        return ()

    def radial_edit(self, part) -> tuple[dict, ...]:
        if not self.internal:
            return ()
        # ISO 6410 section convention: the hatch of an internal thread runs to
        # the major diameter, so the cut face's bore is the major, not the minor.
        xa, xb, d = self._span(part)
        return ({"kind": "inner", "x0": xa, "x1": xb, "radius": d / 2.0},)

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        xa, _, d = self._span(part)
        return (
            DimIntent(
                kind="diameter",
                p1=(xa, d / 2.0),
                p2=(xa, -d / 2.0),
                text_override=self._parsed()["designation"],
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side") else f"a thread has no {view} projection"

    @property
    def minor_ratio(self) -> float:
        return MINOR_RATIO


@_register
@dataclass(frozen=True, kw_only=True)
class Undercut(_Base):
    """A DIN 509 relief groove drawn from the profile the standard publishes.

    ``profile`` is a sequence of ``(along, depth)`` pairs in millimetres,
    measured from the shoulder corner into the smaller cylinder. With no
    explicit profile the registered DIN 509 table is asked - and refuses by
    name, because no row is transcribed in this build. Nothing is reconstructed.
    """

    KIND: ClassVar[str] = "undercut"
    axisymmetric: ClassVar[bool] = True
    at: str
    form: str = "E"
    profile: tuple[tuple[float, float], ...] | None = None

    def _profile(self, part) -> tuple[tuple[float, float], ...]:
        if self.profile is not None:
            return tuple((float(a), float(b)) for a, b in self.profile)
        _, radius, _ = _shoulder(self, part)
        return undercut_profile(self.form, radius * 2.0)

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "Undercut")
        _parse_at(self.at, part)
        profile = self._profile(part)
        if len(profile) < 2:
            raise ValueError("profile: a relief profile needs at least two (along, depth) pairs.")
        previous = -math.inf
        for index, (along, depth) in enumerate(profile):
            _number(along, f"profile[{index}].along", positive=False)
            _number(depth, f"profile[{index}].depth", positive=False)
            if along < previous - _EPS:
                raise ValueError(f"profile[{index}].along: the profile must run monotonically.")
            previous = along
        _, radius, _ = _shoulder(self, part)
        deepest = max(depth for _, depth in profile)
        if deepest >= radius:
            raise ValueError(
                f"profile: the deepest relief {deepest} mm is not smaller than the "
                f"{radius} mm radius it is cut into."
            )

    def _points(self, part) -> tuple[tuple[float, float], ...]:
        x, radius, direction = _shoulder(self, part)
        return tuple(
            (x + direction * along, radius - depth) for along, depth in self._profile(part)
        )

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        if view != "front":
            return ()
        return (Poly(self._points(_require_revolved(part, "Undercut")), False, "visible"),)

    def removal_box(self, part):
        points = self._points(part)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs), min(ys), max(xs), max(ys))

    def omission(self, part, view: str) -> str | None:
        return None if view == "front" else f"a relief groove has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class RetainingGroove(_Base):
    """A DIN 471 (shaft) or DIN 472 (bore) retaining-ring groove."""

    KIND: ClassVar[str] = "retaining_groove"
    axisymmetric: ClassVar[bool] = True
    x: float
    d: float
    where: str = "shaft"
    m: float | None = None
    d2: float | None = None
    standard: str | None = None

    def _standard(self) -> str:
        if self.standard:
            return " ".join(str(self.standard).split()).upper()
        return "DIN 471" if str(self.where).strip().lower() == "shaft" else "DIN 472"

    def _dims(self) -> dict:
        if self.m is not None and self.d2 is not None:
            return {"m": float(self.m), "d2": float(self.d2)}
        return ring_groove_dims(self._standard(), float(self.d))

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "RetainingGroove")
        where = str(self.where).strip().lower()
        if where not in ("shaft", "bore"):
            raise ValueError(f"where: expected 'shaft' or 'bore', got {self.where!r}.")
        x = _number(self.x, "x", positive=False)
        _number(self.d, "d")
        if not 0.0 <= x <= part_length(part) + _EPS:
            raise ValueError(f"x {x} is outside the part (0 .. {part_length(part)} mm).")
        dims = self._dims()
        _number(dims["m"], "m")
        _number(dims["d2"], "d2")
        radius = outer_radius_at(part, x)
        bore = inner_radius_at(part, x)
        if where == "shaft" and dims["d2"] / 2.0 >= radius:
            raise ValueError(
                f"d2 {dims['d2']} mm does not cut into the {radius * 2.0:g} mm shaft at x={x}."
            )
        if where == "bore" and dims["d2"] / 2.0 <= bore:
            raise ValueError(
                f"d2 {dims['d2']} mm does not cut into the {bore * 2.0:g} mm bore at x={x}."
            )

    def _span(self, part) -> tuple[float, float, float, float]:
        dims = self._dims()
        x = float(self.x)
        half = dims["m"] / 2.0
        surface = (
            outer_radius_at(part, x)
            if str(self.where).strip().lower() == "shaft"
            else inner_radius_at(part, x)
        )
        return x - half, x + half, dims["d2"] / 2.0, surface

    def radial_edit(self, part) -> tuple[dict, ...]:
        x0, x1, groove_r, _ = self._span(part)
        kind = "outer" if str(self.where).strip().lower() == "shaft" else "inner"
        return ({"kind": kind, "x0": x0, "x1": x1, "radius": groove_r},)

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        if view != "front":
            return ()
        x0, x1, groove_r, surface = self._span(_require_revolved(part, "RetainingGroove"))
        return (
            Line((x0, surface), (x0, groove_r), "visible"),
            Line((x0, groove_r), (x1, groove_r), "visible"),
            Line((x1, groove_r), (x1, surface), "visible"),
        )

    def removal_box(self, part):
        x0, x1, groove_r, surface = self._span(part)
        return (x0, min(groove_r, surface), x1, max(groove_r, surface))

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        x0, x1, groove_r, _ = self._span(part)
        dims = self._dims()
        return (
            DimIntent(kind="linear", p1=(x0, groove_r), p2=(x1, groove_r), feature=self.id),
            DimIntent(
                kind="diameter",
                p1=(float(self.x), groove_r),
                p2=(float(self.x), -groove_r),
                text_override=f"⌀{dims['d2']:g} ({self._standard()})",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view == "front" else f"a ring groove has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class CentreHole(_Base):
    """A DIN 332-1 centre hole, forms A and B. Form R is refused, not approximated."""

    KIND: ClassVar[str] = "centre_hole"
    at: str = "end"
    form: str = "A"
    size: float = 0.0
    d1: float | None = None
    d2: float | None = None
    d3: float | None = None
    t: float | None = None

    def _dims(self) -> dict:
        form = str(self.form).strip().upper()
        explicit = self.d1 is not None and self.d2 is not None and self.t is not None
        if form == "B":
            explicit = explicit and self.d3 is not None
        if explicit:
            return {
                "d1": float(self.d1),
                "d2": float(self.d2),
                "d3": float(self.d3) if self.d3 is not None else None,
                "t": float(self.t),
            }
        return centre_hole_dims(self.form, float(self.size))

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "CentreHole")
        _parse_at(self.at, part, allow_step=False)
        if str(self.form).strip().upper() == "R":
            # the table module owns the wording; ask it so there is one message
            centre_hole_dims("R", float(self.size))
        dims = self._dims()
        _number(dims["d1"], "d1")
        _number(dims["d2"], "d2")
        _number(dims["t"], "t")
        if dims["d2"] <= dims["d1"]:
            raise ValueError(f"d2 {dims['d2']} must be larger than d1 {dims['d1']}.")
        kind, _ = _parse_at(self.at, part, allow_step=False)
        x = 0.0 if kind == "start" else part_length(part)
        if dims["d2"] / 2.0 >= outer_radius_at(part, x):
            raise ValueError(
                f"d2 {dims['d2']} mm does not fit the {outer_radius_at(part, x) * 2.0:g} mm face."
            )

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_revolved(part, "CentreHole")
        dims = self._dims()
        kind, _ = _parse_at(self.at, part, allow_step=False)
        face = 0.0 if kind == "start" else part_length(part)
        step = 1.0 if kind == "start" else -1.0
        r1, r2 = dims["d1"] / 2.0, dims["d2"] / 2.0
        cone = (r2 - r1) / math.tan(math.radians(30.0))  # 60 deg included angle
        if view == "front":
            prims: list[Prim] = [
                Line((face, r2), (face + step * cone, r1), "hidden"),
                Line((face, -r2), (face + step * cone, -r1), "hidden"),
                Line((face + step * cone, r1), (face + step * dims["t"], r1), "hidden"),
                Line((face + step * cone, -r1), (face + step * dims["t"], -r1), "hidden"),
                Line(
                    (face + step * dims["t"], -r1),
                    (face + step * dims["t"], r1),
                    "hidden",
                ),
            ]
            if str(self.form).strip().upper() == "B" and dims.get("d3"):
                r3 = dims["d3"] / 2.0
                guard = (r3 - r2) / math.tan(math.radians(60.0))  # 120 deg protective cone
                prims.insert(0, Line((face, r3), (face + step * guard, r2), "hidden"))
                prims.insert(1, Line((face, -r3), (face + step * guard, -r2), "hidden"))
            return tuple(prims)
        if view == "side":
            circles: list[Prim] = [
                Circle((0.0, 0.0), r1, "hidden"),
                Circle((0.0, 0.0), r2, "hidden"),
            ]
            if str(self.form).strip().upper() == "B" and dims.get("d3"):
                circles.append(Circle((0.0, 0.0), dims["d3"] / 2.0, "hidden"))
            return tuple(circles)
        return ()

    def removal_box(self, part):
        dims = self._dims()
        kind, _ = _parse_at(self.at, part, allow_step=False)
        face = 0.0 if kind == "start" else part_length(part)
        step = 1.0 if kind == "start" else -1.0
        far = face + step * dims["t"]
        outer = max(dims["d2"], dims.get("d3") or 0.0) / 2.0
        return (min(face, far), -outer, max(face, far), outer)

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side") else f"a centre hole has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class ORingGroove(_Base):
    """An ISO 3601-2 radial O-ring housing. An axial (face) groove is refused."""

    KIND: ClassVar[str] = "oring_groove"
    axisymmetric: ClassVar[bool] = True
    x: float
    cord: float
    where: str = "shaft"
    housing: str = "radial"
    b: float | None = None
    h: float | None = None

    def _dims(self) -> dict:
        if self.b is not None and self.h is not None:
            return {"b": float(self.b), "h": float(self.h)}
        return oring_groove_dims(float(self.cord), str(self.housing))

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "ORingGroove")
        if str(self.housing).strip().lower() not in ORING_KINDS:
            raise ValueError(f"housing: expected one of {ORING_KINDS}, got {self.housing!r}.")
        if str(self.housing).strip().lower() == "axial":
            raise ValueError(
                "housing 'axial': a face groove is not on the revolved silhouette and is "
                "refused rather than drawn as if it were radial. Model the face as a "
                "prismatic pocket."
            )
        if str(self.where).strip().lower() not in ("shaft", "bore"):
            raise ValueError(f"where: expected 'shaft' or 'bore', got {self.where!r}.")
        x = _number(self.x, "x", positive=False)
        _number(self.cord, "cord")
        if not 0.0 <= x <= part_length(part) + _EPS:
            raise ValueError(f"x {x} is outside the part (0 .. {part_length(part)} mm).")
        dims = self._dims()
        _number(dims["b"], "b")
        _number(dims["h"], "h")
        radius = outer_radius_at(part, x)
        if str(self.where).strip().lower() == "shaft" and dims["h"] >= radius:
            raise ValueError(f"h {dims['h']} mm is not smaller than the radius {radius} mm.")

    def _span(self, part) -> tuple[float, float, float, float]:
        dims = self._dims()
        x = float(self.x)
        half = dims["b"] / 2.0
        if str(self.where).strip().lower() == "shaft":
            surface = outer_radius_at(part, x)
            return x - half, x + half, surface - dims["h"], surface
        surface = inner_radius_at(part, x)
        return x - half, x + half, surface + dims["h"], surface

    def radial_edit(self, part) -> tuple[dict, ...]:
        x0, x1, groove_r, _ = self._span(part)
        kind = "outer" if str(self.where).strip().lower() == "shaft" else "inner"
        return ({"kind": kind, "x0": x0, "x1": x1, "radius": groove_r},)

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        if view != "front":
            return ()
        x0, x1, groove_r, surface = self._span(_require_revolved(part, "ORingGroove"))
        return (
            Line((x0, surface), (x0, groove_r), "visible"),
            Line((x0, groove_r), (x1, groove_r), "visible"),
            Line((x1, groove_r), (x1, surface), "visible"),
        )

    def removal_box(self, part):
        x0, x1, groove_r, surface = self._span(part)
        return (x0, min(groove_r, surface), x1, max(groove_r, surface))

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        x0, x1, groove_r, _ = self._span(part)
        return (DimIntent(kind="linear", p1=(x0, groove_r), p2=(x1, groove_r), feature=self.id),)

    def omission(self, part, view: str) -> str | None:
        return None if view == "front" else f"an O-ring groove has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class CrossHole(_Base):
    """A radial hole through (or into) a revolved part.

    Only a radial hole (``angle`` 90 degrees) is projected. An inclined cross
    hole is refused rather than drawn as if it were radial.
    """

    KIND: ClassVar[str] = "cross_hole"
    x: float
    diameter: float
    angle: float = 90.0
    depth: float | None = None

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "CrossHole")
        x = _number(self.x, "x", positive=False)
        diameter = _number(self.diameter, "diameter")
        angle = _number(self.angle, "angle", positive=False)
        if abs(angle - 90.0) > _EPS:
            raise ValueError(
                f"angle {angle}: only a radial cross hole (angle=90) is projected by this "
                "engine; an inclined hole is refused rather than drawn as if it were radial."
            )
        if not 0.0 <= x <= part_length(part) + _EPS:
            raise ValueError(f"x {x} is outside the part (0 .. {part_length(part)} mm).")
        radius = outer_radius_at(part, x)
        if diameter >= 2.0 * radius:
            raise ValueError(
                f"diameter {diameter} mm is not smaller than the {2.0 * radius:g} mm outside "
                f"diameter at x={x}."
            )
        if self.depth is not None:
            depth = _number(self.depth, "depth")
            if depth > 2.0 * radius + _EPS:
                raise ValueError(f"depth {depth} mm is deeper than the part at x={x}.")

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_revolved(part, "CrossHole")
        x = float(self.x)
        half = float(self.diameter) / 2.0
        radius = outer_radius_at(part, x)
        if view == "front":
            bottom = -radius if self.depth is None else radius - float(self.depth)
            prims: list[Prim] = [
                Line((x - half, bottom), (x - half, radius), "hidden"),
                Line((x + half, bottom), (x + half, radius), "hidden"),
            ]
            if self.depth is not None:
                prims.append(Line((x - half, bottom), (x + half, bottom), "hidden"))
            return tuple(prims)
        if view == "side":
            return (
                Line((-radius, half), (radius, half), "hidden"),
                Line((-radius, -half), (radius, -half), "hidden"),
            )
        return ()

    def removal_box(self, part):
        x = float(self.x)
        half = float(self.diameter) / 2.0
        radius = outer_radius_at(part, x)
        bottom = -radius if self.depth is None else radius - float(self.depth)
        return (x - half, bottom, x + half, radius)

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        x = float(self.x)
        half = float(self.diameter) / 2.0
        return (
            DimIntent(
                kind="diameter",
                p1=(x - half, 0.0),
                p2=(x + half, 0.0),
                text_override=f"⌀{float(self.diameter):g}",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side") else f"a cross hole has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class HoleCircle(_Base):
    """A bolt circle on a face: hidden pairs in the axial view, real holes in the end view."""

    KIND: ClassVar[str] = "hole_circle"
    x: float
    pcd: float
    count: int
    diameter: float
    start_angle: float = 0.0
    thread: str | None = None

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "HoleCircle")
        x = _number(self.x, "x", positive=False)
        pcd = _number(self.pcd, "pcd")
        diameter = _number(self.diameter, "diameter")
        _number(self.start_angle, "start_angle", positive=False)
        if int(self.count) < 2:
            raise ValueError(f"count: a bolt circle needs at least 2 holes, got {self.count}.")
        if not 0.0 <= x <= part_length(part) + _EPS:
            raise ValueError(f"x {x} is outside the part (0 .. {part_length(part)} mm).")
        radius = outer_radius_at(part, x)
        bore = inner_radius_at(part, x)
        if pcd / 2.0 + diameter / 2.0 > radius + _EPS:
            raise ValueError(
                f"pcd {pcd} with ⌀{diameter} reaches {pcd / 2.0 + diameter / 2.0:.3f} mm, "
                f"past the {radius:.3f} mm outside radius at x={x}."
            )
        if pcd / 2.0 - diameter / 2.0 < bore - _EPS:
            raise ValueError(
                f"pcd {pcd} with ⌀{diameter} reaches {pcd / 2.0 - diameter / 2.0:.3f} mm, "
                f"inside the {bore:.3f} mm bore at x={x}."
            )
        if self.thread:
            parse_designation(self.thread)

    def _centers(self) -> tuple[tuple[float, float], ...]:
        count = int(self.count)
        radius = float(self.pcd) / 2.0
        start = math.radians(float(self.start_angle))
        return tuple(
            (
                radius * math.cos(start + 2.0 * math.pi * k / count),
                radius * math.sin(start + 2.0 * math.pi * k / count),
            )
            for k in range(count)
        )

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_revolved(part, "HoleCircle")
        pcd_r = float(self.pcd) / 2.0
        half = float(self.diameter) / 2.0
        if view == "front":
            index, _, _, _ = _segment_at_index(part, float(self.x))
            x0, x1 = segment_bounds(part)[index]
            prims: list[Prim] = []
            for sign in (1.0, -1.0):
                for offset in (half, -half):
                    y = sign * (pcd_r + offset)
                    prims.append(Line((x0, y), (x1, y), "hidden"))
                prims.append(Line((x0, sign * pcd_r), (x1, sign * pcd_r), "center"))
            return tuple(prims)
        if view == "side":
            prims = [Circle((0.0, 0.0), pcd_r, "center")]
            for cx, cy in self._centers():
                prims.append(Circle((cx, cy), half, "visible"))
                prims.append(Line((cx - half - 2.0, cy), (cx + half + 2.0, cy), "center"))
                prims.append(Line((cx, cy - half - 2.0), (cx, cy + half + 2.0), "center"))
            return tuple(prims)
        return ()

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "side":
            return ()
        pcd_r = float(self.pcd) / 2.0
        half = float(self.diameter) / 2.0
        first = self._centers()[0]
        label = self.thread or f"⌀{float(self.diameter):g}"
        return (
            DimIntent(
                kind="diameter",
                p1=(first[0] - half, first[1]),
                p2=(first[0] + half, first[1]),
                text_override=f"{int(self.count)}x {label}",
                feature=self.id,
            ),
            DimIntent(
                kind="diameter",
                p1=(-pcd_r, 0.0),
                p2=(pcd_r, 0.0),
                text_override=f"⌀{float(self.pcd):g}",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side") else f"a bolt circle has no {view} projection"


def _segment_at_index(part: RevolvedPart, x: float) -> tuple[int, float, float, Any]:
    bounds = zip(segment_bounds(part), part.segments, strict=True)
    for index, ((x0, x1), segment) in enumerate(bounds):
        if x0 - _EPS <= x <= x1 + _EPS:
            return index, x0, x1, segment
    raise ValueError(f"x={x} is outside the part (0 .. {part_length(part)} mm).")


@_register
@dataclass(frozen=True, kw_only=True)
class AxialBore(_Base):
    """A bore drilled in from one end. Expressed through the radial profile."""

    KIND: ClassVar[str] = "axial_bore"
    at: str = "start"
    diameter: float
    depth: float | None = None

    def _span(self, part) -> tuple[float, float]:
        kind, _ = _parse_at(self.at, part, allow_step=False)
        length = part_length(part)
        depth = length if self.depth is None else float(self.depth)
        return (0.0, depth) if kind == "start" else (length - depth, length)

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "AxialBore")
        _parse_at(self.at, part, allow_step=False)
        diameter = _number(self.diameter, "diameter")
        if self.depth is not None:
            depth = _number(self.depth, "depth")
            if depth > part_length(part) + _EPS:
                raise ValueError(
                    f"depth {depth} mm is longer than the part {part_length(part)} mm."
                )
        x0, x1 = self._span(part)
        for x in (x0 + _EPS, (x0 + x1) / 2.0, x1 - _EPS):
            if diameter >= 2.0 * outer_radius_at(part, x):
                raise ValueError(
                    f"diameter {diameter} mm is not smaller than the "
                    f"{2.0 * outer_radius_at(part, x):g} mm outside diameter at x={x:.3f}."
                )
        for other in getattr(part, "features", ()):
            if other is self or not isinstance(other, AxialBore):
                continue
            ox0, ox1 = other._span(part)
            if min(x1, ox1) - max(x0, ox0) > _EPS:
                raise ValueError(
                    f"bores {self.id!r} and {other.id!r} both remove material over "
                    f"x {max(x0, ox0):.3f}..{min(x1, ox1):.3f}. A bore that changes diameter "
                    "is two segments, not two features."
                )

    def radial_edit(self, part) -> tuple[dict, ...]:
        x0, x1 = self._span(part)
        return ({"kind": "inner", "x0": x0, "x1": x1, "radius": float(self.diameter) / 2.0},)

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_revolved(part, "AxialBore")
        half = float(self.diameter) / 2.0
        kind, _ = _parse_at(self.at, part, allow_step=False)
        x0, x1 = self._span(part)
        if view == "front" and self.depth is not None:
            bottom = x1 if kind == "start" else x0
            return (Line((bottom, -half), (bottom, half), "hidden"),)
        if view == "side":
            near = str(ctx.get("side", "right")).strip().lower()
            open_here = (near == "left" and kind == "start") or (near == "right" and kind == "end")
            return (Circle((0.0, 0.0), half, "visible" if open_here else "hidden"),)
        return ()

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "side":
            return ()
        half = float(self.diameter) / 2.0
        return (
            DimIntent(
                kind="diameter",
                p1=(-half, 0.0),
                p2=(half, 0.0),
                text_override=f"⌀{float(self.diameter):g}",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None  # the bore always reaches the drawing through the radial profile


@_register
@dataclass(frozen=True, kw_only=True)
class GearTeeth(_Base):
    """Involute spur/helical teeth on a segment, from the repository's own generator."""

    KIND: ClassVar[str] = "gear_teeth"
    segment: int
    module: float
    teeth: int
    pressure_angle: float = 20.0
    helix_angle: float = 0.0
    hand: str = "RH"

    def _radii(self) -> tuple[float, float, float]:
        module = float(self.module)
        teeth = int(self.teeth)
        pitch = module * teeth / 2.0
        return pitch + module, pitch, pitch - 1.25 * module

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_revolved(part, "GearTeeth")
        index = _segment_index(part, self.segment)
        _number(self.module, "module")
        angle = _number(self.pressure_angle, "pressure_angle")
        if int(self.teeth) < 6:
            raise ValueError(
                f"teeth: the involute generator needs at least 6 teeth, got {self.teeth}."
            )
        if not 10.0 <= angle <= 35.0:
            raise ValueError(f"pressure_angle {angle}: expected between 10 and 35 degrees.")
        if str(self.hand).strip().upper() not in ("RH", "LH"):
            raise ValueError(f"hand: expected 'RH' or 'LH', got {self.hand!r}.")
        tip, _, _ = self._radii()
        outside = part.segments[index].d_outer
        if abs(tip * 2.0 - outside) > 0.01:
            raise ValueError(
                f"m={float(self.module):g} z={int(self.teeth)} gives a tip diameter of "
                f"{tip * 2.0:g} mm, but segment {index} is {outside:g} mm. Cut the segment to "
                "the tip diameter, or name the module and tooth count that fit it."
            )

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_revolved(part, "GearTeeth")
        index = _segment_index(part, self.segment)
        _, pitch, root = self._radii()
        if view == "front":
            x0, x1 = segment_bounds(part)[index]
            prims: list[Prim] = [
                Line((x0, root), (x1, root), "visible"),
                Line((x0, -root), (x1, -root), "visible"),
                Line((x0, pitch), (x1, pitch), "center"),
                Line((x0, -pitch), (x1, -pitch), "center"),
            ]
            if abs(float(self.helix_angle)) > _EPS:
                # ISO 2203: three thin lines at the helix angle mark the hand.
                mid = (x0 + x1) / 2.0
                run = (x1 - x0) / 3.0
                rise = run * math.tan(math.radians(float(self.helix_angle)))
                sign = 1.0 if str(self.hand).strip().upper() == "RH" else -1.0
                for offset in (-run, 0.0, run):
                    prims.append(
                        Line(
                            (mid + offset - run / 2.0, -pitch / 2.0),
                            (mid + offset + run / 2.0, -pitch / 2.0 + sign * rise),
                            "phantom",
                        )
                    )
            return tuple(prims)
        if view == "side":
            outline = generate_full_gear_outline(
                float(self.module),
                int(self.teeth),
                float(self.pressure_angle),
                float(self.helix_angle),
                str(self.hand).strip().upper(),
                (0.0, 0.0),
            )
            return (
                Poly(tuple(outline), True, "visible"),
                Circle((0.0, 0.0), pitch, "center"),
            )
        return ()

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        tip, _, _ = self._radii()
        if view != "side":
            return ()
        return (
            DimIntent(
                kind="diameter",
                p1=(-tip, 0.0),
                p2=(tip, 0.0),
                text_override=(f"⌀{tip * 2.0:g} (m={float(self.module):g}, z={int(self.teeth)})"),
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side") else f"gear teeth have no {view} projection"


# -- prismatic features ------------------------------------------------------
#
# View frames for a prismatic part:
#   "front" : the outline's own XY
#   "side"  : u = part Y, v = z in [0, thickness]   (looking along -X)
#   "top"   : u = part X, v = z in [0, thickness]   (looking along -Y)
#
# THE MACHINED FACE IS v = thickness. The front view looks at the outline from
# +Z, so the face it shows -- the face a feature placed by its (x, y) is cut
# from -- is the top of the side and top views, and every blind feature hangs
# down from there toward v = 0. Hole, Pocket and anything added later must
# agree on this: a drawing whose pocket is milled from one face and whose blind
# hole is drilled from the other is silently wrong, and no validator can tell.


@_register
@dataclass(frozen=True, kw_only=True)
class Hole(_Base):
    """A hole in a prismatic part, optionally counterbored, countersunk or tapped.

    Drilled from the machined face (``v = thickness``, the one the front view
    looks at), so a blind hole hangs down from there and a counterbore steps
    down from the same face -- the convention ``Pocket`` follows too.
    """

    KIND: ClassVar[str] = "hole"
    x: float
    y: float
    diameter: float
    depth: float | None = None
    cbore_d: float | None = None
    cbore_depth: float | None = None
    csink_d: float | None = None
    thread: str | None = None

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_prismatic(part, "Hole")
        _number(self.x, "x", positive=False)
        _number(self.y, "y", positive=False)
        diameter = _number(self.diameter, "diameter")
        if not point_in_outline(part, float(self.x), float(self.y)):
            raise ValueError(
                f"({float(self.x):g}, {float(self.y):g}) is outside the part outline "
                f"{outline_bbox(part)}."
            )
        if self.depth is not None:
            depth = _number(self.depth, "depth")
            if depth > part.thickness + _EPS:
                raise ValueError(f"depth {depth} is deeper than the {part.thickness} mm thickness.")
        for name, value in (("cbore_d", self.cbore_d), ("csink_d", self.csink_d)):
            if value is not None and _number(value, name) <= diameter:
                raise ValueError(f"{name} {value} must be larger than the hole {diameter}.")
        if self.cbore_d is not None and self.cbore_depth is None:
            raise ValueError("cbore_depth: a counterbore needs its depth.")
        if self.thread:
            parsed = parse_designation(self.thread)
            if abs(parsed["d"] - diameter) > 0.01:
                raise ValueError(
                    f"{parsed['designation']} has a major diameter of {parsed['d']:g} mm but "
                    f"diameter is {diameter:g} mm."
                )

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_prismatic(part, "Hole")
        x, y = float(self.x), float(self.y)
        half = float(self.diameter) / 2.0
        if view == "front":
            prims: list[Prim] = []
            if self.thread:
                minor = MINOR_RATIO * float(self.diameter) / 2.0
                prims.append(Circle((x, y), minor, "visible"))
                prims.append(Arc((x, y), half, 90.0, 0.0, "visible"))
            else:
                prims.append(Circle((x, y), half, "visible"))
            for extra in (self.cbore_d, self.csink_d):
                if extra is not None:
                    prims.append(Circle((x, y), float(extra) / 2.0, "visible"))
            extras = [float(v) / 2.0 for v in (self.cbore_d, self.csink_d) if v is not None]
            reach = max([half, *extras]) + 3.0
            prims.append(Line((x - reach, y), (x + reach, y), "center"))
            prims.append(Line((x, y - reach), (x, y + reach), "center"))
            return tuple(prims)
        if view in ("side", "top"):
            u = y if view == "side" else x
            face = part.thickness
            depth = part.thickness if self.depth is None else float(self.depth)
            floor = face - depth
            prims = [
                Line((u - half, face), (u - half, floor), "hidden"),
                Line((u + half, face), (u + half, floor), "hidden"),
            ]
            if self.depth is not None:
                prims.append(Line((u - half, floor), (u + half, floor), "hidden"))
            if self.cbore_d is not None:
                cb = float(self.cbore_d) / 2.0
                cf = face - float(self.cbore_depth)
                prims.append(Line((u - cb, face), (u - cb, cf), "hidden"))
                prims.append(Line((u + cb, face), (u + cb, cf), "hidden"))
                prims.append(Line((u - cb, cf), (u + cb, cf), "hidden"))
            return tuple(prims)
        return ()

    def removal_box(self, part):
        return _box_around(self._centre(), self._outer_radius())

    def _centre(self) -> tuple[float, float]:
        return (float(self.x), float(self.y))

    def _outer_radius(self) -> float:
        """The widest circle the plane can meet: the drill, or a counterbore or
        countersink that is wider than it."""
        extras = [float(v) / 2.0 for v in (self.cbore_d, self.csink_d) if v is not None]
        return max([float(self.diameter) / 2.0, *extras])

    def cut_spans(self, part, p1, unit):
        """The true chord of the drilled circle, not the square that bounds it.

        On an 80x50x8 plate with a 10 mm hole at (40, 25): a plane at y=29.9
        grazes the hole and cuts a 1.990 mm chord, leaving 624.08 mm2 of cut
        face. The bounding box reports 10 mm at every offset and hatches
        560 mm2 - a 64 mm2 error, with an 8 mm gap drawn through solid metal.
        """
        return _circle_chord(self._centre(), self._outer_radius(), p1, unit)

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        x, y = float(self.x), float(self.y)
        half = float(self.diameter) / 2.0
        label = self.thread or f"⌀{float(self.diameter):g}"
        return (
            DimIntent(
                kind="diameter",
                p1=(x - half, y),
                p2=(x + half, y),
                text_override=label,
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side", "top") else f"a hole has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class Slot(_Base):
    """An obround slot: two parallel flanks closed by two semicircular ends."""

    KIND: ClassVar[str] = "slot"
    x: float
    y: float
    length: float
    width: float
    angle: float = 0.0

    def _ends(self) -> tuple[tuple[float, float], tuple[float, float], float]:
        half_run = (float(self.length) - float(self.width)) / 2.0
        theta = math.radians(float(self.angle))
        ux, uy = math.cos(theta), math.sin(theta)
        x, y = float(self.x), float(self.y)
        return (
            (x - half_run * ux, y - half_run * uy),
            (x + half_run * ux, y + half_run * uy),
            float(self.width) / 2.0,
        )

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_prismatic(part, "Slot")
        _number(self.x, "x", positive=False)
        _number(self.y, "y", positive=False)
        length = _number(self.length, "length")
        width = _number(self.width, "width")
        _number(self.angle, "angle", positive=False)
        if length <= width:
            raise ValueError(
                f"length {length} must be greater than width {width}; a slot as wide as it is "
                "long is a hole."
            )
        for point in self._ends()[:2]:
            if not point_in_outline(part, point[0], point[1]):
                raise ValueError(
                    f"slot end ({point[0]:g}, {point[1]:g}) is outside the part outline "
                    f"{outline_bbox(part)}."
                )

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_prismatic(part, "Slot")
        (c1, c2, half) = self._ends()
        theta = math.radians(float(self.angle))
        nx, ny = -math.sin(theta), math.cos(theta)
        if view == "front":
            return (
                Line(
                    (c1[0] + half * nx, c1[1] + half * ny),
                    (c2[0] + half * nx, c2[1] + half * ny),
                    "visible",
                ),
                Line(
                    (c1[0] - half * nx, c1[1] - half * ny),
                    (c2[0] - half * nx, c2[1] - half * ny),
                    "visible",
                ),
                Arc(
                    c2,
                    half,
                    (float(self.angle) - 90.0) % 360.0,
                    (float(self.angle) + 90.0) % 360.0,
                    "visible",
                ),
                Arc(
                    c1,
                    half,
                    (float(self.angle) + 90.0) % 360.0,
                    (float(self.angle) + 270.0) % 360.0,
                    "visible",
                ),
                Line((c1[0], c1[1]), (c2[0], c2[1]), "center"),
            )
        if view in ("side", "top"):
            box = self.removal_box(part)
            u0, u1 = (box[1], box[3]) if view == "side" else (box[0], box[2])
            return (
                Line((u0, 0.0), (u0, part.thickness), "hidden"),
                Line((u1, 0.0), (u1, part.thickness), "hidden"),
            )
        return ()

    def removal_box(self, part):
        (c1, c2, half) = self._ends()
        xs = (c1[0] - half, c1[0] + half, c2[0] - half, c2[0] + half)
        ys = (c1[1] - half, c1[1] + half, c2[1] - half, c2[1] + half)
        return (min(xs), min(ys), max(xs), max(ys))

    def cut_spans(self, part, p1, unit):
        """The true chord of the obround: the union of its two end circles and
        the rectangle between them. The bounding box of an angled slot is far
        wider than the slot itself, so the box would erase material the slot
        never touches."""
        (c1, c2, half) = self._ends()
        middle = ((c1[0] + c2[0]) / 2.0, (c1[1] + c2[1]) / 2.0)
        run = math.dist(c1, c2)
        return _merge_spans(
            [
                *_circle_chord(c1, half, p1, unit),
                *_circle_chord(c2, half, p1, unit),
                *_rotated_box_chord(middle, run / 2.0, half, float(self.angle), p1, unit),
            ]
        )

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        (c1, c2, half) = self._ends()
        theta = math.radians(float(self.angle))
        nx, ny = -math.sin(theta), math.cos(theta)
        return (
            DimIntent(
                kind="aligned",
                p1=(c1[0], c1[1]),
                p2=(c2[0], c2[1]),
                text_override=f"{float(self.length):g}",
                feature=self.id,
            ),
            DimIntent(
                kind="aligned",
                p1=(c2[0] + half * nx, c2[1] + half * ny),
                p2=(c2[0] - half * nx, c2[1] - half * ny),
                text_override=f"{float(self.width):g}",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side", "top") else f"a slot has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class Pocket(_Base):
    """A milled pocket. ``no_section_hatch`` marks a rib or web (ISO 128-3).

    Milled from the machined face (``v = thickness``), the same face ``Hole``
    drills from.
    """

    KIND: ClassVar[str] = "pocket"
    outline: tuple[tuple[float, float], ...]
    depth: float
    no_section_hatch: bool = False

    def validate(self, part) -> None:
        super().validate(part)
        part = _require_prismatic(part, "Pocket")
        depth = _number(self.depth, "depth")
        if depth >= part.thickness - _EPS:
            raise ValueError(
                f"depth {depth} is not smaller than the {part.thickness} mm thickness; "
                "a pocket that goes through is a Hole or a Slot."
            )
        points = tuple((float(x), float(y)) for x, y in self.outline)
        if len(points) < 3:
            raise ValueError(f"outline: a pocket needs at least 3 vertices, got {len(points)}.")
        from engineering.measure import is_self_intersecting

        if is_self_intersecting(points) is True:
            raise ValueError("outline: the pocket outline crosses itself.")
        for index, point in enumerate(points):
            if not point_in_outline(part, point[0], point[1]):
                raise ValueError(
                    f"outline[{index}] ({point[0]:g}, {point[1]:g}) is outside the part outline "
                    f"{outline_bbox(part)}."
                )

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        part = _require_prismatic(part, "Pocket")
        points = tuple((float(x), float(y)) for x, y in self.outline)
        if view == "front":
            return (Poly(points, True, "visible"),)
        if view in ("side", "top"):
            box = self.removal_box(part)
            u0, u1 = (box[1], box[3]) if view == "side" else (box[0], box[2])
            face = part.thickness
            floor = face - float(self.depth)
            return (
                Line((u0, face), (u0, floor), "hidden"),
                Line((u0, floor), (u1, floor), "hidden"),
                Line((u1, floor), (u1, face), "hidden"),
            )
        return ()

    def cut_spans(self, part, p1, unit):
        """The chord of the pocket's own outline, not of the box around it: a
        pocket is only rectangular when its outline is."""
        return _polygon_chord(
            tuple((float(x), float(y)) for x, y in self.outline),
            p1,
            unit,
            what=f"pocket {self.id!r}",
        )

    def removal_box(self, part):
        xs = [float(x) for x, _ in self.outline]
        ys = [float(y) for _, y in self.outline]
        return (min(xs), min(ys), max(xs), max(ys))

    def omission(self, part, view: str) -> str | None:
        return None if view in ("front", "side", "top") else f"a pocket has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class CornerFillet(_Base):
    """A rounded outline corner: two tangent points and the arc between them."""

    KIND: ClassVar[str] = "corner_fillet"
    vertex: int
    radius: float

    def _frame(self, part):
        points = _require_prismatic(part, "CornerFillet").outline
        count = len(points)
        index = int(self.vertex)
        if not 0 <= index < count:
            raise ValueError(f"vertex {index} is outside the outline (0-{count - 1}).")
        v = points[index]
        p = points[(index - 1) % count]
        n = points[(index + 1) % count]
        return v, p, n

    def validate(self, part) -> None:
        super().validate(part)
        radius = _number(self.radius, "radius")
        v, p, n = self._frame(part)
        edge_p = math.dist(v, p)
        edge_n = math.dist(v, n)
        u1 = ((p[0] - v[0]) / edge_p, (p[1] - v[1]) / edge_p)
        u2 = ((n[0] - v[0]) / edge_n, (n[1] - v[1]) / edge_n)
        cosine = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        half = math.acos(cosine) / 2.0
        if half < 1e-6 or abs(cosine + 1.0) < 1e-9:
            raise ValueError(
                f"vertex {self.vertex} is a straight run, there is no corner to round."
            )
        tangent = radius / math.tan(half)
        if tangent > min(edge_p, edge_n) / 2.0 + _EPS:
            raise ValueError(
                f"radius {radius} needs {tangent:.3f} mm of edge, more than half the shorter "
                f"adjacent edge ({min(edge_p, edge_n):.3f} mm)."
            )

    def outline_edit(self, part) -> dict:
        v, p, n = self._frame(part)
        radius = float(self.radius)
        edge_p = math.dist(v, p)
        edge_n = math.dist(v, n)
        u1 = ((p[0] - v[0]) / edge_p, (p[1] - v[1]) / edge_p)
        u2 = ((n[0] - v[0]) / edge_n, (n[1] - v[1]) / edge_n)
        cosine = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        half = math.acos(cosine) / 2.0
        tangent = radius / math.tan(half)
        before = (v[0] + u1[0] * tangent, v[1] + u1[1] * tangent)
        after = (v[0] + u2[0] * tangent, v[1] + u2[1] * tangent)
        bisector = (u1[0] + u2[0], u1[1] + u2[1])
        norm = math.hypot(*bisector)
        center = (
            v[0] + bisector[0] / norm * (radius / math.sin(half)),
            v[1] + bisector[1] / norm * (radius / math.sin(half)),
        )
        start = math.degrees(math.atan2(before[1] - center[1], before[0] - center[0])) % 360.0
        end = math.degrees(math.atan2(after[1] - center[1], after[0] - center[0])) % 360.0
        cross = u1[0] * u2[1] - u1[1] * u2[0]
        if cross > 0:  # the arc runs the other way round
            start, end = end, start
        return {
            "vertex": int(self.vertex),
            "before": before,
            "after": after,
            "arc": Arc(center, radius, start, end, "visible"),
        }

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        edit = self.outline_edit(part)
        return (
            DimIntent(
                kind="radius",
                p1=edit["arc"].center,
                p2=edit["before"],
                text_override=f"R{float(self.radius):g}",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view == "front" else f"a corner radius has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class CornerChamfer(_Base):
    """A bevelled outline corner: two points and the straight run between them."""

    KIND: ClassVar[str] = "corner_chamfer"
    vertex: int
    size: float

    def _frame(self, part):
        points = _require_prismatic(part, "CornerChamfer").outline
        count = len(points)
        index = int(self.vertex)
        if not 0 <= index < count:
            raise ValueError(f"vertex {index} is outside the outline (0-{count - 1}).")
        return points[index], points[(index - 1) % count], points[(index + 1) % count]

    def validate(self, part) -> None:
        super().validate(part)
        size = _number(self.size, "size")
        v, p, n = self._frame(part)
        shortest = min(math.dist(v, p), math.dist(v, n))
        if size > shortest / 2.0 + _EPS:
            raise ValueError(
                f"size {size} is more than half the shorter adjacent edge ({shortest:.3f} mm)."
            )

    def outline_edit(self, part) -> dict:
        v, p, n = self._frame(part)
        size = float(self.size)
        edge_p = math.dist(v, p)
        edge_n = math.dist(v, n)
        before = (v[0] + (p[0] - v[0]) / edge_p * size, v[1] + (p[1] - v[1]) / edge_p * size)
        after = (v[0] + (n[0] - v[0]) / edge_n * size, v[1] + (n[1] - v[1]) / edge_n * size)
        return {"vertex": int(self.vertex), "before": before, "after": after, "arc": None}

    def dims(self, part, view: str, ctx: dict) -> tuple[DimIntent, ...]:
        if view != "front":
            return ()
        edit = self.outline_edit(part)
        return (
            DimIntent(
                kind="aligned",
                p1=edit["before"],
                p2=edit["after"],
                text_override=f"{float(self.size):g}x45°",
                feature=self.id,
            ),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view == "front" else f"a corner chamfer has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class BendLine(_Base):
    """A sheet-metal bend line, **representation only**.

    Spec section 2 puts flat patterns and bend allowance out of scope: no
    developed length is computed here, because a wrong developed length is a
    scrapped part. This feature draws the phantom line and its R/angle note and
    claims nothing else.
    """

    KIND: ClassVar[str] = "bend_line"
    p1: tuple[float, float]
    p2: tuple[float, float]
    angle: float
    inside_radius: float
    direction: str = "up"

    def validate(self, part) -> None:
        super().validate(part)
        _require_prismatic(part, "BendLine")
        for name, point in (("p1", self.p1), ("p2", self.p2)):
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise ValueError(f"{name}: expected [x, y], got {point!r}.")
            _number(point[0], f"{name}.x", positive=False)
            _number(point[1], f"{name}.y", positive=False)
        if math.dist(tuple(self.p1), tuple(self.p2)) < _EPS:
            raise ValueError("p1 and p2 are the same point; a bend line needs a direction.")
        angle = _number(self.angle, "angle", positive=False)
        if not 0.0 < abs(angle) <= 180.0:
            raise ValueError(f"angle {angle}: a bend angle is between 0 and 180 degrees.")
        _number(self.inside_radius, "inside_radius")
        if str(self.direction).strip().lower() not in ("up", "down"):
            raise ValueError(f"direction: expected 'up' or 'down', got {self.direction!r}.")

    def prims(self, part, view: str, ctx: dict) -> tuple[Prim, ...]:
        if view != "front":
            return ()
        p1 = (float(self.p1[0]), float(self.p1[1]))
        p2 = (float(self.p2[0]), float(self.p2[1]))
        mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
        rotation = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0]))
        note = (
            f"R{float(self.inside_radius):g} {float(self.angle):g}° "
            f"{str(self.direction).strip().upper()}"
        )
        return (
            Line(p1, p2, "phantom"),
            Text((mid[0], mid[1] + 2.0), note, 3.5, rotation, "text"),
        )

    def omission(self, part, view: str) -> str | None:
        return None if view == "front" else f"a bend line has no {view} projection"


@_register
@dataclass(frozen=True, kw_only=True)
class HolePattern(_Base):
    """A linear, grid or polar array of one base hole. Expanded before validation."""

    KIND: ClassVar[str] = "hole_pattern"
    pattern: str
    base: dict
    count: int = 2
    spacing: float = 0.0
    count_y: int = 1
    spacing_y: float = 0.0
    pcd: float = 0.0
    start_angle: float = 0.0
    angle: float = 0.0

    PATTERNS: ClassVar[tuple[str, ...]] = ("linear", "grid", "polar")

    def _base_fields(self) -> dict:
        if not isinstance(self.base, dict):
            raise ValueError(
                f"base: expected a dict of Hole fields, got {type(self.base).__name__}."
            )
        payload = {key: value for key, value in self.base.items() if key not in ("kind", "id")}
        names = {field_.name for field_ in fields(Hole)}
        unknown = sorted(set(payload) - names)
        if unknown:
            raise ValueError(f"base: unknown key(s) {unknown}; a Hole takes {sorted(names)}.")
        for required in ("x", "y", "diameter"):
            if required not in payload:
                raise ValueError(f"base.{required}: a base hole needs x, y and diameter.")
        return payload

    def expand(self) -> tuple[Hole, ...]:
        kind = str(self.pattern).strip().lower()
        if kind not in self.PATTERNS:
            raise ValueError(f"pattern: expected one of {self.PATTERNS}, got {self.pattern!r}.")
        base = self._base_fields()
        x0, y0 = float(base["x"]), float(base["y"])
        count = int(self.count)
        if count < 1:
            raise ValueError(f"count: expected at least 1, got {count}.")
        holes: list[Hole] = []
        if kind == "linear":
            if float(self.spacing) <= _EPS:
                raise ValueError("spacing: a linear pattern needs a spacing greater than zero.")
            theta = math.radians(float(self.angle))
            for k in range(count):
                holes.append(
                    Hole(
                        **{
                            **base,
                            "id": f"{self.id}_{k}",
                            "x": x0 + k * float(self.spacing) * math.cos(theta),
                            "y": y0 + k * float(self.spacing) * math.sin(theta),
                        }
                    )
                )
        elif kind == "grid":
            rows = int(self.count_y)
            if rows < 1:
                raise ValueError(f"count_y: expected at least 1, got {rows}.")
            if count > 1 and float(self.spacing) <= _EPS:
                raise ValueError("spacing: a grid with more than one column needs a spacing.")
            if rows > 1 and float(self.spacing_y) <= _EPS:
                raise ValueError("spacing_y: a grid with more than one row needs a spacing_y.")
            for j in range(rows):
                for i in range(count):
                    holes.append(
                        Hole(
                            **{
                                **base,
                                "id": f"{self.id}_{i}_{j}",
                                "x": x0 + i * float(self.spacing),
                                "y": y0 + j * float(self.spacing_y),
                            }
                        )
                    )
        else:
            if float(self.pcd) <= _EPS:
                raise ValueError("pcd: a polar pattern needs a pitch-circle diameter.")
            if count < 2:
                raise ValueError(f"count: a polar pattern needs at least 2 holes, got {count}.")
            radius = float(self.pcd) / 2.0
            start = math.radians(float(self.start_angle))
            for k in range(count):
                theta = start + 2.0 * math.pi * k / count
                holes.append(
                    Hole(
                        **{
                            **base,
                            "id": f"{self.id}_{k}",
                            "x": x0 + radius * math.cos(theta),
                            "y": y0 + radius * math.sin(theta),
                        }
                    )
                )
        return tuple(holes)

    def validate(self, part) -> None:  # pragma: no cover - expanded before it reaches a part
        super().validate(part)
        for hole in self.expand():
            hole.validate(part)


FEATURE_KINDS: tuple[str, ...] = tuple(sorted(_REGISTRY))
