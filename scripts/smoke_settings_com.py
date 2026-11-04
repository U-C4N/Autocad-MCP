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
    entity_modify_where (dry run, model / paper / block filters, a stale
        expect_count, ByLayer read back) -> layer_rename (+ layer 0 refused)
        -> layer_merge (model, sheet, block definition; sources deleted)
    dimension_auto(selector=) on a plate -> 12 dimensions, the ellipse omitted,
        a second call draws nothing (1.8-13)
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


async def _set_edits(h: LiveHarness) -> dict:
    """1.8-12: entity_modify_where, layer_rename, layer_merge on the seat.

    Measures what the fake seat of tests/test_set_edits_com.py assumes: the
    ``ssget "X"`` group-code filter (8 with a backtick-escaped ``-``, 410
    ``Model`` / ``~Model``, 0 + 2 for a block, 66 for attributes), that a
    selected proxy stays writable after its SelectionSet is deleted, that a
    rename touches no entity, and that a merge reaches a paper layout and a
    block definition before the sources are deleted.
    """
    from engineering.changes.preview import StaleRevisionError

    backend = h.backend
    for name, color in (("SMOKE-OLD", 1), ("SMOKE_walls", 2), ("SMOKE-WALL", 3)):
        await backend.layer_create(name, color=color)
    model = []
    for i in range(4):
        info = await backend.entity_create_line(
            i * 10.0, 0.0, i * 10.0 + 5.0, 5.0, layer="SMOKE-OLD"
        )
        await backend.entity_set_properties(info.handle, color=5, lineweight=-3)
        model.append(info.handle)
    other = await backend.entity_create_line(0.0, 50.0, 5.0, 55.0, layer="SMOKE_walls")
    await backend.layout_set_current("Layout1")
    sheet = await backend.entity_create_line(10.0, 10.0, 20.0, 20.0, layer="SMOKE-OLD")
    await backend.layout_set_current("Model")
    await backend.block_define(
        "SMOKE_DOOR", [{"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0, "layer": "SMOKE_walls"}]
    )
    door = await backend.block_insert("SMOKE_DOOR", 100.0, 100.0)

    dry = await backend.entity_select_where({"layer": "SMOKE-OLD"}, {"color": 256}, "model")
    h.check("where: escaped '-' layer filter, model only", dry["count"] == 4, observed=dry)
    paper = await backend.entity_select_where({"layer": "SMOKE-OLD"}, {"color": 256}, "paper")
    h.check(
        "where: 410 ~Model selects the sheet only",
        paper["handles"] == [sheet.handle],
        observed=paper,
    )
    blocks = await backend.entity_select_where({"block": "SMOKE_D*"}, {"color": 256}, "all")
    h.check("where: 0 INSERT + 2 block name", blocks["handles"] == [door.handle], observed=blocks)
    try:
        await backend.entity_modify_where({"layer": "SMOKE-OLD"}, {"color": 256}, "model", 9)
        h.check("expect_count mismatch refused", False)
    except StaleRevisionError as exc:
        h.check("expect_count mismatch refused stale_revision", exc.kind == "stale_revision")
    applied = await backend.entity_modify_where(
        {"layer": "SMOKE-OLD"}, {"color": 256, "lineweight": "ByLayer"}, "model", 4
    )
    h.check("where: four lines restyled", applied["changed"] == 4, observed=applied)
    after = [await backend.entity_get(handle) for handle in model]
    h.check(
        "where: ByLayer colour and lineweight read back",
        all(row.color == 256 and row.lineweight_name == "ByLayer" for row in after),
        observed=[(row.color, row.lineweight_name) for row in after],
    )

    renamed = await backend.layer_rename("SMOKE-OLD", "SMOKE-RENAMED")
    layers = {layer.name for layer in await backend.layer_list()}
    h.check(
        "rename: the table entry moved",
        "SMOKE-RENAMED" in layers and "SMOKE-OLD" not in layers,
        observed=sorted(layers),
    )
    first = await backend.entity_get(model[0])
    h.check("rename: an entity reads the new name", first.layer == "SMOKE-RENAMED")
    try:
        await backend.layer_rename("0", "SMOKE-ZERO")
        h.check("rename of layer 0 refused", False)
    except Exception as exc:  # noqa: BLE001 - the refusal is the check
        h.check("rename of layer 0 refused by name", "cannot be renamed" in str(exc))

    merged = await backend.layer_merge(["SMOKE-RENAMED", "SMOKE_walls"], "SMOKE-WALL")
    h.check(
        "merge: 4 + 1 model, 1 sheet, 1 block member", merged["reassigned"] == 7, observed=merged
    )
    h.check("merge: the block named", merged["blocks_touched"] == ["SMOKE_DOOR"], observed=merged)
    h.check(
        "merge: both sources deleted",
        merged["layers_deleted"] == ["SMOKE-RENAMED", "SMOKE_walls"],
        observed=merged,
    )
    moved = await backend.entity_get(other.handle)
    h.check("merge: the other line moved", moved.layer == "SMOKE-WALL", observed=moved.layer)
    return {"dry_run": dry, "applied": applied, "renamed": renamed, "merged": merged}


async def _dimension_auto(h: LiveHarness) -> dict:
    """1.8-13: ``dimension_auto`` v2 on the seat - a plate in one call, not twice.

    Measures what tests/test_dimension_auto_v2.py's fake seat assumes: that
    ``entity_select_smart`` finds the AcDbPolyline / circles / arcs by layer
    (the POLYLINE spelling read as an LWPOLYLINE), that AddDimRotated /
    AddDimDiametric / AddDimRadial land on the ensured DIM layer with every
    diameter's text beyond its FIRST chord point (outside the part), that the
    snapshot ``Document.Export`` binds every one of them for ``skip_existing``
    - a second call draws nothing - and that an ellipse is named in
    ``omitted``.
    """
    backend = h.backend
    await backend.layer_create("SMOKE-GEOM", color=7)
    outline = await backend.entity_create_polyline(
        [[0.0, 0.0], [120.0, 0.0], [120.0, 80.0], [0.0, 80.0]], True, layer="SMOKE-GEOM"
    )
    holes = [
        await backend.entity_create_circle(x, y, 5.5, layer="SMOKE-GEOM")
        for x in (15.0, 60.0, 105.0)
        for y in (15.0, 65.0)
    ]
    arcs = [
        await backend.entity_create_arc(40.0, 40.0, 8.0, 0.0, 180.0, layer="SMOKE-GEOM"),
        await backend.entity_create_arc(80.0, 40.0, 8.0, 180.0, 360.0, layer="SMOKE-GEOM"),
    ]
    ellipse = await backend.entity_create_ellipse(60.0, 40.0, 6.0, 0.0, 0.5, layer="SMOKE-GEOM")
    first = await backend.dimension_auto(selector={"layer": "SMOKE-GEOM"}, layer="SMOKE-DIM")
    report = first.to_dict()
    h.check("dimension_auto: twelve dimensions in one call", len(first) == 12, observed=report)
    h.check(
        "dimension_auto: the ellipse is omitted by name",
        [row["handle"] for row in report["omitted"]] == [ellipse.handle],
        observed=report["omitted"],
    )
    h.check(
        "dimension_auto: every dimension on the ensured layer",
        all(info.layer == "SMOKE-DIM" for info in first),
        observed=[info.layer for info in first],
    )
    h.check(
        "dimension_auto: the self-check read the drawing",
        report["dim_overlap"]["checked"] is True and report["dim_overlap"]["issues"] == [],
        observed=report["dim_overlap"],
    )
    again = (await backend.dimension_auto(selector={"layer": "SMOKE-GEOM"})).to_dict()
    h.check(
        "dimension_auto: a second call draws nothing (skip_existing through Document.Export)",
        again["created"] == [] and len(again["skipped_existing"]) == 12,
        observed=again,
    )
    return {
        "outline": outline.handle,
        "holes": [hole.handle for hole in holes],
        "arcs": [arc.handle for arc in arcs],
        "first": report,
        "again": again,
    }


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


async def _dimension_truth(h: LiveHarness) -> dict:
    """1.8-15 / spike S4: does AutoCAD redraw a *D block after a DIMDEC change,
    does Update() do it, does ``%%c<>`` print a double diameter sign?

    Runs after ``_styles`` (ISO-25-SMOKE is current, DIMDEC 2). Every answer
    is read back through the ``Document.Export`` snapshot - the *D text AutoCAD
    actually holds - never taken from the call's own return value.
    """
    from engineering.critique import run_critique

    backend = h.backend
    await backend.entity_create_line(0, 0, 0, 20, layer="0")
    await backend.entity_create_line(19.6, 0, 19.6, 20, layer="0")
    linear = await backend.dimension_linear(0, 0, 19.6, 0, 10, -10)
    await backend.entity_create_circle(100, 0, 10, layer="0")
    double = await backend.dimension_diameter(90, 0, 110, 0, text_override="%%c<>")
    before = await backend._dimension_display([linear.handle, double.handle])
    modified = await backend.dimstyle_modify("ISO-25-SMOKE", {"DIMDEC": 0})
    after = await backend._dimension_display([linear.handle, double.handle])
    key = linear.handle.upper()
    h.check(
        "DIMDEC 2 -> 0 redrawn and confirmed by read-back",
        after.get(key, (None, None))[1] is True and modified["rerender_required"] is False,
        expected="20",
        observed={"before": before.get(key), "after": after.get(key), "modify": modified},
    )
    scaled = await backend.entity_scale(linear.handle, 0, 0, 2)
    h.check(
        "entity_scale redraws the number (Update + read-back)",
        scaled.get("rerendered") == [linear.handle],
        observed=scaled,
    )
    rerender = await backend.dimension_rerender([linear.handle, double.handle])
    h.check("dimension_rerender verified", rerender["verified"] is True, observed=rerender)
    double_text = after.get(double.handle.upper(), (None, None))[0]
    # S4: recorded, not asserted - the answer decides whether dim_double_prefix
    # becomes an error in 1.8.1.
    issues = await run_critique(backend, ["dimension_truth"])
    return {
        "before": before,
        "after": after,
        "modify": modified,
        "scale": scaled,
        "rerender": rerender,
        "double_prefix_rendered": double_text,
        # Both glyphs count: measured on AutoCAD 2026 (2026-11-04) the *D text
        # of "%%c<>" is "Ø∅20" - U+00D8 from %%c, then U+2205, the diameter
        # dimension's own prefix - which a count of "Ø" alone read as single.
        "double_prefix_is_double": bool(
            double_text and sum(double_text.count(sign) for sign in ("Ø", "∅")) >= 2
        ),
        "double_prefix_codepoints": [f"U+{ord(c):04X}" for c in (double_text or "")],
        "dimension_truth": [i.to_dict() for i in issues],
    }


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
        h.stage("dimension truth")
        report["dimension_truth"] = await _dimension_truth(h)
        h.stage("page setup")
        report["page_setup"] = await _page_setup(h)
        h.stage("template")
        report["template"] = await _template(h, smoke_document)
        h.stage("layer state")
        report["layer_state"] = await _layer_state(h)
        h.stage("set edits")
        report["set_edits"] = await _set_edits(h)
        h.stage("dimension auto")
        report["dimension_auto"] = await _dimension_auto(h)
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
