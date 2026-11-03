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
* the six pid_* critique focuses found nothing;
* the pump carries ACADMCP_PID XDATA;
* ``pid_project`` (1.8-21) over the live sheet as the active document plus
  two DXF sheets drawn headlessly into a temporary folder: the live OP-1
  (LINK L-17) matched to the first file's ``in`` connector, the second
  file's L-99 an orphan, both with sheet + handle, no cross-sheet duplicate.

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
        "the six pid_* focuses found nothing",
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
    h.stage("checks")
    assess(h, report)
    assess_project(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("pid", body, description=__doc__.split("\n\n")[0]))
