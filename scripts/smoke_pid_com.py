"""Exercise the P&ID track once against the live AutoCAD on this machine.

Runs on the live harness (scripts/live/harness.py): attach only, a NEW
document of its own (never the operator's drawing), closed on every path.
The whole track goes through the same code the tools call:

    apply_layer_set("pid") -> run_spec(EXAMPLE_SPEC)
        = block_define -> pid_symbol_insert -> ACADMCP_PID xdata
          -> pid_line_draw -> pid_graph -> six pid_* critique focuses
    -> entity_get_xdata on the pump

and the same spec once more on the headless engine as the reference. Checks
(backlog W4-T21-07: the exit code used to depend on ``dangling`` alone):

* every spec item was placed (one handle per id);
* every line landed on a port (``dangling == 0``) and no block went
  unclassified;
* every node was read from the catalogue or its XDATA (``confidence_min``
  at least 0.95 - no heuristic node on a drawing this server drew);
* the live graph has the nodes by kind and the edges by class of the
  headless reference;
* the pid_* critique focuses found nothing;
* the pump carries ACADMCP_PID XDATA;
* ``pid_project`` (1.8-21) over the live sheet as the active document plus
  two DXF sheets drawn headlessly into a temporary folder: the live OP-1
  (LINK L-17) matched to the first file's ``in`` connector, the second
  file's L-99 an orphan, both with sheet + handle, no cross-sheet duplicate;
* (1.8-20) below the sheet, an ``auto`` line runs through the vessel between
  its ports and a ``route="avoid"`` line between the same ports does not:
  its ``obstacle_hits`` is empty and ``pid_line_through_symbol`` names only
  the ``auto`` line.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_pid_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402

XDATA_APP = "ACADMCP_PID"

#: Node sources the reader reports with at least this confidence: the
#: catalogue (1.0) and the ACADMCP_PID payload (0.95), never a heuristic.
MIN_CONFIDENCE = 0.95


def _spec_ids() -> set[str]:
    from engineering.pid.spec import EXAMPLE_SPEC

    sections = ("equipment", "valves", "instruments", "connectors")
    return {item["id"] for section in sections for item in EXAMPLE_SPEC.get(section, [])}


async def reference_stats() -> dict:
    """The same spec on the headless engine: the graph the live one must equal."""
    from backends.ezdxf_backend import EzdxfBackend
    from engineering.layers import apply_layer_set
    from engineering.pid.spec import EXAMPLE_SPEC, run_spec

    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_new()
        await apply_layer_set(backend, "pid")
        result = await run_spec(backend, EXAMPLE_SPEC)
        return dict(result["graph"]["stats"])
    finally:
        await backend.disconnect()


def assess(h: LiveHarness, report: dict) -> None:
    stats = report["graph_stats"]
    reference = report.get("reference_stats") or {}
    wanted = _spec_ids()
    h.check(
        "every spec item placed",
        set(report["handles"]) == wanted,
        expected=sorted(wanted),
        observed=sorted(report["handles"]),
    )
    h.check("every line on a port", stats["dangling"] == 0, expected=0, observed=stats["dangling"])
    h.check(
        "no block unclassified",
        stats["unclassified"] == 0,
        expected=0,
        observed=stats["unclassified"],
    )
    h.check(
        "every node from the catalogue or its XDATA",
        float(stats["confidence_min"]) >= MIN_CONFIDENCE,
        expected=f">= {MIN_CONFIDENCE}",
        observed=stats["confidence_min"],
    )
    for key in ("nodes_by_kind", "edges_by_class"):
        h.check(
            f"{key} as on the headless engine",
            stats.get(key) == reference.get(key),
            expected=reference.get(key),
            observed=stats.get(key),
        )
    h.check(
        "the pid_* focuses found nothing",
        not report["critique"],
        expected=0,
        observed=len(report["critique"]),
    )
    h.check(
        "the pump carries ACADMCP_PID XDATA",
        report["xdata_chunks"] >= 1,
        expected=">= 1",
        observed=report["xdata_chunks"],
    )
    avoid = report["avoid"]
    h.check(
        "auto runs through the vessel between the ports (the fixture means something)",
        avoid["auto_hits"] == ["V-301"],
        expected=["V-301"],
        observed=avoid["auto_hits"],
    )
    h.check(
        "route='avoid' goes round it: no obstacle hit",
        avoid["avoid_hits"] == [],
        expected=[],
        observed=avoid["avoid_hits"],
    )
    h.check(
        "no pid_line_through_symbol on the avoid line",
        avoid["through"] == [avoid["auto_handle"]],
        expected=[avoid["auto_handle"]],
        observed=avoid["through"],
    )


async def avoid_line(backend) -> dict:
    """1.8-20 live: one ``auto`` and one ``avoid`` line past a vessel that
    stands between the ports, below the example sheet. The ``auto`` line is
    the control (it must hit the vessel); the ``avoid`` one must not, and the
    critique must name only the ``auto`` line."""
    from engineering.critique import run_critique
    from engineering.pid.drawlines import draw_line
    from engineering.pid.insert import place_symbol

    pump = await place_symbol(backend, "centrifugal_pump", 0, -220, tag="P-301")
    await place_symbol(backend, "vertical_vessel", 80, -200, tag="V-301")
    target = await place_symbol(backend, "vertical_vessel", 160, -200, tag="V-302")
    ends = (
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": target["handle"], "port": "N3"},
    )
    auto = await draw_line(backend, *ends)
    avoid = await draw_line(backend, *ends, route_mode="avoid")
    issues = await run_critique(backend, ["pid_line_through_symbol"])
    return {
        "auto_handle": auto["handle"],
        "auto_hits": [hit["tag"] for hit in auto["obstacle_hits"]],
        "avoid_hits": [hit["tag"] for hit in avoid["obstacle_hits"]],
        "avoid_vertices": avoid["vertices"],
        "through": sorted({i.handles[0] for i in issues}),
    }


#: The two DXF sheets of the project step (1.8-21): the first receives the
#: live sheet's OP-1 (LINK L-17, an ``out``), the second leaves L-99 unanswered.
_PROJECT_SHEETS = {
    "P-002": {
        "equipment": [{"id": "P-201", "symbol": "centrifugal_pump", "x": 100, "y": 100}],
        "connectors": [
            {"id": "IN17", "direction": "in", "x": 20, "y": 100, "tag": "FROM P&ID-001",
             "link": "L-17", "to": "P-201.suction"},
        ],
    },
    "P-003": {
        "equipment": [{"id": "P-301", "symbol": "centrifugal_pump", "x": 100, "y": 100}],
        "connectors": [
            {"id": "OUT99", "direction": "out", "x": 200, "y": 140, "tag": "TO P&ID-009",
             "link": "L-99", "from": "P-301.discharge"},
        ],
    },
}  # fmt: skip


async def project_files(folder: Path) -> dict[str, dict]:
    """Draw and save the two DXF sheets headlessly; ``{number: {path, handles}}``."""
    from backends.ezdxf_backend import EzdxfBackend
    from engineering.layers import apply_layer_set
    from engineering.pid.spec import run_spec

    out = {}
    for number, spec in _PROJECT_SHEETS.items():
        backend = EzdxfBackend()
        await backend.connect()
        try:
            await backend.drawing_new()
            await apply_layer_set(backend, "pid")
            handles = (await run_spec(backend, spec))["handles"]
            path = folder / f"{number}.dxf"
            await backend.drawing_save_as(str(path))
            out[number] = {"path": str(path), "handles": handles}
        finally:
            await backend.disconnect()
    return out


def assess_project(h: LiveHarness, report: dict) -> None:
    """pid_project over the live active document and two DXF files (1.8-21)."""
    project = report["project"]
    files = report["project_files"]
    sources = [(row["sheet"], row["source"]) for row in project["sheets"]]
    h.check(
        "pid_project read two files and the active document",
        sources == [("P-002", "file"), ("P-003", "file"), ("current", "active_document")],
        expected=[("P-002", "file"), ("P-003", "file"), ("current", "active_document")],
        observed=sources,
    )
    links = {
        row["link"]: (row["status"], sorted((c["sheet"], c["handle"]) for c in row["connectors"]))
        for row in project["links"]
    }
    wanted = {
        "L-17": (
            "matched",
            sorted(
                [
                    ("current", report["handles"]["OP-1"]),
                    ("P-002", files["P-002"]["handles"]["IN17"]),
                ]
            ),
        ),
        "L-99": ("orphan", [("P-003", files["P-003"]["handles"]["OUT99"])]),
    }
    h.check("off-page links across the live sheet and the files", links == wanted,
            expected=wanted, observed=links)  # fmt: skip
    h.check(
        "no cross-sheet duplicate tag",
        project["duplicate_tags"] == [],
        expected=[],
        observed=project["duplicate_tags"],
    )


async def body(h: LiveHarness) -> dict:
    import tempfile

    from engineering.layers import apply_layer_set
    from engineering.pid.project import run_project
    from engineering.pid.spec import EXAMPLE_SPEC, run_spec

    h.stage("reference")
    reference = await reference_stats()
    h.stage("sheet")
    await h.new_document()
    backend = h.backend
    await apply_layer_set(backend, "pid")
    result = await run_spec(backend, EXAMPLE_SPEC)
    got = await backend.entity_get_xdata(result["handles"]["P-101"], XDATA_APP)
    report = {
        "handles": result["handles"],
        "graph_stats": result["graph"]["stats"],
        "reference_stats": reference,
        "crossings_total": result["crossings_total"],
        "critique": result["critique"],
        "xdata_chunks": len(got["xdata"].get(XDATA_APP, [])),
    }
    h.stage("project")
    folder = Path(tempfile.mkdtemp(prefix="acadmcp_pid_project_"))
    report["project_files"] = await project_files(folder)
    report["project"] = await run_project(
        backend,
        "check",
        sheets=[report["project_files"][n]["path"] for n in _PROJECT_SHEETS],
        include_current=True,
    )
    h.stage("avoid")
    report["avoid"] = await avoid_line(backend)
    h.stage("checks")
    assess(h, report)
    assess_project(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("pid", body, description=__doc__.split("\n\n")[0]))
