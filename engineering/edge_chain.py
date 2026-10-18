"""Chain loose LINE / ARC / LWPOLYLINE edges into one closed loop, without any CAD engine.

The headless engine borrows ezdxf's ``edgeminer`` for this. ActiveX exposes no edge graph
(BOUNDARY is command-only), so ``boundary_from_entities`` on the live engine reads each
entity's geometry through COM and chains it here, then stores the loop as one LWPOLYLINE
whose arc edges carry bulges.

Pure functions on plain tuples: no COM, no ezdxf, unit-testable anywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]


@dataclass(frozen=True)
class Edge:
    """A straight or circular edge, stored in its drawn direction.

    ``bulge`` follows the DXF convention for travelling ``start`` -> ``end``:
    ``tan(sweep / 4)``, positive when the arc runs counter-clockwise.
    """

    start: Point
    end: Point
    bulge: float = 0.0
    source: str = ""

    def reversed(self) -> Edge:
        return Edge(self.end, self.start, -self.bulge, self.source)


def line_edge(p: Point, q: Point, source: str = "") -> Edge:
    return Edge((float(p[0]), float(p[1])), (float(q[0]), float(q[1])), 0.0, source)


def arc_edge(
    cx: float, cy: float, radius: float, start_rad: float, end_rad: float, source: str = ""
) -> Edge:
    """An AutoCAD ARC: always counter-clockwise from ``start_rad`` to ``end_rad`` (radians)."""
    sweep = (end_rad - start_rad) % (2.0 * math.pi)
    if sweep == 0.0:
        sweep = 2.0 * math.pi
    p = (cx + radius * math.cos(start_rad), cy + radius * math.sin(start_rad))
    q = (cx + radius * math.cos(end_rad), cy + radius * math.sin(end_rad))
    return Edge(p, q, math.tan(sweep / 4.0), source)


def polyline_edges(coords, bulges, closed: bool, source: str = "") -> list[Edge]:
    """Edges of a polyline given flat ``(x0, y0, x1, y1, ...)`` coordinates and one bulge per vertex."""
    n = len(coords) // 2
    pts = [(float(coords[2 * i]), float(coords[2 * i + 1])) for i in range(n)]
    out = []
    last = n if closed else n - 1
    for i in range(last):
        b = float(bulges[i]) if i < len(bulges) else 0.0
        out.append(Edge(pts[i], pts[(i + 1) % n], b, source))
    return out


def _near(a: Point, b: Point, tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol and math.dist(a, b) <= tol


def chain_loop(edges: list[Edge], tol: float = 1e-9) -> list[Edge] | None:
    """Order and orient *all* edges into one closed loop, or ``None`` when no such loop exists.

    A depth-first walk with backtracking: with more than two edges meeting at a vertex the first
    choice may strand the rest, so the search tries each in turn. Edge counts here are small
    (a part outline); the walk is bounded all the same.
    """
    n = len(edges)
    if n < 2:
        return None
    tol = max(float(tol), 1e-9)
    budget = [200_000]

    def walk(path: list[Edge], used: list[bool]) -> list[Edge] | None:
        if len(path) == n:
            return path if _near(path[-1].end, path[0].start, tol) else None
        budget[0] -= 1
        if budget[0] < 0:
            return None
        tail = path[-1].end
        for i, e in enumerate(edges):
            if used[i]:
                continue
            for cand in (e, e.reversed()):
                if _near(cand.start, tail, tol):
                    used[i] = True
                    found = walk(path + [cand], used)
                    used[i] = False
                    if found:
                        return found
        return None

    used = [False] * n
    used[0] = True
    return walk([edges[0]], used)


def dangling_endpoint(edges: list[Edge], tol: float = 1e-9) -> tuple[Point, Point]:
    """The first endpoint with no partner and the nearest other endpoint, so a refusal can name the gap."""
    tol = max(float(tol), 1e-9)
    ends: list[Point] = []
    for e in edges:
        ends += [e.start, e.end]
    for p in ends:
        if sum(1 for q in ends if _near(p, q, tol)) < 2:
            others = [q for q in ends if not _near(p, q, tol)]
            return p, min(others, key=lambda q: math.dist(p, q), default=p)
    return ends[0], ends[-1]


def loop_vertices(chain: list[Edge]) -> list[tuple[float, float, float]]:
    """``(x, y, bulge)`` per vertex for a closed LWPOLYLINE; the closing edge is implied."""
    return [(e.start[0], e.start[1], e.bulge) for e in chain]


def chain_path(edges: list[Edge], tol: float = 1e-9) -> list[Edge] | None:
    """Order and orient *all* edges into one open path, or ``None``.

    An open path has exactly two free endpoints (degree 1); a closed loop has none and a branching
    drawing has more, so neither is a path. The walk starts at one free end.
    """
    n = len(edges)
    if n < 2:
        return None
    tol = max(float(tol), 1e-9)
    ends: list[Point] = []
    for e in edges:
        ends += [e.start, e.end]
    free = [p for p in ends if sum(1 for q in ends if _near(p, q, tol)) == 1]
    if len(free) != 2:
        return None
    start = free[0]

    def walk(path: list[Edge], used: list[bool]) -> list[Edge] | None:
        if len(path) == n:
            return path
        tail = path[-1].end
        for i, e in enumerate(edges):
            if used[i]:
                continue
            for cand in (e, e.reversed()):
                if _near(cand.start, tail, tol):
                    used[i] = True
                    found = walk(path + [cand], used)
                    used[i] = False
                    if found:
                        return found
        return None

    for i, e in enumerate(edges):
        for cand in (e, e.reversed()):
            if _near(cand.start, start, tol):
                used = [False] * n
                used[i] = True
                found = walk([cand], used)
                if found:
                    return found
    return None


def path_vertices(path: list[Edge]) -> list[tuple[float, float, float]]:
    """``(x, y, bulge)`` per vertex of an open path: every start, then the last end with bulge 0."""
    out = [(e.start[0], e.start[1], e.bulge) for e in path]
    out.append((path[-1].end[0], path[-1].end[1], 0.0))
    return out
