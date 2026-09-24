"""Line networks read off a foreign P&ID: runs, services, diameters, equipment.

`engineering.pid.graph` reads a P&ID this server drew; on a foreign one it sees
lines of class `unknown` between blocks of kind `unknown_block` (spec §1). This
module is the reader the takeoffs stand on (spec §7). It works on a
`Snapshot` - one bulk read - and never touches a drawing.

The algorithm, in the order it runs:

1. **Segments.** LINE, LWPOLYLINE, POLYLINE and ARC records in model space on
   the chosen layers (default: every layer `classify_layer` files under piping
   or electrical with confidence >= 0.9). A bulged polyline segment and an ARC
   stay one arc piece: never split, never flattened.
2. **Merge.** Endpoints closer than `tol` become one vertex; the first one seen
   keeps its coordinates.
3. **Split.** A straight segment is split at every vertex lying on it within
   `tol` - the T-junction, where a branch ends on a header. A crossing where
   neither line ends is *not* a junction: two P&ID lines that cross without a
   dot do not connect.
4. **Duplicates once.** The same split turns a line drawn twice, or a line
   drawn over a polyline, into identical pieces; each piece is kept once and
   the length the copies would have added is `stats.overlap_length`.
5. **Fittings.** An INSERT whose box (grown by `tol`) holds two to four free
   ends is an inline fitting - valve, coupling, reducer - and every one of
   those ends is joined to every other through it (a three- or four-way
   fitting has no hub port, so nothing depends on record order); the fitting
   adds no length. An INSERT that a tag labels is equipment and never a
   fitting. A block whose name contains reducer / переход / redüksiyon /
   verloop / Reduzier(stück) is a reducer, and a reducer is found however the
   pipe meets it: free ends in its box, pieces meeting at a vertex inside its
   box (a line broken at the reducer), or a straight segment drawn through
   the middle of its box (its line within a quarter of the box's smaller side
   of the centre), which is cut at the foot of the centre before step 3.
6. **Runs.** A connected component of pieces and fittings is a run.
7. **Diameter.** Each diameter label goes to the piece nearest to it within
   `label_search` (default 5 x the median text height) - unless an open line
   on a layer that is not read stands strictly nearer, in which case the label
   is that line's and is listed in `stats.labels_skipped` - and holds for
   every piece of that drawn entity on the same side of every reducer
   (*direct*; when two labels reach one piece, the nearer wins and the pair is
   listed in `stats.diameter_conflicts`). An unlabelled piece takes the
   diameter of the nearest direct piece along the run, measured along the
   pipe and never across a reducer (*continuity*); two different diameters
   equally near, or none, leave it *unassigned*. Labels are kept as drawn:
   Ø51, SMS51 and DN20 are different sizes.
8. **Service.** A run's service is the length-weighted service of its layers,
   overridden for the whole run by a supply / return word within
   `label_search` of one of its pieces - a layer alone never splits CIP supply
   from CIP return. A CIP or glycol layer that names no direction (which
   `classify_layer` files as ``unknown``) takes its direction from that word.
9. **Equipment.** A run end inside the box (grown by `tol`) of the geometry a
   tag labels is attached to that tag; the smallest such box wins. A tag labels
   the smallest closed shape holding it, else the nearest within twice
   `label_search`; a shape holding two different tags (a room, a skid, a sheet
   frame) is never equipment.

Arc pieces are measured to their chord for the label search only; their length
is the true arc length. Every "which X lies near Y" question (free ends in a
fitting's box, shapes around a tag, tags inside a shape, equipment around a run
end) goes through a grid index, so a field-sized drawing is read in seconds,
not minutes.
"""

from __future__ import annotations

import functools
import heapq
import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from engineering.understand.labels import parse_diameters, parse_tag, wiring_target
from engineering.understand.snapshot import EntityRecord, Pt, Snapshot
from engineering.understand.vocab import classify_layer, fold, supply_return

__all__ = [
    "EQUIPMENT_SEARCH_SHARE",
    "LABEL_SEARCH_FACTOR",
    "LINE_TYPES",
    "MAX_FITTING_ENDS",
    "NETWORK_CONFIDENCE",
    "REDUCER_WORDS",
    "Run",
    "build_network",
    "default_layers",
    "equipment_boxes",
    "is_reducer",
    "label_search_default",
    "tag_occurrences",
]

MODEL = "Model"
LINE_TYPES = frozenset({"LINE", "LWPOLYLINE", "POLYLINE", "ARC"})
TEXT_TYPES = frozenset({"TEXT", "MTEXT", "MULTILEADER"})
#: Words that make an INSERT a reducer, matched in its block name folded by
#: `vocab.fold` (casefolded, diacritics stripped: REDÜKSİYON reads reduksiyon).
#: The same five words `vocab.equipment_kind` files under ``reducer``.
REDUCER_WORDS = ("reducer", "переход", "reduksiyon", "verloop", "reduzier")
NETWORK_DISCIPLINES = ("piping", "electrical")
NETWORK_CONFIDENCE = 0.9
#: A box touched by more free ends than this is not an inline fitting (a
#: valve has two ports, a three- or four-way valve three or four).
MAX_FITTING_ENDS = 4
LABEL_SEARCH_FACTOR = 5.0
#: A tag written beside its symbol is looked for twice as far as a label.
EQUIPMENT_SEARCH_SHARE = 2.0
#: With no text on the drawing at all, labels are searched this many `tol` away.
LABEL_SEARCH_FALLBACK = 50.0
_PAIRS = {"cip": ("cip_supply", "cip_return"), "glycol": ("glycol_supply", "glycol_return")}
_EPS = 1e-9
#: A box covering more grid cells than this is kept aside and checked on every query.
_WIDE_CELLS = 256


@dataclass(frozen=True)
class Run:
    """One connected line network: its pieces, its service, its free ends and the tags on them."""

    id: str
    edges: tuple[dict, ...]
    service: str
    ends: tuple[Pt, ...]
    tags: tuple[str, ...]


def _positive(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: expected a number, got {value!r}") from None
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name}: must be a finite number greater than zero, got {value!r}")
    return number


def _in_box(p: Pt, box, grow: float) -> bool:
    return box[0] - grow <= p[0] <= box[2] + grow and box[1] - grow <= p[1] <= box[3] + grow


def _box_area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _box_distance(p: Pt, box) -> float:
    dx = max(box[0] - p[0], 0.0, p[0] - box[2])
    dy = max(box[1] - p[1], 0.0, p[1] - box[3])
    return math.hypot(dx, dy)


def _centre(rec: EntityRecord) -> Pt | None:
    """Where a label stands: its box centre, else its insertion point."""
    if rec.bbox is not None:
        return ((rec.bbox[0] + rec.bbox[2]) / 2.0, (rec.bbox[1] + rec.bbox[3]) / 2.0)
    return rec.points[0] if rec.points else None


def _segment_distance(p: Pt, a: Pt, b: Pt) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 == 0.0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _along(p: Pt, a: Pt, b: Pt) -> tuple[float, float]:
    """(distance along a->b of p's foot, distance of p from the line)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    px, py = p[0] - a[0], p[1] - a[1]
    return (px * ux + py * uy, abs(ux * py - uy * px))


def _piece_length(a: Pt, b: Pt, bulge: float) -> float:
    chord = math.dist(a, b)
    if bulge == 0.0 or chord == 0.0:
        return chord
    theta = 4.0 * math.atan(abs(bulge))
    return chord / (2.0 * math.sin(theta / 2.0)) * theta


def is_reducer(block: str | None) -> bool:
    """True when the folded block name contains a reducer word (`REDUCER_WORDS`)."""
    if not block:
        return False
    name = fold(block)
    return any(word in name for word in REDUCER_WORDS)


def _typical_size(boxes, fallback: float) -> float:
    """The median larger side of `boxes`, never below `fallback`: a grid cell size."""
    sides = [max(box[2] - box[0], box[3] - box[1]) for box in boxes]
    return max(statistics.median(sides) if sides else 0.0, fallback, _EPS)


def _cell_range(box, grow: float, cell: float) -> tuple[int, int, int, int]:
    return (
        math.floor((box[0] - grow) / cell),
        math.floor((box[1] - grow) / cell),
        math.floor((box[2] + grow) / cell),
        math.floor((box[3] + grow) / cell),
    )


class _PointGrid:
    """Points bucketed on a square grid, for 'which points lie in this box' queries."""

    def __init__(self, items, cell: float) -> None:
        self.cell = cell
        self.items: list[tuple[object, Pt]] = list(items)
        self.cells: dict[tuple[int, int], list[int]] = {}
        for n, (_key, p) in enumerate(self.items):
            key = (math.floor(p[0] / cell), math.floor(p[1] / cell))
            self.cells.setdefault(key, []).append(n)

    def in_box(self, box, grow: float) -> list:
        """The keys of the points inside `box` grown by `grow`, in insertion order."""
        x0, y0, x1, y1 = _cell_range(box, grow, self.cell)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > len(self.items):
            found = range(len(self.items))
        else:
            found = sorted(
                n
                for cx in range(x0, x1 + 1)
                for cy in range(y0, y1 + 1)
                for n in self.cells.get((cx, cy), ())
            )
        return [self.items[n][0] for n in found if _in_box(self.items[n][1], box, grow)]


class _BoxGrid:
    """Boxes bucketed on a square grid, for 'which boxes hold, or lie within `grow` of, p'.

    A box is filed in every cell its box grown by `grow` covers, so a point
    inside that grown box finds it in the point's own cell; a box covering more
    than `_WIDE_CELLS` cells (a sheet frame, a room) is kept aside and returned
    by every query. Callers filter the candidates exactly.
    """

    def __init__(self, items, *, grow: float, cell: float) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], list] = {}
        self.wide: list = []
        for box, payload in items:
            x0, y0, x1, y1 = _cell_range(box, grow, cell)
            if (x1 - x0 + 1) * (y1 - y0 + 1) > _WIDE_CELLS:
                self.wide.append(payload)
                continue
            for cx in range(x0, x1 + 1):
                for cy in range(y0, y1 + 1):
                    self.cells.setdefault((cx, cy), []).append(payload)

    def near(self, p: Pt) -> list:
        key = (math.floor(p[0] / self.cell), math.floor(p[1] / self.cell))
        return self.cells.get(key, []) + self.wide


def _layer_names(snap: Snapshot) -> list[str]:
    return sorted(set(snap.layers) | {rec.layer for rec in snap.records})


def default_layers(snap: Snapshot) -> tuple[str, ...]:
    """Every layer `classify_layer` files under piping or electrical with confidence >= 0.9."""
    chosen = []
    for name in _layer_names(snap):
        found = classify_layer(name)
        if found["discipline"] in NETWORK_DISCIPLINES and found["confidence"] >= NETWORK_CONFIDENCE:
            chosen.append(name)
    return tuple(chosen)


def label_search_default(snap: Snapshot, *, tol: float = 1.0) -> float:
    """5 x the median model-space text height; 50 x `tol` on a drawing with no text."""
    heights = [
        rec.height
        for rec in snap.records
        if rec.space == MODEL and rec.type in TEXT_TYPES and rec.height and rec.height > 0.0
    ]
    if heights:
        return LABEL_SEARCH_FACTOR * statistics.median(heights)
    return LABEL_SEARCH_FALLBACK * tol


def tag_occurrences(snap: Snapshot) -> dict[str, list[dict]]:
    """Every model-space place a tag is written, by tag.

    A TEXT / MTEXT / MULTILEADER whose plain text `parse_tag` reads - unless it
    is a diameter label or a wiring callout - and an INSERT attribute whose tag
    name contains TAG. ``at`` is the insertion point; ``insert`` is the handle
    of the INSERT that carries the tag, or None for a text.
    """
    found: dict[str, list[dict]] = {}
    for rec in snap.records:
        if rec.space != MODEL or not rec.points:
            continue
        if rec.type in TEXT_TYPES and rec.text:
            if parse_diameters(rec.text) or wiring_target(rec.text):
                continue
            tag = parse_tag(rec.text)
            if tag:
                found.setdefault(tag, []).append(
                    {
                        "tag": tag,
                        "at": rec.points[0],
                        "handle": rec.handle,
                        "box": rec.bbox,
                        "insert": None,
                    }
                )
        elif rec.type == "INSERT":
            for name, value in rec.attribs:
                tag = parse_tag(value) if "TAG" in name.upper() else None
                if tag:
                    found.setdefault(tag, []).append(
                        {
                            "tag": tag,
                            "at": rec.points[0],
                            "handle": rec.handle,
                            "box": rec.bbox,
                            "insert": rec.handle,
                        }
                    )
    return found


def _is_shape(rec: EntityRecord) -> bool:
    if rec.type in ("CIRCLE", "ELLIPSE", "INSERT"):
        return True
    return rec.type in ("LWPOLYLINE", "POLYLINE") and rec.closed


def equipment_boxes(
    snap: Snapshot, occurrences: dict[str, list[dict]], *, exclude_layers=(), search: float
) -> dict[str, list[dict]]:
    """The box of the geometry each tag occurrence labels.

    An INSERT carrying the tag is its own geometry. A text labels the smallest
    closed shape (CIRCLE, ELLIPSE, closed polyline, INSERT) off the network
    layers whose box holds its insertion point, else the nearest one within
    `search`, else only itself (its own box, or its insertion point). A shape
    whose box holds the tags of two or more different items is a room, a skid
    or a sheet frame - not equipment - and is never chosen.
    """
    excluded = set(exclude_layers)
    candidates = [
        rec
        for rec in snap.records
        if rec.space == MODEL
        and rec.bbox is not None
        and rec.layer not in excluded
        and _is_shape(rec)
    ]
    cell = _typical_size([rec.bbox for rec in candidates], search)
    anchors = _PointGrid(
        ((tag, place["at"]) for tag, places in occurrences.items() for place in places), cell
    )
    shapes = [rec for rec in candidates if len(set(anchors.in_box(rec.bbox, 0.0))) <= 1]
    index = _BoxGrid(((rec.bbox, rec) for rec in shapes), grow=search, cell=cell)
    out: dict[str, list[dict]] = {}
    for tag, places in sorted(occurrences.items()):
        for place in places:
            x, y = place["at"]
            if place["insert"] is not None and place["box"] is not None:
                box, handle = place["box"], place["insert"]
            else:
                pool = index.near((x, y))
                inside = [s for s in pool if _in_box((x, y), s.bbox, 0.0)]
                near = [
                    (_box_distance((x, y), s.bbox), _box_area(s.bbox), s.handle, s) for s in pool
                ]
                near = [n for n in near if n[0] <= search]
                if inside:
                    best = min(inside, key=lambda s: (_box_area(s.bbox), s.handle))
                    box, handle = best.bbox, best.handle
                elif near:
                    best = min(near, key=lambda n: n[:3])[3]
                    box, handle = best.bbox, best.handle
                else:
                    box, handle = place["box"] or (x, y, x, y), place["handle"]
            out.setdefault(tag, []).append({"box": box, "handle": handle, "at": (x, y)})
    return out


class _Vertices:
    """Snap points to vertices: a point within ``tol`` of a vertex *is* that vertex."""

    def __init__(self, tol: float) -> None:
        self.tol = tol
        self.points: list[Pt] = []
        self._grid: dict[tuple[int, int], list[int]] = {}

    def index(self, p: Pt) -> int:
        cx, cy = math.floor(p[0] / self.tol), math.floor(p[1] / self.tol)
        best, best_d = None, self.tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self._grid.get((cx + dx, cy + dy), ()):
                    d = math.dist(self.points[i], p)
                    if d <= best_d:
                        best, best_d = i, d
        if best is not None:
            return best
        self.points.append((float(p[0]), float(p[1])))
        self._grid.setdefault((cx, cy), []).append(len(self.points) - 1)
        return len(self.points) - 1


def _choose_layers(snap: Snapshot, layers) -> tuple[str, ...]:
    known = _layer_names(snap)
    if layers is None:
        chosen = default_layers(snap)
        if not chosen:
            raise ValueError(
                "layers: no layer is classified piping or electrical with confidence >= 0.9; "
                f"name the layers to read. The drawing has: {', '.join(known)}"
            )
        return chosen
    names = [str(name) for name in layers]
    if not names:
        raise ValueError("layers: an empty list reads nothing; omit it for the default")
    missing = [name for name in names if name not in known]
    if missing:
        raise ValueError(
            f"layers: {', '.join(missing)} not in the drawing; it has: {', '.join(known)}"
        )
    return tuple(dict.fromkeys(names))


def _arc_segment(rec: EntityRecord):
    if not rec.points or rec.radius is None or rec.angles is None:
        return None
    (cx, cy), r = rec.points[0], rec.radius
    start, end = rec.angles
    sweep = (end - start) % 360.0
    if sweep == 0.0:
        return None
    a = (cx + r * math.cos(math.radians(start)), cy + r * math.sin(math.radians(start)))
    b = (cx + r * math.cos(math.radians(end)), cy + r * math.sin(math.radians(end)))
    return a, b, math.tan(math.radians(sweep) / 4.0)


def _segments(snap: Snapshot, layers, *, open_only=False) -> list[tuple[Pt, Pt, float, str, str]]:
    """Every LINE_TYPES segment in model space on `layers`; `open_only` skips closed polylines."""
    wanted = set(layers)
    out = []
    for rec in snap.records:
        if rec.space != MODEL or rec.layer not in wanted or rec.type not in LINE_TYPES:
            continue
        if open_only and rec.closed and rec.type in ("LWPOLYLINE", "POLYLINE"):
            continue
        if rec.type == "LINE":
            if len(rec.points) >= 2:
                out.append((rec.points[0], rec.points[1], 0.0, rec.handle, rec.layer))
        elif rec.type == "ARC":
            arc = _arc_segment(rec)
            if arc is not None:
                out.append((arc[0], arc[1], arc[2], rec.handle, rec.layer))
        else:
            pts = list(rec.points)
            bulges = list(rec.bulges) + [0.0] * (len(pts) - len(rec.bulges))
            count = len(pts) if rec.closed else len(pts) - 1
            for i in range(max(count, 0)):
                out.append(
                    (pts[i], pts[(i + 1) % len(pts)], float(bulges[i]), rec.handle, rec.layer)
                )
    return out


class _ChordIndex:
    """Grid of chords (a, b) for 'which chords lie within `search` of p, or cross this box'."""

    def __init__(self, chords: list[tuple[Pt, Pt]], search: float) -> None:
        lengths = [math.dist(a, b) for a, b in chords] or [search]
        self.cell = max(search, statistics.median(lengths), _EPS)
        self.search = search
        self.cells: dict[tuple[int, int], list[int]] = {}
        for k, (a, b) in enumerate(chords):
            box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
            x0, y0, x1, y1 = _cell_range(box, search, self.cell)
            for cx in range(x0, x1 + 1):
                for cy in range(y0, y1 + 1):
                    self.cells.setdefault((cx, cy), []).append(k)

    def candidates(self, p: Pt) -> list[int]:
        return self.cells.get((math.floor(p[0] / self.cell), math.floor(p[1] / self.cell)), [])

    def in_box(self, box) -> list[int]:
        """The chords filed in any cell `box` covers, each once, in index order."""
        x0, y0, x1, y1 = _cell_range(box, 0.0, self.cell)
        return sorted(
            {
                k
                for cx in range(x0, x1 + 1)
                for cy in range(y0, y1 + 1)
                for k in self.cells.get((cx, cy), ())
            }
        )


def _through_middle(a: Pt, b: Pt, box, tol: float) -> Pt | None:
    """Where a straight segment passing through the middle of `box` is cut, else None.

    The middle: the segment's line runs within a quarter of the box's smaller
    side (plus `tol`) of the box centre - a pipe drawn through a symbol, not a
    parallel pipe grazing its edge. The cut is the foot of the centre on the
    segment, which must lie strictly between the segment's ends.
    """
    length = math.dist(a, b)
    if length <= _EPS:
        return None
    centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    s, off = _along(centre, a, b)
    reach = min(box[2] - box[0], box[3] - box[1]) / 4.0 + tol
    if off > reach or not tol < s < length - tol:
        return None
    t = s / length
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def build_network(snap: Snapshot, *, layers=None, tol=1.0, label_search=None) -> dict:
    """Runs of the line network on `layers` - see the module docstring for every rule.

    Returns ``{"runs": [Run], "unassigned": [...], "attachments": {run id: [{"at", "tag"}]},
    "equipment": {tag: [{"box", "handle", "at"}]}, "stats": {...}}``. Refused by
    name before anything is read: a non-positive `tol` or `label_search`, a
    named layer the drawing does not have (with the list it has), an empty
    `layers`, and - when `layers` is omitted - a drawing with no layer filed
    under piping or electrical at confidence >= 0.9.
    """
    tol = _positive(tol, "tol")
    search = (
        label_search_default(snap, tol=tol)
        if label_search is None
        else _positive(label_search, "label_search")
    )
    chosen = _choose_layers(snap, layers)
    raw = _segments(snap, chosen)
    verts = _Vertices(tol)
    snapped = []
    below_tol = 0  # segments whose two ends merge into one vertex: dropped, and counted
    for a, b, bulge, handle, layer in raw:
        u, v = verts.index(a), verts.index(b)
        if u != v:
            snapped.append((u, v, bulge, handle, layer))
        else:
            below_tol += 1

    # equipment first: an INSERT a tag labels is never a fitting
    occurrences = tag_occurrences(snap)
    equipment = equipment_boxes(
        snap, occurrences, exclude_layers=chosen, search=EQUIPMENT_SEARCH_SHARE * search
    )
    equipment_handles = {box["handle"] for boxes in equipment.values() for box in boxes}
    inserts = [
        rec
        for rec in snap.records
        if rec.space == MODEL
        and rec.type == "INSERT"
        and rec.bbox is not None
        and rec.handle not in equipment_handles
    ]
    reducer_boxes = [rec.bbox for rec in inserts if is_reducer(rec.block)]

    # a straight segment drawn through the middle of a reducer is cut there, so
    # the reducer separates its two sides (the split below makes the cut)
    if reducer_boxes:
        chords = [(verts.points[u], verts.points[v]) for u, v, _b, _h, _l in snapped]
        chord_index = _ChordIndex(chords, tol)
        for box in reducer_boxes:
            for n in chord_index.in_box((box[0] - tol, box[1] - tol, box[2] + tol, box[3] + tol)):
                u, v, bulge, _handle, _layer = snapped[n]
                a, b = verts.points[u], verts.points[v]
                if bulge != 0.0 or _in_box(a, box, tol) or _in_box(b, box, tol):
                    continue
                cut = _through_middle(a, b, box, tol)
                if cut is not None:
                    verts.index(cut)
    pts = verts.points

    # split straight segments at every vertex lying on them (T-junctions, overlaps)
    order = sorted(range(len(pts)), key=lambda i: pts[i][0])
    xs = [pts[i][0] for i in order]
    pieces_by_key: dict[tuple[int, int, float], dict] = {}
    overlap_count, overlap_length = 0, 0.0
    for u, v, bulge, handle, layer in snapped:
        chain = [u, v]
        if bulge == 0.0:
            pa, pb = pts[u], pts[v]
            length = math.dist(pa, pb)
            lo = bisect_left(xs, min(pa[0], pb[0]) - tol)
            hi = bisect_right(xs, max(pa[0], pb[0]) + tol)
            inner = []
            for k in order[lo:hi]:
                if k in (u, v):
                    continue
                p = pts[k]
                if not min(pa[1], pb[1]) - tol <= p[1] <= max(pa[1], pb[1]) + tol:
                    continue
                s, off = _along(p, pa, pb)
                if off <= tol and tol < s < length - tol:
                    inner.append((s, k))
            chain = [u] + [k for _s, k in sorted(inner)] + [v]
        for p, q in zip(chain, chain[1:], strict=False):
            if p == q:
                continue
            b_signed = bulge if p < q else -bulge
            key = (min(p, q), max(p, q), round(b_signed, 9))
            piece_length = _piece_length(pts[p], pts[q], bulge)
            known = pieces_by_key.get(key)
            if known is not None:
                overlap_count += 1
                overlap_length += piece_length
                known["handles"].add(handle)
                known["layers"].add(layer)
                continue
            pieces_by_key[key] = {
                "u": key[0],
                "v": key[1],
                "bulge": key[2],
                "length": piece_length,
                "layer": layer,
                "layers": {layer},
                "handles": {handle},
            }
    pieces = [pieces_by_key[key] for key in sorted(pieces_by_key)]

    at_vertex: dict[int, list[int]] = {}
    for k, piece in enumerate(pieces):
        at_vertex.setdefault(piece["u"], []).append(k)
        at_vertex.setdefault(piece["v"], []).append(k)

    free = sorted(vertex for vertex, ks in at_vertex.items() if len(ks) == 1)
    joined = sorted(vertex for vertex, ks in at_vertex.items() if len(ks) > 1)
    cell = _typical_size([rec.bbox for rec in inserts], search)
    free_grid = _PointGrid(((vertex, pts[vertex]) for vertex in free), cell)
    joined_grid = _PointGrid(((vertex, pts[vertex]) for vertex in joined), cell)
    bridges: list[tuple[int, int, bool, str]] = []
    # vertices where pieces meet inside a reducer's box: a line broken at the
    # reducer, or cut above where it was drawn straight through it
    reducer_vertices: set[int] = set()
    fittings, reducers, skipped = 0, 0, []
    for rec in inserts:
        reducer = is_reducer(rec.block)
        inside = free_grid.in_box(rec.bbox, tol)
        through = joined_grid.in_box(rec.bbox, tol) if reducer else []
        if len(inside) > MAX_FITTING_ENDS:
            skipped.append(rec.handle)
            continue
        if len(inside) < 2 and not through:
            continue
        fittings += 1
        reducers += int(reducer)
        reducer_vertices.update(through)
        if len(inside) < 2:
            continue
        # every port joins every other: a three- or four-way fitting has no hub
        for n, u in enumerate(inside):
            for v in inside[n + 1 :]:
                bridges.append((u, v, reducer, rec.handle))

    # runs: union-find over pieces and bridges
    parent = list(range(len(pts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for piece in pieces:
        parent[find(piece["u"])] = find(piece["v"])
    for u, v, _reducer, _handle in bridges:
        parent[find(u)] = find(v)
    groups: dict[int, list[int]] = {}
    for k, piece in enumerate(pieces):
        groups.setdefault(find(piece["u"]), []).append(k)

    def first_point(ks: list[int]) -> tuple[float, float]:
        return min(min(pts[pieces[k]["u"]], pts[pieces[k]["v"]]) for k in ks)

    ordered = sorted(groups.values(), key=first_point)
    run_of: dict[int, int] = {}
    for r, ks in enumerate(ordered):
        for k in ks:
            run_of[k] = r

    # neighbours for continuity: shared vertices and bridges, each flagged when a
    # reducer stands between the two pieces
    neighbours: dict[int, set[tuple[int, bool]]] = {k: set() for k in range(len(pieces))}
    for vertex, ks in at_vertex.items():
        across = vertex in reducer_vertices
        for k in ks:
            neighbours[k].update((j, across) for j in ks if j != k)
    for u, v, reducer, _handle in bridges:
        for k in at_vertex.get(u, ()):
            neighbours[k].update((j, reducer) for j in at_vertex.get(v, ()))
        for k in at_vertex.get(v, ()):
            neighbours[k].update((j, reducer) for j in at_vertex.get(u, ()))

    # labels: diameters and supply / return words go to the nearest piece, unless
    # a line on a layer that is not read stands nearer - the label is that line's
    index = _ChordIndex([(pts[piece["u"]], pts[piece["v"]]) for piece in pieces], search)
    unread = set(_layer_names(snap)) - set(chosen)
    others = [(a, b, layer) for a, b, _bulge, _h, layer in _segments(snap, unread, open_only=True)]
    other_index = _ChordIndex([(a, b) for a, b, _layer in others], search)

    def nearest(p: Pt) -> tuple[int, float] | None:
        found = []
        for k in index.candidates(p):
            piece = pieces[k]
            d = _segment_distance(p, pts[piece["u"]], pts[piece["v"]])
            if d <= search:
                found.append((d, k))
        if not found:
            return None
        d, k = min(found)
        return k, d

    def nearer_unread(p: Pt, dist: float) -> str | None:
        """The layer of the nearest unread line strictly nearer to p than `dist`, else None."""
        found = []
        for n in other_index.candidates(p):
            a, b, layer = others[n]
            d = _segment_distance(p, a, b)
            if d < dist - _EPS * max(1.0, dist):
                found.append((d, layer))
        return min(found)[1] if found else None

    def entity_side(k: int) -> list[int]:
        """The pieces of k's drawn entities reached from k without crossing a reducer."""
        handles = pieces[k]["handles"]
        seen, stack = {k}, [k]
        while stack:
            i = stack.pop()
            for j, via_reducer in neighbours[i]:
                if via_reducer or j in seen or not pieces[j]["handles"] & handles:
                    continue
                seen.add(j)
                stack.append(j)
        return sorted(seen)

    reaching: dict[int, list[tuple[float, str, str]]] = {}
    votes: dict[int, list[tuple[float, str, str | None, str]]] = {}
    labels_skipped = []
    for rec in snap.records:
        if rec.space != MODEL or rec.type not in TEXT_TYPES or not rec.text:
            continue
        where = _centre(rec)
        if where is None:
            continue
        tokens = parse_diameters(rec.text)
        direction = supply_return(rec.text)
        if not tokens and direction is None:
            continue
        hit = nearest(where)
        if hit is None:
            continue
        k, dist = hit
        owner = nearer_unread(where, dist)
        if owner is not None:
            labels_skipped.append(
                {
                    "handle": rec.handle,
                    "text": rec.text,
                    "reason": f"nearer to a line on layer {owner}, which is not read",
                }
            )
            continue
        if len(tokens) > 1:
            labels_skipped.append(
                {"handle": rec.handle, "text": rec.text, "reason": "more than one diameter"}
            )
        elif tokens:
            for j in entity_side(k):
                q = pieces[j]
                reaching.setdefault(j, []).append(
                    (_segment_distance(where, pts[q["u"]], pts[q["v"]]), tokens[0], rec.handle)
                )
        if direction is not None:
            family = _family_of(rec.text)
            votes.setdefault(run_of[k], []).append((dist, direction, family, rec.handle))

    direct: dict[int, tuple[str, str]] = {}
    diameter_conflicts = []
    for k, found in sorted(reaching.items()):
        found.sort()
        direct[k] = (found[0][1], found[0][2])
        others = sorted({token for _d, token, _h in found} - {found[0][1]})
        if others:
            diameter_conflicts.append({"piece": k, "kept": found[0][1], "also": others})

    best: dict[int, list] = {k: [0.0, {token}] for k, (token, _h) in direct.items()}
    heap = [(0.0, k, token) for k, (token, _h) in direct.items()]
    heapq.heapify(heap)
    while heap:
        d, k, token = heapq.heappop(heap)
        current = best[k]
        if d > current[0] + _EPS * max(1.0, d) or token not in current[1]:
            continue
        for j, via_reducer in neighbours[k]:
            if via_reducer or j in direct:
                continue
            nd = d + (pieces[k]["length"] + pieces[j]["length"]) / 2.0
            known = best.get(j)
            if known is None or nd < known[0] - _EPS * max(1.0, nd):
                best[j] = [nd, {token}]
                heapq.heappush(heap, (nd, j, token))
            elif abs(nd - known[0]) <= _EPS * max(1.0, nd) and token not in known[1]:
                known[1].add(token)
                heapq.heappush(heap, (nd, j, token))

    # a free end joined through a fitting is no longer a run end
    bridged: dict[int, int] = {}
    for u, v, _reducer, _handle in bridges:
        bridged[u] = bridged.get(u, 0) + 1
        bridged[v] = bridged.get(v, 0) + 1
    equipment_boxes_all = [(box["box"], tag) for tag, boxes in equipment.items() for box in boxes]
    equipment_index = _BoxGrid(
        ((box, (box, tag)) for box, tag in equipment_boxes_all),
        grow=tol,
        cell=_typical_size([box for box, _tag in equipment_boxes_all], search),
    )

    runs, unassigned, attachments, service_conflicts = [], [], {}, []
    for r, ks in enumerate(ordered):
        run_id = f"R{r + 1}"
        service = _run_service([pieces[k] for k in ks], votes.get(r, []), run_id, service_conflicts)
        edges = []
        for n, k in enumerate(sorted(ks), start=1):
            piece = pieces[k]
            if k in direct:
                diameter, source, label = direct[k][0], "direct", direct[k][1]
            elif k in best and len(best[k][1]) == 1:
                diameter, source, label = next(iter(best[k][1])), "continuity", None
            else:
                diameter, source, label = None, "unassigned", None
                unassigned.append(
                    {
                        "run": run_id,
                        "edge": f"{run_id}.{n}",
                        "length": piece["length"],
                        "reason": "two different diameters equally near"
                        if k in best
                        else "no diameter label within label_search and none carried along the run",
                    }
                )
            edges.append(
                {
                    "id": f"{run_id}.{n}",
                    "a": pts[piece["u"]],
                    "b": pts[piece["v"]],
                    "bulge": piece["bulge"],
                    "length": piece["length"],
                    "layer": piece["layer"],
                    "layers": tuple(sorted(piece["layers"])),
                    "handles": tuple(sorted(piece["handles"])),
                    "service": service,
                    "diameter": diameter,
                    "diameter_source": source,
                    "label": label,
                }
            )
        degree: dict[int, int] = {}
        for k in ks:
            for vertex in (pieces[k]["u"], pieces[k]["v"]):
                degree[vertex] = degree.get(vertex, 0) + 1
        for vertex in degree:
            degree[vertex] += bridged.get(vertex, 0)
        end_points = sorted(pts[vertex] for vertex, count in degree.items() if count == 1)
        attached = [{"at": p, "tag": _attach(p, equipment_index, tol)} for p in end_points]
        attachments[run_id] = attached
        runs.append(
            Run(
                id=run_id,
                edges=tuple(edges),
                service=service,
                ends=tuple(end_points),
                tags=tuple(sorted({a["tag"] for a in attached if a["tag"] is not None})),
            )
        )

    return {
        "runs": runs,
        "unassigned": unassigned,
        "attachments": attachments,
        "equipment": equipment,
        "stats": {
            "layers": list(chosen),
            "tol": tol,
            "label_search": search,
            "segments": len(raw),
            "segments_below_tol": below_tol,
            "pieces": len(pieces),
            "overlap_removed": overlap_count,
            "overlap_length": overlap_length,
            "fittings": fittings,
            "reducers": reducers,
            "fittings_skipped": skipped,
            "runs": len(runs),
            "labels_skipped": labels_skipped,
            "diameter_conflicts": diameter_conflicts,
            "service_conflicts": service_conflicts,
        },
    }


def _family(service: str | None) -> str | None:
    for family, pair in _PAIRS.items():
        if service in pair:
            return family
    return None


@functools.lru_cache(maxsize=4096)
def _classified(name: str) -> dict:
    """`classify_layer`, once per distinct name (a run has thousands of pieces, few layers)."""
    return classify_layer(name)


def _family_of(name: str | None) -> str | None:
    """The supply / return pair (``cip``, ``glycol``) a layer name or a text names.

    `classify_layer` files a CIP or glycol name without a direction word as
    service ``unknown`` - the layer alone is not trusted with the direction -
    so the pair is recovered by asking again with a direction word appended.
    """
    if not name:
        return None
    found = _classified(name)
    family = _family(found["service"])
    if family is None and found["service"] == "unknown" and found["keyword"] is not None:
        family = _family(_classified(f"{name} supply")["service"])
    return family


def _run_service(pieces: list[dict], votes: list, run_id: str, conflicts: list) -> str:
    weight: dict[str, float] = {}
    families: dict[str, float] = {}
    for piece in pieces:
        service = _classified(piece["layer"])["service"]
        weight[service] = weight.get(service, 0.0) + piece["length"]
        family = _family_of(piece["layer"])
        if family is not None:
            families[family] = families.get(family, 0.0) + piece["length"]
    known = {s: w for s, w in weight.items() if s != "unknown"} or weight
    base = min(known, key=lambda s: (-known[s], s))
    if not votes:
        return base
    votes = sorted(votes)
    direction = votes[0][1]
    if len({vote[1] for vote in votes}) > 1:
        conflicts.append({"run": run_id, "kept": direction, "reason": "supply and return words"})
    family = _family(base)
    if family is None and base == "unknown" and families:
        # a CIP / glycol layer that names no direction: the word decides it
        family = min(families, key=lambda f: (-families[f], f))
    if family is None:
        family = next((vote[2] for vote in votes if vote[2]), None)
    if family is None:
        conflicts.append(
            {"run": run_id, "kept": base, "reason": f"{direction} word on a line with no pair"}
        )
        return base
    return _PAIRS[family][0 if direction == "supply" else 1]


def _attach(p: Pt, index: _BoxGrid, tol: float) -> str | None:
    hits = [(_box_area(box), tag) for box, tag in index.near(p) if _in_box(p, box, tol)]
    return min(hits)[1] if hits else None
