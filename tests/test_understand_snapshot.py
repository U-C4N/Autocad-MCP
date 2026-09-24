"""The snapshot: one bulk read of a drawing into frozen WCS records.

Every expected coordinate and length is worked out by hand in the comment
beside it. The live engine's export is exercised against a fake document that
behaves the way AutoCAD 2026 was measured to behave (the extension must be
"DXF", the file lands as snap.DXF, the document is not renamed).
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import ezdxf
import pytest
from ezdxf.math import Vec2

import backends.com_backend as cb
import config
from backends.base import UnsupportedCapabilityError
from backends.com_backend import ComBackend
from backends.ezdxf_backend import EzdxfBackend
from engineering.understand.snapshot import (
    EntityRecord,
    Snapshot,
    length_of,
    read_snapshot,
    records_from_doc,
    take_snapshot,
)

EPS = 1e-9


def _doc():
    """A drawing with one entity of each kind the readers use."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("PIPE", color=3, linetype="Continuous")
    msp = doc.modelspace()
    msp.add_line((0, 0), (3, 4), dxfattribs={"layer": "PIPE"})
    # a closed square whose first edge bulges into a semicircle (bulge 1)
    msp.add_lwpolyline(
        [(0, 0, 1.0), (10, 0, 0.0), (10, 10, 0.0), (0, 10, 0.0)],
        format="xyb",
        close=True,
        dxfattribs={"layer": "PIPE"},
    )
    msp.add_polyline2d([(0, 0), (0, 5), (5, 5)], dxfattribs={"layer": "PIPE"})
    msp.add_arc((100, 0), 10, 0, 90, dxfattribs={"layer": "PIPE"})
    # the same quarter arc mirrored: extrusion (0, 0, -1), OCS centre (-200, 0)
    # is WCS (200, 0), and the arc runs from 90 to 180 degrees in WCS
    msp.add_arc((-200, 0), 10, 0, 90, dxfattribs={"layer": "PIPE", "extrusion": (0, 0, -1)})
    msp.add_circle((50, 50), 5)
    msp.add_text("%%c51", dxfattribs={"insert": (20, 20), "height": 2.5, "layer": "TXT"})
    msp.add_mtext("{\\fArial|b0;SMS51}\\PDN20", dxfattribs={"insert": (30, 30), "char_height": 3.5})
    block = doc.blocks.new("TANK_V")
    block.add_circle((0, 0), 600)
    block.add_attdef("TAG", (0, 0), dxfattribs={"height": 250})
    insert = msp.add_blockref("TANK_V", (1000, 2000), dxfattribs={"rotation": 90})
    insert.add_auto_attribs({"TAG": "Т4100"})  # Cyrillic Т
    leader = msp.add_multileader_mtext("Standard")
    leader.set_content("Wiring to CP1", char_height=2.5)
    leader.add_leader_line(ezdxf.render.mleader.ConnectionSide.left, [Vec2(0, 0)])
    leader.build(insert=Vec2(50, 20))
    doc.layouts.get("Layout1").add_line((1, 1), (2, 1))
    return doc


def _by_type(snap: Snapshot) -> dict[str, list[EntityRecord]]:
    out: dict[str, list[EntityRecord]] = {}
    for rec in snap.records:
        out.setdefault(rec.type, []).append(rec)
    return out


def test_records_carry_wcs_geometry_plain_text_and_the_space():
    snap = records_from_doc(_doc(), source="memory")
    kinds = _by_type(snap)
    assert snap.source == "memory"
    assert snap.insunits == 4
    assert snap.layouts == ("Model", "Layout1")
    assert snap.layers["PIPE"] == {"color": 3, "linetype": "Continuous"}
    (line_model,) = [r for r in kinds["LINE"] if r.space == "Model"]
    (line_paper,) = [r for r in kinds["LINE"] if r.space == "Layout1"]
    assert line_model.points == ((0.0, 0.0), (3.0, 4.0))
    assert line_model.layer == "PIPE"
    assert line_paper.points == ((1.0, 1.0), (2.0, 1.0))
    (square,) = kinds["LWPOLYLINE"]
    assert square.closed is True
    assert square.bulges == (1.0, 0.0, 0.0, 0.0)
    (poly,) = kinds["POLYLINE"]
    assert poly.points == ((0.0, 0.0), (0.0, 5.0), (5.0, 5.0)) and poly.closed is False
    arcs = sorted(kinds["ARC"], key=lambda r: r.points[0][0])
    assert arcs[0].points == ((100.0, 0.0),) and arcs[0].angles == pytest.approx((0.0, 90.0))
    assert arcs[1].points[0] == pytest.approx((200.0, 0.0), abs=EPS)
    assert arcs[1].angles == pytest.approx((90.0, 180.0), abs=EPS)
    (text,) = kinds["TEXT"]
    assert text.text == "Ø51" and text.height == 2.5 and text.points == ((20.0, 20.0),)
    (mtext,) = kinds["MTEXT"]
    assert mtext.text == "SMS51\nDN20" and mtext.height == 3.5
    (insert,) = kinds["INSERT"]
    assert insert.block == "TANK_V" and insert.points == ((1000.0, 2000.0),)
    assert insert.attribs == (("TAG", "Т4100"),)
    assert insert.rotation == 90.0
    assert insert.bbox == pytest.approx((400.0, 1400.0, 1600.0, 2600.0), abs=1e-6)
    (leader,) = kinds["MULTILEADER"]
    assert leader.text == "Wiring to CP1"
    assert leader.points[0] == (0.0, 0.0)  # the arrow tip comes first
    assert leader.points[-1] == (50.0, 20.0)  # the text insertion comes last
    assert snap.extmin is None and snap.extmax is None  # never computed


def test_every_record_is_frozen():
    rec = records_from_doc(_doc(), source="memory").records[0]
    with pytest.raises(AttributeError):
        rec.layer = "OTHER"


def test_lengths():
    kinds = _by_type(records_from_doc(_doc(), source="memory"))
    (line_model,) = [r for r in kinds["LINE"] if r.space == "Model"]
    assert length_of(line_model) == pytest.approx(5.0, abs=EPS)  # 3-4-5
    # semicircle of diameter 10 (5 pi) + three straight sides of 10
    (square,) = kinds["LWPOLYLINE"]
    assert length_of(square) == pytest.approx(5.0 * math.pi + 30.0, abs=EPS)
    (poly,) = kinds["POLYLINE"]
    assert length_of(poly) == pytest.approx(10.0, abs=EPS)
    for arc in kinds["ARC"]:  # a quarter of a radius-10 circle
        assert length_of(arc) == pytest.approx(5.0 * math.pi, abs=EPS)
    (circle,) = kinds["CIRCLE"]
    assert length_of(circle) == pytest.approx(10.0 * math.pi, abs=EPS)
    assert length_of(kinds["TEXT"][0]) == 0.0


def test_the_size_limit_defaults_to_512_mb(monkeypatch):
    monkeypatch.delenv("MAX_DXF_BYTES", raising=False)
    assert config.Settings().max_dxf_bytes == 512 * 1024 * 1024
    assert config.DEFAULT_MAX_DXF_BYTES == 536_870_912


def test_read_snapshot_reads_a_file(tmp_path):
    path = tmp_path / "plant.dxf"
    _doc().saveas(path)
    snap = read_snapshot(str(path))
    assert snap.source == str(path)
    assert len(snap.records) == len(records_from_doc(_doc(), source="x").records)


def test_read_snapshot_refuses_a_file_over_the_limit_naming_size_and_variable(
    tmp_path, monkeypatch
):
    path = tmp_path / "big.dxf"
    _doc().saveas(path)
    size = path.stat().st_size
    monkeypatch.setattr(config.settings, "max_dxf_bytes", size - 1)
    with pytest.raises(ValueError) as excinfo:
        read_snapshot(str(path))
    message = str(excinfo.value)
    assert "MAX_DXF_BYTES" in message
    assert f"{size:,} bytes" in message
    monkeypatch.setattr(config.settings, "max_dxf_bytes", 0)  # 0 disables the check
    assert read_snapshot(str(path)).records


def test_read_snapshot_refuses_missing_and_unreadable_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_snapshot(str(tmp_path / "nope.dxf"))
    junk = tmp_path / "junk.dxf"
    junk.write_text("not a dxf", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        read_snapshot(str(junk))
    assert "not a readable DXF" in str(excinfo.value)


def test_a_truncated_dxf_is_refused_not_left_hanging(tmp_path):
    # An interrupted save: the header opens and the file ends. ezdxf's tag reader
    # raises StopIteration here, which asyncio.to_thread cannot hand back to an
    # awaiting tool - the call would never answer. It must be the same refusal
    # as any other unreadable file, also through a worker thread.
    half = tmp_path / "half.dxf"
    half.write_text("  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1015\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a readable DXF"):
        read_snapshot(str(half))

    async def through_a_thread():
        return await asyncio.wait_for(asyncio.to_thread(read_snapshot, str(half)), timeout=10)

    with pytest.raises(ValueError, match="not a readable DXF"):
        asyncio.run(through_a_thread())


# -- take_snapshot on the headless engine ---------------------------------------


@pytest.mark.asyncio
async def test_the_headless_engine_snapshots_its_current_document_without_touching_it():
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    line = await backend.entity_create_line(0, 0, 10, 0, layer="PIPE")
    before = len(await backend.entity_list(limit=1000))
    snap = await take_snapshot(backend)
    assert snap.source == "memory:untitled"
    assert [r.handle for r in snap.records if r.type == "LINE"] == [line.handle]
    assert len(await backend.entity_list(limit=1000)) == before


@pytest.mark.asyncio
async def test_a_dxf_path_is_read_directly(tmp_path):
    path = tmp_path / "plant.dxf"
    _doc().saveas(path)
    backend = EzdxfBackend()
    await backend.connect()
    snap = await take_snapshot(backend, str(path))
    assert snap.source == str(path) and snap.records


@pytest.mark.asyncio
async def test_a_dwg_is_refused_headlessly_with_the_dwg_capability(tmp_path):
    path = tmp_path / "plant.dwg"
    path.write_bytes(b"AC1032")
    backend = EzdxfBackend()
    await backend.connect()
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await take_snapshot(backend, str(path))
    assert excinfo.value.capability == "dwg"
    assert "DXF" in str(excinfo.value)
    with pytest.raises(FileNotFoundError):
        await take_snapshot(backend, str(tmp_path / "missing.dwg"))
    with pytest.raises(ValueError):
        await take_snapshot(backend, str(tmp_path / "plant.txt"))


# -- take_snapshot on the live engine (a fake that behaves as measured) ----------


class _Selection:
    def __init__(self, log):
        self.log = log

    def Select(self, mode, *args):  # noqa: N802 - ActiveX member name
        self.log.append(("select", mode))

    def Delete(self):  # noqa: N802
        self.log.append(("delete_selection",))


class _Selections:
    def __init__(self, log):
        self.log = log

    def Add(self, name):  # noqa: N802
        self.log.append(("add_selection", name))
        return _Selection(self.log)


class _Doc:
    def __init__(self, name, log):
        self.Name = name
        self.log = log
        self.SelectionSets = _Selections(log)

    def Export(self, base, extension, selection):  # noqa: N802
        if extension != "DXF":
            raise ValueError("Invalid argument")  # what AutoCAD 2026 answers to ".dxf"
        self.log.append(("export", Path(base).name, extension))
        _doc().saveas(base + ".DXF")  # AutoCAD writes the upper-case extension

    def SaveAs(self, *args):  # noqa: N802
        raise AssertionError("a snapshot must never rename the document")

    def Close(self, save):  # noqa: N802
        self.log.append(("close", save))


class _Documents:
    def __init__(self, log):
        self.log = log

    def Open(self, path, read_only):  # noqa: N802
        self.log.append(("open", Path(path).name, read_only))
        return _Doc(Path(path).name, self.log)


class _App:
    def __init__(self, log):
        self.Documents = _Documents(log)


def _live(monkeypatch, log):
    backend = ComBackend()

    async def run_inline(func, *args, **kwargs):  # bypass the STA executor
        return func(*args, **kwargs)

    async def ensure_state():
        log.append(("ensure_document_state",))

    backend._run = run_inline
    backend._ensure_document_state = ensure_state
    active = _Doc("Drawing1.dwg", log)
    monkeypatch.setattr(cb, "_acad_app", lambda: _App(log))
    monkeypatch.setattr(cb, "_acad_doc", lambda: active)
    return backend, active


@pytest.mark.asyncio
async def test_the_live_engine_exports_a_dxf_snapshot_and_deletes_it(monkeypatch, tmp_path):
    log: list[tuple] = []
    made: list[str] = []
    real_mkdtemp = __import__("tempfile").mkdtemp

    def mkdtemp(prefix=""):
        folder = real_mkdtemp(prefix=prefix, dir=tmp_path)
        made.append(folder)
        return folder

    monkeypatch.setattr("engineering.understand.snapshot.tempfile.mkdtemp", mkdtemp)
    backend, active = _live(monkeypatch, log)
    snap = await take_snapshot(backend)
    assert snap.source == "live:Drawing1.dwg"
    assert active.Name == "Drawing1.dwg"
    assert ("select", cb._AC_SELECTION_SET_ALL) in log
    assert ("export", "snap", "DXF") in log
    assert ("delete_selection",) in log
    assert {r.type for r in snap.records} >= {"LINE", "INSERT", "MULTILEADER"}
    assert made and not Path(made[0]).exists(), "the temporary snapshot must be deleted"


@pytest.mark.asyncio
async def test_a_dwg_on_the_live_engine_is_opened_read_only_and_closed_unsaved(
    monkeypatch, tmp_path
):
    path = tmp_path / "plant.dwg"
    path.write_bytes(b"AC1032")
    log: list[tuple] = []
    backend, _active = _live(monkeypatch, log)
    snap = await take_snapshot(backend, str(path))
    assert snap.source == "live:plant.dwg"
    assert ("open", "plant.dwg", True) in log
    assert ("close", False) in log
    assert log[-1] == ("ensure_document_state",)
