"""Exercise track H once against the live AutoCAD on this machine.

Runs on the live harness (scripts/live/harness.py). Builds the synthetic plant
pair (tests/fixtures/plant_pair.py) in a temporary folder and reads it through
the live engine - never an operator's drawing:

    open layout.dxf (kept open: the session stays bound to one of the run's own)
    open pid.dxf    -> take_snapshot(backend)            (Document.Export, "DXF")
                    -> drawing_save_as(pid.dwg), close it
                    -> take_snapshot(backend, pid.dwg)   (opened read-only, exported, closed)
    layout.dxf      -> take_snapshot(backend)
    pipe_rows / cable_rows on the two live snapshots, against the pair's truth
    entity_move one tag text by 10 mm -> take_snapshot -> diff_snapshots
                    against the snapshot taken before the move

Checks: every live snapshot holds the same model-space entities (by type) as
the file it came from; the P&ID is called schematic and the takeoffs match the
truth; the diff reports exactly the one moved entity.

**The operator's drawings are never touched.** The harness attaches with
``GetActiveObject`` only, works in documents it opened from the files it just
wrote, refuses any call while AutoCAD's active document is not one of its own,
closes its documents with ``Close(False)`` on every path (unless ``--keep``),
and nothing it prints names a document other than its own. Unlike the 1.6
smoke it does not re-activate the document that was active when it started:
activating the operator's drawing is not the run's to do.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_understand_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402


def _model(snapshot):
    """The snapshot with its model-space records only (AutoCAD adds a paper-space
    viewport when it initialises a layout; the takeoffs read model space)."""
    return dataclasses.replace(
        snapshot, records=tuple(r for r in snapshot.records if r.space == "Model")
    )


def _types(snapshot) -> dict[str, int]:
    return dict(sorted(Counter(r.type for r in _model(snapshot).records).items()))


def _check_takeoffs(truth, pid, layout) -> dict:
    from engineering.understand.network import build_network
    from engineering.understand.scale import scale_check
    from engineering.understand.takeoff import cable_rows, pipe_rows

    scale = scale_check(pid, layout)
    services = tuple(sorted({run["service"] for run in truth["pipe_runs"]}))
    pipe = pipe_rows(build_network(pid), pid, layout, scale=scale, services=services)
    found = {(r["service"], tuple(sorted(r["tags"]))): r for r in pipe["runs"]}
    pipe_mismatch = []
    for run in truth["pipe_runs"]:
        got = found.get((run["service"], tuple(run["tags"])))
        want = run["layout_length_mm"]
        have = None if got is None else got.get("layout_m")
        agrees = got is not None and got["status"] == run["status"]
        if want is None:
            agrees = agrees and have is None
        else:
            agrees = agrees and have is not None and abs(float(have) * 1000.0 - want) <= 1e-6
        if not agrees:
            pipe_mismatch.append({"run": run["id"], "want": want, "have": have})
    rows = {row["tag"]: row for row in cable_rows(pid, layout)["rows"]}
    cable_mismatch = [
        want["tag"]
        for want in truth["cable_rows"]
        if rows.get(want["tag"], {}).get("cable_m") != want["roundup_m"]
        or rows.get(want["tag"], {}).get("panel") != want["panel"]
    ]
    return {
        "scale_verdict": scale["verdict"],
        "within_10pct": scale.get("within_10pct"),
        "runs_checked": len(truth["pipe_runs"]),
        "pipe_mismatch": pipe_mismatch,
        "loads_checked": len(truth["cable_rows"]),
        "cable_mismatch": cable_mismatch,
    }


def assess(h: LiveHarness, report: dict) -> None:
    spaces = report["model_space"]
    h.check(
        "the live P&ID snapshot holds the file's model space",
        spaces["pid_live"] == spaces["pid_file"],
        expected=spaces["pid_file"],
        observed=spaces["pid_live"],
    )
    h.check(
        "the P&ID read from its DWG holds the file's model space",
        spaces["pid_dwg"] == spaces["pid_file"],
        expected=spaces["pid_file"],
        observed=spaces["pid_dwg"],
    )
    h.check(
        "the live layout snapshot holds the file's model space",
        spaces["layout_live"] == spaces["layout_file"],
        expected=spaces["layout_file"],
        observed=spaces["layout_live"],
    )
    takeoffs = report["takeoffs"]
    h.check(
        "the P&ID is schematic",
        takeoffs["scale_verdict"] == "schematic",
        expected="schematic",
        observed=takeoffs["scale_verdict"],
    )
    h.check(
        "every pipe run as the truth",
        takeoffs["runs_checked"] > 0 and not takeoffs["pipe_mismatch"],
        expected=[],
        observed=takeoffs["pipe_mismatch"],
    )
    h.check(
        "every cable row as the truth",
        takeoffs["loads_checked"] > 0 and not takeoffs["cable_mismatch"],
        expected=[],
        observed=takeoffs["cable_mismatch"],
    )
    diff = report["diff"]
    h.check(
        "the diff reports exactly the moved entity",
        diff["changed"] == 1
        and diff["added"] == diff["removed"] == 0
        and diff["moved_is_the_change"],
        expected={"changed": 1, "added": 0, "removed": 0},
        observed=diff,
    )


async def body(h: LiveHarness) -> dict:
    from engineering.understand.diff import diff_snapshots
    from engineering.understand.snapshot import read_snapshot, take_snapshot
    from tests.fixtures.plant_pair import build_plant_pair

    backend = h.backend
    folder = Path(tempfile.mkdtemp(prefix="acadmcp_understand_smoke_"))
    truth = build_plant_pair(folder)

    h.stage("open")
    layout_name = await h.open_document(truth["layout"])
    pid_name = await h.open_document(truth["pid"])

    h.stage("P&ID snapshots")
    started = time.perf_counter()
    live_pid = await take_snapshot(backend)
    export_s = time.perf_counter() - started
    dwg = folder / "pid.dwg"
    await backend.drawing_save_as(str(dwg), "dwg")
    # SaveAs renamed the document to pid.dwg; the harness follows it and binds
    # the session back to the layout once it is closed.
    await h.close_document(pid_name)
    dwg_pid = await take_snapshot(backend, str(dwg))

    h.stage("layout snapshot and takeoffs")
    live_layout = await take_snapshot(backend)
    takeoffs = _check_takeoffs(truth, _model(live_pid), _model(live_layout))

    h.stage("diff")
    tag = next(
        r
        for r in live_layout.records
        if r.type == "TEXT" and r.layer == "TAGS" and r.space == "Model"
    )
    await backend.entity_move(tag.handle, 10.0, 0.0)
    after = await take_snapshot(backend)
    diff = diff_snapshots(_model(live_layout), _model(after))
    await h.close_document(layout_name)

    file_pid, file_layout = read_snapshot(truth["pid"]), read_snapshot(truth["layout"])
    report = {
        "export_s": round(export_s, 3),
        "live_pid_source": live_pid.source,
        "dwg_pid_source": dwg_pid.source,
        "model_space": {
            "pid_file": _types(file_pid),
            "pid_live": _types(live_pid),
            "pid_dwg": _types(dwg_pid),
            "layout_file": _types(file_layout),
            "layout_live": _types(live_layout),
        },
        "paper_space_layouts": sorted({r.space for r in live_layout.records} - {"Model"}),
        "takeoffs": takeoffs,
        "diff": {
            "moved": tag.handle,
            "added": len(diff["added"]),
            "removed": len(diff["removed"]),
            "changed": len(diff["changed"]),
            "moved_is_the_change": f'"{tag.handle}"' in json.dumps(diff["changed"]),
        },
    }
    h.stage("checks")
    assess(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("understand", body, description=__doc__.split("\n\n")[0]))
