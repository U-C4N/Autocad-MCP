"""Exercise track F once against the live AutoCAD on this machine.

Runs on the live harness (scripts/live/harness.py): attach only, a NEW
document of its own (never the operator's drawings), closed on every path.
The whole plan goes through the same code the tools call:

    draw_plan_from_spec(EXAMPLE_SPEC)          (one transaction:)
      drawing_apply_iso_layers("arch") -> draw_walls (junctions, cut
      openings, poche, ACADMCP_ARCH records) -> draw_stair -> label_room x2
      (areas measured off the walls just drawn) -> draw_chains
      -> draw_schedule("doors" / "windows" / "rooms")
    read_plan -> rooms_detect (wall layer) -> run_critique(focus=None)

Checks: nothing the spec asked for was omitted; both rooms were labelled and
each reads back - as the face its label point lies in - within 0.1 % of its
label and of the hand-computed net floor; the three schedules were drawn; the
critique found nothing.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_arch_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402

#: The net floor of EXAMPLE_SPEC's two rooms, by hand (see
#: benchmarks/adapters/autocad_mcp_pro.py::ROUNDTRIP_AREAS_MM2).
HAND_AREAS_MM2 = {"01": 4825.0 * 5750.0, "02": 3825.0 * 5750.0}

#: The schedules EXAMPLE_SPEC asks for.
SCHEDULES = ("doors", "rooms", "windows")


def _inside(point, loop) -> bool:
    from engineering.arch.critique import point_in_loop

    return point_in_loop(point, loop)


def assess(h: LiveHarness, report: dict) -> None:
    h.check("nothing omitted", not report["omitted"], expected=[], observed=report["omitted"])
    h.check(
        "both rooms labelled",
        set(report["rooms"]) == set(HAND_AREAS_MM2),
        expected=sorted(HAND_AREAS_MM2),
        observed=sorted(report["rooms"]),
    )
    for number, row in sorted(report["rooms"].items()):
        h.check(
            f"room {number} reads back within 0.1 % of its label and the hand floor",
            bool(row["agrees"]),
            expected={"label_mm2": row["label_mm2"], "hand_mm2": row["hand_mm2"]},
            observed=row["detected_mm2"],
        )
    kinds = sorted(item["kind"] for item in report["schedules"])
    h.check(
        "the three schedules drawn", kinds == list(SCHEDULES), expected=SCHEDULES, observed=kinds
    )
    h.check(
        "the critique found nothing",
        report["critique_issues"] == 0,
        expected=0,
        observed=report["critique"],
    )


async def body(h: LiveHarness) -> dict:
    from engineering.arch.draw import read_plan
    from engineering.arch.layers import ARCH_ROLE_LAYER
    from engineering.arch.rooms import rooms_detect
    from engineering.arch.spec import EXAMPLE_SPEC, draw_plan_from_spec
    from engineering.critique import run_critique

    h.stage("plan")
    await h.new_document()
    backend = h.backend
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
    report = {
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
    h.stage("checks")
    assess(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("arch", body, description=__doc__.split("\n\n")[0]))
