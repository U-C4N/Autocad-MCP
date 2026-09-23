"""Centre marks, the cutting-plane line and the material hatch.

Pure geometry: these functions return primitive descriptions from
`engineering.mech.primitives`; the `draw_*` functions are the only async part
and the only part that touches a backend.

SOURCE - centre marks and centre lines: ISO 128-23, "Technical drawings -
General principles of presentation - Lines on construction drawings", which
draws a centre line as a long-dash-dotted line that overruns the feature's
outline by a short amount. The *dash pattern itself is not written here*: the
prims are plain LINE entities on the CENTER layer, whose CENTER linetype
(bootstrapped by `drawing_new`) supplies the standard's dash proportions. That
is deliberate - transcribing a dash table this module cannot verify would put
an invented pattern next to a real one.

SOURCE - the cutting-plane line: ISO 128-40, "Technical drawings - General
principles of presentation - Basic conventions for cuts and sections": a
long-dash-dotted line that is *wide* at its ends and at every change of
direction, an arrow at each end pointing in the direction of viewing, and the
same capital letter at both ends. Line width is a layer property here, so the
wide ends are emitted on the `visible` role and the thin middle on the
`center` role - the wide/narrow contrast, expressed in the role vocabulary
this repository already has.

The two widths are not numbers this module picks: they are whatever
`engineering.layers.ENGINEERING_LAYERS` gives GEOMETRY and CENTER. MEASURED
on a drawing `drawing_new` bootstrapped:

    GEOMETRY is 0.50 mm, CENTER is 0.18 mm, a ratio of 2.78:1.

That measured pair is what is stated here - not the standard's own
wide:narrow ratio, which this module does not transcribe.
A ratio quoted from memory beside a measured one is exactly the invented
number the SOURCE blocks above refuse to ship, and an earlier revision of
this block did ship one (it said CENTER was 0.25 mm and the pair 2:1; both
were wrong). `tests/test_mech_marks.py` now pins these three figures to
`ENGINEERING_LAYERS`, so the sentence cannot drift from the layer set again.

DECLARED, not transcribed: ISO 128-40 says the ends are thickened and that
arrows are placed there; it does not dimension them. The end-segment length,
the arrow shaft length, the arrowhead size and the label offset below are this
implementation's stated choices, expressed in text heights.

The material hatch is not authored here at all: it comes from
`engineering.mech.standards.materials.hatch_for`, which carries the ISO 128-50
map and its own refusal.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from engineering.mech.primitives import Line, Poly, Prim, Text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backends.base import AutoCADBackend

__all__ = [
    "CENTRE_STYLES",
    "HATCH_FLATTEN_SAGITTA",
    "SECTION_STYLES",
    "centre_mark_prims",
    "draw_centre_marks",
    "draw_material_hatch",
    "draw_section_line",
    "section_line_prims",
    "section_plane",
]

CENTRE_STYLES: tuple[str, ...] = ("mark", "lines")

# The four styles `views.build_view(kind="section", style=...)` accepts; the
# dict this module returns is fed straight to it.
SECTION_STYLES: tuple[str, ...] = ("full", "half", "offset", "revolved")

# DECLARED layout constants for the cutting-plane line, in text heights.
END_SEGMENT_FACTOR = 2.0  # wide segment at each end
ARROW_SHAFT_FACTOR = 2.0  # arrow shaft length, perpendicular to the plane
ARROW_HEAD_FACTOR = 0.8  # arrowhead length (half-width is 0.35 of it, as elsewhere)
LABEL_OFFSET_FACTOR = 0.7  # gap between the arrow tail and the label

# DECLARED, not transcribed: the largest gap (drawing units) left between a
# curved boundary edge and the chords that stand in for it.
# `entity_create_hatch` takes a vertex list on both engines, so an arc edge
# has to reach it as vertices; what must not happen - and did - is dropping
# the curve and hatching the chord. MEASURED on the r=20 semicircular cut
# face in `tests/test_mech_marks.py`: 0.01 spaces that arc into 50 chords and
# hatches 1027.905195 of its real 1028.318531, an error of 0.413 (0.040%),
# against the 628.3 (61%) an earlier revision silently lost. The payload says
# `accuracy: "flatten_tolerance"` rather than claiming the area is exact.
HATCH_FLATTEN_SAGITTA = 0.01


def centre_mark_prims(
    center: tuple[float, float],
    radius: float,
    *,
    style: str = "mark",
    extension: float = 3.0,
) -> tuple[Prim, ...]:
    """A centre mark or a pair of full centre lines for a circular feature.

    `style="mark"` draws the short cross AutoCAD's DIMCEN draws: arms of
    `extension` either side of the centre. `style="lines"` draws the two full
    centre lines, overrunning the circle by `extension` at each end, which is
    ISO 128-23's short overrun of the outline.
    """
    if style not in CENTRE_STYLES:
        raise ValueError(
            f"centre style {style!r} is unknown; use one of {', '.join(CENTRE_STYLES)}."
        )
    ext = float(extension)
    if not math.isfinite(ext) or ext <= 0.0:
        raise ValueError(f"extension must be a positive finite number; got {extension!r}.")
    cx, cy = float(center[0]), float(center[1])
    if style == "mark":
        arm = ext
    else:
        r = float(radius)
        if not math.isfinite(r) or r <= 0.0:
            raise ValueError(
                f"style='lines' needs the feature's radius to know how far to overrun it; "
                f"got radius={radius!r}."
            )
        arm = r + ext
    return (
        Line((cx - arm, cy), (cx + arm, cy), "center"),
        Line((cx, cy - arm), (cx, cy + arm), "center"),
    )


def _unit(vx: float, vy: float, what: str) -> tuple[float, float]:
    length = math.hypot(vx, vy)
    if length <= 1e-12:
        raise ValueError(f"{what} must be a non-zero vector; got ({vx:g}, {vy:g}).")
    return vx / length, vy / length


def section_line_prims(
    p1: tuple[float, float],
    p2: tuple[float, float],
    *,
    label: str = "A",
    direction: tuple[float, float] = (0.0, -1.0),
    height: float = 5.0,
) -> tuple[Prim, ...]:
    """The ISO 128-40 cutting-plane line: wide ends, arrows, the letter at both ends.

    `direction` is the direction of viewing and must not be parallel to the
    plane. The arrowhead's tip sits on the end of the cutting-plane line and
    its shaft extends away from the part, so the head reads as pointing the way
    the section is viewed; the letter goes beyond the tail.
    """
    if not str(label).strip():
        raise ValueError("a cutting-plane line needs a label; ISO 128-40 letters both ends.")
    h = float(height)
    if not math.isfinite(h) or h <= 0.0:
        raise ValueError(f"height must be a positive finite number; got {height!r}.")
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    ux, uy = _unit(x2 - x1, y2 - y1, "the cutting plane (p1 and p2 must be distinct)")
    dx, dy = _unit(float(direction[0]), float(direction[1]), "direction")
    if abs(ux * dy - uy * dx) < 1e-6:
        raise ValueError(
            f"direction ({dx:g}, {dy:g}) is parallel to the cutting plane; the viewing "
            "direction must be perpendicular to it."
        )

    span = math.hypot(x2 - x1, y2 - y1)
    end = END_SEGMENT_FACTOR * h
    prims: list[Prim] = []
    if span <= 2.0 * end + 1e-9:
        prims.append(Line((x1, y1), (x2, y2), "visible"))
    else:
        a = (x1 + ux * end, y1 + uy * end)
        b = (x2 - ux * end, y2 - uy * end)
        prims.append(Line((x1, y1), a, "visible"))
        prims.append(Line(b, (x2, y2), "visible"))
        prims.append(Line(a, b, "center"))

    shaft = ARROW_SHAFT_FACTOR * h
    head = ARROW_HEAD_FACTOR * h
    px, py = -dy, dx
    text = str(label).strip()
    for tip in ((x1, y1), (x2, y2)):
        tail = (tip[0] - dx * shaft, tip[1] - dy * shaft)
        prims.append(Line(tail, tip, "visible"))
        base = (tip[0] - dx * head, tip[1] - dy * head)
        prims.append(
            Poly(
                (
                    tip,
                    (base[0] + px * head * 0.35, base[1] + py * head * 0.35),
                    (base[0] - px * head * 0.35, base[1] - py * head * 0.35),
                ),
                True,
                "visible",
            )
        )
    for tip in ((x1, y1), (x2, y2)):
        offset = shaft + LABEL_OFFSET_FACTOR * h
        prims.append(Text((tip[0] - dx * offset, tip[1] - dy * offset), text, h))
    return tuple(prims)


def section_plane(
    p1: tuple[float, float],
    p2: tuple[float, float],
    *,
    label: str = "A",
    direction: tuple[float, float] = (0.0, -1.0),
    style: str = "full",
) -> dict:
    """The plane definition `mech_view_add(kind="section")` consumes.

    Exactly the five keys the plan pins as SECTION_PLANE, in that order, with
    `direction` normalised to a unit vector. Returning it from the tool that
    draws the cutting-plane line is what makes the label on the plan and the
    label on the section view the same string by construction.
    """
    if style not in SECTION_STYLES:
        raise ValueError(
            f"section style {style!r} is unknown; use one of {', '.join(SECTION_STYLES)}."
        )
    text = str(label).strip()
    if not text:
        raise ValueError("a section plane needs a label; ISO 128-40 letters both ends.")
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    _unit(x2 - x1, y2 - y1, "the cutting plane (p1 and p2 must be distinct)")
    dx, dy = _unit(float(direction[0]), float(direction[1]), "direction")
    return {
        "p1": (x1, y1),
        "p2": (x2, y2),
        "label": text,
        "direction": (dx, dy),
        "style": style,
    }


# ── the async layer ─────────────────────────────────────────────────────────

_CIRCULAR_TYPES = ("CIRCLE", "ARC")


async def draw_centre_marks(
    backend: AutoCADBackend,
    *,
    handles: list[str] | None = None,
    centers: list[list[float]] | None = None,
    style: str = "mark",
    extension: float = 3.0,
    layer: str | None = None,
) -> dict:
    """Centre marks or centre lines for circular features.

    With `handles`, the circles' real centre and radius are read back out of
    the drawing through `entity_get` - the caller is never asked to restate a
    radius it could get wrong. With `centers`, each row is `[x, y]` or
    `[x, y, radius]`; `style="lines"` needs the radius, because the overrun is
    measured from the outline.
    """
    from engineering.mech.annotate import draw_annotation_prims

    if not handles and not centers:
        raise ValueError("centre_marks needs either handles or centers; both are empty.")
    if handles and centers:
        raise ValueError("centre_marks takes handles or centers, not both.")

    sources: list[dict] = []
    if handles:
        for handle in handles:
            info = await backend.entity_get(handle)
            if info.type not in _CIRCULAR_TYPES:
                raise ValueError(
                    f"handle {handle} is a {info.type}; a centre mark needs a circular "
                    f"feature ({', '.join(_CIRCULAR_TYPES)})."
                )
            radius = info.properties.get("radius")
            if radius is None:
                raise ValueError(
                    f"handle {handle} lies in a plane tilted out of WCS XY, so it has no "
                    "radius in this frame (capability 'ocs_tilted_plane'); its centre mark "
                    "cannot be drawn in xy."
                )
            center = info.properties["center"]
            sources.append(
                {
                    "handle": info.handle,
                    "center": [float(center[0]), float(center[1])],
                    "radius": float(radius),
                }
            )
    else:
        for index, row in enumerate(centers or []):
            if len(row) < 2:
                raise ValueError(f"centers[{index}] must be [x, y] or [x, y, radius].")
            radius = float(row[2]) if len(row) > 2 else None
            if style == "lines" and radius is None:
                raise ValueError(
                    f"centers[{index}] has no radius, and style='lines' measures its overrun "
                    "from the outline; pass [x, y, radius] or use style='mark'."
                )
            sources.append(
                {
                    "handle": None,
                    "center": [float(row[0]), float(row[1])],
                    "radius": radius,
                }
            )

    # Validate every source before the first entity is written.
    batches = [
        centre_mark_prims(
            (source["center"][0], source["center"][1]),
            source["radius"] if source["radius"] is not None else 0.0,
            style=style,
            extension=extension,
        )
        for source in sources
    ]
    handles_out: list[str] = []
    for prims in batches:
        drawn = await draw_annotation_prims(backend, prims, layer=layer)
        handles_out.extend(drawn["handles"])
    return {
        "ok": True,
        "handles": handles_out,
        "count": len(handles_out),
        "style": style,
        "sources": sources,
    }


async def draw_section_line(
    backend: AutoCADBackend,
    p1: tuple[float, float],
    p2: tuple[float, float],
    *,
    label: str = "A",
    direction: tuple[float, float] = (0.0, -1.0),
    style: str = "full",
    height: float = 5.0,
    layer: str | None = None,
) -> dict:
    """Draw the cutting-plane line and hand back the plane the view engine consumes."""
    from engineering.mech.annotate import draw_annotation_prims

    plane = section_plane(p1, p2, label=label, direction=direction, style=style)
    prims = section_line_prims(
        plane["p1"],
        plane["p2"],
        label=plane["label"],
        direction=plane["direction"],
        height=height,
    )
    result = await draw_annotation_prims(backend, prims, layer=layer)
    result["plane"] = plane
    result["label"] = plane["label"]
    return result


def _polygon_area(points: list[tuple[float, float]]) -> float:
    """The shoelace area of a closed straight-edged polygon.

    Legitimate here and nowhere else in this module: it is applied to the
    vertex list actually written into the HATCH, every edge of which really is
    a straight line, so it measures what was drawn rather than approximating
    what was meant. The curved input is handled by `_flatten_bulge` *before*
    this sees it, and the residual is reported, never swallowed.
    """
    total = 0.0
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _flatten_bulge(
    p1: tuple[float, float],
    p2: tuple[float, float],
    bulge: float,
    tolerance: float = HATCH_FLATTEN_SAGITTA,
) -> list[tuple[float, float]]:
    """The interior vertices standing in for one bulged polyline segment.

    DXF stores a circular arc between two LWPOLYLINE vertices as a *bulge*:
    tan(theta/4) of the arc's included angle, positive counter-clockwise. The
    points returned lie strictly between `p1` and `p2`, spaced so no chord
    departs from the arc by more than `tolerance`; an empty list means the
    segment is already straight.
    """
    b = float(bulge)
    if not math.isfinite(b) or abs(b) <= 1e-12:
        return []
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord <= 1e-12:
        return []
    theta = 4.0 * math.atan(b)
    half = theta / 2.0
    radius = (chord / 2.0) / abs(math.sin(half))
    # Centre: off the chord's midpoint along its left normal, signed by the
    # sweep, so a negative bulge puts it on the other side without a branch.
    ux, uy = (x2 - x1) / chord, (y2 - y1) / chord
    offset = -(chord / 2.0) / math.tan(half)
    cx = (x1 + x2) / 2.0 + (-uy) * offset
    cy = (y1 + y2) / 2.0 + ux * offset
    tol = min(float(tolerance), radius)
    step = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - tol / radius)))
    count = max(2, math.ceil(abs(theta) / step)) if step > 1e-12 else 2
    start = math.atan2(y1 - cy, x1 - cx)
    return [
        (
            cx + radius * math.cos(start + theta * index / count),
            cy + radius * math.sin(start + theta * index / count),
        )
        for index in range(1, count)
    ]


async def _discard_traced_loop(backend: AutoCADBackend, handle: str | None) -> bool:
    """Delete the outline `boundary_from_entities` drew, reporting whether it went.

    `boundary_from_entities` does not merely compute a loop, it *writes* one:
    an LWPOLYLINE on the current layer. `hatch_material` asked for a hatch, not
    an outline, so leaving it behind drops a stray polyline on layer 0 that the
    caller cannot find - and that `drawing_critique` / `drawing_finalize` then
    score against the drawing under the layer-discipline rule. The boolean is
    put in the result rather than assumed, so a delete that failed is visible.
    """
    if not handle:
        return False
    try:
        result = await backend.entity_delete(handle)
    except Exception:
        return False
    if isinstance(result, dict):
        return bool(result.get("ok", True))
    return True


async def draw_material_hatch(
    backend: AutoCADBackend,
    *,
    boundary: list[list[float]] | None = None,
    handles: list[str] | None = None,
    material: str = "steel",
    scale: float = 1.0,
    angle: float | None = None,
    layer: str | None = None,
) -> dict:
    """Hatch a closed region with the ISO 128-50 pattern for a material.

    `hatch_for` owns the map and its refusal, so an unknown material name is
    rejected here before anything is drawn. `scale` multiplies the material's
    own pattern scale; `angle` replaces the material's own angle when given.

    With `handles` the loop is chained by `boundary_from_entities`, and both of
    that call's side effects are dealt with here rather than left to the
    caller. Its traced LWPOLYLINE carries `points` **and** a parallel `bulges`
    list; reading only `points` hatches the chord of every arc edge, which on a
    semicircular cut face leaves 61% of the face unhatched while reporting
    success. Each bulged segment is therefore expanded into chords no further
    than `HATCH_FLATTEN_SAGITTA` from the real arc, and the payload states its
    own accuracy the way `analysis_measure_entity` does: `accuracy` is
    `"exact"` or `"flatten_tolerance"`, `area` is the polygon actually written
    and `boundary_area` the exact area of the chained loop, so the two can be
    compared instead of trusted. The traced outline itself is deleted - it was
    scaffolding, and an orphan polyline on layer 0 is scored against the
    drawing - and `traced` reports its handle and whether it went.

    Refused: a loop that encloses no area, and a traced loop that flattens to
    fewer than three vertices. Both used to write a HATCH of area 0.0 and
    return `ok`.
    """
    from engineering.mech.annotate import ensure_annotation_layers
    from engineering.mech.primitives import ROLE_LAYER as _ROLE_LAYER
    from engineering.mech.standards.materials import hatch_for

    spec = hatch_for(material)  # refuses an unknown material, naming the list
    factor = float(scale)
    if not math.isfinite(factor) or factor <= 0.0:
        raise ValueError(f"scale must be a positive finite number; got {scale!r}.")

    if boundary is None and not handles:
        raise ValueError("hatch_material needs either boundary points or handles; both are empty.")
    if boundary is not None and handles:
        raise ValueError("hatch_material takes boundary or handles, not both.")

    traced_report: dict | None = None
    boundary_area: float | None = None
    curved_edges = 0
    if boundary is not None:
        points = [(float(p[0]), float(p[1])) for p in boundary]
        if len(points) < 3:
            raise ValueError(f"a hatch boundary needs at least three points; got {len(points)}.")
    else:
        loop = await backend.boundary_from_entities(list(handles or []))
        if not loop.get("ok"):
            raise ValueError(f"hatch_material could not close a loop: {loop.get('error')}")
        traced_handle = loop.get("handle")
        if loop.get("area") is not None:
            boundary_area = float(loop["area"])
        try:
            traced = await backend.entity_get(traced_handle)
            vertices = [(float(p[0]), float(p[1])) for p in traced.properties["points"]]
            bulges = [float(b) for b in (traced.properties.get("bulges") or ())]
        finally:
            # In `finally:` so a drawing is never left with the scaffolding of
            # a call that raised while reading it.
            traced_report = {
                "handle": traced_handle,
                "removed": await _discard_traced_loop(backend, traced_handle),
            }
        points = []
        for index, vertex in enumerate(vertices):
            points.append(vertex)
            bulge = bulges[index] if index < len(bulges) else 0.0
            arc = _flatten_bulge(vertex, vertices[(index + 1) % len(vertices)], bulge)
            if arc:
                curved_edges += 1
                points.extend(arc)
        if len(points) < 3:
            raise ValueError(
                f"the chained loop flattens to {len(points)} vertices, which encloses nothing; "
                "a hatch boundary needs at least three."
            )

    area = _polygon_area(points)
    if area <= 1e-9:
        raise ValueError(
            f"the hatch boundary encloses no area (measured {area:g}); a zero-area HATCH "
            "fills nothing while reporting success."
        )
    # The traced loop knows its own exact area, arcs and all. If the polygon
    # written here does not match it, something was straightened - say so by
    # name instead of reporting an area that is not the cut face's.
    if boundary_area is None or abs(area - boundary_area) <= max(1e-9, 1e-9 * abs(boundary_area)):
        accuracy, flatten_tolerance = "exact", None
    else:
        accuracy, flatten_tolerance = "flatten_tolerance", HATCH_FLATTEN_SAGITTA

    pattern_scale = float(spec["scale"]) * factor
    pattern_angle = float(spec["angle"]) if angle is None else float(angle)
    target = layer or _ROLE_LAYER["hatch"]
    # MEASURED on AutoCAD 2026: `msp.AddHatch(...)` + `Evaluate()` puts the
    # AcDbHatch in model space, and the next statement - `entity.Layer = "HATCH"`
    # inside `_apply_entity_attrs` - raises `('Key not found')` on a drawing whose
    # layer table is `['0']`, leaving that hatch orphaned on layer 0. ActiveX never
    # creates the layer; `EzdxfBackend._apply_attrs` does, so without this the same
    # call succeeds headless and half-writes live. `drawing_open` runs no bootstrap,
    # which is exactly when a cut face has no HATCH layer to land on.
    created = await ensure_annotation_layers(backend, [target])
    info = await backend.entity_create_hatch(
        spec["pattern"],
        [[x, y] for x, y in points],
        pattern_scale,
        pattern_angle,
        target,
    )
    return {
        "ok": True,
        "handle": info.handle,
        "material": material,
        "pattern": spec["pattern"],
        "scale": pattern_scale,
        "angle": pattern_angle,
        "points": len(points),
        "area": round(area, 6),
        "boundary_area": boundary_area,
        "accuracy": accuracy,
        "flatten_tolerance": flatten_tolerance,
        "curved_edges": curved_edges,
        "layer": target,
        "layers_created": list(created),
        "traced": traced_report,
    }
