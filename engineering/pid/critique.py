"""Six P&ID focuses over one graph per critique run.

Every check is pure over the graph dict; ``run_critique`` builds the graph once
into the per-run ``shared`` context so six focuses cost one read. A drawing
with no P&ID nodes yields no issues — mechanical sheets are never scored here.
"""

from __future__ import annotations

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


def _dangling(graph: dict) -> list[Issue]:
    out = []
    for d in graph["dangling"]:
        nearest = d.get("nearest")
        if nearest:
            hint = (
                f"pid_line_draw(from_={{'handle': ..., 'port': ...}}, "
                f"to={{'handle': '{nearest['node']}', 'port': '{nearest['port']}'}}) "
                f"— nearest port {nearest['distance']} mm away"
            )
        else:
            hint = "no port within reach; connect this end with pid_line_draw or delete the line"
        out.append(
            Issue(
                "error",
                "pid_dangling_line",
                f"Line {d['edge']} {d['end']} end at ({d['x']:.2f}, {d['y']:.2f}) "
                "connects to nothing.",
                [d["edge"]],
                {"end": d["end"], "x": d["x"], "y": d["y"], "nearest": nearest, "hint": hint},
            )
        )
    return out


def _duplicate_tags(graph: dict) -> list[Issue]:
    seen: dict[str, list[str]] = {}
    for node in graph["nodes"]:
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
    for n in graph["nodes"]:
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
    if not graph["nodes"]:
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
