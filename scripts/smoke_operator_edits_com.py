"""Measure the live revision's operator-edit counter (B11) once against the live AutoCAD.

Runs on the live harness (scripts/live/harness.py): attach only, two NEW
documents of its own (never the operator's drawings), closed on every path,
no COM event sink, no typed command (the harness rule). Through the real
``ComBackend``; an "operator edit" is a raw COM call on the run's own
document that does not pass through the backend, followed by one server read:

1. **The server's own writes.** ``entity_create_line`` moves the revision by
   exactly one - the reactor sees the write, the end-of-job read absorbs it.
2. **What an operator edit does to the revision.** Add, move, a property, a
   layer property, a new layer and delete each move it by one; a system
   variable written with ``SetVariable`` is recorded as it comes (it is not a
   database object); no edit moves nothing. Typed commands (ERASE, LINE,
   MOVE, U, ZOOM) need the command line and were measured once by a raw probe
   instead (backends/document_guard.py).
3. **A stale expected_revision.** A preview, a hand edit, no server call:
   ``check_preconditions(expected_revision=...)`` is refused by the revision.
4. **Nothing written into the drawing.** After a save, the reactor is removed
   (the operator's ``vlr-remove``): the next call counts the unwatched gap,
   installs it again, and DBMOD stays 0.
5. **Per document.** A COM edit to document A while B is bound and active
   does not move B; ``document_activate(A)`` then finds A moved by one.
6. **The cost.** A guarded read's median time with the counter on and off.

Exit codes are the harness's.

    uv run --frozen python scripts/smoke_operator_edits_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402


def _find(app, name: str):
    for index in range(int(app.Documents.Count)):
        doc = app.Documents.Item(index)
        if str(doc.Name) == name:
            return doc
    raise LookupError(f"{name} is not open")


def _point(x: float, y: float):
    import pythoncom
    import win32com.client

    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (x, y, 0.0))


async def _revision(h: LiveHarness) -> int:
    await h.backend.system_get_variable("CLAYER")  # one guarded server read
    return h.backend.document_identity()["revision"]


def _lisp(app, source: str):
    from backends import com_backend as cb

    vl = cb._vl_application(app)
    functions = cb._vl_get(cb._vl_get(vl, "ActiveDocument"), "Functions")
    reader = cb._vl_call(functions, "Item", "read")
    evaluator = cb._vl_call(functions, "Item", "eval")
    return cb._vl_call(evaluator, "funcall", cb._vl_call(reader, "funcall", source))


async def _own_writes(h: LiveHarness, a: str) -> dict:
    before = await _revision(h)
    await h.backend.entity_create_line(0, 0, 10, 0)
    after = await _revision(h)
    h.check(
        "a server write is one revision", after - before == 1, expected=1, observed=after - before
    )
    status = h.backend.operator_edits_status()
    h.check("the counter is on", status["counted"] is True, expected=True, observed=status)
    return {"before": before, "after": after, "status": status}


async def _operator_edits(h: LiveHarness, a: str) -> dict:
    state: dict = {}

    def add(app):
        state["line"] = _find(app, a).ModelSpace.AddLine(_point(0, 5), _point(10, 5))

    def move(app):
        state["line"].Move(_point(0, 0), _point(3, 3))

    def prop(app):
        state["line"].LinetypeScale = 2.0

    def layer_prop(app):
        _find(app, a).Layers.Item("0").Lineweight = 50

    def layer_add(app):
        _find(app, a).Layers.Add("ACADMCP_SMOKE")

    def delete(app):
        state["line"].Delete()
        state["line"] = None

    def sysvar(app):
        doc = _find(app, a)
        doc.SetVariable("LTSCALE", float(doc.GetVariable("LTSCALE")) * 2)

    async def nothing():
        return None

    steps = [
        ("add an entity", lambda: h.raw(add), 1),
        ("move it", lambda: h.raw(move), 1),
        ("change its property", lambda: h.raw(prop), 1),
        ("change a layer property", lambda: h.raw(layer_prop), 1),
        ("add a layer", lambda: h.raw(layer_add), 1),
        ("delete it", lambda: h.raw(delete), 1),
        ("no edit", nothing, 0),
    ]
    observed: dict = {}
    for label, edit, expected in steps:
        before = await _revision(h)
        await edit()
        delta = await _revision(h) - before
        observed[label] = delta
        h.check(
            f"{label}: revision +{expected}", delta == expected, expected=expected, observed=delta
        )
    before = await _revision(h)
    await h.raw(sysvar)
    observed["SetVariable LTSCALE"] = await _revision(h) - before  # recorded, not asserted
    return observed


async def _stale(h: LiveHarness, a: str) -> dict:
    from engineering.changes.preview import StaleRevisionError, check_preconditions, make_preview

    preview = await make_preview(h.backend)
    await h.raw(lambda app: _find(app, a).ModelSpace.AddCircle(_point(0, 0), 2.0))
    try:
        await check_preconditions(h.backend, expected_revision=preview.revision)
    except StaleRevisionError as exc:
        refused = {"expected": exc.expected, "actual": exc.actual}
    else:
        refused = None
    h.check(
        "a hand edit after the preview is refused by the revision",
        refused is not None and refused["actual"] == preview.revision + 1,
        expected={"expected": preview.revision, "actual": preview.revision + 1},
        observed=refused,
    )
    return {"preview_revision": preview.revision, "refused": refused}


async def _nothing_written(h: LiveHarness, workspace: Path) -> dict:
    b = await h.new_document()
    await h.backend.drawing_save_as(str(workspace / "operator_edits_b.dwg"))
    b = await h.name_of(b)
    dbmod_saved = await h.raw(lambda app: int(_find(app, b).GetVariable("DBMOD")))
    removed = await h.raw(lambda app: _lisp(app, "(vlr-remove acadmcp:edit-r)"))
    before = h.backend.document_identity()["revision"]
    after = await _revision(h)
    dbmod = await h.raw(lambda app: int(_find(app, b).GetVariable("DBMOD")))
    h.check("the save left DBMOD 0", dbmod_saved == 0, expected=0, observed=dbmod_saved)
    h.check(
        "a removed reactor is a gap: +1", after - before == 1, expected=1, observed=after - before
    )
    h.check("installing it again writes nothing: DBMOD 0", dbmod == 0, expected=0, observed=dbmod)
    return {"document": b, "vlr_remove_answered": removed is not None, "dbmod_after_install": dbmod}


async def _per_document(h: LiveHarness, a: str, b: str) -> dict:
    await h.backend.document_activate(a)
    a_before = await _revision(h)
    await h.backend.document_activate(b)
    b_before = await _revision(h)
    await h.raw(lambda app: _find(app, a).ModelSpace.AddLine(_point(0, 9), _point(9, 9)))
    b_after = await _revision(h)
    await h.backend.document_activate(a)
    a_after = await _revision(h)
    h.check(
        "an edit to A leaves B's revision", b_after == b_before, expected=b_before, observed=b_after
    )
    h.check("A moved by one", a_after - a_before == 1, expected=1, observed=a_after - a_before)
    return {"a": [a_before, a_after], "b": [b_before, b_after]}


async def _cost(h: LiveHarness, rounds: int = 30) -> dict:
    from backends import com_backend as cb

    async def median_ms() -> float:
        samples = []
        for _ in range(rounds):
            started = time.perf_counter()
            await h.backend.system_get_variable("CLAYER")
            samples.append((time.perf_counter() - started) * 1000)
        return round(statistics.median(samples), 2)

    def disable(app):
        cb._COM_STATE["vl"] = (app, RuntimeError("off for the cost measurement"))

    on: list[float] = []
    off: list[float] = []
    for _ in range(3):  # interleaved: the seat's own drift hits both alike
        on.append(await median_ms())
        await h.raw(disable)
        off.append(await median_ms())
        await h.raw(lambda app: cb._COM_STATE.pop("vl", None))
        await _revision(h)  # back on (counts the off window once)
    h.check("the counter is back on", h.backend.operator_edits_status()["counted"] is True)

    backend = h.backend
    document_id = backend.document_identity()["document_id"]

    def reads(app):
        samples = []
        for _ in range(rounds):
            started = time.perf_counter()
            backend._read_edit_count(app, document_id, refetch=False)
            samples.append((time.perf_counter() - started) * 1000)
        return round(statistics.median(samples), 2)

    per_read = await h.raw(reads)
    return {
        "guarded_read_median_ms_on": on,
        "guarded_read_median_ms_off": off,
        "counter_read_median_ms": per_read,
        "counter_reads_per_guarded_job": 2,
    }


def assess(h: LiveHarness, report: dict) -> None:
    """Every check is made inside its step, against the seat state it just read."""


async def body(h: LiveHarness) -> dict:
    workspace = Path(tempfile.mkdtemp(prefix="acadmcp_operator_edits_smoke_"))
    report: dict = {}
    try:
        h.stage("the server's own writes")
        a = await h.new_document()
        report["own_writes"] = await _own_writes(h, a)
        h.stage("operator edits")
        report["operator_edits"] = await _operator_edits(h, a)
        h.stage("stale expected_revision")
        report["stale"] = await _stale(h, a)
        h.stage("nothing written into the drawing")
        report["nothing_written"] = await _nothing_written(h, workspace)
        h.stage("per document")
        report["per_document"] = await _per_document(h, a, report["nothing_written"]["document"])
        h.stage("cost")
        report["cost"] = await _cost(h)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("operator_edits", body, description=__doc__.split("\n\n")[0]))
