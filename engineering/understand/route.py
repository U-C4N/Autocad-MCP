"""Orthogonal lengths on a layout: Manhattan distance and the rectilinear MST.

A pipe or cable on a plant floor runs along the building grid, so its length
between two points is estimated as |dx| + |dy| - never the diagonal. A run that
joins more than two pieces of equipment is estimated as the minimum spanning
tree of their positions under that distance (Prim's algorithm); a two-ended run
is simply |dx| + |dy|. This is the tree on the given points only - no Steiner
points are added - so it is the length a drafter gets by routing each
connection separately, and it is exact for the points given.

A route is drawn along x first, then y (`route_legs`): deterministic, and
stated on every method sheet that uses it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from engineering.understand.snapshot import Pt

__all__ = ["manhattan", "rmst", "route_legs"]


def manhattan(a: Pt, b: Pt) -> float:
    """|dx| + |dy| between two points."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def rmst(points: Sequence[Pt]) -> tuple[float, tuple[tuple[int, int], ...]]:
    """Rectilinear minimum spanning tree: (total length, edges as (from, to) indices).

    Prim's algorithm from point 0; each edge is (the tree vertex it grows from,
    the new vertex), in the order the tree grows. Ties go to the lower index,
    so the answer is deterministic. Fewer than two points: (0.0, ()).
    """
    n = len(points)
    if n < 2:
        return 0.0, ()
    in_tree = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0.0
    total = 0.0
    edges: list[tuple[int, int]] = []
    for _ in range(n):
        k = min((i for i in range(n) if not in_tree[i]), key=lambda i: (best[i], i))
        in_tree[k] = True
        if parent[k] >= 0:
            edges.append((parent[k], k))
            total += best[k]
        for j in range(n):
            if not in_tree[j]:
                d = manhattan(points[k], points[j])
                if d < best[j]:
                    best[j], parent[j] = d, k
    return total, tuple(edges)


def route_legs(a: Pt, b: Pt) -> tuple[tuple[Pt, Pt], ...]:
    """The orthogonal route from a to b: along x first, then y; zero-length legs dropped."""
    corner = (b[0], a[1])
    return tuple(leg for leg in ((a, corner), (corner, b)) if leg[0] != leg[1])
