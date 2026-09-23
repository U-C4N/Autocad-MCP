"""The planar-face finder: straight segments in, the closed faces they bound out.

A room is the floor the walls enclose, so its area is a *face* of the planar
graph the wall outlines draw - never the axis rectangle and never a number the
caller types. This module finds those faces from plain segments, which is what
lets `arch_room` measure a room and `arch_rooms_detect` read a foreign plan made
of LINEs on both engines: the live engine has no `boundary_trace`, and it does
not need one.

The algorithm, in the order it runs:

1. **Merge.** Endpoints closer than ``tol`` become one vertex (the first one
   seen keeps its coordinates), so a corner drawn 0.4 mm short still closes.
2. **Split.** Every segment is split at every point where another one crosses
   it and at every vertex that lies on it within ``tol`` - the T-touch, where a
   partition wall stops on the face of the wall it meets. Collinear overlaps
   fall out of the same rule: each overlapping end splits the other segment and
   the coincident pieces become one edge.
3. **Prune.** An edge that ends in a vertex of degree one bounds nothing - a
   dangling line, an overshoot past a crossing - and is removed, repeatedly,
   until no such edge is left.
4. **Walk.** Each undirected edge is two half-edges. From the half-edge
   ``u -> v`` the walk continues at ``v`` along the next edge *clockwise* from
   ``v -> u``, which keeps the face on the left. A bounded face comes out
   counter-clockwise (positive signed area); the outer boundary of each
   connected piece comes out clockwise.
5. **Nest.** A clockwise boundary that lies inside a bounded face of *another*
   piece is a hole in the smallest such face: the island a column makes in a
   room, or the whole inner outline of a ring of walls inside its outer outline.
   A face's ``area`` is its loop's area minus its holes'.

Areas come from `engineering.measure.polygon_area_perimeter`, the same
arithmetic `analysis_measure_entity` reports; this module computes the *sign* of
the shoelace itself only to tell a face from a boundary, because the shared
function returns the magnitude by design.

Pairs of segments are tested against each other, which is quadratic: a floor
plan of a few thousand wall segments is well inside it, a city block is not.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from engineering.measure import polygon_area_perimeter

Pt = tuple[float, float]

__all__ = ["DEFAULT_TOL", "Face", "face_containing", "planar_faces", "tagged_faces"]

#: Endpoints closer than this (drawing units, mm) are one vertex.
DEFAULT_TOL = 1.0

_EPS = 1e-12


@dataclass(frozen=True)
class Face:
    """One bounded face.

    ``loop`` is counter-clockwise and starts at its lowest, then leftmost,
    vertex. ``holes`` are the clockwise outer boundaries of the pieces nested
    inside it. ``area`` (mm^2) is the loop's area minus the holes'; ``centroid``
    is the centroid of that net region.
    """

    loop: tuple[Pt, ...]
    area: float
    centroid: Pt
    holes: tuple[tuple[Pt, ...], ...] = ()


class _Vertices:
    """Snap points to vertices: a point within ``tol`` of a vertex *is* that vertex."""

    def __init__(self, tol: float) -> None:
        self.tol = tol
        self.points: list[Pt] = []
        self._grid: dict[tuple[int, int], list[int]] = {}

    def _cell(self, p: Pt) -> tuple[int, int]:
        return (math.floor(p[0] / self.tol), math.floor(p[1] / self.tol))

    def index(self, p: Pt) -> int:
        cx, cy = self._cell(p)
        best: int | None = None
        best_d = self.tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self._grid.get((cx + dx, cy + dy), ()):
                    d = math.dist(self.points[i], p)
                    if d <= best_d:
                        best, best_d = i, d
        if best is not None:
            return best
        i = len(self.points)
        self.points.append((float(p[0]), float(p[1])))
        self._grid.setdefault((cx, cy), []).append(i)
        return i


def _number(value, where: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{where}: must be finite, got {value!r}")
    return number


def _point(value, where: str) -> Pt:
    try:
        x, y = value[0], value[1]
    except (TypeError, IndexError, KeyError):
        raise ValueError(f"{where}: expected [x, y], got {value!r}") from None
    return (_number(x, f"{where}[0]"), _number(y, f"{where}[1]"))


def _segments(segments: Iterable) -> list[tuple[Pt, Pt, object]]:
    """``(p1, p2)`` or ``(p1, p2, tag)``; the tag rides along to the edges."""
    out: list[tuple[Pt, Pt, object]] = []
    for i, seg in enumerate(segments):
        try:
            count = len(seg)
        except TypeError:
            raise ValueError(f"segments[{i}]: expected (p1, p2) or (p1, p2, tag)") from None
        if count not in (2, 3):
            raise ValueError(
                f"segments[{i}]: expected (p1, p2) or (p1, p2, tag), got {count} items"
            )
        tag = seg[2] if count == 3 else None
        out.append((_point(seg[0], f"segments[{i}][0]"), _point(seg[1], f"segments[{i}][1]"), tag))
    return out


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _crossing(p1: Pt, p2: Pt, q1: Pt, q2: Pt) -> Pt | None:
    """Where two segments meet, ends included; None for parallel or apart."""
    rx, ry = p2[0] - p1[0], p2[1] - p1[1]
    sx, sy = q2[0] - q1[0], q2[1] - q1[1]
    denom = _cross(rx, ry, sx, sy)
    if abs(denom) <= _EPS * math.hypot(rx, ry) * math.hypot(sx, sy):
        return None
    qx, qy = q1[0] - p1[0], q1[1] - p1[1]
    t = _cross(qx, qy, sx, sy) / denom
    u = _cross(qx, qy, rx, ry) / denom
    if -_EPS <= t <= 1.0 + _EPS and -_EPS <= u <= 1.0 + _EPS:
        return (p1[0] + t * rx, p1[1] + t * ry)
    return None


def _along(p: Pt, a: Pt, b: Pt) -> tuple[float, float]:
    """(distance along a->b of p's foot, distance of p from the line)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    px, py = p[0] - a[0], p[1] - a[1]
    return (px * ux + py * uy, abs(_cross(ux, uy, px, py)))


def _signed_area(points: Sequence[Pt]) -> float:
    total = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _area(points: Sequence[Pt]) -> float:
    return polygon_area_perimeter([(x, y, 0.0) for x, y in points], True)[0]


def _centroid(points: Sequence[Pt]) -> tuple[Pt, float]:
    """(centroid, signed area) of a simple loop; a zero-area loop answers its vertex mean."""
    signed = _signed_area(points)
    if abs(signed) < _EPS:
        n = len(points)
        return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n), 0.0
    cx = cy = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        w = x1 * y2 - x2 * y1
        cx += (x1 + x2) * w
        cy += (y1 + y2) * w
    return (cx / (6.0 * signed), cy / (6.0 * signed)), signed


def _inside(point: Pt, loop: Sequence[Pt]) -> bool:
    """Even-odd ray cast; a point exactly on an edge may answer either way."""
    x, y = point
    inside = False
    n = len(loop)
    for i in range(n):
        x1, y1 = loop[i]
        x2, y2 = loop[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xs = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xs > x:
                inside = not inside
    return inside


def _start_low_left(loop: list[int], points: list[Pt]) -> list[int]:
    k = min(range(len(loop)), key=lambda i: (points[loop[i]][1], points[loop[i]][0]))
    return loop[k:] + loop[:k]


def tagged_faces(
    segments, *, tol: float = DEFAULT_TOL
) -> tuple[tuple[Face, tuple[frozenset, ...]], ...]:
    """`planar_faces`, plus, per face, the tag set of every edge it is bounded by.

    An edge carries the tags of every input segment that covers it, so a reader
    can ask whether a face is enclosed *only* by segments of a kind - which is
    what `detect_rooms` turns into a confidence.
    """
    tol = _number(tol, "tol")
    if tol <= 0.0:
        raise ValueError(f"tol: must be greater than zero, got {tol:g}")
    raw = _segments(segments)
    verts = _Vertices(tol)

    # 1. merge endpoints
    snapped: list[tuple[int, int, object]] = []
    for p1, p2, tag in raw:
        a, b = verts.index(p1), verts.index(p2)
        if a != b:
            snapped.append((a, b, tag))

    # 2. split at crossings and at vertices lying on a segment
    splits: list[set[int]] = [set() for _ in snapped]
    boxes = []
    for a, b, _tag in snapped:
        (ax, ay), (bx, by) = verts.points[a], verts.points[b]
        boxes.append((min(ax, bx) - tol, min(ay, by) - tol, max(ax, bx) + tol, max(ay, by) + tol))
    for i in range(len(snapped)):
        ai, bi, _ = snapped[i]
        for j in range(i + 1, len(snapped)):
            bx0, by0, bx1, by1 = boxes[j]
            if boxes[i][0] > bx1 or bx0 > boxes[i][2] or boxes[i][1] > by1 or by0 > boxes[i][3]:
                continue
            aj, bj, _ = snapped[j]
            hit = _crossing(verts.points[ai], verts.points[bi], verts.points[aj], verts.points[bj])
            if hit is not None:
                v = verts.index(hit)
                splits[i].add(v)
                splits[j].add(v)
    for i, (a, b, _tag) in enumerate(snapped):
        pa, pb = verts.points[a], verts.points[b]
        length = math.dist(pa, pb)
        x0, y0, x1, y1 = boxes[i]
        for v, p in enumerate(verts.points):
            if v in (a, b) or not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1):
                continue
            s, off = _along(p, pa, pb)
            if off <= tol and tol < s < length - tol:
                splits[i].add(v)

    edges: dict[tuple[int, int], set] = {}
    for i, (a, b, tag) in enumerate(snapped):
        pa, pb = verts.points[a], verts.points[b]
        chain = (
            [a]
            + sorted(
                (v for v in splits[i] if v not in (a, b)),
                key=lambda v: _along(verts.points[v], pa, pb)[0],
            )
            + [b]
        )
        for u, v in zip(chain, chain[1:], strict=False):
            if u != v:
                edges.setdefault((min(u, v), max(u, v)), set()).add(tag)

    # 3. prune dangling edges
    adjacent: dict[int, set[int]] = {}
    for u, v in edges:
        adjacent.setdefault(u, set()).add(v)
        adjacent.setdefault(v, set()).add(u)
    loose = [v for v, ns in adjacent.items() if len(ns) == 1]
    while loose:
        v = loose.pop()
        for w in list(adjacent.get(v, ())):
            adjacent[w].discard(v)
            adjacent[v].discard(w)
            edges.pop((min(v, w), max(v, w)), None)
            if len(adjacent[w]) == 1:
                loose.append(w)
    adjacent = {v: ns for v, ns in adjacent.items() if ns}

    # 4. walk: the next half-edge is the next one clockwise at the far vertex
    pts = verts.points
    order: dict[int, list[int]] = {
        v: sorted(ns, key=lambda w, v=v: math.atan2(pts[w][1] - pts[v][1], pts[w][0] - pts[v][0]))
        for v, ns in adjacent.items()
    }
    seen: set[tuple[int, int]] = set()
    cycles: list[list[int]] = []
    for u, ns in adjacent.items():
        for v in ns:
            if (u, v) in seen:
                continue
            cycle: list[int] = []
            a, b = u, v
            while (a, b) not in seen:
                seen.add((a, b))
                cycle.append(a)
                around = order[b]
                a, b = b, around[around.index(a) - 1]
            cycles.append(cycle)

    component: dict[int, int] = {}
    for start in adjacent:
        if start in component:
            continue
        stack = [start]
        component[start] = start
        while stack:
            v = stack.pop()
            for w in adjacent[v]:
                if w not in component:
                    component[w] = start
                    stack.append(w)

    bounded: list[list[int]] = []
    boundaries: list[list[int]] = []
    for cycle in cycles:
        signed = _signed_area([pts[v] for v in cycle])
        if signed > _EPS:
            bounded.append(_start_low_left(cycle, pts))
        elif signed < -_EPS:
            boundaries.append(cycle)

    # 5. nest each piece's outer boundary in the smallest face of another piece
    gross = [_area([pts[v] for v in loop]) for loop in bounded]
    holes: list[list[list[int]]] = [[] for _ in bounded]
    for boundary in boundaries:
        probe = pts[boundary[0]]
        home = None
        for k, loop in enumerate(bounded):
            if component[loop[0]] == component[boundary[0]]:
                continue
            if _inside(probe, [pts[v] for v in loop]) and (home is None or gross[k] < gross[home]):
                home = k
        if home is not None:
            holes[home].append(_start_low_left(boundary, pts))

    out: list[tuple[Face, tuple[frozenset, ...]]] = []
    for k, loop in enumerate(bounded):
        outer = [pts[v] for v in loop]
        (gx, gy), g_signed = _centroid(outer)
        net = gross[k]
        mx, my, weight = gx * g_signed, gy * g_signed, g_signed
        hole_loops = []
        for hole in holes[k]:
            ring = [pts[v] for v in hole]
            (hx, hy), h_signed = _centroid(ring)  # clockwise, so negative: it subtracts
            net -= _area(ring)
            mx, my, weight = mx + hx * h_signed, my + hy * h_signed, weight + h_signed
            hole_loops.append(tuple(ring))
        centroid = (mx / weight, my / weight) if abs(weight) > _EPS else (gx, gy)
        tags = []
        for ring in [loop, *holes[k]]:
            for i in range(len(ring)):
                u, v = ring[i], ring[(i + 1) % len(ring)]
                tags.append(frozenset(edges[(min(u, v), max(u, v))]))
        face = Face(loop=tuple(outer), area=net, centroid=centroid, holes=tuple(hole_loops))
        out.append((face, tuple(tags)))
    out.sort(key=lambda item: (item[0].loop[0][1], item[0].loop[0][0], item[0].area))
    return tuple(out)


def planar_faces(segments, *, tol: float = DEFAULT_TOL) -> tuple[Face, ...]:
    """Every bounded face the segments enclose, CCW, net of nested pieces, in mm^2.

    Refused by path, before anything is computed: a segment that is not
    ``(p1, p2)`` or ``(p1, p2, tag)``, a coordinate that is not a finite
    number, a ``tol`` that is not positive.
    """
    return tuple(face for face, _tags in tagged_faces(segments, tol=tol))


def face_containing(faces: Iterable[Face], point) -> Face | None:
    """The smallest face whose loop holds ``point`` and none of whose holes do."""
    p = _point(point, "point")
    found = [
        face
        for face in faces
        if _inside(p, face.loop) and not any(_inside(p, hole) for hole in face.holes)
    ]
    return min(found, key=lambda face: face.area) if found else None
