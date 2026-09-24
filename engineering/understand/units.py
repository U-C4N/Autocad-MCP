"""Declared versus inferred drawing units, and extents that one stray entity cannot stretch.

`$INSUNITS` is a claim the drawing makes about itself, and a foreign drawing
can make it wrongly: a layout that declares inches over millimetre geometry
reads 25.4 times too large if the header is believed. This module never corrects
the claim silently. It measures what the geometry implies and reports both,
with the numbers that decided it.

**Evidence.** Each item is one median measured in drawing units and the band a
real drawing puts it in, in metres. A candidate unit is *plausible* for an item
when the measurement, converted by that unit, lands in the band. An item that
every candidate or no candidate satisfies says nothing and is dropped as
inconclusive.

========================  ======================  ======  ====================================
item                      band                    weight  basis
========================  ======================  ======  ====================================
``room_area``             1 m² … 5 000 m²         1.0     a labelled room's face (this reader's
                                                          heuristic)
``wall_thickness``        0.05 m … 1.0 m          1.0     nearest parallel wall line (heuristic)
``door_leaf``             0.5 m … 1.5 m           1.0     radius of a quarter-circle arc, a door
                                                          swing (heuristic)
``text_height``           1.8 mm … 4.0 m          0.5     ISO 3098-0 lettering heights 1.8-20 mm
                                                          at ISO 5455 scales 1:1 to 1:200 (the
                                                          1:200 ceiling is this reader's choice
                                                          for plant drawings)
``overall_size``          0.01 m … 2 000 m        0.25    the larger side of the robust extents
                                                          (heuristic: a plant or a building)
========================  ======================  ======  ====================================

**Decision.** A candidate scores the weights of the items it is plausible for;
the header is evidence too and adds :data:`HEADER_WEIGHT` to the declared unit.
The highest score wins and a tie goes to the declared unit, then to the order of
:data:`PREFERENCE`. So the header is overruled only when the geometry
outweighs it - one weak item (the overall size) never does.

**Extents.** The body of the drawing is grown from its middle: the seed is
the quartile box of the entity centres (Q1..Q3 on each axis), and every
entity whose box lies within :data:`OUTLIER_REACH` body diagonals of the body
joins it, pass after pass, until a pass adds nothing. What never joins is an
outlier - an entity farther from everything else than the drawing is large.
The robust extents are the body's box. This is this reader's heuristic, not
a statistical standard: a per-axis fence on centres (Tukey's) flags the edge
walls of a plan whose centres crowd its middle, and a quantile range is
dragged by the outlier itself in a small drawing. Fewer than four entities
have no outliers.

Pure: records in, dicts out. The drawing is never touched.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence

from ezdxf import units as ezdxf_units

from engineering.understand.snapshot import EntityRecord, Snapshot
from engineering.understand.vocab import classify_layer

__all__ = [
    "BANDS",
    "CANDIDATES",
    "HEADER_WEIGHT",
    "MM_PER_UNIT",
    "PREFERENCE",
    "entity_box",
    "infer_units",
    "robust_extents",
    "wall_segments",
]

#: Millimetres per drawing unit, exact by definition: the SI prefixes, and the
#: inch (25.4 mm) and foot (304.8 mm) of the 1959 international yard and pound
#: agreement. ezdxf's own ``METER_FACTOR`` is rounded (39.37007874 in/m), so it
#: is not used for conversion.
MM_PER_UNIT: dict[str, float] = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
CANDIDATES: tuple[str, ...] = ("mm", "cm", "m", "in", "ft")
#: Tie-break after the declared unit.
PREFERENCE: tuple[str, ...] = ("mm", "m", "cm", "in", "ft")
#: What the header's own claim weighs against the geometry.
HEADER_WEIGHT = 0.5
#: (low, high, weight, power): the band in metres (m² for power 2).
BANDS: dict[str, tuple[float, float, float, int]] = {
    "room_area": (1.0, 5000.0, 1.0, 2),
    "wall_thickness": (0.05, 1.0, 1.0, 1),
    "door_leaf": (0.5, 1.5, 1.0, 1),
    "text_height": (0.0018, 4.0, 0.5, 1),
    "overall_size": (0.01, 2000.0, 0.25, 1),
}
#: How far (in body diagonals) an entity may lie from the body and still join it.
OUTLIER_REACH = 1.0
#: A door swing is a quarter circle; the sweep may be drawn a little off.
DOOR_SWEEP = (85.0, 95.0)
#: Wall-thickness pairing reads at most this many wall segments.
WALL_SEGMENT_CAP = 3000

_TEXT_TYPES = frozenset({"TEXT", "MTEXT"})
_POLY_TYPES = frozenset({"LWPOLYLINE", "POLYLINE"})


def entity_box(rec: EntityRecord) -> tuple[float, float, float, float] | None:
    """The record's WCS box: its own ``bbox``, else its points (a circle or arc
    grown by its radius about its centre), else None."""
    if rec.bbox is not None:
        return tuple(float(v) for v in rec.bbox)
    if not rec.points:
        return None
    xs = [float(p[0]) for p in rec.points]
    ys = [float(p[1]) for p in rec.points]
    grow = float(rec.radius) if rec.type in ("CIRCLE", "ARC") and rec.radius else 0.0
    return (min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow)


def _union(boxes: Iterable[tuple[float, float, float, float]]):
    boxes = list(boxes)
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _diag(box) -> float:
    return math.hypot(box[2] - box[0], box[3] - box[1]) if box else 0.0


def _box_gap(a, b) -> float:
    """Distance between two boxes; 0 when they touch or overlap."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _grow_body(boxes: Sequence[tuple[float, float, float, float]]) -> set[int]:
    """Indices of the boxes that never join the body grown from the centre."""
    if len(boxes) < 4:
        return set()
    xs = [(b[0] + b[2]) / 2.0 for b in boxes]
    ys = [(b[1] + b[3]) / 2.0 for b in boxes]
    qx = statistics.quantiles(xs, n=4, method="inclusive")
    qy = statistics.quantiles(ys, n=4, method="inclusive")
    body = (qx[0], qy[0], qx[2], qy[2])
    floor = statistics.median(_diag(b) for b in boxes)
    pending = set(range(len(boxes)))
    while pending:
        reach = OUTLIER_REACH * max(_diag(body), floor)
        joined = [i for i in pending if _box_gap(boxes[i], body) <= reach]
        if not joined:
            break
        for i in joined:
            body = _union([body, boxes[i]])
        pending.difference_update(joined)
    return pending


def _distance_to_box(x: float, y: float, box) -> float:
    dx = max(box[0] - x, 0.0, x - box[2])
    dy = max(box[1] - y, 0.0, y - box[3])
    return math.hypot(dx, dy)


def robust_extents(records: Sequence[EntityRecord]) -> dict:
    """Computed and robust extents of ``records`` and the outliers between them.

    ``outliers`` are sorted farthest first, each with its handle, type, layer,
    centre and its distance from the robust box. ``stretch`` is the computed
    diagonal over the robust one (None when the robust box is a point).
    """
    items = [(rec, box) for rec in records if (box := entity_box(rec)) is not None]
    centres = [((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) for _rec, b in items]
    skip = _grow_body([b for _rec, b in items])
    outside = sorted(skip)
    computed = _union(b for _rec, b in items)
    robust = _union(b for i, (_rec, b) in enumerate(items) if i not in skip)
    outliers = []
    for i in outside:
        rec, _box = items[i]
        cx, cy = centres[i]
        outliers.append(
            {
                "handle": rec.handle,
                "type": rec.type,
                "layer": rec.layer,
                "at": [cx, cy],
                "distance": _distance_to_box(cx, cy, robust) if robust else None,
                "confidence": 0.9,
            }
        )
    outliers.sort(key=lambda o: (-(o["distance"] or 0.0), o["handle"]))
    robust_diag = _diag(robust)
    return {
        "computed": list(computed) if computed else None,
        "robust": list(robust) if robust else None,
        "outliers": outliers,
        "stretch": (_diag(computed) / robust_diag) if robust_diag > 0.0 else None,
        "method": (
            "body grown from the quartile box of entity centres; an entity farther from it "
            f"than {OUTLIER_REACH:g} body diagonal(s) never joins and is an outlier"
        ),
    }


def wall_segments(
    records: Sequence[EntityRecord], *, is_wall=None
) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]], int]:
    """Straight segments of LINE and polyline records on wall layers, plus the
    number of bulged (arc) polyline segments left out.

    ``is_wall(layer)`` defaults to `vocab.classify_layer` naming the
    ``architecture`` discipline.
    """
    cache: dict[str, bool] = {}

    def wall(layer: str) -> bool:
        if layer not in cache:
            if is_wall is not None:
                cache[layer] = bool(is_wall(layer))
            else:
                cache[layer] = classify_layer(layer).get("discipline") == "architecture"
        return cache[layer]

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    bulged = 0
    for rec in records:
        if not wall(rec.layer):
            continue
        if rec.type == "LINE" and len(rec.points) >= 2:
            segments.append((tuple(rec.points[0]), tuple(rec.points[1])))
        elif rec.type in _POLY_TYPES and len(rec.points) >= 2:
            pts = list(rec.points)
            count = len(pts) if rec.closed else len(pts) - 1
            for i in range(count):
                a, b = pts[i], pts[(i + 1) % len(pts)]
                bulge = rec.bulges[i] if i < len(rec.bulges) else 0.0
                if abs(bulge) > 1e-12:
                    bulged += 1
                    continue
                if a != b:
                    segments.append((tuple(a), tuple(b)))
    return segments, bulged


def _wall_thickness(segments) -> tuple[float | None, int]:
    """Median distance from an axis-parallel wall segment to its nearest parallel
    neighbour on either side that overlaps it; (None, 0) without a pair.

    Both sides, because the inner face of a double-line wall has the room on
    one side and its own outer face on the other: looking one way only reads
    half the faces as room widths and the median lands on neither."""
    horizontal: list[tuple[float, float, float]] = []  # (y, x0, x1)
    vertical: list[tuple[float, float, float]] = []  # (x, y0, y1)
    for (ax, ay), (bx, by) in segments[:WALL_SEGMENT_CAP]:
        length = math.hypot(bx - ax, by - ay)
        if length <= 0.0:
            continue
        if abs(by - ay) <= 1e-9 * length:
            horizontal.append((ay, min(ax, bx), max(ax, bx)))
        elif abs(bx - ax) <= 1e-9 * length:
            vertical.append((ax, min(ay, by), max(ay, by)))
    gaps: list[float] = []
    for group in (horizontal, vertical):
        group.sort()
        for i, (c, lo, hi) in enumerate(group):
            best = None
            for step in (1, -1):
                j = i + step
                while 0 <= j < len(group):
                    c2, lo2, hi2 = group[j]
                    d = abs(c2 - c)
                    if best is not None and d >= best:
                        break
                    if d > 0.0 and lo2 < hi and lo < hi2:
                        best = d
                    j += step
            if best is not None:
                gaps.append(best)
    if not gaps:
        return None, 0
    return statistics.median(gaps), len(gaps)


def _declared(code: int) -> dict:
    try:
        name = ezdxf_units.decode(int(code))
        label = ezdxf_units.unit_name(int(code))
    except (IndexError, ValueError, TypeError):
        name, label = None, None
    return {"code": int(code), "name": name, "label": label or "Unitless"}


def infer_units(
    snap: Snapshot,
    *,
    records: Sequence[EntityRecord] | None = None,
    room_areas: Sequence[float] = (),
) -> dict:
    """The unit the geometry implies, against the one ``$INSUNITS`` declares.

    ``records`` defaults to the snapshot's model-space records; pass the ones
    left after the outliers are removed. ``room_areas`` are labelled room faces
    in square drawing units. Every item of ``evidence`` carries its median,
    its sample count, what it is in metres under each candidate, the
    candidates it is plausible for and whether it was informative.
    """
    recs = [r for r in snap.records if r.space == "Model"] if records is None else list(records)
    measured: dict[str, tuple[float, int]] = {}

    heights = [float(r.height) for r in recs if r.type in _TEXT_TYPES and r.height and r.height > 0]
    if heights:
        measured["text_height"] = (statistics.median(heights), len(heights))
    radii = []
    for r in recs:
        if r.type == "ARC" and r.radius and r.angles:
            sweep = (float(r.angles[1]) - float(r.angles[0])) % 360.0
            if DOOR_SWEEP[0] <= sweep <= DOOR_SWEEP[1]:
                radii.append(float(r.radius))
    if radii:
        measured["door_leaf"] = (statistics.median(radii), len(radii))
    thickness, pairs = _wall_thickness(wall_segments(recs)[0])
    if thickness is not None:
        measured["wall_thickness"] = (thickness, pairs)
    areas = [float(a) for a in room_areas if a and a > 0]
    if areas:
        measured["room_area"] = (statistics.median(areas), len(areas))
    box = robust_extents(recs)["robust"]
    if box:
        side = max(box[2] - box[0], box[3] - box[1])
        if side > 0:
            measured["overall_size"] = (side, len(recs))

    evidence = []
    for kind, (value, count) in measured.items():
        low, high, weight, power = BANDS[kind]
        physical = {u: value * (MM_PER_UNIT[u] / 1000.0) ** power for u in CANDIDATES}
        plausible = [u for u in CANDIDATES if low <= physical[u] <= high]
        evidence.append(
            {
                "kind": kind,
                "value": value,
                "count": count,
                "band": [low, high],
                "band_unit": "m2" if power == 2 else "m",
                "weight": weight,
                "as": physical,
                "plausible": plausible,
                "informative": 0 < len(plausible) < len(CANDIDATES),
            }
        )

    declared = _declared(snap.insunits)
    name = declared["name"]
    informative = [e for e in evidence if e["informative"]]
    scores = {u: sum(e["weight"] for e in informative if u in e["plausible"]) for u in CANDIDATES}
    if name in scores:
        scores[name] += HEADER_WEIGHT
    if informative:
        best = max(scores.values())
        order = ([name] if name in scores else []) + [u for u in PREFERENCE if u != name]
        inferred = next(u for u in order if scores[u] == best)
        total = sum(e["weight"] for e in informative) + (HEADER_WEIGHT if name in scores else 0.0)
        confidence = round(0.9 * scores[inferred] / total, 2)
    elif name in MM_PER_UNIT:
        inferred, confidence = name, 0.4
    else:
        inferred, confidence = None, 0.0

    warning = None
    if inferred is not None and inferred != name:
        support = [e for e in informative if inferred in e["plausible"]]
        detail = "; ".join(
            f"{e['kind']} {e['value']:g} = {e['as'][inferred]:.4g} {e['band_unit']} as {inferred}"
            + (f", {e['as'][name]:.4g} {e['band_unit']} as {name}" if name in MM_PER_UNIT else "")
            for e in support
        )
        warning = (
            f"INSUNITS declares {declared['label']} ({declared['code']}) but the geometry reads "
            f"as {inferred}: {detail}. Lengths are measured in {inferred}; the header is not "
            "changed."
        )
    elif inferred is None:
        warning = (
            f"INSUNITS declares {declared['label']} ({declared['code']}) and no measurement "
            "settles the unit; lengths stay in drawing units."
        )
    return {
        "declared": declared,
        "inferred": inferred,
        "mm_per_unit": MM_PER_UNIT.get(inferred) if inferred else None,
        "agree": inferred is not None and inferred == name,
        "confidence": confidence,
        "scores": scores,
        "evidence": evidence,
        "warning": warning,
    }
