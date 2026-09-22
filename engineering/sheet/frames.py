"""ISO 5457 drawing sheets: sizes, margins, frame, zone grid, centring and trim marks.

SOURCE
------
Sheet sizes       ISO 216:2007, A series, trimmed sizes in millimetres.
Frame and margins ISO 5457:1999, 4.2 -- the drawing frame that bounds the
                  drawing space leaves a 20 mm filing margin on the binding
                  edge and 10 mm on the other three, for every size A4 to A0.
Centring marks    ISO 5457:1999, 4.3 -- four marks at the middle of each side
                  of the trimmed sheet, running from the trimmed edge to 5 mm
                  inside the frame.
Grid reference    ISO 5457:1999, 4.4 -- the number of divisions along a side is
                  an even number obtained from the length of that side of the
                  frame divided by 50 mm. Columns are numbered from the left,
                  rows are lettered from the top.
Trimming marks    ISO 5457:1999, 4.5 -- one mark in each corner of the trimmed
                  sheet, two overlapping 10 mm x 5 mm rectangles.

COVERAGE: A4, A3, A2, A1, A0. The B series, the elongated sizes (A3.1, A4.2,
...) and the untrimmed sizes are not transcribed; `sheet_size` refuses an
unknown name and lists the five it has.

NOT IMPLEMENTED, deliberately, so nobody reads more into the geometry than is
in it:

* ISO 5457's allowance for shorter *corner* fields in the grid. The divisions
  drawn here are equal (frame side / n). The division COUNT -- the number a
  zone reference quotes, and the thing that is visible on the sheet -- is the
  standard's; the two end fields' boundary sits up to 5 mm from where a
  corner-field layout would put it.
* The grid is drawn for A4 too, although ISO 5457 does not require one there.
  Pass `zones=False`.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING

from engineering.mech.primitives import Circle, Line, Poly, Prim, Pt, Text

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = [
    "CENTRING_OVERSHOOT",
    "EDGE_MARGIN",
    "FILING_MARGIN",
    "ORIENTATIONS",
    "SHEETS",
    "SHEET_LAYER",
    "SHEET_ROLE_LAYER",
    "SHEET_TEXT_LAYER",
    "TRIM_MARK_LONG",
    "TRIM_MARK_SHORT",
    "ZONE_DIVISIONS",
    "ZONE_MODULE",
    "ZONE_TEXT_HEIGHT",
    "centred_text",
    "draw_sheet_frame",
    "draw_sheet_prims",
    "enter_layout",
    "frame_metrics",
    "frame_prims",
    "sheet_size",
    "zone_divisions",
]

#: Portrait width x height in millimetres (ISO 216). Landscape swaps the pair.
SHEETS: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
}

FILING_MARGIN = 20.0
EDGE_MARGIN = 10.0
ZONE_MODULE = 50.0
ZONE_TEXT_HEIGHT = 3.5
CENTRING_OVERSHOOT = 5.0
TRIM_MARK_LONG = 10.0
TRIM_MARK_SHORT = 5.0

#: The sheet is not part geometry: borders, rules and marks go on TITLEBLOCK and
#: the lettering on TEXT, which is where `titleblock_apply_iso_a3` has always put
#: them and what `tests/test_engineering_titleblock.py` pins.
SHEET_LAYER = "TITLEBLOCK"
SHEET_TEXT_LAYER = "TEXT"

SHEET_ROLE_LAYER: dict[str, str] = {
    "visible": SHEET_LAYER,
    "center": SHEET_LAYER,
    "text": SHEET_TEXT_LAYER,
}

#: (divisions along the long side, divisions along the short side), ISO 5457 4.4.
#: Each row reproduces the even-division rule applied to that side of the frame
#: -- `test_zone_division_counts_match_the_authored_table` asserts both, so the
#: table and the rule can never drift apart.
ZONE_DIVISIONS: dict[str, tuple[int, int]] = {
    "A4": (6, 4),
    "A3": (8, 6),
    "A2": (12, 8),
    "A1": (16, 12),
    "A0": (24, 16),
}

ORIENTATIONS = ("landscape", "portrait")
_LETTERS = string.ascii_uppercase


def sheet_size(size: str, orientation: str = "landscape") -> tuple[float, float]:
    """Trimmed width x height in millimetres for a sheet name and orientation."""
    key = str(size).strip().upper()
    if key not in SHEETS:
        raise ValueError(
            f"ISO 5457 sheet {size!r} is not in the table; covered sizes: {', '.join(SHEETS)}"
        )
    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientation must be one of {ORIENTATIONS}, got {orientation!r}")
    w, h = SHEETS[key]
    return (h, w) if orientation == "landscape" else (w, h)


def zone_divisions(side: float) -> int:
    """ISO 5457 4.4: an even number of divisions, `side` / 50 mm rounded to it.

    `round()` is banker's at an exact .5; no frame side of A4..A0 lands there
    (the closest is A2 landscape's 400 / 100 = 4.0, which is exact), and a side
    that did would round down -- the safer half of the ambiguity, because it
    makes the fields longer rather than shorter.
    """
    return max(int(2 * round(float(side) / (2.0 * ZONE_MODULE))), 2)


def frame_metrics(size: str, *, orientation: str = "landscape") -> dict:
    """Every number the frame is built from, for a caller that wants to place
    geometry inside it without re-deriving the margins."""
    w, h = sheet_size(size, orientation)
    x0, y0 = FILING_MARGIN, EDGE_MARGIN
    x1, y1 = w - EDGE_MARGIN, h - EDGE_MARGIN
    columns = zone_divisions(x1 - x0)
    rows = zone_divisions(y1 - y0)
    if rows > len(_LETTERS):
        raise ValueError(f"{size}: {rows} grid rows need more than {len(_LETTERS)} letters")
    return {
        "size": str(size).strip().upper(),
        "orientation": orientation,
        "sheet": [w, h],
        "margins": {
            "left": FILING_MARGIN,
            "right": EDGE_MARGIN,
            "top": EDGE_MARGIN,
            "bottom": EDGE_MARGIN,
        },
        "frame": [x0, y0, x1, y1],
        "drawing_area": [x1 - x0, y1 - y0],
        "centre": [w / 2.0, h / 2.0],
        "zones": {
            "columns": columns,
            "rows": rows,
            "column_width": (x1 - x0) / columns,
            "row_height": (y1 - y0) / rows,
            "numbers": [str(i + 1) for i in range(columns)],
            "letters": [_LETTERS[i] for i in range(rows)],
        },
    }


def _rect(x0: float, y0: float, x1: float, y1: float, role: str = "visible") -> Poly:
    return Poly(points=((x0, y0), (x1, y0), (x1, y1), (x0, y1)), closed=True, role=role)


def centred_text(cx: float, cy: float, text: str, height: float) -> Text:
    """`entity_create_text` carries no alignment parameter, so centring is done
    by offsetting the left-baseline insertion point: 0.6 * height of advance per
    character (the txt.shx / ISO 3098 width factor) and half a cap height."""
    return Text(at=(cx - 0.3 * height * len(text), cy - 0.5 * height), text=text, height=height)


def frame_prims(
    size: str,
    *,
    orientation: str = "landscape",
    zones: bool = True,
    marks: bool = True,
) -> tuple[Prim, ...]:
    """The ISO 5457 sheet as primitive descriptions, sheet-local, with (0, 0) at
    the lower-left corner of the trimmed sheet.

    Order is pinned: [0] the trimmed sheet, [1] the drawing frame, then the zone
    ticks, then the zone labels, then the four centring marks, then the eight
    trimming rectangles.
    """
    metrics = frame_metrics(size, orientation=orientation)
    w, h = metrics["sheet"]
    x0, y0, x1, y1 = metrics["frame"]
    prims: list[Prim] = [_rect(0.0, 0.0, w, h), _rect(x0, y0, x1, y1)]

    if zones:
        zone = metrics["zones"]
        cw, rh = zone["column_width"], zone["row_height"]
        for i in range(1, zone["columns"]):
            x = x0 + i * cw
            prims.append(Line(p1=(x, y1), p2=(x, h)))
            prims.append(Line(p1=(x, 0.0), p2=(x, y0)))
        for j in range(1, zone["rows"]):
            y = y0 + j * rh
            prims.append(Line(p1=(0.0, y), p2=(x0, y)))
            prims.append(Line(p1=(x1, y), p2=(w, y)))
        for i, label in enumerate(zone["numbers"]):
            cx = x0 + (i + 0.5) * cw
            prims.append(centred_text(cx, y1 + EDGE_MARGIN / 2.0, label, ZONE_TEXT_HEIGHT))
            prims.append(centred_text(cx, y0 / 2.0, label, ZONE_TEXT_HEIGHT))
        for j, label in enumerate(zone["letters"]):
            # ISO 5457 4.4: rows are lettered from the top.
            cy = y1 - (j + 0.5) * rh
            prims.append(centred_text(FILING_MARGIN / 2.0, cy, label, ZONE_TEXT_HEIGHT))
            prims.append(centred_text(x1 + EDGE_MARGIN / 2.0, cy, label, ZONE_TEXT_HEIGHT))

    if marks:
        cx, cy = metrics["centre"]
        prims.append(Line(p1=(0.0, cy), p2=(x0 + CENTRING_OVERSHOOT, cy), role="center"))
        prims.append(Line(p1=(w, cy), p2=(x1 - CENTRING_OVERSHOOT, cy), role="center"))
        prims.append(Line(p1=(cx, 0.0), p2=(cx, y0 + CENTRING_OVERSHOOT), role="center"))
        prims.append(Line(p1=(cx, h), p2=(cx, y1 - CENTRING_OVERSHOOT), role="center"))
        for sx, ox in ((1.0, 0.0), (-1.0, w)):
            for sy, oy in ((1.0, 0.0), (-1.0, h)):
                prims.append(_rect(ox, oy, ox + sx * TRIM_MARK_LONG, oy + sy * TRIM_MARK_SHORT))
                prims.append(_rect(ox, oy, ox + sx * TRIM_MARK_SHORT, oy + sy * TRIM_MARK_LONG))

    return tuple(prims)


async def enter_layout(backend: AutoCADBackend, layout: str | None, *, caller: str) -> str:
    """Make `layout` current and return the space to restore afterwards.

    A sheet border frames the printed sheet rather than the model, so every
    sheet tool takes a layout; asking for a border must not silently leave the
    caller standing on it, which is why the caller's space comes back.
    """
    if layout is None:
        return "Model"
    listing = await backend.layout_list()
    names = {str(entry) for entry in (listing.get("layouts") or [])}
    if layout not in names:
        raise RuntimeError(
            f"{caller}: layout {layout!r} does not exist "
            f"(have: {', '.join(sorted(names))}). Create it with layout_create first."
        )
    previous = getattr(backend, "_current_space", "Model")
    switched = await backend.layout_set_current(layout)
    if not switched.get("ok", True):
        raise RuntimeError(f"{caller}: {switched.get('error')}")
    return previous


async def draw_sheet_prims(
    backend: AutoCADBackend,
    prims,
    *,
    origin: Pt = (0.0, 0.0),
    layer_map: dict | None = None,
) -> list[str]:
    """Draw sheet primitives at `origin` and return their handles, in order."""
    ox, oy = float(origin[0]), float(origin[1])
    layers = dict(SHEET_ROLE_LAYER if layer_map is None else layer_map)
    handles: list[str] = []
    for prim in prims:
        layer = layers.get(prim.role, SHEET_LAYER)
        if isinstance(prim, Poly):
            entity = await backend.entity_create_polyline(
                points=[[ox + px, oy + py] for px, py in prim.points],
                closed=prim.closed,
                layer=layer,
            )
        elif isinstance(prim, Line):
            entity = await backend.entity_create_line(
                ox + prim.p1[0], oy + prim.p1[1], ox + prim.p2[0], oy + prim.p2[1], layer=layer
            )
        elif isinstance(prim, Circle):
            entity = await backend.entity_create_circle(
                ox + prim.center[0], oy + prim.center[1], prim.radius, layer=layer
            )
        elif isinstance(prim, Text):
            entity = await backend.entity_create_text(
                text=prim.text,
                x=ox + prim.at[0],
                y=oy + prim.at[1],
                height=prim.height,
                rotation=prim.rotation,
                layer=layer,
            )
        else:
            raise ValueError(
                f"sheet primitives are Line, Circle, Poly and Text; got {type(prim).__name__}"
            )
        handles.append(entity.handle)
    return handles


async def draw_sheet_frame(
    backend: AutoCADBackend,
    size: str,
    *,
    orientation: str = "landscape",
    zones: bool = True,
    marks: bool = True,
    origin: Pt = (0.0, 0.0),
    layout: str | None = None,
) -> dict:
    """Draw the ISO 5457 frame. Every refusal fires before the first entity."""
    metrics = frame_metrics(size, orientation=orientation)
    prims = frame_prims(size, orientation=orientation, zones=zones, marks=marks)
    previous = await enter_layout(backend, layout, caller="sheet_frame")
    try:
        handles = await draw_sheet_prims(backend, prims, origin=origin)
    finally:
        if layout is not None:
            await backend.layout_set_current(previous)
    ox, oy = float(origin[0]), float(origin[1])
    w, h = metrics["sheet"]
    return {
        "ok": True,
        "size": metrics["size"],
        "orientation": orientation,
        "layout": layout or "Model",
        "zones": zones,
        "marks": marks,
        "handles": handles,
        "outer_border": handles[0],
        "inner_border": handles[1],
        "metrics": metrics,
        "bbox": {"min": [ox, oy], "max": [ox + w, oy + h]},
    }
