"""Exercise plan item 1.8-06 (in-place edits, dimension tags) against live AutoCAD.

Runs on the live harness (scripts/live/harness.py): attach only, a NEW
document of its own (never the operator's drawings), closed on every path.
Everything goes through the backend members the tools call:

    layer_create(GEOMETRY) -> entity_create_polyline(4 vertices, xdata)
      -> entity_set_vertices(same count, moved)          (in place)
      -> entity_set_vertices(6 vertices, two bulges)     (spike S4)
      -> entity_measure -> entity_get_xdata
    entity_create_text -> entity_edit_text(x, y)
    entity_create_mtext -> entity_edit_geometry(x, y)
    dimension_linear(xdata) / entity_create_table(xdata) -> entity_get_xdata
    draw_part(dimension=True) -> every DIMENSION tagged role "dim"

Checks: the same-count write kept the handle; the 4 -> 6 write left a polyline
with the six vertices, the two bulges, its layer and its XDATA - in place, or
replaced and said so (``replaced: [{old, new}]``); the perimeter is the
arc-exact one; both labels moved without changing handle; the dimension and the
table carry the tag written in their create call; every mechanical dimension
is tagged. The report's ``s4`` block records which way AutoCAD answered the
``Coordinates`` write with a different vertex count - the measurement spike S4
left pending - so ``docs/analysis/spikes-1.8.0.json`` can be filled from it.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_edit_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402

APP = "ACADMCP_MECH"
TAG = {APP: ['{"feature":"","kind":"feature","part":"SMOKE","v":1,"view":"front"}']}
FOUR = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]
SIX = [[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [20.0, 30.0], [0.0, 20.0], [0.0, 10.0]]
SIX_BULGES = [1.0, 0.0, 0.5, 0.0, 0.0, 0.0]
TOL = 1e-6

PLATE = {
    "kind": "prismatic",
    "name": "plate",
    "material": "aluminium",
    "thickness": 8.0,
    "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
    "features": [
        {"kind": "hole", "id": "h1", "x": 20.0, "y": 25.0, "diameter": 10.0},
        {"kind": "hole", "id": "h2", "x": 60.0, "y": 25.0, "diameter": 10.0},
    ],
}


def _arc_length(p1, p2, bulge: float) -> float:
    chord = math.dist(p1, p2)
    if bulge == 0.0:
        return chord
    sweep = 4.0 * math.atan(abs(bulge))
    return chord / (2.0 * math.sin(sweep / 2.0)) * sweep


def _perimeter(points, bulges) -> float:
    pairs = list(zip(points, points[1:] + points[:1], strict=True))
    return sum(_arc_length(a, b, bulges[i]) for i, (a, b) in enumerate(pairs))


def _same_points(got, want) -> bool:
    return len(got) == len(want) and all(
        abs(float(g[0]) - w[0]) <= TOL and abs(float(g[1]) - w[1]) <= TOL
        for g, w in zip(got, want, strict=True)
    )


async def _xdata(backend, handle):
    raw = await backend.entity_get_xdata(handle, APP)
    return (raw.get("xdata") or {}).get(APP) or []


def assess(h: LiveHarness, report: dict) -> None:
    same = report["same_count"]
    h.check("a same-count vertex write keeps the handle", same["handle_kept"], observed=same)
    six = report["four_to_six"]
    h.check(
        "the 4 -> 6 write left six vertices and two bulges",
        six["points_ok"] and six["bulges_ok"],
        expected={"points": SIX, "bulges": SIX_BULGES},
        observed={"points": six["points"], "bulges": six["bulges"]},
    )
    h.check(
        "the 4 -> 6 write kept the handle or said it replaced it",
        six["handle_kept"] or six["replaced"] == [{"old": six["old"], "new": six["handle"]}],
        observed={"handle": six["handle"], "replaced": six["replaced"]},
    )
    h.check(
        "the rewritten polyline kept its layer and XDATA",
        six["layer"] == "GEOMETRY" and six["xdata"] == TAG[APP],
        expected={"layer": "GEOMETRY", "xdata": TAG[APP]},
        observed={"layer": six["layer"], "xdata": six["xdata"]},
    )
    h.check(
        "the perimeter is the arc-exact one",
        abs(six["perimeter"] - six["perimeter_hand"]) <= 1e-6 * six["perimeter_hand"],
        expected=six["perimeter_hand"],
        observed=six["perimeter"],
    )
    for key in ("text", "mtext"):
        row = report[key]
        h.check(
            f"the {key.upper()} moved in place",
            row["handle_kept"] and _same_points([row["insertion"]], [row["target"]]),
            expected=row["target"],
            observed=row,
        )
    h.check(
        "the dimension carries the tag of its create call",
        report["dimension_xdata"] == TAG[APP],
        observed=report["dimension_xdata"],
    )
    h.check(
        "the table carries the tag of its create call",
        report["table_xdata"] == TAG[APP],
        observed=report["table_xdata"],
    )
    h.check(
        "every mechanical dimension is tagged role dim",
        report["mech_dimensions"] > 0 and report["mech_roles"] == ["dim"],
        observed={"dimensions": report["mech_dimensions"], "roles": report["mech_roles"]},
    )


async def body(h: LiveHarness) -> dict:
    from engineering.mech.draw import draw_part
    from engineering.mech.part import build_part
    from engineering.mech.xdata import decode

    h.stage("polyline")
    await h.new_document()
    backend = h.backend
    await backend.layer_create(name="GEOMETRY")
    pline = await backend.entity_create_polyline(FOUR, True, layer="GEOMETRY", xdata=TAG)
    moved = [[x + 5.0, y] for x, y in FOUR]
    same = await backend.entity_set_vertices(pline.handle, moved)
    report: dict = {
        "same_count": {
            "handle_kept": same.handle == pline.handle,
            "points_ok": _same_points(same.properties.get("points") or [], moved),
        }
    }

    six = await backend.entity_set_vertices(same.handle, SIX, SIX_BULGES)
    replaced = six.properties.get("replaced") or []
    measured = await backend.entity_measure(six.handle)
    report["four_to_six"] = {
        "old": same.handle,
        "handle": six.handle,
        "handle_kept": six.handle == same.handle,
        "replaced": replaced,
        "points": six.properties.get("points"),
        "bulges": six.properties.get("bulges"),
        "points_ok": _same_points(six.properties.get("points") or [], SIX),
        "bulges_ok": [round(float(b), 9) for b in six.properties.get("bulges") or []] == SIX_BULGES,
        "layer": six.layer,
        "xdata": await _xdata(backend, six.handle),
        "perimeter": float(measured.get("perimeter") or 0.0),
        "perimeter_hand": _perimeter(SIX, SIX_BULGES),
    }
    # The S4 answer, in the shape docs/analysis/spikes-1.8.0.json records.
    report["s4"] = {
        "question": "AcadLWPolyline.Coordinates with a different vertex count",
        "answer": "replaced" if replaced else "in_place",
    }

    h.stage("labels")
    text = await backend.entity_create_text("SMOKE", 1.0, 2.0)
    text_moved = await backend.entity_edit_text(text.handle, x=50.0, y=60.0)
    report["text"] = {
        "handle_kept": text_moved.handle == text.handle,
        "insertion": text_moved.properties.get("insertion"),
        "target": [50.0, 60.0],
    }
    mtext = await backend.entity_create_mtext("smoke note", 0.0, 0.0)
    mtext_moved = await backend.entity_edit_geometry(mtext.handle, x=-5.0, y=12.5)
    report["mtext"] = {
        "handle_kept": mtext_moved.handle == mtext.handle,
        "insertion": mtext_moved.properties.get("insertion"),
        "target": [-5.0, 12.5],
    }

    h.stage("tags")
    dim = await backend.dimension_linear(0.0, -20.0, 40.0, -20.0, 20.0, -30.0, xdata=TAG)
    report["dimension_xdata"] = await _xdata(backend, dim.handle)
    table = await backend.entity_create_table(100.0, 0.0, [["A", "1"]], ["K", "V"], xdata=TAG)
    report["table_xdata"] = await _xdata(backend, table.handle)

    before = {d.handle for d in await backend.entity_list(type_filter="DIMENSION", limit=1000)}
    await draw_part(backend, build_part(PLATE), at=(0.0, 200.0))
    dims = [
        d
        for d in await backend.entity_list(type_filter="DIMENSION", limit=1000)
        if d.handle not in before
    ]
    roles = set()
    for d in dims:
        values = await _xdata(backend, d.handle)
        roles.add(decode(values).get("role") if values else None)
    report["mech_dimensions"] = len(dims)
    report["mech_roles"] = sorted(str(r) for r in roles)

    h.stage("checks")
    assess(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("edit", body, description=__doc__.split("\n\n")[0]))
