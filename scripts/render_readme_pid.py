"""Render the P&ID sheet the README shows, using only the server's own tools.

An A3 feed-and-reaction P&ID: feed tank TK-101, feed pump P-101, shell-and-tube
exchanger E-101, reactor R-101 and product pump P-102, with a level, a flow
and a temperature loop (ISA-5.1 bubbles, globe control valves with diaphragm
actuators and fail positions, an orifice flow element), pressure gauges on both
pump discharges, cooling water to and from off-page connectors and every
process line numbered size-service-sequence-spec. The whole sheet is one
``pid_from_spec`` document (``build_spec`` below, ``sheet.routing = "avoid"``:
every line without its own route is routed round the symbols), placed and
routed through the same function the MCP tool calls, then framed (ISO 5457
``sheet_frame``), titled (ISO 7200 ``titleblock_apply``), given revision A
(``revision_add``), read back by ``pid_graph`` and gated the way
``drawing_finalize`` gates it: the structural validator plus
``drawing_critique(focus=None)`` - the seven P&ID focuses included - over one
read of the saved drawing. Nothing on the sheet is drawn by hand.

The signal lines into a valve actuator or the orifice tap are the one place
the spec names waypoints: a catalogue tag stands centred just above the
signal port, so a line dropped straight onto the port runs through the tag.
The waypoints bring the line in level, just under the tag, and are computed
from the catalogue's own port positions (``signal_entry``), never typed. The
cooling-water supply onto E-101's shell_in takes waypoints for the same
reason (shell_in stands under the E-101 tag), computed from TCV-103's ``out``
and E-101's ``shell_in`` ports. Gauges and transmitters stand beside what they
read: PI-104 / PI-105 just above their pump discharge lines, LT-101 against
the TK-101 shell (its x from the vessel's width), TT-103 at the reactor inlet.

Reproduce:

    AUTOCAD_MCP_BACKEND=ezdxf uv run --frozen python scripts/render_readme_pid.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "pid-showcase.png"
#: The saved drawing the finalize gate validates (gitignored ``out/``).
DEFAULT_SAVE = ROOT / "out" / "readme-pid" / "pid-showcase.dxf"
#: The whole sheet at this many pixels on its long side; the PNG is checked
#: against ``MAX_PNG_BYTES`` and re-rendered at ``FALLBACK_PX`` when larger.
RENDER_PX = 3200
FALLBACK_PX = 2600
MAX_PNG_BYTES = 700 * 1024

DRAWING_NO = "AMC-PID-1800-001"
TITLE = "FEED AND REACTION P&ID"
DATE = "2026-11-05"

#: Symbol scale on the sheet: the catalogue is authored at 1 (an 8 mm pump,
#: 2.5 mm tags), which reads as specks on an A3 sheet rendered whole.
EQ = 1.5  # equipment, valves, inline items: 3.75 mm tags
INST = 1.5  # instrument bubbles: r = 7.5 mm, 3 mm letters
OPC = 1.3  # off-page connectors
#: How far above a signal port the level run of its line stands: under the
#: tag, whose baseline is 3.5 catalogue mm above the port (``tag_attdefs``).
SIGNAL_DROP = 2.0
#: P-101's discharge turns this soon (stub) so the header runs under its tag.
PUMP_STUB = 1.2
#: The stub ``pid_from_spec`` gives a connector's own line (``draw_line``'s
#: default): P-102's discharge rises this far before it turns to the connector.
CONNECTOR_STUB = 5.0

#: Insertion points (WCS mm). The process header runs at HEADER_Y.
HEADER_Y = 124.0
POS = {
    "OPC-FEED": (38.0, 232.0),
    "LCV-101": (85.0, 232.0),
    "LIC-101": (55.0, 262.0),
    "TK-101": (120.0, 165.0),
    "LT-101": (None, 190.0),  # x against the TK-101 shell (TANK_GAP)
    "P-101": (160.0, None),  # y from the header: discharge + PUMP_STUB = HEADER_Y
    "PI-104": (181.0, 138.0),  # just above 50-P-1003-CS1, by P-101's discharge
    "FE-102": (203.0, HEADER_Y),
    "FT-102": (216.0, 165.0),
    "FIC-102": (205.0, 262.0),
    "FCV-102": (245.0, HEADER_Y),
    "OPC-CWS": (239.0, 226.0),
    "TCV-103": (292.0, 226.0),
    "TIC-103": (304.0, 262.0),
    "E-101": (300.0, HEADER_Y),
    "TT-103": (322.0, 216.0),  # at the top of the reactor inlet riser 50-P-1005-CS1
    "R-101": (344.0, 160.0),
    "OPC-CWR": (322.0, 82.0),
    "P-102": (347.0, 92.0),
    "OPC-PROD": (397.0, None),  # y from P-102's discharge + CONNECTOR_STUB
    "PI-105": (374.0, None),  # just above 50-P-1007-CS1 (y from that line)
}
#: Gap between the TK-101 shell and LT-101's bubble, and between a line and a
#: pressure gauge's bubble above its label.
TANK_GAP = 1.5
GAUGE_RISE = 14.0
#: The cooling-water supply leaves TCV-103 level for this long, comes down
#: right of E-101's tag (shell_in stands under the tag), then runs level just
#: under the tag onto shell_in.
CW_RUN = 13.0

NOTES = (
    "NOTES",
    "1. LINE NUMBER: SIZE (DN) - SERVICE - SEQUENCE - PIPING SPEC; P = PROCESS, CW = COOLING WATER.",
    "2. INSTRUMENT SIGNALS ELECTRIC (ISA-5.1). FC = FAIL CLOSED, FO = FAIL OPEN.",
    "3. TK-101 FEED TANK, P-101 FEED PUMP, E-101 FEED COOLER, R-101 REACTOR, P-102 PRODUCT PUMP.",
    "4. OFF-PAGE CONNECTORS CONTINUE ON THE P&ID NAMED IN THEIR LINK.",
)


TANK_PARAMS = {
    "width": 26,
    "height": 56,
    "nozzles": [
        {"name": "N1", "side": "top", "fraction": 0.2},
        {"name": "N2", "side": "right", "fraction": 0.12},
    ],
}


def _port(symbol: str, port: str, at: tuple[float, float], scale: float, **options) -> dict:
    """A catalogue port in WCS for a symbol inserted at ``at`` (no rotation)."""
    from engineering.pid.symbols import resolve, transform_port

    spec = resolve(symbol, **options)
    found = next(p for p in spec.ports if p.name == port)
    return transform_port(found, at[0], at[1], 0.0, scale)


def signal_entry(source: tuple[float, float], port: dict) -> list[list[float]]:
    """Waypoints from an instrument bubble at ``source`` to a signal ``port``:
    straight down from the bubble to just above the port, level to it, then
    the last drop onto it - which keeps the run clear of the tag above."""
    y = port["y"] + SIGNAL_DROP
    return [[source[0], y], [port["x"], y]]


def build_spec() -> dict:
    pos = dict(POS)
    discharge = _port("centrifugal_pump", "discharge", (0.0, 0.0), EQ)["y"]
    pos["P-101"] = (pos["P-101"][0], HEADER_Y - discharge - PUMP_STUB)
    pos["OPC-PROD"] = (pos["OPC-PROD"][0], pos["P-102"][1] + discharge + CONNECTOR_STUB)
    # gauges just above the line they read; LT-101 against the tank shell
    pos["PI-104"] = (pos["PI-104"][0], HEADER_Y + GAUGE_RISE)
    pos["PI-105"] = (pos["PI-105"][0], pos["OPC-PROD"][1] + GAUGE_RISE)
    tank_x1 = TANK_PARAMS["width"] / 2.0 * EQ  # the shell (the bbox holds N2's stub)
    bubble_r = _port("instrument", "signal", (0.0, 0.0), INST, type="discrete", location="field")[
        "radius"
    ]
    pos["LT-101"] = (pos["TK-101"][0] + tank_x1 + TANK_GAP + bubble_r, pos["LT-101"][1])

    valve = {"symbol": "globe", "actuator": "diaphragm", "scale": EQ}
    tcv_out = _port("globe", "out", pos["TCV-103"], EQ, actuator="diaphragm")
    shell_in = _port("shell_tube_hx", "shell_in", pos["E-101"], EQ)
    cw_x = tcv_out["x"] + CW_RUN
    cw_route = [
        [cw_x, tcv_out["y"]],
        [cw_x, shell_in["y"] + SIGNAL_DROP],
        [shell_in["x"], shell_in["y"] + SIGNAL_DROP],
    ]
    lcv_sig = _port("globe", "signal", pos["LCV-101"], EQ, actuator="diaphragm")
    fcv_sig = _port("globe", "signal", pos["FCV-102"], EQ, actuator="diaphragm")
    tcv_sig = _port("globe", "signal", pos["TCV-103"], EQ, actuator="diaphragm")
    fe_sig = _port("orifice_plate", "signal", pos["FE-102"], EQ)

    def at(item_id: str) -> dict:
        x, y = pos[item_id]
        return {"x": x, "y": y}

    def inst(tag: str, kind: str, location: str) -> dict:
        return {"id": tag, "tag": tag, "type": kind, "location": location, "scale": INST, **at(tag)}

    return {
        "sheet": {
            "layer_set": "pid",
            "sheet_size": "A3",
            "scale": 1.0,
            "intent": "Feed and reaction section P&ID",
            "routing": "avoid",
        },
        "equipment": [
            {
                "id": "TK-101",
                "symbol": "vertical_vessel",
                "scale": EQ,
                "tag": "TK-101",
                "desc": "FEED TANK",
                "params": TANK_PARAMS,
                **at("TK-101"),
            },
            {
                "id": "P-101",
                "symbol": "centrifugal_pump",
                "scale": EQ,
                "tag": "P-101",
                **at("P-101"),
            },
            {
                "id": "FE-102",
                "symbol": "orifice_plate",
                "scale": EQ,
                "tag": "FE-102",
                **at("FE-102"),
            },
            {"id": "E-101", "symbol": "shell_tube_hx", "scale": EQ, "tag": "E-101", **at("E-101")},
            {
                "id": "R-101",
                "symbol": "reactor",
                "scale": EQ,
                "tag": "R-101",
                "desc": "REACTOR",
                "params": {
                    "width": 24,
                    "height": 40,
                    "nozzles": [
                        {"name": "N1", "side": "top", "fraction": 0.2},
                        {"name": "N2", "side": "bottom", "fraction": 0.2},
                    ],
                },
                **at("R-101"),
            },
            {
                "id": "P-102",
                "symbol": "centrifugal_pump",
                "scale": EQ,
                "tag": "P-102",
                **at("P-102"),
            },
        ],
        "valves": [
            {"id": "LCV-101", "tag": "LCV-101", "fail": "FC", **valve, **at("LCV-101")},
            {"id": "FCV-102", "tag": "FCV-102", "fail": "FC", **valve, **at("FCV-102")},
            {"id": "TCV-103", "tag": "TCV-103", "fail": "FO", **valve, **at("TCV-103")},
        ],
        "instruments": [
            inst("LT-101", "discrete", "field"),
            inst("LIC-101", "dcs", "primary"),
            inst("FT-102", "discrete", "field"),
            inst("FIC-102", "dcs", "primary"),
            inst("TT-103", "discrete", "field"),
            inst("TIC-103", "dcs", "primary"),
            inst("PI-104", "discrete", "field"),
            inst("PI-105", "discrete", "field"),
        ],
        "connectors": [
            {
                "id": "OPC-FEED",
                "direction": "in",
                "tag": "FEED",
                "link": "P&ID-000",
                "scale": OPC,
                "to": "LCV-101.in",
                "class": "process_major",
                "number": "80-P-1001-CS1",
                **at("OPC-FEED"),
            },
            {
                "id": "OPC-PROD",
                "direction": "out",
                "tag": "TO 002",
                "link": "P&ID-002",
                "scale": OPC,
                "from": "P-102.discharge",
                "class": "process_major",
                "number": "50-P-1007-CS1",
                **at("OPC-PROD"),
            },
            {
                "id": "OPC-CWS",
                "direction": "in",
                "tag": "CWS",
                "link": "P&ID-005",
                "scale": OPC,
                "to": "TCV-103.in",
                "class": "utility",
                "number": "50-CW-2001-CS1",
                **at("OPC-CWS"),
            },
            {
                "id": "OPC-CWR",
                "direction": "out",
                "tag": "CWR",
                "link": "P&ID-005",
                "scale": OPC,
                "from": "E-101.shell_out",
                "class": "utility",
                "number": "50-CW-2002-CS1",
                **at("OPC-CWR"),
            },
        ],
        "lines": [
            {
                "from": "LCV-101.out",
                "to": "TK-101.N1",
                "class": "process_major",
                "number": "80-P-1001-CS1",
                "label": False,
            },
            {
                "from": "TK-101.N2",
                "to": "P-101.suction",
                "class": "process_major",
                "number": "80-P-1002-CS1",
            },
            {
                "from": "P-101.discharge",
                "to": "FE-102.in",
                "class": "process_major",
                "number": "50-P-1003-CS1",
                "stub": PUMP_STUB,
            },
            {
                "from": "FE-102.out",
                "to": "FCV-102.in",
                "class": "process_major",
                "number": "50-P-1003-CS1",
                "label": False,
            },
            {
                "from": "FCV-102.out",
                "to": "E-101.tube_in",
                "class": "process_major",
                "number": "50-P-1004-CS1",
            },
            {
                "from": "E-101.tube_out",
                "to": "R-101.N1",
                "class": "process_major",
                "number": "50-P-1005-CS1",
            },
            {
                "from": "R-101.N2",
                "to": "P-102.suction",
                "class": "process_major",
                "number": "50-P-1006-CS1",
            },
            {
                "from": "TCV-103.out",
                "to": "E-101.shell_in",
                "class": "utility",
                "number": "50-CW-2001-CS1",
                "label": False,
                "route": cw_route,
            },
            # the three control loops: ISA-5.1 electric signal lines
            # LT-101 stands against the shell: up first, then level to LIC-101
            {
                "from": "LT-101",
                "to": "LIC-101",
                "class": "electric",
                "route": [[pos["LT-101"][0], pos["LIC-101"][1]]],
            },
            {
                "from": "LIC-101",
                "to": "LCV-101.signal",
                "class": "electric",
                "route": signal_entry(pos["LIC-101"], lcv_sig),
            },
            {
                "from": "FT-102",
                "to": "FE-102.signal",
                "class": "electric",
                "route": signal_entry(pos["FT-102"], fe_sig),
            },
            {"from": "FT-102", "to": "FIC-102", "class": "electric"},
            {
                "from": "FIC-102",
                "to": "FCV-102.signal",
                "class": "electric",
                "route": [
                    [pos["FIC-102"][0] + 22.0, pos["FIC-102"][1]],
                    *[[pos["FIC-102"][0] + 22.0, fcv_sig["y"] + SIGNAL_DROP]],
                    [fcv_sig["x"], fcv_sig["y"] + SIGNAL_DROP],
                ],
            },
            {
                "from": "TT-103",
                "to": "TIC-103",
                "class": "electric",
                "route": [[pos["TT-103"][0], pos["TIC-103"][1]]],
            },
            {
                "from": "TIC-103",
                "to": "TCV-103.signal",
                "class": "electric",
                "route": signal_entry(pos["TIC-103"], tcv_sig),
            },
        ],
    }


async def build(backend) -> dict:
    """Draw the sheet; return what was measured on it.

    The return dict keeps ``spec`` (pid_from_spec's result) and ``graph``
    (pid_graph read back from the drawing)."""
    from engineering.pid.graph import build_graph
    from engineering.pid.spec import run_spec
    from engineering.sheet.frames import draw_sheet_frame
    from engineering.sheet.revision import add_revision
    from engineering.sheet.titleblock import TitleBlockMetadata, apply_titleblock

    spec = build_spec()
    await backend.drawing_new()
    await backend.drawing_plan(spec["sheet"]["intent"], "A3", 1.0, "pid")
    await backend.drawing_apply_iso_layers("pid")

    result = await run_spec(backend, spec)

    await draw_sheet_frame(backend, "A3", orientation="landscape", zones=True, marks=True)
    await apply_titleblock(
        backend,
        size="A3",
        metadata=TitleBlockMetadata(
            title=TITLE,
            drawing_no=DRAWING_NO,
            part_no="UNIT 100",
            material="SEE LINE LIST",
            scale="NTS",
            drawn_by="autocad-mcp-pro",
            checked_by="drawing_critique",
            date=DATE,
            revision="A",
            company="autocad-mcp-pro",
        ),
        projection=None,
        frame=False,
    )
    await add_revision(
        backend, rev="A", description="ISSUED FOR REVIEW", date=DATE, by="autocad-mcp-pro"
    )
    y = 58.0
    for i, line in enumerate(NOTES):
        await backend.entity_create_text(line, 28.0, y, 3.5 if i == 0 else 2.5, layer="TEXT")
        y -= 6.0 if i == 0 else 4.5

    graph = await build_graph(backend)
    return {"spec": result, "graph": graph}


async def finalize(backend, save_path: Path) -> dict:
    """``drawing_finalize``'s gate, as server.py runs it: save, then the
    validator and the full critique over one shared read, combined into the
    0-100 score (``engineering.scoring.combine``)."""
    from engineering import DrawingValidator
    from engineering.scoring import combine

    save_path.parent.mkdir(parents=True, exist_ok=True)
    await backend.drawing_save_as(str(save_path))
    shared: dict = {}
    validation = await DrawingValidator().run(backend, expected={}, shared=shared)
    issues = await backend.drawing_critique(focus=None, shared=shared)
    summary = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        summary[issue.severity] = summary.get(issue.severity, 0) + 1
    return {
        "issues": issues,
        "validator": [f.code for f in validation.findings],
        "score": combine(validation.summary, summary),
    }


async def render(output: Path, save_path: Path) -> dict:
    os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")
    from backends.ezdxf_backend import EzdxfBackend

    backend = EzdxfBackend()
    await backend.connect()
    try:
        built = await build(backend)
        gate = await finalize(backend, save_path)
        info = await backend.drawing_info()
        px = RENDER_PX
        shot = await backend.view_render(max_px=px)
        png = shot.get("png")
        if png and len(png) > MAX_PNG_BYTES:
            px = FALLBACK_PX
            shot = await backend.view_render(max_px=px)
            png = shot.get("png")
    finally:
        await backend.disconnect()
    if not png:
        raise RuntimeError(f"view_render returned no PNG: {shot.get('error')}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    for issue in gate["issues"]:
        print(f"critique: {issue.focus}: {issue.message}", file=sys.stderr)
    from PIL import Image

    with Image.open(output) as image:
        width, height = image.size
    spec = built["spec"]
    graph = built["graph"]
    stats = graph["stats"]
    return {
        "output": str(output),
        "bytes": len(png),
        "size_px": f"{width}x{height}",
        "entities": info.entity_count,
        "critique_issues": len(gate["issues"]),
        "pid_focus_issues": len(spec["critique"]),
        "validator_findings": gate["validator"],
        "score": gate["score"],
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "dangling": stats.get("dangling"),
        "confidence_min": stats.get("confidence_min"),
        "stats": stats,
        "crossings_total": spec["crossings_total"],
        "obstacle_hits": spec["obstacle_hits"],
        "label_hits": spec["label_hits"],
        "saved": str(save_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--save", type=Path, default=DEFAULT_SAVE)
    args = parser.parse_args()
    report = asyncio.run(render(args.output, args.save))
    print(report)
    return 0 if report["critique_issues"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
