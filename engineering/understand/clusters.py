"""Separate bodies of model-space content: sheets, plan copies, details.

A foreign model space is often several drawings side by side - two copies of
one plan, a detail beside it, a legend. Every reader after this one must know
which body a tag or a room belongs to, or it matches a tag in one copy against
the same tag in the other.

The algorithm, in the order it runs:

1. **Raster.** The box of the records is divided into square cells of
   ``cell`` drawing units (default :data:`CELL_FRACTION` of its diagonal, so
   the grid never exceeds about 50 x 50 cells). Every record marks the cells
   its box covers.
2. **Connect.** Occupied cells that touch, corners included, are one
   component; a record belongs to the component of its cells. Two bodies stay
   apart when an empty cell separates them - a gap of two cells is always
   enough.
3. **Absorb.** Components whose boxes intersect are merged, repeatedly: a tag
   floating in the middle of a room lies inside the plan's box and belongs to
   the plan, not to a cluster of its own.

Ids are ``C1``, ``C2``, ... by entity count, largest first (ties left to right,
then bottom to top). Outliers are the caller's to exclude: one entity thousands of
kilometres away would otherwise set the cell size. Pure; the drawing is never touched.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from engineering.understand.snapshot import EntityRecord
from engineering.understand.units import entity_box

__all__ = ["CELL_FRACTION", "find_clusters"]

#: Cell side as a fraction of the content's diagonal.
CELL_FRACTION = 0.02
#: Labels listed per cluster.
LABELS_PER_CLUSTER = 5

_TEXT_TYPES = frozenset({"TEXT", "MTEXT", "MULTILEADER"})


def _intersect(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _merge(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _root(parent: list[int], i: int) -> int:
    """Union-find root with path halving."""
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _absorb(bodies: list[tuple[tuple, list[int]]]) -> list[tuple[tuple, list[int]]]:
    """Merge bodies whose boxes intersect until none do.

    One pass is a sweep over boxes sorted by their left edge (a pair can only
    intersect while the next left edge is inside the current box) feeding a
    union-find; a merged box can reach a body it did not touch before, so
    passes repeat until one merges nothing.
    """
    while True:
        order = sorted(range(len(bodies)), key=lambda i: bodies[i][0][0])
        parent = list(range(len(bodies)))
        joined = False
        for n, i in enumerate(order):
            box = bodies[i][0]
            for j in order[n + 1 :]:
                if bodies[j][0][0] > box[2]:
                    break
                ri, rj = _root(parent, i), _root(parent, j)
                if ri != rj and _intersect(box, bodies[j][0]):
                    parent[rj] = ri
                    joined = True
        if not joined:
            return bodies
        merged: dict[int, tuple[tuple, list[int]]] = {}
        for i, (box, members) in enumerate(bodies):
            r = _root(parent, i)
            if r in merged:
                merged[r] = (_merge(merged[r][0], box), merged[r][1] + members)
            else:
                merged[r] = (box, list(members))
        bodies = list(merged.values())


def find_clusters(
    records: Sequence[EntityRecord],
    *,
    exclude: Iterable[str] = (),
    cell: float | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """``(clusters, membership)``: one row per body of content, and handle -> id.

    A row is ``{"id", "box": [x0, y0, x1, y1], "size": [w, h], "entities",
    "layers": [the three commonest], "labels": [the tallest texts]}``.
    ``exclude`` names handles to leave out (the outliers). A record without a
    box (no geometry the snapshot could read) belongs to no cluster.
    """
    skip = set(exclude)
    items = [(rec, box) for rec in records if rec.handle not in skip and (box := entity_box(rec))]
    if not items:
        return [], {}
    x0 = min(b[0] for _r, b in items)
    y0 = min(b[1] for _r, b in items)
    x1 = max(b[2] for _r, b in items)
    y1 = max(b[3] for _r, b in items)
    if cell is None:
        diag = math.hypot(x1 - x0, y1 - y0)
        cell = diag * CELL_FRACTION if diag > 0.0 else 1.0
    if not cell > 0.0:
        raise ValueError(f"cell: must be greater than zero, got {cell!r}")

    def span(lo: float, hi: float, origin: float) -> range:
        return range(
            int(math.floor((lo - origin) / cell)), int(math.floor((hi - origin) / cell)) + 1
        )

    owner: dict[tuple[int, int], list[int]] = {}
    first_cell: list[tuple[int, int]] = []
    for k, (_rec, b) in enumerate(items):
        cols, rows = span(b[0], b[2], x0), span(b[1], b[3], y0)
        first_cell.append((cols[0], rows[0]))
        for i in cols:
            for j in rows:
                owner.setdefault((i, j), []).append(k)

    component: dict[tuple[int, int], int] = {}
    count = 0
    for start in owner:
        if start in component:
            continue
        component[start] = count
        stack = [start]
        while stack:
            ci, cj = stack.pop()
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    nxt = (ci + di, cj + dj)
                    if nxt in owner and nxt not in component:
                        component[nxt] = count
                        stack.append(nxt)
        count += 1

    groups: dict[int, list[int]] = {}
    for k in range(len(items)):
        groups.setdefault(component[first_cell[k]], []).append(k)
    bodies = []
    for members in groups.values():
        box = items[members[0]][1]
        for k in members[1:]:
            box = _merge(box, items[k][1])
        bodies.append((box, members))
    bodies = _absorb(bodies)

    bodies.sort(key=lambda body: (-len(body[1]), body[0][0], body[0][1]))
    clusters: list[dict] = []
    membership: dict[str, str] = {}
    for n, (box, members) in enumerate(bodies, start=1):
        cid = f"C{n}"
        layers: dict[str, int] = {}
        texts = []
        for k in members:
            rec = items[k][0]
            membership[rec.handle] = cid
            layers[rec.layer] = layers.get(rec.layer, 0) + 1
            if rec.type in _TEXT_TYPES and rec.text and rec.text.strip():
                texts.append((-(rec.height or 0.0), rec.text.strip()))
        labels: list[str] = []
        for _h, text in sorted(texts):
            if text not in labels:
                labels.append(text)
            if len(labels) == LABELS_PER_CLUSTER:
                break
        clusters.append(
            {
                "id": cid,
                "box": list(box),
                "size": [box[2] - box[0], box[3] - box[1]],
                "entities": len(members),
                "layers": [
                    name for name, _c in sorted(layers.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
                ],
                "labels": labels,
            }
        )
    return clusters, membership
