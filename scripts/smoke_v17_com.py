# scripts/smoke_v17_com.py
"""Exercise the v1.7 live-engine fixes once against the AutoCAD on this machine.

Creates a NEW document (never touches the operator's open drawings), runs every
v1.7 COM path through the same backend methods the tools call, and closes the
scratch document without saving:

    busy seat      a second COM client keeps AutoCAD inside ``_.DELAY`` while
                   this one draws: without the retry filter some creates are
                   refused (and leave nothing behind); with it none are
    system_status  ``seat`` diagnosis (quiescent, active_commands, lock state)
    layout_create  no default viewport on the new tab (report #3)
    viewport_create looks at the requested model point (report #2): VIEWCTR
    viewport_list  no crash on the narrowed entity (report #8)
    system_set_variable LAYOUTCREATEVIEWPORT round trip (report #9)
    page_setup_apply full_bleed ANSI B 1:1, then batch_plot: /MediaBox 431.8 x
                   279.4 and the label plotted 1:1 (report #6 / #7)
    layer_create duplicate / exist_ok, layer_delete of the current layer
    true colour on an entity and a layer, a bold text style, an MTEXT anchored
                   middle-centre with line spacing
    drawing_export_dxf R12 (last: AutoCAD's SaveAs rebinds the document)

Prints one JSON report. Exit codes: 0 every check held, 1 a check failed
(listed in ``failures``), 2 no live AutoCAD reachable.

    uv run --frozen python scripts/smoke_v17_com.py [--out DIR] [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ANSI_B_LANDSCAPE_MM = (431.8, 279.4)
MEDIABOX_TOLERANCE_MM = 0.5


def _check(failures: list[str], label: str, ok: bool) -> None:
    if not ok:
        failures.append(label)


def _busy_seat(doc_name: str, seconds: float, ready: threading.Event, out: dict) -> None:
    """Keep AutoCAD inside ``_.DELAY`` from a second COM apartment."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.GetActiveObject("AutoCAD.Application")
        doc = next(
            app.Documents.Item(i)
            for i in range(app.Documents.Count)
            if str(app.Documents.Item(i).Name) == doc_name
        )
        ready.set()
        started = time.monotonic()
        doc.SendCommand(f"_.DELAY {int(seconds * 1000)}\n")
        out["delay_s"] = round(time.monotonic() - started, 2)
    except Exception as exc:  # reported, never raised out of the thread
        out["error"] = repr(exc)
        ready.set()
    finally:
        pythoncom.CoUninitialize()


async def _draw_while_busy(backend, doc_name: str, y: float, budget_ms: int | None) -> dict:
    """Ten lines while another client holds AutoCAD in DELAY; count what landed."""
    from backends import com_message_filter

    def _set_budget():
        state = com_message_filter._STATES.get(threading.get_ident())
        if state is None:
            return None
        previous = state.budget_ms
        if budget_ms is not None:
            state.budget_ms = budget_ms
        return previous

    previous = await backend._run(_set_budget)
    before = com_message_filter.stats()["retried_calls"]
    ready, out = threading.Event(), {}
    thread = threading.Thread(target=_busy_seat, args=(doc_name, 4.0, ready, out))
    thread.start()
    ready.wait(10)
    await asyncio.sleep(0.4)
    made, refused = [], []
    for i in range(10):
        try:
            made.append(
                (await backend.entity_create_line(i * 5, y, i * 5 + 3, y, layer="V17_BUSY")).handle
            )
        except Exception as exc:
            refused.append(str(exc)[:160])
    thread.join(30)
    if previous is not None:

        def _restore():
            com_message_filter._STATES[threading.get_ident()].budget_ms = previous

        await backend._run(_restore)
    on_row = [
        row
        for row in await backend.entity_list(layer_filter="V17_BUSY", limit=1000)
        if abs(row.properties.get("start", [0, 0])[1] - y) < 1e-6
    ]
    return {
        "made": len(made),
        "refused": len(refused),
        "first_refusal": refused[0] if refused else None,
        "entities_on_row": len(on_row),
        "retried_calls": com_message_filter.stats()["retried_calls"] - before,
        "busy_thread": out,
    }


async def main(args: argparse.Namespace) -> int:
    os.environ.setdefault("AUTOCAD_MCP_BACKEND", "com")
    out_dir = Path(args.out or tempfile.mkdtemp(prefix="acadmcp-v17-")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ALLOWED_PATHS"] = str(out_dir)

    from backends.com_backend import ComBackend
    from engineering.standards.papers import resolve_page_setup
    from engineering.standards.plot import batch_plot

    backend = ComBackend()
    await backend.connect()
    status = await backend.system_status()
    if not status.get("connected"):
        print(json.dumps({"reachable": False, "status": status}, indent=1, default=str))
        return 2

    report: dict = {"autocad_version": status.get("autocad_version"), "out_dir": str(out_dir)}
    failures: list[str] = []
    scratch = None
    try:
        await backend.drawing_new()
        scratch = (await backend.system_status()).get("active_document")
        report["scratch_document"] = scratch

        status = await backend.system_status()
        report["seat"] = status.get("seat")
        _check(failures, "system_status carries a seat diagnosis", "seat" in status)

        await backend.layer_create("V17_BUSY", color=3)
        report["busy_without_retry"] = await _draw_while_busy(backend, scratch, 100, 0)
        report["busy_with_retry"] = await _draw_while_busy(backend, scratch, 200, None)
        without, with_retry = report["busy_without_retry"], report["busy_with_retry"]
        _check(
            failures,
            "a refused create leaves nothing behind (entities == made)",
            without["entities_on_row"] == without["made"],
        )
        _check(failures, "with the retry filter no create is refused", with_retry["refused"] == 0)
        _check(
            failures,
            "with the retry filter all ten lines exist",
            with_retry["entities_on_row"] == 10,
        )

        # ── layer rules ──────────────────────────────────────────────────
        try:
            await backend.layer_create("V17_BUSY")
            _check(failures, "duplicate layer refused", False)
        except ValueError:
            pass
        layer = await backend.layer_create("V17_BUSY", color=5, exist_ok=True)
        _check(failures, "exist_ok updates the layer", layer.color == 5)
        await backend.layer_create("V17_CUR")
        await backend.layer_set_current("V17_CUR")
        try:
            await backend.layer_delete("V17_CUR")
            _check(failures, "current layer delete refused", False)
        except ValueError:
            pass
        await backend.layer_set_current("0")

        # ── a 30 x 52 label at model (100, 100) ───────────────────────────
        await backend.layer_create("CUT", color=1)
        label = await backend.entity_create_polyline(
            [[100, 100], [130, 100], [130, 152], [100, 152]], closed=True, layer="CUT"
        )
        tc = await backend.entity_set_true_color(label.handle, "#25282A")
        _check(failures, "entity true colour", tc["true_color"] == "#25282A")
        ltc = await backend.layer_set_true_color("CUT", [255, 0, 128])
        _check(failures, "layer true colour", ltc["true_color"] == "#FF0080")
        style = await backend.textstyle_create("V17_BOLD", "arial.ttf", bold=True)
        _check(failures, "bold text style", style["bold"] is True)
        text = await backend.entity_create_mtext(
            "AIRFLEX +\\PTEMP TECH",
            115,
            126,
            width=28,
            height=3,
            attachment="middle_center",
            line_spacing=0.9,
        )
        report["mtext"] = text.handle

        # ── sysvar (report #9) ───────────────────────────────────────────
        original = await backend.system_get_variable("LAYOUTCREATEVIEWPORT")
        flipped = 0 if int(original) else 1
        await backend.system_set_variable("LAYOUTCREATEVIEWPORT", flipped)
        now = await backend.system_get_variable("LAYOUTCREATEVIEWPORT")
        await backend.system_set_variable("LAYOUTCREATEVIEWPORT", int(original))
        report["layoutcreateviewport"] = {"original": original, "flipped_to": now}
        _check(failures, "LAYOUTCREATEVIEWPORT set through SetVariable", int(now) == flipped)

        # ── sheet: layout, page setup, viewport, plot ────────────────────
        created = await backend.layout_create("V17_SHEET")
        report["layout_create"] = created
        vps = (await backend.viewport_list("V17_SHEET"))["viewports"]
        report["viewports_after_layout_create"] = len(vps)
        _check(failures, "new layout carries only its paper-space viewport", len(vps) <= 1)

        setup = await backend.page_setup_apply(
            "V17_SHEET",
            resolve_page_setup("ANSI_B", "landscape", "acad.ctb", "1:1", full_bleed=True),
        )
        row = (await backend.page_setup_list("V17_SHEET"))[0]
        report["page_setup"] = {
            "media": row["canonical_media_name"],
            "margins_mm": row["margins_mm"],
            "paper_units": row["paper_units"],
            "warnings": setup.get("warnings"),
        }
        _check(
            failures, "full-bleed media applied", "full_bleed" in str(row["canonical_media_name"])
        )
        _check(
            failures,
            "full-bleed margins are zero",
            row["margins_mm"] is not None and max(row["margins_mm"]) < 0.01,
        )
        _check(failures, "paper units stay mm", row["paper_units"] == "mm")

        vp = await backend.viewport_create("V17_SHEET", 215.9, 139.7, 400, 260, 115, 126, 1.0)
        report["viewport_create"] = vp
        centre = vp.get("view_center") or [None, None]
        _check(
            failures,
            "viewport looks at the requested model point",
            centre[0] is not None and abs(centre[0] - 115) < 0.01 and abs(centre[1] - 126) < 0.01,
        )
        rows = (await backend.viewport_list("V17_SHEET"))["viewports"]
        report["viewport_list"] = rows
        await backend.viewport_lock(vp["handle"], True)

        plotted = await batch_plot(backend, ["V17_SHEET"], str(out_dir), "{drawing}-{layout}.pdf")
        sheet = plotted["sheets"][0] if plotted.get("sheets") else {}
        report["batch_plot"] = sheet
        box = sheet.get("mediabox_mm") or [0, 0]
        _check(
            failures,
            "PDF MediaBox is ANSI B landscape",
            abs(box[0] - ANSI_B_LANDSCAPE_MM[0]) < MEDIABOX_TOLERANCE_MM
            and abs(box[1] - ANSI_B_LANDSCAPE_MM[1]) < MEDIABOX_TOLERANCE_MM,
        )

        # ── DXF release (last: SaveAs rebinds the scratch document) ──────
        target = out_dir / "v17_label_R12.dxf"
        exported = await backend.drawing_export_dxf(str(target), "R12")
        report["export_r12"] = exported
        _check(failures, "R12 DXF written", exported.get("acadver") == "AC1009")
        scratch = (await backend.system_status()).get("active_document")
    except Exception as exc:
        failures.append(f"unexpected error: {exc!r}")
    finally:
        if scratch and not args.keep:
            try:
                await backend.document_close(scratch, discard=True)
                report["scratch_closed"] = scratch
            except Exception as exc:
                report["scratch_close_error"] = repr(exc)
        await backend.disconnect()

    report["failures"] = failures
    print(json.dumps(report, indent=1, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", help="folder for the PDF and DXF (default: a temp folder)")
    parser.add_argument("--keep", action="store_true", help="leave the scratch document open")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
