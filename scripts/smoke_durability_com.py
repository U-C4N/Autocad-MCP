"""Exercise plan item 1.8-08 (durable checkpoints) against live AutoCAD.

Runs on the live harness (scripts/live/harness.py): attach only, NEW documents
of its own (never the operator's drawings), closed on every path. Everything
goes through the backend members the tools call:

    drawing_new -> entity_create_circle -> checkpoint_create("smoke-1")
      -> checkpoint_list                       (the row, its kb, newest first)
    entity_create_circle -> checkpoint_restore(mode="in_place")
      -> refused, capability checkpoint_restore_in_place, nothing changed
    transaction_begin -> its checkpoint (the cad_batch rollback boundary)
      -> transaction_rollback
    checkpoint_before("drawing_purge") -> drawing_purge  (what the tool does)
    checkpoint_restore("smoke-1", mode="new_document")
      -> <stem>-cp<seq>.dxf opened and bound, holding the one circle

Checks: ``Document.Export`` never renamed the document; the checkpoint lists
newest first with a size; the in-place restore is refused by capability with
the drawing untouched; the transaction boundary and the destructive tool each
left a checkpoint; the new document is the checkpoint, bound, and the
original is still open.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_durability_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, _norm, main_cli  # noqa: E402

LABEL = "smoke-1"


async def _circles(backend) -> int:
    return len(await backend.entity_list(type_filter="CIRCLE", limit=1000))


def assess(h: LiveHarness, report: dict) -> None:
    made = report["created"]
    h.check(
        "checkpoint_create exported the document without renaming it",
        made["label"] == LABEL and made["kb"] > 0 and report["name_after_export"] == report["name"],
        observed={"row": made, "name": report["name"], "after": report["name_after_export"]},
    )
    h.check(
        "checkpoint_list shows it, newest first",
        report["listed"][:1] == [made["cp"]],
        observed=report["listed"],
    )
    refused = report["in_place"]
    h.check(
        "an in-place restore is refused by capability and changes nothing",
        refused["capability"] == "checkpoint_restore_in_place"
        and refused["circles_before"] == refused["circles_after"] == 2,
        expected={"capability": "checkpoint_restore_in_place", "circles": 2},
        observed=refused,
    )
    h.check(
        "the transaction boundary left a checkpoint",
        bool(report["transaction_checkpoint"]),
        observed=report["transaction_checkpoint"],
    )
    h.check(
        "the destructive tool left a checkpoint first",
        report["purge_checkpoint"] is not None and report["purge_kind"] == "destructive",
        observed={"cp": report["purge_checkpoint"], "kind": report["purge_kind"]},
    )
    restored = report["new_document"]
    h.check(
        "new_document opened <stem>-cp<seq>.dxf, bound it, and it holds the checkpoint",
        restored["name_ok"] and restored["bound"] and restored["circles"] == 1,
        observed=restored,
    )


async def body(h: LiveHarness) -> dict:
    from backends.base import UnsupportedCapabilityError
    from backends.checkpoints import open_store

    h.stage("create")
    name = await h.new_document()
    backend = h.backend
    await backend.entity_create_circle(0.0, 0.0, 5.0)
    made = await backend.checkpoint_create(LABEL)
    listed = await backend.checkpoint_list()
    report: dict = {
        "name": name,
        "created": {k: made.get(k) for k in ("cp", "seq", "tool", "rev", "kb", "label")},
        "name_after_export": await h.name_of(name),
        "listed": [row["cp"] for row in listed["checkpoints"]],
    }

    h.stage("in_place")
    await backend.entity_create_circle(20.0, 0.0, 5.0)
    before = await _circles(backend)
    capability = None
    try:
        await backend.checkpoint_restore(made["cp"], mode="in_place")
    except UnsupportedCapabilityError as exc:
        capability = exc.capability
    report["in_place"] = {
        "capability": capability,
        "circles_before": before,
        "circles_after": await _circles(backend),
    }

    h.stage("boundaries")
    begun = await backend.transaction_begin()
    report["transaction_checkpoint"] = begun.get("checkpoint")
    await backend.transaction_rollback()
    purge_cp = await backend.checkpoint_before("drawing_purge")
    await backend.drawing_purge()
    report["purge_checkpoint"] = purge_cp
    store = open_store()
    lineage = (backend._bound_entry() or {}).get("lineage_id")
    report["purge_kind"] = store.resolve(lineage, purge_cp)["kind"] if purge_cp else None

    h.stage("new_document")
    with h.unguarded():
        keys = await h._keys()
        result = await backend.checkpoint_restore(LABEL, mode="new_document")
        opened = await h._adopt(
            keys,
            lambda _name, full: bool(full) and _norm(full) == _norm(result["path"]),
            "checkpoint_restore(new_document)",
        )
    report["new_document"] = {
        "path_name": Path(result["path"]).name,
        "name_ok": Path(result["path"]).name == f"{Path(name).stem}-cp{made['seq']}.dxf",
        "bound": backend.document_identity()["name"] == opened,
        "circles": await _circles(backend),
    }

    h.stage("checks")
    assess(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("durability", body, description=__doc__.split("\n\n")[0]))
