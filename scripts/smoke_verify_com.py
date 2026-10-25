"""Exercise the 1.7.0 verification path once against the live AutoCAD on this machine.

Runs on the live harness (scripts/live/harness.py): attach only, two NEW
documents of its own (never the operator's drawings), closed on every path.
Through the same code the tools call:

1. **The bound document and its refusal (B11).** Two own documents A and B;
   the session is bound to B. A is made active behind the session's back (what
   an operator's tab click does - here on the run's own document), and a write
   is refused with ``active_document_changed`` naming both; nothing lands in A
   or B and the revision does not move. ``document_activate(B)`` binds again.
2. **HATCH loops and DIMENSION read-back (B16, B31).** A 100 x 60 hatch with a
   Ø20 island built from two true arcs: ``entity_get`` reads two loops and a
   filled area of 6000 - 100 pi from the drawing's export. A linear and a
   diameter DIMENSION read back their type, defpoints and measurement.
3. **Verification on the live document (B22).** Ø20 bound to the circle and to
   its diameter dimension: ``verify_requirements`` passes it, measured on the
   geometry, with the dimension's evidence agreeing.
4. **Export delivery (B21, B23).** ``deliver_drawing(formats=["dxf"])`` writes
   the DXF through ``Document.Export`` (never ``SaveAs``): the open document
   keeps its name and path, the reopen parity holds and the requirement is
   accepted on the delivered file.
5. **Saved-artifact verification (B21).** ``verify_artifact`` measures Ø20
   again on the delivered DXF (re-bound from the live document, nothing
   changed); on a copy of it whose circle was edited to Ø22 it fails with 22
   measured; and on a DWG written by ``drawing_export_dwg`` - read through a
   private copy AutoCAD opens read-only - it passes again.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_verify_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402

#: The hatch: a 100 x 60 rectangle with a Ø20 island centred at (30, 30).
RECT = [(0.0, 0.0), (100.0, 0.0), (100.0, 60.0), (0.0, 60.0)]
HOLE_CENTRE = (30.0, 30.0)
HOLE_RADIUS = 10.0
FILLED_AREA = 100.0 * 60.0 - math.pi * HOLE_RADIUS**2
AREA_TOL = 1e-6 * FILLED_AREA
EPS = 1e-6


def _requirements(circle: str, dimension: str):
    """Ø20 twice: R1 bound to the circle by handle, R2 through its diameter
    DIMENSION (bound to the geometry the dimension's defpoints lie on, its
    text and own measurement judged against that geometry)."""
    from engineering.verification.requirements import parse_requirements

    def item(rid: str, selector: dict) -> dict:
        return {
            "id": rid,
            "quantity": "diameter",
            "target": {"nominal": 2 * HOLE_RADIUS, "unit": "mm"},
            "features": [selector],
            "provenance": "explicit",
            "source": {"document": "smoke_verify_com synthetic sheet"},
        }

    return parse_requirements(
        {
            "units": "mm",
            "items": [
                item("R1", {"kind": "handle", "handle": circle}),
                item("R2", {"kind": "dimension", "handle": dimension}),
            ],
        }
    )


def _row(report: dict, rid: str) -> dict:
    row = next(item for item in report["rows"] if item["id"] == rid)
    measured = (row.get("measured") or {}).get("value")
    dimension = row.get("dimension") or {}
    return {
        "status": row["status"],
        "measured": measured,
        "handles": row.get("handles"),
        "dimension_mismatch": dimension.get("mismatch"),
        "dimension_measurement": dimension.get("dimension_measurement"),
    }


def _passes(row: dict, value: float = 2 * HOLE_RADIUS) -> bool:
    return (
        row["status"] == "pass"
        and row["measured"] is not None
        and abs(row["measured"] - value) <= EPS
    )


def _find(app, name: str):
    """One of the run's own documents by name (COM thread)."""
    for index in range(int(app.Documents.Count)):
        doc = app.Documents.Item(index)
        if str(doc.Name) == name:
            return doc
    raise LookupError(f"{name} is not open")


async def _bound_refusal(h: LiveHarness) -> dict:
    from backends.document_guard import ActiveDocumentChangedError

    backend = h.backend
    a = await h.new_document()
    await backend.entity_create_line(0.0, 0.0, 10.0, 0.0, layer="0")
    b = await h.new_document()
    bound = backend.document_identity()
    h.check("drawing_new binds the new document", bound["name"] == b, expected=b, observed=bound)

    def counts(app):
        return {name: int(_find(app, name).ModelSpace.Count) for name in (a, b)}

    before = await h.raw(counts)
    # What an operator's click on another tab does - here on the run's own A.
    await h.raw(lambda app: _find(app, a).Activate())
    refusal: dict | None = None
    try:
        await backend.entity_create_line(0.0, 0.0, 20.0, 0.0, layer="0")
    except ActiveDocumentChangedError as exc:
        refusal = exc.to_dict()
    after = await h.raw(counts)
    h.check(
        "a write after the switch is refused as active_document_changed",
        refusal is not None and refusal.get("kind") == "active_document_changed",
        expected="active_document_changed",
        observed=refusal,
    )
    h.check(
        "the refusal names the bound and the active document",
        refusal is not None
        and (refusal.get("bound") or {}).get("name") == b
        and (refusal.get("active") or {}).get("name") == a,
        expected={"bound": b, "active": a},
        observed=refusal,
    )
    h.check("nothing landed in either document", after == before, expected=before, observed=after)
    h.check(
        "the refused write moved no revision",
        backend.document_identity()["revision"] == bound["revision"],
        expected=bound["revision"],
        observed=backend.document_identity()["revision"],
    )
    activated = await backend.document_activate(b)
    rebound = backend.document_identity()
    h.check(
        "document_activate binds the session again, under the same id",
        activated["active"] == b and rebound["document_id"] == bound["document_id"],
        expected=bound["document_id"],
        observed=rebound["document_id"],
    )
    await h.close_document(a)
    return {"documents": [a, b], "refusal": refusal, "counts": after}


async def _read_back(h: LiveHarness) -> dict:
    backend = h.backend
    hatch = await backend.entity_create_hatch("ANSI31", RECT)
    cx, cy = HOLE_CENTRE
    island = [
        {
            "type": "arc",
            "center": [cx, cy],
            "radius": HOLE_RADIUS,
            "start_angle": 0.0,
            "end_angle": 180.0,
        },
        {
            "type": "arc",
            "center": [cx, cy],
            "radius": HOLE_RADIUS,
            "start_angle": 180.0,
            "end_angle": 360.0,
        },
    ]
    await backend.hatch_add_boundary(hatch.handle, island)
    circle = await backend.entity_create_circle(cx, cy, HOLE_RADIUS, layer="0")
    linear = await backend.dimension_linear(0.0, 0.0, 100.0, 0.0, 50.0, -15.0)
    diameter = await backend.dimension_diameter(
        cx - HOLE_RADIUS, cy, cx + HOLE_RADIUS, cy, leader_length=15.0
    )

    hatch_info = (await backend.entity_get(hatch.handle)).properties
    area = hatch_info.get("area")
    h.check(
        "the HATCH is read from the drawing's export",
        hatch_info.get("detail_source") == "dxf_export",
        expected="dxf_export",
        observed=hatch_info.get("detail_source") or hatch_info.get("detail_unavailable"),
    )
    h.check(
        "the HATCH reads two loops",
        hatch_info.get("loop_count") == 2,
        expected=2,
        observed=hatch_info.get("loop_count"),
    )
    h.check(
        "the HATCH fills 6000 - 100 pi",
        area is not None and abs(float(area) - FILLED_AREA) <= AREA_TOL,
        expected=round(FILLED_AREA, 6),
        observed=area,
    )
    dims = {}
    expected_points = {
        # linear: defpoint2 / defpoint3 are the two measured points
        "linear": {(1, (0.0, 0.0)), (2, (100.0, 0.0))},
        # diameter: defpoint and defpoint4 are the two chord ends, either way round
        "diameter": {(0, (cx + HOLE_RADIUS, cy)), (3, (cx - HOLE_RADIUS, cy))},
    }
    for label, handle, measure in (
        ("linear", linear.handle, 100.0),
        ("diameter", diameter.handle, 2 * HOLE_RADIUS),
    ):
        fields = (await backend.entity_get(handle)).properties.get("dimension") or {}
        dims[label] = fields
        h.check(
            f"the {label} DIMENSION reads back its type",
            fields.get("dimtype") == label,
            expected=label,
            observed=fields.get("dimtype"),
        )
        value = fields.get("measurement")
        h.check(
            f"the {label} DIMENSION reads back its measurement",
            value is not None and abs(float(value) - measure) <= EPS,
            expected=measure,
            observed=value,
        )
        points = fields.get("defpoints") or []
        wanted = expected_points[label]
        got = {
            (index, (round(points[index][0], 6), round(points[index][1], 6)))
            for index, _xy in wanted
            if index < len(points) and points[index] is not None
        }
        if label == "diameter" and got != wanted:  # the chord ends may come back swapped
            got = {(3 - index, xy) for index, xy in got}
        h.check(
            f"the {label} DIMENSION reads back its defpoints",
            got == wanted,
            expected=sorted(wanted),
            observed=points,
        )
    return {
        "hatch": hatch.handle,
        "circle": circle.handle,
        "linear": linear.handle,
        "diameter": diameter.handle,
        "hatch_area": area,
        "hatch_loops": hatch_info.get("loop_count"),
        "dimensions": dims,
    }


async def _live_verification(h: LiveHarness, rs) -> dict:
    from engineering.verification.report import verify_requirements

    report = await verify_requirements(h.backend, rs)
    by_handle, by_dimension = _row(report, "R1"), _row(report, "R2")
    h.check(
        "Ø20 passes on the live document, measured on the circle",
        _passes(by_handle),
        expected={"status": "pass", "measured": 20.0},
        observed=by_handle,
    )
    h.check(
        "Ø20 passes through its diameter DIMENSION, which agrees with the geometry",
        _passes(by_dimension) and by_dimension["dimension_mismatch"] is False,
        expected={"status": "pass", "measured": 20.0, "dimension_mismatch": False},
        observed=by_dimension,
    )
    h.check("the live report is accepted", report["accepted"] is True, observed=report["status"])
    return {
        "rows": {"R1": by_handle, "R2": by_dimension},
        "status": report["status"],
        "accepted": report["accepted"],
    }


async def _delivery(h: LiveHarness, rs, workspace: Path) -> dict:
    from engineering.delivery import deliver_drawing
    from engineering.understand.snapshot import dxf_version_of

    def identity(app):
        doc = app.ActiveDocument
        return {"name": str(doc.Name), "full_name": str(doc.FullName), "saved": bool(doc.Saved)}

    before = await h.raw(identity)
    out = workspace / "delivery"
    result = await deliver_drawing(h.backend, out, formats=["dxf"], requirements=rs)
    after = await h.raw(identity)
    payload = result.to_dict()
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    dxf = next(item for item in payload["artifacts"] if item["format"] == "dxf")
    delivered = out / dxf["filename"]
    export = manifest.get("dxf_export") or {}
    h.check(
        "the delivery DXF is written through Document.Export",
        export.get("method") == "Document.Export" and export.get("repointed") is False,
        expected={"method": "Document.Export", "repointed": False},
        observed=export,
    )
    h.check(
        "the open document is not re-pointed or saved",
        after == before,
        expected=before,
        observed=after,
    )
    version = dxf_version_of(delivered)
    h.check(
        "the delivered DXF is on disk as R2018",
        delivered.is_file() and version == "AC1032",
        expected="AC1032",
        observed=version,
    )
    h.check("the reopen parity holds", payload["parity"]["ok"] is True, observed=payload["parity"])
    acceptance = payload.get("geometry_acceptance") or {}
    h.check(
        "the requirements are accepted on the delivered file",
        acceptance.get("accepted") is True,
        observed=acceptance,
    )
    return {
        "status": payload["status"],
        "blocking": payload.get("blocking"),
        "dxf_export": export,
        "parity_ok": payload["parity"]["ok"],
        "acceptance": acceptance,
        "delivered": delivered,
    }


async def _artifacts(h: LiveHarness, rs, delivered: Path, workspace: Path) -> dict:
    import ezdxf

    from engineering.verification.artifact import verify_artifact

    backend = h.backend
    on_dxf = await verify_artifact(str(delivered), rs, backend=backend)
    rows = {rid: _row(on_dxf, rid) for rid in ("R1", "R2")}
    h.check(
        "Ø20 passes again on the delivered DXF, by handle and by dimension",
        all(_passes(row) for row in rows.values()) and on_dxf["accepted"] is True,
        expected={"status": "pass", "measured": 20.0},
        observed=rows,
    )
    comparison = on_dxf.get("comparison") or []
    h.check(
        "the before / after comparison shows no change",
        len(comparison) == 2 and not any(item["changed"] for item in comparison),
        observed=comparison,
    )

    edited = workspace / "edited.dxf"
    shutil.copyfile(delivered, edited)
    doc = ezdxf.readfile(str(edited))
    circles = [
        e
        for e in doc.modelspace().query("CIRCLE")
        if abs(e.dxf.center.x - HOLE_CENTRE[0]) <= EPS
        and abs(e.dxf.center.y - HOLE_CENTRE[1]) <= EPS
    ]
    for circle in circles:
        circle.dxf.radius = HOLE_RADIUS + 1.0
    doc.saveas(str(edited))
    on_edit = await verify_artifact(str(edited), rs, backend=backend)
    edit_row = _row(on_edit, "R1")
    h.check(
        "an artifact edited to Ø22 fails, measured on the file",
        len(circles) == 1
        and edit_row["status"] == "fail"
        and abs((edit_row["measured"] or 0.0) - 22.0) <= EPS
        and on_edit["accepted"] is False,
        expected={"status": "fail", "measured": 22.0, "accepted": False},
        observed={"row": edit_row, "accepted": on_edit["accepted"]},
    )

    dwg = workspace / "verify_smoke.dwg"
    await backend.drawing_export_dwg(str(dwg), "R2018")
    on_dwg = await verify_artifact(str(dwg), rs, backend=backend)
    dwg_rows = {rid: _row(on_dwg, rid) for rid in ("R1", "R2")}
    read = (on_dwg.get("artifact") or {}).get("read")
    h.check(
        "Ø20 passes on the DWG, read through a private read-only copy",
        all(_passes(row) for row in dwg_rows.values())
        and read == "private copy, Documents.Open read-only + Document.Export",
        expected={"status": "pass", "measured": 20.0},
        observed={"rows": dwg_rows, "read": read},
    )
    return {
        "dxf": {"rows": rows, "rebinding": on_dxf.get("rebinding"), "comparison": comparison},
        "edited": {"row": edit_row, "accepted": on_edit["accepted"]},
        "dwg": {"rows": dwg_rows, "read": read, "rebinding": on_dwg.get("rebinding")},
    }


def assess(h: LiveHarness, report: dict) -> None:
    """Nothing is left to assess afterwards: every check is made inside its
    step, against the seat state that step just read."""


async def body(h: LiveHarness) -> dict:
    workspace = Path(tempfile.mkdtemp(prefix="acadmcp_verify_smoke_"))
    report: dict = {}
    h.stage("bound document")
    report["bound"] = await _bound_refusal(h)
    h.stage("hatch and dimension read-back")
    drawn = await _read_back(h)
    report["read_back"] = drawn
    rs = _requirements(drawn["circle"], drawn["diameter"])
    h.stage("live verification")
    report["live"] = await _live_verification(h, rs)
    h.stage("export delivery")
    delivery = await _delivery(h, rs, workspace)
    delivered = delivery.pop("delivered")
    report["delivery"] = delivery
    h.stage("saved-artifact verification")
    report["artifacts"] = await _artifacts(h, rs, delivered, workspace)
    assess(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("verify", body, description=__doc__.split("\n\n")[0]))
