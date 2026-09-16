"""Read a P&ID back as a graph: nodes (symbols), edges (lines), junctions, dangling ends.

Rules the reader keeps (spec §9.4): it never modifies the drawing, it reads
geometry from the drawing rather than the catalogue, and confidence is the
minimum of its inputs — never raised by agreement between heuristics.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .lines import LAYER_TO_CLASS, PID_LINE_LAYERS
from .symbols import Port, transform_port
from .tags import parse_tag
from .xdata import read_payload

if TYPE_CHECKING:
    from backends.base import AutoCADBackend, EntityInfo

INSERT_TYPES = {"INSERT", "BLOCKREFERENCE"}
# Same set ``drawlines`` keeps: ezdxf says LWPOLYLINE, the COM engine derives
# POLYLINE (AcDbPolyline) / 2DPOLYLINE from the ActiveX ObjectName.
POLY_TYPES = {"LWPOLYLINE", "POLYLINE", "2DPOLYLINE"}
LINE_TYPES = {"LINE"}
TEXT_TYPES = {"TEXT", "MTEXT"}
ATTRIBUTE_TAGS = ("TAG", "TAGNO", "TAGNUMBER", "EQUIP_TAG", "EQUIPMENT_TAG", "INST_TAG", "INSTTAG")
# Block-name keywords (spec §9.2 step 2), matched as whole tokens of the name
# split on non-letters: ``GATE_VALVE`` → VALVE, ``P101PUMP`` → PUMP, but
# ``COMPANY_LOGO`` is a logo, not a compressor — a substring match would file
# every title block and logo as equipment at 0.6.
KEYWORDS = {
    "VALVE": "valve",
    "VLV": "valve",
    "PUMP": "equipment",
    "TANK": "equipment",
    "VESSEL": "equipment",
    "DRUM": "equipment",
    "INST": "instrument",
    "INSTRUMENT": "instrument",
    "BUBBLE": "instrument",
    "HX": "equipment",
    "EXCH": "equipment",
    "EXCHANGER": "equipment",
    "FILTER": "equipment",
    "COMP": "equipment",
    "COMPRESSOR": "equipment",
}
_TOKEN_RE = re.compile(r"[A-Z]+")
FAMILY_KIND = {"instrument": "instrument", "valve": "valve", "connector": "connector"}
LINE_NUMBER_RE = re.compile(r'^\d+(?:"|MM)?-[A-Z]{1,4}-\d+', re.IGNORECASE)
CONFIDENCE = {"catalog": 1.0, "xdata": 0.95, "heuristic": 0.6, "inferred": 0.3}
_PAGE = 1000


@dataclass
class _Node:
    id: str
    kind: str
    block_name: str
    family: str | None
    symbol: str | None
    x: float
    y: float
    rotation_deg: float
    scale: float
    y_scale: float
    mirrored: bool
    layer: str
    space: str
    bbox: tuple[float, float, float, float] | None
    source: str
    confidence: float
    attributes: dict
    ports: dict = field(default_factory=dict)
    tag: str | None = None
    tag_source: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        parsed = None
        if self.tag:
            parsed = parse_tag(self.tag, kind="instrument" if self.kind == "instrument" else "auto")
        return {
            "id": self.id,
            "kind": self.kind,
            "block_name": self.block_name,
            "family": self.family,
            "symbol": self.symbol,
            "tag": self.tag,
            "tag_parsed": parsed,
            "tag_source": self.tag_source,
            "x": self.x,
            "y": self.y,
            "rotation_deg": self.rotation_deg,
            "scale": self.scale,
            "y_scale": self.y_scale,
            "mirrored": self.mirrored,
            "layer": self.layer,
            "space": self.space,
            "ports": self.ports,
            "description": self.attributes.get("DESC") or None,
            "source": self.source,
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


@dataclass
class _Edge:
    id: str
    vertices: list[tuple[float, float]]
    bulges: list[float]  # per vertex, the arc leaving it (DXF convention); 0.0 = straight
    length: float | None  # the engine's own measurement (bulge-aware), None when it gave none
    layer: str
    linetype: str
    space: str
    payload: dict | None
    source: str
    line_class: str | None = None
    class_source: str | None = None
    line_number: str | None = None
    number_source: str | None = None
    from_ref: dict | None = None
    to_ref: dict | None = None

    def to_dict(self, include_geometry: bool) -> dict:
        p = self.payload or {}
        out = {
            "id": self.id,
            "line_class": self.line_class,
            "class_source": self.class_source,
            "line_number": self.line_number,
            "number_source": self.number_source,
            "size": p.get("size"),
            "service": p.get("service"),
            "spec": p.get("spec"),
            "insulation": p.get("insulation"),
            "from": self.from_ref,
            "to": self.to_ref,
            # Both engines measure the real geometry (ezdxf via the shared
            # bulge-aware maths, COM via ActiveX `Length`); a chord walk over
            # the vertices is 12.5% short on a semicircular jump and says
            # nothing. The chord sum is kept only for an engine that reports
            # no length at all, and then the approximation is named.
            "length": round(self.length, 6) if self.length is not None else self.chord_length(),
            "layer": self.layer,
            "space": self.space,
            "source": self.source,
        }
        if self.length is None:
            out["length_approximate"] = True
        if include_geometry:
            out["vertices"] = [[x, y] for x, y in self.vertices]
            out["bulges"] = list(self.bulges)
        return out

    def chord_length(self) -> float:
        return round(
            sum(math.dist(a, b) for a, b in zip(self.vertices, self.vertices[1:], strict=False)),
            6,
        )

    def segments(self):
        """``(a, b, bulge)`` per segment, the bulge belonging to the segment's start."""
        for i, (a, b) in enumerate(zip(self.vertices, self.vertices[1:], strict=False)):
            yield a, b, self.bulges[i] if i < len(self.bulges) else 0.0


class _Grid:
    """Uniform grid for nearest-within-tolerance queries, one per space.

    Every layout is its own sheet: a line end in SHEET-2 may only attach to
    what is drawn in SHEET-2. Keying the cells by space is what keeps a
    ``scope="all"`` read from joining two sheets that happen to share
    coordinates — every layout starts at the origin.
    """

    def __init__(self, cell: float):
        self.cell = cell
        self.cells: dict[tuple[str, int, int], list] = {}

    def _key(self, space: str, x: float, y: float) -> tuple[str, int, int]:
        return (space, int(math.floor(x / self.cell)), int(math.floor(y / self.cell)))

    def add(self, space: str, x: float, y: float, item) -> None:
        self.cells.setdefault(self._key(space, x, y), []).append((x, y, item))

    def near(self, space: str, x: float, y: float, radius: float):
        _, kx, ky = self._key(space, x, y)
        reach = int(math.ceil(radius / self.cell))
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for px, py, item in self.cells.get((space, kx + dx, ky + dy), []):
                    d = math.hypot(px - x, py - y)
                    if d <= radius:
                        yield d, item


def _vertices(info: EntityInfo) -> list[tuple[float, float]]:
    if info.type in LINE_TYPES:
        s, e = info.properties["start"], info.properties["end"]
        return [(float(s[0]), float(s[1])), (float(e[0]), float(e[1]))]
    return [(float(p[0]), float(p[1])) for p in info.properties.get("points") or []]


def _bulges(info: EntityInfo, count: int) -> list[float]:
    """Per-vertex bulges as the engine reports them; straight when it reports none."""
    raw = info.properties.get("bulges") or []
    out = [float(b or 0.0) for b in raw[:count]]
    return out + [0.0] * (count - len(out))


def _length(info: EntityInfo) -> float | None:
    raw = info.properties.get("length")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _point_segment_distance(p, a, b, bulge: float = 0.0) -> float:
    """Distance from ``p`` to the polyline segment a→b, an arc when it carries a bulge.

    The bulge is the DXF one: tan(sweep/4), positive counter-clockwise from
    ``a`` to ``b``. Testing against the chord instead misses every line that
    ends on the curved "jump" foreign P&IDs draw where two lines cross.
    """
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        return math.hypot(px - ax, py - ay)
    if abs(bulge) < 1e-12:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
    theta = 4.0 * math.atan(bulge)  # signed sweep
    radius = chord / (2.0 * math.sin(abs(theta) / 2.0))
    sagitta = abs(bulge) * chord / 2.0
    # The centre sits on the chord's normal: opposite the arc while it is less
    # than a semicircle, on the arc's side once it is more (|bulge| > 1). A
    # positive bulge bows to the right of a→b, so its centre is to the left —
    # and ``radius - sagitta`` goes negative past the semicircle, which is
    # exactly what carries the centre across the chord. ``copysign`` used to
    # discard that sign, so a bulge-2 arc was tested as its own reflection.
    nx, ny = -dy / chord, dx / chord  # left of a→b
    offset = (radius - sagitta) * (1.0 if bulge > 0 else -1.0)
    cx, cy = (ax + bx) / 2.0 + offset * nx, (ay + by) / 2.0 + offset * ny
    start = math.atan2(ay - cy, ax - cx)
    phi = math.atan2(py - cy, px - cx)
    swept = (phi - start) % (2.0 * math.pi) if theta > 0 else (start - phi) % (2.0 * math.pi)
    if swept <= abs(theta):
        return abs(math.hypot(px - cx, py - cy) - radius)
    return min(math.hypot(px - ax, py - ay), math.hypot(px - bx, py - by))


def _keyword_kind(block_name: str) -> str | None:
    """Kind implied by a foreign block's name, or None when no token is a keyword."""
    for token in _TOKEN_RE.findall(block_name.upper()):
        kind = KEYWORDS.get(token)
        if kind:
            return kind
    return None


def _bbox(info: EntityInfo):
    """The foreign symbol's box: its drawn geometry when the engine reports it.

    ``bounding_box`` takes the ATTRIBs with it, so a TAG lettered above a
    valve pushes the box past the body and a line ending on the body's real
    edge on that side lies *inside* the box. Both engines report
    ``geometry_bbox`` (attributes excluded) for an INSERT that draws anything;
    the attribute-inclusive box is the fallback for a reader that lacks it.

    An engine may mark ``geometry_bbox`` ``approximate`` (the COM engine does
    for an INSERT rotated off a right angle whose members it cannot measure
    exactly): the box then encloses the drawn geometry but may overshoot it,
    and a line ending on the body's true edge would sit *inside* that box
    and dangle. The drawn geometry lies inside the attribute-inclusive box
    too, so the tighter wall of the two is taken on every side.
    """
    geometry = info.properties.get("geometry_bbox")
    outer = info.properties.get("bounding_box")
    boxes = [
        (float(bb["min"][0]), float(bb["min"][1]), float(bb["max"][0]), float(bb["max"][1]))
        for bb in (geometry, outer)
        if bb
    ]
    if not boxes:
        return None
    if geometry and outer and geometry.get("approximate"):
        return (
            max(boxes[0][0], boxes[1][0]),
            max(boxes[0][1], boxes[1][1]),
            min(boxes[0][2], boxes[1][2]),
            min(boxes[0][3], boxes[1][3]),
        )
    return boxes[0]


def _on_bbox_boundary(p, bbox, tolerance) -> bool:
    """Within ``tolerance`` of the box's boundary (spec §9.2 step 3).

    The boundary, not the interior: a foreign block's box is only a proxy for
    where its ports are, and its edges are the only place a port can be. An
    end strictly inside was once accepted as "drawn into the body" — but a
    sheet border or title block is an INSERT too, its box encloses the whole
    drawing, and every end that missed a port then attached to the border
    instead of becoming the junction or dangling end the tool exists to
    report. Overshoot into a body is the tolerance's business.
    """
    x, y = p
    xmin, ymin, xmax, ymax = bbox
    inside_x = xmin - tolerance <= x <= xmax + tolerance
    inside_y = ymin - tolerance <= y <= ymax + tolerance
    near_vertical = min(abs(x - xmin), abs(x - xmax)) <= tolerance and inside_y
    near_horizontal = min(abs(y - ymin), abs(y - ymax)) <= tolerance and inside_x
    return near_vertical or near_horizontal


async def _collect(backend: AutoCADBackend, scope: str) -> list[tuple[EntityInfo, str]]:
    async def page_all(space: str):
        out = []
        offset = 0
        while True:
            page = await backend.entity_list(limit=_PAGE, offset=offset)
            out.extend((info, space) for info in page)
            if len(page) < _PAGE:
                return out
            offset += _PAGE

    layouts = await backend.layout_list()
    current = layouts.get("current") or "Model"
    if scope != "all":
        return await page_all(current)
    entities = []
    try:
        for name in layouts.get("layouts", [current]):
            await backend.layout_set_current(name)
            entities.extend(await page_all(name))
    finally:
        await backend.layout_set_current(current)
    return entities


async def build_graph(
    backend: AutoCADBackend,
    tolerance: float = 0.5,
    label_search: float = 15.0,
    scope: str = "current_space",
    include_foreign: bool = True,
    include_geometry: bool = False,
) -> dict:
    if tolerance <= 0:
        raise ValueError("tolerance must be > 0")
    if scope not in ("current_space", "all"):
        raise ValueError("scope must be 'current_space' or 'all'")
    entities = await _collect(backend, scope)
    layer_linetypes = {lyr.name.upper(): lyr.linetype.upper() for lyr in await backend.layer_list()}

    # 1 — classify INSERTs
    nodes: dict[str, _Node] = {}
    candidates: dict[str, _Node] = {}  # unknown blocks kept only if a line touches them
    for info, space in entities:
        if info.type not in INSERT_TYPES:
            continue
        name = str(info.properties.get("block_name", ""))
        if name.startswith("PID_MARKER_"):
            continue
        payload = await read_payload(backend, info.handle)
        try:
            attributes = await backend.block_get_attributes(info.handle)
        except Exception:
            attributes = {}
        ins = info.properties.get("insertion", [0.0, 0.0])
        rotation = float(info.properties.get("rotation_deg", 0.0))
        scale = float(info.properties.get("x_scale", 1.0))
        y_scale = float(info.properties.get("y_scale", scale))
        mirrored = bool(info.properties.get("mirrored", False))
        base = dict(
            id=info.handle,
            block_name=name,
            x=float(ins[0]),
            y=float(ins[1]),
            rotation_deg=rotation,
            scale=scale,
            y_scale=y_scale,
            mirrored=mirrored,
            layer=info.layer,
            space=space,
            bbox=_bbox(info),
            attributes=attributes,
        )
        if payload and payload.get("kind") == "symbol":
            source = "catalog" if name.startswith("PID_") else "xdata"
            family = payload.get("family")
            node = _Node(
                kind=FAMILY_KIND.get(family, "equipment"),
                family=family,
                symbol=payload.get("symbol"),
                source=source,
                confidence=CONFIDENCE[source],
                **base,
            )
            stretched = abs(abs(scale) - abs(y_scale)) > 1e-9
            for pname, (lx, ly, ldir, kind, radius) in payload.get("ports", {}).items():
                # Measured from the INSERT as it is (spec §9.4): a mirror is
                # either y_scale = -1 (entity_mirror) or extrusion -Z
                # (MIRROR3D, foreign DXFs), and the ports sit on the mirrored
                # geometry either way. A stretched INSERT (someone scaled one
                # axis by hand) is measured per axis, which is exact for a
                # point port; only a bubble has no circle to be exact on, and
                # that is said in the node's confidence — never with
                # `inferred`, which spec §9.1 reserves for foreign-block ports.
                local = Port(pname, float(lx), float(ly), ldir, kind, float(radius))
                port = transform_port(
                    local, node.x, node.y, rotation, scale, y_scale, mirrored, strict=False
                )
                if stretched and local.radius > 0:
                    node.confidence = min(node.confidence, CONFIDENCE["heuristic"])
                    if "non-uniform scale" not in node.notes:
                        node.notes.append("non-uniform scale")
                node.ports[pname] = {**port, "edges": []}
            nodes[node.id] = node
            continue
        if not include_foreign:
            continue
        keyword_kind = _keyword_kind(name)
        tag_key = next((k for k in ATTRIBUTE_TAGS if k in attributes), None)
        if keyword_kind or tag_key:
            node = _Node(
                kind=keyword_kind or "equipment",
                family=None,
                symbol=None,
                source="heuristic",
                confidence=CONFIDENCE["heuristic"],
                **base,
            )
            nodes[node.id] = node
        elif base["bbox"] is not None:
            candidates[info.handle] = _Node(
                kind="unknown_block",
                family=None,
                symbol=None,
                source="inferred",
                confidence=CONFIDENCE["inferred"],
                **base,
            )

    # 2 — candidate edges
    edges: dict[str, _Edge] = {}
    foreign_edges: dict[str, _Edge] = {}
    for info, space in entities:
        if info.type not in POLY_TYPES | LINE_TYPES:
            continue
        verts = _vertices(info)
        if len(verts) < 2:
            continue
        payload = await read_payload(backend, info.handle) if info.type in POLY_TYPES else None
        layer = info.layer.upper()
        edge = _Edge(
            id=info.handle,
            vertices=verts,
            bulges=_bulges(info, len(verts)),
            length=_length(info),
            layer=info.layer,
            linetype=info.linetype,
            space=space,
            payload=payload if payload and payload.get("kind") == "line" else None,
            source="touch",
        )
        if edge.payload:
            edge.source = "xdata"
            edge.line_class, edge.class_source = edge.payload.get("class"), "xdata"
            edge.line_number = edge.payload.get("number")
            edge.number_source = "xdata" if edge.line_number else None
            edges[edge.id] = edge
        elif layer in PID_LINE_LAYERS:
            edge.source = "layer"
            cls = LAYER_TO_CLASS[layer]
            if cls is None:
                own = (info.linetype or "").upper()
                effective = own if own and own != "BYLAYER" else layer_linetypes.get(layer, "")
                cls = "electric" if "DASH" in effective else "signal_unknown"
            edge.line_class, edge.class_source = cls, "layer"
            edges[edge.id] = edge
        elif include_foreign:
            foreign_edges[edge.id] = edge

    # 3 — spatial index of ports (edge vertices join once membership is settled)
    grid = _Grid(cell=10.0 * tolerance)
    radial_ports: list[tuple[str, str, float, float, float]] = []
    for node in nodes.values():
        for pname, port in node.ports.items():
            if port.get("radius", 0.0) > 0:
                # a bubble's line starts on its circle, not at its centre
                radial_ports.append((node.id, pname, port["x"], port["y"], port["radius"]))
            else:
                grid.add(node.space, port["x"], port["y"], ("port", node.id, pname))

    # 4 — resolve endpoints
    junctions: list[dict] = []
    dangling: list[dict] = []

    def _end(edge: _Edge, end: str) -> tuple[float, float]:
        return edge.vertices[0] if end == "from" else edge.vertices[-1]

    def _attach(edge: _Edge, end: str) -> dict | None:
        """``{node, port}`` when this end sits on a port, or on the boundary of
        a foreign block's drawn box; None otherwise. Only a successful match
        has side effects."""
        x, y = _end(edge, end)
        for node_id, pname, cx, cy, r in radial_ports:
            if nodes[node_id].space != edge.space:
                continue
            if abs(math.hypot(x - cx, y - cy) - r) <= tolerance:
                nodes[node_id].ports[pname]["edges"].append(edge.id)
                return {"node": node_id, "port": pname}
        best = None
        for d, item in grid.near(edge.space, x, y, tolerance):
            if item[0] == "port" and (best is None or d < best[0]):
                best = (d, item)
        if best:
            _, (_kind, node_id, pname) = best
            nodes[node_id].ports[pname]["edges"].append(edge.id)
            return {"node": node_id, "port": pname}
        # Two foreign boundaries can share the end (a valve drawn flush with
        # a vessel's outline, a nested detail); the one inserted nearest wins.
        node = min(
            (
                n
                for n in list(nodes.values()) + list(candidates.values())
                if n.source in ("heuristic", "inferred")
                and n.space == edge.space
                and n.bbox
                and _on_bbox_boundary((x, y), n.bbox, tolerance)
            ),
            key=lambda n: math.hypot(x - n.x, y - n.y),
            default=None,
        )
        if node is not None:
            if node.id in candidates:
                nodes[node.id] = candidates.pop(node.id)
            pname = f"p{len(node.ports) + 1}"
            node.ports[pname] = {
                "name": pname,
                "x": x,
                "y": y,
                "direction_deg": None,
                "kind": "process",
                "radius": 0.0,
                "inferred": True,
                "edges": [edge.id],
            }
            grid.add(node.space, x, y, ("port", node.id, pname))
            return {"node": node.id, "port": pname}
        return None

    def _junction_at(space, x, y, edge_id, other_id) -> dict:
        for j in junctions:
            if j["space"] == space and math.hypot(j["x"] - x, j["y"] - y) <= tolerance:
                for e in (edge_id, other_id):
                    if e not in j["edges"]:
                        j["edges"].append(e)
                return {"junction": j["id"]}
        j = {
            "id": f"J{len(junctions) + 1}",
            "x": x,
            "y": y,
            "edges": [edge_id, other_id],
            "space": space,
        }
        junctions.append(j)
        return {"junction": j["id"]}

    def _junction_or_dangling(edge: _Edge, end: str) -> dict | None:
        x, y = _end(edge, end)
        for _d, item in grid.near(edge.space, x, y, tolerance):
            if item[0] == "vertex" and item[1] != edge.id:
                return _junction_at(edge.space, x, y, edge.id, item[1])
        for other in edges.values():
            if other.id == edge.id or other.space != edge.space:
                continue
            for a, b, bulge in other.segments():
                if _point_segment_distance((x, y), a, b, bulge) <= tolerance:
                    return _junction_at(edge.space, x, y, edge.id, other.id)
        nearest = None
        for d, item in grid.near(edge.space, x, y, 10.0 * tolerance):
            if item[0] == "port" and (nearest is None or d < nearest[0]):
                nearest = (d, item)
        dangling.append(
            {
                "edge": edge.id,
                "end": end,
                "x": x,
                "y": y,
                "nearest": (
                    {"node": nearest[1][1], "port": nearest[1][2], "distance": round(nearest[0], 6)}
                    if nearest
                    else None
                ),
            }
        )
        return None

    # 4a — node attachment first, so a foreign line's membership (spec §9.2
    # step 4c: it is an edge only when an end touches a node) is settled
    # before any junction exists. Resolving junctions first let a P&ID line
    # end on a foreign line that was then discarded, leaving a `{"junction"}`
    # reference to nothing and one dangling end short.
    for edge in list(edges.values()):
        edge.from_ref = _attach(edge, "from")
        edge.to_ref = _attach(edge, "to")
    for edge in list(foreign_edges.values()):
        from_ref = _attach(edge, "from")
        to_ref = _attach(edge, "to")
        if from_ref or to_ref:
            edge.from_ref, edge.to_ref = from_ref, to_ref
            edges[edge.id] = edge
    # 4b — junctions and dangling ends over the edges that are in the graph
    for edge in edges.values():
        for v in edge.vertices:
            grid.add(edge.space, v[0], v[1], ("vertex", edge.id))
    for edge in edges.values():
        if edge.from_ref is None:
            edge.from_ref = _junction_or_dangling(edge, "from")
        if edge.to_ref is None:
            edge.to_ref = _junction_or_dangling(edge, "to")

    # 5 — tags and line numbers from attributes or nearby text
    texts = [(info, space) for info, space in entities if info.type in TEXT_TYPES]

    def _text_near(space, x, y, radius, pattern=None, above=None, exclude=None):
        """Nearest text (by insertion) within ``radius`` of (x, y) in ``space``.

        ``pattern`` keeps only matching content; ``exclude`` drops it. With
        ``above`` (a box top), only a text whose insertion is at or above
        ``above - height / 2`` counts — an equipment tag is lettered over the
        symbol (spec §9.2 step 6), and a full circle around the top-centre
        let a note or line label under the body win over the tag above it.
        """
        best = None
        for info, text_space in texts:
            if text_space != space:
                continue
            ins = info.properties.get("insertion")
            content = str(info.properties.get("text", "")).strip()
            if not ins or not content or (pattern and not pattern.match(content)):
                continue
            if exclude and exclude.match(content):
                continue
            if above is not None:
                height = info.properties.get("height", info.properties.get("char_height", 0.0))
                if float(ins[1]) < above - 0.5 * float(height or 0.0):
                    continue
            d = math.hypot(float(ins[0]) - x, float(ins[1]) - y)
            if d <= radius and (best is None or d < best[0]):
                best = (d, content)
        return best[1] if best else None

    for node in nodes.values():
        attrs = node.attributes
        if node.kind == "instrument" and attrs.get("FUNC"):
            node.tag = f"{attrs['FUNC']}-{attrs.get('LOOP', '')}".rstrip("-")
            node.tag_source = "attribute"
        else:
            key = next((k for k in ATTRIBUTE_TAGS if attrs.get(k)), None)
            if key:
                node.tag, node.tag_source = str(attrs[key]).strip(), "attribute"
        if node.tag is None:
            radius = max((p.get("radius", 0.0) for p in node.ports.values()), default=0.0)
            if node.kind == "instrument" and radius:
                found = _text_near(node.space, node.x, node.y, radius)
            else:
                cx = ((node.bbox[0] + node.bbox[2]) / 2.0) if node.bbox else node.x
                top = node.bbox[3] if node.bbox else node.y
                found = _text_near(
                    node.space, cx, top, label_search, above=top, exclude=LINE_NUMBER_RE
                )
            if found:
                node.tag, node.tag_source = found, "text"
    for edge in edges.values():
        if edge.line_number:
            continue
        a, b = max(zip(edge.vertices, edge.vertices[1:], strict=False), key=lambda s: math.dist(*s))
        found = _text_near(
            edge.space, (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, label_search, LINE_NUMBER_RE
        )
        if found:
            edge.line_number, edge.number_source = found, "label"

    # 6 — off-page links
    links: dict[str, list[str]] = {}
    for node in nodes.values():
        link = node.attributes.get("LINK")
        if node.kind == "connector" and link:
            links.setdefault(str(link), []).append(node.id)
    offpage_links = [{"link": k, "nodes": sorted(v)} for k, v in sorted(links.items())]

    # 7 — assemble
    node_rows = [n.to_dict() for n in nodes.values()]
    edge_rows = [e.to_dict(include_geometry) for e in edges.values()]
    by_kind: dict[str, int] = {}
    for n in node_rows:
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
    by_class: dict[str, int] = {}
    for e in edge_rows:
        key = e["line_class"] or "unknown"
        by_class[key] = by_class.get(key, 0) + 1
    return {
        "nodes": node_rows,
        "edges": edge_rows,
        "junctions": junctions,
        "dangling": dangling,
        "unclassified": [{"handle": c.id, "block_name": c.block_name} for c in candidates.values()],
        "offpage_links": offpage_links,
        "stats": {
            "nodes_by_kind": dict(sorted(by_kind.items())),
            "edges_by_class": dict(sorted(by_class.items())),
            "dangling": len(dangling),
            "unclassified": len(candidates),
            "confidence_min": min((n["confidence"] for n in node_rows), default=1.0),
            "entities_scanned": len(entities),
        },
        "tolerance": tolerance,
        "scope": scope,
        "backend": backend.name,
    }
