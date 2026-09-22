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

from engineering.mech.features import Pocket as _PocketType
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
    translate,
)
from engineering.mech.primitives import scale as scale_prims

_EPS = 1e-9
_SNAP = 6  # decimals used to match a corner vertex to a corner edit
#: Radii closer together than this are the same circle, in millimetres. Looser
#: than ``_EPS`` on purpose: it compares *measured* radii, not exact vertices.
_TOL = 1e-6

VIEW_KINDS: tuple[str, ...] = ("front", "side", "top", "section", "detail")
PROJECTIONS: tuple[str, ...] = ("first", "third")
SIDES: tuple[str, ...] = ("right", "left")

#: How far the axis and the centre lines run past the material, as a fraction
#: of the part's largest dimension (ISO 128-23 asks for a short, even overrun).
AXIS_OVERRUN = 0.10

#: The cutting-plane record `section_line` (Task 11, group A) produces and this
#: engine consumes. `via` is optional and only meaningful for an offset plane.
SECTION_PLANE = {
    "p1": (0.0, 0.0),
    "p2": (0.0, 0.0),
    "label": "A",
    "direction": (0.0, -1.0),
    "style": "full",
}

SECTION_STYLES: tuple[str, ...] = ("full", "half", "offset", "revolved")

#: A circular cut-face boundary has to be handed to the hatch as points. 180
#: segments is a 2 degree chord: the boundary is never further than
#: r * (1 - cos(1 deg)) = 0.015% of the radius from the true circle, and the
#: area of the inscribed 180-gon is 0.0203% under the circle's. Both figures are
#: stated rather than hidden, and the same loop is what the hatch and any area
#: check read, so they cannot disagree with each other.
CIRCLE_SEGMENTS = 180


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


def _corner_edits(part: RevolvedPart) -> list[dict]:
    """Every feature's silhouette corner substitution, tagged with its owner."""
    edits: list[dict] = []
    for feature in part.features:
        edit = feature.corner_edit(part)
        if edit is not None:
            edits.append({**edit, "owner": feature.id})
    return edits


def _unplaced(edits: list[dict], used: set[str]) -> tuple[dict, ...]:
    return tuple(
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


def silhouette_prims(part: RevolvedPart) -> tuple[tuple[Prim, ...], tuple[dict, ...]]:
    """The mirrored half-outline (end faces included), the bore, and any unplaced edit."""
    bands = radial_profile(part)
    edits = _corner_edits(part)

    upper, used = _emit_path(_outer_path(bands), edits, "visible")
    prims: list[Prim] = list(upper) + list(mirror_about_x(upper))

    for run in _bore_path(bands):
        bore, _ = _emit_path(run, [], "hidden")
        prims.extend(bore)
        prims.extend(mirror_about_x(bore))

    return tuple(prims), _unplaced(edits, used)


def section_profile_prims(
    part: RevolvedPart, *, half: bool = False
) -> tuple[tuple[Prim, ...], tuple[dict, ...]]:
    """The longitudinal **cut** outline of a revolved part.

    Every edge of this outline lies in the cutting plane, so every edge is a
    continuous line: ISO 128-50 bounds a surface lying in the cutting plane
    with a continuous wide line, and ISO 128-3 leaves hidden detail out of a
    sectional view altogether. The bore wall is the case that matters - the
    un-cut silhouette draws it dashed because material hides it, but in a
    section it *is* the cut face's own boundary, and drawing the hatch loop and
    a dashed line on the same coordinates is the one thing a section must not
    do. ``half=True`` keeps the dashed bore on the -Y half, which is the half a
    half section does not cut.
    """
    bands = radial_profile(part)
    edits = _corner_edits(part)

    upper, used = _emit_path(_outer_path(bands), edits, "visible")
    prims: list[Prim] = list(upper) + list(mirror_about_x(upper))

    for run in _bore_path(bands):
        bore, _ = _emit_path(run, [], "visible")
        prims.extend(bore)
        mirrored = mirror_about_x(bore)
        if half:
            mirrored = tuple(prim._replace(role="hidden") for prim in mirrored)
        prims.extend(mirrored)

    return tuple(prims), _unplaced(edits, used)


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
    if name == "section":
        return section_view(part, plane, style=style, scale=scale, side=where)
    if name == "detail":
        return detail_view(part, detail or {}, projection=projection, side=where)

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


# -- sections ----------------------------------------------------------------


def normalise_plane(plane: dict | None) -> dict:
    """Fill the `SECTION_PLANE` shape and refuse a plane that has no direction."""
    if plane is None:
        raise ValueError(
            "section: a cutting plane is required - pass plane={'p1': ..., 'p2': ...}, "
            "or take one from section_line()."
        )
    if not isinstance(plane, dict):
        raise ValueError(f"plane: expected a dict, got {type(plane).__name__}.")
    unknown = sorted(set(plane) - set(SECTION_PLANE) - {"via"})
    if unknown:
        raise ValueError(f"plane: unknown key(s) {unknown}; it takes {sorted(SECTION_PLANE)}.")
    p1 = tuple(float(v) for v in plane.get("p1", SECTION_PLANE["p1"]))
    p2 = tuple(float(v) for v in plane.get("p2", SECTION_PLANE["p2"]))
    if math.dist(p1, p2) < 1e-6:
        raise ValueError(f"plane: p1 and p2 are the same point, the plane has zero length ({p1}).")
    style = str(plane.get("style", SECTION_PLANE["style"])).strip().lower()
    if style not in SECTION_STYLES:
        raise ValueError(f"plane.style: expected one of {SECTION_STYLES}, got {style!r}.")
    via = tuple(tuple(float(v) for v in point) for point in plane.get("via", ()))
    return {
        "p1": p1,
        "p2": p2,
        "label": str(plane.get("label", SECTION_PLANE["label"])),
        "direction": tuple(float(v) for v in plane.get("direction", SECTION_PLANE["direction"])),
        "style": style,
        "via": via,
    }


def _inner_path(bands: tuple[Band, ...]) -> list[Pt]:
    points: list[Pt] = [(bands[0].x0, bands[0].r_in)]
    for band in bands:
        if math.dist(points[-1], (band.x0, band.r_in)) > _EPS:
            points.append((band.x0, band.r_in))
        points.append((band.x1, band.r_in))
    return points


def _arc_loop_points(
    radius: float, start_deg: float, end_deg: float, *, center: Pt = (0.0, 0.0)
) -> tuple[Pt, ...]:
    """An arc as points, at the same 2 degree chord ``CIRCLE_SEGMENTS`` states.

    Endpoints are exact, so a loop built from an arc plus a polyline closes on
    the polyline's own corners.
    """
    sweep = (float(end_deg) - float(start_deg)) % 360.0
    if sweep <= _EPS:
        sweep = 360.0
    count = max(1, math.ceil(sweep / (360.0 / CIRCLE_SEGMENTS) - 1e-9))
    out: list[Pt] = []
    for step in range(count + 1):
        angle = math.radians(float(start_deg) + sweep * step / count)
        out.append((center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle)))
    return tuple(out)


def _flatten_path(prims) -> tuple[Pt, ...]:
    """A contiguous run of ``Line``/``Arc`` primitives as a point path.

    This is what makes the drawn outline and the hatched loop **one**
    calculation rather than two that agree by inspection: the loop is the
    emitted outline, flattened, so a chamfer or a fillet that substitutes a
    silhouette corner substitutes it in the cut face too.
    """
    points: list[Pt] = []

    def push(point: Pt) -> None:
        pt = (float(point[0]), float(point[1]))
        if not points or math.dist(points[-1], pt) > _EPS:
            points.append(pt)

    for prim in prims:
        if isinstance(prim, Line):
            push(prim.p1)
            push(prim.p2)
        elif isinstance(prim, Arc):
            for point in _arc_loop_points(
                prim.radius, prim.start_deg, prim.end_deg, center=prim.center
            ):
                push(point)
        else:  # pragma: no cover - only an outline path is ever flattened
            raise TypeError(f"_flatten_path: {type(prim).__name__} is not a path primitive.")
    return tuple(points)


def _circle_loop(radius: float, *, reverse: bool = False) -> tuple[Pt, ...]:
    steps = range(CIRCLE_SEGMENTS - 1, -1, -1) if reverse else range(CIRCLE_SEGMENTS)
    return tuple(
        (
            radius * math.cos(2.0 * math.pi * k / CIRCLE_SEGMENTS),
            radius * math.sin(2.0 * math.pi * k / CIRCLE_SEGMENTS),
        )
        for k in steps
    )


def _is_longitudinal(plane: dict) -> bool:
    """True when the cutting plane runs along the revolved axis (y = 0 at both ends)."""
    return abs(plane["p1"][1]) < 1e-6 and abs(plane["p2"][1]) < 1e-6


def _subtract(intervals: list[tuple[float, float]], cut: tuple[float, float]):
    out: list[tuple[float, float]] = []
    low, high = cut
    for a, b in intervals:
        if high <= a + _EPS or low >= b - _EPS:
            out.append((a, b))
            continue
        if low > a + _EPS:
            out.append((a, low))
        if high < b - _EPS:
            out.append((high, b))
    return [(a, b) for a, b in out if b - a > 1e-9]


def _chords(outline, p1: Pt, unit: Pt) -> list[tuple[float, float]]:
    """Parameters along `unit` from `p1` where the line is inside the outline.

    Crossings are paired in order, which is exact for a simple polygon whose
    edges the line crosses transversally. A line that grazes a vertex produces
    an odd crossing count and is refused by the caller rather than paired wrong.
    """
    ts: list[float] = []
    count = len(outline)
    for index in range(count):
        ax, ay = outline[index]
        bx, by = outline[(index + 1) % count]
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
            "section: the cutting plane grazes an outline vertex, so its crossings cannot be "
            "paired. Move the plane off the vertex rather than accept a guessed cut face."
        )
    return [(ts[i], ts[i + 1]) for i in range(0, len(ts), 2)]


def _plane_legs(plane: dict) -> list[tuple[Pt, Pt]]:
    """The cutting legs of an offset plane.

    ISO 128-40: the offset itself is not drawn, so with ``via`` present the
    odd-numbered legs are the transfer jogs and contribute nothing to the cut
    face.
    """
    points = [plane["p1"], *plane["via"], plane["p2"]]
    legs = [(points[i], points[i + 1]) for i in range(len(points) - 1)]
    if plane["via"]:
        legs = [leg for index, leg in enumerate(legs) if index % 2 == 0]
    return legs


def _prismatic_cut(part: PrismaticPart, plane: dict, style: str):
    """``[(u0, u1, v0, v1, hatched), ...]`` - the cut rectangles along the plane."""
    legs = _plane_legs(plane) if style == "offset" else [(plane["p1"], plane["p2"])]
    thickness = float(part.thickness)
    rectangles: list[tuple[float, float, float, float, bool]] = []
    origin = 0.0
    for start, end in legs:
        length = math.dist(start, end)
        if length < 1e-9:
            continue
        unit = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
        intervals = _chords(part.outline, start, unit)
        intervals = [(a, b) for a, b in intervals if b > -1e-9 and a < length + 1e-9]
        intervals = [(max(a, 0.0), min(b, length)) for a, b in intervals]
        intervals = [(a, b) for a, b in intervals if b - a > 1e-9]

        pockets: list[tuple[tuple[float, float], float, bool]] = []
        for feature in part.features:
            # the feature's own chord, never the box around it: a box punches
            # the full width of a hole or an angled slot into the cut face
            # wherever the plane passes, which is a wrong hatched area and a
            # wrong drawn outline at the same time.
            for span in feature.cut_spans(part, start, unit):
                span = (max(span[0], 0.0), min(span[1], length))
                if span[1] - span[0] <= 1e-9:
                    continue
                if isinstance(feature, _PocketType):
                    pockets.append(
                        (span, thickness - float(feature.depth), feature.no_section_hatch)
                    )
                else:
                    intervals = _subtract(intervals, span)

        for pocket_span, floor, unhatched in pockets:
            intervals = _subtract(intervals, pocket_span)
            rectangles.append(
                (origin + pocket_span[0], origin + pocket_span[1], 0.0, floor, not unhatched)
            )
        for a, b in intervals:
            rectangles.append((origin + a, origin + b, 0.0, thickness, True))
        origin += length
    if rectangles:
        # the section view has its own frame: u = 0 at the first cut.
        shift = min(u0 for u0, _u1, _v0, _v1, _hatched in rectangles)
        rectangles = [
            (u0 - shift, u1 - shift, v0, v1, hatched) for u0, u1, v0, v1, hatched in rectangles
        ]
    return rectangles


def _band_at(bands: tuple[Band, ...], station: float) -> Band | None:
    for band in bands:
        if band.x0 - _TOL <= station <= band.x1 + _TOL:
            return band
    return None


def _represented_by_the_profile(feature, part, station: float) -> bool:
    """True when the radial profile already carries this feature at ``station``.

    A groove, an undercut, an axial bore or an ISO 6410 internal thread edits
    the profile, and the transverse cut face is read off that profile - so the
    feature is in the face already and must not be reported missing from it.
    """
    for edit in feature.radial_edit(part):
        if float(edit["x0"]) - _TOL <= station <= float(edit["x1"]) + _TOL:
            return True
    return False


def _transverse_face(part: RevolvedPart, station: float) -> dict:
    """One transverse cut face: its boundary primitives, its loops, its omissions.

    The radii come from ``radial_profile``, the same one calculation the
    silhouette and the longitudinal cut face come out of, so a groove or an
    internal thread at that station is in the face without being asked for.
    A feature that is *not* axisymmetric is asked for its own ``cross_section``
    notch; one that the plane really passes through and that has no such record
    is reported rather than hatched over - a keyed shaft whose cross section
    came out as a plain hatched disc is exactly the drawing the honesty rule
    exists to prevent.
    """
    bands = radial_profile(part)
    band = _band_at(bands, station)
    if band is None:
        raise ValueError(
            f"section: the plane at x={station:g} is outside {part.name!r} "
            f"(0 .. {part_length(part):g} mm)."
        )
    span = band.x1 - band.x0
    fraction = 0.0 if span <= _EPS else (station - band.x0) / span
    r_out = band.r_out0 + fraction * (band.r_out1 - band.r_out0)
    r_in = band.r_in

    notch: dict | None = None
    omitted: list[dict] = []
    for feature in part.features:
        record = feature.cross_section(part, station)
        if record is not None:
            if notch is not None:
                omitted.append(
                    {
                        "feature": feature.id,
                        "reason": (
                            "the cut face already carries another feature's notch; this "
                            "engine cuts one notch per transverse face"
                        ),
                    }
                )
                continue
            measured = float(record["radius"])
            if abs(measured - r_out) > _TOL:
                omitted.append(
                    {
                        "feature": feature.id,
                        "reason": (
                            f"its notch was measured on a {2.0 * measured:g} mm surface but "
                            f"the cut face at x={station:g} is {2.0 * r_out:g} mm, so the "
                            "notch would not reach it"
                        ),
                    }
                )
                continue
            notch = record
            continue
        if _represented_by_the_profile(feature, part, station):
            continue
        box = feature.removal_box(part)
        if box is not None and float(box[0]) - _TOL <= station <= float(box[2]) + _TOL:
            omitted.append(
                {
                    "feature": feature.id,
                    "reason": (
                        f"the cutting plane at x={station:g} passes through it, but a "
                        f"{type(feature).__name__} has no transverse cut-face projection"
                    ),
                }
            )
        else:
            omitted.append(
                {
                    "feature": feature.id,
                    "reason": f"it is not on the cutting plane at x={station:g}",
                }
            )

    if notch is None:
        loops: list[tuple[tuple[Pt, ...], bool]] = [(_circle_loop(r_out), True)]
        prims: list[Prim] = [Circle((0.0, 0.0), r_out, "visible")]
    else:
        path = tuple((float(x), float(y)) for x, y in notch["path"])
        start, end = float(notch["start_deg"]), float(notch["end_deg"])
        # the material arc plus the notch walls: one boundary, and the loop is
        # that same boundary sampled, so the hatch cannot cover the notch. The
        # arc ends where the path ends, so the path is walked backwards to
        # close the loop, and both of its endpoints are already on the arc.
        closing = tuple(reversed(path))[1:-1]
        loops = [(_arc_loop_points(r_out, start, end) + closing, True)]
        prims = [Arc((0.0, 0.0), r_out, start, end, "visible")]
        prims.extend(
            Line(path[index], path[index + 1], "visible") for index in range(len(path) - 1)
        )
    if r_in > _EPS:
        loops.append((_circle_loop(r_in, reverse=True), True))
        prims.append(Circle((0.0, 0.0), r_in, "visible"))

    return {
        "r_out": r_out,
        "r_in": r_in,
        "loops": tuple(loops),
        "prims": tuple(prims),
        "omitted": tuple(omitted),
    }


def cut_loops(part, plane: dict, *, style: str = "full"):
    """The closed cut-face loops, each flagged with whether it is hatched."""
    plane = normalise_plane(plane)
    name = str(style or "full").strip().lower()
    if name not in SECTION_STYLES:
        raise ValueError(f"style: expected one of {SECTION_STYLES}, got {style!r}.")

    if isinstance(part, RevolvedPart):
        bands = radial_profile(part)
        if _is_longitudinal(plane):
            if name == "offset":
                raise ValueError(
                    "section: an offset plane has no meaning along a revolved part's axis; "
                    "use style='full' (or a transverse plane for a cross section)."
                )
            # the loop is the *emitted* outline flattened, so a chamfer or a
            # fillet is cut out of the hatched area exactly where it is drawn
            outline, _used = _emit_path(_outer_path(bands), _corner_edits(part), "visible")
            upper = _flatten_path(outline) + tuple(reversed(_inner_path(bands)))
            loops = [(upper, True)]
            if name != "half":
                loops.append((tuple((x, -y) for x, y in upper), True))
            return tuple(loops)
        if name == "half":
            raise ValueError(
                "section: a half section is defined on a longitudinal plane through the axis; "
                "a transverse plane takes style='full' or 'revolved'."
            )
        return _transverse_face(part, plane["p1"][0])["loops"]

    if name in ("half", "revolved"):
        raise ValueError(
            f"section: a {name} section is defined on a revolved part; a prismatic part takes "
            "style='full' or 'offset'."
        )
    rectangles = _prismatic_cut(part, plane, name)
    if not rectangles:
        raise ValueError(
            f"section: the plane {plane['p1']} -> {plane['p2']} does not cut {part.name!r}."
        )
    return tuple(
        (((u0, v0), (u1, v0), (u1, v1), (u0, v1)), hatched)
        for u0, u1, v0, v1, hatched in rectangles
    )


def section_view(
    part, plane: dict, *, style: str = "full", scale: float = 1.0, side: str = "right"
) -> View:
    """A cut view: the cut faces as hatch areas, plus the geometry behind them."""
    from engineering.mech.standards.materials import hatch_for

    plane = normalise_plane(plane)
    name = str(style or plane["style"]).strip().lower()
    hatch_for(part.material)  # refuse an unknown material before anything is built
    ctx = {"side": side, "scale": float(scale), "projection": "first", "plane": plane}

    loops = cut_loops(part, plane, style=name)
    prims: list[Prim] = []
    omitted: list[dict] = []
    dims: list[DimIntent] = []

    if isinstance(part, RevolvedPart) and _is_longitudinal(plane):
        body, unplaced = section_profile_prims(part, half=name == "half")
        prims.extend(body)
        prims.extend(_axis_prims(part))
        omitted.extend(unplaced)
        for feature in part.features:
            own = feature.cut_prims(part, plane, ctx)
            if own and getattr(feature, "axisymmetric", False):
                own = tuple(own) + mirror_about_x(own)
            prims.extend(own)
            dims.extend(feature.dims(part, "front", ctx))
    elif isinstance(part, RevolvedPart):
        role = "phantom" if name == "revolved" else "visible"
        face = _transverse_face(part, plane["p1"][0])
        prims.extend(prim._replace(role=role) for prim in face["prims"])
        omitted.extend(face["omitted"])
        over = face["r_out"] * (1.0 + AXIS_OVERRUN)
        prims.append(Line((-over, 0.0), (over, 0.0), "center"))
        prims.append(Line((0.0, -over), (0.0, over), "center"))
    else:
        for points, _hatched in loops:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            prims.extend(_rectangle(min(xs), max(xs), min(ys), max(ys)))

    hatched = [points for points, flag in loops if flag]
    if isinstance(part, RevolvedPart) and not _is_longitudinal(plane):
        # a transverse cut face is one region with the bore as its island
        if hatched:
            prims.append(HatchArea(tuple(hatched), part.material))
    else:
        for points in hatched:
            prims.append(HatchArea((points,), part.material))

    label = f"{plane['label']}-{plane['label']}"
    return View(
        kind="section",
        prims=tuple(prims),
        dims=tuple(dims),
        omitted=tuple(omitted),
        bbox=bbox(prims) if prims else (0.0, 0.0, 0.0, 0.0),
        label=label,
        scale=float(scale),
    )


# -- detail views ------------------------------------------------------------


def _prim_bbox(prim) -> tuple[float, float, float, float]:
    return bbox([prim])


def _touches(prim, center: Pt, radius: float) -> bool:
    x0, y0, x1, y1 = _prim_bbox(prim)
    nearest_x = min(max(center[0], x0), x1)
    nearest_y = min(max(center[1], y0), y1)
    return math.dist((nearest_x, nearest_y), center) <= radius + 1e-9


def _clip_segment(p1: Pt, p2: Pt, center: Pt, radius: float):
    """The part of the segment inside the circle, or None."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    fx, fy = p1[0] - center[0], p1[1] - center[1]
    a = dx * dx + dy * dy
    if a < 1e-18:
        return (p1, p2) if math.dist(p1, center) <= radius + 1e-9 else None
    b = 2.0 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - radius * radius
    discriminant = b * b - 4.0 * a * c
    if discriminant <= 0.0:
        return None
    root = math.sqrt(discriminant)
    t0 = max(0.0, (-b - root) / (2.0 * a))
    t1 = min(1.0, (-b + root) / (2.0 * a))
    if t1 - t0 <= 1e-9:
        return None
    return ((p1[0] + t0 * dx, p1[1] + t0 * dy), (p1[0] + t1 * dx, p1[1] + t1 * dy))


def _clip_to_circle(prim, center: Pt, radius: float) -> tuple[Prim, ...]:
    """Straight geometry is trimmed to the detail circle; curved geometry is kept
    whole when it touches it.

    Trimming an arc would mean solving its angular range against the circle, and
    a detail that silently re-cuts an arc is worse than one that carries a
    slightly long arc into the enlargement - so the rule is stated rather than
    approximated, and a detail is meant to be drawn inside its circle.
    """
    if isinstance(prim, Line):
        clipped = _clip_segment(prim.p1, prim.p2, center, radius)
        return (Line(clipped[0], clipped[1], prim.role),) if clipped else ()
    if isinstance(prim, Poly):
        points = list(prim.points)
        if prim.closed and points:
            points.append(points[0])
        out: list[Prim] = []
        for index in range(len(points) - 1):
            clipped = _clip_segment(points[index], points[index + 1], center, radius)
            if clipped:
                out.append(Line(clipped[0], clipped[1], prim.role))
        return tuple(out)
    return (prim,) if _touches(prim, center, radius) else ()


def detail_marker_prims(detail: dict) -> tuple[Prim, ...]:
    """ISO 128-34 detail circle and its letter, for the **parent** view."""
    center = tuple(float(v) for v in detail["center"])
    radius = float(detail["radius"])
    label = str(detail.get("label", "A"))
    return (
        Circle(center, radius, "visible"),
        Text((center[0] + radius + 2.0, center[1] + radius + 2.0), label, 5.0, 0.0, "text"),
    )


def detail_view(part, detail: dict, *, projection: str = "first", side: str = "right") -> View:
    """A circular region of a parent view, enlarged, with the ISO 128-34 labels.

    Straight geometry is trimmed to the circle and curved geometry that touches
    it is kept whole (see :func:`_clip_to_circle`); every feature whose own
    primitives all fall outside is reported in ``omitted``.
    """
    if not isinstance(detail, dict):
        raise ValueError(f"detail: expected a dict, got {type(detail).__name__}.")
    if "radius" not in detail:
        raise ValueError("detail.radius: a detail needs the radius of its circle.")
    center = tuple(float(v) for v in detail.get("center", (0.0, 0.0)))
    radius = float(detail["radius"])
    if radius <= _EPS:
        raise ValueError(f"detail.radius: must be greater than zero, got {radius}.")
    factor = float(detail.get("scale", 5.0))
    if factor <= _EPS:
        raise ValueError(f"detail.scale: must be greater than zero, got {factor}.")
    label = str(detail.get("label", "A"))
    of = str(detail.get("of", "front")).strip().lower()

    parent = build_view(part, of, side=side, projection=projection)
    kept: list[Prim] = []
    for prim in parent.prims:
        kept.extend(_clip_to_circle(prim, center, radius))
    kept = tuple(kept)
    if not kept:
        raise ValueError(
            f"detail: the circle at {center} r={radius} contains no geometry of the {of} view."
        )

    moved = translate(kept, -center[0], -center[1])
    enlarged = scale_prims(moved, factor, (0.0, 0.0))

    omitted = list(parent.omitted)
    for feature in part.features:
        own = feature.prims(part, of, {"side": side, "scale": 1.0, "projection": projection})
        if own and not any(_touches(prim, center, radius) for prim in own):
            omitted.append({"feature": feature.id, "reason": f"outside the detail circle {label}"})

    return View(
        kind="detail",
        prims=tuple(enlarged),
        dims=(),
        omitted=tuple(omitted),
        bbox=bbox(enlarged),
        label=f"{label} ({factor:g}:1)",
        scale=factor,
    )
