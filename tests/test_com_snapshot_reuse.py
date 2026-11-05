"""Issue #20: a live entity_get on a DIMENSION / HATCH reuses one export.

1.7.0 / 1.8.0 ran ``Document.Export`` of the whole drawing on every
``entity_get`` of a DIMENSION or a HATCH (``_detail_from_export`` ->
``take_snapshot``), so N reads of an unchanged drawing cost N exports - on the
reporter's 3.5 MB site plan 120-161 s each. ``ComBackend._cached_snapshot``
reuses the last export for ``COM_SNAPSHOT_TTL`` seconds while the bound
document, its revision and its operator-edit count stand still.

No AutoCAD is touched: ``_run`` is the real one on a plain daemon thread and the
seat models only members measured on AutoCAD 2026 (Documents.Count / Item,
ActiveDocument, Name, FullName, Activate, GetVariable / SetVariable,
HandleToObject, SelectionSets.Add / Select / Delete, Export). Every
``Document.Export`` is counted.
"""

from __future__ import annotations

import asyncio
import shutil

import ezdxf
import pytest

import config
from backends import com_backend as cb
from backends.document_guard import ActiveDocumentChangedError
from backends.quarantine import DaemonExecutor


class _Selection:
    def Select(self, mode):
        self.mode = mode

    def Delete(self):
        pass


class _SelectionSets:
    def Add(self, name):
        return _Selection()


class _Dimension:
    """An AcDbRotatedDimension with the members ``_entity_info`` reads."""

    ObjectName = "AcDbRotatedDimension"
    Layer = "0"
    color = 256
    Linetype = "ByLayer"
    Visible = True
    TextPosition = (21.0, 5.0, 0.0)

    def __init__(self, handle):
        self.Handle = handle
        self.updates = 0

    def GetBoundingBox(self):
        return (0.0, 0.0, 0.0), (42.0, 5.0, 0.0)

    def Update(self):
        self.updates += 1

    def __getattr__(self, name):
        raise AttributeError(name)


class _Doc:
    def __init__(self, seat, name, full_name):
        self._seat = seat
        self.Name = name
        self.FullName = full_name
        self.vars = {"LTSCALE": 1.0, "CMDACTIVE": 0}
        self.SelectionSets = _SelectionSets()

    def GetVariable(self, name):
        return self.vars.get(name, 0)

    def SetVariable(self, name, value):
        self.vars[name] = value

    def HandleToObject(self, handle):
        return self._seat.dimension

    def Activate(self):
        self._seat.ActiveDocument = self

    def Regen(self, which):
        pass

    def Export(self, base, extension, selection):
        self._seat.exports.append(self.Name)
        shutil.copyfile(self._seat.dxf, f"{base}.dxf")


class _Docs:
    def __init__(self, seat):
        self._seat = seat
        self._docs: list[_Doc] = []

    @property
    def Count(self):
        return len(self._docs)

    def Item(self, index):
        return self._docs[index]


class _Seat:
    Version = "25.1s (test seat)"

    def __init__(self, dxf, handle):
        self.dxf = dxf
        self.dimension = _Dimension(handle)
        self.exports: list[str] = []
        self.Documents = _Docs(self)
        self.first = _Doc(self, "First.dwg", "C:\\work\\First.dwg")
        self.second = _Doc(self, "Second.dwg", "C:\\work\\Second.dwg")
        self.Documents._docs.extend([self.first, self.second])
        self.ActiveDocument = self.first


@pytest.fixture
def seat(monkeypatch, tmp_path):
    doc = ezdxf.new("R2010")
    dim = doc.modelspace().add_linear_dim(base=(0, 5), p1=(0, 0), p2=(42, 0))
    dim.render()
    path = tmp_path / "drawing.dxf"
    doc.saveas(path)
    fake = _Seat(str(path), dim.dimension.dxf.handle)
    monkeypatch.setattr(cb, "_acad_app", lambda: fake)
    monkeypatch.setattr(config.settings, "com_call_timeout", 30)
    monkeypatch.setattr(config.settings, "com_snapshot_ttl", 5.0)
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


async def _read(com, seat):
    info = await com.entity_get(seat.dimension.Handle)
    assert info.properties["detail_source"] == "dxf_export"
    assert info.properties["dimension"]["measurement"] == 42.0
    return info


@pytest.mark.asyncio
async def test_three_reads_of_an_unchanged_drawing_cost_one_export(com, seat):
    for _ in range(3):
        await _read(com, seat)
    assert seat.exports == ["First.dwg"]  # 1.8.0: three
    assert com.snapshot_reuse_status() == {"ttl_s": 5.0, "exports": 1, "reused": 2}


@pytest.mark.asyncio
async def test_reads_issued_together_share_one_export(com, seat):
    await asyncio.gather(*(_read(com, seat) for _ in range(4)))
    assert seat.exports == ["First.dwg"]


@pytest.mark.asyncio
async def test_a_write_makes_the_next_read_export_again(com, seat):
    await _read(com, seat)
    before = com.document_identity()["revision"]
    await com.system_set_variable("LTSCALE", 2.0)
    assert com.document_identity()["revision"] == before + 1
    await _read(com, seat)
    await _read(com, seat)
    assert seat.exports == ["First.dwg", "First.dwg"]


@pytest.mark.asyncio
async def test_an_export_older_than_the_ttl_is_not_reused(com, seat):
    await _read(com, seat)
    com._snapshot_cache["taken"] -= 5.01  # the age, not the revision, expires it
    await _read(com, seat)
    assert seat.exports == ["First.dwg", "First.dwg"]


@pytest.mark.asyncio
async def test_a_document_switch_makes_the_next_read_export_again(com, seat):
    await _read(com, seat)
    await com.document_activate("Second.dwg")
    await _read(com, seat)
    await com.document_activate("First.dwg")
    await _read(com, seat)  # First's revision never moved: the rebinding retired it
    assert seat.exports == ["First.dwg", "Second.dwg", "First.dwg"]


@pytest.mark.asyncio
async def test_a_reused_export_still_refuses_a_switched_active_document(com, seat):
    await _read(com, seat)
    seat.ActiveDocument = seat.second  # the operator clicks the other tab
    with pytest.raises(ActiveDocumentChangedError):
        await com.entity_get(seat.dimension.Handle)
    assert seat.exports == ["First.dwg"]


@pytest.mark.asyncio
async def test_ttl_zero_exports_on_every_read(com, seat, monkeypatch):
    monkeypatch.setattr(config.settings, "com_snapshot_ttl", 0.0)
    for _ in range(3):
        await _read(com, seat)
    assert seat.exports == ["First.dwg"] * 3
    assert com._snapshot_cache is None


@pytest.mark.asyncio
async def test_a_write_never_reads_its_before_and_after_from_one_export(com, seat):
    handle = seat.dimension.Handle
    await _read(com, seat)  # a warm, current export
    report = await com.dimension_rerender([handle])
    assert seat.dimension.updates == 1
    assert handle in report["rerendered"] + report["already_current"] + report["stale"]
    # before and after each exported; neither reused the warm one
    assert seat.exports == ["First.dwg"] * 3
    await _read(com, seat)  # the revision moved: a fresh export
    assert seat.exports == ["First.dwg"] * 4


@pytest.mark.asyncio
async def test_an_entity_missing_from_a_reused_export_is_read_from_a_fresh_one(com, seat, tmp_path):
    """An entity drawn since the export, by an edit the revision does not count."""
    await _read(com, seat)  # warm on dimension A
    doc = ezdxf.readfile(seat.dxf)
    added = doc.modelspace().add_linear_dim(base=(0, 15), p1=(0, 10), p2=(30, 10))
    added.render()
    newer = tmp_path / "drawing_b.dxf"
    doc.saveas(newer)
    seat.dxf = str(newer)  # the next export holds B
    seat.dimension = _Dimension(added.dimension.dxf.handle)  # HandleToObject finds B
    info = await com.entity_get(seat.dimension.Handle)  # within the TTL, revision unmoved
    assert "detail_unavailable" not in info.properties
    assert info.properties["detail_source"] == "dxf_export"
    assert info.properties["dimension"]["measurement"] == 30.0
    assert seat.exports == ["First.dwg", "First.dwg"]
    await com.entity_get(seat.dimension.Handle)  # the fresh export is the cached one now
    assert seat.exports == ["First.dwg", "First.dwg"]


@pytest.mark.asyncio
async def test_an_operator_edit_the_counter_sees_makes_the_next_read_export_again(
    com, seat, monkeypatch
):
    """The reactor's count is the only thing that moves between the reads."""
    reactor = {"count": 0}
    monkeypatch.setattr(com, "_install_edit_counter", lambda app, doc_id, window: 0)
    monkeypatch.setattr(com, "_read_edit_count", lambda app, doc_id, **kw: reactor["count"])
    await _read(com, seat)
    await _read(com, seat)
    assert seat.exports == ["First.dwg"]  # the counter stood still: reused
    reactor["count"] += 1  # the operator moves a grip
    await _read(com, seat)
    assert seat.exports == ["First.dwg", "First.dwg"]


def test_the_ttl_setting_defaults_to_five_seconds_and_clamps_negatives(monkeypatch):
    monkeypatch.delenv("COM_SNAPSHOT_TTL", raising=False)
    assert config.Settings().com_snapshot_ttl == 5.0
    monkeypatch.setenv("COM_SNAPSHOT_TTL", "0")
    assert config.Settings().com_snapshot_ttl == 0.0
    monkeypatch.setenv("COM_SNAPSHOT_TTL", "-2")
    assert config.Settings().com_snapshot_ttl == 0.0
