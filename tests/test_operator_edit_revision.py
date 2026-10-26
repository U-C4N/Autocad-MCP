"""B11 on the live engine: an edit the operator makes by hand moves the revision.

The live revision used to count this server's own writes only, because the
COM document events broke the seat (backends/document_guard.py). It now also
reads an edit counter kept by a transient AutoLISP database reactor in the
bound document's own namespace, evaluated through ``VL.Application.16`` - no
COM event sink, no ``SendCommand``, nothing written into the drawing. The
first ``_acad_doc()`` of a job compares the counter with the value the
previous job left, and a difference is one more revision.

No AutoCAD is touched: the seat of tests/test_bound_document_com.py plus a
fake VL application whose ``read`` / ``eval`` model the two forms the backend
sends. Like the measured reactor, the fake counter moves on every database
change - the server's own writes included.
"""

from __future__ import annotations

import re

import pytest

from backends import com_backend as cb
from backends.quarantine import DaemonExecutor
from engineering.changes.preview import StaleRevisionError, check_preconditions, make_preview
from tests.test_bound_document_com import _Doc, _Seat


class _Fn:
    def __init__(self, name, seat, doc):
        self.name, self._seat, self._doc = name, seat, doc

    def funcall(self, arg):
        if self.name == "read":
            return arg  # the "parsed" form is the source text itself
        self._seat.evals += 1
        ns = self._doc.lisp
        if "vlr-acdb-reactor" in arg:
            ns.setdefault("n", 0)
            ns["reactor"] = True
            ns["id"] = re.search(r'acadmcp:edit-id "([0-9a-f]+)"', arg).group(1)
        elif not ns.get("reactor") or "id" not in ns:
            return None
        return f"{ns['id']}:{ns['n']}"


class _Functions:
    def __init__(self, seat, doc):
        self._seat, self._doc = seat, doc

    def Item(self, name):
        return _Fn(name, self._seat, self._doc)


class _VLDoc:
    def __init__(self, seat, doc):
        self.Functions = _Functions(seat, doc)


class _VL:
    def __init__(self, seat):
        self._seat = seat

    @property
    def ActiveDocument(self):
        return _VLDoc(self._seat, self._seat.ActiveDocument)


class _EditDoc(_Doc):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lisp: dict = {}

    def SetVariable(self, name, value):
        super().SetVariable(name, value)
        self.changed()  # the reactor sees the server's own writes too

    def changed(self):
        if self.lisp.get("reactor"):
            self.lisp["n"] += 1


class _VLSeat(_Seat):
    def __init__(self):
        super().__init__()
        self.first = _EditDoc(self, "First.dwg", "C:\\work\\First.dwg")
        self.second = _EditDoc(self, "Second.dwg", "C:\\work\\Second.dwg")
        self.Documents._docs[:] = [self.first, self.second]
        self.ActiveDocument = self.first
        self.evals = 0
        self.vl_progids: list[str] = []

    def GetInterfaceObject(self, progid):
        self.vl_progids.append(progid)
        return _VL(self)


@pytest.fixture
def seat(monkeypatch):
    fake = _VLSeat()
    monkeypatch.setattr(cb, "_acad_app", lambda: fake)
    cb._COM_STATE.clear()
    yield fake
    cb._COM_STATE.clear()


@pytest.fixture
def com(seat):
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")
    backend._connected = True
    yield backend
    backend._executor.shutdown(wait=False)
    backend._executor = None


@pytest.mark.asyncio
async def test_an_operator_edit_between_two_calls_moves_the_revision(com, seat):
    await com.system_set_variable("LTSCALE", 2.0)
    assert com.document_identity()["revision"] == 1
    assert seat.first.lisp["id"] == com.document_identity()["document_id"]

    seat.first.changed()  # the operator moves a line by hand
    await com.system_get_variable("LTSCALE")
    assert com.document_identity()["revision"] == 2, "the hand edit is one more revision"

    await com.system_get_variable("LTSCALE")
    assert com.document_identity()["revision"] == 2, "nothing changed since: no move"


@pytest.mark.asyncio
async def test_the_servers_own_writes_are_not_counted_twice(com, seat):
    for value in (2.0, 3.0, 4.0):
        await com.system_set_variable("LTSCALE", value)
    assert seat.first.lisp["n"] == 3, "the reactor saw every write"
    assert com.document_identity()["revision"] == 3, "one revision per write, none for the reactor"


@pytest.mark.asyncio
async def test_a_stale_expected_revision_is_refused_by_the_revision_itself(com, seat):
    await com.system_set_variable("LTSCALE", 2.0)
    preview = await make_preview(com)
    await check_preconditions(com, expected_revision=preview.revision)  # still current

    seat.first.changed()  # the operator edits; no server call in between
    with pytest.raises(StaleRevisionError) as refused:
        await check_preconditions(com, expected_revision=preview.revision)
    assert refused.value.actual == preview.revision + 1


@pytest.mark.asyncio
async def test_a_removed_reactor_counts_as_a_change_and_is_installed_again(com, seat):
    await com.system_get_variable("LTSCALE")
    before = com.document_identity()["revision"]
    seat.first.lisp["reactor"] = False  # (vlr-remove-all) at the operator's prompt
    await com.system_get_variable("LTSCALE")
    assert com.document_identity()["revision"] == before + 1, "an unwatched gap is not vouched for"
    assert seat.first.lisp["reactor"] is True
    await com.system_get_variable("LTSCALE")
    assert com.document_identity()["revision"] == before + 1


@pytest.mark.asyncio
async def test_an_edit_in_another_document_does_not_move_the_bound_revision(com, seat):
    await com.system_get_variable("LTSCALE")
    seat.second.lisp.update(reactor=True, n=0, id="f" * 32)
    seat.second.changed()
    await com.system_get_variable("LTSCALE")
    assert com.document_identity()["revision"] == 0
    assert "reactor" not in seat.first.lisp or seat.first.lisp["id"] != "f" * 32


@pytest.mark.asyncio
async def test_a_refused_switch_reads_no_counter(com, seat):
    await com.system_get_variable("LTSCALE")
    evals = seat.evals
    seat.ActiveDocument = seat.second
    with pytest.raises(cb.ActiveDocumentChangedError):
        await com.system_get_variable("LTSCALE")
    assert seat.evals == evals and seat.second.lisp == {}, "nothing is installed in the other one"


@pytest.mark.asyncio
async def test_the_counter_reports_how_operator_edits_are_counted(com, seat):
    await com.system_get_variable("LTSCALE")
    assert com.operator_edits_status() == {
        "counted": True,
        "method": "lisp_database_reactor",
        "reason": None,
    }
    assert seat.vl_progids == ["VL.Application.16"]


@pytest.mark.asyncio
async def test_without_visual_lisp_the_revision_counts_the_servers_writes_only(
    com, seat, monkeypatch
):
    def refuse(progid):
        raise RuntimeError("Problem in loading application")

    monkeypatch.setattr(seat, "GetInterfaceObject", refuse)
    await com.system_set_variable("LTSCALE", 2.0)
    seat.first.changed()
    await com.system_get_variable("LTSCALE")
    assert com.document_identity()["revision"] == 1, "no counter: a hand edit is not seen"
    status = com.operator_edits_status()
    assert status["counted"] is False and "Problem in loading application" in status["reason"]
