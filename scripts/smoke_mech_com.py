"""Exercise tracks B and G once against the live AutoCAD on this machine.

Runs on the live harness (scripts/live/harness.py): attach only, NEW
documents of its own (never the operator's drawings), each closed on every
path. The whole track goes through the same code the tools call:

    xref source: drawing_new -> a line -> drawing_export_dwg -> close
    apply_layer_set("mech") -> apply_titleblock (ISO 5457 A3 + ISO 7200)
      -> build_part -> draw_part -> add_view(section) -> dimension_part
      -> insert_std_part (ISO 4014 block + ACADMCP_MECH xdata)
      -> weld_symbol_prims -> extract_records -> rows_from_records
      -> draw_bom_table -> add_balloon
      -> xref_attach / xref_manage("list") / xref_manage("detach")
      -> run_critique(focus=None) -> drawing_export_dwg

Checks: the section view omitted nothing; the part was dimensioned; the bolt
carries its ACADMCP_MECH XDATA; the parts list read off the drawing is the
bolt (the shaft is lines, not a block) and was drawn as a native TABLE; the
balloon was placed; the xref was listed once attached and gone once detached;
the DWG is on disk and opens with the "AC10" magic; the critique found nothing.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_mech_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402

XDATA_APP = "ACADMCP_MECH"

#: Every DWG since R13 opens with "AC10xx" (AC1032 for the 2018 format).
DWG_MAGIC = "AC10"

#: The parts list of the sheet below. A parts-list record is read off a block
#: reference (``extract_records``), so the drawn shaft - lines, not a block -
#: has none: one row, the ISO 4014 bolt.
PARTS_LIST_ROWS = 1


async def _xref_source(h: LiveHarness, workspace: Path) -> Path:
    """A one-line DWG in its own document, saved and closed.

    An xref has to be a DWG, and it cannot be the host document itself -
    ``SaveAs`` rebinds the host to the file it wrote.
    """
    name = await h.new_document()
    await h.backend.entity_create_line(0.0, 0.0, 50.0, 0.0, layer="0")
    path = workspace / "xref_source.dwg"
    await h.backend.drawing_export_dwg(str(path), "R2018")
    await h.close_document(name)
    return path


def assess(h: LiveHarness, report: dict) -> None:
    h.check(
        "the section view omitted nothing",
        not report["section_omitted"],
        expected=[],
        observed=report["section_omitted"],
    )
    h.check("the part was dimensioned", report["dimensions"] > 0, observed=report["dimensions"])
    h.check(
        "the bolt carries ACADMCP_MECH XDATA",
        report["xdata_chunks"] >= 1,
        expected=">= 1",
        observed=report["xdata_chunks"],
    )
    h.check(
        "the parts list read off the drawing is the bolt",
        report["parts_list_rows"] == PARTS_LIST_ROWS
        and report["bolt_handle"] in report["parts_list_handles"],
        expected={"rows": PARTS_LIST_ROWS, "handle": report["bolt_handle"]},
        observed={"rows": report["parts_list_rows"], "handles": report["parts_list_handles"]},
    )
    h.check(
        "the parts list drawn as a native TABLE",
        report["parts_list_representation"] == "native",
        expected="native",
        observed=report["parts_list_representation"],
    )
    h.check("the balloon placed", bool(report["balloon"]), observed=report["balloon"])
    h.check(
        "the xref listed once attached",
        report["xref_listed"] == 1,
        expected=1,
        observed=report["xref_listed"],
    )
    h.check(
        "the xref gone once detached",
        report["xref_detached"] and report["xrefs_after_detach"] == 0,
        expected=0,
        observed=report["xrefs_after_detach"],
    )
    dwg = report["dwg"]
    h.check(
        "the DWG is on disk with the AC10 magic",
        dwg["exists"] and dwg["magic"].startswith(DWG_MAGIC),
        expected=DWG_MAGIC,
        observed=dwg["magic"],
    )
    h.check(
        "the critique found nothing",
        report["critique_issues"] == 0,
        expected=0,
        observed=report["critique"],
    )


async def body(h: LiveHarness) -> dict:
    from engineering.critique import run_critique
    from engineering.layers import apply_layer_set
    from engineering.mech.annotate import weld_symbol_prims
    from engineering.mech.draw import add_view, dimension_part, draw_part, draw_prims
    from engineering.mech.part import build_part
    from engineering.mech.stdparts import insert_std_part
    from engineering.sheet.bom import (
        add_balloon,
        draw_bom_table,
        extract_records,
        rows_from_records,
    )
    from engineering.sheet.titleblock import (
        TB_HEIGHT,
        TitleBlockMetadata,
        apply_titleblock,
        titleblock_origin,
    )

    workspace = Path(tempfile.mkdtemp(prefix="mech_smoke_"))
    h.stage("xref source")
    xref_path = await _xref_source(h, workspace)

    h.stage("sheet")
    await h.new_document()
    backend = h.backend
    await apply_layer_set(backend, "mech")
    await apply_titleblock(
        backend,
        size="A3",
        metadata=TitleBlockMetadata(title="COM SMOKE", drawing_no="SMOKE-001", scale="1:1"),
        projection="first",
        frame=True,
        zones=True,
        marks=True,
    )
    shaft = build_part(
        {
            "kind": "revolved",
            "name": "SHAFT",
            "material": "steel",
            "segments": [
                {"length": 40.0, "d_outer": 30.0},
                {"length": 20.0, "d_outer": 50.0, "d_inner": 20.0},
            ],
        }
    )
    drawn = await draw_part(backend, shaft, at=(125.0, 170.0), views=("front",), dimension=False)
    section = await add_view(
        backend,
        drawn["part_id"],
        "section",
        plane={"p1": [0.0, 0.0], "p2": [60.0, 0.0], "label": "A"},
        style="full",
    )
    dims = await dimension_part(backend, drawn["part_id"], style="chain")
    bolt = await insert_std_part(backend, "ISO 4014 - M12x60", at=(250.0, 120.0), view="side")
    bolt_handle = bolt.get("handle") or (bolt.get("handles") or [None])[0]
    got = await backend.entity_get_xdata(bolt_handle, XDATA_APP)
    await draw_prims(backend, weld_symbol_prims(kind="fillet", size=5.0), at=(60.0, 100.0))

    h.stage("parts list")
    records = await extract_records(backend)
    rows = rows_from_records(records)
    tb_x, tb_y = titleblock_origin("A3")
    table = await draw_bom_table(backend, rows, at=(tb_x, tb_y + TB_HEIGHT))
    balloon = await add_balloon(
        backend,
        item=int(rows[0]["item"]),
        at=(250.0, 150.0),
        leader_to=(250.0, 125.0),
        targets=[str(handle) for handle in rows[0]["handles"]],
    )

    h.stage("xref")
    attached = await backend.xref_attach(str(xref_path), (30.0, 30.0), 1.0, 0.0, "attach")
    listed = await backend.xref_manage(attached["name"], "list", None)
    detached = await backend.xref_manage(attached["name"], "detach", None)
    after = await backend.xref_manage(attached["name"], "list", None)

    h.stage("critique and DWG")
    issues = await run_critique(backend, None)
    dwg_path = workspace / "mech_smoke.dwg"
    exported = await backend.drawing_export_dwg(str(dwg_path), "R2018")
    head = dwg_path.read_bytes()[:6] if dwg_path.exists() else b""
    report = {
        "part_id": drawn["part_id"],
        "views": len(drawn.get("views") or []) + 1,
        "section_omitted": section.get("omitted"),
        "dimensions": int(dims.get("count") or len(dims.get("handles") or [])),
        "bolt_handle": bolt_handle,
        "xdata_chunks": len(got["xdata"].get(XDATA_APP, [])),
        "parts_list_rows": len(rows),
        "parts_list_handles": sorted({str(h) for row in rows for h in row["handles"]}),
        "parts_list_representation": table.get("representation"),
        "balloon": balloon.get("handle"),
        "xref_attached": attached.get("name"),
        "xref_listed": len(listed.get("xrefs") or []),
        "xref_detached": bool(detached.get("ok")),
        "xrefs_after_detach": len(after.get("xrefs") or []),
        "dwg": {
            "name": Path(exported.get("path", str(dwg_path))).name,
            "exists": dwg_path.exists(),
            "bytes": dwg_path.stat().st_size if dwg_path.exists() else 0,
            "magic": head.decode("ascii", "replace"),
        },
        "critique_issues": len(issues),
        "critique": [issue.to_dict() for issue in issues],
    }
    h.stage("checks")
    assess(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("mech", body, description=__doc__.split("\n\n")[0]))
