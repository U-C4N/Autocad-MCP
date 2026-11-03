"""Least-call oracles (1.8-14): how many tool calls a scripted job costs, two ways.

The rival's 124-vs-11 (H4, layer cleanup) and 89-vs-10 (H5, part
dimensioning) are call-count claims. This harness states them as scripted
jobs on fixtures built here, each run two ways through the real in-memory
server (a ``fastmcp.Client`` on ``server.mcp``, the headless engine):

``naive``
    One tool per entity - the shape the rival observed an agent take with this
    server. It is labelled as that on every report, never presented as the
    best this server can do.
``batched``
    The least-call oracle: the fewest calls the server's own verbs need. A
    scenario whose oracle needs a verb this tree does not have yet
    (``entity_modify_where`` / ``layer_merge`` 1.8-12, ``dimension_auto`` v2
    1.8-13, ``drawing_history`` 1.8-10) reports its batched arm as
    ``pending_tool`` with the missing verbs named - never as a pass - and the
    owning item writes the oracle.

Every arm that runs must also *do the job*: each scenario has a verifier that
reads the drawing back (outside the counted calls) - a cheap arm that leaves
the job half done is ``failed``, not cheap.

Ceilings live in ``benchmarks/token_budget.json`` (``task``). ``main`` exits 1
when a measured oracle exceeds its ceiling, any arm fails, or a ``ci``-gated
cell has no oracle; a ``release``-gated cell still pending is reported under
``pending`` (``--release`` turns that into a failure too).
``benchmarks/token_suite.py``'s task lane runs the same scenarios and prices
them: characters, modelled session input, cached and uncached.

    python -m benchmarks.call_budget
    python -m benchmarks.call_budget --json --out out/call-budget.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUDGET = ROOT / "benchmarks" / "token_budget.json"

SCHEMA_VERSION = "call-budget/1"

#: What every naive arm is, said on every report.
NAIVE_LABEL = (
    "naive: one tool per entity - the shape the rival observed an agent take with "
    "this server; not the least this server needs"
)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Fixtures (pure ezdxf: written to a file, opened by the scenario's setup)
# ---------------------------------------------------------------------------

#: The "messy layers" fixture: six misnamed layers and the ISO names they mean.
MESSY_LAYERS: dict[str, str] = {
    "geom": "GEOMETRY",
    "Dims": "DIM",
    "txt": "TEXT",
    "hidden lines": "HIDDEN",
    "centre": "CENTER",
    "Hatching": "HATCH",
}
MESSY_ENTITIES = 300

#: The plate: a closed LWPOLYLINE, six circles and two arcs, on GEOMETRY.
PLATE_OUTLINE = ((0.0, 0.0), (120.0, 0.0), (120.0, 80.0), (0.0, 80.0))
PLATE_HOLES = (
    (15.0, 15.0),
    (60.0, 15.0),
    (105.0, 15.0),
    (15.0, 65.0),
    (60.0, 65.0),
    (105.0, 65.0),
)
PLATE_HOLE_RADIUS = 5.5
PLATE_ARCS = ((40.0, 40.0, 8.0, 0.0, 180.0), (80.0, 40.0, 8.0, 180.0, 360.0))
#: Four edges, six diameters, two radii.
PLATE_DIMENSIONS = len(PLATE_OUTLINE) + len(PLATE_HOLES) + len(PLATE_ARCS)

#: A drawing somebody else made: nonstandard layers, tags, a mixed population.
FOREIGN_LAYERS = ("A-WALL", "M-PIPE-SUPPLY", "M-PIPE-RETURN", "E-POWER", "TAGS")
FOREIGN_ENTITIES = 300

#: history_value_lookup: the hole's diameter in rounds 1, 2 and 3.
HISTORY_DIAMETERS = (10.0, 12.0, 14.0)
HISTORY_ROUND = 2


def build_messy_layers(path: Path) -> Path:
    """Six misnamed layers, colour overrides, 300 entities at lineweight -3 (Default).

    The ISO layers exist already (a ``drawing_new`` would have made them); the
    job is to move every entity onto them, clear each entity's colour and
    lineweight to ByLayer and remove the misnamed layers.
    """
    import ezdxf

    doc = ezdxf.new("R2018")
    for name in MESSY_LAYERS.values():
        doc.layers.add(name)
    for index, name in enumerate(MESSY_LAYERS):
        doc.layers.add(name, color=index + 1)
    msp = doc.modelspace()
    names = list(MESSY_LAYERS)
    for index in range(MESSY_ENTITIES):
        layer = names[index % len(names)]
        attribs = {"layer": layer, "color": (index % 6) + 1, "lineweight": -3}
        x, y = float(index % 20) * 10.0, float(index // 20) * 10.0
        if index % 2:
            msp.add_circle((x + 4.0, y + 4.0), 2.0, dxfattribs=attribs)
        else:
            msp.add_line((x, y), (x + 8.0, y + 6.0), dxfattribs=attribs)
    doc.saveas(path)
    return path


def build_plate(path: Path) -> Path:
    import ezdxf

    doc = ezdxf.new("R2018")
    doc.layers.add("GEOMETRY")
    doc.layers.add("DIM")
    msp = doc.modelspace()
    msp.add_lwpolyline(PLATE_OUTLINE, close=True, dxfattribs={"layer": "GEOMETRY"})
    for x, y in PLATE_HOLES:
        msp.add_circle((x, y), PLATE_HOLE_RADIUS, dxfattribs={"layer": "GEOMETRY"})
    for cx, cy, radius, start, end in PLATE_ARCS:
        msp.add_arc((cx, cy), radius, start, end, dxfattribs={"layer": "GEOMETRY"})
    doc.saveas(path)
    return path


def build_foreign(path: Path) -> Path:
    import ezdxf

    doc = ezdxf.new("R2018")
    for name in FOREIGN_LAYERS:
        doc.layers.add(name)
    msp = doc.modelspace()
    for index in range(FOREIGN_ENTITIES):
        layer = FOREIGN_LAYERS[index % len(FOREIGN_LAYERS)]
        x, y = float(index % 25) * 400.0, float(index // 25) * 400.0
        shape = index % 4
        if layer == "TAGS":
            msp.add_text(
                f"P-{100 + index}", height=125.0, dxfattribs={"layer": layer, "insert": (x, y)}
            )
        elif shape == 0:
            msp.add_line((x, y), (x + 300.0, y), dxfattribs={"layer": layer})
        elif shape == 1:
            msp.add_circle((x + 150.0, y + 150.0), 100.0, dxfattribs={"layer": layer})
        elif shape == 2:
            msp.add_arc((x, y), 150.0, 0.0, 90.0, dxfattribs={"layer": layer})
        else:
            msp.add_lwpolyline(
                [(x, y), (x + 300.0, y), (x + 300.0, y + 200.0)], dxfattribs={"layer": layer}
            )
    doc.saveas(path)
    return path


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def result_text(result: Any) -> str:
    """The text a tool result hands the model: every text block, in order."""
    return "".join(
        text
        for text in (getattr(block, "text", None) for block in (result.content or []))
        if isinstance(text, str)
    )


class ArmError(RuntimeError):
    """A counted call answered an error: the arm did not do its job."""


@dataclass
class CallRecord:
    tool: str
    request_text: str
    result_text: str
    is_error: bool


class Recorder:
    """Counts, and keeps the text of, every call an arm makes."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls: list[CallRecord] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        result = await self.client.call_tool(tool, arguments, raise_on_error=False)
        text = result_text(result)
        request = _dumps({"name": tool, "arguments": arguments})
        self.calls.append(CallRecord(tool, request, text, bool(result.is_error)))
        if result.is_error:
            structured = result.structured_content or {}
            raise ArmError(f"{tool}: {structured.get('kind')}: {structured.get('message', text)}")
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text


async def _quiet(client: Any, tool: str, arguments: dict[str, Any]) -> Any:
    """A call outside the count (setup, verification): its answer, parsed."""
    text = result_text(await client.call_tool(tool, arguments))
    return json.loads(text) if text else None


async def _all_entities(client: Any, **filters: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    while True:
        page = await _quiet(client, "entity_list", {"limit": 1000, "offset": len(rows), **filters})
        rows.extend(page or [])
        if not page or len(page) < 1000:
            return rows


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

Arm = Callable[[Recorder, dict[str, Any]], Awaitable[Any]]
Verify = Callable[[Any, dict[str, Any], Any], Awaitable[tuple[bool, str]]]
Setup = Callable[[Any, Path], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    question: str
    setup: Setup
    naive: Arm
    verify: Verify
    batched: Arm | None = None
    batched_label: str = ""
    #: Verbs the batched oracle calls; an absent one makes it ``pending_tool``.
    requires: tuple[str, ...] = ()
    owner: str = "1.8-14"
    naive_note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# -- sketch_20_entities ------------------------------------------------------

SKETCH_STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    *(
        ("entity_create_line", {"x1": 10.0 * i, "y1": 0.0, "x2": 10.0 * i + 8.0, "y2": 6.0})
        for i in range(8)
    ),
    *(
        ("entity_create_circle", {"cx": 10.0 * i + 4.0, "cy": 20.0, "radius": 3.0})
        for i in range(6)
    ),
    *(
        (
            "entity_create_arc",
            {"cx": 10.0 * i, "cy": 40.0, "radius": 4.0, "start_angle": 0.0, "end_angle": 135.0},
        )
        for i in range(4)
    ),
    *(
        (
            "entity_create_polyline",
            {"points": [[100.0 * i, 60.0], [100.0 * i + 50.0, 60.0], [100.0 * i + 50.0, 90.0]]},
        )
        for i in range(2)
    ),
)


def _on_geometry(args: dict[str, Any]) -> dict[str, Any]:
    return {**args, "layer": "GEOMETRY"}


async def _setup_new(client: Any, _work: Path) -> dict[str, Any]:
    await _quiet(client, "drawing_new", {})
    return {}


async def _sketch_naive(rec: Recorder, _ctx: dict[str, Any]) -> Any:
    for tool, args in SKETCH_STEPS:
        await rec.call(tool, _on_geometry(args))


async def _sketch_batched(rec: Recorder, _ctx: dict[str, Any]) -> Any:
    steps = [{"tool": tool, "args": _on_geometry(args)} for tool, args in SKETCH_STEPS]
    answer = await rec.call("cad_batch", {"steps": steps})
    if not (isinstance(answer, dict) and answer.get("ok")):
        raise ArmError(f"cad_batch did not commit: {answer!r}"[:300])


async def _sketch_verify(client: Any, _ctx: dict[str, Any], _answer: Any) -> tuple[bool, str]:
    rows = await _all_entities(client)
    want = {"LINE": 8, "CIRCLE": 6, "ARC": 4, "LWPOLYLINE": 2}
    got: dict[str, int] = {}
    for row in rows:
        got[row["type"]] = got.get(row["type"], 0) + 1
    on_layer = all(row["layer"] == "GEOMETRY" for row in rows)
    return got == want and on_layer, f"types {got}, all on GEOMETRY: {on_layer}"


# -- layer_cleanup_300 -------------------------------------------------------


async def _setup_messy(client: Any, work: Path) -> dict[str, Any]:
    path = build_messy_layers(work / "messy_layers.dxf")
    await _quiet(client, "drawing_open", {"path": str(path)})
    return {"path": str(path)}


async def _cleanup_naive(rec: Recorder, _ctx: dict[str, Any]) -> Any:
    await rec.call("layer_list", {})
    rows: list[dict[str, Any]] = []
    while True:
        page = await rec.call("entity_list", {"limit": 100, "offset": len(rows)}) or []
        rows.extend(page)
        if len(page) < 100:
            break
    for row in rows:
        target = MESSY_LAYERS.get(row["layer"], row["layer"])
        await rec.call(
            "entity_set_properties",
            {"handle": row["handle"], "layer": target, "color": 256, "lineweight": -1},
        )
    for name in MESSY_LAYERS:
        await rec.call("layer_delete", {"name": name})


async def _cleanup_verify(client: Any, _ctx: dict[str, Any], _answer: Any) -> tuple[bool, str]:
    rows = await _all_entities(client)
    layers = {row["name"] for row in await _quiet(client, "layer_list", {})}
    misplaced = [row["handle"] for row in rows if row["layer"] not in MESSY_LAYERS.values()]
    overridden = [
        row["handle"]
        for row in rows
        if row.get("color") != 256 or row.get("lineweight_name") != "ByLayer"
    ]
    leftover = sorted(set(MESSY_LAYERS) & layers)
    ok = len(rows) == MESSY_ENTITIES and not misplaced and not overridden and not leftover
    return ok, (
        f"{len(rows)} entities, {len(misplaced)} off the ISO layers, "
        f"{len(overridden)} with an override, misnamed layers left: {leftover}"
    )


# -- dimension_plate ---------------------------------------------------------


async def _setup_plate(client: Any, work: Path) -> dict[str, Any]:
    path = build_plate(work / "plate.dxf")
    await _quiet(client, "drawing_open", {"path": str(path)})
    return {"path": str(path)}


async def _plate_naive(rec: Recorder, _ctx: dict[str, Any]) -> Any:
    listed = await rec.call("entity_list", {"layer_filter": "GEOMETRY"}) or []
    entities = [await rec.call("entity_get", {"handle": row["handle"]}) for row in listed]
    for entity in entities:
        props = entity["properties"]
        if entity["type"] == "LWPOLYLINE":
            points = props["points"]
            cx = sum(p[0] for p in points) / len(points)
            cy = sum(p[1] for p in points) / len(points)
            for i, (x1, y1) in enumerate(points):
                x2, y2 = points[(i + 1) % len(points)]
                if math.isclose(y1, y2):
                    side = -10.0 if y1 < cy else 10.0
                    args = {"dim_x": (x1 + x2) / 2, "dim_y": y1 + side, "rotation": 0.0}
                else:
                    side = -10.0 if x1 < cx else 10.0
                    args = {"dim_x": x1 + side, "dim_y": (y1 + y2) / 2, "rotation": 90.0}
                await rec.call(
                    "dimension_linear",
                    {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": "DIM", **args},
                )
        elif entity["type"] == "CIRCLE":
            (x, y), r = props["center"], props["radius"]
            await rec.call(
                "dimension_diameter",
                {"x1": x - r, "y1": y, "x2": x + r, "y2": y, "leader_length": 8.0, "layer": "DIM"},
            )
        elif entity["type"] == "ARC":
            (x, y), r = props["center"], props["radius"]
            middle = math.radians((props["start_angle"] + props["end_angle"]) / 2.0)
            await rec.call(
                "dimension_radius",
                {
                    "center_x": x,
                    "center_y": y,
                    "chord_x": x + r * math.cos(middle),
                    "chord_y": y + r * math.sin(middle),
                    "layer": "DIM",
                },
            )
    await rec.call("drawing_critique", {"focus": ["dim_overlap"]})


async def _plate_verify(client: Any, _ctx: dict[str, Any], _answer: Any) -> tuple[bool, str]:
    dims = await _all_entities(client, type_filter="DIMENSION")
    return len(dims) == PLATE_DIMENSIONS, f"{len(dims)} of {PLATE_DIMENSIONS} dimensions"


# -- inspect_foreign ---------------------------------------------------------


async def _setup_foreign(client: Any, work: Path) -> dict[str, Any]:
    path = build_foreign(work / "foreign.dxf")
    await _quiet(client, "drawing_open", {"path": str(path)})
    return {"path": str(path)}


async def _foreign_naive(rec: Recorder, _ctx: dict[str, Any]) -> Any:
    layers = await rec.call("layer_list", {}) or []
    rows: list[dict[str, Any]] = []
    while True:
        page = await rec.call("entity_list", {"limit": 100, "offset": len(rows)}) or []
        rows.extend(page)
        if len(page) < 100:
            break
    return {"entities": len(rows), "layers": sorted(row["name"] for row in layers)}


async def _foreign_batched(rec: Recorder, _ctx: dict[str, Any]) -> Any:
    report = await rec.call("drawing_understand", {}) or {}
    return {
        "entities": report.get("entities"),
        "layers": sorted(row["name"] for row in report.get("layers") or []),
    }


async def _foreign_verify(_client: Any, _ctx: dict[str, Any], answer: Any) -> tuple[bool, str]:
    answer = answer or {}
    found = set(answer.get("layers") or [])
    ok = answer.get("entities") == FOREIGN_ENTITIES and set(FOREIGN_LAYERS) <= found
    return (
        ok,
        f"{answer.get('entities')} entities read; fixture layers found: {set(FOREIGN_LAYERS) <= found}",
    )


# -- history_value_lookup ----------------------------------------------------


async def _setup_history(client: Any, work: Path) -> dict[str, Any]:
    """Three rounds on one hole; the agent saved a file after each round."""
    await _quiet(client, "drawing_new", {})
    hole = await _quiet(
        client,
        "entity_create_circle",
        {"cx": 50.0, "cy": 50.0, "radius": HISTORY_DIAMETERS[0] / 2, "layer": "GEOMETRY"},
    )
    paths = []
    for index, diameter in enumerate(HISTORY_DIAMETERS, start=1):
        if index > 1:
            await _quiet(
                client, "entity_edit_geometry", {"handle": hole["handle"], "radius": diameter / 2}
            )
        path = work / f"round{index}.dxf"
        await _quiet(client, "drawing_save_as", {"path": str(path)})
        paths.append(str(path))
    return {"rounds": paths, "handle": hole["handle"]}


async def _history_naive(rec: Recorder, ctx: dict[str, Any]) -> Any:
    await rec.call("drawing_open", {"path": ctx["rounds"][HISTORY_ROUND - 1]})
    circles = await rec.call("entity_list", {"type_filter": "CIRCLE"}) or []
    await rec.call("drawing_open", {"path": ctx["rounds"][-1]})
    return 2.0 * circles[0]["properties"]["radius"] if circles else None


async def _history_verify(client: Any, ctx: dict[str, Any], answer: Any) -> tuple[bool, str]:
    info = await _quiet(client, "drawing_info", {})
    back = str(info.get("full_path") or "")
    want = HISTORY_DIAMETERS[HISTORY_ROUND - 1]
    current = Path(back).name == Path(ctx["rounds"][-1]).name
    ok = answer is not None and math.isclose(float(answer), want) and current
    return (
        ok,
        f"round {HISTORY_ROUND} diameter {answer} (want {want}); latest round reopened: {current}",
    )


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "sketch_20_entities",
        "Draw a 20-entity sketch: 8 lines, 6 circles, 4 arcs, 2 polylines on GEOMETRY.",
        _setup_new,
        _sketch_naive,
        _sketch_verify,
        batched=_sketch_batched,
        batched_label="cad_batch: the 20 creates as one round trip",
        requires=("cad_batch",),
    ),
    Scenario(
        "layer_cleanup_300",
        "Clean the messy-layers drawing: every entity onto its ISO layer with colour and "
        "lineweight ByLayer, the six misnamed layers removed (the rival's H4).",
        _setup_messy,
        _cleanup_naive,
        _cleanup_verify,
        requires=("entity_modify_where", "layer_merge"),
        batched_label="layer_merge per misnamed layer + one entity_modify_where (1.8-12)",
        owner="1.8-12 / 1.8-13",
    ),
    Scenario(
        "dimension_plate",
        "Dimension the plate: four edges, six hole diameters, two arc radii, then a "
        "dim_overlap critique (the rival's H5).",
        _setup_plate,
        _plate_naive,
        _plate_verify,
        requires=("dimension_auto_v2",),
        batched_label="dimension_auto(selector={'layer': 'GEOMETRY'}) + drawing_critique (1.8-13)",
        owner="1.8-13",
        naive_note="reads every entity with entity_get, then one dimension per feature",
    ),
    Scenario(
        "inspect_foreign",
        "Describe a 300-entity drawing somebody else made: its layers and how much is on them.",
        _setup_foreign,
        _foreign_naive,
        _foreign_verify,
        batched=_foreign_batched,
        batched_label="drawing_understand: one read-only report",
        requires=("drawing_understand",),
        naive_note="layer_list, then entity_list paged 100 at a time",
    ),
    Scenario(
        "history_value_lookup",
        f"What was the hole's diameter in round {HISTORY_ROUND}? (the rival's H1)",
        _setup_history,
        _history_naive,
        _history_verify,
        requires=("drawing_history",),
        batched_label="drawing_history(limit=...) and its value (1.8-10)",
        owner="1.8-10",
        naive_note=(
            "assumes the agent saved one file per round and reopens round 2's; without "
            "those files 1.7.0 cannot answer at all (the rival measured 0/3)"
        ),
    ),
)


def _dimension_auto_v2(tools: dict[str, Any]) -> bool:
    """``dimension_auto`` v2 (1.8-13) takes a ``selector``; today's takes LINE handles only."""
    tool = tools.get("dimension_auto")
    schema = getattr(tool, "inputSchema", None) or {}
    return "selector" in (schema.get("properties") or {})


#: Verbs that are a new *shape* of an existing tool rather than a new name.
_VERB_PROBES: dict[str, Callable[[dict[str, Any]], bool]] = {
    "dimension_auto_v2": _dimension_auto_v2,
}


def scenario_by_id(scenario_id: str) -> Scenario:
    for scenario in SCENARIOS:
        if scenario.scenario_id == scenario_id:
            return scenario
    raise KeyError(scenario_id)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _missing(scenario: Scenario, registered: dict[str, Any]) -> list[str]:
    missing = []
    for verb in scenario.requires:
        probe = _VERB_PROBES.get(verb)
        present = probe(registered) if probe is not None else verb in registered
        if not present:
            missing.append(verb)
    return missing


def _arm_row(arm: str, label: str, rec: Recorder | None) -> dict[str, Any]:
    calls = rec.calls if rec is not None else []
    by_tool: dict[str, int] = {}
    for call in calls:
        by_tool[call.tool] = by_tool.get(call.tool, 0) + 1
    return {
        "arm": arm,
        "label": label,
        "calls": len(calls),
        "by_tool": dict(sorted(by_tool.items())),
        "request_chars": sum(len(call.request_text) for call in calls),
        "result_chars": sum(len(call.result_text) for call in calls),
        "sizes": [[len(call.request_text), len(call.result_text)] for call in calls],
    }


async def _run_arm(
    client: Any, scenario: Scenario, arm: str, func: Arm, label: str, work: Path
) -> tuple[dict[str, Any], Recorder]:
    ctx = await scenario.setup(client, work)
    rec = Recorder(client)
    row = _arm_row(arm, label, None)
    try:
        answer = await func(rec, ctx)
        verified, detail = await scenario.verify(client, ctx, answer)
    except Exception as exc:  # an arm that errors did not do the job
        verified, detail = False, f"{type(exc).__name__}: {exc}"[:500]
    row = _arm_row(arm, label, rec)
    row.update(
        {"status": "measured" if verified else "failed", "verified": verified, "detail": detail}
    )
    return row, rec


async def run_scenario(
    client: Any, scenario: Scenario, work: Path, registered: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Recorder]]:
    """Both arms of ``scenario``: the report row and each arm's recorder."""
    recorders: dict[str, Recorder] = {}
    naive_label = NAIVE_LABEL + (f"; {scenario.naive_note}" if scenario.naive_note else "")
    naive, recorders["naive"] = await _run_arm(
        client, scenario, "naive", scenario.naive, naive_label, work / "naive"
    )
    missing = _missing(scenario, registered)
    if scenario.batched is None or missing:
        batched = {
            **_arm_row("batched", scenario.batched_label, None),
            "status": "pending_tool",
            "verified": None,
            "missing_tools": missing,
            "owner": scenario.owner,
            "detail": (
                "the least-call oracle needs a verb this tree does not have yet"
                if missing
                else "the verbs exist; the least-call oracle is not written yet"
            )
            + f" (owner {scenario.owner}); never counted as a pass",
        }
    else:
        batched, recorders["batched"] = await _run_arm(
            client, scenario, "batched", scenario.batched, scenario.batched_label, work / "batched"
        )
    return (
        {
            "scenario": scenario.scenario_id,
            "question": scenario.question,
            "owner": scenario.owner,
            "arms": {"naive": naive, "batched": batched},
        },
        recorders,
    )


async def run_scenarios(
    client: Any, scenarios: tuple[Scenario, ...] = SCENARIOS
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Recorder]]]:
    registered = {tool.name: tool for tool in await client.list_tools()}
    rows: list[dict[str, Any]] = []
    traces: dict[str, dict[str, Recorder]] = {}
    with tempfile.TemporaryDirectory(prefix="acadmcp_call_budget_") as folder:
        for scenario in scenarios:
            work = Path(folder) / scenario.scenario_id
            for arm in ("naive", "batched"):
                (work / arm).mkdir(parents=True, exist_ok=True)
            row, recorders = await run_scenario(client, scenario, work, registered)
            rows.append(row)
            traces[scenario.scenario_id] = recorders
    return rows, traces


# ---------------------------------------------------------------------------
# The ceilings
# ---------------------------------------------------------------------------


def load_budget(path: Path = DEFAULT_BUDGET) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check_calls(
    rows: list[dict[str, Any]], budget: dict[str, Any], *, release: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Every scenario against its ``task`` cell: violations, over-ceiling and pending."""
    cells = budget.get("task", {})
    violations: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    seen = {row["scenario"] for row in rows}
    for name in cells:
        if name not in seen:
            violations.append({"cell": f"task.{name}", "reason": "not measured"})
    for row in rows:
        cell = cells.get(row["scenario"])
        for arm in row["arms"].values():
            if arm["status"] == "failed":
                violations.append(
                    {
                        "cell": f"task.{row['scenario']}.{arm['arm']}",
                        "reason": f"the arm did not do the job: {arm['detail']}",
                    }
                )
        if cell is None:
            continue
        batched = row["arms"]["batched"]
        entry = {
            "cell": f"task.{row['scenario']}",
            "ceiling": cell["ceiling"],
            "gate": cell["gate"],
            "owner": cell.get("owner"),
        }
        if batched["status"] == "pending_tool":
            item = {**entry, "reason": f"pending_tool: {batched['missing_tools']}"}
            if cell["gate"] == "ci" or release:
                violations.append(item)
            else:
                pending.append(item)
        elif batched["status"] == "measured" and batched["calls"] > cell["ceiling"]:
            item = {**entry, "measured": batched["calls"], "reason": "over the ceiling"}
            if cell["gate"] == "ci" or release:
                violations.append(item)
            else:
                pending.append(item)
    return {"violations": violations, "pending": pending}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def run(budget_path: Path, *, release: bool = False) -> dict[str, Any]:
    from fastmcp import Client

    previous = os.environ.get("AUTOCAD_MCP_BACKEND")
    os.environ["AUTOCAD_MCP_BACKEND"] = "ezdxf"
    try:
        import server

        async with Client(server.mcp) as client:
            status = (await client.call_tool("system_status", {})).structured_content or {}
            if status.get("backend") != "ezdxf":
                raise RuntimeError(
                    f"refusing to run against the {status.get('backend')!r} backend: "
                    "call_budget is headless-only (AUTOCAD_MCP_BACKEND=ezdxf)"
                )
            rows, _traces = await run_scenarios(client)
    finally:
        if previous is None:
            os.environ.pop("AUTOCAD_MCP_BACKEND", None)
        else:
            os.environ["AUTOCAD_MCP_BACKEND"] = previous
    budget = load_budget(budget_path)
    checked = check_calls(rows, budget, release=release)
    return {
        "schema_version": SCHEMA_VERSION,
        "budget": str(budget_path),
        "release": release,
        "scenarios": rows,
        **checked,
        "ok": not checked["violations"],
    }


def summarize(report: dict[str, Any]) -> str:
    lines = ["AutoCAD MCP Pro - call budget (headless, scripted)"]
    for row in report["scenarios"]:
        naive = row["arms"]["naive"]
        batched = row["arms"]["batched"]
        tail = (
            f"batched {batched['calls']} calls"
            if batched["status"] == "measured"
            else f"batched {batched['status']} {batched.get('missing_tools') or ''}"
        )
        lines.append(
            f"  {row['scenario']:22} naive {naive['calls']:4d} calls ({naive['status']})  {tail}"
        )
    for item in report["pending"]:
        lines.append(f"  PENDING {item['cell']}: {item['reason']} (owner {item.get('owner')})")
    for item in report["violations"]:
        lines.append(f"  FAIL    {item['cell']}: {item['reason']}")
    lines.append("  ok" if report["ok"] else "  FAILED")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Least-call oracles against token_budget.json.")
    parser.add_argument("--budget", type=Path, default=DEFAULT_BUDGET)
    parser.add_argument("--json", action="store_true", help="print the JSON report on stdout")
    parser.add_argument("--out", type=Path, default=None, help="also write the JSON report here")
    parser.add_argument(
        "--release", action="store_true", help="a pending release-gated cell fails too"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = asyncio.run(run(args.budget, release=args.release))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    print(rendered if args.json else summarize(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
