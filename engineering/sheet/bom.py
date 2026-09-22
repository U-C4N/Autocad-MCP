"""ISO 7573 parts lists and ISO 6433 item references.

SOURCE
------
Parts list   ISO 7573:2008 -- an item list carries at least an item reference
             (the position number), a quantity, a designation and a reference
             to the document or standard that defines the item. When the list
             is drawn on the drawing it is placed directly above the title
             block, has the same width as it, and the item numbers ascend
             upwards so the list can be extended -- which puts the heading row
             at the bottom, against the title block. `direction="down"` draws
             the same list the other way up, for a list on its own sheet.
Balloons     ISO 6433:2012 -- an item reference is a numeral placed outside the
             outline of the item and connected to it by a leader line that
             terminates in a dot on the item.

NOT TRANSCRIBED, and therefore not asserted anywhere as if it were the
standard's:

* The column widths. ISO 7573 fixes neither the widths nor the order; the
  shares below total the 180 mm title-block width, which *is* the standard's
  rule, and any column set is rescaled to that total.
* The row height. 7 mm is this repository's `entity_create_table` default.
* ISO 6433's rule that the numeral is at least twice the height of the
  dimension characters. The numeral here is 1.4 * the balloon radius, so the
  default radius of 4 mm gives a 5.6 mm numeral -- which clears 2 x 2.5 mm
  dimension text but not 2 x 3.5 mm. A drawing dimensioned at 3.5 mm should
  pass `radius=5.0`, and the docstring of `balloon_prims` says so.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from engineering.mech.primitives import Circle, Line, Prim, Pt, Text
from engineering.mech.xdata import APP_ID, PAYLOAD_VERSION, decode, to_values
from engineering.sheet.frames import SHEET_TEXT_LAYER, draw_sheet_prims, enter_layout

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = [
    "BALLOON_DOT_RADIUS",
    "BOM_COLUMNS",
    "COLUMN_LABELS",
    "COLUMN_SHARES",
    "DEFAULT_COLUMNS",
    "DIRECTIONS",
    "LIST_STANDARDS",
    "LIST_WIDTH",
    "PAGE_SIZE",
    "ROW_HEIGHT",
    "add_balloon",
    "all_entities",
    "balloon_prims",
    "draw_bom_table",
    "extract_records",
    "read_balloons",
    "rows_from_records",
    "table_layout",
    "table_prims",
]

LIST_WIDTH = 180.0  # ISO 7573: the item list is as wide as the title block.
ROW_HEIGHT = 7.0
TEXT_HEIGHT = 2.5
BALLOON_DOT_RADIUS = 0.5
DIRECTIONS = ("up", "down")
LIST_STANDARDS = ("ISO 7573",)

#: Every column this module knows how to print, in the order it prints them.
BOM_COLUMNS: tuple[str, ...] = (
    "item",
    "qty",
    "designation",
    "standard",
    "material",
    "mass",
    "remarks",
)

#: The four ISO 7573 minimum columns plus the material this shop always wants.
DEFAULT_COLUMNS: tuple[str, ...] = ("item", "qty", "designation", "standard", "material")

COLUMN_LABELS: dict[str, str] = {
    "item": "POS",
    "qty": "QTY",
    "designation": "DESIGNATION",
    "standard": "STANDARD",
    "material": "MATERIAL",
    "mass": "MASS",
    "remarks": "REMARKS",
}

#: Relative widths; `table_layout` rescales the chosen set to LIST_WIDTH. The
#: default five happen to total 180.0, so their scale factor is exactly 1.
COLUMN_SHARES: dict[str, float] = {
    "item": 12.0,
    "qty": 14.0,
    "designation": 74.0,
    "standard": 46.0,
    "material": 34.0,
    "mass": 22.0,
    "remarks": 40.0,
}


#: How many entities one `entity_list` call asks for while paging. It is a page
#: size, never a cap: `all_entities` keeps asking until a short page comes back.
PAGE_SIZE = 1000


async def all_entities(backend: AutoCADBackend, *, type_filter=None, layer=None):
    """Every entity of a type in the CURRENT space -- not the first 200 of them.

    `entity_list`'s `limit` defaults to 200 on both engines, so a single call is
    a silent truncation on any real drawing: a parts list that stops at the
    200th INSERT prints a wrong QTY, and a duplicate-balloon guard that only
    sees the first 200 CIRCLEs is blind on a drawing whose hole pattern fills
    them. Everything in this module that *counts* or *guards* reads through
    here instead.

    The loop stops on a short page, and also when a page brings nothing new --
    a backend that ignored `offset` would otherwise hand back the same page for
    ever. Handles are unique per drawing, so that guard cannot drop a real
    entity.
    """
    found: list = []
    seen: set[str] = set()
    offset = 0
    while True:
        page = await backend.entity_list(
            type_filter=type_filter, layer_filter=layer, limit=PAGE_SIZE, offset=offset
        )
        fresh = [entity for entity in page if entity.handle not in seen]
        if not fresh:
            return tuple(found)
        seen.update(entity.handle for entity in fresh)
        found.extend(fresh)
        if len(page) < PAGE_SIZE:
            return tuple(found)
        offset += len(page)


def _check_columns(columns) -> tuple[str, ...]:
    chosen = tuple(DEFAULT_COLUMNS if columns is None else columns)
    unknown = [c for c in chosen if c not in BOM_COLUMNS]
    if unknown:
        raise ValueError(
            f"unknown parts-list column(s) {', '.join(unknown)}; "
            f"available: {', '.join(BOM_COLUMNS)}"
        )
    if "item" not in chosen:
        raise ValueError("ISO 7573: the item reference column 'item' cannot be dropped")
    return chosen


def rows_from_records(records, *, columns=None, group_by: str | None = "designation"):
    """Collapse extracted INSERT records into numbered parts-list rows.

    `group_by` is a record key (`"designation"`, `"standard"`, ...) or None for
    one row per insert. Rows are sorted by the grouping value so a rerun on the
    same drawing numbers the same parts the same way; `handles` carries every
    INSERT the row stands for, which is what a balloon links to.
    """
    chosen = _check_columns(columns)
    if group_by is not None and group_by not in BOM_COLUMNS:
        raise ValueError(
            f"group_by must be None or one of {', '.join(BOM_COLUMNS)}; got {group_by!r}"
        )
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for index, record in enumerate(records):
        key = str(record.get(group_by, "")) if group_by is not None else f"#{index:06d}"
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {c: record.get(c, "") for c in chosen if c != "item"}
            bucket["qty"] = 0
            bucket["handles"] = []
            bucket["_key"] = key
            buckets[key] = bucket
            order.append(key)
        bucket["qty"] += int(record.get("qty", 1) or 1)
        handle = record.get("handle")
        if handle:
            bucket["handles"].append(str(handle))
    if group_by is not None:
        order.sort()
    rows: list[dict] = []
    for item, key in enumerate(order, start=1):
        bucket = buckets[key]
        bucket.pop("_key", None)
        bucket["handles"] = tuple(bucket["handles"])
        bucket["item"] = item
        rows.append({c: bucket.get(c, "") for c in chosen} | {"handles": bucket["handles"]})
    return tuple(rows)


def table_layout(
    rows,
    *,
    at: Pt,
    direction: str = "up",
    columns=None,
    row_height: float = ROW_HEIGHT,
):
    """Everything both renderers need, so the prims and the real TABLE agree.

    `at` is the anchor: the list's bottom-left corner for `direction="up"` (it
    sits on the title block) and its top-left corner for `direction="down"`.
    `insert` is where `entity_create_table` must start, since a TABLE is drawn
    downwards from its insertion point.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    chosen = _check_columns(columns)
    if row_height <= 0:
        raise ValueError(f"row_height must be positive, got {row_height!r}")
    raw = [COLUMN_SHARES[c] for c in chosen]
    scale = LIST_WIDTH / sum(raw)
    widths = [w * scale for w in raw]
    heading = [COLUMN_LABELS[c] for c in chosen]
    body = [[str(row.get(c, "")) for c in chosen] for row in rows]
    cells = list(reversed(body)) + [heading] if direction == "up" else [heading] + body
    height = row_height * len(cells)
    ax, ay = float(at[0]), float(at[1])
    insert = (ax, ay + height) if direction == "up" else (ax, ay)
    return {
        "columns": chosen,
        "labels": heading,
        "widths": widths,
        "width": sum(widths),
        "height": height,
        "row_height": row_height,
        "direction": direction,
        "anchor": (ax, ay),
        "insert": insert,
        "cells": cells,
    }


def table_prims(
    rows,
    *,
    at: Pt,
    direction: str = "up",
    standard: str = "ISO 7573",
    height: float = ROW_HEIGHT,
) -> tuple[Prim, ...]:
    """The parts list as primitive descriptions, for a caller that does not
    want a real TABLE (and for the tests that pin the layout)."""
    if standard not in LIST_STANDARDS:
        raise ValueError(
            f"parts-list standard {standard!r} is not transcribed; "
            f"available: {', '.join(LIST_STANDARDS)}"
        )
    layout = table_layout(rows, at=at, direction=direction, row_height=height)
    ax, ay = layout["anchor"]
    total_w, total_h = layout["width"], layout["height"]
    prims: list[Prim] = []
    for index in range(len(layout["cells"]) + 1):
        y = ay + index * layout["row_height"]
        prims.append(Line(p1=(ax, y), p2=(ax + total_w, y)))
    x = ax
    prims.append(Line(p1=(x, ay), p2=(x, ay + total_h)))
    for width in layout["widths"]:
        x += width
        prims.append(Line(p1=(x, ay), p2=(x, ay + total_h)))
    pad = min(1.0, layout["row_height"] * 0.15)
    for row_index, cells in enumerate(layout["cells"]):
        # cells are top-to-bottom; the top row is the last one up the sheet
        baseline = ay + (len(layout["cells"]) - row_index - 1) * layout["row_height"] + pad
        x = ax
        for column_index, value in enumerate(cells):
            if value:
                prims.append(Text(at=(x + pad, baseline), text=value, height=TEXT_HEIGHT))
            x += layout["widths"][column_index]
    return tuple(prims)


def balloon_prims(
    item: int,
    at: Pt,
    leader_to: Pt,
    *,
    radius: float = 4.0,
    height: float | None = None,
) -> tuple[Prim, ...]:
    """One ISO 6433 item reference: the balloon, the leader, the dot, the numeral.

    The numeral is 1.4 * `radius` high. ISO 6433 asks for at least twice the
    dimension character height, so a sheet dimensioned at 3.5 mm wants
    `radius=5.0` (a 7 mm numeral); the 4 mm default clears 2.5 mm dimensions.
    """
    number = int(item)
    if number <= 0:
        raise ValueError(f"an ISO 6433 item reference is a positive integer; got {item!r}")
    if radius <= 0:
        raise ValueError(f"balloon radius must be positive; got {radius!r}")
    text_height = float(radius) * 1.4 if height is None else float(height)
    cx, cy = float(at[0]), float(at[1])
    tx, ty = float(leader_to[0]), float(leader_to[1])
    distance = math.hypot(tx - cx, ty - cy)
    if distance <= radius:
        raise ValueError(
            f"balloon {number}: the leader target ({tx}, {ty}) is inside the balloon at "
            f"({cx}, {cy}) r={radius}; ISO 6433 puts the reference outside the item"
        )
    ux, uy = (tx - cx) / distance, (ty - cy) / distance
    label = str(number)
    return (
        Circle(center=(cx, cy), radius=float(radius)),
        Line(p1=(cx + ux * radius, cy + uy * radius), p2=(tx, ty)),
        Circle(center=(tx, ty), radius=BALLOON_DOT_RADIUS),
        Text(
            at=(cx - 0.3 * text_height * len(label), cy - 0.5 * text_height),
            text=label,
            height=text_height,
        ),
    )


#: Block attributes read when an INSERT carries no ACADMCP_MECH payload.
_ATTRIBUTE_KEYS = {
    "designation": ("DESIGNATION", "DESIG", "NAME", "TAG"),
    "standard": ("STANDARD", "STD", "NORM"),
    "material": ("MATERIAL", "MAT"),
    "size": ("SIZE",),
    "qty": ("QTY", "QUANTITY"),
    "remarks": ("REMARKS", "NOTE"),
}


def _from_attributes(attributes: dict) -> dict:
    upper = {str(k).upper(): v for k, v in (attributes or {}).items()}
    record: dict = {}
    for field, keys in _ATTRIBUTE_KEYS.items():
        for key in keys:
            if key in upper and str(upper[key]).strip():
                record[field] = str(upper[key]).strip()
                break
    return record


async def extract_records(backend: AutoCADBackend, *, layer=None, limit: int | None = None):
    """Walk the INSERTs and return one parts-list record per block reference.

    Reads only: the entity listing, each INSERT's attributes and its
    ACADMCP_MECH payload. Nothing is written, so this is safe on a drawing that
    is not ours. `source` is "xdata" when the payload supplied the row and
    "attributes" when the block's ATTRIBs did; a block with neither is skipped
    rather than guessed at.

    `limit` is None by default and the whole drawing is read, by paging
    `entity_list` rather than taking its 200-entity default: a parts list that
    stops at the 200th INSERT does not print a shorter list, it prints a wrong
    QTY, and nothing on the sheet says so. A caller that wants a cap passes one
    and reads `len(records)` back against it -- `bom_extract` asks for one more
    record than the cap so it can report `truncated` exactly.
    """
    if limit is not None and int(limit) <= 0:
        raise ValueError(f"bom_extract: limit must be a positive number of records; got {limit!r}")
    inserts = await all_entities(backend, type_filter="INSERT", layer=layer)
    records: list[dict] = []
    for entity in inserts:
        payload: dict = {}
        try:
            raw = await backend.entity_get_xdata(entity.handle, APP_ID)
            payload = decode(raw.get("xdata", {}).get(APP_ID, [])) or {}
        except (ValueError, KeyError, TypeError):
            payload = {}
        if payload.get("kind") != "std_part":
            payload = {}
        # Measured on both engines: `block_get_attributes` returns the flat
        # ``{tag: value}`` mapping itself, not ``{"attributes": {...}}``
        # (ezdxf_backend.py:6687 builds it from ``ent.attribs``, com_backend.py:5703
        # from ``ref.GetAttributes()``). Reading a wrapper key here would have
        # made every attribute fallback silently empty.
        attributes = {}
        try:
            attributes = await backend.block_get_attributes(entity.handle)
        except Exception:  # noqa: BLE001 - a block with no ATTRIBs is not an error here
            attributes = {}
        fallback = _from_attributes(attributes)
        designation = str(payload.get("designation") or fallback.get("designation") or "").strip()
        if not designation:
            continue
        records.append(
            {
                "handle": entity.handle,
                "block": entity.properties.get("block_name", ""),
                "layer": entity.layer,
                "designation": designation,
                "standard": str(payload.get("standard") or fallback.get("standard") or ""),
                "size": str(payload.get("size") or fallback.get("size") or ""),
                "material": str(payload.get("material") or fallback.get("material") or ""),
                "mass": str(payload.get("mass") or ""),
                "remarks": str(payload.get("remarks") or fallback.get("remarks") or ""),
                "qty": int(payload.get("qty") or fallback.get("qty") or 1),
                "source": "xdata" if payload else "attributes",
            }
        )
        if limit is not None and len(records) >= int(limit):
            break
    return tuple(records)


async def draw_bom_table(
    backend: AutoCADBackend,
    rows,
    *,
    at: Pt,
    direction: str = "up",
    columns=None,
    row_height: float = ROW_HEIGHT,
    layer: str = SHEET_TEXT_LAYER,
    layout: str | None = None,
) -> dict:
    """Draw the parts list as a real TABLE entity.

    `entity_create_table` grows downwards from its insertion point, so an
    upward-growing ISO 7573 list is drawn from an insertion one table height
    above the anchor, with the heading as the last (bottom) row.

    `representation` is reported rather than assumed: the live engine draws a
    real ACAD_TABLE, while the headless one returns a *composite* whose
    ``handle`` is its first child rule (``tests/test_table_leader.py`` pins
    that contract). A caller that means to edit the table afterwards has to
    know which of the two it got.
    """
    if not rows:
        raise ValueError("bom_table: there are no parts-list rows to draw")
    spec = table_layout(rows, at=at, direction=direction, columns=columns, row_height=row_height)
    previous = await enter_layout(backend, layout, caller="bom_table")
    try:
        entity = await backend.entity_create_table(
            spec["insert"][0],
            spec["insert"][1],
            spec["cells"],
            headers=None,
            column_widths=spec["widths"],
            row_height=spec["row_height"],
            text_height=TEXT_HEIGHT,
            layer=layer,
        )
    finally:
        if layout is not None:
            await backend.layout_set_current(previous)
    ax, ay = spec["anchor"]
    return {
        "ok": True,
        "handle": entity.handle,
        "entity_type": entity.type,
        "representation": entity.properties.get("representation", "native"),
        "child_handles": list(entity.properties.get("child_handles") or []),
        "standard": "ISO 7573",
        "direction": direction,
        "columns": list(spec["columns"]),
        "rows": len(rows),
        "insert": spec["insert"],
        "layout": layout or "Model",
        "bbox": {"min": [ax, ay], "max": [ax + spec["width"], ay + spec["height"]]},
    }


async def read_balloons(backend: AutoCADBackend):
    """Every ISO 6433 balloon this server drew in the CURRENT space, from its
    ACADMCP_MECH payload.

    Paged, not capped: a drawing whose first 200 CIRCLEs are a hole pattern --
    routine -- used to read back no balloons at all, which made every guard in
    `add_balloon` silently pass.
    """
    found: list[dict] = []
    for entity in await all_entities(backend, type_filter="CIRCLE"):
        try:
            raw = await backend.entity_get_xdata(entity.handle, APP_ID)
            payload = decode(raw.get("xdata", {}).get(APP_ID, [])) or {}
        except (ValueError, KeyError, TypeError):
            continue
        if payload.get("kind") != "balloon":
            continue
        found.append(
            {
                "handle": entity.handle,
                "item": int(payload.get("item", 0)),
                "targets": list(payload.get("targets") or []),
                "text": payload.get("text", ""),
            }
        )
    found.sort(key=lambda row: row["item"])
    return tuple(found)


async def add_balloon(
    backend: AutoCADBackend,
    *,
    item: int,
    at: Pt,
    leader_to: Pt,
    targets=(),
    radius: float = 4.0,
    layer: str = "DIM",
    layout: str | None = None,
) -> dict:
    """Draw (or renumber) one ISO 6433 item reference.

    A balloon already pointing at the same targets is *renumbered* -- its
    numeral is edited and its payload rewritten -- so running the balloon pass
    again after the parts list is regrouped does not litter the sheet with
    duplicates. An item number already on the sheet is refused before anything
    is drawn unless it is that renumber: ISO 129-1 and ISO 6433 both want one
    reference per item.

    Both guards read the space the balloon is going ON. `read_balloons` lists
    the current space, so reading it before `enter_layout` inspected Model
    while the balloons lived on the layout -- and the layout is the sheet
    workflow, so the guards were off exactly where they matter.
    """
    number = int(item)
    wanted = tuple(sorted(str(t) for t in targets))
    prims = balloon_prims(number, at, leader_to, radius=radius)  # validates first

    previous = await enter_layout(backend, layout, caller="balloon_add")
    try:
        existing = await read_balloons(backend)
        for balloon in existing:
            if wanted and tuple(sorted(balloon["targets"])) == wanted:
                if balloon["text"]:
                    await backend.entity_edit_text(balloon["text"], text=str(number))
                payload = {
                    "v": PAYLOAD_VERSION,
                    "kind": "balloon",
                    "item": number,
                    "targets": list(wanted),
                    "text": balloon["text"],
                }
                await backend.entity_set_xdata(balloon["handle"], APP_ID, to_values(payload))
                return {
                    "ok": True,
                    "renumbered": True,
                    "item": number,
                    "handle": balloon["handle"],
                    "text": balloon["text"],
                    "targets": list(wanted),
                    "layout": layout or "Model",
                }
        # Anything still carrying this number is a clash: the renumber arm above
        # has already returned for the one balloon that stands for these
        # targets. Comparing the target tuples here instead let two untargeted
        # balloons -- `targets` is optional in the tool, so `() != ()` -- carry
        # the same item number with no refusal at all.
        clash = next((b for b in existing if b["item"] == number), None)
        if clash is not None:
            has_targets = f"targets {clash['targets']}" if clash["targets"] else "no targets"
            raise ValueError(
                f"item reference {number} is already used by balloon {clash['handle']} "
                f"({has_targets}); ISO 6433 gives each item one reference. Pass that "
                "balloon's targets to renumber it in place, or use a free number."
            )

        handles = await draw_sheet_prims(
            backend, prims, layer_map={"visible": layer, "text": layer, "center": layer}
        )
        payload = {
            "v": PAYLOAD_VERSION,
            "kind": "balloon",
            "item": number,
            "targets": list(wanted),
            "text": handles[3],
        }
        await backend.entity_set_xdata(handles[0], APP_ID, to_values(payload))
        return {
            "ok": True,
            "renumbered": False,
            "item": number,
            "handle": handles[0],
            "leader": handles[1],
            "dot": handles[2],
            "text": handles[3],
            "targets": list(wanted),
            "layout": layout or "Model",
        }
    finally:
        if layout is not None:
            await backend.layout_set_current(previous)
