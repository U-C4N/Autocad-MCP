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
* the pump carries ACADMCP_PID XDATA.

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


async def body(h: LiveHarness) -> dict:
    from engineering.layers import apply_layer_set
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
    h.stage("checks")
    assess(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("pid", body, description=__doc__.split("\n\n")[0]))
