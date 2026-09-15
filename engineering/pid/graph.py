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
    layer: str
    space: str
    bbox: tuple[float, float, float, float] | None
    source: str
    confidence: float
    attributes: dict
    ports: dict = field(default_factory=dict)
    tag: str | None = None
    tag_source: str | None = None

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
            "layer": self.layer,
            "space": self.space,
            "ports": self.ports,
            "description": self.attributes.get("DESC") or None,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class _Edge:
    id: str
    vertices: list[tuple[float, float]]
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
            "length": round(
                sum(
                    math.dist(a, b) for a, b in zip(self.vertices, self.vertices[1:], strict=False)
                ),
                6,
            ),
            "layer": self.layer,
            "space": self.space,
            "source": self.source,
        }
        if include_geometry:
            out["vertices"] = [[x, y] for x, y in self.vertices]
        return out


class _Grid:
    """Uniform grid for nearest-within-tolerance queries."""

    def __init__(self, cell: float):
        self.cell = cell
        self.cells: dict[tuple[int, int], list] = {}

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return (int(math.floor(x / self.cell)), int(math.floor(y / self.cell)))

    def add(self, x: float, y: float, item) -> None:
        self.cells.setdefault(self._key(x, y), []).append((x, y, item))

    def near(self, x: float, y: float, radius: float):
        kx, ky = self._key(x, y)
        reach = int(math.ceil(radius / self.cell))
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for px, py, item in self.cells.get((kx + dx, ky + dy), []):
                    d = math.hypot(px - x, py - y)
                    if d <= radius:
                        yield d, item


def _vertices(info: EntityInfo) -> list[tuple[float, float]]:
    if info.type in LINE_TYPES:
        s, e = info.properties["start"], info.properties["end"]
        return [(float(s[0]), float(s[1])), (float(e[0]), float(e[1]))]
    return [(float(p[0]), float(p[1])) for p in info.properties.get("points") or []]


def _point_segment_distance(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _keyword_kind(block_name: str) -> str | None:
    """Kind implied by a foreign block's name, or None when no token is a keyword."""
    for token in _TOKEN_RE.findall(block_name.upper()):
        kind = KEYWORDS.get(token)
        if kind:
            return kind
    return None


def _bbox(info: EntityInfo):
    bb = info.properties.get("bounding_box")
    if not bb:
        return None
    return (float(bb["min"][0]), float(bb["min"][1]), float(bb["max"][0]), float(bb["max"][1]))


def _on_bbox_boundary(p, bbox, tolerance) -> bool:
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
        base = dict(
            id=info.handle,
            block_name=name,
            x=float(ins[0]),
            y=float(ins[1]),
            rotation_deg=rotation,
            scale=scale,
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
            for pname, (lx, ly, ldir, kind, radius) in payload.get("ports", {}).items():
                # Measured from the INSERT as it is: a mirrored symbol carries
                # y_scale = -1 and its ports sit on the mirrored geometry.
                local = Port(pname, float(lx), float(ly), ldir, kind, float(radius))
                try:
                    port = transform_port(local, node.x, node.y, rotation, scale, y_scale)
                except ValueError:
                    # A stretched INSERT (someone scaled one axis by hand): the
                    # reader never refuses a drawing, so it measures along the
                    # X factor and says so instead of raising.
                    port = transform_port(local, node.x, node.y, rotation, scale)
                    port["inferred"] = True
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

    # 3 — spatial index of ports and vertices
    grid = _Grid(cell=10.0 * tolerance)
    radial_ports: list[tuple[str, str, float, float, float]] = []
    for node in nodes.values():
        for pname, port in node.ports.items():
            if port.get("radius", 0.0) > 0:
                # a bubble's line starts on its circle, not at its centre
                radial_ports.append((node.id, pname, port["x"], port["y"], port["radius"]))
            else:
                grid.add(port["x"], port["y"], ("port", node.id, pname))
    all_edges = {**edges, **foreign_edges}
    for edge in all_edges.values():
        for v in edge.vertices:
            grid.add(v[0], v[1], ("vertex", edge.id))

    # 4 — resolve endpoints
    junctions: list[dict] = []
    dangling: list[dict] = []

    def _junction_at(x, y, edge_id, other_id) -> dict:
        for j in junctions:
            if math.hypot(j["x"] - x, j["y"] - y) <= tolerance:
                for e in (edge_id, other_id):
                    if e not in j["edges"]:
                        j["edges"].append(e)
                return {"junction": j["id"]}
        j = {"id": f"J{len(junctions) + 1}", "x": x, "y": y, "edges": [edge_id, other_id]}
        junctions.append(j)
        return {"junction": j["id"]}

    def _resolve(edge: _Edge, end: str) -> dict | None:
        x, y = edge.vertices[0] if end == "from" else edge.vertices[-1]
        for node_id, pname, cx, cy, r in radial_ports:
            if abs(math.hypot(x - cx, y - cy) - r) <= tolerance:
                nodes[node_id].ports[pname]["edges"].append(edge.id)
                return {"node": node_id, "port": pname}
        best = None
        for d, item in grid.near(x, y, tolerance):
            if item[0] == "port" and (best is None or d < best[0]):
                best = (d, item)
        if best:
            _, (_kind, node_id, pname) = best
            nodes[node_id].ports[pname]["edges"].append(edge.id)
            return {"node": node_id, "port": pname}
        for node in list(nodes.values()) + list(candidates.values()):
            if (
                node.source in ("heuristic", "inferred")
                and node.bbox
                and _on_bbox_boundary((x, y), node.bbox, tolerance)
            ):
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
                grid.add(x, y, ("port", node.id, pname))
                return {"node": node.id, "port": pname}
        for _d, item in grid.near(x, y, tolerance):
            if item[0] == "vertex" and item[1] != edge.id:
                return _junction_at(x, y, edge.id, item[1])
        for other in all_edges.values():
            if other.id == edge.id:
                continue
            for a, b in zip(other.vertices, other.vertices[1:], strict=False):
                if _point_segment_distance((x, y), a, b) <= tolerance:
                    return _junction_at(x, y, edge.id, other.id)
        nearest = None
        for d, item in grid.near(x, y, 10.0 * tolerance):
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

    for edge in list(edges.values()):
        edge.from_ref = _resolve(edge, "from")
        edge.to_ref = _resolve(edge, "to")
    for edge in list(foreign_edges.values()):
        # a foreign line joins the graph only through a node
        probe_from = _resolve(edge, "from")
        probe_to = _resolve(edge, "to")
        attached = any(ref and "node" in ref for ref in (probe_from, probe_to))
        if attached:
            edge.from_ref, edge.to_ref = probe_from, probe_to
            edges[edge.id] = edge
        else:
            dangling[:] = [d for d in dangling if d["edge"] != edge.id]
            for j in junctions:
                if edge.id in j["edges"]:
                    j["edges"].remove(edge.id)
    junctions[:] = [j for j in junctions if len(j["edges"]) >= 2]

    # 5 — tags and line numbers from attributes or nearby text
    texts = [(info, space) for info, space in entities if info.type in TEXT_TYPES]

    def _text_near(x, y, radius, pattern=None):
        best = None
        for info, _space in texts:
            ins = info.properties.get("insertion")
            content = str(info.properties.get("text", "")).strip()
            if not ins or not content or (pattern and not pattern.match(content)):
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
                found = _text_near(node.x, node.y, radius)
            else:
                cx = ((node.bbox[0] + node.bbox[2]) / 2.0) if node.bbox else node.x
                top = node.bbox[3] if node.bbox else node.y
                found = _text_near(cx, top, label_search)
            if found:
                node.tag, node.tag_source = found, "text"
    for edge in edges.values():
        if edge.line_number:
            continue
        a, b = max(zip(edge.vertices, edge.vertices[1:], strict=False), key=lambda s: math.dist(*s))
        found = _text_near((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, label_search, LINE_NUMBER_RE)
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
