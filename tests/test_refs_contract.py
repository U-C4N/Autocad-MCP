"""External references, raster images and DWG output on both engines.

The three capability keys are the point of this file: a refusal the feature map
never mentions is a lie by omission, so every key raised here is asserted to be
declared on both backends.
"""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import pytest

from backends.base import AutoCADBackend, UnsupportedCapabilityError
from backends.com_backend import ComBackend
from backends.contracts.refs import (
    DWG_VERSIONS,
    XREF_ACTIONS,
    XREF_KINDS,
    RefsContract,
)
from backends.ezdxf_backend import EzdxfBackend

TRACK_G_KEYS = ("dwg_write", "xref_live", "xlsx_write")


def _features(backend) -> dict:
    return backend.capabilities().to_dict()["features"]


def test_the_contract_is_composed_into_both_backends():
    assert issubclass(AutoCADBackend, RefsContract)
    assert issubclass(EzdxfBackend, RefsContract)
    assert issubclass(ComBackend, RefsContract)
    for name in ("xref_attach", "xref_manage", "image_attach", "drawing_export_dwg"):
        assert callable(getattr(AutoCADBackend, name, None)), name


def test_the_three_keys_are_declared_in_both_capability_maps():
    ezdxf_features = _features(EzdxfBackend())
    com_features = _features(ComBackend())
    for key in TRACK_G_KEYS:
        assert key in ezdxf_features, f"{key} undeclared on ezdxf"
        assert key in com_features, f"{key} undeclared on com"


def test_com_declares_dwg_and_live_xrefs_and_ezdxf_does_not():
    com_features = _features(ComBackend())
    ezdxf_features = _features(EzdxfBackend())
    assert com_features["dwg_write"]["supported"] is True
    assert com_features["xref_live"]["supported"] is True
    assert ezdxf_features["xref_live"]["supported"] is False
    assert "live" in (ezdxf_features["xref_live"]["reason"] or "")


def test_ezdxf_dwg_write_is_re_evaluated_from_the_converter_not_hardcoded():
    """The same shape as viewport_render's matplotlib check: the flag follows
    the machine, and the reason names the way to change it."""
    from ezdxf.addons import odafc

    feature = _features(EzdxfBackend())["dwg_write"]
    assert feature["supported"] is bool(odafc.is_installed())
    if not feature["supported"]:
        assert "oda" in (feature["reason"] or "").lower()


def test_xlsx_write_follows_openpyxl_on_both_engines():
    present = find_spec("openpyxl") is not None
    for backend in (EzdxfBackend(), ComBackend()):
        feature = _features(backend)["xlsx_write"]
        assert feature["supported"] is present, backend.name
        if not present:
            assert "openpyxl" in (feature["reason"] or "")


def test_the_vocabularies_are_closed():
    assert XREF_KINDS == ("attach", "overlay")
    assert XREF_ACTIONS == ("list", "reload", "bind", "detach", "path")
    assert DWG_VERSIONS == ("R2000", "R2004", "R2007", "R2010", "R2013", "R2018")


# -- ezdxf -------------------------------------------------------------------


@pytest.fixture
def external(tmp_path: Path) -> str:
    """A real DXF on disk for the xref to point at."""
    import ezdxf

    doc = ezdxf.new("R2018")
    doc.modelspace().add_circle((0, 0), 5)
    target = tmp_path / "BASE.dxf"
    doc.saveas(target)
    return str(target)


@pytest.mark.asyncio
async def test_attach_creates_an_xref_block_and_an_insert(backend, external):
    result = await backend.xref_attach(external, (10.0, 20.0), 2.0, 30.0, "attach")
    assert result["ok"] is True
    assert result["name"] == "BASE"
    assert result["kind"] == "attach"
    insert = await backend.entity_get(result["handle"])
    assert insert.type == "INSERT"
    assert insert.properties["block_name"] == "BASE"

    listing = await backend.xref_manage("", "list", None)
    assert [row["name"] for row in listing["xrefs"]] == ["BASE"]
    assert listing["xrefs"][0]["kind"] == "attach"
    assert Path(listing["xrefs"][0]["path"]).name == "BASE.dxf"
    assert listing["xrefs"][0]["inserts"] == 1


@pytest.mark.asyncio
async def test_overlay_is_flagged_as_an_overlay(backend, external):
    await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "overlay")
    listing = await backend.xref_manage("", "list", None)
    assert listing["xrefs"][0]["kind"] == "overlay"


@pytest.mark.asyncio
async def test_attaching_the_same_name_twice_is_refused_before_writing(backend, external):
    await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    before = len(await backend.entity_list())
    with pytest.raises(ValueError) as excinfo:
        await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    assert "BASE" in str(excinfo.value)
    assert len(await backend.entity_list()) == before


@pytest.mark.asyncio
async def test_a_missing_file_and_a_bad_kind_are_refused(backend, tmp_path):
    with pytest.raises(ValueError) as missing:
        await backend.xref_attach(str(tmp_path / "nope.dxf"), (0.0, 0.0), 1.0, 0.0, "attach")
    assert "nope.dxf" in str(missing.value)
    with pytest.raises(ValueError) as kind:
        await backend.xref_attach(str(tmp_path), (0.0, 0.0), 1.0, 0.0, "underlay")
    assert "underlay" in str(kind.value)


@pytest.mark.asyncio
async def test_path_rewrites_the_saved_path_and_detach_removes_the_block(
    backend, external, tmp_path
):
    await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    moved = str(tmp_path / "moved" / "BASE.dxf")
    changed = await backend.xref_manage("BASE", "path", moved)
    assert changed["ok"] is True
    assert changed["path"] == moved
    assert (await backend.xref_manage("", "list", None))["xrefs"][0]["path"] == moved

    detached = await backend.xref_manage("BASE", "detach", None)
    assert detached["ok"] is True and detached["inserts_removed"] == 1
    assert (await backend.xref_manage("", "list", None))["xrefs"] == []


@pytest.mark.asyncio
async def test_an_xref_shown_on_a_sheet_is_counted_and_detached_with_it(backend, external):
    """Model space is not the drawing.

    An xref is most often *shown* on a paper-space sheet. Counting or deleting
    over `doc.modelspace()` alone reported 1 insert where the drawing held 2
    and then removed the definition with `safe=False`, leaving the sheet's
    INSERT with no BLOCK behind it -- which ezdxf's own recover reports as
    `FIX 103 UNDEFINED_BLOCK` and throws the geometry away.
    """
    await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    doc = backend._doc
    doc.layout("Layout1").add_blockref("BASE", (10, 10))

    listing = await backend.xref_manage("", "list", None)
    assert listing["xrefs"][0]["inserts"] == 2

    detached = await backend.xref_manage("BASE", "detach", None)
    assert detached["inserts_removed"] == 2
    assert "BASE" not in doc.blocks
    assert [e for e in doc.layout("Layout1") if e.dxftype() == "INSERT"] == []


@pytest.mark.asyncio
async def test_detach_leaves_a_drawing_that_survives_a_round_trip(backend, external, tmp_path):
    """The evidence a caller actually cares about: the saved file audits clean."""
    import ezdxf.recover

    await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    backend._doc.layout("Layout1").add_blockref("BASE", (10, 10))
    await backend.xref_manage("BASE", "detach", None)

    out = tmp_path / "detached.dxf"
    backend._doc.saveas(out)
    _, auditor = ezdxf.recover.readfile(str(out))
    assert [f.message for f in auditor.fixes] == []
    assert [e.message for e in auditor.errors] == []


@pytest.mark.asyncio
async def test_the_definition_is_dropped_with_the_in_use_check_on(backend, external):
    """`safe=True` is the seatbelt, not decoration.

    The broken version passed `safe=False`, the flag that *suppresses* the
    in-use check, so a missed insert became a corrupt file instead of an
    error. This pins the check: with the model-space insert gone but the
    sheet's still there -- exactly the state the old victim scan produced --
    `delete_block` refuses.
    """
    from ezdxf.lldxf.const import DXFBlockInUseError

    await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    doc = backend._doc
    doc.layout("Layout1").add_blockref("BASE", (10, 10))
    msp = doc.modelspace()
    for entity in [e for e in msp if e.dxftype() == "INSERT"]:
        msp.delete_entity(entity)

    with pytest.raises(DXFBlockInUseError):
        doc.blocks.delete_block("BASE", safe=True)
    assert "BASE" in doc.blocks


@pytest.mark.asyncio
async def test_reload_and_bind_refuse_headlessly_with_the_xref_live_key(backend, external):
    await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    for action in ("reload", "bind"):
        with pytest.raises(UnsupportedCapabilityError) as excinfo:
            await backend.xref_manage("BASE", action, None)
        assert excinfo.value.capability == "xref_live"
        assert "com" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_an_unknown_action_and_an_unknown_name_are_refused(backend, external):
    await backend.xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    with pytest.raises(ValueError) as action:
        await backend.xref_manage("BASE", "unload", None)
    assert "unload" in str(action.value)
    with pytest.raises(ValueError) as name:
        await backend.xref_manage("NOPE", "detach", None)
    assert "NOPE" in str(name.value)


@pytest.mark.asyncio
async def test_image_attach_places_the_raster_at_its_pixel_aspect(backend, tmp_path):
    from PIL import Image

    png = tmp_path / "logo.png"
    Image.new("RGB", (800, 400), "white").save(png)
    result = await backend.image_attach(str(png), (5.0, 5.0), 0.1, 0.0)
    assert result["ok"] is True
    assert result["pixels"] == [800, 400]
    assert result["size_mm"] == [80.0, 40.0]
    image = await backend.entity_get(result["handle"])
    assert image.type == "IMAGE"


@pytest.mark.asyncio
async def test_a_non_image_file_is_refused_before_writing(backend, external):
    before = len(await backend.entity_list())
    with pytest.raises(ValueError) as excinfo:
        await backend.image_attach(external, (0.0, 0.0), 1.0, 0.0)
    assert "BASE.dxf" in str(excinfo.value)
    assert len(await backend.entity_list()) == before


@pytest.mark.asyncio
async def test_headless_dwg_export_refuses_with_dwg_write_when_the_converter_is_absent(
    backend, tmp_path
):
    from ezdxf.addons import odafc

    if odafc.is_installed():
        pytest.skip("the ODA File Converter is installed on this machine")
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.drawing_export_dwg(str(tmp_path / "out.dwg"), "R2018")
    assert excinfo.value.capability == "dwg_write"
    assert "oda" in str(excinfo.value).lower()
    assert not (tmp_path / "out.dwg").exists()


@pytest.mark.asyncio
async def test_an_unknown_dwg_version_is_refused_on_both_engines(backend, tmp_path):
    with pytest.raises(ValueError) as excinfo:
        await backend.drawing_export_dwg(str(tmp_path / "out.dwg"), "R14")
    message = str(excinfo.value)
    assert "R14" in message
    assert "R2018" in message


# -- COM, against fakes that model the measured ActiveX members --------------


def _com_backend():
    b = ComBackend()

    async def _run(func, *args, **kwargs):  # bypass the single-thread executor
        return func(*args, **kwargs)

    b._run = _run
    return b


def _point(variant):
    """The doubles a VARIANT point carries.

    ``_apoint`` hands ActiveX a ``win32com.client.VARIANT``, and that object is
    *not* iterable -- measured here:
    ``tuple(VARIANT(VT_ARRAY | VT_R8, [1.0, 2.0, 0.0]))`` raises
    ``TypeError: 'VARIANT' object is not iterable``. The real ModelSpace reads
    the array behind it, so the fakes read ``.value`` rather than pretending
    the argument is a plain sequence.
    """
    return tuple(getattr(variant, "value", variant))


class _Block:
    """Measured members of an AcadBlock that is an xref (AutoCAD 2026)."""

    def __init__(self, name, path="", is_xref=True):
        self.Name = name
        self.Path = path
        self.IsXRef = is_xref
        self.calls = []

    def Reload(self):
        self.calls.append("Reload")

    def Bind(self, bPrefixName):
        self.calls.append(("Bind", bPrefixName))

    def Detach(self):
        self.calls.append("Detach")


class _Blocks:
    def __init__(self, blocks):
        self._blocks = list(blocks)

    def Item(self, index):
        if isinstance(index, int):
            return self._blocks[index]
        for block in self._blocks:
            if block.Name == index:
                return block
        raise KeyError(index)

    @property
    def Count(self):
        return len(self._blocks)

    def __iter__(self):
        return iter(self._blocks)


class _Ref:
    def __init__(self, handle="7F"):
        self.Handle = handle


class _ModelSpace:
    def __init__(self):
        self.attached = []
        self.rasters = []

    def AttachExternalReference(
        self, PathName, Name, InsertionPoint, Xscale, Yscale, Zscale, Rotation, bOverlay
    ):
        self.attached.append(
            (PathName, Name, _point(InsertionPoint), Xscale, Yscale, Zscale, Rotation, bOverlay)
        )
        return _Ref("A1")

    def AddRaster(self, imageFileName, InsertionPoint, ScaleFactor, RotationAngle):
        self.rasters.append((imageFileName, _point(InsertionPoint), ScaleFactor, RotationAngle))
        return _Ref("B2")


class _ComDoc:
    def __init__(self, blocks=()):
        self.ModelSpace = _ModelSpace()
        self.Blocks = _Blocks(blocks)
        self.saved = []

    def SaveAs(self, FullFileName, SaveAsType):
        self.saved.append((FullFileName, SaveAsType))


@pytest.mark.asyncio
async def test_com_attach_calls_the_measured_member_with_radians(monkeypatch, external):
    import math

    import backends.com_backend as cb

    doc = _ComDoc()
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    result = await _com_backend().xref_attach(external, (10.0, 20.0), 2.0, 90.0, "overlay")
    assert result["handle"] == "A1"
    path, name, insert, sx, sy, sz, rotation, overlay = doc.ModelSpace.attached[0]
    assert name == "BASE" and overlay is True
    assert (sx, sy, sz) == (2.0, 2.0, 2.0)
    assert rotation == pytest.approx(math.pi / 2)
    assert insert[:2] == (10.0, 20.0)


@pytest.mark.asyncio
async def test_com_manage_uses_reload_bind_detach_on_the_block(monkeypatch):
    import backends.com_backend as cb

    block = _Block("BASE", path="C:/refs/BASE.dwg")
    doc = _ComDoc([block])
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    b = _com_backend()

    assert (await b.xref_manage("BASE", "reload", None))["ok"] is True
    assert (await b.xref_manage("BASE", "bind", None))["ok"] is True
    assert (await b.xref_manage("BASE", "detach", None))["ok"] is True
    assert block.calls == ["Reload", ("Bind", False), "Detach"]

    listing = await b.xref_manage("", "list", None)
    assert listing["xrefs"][0]["name"] == "BASE"
    assert listing["xrefs"][0]["path"] == "C:/refs/BASE.dwg"


@pytest.mark.asyncio
async def test_com_list_reports_kind_as_unknown_rather_than_inventing_attach(monkeypatch):
    """IAcadBlock has no overlay indicator.

    Measured from the seat's own registered type library (acax25*.tlb,
    "AutoCAD 2025 Type Library", installed with AutoCAD 2026): the xref
    members are IsXRef, Path, Name, Reload, Unload, Bind, Detach,
    XRefDatabase -- nothing says attach-vs-overlay. The fake therefore has no
    such member either (a fake that models one enshrines the bug), and the
    honest row says `None`, the way `inserts` already does.
    """
    import backends.com_backend as cb

    doc = _ComDoc([_Block("BASE", path="C:/refs/BASE.dwg")])
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    row = (await _com_backend().xref_manage("", "list", None))["xrefs"][0]
    assert row["kind"] is None
    assert row["inserts"] is None
    for invented in ("IsOverlay", "Overlay", "XRefType"):
        assert not hasattr(doc.Blocks.Item("BASE"), invented)


@pytest.mark.asyncio
async def test_com_attach_refuses_a_name_the_drawing_already_holds(monkeypatch, external):
    """The refusal the contract and the tool docstring both promise.

    It has to be on this engine too, and it has to fire *before*
    AttachExternalReference is dispatched -- a second attach under a held name
    re-points or half-writes the definition while the payload still claims the
    new path was attached.
    """
    import backends.com_backend as cb

    doc = _ComDoc([_Block("BASE", path="C:/other/BASE.dwg")])
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    with pytest.raises(ValueError) as excinfo:
        await _com_backend().xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    assert "BASE" in str(excinfo.value)
    assert "detach" in str(excinfo.value)
    assert doc.ModelSpace.attached == []


@pytest.mark.asyncio
async def test_com_attach_refuses_a_plain_block_of_the_same_name_case_insensitively(
    monkeypatch, external
):
    """AutoCAD symbol-table names are case-insensitive and a collision is a
    collision whether or not the block in the way is itself an xref."""
    import backends.com_backend as cb

    doc = _ComDoc([_Block("Base", is_xref=False)])
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    with pytest.raises(ValueError):
        await _com_backend().xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    assert doc.ModelSpace.attached == []


@pytest.mark.asyncio
async def test_com_attach_still_works_when_no_block_of_that_name_exists(monkeypatch, external):
    import backends.com_backend as cb

    doc = _ComDoc([_Block("OTHER", is_xref=True)])
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    result = await _com_backend().xref_attach(external, (0.0, 0.0), 1.0, 0.0, "attach")
    assert result["ok"] is True and result["name"] == "BASE"
    assert len(doc.ModelSpace.attached) == 1


@pytest.mark.asyncio
async def test_com_raster_calls_addraster_with_radians(monkeypatch, tmp_path):
    import math

    from PIL import Image

    import backends.com_backend as cb

    png = tmp_path / "logo.png"
    Image.new("RGB", (400, 200), "white").save(png)
    doc = _ComDoc()
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    result = await _com_backend().image_attach(str(png), (1.0, 2.0), 0.5, 180.0)
    assert result["handle"] == "B2"
    name, insert, scale, rotation = doc.ModelSpace.rasters[0]
    assert Path(name).name == "logo.png"
    assert insert[:2] == (1.0, 2.0)
    assert scale == 0.5
    assert rotation == pytest.approx(math.pi)


@pytest.mark.asyncio
async def test_com_dwg_export_uses_the_measured_acsaveastype_values(monkeypatch, tmp_path):
    import backends.com_backend as cb

    doc = _ComDoc()
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    b = _com_backend()
    await b.drawing_export_dwg(str(tmp_path / "a.dwg"), "R2018")
    await b.drawing_export_dwg(str(tmp_path / "b.dwg"), "R2000")
    assert [entry[1] for entry in doc.saved] == [64, 12]
