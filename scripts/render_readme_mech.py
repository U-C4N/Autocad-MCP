"""Render the mechanical sheet the README shows, using only the server's own tools.

An A3 flange-coupling sheet, drawn from part models rather than lines:

* the HUB - a ``RevolvedPart`` (a 70 mm boss, an 18 mm flange of 140 mm, a
  40 mm bore through both and a step fillet) - drawn by ``mech_part_draw`` as
  its end view, with the full section A-A beside it through ``mech_view_add``
  (hatched by the view engine). The plain front view is not drawn: it would
  only repeat the section. The four M12 clearance holes are a polar
  ``mech_hole_pattern`` (with its centre marks) on the end view - see HOLES.
* the SHAFT the hub sits on - a second ``RevolvedPart`` carrying the DIN 6885
  12 x 8 keyway (the part model's keyway is a feature of an outside face, so
  the key seat is drawn where the model can draw it: on the shaft) - with its
  end view;
* every dimension through the server's ``dimension_linear`` /
  ``dimension_diameter`` / ``dimension_radius`` tools, each diameter on its
  own station off the part, plus ``dimension_auto`` on the key seat's width;
  the bore at ISO 286 H7 and the shaft at k6 (written as the class, see
  BORE_FIT); ISO 21920-1 surface-texture symbols on the bore and on the seat;
* four ISO 4014 M12 x 60 bolts, each with an ISO 4032 M12 nut, inserted as
  blocks by ``std_part_insert`` (each with its ACADMCP_MECH XDATA), the
  ISO 7573 parts list *read off the drawing* (``bom_extract``) and drawn by
  ``bom_table``, ISO 6433 balloons linked to the inserts by XDATA
  (``balloon_add``);
* an ISO 5457 A3 frame with zones and an ISO 7200 title block
  (``titleblock_apply``), a revision row (``revision_add``), and a dimension
  style sized for A3 - 3.5 mm text and arrows - through ``dimstyle_create``.
  (DIMSCALE is not used: headlessly ezdxf renders from the dimstyle entry.)

Nothing the view engine drew is edited afterwards (the model-drift check
refuses that); the dimensions are placed by reading the drawn entities back,
clear of the axis lines' overrun.

The script ends on the gates the README quotes: ``drawing_critique(focus=None)``
must be empty (the script exits 1 and prints the issues otherwise), and
``drawing_finalize`` - the server's own tool - saves the sheet under ``out/``
and reports the score. The picture is ``view_render(region=<the A3 sheet>,
max_px=3200)``, dropped to 2600 px if the PNG would pass 700 KB.

Reproduce:

    AUTOCAD_MCP_BACKEND=ezdxf uv run --frozen python scripts/render_readme_mech.py
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "assets" / "mech-showcase.png"
SAVE_PATH = ROOT / "out" / "readme-mech" / "flange_coupling.dxf"

PNG_LIMIT = 700 * 1024
SHEET = [0.0, 0.0, 420.0, 297.0]  # A3 landscape, the region rendered

#: A3 lettering: ISO 3098 row 3.5 for dimensions, arrows matched to the text.
DIMSTYLE = "ISO-A3"
DIM_OVERRIDES = {
    "DIMTXT": 3.5,
    "DIMASZ": 3.5,
    "DIMEXE": 2.0,
    "DIMEXO": 1.0,
    "DIMGAP": 1.0,
    "DIMDSEP": ".",
    "DIMTFAC": 0.7,
}

BORE = 40.0
BOSS_D, BOSS_L = 70.0, 52.0
FLANGE_D, FLANGE_L = 140.0, 18.0
FILLET_R = 3.0
PCD = 110.0
HOLE_D = 13.5  # ISO 273 medium clearance for M12
HUB = {
    "kind": "revolved",
    "name": "HUB",
    "material": "steel",
    "segments": [
        {"length": BOSS_L, "d_outer": BOSS_D, "d_inner": BORE},
        {"length": FLANGE_L, "d_outer": FLANGE_D, "d_inner": BORE},
    ],
    "features": [
        {"kind": "fillet", "id": "r_step", "at": "step:0", "radius": FILLET_R},
    ],
}
#: The four M12 clearance holes, on the pitch circle at 45 degrees so the axial
#: section A-A runs between them. They are drawn by ``mech_hole_pattern`` on the
#: end view, not as the model's ``hole_circle``: that feature has no cut
#: geometry, so in a section it draws the holes' hidden and centre lines over
#: the hatch at the full pitch radius whatever the angle - and the drift check
#: (rightly) refuses any hand edit of the lines the model drew.
HOLES = {"count": 4, "diameter": HOLE_D, "pcd": PCD, "start_angle": 45.0}

SHAFT_D, SHAFT_L = BORE, 90.0
KEY_L, KEY_OFFSET = 45.0, 10.0
#: ISO 286 tolerance classes. They are written as the class alone ("Ø40 H7"),
#: which ISO 286-1 / ISO 14405-1 accept: the dimension text's deviations are
#: formatted by DIMTDEC, which the dimstyle whitelist does not carry, and at
#: two places the H7 upper deviation +0.025 would be shown as +0.03 - a
#: tolerance wider than the class. The deviations are reported by fit_lookup.
BORE_FIT, SHAFT_FIT = "H7", "k6"
SHAFT = {
    "kind": "revolved",
    "name": "SHAFT",
    "material": "steel",
    "segments": [{"length": SHAFT_L, "d_outer": SHAFT_D}],
    "features": [
        # DIN 6885-1 for a 40 mm shaft (12 x 8, t1 = 5) from engineering.keyway.
        {
            "kind": "keyway",
            "id": "key",
            "segment": 0,
            "length": KEY_L,
            "offset": KEY_OFFSET,
            "from_end": "right",
        },
    ],
}

#: Placement (WCS, mm). The frame runs (20, 10)-(410, 287); the title block
#: fills (230, 10)-(410, 70), the parts list grows up from y = 70 and the
#: revision block hangs from the frame's top-right corner.
HUB_AT = (32.0, 115.0)  # lower-left of the hub's end view frame
SECTION_GAP = 24.0  # room for the two diameters left of the section
SHAFT_AT = (105.0, 34.0)  # lower-left of the shaft's front view frame
FASTENER_X = 331.0  # bolt datum (under the head) of each bolt-and-nut pair
FASTENER_Y = (240.0, 212.0, 184.0, 156.0)
NUT_OFFSET = 46.0  # nut datum past the bolt datum: on the thread
LABEL_HEIGHT = 5.0  # ISO 3098 row 5 for the view labels on A3


def _deviation(code: str, nominal: float) -> str:
    from engineering.fits import fit_lookup

    dev = fit_lookup(code, nominal)
    return f"{dev.upper_mm:+.3f} / {dev.lower_mm:+.3f}"


def _box(record: dict) -> list[float]:
    return [round(float(v), 1) for v in record["bbox"]]


async def _drawn(backend, handles) -> list:
    return [await backend.entity_get(h) for h in handles]


def _line_ends(ent):
    (sx, sy), (ex, ey) = ent.properties["start"][:2], ent.properties["end"][:2]
    return float(sx), float(sy), float(ex), float(ey)


class _Ctx:
    """The lifespan context the server's tools read the backend from."""

    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def warning(self, message):
        pass

    async def info(self, message):
        pass

    async def debug(self, message):
        pass

    async def report_progress(self, *args, **kwargs):
        pass


async def build(backend) -> dict:
    import server
    from engineering.mech.annotate import draw_surface_texture
    from engineering.mech.draw import add_view, draw_part
    from engineering.mech.marks import draw_section_line
    from engineering.mech.part import build_part
    from engineering.mech.stdparts import insert_std_part
    from engineering.sheet.bom import (
        add_balloon,
        draw_bom_table,
        extract_records,
        rows_from_records,
    )
    from engineering.sheet.revision import add_revision
    from engineering.sheet.titleblock import (
        TB_HEIGHT,
        TitleBlockMetadata,
        apply_titleblock,
        titleblock_origin,
    )
    from engineering.standards.dimstyles import ISO_25

    ctx = _Ctx(backend)

    async def dim_linear(p1, p2, at, *, vertical: bool, **kw):
        return await server.dimension_linear(
            p1[0],
            p1[1],
            p2[0],
            p2[1],
            at[0],
            at[1],
            rotation=90.0 if vertical else 0.0,
            layer="DIM",
            ctx=ctx,
            **kw,
        )

    await backend.drawing_new()
    await backend.drawing_plan(
        "Flange coupling: hub (end view, section A-A), shaft with DIN 6885 keyway "
        "(front and end view), M12 fasteners, ISO 7573 parts list",
        sheet_size="A3",
        scale=1.0,
        layer_set_id="mech",
        view_count=4,
        dim_style="chain",
    )
    await backend.drawing_apply_iso_layers("mech")
    await backend.drawing_settings({"units": "mm", "linear_precision": 2})
    await backend.dimstyle_create(DIMSTYLE, {**ISO_25, **DIM_OVERRIDES}, set_current=True)

    # -- sheet: ISO 5457 frame with zones + ISO 7200 title block + revision row
    await apply_titleblock(
        backend,
        size="A3",
        metadata=TitleBlockMetadata(
            title="FLANGE COUPLING",
            drawing_no="AMC-1800-010",
            part_no="1800-010",
            material="S235JR",
            scale="1:1",
            drawn_by="autocad-mcp-pro",
            checked_by="drawing_critique",
            date="2026-11-05",
            revision="A",
            company="autocad-mcp-pro",
        ),
        projection="first",
        frame=True,
        zones=True,
        marks=True,
    )
    await add_revision(backend, rev="A", description="FIRST ISSUE", date="2026-11-05", by="AMP")

    # ---------------------------------------------------------------- the hub
    hub = await draw_part(backend, build_part(HUB), at=HUB_AT, views=("side",), dimension=False)
    hub_id = hub["part_id"]
    section = await add_view(
        backend,
        hub_id,
        "section",
        side="left",
        plane={"p1": [0.0, 0.0], "p2": [BOSS_L + FLANGE_L, 0.0], "label": "A"},
        style="full",
        gap=SECTION_GAP,
    )
    sec_ents = await _drawn(backend, section["handles"])
    geo = [e for e in sec_ents if e.type == "LINE" and e.layer == "GEOMETRY"]
    xs = [v for e in geo for v in (_line_ends(e)[0], _line_ends(e)[2])]
    ys = [v for e in geo for v in (_line_ends(e)[1], _line_ends(e)[3])]
    sx_lo, sx_hi = min(xs), max(xs)
    axis = (min(ys) + max(ys)) / 2.0
    # which end is the boss: the end face whose outline reaches only the boss
    left_top = max(
        max(_line_ends(e)[1], _line_ends(e)[3])
        for e in geo
        if abs(_line_ends(e)[0] - sx_lo) < 1e-6 and abs(_line_ends(e)[2] - sx_lo) < 1e-6
    )
    boss_left = left_top < axis + FLANGE_D / 2.0 - 1.0
    s = 1.0 if boss_left else -1.0
    x_boss = sx_lo if boss_left else sx_hi  # the boss's end face
    x_face = sx_hi if boss_left else sx_lo  # the flange's outer face
    x_step = x_face - s * FLANGE_L  # the flange's inner face

    hidden_in_section = sum(1 for e in sec_ents if e.layer == "HIDDEN")

    # diameters, each on its own station off the part
    r_b, r_f, r_i = BOSS_D / 2.0, FLANGE_D / 2.0, BORE / 2.0
    sec_dims = []
    sec_dims.append(
        await dim_linear(
            (x_boss, axis - r_i),
            (x_boss, axis + r_i),
            (x_boss - s * 14.0, axis),
            vertical=True,
            text_override=f"Ø<> {BORE_FIT}",
        )
    )
    sec_dims.append(
        await dim_linear(
            (x_boss, axis - r_b),
            (x_boss, axis + r_b),
            (x_boss - s * 32.0, axis),
            vertical=True,
            text_override="Ø<>",
        )
    )
    sec_dims.append(
        await dim_linear(
            (x_face, axis - r_f),
            (x_face, axis + r_f),
            (x_face + s * 18.0, axis),
            vertical=True,
            text_override="Ø<>",
        )
    )
    # lengths below the section: the flange's thickness, then the overall
    sec_dims.append(
        await dim_linear(
            (x_step, axis - r_f),
            (x_face, axis - r_f),
            (x_face, axis - r_f - 10.0),
            vertical=False,
        )
    )
    sec_dims.append(
        await dim_linear(
            (x_boss, axis - r_b),
            (x_face, axis - r_f),
            (x_face, axis - r_f - 20.0),
            vertical=False,
        )
    )
    # the step fillet: a real radius dimension on the ARC the engine drew. Its
    # line runs from the arc through the corner of the cut face, and its text
    # stands in the open bore, under the bore line - never on the hatch.
    fillet = next(
        e for e in sec_ents if e.type == "ARC" and float(e.properties["center"][1]) > axis
    )
    fcx, fcy = (float(v) for v in fillet.properties["center"][:2])
    fr = float(fillet.properties["radius"])
    a0 = float(fillet.properties["start_angle"]) % 360.0
    a1 = float(fillet.properties["end_angle"]) % 360.0
    span = (a1 - a0) % 360.0
    target = 300.0 if boss_left else 240.0  # 30 degrees off the boss's generator
    if (target - a0) % 360.0 > span:
        target = a0 + span / 2.0
    ang = math.radians(target)
    text_y = axis + r_i - 8.0
    reach = (fcy - text_y) / -math.sin(ang)
    fillet_dim = await server.dimension_radius(
        fcx,
        fcy,
        fcx + fr * math.cos(ang),
        fcy + fr * math.sin(ang),
        leader_length=reach - fr,
        layer="DIM",
        ctx=ctx,
    )
    # Ra 1.6 on the bore, standing on the lower bore line inside the cavity
    await draw_surface_texture(
        backend,
        at=(x_boss + s * 18.0, axis - r_i),
        ra=1.6,
        machining="required",
        height=3.5,
    )

    # the end view: the bolt circle, the cutting plane and the labels
    end_ents = await _drawn(backend, hub["handles"])
    outer = max(
        (e for e in end_ents if e.type == "CIRCLE" and e.layer == "GEOMETRY"),
        key=lambda e: float(e.properties["radius"]),
    )
    ecx, ecy = (float(v) for v in outer.properties["center"][:2])
    pattern = await server.mech_hole_pattern(
        "polar",
        ecx,
        ecy,
        HOLES["diameter"],
        count=HOLES["count"],
        pcd=HOLES["pcd"],
        start_angle=HOLES["start_angle"],
        centre_marks=True,
        ctx=ctx,
    )
    # the pitch circle itself: a thin chain line (ISO 128-20 type 04.1)
    await backend.entity_create_circle(ecx, ecy, PCD / 2.0, layer="CENTER")
    holes = [
        e
        for e in await backend.entity_list(
            type_filter="CIRCLE", layer_filter="GEOMETRY", limit=1000
        )
        if abs(float(e.properties["radius"]) - HOLE_D / 2.0) < 1e-6
        and abs(math.dist(e.properties["center"][:2], (ecx, ecy)) - PCD / 2.0) < 1e-6
    ]
    top_right = max(holes, key=lambda e: e.properties["center"][0] + e.properties["center"][1])
    hx, hy = (float(v) for v in top_right.properties["center"][:2])
    hr = float(top_right.properties["radius"])
    ha = math.atan2(hy - ecy, hx - ecx)
    await server.dimension_diameter(
        hx + hr * math.cos(ha),
        hy + hr * math.sin(ha),
        hx - hr * math.cos(ha),
        hy - hr * math.sin(ha),
        leader_length=24.0,
        layer="DIM",
        text_override=f"{len(holes)}x <>",
        ctx=ctx,
    )
    # the pitch circle at 240 degrees: off both centre lines, 15 degrees clear
    # of the holes, its text beyond the flange's outline
    pa = math.radians(240.0)
    rp = PCD / 2.0
    await server.dimension_diameter(
        ecx + rp * math.cos(pa),
        ecy + rp * math.sin(pa),
        ecx - rp * math.cos(pa),
        ecy - rp * math.sin(pa),
        leader_length=30.0,
        layer="DIM",
        text_override="PCD <>",
        ctx=ctx,
    )
    await draw_section_line(
        backend,
        (ecx, ecy + FLANGE_D / 2.0 + 10.0),
        (ecx, ecy - FLANGE_D / 2.0 - 10.0),
        label="A",
        direction=(1.0, 0.0),
        height=LABEL_HEIGHT,
    )
    sec_box = section["view"]["bbox"]
    # the view label: above the view, over the boss (the flange reaches up to
    # the revision block, so there is no room over its middle)
    await backend.entity_create_text(
        "A-A", x_boss + s * 2.0, axis + r_f - LABEL_HEIGHT, LABEL_HEIGHT, layer="TEXT"
    )

    # -------------------------------------------------------------- the shaft
    shaft = await draw_part(
        backend, build_part(SHAFT), at=SHAFT_AT, views=("front", "side"), dimension=False
    )
    sh_ents = await _drawn(backend, shaft["handles"])
    front_ents = [e for e in sh_ents if e.handle in set(shaft["handles"])]
    sh_box = shaft["views"][0]["bbox"]
    sgeo = [
        e
        for e in front_ents
        if e.type == "LINE"
        and e.layer == "GEOMETRY"
        and sh_box[0] <= min(_line_ends(e)[0], _line_ends(e)[2])
        and max(_line_ends(e)[0], _line_ends(e)[2]) <= sh_box[2]
        and sh_box[1] <= min(_line_ends(e)[1], _line_ends(e)[3])
        and max(_line_ends(e)[1], _line_ends(e)[3]) <= sh_box[3]
    ]
    fx_lo = min(min(_line_ends(e)[0], _line_ends(e)[2]) for e in sgeo)
    fx_hi = max(max(_line_ends(e)[0], _line_ends(e)[2]) for e in sgeo)
    fy_lo = min(min(_line_ends(e)[1], _line_ends(e)[3]) for e in sgeo)
    fy_hi = max(max(_line_ends(e)[1], _line_ends(e)[3]) for e in sgeo)
    f_axis = (fy_lo + fy_hi) / 2.0
    # the key seat's ends, read off the outline: the top edge breaks there
    top_pts = sorted(
        {
            round(x, 6)
            for e in sgeo
            for x, y in ((_line_ends(e)[0], _line_ends(e)[1]), (_line_ends(e)[2], _line_ends(e)[3]))
            if abs(y - fy_hi) < 1e-6
        }
    )
    key_lo, key_hi = fx_hi - KEY_OFFSET - KEY_L, fx_hi - KEY_OFFSET
    if not any(abs(x - key_lo) < 1e-3 for x in top_pts):
        key_lo, key_hi = fx_lo + KEY_OFFSET, fx_lo + KEY_OFFSET + KEY_L
    shaft_dims = []
    shaft_dims.append(
        await dim_linear((fx_lo, fy_hi), (key_lo, fy_hi), (fx_lo, fy_hi + 20.0), vertical=False)
    )
    shaft_dims.append(
        await dim_linear((key_lo, fy_hi), (key_hi, fy_hi), (key_lo, fy_hi + 20.0), vertical=False)
    )
    shaft_dims.append(
        await dim_linear((fx_lo, fy_hi), (fx_hi, fy_hi), (fx_lo, fy_hi + 30.0), vertical=False)
    )
    shaft_dims.append(
        await dim_linear(
            (fx_hi, fy_lo),
            (fx_hi, fy_hi),
            (fx_hi + 13.0, f_axis),
            vertical=True,
            text_override=f"Ø<> {SHAFT_FIT}",
        )
    )
    # Ra 0.8 on the seat, standing on the top generator left of the key seat,
    # under the dimension rows
    await draw_surface_texture(
        backend,
        at=((fx_lo + key_lo) / 2.0 - 6.0, fy_hi),
        ra=0.8,
        machining="required",
        height=3.5,
    )

    # the shaft's end view: key seat width (dimension_auto) and d - t1
    side_box = shaft["views"][1]["bbox"]
    side_ents = [
        e
        for e in sh_ents
        if e.type == "LINE"
        and e.layer == "GEOMETRY"
        and all(
            side_box[0] <= px <= side_box[2] and side_box[1] <= py <= side_box[3]
            for px, py in (
                (_line_ends(e)[0], _line_ends(e)[1]),
                (_line_ends(e)[2], _line_ends(e)[3]),
            )
        )
    ]
    floor = [
        e
        for e in side_ents
        if abs(_line_ends(e)[1] - _line_ends(e)[3]) < 1e-6
        and abs(abs(_line_ends(e)[2] - _line_ends(e)[0]) - 12.0) < 1e-6
    ]
    key_auto = await backend.dimension_auto(handles=[e.handle for e in floor], layer="DIM")
    shaft_circle = next(
        e
        for e in sh_ents
        if e.type == "CIRCLE"
        and side_box[0] <= float(e.properties["center"][0]) <= side_box[2]
        and side_box[1] <= float(e.properties["center"][1]) <= side_box[3]
    )
    scx, scy = (float(v) for v in shaft_circle.properties["center"][:2])
    if floor:
        fl = floor[0]
        flx = min(_line_ends(fl)[0], _line_ends(fl)[2])
        fly = _line_ends(fl)[1]
        shaft_dims.append(
            await dim_linear(
                (scx, scy - SHAFT_D / 2.0),
                (flx, fly),
                (scx - SHAFT_D / 2.0 - 10.0, scy),
                vertical=True,
            )
        )

    # view labels
    await backend.entity_create_text(
        "SHAFT", (fx_lo + fx_hi) / 2.0 - 9.0, fy_hi + 38.0, LABEL_HEIGHT, layer="TEXT"
    )

    # -- fasteners: four ISO 4014 bolts, each with its ISO 4032 nut on the thread
    bolts, nuts = [], []
    for y in FASTENER_Y:
        bolts.append(
            await insert_std_part(backend, "ISO 4014 - M12x60", at=(FASTENER_X, y), view="side")
        )
        nuts.append(
            await insert_std_part(
                backend, "ISO 4032 - M12", at=(FASTENER_X + NUT_OFFSET, y), view="side"
            )
        )

    # -- the parts list is a read of the drawing, not a list typed beside it
    records = await extract_records(backend)
    rows = rows_from_records(records)
    tb_x, tb_y = titleblock_origin("A3")
    await draw_bom_table(backend, rows, at=(tb_x, tb_y + TB_HEIGHT))
    for row in rows:
        targets = [str(h) for h in row.get("handles") or ()]
        is_bolt = "4014" in str(row.get("designation", ""))
        y = FASTENER_Y[0]
        x = FASTENER_X + (25.0 if is_bolt else NUT_OFFSET + 5.0)
        await add_balloon(
            backend,
            item=int(row["item"]),
            at=(x + 10.0, y + 16.0),
            leader_to=(x, y + (2.0 if is_bolt else 6.0)),
            targets=targets,
            radius=5.0,
        )

    issues = await backend.drawing_critique(focus=None)
    info = await backend.drawing_info()
    return {
        "issues": len(issues),
        "issue_list": [issue.to_dict() for issue in issues],
        "entities": info.entity_count,
        "views": {
            "hub": [_box(r) for r in hub["views"]] + [_box(section["view"])],
            "shaft": [_box(r) for r in shaft["views"]],
        },
        "section_box": [round(v, 1) for v in sec_box],
        "hidden_in_section": hidden_in_section,
        "hole_pattern": {k: pattern.get(k) for k in ("count", "handles") if k in pattern},
        "omitted": hub["omitted"] + section["omitted"] + shaft["omitted"],
        "dimensions": {
            "hub_section": len(sec_dims) + (1 if fillet_dim else 0),
            "hub_end_view": 2,
            "shaft": len(shaft_dims),
            "dimension_auto": len(key_auto),
        },
        "fits": {
            f"bore {BORE:g} {BORE_FIT}": _deviation(BORE_FIT, BORE),
            f"shaft {SHAFT_D:g} {SHAFT_FIT}": _deviation(SHAFT_FIT, SHAFT_D),
        },
        "parts_list": [
            {k: row.get(k) for k in ("item", "qty", "designation", "standard")} for row in rows
        ],
        "inserts": len(bolts) + len(nuts),
    }


async def render(output: Path) -> dict:
    os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")
    import server
    from backends.ezdxf_backend import EzdxfBackend

    backend = EzdxfBackend()
    await backend.connect()
    try:
        summary = await build(backend)
        SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        final = await server.drawing_finalize(save_path=str(SAVE_PATH), ctx=_Ctx(backend))
        png, px = None, (0, 0)
        for size in (3200, 2600):
            shot = await backend.view_render(region=SHEET, max_px=size)
            png, px = shot["png"], shot["px"]
            if png and len(png) <= PNG_LIMIT:
                break
    finally:
        await backend.disconnect()
    if not png:
        raise RuntimeError("view_render returned no PNG (is matplotlib installed?)")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    return {
        "output": str(output),
        "bytes": len(png),
        "px": f"{px[0]}x{px[1]}",
        "saved": str(SAVE_PATH),
        "finalize_score": final["score"],
        "finalize_critique": len(final["critique"]),
        "finalize_findings": final.get("findings"),
        **summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = asyncio.run(render(args.output))
    for key, value in report.items():
        print(f"{key}: {value}")
    return 0 if report["issues"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
