"""The view engine: a part model projected onto a plane, as primitive descriptions.

Pure: no backend, no async, no DXF. A ``View`` carries the primitives, the
dimension intents its features asked for, the features it could **not** project
(the honesty rule of spec section 6 - nothing is silently dropped), and its own
bounding box, so the next view can be laid out against it.

The one calculation everything comes out of for a revolved part is the **radial
profile**: the part cut into bands of constant (or linearly tapering) outer
radius and constant bore radius, with every feature's ``radial_edit`` applied.
The silhouette is that profile walked as a path; the section cut face (Task 5)
is the same profile walked as a closed loop. One calculation, so the outline
and the hatched area can never disagree.

Line roles follow ISO 128 and are mapped to layers by
``engineering/mech/primitives.py``: the silhouette is ``visible``, a bore or a
feature behind material is ``hidden``, the axis and centre marks are
``center``, a bend line is ``phantom``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, NamedTuple

from engineering.mech.features import mirror_about_x
from engineering.mech.part import (
    PrismaticPart,
    RevolvedPart,
    outline_bbox,
    part_length,
    part_max_diameter,
    segment_bounds,
)
from engineering.mech.primitives import (
    Arc,
    Circle,
    DimIntent,
    HatchArea,
    Line,
    Poly,
    Prim,
    Pt,
    Text,
    bbox,
)

_EPS = 1e-9
_SNAP = 6  # decimals used to match a corner vertex to a corner edit
#: Radii closer together than this are the same circle, in millimetres. Looser
#: than ``_EPS`` on purpose: it compares *measured* radii, not exact vertices.
_TOL = 1e-6

VIEW_KINDS: tuple[str, ...] = ("front", "side", "top")
PROJECTIONS: tuple[str, ...] = ("first", "third")
SIDES: tuple[str, ...] = ("right", "left")

#: How far the axis and the centre lines run past the material, as a fraction
#: of the part's largest dimension (ISO 128-23 asks for a short, even overrun).
AXIS_OVERRUN = 0.10


@dataclass(frozen=True)
class View:
    kind: str
    prims: tuple[Prim, ...]
    dims: tuple[DimIntent, ...]
    omitted: tuple[dict, ...]
    bbox: tuple[float, float, float, float]
    label: str | None = None
    scale: float = 1.0


class Band(NamedTuple):
    """One axial slice of a revolved part with a constant bore and a linear outside."""

    x0: float
    x1: float
    r_in: float
    r_out0: float
    r_out1: float


# -- the radial profile ------------------------------------------------------


def _band_radii(part: RevolvedPart, a: float, b: float) -> tuple[float, float, float]:
    """``(r_out0, r_out1, r_in)`` of one band, read off the segment it lies in.

    The segment is resolved from the band's **midpoint** - a band never spans a
    segment boundary, because every boundary is one of the cuts - and a taper is
    then evaluated at the band's own two ends. Sampling a hair inside the band
    instead (the obvious way to dodge the boundary ambiguity) writes the taper
    slope times that inset into every coordinate the silhouette is built from:
    a 30 -> 10 mm cone over 20 mm reports 14.99999 for a radius that is exactly
    15, and nothing downstream can tell that it is wrong.
    """
    mid = (a + b) / 2.0
    found: tuple[float, float, Any] | None = None
    for (x0, x1), segment in zip(segment_bounds(part), part.segments, strict=True):
        if x0 - _EPS <= mid <= x1 + _EPS:
            found = (x0, x1, segment)
            break
    if found is None:  # pragma: no cover - the cuts always include every segment bound
        raise ValueError(f"radial_profile: x={mid} is outside the part.")
    x0, x1, segment = found
    r_in = segment.d_inner / 2.0
    if segment.taper_to is None:
        radius = segment.d_outer / 2.0
        return radius, radius, r_in
    span = x1 - x0

    def taper_at(x: float) -> float:
        t = 0.0 if span <= _EPS else (x - x0) / span
        return (segment.d_outer + t * (segment.taper_to - segment.d_outer)) / 2.0

    return taper_at(a), taper_at(b), r_in


def radial_profile(part: RevolvedPart) -> tuple[Band, ...]:
    """The part as bands, with every feature's ``radial_edit`` applied."""
    length = part_length(part)
    cuts: set[float] = {0.0, length}
    for x0, x1 in segment_bounds(part):
        cuts.add(x0)
        cuts.add(x1)

    edits: list[dict] = []
    for feature in part.features:
        for edit in feature.radial_edit(part):
            x0 = max(0.0, min(length, float(edit["x0"])))
            x1 = max(0.0, min(length, float(edit["x1"])))
            if x1 - x0 <= _EPS:
                continue
            edits.append(
                {"kind": edit["kind"], "x0": x0, "x1": x1, "radius": float(edit["radius"])}
            )
            cuts.add(x0)
            cuts.add(x1)

    xs = sorted(cuts)
    bands: list[Band] = []
    for a, b in zip(xs, xs[1:], strict=False):
        if b - a <= _EPS:
            continue
        r_out0, r_out1, r_in = _band_radii(part, a, b)
        for edit in edits:
            if edit["x0"] - _EPS <= a and b <= edit["x1"] + _EPS:
                if edit["kind"] == "outer":
                    r_out0 = min(r_out0, edit["radius"])
                    r_out1 = min(r_out1, edit["radius"])
                else:
                    r_in = max(r_in, edit["radius"])
        bands.append(Band(a, b, r_in, r_out0, r_out1))
    return tuple(bands)


def _outer_path(bands: tuple[Band, ...]) -> list[Pt]:
    """The +Y half-outline, from the left face at the bore, over the outside, down
    to the right face at the bore. The end faces are *part of the path*, so a
    chamfer or fillet at an end corner substitutes into them instead of being
    drawn across them - which is what stops the overshoot the `untrimmed_corner`
    critique focus looks for."""
    points: list[Pt] = [(bands[0].x0, bands[0].r_in), (bands[0].x0, bands[0].r_out0)]
    for band in bands:
        if math.dist(points[-1], (band.x0, band.r_out0)) > _EPS:
            points.append((band.x0, band.r_out0))
        points.append((band.x1, band.r_out1))
    points.append((bands[-1].x1, bands[-1].r_in))
    return points


def _bore_path(bands: tuple[Band, ...]) -> list[list[Pt]]:
    """One path per contiguous run of bored bands; a blind end is capped."""
    runs: list[list[Pt]] = []
    current: list[Pt] = []
    for band in bands:
        if band.r_in <= _EPS:
            if current:
                runs.append(current)
                current = []
            continue
        if not current:
            current = [(band.x0, band.r_in)]
        elif math.dist(current[-1], (band.x0, band.r_in)) > _EPS:
            current.append((band.x0, band.r_in))
        current.append((band.x1, band.r_in))
    if current:
        runs.append(current)
    return runs


def _key(point: Pt) -> tuple[float, float]:
    return (round(float(point[0]), _SNAP), round(float(point[1]), _SNAP))


def _emit_path(points: list[Pt], edits: list[dict], role: str, *, closed: bool = False):
    """Walk a vertex path, substituting corner edits, and emit Lines and Arcs.

    Returns ``(prims, used)`` - ``used`` is the set of edit ids that actually
    found their corner, so the caller can report the ones that did not.
    """
    by_corner: dict[tuple[float, float], dict] = {}
    for edit in edits:
        by_corner[_key(edit["corner"])] = edit

    sequence: list[tuple[str, Any]] = []
    used: set[str] = set()
    for point in points:
        edit = by_corner.get(_key(point))
        if edit is None:
            sequence.append(("pt", (float(point[0]), float(point[1]))))
            continue
        used.add(edit["owner"])
        sequence.append(("pt", edit["before"]))
        if edit["arc"] is not None:
            sequence.append(("arc", edit["arc"]))
        sequence.append(("pt", edit["after"]))

    if closed and sequence:
        sequence.append(sequence[0])

    prims: list[Prim] = []
    previous: Pt | None = None
    for tag, item in sequence:
        if tag == "pt":
            if previous is not None and math.dist(previous, item) > _EPS:
                prims.append(Line(previous, item, role))
            previous = item
        else:
            prims.append(item)
            previous = None
    return tuple(prims), used


def silhouette_prims(part: RevolvedPart) -> tuple[tuple[Prim, ...], tuple[dict, ...]]:
    """The mirrored half-outline (end faces included), the bore, and any unplaced edit."""
    bands = radial_profile(part)
    edits: list[dict] = []
    for feature in part.features:
        edit = feature.corner_edit(part)
        if edit is not None:
            edits.append({**edit, "owner": feature.id})

    upper, used = _emit_path(_outer_path(bands), edits, "visible")
    prims: list[Prim] = list(upper) + list(mirror_about_x(upper))

    for run in _bore_path(bands):
        bore, _ = _emit_path(run, [], "hidden")
        prims.extend(bore)
        prims.extend(mirror_about_x(bore))

    unplaced = tuple(
        {
            "feature": edit["owner"],
            "reason": (
                "its silhouette corner was changed by another feature, so the "
                "chamfer/fillet could not be placed"
            ),
        }
        for edit in edits
        if edit["owner"] not in used
    )
    return tuple(prims), unplaced


def outline_prims(part: PrismaticPart) -> tuple[tuple[Prim, ...], tuple[dict, ...]]:
    """The closed outline with every corner fillet/chamfer substituted in."""
    edits: list[dict] = []
    for feature in part.features:
        edit = feature.outline_edit(part)
        if edit is None:
            continue
        corner = part.outline[int(edit["vertex"]) % len(part.outline)]
        edits.append({**edit, "corner": corner, "owner": feature.id})

    prims, used = _emit_path(list(part.outline), edits, "visible", closed=True)
    unplaced = tuple(
        {"feature": edit["owner"], "reason": "its outline vertex was already replaced"}
        for edit in edits
        if edit["owner"] not in used
    )
    return prims, unplaced


# -- view construction -------------------------------------------------------


def _axis_prims(part: RevolvedPart) -> tuple[Prim, ...]:
    length = part_length(part)
    over = max(length, part_max_diameter(part)) * AXIS_OVERRUN
    return (Line((-over, 0.0), (length + over, 0.0), "center"),)


def _band_order(bands: tuple[Band, ...], side: str) -> list[Band]:
    """The bands from the end the observer stands at, nearest first."""
    return list(reversed(bands)) if side == "right" else list(bands)


def _band_radii_near_first(band: Band, side: str) -> tuple[float, float]:
    """A band's two outer radii, the one nearer the observer first.

    They differ only on a taper: ``r_out0`` sits at ``x0`` and ``r_out1`` at
    ``x1``, so which of them faces the observer depends on the end they are
    looking from.
    """
    return (band.r_out1, band.r_out0) if side == "right" else (band.r_out0, band.r_out1)


def _front_max(bands: tuple[Band, ...], side: str, x: float) -> float:
    """The largest outer radius of the material lying **between** ``x`` and the observer.

    This is the whole hidden-line rule for a view along the axis of a solid of
    revolution: a circular edge of radius ``r`` at station ``x`` is drawn
    continuous when ``r`` is larger than everything in front of it, because then
    nothing can cover it, and dashed when it is not, because the material in
    front does cover it. A step face whose circle is *larger* than the material
    ahead of it is the one edge that cannot be occluded.
    """
    best = 0.0
    for band in bands:
        nearer = band.x0 >= x - _TOL if side == "right" else band.x1 <= x + _TOL
        if nearer:
            best = max(best, band.r_out0, band.r_out1)
    return best


def _end_circles(part: RevolvedPart, side: str) -> tuple[Prim, ...]:
    """The concentric circles the profile crosses at one end, roled by occlusion.

    Outer edges: walking from the observer, a radius larger than everything
    already passed is ``visible``; a smaller one is behind that material and is
    ``hidden`` - it is still drawn, dashed, because it is a real edge of the
    part. Bore edges follow the mirror-image rule: you can see down a bore only
    while it stays at least as wide, so a bore radius no larger than the
    narrowest bore in front of it is ``visible`` and anything wider (or anything
    behind solid material) is ``hidden``.

    Each distinct radius is emitted once: two coincident circles would be a
    duplicate entity, which is what ``drawing_critique`` reports.
    """
    bands = radial_profile(part)
    order = _band_order(bands, side)

    prims: list[Prim] = []
    emitted: set[float] = set()

    def emit(radius: float, role: str) -> None:
        key = round(float(radius), _SNAP)
        if key <= _TOL or key in emitted:
            return
        emitted.add(key)
        prims.append(Circle((0.0, 0.0), float(radius), role))

    front_max = 0.0
    for band in order:
        for radius in _band_radii_near_first(band, side):
            emit(radius, "visible" if radius > front_max + _TOL else "hidden")
            front_max = max(front_max, radius)

    open_r = math.inf
    for band in order:
        if band.r_in <= _TOL:
            open_r = 0.0  # solid across the whole section: nothing behind is seen
            continue
        emit(band.r_in, "visible" if band.r_in <= open_r + _TOL else "hidden")
        open_r = min(open_r, band.r_in)

    over = front_max * (1.0 + AXIS_OVERRUN)
    prims.append(Line((-over, 0.0), (over, 0.0), "center"))
    prims.append(Line((0.0, -over), (0.0, over), "center"))
    return tuple(prims)


def _rectangle(
    u0: float, u1: float, v0: float, v1: float, role: str = "visible"
) -> tuple[Prim, ...]:
    return (
        Line((u0, v0), (u1, v0), role),
        Line((u1, v0), (u1, v1), role),
        Line((u1, v1), (u0, v1), role),
        Line((u0, v1), (u0, v0), role),
    )


def _mirror_u(prims, axis: float) -> tuple[Prim, ...]:
    """Reflect primitives about the vertical line ``u = axis`` (screen left <-> right).

    ``Text`` moves with the drawing but keeps its rotation: a mirrored label
    would read backwards, and no drawing has ever wanted that.
    """

    def flip(point: Pt) -> Pt:
        return (2.0 * axis - float(point[0]), float(point[1]))

    out: list[Prim] = []
    for prim in prims:
        if isinstance(prim, Line):
            out.append(Line(flip(prim.p1), flip(prim.p2), prim.role))
        elif isinstance(prim, Arc):
            # reflection reverses the sweep, so the CCW arc runs from the
            # reflected end angle to the reflected start angle
            out.append(
                Arc(
                    flip(prim.center),
                    prim.radius,
                    (180.0 - prim.end_deg) % 360.0,
                    (180.0 - prim.start_deg) % 360.0,
                    prim.role,
                )
            )
        elif isinstance(prim, Circle):
            out.append(Circle(flip(prim.center), prim.radius, prim.role))
        elif isinstance(prim, Poly):
            out.append(Poly(tuple(flip(pt) for pt in prim.points), prim.closed, prim.role))
        elif isinstance(prim, HatchArea):
            out.append(
                HatchArea(
                    tuple(tuple(flip(pt) for pt in loop) for loop in prim.loops), prim.material
                )
            )
        elif isinstance(prim, Text):
            out.append(prim._replace(at=flip(prim.at)))
        else:  # pragma: no cover - Prim is a closed union
            raise TypeError(f"_mirror_u: not a primitive: {prim!r}")
    return tuple(out)


def _same_geometry(a: Prim, b: Prim) -> bool:
    """True when two primitives would be drawn on top of each other, role aside.

    Exactly what ``engineering/critique.py::_check_duplicate_entities`` matches,
    so anything this returns True for is a warning on the finished drawing.
    """
    if isinstance(a, Line) and isinstance(b, Line):
        forward = math.dist(a.p1, b.p1) <= _TOL and math.dist(a.p2, b.p2) <= _TOL
        reverse = math.dist(a.p1, b.p2) <= _TOL and math.dist(a.p2, b.p1) <= _TOL
        return forward or reverse
    if isinstance(a, Circle) and isinstance(b, Circle):
        return math.dist(a.center, b.center) <= _TOL and abs(a.radius - b.radius) <= _TOL
    if isinstance(a, Arc) and isinstance(b, Arc):
        return (
            math.dist(a.center, b.center) <= _TOL
            and abs(a.radius - b.radius) <= _TOL
            and abs((a.start_deg - b.start_deg) % 360.0) <= _TOL
            and abs((a.end_deg - b.end_deg) % 360.0) <= _TOL
        )
    return False


def _profile_owned(feature, part, prim: Prim, kind: str, body: list[Prim]) -> bool:
    """True when the radial profile has already drawn this primitive.

    Two separate reasons, and both are the same principle - the profile is the
    one calculation the silhouette comes out of, so a feature never draws it a
    second time:

    * the primitive is coincident with something the body already emitted; or
    * the feature edits the radial profile and this is a **silhouette** line of
      a revolved front view. ``radial_profile`` already cut the notch, and the
      feature's own idea of where the surface is can even contradict it - a
      groove straddling a step reads one surface radius for a span that has two.
    """
    if any(_same_geometry(prim, other) for other in body):
        return True
    if kind == "front" and getattr(prim, "role", None) == "visible":
        return bool(feature.radial_edit(part))
    return False


def _end_view_role(feature, part, prim: Prim, bands: tuple[Band, ...], side: str) -> Prim:
    """Re-role a feature's axis-centred end-view circle by the same occlusion rule.

    The feature knows its diameter, not what is standing in front of it. Its
    axial station comes from ``removal_box``; a feature that does not report one
    keeps its own verdict rather than having one guessed for it.
    """
    if not isinstance(prim, Circle) or math.dist(prim.center, (0.0, 0.0)) > _TOL:
        return prim
    box = feature.removal_box(part)
    if box is None:
        return prim
    x_near = float(box[2]) if side == "right" else float(box[0])
    covered = _front_max(bands, side, x_near)
    return prim._replace(role="visible" if prim.radius > covered + _TOL else "hidden")


def _feature_layer(
    part,
    kind: str,
    ctx: dict,
    *,
    body: list[Prim] | None = None,
    bands: tuple[Band, ...] | None = None,
) -> tuple[list[Prim], list[DimIntent], list[dict]]:
    drawn: list[Prim] = list(body or ())
    prims: list[Prim] = []
    dims: list[DimIntent] = []
    omitted: list[dict] = []
    end_view = bands is not None and kind == "side"
    side = str(ctx.get("side", "right")).strip().lower()
    for feature in part.features:
        own = tuple(feature.prims(part, kind, ctx))
        if end_view:
            own = tuple(_end_view_role(feature, part, prim, bands, side) for prim in own)
        own = tuple(prim for prim in own if not _profile_owned(feature, part, prim, kind, drawn))
        if own and kind == "front" and getattr(feature, "axisymmetric", False):
            own = tuple(own) + mirror_about_x(own)
        prims.extend(own)
        drawn.extend(own)
        dims.extend(feature.dims(part, kind, ctx))
        if own:
            continue
        if feature.radial_edit(part) or feature.corner_edit(part) or feature.outline_edit(part):
            continue
        reason = feature.omission(part, kind)
        if reason:
            omitted.append({"feature": feature.id, "reason": reason})
    return prims, dims, omitted


def build_view(
    part,
    kind: str,
    *,
    side: str = "right",
    plane: dict | None = None,
    style: str = "full",
    scale: float = 1.0,
    projection: str = "first",
    detail: dict | None = None,
) -> View:
    """Project a part onto one view plane and return its primitive description.

    ``side`` is the end the observer stands at for an end view, and it decides
    both what is in front of what and which way round the view reads. The
    ``top`` view is always seen from above, so ``side`` does not apply to it.
    """
    name = str(kind or "").strip().lower()
    if name not in VIEW_KINDS:
        raise ValueError(f"build_view: kind {kind!r} is not one of {VIEW_KINDS}.")
    where = str(side or "right").strip().lower()
    if where not in SIDES:
        raise ValueError(f"build_view: side {side!r} is not one of {SIDES}.")
    del plane, style, detail  # the orthographic kinds take no plane or detail

    ctx = {"side": where, "scale": float(scale), "projection": str(projection)}
    prims: list[Prim] = []
    omitted: list[dict] = []
    bands: tuple[Band, ...] | None = None
    mirror_u: float | None = None

    if isinstance(part, RevolvedPart):
        if name == "top":
            raise ValueError(
                "build_view: a revolved part's top view is identical to its front view; "
                "use kind='front', or kind='section' for a cut."
            )
        if name == "front":
            body, unplaced = silhouette_prims(part)
            prims.extend(body)
            prims.extend(_axis_prims(part))
            omitted.extend(unplaced)
        else:
            bands = radial_profile(part)
            prims.extend(_end_circles(part, where))
    elif isinstance(part, PrismaticPart):
        x0, y0, x1, y1 = outline_bbox(part)
        if name == "front":
            body, unplaced = outline_prims(part)
            prims.extend(body)
            omitted.extend(unplaced)
        elif name == "side":
            prims.extend(_rectangle(y0, y1, 0.0, part.thickness))
            # An observer standing at -X sees screen-right = -Y, so the left
            # view is the right view mirrored. The thickness rectangle spans
            # y0..y1 either way and is its own mirror; the features are not.
            if where == "left":
                mirror_u = (y0 + y1) / 2.0
        else:
            prims.extend(_rectangle(x0, x1, 0.0, part.thickness))
    else:  # pragma: no cover - build_part only makes these two
        raise ValueError(f"build_view: {type(part).__name__} is not a part model.")

    feature_prims, dims, feature_omitted = _feature_layer(part, name, ctx, body=prims, bands=bands)
    if mirror_u is not None:
        feature_prims = list(_mirror_u(feature_prims, mirror_u))
    prims.extend(feature_prims)
    omitted.extend(feature_omitted)

    return View(
        kind=name,
        prims=tuple(prims),
        dims=tuple(dims),
        omitted=tuple(omitted),
        bbox=bbox(prims) if prims else (0.0, 0.0, 0.0, 0.0),
        label=None,
        scale=float(scale),
    )


def layout_origin(
    parent: View,
    kind: str,
    *,
    side: str = "right",
    projection: str = "first",
    gap: float = 20.0,
    child: View | None = None,
) -> Pt:
    """Where a child view's bounding box lower-left corner goes, relative to its parent.

    First angle (ISO 128-30, the default here): the view obtained by looking
    from the right is placed on the **left**, and the view from above is placed
    **below**. Third angle flips both. With no ``child`` the child is assumed
    the same size as the parent, which keeps the views aligned even before the
    child has been built.
    """
    projection_name = str(projection or "").strip().lower()
    if projection_name not in PROJECTIONS:
        raise ValueError(f"layout_origin: projection {projection!r} is not one of {PROJECTIONS}.")
    px0, py0, px1, py1 = parent.bbox
    if child is None:
        width, height = px1 - px0, py1 - py0
    else:
        cx0, cy0, cx1, cy1 = child.bbox
        width, height = cx1 - cx0, cy1 - cy0
    mid_x = (px0 + px1) / 2.0
    mid_y = (py0 + py1) / 2.0
    first = projection_name == "first"
    name = str(kind or "").strip().lower()
    gap = float(gap)

    if name in ("side", "section"):
        looking_from_right = str(side or "right").strip().lower() == "right"
        to_the_left = looking_from_right == first
        if to_the_left:
            return (px0 - gap - width, mid_y - height / 2.0)
        return (px1 + gap, mid_y - height / 2.0)
    if name == "top":
        if first:
            return (mid_x - width / 2.0, py0 - gap - height)
        return (mid_x - width / 2.0, py1 + gap)
    if name == "detail":
        return (px1 + gap, mid_y - height / 2.0)
    raise ValueError(
        f"layout_origin: kind {kind!r} has no projection position; expected "
        "'side', 'top', 'section' or 'detail'."
    )
