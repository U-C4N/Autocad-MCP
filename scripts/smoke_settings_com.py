# scripts/smoke_settings_com.py
"""Exercise the settings track once against the live AutoCAD on this machine.

Creates a NEW document (never touches the operator's open drawing) and runs
every SECTION 18-20 path that needs a live seat, through the same backend
methods the tools call:

    system_launch (attach)                  -> {attached, launched, version}
    dimstyle_create ISO-25 / textstyle_create ISOCP
        / mleaderstyle_create iso -> listed with arrow_size 2.5
    page_setup_apply ISO_A3 -> batch_plot -> the PDF's own /MediaBox (420 x 297)
    plot_style_list (installed .ctb files)
    drawing_new(template=iso_a3_mech .dwt) -> mech layers, ISO-25 current, ISO_A3
    document_list / document_activate
    layer_state_save -> change -> layer_state_restore -> layer_state_delete
    view_named_save / view_named_list / view_named_restore
    ucs_set / ucs_list / ucs_restore("world") + a non-orthogonal refusal
    preferences_get + ONE reversible preferences_set (Display.CursorSize)
        + a read-only key refused
    drawing_properties_set / get (SummaryInfo + a custom property)
    system_prompt_message
    --interactive: user_pick_point (needs the operator at the keyboard)
    --build-dwt:   open each templates/<name>.dxf and save the .dwt twin
                   through drawing_template_save, before anything else

Prints one JSON report. Exit codes:

    0  every assertion held (``failures`` is empty)
    1  the seat answered but at least one assertion failed (listed)
    2  no live CAD application reachable (pywin32 missing, or the ProgID
       cannot be created) -- the COM paths then remain fake-tested only

Reachability is read from ``system_status()`` because ``connect()`` is lazy.
The ``Dispatch`` fallback *launches* the ProgID when nothing is running and a
cold start can exceed ``COM_CALL_TIMEOUT`` (60 s): open AutoCAD first.

    AUTOCAD_MCP_BACKEND=com uv run --frozen python scripts/smoke_settings_com.py
    uv run --frozen python scripts/smoke_settings_com.py --build-dwt
    uv run --frozen python scripts/smoke_settings_com.py --interactive --keep
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The five bundled templates (spec §6). Pinned here rather than read from
#: the catalogue so the smoke also notices a catalogue that lost a row.
TEMPLATE_NAMES = ("iso_a3_mech", "iso_a1_arch", "iso_a3_pid", "ansi_b_mech", "ansi_d_arch")

#: Every DWG/DWT since R13 opens with "AC10xx" (AC1032 for the 2018 format).
DWG_MAGIC = b"AC10"

#: What the ISO A3 landscape sheet must read back as, from the PDF itself.
A3_LANDSCAPE_MM = (420.0, 297.0)
MEDIABOX_TOLERANCE_MM = 0.5


def _check(failures: list[str], label: str, ok: bool) -> None:
    if not ok:
        failures.append(label)


#: Names of the documents this run opened and has not closed yet. A run that
#: dies half-way closes them in ``main``'s ``finally`` (unless ``--keep``), so
#: a failed smoke never leaves its scratch documents on the operator's seat.
_OPENED: list[str] = []


async def _opened_now(backend) -> str:
    """Record the document a ``drawing_new`` / ``drawing_open`` just made active."""
    name = (await backend.system_status()).get("active_document")
    if name and name not in _OPENED:
        _OPENED.append(name)
    return name


async def _close_ours(backend, name: str) -> None:
    await backend.document_close(name, discard=True)
    if name in _OPENED:
        _OPENED.remove(name)


async def _close_leftovers(backend) -> None:
    open_names = {row["name"] for row in await backend.document_list()}
    for name in list(_OPENED):
        if name in open_names:
            try:
                await backend.document_close(name, discard=True)
            except Exception as exc:  # best effort on the way out
                print(f"could not close {name}: {exc}", file=sys.stderr)
        _OPENED.remove(name)


async def build_dwt(backend) -> list[dict]:
    """Open each bundled DXF over COM and save its .dwt twin next to it."""
    from engineering.standards.templates import TEMPLATE_CATALOG, TEMPLATES_DIR

    rows: list[dict] = []
    if len(TEMPLATE_CATALOG) != len(TEMPLATE_NAMES):
        raise SystemExit(
            f"TEMPLATE_CATALOG has {len(TEMPLATE_CATALOG)} rows, the smoke pins "
            f"{len(TEMPLATE_NAMES)}: update TEMPLATE_NAMES deliberately"
        )
    for name in TEMPLATE_NAMES:
        dxf = Path(TEMPLATES_DIR).resolve() / f"{name}.dxf"
        dwt = Path(TEMPLATES_DIR).resolve() / f"{name}.dwt"
        if not dxf.exists():
            raise SystemExit(f"{dxf} is missing: run scripts/build_templates.py first")
        await backend.drawing_open(str(dxf))
        opened_as = await _opened_now(backend)
        saved = await backend.drawing_template_save(
            str(dwt), name=name, description=f"AutoCAD MCP Pro bundled template {name}"
        )
        # SaveAs rebinds the document to the .dwt name (``document_rebound``).
        if opened_as in _OPENED:
            _OPENED.remove(opened_as)
        await _close_ours(backend, await _opened_now(backend))
        head = dwt.read_bytes()[:6]
        rows.append(
            {
                "name": name,
                "path": str(dwt.relative_to(ROOT)),
                "bytes": dwt.stat().st_size,
                "magic": head.decode("ascii", "replace"),
                "is_dwg_container": head.startswith(DWG_MAGIC),
                "format": saved["format"],
            }
        )
    return rows


async def main(args: argparse.Namespace) -> int:
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

    failures: list[str] = []
    report: dict = {}
    try:
        status = await backend.system_status()
        if not status.get("connected"):
            print(f"no live AutoCAD reachable: {status.get('error', status)}")
            return 2
        report["autocad"] = status.get("autocad_version") or status.get("cad_progid")
        report["system_launch"] = await backend.system_launch(visible=True)
        _check(failures, "system_launch attached", report["system_launch"]["attached"] is True)

        if args.build_dwt:
            report["dwt_built"] = await build_dwt(backend)
            _check(
                failures,
                "every .dwt is a DWG container",
                all(row["is_dwg_container"] for row in report["dwt_built"]),
            )

        await backend.drawing_new()
        smoke_document = await _opened_now(backend)
        report["document"] = smoke_document

        # ── styles ────────────────────────────────────────────────────────
        from engineering.standards.dimstyles import resolve_dimstyle
        from engineering.standards.mleaderstyles import resolve_mleaderstyle

        created = await backend.dimstyle_create(
            "ISO-25-SMOKE", resolve_dimstyle("iso-25", None), set_current=True
        )
        dimstyles = {row["name"]: row for row in await backend.dimstyle_list()}
        smoke_style = dimstyles["ISO-25-SMOKE"]
        _check(failures, "dimstyle DIMTXT 2.5", abs(smoke_style["values"]["DIMTXT"] - 2.5) < 1e-6)
        _check(failures, "dimstyle DIMDSEP ','", smoke_style["values"]["DIMDSEP"] == ",")
        _check(failures, "dimstyle current", smoke_style["current"] is True)
        textstyle = await backend.textstyle_create("ISOCP-SMOKE", "isocp.shx")
        _check(failures, "isocp.shx resolved on the seat", textstyle["font_resolved"] is True)
        # Both engines create MLEADER styles (spec: COM through the
        # ACAD_MLEADERSTYLE dictionary's AddObject; no `mleaderstyle` key).
        mleader_created = await backend.mleaderstyle_create(
            "SMOKE", resolve_mleaderstyle("iso", None)
        )
        mleader_styles = {row["name"]: row for row in await backend.mleaderstyle_list()}
        _check(failures, "mleaderstyle created on COM", mleader_created["ok"] is True)
        _check(failures, "mleaderstyle listed", "SMOKE" in mleader_styles)
        _check(
            failures,
            "mleaderstyle ISO arrow 2.5",
            abs(float(mleader_styles.get("SMOKE", {}).get("arrow_size", 0.0)) - 2.5) < 1e-6,
        )
        report["styles"] = {
            "dimstyle": created,
            "textstyle": textstyle,
            "mleaderstyle": mleader_created,
            "mleaderstyles_on_seat": sorted(mleader_styles),
        }

        # ── page setup + the PDF's own mediabox ───────────────────────────
        from engineering.standards.papers import resolve_page_setup
        from engineering.standards.plot import batch_plot

        applied = await backend.page_setup_apply(
            "Layout1", resolve_page_setup("ISO_A3", orientation="landscape", scale="1:1")
        )
        listed = (await backend.page_setup_list("Layout1"))[0]
        _check(
            failures,
            "page_setup_list says 420 x 297",
            tuple(float(v) for v in listed["size_mm"]) == A3_LANDSCAPE_MM,
        )
        out_dir = Path(tempfile.mkdtemp(prefix="acadmcp_settings_smoke_"))
        plotted = await batch_plot(backend, ["Layout1"], str(out_dir), "{drawing}-{layout}.pdf")
        pdf_row = plotted["sheets"][0]
        width_mm, height_mm = (float(v) for v in pdf_row["mediabox_mm"])
        _check(
            failures,
            "PDF /MediaBox is 420 x 297 (+-0.5 mm)",
            abs(width_mm - A3_LANDSCAPE_MM[0]) < MEDIABOX_TOLERANCE_MM
            and abs(height_mm - A3_LANDSCAPE_MM[1]) < MEDIABOX_TOLERANCE_MM,
        )
        # Catalogue rows carry ``installed`` (True/False, None when the
        # Preferences object is unreachable); ``source: "installed"`` marks
        # files the catalogue does not know.
        installed = sorted(
            row["name"] for row in await backend.plot_style_list() if row.get("installed") is True
        )
        _check(failures, "monochrome.ctb installed on the seat", "monochrome.ctb" in installed)
        report["page_setup"] = {
            "applied": applied["applied"],
            "changed": applied["changed"],
            "pdf": pdf_row,
            "installed_plot_styles": installed,
        }

        # ── bundled template over COM ─────────────────────────────────────
        from engineering.standards.templates import resolve_template

        template_path, template_source = resolve_template("iso_a3_mech", "com")
        opened = await backend.drawing_new(template=template_path)
        await _opened_now(backend)
        template_layers = {layer.name for layer in await backend.layer_list()}
        current_dimstyles = [row["name"] for row in await backend.dimstyle_list() if row["current"]]
        template_papers = [row["paper"] for row in await backend.page_setup_list()]
        _check(
            failures,
            "template carries the mech layers",
            {"GEOMETRY", "DIM", "CENTER", "HIDDEN", "TITLEBLOCK"} <= template_layers,
        )
        _check(failures, "template has ISO-25 current", current_dimstyles == ["ISO-25"])
        _check(failures, "template has an ISO_A3 page setup", "ISO_A3" in template_papers)
        report["template"] = {
            "path": str(template_path),
            "source": template_source,
            "document": opened["name"],
            "dimstyle_current": current_dimstyles,
            "papers": template_papers,
        }
        await _close_ours(backend, opened["name"])
        activated = await backend.document_activate(smoke_document)
        _check(failures, "smoke document re-activated", activated["active"] == smoke_document)
        report["documents"] = await backend.document_list()

        # ── layer state round trip ────────────────────────────────────────
        await backend.layer_create("SMOKE_A", color=1)
        await backend.layer_create("SMOKE_B", color=2)
        saved = await backend.layer_state_save("SMOKE", description="settings smoke")
        await backend.layer_modify("SMOKE_A", color=5)
        await backend.layer_freeze("SMOKE_B")
        restored = await backend.layer_state_restore("SMOKE")
        after = {layer.name: layer for layer in await backend.layer_list()}
        _check(failures, "layer state restored the colour", after["SMOKE_A"].color == 1)
        _check(failures, "layer state thawed SMOKE_B", after["SMOKE_B"].is_frozen is False)
        _check(failures, "restore reports nothing missing", restored["missing_layers"] == [])
        _check(
            failures,
            "restore applied every layer",
            restored["applied"]["layers"] == saved["layer_count"],
        )
        _check(failures, "restore raised no warnings", "warnings" not in restored)
        states = await backend.layer_state_list()
        _check(failures, "layer state listed", any(row["name"] == "SMOKE" for row in states))
        deleted = await backend.layer_state_delete("SMOKE")
        report["layer_state"] = {"saved": saved, "restored": restored, "deleted": deleted}

        # ── named view ────────────────────────────────────────────────────
        view = await backend.view_named_save("SMOKE_VIEW", center=[100.0, 50.0], height=80.0)
        views = await backend.view_named_list()
        _check(failures, "named view listed", any(row["name"] == "SMOKE_VIEW" for row in views))
        report["view"] = {"saved": view, "restored": await backend.view_named_restore("SMOKE_VIEW")}

        # ── UCS ───────────────────────────────────────────────────────────
        ucs = await backend.ucs_set(
            "SMOKE_UCS", [10.0, 20.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
        )
        ucs_rows = await backend.ucs_list()
        _check(
            failures,
            "ucs current",
            any(row["name"] == "SMOKE_UCS" and row["current"] for row in ucs_rows),
        )
        try:
            await backend.ucs_set("SMOKE_BAD", [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0])
            _check(failures, "non-orthogonal UCS refused", False)
        except ValueError as exc:
            _check(failures, "non-orthogonal UCS refusal names the angle", "45" in str(exc))
        report["ucs"] = {"set": ucs, "world": await backend.ucs_restore("world")}

        # ── preferences: get, one reversible set, a read-only refusal ─────
        prefs = await backend.preferences_get()
        cursor = int(prefs["values"]["Display.CursorSize"])
        probe = cursor + 1 if cursor < 100 else cursor - 1
        bumped = await backend.preferences_set("Display.CursorSize", probe)
        reverted = await backend.preferences_set("Display.CursorSize", cursor)
        _check(failures, "preference set reports old", bumped["old"] == cursor)
        _check(failures, "preference set reports new", bumped["new"] == probe)
        _check(failures, "preference reverted", reverted["new"] == cursor)
        _check(failures, "read-only keys listed", "Files.SupportPath" in prefs["read_only"])
        try:
            await backend.preferences_set("Files.SupportPath", "C:/nope")
            _check(failures, "read-only preference refused", False)
        except ValueError:
            _check(failures, "read-only preference refused", True)
        report["preferences"] = {
            "cursor_size": cursor,
            "read_only": prefs["read_only"],
            "template_dwg_path": prefs["values"].get("Files.TemplateDwgPath"),
        }

        # ── SummaryInfo ───────────────────────────────────────────────────
        written = await backend.drawing_properties_set(
            summary={"title": "ACADMCP settings smoke", "author": "smoke_settings_com.py"},
            custom={"ACADMCP_SMOKE": "1"},
        )
        props = await backend.drawing_properties_get()
        _check(failures, "summary available on COM", props["summary_available"] is True)
        _check(failures, "title round trip", props["summary"]["title"] == "ACADMCP settings smoke")
        _check(failures, "custom property round trip", props["custom"].get("ACADMCP_SMOKE") == "1")
        report["properties"] = {"set": written, "get": props}

        # ── operator prompt line ──────────────────────────────────────────
        report["prompt_message"] = await backend.system_prompt_message(
            "ACADMCP settings smoke: done"
        )

        if args.interactive:
            picked = await backend.user_pick_point("ACADMCP smoke: pick any point (Esc cancels)")
            report["user_pick_point"] = picked
            _check(
                failures,
                "pick returns a point, cancelled or timed_out",
                bool(picked.get("cancelled"))
                or bool(picked.get("timed_out"))
                or ("x" in picked and "y" in picked),
            )

        report["failures"] = failures
        print(json.dumps(report, indent=2, default=str))
        if not args.keep:
            await _close_ours(backend, smoke_document)
        return 0 if not failures else 1
    finally:
        if not args.keep:
            await _close_leftovers(backend)
        await backend.disconnect()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--build-dwt", action="store_true", help="build templates/*.dwt first")
    parser.add_argument("--interactive", action="store_true", help="also run user_pick_point")
    parser.add_argument("--keep", action="store_true", help="leave the smoke document open")
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_parser().parse_args())))
