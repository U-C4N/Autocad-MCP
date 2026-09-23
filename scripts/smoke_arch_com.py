"""Exercise track F once against the live AutoCAD on this machine.

Creates a NEW document (never touches the operator's open drawings), then runs
the whole plan through the same code the tools call:

    draw_plan_from_spec(EXAMPLE_SPEC)          (one transaction:)
      drawing_apply_iso_layers("arch") -> draw_walls (junctions, cut
      openings, poche, ACADMCP_ARCH records) -> draw_stair -> label_room x2
      (areas measured off the walls just drawn) -> draw_chains
      -> draw_schedule("doors" / "windows" / "rooms")
    read_plan -> rooms_detect (wall layer) -> run_critique(focus=None)

and prints what came back as JSON. Exit codes:

    0  the plan drew, both rooms read back within 0.1 % of their labels and of
       the hand-computed net floor, and the critique returned nothing
    1  it drew, but a room disagreed or the critique found something
    2  no live CAD application reachable (pywin32 missing, or the ProgID
       cannot be created) -- the COM paths then remain fake-tested only

The backend's ``connect()`` is lazy on purpose (the server must start before
AutoCAD is open), so reachability is read from ``system_status()`` rather than
from ``connect()``. Every document this run opens is closed on the way out,
also when a step fails, unless ``--keep`` is given.

    AUTOCAD_MCP_BACKEND=com uv run --frozen python scripts/smoke_arch_com.py
    uv run --frozen python scripts/smoke_arch_com.py --keep   # leave it open
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The net floor of EXAMPLE_SPEC's two rooms, by hand (see
#: benchmarks/adapters/autocad_mcp_pro.py::ROUNDTRIP_AREAS_MM2).
HAND_AREAS_MM2 = {"01": 4825.0 * 5750.0, "02": 3825.0 * 5750.0}

#: ``RPC_E_CALL_REJECTED``: AutoCAD's message filter refused the call because
#: it was busy (a document still initialising after ``Documents.Add`` is the
#: case measured on this seat by scripts/smoke_mech_com.py). A refused call
#: was not executed.
RPC_E_CALL_REJECTED = -2147418111

#: How long a refused call is retried before the smoke gives up on it.
REJECTED_PATIENCE_S = 90.0

#: Names of the documents this run opened and has not closed yet.
_OPENED: list[str] = []


def _patient(backend) -> None:
    """Retry any backend call AutoCAD refused with ``RPC_E_CALL_REJECTED``."""
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


def _inside(point, loop) -> bool:
    from engineering.arch.critique import point_in_loop

    return point_in_loop(point, loop)


async def _plan(backend) -> dict:
    from engineering.arch.draw import read_plan
    from engineering.arch.layers import ARCH_ROLE_LAYER
    from engineering.arch.rooms import rooms_detect
    from engineering.arch.spec import EXAMPLE_SPEC, draw_plan_from_spec
    from engineering.critique import run_critique

    await _new_document(backend)
    drawn = await draw_plan_from_spec(backend, EXAMPLE_SPEC)
    plan = await read_plan(backend)
    detected = await rooms_detect(backend, layers=[ARCH_ROLE_LAYER["wall"]])
    faces = list(detected.get("rooms") or [])
    rooms = {}
    for room in plan["rooms"]:
        face = next((f for f in faces if _inside(room.at, f["loop"])), None)
        found = float(face["area"]) if face is not None else 0.0
        hand = HAND_AREAS_MM2.get(str(room.number), 0.0)
        rooms[str(room.number)] = {
            "label_mm2": round(float(room.area), 3),
            "detected_mm2": round(found, 3),
            "hand_mm2": hand,
            "agrees": face is not None
            and abs(found - float(room.area)) <= 1e-3 * float(room.area)
            and abs(found - hand) <= 1e-3 * hand,
        }
    issues = await run_critique(backend, None)
    return {
        "walls": drawn["walls"],
        "openings": drawn["openings"],
        "omitted": drawn["omitted"],
        "stairs": len(drawn["stairs"]),
        "schedules": [
            {
                "kind": item.get("kind"),
                "representation": item.get("representation"),
                "rows": item.get("row_count"),
            }
            for item in drawn["schedules"]
        ],
        "rooms": rooms,
        "faces_detected": len(faces),
        "confidence_min": detected.get("confidence_min"),
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
        report = {
            "autocad": status.get("autocad_version") or status.get("cad_progid"),
            **await _plan(backend),
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        ok = (
            report["critique_issues"] == 0
            and set(report["rooms"]) == set(HAND_AREAS_MM2)
            and all(row["agrees"] for row in report["rooms"].values())
        )
        return 0 if ok else 1
    finally:
        if not keep_document:
            await _close_leftovers(backend)
        await backend.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(keep_document="--keep" in sys.argv[1:])))
