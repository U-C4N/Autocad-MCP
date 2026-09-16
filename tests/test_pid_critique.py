"""Six P&ID focuses; all silent on a drawing with no P&ID content."""

from __future__ import annotations

import pytest

from engineering.pid.drawlines import draw_line
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


async def test_mechanical_drawing_is_untouched_by_pid_focuses(backend):
    from engineering.layers import ensure_engineering_layers

    await ensure_engineering_layers(backend)
    await backend.entity_create_circle(0, 0, 20, layer="GEOMETRY")
    await backend.entity_create_line(-30, 0, 30, 0, layer="CENTER")
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
