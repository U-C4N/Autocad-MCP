"""Measure the live engine's bound-document guard (B11) once against the live AutoCAD.

Runs on the live harness (scripts/live/harness.py): attach only, two NEW
documents of its own (never the operator's drawings), closed on every path.
Through the real ``ComBackend``:

1. **Unsaved documents.** ``drawing_new`` twice: each document's ``FullName``
   is ``''`` (so its key is ``(Name, '')`` and ``path`` is None), and the two
   keys - and ids - differ.
2. **The guard.** Bound to B, A is activated through COM behind the session's
   back (what an operator's tab click does - here on the run's own document).
   The next write is refused with ``active_document_changed`` naming both
   (A under the id it was bound with before), nothing lands, the revision does
   not move, ``view_screenshot`` is refused too and ``system_status`` reports
   the switch.
3. **``_reactivate_bound_sync``.** It makes B active again (``Documents.Item``
   by index, ``Activate``) and answers True, then False once B already is; the
   next guarded call proceeds.
4. **``document_activate``.** It binds A under its known id, a write there
   moves A's revision only, and B is bound again under its own id.
5. **The application identity.** ``Application.HWND`` is an ``int`` and the
   same on a second ``GetActiveObject`` proxy; after the cached proxy is
   dropped (what the RPC-disconnect path does) the re-attached session keeps
   B's id and revision.
6. **A DWG snapshot hands the seat back.** ``take_snapshot`` of a copy of B's
   DWG opens it read-only, exports, closes it and re-activates B in the same
   job: B is active afterwards and the next call is not refused.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_document_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402


def _find(app, name: str):
    """One of the run's own documents by name (COM thread)."""
    for index in range(int(app.Documents.Count)):
        doc = app.Documents.Item(index)
        if str(doc.Name) == name:
            return doc
    raise LookupError(f"{name} is not open")


def _active_key(app) -> list[str]:
    doc = app.ActiveDocument
    return [str(doc.Name), str(doc.FullName)]


async def _unsaved(h: LiveHarness) -> dict:
    backend = h.backend
    a = await h.new_document()
    id_a = backend.document_identity()
    b = await h.new_document()
    id_b = backend.document_identity()
    keys = await h.raw(lambda app: {n: str(_find(app, n).FullName) for n in (a, b)})
    h.check(
        "an unsaved document's FullName is ''",
        keys == {a: "", b: ""},
        expected={a: "", b: ""},
        observed=keys,
    )
    h.check(
        "its identity reports no path",
        id_a["path"] is None and id_b["path"] is None,
        expected=None,
        observed=[id_a["path"], id_b["path"]],
    )
    h.check(
        "two unsaved documents are two keys and two ids",
        a != b and id_a["document_id"] != id_b["document_id"] and id_b["name"] == b,
        expected="distinct",
        observed={"names": [a, b], "ids": [id_a["document_id"], id_b["document_id"]]},
    )
    return {"a": a, "b": b, "id_a": id_a, "id_b": id_b}


async def _guard(h: LiveHarness, docs: dict) -> dict:
    from backends.document_guard import ActiveDocumentChangedError

    backend = h.backend
    a, b = docs["a"], docs["b"]
    await backend.entity_create_line(0.0, 0.0, 10.0, 0.0, layer="0")
    bound = backend.document_identity()
    h.check("a write in B moves B's revision", bound["revision"] == 1, observed=bound)

    def counts(app):
        return {name: int(_find(app, name).ModelSpace.Count) for name in (a, b)}

    before = await h.raw(counts)
    await h.raw(lambda app: _find(app, a).Activate())
    refusal = None
    try:
        await backend.entity_create_line(0.0, 0.0, 20.0, 0.0, layer="0")
    except ActiveDocumentChangedError as exc:
        refusal = exc.to_dict()
    after = await h.raw(counts)
    h.check(
        "the next write is refused as active_document_changed",
        refusal is not None and refusal["kind"] == "active_document_changed",
        expected="active_document_changed",
        observed=refusal,
    )
    h.check(
        "the refusal names B as bound and A, under its known id, as active",
        refusal is not None
        and refusal["bound"]["name"] == b
        and refusal["bound"]["document_id"] == docs["id_b"]["document_id"]
        and refusal["active"]["name"] == a
        and refusal["active"]["document_id"] == docs["id_a"]["document_id"]
        and refusal["application_changed"] is False,
        expected={"bound": b, "active": a, "application_changed": False},
        observed=refusal,
    )
    h.check("nothing landed in either document", after == before, expected=before, observed=after)
    h.check(
        "the refused write moved no revision",
        backend.document_identity() == bound,
        expected=bound,
        observed=backend.document_identity(),
    )
    screenshot = None
    try:
        await backend.view_screenshot()
    except ActiveDocumentChangedError as exc:
        screenshot = exc.to_dict()["kind"]
    h.check(
        "view_screenshot is refused too",
        screenshot == "active_document_changed",
        expected="active_document_changed",
        observed=screenshot,
    )
    status = await backend.system_status()
    h.check(
        "system_status reports the switch",
        status.get("active_document_changed") is True
        and (status.get("bound_document") or {}).get("name") == b
        and status.get("active_document") == a,
        observed={
            key: status.get(key)
            for key in ("active_document", "bound_document", "active_document_changed")
        },
    )
    return {"bound": bound, "refusal": refusal, "counts": after}


async def _reactivate(h: LiveHarness, docs: dict) -> dict:
    backend = h.backend
    b = docs["b"]
    first = await h.raw(backend._reactivate_bound_sync)
    active = await h.raw(_active_key)
    h.check(
        "_reactivate_bound_sync makes the bound document active again",
        first is True and active[0] == b,
        expected=[True, b],
        observed=[first, active[0]],
    )
    again = await h.raw(backend._reactivate_bound_sync)
    h.check("it answers False once it already is", again is False, observed=again)
    clayer = await backend.system_get_variable("CLAYER")
    h.check("the next guarded call proceeds", clayer == "0", expected="0", observed=clayer)
    return {"first": first, "again": again, "active": active}


async def _activate(h: LiveHarness, docs: dict) -> dict:
    backend = h.backend
    a, b = docs["a"], docs["b"]
    b_before = backend.document_identity()
    await backend.document_activate(a)
    on_a = backend.document_identity()
    h.check(
        "document_activate binds A under its known id",
        on_a["name"] == a and on_a["document_id"] == docs["id_a"]["document_id"],
        expected=docs["id_a"]["document_id"],
        observed=on_a,
    )
    await backend.entity_create_line(0.0, 5.0, 10.0, 5.0, layer="0")
    moved = backend.document_identity()["revision"] - on_a["revision"]
    h.check("a write in A moves A's revision by one", moved == 1, observed=moved)
    await backend.document_activate(b)
    on_b = backend.document_identity()
    h.check(
        "B is bound again under its own id and revision",
        on_b["document_id"] == b_before["document_id"] and on_b["revision"] == b_before["revision"],
        expected=b_before,
        observed=on_b,
    )
    return {"a": on_a, "b": on_b}


async def _application(h: LiveHarness) -> dict:
    from backends import com_backend as cb

    backend = h.backend
    client = h._client
    progid = h.progid

    def _probe(app):
        other = client.GetActiveObject(progid)
        hwnd, again = app.HWND, other.HWND
        same = cb._app_identity(app) == cb._app_identity(other)
        cb._app_identity(app)  # leave the cache on the harness's proxy
        del other
        return {
            "hwnd_type": type(hwnd).__name__,
            "same_hwnd": hwnd == again,
            "same_identity": same,
        }

    probe = await h.raw(_probe)
    h.check(
        "Application.HWND is an int, the same on a second GetActiveObject proxy",
        probe == {"hwnd_type": "int", "same_hwnd": True, "same_identity": True},
        observed=probe,
    )
    before = backend.document_identity()
    await h.raw(lambda _app: cb._drop_app())  # what the RPC-disconnect path does
    await backend.entity_create_line(0.0, 10.0, 10.0, 10.0, layer="0")
    after = backend.document_identity()
    h.check(
        "a re-attached session keeps the document's id and revision",
        after["document_id"] == before["document_id"]
        and after["revision"] == before["revision"] + 1,
        expected={"document_id": before["document_id"], "revision": before["revision"] + 1},
        observed=after,
    )
    return {"probe": probe, "before": before, "after": after}


async def _snapshot(h: LiveHarness, docs: dict, workspace: Path) -> dict:
    from engineering.understand.snapshot import take_snapshot

    backend = h.backend
    dwg = workspace / "bound.dwg"
    await backend.drawing_export_dwg(str(dwg), "R2018")
    b = await h.name_of(docs["b"])
    copy = workspace / "copy.dwg"
    shutil.copyfile(dwg, copy)
    before = await h.raw(lambda app: int(app.Documents.Count))
    snap = await take_snapshot(backend, str(copy))
    active = await h.raw(_active_key)
    count = await h.raw(lambda app: int(app.Documents.Count))
    h.check(
        "the snapshot read the copy",
        snap.source == "live:copy.dwg" and len(snap.records) >= 2,
        observed={"source": snap.source, "records": len(snap.records)},
    )
    h.check(
        "the read-only copy is closed and the bound document is active again",
        count == before and active[0] == b,
        expected={"documents": before, "active": b},
        observed={"documents": count, "active": active[0]},
    )
    clayer = await backend.system_get_variable("CLAYER")
    h.check("the next call is not refused", clayer == "0", observed=clayer)
    image = await backend.view_screenshot()
    h.check(
        "view_screenshot of the bound document answers",
        image is None or isinstance(image, bytes),
        observed=None if image is None else len(image),
    )
    return {"source": snap.source, "records": len(snap.records), "active": active[0]}


def assess(h: LiveHarness, report: dict) -> None:
    """Every check is made inside its step, against the seat state it just read."""


async def body(h: LiveHarness) -> dict:
    workspace = Path(tempfile.mkdtemp(prefix="acadmcp_document_smoke_"))
    report: dict = {}
    try:
        h.stage("unsaved documents")
        docs = await _unsaved(h)
        report["unsaved"] = docs
        h.stage("the guard")
        report["guard"] = await _guard(h, docs)
        h.stage("reactivate the bound document")
        report["reactivate"] = await _reactivate(h, docs)
        h.stage("document_activate")
        report["activate"] = await _activate(h, docs)
        h.stage("application identity")
        report["application"] = await _application(h)
        h.stage("dwg snapshot hand-back")
        report["snapshot"] = await _snapshot(h, docs, workspace)
        assess(h, report)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("document", body, description=__doc__.split("\n\n")[0]))
