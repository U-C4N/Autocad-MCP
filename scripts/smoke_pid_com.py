"""Exercise the P&ID track once against the live AutoCAD on this machine.

Creates a NEW document (never touches the operator's open drawing), then runs
the whole track through the same code the tools call:

    apply_layer_set("pid") -> run_spec(EXAMPLE_SPEC)
        = block_define -> pid_symbol_insert -> ACADMCP_PID xdata
          -> pid_line_draw -> pid_graph -> six pid_* critique focuses
    -> entity_get_xdata on the pump

and prints the graph stats as JSON. Exit codes:

    0  every line landed on a port (``dangling == 0``)
    1  the sheet drew but the reader found a dangling end
    2  no live CAD application reachable (pywin32 missing, or the ProgID
       cannot be created) -- the COM paths then remain fake-tested only

The backend's ``connect()`` is lazy on purpose (the server must start before
AutoCAD is open), so reachability is read from ``system_status()`` rather than
from ``connect()``. Note that the backend's ``Dispatch`` fallback *launches*
the ProgID when nothing is running; a cold AutoCAD start can take longer than
``COM_CALL_TIMEOUT`` (60 s), so open AutoCAD first when the first call times
out.

    AUTOCAD_MCP_BACKEND=com uv run --frozen python scripts/smoke_pid_com.py
    uv run --frozen python scripts/smoke_pid_com.py --keep   # leave the document open
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

XDATA_APP = "ACADMCP_PID"


async def main(keep_document: bool = False) -> int:
    try:
        from backends.com_backend import ComBackend
    except ImportError as exc:
        print(f"pywin32 not available: {exc}")
        return 2
    backend = ComBackend()
    try:
        await backend.connect()
    except Exception as exc:
        print(f"no live AutoCAD reachable: {exc}")
        return 2
    try:
        status = await backend.system_status()
        if not status.get("connected"):
            print(f"no live AutoCAD reachable: {status.get('error', status)}")
            return 2

        from engineering.layers import apply_layer_set
        from engineering.pid.spec import EXAMPLE_SPEC, run_spec

        await backend.drawing_new()
        await apply_layer_set(backend, "pid")
        result = await run_spec(backend, EXAMPLE_SPEC)
        got = await backend.entity_get_xdata(result["handles"]["P-101"], XDATA_APP)
        report = {
            "autocad": status.get("autocad_version") or status.get("cad_progid"),
            "active_document": (await backend.system_status()).get("active_document"),
            "handles": result["handles"],
            "graph_stats": result["graph"]["stats"],
            "crossings_total": result["crossings_total"],
            "critique_issues": len(result["critique"]),
            "critique": result["critique"],
            "xdata_chunks": len(got["xdata"].get(XDATA_APP, [])),
        }
        print(json.dumps(report, indent=2))
        if not keep_document:
            await backend.drawing_close(save=False)
        return 0 if result["graph"]["stats"]["dangling"] == 0 else 1
    finally:
        await backend.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(keep_document="--keep" in sys.argv[1:])))
