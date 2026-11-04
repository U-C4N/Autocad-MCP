"""Obstacle-aware orthogonal routing for P&ID lines (plan 1.8-20).

Pure: no backend, no drawing. ``lines.route`` picks the shortest of seven
fixed candidates and knows nothing about what lies between the two ports, so
a line could run straight through a vessel and pass every ``pid_*`` focus.
``route_avoiding`` searches instead:

* a sparse Hanan grid - the x and y of both ports, both stub points, the
  midlines the fixed candidates use, and every obstacle edge inflated by
  ``clearance`` - so every clean fixed candidate is a path of the grid;
* A* over ``(node, incoming axis)`` states: length, plus ``bend_cost`` per
  turn, plus ``cross_cost`` per existing P&ID line crossed (or run along); no
  U-turn on a node;
* an elementary edge that enters an inflated box is blocked (running along
  its boundary, exactly ``clearance`` from the symbol, is allowed);
* ties are broken by coordinate, so the same input always gives the same
  route - the dry run of ``pid_from_spec`` and its apply agree vertex for
  vertex;
* the end symbols are not obstacles (the caller leaves them out); the route
  leaves each port along its direction for at least ``stub`` and arrives
  against the other port's direction the same way.

No route is :class:`NoRouteError` (kind ``no_route``) naming the obstacles
that enclose the smaller side; more than ``max_nodes`` expanded states is
:class:`RoutingBudgetError` (kind ``routing_budget_exceeded``). With
``on_blocked="cross"`` a blocked edge is priced (``BLOCKED_EDGE_COST``)
instead of refused, so the route enters as few boxes as it can and the
caller reports them in ``obstacle_hits``.

The search prices a crossing per elementary grid edge; the crossings a
:class:`Route` reports are counted once more on the final vertices with
``lines.count_crossings``, the count every other P&ID tool uses.
"""

from __future__ import annotations

import heapq
import math
from bisect import bisect_left
from dataclasses import dataclass, field

from .lines import (
    AXIS,
    Point,
    _finite_point,
    axis_direction,
    count_crossings,
    flatten_bulges,
    path_length,
    segment_arc_crossings,
    segments_cross,
    simplify,
)

__all__ = [
    "BLOCKED_EDGE_COST",
    "NoRouteError",
    "Obstacle",
    "Route",
    "RoutingBudgetError",
    "RoutingError",
    "box_entered",
    "normalise_obstacles",
    "obstacle_hits",
    "overlaps",
    "route_avoiding",
    "route_cost",
]

Box = tuple[float, float, float, float]

#: What one elementary grid edge inside an inflated box costs with
#: ``on_blocked="cross"``: far above any length, bend or crossing on a sheet,
#: so the route enters as few boxes (and as little of each) as it can.
BLOCKED_EDGE_COST = 1.0e6
_EPS = 1e-9
#: How deep a segment must reach into a box to have entered it (mm).
_HIT_EPS = 1e-6
#: The four search directions, in AXIS order: +x, +y, -x, -y.
_DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))
_DIR_OF_AXIS = {0.0: 0, 90.0: 1, 180.0: 2, 270.0: 3}
_NONE = 4  # no incoming axis yet (a free start)


class RoutingError(ValueError):
    """A route the router could not produce. A ``ValueError``, so every caller
    that reports a routing refusal by path (``pid_from_spec``'s ``lines[2]:``)
    keeps doing so; ``kind`` is what ``engineering.errors.classify`` reports."""

    kind = "failed"
    #: The spec path a ``prefixed`` copy names (``lines[2]``), else None.
    where: str | None = None

    def fields(self) -> dict:
        return {}

    def prefixed(self, where: str) -> RoutingError:
        """The same refusal with ``where`` in front of its message."""
        clone = self.__class__.__new__(self.__class__)
        ValueError.__init__(clone, f"{where}: {self}")
        clone.__dict__.update(self.__dict__)
        clone.where = where
        return clone


class NoRouteError(RoutingError):
    """No orthogonal path keeps ``clearance`` from every obstacle."""

    kind = "no_route"

    def __init__(self, reason: str, obstacles: list[dict], clearance: float, start, end):
        self.reason = str(reason)
        self.obstacles = [dict(o) for o in obstacles]
        self.clearance = float(clearance)
        self.start, self.end = tuple(start), tuple(end)
        super().__init__(
            f"no route from {self.start} to {self.end}: {self.reason} {_tags(self.obstacles)}. "
            "Move a symbol, lower clearance, pass waypoints, or on_blocked='cross'"
        )

    def relabel(self, obstacles: list[dict]) -> NoRouteError:
        """The same refusal naming ``obstacles`` (the caller's tags filled in)."""
        return NoRouteError(self.reason, obstacles, self.clearance, self.start, self.end)

    def fields(self) -> dict:
        return {"obstacles": [dict(o) for o in self.obstacles], "clearance": self.clearance}


class RoutingBudgetError(RoutingError):
    """The search expanded more than ``max_nodes`` states."""

    kind = "routing_budget_exceeded"

    def __init__(self, expanded: int, max_nodes: int, grid: tuple[int, int]):
        self.expanded = int(expanded)
        self.max_nodes = int(max_nodes)
        self.grid = (int(grid[0]), int(grid[1]))
        super().__init__(
            f"the obstacle-aware route search expanded {self.expanded} states, more than its "
            f"budget of {self.max_nodes} (grid {self.grid[0]} x {self.grid[1]}); pass "
            "waypoints, route='auto', or fewer keep_out boxes"
        )

    def fields(self) -> dict:
        return {"expanded": self.expanded, "max_nodes": self.max_nodes, "grid": list(self.grid)}


@dataclass(frozen=True)
class Obstacle:
    """A box to keep away from, and what to call it when it is reported."""

    box: Box
    ref: dict = field(default_factory=dict)


@dataclass
class Route:
    vertices: list[Point]
    cost: float
    length: float
    bends: int
    crossings: int
    overlaps: int
    expanded: int
    crossed_boxes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "vertices": [[x, y] for x, y in self.vertices],
            "cost": self.cost,
            "length": self.length,
            "bends": self.bends,
            "crossings": self.crossings,
            "overlaps": self.overlaps,
            "expanded": self.expanded,
        }


# ── inputs ──────────────────────────────────────────────────────────────────


def _finite(name: str, value, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum:g}, got {number}")
    return number


def _box(name: str, raw) -> Box:
    if isinstance(raw, dict) and "min" in raw and "max" in raw:
        raw = (raw["min"][0], raw["min"][1], raw["max"][0], raw["max"][1])
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError(f"{name}: expected [xmin, ymin, xmax, ymax]")
    x0, y0, x1, y1 = (_finite(f"{name}[{i}]", v, -math.inf) for i, v in enumerate(raw))
    if x1 < x0 or y1 < y0:
        raise ValueError(f"{name}: [xmin, ymin, xmax, ymax] with xmax < xmin or ymax < ymin")
    return (x0, y0, x1, y1)


def normalise_obstacles(obstacles, name: str = "obstacles") -> list[Obstacle]:
    """``Obstacle`` rows from obstacles, ``{"box": ..., **ref}`` dicts or bare boxes."""
    out: list[Obstacle] = []
    for i, raw in enumerate(obstacles or ()):
        if isinstance(raw, Obstacle):
            out.append(Obstacle(_box(f"{name}[{i}]", raw.box), dict(raw.ref)))
        elif isinstance(raw, dict) and "box" in raw:
            ref = {k: v for k, v in raw.items() if k != "box"}
            out.append(Obstacle(_box(f"{name}[{i}].box", raw["box"]), ref))
        else:
            out.append(Obstacle(_box(f"{name}[{i}]", raw), {"index": i}))
    return out


def _chains(existing) -> tuple[list[list[Point]], list[list[float]]]:
    chains: list[list[Point]] = []
    bulges: list[list[float]] = []
    for item in existing or ():
        if isinstance(item, dict):
            verts = item.get("vertices") or []
            blg = item.get("bulges") or []
        else:
            verts, blg = item, []
        pts = [(float(x), float(y)) for x, y in verts]
        if len(pts) >= 2:
            chains.append(pts)
            bulges.append([float(b or 0.0) for b in blg])
    return chains, bulges


# ── geometry ────────────────────────────────────────────────────────────────


def _inflate(box: Box, by: float) -> Box:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def box_entered(a: Point, b: Point, box: Box, depth: float = _HIT_EPS) -> bool:
    """True when the segment a→b passes through the box's interior, more than
    ``depth`` inside every wall (Liang-Barsky clip against the shrunk box).
    Running along a wall, touching a corner or ending on the boundary is not
    entering."""
    x0, y0, x1, y1 = box[0] + depth, box[1] + depth, box[2] - depth, box[3] - depth
    if x1 <= x0 or y1 <= y0:
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-15:
            if q <= 0.0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return (t1 - t0) * math.hypot(dx, dy) > _EPS


def _point_inside(p: Point, box: Box, depth: float = _HIT_EPS) -> bool:
    return box[0] + depth < p[0] < box[2] - depth and box[1] + depth < p[1] < box[3] - depth


def obstacle_hits(vertices, obstacles, depth: float = _HIT_EPS) -> list[dict]:
    """The refs of every obstacle whose box a segment of ``vertices`` enters,
    in the order the route reaches them. An obstacle whose box holds one of
    the route's two ends is that end's own symbol and is never a hit."""
    pts = [(float(x), float(y)) for x, y in vertices]
    if len(pts) < 2:
        return []
    rows = normalise_obstacles(obstacles)
    ends = (pts[0], pts[-1])
    candidates = [o for o in rows if not any(_point_inside(p, o.box, depth) for p in ends)]
    out: list[dict] = []
    seen: set[int] = set()
    for a, b in zip(pts, pts[1:], strict=False):
        for index, obstacle in enumerate(candidates):
            if index in seen:
                continue
            if box_entered(a, b, obstacle.box, depth):
                seen.add(index)
                out.append(dict(obstacle.ref))
    return out


def _collinear_overlap(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Two straight segments on one line sharing more than ``_EPS`` of length."""
    length = math.dist(a, b)
    if length <= _EPS or math.dist(c, d) <= _EPS:
        return False
    for p in (c, d):
        cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        if abs(cross) / length > _EPS:
            return False
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    tc = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
    td = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
    lo, hi = max(0.0, min(tc, td)), min(length, max(tc, td))
    return hi - lo > _EPS


def overlaps(vertices, existing) -> int:
    """How many (segment, existing straight segment) pairs run along each other."""
    pts = [(float(x), float(y)) for x, y in vertices]
    chains, bulges = _chains(existing)
    count = 0
    for chain, blg in zip(chains, bulges, strict=False):
        for j, (c, d) in enumerate(zip(chain, chain[1:], strict=False)):
            if j < len(blg) and abs(blg[j]) > 1e-12:
                continue
            for a, b in zip(pts, pts[1:], strict=False):
                if _collinear_overlap(a, b, c, d):
                    count += 1
    return count


def route_cost(vertices, existing=(), bend_cost: float = 10.0, cross_cost: float = 25.0) -> float:
    """Length + ``bend_cost`` per bend + ``cross_cost`` per crossing or overlap:
    the measure ``route_avoiding`` minimises, applied to any route."""
    pts = simplify(vertices)
    chains, bulges = _chains(existing)
    crossings = count_crossings(pts, chains, bulges=bulges) if chains else 0
    return (
        path_length(pts)
        + bend_cost * max(0, len(pts) - 2)
        + cross_cost * (crossings + overlaps(pts, existing))
    )


# ── the grid ────────────────────────────────────────────────────────────────


def _axis_values(values) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > _EPS:
            out.append(v)
    return out


def _index(axis: list[float], value: float) -> int:
    i = bisect_left(axis, value - _EPS)
    if i < len(axis) and abs(axis[i] - value) <= _EPS:
        return i
    raise AssertionError(f"{value} is not on the routing grid")  # pragma: no cover


class _Grid:
    """The Hanan grid, its blocked edges and its edge prices, built lazily."""

    def __init__(self, xs, ys, boxes, refs, chains, bulges, cross_cost, crossing_allowed):
        self.xs, self.ys = xs, ys
        self.boxes, self.refs = boxes, refs
        self.cross_cost = cross_cost
        self.crossing_allowed = crossing_allowed
        self.row_boxes = [
            [k for k, b in enumerate(boxes) if b[1] + _EPS < y < b[3] - _EPS] for y in ys
        ]
        self.col_boxes = [
            [k for k, b in enumerate(boxes) if b[0] + _EPS < x < b[2] - _EPS] for x in xs
        ]
        segs = []
        for chain, blg in zip(chains, bulges, strict=False):
            for j, (c, d) in enumerate(zip(chain, chain[1:], strict=False)):
                bulge = blg[j] if j < len(blg) else 0.0
                pts = flatten_bulges([c, d], [bulge]) if abs(bulge) > 1e-12 else [c, d]
                xs_, ys_ = [p[0] for p in pts], [p[1] for p in pts]
                segs.append((min(xs_), min(ys_), max(xs_), max(ys_), c, d, bulge))
        self.segs = segs
        self._row_segs: dict[int, list] = {}
        self._col_segs: dict[int, list] = {}
        self._blocked: dict = {}
        self._price: dict = {}
        self.blockers: set[int] = set()

    def _segs_on(self, horizontal: bool, index: int) -> list:
        """Existing segments whose extent spans grid row (column) ``index``."""
        cache = self._row_segs if horizontal else self._col_segs
        found = cache.get(index)
        if found is None:
            if horizontal:
                y = self.ys[index]
                found = [s for s in self.segs if s[1] - _EPS <= y <= s[3] + _EPS]
            else:
                x = self.xs[index]
                found = [s for s in self.segs if s[0] - _EPS <= x <= s[2] + _EPS]
            cache[index] = found
        return found

    def _edge_key(self, i: int, j: int, d: int) -> tuple[int, int, int]:
        """The edge leaving node (i, j) in direction d, keyed by its low end."""
        di, dj = _DIRS[d]
        if di < 0 or dj < 0:
            return (i + di, j + dj, 0 if di else 1)
        return (i, j, 0 if di else 1)

    def blocked_by(self, key) -> list[int]:
        cached = self._blocked.get(key)
        if cached is not None:
            return cached
        i, j, horizontal = key[0], key[1], key[2] == 0
        if horizontal:
            lo, hi = self.xs[i], self.xs[i + 1]
            hits = [
                k
                for k in self.row_boxes[j]
                if self.boxes[k][0] <= lo + _EPS and self.boxes[k][2] >= hi - _EPS
            ]
        else:
            lo, hi = self.ys[j], self.ys[j + 1]
            hits = [
                k
                for k in self.col_boxes[i]
                if self.boxes[k][1] <= lo + _EPS and self.boxes[k][3] >= hi - _EPS
            ]
        self._blocked[key] = hits
        return hits

    def price(self, key) -> float:
        """Crossings and overlaps of one elementary edge, times ``cross_cost``."""
        cached = self._price.get(key)
        if cached is not None:
            return cached
        i, j, horizontal = key[0], key[1], key[2] == 0
        a = (self.xs[i], self.ys[j])
        b = (self.xs[i + 1], self.ys[j]) if horizontal else (self.xs[i], self.ys[j + 1])
        lo_x, lo_y, hi_x, hi_y = a[0], a[1], b[0], b[1]
        hits = 0
        for sx0, sy0, sx1, sy1, c, d, bulge in self._segs_on(horizontal, j if horizontal else i):
            if sx1 < lo_x - _EPS or sx0 > hi_x + _EPS or sy1 < lo_y - _EPS or sy0 > hi_y + _EPS:
                continue
            if abs(bulge) > 1e-12:
                hits += segment_arc_crossings(a, b, c, d, bulge)
            elif segments_cross(a, b, c, d) or _collinear_overlap(a, b, c, d):
                hits += 1
        value = self.cross_cost * hits
        self._price[key] = value
        return value

    def step(self, i: int, j: int, d: int):
        """``(ni, nj, length, extra)`` for the move, or None when it leaves the
        grid or is blocked (``extra`` carries the crossing and box prices)."""
        di, dj = _DIRS[d]
        ni, nj = i + di, j + dj
        if not (0 <= ni < len(self.xs) and 0 <= nj < len(self.ys)):
            return None
        key = self._edge_key(i, j, d)
        blockers = self.blocked_by(key)
        extra = 0.0
        if blockers:
            self.blockers.update(blockers)
            if not self.crossing_allowed:
                return None
            extra += BLOCKED_EDGE_COST
        length = abs(self.xs[ni] - self.xs[i]) + abs(self.ys[nj] - self.ys[j])
        return ni, nj, length, extra + self.price(key)


def _walk(grid: _Grid, i: int, j: int, d: int, target: tuple[int, int]):
    """Follow direction ``d`` from (i, j) to ``target``: ``(cost, blockers)``."""
    cost = 0.0
    blockers: list[int] = []
    while (i, j) != target:
        key = grid._edge_key(i, j, d)
        hit = grid.blocked_by(key)
        blockers.extend(k for k in hit if k not in blockers)
        if hit:
            cost += BLOCKED_EDGE_COST
        di, dj = _DIRS[d]
        ni, nj = i + di, j + dj
        cost += abs(grid.xs[ni] - grid.xs[i]) + abs(grid.ys[nj] - grid.ys[j]) + grid.price(key)
        i, j = ni, nj
    return cost, blockers


def _flood(grid: _Grid, start: tuple[int, int], limit: int) -> tuple[int, set[int]]:
    """Nodes reachable from ``start`` ignoring direction rules, and the boxes
    that bound them (for a ``no_route`` report)."""
    seen = {start}
    stack = [start]
    bound: set[int] = set()
    while stack and len(seen) <= limit:
        i, j = stack.pop()
        for d in range(4):
            di, dj = _DIRS[d]
            ni, nj = i + di, j + dj
            if not (0 <= ni < len(grid.xs) and 0 <= nj < len(grid.ys)):
                continue
            hit = grid.blocked_by(grid._edge_key(i, j, d))
            if hit:
                bound.update(hit)
                continue
            if (ni, nj) not in seen:
                seen.add((ni, nj))
                stack.append((ni, nj))
    return len(seen), bound


def _refs(grid: _Grid, indices) -> list[dict]:
    """The reported refs of ``indices``, ordered by box (left, bottom)."""
    order = sorted(set(indices), key=lambda k: (grid.boxes[k][0], grid.boxes[k][1], k))
    return [dict(grid.refs[k]) for k in order]


# ── the search ──────────────────────────────────────────────────────────────


def route_avoiding(
    start,
    start_dir,
    end,
    end_dir,
    obstacles,
    existing=(),
    clearance: float = 2.0,
    stub: float = 5.0,
    bend_cost: float = 10.0,
    cross_cost: float = 25.0,
    max_nodes: int = 50000,
    *,
    on_blocked: str = "refuse",
) -> Route:
    """The cheapest orthogonal route from ``start`` (leaving along
    ``start_dir``) to ``end`` (arriving against ``end_dir``) that keeps
    ``clearance`` from every obstacle box. A ``None`` direction is a free end:
    any first (last) direction, no stub. ``obstacles`` are ``Obstacle`` rows,
    ``{"box": [xmin, ymin, xmax, ymax], **ref}`` dicts or bare boxes;
    ``existing`` are P&ID chains (vertex lists or ``{vertices, bulges}``)."""
    s = _finite_point("start", start)
    e = _finite_point("end", end)
    clearance = _finite("clearance", clearance)
    stub = _finite("stub", stub)
    bend_cost = _finite("bend_cost", bend_cost)
    cross_cost = _finite("cross_cost", cross_cost)
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValueError(f"max_nodes must be a whole number >= 1, got {max_nodes!r}")
    if on_blocked not in ("refuse", "cross"):
        raise ValueError(f"on_blocked must be 'refuse' or 'cross', got {on_blocked!r}")
    if math.dist(s, e) < 0.01:
        raise ValueError("zero-length line: both ends are the same point")
    ds = None if start_dir is None else axis_direction("start_dir", start_dir)
    de = None if end_dir is None else axis_direction("end_dir", end_dir)
    rows = normalise_obstacles(obstacles)
    chains, bulges = _chains(existing)
    run = max(stub, 0.01)
    s1 = s if ds is None else (s[0] + AXIS[ds][0] * run, s[1] + AXIS[ds][1] * run)
    e1 = e if de is None else (e[0] + AXIS[de][0] * run, e[1] + AXIS[de][1] * run)

    boxes = [_inflate(o.box, clearance) for o in rows]
    refs = [o.ref for o in rows]
    xs = _axis_values(
        [s[0], s1[0], e[0], e1[0], (s1[0] + e1[0]) / 2.0] + [v for b in boxes for v in (b[0], b[2])]
    )
    ys = _axis_values(
        [s[1], s1[1], e[1], e1[1], (s1[1] + e1[1]) / 2.0] + [v for b in boxes for v in (b[1], b[3])]
    )
    grid = _Grid(xs, ys, boxes, refs, chains, bulges, cross_cost, on_blocked == "cross")
    si, sj = _index(xs, s[0]), _index(ys, s[1])
    s1i, s1j = _index(xs, s1[0]), _index(ys, s1[1])
    ei, ej = _index(xs, e[0]), _index(ys, e[1])
    e1i, e1j = _index(xs, e1[0]), _index(ys, e1[1])

    # The two stubs are fixed: their cost is a constant, and a stub that
    # enters a box (another symbol hard against the port) has no way round.
    start_cost, start_block = (0.0, [])
    if ds is not None:
        start_cost, start_block = _walk(grid, si, sj, _DIR_OF_AXIS[ds], (s1i, s1j))
    end_cost, end_block = (0.0, [])
    arrive = None  # the direction the final stub runs in
    if de is not None:
        arrive = (_DIR_OF_AXIS[de] + 2) % 4
        end_cost, end_block = _walk(grid, e1i, e1j, arrive, (ei, ej))
    direct = _direct(s, e, ds, de, boxes, chains, bulges, bend_cost, cross_cost)
    if direct is not None and direct.crossings == 0 and direct.overlaps == 0:
        return direct  # aligned, clean, no bend: nothing can be cheaper
    stub_block = start_block + [k for k in end_block if k not in start_block]
    if stub_block and on_blocked == "refuse":
        if direct is not None:
            return direct
        raise NoRouteError(
            f"a {run:g} mm stub leaving the port comes within {clearance:g} mm of",
            _refs(grid, stub_block),
            clearance,
            s,
            e,
        )

    goal = (e1i, e1j)
    forbidden = None if arrive is None else (arrive + 2) % 4  # a U-turn into the stub

    def heuristic(i: int, j: int) -> float:
        # Manhattan to the stub point plus the fixed final stub: consistent.
        return abs(xs[i] - xs[goal[0]]) + abs(ys[j] - ys[goal[1]]) + end_cost

    first = _NONE if ds is None else _DIR_OF_AXIS[ds]
    start_state = (s1i, s1j, first)
    best: dict = {start_state: start_cost}
    parent: dict = {start_state: None}
    h0 = heuristic(s1i, s1j)
    # (f, goal-first, h, x, y, dir): equal costs resolve by coordinate.
    heap = [(start_cost + h0, 1, h0, xs[s1i], ys[s1j], first, start_state)]
    expanded = 0
    final = None
    while heap:
        f, is_node, _h, _x, _y, _d, state = heapq.heappop(heap)
        if is_node == 0:
            final = (f, state)
            break
        g = best.get(state)
        if g is None or f - _h > g + 1e-9:
            continue  # a stale entry
        expanded += 1
        if expanded > max_nodes:
            raise RoutingBudgetError(expanded - 1, max_nodes, (len(xs), len(ys)))
        i, j, d = state
        if (i, j) == goal and d != forbidden:
            bend = arrive is not None and d not in (_NONE, arrive)
            total = g + end_cost + (bend_cost if bend else 0.0)
            heapq.heappush(heap, (total, 0, 0.0, xs[i], ys[j], d, state))
        for nd in range(4):
            if d != _NONE and nd == (d + 2) % 4:
                continue
            moved = grid.step(i, j, nd)
            if moved is None:
                continue
            ni, nj, length, extra = moved
            turn = bend_cost if d not in (_NONE, nd) else 0.0
            ng = g + length + turn + extra
            nxt = (ni, nj, nd)
            if ng < best.get(nxt, math.inf) - 1e-9:
                best[nxt] = ng
                parent[nxt] = state
                h = heuristic(ni, nj)
                heapq.heappush(heap, (ng + h, 1, h, xs[ni], ys[nj], nd, nxt))

    if final is None:
        if direct is not None:
            return direct
        raise _no_route(grid, s, e, (s1i, s1j), goal, clearance)

    nodes: list[Point] = []
    state = final[1]
    while state is not None:
        nodes.append((xs[state[0]], ys[state[1]]))
        state = parent[state]
    nodes.reverse()
    vertices = simplify([s, *nodes, e])
    crossed = obstacle_hits(vertices, [Obstacle(b, r) for b, r in zip(boxes, refs, strict=False)])
    crossings = count_crossings(vertices, chains, bulges=bulges) if chains else 0
    over = overlaps(
        vertices, [{"vertices": c, "bulges": b} for c, b in zip(chains, bulges, strict=False)]
    )
    length = path_length(vertices)
    bends = max(0, len(vertices) - 2)
    found = Route(
        vertices=vertices,
        cost=length + bend_cost * bends + cross_cost * (crossings + over),
        length=length,
        bends=bends,
        crossings=crossings,
        overlaps=over,
        expanded=expanded,
        crossed_boxes=crossed,
    )
    if direct is not None and direct.cost <= found.cost:
        direct.expanded = expanded
        return direct
    return found


def _direct(s, e, ds, de, boxes, chains, bulges, bend_cost, cross_cost) -> Route | None:
    """The straight segment s→e when it is a legal route: axis-aligned, leaving
    along ``ds`` and arriving against ``de`` (the stub rule does not apply to
    a straight line, as in ``lines.route``), entering no inflated box."""
    dx, dy = e[0] - s[0], e[1] - s[1]
    if abs(dx) > _EPS and abs(dy) > _EPS:
        return None
    heading = (0 if dx > 0 else 2) if abs(dx) > _EPS else (1 if dy > 0 else 3)
    if ds is not None and _DIR_OF_AXIS[ds] != heading:
        return None
    if de is not None and (_DIR_OF_AXIS[de] + 2) % 4 != heading:
        return None
    if any(box_entered(s, e, box, 0.0) for box in boxes):
        return None
    vertices = [s, e]
    existing = [{"vertices": c, "bulges": b} for c, b in zip(chains, bulges, strict=False)]
    crossings = count_crossings(vertices, chains, bulges=bulges) if chains else 0
    over = overlaps(vertices, existing)
    length = path_length(vertices)
    return Route(
        vertices=vertices,
        cost=length + cross_cost * (crossings + over),
        length=length,
        bends=0,
        crossings=crossings,
        overlaps=over,
        expanded=0,
    )


def _tags(refs: list[dict]) -> str:
    names = [
        str(r.get("tag") or r.get("id") or r.get("handle") or r.get("index", "?")) for r in refs
    ]
    return ", ".join(names) if names else "no named obstacle"


def _no_route(grid: _Grid, s: Point, e: Point, start, goal, clearance: float) -> NoRouteError:
    """``no_route`` naming the boxes around the smaller of the two sides."""
    limit = len(grid.xs) * len(grid.ys)
    n_start, around_start = _flood(grid, start, limit)
    n_goal, around_goal = _flood(grid, goal, limit)
    around = around_start if n_start <= n_goal else around_goal
    refs = _refs(grid, around or grid.blockers)
    return NoRouteError(
        f"no orthogonal path keeps {clearance:g} mm from every obstacle; enclosed by",
        refs,
        clearance,
        s,
        e,
    )
