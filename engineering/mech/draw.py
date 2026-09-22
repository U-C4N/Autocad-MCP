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
    """Draw primitive descriptions, placed at ``at`` and turned by ``rotation``."""
    placed = tuple(prims)
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
            for island in prim.loops[1:]:
                points = list(island)
                edges = [
                    {
                        "type": "line",
                        "start": [points[i][0], points[i][1]],
                        "end": [points[(i + 1) % len(points)][0], points[(i + 1) % len(points)][1]],
                    }
                    for i in range(len(points))
                ]
                await backend.hatch_add_boundary(info.handle, edges)
        else:  # pragma: no cover - the Prim union is closed
            raise ValueError(f"draw_prims: unknown primitive {type(prim).__name__}.")
        handles.append(info.handle)
        key = type(prim).__name__.lower()
        counts[key] = counts.get(key, 0) + 1

    return {"handles": handles, "counts": counts}


# -- dimensions --------------------------------------------------------------


async def _draw_dimension(backend, intent) -> tuple[str | None, dict]:
    """One laid-out intent as a real DIMENSION. Returns (handle, the resolved row)."""
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
        horizontal = abs(intent.p2[1] - intent.p1[1]) <= abs(intent.p2[0] - intent.p1[0])
        info = await backend.dimension_linear(
            intent.p1[0],
            intent.p1[1],
            intent.p2[0],
            intent.p2[1],
            text_at[0],
            text_at[1],
            0.0 if horizontal else 90.0,
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


def _view_record(view: View, offset: tuple[float, float], kw: dict) -> dict:
    x0, y0, x1, y1 = view.bbox
    return {
        "kind": view.kind,
        "label": view.label,
        "scale": view.scale,
        "offset": [float(offset[0]), float(offset[1])],
        "bbox": [x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]],
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
    """Draw a part's views, dimension them, and write the model onto the drawing."""
    validate_part(part)
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
            offset = _offset_for(view, (float(at[0]), float(at[1])))
        else:
            lower_left = layout_origin(
                _placed(parent, parent_offset),
                kind,
                projection=projection,
                gap=DEFAULT_GAP,
                child=view,
            )
            offset = _offset_for(view, lower_left)
        drawn = await draw_prims(backend, view.prims, at=offset, rotation=rotation)
        handles.extend(drawn["handles"])
        omitted.extend(view.omitted)
        records.append(_view_record(view, offset, {"scale": scale}))
        if index == 0:
            parent, parent_offset = view, offset

    anchor = await backend.entity_create_point(float(at[0]), float(at[1]), layer=ANCHOR_LAYER)
    payload = {
        "v": PAYLOAD_VERSION,
        "kind": "part",
        "id": anchor.handle,
        "view": records[0]["kind"],
        "part": part_to_dict(part),
        "placement": {
            "at": [float(at[0]), float(at[1])],
            "rotation": float(rotation),
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


async def add_view(backend, part_id: str, kind: str, **kw) -> dict:
    """Add another view of a part already on the drawing, laid out against its first."""
    payload = await read_part(backend, part_id)
    part = build_part(payload["part"])
    records = list(payload.get("views") or [])
    if not records:
        raise ValueError(f"{part_id}: the payload carries no view to lay this one out against.")
    placement = payload.get("placement") or {}
    projection = str(placement.get("projection", "first"))
    rotation = float(placement.get("rotation", 0.0))

    plane = kw.pop("plane", None)
    if plane is not None:
        plane = normalise_plane(plane)
    detail = kw.pop("detail", None)
    gap = float(kw.pop("gap", DEFAULT_GAP))
    side = str(kw.pop("side", "right"))
    style = str(kw.pop("style", "full"))
    scale = float(kw.pop("scale", 1.0))
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
    lower_left = layout_origin(
        _placed(parent_view, parent_record["offset"]),
        kind,
        side=side,
        projection=projection,
        gap=gap,
        child=view,
    )
    # step past every view already placed, so a third view never lands on the second
    width = view.bbox[2] - view.bbox[0]
    for record in records[1:]:
        x0, y0, x1, y1 = record["bbox"]
        del y0, y1
        if lower_left[0] < x1 + gap and lower_left[0] + width > x0 - gap:
            lower_left = (x0 - gap - width, lower_left[1])
    offset = _offset_for(view, lower_left)

    drawn = await draw_prims(backend, view.prims, at=offset, rotation=rotation)
    marker_handles: list[str] = []
    if kind == "detail":
        marker = detail_marker_prims(detail or {})
        marker_drawn = await draw_prims(
            backend, marker, at=parent_record["offset"], rotation=rotation
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


async def dimension_part(
    backend,
    part_id: str,
    *,
    style: str = "chain",
    hole_table_threshold: int = 8,
    fits: dict | None = None,
) -> dict:
    """Dimension every view of a part, laid out so no two texts overlap."""
    payload = await read_part(backend, part_id)
    part = build_part(payload["part"])
    records = payload.get("views") or []
    if fits is not None and not isinstance(fits, dict):
        raise ValueError(
            f"fits: expected a dict of {{key: ISO 286 code}}, got {type(fits).__name__}."
        )
    fits = dict(fits or {})
    unclaimed = set(fits)

    handles: list[str] = []
    resolved_rows: list[dict] = []
    skipped: list[dict] = []
    table_handle: str | None = None

    for record in records:
        view = _view_from_record(part, payload, record)
        mine = _fits_for_view(part, view, fits, style=style, threshold=int(hole_table_threshold))
        unclaimed -= set(mine)
        intents = dimension_intents(
            part, view, style=style, hole_table_threshold=hole_table_threshold, fits=mine or None
        )
        placed = layout_dimensions(view, intents)
        offset = record["offset"]
        for intent in placed:
            moved = intent._replace(
                p1=(intent.p1[0] + offset[0], intent.p1[1] + offset[1]),
                p2=(intent.p2[0] + offset[0], intent.p2[1] + offset[1]),
                text_at=(intent.text_at[0] + offset[0], intent.text_at[1] + offset[1]),
            )
            handle, row = await _draw_dimension(backend, moved)
            if handle is None:
                skipped.append(row)
                continue
            handles.append(handle)
            resolved_rows.append(row)

        rows = hole_table_rows(part, view, hole_table_threshold=hole_table_threshold)
        if rows and table_handle is None:
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

    if unclaimed:
        # No view claimed these keys. Hand them back to the module that owns the
        # vocabulary so the refusal names the keys that do exist, and the hole
        # table's own message survives.
        dimension_intents(
            part,
            _view_from_record(part, payload, records[0]),
            style=style,
            hole_table_threshold=hole_table_threshold,
            fits={key: fits[key] for key in sorted(unclaimed)},
        )

    return {
        "part_id": part_id,
        "count": len(handles) - (1 if table_handle else 0),
        "handles": handles,
        "resolved": resolved_rows,
        "skipped": skipped,
        "hole_table": table_handle,
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
        at = item.get("at") or [0.0, 0.0]
        jobs.append(
            {
                "part": part,
                "at": (float(at[0]), float(at[1])),
                "rotation": float(item.get("rotation", 0.0)),
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
        raise RuntimeError(f"mech_part_from_spec: could not open a transaction: {begun}")

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
