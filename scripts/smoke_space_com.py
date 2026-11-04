"""Measure the live engine's target space, raw command and undo (1.8-05) against AutoCAD.

Runs on the live harness (scripts/live/harness.py): attach with
``GetActiveObject`` only, NEW documents of its own (never the operator's),
closed with ``Close(False)`` on every path. Through the real ``ComBackend``:

1. **Layout1 active, a fresh document.** The tab is switched to Layout1 behind
   the session's back (what a click does), then ``entity_create_circle``,
   ``entity_scale``, ``entity_trim`` and ``entity_fillet``: no paper layout's
   ``Block.Count`` moves, every new entity is owned by ModelSpace, and Layout1
   is still the active tab afterwards.
2. **A raw command.** ``system_run_command("_CIRCLE ...")`` with the
   operator still on Layout1 is reported in Layout1 with
   ``created_outside_target_space``; on the Model tab it completes and reports
   the circle in Model, and the seat is idle (CMDACTIVE 0) afterwards. No
   command left at its prompt is sent: on this seat it blocks every COM call
   until a key is pressed in AutoCAD (``PROMPT_LIMITATION``, measured).
3. **A DWG saved with Layout1 active.** Saved, closed, reopened through
   ``drawing_open``: AutoCAD opens it on Layout1, the next create lands in
   model space all the same.
4. **A non-world UCS.** ``ucs_set`` rotates the UCS 90 degrees; a trim with a
   WCS pick point (``*x,y``) cuts the same side it cuts in WCS.
5. **Undo, verified.** ``drawing_undo`` after a create answers ``verified:
   true`` with the entity total down by one; ``drawing_redo`` brings it back.
6. **S5** (docs/analysis/spikes-1.8.0.json): which of the three candidate
   mechanisms of the rival's paper-space circle the 1.7.0 rule
   (``ActiveLayout.Block``) would have reproduced on this seat - recorded in
   the report's ``s5`` block, not a pass/fail check.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_space_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402


def _active(app):
    return app.ActiveDocument


def _counts(app) -> dict[str, int]:
    """``{layout name: Block.Count}`` for the active document (COM thread)."""
    doc = _active(app)
    return {
        str(doc.Layouts.Item(i).Name): int(doc.Layouts.Item(i).Block.Count)
        for i in range(int(doc.Layouts.Count))
    }


def _paper(counts: dict[str, int]) -> dict[str, int]:
    return {name: n for name, n in counts.items() if name != "Model"}


def _owned_by_model(handles: list[str]):
    def _sync(app):
        doc = _active(app)
        model = doc.ModelSpace.ObjectID
        return {h: doc.HandleToObject(h).OwnerID == model for h in handles}

    return _sync


def _set_tab(name: str):
    def _sync(app):
        doc = _active(app)
        doc.ActiveLayout = doc.Layouts.Item(name)
        return str(doc.ActiveLayout.Name)

    return _sync


def _tab(app) -> str:
    return str(_active(app).ActiveLayout.Name)


def _would_17_draw_on(app) -> str:
    """Where 1.7.0's ``_msp()`` (``ActiveLayout.Block``) would have drawn."""
    doc = _active(app)
    return str(doc.ActiveLayout.Name)


async def _layout1_active(h: LiveHarness) -> dict:
    backend = h.backend
    await h.new_document()
    tab = await h.raw(_set_tab("Layout1"))
    rule_17 = await h.raw(_would_17_draw_on)
    before = await h.raw(_counts)
    circle = await backend.entity_create_circle(0.0, 0.0, 5.0)
    await backend.entity_scale(circle.handle, 0.0, 0.0, 2.0)
    a = await backend.entity_create_line(0.0, 0.0, 100.0, 0.0)
    b = await backend.entity_create_line(50.0, -50.0, 50.0, 50.0)
    c = await backend.entity_create_line(0.0, 20.0, 0.0, 80.0)
    d = await backend.entity_create_line(-20.0, 100.0, 40.0, 100.0)
    await backend.entity_trim(a.handle, b.handle, 10.0, 0.0)
    fillet = await backend.entity_fillet(c.handle, d.handle, 5.0)
    after = await h.raw(_counts)
    handles = [circle.handle, a.handle, b.handle, c.handle, d.handle, fillet.handle]
    owners = await h.raw(_owned_by_model(handles))
    trimmed = await backend.entity_get(a.handle)
    h.check("the operator's tab was Layout1", tab == "Layout1", observed=tab)
    h.check(
        "no paper layout's Block.Count moved",
        _paper(after) == _paper(before),
        expected=_paper(before),
        observed=_paper(after),
    )
    h.check("every new entity is owned by ModelSpace", all(owners.values()), observed=owners)
    h.check(
        "the trim cut the model-space line at the cutter (half of 100 is left)",
        abs(float(trimmed.properties.get("length") or 0.0) - 50.0) < 1e-6,
        expected=50.0,
        observed=trimmed.properties.get("length"),
    )
    h.check("the fillet drew an ARC", fillet.type == "ARC", observed=fillet.type)
    tab_after = await h.raw(_tab)
    h.check("Layout1 is still the operator's tab", tab_after == "Layout1", observed=tab_after)
    return {"before": before, "after": after, "s5_operator_tab_rule_17": rule_17}


#: Measured on AutoCAD 2026 (2026-11-04, scratch documents): why this smoke
#: sends no raw command that stops at a prompt. It would leave the seat
#: refusing every call until a person presses ESC in AutoCAD, so the run could
#: neither go on nor close its own document.
PROMPT_LIMITATION = {
    "send_command": "SendCommand('_SCALE\\n') does not return until the prompt is answered",
    "post_command": "PostCommand returns, then every COM call (CMDACTIVE, LASTPROMPT, "
    "SendCommand, Close) is RPC_E_CALL_REJECTED until a key is pressed in AutoCAD",
    "esc_in_string": "SendCommand with ESC characters raises 'Invalid input' and runs nothing",
    "consequence": "a raw command left at its prompt overruns COM_CALL_TIMEOUT; "
    "command_incomplete is not reachable through ActiveX on this seat",
}


async def _raw_command(h: LiveHarness) -> dict:
    """A completed raw command, reported honestly in the space it drew in.

    The operator's tab is still Layout1 from the first step and a raw command
    runs in the active space (1.8-05 reports it, it does not move it), so the
    first circle is reported in Layout1 with ``created_outside_target_space``;
    with the Model tab active the same command is reported in Model, clean.
    """
    backend = h.backend
    tab = await h.raw(_tab)
    on_paper = await backend.system_run_command("_CIRCLE 200,0 3\n")
    h.check(
        "a raw command on the operator's Layout1 is reported there, with the warning",
        tab == "Layout1"
        and on_paper.get("completed") is True
        and on_paper.get("created_count") == 1
        and on_paper["created"][0]["space"] == "Layout1"
        and on_paper.get("warning") == "created_outside_target_space",
        observed={"tab": tab, "result": on_paper},
    )
    await h.raw(_set_tab("Model"))
    done = await backend.system_run_command("_CIRCLE 200,0 3\n")
    h.check(
        "a completed raw command on the Model tab reports its circle in Model",
        done.get("completed") is True
        and done.get("created_count") == 1
        and done["created"][0]["space"] == "Model"
        and "warning" not in done,
        observed=done,
    )
    cmdactive = await h.raw(lambda app: int(_active(app).GetVariable("CMDACTIVE")))
    h.check("the seat is idle after it", cmdactive == 0, expected=0, observed=cmdactive)
    return {"on_paper": on_paper, "completed": done, "prompt_limitation": PROMPT_LIMITATION}


async def _saved_on_layout1(h: LiveHarness, workspace: Path) -> dict:
    backend = h.backend
    name = await h.new_document()
    await h.raw(_set_tab("Layout1"))
    path = workspace / "saved_on_layout1.dwg"
    await backend.drawing_save_as(str(path), "dwg")
    await h.close_document(await h.name_of(name))
    await h.open_document(str(path))
    opened_on = await h.raw(_tab)
    before = await h.raw(_counts)
    await backend.entity_create_circle(0.0, 0.0, 5.0)
    after = await h.raw(_counts)
    h.check(
        "a DWG saved on Layout1 reopens there and still draws in model space",
        after["Model"] == before["Model"] + 1 and _paper(after) == _paper(before),
        observed={"opened_on": opened_on, "before": before, "after": after},
    )
    return {"opened_on": opened_on, "s5_reopened_layout1_rule_17": opened_on}


async def _trimmed_x_range(backend, y: float) -> list[float]:
    """Trim a 0..100 line at x=50 with the pick at x=10; the x range left."""
    a = await backend.entity_create_line(0.0, y, 100.0, y)
    b = await backend.entity_create_line(50.0, y - 20.0, 50.0, y + 20.0)
    await backend.entity_trim(a.handle, b.handle, 10.0, y)
    props = (await backend.entity_get(a.handle)).properties
    xs = [float(props["start"][0]), float(props["end"][0])]
    return [round(min(xs), 6), round(max(xs), 6)]


async def _ucs_trim(h: LiveHarness) -> dict:
    backend = h.backend
    await h.new_document()
    in_wcs = await _trimmed_x_range(backend, 0.0)
    await backend.ucs_set("acadmcp_rotated", [10.0, 10.0, 0.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0])
    try:
        in_ucs = await _trimmed_x_range(backend, 200.0)
    finally:
        await backend.ucs_restore("world")
    h.check(
        "a trim under a rotated UCS cuts exactly what the same trim cuts in WCS",
        in_ucs == in_wcs,
        expected=in_wcs,
        observed=in_ucs,
    )
    return {"wcs": in_wcs, "rotated_ucs": in_ucs}


async def _undo(h: LiveHarness) -> dict:
    backend = h.backend
    await backend.entity_create_circle(500.0, 0.0, 1.0)
    undo = await backend.drawing_undo()
    h.check(
        "drawing_undo after a create is verified with one entity fewer",
        undo.get("verified") is True
        and undo["evidence"]["entities_after"] == undo["evidence"]["entities_before"] - 1,
        observed=undo,
    )
    redo = await backend.drawing_redo()
    h.check(
        "drawing_redo brings it back, verified",
        redo.get("verified") is True
        and redo["evidence"]["entities_after"] == redo["evidence"]["entities_before"] + 1,
        observed=redo,
    )
    return {"undo": undo, "redo": redo}


def assess(h: LiveHarness, report: dict) -> None:
    """S5 is evidence for docs/analysis/spikes-1.8.0.json, not a check."""
    first = report.get("layout1_active") or {}
    saved = report.get("saved_on_layout1") or {}
    raw = report.get("raw_command") or {}
    report["s5"] = {
        "question": "which mechanism reproduces the paper-space circle on 1.7.0",
        "saved_layout1_then_create": {
            "rule_1_7_draws_on": saved.get("s5_reopened_layout1_rule_17"),
            "reproduces_on_1_7": saved.get("s5_reopened_layout1_rule_17") not in (None, "Model"),
        },
        "operator_tab_layout1_then_create": {
            "rule_1_7_draws_on": first.get("s5_operator_tab_rule_17"),
            "reproduces_on_1_7": first.get("s5_operator_tab_rule_17") not in (None, "Model"),
        },
        "scale_left_at_prompt": {
            "measured": raw.get("prompt_limitation"),
            "note": "not sent by this smoke: on this seat a command left at its prompt "
            "blocks every COM call until a key is pressed in AutoCAD",
        },
    }


async def body(h: LiveHarness) -> dict:
    workspace = Path(tempfile.mkdtemp(prefix="acadmcp_space_smoke_"))
    report: dict = {}
    try:
        h.stage("Layout1 active")
        report["layout1_active"] = await _layout1_active(h)
        h.stage("raw command")
        report["raw_command"] = await _raw_command(h)
        h.stage("DWG saved on Layout1")
        report["saved_on_layout1"] = await _saved_on_layout1(h, workspace)
        h.stage("non-world UCS trim")
        report["ucs_trim"] = await _ucs_trim(h)
        h.stage("verified undo")
        report["undo"] = await _undo(h)
        assess(h, report)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("space", body, description=__doc__.split("\n\n")[0]))
