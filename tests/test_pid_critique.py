"""Six P&ID focuses; all silent on a drawing with no P&ID content."""

from __future__ import annotations

import pytest

from engineering.pid.critique import has_pid_content, issues_for
from engineering.pid.drawlines import draw_line
from engineering.pid.graph import build_graph
from engineering.pid.insert import place_symbol
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.preflight import pid_plan_warnings, preflight_drawing

pytestmark = pytest.mark.asyncio

PID_FOCUSES = (
    "pid_dangling_line",
    "pid_duplicate_tag",
    "pid_incompatible_connection",
    "pid_untagged_instrument",
    "pid_illegal_tag",
    "pid_unconnected_equipment",
)


def test_focuses_are_in_the_closed_enum():
    for focus in PID_FOCUSES:
        assert focus in ALL_CRITIQUE_FOCUSES


async def _clean(backend):
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-101")
    cv = await place_symbol(backend, "globe", 160, 106, tag="FCV-101", actuator="diaphragm")
    fic = await place_symbol(
        backend, "instrument", 160, 140, tag="FIC-101", type="dcs", location="primary"
    )
    await draw_line(
        backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": cv["handle"], "port": "in"},
    )
    await draw_line(backend, {"handle": cv["handle"], "port": "out"}, {"x": 220, "y": 106})
    await draw_line(
        backend,
        {"handle": fic["handle"]},
        {"handle": cv["handle"], "port": "signal"},
        line_class="electric",
    )
    return pump, cv, fic


async def test_clean_pid_has_no_pid_issues_except_the_free_end(backend):
    await _clean(backend)
    issues = await backend.drawing_critique(list(PID_FOCUSES))
    focuses = [i.focus for i in issues]
    assert focuses == ["pid_dangling_line"], "the line to a free point is the only finding"
    assert issues[0].severity == "error" and "hint" in issues[0].detail


async def test_dangling_carries_the_nearest_port_hint(backend):
    pump, cv, _ = await _clean(backend)
    await backend.entity_move(cv["handle"], 0.0, 1.0)
    issues = await backend.drawing_critique(["pid_dangling_line"])
    hinted = [i for i in issues if i.detail.get("nearest")]
    assert hinted and hinted[0].detail["nearest"]["node"] == cv["handle"]
    assert "pid_line_draw" in hinted[0].detail["hint"]


async def test_duplicate_and_illegal_and_untagged(backend):
    await place_symbol(backend, "instrument", 0, 0, tag="FIC-101")
    await place_symbol(backend, "instrument", 30, 0, tag="FIC-101")
    await place_symbol(backend, "instrument", 60, 0, tag="FCI-102")
    await place_symbol(backend, "instrument", 90, 0)
    await place_symbol(backend, "gate", 120, 0, tag="FIC-101")
    issues = await backend.drawing_critique(
        ["pid_duplicate_tag", "pid_illegal_tag", "pid_untagged_instrument"]
    )
    by_focus = {}
    for issue in issues:
        by_focus.setdefault(issue.focus, []).append(issue)
    assert len(by_focus["pid_duplicate_tag"]) == 1
    assert len(by_focus["pid_duplicate_tag"][0].handles) == 3
    assert by_focus["pid_duplicate_tag"][0].severity == "error"
    assert len(by_focus["pid_illegal_tag"]) == 1
    assert by_focus["pid_illegal_tag"][0].severity == "warning"
    assert len(by_focus["pid_untagged_instrument"]) == 1


async def test_incompatible_connection(backend):
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-101")
    fic = await place_symbol(backend, "instrument", 100, 160, tag="FIC-1")
    await draw_line(
        backend,
        {"handle": fic["handle"]},
        {"handle": pump["handle"], "port": "discharge"},
        line_class="electric",
    )
    issues = await backend.drawing_critique(["pid_incompatible_connection"])
    assert len(issues) == 1 and issues[0].severity == "error"
    assert issues[0].detail == {
        "expected": "signal",
        "actual": "process",
        "port": "discharge",
        "line_class": "electric",
        "hint": issues[0].detail["hint"],
    }


async def test_unconnected_equipment_is_informational(backend):
    await place_symbol(backend, "vertical_vessel", 0, 0, tag="V-1")
    issues = await backend.drawing_critique(["pid_unconnected_equipment"])
    assert len(issues) == 1 and issues[0].severity == "info"


async def test_incompatible_is_silent_on_a_foreign_blocks_inferred_port(backend):
    """A CTO-library control valve has no declared ports: the graph infers one
    where the line lands and gives it a placeholder ``kind: "process"``. A signal
    line to its actuator is correct, and the old "redraw as a process line"
    error was a guess presented as a measurement."""
    await backend.drawing_apply_iso_layers("pid")
    await backend.block_define(
        "CONTROL_VALVE",
        [
            {"type": "polyline", "points": [[-6, -3], [6, 3], [6, -3], [-6, 3]], "closed": True},
            {"type": "line", "x1": 0, "y1": 0, "x2": 0, "y2": 6},
            {"type": "arc", "cx": 0, "cy": 6, "r": 3, "start_deg": 0, "end_deg": 180},
        ],
        attdefs=[{"tag": "TAG", "x": 0, "y": 12, "height": 2.5}],
    )
    cv = await backend.block_insert(
        "CONTROL_VALVE", 160, 100, attributes={"TAG": "FCV-101"}, layer="PROCESS-EQUIPMENT"
    )
    fic = await place_symbol(
        backend, "instrument", 160, 140, tag="FIC-101", type="dcs", location="primary"
    )
    await draw_line(backend, {"handle": fic["handle"]}, {"x": 160, "y": 109}, line_class="electric")
    graph = await build_graph(backend, include_foreign=True)
    valve = next(n for n in graph["nodes"] if n["id"] == cv.handle)
    assert valve["source"] == "heuristic" and valve["ports"]["p1"]["inferred"] is True
    issues = await backend.drawing_critique(None)
    assert [i.focus for i in issues if i.focus.startswith("pid_")] == []


async def test_a_dangling_line_off_a_foreign_valve_is_still_reported(backend):
    """The evidence rule must not silence the foreign path spec §9.3 pins: a
    heuristic node (keyword name + TAG attribute) is recognised, so a plain
    line leaving its box for nowhere is a real dangling end."""
    await backend.block_define(
        "GATE_VALVE",
        [
            {"type": "polyline", "points": [[-4, -2], [-4, 2], [0, 0]], "closed": True},
            {"type": "polyline", "points": [[4, -2], [4, 2], [0, 0]], "closed": True},
        ],
        attdefs=[{"tag": "TAG", "x": 0, "y": 4, "height": 2.5}],
    )
    await backend.block_insert("GATE_VALVE", 50, 50, attributes={"TAG": "HV-9"})
    line = await backend.entity_create_line(54, 50, 90, 50, layer="0")
    issues = await backend.drawing_critique(["pid_dangling_line"])
    assert [(i.severity, i.handles) for i in issues] == [("error", [line.handle])]


async def test_mechanical_drawing_is_untouched_by_pid_focuses(backend):
    from engineering.layers import ensure_engineering_layers

    await ensure_engineering_layers(backend)
    await backend.entity_create_circle(0, 0, 20, layer="GEOMETRY")
    await backend.entity_create_line(-30, 0, 30, 0, layer="CENTER")
    assert await backend.drawing_critique(list(PID_FOCUSES)) == []


async def _bolt_with_note_leader(backend):
    """A mechanical INSERT with a note leader from its edge — the shape that
    files the bolt as an ``unknown_block`` at 0.3 and the leader as a foreign
    edge with a free far end."""
    await backend.block_define(
        "BOLT_M10",
        [
            {"type": "circle", "cx": 0, "cy": 0, "r": 5},
            {"type": "polyline", "points": [[-5, -5], [5, -5], [5, 5], [-5, 5]], "closed": True},
        ],
    )
    bolt = await backend.block_insert("BOLT_M10", 100, 100, layer="GEOMETRY")
    leader = await backend.entity_create_line(105, 100, 140, 120, layer="GEOMETRY")
    await backend.entity_create_text("SEE NOTE 3", 141, 120, 3.5, layer="TEXT")
    return bolt.handle, leader.handle


async def test_a_mechanical_insert_with_a_note_leader_is_not_pid_content(backend):
    """Spec §11.1: a drawing without P&ID content is never scored. The graph
    still files the bolt as an ``unknown_block`` (that is the reader's job);
    the critique must not mistake that 0.3 guess for a P&ID symbol and fail
    the finalize gate on the leader's free end."""
    await backend.drawing_apply_iso_layers("mech")
    bolt, leader = await _bolt_with_note_leader(backend)
    graph = await build_graph(backend, include_foreign=True)
    assert [(n["id"], n["kind"], n["confidence"]) for n in graph["nodes"]] == [
        (bolt, "unknown_block", 0.3)
    ]
    assert [d["edge"] for d in graph["dangling"]] == [leader]
    assert has_pid_content(graph) is False
    for focus in PID_FOCUSES:
        assert issues_for(focus, graph) == [], focus
    issues = await backend.drawing_critique(None)
    assert [i.focus for i in issues if i.focus.startswith("pid_")] == []


async def test_finalize_gate_passes_a_mechanical_sheet_with_a_note_leader(backend, tmp_path):
    """The gate runs ``focus=None``, so the six focuses sit inside every
    ``drawing_finalize`` — the mechanical sheet must pass it as it did before
    they joined."""
    import server

    class _Ctx:
        def __init__(self, backend):
            self.lifespan_context = {"backend": backend}

        async def warning(self, message):
            pass

    await backend.drawing_apply_iso_layers("mech")
    await _bolt_with_note_leader(backend)
    payload = await server.drawing_finalize(save_path=str(tmp_path / "bolt.dxf"), ctx=_Ctx(backend))
    assert payload["critique"] == []


async def test_a_note_leader_off_an_unknown_block_on_a_pid_is_not_dangling(backend):
    """On a real P&ID the same leader is in the graph only because it touched
    a 0.3 box; the one genuine finding (the free process line) stays."""
    await _clean(backend)
    await _bolt_with_note_leader(backend)
    issues = await backend.drawing_critique(["pid_dangling_line"])
    assert len(issues) == 1 and issues[0].detail["x"] == pytest.approx(220.0)


async def _frame_and_pump(backend):
    """A sheet frame as a block reference (a CTO title block is one) and a
    catalogue pump: the shape that files the frame as an ``unknown_block`` at
    0.3 the moment a P&ID line stops on its border."""
    await backend.drawing_apply_iso_layers("pid")
    await backend.block_define(
        "FRAME_A3",
        [{"type": "polyline", "points": [[0, 0], [420, 0], [420, 297], [0, 297]], "closed": True}],
    )
    frame = await backend.block_insert("FRAME_A3", 0, 0, layer="TITLEBLOCK")
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-101")
    return frame.handle, pump


async def test_a_pid_line_ending_on_a_sheet_frame_insert_is_dangling(backend):
    """Spec §11.1: an edge end that resolves to nothing is dangling. The graph
    attaches the process line to the frame's box and invents port ``p1`` at
    the line's own end, so its ``dangling`` list is empty — but a 0.3 touch
    guess is nothing the drawing vouches for, and it can no more suppress a
    finding than create one. Landing on the frame and stopping 2 mm short of
    it must be the same error."""
    frame, pump = await _frame_and_pump(backend)
    line = await draw_line(
        backend, {"handle": pump["handle"], "port": "discharge"}, {"x": 420, "y": 106}
    )
    graph = await build_graph(backend, include_foreign=True)
    node = next(n for n in graph["nodes"] if n["id"] == frame)
    assert node["kind"] == "unknown_block" and node["ports"]["p1"]["inferred"] is True
    edge = next(e for e in graph["edges"] if e["id"] == line["handle"])
    assert edge["to"] == {"node": frame, "port": "p1"} and graph["dangling"] == []

    issues = await backend.drawing_critique(list(PID_FOCUSES))
    assert [(i.severity, i.focus, i.handles, i.detail["end"]) for i in issues] == [
        ("error", "pid_dangling_line", [line["handle"]], "to")
    ]
    assert issues[0].detail["x"] == pytest.approx(420.0)
    assert issues[0].detail["y"] == pytest.approx(106.0)
    assert issues[0].detail["touches"] == frame
    assert issues[0].detail["nearest"] is None and "hint" in issues[0].detail


async def test_stopping_short_of_the_frame_is_the_same_finding(backend):
    _frame, pump = await _frame_and_pump(backend)
    line = await draw_line(
        backend, {"handle": pump["handle"], "port": "discharge"}, {"x": 418, "y": 106}
    )
    issues = await backend.drawing_critique(["pid_dangling_line"])
    assert [(i.severity, i.handles, i.detail["end"]) for i in issues] == [
        ("error", [line["handle"]], "to")
    ]


async def test_finalize_gate_fails_a_pid_line_that_ends_on_the_frame(backend, tmp_path):
    """The gate runs ``focus=None``; a process line "connected" to the title
    block must not pass it."""
    import server

    class _Ctx:
        def __init__(self, backend):
            self.lifespan_context = {"backend": backend}

        async def warning(self, message):
            pass

    from fastmcp.exceptions import ToolError

    _frame, pump = await _frame_and_pump(backend)
    await draw_line(backend, {"handle": pump["handle"], "port": "discharge"}, {"x": 420, "y": 106})
    with pytest.raises(ToolError, match="pid_dangling_line"):
        await server.drawing_finalize(save_path=str(tmp_path / "pid.dxf"), ctx=_Ctx(backend))


async def test_unknown_block_end_hint_names_the_nearest_recognised_port(backend):
    """The hint a free end gets from the graph (nearest port within
    10·tolerance) is computed the same way for an end the graph hung on an
    ``unknown_block``: over ports the drawing vouches for, never the invented
    ``p1``."""
    frame, pump = await _frame_and_pump(backend)
    cv = await place_symbol(backend, "globe", 418, 106, tag="FCV-101")
    port = cv["ports"]["in"]
    await backend.entity_move(frame, port["x"] - 420, 0)
    out = pump["ports"]["discharge"]
    line = await backend.entity_create_line(
        out["x"], out["y"], port["x"], port["y"] + 1.0, layer="PROCESS-PIPING-MAIN"
    )
    graph = await build_graph(backend, include_foreign=True)
    edge = next(e for e in graph["edges"] if e["id"] == line.handle)
    assert edge["to"]["node"] == frame, "the end sits on the frame's border, 1 mm off the port"
    issues = await backend.drawing_critique(["pid_dangling_line"])
    assert [i.handles for i in issues] == [[line.handle]]
    nearest = issues[0].detail["nearest"]
    assert nearest["node"] == cv["handle"] and nearest["port"] == "in"
    assert nearest["distance"] == pytest.approx(1.0)
    assert f"'{cv['handle']}'" in issues[0].detail["hint"]


async def test_readme_gear_sheet_is_untouched_by_pid_focuses(backend):
    """The spec's own negative: all six return [] on the README gear sheet."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "render_readme_showcase.py"
    spec = importlib.util.spec_from_file_location("render_readme_showcase", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await module.build(backend)
    assert await backend.drawing_critique(list(PID_FOCUSES)) == []


async def test_refine_reports_pid_issues_as_manual_and_moves_nothing(backend):
    # The refine loop is `engineering.refiner.refine_drawing` (what the
    # `drawing_refine` tool calls) — the backend has no `drawing_refine` method.
    from engineering.refiner import refine_drawing

    await _clean(backend)
    before = await backend.entity_count()
    result = (
        await refine_drawing(
            backend, max_rounds=1, min_score=100.0, focus=list(PID_FOCUSES), dry_run=True
        )
    ).to_dict()
    actions = [a for r in result["rounds"] for a in r["actions"]]
    assert actions, "the free-end dangling line must reach the refiner as an action"
    assert all(a["status"] == "manual_required" for a in actions)
    assert await backend.entity_count() == before


def test_preflight_flags_a_pid_intent_on_the_wrong_layer_set():
    result = preflight_drawing(
        "P&ID for the feed section",
        {"units": "mm", "part_type": "pid", "dimensions": {}, "tolerance_policy": "none"},
        layer_set_id="mech",
    )
    codes = [c.code for c in result.conflicts]
    assert "LAYER_SET_INTENT_MISMATCH" in codes
    ok = preflight_drawing(
        "P&ID for the feed section",
        {"units": "mm", "part_type": "pid", "dimensions": {}, "tolerance_policy": "none"},
        layer_set_id="pid",
    )
    assert "LAYER_SET_INTENT_MISMATCH" not in [c.code for c in ok.conflicts]
    assert pid_plan_warnings("Bearing housing", "mech") == []
    assert pid_plan_warnings("piping and instrumentation diagram", "mech")
