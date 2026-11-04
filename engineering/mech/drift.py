"""Model drift: is a drawn part still exactly what its stored model draws? (v1.8.0, 1.8-19)

``mech_part_draw`` writes the part model onto its anchor and tags every entity
it draws with ``{part, feature, view, i}`` - ``i`` the entity's place in its
view's primitive list. This module reads one snapshot of the drawing, rebuilds
every view of the stored model with the view engine, and compares each tagged
entity's **real geometry** with the primitive it was drawn from, within the
verification epsilon (``engineering/verification/measure.py``:
``RELATIVE_EPSILON`` of the largest coordinate, ``ANGLE_EPSILON_DEG`` for an
arc's angles). It is the read-only face of ``mech_part_revise``, which refuses
to rebuild over a hand edit unless told to.

Each tagged entity gets one row, ``{feature, view, type, i, handle, expected,
measured, status}``:

* ``ok`` - the entity is where the model draws it;
* ``moved`` - same view, slot, type and feature, other geometry (a hand edit);
* ``duplicate`` - a second entity claims a slot already matched (a copy
  carries its original's tag);
* ``unexpected`` - a tag naming a slot the model does not draw;
* ``missing`` - a primitive the model draws that no entity carries (deleted);
* ``untracked`` - a tag without ``i`` (drawn before 1.8.0): it cannot be
  matched to a primitive at all.

What is *not* geometry is not drift: a layer, a colour or a linetype changed
by hand is kept by a revision and never reported here. A HATCH is compared by
the box of its boundary (its pattern is not on the snapshot record).

Two facts about the payload sit above the rows. ``stamped``: the payload
carries ``gen`` (written since 1.8.0) and every view record its ``sha`` - the
digest of the primitives it was drawn from. ``engine_drift``: views whose
stored digest is not what the view engine draws from the same model today, so
the rows of that view cannot tell a hand edit from an engine change. An
unstamped payload or an engine drift is ``model_drift`` to the revision.

The ``model_drift`` critique focus (mechanical domain) reports the same: one
warning per part whose tagged entities drifted (``hand_edited_tagged_entity``)
or whose views the engine no longer reproduces (``model_drift``), and an
``info`` for a part drawn before 1.8.0 (``unstamped``), whose drift cannot be
read. Silent (``domain_absent``) on a drawing with no part anchor.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engineering.mech.primitives import (
    Arc,
    Circle,
    HatchArea,
    Line,
    Poly,
    Text,
    bbox,
    layer_for,
    prims_digest,
)
from engineering.mech.xdata import APP_ID
from engineering.verification.measure import ANGLE_EPSILON_DEG, RELATIVE_EPSILON

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

FOCUS = "model_drift"

#: Drift statuses, in the order a report counts them.
STATUSES: tuple[str, ...] = ("ok", "moved", "duplicate", "unexpected", "missing", "untracked")

#: The primitive class -> the DXF type the entity it draws reads back as.
DXF_TYPE: dict[type, str] = {
    Line: "LINE",
    Arc: "ARC",
    Circle: "CIRCLE",
    Poly: "LWPOLYLINE",
    Text: "TEXT",
    HatchArea: "HATCH",
}

#: A HATCH box is compared at this share of the drawing's size: its boundary
#: is read back through the engine's own path reader, not as the loops given.
HATCH_RELATIVE_EPSILON = 1e-6

_ROUND = 6


def record_view_tag(record: dict) -> str:
    """The view tag a record's entities carry (``draw._view_tag`` from a record)."""
    label = record.get("label")
    return f"{record['kind']}:{label}" if label else str(record["kind"])


def prim_type(prim) -> str:
    return DXF_TYPE[type(prim)]


@dataclass(frozen=True)
class Expected:
    """One primitive of a stored view, in WCS, and the slot it was drawn in."""

    view: str
    index: int
    prim: Any
    record: int
    marker: bool = False

    @property
    def type(self) -> str:
        return prim_type(self.prim)

    @property
    def feature(self) -> str:
        return str(getattr(self.prim, "feature", "") or "")


@dataclass(frozen=True)
class Tagged:
    """One entity carrying this part's feature tag, as the snapshot read it."""

    handle: str
    type: str
    layer: str
    view: str
    feature: str
    index: int | None
    record: Any
    tag: dict


@dataclass
class DriftReport:
    part_id: str
    stamped: bool
    engine_drift: list[str]
    unstamped_views: list[str]
    epsilon: float
    rows: list[dict]
    expected: list[Expected]
    tagged: list[Tagged]
    #: (view, index, marker) of an expected primitive -> the entity matched to it
    matched: dict[tuple, Tagged] = field(default_factory=dict)

    @property
    def drifted(self) -> list[dict]:
        return [row for row in self.rows if row["status"] != "ok"]

    @property
    def hand_edited_handles(self) -> list[str]:
        return sorted(
            {row["handle"] for row in self.drifted if row.get("handle")},
            key=lambda h: (len(h), h),
        )

    @property
    def model_is_current(self) -> bool:
        return self.stamped and not self.engine_drift and not self.drifted

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATUSES, 0)
        for row in self.rows:
            out[row["status"]] += 1
        return out

    def to_dict(self, *, limit: int | None = None) -> dict:
        drifted = self.drifted
        shown = drifted if limit is None else drifted[:limit]
        return {
            "part_id": self.part_id,
            "model_is_current": self.model_is_current,
            "stamped": self.stamped,
            "engine_drift": list(self.engine_drift),
            "unstamped_views": list(self.unstamped_views),
            "counts": self.counts(),
            "epsilon": self.epsilon,
            "drift": shown,
            "drift_total": len(drifted),
        }


# -- the stored views, rebuilt --------------------------------------------------


def expected_prims(part, payload: dict) -> tuple[list[Expected], list[dict]]:
    """Every primitive the stored model draws, view by view, in WCS.

    Returns ``(expected, views)``; ``views`` has one row per record:
    ``{index, view, sha, stored}`` - the digest the engine gives today and the
    one the record carries (None before 1.8.0). A detail view's marker circle
    and letter, drawn into the first view in their own call, are expected
    there too (``marker=True``).
    """
    from engineering.mech.draw import _placement, _to_wcs, _view_from_record
    from engineering.mech.views import detail_marker_prims

    origin, rotation = _placement(payload)
    records = list(payload.get("views") or [])
    out: list[Expected] = []
    views: list[dict] = []
    for number, record in enumerate(records):
        view = _view_from_record(part, payload, record)
        wcs = _to_wcs(view.prims, record["offset"], origin, rotation)
        tag = record_view_tag(record)
        out.extend(Expected(tag, i, prim, number) for i, prim in enumerate(wcs))
        views.append(
            {"index": number, "view": tag, "sha": prims_digest(wcs), "stored": record.get("sha")}
        )
        if record.get("kind") == "detail" and records:
            detail = (record.get("kw") or {}).get("detail") or {}
            marker = _to_wcs(detail_marker_prims(detail), records[0]["offset"], origin, rotation)
            parent = str(records[0]["kind"])
            out.extend(Expected(parent, i, prim, 0, marker=True) for i, prim in enumerate(marker))
    return out, views


def epsilon_for(prims) -> float:
    """The verification epsilon over these primitives: ``RELATIVE_EPSILON`` of
    the largest absolute coordinate, never below ``RELATIVE_EPSILON``."""
    largest = 1.0
    for prim in prims:
        try:
            box = bbox([prim])
        except (TypeError, ValueError):
            continue
        largest = max(largest, *(abs(v) for v in box))
        if isinstance(prim, (Arc, Circle)):
            largest = max(largest, abs(prim.radius))
    return RELATIVE_EPSILON * largest


# -- the tagged entities ----------------------------------------------------------


def _payload_of(rec) -> dict | None:
    text = dict(rec.xdata).get(APP_ID)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def tagged_entities(records, part_id: str) -> list[Tagged]:
    """Every entity tagged as geometry of ``part_id`` (not its dimensions or table)."""
    out: list[Tagged] = []
    for rec in records:
        payload = _payload_of(rec)
        if payload is None or payload.get("kind") != "feature":
            continue
        if str(payload.get("part", "")) != str(part_id) or payload.get("role"):
            continue
        raw = payload.get("i")
        index = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        out.append(
            Tagged(
                handle=str(rec.handle),
                type=str(rec.type),
                layer=str(rec.layer),
                view=str(payload.get("view", "")),
                feature=str(payload.get("feature", "") or ""),
                index=index,
                record=rec,
                tag=payload,
            )
        )
    return out


def annotations_of(records, part_id: str, role: str | None = None) -> list[tuple[Any, dict]]:
    """``(record, tag)`` of every annotation of ``part_id`` (``role`` set), or of one role."""
    out = []
    for rec in records:
        payload = _payload_of(rec)
        if payload is None or payload.get("kind") != "feature":
            continue
        if str(payload.get("part", "")) != str(part_id) or not payload.get("role"):
            continue
        if role is not None and payload.get("role") != role:
            continue
        out.append((rec, payload))
    return out


def part_anchors(records) -> list[tuple[Any, dict]]:
    """``(record, payload)`` of every part anchor the snapshot holds."""
    out = []
    for rec in records:
        payload = _payload_of(rec)
        if payload is not None and payload.get("kind") == "part":
            out.append((rec, payload))
    return out


# -- geometry ---------------------------------------------------------------------


def _r(value: float) -> float:
    return round(float(value), _ROUND) + 0.0


def _pt(point) -> list[float]:
    return [_r(point[0]), _r(point[1])]


def prim_geometry(prim) -> dict:
    """The compact geometry a drift row reports as ``expected``."""
    if isinstance(prim, Line):
        return {"start": _pt(prim.p1), "end": _pt(prim.p2)}
    if isinstance(prim, Arc):
        return {
            "center": _pt(prim.center),
            "radius": _r(prim.radius),
            "start_angle": _r(prim.start_deg % 360.0),
            "end_angle": _r(prim.end_deg % 360.0),
        }
    if isinstance(prim, Circle):
        return {"center": _pt(prim.center), "radius": _r(prim.radius)}
    if isinstance(prim, Poly):
        return {"points": [_pt(p) for p in prim.points], "closed": bool(prim.closed)}
    if isinstance(prim, Text):
        return {"at": _pt(prim.at), "text": prim.text}
    if isinstance(prim, HatchArea):
        return {"bbox": [_r(v) for v in bbox([prim])]}
    raise TypeError(f"prim_geometry: not a primitive: {prim!r}")


def record_geometry(rec) -> dict:
    """The compact geometry a drift row reports as ``measured``."""
    kind = str(rec.type)
    points = list(rec.points or ())
    if kind == "LINE" and len(points) == 2:
        return {"start": _pt(points[0]), "end": _pt(points[1])}
    if kind == "ARC" and points and rec.angles:
        return {
            "center": _pt(points[0]),
            "radius": _r(rec.radius or 0.0),
            "start_angle": _r(rec.angles[0] % 360.0),
            "end_angle": _r(rec.angles[1] % 360.0),
        }
    if kind == "CIRCLE" and points:
        return {"center": _pt(points[0]), "radius": _r(rec.radius or 0.0)}
    if kind in ("LWPOLYLINE", "POLYLINE"):
        return {"points": [_pt(p) for p in points], "closed": bool(rec.closed)}
    if kind in ("TEXT", "MTEXT") and points:
        return {"at": _pt(points[0]), "text": rec.text}
    if kind == "HATCH":
        return {"bbox": None if rec.bbox is None else [_r(v) for v in rec.bbox]}
    return {"type": kind}


def _near(a, b, eps: float) -> bool:
    return math.dist((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))) <= eps


def _angle_near(a: float, b: float) -> bool:
    return abs(((float(a) - float(b) + 180.0) % 360.0) - 180.0) <= ANGLE_EPSILON_DEG


def matches(rec, prim, eps: float, *, hatch_eps: float | None = None) -> bool:
    """Does the entity a snapshot record describes have the primitive's geometry?"""
    kind = str(rec.type)
    if kind != prim_type(prim):
        return False
    points = list(rec.points or ())
    if isinstance(prim, Line):
        if len(points) != 2:
            return False
        a, b = points
        return (_near(a, prim.p1, eps) and _near(b, prim.p2, eps)) or (
            _near(a, prim.p2, eps) and _near(b, prim.p1, eps)
        )
    if isinstance(prim, Circle):
        return (
            bool(points)
            and _near(points[0], prim.center, eps)
            and (rec.radius is not None and abs(float(rec.radius) - prim.radius) <= eps)
        )
    if isinstance(prim, Arc):
        if not points or rec.radius is None or not rec.angles:
            return False
        return (
            _near(points[0], prim.center, eps)
            and abs(float(rec.radius) - prim.radius) <= eps
            and _angle_near(rec.angles[0], prim.start_deg)
            and _angle_near(rec.angles[1], prim.end_deg)
        )
    if isinstance(prim, Poly):
        if len(points) != len(prim.points) or bool(rec.closed) != bool(prim.closed):
            return False
        return all(_near(a, b, eps) for a, b in zip(points, prim.points, strict=True))
    if isinstance(prim, Text):
        return (
            bool(points)
            and _near(points[0], prim.at, eps)
            and str(rec.text or "") == str(prim.text)
            and (rec.height is None or abs(float(rec.height) - prim.height) <= eps)
        )
    if isinstance(prim, HatchArea):
        if rec.bbox is None:
            # a boundary the engine's reader could not box: not evidence of an
            # edit either way, and a refusal must rest on evidence
            return True
        tol = hatch_eps if hatch_eps is not None else eps
        return all(
            abs(float(a) - float(b)) <= tol for a, b in zip(rec.bbox, bbox([prim]), strict=True)
        )
    return False


def same_prim(a, b, eps: float) -> bool:
    """Two primitives the engine emitted are the same drawing instruction."""
    if type(a) is not type(b):
        return False
    if layer_for(a) != layer_for(b) or getattr(a, "feature", "") != getattr(b, "feature", ""):
        return False
    if isinstance(a, HatchArea):
        if a.material != b.material or len(a.loops) != len(b.loops):
            return False
        return all(
            len(la) == len(lb) and all(_near(p, q, eps) for p, q in zip(la, lb, strict=True))
            for la, lb in zip(a.loops, b.loops, strict=True)
        )
    if isinstance(a, Text):
        return (
            a.text == b.text
            and _near(a.at, b.at, eps)
            and abs(a.height - b.height) <= eps
            and _angle_near(a.rotation, b.rotation)
        )
    if isinstance(a, Poly):
        return (
            a.closed == b.closed
            and len(a.points) == len(b.points)
            and all(_near(p, q, eps) for p, q in zip(a.points, b.points, strict=True))
        )
    if isinstance(a, Line):
        return _near(a.p1, b.p1, eps) and _near(a.p2, b.p2, eps)
    if isinstance(a, Circle):
        return _near(a.center, b.center, eps) and abs(a.radius - b.radius) <= eps
    if isinstance(a, Arc):
        return (
            _near(a.center, b.center, eps)
            and abs(a.radius - b.radius) <= eps
            and _angle_near(a.start_deg, b.start_deg)
            and _angle_near(a.end_deg, b.end_deg)
        )
    return False


# -- the comparison ---------------------------------------------------------------


def _row(status: str, *, expected: Expected | None = None, entity: Tagged | None = None) -> dict:
    return {
        "feature": expected.feature if expected else (entity.feature if entity else ""),
        "view": expected.view if expected else (entity.view if entity else ""),
        "type": expected.type if expected else (entity.type if entity else ""),
        "i": expected.index if expected else (entity.index if entity else None),
        "handle": entity.handle if entity else None,
        "expected": prim_geometry(expected.prim) if expected else None,
        "measured": record_geometry(entity.record) if entity else None,
        "status": status,
    }


def _slot(expected: Expected) -> tuple:
    return (expected.view, expected.index, expected.marker)


def compare(part_id: str, part, payload: dict, records) -> DriftReport:
    """The drift of one part: its stored views rebuilt and matched to the snapshot."""
    expected, views = expected_prims(part, payload)
    eps = epsilon_for([e.prim for e in expected])
    largest = eps / RELATIVE_EPSILON
    hatch_eps = max(eps, HATCH_RELATIVE_EPSILON * largest)
    tagged = tagged_entities(records, part_id)

    stamped = payload.get("gen") is not None and all(v["stored"] for v in views)
    engine_drift = [v["view"] for v in views if v["stored"] and v["stored"] != v["sha"]]
    unstamped = [v["view"] for v in views if not v["stored"]]

    by_slot: dict[tuple, list[Expected]] = {}
    for item in expected:
        by_slot.setdefault((item.view, item.index), []).append(item)
    actual_by_slot: dict[tuple, list[Tagged]] = {}
    rows: list[dict] = []
    matched: dict[tuple, Tagged] = {}
    for entity in tagged:
        if entity.index is None:
            rows.append(_row("untracked", entity=entity))
            continue
        actual_by_slot.setdefault((entity.view, entity.index), []).append(entity)

    for slot in sorted(actual_by_slot, key=lambda s: (s[0], s[1])):
        entities = sorted(actual_by_slot[slot], key=lambda t: (len(t.handle), t.handle))
        open_slots = list(by_slot.get(slot, ()))
        unplaced: list[Tagged] = []
        for entity in entities:
            hit = next(
                (
                    c
                    for c in open_slots
                    if c.type == entity.type
                    and c.feature == entity.feature
                    and matches(entity.record, c.prim, eps, hatch_eps=hatch_eps)
                ),
                None,
            )
            if hit is None:
                unplaced.append(entity)
                continue
            open_slots.remove(hit)
            matched[_slot(hit)] = entity
            rows.append(_row("ok", expected=hit, entity=entity))
        for entity in unplaced:
            hit = next(
                (c for c in open_slots if c.type == entity.type and c.feature == entity.feature),
                None,
            )
            if hit is not None:
                open_slots.remove(hit)
                matched[_slot(hit)] = entity
                rows.append(_row("moved", expected=hit, entity=entity))
                continue
            claimed = any(
                c.type == entity.type and c.feature == entity.feature for c in by_slot.get(slot, ())
            )
            rows.append(_row("duplicate" if claimed else "unexpected", entity=entity))

    for item in expected:
        if _slot(item) not in matched:
            rows.append(_row("missing", expected=item))

    return DriftReport(
        part_id=str(part_id),
        stamped=stamped,
        engine_drift=engine_drift,
        unstamped_views=unstamped,
        epsilon=eps,
        rows=rows,
        expected=expected,
        tagged=tagged,
        matched=matched,
    )


async def inspect_drift(backend, part_id: str, *, snapshot=None) -> DriftReport:
    """Read the part's anchor and one snapshot, and compare (never writes)."""
    from engineering.mech.draw import read_part
    from engineering.mech.part import build_part
    from engineering.understand.snapshot import take_snapshot

    payload = await read_part(backend, part_id)
    part = build_part(payload["part"])
    snap = snapshot if snapshot is not None else await take_snapshot(backend)
    return compare(str(payload["part_id"]), part, payload, snap.records)


# -- the critique focus -------------------------------------------------------------


async def check_model_drift(backend: AutoCADBackend, shared: dict) -> list:
    """The ``model_drift`` focus: every part anchor of the run's snapshot, compared."""
    from engineering.critique import scan_incomplete_issue
    from engineering.mech.part import build_part
    from engineering.plan_spec import Issue
    from engineering.qa_coverage import DOCUMENT, domain_row, ledger_of
    from engineering.qa_scan import qa_snapshot

    snap, coverage = await qa_snapshot(backend, shared)
    if snap is None or not coverage.complete:
        ledger_of(shared).not_checked(FOCUS, DOCUMENT, "scan_incomplete")
        return [scan_incomplete_issue(FOCUS, coverage)]
    anchors = part_anchors(snap.records)
    domain_row(shared, FOCUS, present=bool(anchors), n=len(anchors))
    issues: list = []
    for rec, payload in anchors:
        part_id = str(payload.get("id") or rec.handle)
        try:
            part = build_part(payload.get("part") or {})
            report = compare(part_id, part, payload, snap.records)
        except ValueError as exc:
            issues.append(
                Issue(
                    "warning",
                    FOCUS,  # type: ignore[arg-type]
                    f"Part {part_id}: its stored model could not be rebuilt ({exc}); its drift "
                    "was not checked.",
                    [rec.handle],
                    {"code": "model_unreadable", "part_id": part_id, "repairable": False},
                )
            )
            continue
        if not report.stamped:
            issues.append(
                Issue(
                    "info",
                    FOCUS,  # type: ignore[arg-type]
                    f"Part {part_id} was drawn before 1.8.0 (no gen stamp): whether its "
                    "entities still match the model is not checked.",
                    [rec.handle],
                    {
                        "code": "unstamped",
                        "part_id": part_id,
                        "views": report.unstamped_views,
                        "repairable": False,
                    },
                )
            )
            continue
        if report.engine_drift:
            issues.append(
                Issue(
                    "warning",
                    FOCUS,  # type: ignore[arg-type]
                    f"Part {part_id}: the view engine no longer draws the stored model as "
                    f"drawn in view(s) {', '.join(report.engine_drift)}; a revision is refused "
                    "(model_drift) until mech_part_revise(overwrite=True) redraws it.",
                    [rec.handle],
                    {
                        "code": "model_drift",
                        "part_id": part_id,
                        "views": report.engine_drift,
                        "repairable": False,
                    },
                )
            )
            continue
        drifted = report.drifted
        if drifted:
            handles = report.hand_edited_handles
            issues.append(
                Issue(
                    "warning",
                    FOCUS,  # type: ignore[arg-type]
                    f"Part {part_id}: {len(drifted)} tagged entit"
                    f"{'y' if len(drifted) == 1 else 'ies'} no longer match the model "
                    f"({', '.join(sorted({r['status'] for r in drifted}))}); "
                    "mech_part_revise refuses to rebuild over them (hand_edited_tagged_entity).",
                    [rec.handle, *handles],
                    {
                        "code": "hand_edited_tagged_entity",
                        "part_id": part_id,
                        "counts": report.counts(),
                        "drift": drifted[:20],
                        "repairable": False,
                    },
                )
            )
    return issues


check_model_drift.needs_shared = True  # type: ignore[attr-defined]
