"""Exercise tracks B and G once against the live AutoCAD on this machine.

Creates NEW documents (never touches the operator's open drawings), then runs
the whole track through the same code the tools call:

    xref source: drawing_new -> a line -> drawing_export_dwg -> close
    apply_layer_set("mech") -> apply_titleblock (ISO 5457 A3 + ISO 7200)
      -> build_part -> draw_part -> add_view(section) -> dimension_part
      -> insert_std_part (ISO 4014 block + ACADMCP_MECH xdata)
      -> weld_symbol_prims -> extract_records -> rows_from_records
      -> draw_bom_table -> add_balloon
      -> xref_attach / xref_manage("list") / xref_manage("detach")
      -> run_critique(focus=None) -> drawing_export_dwg

and prints what came back as JSON. Exit codes:

    0  the sheet drew, the critique returned nothing and the .dwg exists
    1  it drew but the critique found something or the DWG is missing
    2  no live CAD application reachable (pywin32 missing, or the ProgID
       cannot be created) -- the COM paths then remain fake-tested only

The backend's ``connect()`` is lazy on purpose (the server must start before
AutoCAD is open), so reachability is read from ``system_status()`` rather than
from ``connect()``. Every document this run opens is closed on the way out,
also when a step fails, unless ``--keep`` is given.

    AUTOCAD_MCP_BACKEND=com uv run --frozen python scripts/smoke_mech_com.py
    uv run --frozen python scripts/smoke_mech_com.py --keep   # leave it open
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

XDATA_APP = "ACADMCP_MECH"

#: Every DWG since R13 opens with "AC10xx" (AC1032 for the 2018 format).
DWG_MAGIC = b"AC10"

#: ``RPC_E_CALL_REJECTED``: AutoCAD's message filter refused the call because
#: it was busy (a document still initialising after ``Documents.Add`` is the
#: case measured on this seat). A refused call was not executed.
RPC_E_CALL_REJECTED = -2147418111

#: How long a refused call is retried before the smoke gives up on it.
REJECTED_PATIENCE_S = 90.0

#: Names of the documents this run opened and has not closed yet.
_OPENED: list[str] = []


def _patient(backend) -> None:
    """Retry any backend call AutoCAD refused with ``RPC_E_CALL_REJECTED``.

    Measured on AutoCAD 2026: the first call after ``Documents.Add`` was
    refused, the smoke died, and its cleanup - refused as well - left the
    scratch document open. Refused calls are retried with backoff for up to
    ``REJECTED_PATIENCE_S``; a document created twice by a retried
    ``drawing_new`` is still closed, because ``_new_document`` tracks the
    document list, not a name it assumes.
    """
    run = backend._run

    async def patient(func, *args, **kwargs):
        deadline = time.monotonic() + REJECTED_PATIENCE_S
        pause = 0.5
        while True:
            try:
                return await run(func, *args, **kwargs)
            except RuntimeError as exc:
                cause = exc.__cause__
                hr = cause.args[0] if cause is not None and cause.args else None
                if hr != RPC_E_CALL_REJECTED or time.monotonic() + pause > deadline:
                    raise
                await asyncio.sleep(pause)
                pause = min(pause * 2.0, 5.0)

    backend._run = patient


async def _names(backend) -> set[str]:
    return {row["name"] for row in await backend.document_list()}


async def _new_document(backend) -> str:
    """``drawing_new``, recording every document it added (normally one)."""
    before = await _names(backend)
    await backend.drawing_new()
    added = sorted((await _names(backend)) - before)
    _OPENED.extend(name for name in added if name not in _OPENED)
    return added[-1] if added else ""


async def _close_leftovers(backend) -> None:
    open_names = {row["name"] for row in await backend.document_list()}
    for name in list(_OPENED):
        if name in open_names:
            try:
                await backend.document_close(name, discard=True)
            except Exception as exc:  # best effort on the way out
                print(f"could not close {name}: {exc}", file=sys.stderr)
        _OPENED.remove(name)


async def _xref_source(backend, workspace: Path) -> Path:
    """A one-line DWG in its own scratch document, saved and closed.

    An xref has to be a DWG, and it cannot be the host document itself -
    ``SaveAs`` rebinds the host to the file it wrote.
    """
    created = await _new_document(backend)
    await backend.entity_create_line(0.0, 0.0, 50.0, 0.0, layer="0")
    path = workspace / "xref_source.dwg"
    before = await _names(backend)
    await backend.drawing_export_dwg(str(path), "R2018")
    rebound = await _rebound(backend, created, before)
    await backend.document_close(rebound, discard=True)
    _OPENED.remove(rebound)
    return path


async def _rebound(backend, created: str, before: set[str]) -> str:
    """``SaveAs`` renames the active document to the file it wrote; track that name.

    Read from the document list rather than ``system_status``: the latter
    swallows a refused call and answers without ``active_document``.
    """
    after = await _names(backend)
    renamed = sorted(after - before)
    rebound = renamed[0] if created not in after and len(renamed) == 1 else created
    if created in _OPENED:
        _OPENED.remove(created)
    if rebound not in _OPENED:
        _OPENED.append(rebound)
    return rebound


async def _sheet(backend, workspace: Path, xref_path: Path) -> dict:
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

    created = await _new_document(backend)
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

    records = await extract_records(backend)
    rows = rows_from_records(records)
    tb_x, tb_y = titleblock_origin("A3")
    table = await draw_bom_table(backend, rows, at=(tb_x, tb_y + TB_HEIGHT))
    balloon = await add_balloon(
        backend,
        item=int(rows[0]["item"]),
        at=(250.0, 150.0),
        leader_to=(250.0, 125.0),
        targets=[str(h) for h in rows[0]["handles"]],
    )

    attached = await backend.xref_attach(str(xref_path), (30.0, 30.0), 1.0, 0.0, "attach")
    listed = await backend.xref_manage(attached["name"], "list", None)
    detached = await backend.xref_manage(attached["name"], "detach", None)
    after = await backend.xref_manage(attached["name"], "list", None)

    issues = await run_critique(backend, None)
    dwg_path = workspace / "mech_smoke.dwg"
    before = await _names(backend)
    exported = await backend.drawing_export_dwg(str(dwg_path), "R2018")
    await _rebound(backend, created, before)
    head = dwg_path.read_bytes()[:6] if dwg_path.exists() else b""
    return {
        "part_id": drawn["part_id"],
        "views": len(drawn.get("views") or []) + 1,
        "section_omitted": section.get("omitted"),
        "dimensions": int(dims.get("count") or len(dims.get("handles") or [])),
        "bolt_handle": bolt_handle,
        "xdata_chunks": len(got["xdata"].get(XDATA_APP, [])),
        "parts_list_rows": len(rows),
        "parts_list_representation": table.get("representation"),
        "balloon": balloon.get("handle"),
        "xref_attached": attached.get("name"),
        "xref_listed": len(listed.get("xrefs") or []),
        "xref_detached": bool(detached.get("ok")),
        "xrefs_after_detach": len(after.get("xrefs") or []),
        "dwg": {
            "path": exported.get("path", str(dwg_path)),
            "exists": dwg_path.exists(),
            "bytes": dwg_path.stat().st_size if dwg_path.exists() else 0,
            "magic": head.decode("ascii", "replace"),
        },
        "critique_issues": len(issues),
        "critique": [issue.to_dict() for issue in issues],
    }


async def main(keep_document: bool = False) -> int:
    try:
        from backends.com_backend import ComBackend
    except ImportError as exc:
        print(f"pywin32 not available: {exc}")
        return 2
    backend = ComBackend()
    _patient(backend)
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
        workspace = Path(tempfile.mkdtemp(prefix="mech_smoke_"))
        xref_path = await _xref_source(backend, workspace)
        report = {
            "autocad": status.get("autocad_version") or status.get("cad_progid"),
            **await _sheet(backend, workspace, xref_path),
        }
        print(json.dumps(report, indent=2))
        ok = (
            report["critique_issues"] == 0
            and report["dwg"]["exists"]
            and report["dwg"]["magic"].startswith(DWG_MAGIC.decode())
            and report["xrefs_after_detach"] == 0
        )
        return 0 if ok else 1
    finally:
        if not keep_document:
            await _close_leftovers(backend)
        await backend.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(keep_document="--keep" in sys.argv[1:])))
