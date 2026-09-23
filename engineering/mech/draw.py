"""The only module in `engineering/mech/` that touches a backend.

Everything above it is pure: the part model, the features, the view engine and
the dimension layout take numbers and return primitive *descriptions*. This
module turns those descriptions into `entity_create_*` / `dimension_*` calls,
places them in WCS, and writes the part model back onto the drawing as
`ACADMCP_MECH` XDATA so `mech_view_add` can add a view months later without the
caller re-describing the part.

It is not a contract member and neither are the tools above it - exactly like
`gear_draw_*`, `keyway_draw_*` and `titleblock_apply_iso_a3`, it composes the
generic members, so it is written once and works on both engines by
construction.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import anyio

from engineering.mech.dimension import (
    dimension_intents,
    hole_table_rows,
    intent_text,
    layout_dimensions,
    resolve_tolerance,
)
from engineering.mech.features import HolePattern
from engineering.mech.part import build_part, part_to_dict, validate_part
from engineering.mech.primitives import (
    ROLE_LAYER,
    Arc,
    Circle,
    DimIntent,
    HatchArea,
    Line,
    Poly,
    Text,
    layer_for,
    rotate,
    translate,
)
from engineering.mech.standards.materials import hatch_for
from engineering.mech.views import (
    View,
    build_view,
    detail_marker_prims,
    layout_origin,
    normalise_plane,
)
from engineering.mech.xdata import APP_ID, PAYLOAD_VERSION, decode, to_values

log = logging.getLogger(__name__)

#: The layer the invisible per-part anchor POINT lives on. It carries the
#: ACADMCP_MECH payload and nothing else, so `read_part` can find every part in
#: a drawing with one filtered `entity_list`.
ANCHOR_LAYER = "MECH"

DEFAULT_GAP = 20.0

#: How far a standalone hole pattern's ISO 128-23 centre mark reaches past the
#: hole it marks, in drawing units. DECLARED, not standard: ISO 128-23 fixes
#: that the mark overruns the circle, not by how much.
CENTRE_MARK_OVERRUN = 3.0


# -- the XDATA adapter -------------------------------------------------------
#
# `engineering/mech/xdata.encode` returns DXF (group code, value) rows, which is
# the on-disk shape. `entity_set_xdata` takes plain JSON values and assigns the
# codes itself through backends/xdata_specs.py, so the 1001 APPID row is the
# backend's business and only the 1000 chunks travel. `to_values(payload)` is
# that projection and it lives in `engineering/mech/xdata.py` (Task 1) - every
# writer in this repository calls it rather than re-deriving the split, so a
# change to the chunking is made in one place.


def _xdata_payload(values) -> dict:
    return decode(values)


def _number(value, where: str, *, positive: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected a number, got {value!r}.") from None
    if not math.isfinite(number):
        raise ValueError(f"{where}: must be finite, got {value!r}.")
    if positive and number <= 0.0:
        raise ValueError(f"{where}: must be greater than zero, got {number:g}.")
    return number


# -- primitives to entities --------------------------------------------------


async def draw_prims(
    backend,
    prims,
    *,
    at: tuple[float, float] = (0.0, 0.0),
    rotation: float = 0.0,
    layer_prefix: str = "",
) -> dict:
    """Draw primitive descriptions, turned by ``rotation`` about their origin, then placed at ``at``."""
    placed = tuple(prims)
    _refuse_islands_without_edge_paths(backend, placed)
    await _ensure_layers(backend, sorted({f"{layer_prefix}{layer_for(p)}" for p in placed}))
    if abs(float(rotation)) > 1e-12:
        placed = rotate(placed, float(rotation), (0.0, 0.0))
    if abs(float(at[0])) > 1e-12 or abs(float(at[1])) > 1e-12:
        placed = translate(placed, float(at[0]), float(at[1]))

    handles: list[str] = []
    counts: dict[str, int] = {}

    for prim in placed:
        layer = f"{layer_prefix}{layer_for(prim)}"
        if isinstance(prim, Line):
            info = await backend.entity_create_line(
                prim.p1[0], prim.p1[1], prim.p2[0], prim.p2[1], layer=layer
            )
        elif isinstance(prim, Arc):
            info = await backend.entity_create_arc(
                prim.center[0],
                prim.center[1],
                prim.radius,
                prim.start_deg,
                prim.end_deg,
                layer=layer,
            )
        elif isinstance(prim, Circle):
            info = await backend.entity_create_circle(
                prim.center[0], prim.center[1], prim.radius, layer=layer
            )
        elif isinstance(prim, Poly):
            info = await backend.entity_create_polyline(
                [[x, y] for x, y in prim.points], prim.closed, layer=layer
            )
        elif isinstance(prim, Text):
            info = await backend.entity_create_text(
                prim.text,
                prim.at[0],
                prim.at[1],
                prim.height,
                prim.rotation,
                layer=layer,
            )
        elif isinstance(prim, HatchArea):
            style = hatch_for(prim.material)
            info = await backend.entity_create_hatch(
                style["pattern"],
                [[x, y] for x, y in prim.loops[0]],
                style["scale"],
                style["angle"],
                layer=layer,
            )
            for number, island in enumerate(prim.loops[1:], start=1):
                points = list(island)
                edges = [
                    {
                        "type": "line",
                        "start": [points[i][0], points[i][1]],
                        "end": [points[(i + 1) % len(points)][0], points[(i + 1) % len(points)][1]],
                    }
                    for i in range(len(points))
                ]
                # `hatch_add_boundary` answers a bad edge list or a bad handle
                # with {"ok": False} rather than raising. An island that did not
                # attach leaves the hatch solid straight through the bore it was
                # meant to exclude, so the hatch is taken back out and the
                # failure is raised - a missing hatch is honest, a wrong one is not.
                try:
                    added = await backend.hatch_add_boundary(info.handle, edges)
                except Exception:
                    await _discard(backend, info.handle)
                    raise
                if not isinstance(added, dict) or not added.get("ok"):
                    await _discard(backend, info.handle)
                    error = added.get("error") if isinstance(added, dict) else added
                    raise ValueError(
                        f"hatch island {number} of {len(prim.loops) - 1} ({prim.material}) "
                        f"did not attach: {error}; the hatch was removed rather than left "
                        "filling the material it was meant to exclude."
                    )
        else:  # pragma: no cover - the Prim union is closed
            raise ValueError(f"draw_prims: unknown primitive {type(prim).__name__}.")
        handles.append(info.handle)
        key = type(prim).__name__.lower()
        counts[key] = counts.get(key, 0) + 1

    return {"handles": handles, "counts": counts}


async def _ensure_layers(backend, names) -> tuple[str, ...]:
    """Create whichever target layers the drawing lacks, before the first entity.

    MEASURED on AutoCAD 2026 by ``scripts/smoke_mech_com.py``: ``draw_part``
    put its anchor POINT on ``MECH``, a layer no layer set creates, and
    ActiveX answered ``entity.Layer = "MECH"`` with ``-0x7ffdfff7 ('Key not
    found')`` - after every view had already been drawn. The headless engine
    creates a missing layer on assignment, so the same call passed every test.
    Group A measured the same refusal for annotation layers; this is the same
    cure (``ensure_annotation_layers``) at the places every mechanical drawing
    path passes through.
    """
    from engineering.mech.annotate import ensure_annotation_layers

    return await ensure_annotation_layers(backend, [str(name) for name in names if name])


def _refuse_islands_without_edge_paths(backend, prims) -> None:
    """Refuse a hatch with islands on an engine that cannot attach them - before drawing.

    Islands go through `hatch_add_boundary`, which the live engine refuses
    (capability ``hatch_edge_paths``: ActiveX appends loops as objects, not
    typed edges). Finding that out at the hatch would leave the rest of the
    view already drawn; asking the capability map first leaves nothing behind.
    """
    if not any(isinstance(p, HatchArea) and len(p.loops) > 1 for p in prims):
        return
    try:
        feature = backend.capabilities().features.get("hatch_edge_paths")
    except Exception:  # a backend without a capability map: let the member answer
        return
    if feature is not None and not feature.supported:
        from backends.capability import UnsupportedCapabilityError

        raise UnsupportedCapabilityError(
            "hatch_edge_paths",
            "this view hatches a cut face with an island (a bore or a hole through the "
            f"section), and the {getattr(backend, 'name', 'current')} backend cannot attach "
            f"hatch islands ({feature.reason}); nothing was drawn. Draw this view on the "
            "headless backend (AUTOCAD_MCP_BACKEND=ezdxf).",
        )


async def _discard(backend, handle: str) -> None:
    """Delete an entity this module just made; a failure here must not mask the cause."""
    try:
        await backend.entity_delete(handle)
    except Exception as exc:  # pragma: no cover - the original error is the one to raise
        log.warning("draw_prims: could not remove hatch %s after a failed island: %s", handle, exc)


# -- the placement: the sheet frame and WCS ----------------------------------
#
# Every view of a part is laid out in one *sheet frame*: the unrotated frame in
# which the first view's lower-left corner sits at `placement.at`. Rotation is
# then applied to the whole sheet as a rigid body about `at`, so the views stay
# on their projection axes and the anchor POINT stays on the part's corner.
# A view record keeps both boxes: `frame_bbox` is what `add_view` lays the next
# view out against, `bbox` is the WCS extent of what was drawn.


def _turn(point, at, rotation: float) -> tuple[float, float]:
    x, y = float(point[0]), float(point[1])
    if abs(rotation) <= 1e-12:
        return (x, y)
    cos = math.cos(math.radians(rotation))
    sin = math.sin(math.radians(rotation))
    dx, dy = x - float(at[0]), y - float(at[1])
    return (float(at[0]) + dx * cos - dy * sin, float(at[1]) + dx * sin + dy * cos)


def _to_wcs(prims, offset, at, rotation: float) -> tuple:
    """Primitives in a view's own coordinates -> WCS: offset into the frame, turn about ``at``."""
    placed = translate(prims, float(offset[0]), float(offset[1]))
    if abs(rotation) > 1e-12:
        placed = rotate(placed, float(rotation), (float(at[0]), float(at[1])))
    return placed


def _wcs_bbox(frame_bbox, at, rotation: float) -> list[float]:
    x0, y0, x1, y1 = (float(v) for v in frame_bbox)
    corners = [_turn(p, at, rotation) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def _frame_bbox(record: dict) -> list[float]:
    """The sheet-frame box of a record (a payload written before it existed: its WCS box)."""
    return [float(v) for v in (record.get("frame_bbox") or record["bbox"])]


def _full_size(scale, where: str) -> float:
    """Refuse a view scale the geometry would not have.

    The view engine builds every view but a detail at 1:1, so a scale here
    would only be *recorded* - and `mech_part_inspect` would then report a
    scale the drawing does not have. Refused by name until it is honoured.
    """
    value = _number(scale, where)
    if abs(value - 1.0) > 1e-12:
        raise ValueError(
            f"{where}: {value:g} is refused - views are drawn full size (1:1) in this build, "
            "so the scale would be recorded but not drawn. Scale the sheet through a paper-space "
            "viewport (viewport_set_scale); a detail view takes its own scale in detail['scale']."
        )
    return value


# -- dimensions --------------------------------------------------------------


async def _draw_dimension(
    backend, intent, *, linear_angle: float | None = None
) -> tuple[str | None, dict]:
    """One laid-out intent as a real DIMENSION. Returns (handle, the resolved row).

    ``linear_angle`` is the measuring direction of a linear intent when the
    caller knows it better than the points do: a part turned by its placement
    rotation measures along its own axes, which the WCS points alone no longer
    reveal. ``None`` reads it from the points (horizontal or vertical).
    """
    resolved = resolve_tolerance(intent)
    row = {
        "feature": intent.feature,
        "kind": intent.kind,
        "text": intent_text(intent),
        "fit": intent.fit,
        **resolved,
    }
    text_at = intent.text_at or (
        (intent.p1[0] + intent.p2[0]) / 2.0,
        (intent.p1[1] + intent.p2[1]) / 2.0,
    )
    tol = {
        "tol_upper": resolved["tol_upper"],
        "tol_lower": resolved["tol_lower"],
        "tol_mode": resolved["tol_mode"],
        "text_override": resolved["text_override"],
    }

    if intent.kind == "linear":
        if linear_angle is None:
            horizontal = abs(intent.p2[1] - intent.p1[1]) <= abs(intent.p2[0] - intent.p1[0])
            linear_angle = 0.0 if horizontal else 90.0
        info = await backend.dimension_linear(
            intent.p1[0],
            intent.p1[1],
            intent.p2[0],
            intent.p2[1],
            text_at[0],
            text_at[1],
            float(linear_angle),
            ROLE_LAYER["dim"],
            **tol,
        )
    elif intent.kind == "aligned":
        if tol["tol_mode"] != "none" or tol["text_override"]:
            angle = math.degrees(
                math.atan2(intent.p2[1] - intent.p1[1], intent.p2[0] - intent.p1[0])
            )
            # dimension_aligned carries no tolerance arguments, so a toleranced
            # aligned dimension is drawn as a rotated linear one - the same
            # measurement, with the tolerance the caller asked for.
            info = await backend.dimension_linear(
                intent.p1[0],
                intent.p1[1],
                intent.p2[0],
                intent.p2[1],
                text_at[0],
                text_at[1],
                angle,
                ROLE_LAYER["dim"],
                **tol,
            )
        else:
            info = await backend.dimension_aligned(
                intent.p1[0],
                intent.p1[1],
                intent.p2[0],
                intent.p2[1],
                text_at[0],
                text_at[1],
                ROLE_LAYER["dim"],
            )
    elif intent.kind == "diameter":
        info = await backend.dimension_diameter(
            intent.p1[0],
            intent.p1[1],
            intent.p2[0],
            intent.p2[1],
            10.0,
            ROLE_LAYER["dim"],
            **tol,
        )
    elif intent.kind == "radius":
        info = await backend.dimension_radius(
            intent.p1[0],
            intent.p1[1],
            intent.p2[0],
            intent.p2[1],
            10.0,
            ROLE_LAYER["dim"],
            **tol,
        )
    else:
        return None, {
            **row,
            "skipped": "an angular intent needs a vertex, which a DimIntent does not carry; "
            "no feature in this release emits one",
        }
    return info.handle, row


# -- the part payload --------------------------------------------------------


def _view_record(
    view: View,
    offset: tuple[float, float],
    kw: dict,
    at: tuple[float, float] = (0.0, 0.0),
    rotation: float = 0.0,
) -> dict:
    x0, y0, x1, y1 = view.bbox
    frame = [x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]]
    return {
        "kind": view.kind,
        "label": view.label,
        "scale": view.scale,
        "offset": [float(offset[0]), float(offset[1])],
        # `bbox` is the WCS extent of what was drawn - the part turned by the
        # placement rotation - and `frame_bbox` the same box in the sheet frame
        # the next view is laid out in. They are equal while rotation is 0.
        "bbox": _wcs_bbox(frame, at, rotation),
        "frame_bbox": frame,
        "dimensioned": False,
        "kw": kw,
    }


def _offset_for(view: View, lower_left: tuple[float, float]) -> tuple[float, float]:
    return (lower_left[0] - view.bbox[0], lower_left[1] - view.bbox[1])


def _placed(view: View, offset) -> View:
    x0, y0, x1, y1 = view.bbox
    return View(
        kind=view.kind,
        prims=view.prims,
        dims=view.dims,
        omitted=view.omitted,
        bbox=(x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]),
        label=view.label,
        scale=view.scale,
    )


def _view_from_record(part, payload: dict, record: dict) -> View:
    """Rebuild the view a record describes, with the keywords it was drawn with."""
    kw = record.get("kw") or {}
    return build_view(
        part,
        record["kind"],
        side=str(kw.get("side", "right")),
        plane=kw.get("plane"),
        style=str(kw.get("style", "full")),
        scale=float(kw.get("scale", 1.0)),
        projection=str((payload.get("placement") or {}).get("projection", "first")),
        detail=kw.get("detail"),
    )


def _placement(payload: dict) -> tuple[tuple[float, float], float]:
    placement = payload.get("placement") or {}
    at = placement.get("at") or [0.0, 0.0]
    return (float(at[0]), float(at[1])), float(placement.get("rotation", 0.0))


async def draw_part(
    backend,
    part,
    *,
    at: tuple[float, float] = (0.0, 0.0),
    rotation: float = 0.0,
    views=("front",),
    projection: str = "first",
    dimension: bool = True,
    style: str = "chain",
    scale: float = 1.0,
) -> dict:
    """Draw a part's views, dimension them, and write the model onto the drawing.

    ``at`` is where the first view's lower-left corner goes, and ``rotation``
    turns the whole sheet of views about that point as one rigid body, so the
    views keep their projection axes and the dimensions turn with them.
    ``scale`` must be 1.0: every view is drawn full size in this build and a
    recorded-but-undrawn scale is refused rather than written into the model.
    """
    validate_part(part)
    try:
        at_x, at_y = at
    except (TypeError, ValueError):
        raise ValueError(f"at: expected [x, y], got {at!r}.") from None
    origin = (_number(at_x, "at[0]", positive=False), _number(at_y, "at[1]", positive=False))
    turn = _number(rotation, "rotation", positive=False)
    scale = _full_size(scale, "scale")
    kinds = [str(kind).strip().lower() for kind in (views or ())]
    if not kinds:
        raise ValueError("views: name at least one view to draw.")

    handles: list[str] = []
    records: list[dict] = []
    omitted: list[dict] = []
    parent: View | None = None
    parent_offset = (0.0, 0.0)

    for index, kind in enumerate(kinds):
        view = build_view(part, kind, projection=projection, scale=scale)
        if index == 0:
            offset = _offset_for(view, origin)
        else:
            lower_left = layout_origin(
                _placed(parent, parent_offset),
                kind,
                projection=projection,
                gap=DEFAULT_GAP,
                child=view,
            )
            offset = _offset_for(view, lower_left)
        drawn = await draw_prims(backend, _to_wcs(view.prims, offset, origin, turn))
        handles.extend(drawn["handles"])
        omitted.extend(view.omitted)
        records.append(_view_record(view, offset, {"scale": scale}, origin, turn))
        if index == 0:
            parent, parent_offset = view, offset

    await _ensure_layers(backend, [ANCHOR_LAYER])
    anchor = await backend.entity_create_point(origin[0], origin[1], layer=ANCHOR_LAYER)
    payload = {
        "v": PAYLOAD_VERSION,
        "kind": "part",
        "id": anchor.handle,
        "view": records[0]["kind"],
        "part": part_to_dict(part),
        "placement": {
            "at": [origin[0], origin[1]],
            "rotation": turn,
            "projection": projection,
        },
        "views": records,
    }
    await backend.entity_set_xdata(anchor.handle, APP_ID, to_values(payload))

    result = {
        "part_id": anchor.handle,
        "views": records,
        "handles": handles,
        "omitted": omitted,
        "backend": backend.name,
    }
    if dimension:
        dimensioned = await dimension_part(backend, anchor.handle, style=style)
        for index in dimensioned["views_dimensioned"]:
            records[index]["dimensioned"] = True
        result["dimensions"] = dimensioned["count"]
        result["handles"].extend(dimensioned["handles"])
    return result


async def read_part(backend, part_id: str | None = None) -> dict:
    """The part model written onto the drawing, read back out of its anchor."""
    if part_id:
        raw = await backend.entity_get_xdata(part_id, APP_ID)
        values = (raw.get("xdata") or {}).get(APP_ID) or []
        if not values:
            raise ValueError(f"{part_id}: carries no {APP_ID} part payload.")
        payload = _xdata_payload(values)
        return {"part_id": part_id, **payload}

    anchors = await backend.entity_list(type_filter="POINT", layer_filter=ANCHOR_LAYER, limit=1000)
    found: list[tuple[str, dict]] = []
    for anchor in anchors:
        raw = await backend.entity_get_xdata(anchor.handle, APP_ID)
        values = (raw.get("xdata") or {}).get(APP_ID) or []
        if values:
            found.append((anchor.handle, _xdata_payload(values)))
    if not found:
        raise ValueError(
            f"this drawing carries no {APP_ID} part payload; draw one with mech_part_draw."
        )
    if len(found) > 1:
        raise ValueError(
            "this drawing carries "
            f"{len(found)} parts ({', '.join(handle for handle, _ in found)}); "
            "name one with part_id."
        )
    handle, payload = found[0]
    return {"part_id": handle, **payload}


def _overlaps(box, other, gap: float) -> bool:
    """Do two frame rectangles come closer than ``gap`` on *both* axes?"""
    eps = 1e-6
    return (
        box[0] < other[2] + gap - eps
        and box[2] > other[0] - gap + eps
        and box[1] < other[3] + gap - eps
        and box[3] > other[1] - gap + eps
    )


def _clear_of(records, lower_left, width: float, height: float, parent_box, gap: float):
    """Step a new view past every view already placed, away from its parent.

    `layout_origin` put the view on its projection axis (left, right, above or
    below the parent); a collision is resolved by stepping further along that
    same axis, so a third-angle end view is never shoved back into the front
    view it was placed away from, and a view that only shares an X range with
    another (a top view under the front) is not a collision at all.
    """
    cx = lower_left[0] + width / 2.0
    cy = lower_left[1] + height / 2.0
    dx = cx - (parent_box[0] + parent_box[2]) / 2.0
    dy = cy - (parent_box[1] + parent_box[3]) / 2.0
    boxes = [_frame_bbox(record) for record in records]
    for _ in range(len(boxes) + 1):
        box = (lower_left[0], lower_left[1], lower_left[0] + width, lower_left[1] + height)
        hit = next((other for other in boxes if _overlaps(box, other, gap)), None)
        if hit is None:
            return lower_left
        if abs(dx) >= abs(dy):
            x = hit[2] + gap if dx > 0 else hit[0] - gap - width
            lower_left = (x, lower_left[1])
        else:
            y = hit[3] + gap if dy > 0 else hit[1] - gap - height
            lower_left = (lower_left[0], y)
    return lower_left  # pragma: no cover - each step clears one box for good


async def add_view(backend, part_id: str, kind: str, **kw) -> dict:
    """Add another view of a part already on the drawing, laid out against its first."""
    payload = await read_part(backend, part_id)
    part = build_part(payload["part"])
    records = list(payload.get("views") or [])
    if not records:
        raise ValueError(f"{part_id}: the payload carries no view to lay this one out against.")
    projection = str((payload.get("placement") or {}).get("projection", "first"))
    origin, rotation = _placement(payload)

    plane = kw.pop("plane", None)
    if plane is not None:
        plane = normalise_plane(plane)
    detail = kw.pop("detail", None)
    gap = float(kw.pop("gap", DEFAULT_GAP))
    side = str(kw.pop("side", "right"))
    style = str(kw.pop("style", "full"))
    scale = _full_size(kw.pop("scale", 1.0), "scale")
    if kw:
        raise ValueError(f"add_view: unknown argument(s) {sorted(kw)}.")

    view = build_view(
        part,
        kind,
        side=side,
        plane=plane,
        style=style,
        scale=scale,
        projection=projection,
        detail=detail,
    )

    parent_record = records[0]
    parent_view = _view_from_record(part, payload, parent_record)
    parent_frame = _placed(parent_view, parent_record["offset"])
    lower_left = layout_origin(
        parent_frame,
        kind,
        side=side,
        projection=projection,
        gap=gap,
        child=view,
    )
    lower_left = _clear_of(
        records,
        lower_left,
        view.bbox[2] - view.bbox[0],
        view.bbox[3] - view.bbox[1],
        parent_frame.bbox,
        gap,
    )
    offset = _offset_for(view, lower_left)

    drawn = await draw_prims(backend, _to_wcs(view.prims, offset, origin, rotation))
    marker_handles: list[str] = []
    if kind == "detail":
        marker = detail_marker_prims(detail or {})
        marker_drawn = await draw_prims(
            backend, _to_wcs(marker, parent_record["offset"], origin, rotation)
        )
        marker_handles = marker_drawn["handles"]

    records.append(
        _view_record(
            view,
            offset,
            {
                "side": side,
                "style": style,
                "scale": scale,
                "plane": _plane_to_json(plane),
                "detail": detail,
            },
            origin,
            rotation,
        )
    )
    payload["views"] = records
    await backend.entity_set_xdata(
        part_id, APP_ID, to_values({k: v for k, v in payload.items() if k != "part_id"})
    )
    return {
        "part_id": part_id,
        "view": records[-1],
        "handles": drawn["handles"] + marker_handles,
        "omitted": list(view.omitted),
        "backend": backend.name,
    }


def _plane_to_json(plane: dict | None) -> dict | None:
    """`normalise_plane` answers with tuples; JSON round-trips them as lists."""
    if plane is None:
        return None
    return {
        "p1": [float(plane["p1"][0]), float(plane["p1"][1])],
        "p2": [float(plane["p2"][0]), float(plane["p2"][1])],
        "label": str(plane["label"]),
        "direction": [float(plane["direction"][0]), float(plane["direction"][1])],
        "style": str(plane["style"]),
        "via": [[float(p[0]), float(p[1])] for p in plane.get("via", ())],
    }


def _fits_for_view(part, view, fits: dict, *, style: str, threshold: int) -> dict:
    """The subset of ``fits`` this view actually dimensions.

    ``dimension_intents`` refuses a key that names nothing *in the view it is
    given*, which is right for one view and wrong for a part drawn in three:
    a fit on ``segment[0]`` names nothing in the end view, and refusing there
    would make a correct sheet undrawable. So the split happens here and the
    keys nothing anywhere claims are re-offered to :func:`dimension_intents`
    afterwards, which refuses them in its own words.
    """
    if not fits:
        return {}
    plain = dimension_intents(part, view, style=style, hole_table_threshold=threshold)
    available = {intent.feature for intent in plain if intent.feature}
    return {key: code for key, code in fits.items() if str(key) in available}


def _view_name(index: int, record: dict) -> str:
    return f"{index} ({record.get('label') or record.get('kind')})"


def _dimension_targets(part_id: str, records: list[dict], views) -> list[int]:
    """Which views this call dimensions - never one that already carries its dimensions.

    ISO 129-1: each dimension appears once. A view record carries a
    ``dimensioned`` flag, so a second call (or `mech_dimension_part` after a
    `mech_part_draw` that already dimensioned) adds only the views that have
    none, and naming a dimensioned view is refused rather than stacked.
    """
    if views is None:
        targets = [i for i, record in enumerate(records) if not record.get("dimensioned")]
        if not targets:
            names = ", ".join(_view_name(i, r) for i, r in enumerate(records))
            raise ValueError(
                f"{part_id}: every view is already dimensioned ({names}); each measurement "
                "appears once (ISO 129-1). Add a view with mech_view_add, then dimension it."
            )
        return targets
    if isinstance(views, (str, bytes)) or not isinstance(views, (list, tuple)) or not views:
        raise ValueError(f"views: expected a list of view indices such as [1], got {views!r}.")
    targets: list[int] = []
    for n, value in enumerate(views):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"views[{n}]: expected a view index (int), got {value!r}.")
        if not 0 <= value < len(records):
            names = ", ".join(_view_name(i, r) for i, r in enumerate(records))
            raise ValueError(f"views[{n}]: {value} is not a view of {part_id}; it has {names}.")
        if value not in targets:
            targets.append(value)
    already = [_view_name(i, records[i]) for i in targets if records[i].get("dimensioned")]
    if already:
        raise ValueError(
            f"views: {', '.join(already)} already dimensioned; each measurement appears once "
            "(ISO 129-1), so dimensioning it again would stack a duplicate on every measurement."
        )
    return targets


async def dimension_part(
    backend,
    part_id: str,
    *,
    style: str = "chain",
    hole_table_threshold: int = 8,
    fits: dict | None = None,
    views: list[int] | None = None,
) -> dict:
    """Dimension the views of a part that carry no dimensions yet, laid out without overlaps.

    ``views`` names view indices (the order `read_part` lists them in); by
    default every view not yet dimensioned is. A view already dimensioned is
    never dimensioned again (ISO 129-1), and the flag that says so is written
    back into the part payload.
    """
    payload = await read_part(backend, part_id)
    part = build_part(payload["part"])
    records = list(payload.get("views") or [])
    if not records:
        raise ValueError(f"{part_id}: the payload carries no view to dimension.")
    if fits is not None and not isinstance(fits, dict):
        raise ValueError(
            f"fits: expected a dict of {{key: ISO 286 code}}, got {type(fits).__name__}."
        )
    fits = dict(fits or {})
    targets = _dimension_targets(part_id, records, views)
    origin, rotation = _placement(payload)
    threshold = int(hole_table_threshold)

    # Everything that can be refused is refused before the first DIMENSION is
    # drawn: a fit no target view claims must not leave half a view dimensioned
    # and unflagged behind it.
    planned: list[tuple[int, View, list]] = []
    unclaimed = set(fits)
    for index in targets:
        view = _view_from_record(part, payload, records[index])
        mine = _fits_for_view(part, view, fits, style=style, threshold=threshold)
        unclaimed -= set(mine)
        intents = dimension_intents(
            part, view, style=style, hole_table_threshold=threshold, fits=mine or None
        )
        planned.append((index, view, layout_dimensions(view, intents)))
    if unclaimed:
        # No view claimed these keys. Hand them back to the module that owns the
        # vocabulary so the refusal names the keys that do exist, and the hole
        # table's own message survives.
        dimension_intents(
            part,
            planned[0][1],
            style=style,
            hole_table_threshold=threshold,
            fits={key: fits[key] for key in sorted(unclaimed)},
        )

    handles: list[str] = []
    resolved_rows: list[dict] = []
    skipped: list[dict] = []
    table_handle: str | None = None

    await _ensure_layers(backend, [ROLE_LAYER["dim"]])
    for index, view, placed in planned:
        record = records[index]
        offset = record["offset"]
        for intent in placed:
            frame = intent._replace(
                p1=(intent.p1[0] + offset[0], intent.p1[1] + offset[1]),
                p2=(intent.p2[0] + offset[0], intent.p2[1] + offset[1]),
                text_at=(intent.text_at[0] + offset[0], intent.text_at[1] + offset[1]),
            )
            angle = None
            if intent.kind == "linear":
                # the measuring direction is decided in the part's own frame and
                # turned with it; the rotated points alone no longer show it
                horizontal = abs(frame.p2[1] - frame.p1[1]) <= abs(frame.p2[0] - frame.p1[0])
                angle = (0.0 if horizontal else 90.0) + rotation
            moved = frame._replace(
                p1=_turn(frame.p1, origin, rotation),
                p2=_turn(frame.p2, origin, rotation),
                text_at=_turn(frame.text_at, origin, rotation),
            )
            handle, row = await _draw_dimension(backend, moved, linear_angle=angle)
            if handle is None:
                skipped.append(row)
                continue
            handles.append(handle)
            resolved_rows.append(row)

        rows = hole_table_rows(part, view, hole_table_threshold=threshold)
        if rows and table_handle is None and not payload.get("hole_table"):
            info = await backend.entity_create_table(
                record["bbox"][2] + DEFAULT_GAP,
                record["bbox"][3],
                [[r["tag"], f"{r['d']:g}", str(r["qty"]), r["note"]] for r in rows],
                ["TAG", "DIA", "QTY", "NOTE"],
                None,
                7.0,
                2.5,
                "HOLE TABLE",
                ROLE_LAYER["text"],
            )
            table_handle = info.handle
            handles.append(info.handle)
        if placed or rows:
            record["dimensioned"] = True
        else:
            # Said, not silently counted as done: the view stays unflagged, so a
            # later call can dimension it once the dimension layer covers it.
            skipped.append(
                {
                    "view": _view_name(index, record),
                    "kind": view.kind,
                    "skipped": f"the dimension layer emits no intents for a {view.kind} view "
                    "in this build; the view is left undimensioned",
                }
            )

    payload["views"] = records
    if table_handle:
        payload["hole_table"] = table_handle
    await backend.entity_set_xdata(
        part_id, APP_ID, to_values({k: v for k, v in payload.items() if k != "part_id"})
    )

    return {
        "part_id": part_id,
        "count": len(handles) - (1 if table_handle else 0),
        "handles": handles,
        "resolved": resolved_rows,
        "skipped": skipped,
        "hole_table": table_handle,
        "views_dimensioned": [i for i in targets if records[i].get("dimensioned")],
        "backend": backend.name,
    }


# -- the standalone hole pattern ---------------------------------------------


async def draw_hole_pattern(
    backend,
    *,
    pattern: str,
    x: float,
    y: float,
    diameter: float,
    count: int = 2,
    spacing: float = 0.0,
    count_y: int = 1,
    spacing_y: float = 0.0,
    pcd: float = 0.0,
    start_angle: float = 0.0,
    angle: float = 0.0,
    layer: str | None = None,
    centre_marks: bool = True,
    dimension: bool = False,
) -> dict:
    """Draw a hole pattern on any drawing - no part model needed."""
    # A standalone pattern has no part, so `Hole.validate` (which needs one) is
    # out of reach: the size guard is made here rather than left to the drawing.
    diameter = _number(diameter, "diameter")
    _number(x, "x", positive=False)
    _number(y, "y", positive=False)
    expansion = HolePattern(
        id="pattern",
        pattern=pattern,
        base={"x": float(x), "y": float(y), "diameter": diameter},
        count=int(count),
        spacing=float(spacing),
        count_y=int(count_y),
        spacing_y=float(spacing_y),
        pcd=float(pcd),
        start_angle=float(start_angle),
        angle=float(angle),
    ).expand()  # validates the pattern before anything is drawn

    prims: list[Any] = []
    for hole in expansion:
        radius = float(hole.diameter) / 2.0
        prims.append(Circle((float(hole.x), float(hole.y)), radius, "visible"))
        if centre_marks:
            reach = radius + CENTRE_MARK_OVERRUN
            prims.append(Line((hole.x - reach, hole.y), (hole.x + reach, hole.y), "center"))
            prims.append(Line((hole.x, hole.y - reach), (hole.x, hole.y + reach), "center"))
    drawn = await draw_prims(backend, prims)
    await _ensure_layers(backend, [layer, ROLE_LAYER["dim"] if dimension else None])
    if layer:
        # the role map owns the layers; an explicit override moves the circles
        # only, so the ISO 128-23 centre marks stay on CENTER.
        for handle, prim in zip(drawn["handles"], prims, strict=True):
            if isinstance(prim, Circle):
                await backend.entity_set_properties(handle, layer=layer)

    result = {
        "count": len(expansion),
        "handles": drawn["handles"],
        "centers": [[float(h.x), float(h.y)] for h in expansion],
        "backend": backend.name,
    }
    if dimension:
        first = expansion[0]
        radius = float(first.diameter) / 2.0
        handle, _row = await _draw_dimension(
            backend,
            DimIntent(
                kind="diameter",
                p1=(first.x - radius, first.y),
                p2=(first.x + radius, first.y),
                text_at=(first.x + radius + 12.0, first.y + 12.0),
                text_override=f"{len(expansion)}x ⌀{float(first.diameter):g}",
                feature="pattern",
            ),
        )
        if handle:
            result["handles"].append(handle)
    return result


# -- the whole sheet ---------------------------------------------------------


async def draw_from_spec(backend, spec: dict) -> dict:
    """A whole mechanical sheet from one declarative spec, inside one transaction.

    Every part is validated before the transaction opens, so a refusal leaves
    the drawing untouched. The rollback is shielded for the reason
    `engineering/pid/spec.py` documents at length: the MCP SDK cancels a request
    by cancelling an anyio scope, anyio cancellation is level-triggered, and a
    bare ``await`` in the handler would itself be cancelled before the rollback
    body ran - leaving half a sheet inside an open transaction.
    """
    if not isinstance(spec, dict):
        raise ValueError(f"spec: expected a dict, got {type(spec).__name__}.")
    raw_parts = spec.get("parts")
    if not isinstance(raw_parts, (list, tuple)) or not raw_parts:
        raise ValueError("parts: name at least one part to draw.")

    jobs: list[dict] = []
    for index, item in enumerate(raw_parts):
        if not isinstance(item, dict):
            raise ValueError(f"parts[{index}]: expected a dict with a 'part' key.")
        try:
            part = build_part(item.get("part") or {})
        except ValueError as exc:
            raise ValueError(f"parts[{index}]: {exc}") from exc
        at = item.get("at")
        if at is None:
            at = [0.0, 0.0]
        if isinstance(at, (str, bytes)) or not isinstance(at, (list, tuple)) or len(at) != 2:
            raise ValueError(f"parts[{index}]: at: expected [x, y], got {at!r}.")
        jobs.append(
            {
                "part": part,
                "at": (
                    _number(at[0], f"parts[{index}]: at[0]", positive=False),
                    _number(at[1], f"parts[{index}]: at[1]", positive=False),
                ),
                "rotation": _number(
                    item.get("rotation", 0.0), f"parts[{index}]: rotation", positive=False
                ),
                "views": tuple(item.get("views") or ("front",)),
                "projection": str(item.get("projection", "first")),
                "dimension": bool(item.get("dimension", True)),
                "style": str(item.get("style", "chain")),
            }
        )

    sheet = spec.get("sheet") or {}
    if backend.get_plan_spec() is None:
        await backend.drawing_plan(
            sheet.get("intent", "mechanical part sheet"),
            sheet.get("sheet_size", "A3"),
            float(sheet.get("scale", 1.0)),
            sheet.get("layer_set", "mech"),
        )

    begun = await backend.transaction_begin()
    if not isinstance(begun, dict) or not begun.get("ok"):
        # a refusal, not a crash: ValueError is what the tool layer turns into
        # a ToolError, and nothing has been drawn yet
        detail = begun.get("error") if isinstance(begun, dict) else begun
        raise ValueError(
            f"mech_part_from_spec: could not open a transaction ({detail}); nothing was drawn. "
            "If one is already open, close it with transaction_commit or "
            "transaction_rollback and retry."
        )

    drawn: list[dict] = []
    try:
        for index, job in enumerate(jobs):
            try:
                drawn.append(
                    await draw_part(
                        backend,
                        job["part"],
                        at=job["at"],
                        rotation=job["rotation"],
                        views=job["views"],
                        projection=job["projection"],
                        dimension=job["dimension"],
                        style=job["style"],
                    )
                )
            except ValueError as exc:
                raise ValueError(f"parts[{index}]: {exc}") from exc
    except BaseException as exc:
        cancelled = isinstance(exc, anyio.get_cancelled_exc_class())
        with anyio.CancelScope(shield=True):
            try:
                await backend.transaction_rollback()
            except Exception as rollback_exc:
                if not cancelled:
                    raise
                log.error(
                    "mech_part_from_spec: rollback after a cancelled request failed (%s: %s); "
                    "the sheet may be half-drawn - check system_status and "
                    "transaction_rollback before retrying",
                    type(rollback_exc).__name__,
                    rollback_exc,
                )
        raise
    await backend.transaction_commit()

    return {
        "parts": drawn,
        "part_ids": [item["part_id"] for item in drawn],
        "backend": backend.name,
    }
