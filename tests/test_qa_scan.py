"""engineering/qa_scan.py: one complete, all-spaces read per QA run.

Every expected count below is the number of entities the test itself placed.
The live-engine paths run against a fake whose members are the ones measured
on AutoCAD 2026 on 2026-10-24 (``Layouts.Count``, ``Layouts.Item(i)``,
``Layout.Name``, ``Layout.TabOrder``, ``Layout.Block.Count``) - per layout,
``Block.Count`` equalled the exported DXF's entity count, VIEWPORTs included.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import ezdxf
import pytest

import config
from backends.quarantine import DocumentQuarantineError
from engineering import qa_scan
from engineering.qa_scan import ScanCoverage, entity_infos_all_spaces, qa_snapshot
from engineering.understand.snapshot import EntityRecord, records_from_doc

pytestmark = pytest.mark.asyncio


def _paper(backend):
    return backend._doc.layouts.get("Layout1")


# -- EntityRecord.visible ---------------------------------------------------------------


async def test_a_record_reads_dxf_group_60(backend):
    msp = backend._doc.modelspace()
    shown = msp.add_line((0, 0), (1, 0))
    hidden = msp.add_line((0, 1), (1, 1))
    hidden.dxf.invisible = 1
    toggled = msp.add_line((0, 2), (1, 2))
    toggled.dxf.invisible = 1
    toggled.dxf.invisible = 0  # stored 0 is visible, not "attribute present"
    snap = records_from_doc(backend._doc, source="test")
    by_handle = {r.handle: r.visible for r in snap.records}
    assert by_handle[shown.dxf.handle] is True
    assert by_handle[hidden.dxf.handle] is False
    assert by_handle[toggled.dxf.handle] is True
    assert EntityRecord(handle="1", type="LINE", layer="0", space="Model").visible is True


# -- one read, every space -----------------------------------------------------------


async def test_one_read_covers_model_and_paper_and_is_cached(backend, monkeypatch):
    msp = backend._doc.modelspace()
    for i in range(3):
        msp.add_line((0, 10 * i), (5, 10 * i))
    _paper(backend).add_line((0, 0), (5, 0))
    calls = []
    real = qa_scan.records_from_doc

    def counting(doc, *, source):
        calls.append(source)
        return real(doc, source=source)

    monkeypatch.setattr(qa_scan, "records_from_doc", counting)
    shared: dict = {}
    snap, coverage = await qa_snapshot(backend, shared)
    again = await qa_snapshot(backend, shared)

    assert coverage == ScanCoverage(True, 4, ("Model", "Layout1"))
    assert sorted(r.space for r in snap.records) == ["Layout1", "Model", "Model", "Model"]
    assert again == (snap, coverage)
    assert shared["qa_snapshot"] == (snap, coverage)
    assert len(calls) == 1


async def test_over_max_qa_entities_is_refused_before_reading(backend, monkeypatch):
    msp = backend._doc.modelspace()
    for i in range(3):
        msp.add_line((0, 10 * i), (5, 10 * i))
    _paper(backend).add_line((0, 0), (5, 0))

    def never(*_a, **_k):
        raise AssertionError("the drawing was read past MAX_QA_ENTITIES")

    monkeypatch.setattr(qa_scan, "records_from_doc", never)
    monkeypatch.setattr(config.settings, "max_qa_entities", 3)
    snap, coverage = await qa_snapshot(backend, {})

    assert snap is None
    assert coverage.complete is False and coverage.records == 0
    assert coverage.spaces == ("Model", "Layout1")
    assert "4 entities" in coverage.reason and "MAX_QA_ENTITIES=3" in coverage.reason


async def test_max_qa_entities_zero_disables_the_check(backend, monkeypatch):
    backend._doc.modelspace().add_line((0, 0), (5, 0))
    monkeypatch.setattr(config.settings, "max_qa_entities", 0)
    _snap, coverage = await qa_snapshot(backend, {})
    assert coverage.complete is True and coverage.records == 1


async def test_the_default_ceiling_is_two_million():
    assert config.Settings().max_qa_entities == 2_000_000


async def test_a_failed_read_is_cached_as_an_incomplete_scan(backend, monkeypatch):
    calls = []

    async def broken(_backend):
        calls.append(1)
        raise RuntimeError("export failed")

    monkeypatch.setattr(qa_scan, "_read_all_spaces", broken)
    shared: dict = {}
    first = await qa_snapshot(backend, shared)
    second = await qa_snapshot(backend, shared)

    assert first == second
    snap, coverage = first
    assert snap is None and coverage.complete is False
    assert coverage.reason == "the drawing could not be read: RuntimeError: export failed"
    assert len(calls) == 1


async def _quarantine(backend, monkeypatch):
    """Abandon a real write, the way a real timeout does."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.2)
    entered = threading.Event()
    release = threading.Event()

    def _writer():
        entered.set()
        release.wait(5.0)
        return "too late"

    with pytest.raises(RuntimeError):
        await backend._async(_writer)
    assert entered.is_set()
    assert backend._quarantine is not None
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 10)
    return release


async def test_a_quarantine_refusal_propagates_and_is_not_cached(backend, monkeypatch):
    backend._doc.modelspace().add_line((0, 0), (5, 0))
    release = await _quarantine(backend, monkeypatch)
    shared: dict = {}
    try:
        with pytest.raises(DocumentQuarantineError):
            await qa_snapshot(backend, shared)
    finally:
        release.set()
    assert "qa_snapshot" not in shared


# -- the read-only flag (Task 1 lands it on its own branch) ---------------------------


class _AsyncWithFlag:
    def __init__(self):
        self.seen = []

    async def _async(self, func, *args, quarantine_exit=False, readonly=False, **kwargs):
        self.seen.append(readonly)
        return func(*args, **kwargs)


class _AsyncWithoutFlag:
    def __init__(self):
        self.seen = []

    async def _async(self, func, *args, quarantine_exit=False, **kwargs):
        self.seen.append(kwargs)
        return func(*args, **kwargs)


async def test_readonly_is_passed_only_when_async_declares_it():
    with_flag = _AsyncWithFlag()
    without_flag = _AsyncWithoutFlag()

    assert await qa_scan._async_readonly(with_flag, lambda: 7) == 7
    # Forwarding readonly through **kwargs would hand it to the closure, which
    # takes no arguments: the call would raise TypeError.
    assert await qa_scan._async_readonly(without_flag, lambda: 8) == 8
    assert with_flag.seen == [True]
    assert without_flag.seen == [{}]


# -- the live engine: the export is compared with AutoCAD's own counts ----------------


class _Block:
    def __init__(self, count):
        self.Count = count


class _Layout:
    def __init__(self, name, tab, count):
        self.Name = name
        self.TabOrder = tab
        self.Block = _Block(count)


class _Layouts:
    def __init__(self, rows):
        self._rows = rows
        self.Count = len(rows)

    def Item(self, index):
        return self._rows[index]


class _Doc:
    def __init__(self, rows):
        # Collection order is not tab order on AutoCAD (measured: Layout1,
        # Layout2, Model) - the reader sorts by TabOrder.
        self.Layouts = _Layouts(rows)


class _FakeCom:
    name = "com"

    async def _run(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def _wait_out_rejected_call(self, read):
        return read()


def _export_with(tmp_path: Path, model: int, paper: int):
    def _export(_backend, path):
        assert path is None
        folder = tmp_path / "snapfolder"
        folder.mkdir(exist_ok=True)
        doc = ezdxf.new()
        for i in range(model):
            doc.modelspace().add_line((0, i), (1, i))
        for i in range(paper):
            doc.layouts.get("Layout1").add_line((0, i), (1, i))
        target = folder / "snap.DXF"
        doc.saveas(target)
        return str(target), str(folder), "live:Fake.dwg"

    return _export


async def test_live_counts_that_match_the_export_are_a_complete_scan(tmp_path, monkeypatch):
    import backends.com_backend as com

    doc = _Doc([_Layout("Layout1", 1, 1), _Layout("Model", 0, 2)])
    monkeypatch.setattr(com, "_acad_doc", lambda: doc)
    monkeypatch.setattr(qa_scan, "_com_export", _export_with(tmp_path, model=2, paper=1))
    snap, coverage = await qa_snapshot(_FakeCom(), {})

    assert coverage.complete is True and coverage.records == 3
    assert snap.source == "live:Fake.dwg"
    assert not (tmp_path / "snapfolder").exists()  # the temporary export is deleted


async def test_an_export_short_of_the_live_counts_is_incomplete(tmp_path, monkeypatch):
    import backends.com_backend as com

    doc = _Doc([_Layout("Layout1", 1, 1), _Layout("Model", 0, 3)])
    monkeypatch.setattr(com, "_acad_doc", lambda: doc)
    monkeypatch.setattr(qa_scan, "_com_export", _export_with(tmp_path, model=2, paper=1))
    snap, coverage = await qa_snapshot(_FakeCom(), {})

    assert snap is not None and len(snap.records) == 3
    assert coverage.complete is False
    assert coverage.reason == (
        "the DXF export does not match the live drawing "
        "(Model: AutoCAD holds 3, the export carried 2)"
    )


async def test_over_the_ceiling_the_live_drawing_is_never_exported(tmp_path, monkeypatch):
    import backends.com_backend as com

    doc = _Doc([_Layout("Layout1", 1, 1), _Layout("Model", 0, 2)])
    monkeypatch.setattr(com, "_acad_doc", lambda: doc)

    def never(*_a, **_k):
        raise AssertionError("exported past MAX_QA_ENTITIES")

    monkeypatch.setattr(qa_scan, "_com_export", never)
    monkeypatch.setattr(config.settings, "max_qa_entities", 2)
    snap, coverage = await qa_snapshot(_FakeCom(), {})

    assert snap is None and coverage.complete is False
    assert coverage.spaces == ("Model", "Layout1")
    assert "3 entities (Model 2, Layout1 1)" in coverage.reason


# -- every layout without switching (backlog W3-T14-09) -------------------------------


async def test_every_layout_is_read_without_switching_or_dirtying(backend):
    await backend.layout_create("SHEET2")
    backend._doc.modelspace().add_line((0, 0), (1, 0))
    backend._doc.layouts.get("SHEET2").add_line((0, 0), (2, 0))
    backend._doc.layouts.set_active_layout("Layout1")
    backend._dirty = False
    before = backend._doc.layouts.active_layout().name

    rows = await entity_infos_all_spaces(backend)

    assert sorted((info.type, space) for info, space in rows) == [
        ("LINE", "Model"),
        ("LINE", "SHEET2"),
    ]
    assert backend._doc.layouts.active_layout().name == before == "Layout1"
    assert backend._dirty is False


async def test_the_pid_graph_scope_all_leaves_the_paper_binding_alone(backend):
    """W3-T14-09: active paper layout Layout1, current Model, a layout SHEET2
    added - build_graph(scope='all') left SHEET2 active afterwards."""
    from engineering.pid.graph import build_graph
    from engineering.pid.insert import place_symbol

    await place_symbol(backend, "gate", 0, 0, tag="HV-1")
    await backend.layout_create("SHEET2")
    await backend.layout_set_current("SHEET2")
    await place_symbol(backend, "gate", 0, 0, tag="HV-2")
    await backend.layout_set_current("Layout1")
    await backend.layout_set_current("Model")
    backend._dirty = False

    graph = await build_graph(backend, scope="all")

    assert sorted(n["tag"] for n in graph["nodes"]) == ["HV-1", "HV-2"]
    assert backend._doc.layouts.active_layout().name == "Layout1"
    assert (await backend.layout_list())["current"] == "Model"
    assert backend._dirty is False


async def test_a_live_entity_that_cannot_be_read_refuses_the_whole_read(monkeypatch):
    import backends.com_backend as com

    class _Items:
        def __init__(self, items):
            self._items = items
            self.Count = len(items)

        def Item(self, index):
            return self._items[index]

    class _LiveLayout:
        def __init__(self, name, tab, items):
            self.Name = name
            self.TabOrder = tab
            self.Block = _Items(items)

    doc = _Doc([_LiveLayout("Model", 0, ["good", "bad"])])
    monkeypatch.setattr(com, "_acad_doc", lambda: doc)

    def entity_info(item):
        if item == "bad":
            raise RuntimeError("RPC_E_CALL_REJECTED")
        return item

    monkeypatch.setattr(com, "_entity_info", entity_info)
    with pytest.raises(RuntimeError, match=r"1 entities could not be read \(Model\[1\]"):
        await entity_infos_all_spaces(_FakeCom())


async def test_qa_snapshot_runs_the_read_off_the_event_loop(backend):
    """The read is one locked call in a worker thread: the loop keeps turning."""
    msp = backend._doc.modelspace()
    for i in range(20_000):
        msp.add_line((0, 10 * i), (5, 10 * i))
    ticks = 0
    done = False

    async def ticker():
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    _snap, coverage = await qa_snapshot(backend, {})
    done = True
    await task
    assert coverage.records == 20_000
    assert ticks > 1
