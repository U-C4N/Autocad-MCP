"""Door, window and room schedules - a read of the drawing, drawn as a real TABLE.

`schedule_rows` turns plan data (the shape `read_plan` returns: ``walls``,
``openings``, ``rooms`` lists of model dataclasses) into rows; `draw_schedule`
reads that data off the drawing's ``ACADMCP_ARCH`` records and draws it through
the same `entity_create_table` path the ISO 7573 parts list takes
(`engineering/sheet/bom.py::draw_bom_table`): insertion at the top-left corner,
the table growing downwards, and the engine's representation reported rather
than assumed - a real ACAD_TABLE live, a composite of rules and MTEXT headless.

Rows:

* doors - tag, width, height, swing, hand, wall; windows - tag, width,
  height, sill, wall. Sorted by tag, numbers compared as numbers (D2 before
  D10). An opening without a tag gets the next free ``<prefix><n>`` in wall
  order (the order of ``walls``, then the offset along the wall); an explicit
  tag is never renumbered and a generated one never repeats it.
* rooms - number, name, area (the measured one the label carries), sorted by
  number with the unnumbered last, plus a total row.

A value the model does not carry (a door without a height) is an empty cell,
never a default. Headers come from `vocab(lang)`; ``lang="tr"`` also writes
decimal commas.

DECLARED, not standard: the paper column widths below and the reuse of the
parts list's 7 mm row and 2.5 mm text (`engineering.sheet.bom.ROW_HEIGHT`,
`TEXT_HEIGHT`) - no standard fixes a schedule's layout.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from engineering.arch.lang import LANGS, fmt_area_m2, fmt_number, tag_prefix, vocab
from engineering.arch.layers import ARCH_LAYERS, ARCH_ROLE_LAYER

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = [
    "COLUMN_WIDTHS",
    "MAX_ROWS",
    "SCHEDULE_COLUMNS",
    "SCHEDULE_KINDS",
    "SCHEDULE_TITLES",
    "draw_schedule",
    "schedule_rows",
]

SCHEDULE_KINDS: tuple[str, ...] = ("doors", "windows", "rooms")

SCHEDULE_COLUMNS: dict[str, tuple[str, ...]] = {
    "doors": ("tag", "width", "height", "swing", "hand", "wall"),
    "windows": ("tag", "width", "height", "sill", "wall"),
    "rooms": ("number", "name", "area"),
}

#: The `vocab` key of each schedule's title row.
SCHEDULE_TITLES: dict[str, str] = {
    "doors": "door_schedule",
    "windows": "window_schedule",
    "rooms": "room_schedule",
}

#: Column widths in paper millimetres (x the scale denominator in the drawing).
COLUMN_WIDTHS: dict[str, float] = {
    "tag": 15.0,
    "width": 20.0,
    "height": 20.0,
    "swing": 20.0,
    "hand": 20.0,
    "sill": 20.0,
    "wall": 20.0,
    "number": 15.0,
    "name": 45.0,
    "area": 25.0,
}

#: `entity_create_table` takes at most 200 rows, the title and header included.
MAX_ROWS = 198

_OPENING_KIND = {"doors": "door", "windows": "window"}


def _check(kind: str, lang: str) -> None:
    if kind not in SCHEDULE_KINDS:
        raise ValueError(
            f"kind: {kind!r} is not a schedule; schedules are {', '.join(SCHEDULE_KINDS)}"
        )
    if lang not in LANGS:
        raise ValueError(
            f"lang: {lang!r} is not a schedule language; languages are {', '.join(LANGS)}"
        )


def _natural(text: str) -> tuple:
    """``D10`` after ``D2``: digit runs compare as numbers."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.findall(r"\d+|\D+", str(text))
    )


def _mm(value, lang: str) -> str:
    """A length in mm: whole numbers without decimals, others to 0.1 mm; None is blank."""
    if value is None:
        return ""
    number = float(value)
    decimals = 0 if abs(number - round(number)) < 1e-9 else 1
    return fmt_number(number, lang, decimals)


def _opening_rows(kind: str, plan: dict, lang: str) -> tuple[dict, ...]:
    words = vocab(lang)
    wanted = _OPENING_KIND[kind]
    wall_order = {wall.id: index for index, wall in enumerate(plan.get("walls") or ())}
    openings = [o for o in plan.get("openings") or () if o.kind == wanted]
    openings.sort(
        key=lambda o: (wall_order.get(o.wall, len(wall_order)), o.wall, float(o.offset), o.id)
    )
    prefix = tag_prefix(wanted, lang)
    used = {str(o.tag) for o in openings if o.tag}
    counter = 0
    rows: list[dict] = []
    for opening in openings:
        tag = str(opening.tag) if opening.tag else None
        generated = tag is None
        if generated:
            counter += 1
            while f"{prefix}{counter}" in used:
                counter += 1
            tag = f"{prefix}{counter}"
            used.add(tag)
        row = {
            "id": opening.id,
            "tag": tag,
            "generated": generated,
            "width": _mm(opening.width, lang),
            "height": _mm(opening.height, lang),
            "wall": opening.wall,
        }
        if kind == "doors":
            row["swing"] = words[opening.swing]
            row["hand"] = words[opening.hand]
        else:
            row["sill"] = _mm(opening.sill, lang)
        rows.append(row)
    rows.sort(key=lambda row: _natural(row["tag"]))
    return tuple(rows)


def _room_rows(plan: dict, lang: str) -> tuple[dict, ...]:
    rooms = sorted(
        plan.get("rooms") or (),
        key=lambda r: (
            r.number is None or str(r.number) == "",
            _natural(r.number or ""),
            r.name,
            r.id,
        ),
    )
    if not rooms:
        return ()
    rows = [
        {
            "id": room.id,
            "number": "" if room.number is None else str(room.number),
            "name": room.name,
            "area": fmt_area_m2(room.area, lang),
            "area_mm2": float(room.area),
            "total": False,
        }
        for room in rooms
    ]
    total = math.fsum(float(room.area) for room in rooms)
    rows.append(
        {
            "id": "",
            "number": "",
            "name": vocab(lang)["total"],
            "area": fmt_area_m2(total, lang),
            "area_mm2": total,
            "total": True,
        }
    )
    return tuple(rows)


def schedule_rows(kind: str, plan: dict, *, lang: str = "en") -> tuple[dict, ...]:
    """The rows of one schedule, each a dict holding every column of ``SCHEDULE_COLUMNS[kind]``
    as display text, plus ``id`` (and ``generated`` for openings, ``area_mm2`` and
    ``total`` for rooms). Refused: an unknown kind or language, named with the list."""
    _check(kind, lang)
    if kind == "rooms":
        return _room_rows(plan, lang)
    return _opening_rows(kind, plan, lang)


async def _ensure_layer(backend: AutoCADBackend, name: str) -> None:
    """Create the schedule layer from its `ARCH_LAYERS` row when the drawing lacks it.

    Live ActiveX will not create a layer on assignment (the repository rule),
    so the layer exists before the table does.
    """
    if name.casefold() in {layer.name.casefold() for layer in await backend.layer_list()}:
        return
    row = next(r for r in ARCH_LAYERS if r[0] == name)
    await backend.layer_create(name=row[0], color=row[1], linetype=row[2], lineweight=row[3])


async def draw_schedule(
    backend: AutoCADBackend, kind: str, *, at, lang: str = "en", scale=50
) -> dict:
    """Read the drawing's records and draw one schedule as a TABLE whose top-left is ``at``.

    Refused before anything is drawn: an unknown kind or language, a scale that
    is not a finite positive number, an ``at`` that is not two finite numbers,
    a drawing with nothing to schedule, and more rows than one TABLE takes.
    """
    from engineering.arch.rooms import read_records  # the one ACADMCP_ARCH reader
    from engineering.sheet.bom import ROW_HEIGHT, TEXT_HEIGHT

    _check(kind, lang)
    try:
        factor = float(scale)
    except (TypeError, ValueError):
        raise ValueError(f"scale: expected a number, got {scale!r}") from None
    if not math.isfinite(factor) or factor <= 0.0:
        raise ValueError(f"scale: must be a finite number greater than zero, got {scale!r}")
    try:
        x, y = float(at[0]), float(at[1])
    except (TypeError, ValueError, IndexError):
        raise ValueError(f"at: expected [x, y], got {at!r}") from None
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"at: must be two finite numbers, got {at!r}")

    plan = await read_records(backend)
    rows = schedule_rows(kind, plan, lang=lang)
    if not rows:
        raise ValueError(
            f"kind: this drawing carries no {kind[:-1]} records to schedule "
            "(ACADMCP_ARCH on the architectural layers); draw the plan first"
        )
    if len(rows) > MAX_ROWS:
        raise ValueError(
            f"kind: {len(rows)} {kind} rows do not fit one TABLE ({MAX_ROWS} data rows at most)"
        )
    words = vocab(lang)
    columns = SCHEDULE_COLUMNS[kind]
    layer = ARCH_ROLE_LAYER["symbol"]
    await _ensure_layer(backend, layer)
    entity = await backend.entity_create_table(
        x,
        y,
        [[row[c] for c in columns] for row in rows],
        headers=[words[c] for c in columns],
        column_widths=[COLUMN_WIDTHS[c] * factor for c in columns],
        row_height=ROW_HEIGHT * factor,
        text_height=TEXT_HEIGHT * factor,
        title=words[SCHEDULE_TITLES[kind]],
        layer=layer,
    )
    width = sum(COLUMN_WIDTHS[c] for c in columns) * factor
    height = (len(rows) + 2) * ROW_HEIGHT * factor
    return {
        "ok": True,
        "kind": kind,
        "lang": lang,
        "handle": entity.handle,
        "entity_type": entity.type,
        "representation": entity.properties.get("representation", "native"),
        "child_handles": list(entity.properties.get("child_handles") or []),
        "layer": layer,
        "columns": list(columns),
        "headers": [words[c] for c in columns],
        "title": words[SCHEDULE_TITLES[kind]],
        "rows": [dict(row) for row in rows],
        "row_count": len(rows),
        "table_rows": entity.properties.get("rows"),
        "insert": [x, y],
        "bbox": {"min": [x, y - height], "max": [x + width, y]},
    }
