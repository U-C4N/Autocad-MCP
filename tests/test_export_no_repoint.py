"""Task 17 (B23 residue): exporting a DXF never re-points the open document.

`drawing_save_as` keeps its re-pointing semantics; `drawing_export_dxf` writes a
copy on the headless engine and goes through `Document.Export` on the live one.
The live fake models only members measured on AutoCAD 2026 (25.1s) on
2026-10-24 in a scratch document: `Export(<base>, "DXF", <selection set>)`
wrote `<base>.DXF` (upper-case extension, AC1032) and left `Name`, `FullName`
and `Saved` as they were. It has no `SaveAs`: a call to it is the bug.
"""

from __future__ import annotations

import ezdxf
import pytest

import backends.com_backend as cb
from backends.base import UnsupportedCapabilityError
from backends.com_backend import ComBackend
from backends.quarantine import DaemonExecutor
from engineering.understand.snapshot import dxf_version_of
from engineering.verification.report import document_identity_of


def test_dxf_version_of_reads_the_acadver_header(tmp_path):
    for release, code in (("R2010", "AC1024"), ("R2018", "AC1032")):  # the DXF reference's codes
        path = tmp_path / f"{release}.dxf"
        ezdxf.new(release).saveas(path)
        assert dxf_version_of(path) == code
    text = tmp_path / "note.dxf"
    text.write_text("not a drawing", encoding="utf-8")
    assert dxf_version_of(text) is None
    assert dxf_version_of(tmp_path / "absent.dxf") is None


@pytest.mark.asyncio
async def test_headless_export_writes_a_copy_and_leaves_the_document_alone(backend, tmp_path):
    await backend.entity_create_circle(0, 0, 20)
    before_info = await backend.drawing_info()
    before = await document_identity_of(backend)
    target = tmp_path / "copy.dxf"
    result = await backend.drawing_export_dxf(str(target))
    after_info = await backend.drawing_info()
    after = await document_identity_of(backend)
    assert (after_info.full_path, after_info.saved) == ("", False)
    assert (after_info.full_path, after_info.saved) == (before_info.full_path, before_info.saved)
    assert after["revision"] == before["revision"]
    assert result["repointed"] is False
    assert result["method"] == "ezdxf.write"
    assert result["dxf_version"] == "AC1024"  # drawing_new writes R2010
    assert ezdxf.readfile(target).modelspace().query("CIRCLE")[0].dxf.radius == 20.0


@pytest.mark.asyncio
async def test_headless_export_still_refuses_a_dwg_name(backend, tmp_path):
    with pytest.raises(UnsupportedCapabilityError):
        await backend.drawing_export_dxf(str(tmp_path / "copy.dwg"))


@pytest.mark.asyncio
async def test_save_as_keeps_re_pointing_the_document(backend, tmp_path):
    target = tmp_path / "saved.dxf"
    await backend.drawing_save_as(str(target), "dxf")
    info = await backend.drawing_info()
    assert (info.full_path, info.saved) == (str(target), True)


class _Selection:
    def __init__(self, log):
        self.log = log

    def Select(self, mode):  # noqa: N802 - the ActiveX member name
        self.log.append(("Select", mode))

    def Delete(self):  # noqa: N802
        self.log.append(("Delete",))


class _SelectionSets:
    def __init__(self, log):
        self.log = log

    def Add(self, name):  # noqa: N802
        self.log.append(("Add", name))
        return _Selection(self.log)


class _Document:
    """The measured members only: Name, FullName, Saved, SelectionSets, Export."""

    def __init__(self):
        self.Name = "Drawing7.dwg"
        self.FullName = ""
        self.Saved = False
        self.log: list[tuple] = []
        self.SelectionSets = _SelectionSets(self.log)

    def Export(self, base, extension, selection):  # noqa: N802
        self.log.append(("Export", extension))
        if extension != "DXF":
            raise ValueError("Invalid argument")  # what ".dxf" got on AutoCAD 2026
        doc = ezdxf.new("R2018", units=4)
        doc.modelspace().add_circle((0, 0), 20)
        doc.saveas(base + ".DXF")


@pytest.fixture
def com_backend(monkeypatch):
    def _off_limits():
        raise AssertionError("this test must never reach the live AutoCAD")

    monkeypatch.setattr(cb, "_acad_app", _off_limits)
    backend = ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend
    backend._executor.shutdown(wait=False)
    backend._executor = None


@pytest.mark.asyncio
async def test_live_export_goes_through_document_export_and_renames_the_dxf(
    com_backend, tmp_path, monkeypatch
):
    fake = _Document()
    # _acad_doc() is where Task 9 runs the bound-document guard inside a job;
    # standing in for it here keeps the guard - and the live seat - out of the test.
    monkeypatch.setattr(cb, "_acad_doc", lambda: fake)
    target = tmp_path / "drawing.dxf"
    result = await com_backend.drawing_export_dxf(str(target))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["drawing.dxf"]
    assert (fake.Name, fake.FullName, fake.Saved) == ("Drawing7.dwg", "", False)
    assert ("Export", "DXF") in fake.log
    assert fake.log[-1] == ("Delete",)
    assert result["method"] == "Document.Export"
    assert result["repointed"] is False
    assert result["dxf_version"] == "AC1032"  # R2018, what AutoCAD 2026's Export wrote
    assert result["document"] == {"name": "Drawing7.dwg", "full_name": ""}
    assert ezdxf.readfile(target).modelspace().query("CIRCLE")[0].dxf.radius == 20.0


@pytest.mark.asyncio
async def test_live_export_refuses_a_name_that_is_not_dxf(com_backend, tmp_path, monkeypatch):
    fake = _Document()
    monkeypatch.setattr(cb, "_acad_doc", lambda: fake)
    with pytest.raises(ValueError, match="name the file .dxf"):
        await com_backend.drawing_export_dxf(str(tmp_path / "drawing.dwg"))
    assert fake.log == []
