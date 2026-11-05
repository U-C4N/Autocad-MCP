"""Render the drawing the README shows, using only the server's own tools.

A CAD README that shows nothing but bar charts is asking to be taken on faith.
This produces the one asset that is not a claim: an actual A3 sheet, built
through the same backend methods and engineering helpers the MCP tools call, in
the order CLAUDE.md's premium workflow prescribes -- a committed plan, ISO
layers, a 3.5 mm ISO 129 dimension style, deterministic gear geometry with a
DIN 6885 keyed bore, the hatched section A-A, an ISO 286 H7 fit resolved from
the authored tables, ISO 128-23 centre marks, an ISO 21920-1 surface-texture
symbol, an ISO 128-40 cutting-plane line, an ISO 5457 frame with zones, an
ISO 7200 title block and a revision row -- and rendered headlessly at 3200 px.

Reproduce:

    AUTOCAD_MCP_BACKEND=ezdxf uv run --frozen python scripts/render_readme_showcase.py

Nothing here is decoration: every line on the sheet comes from a tool the
server exposes, so if a tool regresses the picture changes with it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "autocad-mcp-showcase.png"
#: drawing_finalize saves before it validates; the saved sheet goes under the
#: gitignored out/ folder, never into a tracked path.
FINALIZE_DXF = ROOT / "out" / "readme-showcase" / "spur-gear-m6-z24.dxf"

#: A 24-tooth, module-6 spur gear, drawn at 1:1 on an A3 sheet.
GEAR_MODULE = 6.0
GEAR_TEETH = 24
BORE_DIAMETER = 40.0
KEYWAY_WIDTH = 12.0
KEYWAY_DEPTH = 3.3
FACE_WIDTH = 46.0
#: The front view's centre and the section's left edge. The A3 drawing area
#: inside the ISO 5457 frame is x 20..410, y 10..287; the title block takes
#: the lower-right 180 mm. These put the two views across the sheet's width
#: with their dimensions clear of the frame, the title block and each other.
GEAR_CENTER = (138.0, 160.0)
SECTION_X = 268.0

#: The sheet's lettering: ISO 3098-1 rows. 3.5 mm dimensions and notes on A3,
#: 5 mm view labels. The dimension style carries them, because ezdxf renders a
#: DIMENSION from its dimstyle table entry - the header's DIMSCALE alone
#: changes nothing headlessly (measured), so no DIMSCALE is set here.
TEXT_H = 3.5
LABEL_H = 5.0
RENDER_PX = 3200
MAX_PNG_BYTES = 700_000


async def _end(backend, handle: str, near: tuple[float, float]) -> tuple[float, float]:
    """The endpoint of `handle` nearest `near`, read from the drawing."""
    x, y = await backend.point_from_snap(handle, "end", near[0], near[1])
    return float(x), float(y)


async def _vertex(backend, handle: str, near: tuple[float, float]) -> tuple[float, float]:
    """The vertex of polyline `handle` nearest `near`, read from the drawing."""
    props = (await backend.entity_get(handle)).properties
    points = props.get("points") or props.get("vertices")
    if not points:
        raise RuntimeError(f"{handle} reports no vertices: {sorted(props)}")
    best = min(points, key=lambda p: (p[0] - near[0]) ** 2 + (p[1] - near[1]) ** 2)
    return float(best[0]), float(best[1])


async def build(backend) -> dict:
    from engineering.fits import fit_lookup
    from engineering.gear import draw_gear_section_aa, draw_spur_gear_front_view
    from engineering.mech.annotate import draw_surface_texture
    from engineering.mech.marks import draw_centre_marks, draw_material_hatch, draw_section_line
    from engineering.sheet.frames import draw_sheet_frame
    from engineering.sheet.revision import add_revision
    from engineering.sheet.titleblock import TitleBlockMetadata, apply_titleblock
    from engineering.standards.dimstyles import resolve_dimstyle

    await backend.drawing_new()
    await backend.drawing_plan(
        f"Spur gear m{GEAR_MODULE:g} z{GEAR_TEETH}: front view with a DIN 6885 keyed "
        "bore and the hatched section A-A, ISO 129 dimensions, H7 bore",
        sheet_size="A3",
        scale=1.0,
        layer_set_id="mech",
        view_count=2,
    )
    await backend.drawing_apply_iso_layers("mech")
    await backend.drawing_settings({"units": "mm", "linear_precision": 2})

    # ISO-25 is the 2.5 mm row; an A3 sheet read at arm's length wants 3.5.
    dim_values = resolve_dimstyle(
        "iso-25",
        {
            "DIMTXT": TEXT_H,
            "DIMASZ": TEXT_H,
            "DIMEXE": 2.0,
            "DIMEXO": 1.0,
            "DIMGAP": 1.0,
            # ISO 129-1 writes a decimal point in this repository's checks
            # (the iso128 critique flags a comma), and an H7 deviation on a
            # 40 mm bore is 0.025: three decimals, or the fit is misprinted.
            "DIMDSEP": ".",
            "DIMDEC": 3,
            # Text moved off the dimension line gets a leader back to it.
            "DIMTMOVE": 1,
        },
    )
    await backend.dimstyle_create("ISO-35", dim_values, set_current=True)

    # ── the ISO 5457 frame first, so every view is placed inside it ─────────
    frame = await draw_sheet_frame(backend, "A3", zones=True, marks=True)

    # ── front view ──────────────────────────────────────────────────────────
    gear = await draw_spur_gear_front_view(
        backend,
        module=GEAR_MODULE,
        teeth=GEAR_TEETH,
        center=GEAR_CENTER,
        bore_diameter=BORE_DIAMETER,
        keyway_width=KEYWAY_WIDTH,
        keyway_depth=KEYWAY_DEPTH,
    )
    meta = gear["metadata"]
    cx, cy = (float(v) for v in meta["center"])
    pitch_r = float(meta["pitch_radius"])
    outer_r = float(meta["outer_radius"])
    bore_r = BORE_DIAMETER / 2.0

    # ── section A-A (hatched by the generator) ──────────────────────────────
    # `gear_metadata` is the geometry record, not the handle map the front view
    # returns -- the section needs radii, not handles.
    section = await draw_gear_section_aa(
        backend,
        gear_metadata=meta,
        x_offset=SECTION_X,
        face_width=FACE_WIDTH,
    )

    # The generator hatches its whole outer rectangle, the bore included - and
    # a bore is a void, not a cut face. Its hatch is replaced by the two faces
    # the cut really passes through, bounded by points read back off the
    # section's own lines, hatched with the ISO 128-50 pattern for steel.
    s_left, s_top = await _end(backend, section["top"], (SECTION_X, cy + outer_r))
    s_right, s_bot = await _end(backend, section["bottom"], (SECTION_X + FACE_WIDTH, cy - outer_r))
    bore_bot_y = (await _end(backend, section["bore"][1], (s_left, cy - bore_r)))[1]
    kw = section["keyway"]["keyway"]  # bottom, left wall, top, right wall
    _, n_top = await _end(backend, kw[2], (s_left, cy + bore_r + KEYWAY_DEPTH))
    if section.get("hatch"):
        await backend.entity_delete(section["hatch"])

    # The cutting plane A-A is vertical through the centre, so it runs along
    # the hub keyway: on this cut the DIN 6885 groove spans the whole face
    # width, and the void above the axis reaches the keyway floor (bore radius
    # + t2) from face to face. The generator draws a 12 mm notch as if section
    # x were front-view x; its notch and the upper bore line are replaced by
    # the keyway floor, and the faces' end lines are cut back to the material
    # (the bore is a void, so no face edge crosses it).
    for handle in [*kw, section["bore"][0], section["left"], section["right"]]:
        await backend.entity_delete(handle)
    keyway_floor = await backend.entity_create_line(s_left, n_top, s_right, n_top, layer="GEOMETRY")
    face_edges = []
    for x in (s_left, s_right):
        for y0, y1 in ((s_bot, bore_bot_y), (n_top, s_top)):
            edge = await backend.entity_create_line(x, y0, x, y1, layer="GEOMETRY")
            face_edges.append(edge.handle)
    upper_face = await draw_material_hatch(
        backend,
        material="steel",
        boundary=[
            [s_left, n_top],
            [s_right, n_top],
            [s_right, s_top],
            [s_left, s_top],
        ],
    )
    lower_face = await draw_material_hatch(
        backend,
        material="steel",
        boundary=[
            [s_left, s_bot],
            [s_right, s_bot],
            [s_right, bore_bot_y],
            [s_left, bore_bot_y],
        ],
    )

    # ── centre lines ────────────────────────────────────────────────────────
    # Front view: the full ISO 128-23 centre lines across the whole gear,
    # overrunning the tip circle by 5 mm, read from the circle itself. They
    # pass through the bore's centre too (the bore is concentric), so they
    # replace the keyed-bore helper's short bore-only pair, which would lie
    # collinear on top of them.
    keyed = gear["keyway"]
    for handle in (keyed.get("centerline_h"), keyed.get("centerline_v")):
        if handle:
            await backend.entity_delete(handle)
    marks = await draw_centre_marks(
        backend, handles=[gear["outer_circle"]], style="lines", extension=5.0
    )
    # Section A-A: the axis, a chain line at the centre height read off the
    # section's own top and bottom edges, overrunning both faces by 5 mm; and
    # the pitch lines ISO 2203 shows on a gear section, at the pitch radius
    # the generator's record carries.
    axis_y = (s_top + s_bot) / 2.0
    axis = await backend.entity_create_line(
        s_left - 5.0, axis_y, s_right + 5.0, axis_y, layer="CENTER"
    )
    axis_ends = [
        await _end(backend, axis.handle, (s_left - 5.0, axis_y)),
        await _end(backend, axis.handle, (s_right + 5.0, axis_y)),
    ]
    pitch_lines = []
    for y in (axis_y + pitch_r, axis_y - pitch_r):
        line = await backend.entity_create_line(s_left - 3.0, y, s_right + 3.0, y, layer="CENTER")
        pitch_lines.append(line.handle)

    # ── ISO 128-40 cutting plane through the front view; viewed from the left,
    #    so the section stands to the right (first-angle projection) ─────────
    cut = await draw_section_line(
        backend,
        (cx, cy - outer_r - 8.0),
        (cx, cy + outer_r + 8.0),
        label="A",
        direction=(1.0, 0.0),
        height=LABEL_H,
    )
    section_cx = (s_left + s_right) / 2.0
    view_label = await backend.entity_create_text(
        f"{cut['plane']['label']}-{cut['plane']['label']}",
        section_cx - LABEL_H,
        s_top + 24.0,
        height=LABEL_H,
        layer="TEXT",
    )

    # ── ISO 129 dimensions ──────────────────────────────────────────────────
    dims: dict[str, str] = {}
    # Pitch diameter, front view, above the gear.
    dims["pitch"] = (
        await backend.dimension_linear(
            cx - pitch_r,
            cy,
            cx + pitch_r,
            cy,
            cx,
            cy + outer_r + 22.0,
            layer="DIM",
            # ISO 129-1: a size across the axis is a diameter and says so.
            text_override="%%c<>",
        )
    ).handle
    # dimension_diameter takes the two ENDS OF A DIAMETER, not the centre and a
    # point on the circle, and its text stands beyond the FIRST point. A real
    # ISO 286 fit: the deviations are looked up exactly as the tool's `fit=`
    # does, never typed.
    bore_fit = fit_lookup("H7", BORE_DIAMETER)
    import math

    # The stacked DIMTOL deviations render with the tolerance precision
    # DIMTDEC, which neither dimstyle_create's whitelist nor the header fold
    # carries - measured: +0.025 printed as "+0.03". A rounded deviation is a
    # misprinted fit, so the deviations are written in the ISO 14405-1
    # bracket form instead, formatted from the fit_lookup result (never typed),
    # while "<>" keeps the measured nominal.
    def _dev(mm: float) -> str:
        return "0" if abs(mm) < 1e-12 else f"{mm:+.3f}"

    bore_text = f"<> {bore_fit.code} {_dev(bore_fit.upper_mm)} / {_dev(bore_fit.lower_mm)}"
    # Lower left, the sheet's emptiest quadrant: the pitch dimension stands
    # above the gear, so no extension line crosses this leader.
    a = math.radians(225.0)
    dims["bore"] = (
        await backend.dimension_diameter(
            cx + bore_r * math.cos(a),
            cy + bore_r * math.sin(a),
            cx - bore_r * math.cos(a),
            cy - bore_r * math.sin(a),
            leader_length=84.0,
            layer="DIM",
            text_override=bore_text,
        )
    ).handle

    # Keyway width and depth, from the keyway polyline's own corners.
    kp = gear["keyway"]["keyway_polyline"]
    k_ul = await _vertex(backend, kp, (cx - KEYWAY_WIDTH / 2, cy + bore_r + KEYWAY_DEPTH))
    k_ur = await _vertex(backend, kp, (cx + KEYWAY_WIDTH / 2, cy + bore_r + KEYWAY_DEPTH))
    dims["keyway_width"] = (
        await backend.dimension_linear(
            k_ul[0], k_ul[1], k_ur[0], k_ur[1], cx, k_ul[1] + 12.0, layer="DIM"
        )
    ).handle
    # DIN 6885 practice dimensions the hub keyway as d + t2: from the bore's
    # far side to the keyway floor.
    bore_far = await backend.point_from_snap(gear["bore"], "quad", cx, cy - bore_r)
    dims["keyway_depth"] = (
        await backend.dimension_linear(
            float(bore_far[0]),
            float(bore_far[1]),
            k_ur[0],
            k_ur[1],
            cx + 32.0,
            cy,
            rotation=90.0,
            layer="DIM",
        )
    ).handle

    # Face width, on the section, above it (below it is the title block).
    dims["face_width"] = (
        await backend.dimension_linear(
            s_left, s_top, s_right, s_top, section_cx, s_top + 10.0, layer="DIM"
        )
    ).handle
    # Outside (tip) diameter, on the section, to its right.
    dims["outside"] = (
        await backend.dimension_linear(
            s_right,
            s_bot,
            s_right,
            s_top,
            s_right + 22.0,
            cy,
            rotation=90.0,
            layer="DIM",
            text_override="%%c<>",
        )
    ).handle

    # ── surface texture on the bore: in the bore void of the section,
    #    its apex standing on the bore surface ──────────────────────────────
    texture = await draw_surface_texture(
        backend,
        at=(s_left + 9.0, bore_bot_y),
        ra=1.6,
        machining="required",
        height=TEXT_H,
    )

    # ── notes, clear of the views ───────────────────────────────────────────
    notes = [
        "GENERAL TOLERANCES ISO 2768-m",
        "UNLESS OTHERWISE STATED: DIMENSIONS IN mm",
        # Sizes belong to the dimensions; a typed "12 x 3.3" here is exactly
        # the fake dimension text the validator flags.
        "HUB KEYWAY TO DIN 6885, BORE TO ISO 286",
    ]
    note_handles = []
    for i, line in enumerate(notes):
        note = await backend.entity_create_text(
            line, 28.0, 30.0 - i * (TEXT_H * 1.7), height=TEXT_H, layer="TEXT"
        )
        note_handles.append(note.handle)

    gear_data = [
        "GEAR DATA",
        f"MODULE m = {GEAR_MODULE:g}",
        f"TEETH z = {GEAR_TEETH}",
        f"PRESSURE ANGLE = {float(meta['pressure_angle']):g}°",
        f"PITCH DIA d = {2 * pitch_r:g}",
        f"TIP DIA da = {2 * outer_r:g}",
        f"ROOT DIA df = {2 * float(meta['root_radius']):g}",
    ]
    for i, line in enumerate(gear_data):
        h = LABEL_H if i == 0 else TEXT_H
        note = await backend.entity_create_text(
            line, 346.0, 200.0 - i * (TEXT_H * 1.9) - (2.0 if i else 0.0), height=h, layer="TEXT"
        )
        note_handles.append(note.handle)

    # ── title block and revision row ────────────────────────────────────────
    await apply_titleblock(
        backend,
        size="A3",
        metadata=TitleBlockMetadata(
            title=f"SPUR GEAR m{GEAR_MODULE:g} z{GEAR_TEETH}",
            drawing_no="AMC-1500-001",
            part_no="1500-001",
            material="C45E",
            scale="1:1",
            drawn_by="autocad-mcp-pro",
            checked_by="drawing_critique",
            date="2026-11-05",
            revision="A",
            # The project's own name, not the module's placeholder company.
            company="autocad-mcp-pro",
        ),
        origin=(0.0, 0.0),
        projection="first",
        frame=False,
    )
    revision = await add_revision(
        backend,
        rev="A",
        description="FIRST ISSUE",
        date="2026-11-05",
        by="autocad-mcp-pro",
    )

    issues = await backend.drawing_critique(focus=None)
    info = await backend.drawing_info()
    return {
        "issues": len(issues),
        "entities": info.entity_count,
        "gear": gear,
        "critique": [issue.to_dict() for issue in issues],
        "section": section,
        "hatches": [upper_face, lower_face],
        "centre_marks": marks,
        "section_axis": {"handle": axis.handle, "ends": axis_ends},
        "section_pitch_lines": pitch_lines,
        "keyway_floor": keyway_floor.handle,
        "face_edges": face_edges,
        "section_plane": cut["plane"],
        "surface_texture": texture,
        "dimensions": dims,
        "bore_fit": bore_fit.to_dict() if hasattr(bore_fit, "to_dict") else str(bore_fit),
        "frame": frame,
        "revision": revision,
        "view_label": view_label.handle,
        "notes": note_handles,
    }


async def finalize(backend) -> dict:
    """What `drawing_finalize` computes: save, validate, critique, score."""
    from engineering import DrawingValidator
    from engineering.scoring import combine

    FINALIZE_DXF.parent.mkdir(parents=True, exist_ok=True)
    await backend.drawing_save_as(str(FINALIZE_DXF), fmt="dxf")
    shared: dict = {}
    result = await DrawingValidator().run(
        backend, expected={"must_have_bore": True, "must_have_keyway": True}, shared=shared
    )
    issues = await backend.drawing_critique(focus=None, shared=shared)
    summary = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        summary[issue.severity] = summary.get(issue.severity, 0) + 1
    return {
        "findings": [
            f.to_dict() if hasattr(f, "to_dict") else str(f)
            for f in result.findings
            if f.severity != "info"
        ],
        "score": combine(result.summary, summary),
        "validator": result.summary,
        "critique": summary,
        "saved": str(FINALIZE_DXF),
    }


async def main(output: Path) -> int:
    os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")
    from backends.ezdxf_backend import EzdxfBackend

    backend = EzdxfBackend()
    await backend.connect()
    try:
        summary = await build(backend)
        if summary["issues"]:
            for row in summary["critique"]:
                print("critique:", row, file=sys.stderr)
        final = await finalize(backend)
        # The sheet, not the extents: the ISO 5457 trimmed sheet is 420 x 297.
        px = RENDER_PX
        while True:
            shot = await backend.view_render(region=[0.0, 0.0, 420.0, 297.0], max_px=px)
            png = shot.get("png")
            if not png or len(png) <= MAX_PNG_BYTES or px <= 2600:
                break
            px = 2600
    finally:
        await backend.disconnect()

    if not png:
        print("render produced no image", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    w, h = shot["px"]
    print(
        f"wrote {output} ({len(png)} bytes, {w}x{h} px) - "
        f"{summary['entities']} entities, {summary['issues']} critique issues, "
        f"finalize score {final['score']}"
    )
    print(f"validator {final['validator']}, critique {final['critique']}, saved {final['saved']}")
    for row in final["findings"]:
        print("validator:", row)
    print(f"bore fit {summary['bore_fit']}")
    print(f"centre marks {summary['centre_marks']}")
    print(f"section axis {summary['section_axis']}")
    print(
        f"hatches {[{k: v for k, v in h.items() if k != 'boundary'} for h in summary['hatches']]}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(build_parser().parse_args().output)))
