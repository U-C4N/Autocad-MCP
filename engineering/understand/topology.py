"""The topology of a line network: dangling ends, near misses, interior crossings.

Spec §11. Pure: a ``Snapshot`` in, three lists out; nothing touches a drawing.

What is analysed: the entities on ``layers`` that draw a network - LINE,
LWPOLYLINE, POLYLINE (a bulged segment is a true arc) and ARC. Their *ends*
are the ends of the entity: a polyline's first and last vertex, never an
interior vertex, and a closed polyline has none. Everything else in the same
space is context: every LINE / LWPOLYLINE / POLYLINE / ARC / CIRCLE on another
layer (a tank outline, a wall), every CIRCLE on the analysed layers, and the
box of every INSERT on any layer (a valve, a pump, a reducer).

Each end is exactly one of, tested in this order:

* **joined** (no finding) - within ``tol`` of another network end, or of a
  network segment other than its own segment and that segment's neighbours.
  A polyline of three or more segments may join its own other end (a ring
  drawn as an open polyline). A segment lying along the end's own segment and
  running into it - a line or an arc drawn twice, a line over a polyline -
  is not a join, so a pipe drawn twice dangles exactly like a pipe drawn once;
* **attached** (no finding) - within ``tol`` of a context curve, or inside or
  within ``tol`` of an INSERT box: a line ending on equipment or on a fitting
  is connected, whatever layer the equipment is on;
* **near miss** - the nearest network end or segment (the candidates of
  *joined*) is within ``gap``: ``end_end`` when an end is nearest (an end
  wins a tie), else ``end_segment``. One row per pair;
* **dangling** - none of the above.

An **interior crossing** is two straight network segments on the same layer
that cross at a point more than ``tol`` from every end of both segments: a
junction nobody drew, or a crossing drawn without a break. A crossing through
a polyline vertex reads as a touch and is not reported, arcs are not tested
for crossings, and two different layers crossing is how every plan is drawn (a
cable over a pipe), so it is not a finding. Spaces are analysed apart: model
space and each paper layout never join each other.

``gap`` defaults to 0.2 % of the diagonal of the analysed network's box, never
less than ten ``tol``. The 0.2 %, the ten-``tol`` floor and the same-layer
rule for crossings are this module's declared defaults, not standards values.

``EntityRecord.points[0]`` is the centre of an ARC or a CIRCLE (the reader in
``engineering.understand.snapshot``); an ARC or CIRCLE without one is skipped
and counted in ``stats["skipped"]``. Spatial lookups go through a uniform
grid, so a large drawing is read in one pass rather than in pairs of
entities.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from engineering.understand.snapshot import EntityRecord, Pt, Snapshot
from engineering.understand.vocab import classify_layer

__all__ = [
    "DEFAULT_TOL",
    "GAP_FLOOR_TOLS",
    "GAP_FRACTION",
    "NETWORK_CONFIDENCE",
    "NETWORK_DISCIPLINES",
    "Piece",
    "cap_findings",
    "default_gap",
    "entity_pieces",
    "network_layers",
    "topology_findings",
]

DEFAULT_TOL = 0.01
#: The default gap, as a share of the analysed network's diagonal.
GAP_FRACTION = 0.002
#: ... and never less than this many ``tol``.
GAP_FLOOR_TOLS = 10.0
#: What makes a layer a network layer by default (spec §11).
NETWORK_DISCIPLINES = ("piping", "electrical")
NETWORK_CONFIDENCE = 0.9
FINDING_KINDS = ("dangling", "near_miss", "crossing")

_NETWORK_TYPES = frozenset({"LINE", "LWPOLYLINE", "POLYLINE", "ARC"})
_CONTEXT_TYPES = _NETWORK_TYPES | {"CIRCLE"}
_TWO_PI = 2.0 * math.pi
#: A piece whose box covers more grid cells than this is kept on a side list.
_MAX_CELLS = 4096


@dataclass(frozen=True)
class Piece:
    """One segment of an entity: straight, or an arc when ``centre`` is set.

    ``a`` and ``b`` are its ends as drawn. An arc runs counter-clockwise from
    the angle ``start`` (radians) through ``sweep`` (radians, > 0).
    """

    owner: str
    layer: str
    index: int
    a: Pt
    b: Pt
    centre: Pt | None = None
    radius: float = 0.0
    start: float = 0.0
    sweep: float = 0.0

    def box(self) -> tuple[float, float, float, float]:
        if self.centre is not None:
            cx, cy = self.centre
            r = self.radius
            return (cx - r, cy - r, cx + r, cy + r)
        return (
            min(self.a[0], self.b[0]),
            min(self.a[1], self.b[1]),
            max(self.a[0], self.b[0]),
            max(self.a[1], self.b[1]),
        )

    def nearest(self, p: Pt) -> Pt:
        """The point of this piece nearest to ``p``."""
        if self.centre is None:
            ax, ay = self.a
            dx, dy = self.b[0] - ax, self.b[1] - ay
            length2 = dx * dx + dy * dy
            if length2 == 0.0:
                return self.a
            t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2
            t = min(1.0, max(0.0, t))
            return (ax + t * dx, ay + t * dy)
        cx, cy = self.centre
        rel = (math.atan2(p[1] - cy, p[0] - cx) - self.start) % _TWO_PI
        distance = math.hypot(p[0] - cx, p[1] - cy)
        if rel <= self.sweep + 1e-12 and distance > 0.0:
            return (
                cx + self.radius * (p[0] - cx) / distance,
                cy + self.radius * (p[1] - cy) / distance,
            )
        return self.a if math.dist(p, self.a) <= math.dist(p, self.b) else self.b


def _arc_from_bulge(owner: str, layer: str, index: int, a: Pt, b: Pt, bulge: float) -> Piece:
    """A polyline segment with a bulge: the arc it stands for.

    The bulge is tan(sweep / 4), positive counter-clockwise (the DXF rule).
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    chord = math.hypot(dx, dy)
    theta = 4.0 * math.atan(bulge)
    if chord == 0.0 or theta == 0.0:
        return Piece(owner, layer, index, a, b)
    offset = chord / (2.0 * math.tan(theta / 2.0))
    nx, ny = -dy / chord, dx / chord
    centre = ((a[0] + b[0]) / 2.0 + nx * offset, (a[1] + b[1]) / 2.0 + ny * offset)
    radius = abs(chord / (2.0 * math.sin(theta / 2.0)))
    first = a if theta > 0 else b
    start = math.atan2(first[1] - centre[1], first[0] - centre[0])
    return Piece(owner, layer, index, a, b, centre, radius, start, abs(theta))


def _arc_piece(rec: EntityRecord, *, full: bool) -> Piece | None:
    if not rec.points or rec.radius is None:
        return None
    centre = (float(rec.points[0][0]), float(rec.points[0][1]))
    r = float(rec.radius)
    if full or rec.angles is None:
        a = b = (centre[0] + r, centre[1])
        return Piece(rec.handle, rec.layer, 0, a, b, centre, r, 0.0, _TWO_PI)
    s = math.radians(float(rec.angles[0]))
    e = math.radians(float(rec.angles[1]))
    sweep = (e - s) % _TWO_PI or _TWO_PI
    a = (centre[0] + r * math.cos(s), centre[1] + r * math.sin(s))
    b = (centre[0] + r * math.cos(e), centre[1] + r * math.sin(e))
    return Piece(rec.handle, rec.layer, 0, a, b, centre, r, s, sweep)


def entity_pieces(rec: EntityRecord) -> tuple[list[Piece], tuple[Pt, Pt] | None] | None:
    """The pieces of one record and its two ends (None when it has none).

    None for a record that draws no curve or lacks what it needs.
    """
    kind = rec.type
    if kind == "LINE":
        if len(rec.points) < 2:
            return None
        a, b = tuple(rec.points[0]), tuple(rec.points[1])
        return [Piece(rec.handle, rec.layer, 0, a, b)], (a, b)
    if kind in ("LWPOLYLINE", "POLYLINE"):
        points = [(float(p[0]), float(p[1])) for p in rec.points]
        if len(points) < 2:
            return None
        bulges = list(rec.bulges) + [0.0] * (len(points) - len(rec.bulges))
        count = len(points) if rec.closed else len(points) - 1
        pieces = [
            _arc_from_bulge(
                rec.handle,
                rec.layer,
                i,
                points[i],
                points[(i + 1) % len(points)],
                float(bulges[i]),
            )
            for i in range(count)
        ]
        return pieces, None if rec.closed else (points[0], points[-1])
    if kind == "ARC":
        piece = _arc_piece(rec, full=False)
        return None if piece is None else ([piece], (piece.a, piece.b))
    if kind == "CIRCLE":
        piece = _arc_piece(rec, full=True)
        return None if piece is None else ([piece], None)
    return None


def network_layers(snap: Snapshot) -> list[dict]:
    """The layers ``vocab.classify_layer`` calls piping or electrical with
    confidence >= 0.9, with the classification as evidence."""
    names = set(snap.layers) | {rec.layer for rec in snap.records}
    rows = []
    for name in sorted(names):
        meaning = classify_layer(name)
        confidence = float(meaning.get("confidence") or 0.0)
        if meaning.get("discipline") in NETWORK_DISCIPLINES and confidence >= NETWORK_CONFIDENCE:
            rows.append(
                {
                    "layer": name,
                    "discipline": meaning.get("discipline"),
                    "service": meaning.get("service"),
                    "confidence": confidence,
                    "keyword": meaning.get("keyword"),
                }
            )
    return rows


def default_gap(points: Iterable[Pt], tol: float) -> float:
    """0.2 % of the diagonal of the points' box, never less than ten ``tol``."""
    xs: list[float] = []
    ys: list[float] = []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    diagonal = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if xs else 0.0
    return max(GAP_FRACTION * diagonal, GAP_FLOOR_TOLS * tol)


def _positive(name: str, value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number; got {value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive number; got {value!r}")
    return number


def _layer_list(layers) -> set[str]:
    if isinstance(layers, str):
        layers = [layers]
    names = {str(name) for name in (layers or []) if str(name)}
    if not names:
        raise ValueError("layers: name at least one layer to check")
    return names


class _Grid:
    """A uniform grid of item indices by box; oversized boxes on a side list."""

    def __init__(self, cell: float) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.oversize: list[int] = []

    def _span(self, box) -> tuple[int, int, int, int]:
        c = self.cell
        return (
            math.floor(box[0] / c),
            math.floor(box[1] / c),
            math.floor(box[2] / c),
            math.floor(box[3] / c),
        )

    def add(self, item: int, box) -> None:
        x0, y0, x1, y1 = self._span(box)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > _MAX_CELLS:
            self.oversize.append(item)
            return
        for ix in range(x0, x1 + 1):
            for iy in range(y0, y1 + 1):
                self.cells[(ix, iy)].append(item)

    def near(self, box) -> list[int]:
        x0, y0, x1, y1 = self._span(box)
        found: set[int] = set(self.oversize)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > _MAX_CELLS:
            for items in self.cells.values():
                found.update(items)
            return sorted(found)
        for ix in range(x0, x1 + 1):
            for iy in range(y0, y1 + 1):
                found.update(self.cells.get((ix, iy), ()))
        return sorted(found)


def _around(p: Pt, r: float) -> tuple[float, float, float, float]:
    return (p[0] - r, p[1] - r, p[0] + r, p[1] + r)


def _box_distance(p: Pt, box) -> float:
    dx = max(box[0] - p[0], 0.0, p[0] - box[2])
    dy = max(box[1] - p[1], 0.0, p[1] - box[3])
    return math.hypot(dx, dy)


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _crossing(p: Piece, q: Piece, tol: float) -> Pt | None:
    """Where two straight pieces cross strictly inside both, away from their ends."""
    rx, ry = p.b[0] - p.a[0], p.b[1] - p.a[1]
    sx, sy = q.b[0] - q.a[0], q.b[1] - q.a[1]
    denominator = _cross(rx, ry, sx, sy)
    if denominator == 0.0:
        return None
    qx, qy = q.a[0] - p.a[0], q.a[1] - p.a[1]
    t = _cross(qx, qy, sx, sy) / denominator
    u = _cross(qx, qy, rx, ry) / denominator
    if not (0.0 < t < 1.0 and 0.0 < u < 1.0):
        return None
    point = (p.a[0] + t * rx, p.a[1] + t * ry)
    if min(math.dist(point, e) for e in (p.a, p.b, q.a, q.b)) <= tol:
        return None
    return point


def _overlaps(own: Piece, point: Pt, other: Piece, tol: float) -> bool:
    """True when ``other`` lies along ``own`` and runs from ``point`` into it:
    a line drawn twice, or a line over a polyline. Such a piece is not a join -
    otherwise a pipe drawn twice would join itself and never dangle. A
    collinear piece that continues the other way (a butt joint) is a join.

    Arcs follow the same rule: an arc on the same circle (centre and radius
    within ``tol``) that covers ``own`` just inside ``point`` - an ARC drawn
    twice, an ARC over a bulged polyline segment - is not a join; a straight
    piece never lies along an arc."""
    if (own.centre is None) != (other.centre is None):
        return False
    if own.centre is not None:
        if math.dist(own.centre, other.centre) > tol or abs(own.radius - other.radius) > tol:
            return False
        if own.radius <= 0.0:
            return False
        cx, cy = own.centre
        # ``own`` runs counter-clockwise from ``start`` through ``sweep``; probe
        # 2 tol of arc (at most half the sweep) inside it from whichever end
        # ``point`` is.
        step = min(2.0 * tol / own.radius, own.sweep / 2.0)
        first = (cx + own.radius * math.cos(own.start), cy + own.radius * math.sin(own.start))
        last_angle = own.start + own.sweep
        last = (cx + own.radius * math.cos(last_angle), cy + own.radius * math.sin(last_angle))
        angle = (
            own.start + step
            if math.dist(point, first) <= math.dist(point, last)
            else (last_angle - step)
        )
        probe = (cx + own.radius * math.cos(angle), cy + own.radius * math.sin(angle))
        return math.dist(probe, other.nearest(probe)) <= tol
    far = own.b if math.dist(point, own.a) <= math.dist(point, own.b) else own.a
    length = math.dist(point, far)
    if length == 0.0:
        return False
    ux, uy = (far[0] - point[0]) / length, (far[1] - point[1]) / length
    along = []
    for p in (other.a, other.b):
        dx, dy = p[0] - point[0], p[1] - point[1]
        if abs(dy * ux - dx * uy) > tol:
            return False
        along.append(dx * ux + dy * uy)
    return max(along) > tol


def _analyse_space(
    space: str,
    records: list[EntityRecord],
    wanted: set[str],
    gap: float,
    tol: float,
    out: dict,
    skipped: Counter,
) -> tuple[int, int]:
    net: list[Piece] = []
    counts: dict[str, int] = {}
    closed: set[str] = set()
    # handle, layer, "start" | "end", the point, the index in `net` of the piece it ends
    ends: list[tuple[str, str, str, Pt, int]] = []
    context: list[Piece] = []
    boxes: list[tuple[float, float, float, float]] = []
    for rec in records:
        if rec.type == "INSERT":
            if rec.bbox is not None:
                boxes.append(tuple(float(v) for v in rec.bbox))
            continue
        if rec.type not in _CONTEXT_TYPES:
            continue
        built = entity_pieces(rec)
        if built is None:
            skipped[rec.type] += 1
            continue
        pieces, entity_ends = built
        if rec.layer in wanted and rec.type in _NETWORK_TYPES:
            first = len(net)
            net.extend(pieces)
            counts[rec.handle] = len(pieces)
            if entity_ends is None:
                closed.add(rec.handle)
            else:
                ends.append((rec.handle, rec.layer, "start", entity_ends[0], first))
                ends.append((rec.handle, rec.layer, "end", entity_ends[1], len(net) - 1))
        else:
            context.extend(pieces)
    if not net:
        return 0, 0

    cell = max(8.0 * gap, 100.0 * tol)
    net_grid = _Grid(cell)
    for k, piece in enumerate(net):
        net_grid.add(k, piece.box())
    end_grid = _Grid(cell)
    for k, (_h, _l, _w, point, _piece) in enumerate(ends):
        end_grid.add(k, _around(point, 0.0))
    context_grid = _Grid(cell)
    for k, piece in enumerate(context):
        context_grid.add(k, piece.box())
    box_grid = _Grid(cell)
    for k, box in enumerate(boxes):
        box_grid.add(k, box)

    seen_pairs: set = set()
    for handle, layer, which, point, own_k in sorted(ends, key=lambda e: (e[0], e[2])):
        own = net[own_k]
        pieces_of_owner = counts[handle]
        own_index = 0 if which == "start" else pieces_of_owner - 1
        reach = _around(point, gap)
        best_end: tuple[float, int] | None = None
        for k in end_grid.near(reach):
            other_handle, _ol, other_which, other_point, other_k = ends[k]
            if other_handle == handle and (other_which == which or pieces_of_owner < 3):
                continue
            if _overlaps(own, point, net[other_k], tol):
                continue
            distance = math.dist(point, other_point)
            if best_end is None or distance < best_end[0]:
                best_end = (distance, k)
        best_piece: tuple[float, int, Pt] | None = None
        for k in net_grid.near(reach):
            piece = net[k]
            if piece.owner == handle and abs(piece.index - own_index) <= 1:
                continue
            if _overlaps(own, point, piece, tol):
                continue
            foot = piece.nearest(point)
            distance = math.dist(point, foot)
            if best_piece is None or distance < best_piece[0]:
                best_piece = (distance, k, foot)
        nearest = min(
            best_end[0] if best_end is not None else math.inf,
            best_piece[0] if best_piece is not None else math.inf,
        )
        if nearest <= tol:
            continue  # joined
        touch = _around(point, tol)
        if any(
            math.dist(point, context[k].nearest(point)) <= tol for k in context_grid.near(touch)
        ) or any(_box_distance(point, boxes[k]) <= tol for k in box_grid.near(touch)):
            continue  # attached
        if nearest <= gap:
            if best_end is not None and (best_piece is None or best_end[0] <= best_piece[0]):
                other_handle, other_layer, other_which, other_point, _k = ends[best_end[1]]
                key = frozenset({(handle, which), (other_handle, other_which)})
                kind, distance, to = "end_end", best_end[0], other_point
            else:
                piece = net[best_piece[1]]
                other_handle, other_layer = piece.owner, piece.layer
                key = (handle, which, other_handle)
                kind, distance, to = "end_segment", best_piece[0], best_piece[2]
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            out["near_miss"].append(
                {
                    "handles": [handle, other_handle],
                    "layers": [layer, other_layer],
                    "kind": kind,
                    "gap": distance,
                    "at": [point[0], point[1]],
                    "to": [to[0], to[1]],
                    "space": space,
                }
            )
            continue
        out["dangling"].append(
            {
                "handle": handle,
                "layer": layer,
                "end": which,
                "at": [point[0], point[1]],
                "space": space,
            }
        )

    for k, piece in enumerate(net):
        if piece.centre is not None:
            continue
        for m in net_grid.near(piece.box()):
            if m <= k:
                continue
            other = net[m]
            if other.centre is not None or other.layer != piece.layer:
                continue
            if other.owner == piece.owner:
                apart = abs(other.index - piece.index)
                wraps = piece.owner in closed and apart == counts[piece.owner] - 1
                if apart <= 1 or wraps:
                    continue
            point = _crossing(piece, other, tol)
            if point is None:
                continue
            out["crossing"].append(
                {
                    "handles": [piece.owner, other.owner],
                    "layer": piece.layer,
                    "at": [point[0], point[1]],
                    "space": space,
                }
            )
    return len(counts), len(ends)


def topology_findings(
    snap: Snapshot, *, layers, gap: float | None = None, tol: float = DEFAULT_TOL
) -> dict:
    """Dangling ends, near misses and interior crossings on ``layers`` (spec §11).

    Refused before any work: an empty ``layers``, a non-positive ``tol`` or
    ``gap``, and a ``gap`` that does not exceed ``tol``.
    """
    tol = _positive("tol", tol)
    wanted = _layer_list(layers)
    if gap is not None:
        gap = _positive("gap", gap)
        if gap <= tol:
            raise ValueError(
                f"gap ({gap:g}) must exceed tol ({tol:g}): an end within tol is joined, and a "
                "near miss is a distance between the two"
            )
    by_space: dict[str, list[EntityRecord]] = defaultdict(list)
    for rec in snap.records:
        by_space[rec.space].append(rec)
    if gap is None:
        points: list[Pt] = []
        for rec in snap.records:
            if rec.layer in wanted and rec.type in _NETWORK_TYPES:
                built = entity_pieces(rec)
                if built is not None:
                    for piece in built[0]:
                        points.extend((piece.a, piece.b))
        gap = default_gap(points, tol)
    out: dict = {kind: [] for kind in FINDING_KINDS}
    skipped: Counter = Counter()
    entities = end_count = 0
    for space in sorted(by_space):
        n_entities, n_ends = _analyse_space(space, by_space[space], wanted, gap, tol, out, skipped)
        entities += n_entities
        end_count += n_ends
    out["dangling"].sort(key=lambda r: (r["space"], r["handle"], r["end"]))
    out["near_miss"].sort(key=lambda r: (r["space"], r["handles"], r["at"]))
    out["crossing"].sort(key=lambda r: (r["space"], r["handles"], r["at"]))
    return {
        "layers": sorted(wanted),
        "gap": gap,
        "tol": tol,
        **out,
        "stats": {
            "entities": entities,
            "ends": end_count,
            "skipped": dict(sorted(skipped.items())),
        },
    }


def cap_findings(findings: dict, limit: int) -> dict:
    """``findings`` with each list cut to ``limit`` rows, plus ``counts`` and
    ``truncated`` per kind."""
    if int(limit) < 1:
        raise ValueError(f"limit must be at least 1; got {limit!r}")
    capped = dict(findings)
    capped["counts"] = {kind: len(findings.get(kind) or []) for kind in FINDING_KINDS}
    capped["truncated"] = {
        kind: max(0, capped["counts"][kind] - int(limit)) for kind in FINDING_KINDS
    }
    for kind in FINDING_KINDS:
        capped[kind] = list(findings.get(kind) or [])[: int(limit)]
    return capped
