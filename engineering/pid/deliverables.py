"""Instrument index, line list and equipment list — rows derived from the graph.

Every row comes from ``build_graph`` (spec §9.5): the deliverables never read
the catalogue or a typed-in list, so a list of a foreign P&ID is as honest as
the graph's confidence says. Row keys are in the documented order; rows sort
by tag with a natural key (``P-2`` before ``P-10``) and untagged rows last.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .graph import build_graph

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

_NUM = re.compile(r"(\d+)")

EQUIPMENT_KINDS = ("equipment", "valve", "connector", "unknown_block")


def natural_key(tag):
    """Sort key: digit runs compare numerically, ``None`` sorts after every tag."""
    if tag is None:
        return (1, [])
    return (0, [int(part) if part.isdigit() else part for part in _NUM.split(str(tag))])


def _node_map(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


def _tag_of(ref: dict | None, nodes: dict) -> tuple[str | None, str | None]:
    """``(tag, port)`` of an edge end: a node's tag and port, or the junction id."""
    if not ref:
        return None, None
    if "node" in ref:
        return nodes[ref["node"]].get("tag"), ref.get("port")
    return ref.get("junction"), None


def _bubble_part(node: dict, index: int) -> str | None:
    """``PID_INST_<TYPE>_<LOCATION...>`` → type (index 2) or location (index 3+)."""
    name = node.get("block_name") or ""
    if not name.startswith("PID_INST_"):
        return None
    parts = name.split("_")
    if index == 2:
        return parts[2].lower() if len(parts) > 2 else None
    return "_".join(parts[3:]).lower() if len(parts) > 3 else None


def instrument_index(graph: dict) -> list[dict]:
    nodes = _node_map(graph)
    edges = {e["id"]: e for e in graph["edges"]}
    rows = []
    for node in graph["nodes"]:
        if node["kind"] != "instrument":
            continue
        parsed = node.get("tag_parsed") or {}
        connected: list[str] = []
        signal_lines = 0
        for port in node["ports"].values():
            for edge_id in port["edges"]:
                edge = edges[edge_id]
                signal_lines += 1
                for ref in (edge["from"], edge["to"]):
                    if ref and "node" in ref and ref["node"] != node["id"]:
                        other = nodes[ref["node"]]
                        connected.append(other.get("tag") or edge.get("line_number") or other["id"])
        rows.append(
            {
                "tag": node.get("tag"),
                "func": parsed.get("letters"),
                "loop": parsed.get("loop"),
                "description": parsed.get("description"),
                "type": _bubble_part(node, 2),
                "location": _bubble_part(node, 3),
                "connected_to": ", ".join(dict.fromkeys(connected)),
                "signal_lines": signal_lines,
                "handle": node["id"],
                "confidence": node["confidence"],
            }
        )
    rows.sort(key=lambda r: natural_key(r["tag"]))
    return rows


def line_list(graph: dict) -> list[dict]:
    nodes = _node_map(graph)
    rows = []
    for edge in graph["edges"]:
        from_tag, from_port = _tag_of(edge["from"], nodes)
        to_tag, to_port = _tag_of(edge["to"], nodes)
        rows.append(
            {
                "line_number": edge.get("line_number"),
                "line_class": edge.get("line_class"),
                "size": edge.get("size"),
                "service": edge.get("service"),
                "spec": edge.get("spec"),
                "insulation": edge.get("insulation"),
                "from_tag": from_tag,
                "from_port": from_port,
                "to_tag": to_tag,
                "to_port": to_port,
                "length_mm": round(float(edge.get("length") or 0.0), 1),
                "handle": edge["id"],
                "number_source": edge.get("number_source"),
            }
        )
    rows.sort(key=lambda r: natural_key(r["line_number"]))
    return rows


def equipment_list(graph: dict) -> list[dict]:
    rows = []
    for node in graph["nodes"]:
        if node["kind"] not in EQUIPMENT_KINDS:
            continue
        ports = node["ports"]
        rows.append(
            {
                "tag": node.get("tag"),
                "kind": node["kind"],
                "symbol": node.get("symbol"),
                "description": node.get("description")
                or (node.get("tag_parsed") or {}).get("equipment_kind"),
                "ports": len(ports),
                "connected_lines": sum(len(p["edges"]) for p in ports.values()),
                "unconnected_ports": ", ".join(name for name, p in ports.items() if not p["edges"]),
                "handle": node["id"],
                "confidence": node["confidence"],
            }
        )
    rows.sort(key=lambda r: natural_key(r["tag"]))
    return rows


def write_csv(rows: list[dict], path: str) -> str:
    """Write ``rows`` as CSV (header = row keys) inside ALLOWED_PATHS; return the resolved path."""
    from security import validate_path

    target = validate_path(path, allow_write=True)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if rows:
            writer.writerow(list(rows[0]))
            for row in rows:
                writer.writerow(["" if v is None else v for v in row.values()])
    return str(target)


_BUILDERS = {
    "instrument_index": instrument_index,
    "line_list": line_list,
    "equipment_list": equipment_list,
}


async def deliverable(
    backend: AutoCADBackend, which: str, csv_path: str | None = None, **graph_kwargs
) -> dict:
    """Build the graph once and derive ``which`` deliverable from it; optionally write CSV."""
    if which not in _BUILDERS:
        raise ValueError(f"which must be one of {', '.join(_BUILDERS)}, got {which!r}")
    graph = await build_graph(backend, **graph_kwargs)
    rows = _BUILDERS[which](graph)
    written = write_csv(rows, csv_path) if csv_path else None
    return {"rows": rows, "count": len(rows), "csv_path": written, "graph_stats": graph["stats"]}
