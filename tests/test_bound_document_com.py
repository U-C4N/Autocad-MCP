"""B11 on the live engine: the bound document, checked inside every call's own job.

``_acad_doc()`` re-read ``ActiveDocument`` on every call, so an operator who
switched AutoCAD's active document between two calls had the second call
write into the document they had just switched to. ``ComBackend`` now binds
``(Name, FullName)`` and an id on new / open / activate / close (and at the
first call that touches a document), and the first ``_acad_doc()`` of every
executor job compares AutoCAD's active document with the bound one before any
work: a mismatch is refused with ``ActiveDocumentChangedError`` and nothing is
written - reads used as evidence included.

No AutoCAD is touched: ``_run`` is the real one on a plain daemon thread (no
CoInitialize) and the seat models only members measured on AutoCAD 2026 -
Documents.Count / Item / Open, ActiveDocument, Name, FullName, Activate,
Close, SaveAs, Save, GetVariable / SetVariable, SendCommand,
StartUndoMark / EndUndoMark, SelectionSets.Add / Select / Delete, Export.
"""

from __future__ import annotations

import inspect
import os

import ezdxf
import pytest
from fastmcp import Client

import server
from backends import com_backend as cb
from backends.document_guard import ActiveDocumentChangedError
from backends.ezdxf_backend import EzdxfBackend
from backends.quarantine import DaemonExecutor


class _Space:
    Count = 0

    def __iter__(self):
        return iter(())


class _Selection:
    def Select(self, mode):
        self.mode = mode

    def Delete(self):
        pass


class _SelectionSets:
    def Add(self, name):
        return _Selection()


class _Doc:
    def __init__(self, seat, name, full_name):
        self._seat = seat
        self.Name = name
        self.FullName = full_name
        self.Saved = True
        self.vars = {"LTSCALE": 1.0, "CMDACTIVE": 0, "OSMODE": 0, "SNAPMODE": 0, "CMDECHO": 1}
        self.writes: list[tuple[str, object]] = []
        self.commands: list[str] = []
        self.undo_marks: list[str] = []
        self.ModelSpace = _Space()
        self.SelectionSets = _SelectionSets()
        self.refuse_writes = False

    def GetVariable(self, name):
        return self.vars.get(name, 0)

    def SetVariable(self, name, value):
        if self.refuse_writes:
            raise RuntimeError("SetVariable refused")
        self.writes.append((name, value))
        self.vars[name] = value

    def SendCommand(self, text):
        self.commands.append(text)

    def StartUndoMark(self):
        self.undo_marks.append("start")

    def EndUndoMark(self):
        self.undo_marks.append("end")

    def Activate(self):
        self._seat.ActiveDocument = self

    def Close(self, save_changes):
        docs = self._seat.Documents._docs
        docs.remove(self)
        self._seat.ActiveDocument = docs[-1] if docs else None

    def SaveAs(self, path, save_as_type=None):
        self.FullName = str(path)
        self.Name = os.path.basename(str(path))

    def Save(self):
        self.Saved = True

    def Export(self, base, extension, selection):
        ezdxf.new("R2010").saveas(f"{base}.dxf")


class _Docs:
    def __init__(self, seat):
        self._seat = seat
        self._docs: list[_Doc] = []

    @property
    def Count(self):
        return len(self._docs)

    def Item(self, index):
        return self._docs[index]

    def Open(self, path, read_only=False):
        doc = _Doc(self._seat, os.path.basename(str(path)), os.path.abspath(str(path)))
        self._docs.append(doc)
        self._seat.ActiveDocument = doc
        return doc


class _Seat:
    Version = "25.1s (test seat)"

    def __init__(self):
        self.Documents = _Docs(self)
        self.first = _Doc(self, "First.dwg", "C:\\work\\First.dwg")
        self.second = _Doc(self, "Second.dwg", "C:\\work\\Second.dwg")
        self.Documents._docs.extend([self.first, self.second])
        self.ActiveDocument = self.first


@pytest.fixture
def seat(monkeypatch):
    fake = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: fake)
    return fake


@pytest.fixture
def com(seat):
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend
    if backend._executor is not None:
        backend._executor.shutdown(wait=False)
        backend._executor = None


@pytest.mark.asyncio
async def test_the_first_call_binds_the_active_document(com, seat):
    assert com.document_identity() == {
        "document_id": None,
        "revision": None,
        "name": None,
        "path": None,
        "backend": "com",
    }
    assert await com.system_get_variable("LTSCALE") == 1.0
    identity = com.document_identity()
    assert identity["name"] == "First.dwg" and identity["path"] == "C:\\work\\First.dwg"
    assert len(identity["document_id"]) == 32 and identity["revision"] == 0


@pytest.mark.asyncio
async def test_a_switch_between_two_calls_is_refused_and_writes_nothing(com, seat):
    """Review Focus 4: the operator switches AutoCAD's active document between two calls."""
    await com.system_set_variable("LTSCALE", 2.0)
    bound = com.document_identity()
    assert seat.first.writes == [("LTSCALE", 2.0)] and bound["revision"] == 1

    seat.ActiveDocument = seat.second  # the operator clicks the other drawing tab
    with pytest.raises(ActiveDocumentChangedError) as refused:
        await com.system_set_variable("LTSCALE", 3.0)
    assert seat.second.writes == [], "nothing may land in the newly active document"
    assert seat.first.writes == [("LTSCALE", 2.0)]
    payload = refused.value.to_dict()
    assert payload["ok"] is False and payload["kind"] == "active_document_changed"
    assert payload["bound"] == {
        "document_id": bound["document_id"],
        "name": "First.dwg",
        "path": "C:\\work\\First.dwg",
    }
    assert payload["active"] == {
        "document_id": None,
        "name": "Second.dwg",
        "path": "C:\\work\\Second.dwg",
    }
    assert "document_activate('Second.dwg')" in payload["error"]

    with pytest.raises(ActiveDocumentChangedError):
        await com.system_get_variable("LTSCALE")  # a read used as evidence is refused too
    assert com.document_identity() == bound, "a refused call moves no revision"


@pytest.mark.asyncio
async def test_document_activate_binds_the_document_the_operator_chose(com, seat):
    await com.system_get_variable("LTSCALE")
    first = com.document_identity()
    com._plan_spec = object()
    seat.ActiveDocument = seat.second
    result = await com.document_activate("Second.dwg")
    assert result["active"] == "Second.dwg"
    second = com.document_identity()
    assert second["name"] == "Second.dwg" and second["document_id"] != first["document_id"]
    assert com._plan_spec is None, "drawing-scoped state belongs to the document it was made for"
    await com.system_set_variable("LTSCALE", 4.0)
    assert seat.second.writes == [("LTSCALE", 4.0)]
    await com.document_activate("First.dwg")
    assert com.document_identity()["document_id"] == first["document_id"], "a known id is kept"


@pytest.mark.asyncio
async def test_transaction_rollback_addresses_the_bound_document(com, seat):
    assert (await com.transaction_begin())["ok"] is True
    assert seat.first.commands == ["_.UNDO _M\n"] and seat.first.undo_marks == ["start"]
    seat.ActiveDocument = seat.second
    with pytest.raises(ActiveDocumentChangedError):
        await com.transaction_rollback()
    assert seat.second.commands == [] and seat.second.undo_marks == []
    assert com._transaction_active is True, "the mark is still open in First.dwg"
    seat.ActiveDocument = seat.first
    assert (await com.transaction_rollback())["ok"] is True
    assert seat.first.undo_marks == ["start", "end"]
    assert seat.first.commands[-1] == "_.UNDO _B\n"
    assert com._transaction_active is False


@pytest.mark.asyncio
async def test_revision_moves_on_writes_success_or_failure_never_on_saves(com, seat, tmp_path):
    await com.system_get_variable("LTSCALE")

    def revision() -> int:
        return com.document_identity()["revision"]

    assert revision() == 0
    await com.system_set_variable("LTSCALE", 2.0)
    assert revision() == 1
    seat.first.refuse_writes = True
    with pytest.raises(RuntimeError, match="SetVariable refused"):
        await com.system_set_variable("LTSCALE", 3.0)
    assert revision() == 2, "a failed write may have landed in part: the revision moves anyway"
    seat.first.refuse_writes = False
    await com.drawing_undo()
    assert revision() == 3
    await com.drawing_save()
    await com.drawing_save_as(str(tmp_path / "Saved.dwg"), "dwg")
    assert revision() == 3, "saving changes no content"


@pytest.mark.asyncio
async def test_our_own_save_as_keeps_the_binding_and_the_id(com, seat, tmp_path):
    await com.system_get_variable("LTSCALE")
    before = com.document_identity()
    target = tmp_path / "Renamed.dwg"
    await com.drawing_save_as(str(target), "dwg")
    after = com.document_identity()
    assert after["document_id"] == before["document_id"]
    assert after["name"] == "Renamed.dwg" and after["path"] == str(target)
    assert await com.system_get_variable("LTSCALE") == 1.0, "the renamed document is still bound"


@pytest.mark.asyncio
async def test_a_dwg_snapshot_hands_the_seat_back_to_the_bound_document(com, seat, tmp_path):
    from engineering.understand.snapshot import take_snapshot

    await com.system_get_variable("LTSCALE")  # bound to First.dwg
    part = tmp_path / "part.dwg"
    part.write_bytes(b"AC1032 stand-in: the seat's Export writes the DXF")
    snapshot = await take_snapshot(com, str(part))
    assert snapshot.source == "live:part.dwg" and snapshot.records == ()
    assert seat.ActiveDocument is seat.first, "Close(False) left Second.dwg active; it is undone"
    assert [d.Name for d in seat.Documents._docs] == ["First.dwg", "Second.dwg"]


def test_every_content_changing_method_is_mutating_on_the_live_engine():
    """The oracle is the headless engine: whatever sets ``dirty`` there changes content."""
    dirty = set()
    for name, fn in vars(EzdxfBackend).items():
        if not inspect.iscoroutinefunction(fn):
            continue
        source = inspect.getsource(fn)
        if "_mark_dirty()" in source or "self._dirty = True" in source:
            dirty.add(name)
    own = {name for name, fn in vars(cb.ComBackend).items() if inspect.iscoroutinefunction(fn)}
    expected = (dirty & own) | {
        "drawing_undo",  # the headless twins restore a snapshot instead of setting dirty inline
        "drawing_redo",
        "solid_box",  # live-only content writers
        "solid_cylinder",
        "solid_extrude",
        "solid_revolve",
        "solid_boolean",
        "system_run_command",  # escape hatches: assume they write
        "system_run_lisp",
    }
    assert cb._MUTATING_METHODS == expected
    assert len(cb._MUTATING_METHODS) == 98
    for name in cb._MUTATING_METHODS:
        assert getattr(vars(cb.ComBackend)[name], "__acadmcp_mutating__", False) is True, name
    never = {
        "drawing_save",
        "drawing_save_as",
        "drawing_export_dxf",
        "drawing_export_dwg",
        "drawing_export_pdf",
        "drawing_template_save",
        "drawing_info",
        "entity_get",
        "entity_list",
        "document_list",
        "system_status",
        "transaction_begin",
        "transaction_commit",
        "preferences_set",
    }
    assert not (never & cb._MUTATING_METHODS)


@pytest.mark.asyncio
async def test_the_refusal_reaches_the_client_as_a_typed_payload(com, seat, monkeypatch):
    await com.system_get_variable("LTSCALE")
    seat.ActiveDocument = seat.second

    async def make():
        return com

    monkeypatch.setattr(server, "_make_backend", make)
    async with Client(server.mcp) as client:
        result = await client.call_tool("drawing_info", {}, raise_on_error=False)
    assert result.is_error is True
    payload = result.structured_content
    assert payload["kind"] == "active_document_changed"
    assert payload["tool"] == "drawing_info" and payload["backend"] == "com"
    assert payload["bound"]["name"] == "First.dwg" and payload["active"]["name"] == "Second.dwg"
