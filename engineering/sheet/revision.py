"""The revision block, revision clouds and the triangular revision tags.

SOURCE
------
ISO 7200:2004 lists `version` (the revision code) among the title block's data
fields, and a drawing that has been revised records each version with its
description, date and issuer. ISO 7200 does NOT standardise the revision
block's geometry, so the block here reuses the ISO 7573 item-list ruling: the
same 180 mm width as the title block and the same 7 mm rows. That is a layout
decision, and it is written down here rather than asserted as a standard.

The triangular revision tag and the revision cloud are drawing-office practice,
not ISO geometry: the cloud marks the changed region and the triangle carries
the revision code that the block's row explains. `entity_create_revcloud` draws
the cloud, so a revision *with* clouds is refused up front on an engine that
has no revcloud (the COM backend: `revcloud` is false there, no ActiveX member,
command-line only) rather than half-written.

Placement: the block defaults to the upper-right corner of the drawing frame
and grows downwards, so it does not fight the ISO 7573 item list for the space
directly above the title block.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from backends.xdata_specs import MAX_BYTES, app_size
from engineering.mech.primitives import Line, Poly, Prim, Pt, Text
from engineering.sheet.bom import LIST_WIDTH, ROW_HEIGHT, TEXT_HEIGHT, all_entities
from engineering.sheet.frames import (
    SHEET_LAYER,
    draw_sheet_prims,
    enter_layout,
    frame_metrics,
)

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = [
    "REVISION_COLUMNS",
    "REVISION_LABELS",
    "REVISION_SHARES",
    "REV_APP_ID",
    "REV_PAYLOAD_VERSION",
    "REV_RE",
    "TAG_SIDE",
    "add_revision",
    "read_revisions",
    "rev_payload",
    "rev_values",
    "revision_block_prims",
    "revision_heading_prims",
    "revision_row_prims",
    "revision_tag_prims",
]

REVISION_COLUMNS: tuple[str, ...] = ("rev", "description", "date", "by")
REVISION_LABELS: dict[str, str] = {
    "rev": "REV",
    "description": "DESCRIPTION",
    "date": "DATE",
    "by": "BY",
}
#: Widths in millimetres; they total the 180 mm the title block is wide.
REVISION_SHARES: dict[str, float] = {
    "rev": 16.0,
    "description": 96.0,
    "date": 40.0,
    "by": 28.0,
}
TAG_SIDE = 7.0
TAG_TEXT_HEIGHT = 3.5

#: A revision code: one to three capitals or digits (ISO 7200's `version`
#: field is free text; this is the shop's form and keeps the tag legible).
REV_RE = re.compile(r"^[A-Z0-9]{1,3}$")


def _check_rev(rev: str) -> str:
    code = str(rev).strip().upper()
    if not REV_RE.fullmatch(code):
        raise ValueError(
            f"revision code {rev!r} must be one to three capitals or digits (A, B, 01, ...)"
        )
    return code


def revision_tag_prims(rev: str, at: Pt, *, side: float = TAG_SIDE) -> tuple[Prim, ...]:
    """An equilateral triangle carrying the revision code, apex up, `at` its
    lower-left corner."""
    code = _check_rev(rev)
    if side <= 0:
        raise ValueError(f"revision tag side must be positive; got {side!r}")
    x, y = float(at[0]), float(at[1])
    height = side * (3.0**0.5) / 2.0
    triangle = Poly(
        points=((x, y), (x + side, y), (x + side / 2.0, y + height)),
        closed=True,
    )
    label = Text(
        at=(x + side / 2.0 - 0.3 * TAG_TEXT_HEIGHT * len(code), y + height * 0.25),
        text=code,
        height=TAG_TEXT_HEIGHT,
    )
    return (triangle, label)


def _band(cells: dict, *, ax: float, top: float, height: float) -> list[Prim]:
    """One horizontal band: its bottom rule, its five verticals and its text.

    The band's top rule belongs to the band above it (or to
    `revision_heading_prims`), so bands stack without drawing a rule twice --
    which is what lets `add_revision` append one band at a time and still
    produce the block `revision_block_prims` describes.
    """
    bottom = top - height
    widths = [REVISION_SHARES[c] for c in REVISION_COLUMNS]
    prims: list[Prim] = [Line(p1=(ax, bottom), p2=(ax + LIST_WIDTH, bottom))]
    x = ax
    prims.append(Line(p1=(x, top), p2=(x, bottom)))
    for width in widths:
        x += width
        prims.append(Line(p1=(x, top), p2=(x, bottom)))
    pad = min(1.0, height * 0.15)
    x = ax
    for column_index, column in enumerate(REVISION_COLUMNS):
        value = str(cells.get(column, ""))
        if value:
            prims.append(Text(at=(x + pad, bottom + pad), text=value, height=TEXT_HEIGHT))
        x += widths[column_index]
    return prims


def revision_heading_prims(*, at: Pt, height: float = ROW_HEIGHT) -> tuple[Prim, ...]:
    """The block's top rule and its heading band. `at` is the TOP-left corner."""
    if height <= 0:
        raise ValueError(f"row height must be positive; got {height!r}")
    ax, ay = float(at[0]), float(at[1])
    heading = {c: REVISION_LABELS[c] for c in REVISION_COLUMNS}
    return (
        Line(p1=(ax, ay), p2=(ax + LIST_WIDTH, ay)),
        *_band(heading, ax=ax, top=ay, height=height),
    )


def revision_row_prims(
    row: dict, *, at: Pt, index: int, height: float = ROW_HEIGHT
) -> tuple[Prim, ...]:
    """One revision band, `index` rows below the heading. Its first Text is the
    REV cell -- the primitive `add_revision` hangs the payload on."""
    if height <= 0:
        raise ValueError(f"row height must be positive; got {height!r}")
    if int(index) < 0:
        raise ValueError(f"row index must be zero or more; got {index!r}")
    ax, ay = float(at[0]), float(at[1])
    return tuple(_band(row, ax=ax, top=ay - (int(index) + 1) * height, height=height))


#: The revision row's own XDATA application.
#:
#: `engineering/mech/xdata.py::KINDS` is pinned to ("part", "std_part",
#: "balloon") and `engineering/mech/critique.py` scans ACADMCP_MECH for exactly
#: those three on INSERTs and CIRCLEs. A revision row is neither, so it carries
#: its own application rather than a fourth kind that `encode` would refuse.
#: The chunking is the same rule the mech codec follows -- ASCII JSON split at
#: 255 characters -- and `entity_set_xdata(handle, app_name, values)` supplies
#: the 1001 application tag itself, so only the chunks travel.
REV_APP_ID = "ACADMCP_SHEET_REV"
REV_PAYLOAD_VERSION = 1
_REV_CHUNK = 255


def rev_values(payload: dict) -> list[str]:
    """The chunk strings for one revision record. Refused before anything is written."""
    if not isinstance(payload, dict):
        raise TypeError(f"{REV_APP_ID}: a payload must be a dict, got {type(payload).__name__}")
    version = payload.get("v", REV_PAYLOAD_VERSION)
    if version != REV_PAYLOAD_VERSION:
        raise ValueError(
            f"{REV_APP_ID}: payload version {version!r}; this build writes and reads "
            f"version {REV_PAYLOAD_VERSION}"
        )
    text = json.dumps(
        {**payload, "v": REV_PAYLOAD_VERSION},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    )
    values = [text[i : i + _REV_CHUNK] for i in range(0, len(text), _REV_CHUNK)]
    size = app_size(REV_APP_ID, [(1000, value) for value in values])
    if size > MAX_BYTES:
        raise ValueError(
            f"{REV_APP_ID}: the record encodes to {size} bytes; the limit is 16 KB per "
            "entity across every application -- shorten the description"
        )
    return values


def rev_payload(values) -> dict:
    """The record back, or a ValueError. Never a plausible-looking guess."""
    parts = [value if isinstance(value, str) else str(value[1]) for value in values]
    try:
        payload = json.loads("".join(parts))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{REV_APP_ID}: the chunk stream is not JSON ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"{REV_APP_ID}: the payload decoded to {type(payload).__name__}, not a dict"
        )
    if payload.get("v") != REV_PAYLOAD_VERSION:
        raise ValueError(f"{REV_APP_ID}: payload version {payload.get('v')!r} is not readable here")
    return payload


def revision_block_prims(rows, *, at: Pt, height: float = ROW_HEIGHT) -> tuple[Prim, ...]:
    """The whole revision block: `at` is its TOP-left corner, the heading is
    the first band and the revisions follow downwards in the order given."""
    prims: list[Prim] = list(revision_heading_prims(at=at, height=height))
    for index, row in enumerate(rows):
        prims.extend(revision_row_prims(row, at=at, index=index, height=height))
    return tuple(prims)


async def read_revisions(backend: AutoCADBackend):
    """Every revision row this server wrote in the CURRENT space, from its
    ACADMCP_SHEET_REV payload.

    Paged, not capped: `entity_list`'s 200-entity default made a lettered
    drawing read back no revisions at all, which both broke the idempotency
    guard and restarted `index = len(existing)` at 0 -- a second heading band
    and a second row drawn exactly on top of the first.
    """
    found: list[dict] = []
    for entity in await all_entities(backend, type_filter="TEXT"):
        try:
            raw = await backend.entity_get_xdata(entity.handle, REV_APP_ID)
            payload = rev_payload(raw.get("xdata", {}).get(REV_APP_ID, [])) or {}
        except (ValueError, KeyError, TypeError):
            continue
        if not payload.get("rev"):
            continue
        found.append(
            {
                "handle": entity.handle,
                "rev": str(payload.get("rev", "")),
                "description": str(payload.get("description", "")),
                "date": str(payload.get("date", "")),
                "by": str(payload.get("by", "")),
            }
        )
    found.sort(key=lambda row: row["rev"])
    return tuple(found)


async def add_revision(
    backend: AutoCADBackend,
    *,
    rev: str,
    description: str,
    date: str = "",
    by: str = "",
    clouds=(),
    tags=(),
    size: str = "A3",
    orientation: str = "landscape",
    origin: Pt = (0.0, 0.0),
    at: Pt | None = None,
    cloud_segment: float = 8.0,
    layout: str | None = None,
) -> dict:
    """Append one revision row, and optionally cloud and tag what changed.

    Idempotent per revision code: a code the drawing already carries returns
    `created=False` and draws nothing at all, so re-running a revision pass
    cannot stack duplicate rows. Refusals fire before the first entity: a
    malformed code, a cloud region that is not four finite numbers, and -- on
    an engine whose `revcloud` capability is false -- any request for clouds.

    The idempotency guard reads the space the row is going ON. `read_revisions`
    lists the current space, so reading it before `enter_layout` inspected
    Model while the block lived on the layout, and every re-run of a
    `layout=`-scoped revision pass stacked a second heading band and a second
    row exactly on top of the first.
    """
    code = _check_rev(rev)
    regions = [[float(v) for v in region] for region in clouds]
    for index, region in enumerate(regions):
        if len(region) != 4:
            raise ValueError(f"clouds[{index}] must be [x0, y0, x1, y1]; got {clouds[index]!r}")
    points = [[float(p[0]), float(p[1])] for p in tags]
    if regions:
        feature = backend.capabilities().features.get("revcloud")
        if feature is None or not feature.supported:
            from backends.base import UnsupportedCapabilityError

            raise UnsupportedCapabilityError(
                "revcloud",
                f"add_revision: the {backend.name} backend cannot draw revision clouds "
                "(no ActiveX member; REVCLOUD is command-line only). Re-run without "
                "`clouds` to record the row, or draw the sheet on the ezdxf engine.",
            )

    previous = await enter_layout(backend, layout, caller="revision_add")
    try:
        existing = await read_revisions(backend)
        match = next((row for row in existing if row["rev"] == code), None)
        if match is not None:
            return {
                "ok": True,
                "created": False,
                "rev": code,
                "handle": match["handle"],
                "row": len(existing),
                "clouds": [],
                "tags": [],
                "layout": layout or "Model",
            }

        index = len(existing)
        new_row = {"rev": code, "description": description, "date": date, "by": by}
        metrics = frame_metrics(size, orientation=orientation)
        _x0, _y0, x1, y1 = metrics["frame"]
        anchor = (
            (float(origin[0]) + x1 - LIST_WIDTH, float(origin[1]) + y1)
            if at is None
            else (float(at[0]), float(at[1]))
        )
        # Append-only: the heading band is drawn once, on the first revision, and
        # every later revision adds exactly one band below the last. Nothing is
        # redrawn, so a revision can never stack a second copy of the block.
        head_prims = list(revision_heading_prims(at=anchor)) if index == 0 else []
        row_prims = list(revision_row_prims(new_row, at=anchor, index=index))

        block_handles = await draw_sheet_prims(backend, head_prims + row_prims)
        cloud_handles = []
        for region in regions:
            x0, y0, x1r, y1r = region
            cloud = await backend.entity_create_revcloud(
                [[x0, y0], [x1r, y0], [x1r, y1r], [x0, y1r]],
                cloud_segment,
                layer=SHEET_LAYER,
                closed=True,
            )
            cloud_handles.append(cloud.get("handle"))
        # One entry per tag *asked for*, not per entity drawn: a tag is two
        # primitives (the triangle and its letter), so a flat handle list would
        # report twice as many tags as the caller requested.
        tag_handles = []
        for point in points:
            drawn = await draw_sheet_prims(backend, revision_tag_prims(code, (point[0], point[1])))
            tag_handles.append({"at": [point[0], point[1]], "handles": drawn})

        # The payload rides on the row band's first Text primitive, which is its
        # REV cell -- `rev` is validated non-empty, so that cell is always drawn.
        row_handles = block_handles[len(head_prims) :]
        row_handle = next(
            handle
            for handle, prim in zip(row_handles, row_prims, strict=True)
            if isinstance(prim, Text)
        )
        await backend.entity_set_xdata(
            row_handle,
            REV_APP_ID,
            rev_values(
                {
                    "v": REV_PAYLOAD_VERSION,
                    "rev": code,
                    "description": description,
                    "date": date,
                    "by": by,
                }
            ),
        )

        return {
            "ok": True,
            "created": True,
            "rev": code,
            "handle": row_handle,
            "row": index + 1,
            "block_handles": block_handles,
            "clouds": cloud_handles,
            "tags": tag_handles,
            "layout": layout or "Model",
            "bbox": {
                "min": [anchor[0], anchor[1] - ROW_HEIGHT * (index + 2)],
                "max": [anchor[0] + LIST_WIDTH, anchor[1]],
            },
        }
    finally:
        if layout is not None:
            await backend.layout_set_current(previous)
