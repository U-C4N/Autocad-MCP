"""The six mechanical critique focuses (spec §13).

Every check is pure over one index built once per critique run and shared
through ``run_critique``'s context, so six focuses cost one read of the
drawing. A drawing with no ``ACADMCP_MECH`` payload on it yields no issues at
all: a P&ID sheet or a plain DXF must not be scored against mechanical
conventions, and ``focus=None`` has to stay cheap for them.

What the index reads, and why only that: every mechanical payload this server
writes lands on a POINT (``mech_part_draw``'s part anchor, on layer ``MECH``),
an INSERT (a standard part; BLOCKREFERENCE on the live engine) or a CIRCLE (a
balloon is a circle with a leader), so XDATA is read for those types only. The
rest of the drawing comes from a paged ``entity_list`` -- every entity, not the
first 200 -- whose ``EntityInfo`` already carries each entity's WCS
``bounding_box`` on both engines.

A part payload written by ``mech_part_draw`` lists every view it drew under
``views``, each with its WCS ``bbox`` and its ``frame_bbox`` (the same box in
the unrotated sheet frame the views are laid out in). The index expands that
list into one view entry per drawn view, so the section check looks inside the
section's own box and the alignment check compares boxes in the frame the
projection rule is stated in. A payload with no ``views`` list stands for one
view (its ``view`` key) at its anchor.

Two narrowings are deliberate, and neither is hidden:

* ``mech_view_misaligned`` checks the projection *axis*, not the quadrant. The
  pinned part payload records the view kind and the part id — not the
  projection angle, and not which side a side view was taken from — so the
  quadrant rule has nothing true to read. Guessing it would report a correct
  drawing as wrong, or the reverse.
* ``mech_duplicate_dimension`` reports two dimensions of the same type that
  occupy the same place: the failure a second run of ``dimension_auto``, or a
  manual dimension over an automatic one, produces. A semantic restatement (a
  chain that also carries the overall length) is not detected, because no
  engine exposes a dimension's measured feature.

Where a box has to be guessed at all — the region of a view — it is guessed
*generously*. A check that misses a bad drawing costs one review; a check that
calls a correct drawing bad gets switched off.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from engineering.plan_spec import Issue

from .primitives import ROLE_LAYER
from .standards.threads import MINOR_RATIO
from .xdata import APP_ID, decode

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

MECH_FOCUSES = (
    "mech_missing_centreline",
    "mech_unhatched_section",
    "mech_view_misaligned",
    "mech_duplicate_dimension",
    "mech_thread_unrepresented",
    "mech_bom_balloon_mismatch",
)
_KEY = "mech_index"

#: The entity types that can carry an ACADMCP_MECH payload. The live engine
#: names a type from its ActiveX ObjectName (``AcDb`` stripped, upper-cased), so
#: an INSERT reads back as BLOCKREFERENCE there -- measured on AutoCAD 2026.
_XDATA_TYPES = ("POINT", "INSERT", "BLOCKREFERENCE", "CIRCLE")
#: One ``entity_list`` page; the index pages until a short page.
PAGE_SIZE = 1000
#: Half a millimetre is visible on a printed sheet.
ALIGN_TOL_MM = 0.5
#: How close a CENTER line must pass to a circle's centre to be its mark.
CENTRE_TOL_MM = 0.5
#: ISO 6410 draws a thread's minor diameter at 0.8 x the nominal -- the same
#: constant the view engine draws it with, so the check and the drawing agree.
THREAD_MINOR_RATIO = MINOR_RATIO
#: Relative tolerance when matching drawn geometry against a computed diameter.
DIAMETER_TOL = 0.02
#: Padding added to a view's computed extents before looking inside it.
VIEW_PAD_MM = 10.0

_DIM_TYPES = {
    "DIMENSION",
    "DIMLINEAR",
    "DIMALIGNED",
    "DIMANGULAR",
    "DIMRADIUS",
    "DIMDIAMETER",
    "DIMORDINATE",
}


def _is_dimension(ent) -> bool:
    """A DIMENSION on either engine: ezdxf says ``DIMENSION``; the live engine
    says ``ROTATEDDIMENSION`` / ``ALIGNEDDIMENSION`` / ``RADIALDIMENSION`` ...
    (the ObjectName, measured on AutoCAD 2026)."""
    kind = str(ent.type).upper()
    return kind in _DIM_TYPES or kind.endswith("DIMENSION")


_METRIC_THREAD = re.compile(r"^M\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


# ── the index ───────────────────────────────────────────────────────────────


def _anchor_point(ent) -> tuple[float, float] | None:
    props = getattr(ent, "properties", None) or {}
    for key in ("insertion", "center"):
        value = props.get(key)
        if value and len(value) >= 2:
            return (float(value[0]), float(value[1]))
    box = props.get("bounding_box")
    if box:
        return (
            (float(box["min"][0]) + float(box["max"][0])) / 2.0,
            (float(box["min"][1]) + float(box["max"][1])) / 2.0,
        )
    return None


def _bbox_center(ent) -> tuple[float, float] | None:
    box = (getattr(ent, "properties", None) or {}).get("bounding_box")
    if not box:
        return None
    return (
        (float(box["min"][0]) + float(box["max"][0])) / 2.0,
        (float(box["min"][1]) + float(box["max"][1])) / 2.0,
    )


async def _all_entities(backend: AutoCADBackend) -> list:
    """Every entity of the current space, paged -- ``entity_list`` defaults to 200.

    Stops on a short page, and on a page that brings nothing new (a backend
    that ignored ``offset`` would otherwise return the same page for ever).
    """
    found: list = []
    seen: set[str] = set()
    offset = 0
    while True:
        page = await backend.entity_list(limit=PAGE_SIZE, offset=offset)
        fresh = [ent for ent in page if ent.handle not in seen]
        if not fresh:
            return found
        seen.update(ent.handle for ent in fresh)
        found.extend(fresh)
        if len(page) < PAGE_SIZE:
            return found
        offset += len(page)


def _box(value) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _centre(box) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _views_of(record: dict) -> list[dict]:
    """The views one part payload stands for (see the module docstring)."""
    payload = record["payload"]
    part_id = str(payload.get("id") or record["handle"])
    drawn = payload.get("views")
    if not (isinstance(drawn, list) and drawn):
        return [
            {
                "handle": record["handle"],
                "part_id": part_id,
                "kind": _view_kind(record),
                "index": 0,
                "at": record["at"],
                "box": _view_box(record),
            }
        ]
    out: list[dict] = []
    for position, view in enumerate(drawn):
        if not isinstance(view, dict):
            continue
        wcs = _box(view.get("bbox"))
        frame = _box(view.get("frame_bbox")) or wcs
        if wcs is None or frame is None:
            continue
        out.append(
            {
                "handle": record["handle"],
                "part_id": part_id,
                "kind": str(view.get("kind") or "").split(":")[0].strip().lower(),
                "index": position,
                "at": _centre(frame),
                "box": (
                    wcs[0] - VIEW_PAD_MM,
                    wcs[1] - VIEW_PAD_MM,
                    wcs[2] + VIEW_PAD_MM,
                    wcs[3] + VIEW_PAD_MM,
                ),
            }
        )
    return out


async def build_index(backend: AutoCADBackend) -> dict:
    """One read of the drawing: every entity, plus every ACADMCP_MECH payload."""
    try:
        entities = await _all_entities(backend)
    except Exception:
        entities = []
    records: list[dict] = []
    for ent in entities:
        if str(ent.type).upper() not in _XDATA_TYPES:
            continue
        try:
            raw = await backend.entity_get_xdata(ent.handle, APP_ID)
        except Exception:
            continue
        values = (raw.get("xdata") or {}).get(APP_ID) or []
        if not values:
            continue
        try:
            payload = decode(values)
        except ValueError:
            # A foreign or corrupt payload is not mechanical content; the codec
            # already refused it, and guessing at it here would undo that.
            continue
        at = _anchor_point(ent)
        if at is None:
            continue
        records.append({"handle": ent.handle, "payload": payload, "at": at})
    views = [
        view
        for record in records
        if record["payload"].get("kind") == "part"
        for view in _views_of(record)
    ]
    return {"entities": entities, "records": records, "views": views}


def has_mech_content(index: dict) -> bool:
    """True when at least one entity carries an ACADMCP_MECH payload."""
    return bool(index.get("records"))


def _of_kind(index: dict, kind: str) -> list[dict]:
    return [r for r in index["records"] if r["payload"].get("kind") == kind]


def _view_kind(record: dict) -> str:
    return str(record["payload"].get("view") or "").split(":")[0].strip().lower()


def _extents(part: dict) -> tuple[float, float] | None:
    """``(length, height)`` of a part model, read defensively.

    The pinned ``Segment`` is a NamedTuple ``(length, d_outer, d_inner,
    taper_to)``, so a serialised segment is either a mapping or that sequence.
    Both are read rather than assuming which one ``part_to_dict`` chose.
    """
    if not isinstance(part, dict):
        return None
    segments = part.get("segments")
    if segments:
        length = 0.0
        dmax = 0.0
        for seg in segments:
            if isinstance(seg, dict):
                run = float(seg.get("length", 0.0) or 0.0)
                outer = float(seg.get("d_outer", 0.0) or 0.0)
                taper = seg.get("taper_to")
            else:
                row = list(seg)
                run = float(row[0]) if row else 0.0
                outer = float(row[1]) if len(row) > 1 else 0.0
                taper = row[3] if len(row) > 3 else None
            length += run
            dmax = max(dmax, outer, float(taper) if taper else 0.0)
        return (length, dmax)
    outline = part.get("outline")
    if outline:
        xs = [float(p[0]) for p in outline]
        ys = [float(p[1]) for p in outline]
        return (max(xs) - min(xs), max(ys) - min(ys))
    return None


def _view_box(record: dict) -> tuple[float, float, float, float] | None:
    """A deliberately generous box around a view's anchor.

    The anchor may be the view's origin or its centre depending on how the
    view was placed, so the box spans the part's full extents on *both* sides
    plus a pad. It answers only "is there any hatch near this section", which
    is the question that matters.
    """
    extents = _extents(record["payload"].get("part") or {})
    if extents is None:
        return None
    x, y = record["at"]
    half_w = extents[0] + VIEW_PAD_MM
    half_h = max(extents[1], extents[0]) + VIEW_PAD_MM
    return (x - half_w, y - half_h, x + half_w, y + half_h)


def _inside(point, box) -> bool:
    if point is None or box is None:
        return False
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _distance_to_segment(point, start, end) -> float:
    if not start or not end:
        return math.inf
    px, py = point
    ax, ay = float(start[0]), float(start[1])
    bx, by = float(end[0]), float(end[1])
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


# ── the six checks ──────────────────────────────────────────────────────────


def _missing_centreline(index: dict) -> list[Issue]:
    """ISO 128-23: a circular feature carries a centre mark."""
    geometry = {ROLE_LAYER["visible"].upper(), ROLE_LAYER["hidden"].upper()}
    centre_layer = ROLE_LAYER["center"].upper()
    marks = [
        e
        for e in index["entities"]
        if str(e.type).upper() == "LINE" and str(e.layer).upper() == centre_layer
    ]
    out: list[Issue] = []
    for ent in index["entities"]:
        if str(ent.type).upper() != "CIRCLE" or str(ent.layer).upper() not in geometry:
            continue
        centre = (ent.properties or {}).get("center")
        if not centre:
            continue
        point = (float(centre[0]), float(centre[1]))
        crossing = [
            m
            for m in marks
            if _distance_to_segment(
                point, (m.properties or {}).get("start"), (m.properties or {}).get("end")
            )
            <= CENTRE_TOL_MM
        ]
        if len(crossing) < 2:
            out.append(
                Issue(
                    "warning",
                    "mech_missing_centreline",
                    f"Circle {ent.handle} at ({point[0]:.3f}, {point[1]:.3f}) has no ISO "
                    f"128-23 centre mark ({len(crossing)} of the two crossing "
                    f"{centre_layer} lines found).",
                    [ent.handle],
                    {"hint": "centre_marks(handles=[...]) draws them", "found": len(crossing)},
                )
            )
    return out


def _unhatched_section(index: dict) -> list[Issue]:
    hatches = [e for e in index["entities"] if str(e.type).upper() == "HATCH"]
    out: list[Issue] = []
    for view in index.get("views", ()):
        if view["kind"] != "section":
            continue
        if any(_inside(_bbox_center(h), view["box"]) for h in hatches):
            continue
        out.append(
            Issue(
                "error",
                "mech_unhatched_section",
                f"Section view {view['index']} of part {view['part_id']!r} "
                f"({view['handle']}) has no hatched cut face; ISO 128-50 hatches every "
                "cut surface.",
                [view["handle"]],
                {
                    "hint": "hatch_material(handles=[...], material=...)",
                    "hatches": len(hatches),
                    "view": view["index"],
                },
            )
        )
    return out


def _view_misaligned(index: dict) -> list[Issue]:
    out: list[Issue] = []
    by_part: dict[str, list[dict]] = {}
    for view in index.get("views", ()):
        by_part.setdefault(view["part_id"], []).append(view)
    for part_id, records in sorted(by_part.items()):
        fronts = [r for r in records if r["kind"] == "front"]
        if not fronts:
            continue
        front = fronts[0]
        fx, fy = front["at"]
        for record in records:
            kind = record["kind"]
            x, y = record["at"]
            if kind in ("side", "section"):
                delta = abs(y - fy)
                axis = "y"
            elif kind == "top":
                delta = abs(x - fx)
                axis = "x"
            else:
                continue
            if delta <= ALIGN_TOL_MM:
                continue
            out.append(
                Issue(
                    "warning",
                    "mech_view_misaligned",
                    f"The {kind} view {record['handle']} of part {part_id!r} sits "
                    f"{delta:.3f} mm off the projection axis of its front view "
                    f"{front['handle']} (view {record['index']} of the part).",
                    list(dict.fromkeys((record["handle"], front["handle"]))),
                    {
                        "axis": axis,
                        "delta_mm": round(delta, 4),
                        "tolerance_mm": ALIGN_TOL_MM,
                    },
                )
            )
    return out


def _duplicate_dimension(index: dict) -> list[Issue]:
    seen: dict[tuple, str] = {}
    out: list[Issue] = []
    for ent in index["entities"]:
        if not _is_dimension(ent):
            continue
        props = ent.properties or {}
        box = props.get("bounding_box")
        if not box:
            continue
        defpoint = props.get("defpoint")
        key = (
            str(ent.type).upper(),
            tuple(
                round(float(v), 3)
                for v in (box["min"][0], box["min"][1], box["max"][0], box["max"][1])
            ),
            tuple(round(float(v), 3) for v in defpoint[:2]) if defpoint else None,
        )
        if key in seen:
            out.append(
                Issue(
                    "warning",
                    "mech_duplicate_dimension",
                    f"Dimensions {seen[key]} and {ent.handle} occupy the same place and "
                    "measure the same feature; ISO 129-1 gives each feature one dimension.",
                    [seen[key], ent.handle],
                    {"hint": "delete one, or dimension the feature in a single view"},
                )
            )
        else:
            seen[key] = ent.handle
    return out


def _thread_diameter(feature: dict) -> float | None:
    for key in ("d", "diameter", "nominal", "nominal_mm"):
        value = feature.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    match = _METRIC_THREAD.match(str(feature.get("designation") or ""))
    return float(match.group(1)) if match else None


def _has_diameter(index: dict, target: float) -> bool:
    """Is anything on the sheet drawn at this diameter?

    A round feature answers as a CIRCLE or ARC of half the diameter; the axial
    view answers as two horizontal lines that far apart.
    """
    tol = max(DIAMETER_TOL * target, 1e-6)
    horizontals: list[float] = []
    for ent in index["entities"]:
        kind = str(ent.type).upper()
        props = ent.properties or {}
        if kind in ("CIRCLE", "ARC"):
            radius = props.get("radius")
            if radius is not None and abs(2.0 * float(radius) - target) <= tol:
                return True
        elif kind == "LINE":
            start, end = props.get("start"), props.get("end")
            if start and end and abs(float(start[1]) - float(end[1])) <= 1e-6:
                horizontals.append(float(start[1]))
    for i, y1 in enumerate(horizontals[:500]):
        for y2 in horizontals[i + 1 : 500]:
            if abs(abs(y1 - y2) - target) <= tol:
                return True
    return False


def _thread_unrepresented(index: dict) -> list[Issue]:
    out: list[Issue] = []
    for record in _of_kind(index, "part"):
        part = record["payload"].get("part") or {}
        for feature in part.get("features") or ():
            if not isinstance(feature, dict):
                continue
            if not str(feature.get("kind") or "").lower().startswith("thread"):
                continue
            nominal = _thread_diameter(feature)
            if nominal is None:
                continue
            minor = THREAD_MINOR_RATIO * nominal
            # ISO 6410: the thin line is the minor diameter of an external
            # thread and the major diameter of an internal one (a tapped
            # hole's drilled bore is the minor, drawn thick).
            internal = bool(feature.get("internal"))
            thin = nominal if internal else minor
            if _has_diameter(index, thin):
                continue
            name = feature.get("id") or feature.get("designation") or "thread"
            which = (
                f"major diameter ({nominal:.3f} mm)"
                if internal
                else f"minor diameter ({minor:.3f} mm = {THREAD_MINOR_RATIO:g} x {nominal:.3f} mm)"
            )
            out.append(
                Issue(
                    "error",
                    "mech_thread_unrepresented",
                    f"Thread {name!r} on part {record['payload'].get('id')!r} is drawn "
                    f"without its ISO 6410 thin-line {which}.",
                    [record["handle"]],
                    {
                        "nominal_mm": round(nominal, 4),
                        "minor_mm": round(minor, 4),
                        "internal": internal,
                    },
                )
            )
    return out


def _bom_balloon_mismatch(index: dict) -> list[Issue]:
    out: list[Issue] = []
    balloons = _of_kind(index, "balloon")
    parts = _of_kind(index, "std_part")
    if not balloons and not parts:
        return out
    part_handles = {r["handle"] for r in parts} | {r["handle"] for r in _of_kind(index, "part")}
    balloon_targets: set[str] = set()
    by_item: dict[str, list[dict]] = {}
    for record in balloons:
        item = record["payload"].get("item")
        targets = [str(t) for t in (record["payload"].get("targets") or [])]
        by_item.setdefault(str(item), []).append(record)
        resolved = [t for t in targets if t in part_handles]
        balloon_targets.update(resolved)
        if not resolved:
            out.append(
                Issue(
                    "error",
                    "mech_bom_balloon_mismatch",
                    f"Balloon {item!r} ({record['handle']}) points at "
                    f"{targets or 'nothing'}, which is not a part on this sheet.",
                    [record["handle"]],
                    {"targets": targets},
                )
            )
    for item, records in sorted(by_item.items()):
        if len(records) > 1:
            out.append(
                Issue(
                    "warning",
                    "mech_bom_balloon_mismatch",
                    f"Item number {item} is carried by {len(records)} balloons; ISO 6433 "
                    "gives one item reference per parts-list row.",
                    [r["handle"] for r in records],
                    {"item": item, "count": len(records)},
                )
            )
    if balloons:
        # ISO 6433 balloons a parts-list *row*, not every instance: four
        # identical bolts share one item reference, so a part is covered when
        # any balloon points at a part of the same designation.
        covered = {
            str(r["payload"].get("designation")) for r in parts if r["handle"] in balloon_targets
        }
        for record in parts:
            if record["handle"] in balloon_targets:
                continue
            if str(record["payload"].get("designation")) in covered:
                continue
            out.append(
                Issue(
                    "warning",
                    "mech_bom_balloon_mismatch",
                    f"{record['payload'].get('designation')} ({record['handle']}) has no "
                    "balloon; ISO 7573 gives every parts-list row an item reference.",
                    [record["handle"]],
                    {"designation": record["payload"].get("designation")},
                )
            )
    inserted: dict[str, int] = {}
    declared: dict[str, int] = {}
    for record in parts:
        designation = str(record["payload"].get("designation"))
        inserted[designation] = inserted.get(designation, 0) + 1
        declared[designation] = declared.get(designation, 0) + int(
            record["payload"].get("qty") or 1
        )
    for designation, count in sorted(inserted.items()):
        if declared[designation] != count:
            out.append(
                Issue(
                    "warning",
                    "mech_bom_balloon_mismatch",
                    f"{designation} is inserted {count} time(s) but its XDATA declares "
                    f"{declared[designation]}.",
                    [r["handle"] for r in parts if r["payload"].get("designation") == designation],
                    {"inserted": count, "declared": declared[designation]},
                )
            )
    return out


_CHECKS = {
    "mech_missing_centreline": _missing_centreline,
    "mech_unhatched_section": _unhatched_section,
    "mech_view_misaligned": _view_misaligned,
    "mech_duplicate_dimension": _duplicate_dimension,
    "mech_thread_unrepresented": _thread_unrepresented,
    "mech_bom_balloon_mismatch": _bom_balloon_mismatch,
}


def issues_for(focus: str, index: dict) -> list[Issue]:
    if not has_mech_content(index):
        return []
    return _CHECKS[focus](index)


def _make(focus: str):
    async def check(backend: AutoCADBackend, shared: dict) -> list[Issue]:
        index = shared.get(_KEY)
        if index is None:
            index = shared[_KEY] = await build_index(backend)
        return issues_for(focus, index)

    check.needs_shared = True  # type: ignore[attr-defined]
    check.__name__ = f"check_{focus}"
    return check


MECH_DISPATCH = {focus: _make(focus) for focus in MECH_FOCUSES}
