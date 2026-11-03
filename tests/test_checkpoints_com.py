"""1.8-08 on the live engine, against a fake seat (no AutoCAD is touched).

A live checkpoint is the bound document exported with ``Document.Export``
(the seat below writes a real DXF where AutoCAD would), gzip level 1 into the
same store the headless engine uses. ``mode="in_place"`` is refused with
capability ``checkpoint_restore_in_place``; ``mode="new_document"`` opens the
checkpoint as ``<stem>-cp<seq>.dxf`` and binds it. ``transaction_begin`` - the
``cad_batch(on_error="rollback")`` boundary - and the destructive tools take a
checkpoint first. The live run is ``scripts/smoke_durability_com.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import ezdxf
import pytest

pytest.importorskip("win32com.client", reason="pywin32 builds the point VARIANTs")

from backends import com_backend as cb  # noqa: E402
from backends.base import UnsupportedCapabilityError  # noqa: E402
from backends.checkpoints import open_store  # noqa: E402
from backends.quarantine import DaemonExecutor  # noqa: E402
from tests.test_com_space import _Doc, _Seat  # noqa: E402

pytestmark = pytest.mark.asyncio


class _Selection:
    def __init__(self, doc):
        self._doc = doc
        self.mode = None

    def Select(self, mode):  # noqa: N802 - ActiveX spelling
        self.mode = mode

    def Delete(self):  # noqa: N802
        pass


class _Selections:
    def __init__(self, doc):
        self._doc = doc

    def Add(self, name):  # noqa: N802
        return _Selection(self._doc)


def _exportable(doc: _Doc) -> None:
    """Give a fake document ``SelectionSets`` and ``Export`` (a real DXF of its circles)."""
    doc.SelectionSets = _Selections(doc)
    doc.exports = []

    def export(base, kind, selection):
        assert kind == "DXF", "AutoCAD refuses '.dxf' (measured)"
        out = ezdxf.new("R2018")
        for entity in doc.ModelSpace:
            if entity.ObjectName == "AcDbCircle":
                out.modelspace().add_circle(entity.Center[:2], entity.Radius)
        out.saveas(f"{base}.DXF")
        doc.exports.append(base)

    doc.Export = export


@pytest.fixture
def seat(monkeypatch):
    fake = _Seat()
    _exportable(fake.doc)
    real_open = fake.Documents.Open

    def open_(path, read_only=False):
        opened = real_open(path, read_only)
        _exportable(opened)
        return opened

    fake.Documents.Open = open_
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


async def test_checkpoint_create_exports_the_bound_document_into_the_store(com, seat, tmp_path):
    await com.entity_create_circle(1.0, 2.0, 3.0)
    made = await com.checkpoint_create("before purge")
    assert made["ok"] is True and made["label"] == "before purge" and made["granularity"] == "label"
    assert made["method"] == "Document.Export" and len(seat.doc.exports) == 1
    store = open_store()
    row = store.resolve(made["lineage_id"], "before purge")
    copy = tmp_path / "cp.dxf"
    copy.write_bytes(store.read(made["lineage_id"], row))
    exported = ezdxf.readfile(str(copy))
    circles = [e for e in exported.modelspace() if e.dxftype() == "CIRCLE"]
    assert len(circles) == 1 and circles[0].dxf.radius == 3.0
    listed = await com.checkpoint_list()
    assert [r["cp"] for r in listed["checkpoints"]] == [made["cp"]]
    assert com.latest_checkpoint() == made["cp"]
    assert seat.doc.Name == "Sheet.dwg", "Export never renames the document"


async def test_in_place_is_refused_by_capability_and_changes_nothing(com, seat):
    await com.entity_create_circle(0.0, 0.0, 1.0)
    made = await com.checkpoint_create()
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await com.checkpoint_restore(made["cp"], mode="in_place")
    assert excinfo.value.capability == "checkpoint_restore_in_place"
    assert "new_document" in str(excinfo.value)
    assert seat.ActiveDocument is seat.doc


async def test_new_document_opens_the_checkpoint_as_stem_cp_seq_and_binds_it(com, seat):
    await com.entity_create_circle(0.0, 0.0, 4.0)
    made = await com.checkpoint_create("one")
    result = await com.checkpoint_restore("one", mode="new_document")
    assert result["mode"] == "new_document" and result["restored"]["cp"] == made["cp"]
    assert Path(result["path"]).name == f"Sheet-cp{made['seq']}.dxf"
    assert seat.ActiveDocument is not seat.doc and seat.ActiveDocument.Name == (
        f"Sheet-cp{made['seq']}.dxf"
    )
    assert com.document_identity()["name"] == seat.ActiveDocument.Name, "the new document is bound"
    assert os.path.isfile(result["path"])


async def test_a_transaction_boundary_takes_a_checkpoint_before_the_mark(com, seat):
    await com.entity_create_circle(0.0, 0.0, 1.0)
    begun = await com.transaction_begin()
    assert begun["ok"] is True and begun["checkpoint"]
    store = open_store()
    lineage = com._bound_entry()["lineage_id"]
    row = store.resolve(lineage, begun["checkpoint"])
    assert (row["kind"], row["tool"], row["granularity"]) == ("batch", "transaction_begin", "batch")
    assert seat.doc.sent and seat.doc.sent[-1].startswith("_.UNDO _M")
    await com.transaction_commit()


async def test_a_seat_that_cannot_export_still_begins_the_transaction(com, seat):
    del seat.doc.Export  # an export failure is logged, never a refused boundary
    await com.entity_create_circle(0.0, 0.0, 1.0)
    begun = await com.transaction_begin()
    assert begun["ok"] is True and "checkpoint" not in begun
    await com.transaction_commit()


async def test_the_checkpoint_tools_add_no_mutating_method():
    """A checkpoint never changes the bound document's content: no revision bump."""
    for name in ("checkpoint_create", "checkpoint_list", "checkpoint_restore", "checkpoint_before"):
        assert name not in cb._MUTATING_METHODS
