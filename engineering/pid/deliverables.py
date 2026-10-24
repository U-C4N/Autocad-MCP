"""Instrument index, line list and equipment list — rows derived from the graph.

Every row comes from ``build_graph`` (spec §9.5): the deliverables never read
the catalogue or a typed-in list, so a list of a foreign P&ID is as honest as
the graph's confidence says. Row keys are in the documented order; rows sort
by tag with a natural key (``P-2`` before ``P-10``) and untagged rows last.

A column that names "what is at the other end" (``connected_to``, ``from_tag``,
``to_tag``) names something a reader can find in the drawing: a node's tag, or
its handle when it has none; for a junction (a tap into another line), the
line number of every line it joins, or that line's handle when it is
unnumbered. The reader's internal junction ids (``J1``) never appear.

The CSV goes through ``engineering.export.cells``: text that starts like a
formula is prefixed with ``'``, numbers stay numbers (1.7.0, B03).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from engineering.export.cells import write_csv as write_cells

from .graph import _is_signal_class, build_graph

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

_NUM = re.compile(r"(\d+)")

EQUIPMENT_KINDS = ("equipment", "valve", "connector", "unknown_block")

INDEX_COLUMNS = (
    "tag",
    "func",
    "loop",
    "description",
    "type",
    "location",
    "connected_to",
    "signal_lines",
    "handle",
    "confidence",
)
LINE_COLUMNS = (
    "line_number",
    "line_class",
    "size",
    "service",
    "spec",
    "insulation",
    "from_tag",
    "from_port",
    "to_tag",
    "to_port",
    "length_mm",
    "handle",
    "number_source",
)
EQUIPMENT_COLUMNS = (
    "tag",
    "kind",
    "symbol",
    "description",
    "ports",
    "connected_lines",
    "unconnected_ports",
    "handle",
    "confidence",
)
#: Columns written as numbers; every other column is text (a loop number such
#: as ``101A`` and a size such as ``100`` are designations, not quantities).
NUMBER_COLUMNS = frozenset({"signal_lines", "confidence", "length_mm", "ports", "connected_lines"})


def natural_key(tag):
    """Sort key: digit runs compare numerically, ``None`` sorts after every tag.

    ``isdecimal`` and not ``isdigit``: ``'²'`` and ``'①'`` are digits that
    ``int`` refuses, and ``\\d`` (which splits the runs) matches exactly the
    decimal ones.
    """
    if tag is None:
        return (1, [])
    return (0, [int(part) if part.isdecimal() else part for part in _NUM.split(str(tag))])


def _node_map(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


def _node_name(node: dict) -> str:
    """A node's tag, or its handle when it has none."""
    return node.get("tag") or node["id"]


def _junction_names(junction: dict, edge_id: str, edges: dict) -> list[str]:
    """The line number (or handle) of every line a junction joins, ``edge_id`` aside."""
    names = []
    for other_id in junction["edges"]:
        if other_id != edge_id:
            other = edges.get(other_id) or {}
            names.append(other.get("line_number") or other_id)
    return names


def _tag_of(ref: dict | None, edge_id: str, nodes: dict, edges: dict, junctions: dict):
    """``(tag, port)`` of a line end: a node's name and port, or the lines a junction joins."""
    if not ref:
        return None, None
    if "node" in ref:
        return _node_name(nodes[ref["node"]]), ref.get("port")
    junction = junctions.get(ref.get("junction"))
    if junction is None:
        return None, None
    return ", ".join(dict.fromkeys(_junction_names(junction, edge_id, edges))) or None, None


def _bubble_part(node: dict, index: int) -> str | None:
    """``PID_INST_<TYPE>_<LOCATION...>`` → type (index 2) or location (index 3+)."""
    name = node.get("block_name") or ""
    if not name.startswith("PID_INST_"):
        return None
    parts = name.split("_")
    if index == 2:
        return parts[2].lower() if len(parts) > 2 else None
    return "_".join(parts[3:]).lower() if len(parts) > 3 else None


def _far_ends(
    node_id: str, edge: dict, nodes: dict, edges: dict, junctions: dict, on_edge: dict
) -> list[str]:
    """What an instrument's line reaches (spec 9.5).

    A node end names that node's tag, or its handle when it has none. A
    junction — at either end of the line, or on it, where another line tees
    into its middle — names the line number of every other line it joins, or
    that line's handle when it is unnumbered. The instrument's own line number
    is never an answer: Task 13 stamps one on every line, so falling back to
    it would label every untagged mounting with the wire instead of the thing
    it is mounted on.
    """
    names: list[str] = []
    seen: set[str] = set()
    for ref in (edge["from"], edge["to"]):
        if not ref:
            continue  # dangling end — nothing to name
        if "node" in ref:
            if ref["node"] != node_id:
                names.append(_node_name(nodes[ref["node"]]))
        elif "junction" in ref and ref["junction"] in junctions:
            seen.add(ref["junction"])
            names.extend(_junction_names(junctions[ref["junction"]], edge["id"], edges))
    for junction in on_edge.get(edge["id"], ()):
        if junction["id"] not in seen:
            seen.add(junction["id"])
            names.extend(_junction_names(junction, edge["id"], edges))
    return names


def _junctions_by_edge(junctions: dict) -> dict[str, list[dict]]:
    by_edge: dict[str, list[dict]] = {}
    for junction in junctions.values():
        for edge_id in junction["edges"]:
            by_edge.setdefault(edge_id, []).append(junction)
    return by_edge


def instrument_index(graph: dict) -> list[dict]:
    nodes = _node_map(graph)
    edges = {e["id"]: e for e in graph["edges"]}
    junctions = {j["id"]: j for j in graph.get("junctions", [])}
    on_edge = _junctions_by_edge(junctions)
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
                # A process line drawn onto a bubble is a connection, not a
                # signal line (ISA-5.1 Table 5.3.1 classes only).
                if _is_signal_class(edge.get("line_class")):
                    signal_lines += 1
                connected.extend(_far_ends(node["id"], edge, nodes, edges, junctions, on_edge))
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
    edges = {e["id"]: e for e in graph["edges"]}
    junctions = {j["id"]: j for j in graph.get("junctions", [])}
    rows = []
    for edge in graph["edges"]:
        from_tag, from_port = _tag_of(edge["from"], edge["id"], nodes, edges, junctions)
        to_tag, to_port = _tag_of(edge["to"], edge["id"], nodes, edges, junctions)
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


def _csv_target(path: str):
    """Validate a CSV target inside ALLOWED_PATHS; refuse a directory by name.

    ``validate_path(allow_write=True)`` already refuses an empty path and a
    missing parent directory, which is why there is no ``mkdir`` here.
    """
    from fastmcp.exceptions import ToolError

    from security import validate_path

    target = validate_path(path, allow_write=True)
    if target.is_dir():
        raise ToolError(f"csv_path: '{target}' is a directory; name the .csv file to write")
    return target


def write_csv(rows: list[dict], path: str, columns=None) -> str:
    """Write ``rows`` as CSV inside ALLOWED_PATHS; return the resolved path.

    ``columns`` is the header (the builder's documented order); without it the
    first row's keys are used, so zero rows need ``columns`` — a header-only
    file is written then, never a 0-byte one. Text cells that start like a
    formula get a leading ``'``; the :data:`NUMBER_COLUMNS` stay numbers.
    """
    if columns is None:
        if not rows:
            raise ValueError("write_csv: no rows and no columns; pass the columns for the header")
        columns = list(rows[0])
    target = _csv_target(path)
    kinds = ["number" if c in NUMBER_COLUMNS else "text" for c in columns]
    write_cells(target, columns, ([row.get(c) for c in columns] for row in rows), kinds)
    return str(target)


_BUILDERS = {
    "instrument_index": (instrument_index, INDEX_COLUMNS),
    "line_list": (line_list, LINE_COLUMNS),
    "equipment_list": (equipment_list, EQUIPMENT_COLUMNS),
}


async def deliverable(
    backend: AutoCADBackend, which: str, csv_path: str | None = None, **graph_kwargs
) -> dict:
    """Build the graph once and derive ``which`` deliverable from it; optionally write CSV.

    ``csv_path`` is validated before the drawing is read; ``""`` is a path (and
    refused as empty), only ``None`` means "no CSV".
    """
    if which not in _BUILDERS:
        raise ValueError(f"which must be one of {', '.join(_BUILDERS)}, got {which!r}")
    if csv_path is not None:
        _csv_target(csv_path)
    graph = await build_graph(backend, **graph_kwargs)
    builder, columns = _BUILDERS[which]
    rows = builder(graph)
    written = write_csv(rows, csv_path, columns) if csv_path is not None else None
    return {"rows": rows, "count": len(rows), "csv_path": written, "graph_stats": graph["stats"]}
