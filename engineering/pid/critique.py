"""Six P&ID focuses over one graph per critique run.

Every check is pure over the graph dict; ``run_critique`` builds the graph once
into the per-run ``shared`` context so six focuses cost one read. A drawing
with no recognised P&ID symbol yields no issues — mechanical sheets are never
scored here.

The checks score evidence, never a guess. ``build_graph(include_foreign=True)``
files *any* INSERT as an ``unknown_block`` at confidence 0.3 the moment a plain
LINE's end lands on its bounding box (spec §9.2 step 2) — a bolt with a note
leader qualifies, and so does the sheet frame — and gives every foreign block's
inferred port a placeholder ``kind: "process"``. Neither is a measurement (spec
§9.4: read the confidence before trusting a foreign graph), so no focus rests
on them in *either* direction: an ``unknown_block`` is not P&ID content, is
never tagged or scored, and an edge anchored on nothing better is never called
dangling — but it is nothing the drawing vouches for either, so a P&ID line by
evidence whose end resolves to one *is* dangling (spec §11.1: the end resolves
to nothing); an inferred port is never called incompatible.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from engineering.plan_spec import Issue

from .graph import build_graph
from .lines import LINE_CLASSES
from .tags import parse_tag

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

PID_FOCUSES = (
    "pid_dangling_line",
    "pid_duplicate_tag",
    "pid_incompatible_connection",
    "pid_untagged_instrument",
    "pid_illegal_tag",
    "pid_unconnected_equipment",
)
_KEY = "pid_graph"


def _recognised(node: dict) -> bool:
    """A node the drawing vouches for: catalogue, XDATA or heuristic (a keyword
    block name / tag attribute). ``unknown_block`` is a 0.3 touch guess."""
    return node["kind"] != "unknown_block"


def has_pid_content(graph: dict) -> bool:
    """True when at least one recognised P&ID symbol is on the sheet."""
    return any(_recognised(n) for n in graph["nodes"])


def _scored_nodes(graph: dict) -> list[dict]:
    return [n for n in graph["nodes"] if _recognised(n)]


def _evidenced_edges(graph: dict) -> set[str]:
    """Edge ids that are P&ID lines by evidence: a line class from XDATA or a
    P&ID line layer, or an end on a recognised node. A foreign line whose only
    anchor is an ``unknown_block`` box is in the graph on a guess."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    out: set[str] = set()
    for edge in graph["edges"]:
        if edge.get("class_source"):
            out.add(edge["id"])
            continue
        for ref in (edge["from"], edge["to"]):
            if ref and "node" in ref and _recognised(nodes[ref["node"]]):
                out.add(edge["id"])
                break
    return out


def _nearest_recognised_port(graph: dict, nodes: dict, space, x: float, y: float) -> dict | None:
    """The same hint the graph gives a free end (spec §9.2 step 5: the nearest
    port within ``10·tolerance``), over ports the drawing vouches for. A
    bubble's radial port is matched on its circle, not its centre, so it is
    left out exactly as the graph's grid leaves it out."""
    reach = 10.0 * graph["tolerance"]
    best = None
    for node in nodes.values():
        if not _recognised(node) or node["space"] != space:
            continue
        for pname, port in node["ports"].items():
            if port.get("radius", 0.0) > 0:
                continue
            d = math.hypot(port["x"] - x, port["y"] - y)
            if d <= reach and (best is None or d < best["distance"]):
                best = {"node": node["id"], "port": pname, "distance": round(d, 6)}
    return best


def _dangling_ends(graph: dict) -> list[dict]:
    """Free ends of evidenced edges, in the graph's ``dangling`` row shape.

    Two sources, one rule (spec §11.1: the end resolves to nothing). The graph's
    own ``dangling`` rows are ends it could attach to nothing at all. An end the
    graph attached to an ``unknown_block`` is the other half: that node is a 0.3
    touch guess with a port invented at the line's own end, so a sheet frame,
    a logo or a note symbol "connects" whatever line happens to stop on its box.
    A 0.3 guess cannot create a finding, and it cannot suppress one either.
    """
    evidenced = _evidenced_edges(graph)
    nodes = {n["id"]: n for n in graph["nodes"]}
    rows = [d for d in graph["dangling"] if d["edge"] in evidenced]
    for edge in graph["edges"]:
        if edge["id"] not in evidenced:
            continue
        for end in ("from", "to"):
            ref = edge[end]
            if not ref or "node" not in ref or _recognised(nodes[ref["node"]]):
                continue
            port = nodes[ref["node"]]["ports"][ref["port"]]
            x, y = port["x"], port["y"]
            rows.append(
                {
                    "edge": edge["id"],
                    "end": end,
                    "x": x,
                    "y": y,
                    "nearest": _nearest_recognised_port(graph, nodes, edge["space"], x, y),
                    "touches": ref["node"],
                }
            )
    return rows


def _dangling(graph: dict) -> list[Issue]:
    out = []
    for d in _dangling_ends(graph):
        nearest = d.get("nearest")
        if nearest:
            hint = (
                f"pid_line_draw(from_={{'handle': ..., 'port': ...}}, "
                f"to={{'handle': '{nearest['node']}', 'port': '{nearest['port']}'}}) "
                f"— nearest port {nearest['distance']} mm away"
            )
        else:
            hint = "no port within reach; connect this end with pid_line_draw or delete the line"
        detail = {"end": d["end"], "x": d["x"], "y": d["y"], "nearest": nearest, "hint": hint}
        if d.get("touches"):
            detail["touches"] = d["touches"]
        out.append(
            Issue(
                "error",
                "pid_dangling_line",
                f"Line {d['edge']} {d['end']} end at ({d['x']:.2f}, {d['y']:.2f}) "
                "connects to nothing.",
                [d["edge"]],
                detail,
            )
        )
    return out


def _duplicate_tags(graph: dict) -> list[Issue]:
    seen: dict[str, list[str]] = {}
    for node in _scored_nodes(graph):
        # connectors carry free text ("TO P&ID-002"), never a unique tag
        if node.get("tag") and node["kind"] != "connector":
            seen.setdefault(node["tag"].upper(), []).append(node["id"])
    return [
        Issue(
            "error",
            "pid_duplicate_tag",
            f"Tag {tag} is carried by {len(handles)} symbols.",
            handles,
            {
                "tag": tag,
                "hint": "block_set_attributes(handle, {'TAG'|'FUNC'/'LOOP': ...}) on all but one",
            },
        )
        for tag, handles in seen.items()
        if len(handles) > 1
    ]


def _incompatible(graph: dict) -> list[Issue]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    out = []
    for edge in graph["edges"]:
        cls = LINE_CLASSES.get(edge.get("line_class") or "")
        if cls is None:
            continue
        for ref in (edge["from"], edge["to"]):
            if not ref or "node" not in ref:
                continue
            port = nodes[ref["node"]]["ports"].get(ref["port"], {})
            if port.get("inferred"):
                # A foreign block's port kind is a placeholder, not a
                # measurement: a signal line to a CTO control valve's actuator
                # is right, and "redraw as a process line" would be the wrong
                # fix. Only a declared (catalogue / XDATA) port kind is judged.
                continue
            actual = port.get("kind")
            if actual in ("process", "signal") and actual != cls.kind:
                out.append(
                    Issue(
                        "error",
                        "pid_incompatible_connection",
                        f"{cls.kind} line {edge['id']} ends on {actual} port {ref['port']} "
                        f"of {ref['node']}.",
                        [edge["id"], ref["node"]],
                        {
                            "expected": cls.kind,
                            "actual": actual,
                            "port": ref["port"],
                            "line_class": edge["line_class"],
                            "hint": (
                                f"redraw with a {actual} line class or connect to a {cls.kind} port"
                            ),
                        },
                    )
                )
    return out


def _untagged(graph: dict) -> list[Issue]:
    return [
        Issue(
            "warning",
            "pid_untagged_instrument",
            f"Instrument {n['id']} has no tag.",
            [n["id"]],
            {"hint": "block_set_attributes(handle, {'FUNC': 'FIC', 'LOOP': '101'})"},
        )
        for n in graph["nodes"]
        if n["kind"] == "instrument" and not n.get("tag")
    ]


def _illegal(graph: dict) -> list[Issue]:
    out = []
    for n in _scored_nodes(graph):
        if not n.get("tag") or n["kind"] == "connector":
            continue
        parsed = parse_tag(n["tag"], kind="instrument" if n["kind"] == "instrument" else "auto")
        if not parsed["valid"]:
            out.append(
                Issue(
                    "warning",
                    "pid_illegal_tag",
                    f"Tag {n['tag']} on {n['id']} is not ISA-5.1: {parsed['errors'][0]}",
                    [n["id"]],
                    {"errors": parsed["errors"], "hint": "block_set_attributes with a valid tag"},
                )
            )
    return out


def _unconnected(graph: dict) -> list[Issue]:
    return [
        Issue(
            "info",
            "pid_unconnected_equipment",
            f"{n['kind']} {n.get('tag') or n['id']} has no line on any port.",
            [n["id"]],
            {"hint": "informational — connect it with pid_line_draw or leave it if intended"},
        )
        for n in graph["nodes"]
        if n["kind"] in ("equipment", "valve")
        and n["ports"]
        and not any(p["edges"] for p in n["ports"].values())
    ]


_CHECKS = {
    "pid_dangling_line": _dangling,
    "pid_duplicate_tag": _duplicate_tags,
    "pid_incompatible_connection": _incompatible,
    "pid_untagged_instrument": _untagged,
    "pid_illegal_tag": _illegal,
    "pid_unconnected_equipment": _unconnected,
}


def issues_for(focus: str, graph: dict) -> list[Issue]:
    if not has_pid_content(graph):
        return []
    return _CHECKS[focus](graph)


def _make(focus: str):
    async def check(backend: AutoCADBackend, shared: dict) -> list[Issue]:
        graph = shared.get(_KEY)
        if graph is None:
            graph = shared[_KEY] = await build_graph(backend, include_foreign=True)
        return issues_for(focus, graph)

    check.needs_shared = True  # type: ignore[attr-defined]
    check.__name__ = f"check_{focus}"
    return check


PID_DISPATCH = {focus: _make(focus) for focus in PID_FOCUSES}
