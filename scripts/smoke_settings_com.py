# scripts/smoke_settings_com.py
"""Exercise the settings track once against the live AutoCAD on this machine.

Runs on the live harness (scripts/live/harness.py): attach only, NEW
documents of its own (never the operator's drawings), each closed on every
path. Every SECTION 18-20 path that needs a live seat goes through the same
backend methods the tools call:

    system_launch (attach)                  -> {attached, launched, version}
    dimstyle_create ISO-25 / textstyle_create ISOCP
        / mleaderstyle_create iso -> listed with arrow_size 2.5
    page_setup_apply ISO_A3 -> batch_plot -> the PDF's own /MediaBox (420 x 297)
    plot_style_list (installed .ctb files)
    drawing_new(template=iso_a3_mech .dwt) -> mech layers, ISO-25 current, ISO_A3
    document_activate (back to the smoke's own document)
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

Every assertion is a harness check. Exit codes are the harness's: 0 every
check passed, 1 one failed or raised, 2 no running AutoCAD to attach to or the
active document changed under it.

    uv run --frozen python scripts/smoke_settings_com.py [--keep] [--json PATH]
    uv run --frozen python scripts/smoke_settings_com.py --build-dwt
    uv run --frozen python scripts/smoke_settings_com.py --interactive --keep
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, parser, run_smoke  # noqa: E402

#: The five bundled templates (spec §6). Pinned here rather than read from
#: the catalogue so the smoke also notices a catalogue that lost a row.
TEMPLATE_NAMES = ("iso_a3_mech", "iso_a1_arch", "iso_a3_pid", "ansi_b_mech", "ansi_d_arch")

#: Every DWG/DWT since R13 opens with "AC10xx" (AC1032 for the 2018 format).
DWG_MAGIC = b"AC10"

#: What the ISO A3 landscape sheet must read back as, from the PDF itself.
A3_LANDSCAPE_MM = (420.0, 297.0)
MEDIABOX_TOLERANCE_MM = 0.5


async def build_dwt(h: LiveHarness) -> list[dict]:
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
        opened = await h.open_document(str(dxf))
        saved = await h.backend.drawing_template_save(
            str(dwt), name=name, description=f"AutoCAD MCP Pro bundled template {name}"
        )
        # SaveAs rebinds the document to the .dwt name; the harness follows it.
        await h.close_document(opened)
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


async def _styles(h: LiveHarness) -> dict:
    from engineering.standards.dimstyles import resolve_dimstyle
    from engineering.standards.mleaderstyles import resolve_mleaderstyle

    backend = h.backend
    created = await backend.dimstyle_create(
        "ISO-25-SMOKE", resolve_dimstyle("iso-25", None), set_current=True
    )
    dimstyles = {row["name"]: row for row in await backend.dimstyle_list()}
    smoke_style = dimstyles["ISO-25-SMOKE"]
    dimtxt = smoke_style["values"]["DIMTXT"]
    h.check("dimstyle DIMTXT 2.5", abs(dimtxt - 2.5) < 1e-6, expected=2.5, observed=dimtxt)
    dimdsep = smoke_style["values"]["DIMDSEP"]
    h.check("dimstyle DIMDSEP ','", dimdsep == ",", expected=",", observed=dimdsep)
    h.check("dimstyle current", smoke_style["current"] is True, observed=smoke_style["current"])
    textstyle = await backend.textstyle_create("ISOCP-SMOKE", "isocp.shx")
    h.check(
        "isocp.shx resolved on the seat",
        textstyle["font_resolved"] is True,
        observed=textstyle["font_resolved"],
    )
    # Both engines create MLEADER styles (spec: COM through the
    # ACAD_MLEADERSTYLE dictionary's AddObject; no `mleaderstyle` key).
    mleader_created = await backend.mleaderstyle_create("SMOKE", resolve_mleaderstyle("iso", None))
    mleader_styles = {row["name"]: row for row in await backend.mleaderstyle_list()}
    h.check("mleaderstyle created on COM", mleader_created["ok"] is True)
    h.check("mleaderstyle listed", "SMOKE" in mleader_styles, observed=sorted(mleader_styles))
    arrow = float(mleader_styles.get("SMOKE", {}).get("arrow_size", 0.0))
    h.check("mleaderstyle ISO arrow 2.5", abs(arrow - 2.5) < 1e-6, expected=2.5, observed=arrow)
    return {
        "dimstyle": created,
        "textstyle": textstyle,
        "mleaderstyle": mleader_created,
        "mleaderstyles_on_seat": sorted(mleader_styles),
    }


async def _page_setup(h: LiveHarness) -> dict:
    from engineering.standards.papers import resolve_page_setup
    from engineering.standards.plot import batch_plot

    backend = h.backend
    applied = await backend.page_setup_apply(
        "Layout1", resolve_page_setup("ISO_A3", orientation="landscape", scale="1:1")
    )
    listed = (await backend.page_setup_list("Layout1"))[0]
    size = tuple(float(v) for v in listed["size_mm"])
    h.check(
        "page_setup_list says 420 x 297",
        size == A3_LANDSCAPE_MM,
        expected=A3_LANDSCAPE_MM,
        observed=size,
    )
    out_dir = Path(tempfile.mkdtemp(prefix="acadmcp_settings_smoke_"))
    plotted = await batch_plot(backend, ["Layout1"], str(out_dir), "{drawing}-{layout}.pdf")
    pdf_row = plotted["sheets"][0]
    mediabox = pdf_row.get("mediabox_mm") or [0.0, 0.0]
    width_mm, height_mm = (float(v) for v in mediabox)
    h.check(
        "PDF /MediaBox is 420 x 297 (+-0.5 mm)",
        abs(width_mm - A3_LANDSCAPE_MM[0]) < MEDIABOX_TOLERANCE_MM
        and abs(height_mm - A3_LANDSCAPE_MM[1]) < MEDIABOX_TOLERANCE_MM,
        expected=A3_LANDSCAPE_MM,
        observed=mediabox,
    )
    # Catalogue rows carry ``installed`` (True/False, None when the
    # Preferences object is unreachable); ``source: "installed"`` marks
    # files the catalogue does not know.
    installed = sorted(
        row["name"] for row in await backend.plot_style_list() if row.get("installed") is True
    )
    h.check("monochrome.ctb installed on the seat", "monochrome.ctb" in installed)
    return {
        "applied": applied["applied"],
        "changed": applied["changed"],
        "pdf": {key: value for key, value in pdf_row.items() if key != "path"}
        | {"name": Path(str(pdf_row.get("path", ""))).name},
        "installed_plot_styles": installed,
    }


async def _template(h: LiveHarness, smoke_document: str) -> dict:
    from engineering.standards.templates import resolve_template

    backend = h.backend
    template_path, template_source = resolve_template("iso_a3_mech", "com")
    opened = await h.new_document(template=str(template_path))
    template_layers = {layer.name for layer in await backend.layer_list()}
    current_dimstyles = [row["name"] for row in await backend.dimstyle_list() if row["current"]]
    template_papers = [row["paper"] for row in await backend.page_setup_list()]
    h.check(
        "template carries the mech layers",
        {"GEOMETRY", "DIM", "CENTER", "HIDDEN", "TITLEBLOCK"} <= template_layers,
        observed=sorted(template_layers),
    )
    h.check(
        "template has ISO-25 current",
        current_dimstyles == ["ISO-25"],
        expected=["ISO-25"],
        observed=current_dimstyles,
    )
    h.check(
        "template has an ISO_A3 page setup", "ISO_A3" in template_papers, observed=template_papers
    )
    await h.close_document(opened)
    activated = await backend.document_activate(smoke_document)
    h.check(
        "smoke document re-activated",
        activated["active"] == smoke_document,
        expected=smoke_document,
        observed=activated["active"],
    )
    return {
        "template": Path(str(template_path)).name,
        "source": template_source,
        "dimstyle_current": current_dimstyles,
        "papers": template_papers,
    }


async def _layer_state(h: LiveHarness) -> dict:
    backend = h.backend
    await backend.layer_create("SMOKE_A", color=1)
    await backend.layer_create("SMOKE_B", color=2)
    saved = await backend.layer_state_save("SMOKE", description="settings smoke")
    await backend.layer_modify("SMOKE_A", color=5)
    await backend.layer_freeze("SMOKE_B")
    restored = await backend.layer_state_restore("SMOKE")
    after = {layer.name: layer for layer in await backend.layer_list()}
    h.check(
        "layer state restored the colour",
        after["SMOKE_A"].color == 1,
        expected=1,
        observed=after["SMOKE_A"].color,
    )
    h.check("layer state thawed SMOKE_B", after["SMOKE_B"].is_frozen is False)
    h.check(
        "restore reports nothing missing",
        restored["missing_layers"] == [],
        observed=restored["missing_layers"],
    )
    h.check(
        "restore applied every layer",
        restored["applied"]["layers"] == saved["layer_count"],
        expected=saved["layer_count"],
        observed=restored["applied"]["layers"],
    )
    h.check("restore raised no warnings", "warnings" not in restored)
    states = await backend.layer_state_list()
    h.check("layer state listed", any(row["name"] == "SMOKE" for row in states))
    deleted = await backend.layer_state_delete("SMOKE")
    return {"saved": saved, "restored": restored, "deleted": deleted}


async def _view_and_ucs(h: LiveHarness) -> dict:
    backend = h.backend
    view = await backend.view_named_save("SMOKE_VIEW", center=[100.0, 50.0], height=80.0)
    views = await backend.view_named_list()
    h.check("named view listed", any(row["name"] == "SMOKE_VIEW" for row in views))
    restored = await backend.view_named_restore("SMOKE_VIEW")
    ucs = await backend.ucs_set("SMOKE_UCS", [10.0, 20.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
    ucs_rows = await backend.ucs_list()
    h.check(
        "ucs current",
        any(row["name"] == "SMOKE_UCS" and row["current"] for row in ucs_rows),
    )
    try:
        await backend.ucs_set("SMOKE_BAD", [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0])
        h.check("non-orthogonal UCS refused", False)
    except ValueError as exc:
        h.check("non-orthogonal UCS refusal names the angle", "45" in str(exc), observed=str(exc))
    return {
        "view": {"saved": view, "restored": restored},
        "ucs": {"set": ucs, "world": await backend.ucs_restore("world")},
    }


async def _preferences(h: LiveHarness) -> dict:
    backend = h.backend
    prefs = await backend.preferences_get()
    cursor = int(prefs["values"]["Display.CursorSize"])
    probe = cursor + 1 if cursor < 100 else cursor - 1
    bumped = await backend.preferences_set("Display.CursorSize", probe)
    reverted = await backend.preferences_set("Display.CursorSize", cursor)
    h.check("preference set reports old", bumped["old"] == cursor, expected=cursor, observed=bumped)
    h.check("preference set reports new", bumped["new"] == probe, expected=probe, observed=bumped)
    h.check("preference reverted", reverted["new"] == cursor, expected=cursor, observed=reverted)
    h.check("read-only keys listed", "Files.SupportPath" in prefs["read_only"])
    try:
        await backend.preferences_set("Files.SupportPath", "C:/nope")
        h.check("read-only preference refused", False)
    except ValueError:
        h.check("read-only preference refused", True)
    return {"cursor_size": cursor, "read_only": prefs["read_only"]}


async def _properties(h: LiveHarness) -> dict:
    backend = h.backend
    written = await backend.drawing_properties_set(
        summary={"title": "ACADMCP settings smoke", "author": "smoke_settings_com.py"},
        custom={"ACADMCP_SMOKE": "1"},
    )
    props = await backend.drawing_properties_get()
    h.check("summary available on COM", props["summary_available"] is True)
    h.check(
        "title round trip",
        props["summary"]["title"] == "ACADMCP settings smoke",
        observed=props["summary"]["title"],
    )
    h.check(
        "custom property round trip",
        props["custom"].get("ACADMCP_SMOKE") == "1",
        observed=props["custom"].get("ACADMCP_SMOKE"),
    )
    return {"set": written, "get": props}


def make_body(*, build: bool = False, interactive: bool = False):
    async def body(h: LiveHarness) -> dict:
        backend = h.backend
        report: dict = {}
        h.stage("attach")
        launched = await backend.system_launch(visible=True)
        # Only the two flags: the rest names the document AutoCAD has active
        # now, which is the operator's.
        launch = {key: launched[key] for key in ("attached", "launched")}
        report["system_launch"] = launch
        h.check("system_launch attached", launch["attached"] is True, observed=launch)
        h.check("system_launch launched nothing", launch["launched"] is False, observed=launch)

        if build:
            h.stage("dwt")
            report["dwt_built"] = await build_dwt(h)
            h.check(
                "every .dwt is a DWG container",
                all(row["is_dwg_container"] for row in report["dwt_built"]),
            )

        smoke_document = await h.new_document()
        report["document"] = smoke_document
        h.stage("styles")
        report["styles"] = await _styles(h)
        h.stage("page setup")
        report["page_setup"] = await _page_setup(h)
        h.stage("template")
        report["template"] = await _template(h, smoke_document)
        h.stage("layer state")
        report["layer_state"] = await _layer_state(h)
        h.stage("view and ucs")
        report.update(await _view_and_ucs(h))
        h.stage("preferences")
        report["preferences"] = await _preferences(h)
        h.stage("properties")
        report["properties"] = await _properties(h)
        h.stage("prompt")
        report["prompt_message"] = await backend.system_prompt_message(
            "ACADMCP settings smoke: done"
        )
        if interactive:
            h.stage("pick")
            picked = await backend.user_pick_point("ACADMCP smoke: pick any point (Esc cancels)")
            report["user_pick_point"] = picked
            h.check(
                "pick returns a point, cancelled or timed_out",
                bool(picked.get("cancelled"))
                or bool(picked.get("timed_out"))
                or ("x" in picked and "y" in picked),
                observed=picked,
            )
        return report

    return body


#: The default run (no --build-dwt, no --interactive), as the tests load it.
body = make_body()


def assess(h: LiveHarness, report: dict) -> None:
    """Nothing is left to assess afterwards: the settings smoke checks inside
    each step, because the seat state a step reads (a preference, a restored
    layer state) is gone once the next step runs."""


def main(argv: list[str] | None = None) -> int:
    cli = parser(__doc__.split("\n\n")[0])
    cli.add_argument("--build-dwt", action="store_true", help="build templates/*.dwt first")
    cli.add_argument("--interactive", action="store_true", help="also run user_pick_point")
    args = cli.parse_args(argv)
    run = make_body(build=args.build_dwt, interactive=args.interactive)
    return asyncio.run(run_smoke("settings", run, keep=args.keep, json_path=args.json))


if __name__ == "__main__":
    raise SystemExit(main())
