"""Page setup on both engines; the PDF's own /MediaBox is the evidence.

ezdxf: apply a paper, export the layout, parse the PDF back and compare to the
paper size. COM: a fake ActiveX layout records the property sequence.
"""

from __future__ import annotations

import re
import types
import zlib

import pytest

from engineering.standards.papers import (
    CTB_CATALOG,
    PAPER_SIZES,
    SCALES,
    canonical_media_name,
    paper_from_size,
    resolve_page_setup,
    scale_label,
    scale_ratio,
)
from engineering.standards.pdfinfo import read_mediabox

pytestmark = pytest.mark.asyncio

SHEET = "Sheet"


# ── pure data ───────────────────────────────────────────────────────────────


def test_paper_sizes_are_iso_216_and_ansi_y14_1():
    assert PAPER_SIZES == {
        "ISO_A0": (841, 1189),
        "ISO_A1": (594, 841),
        "ISO_A2": (420, 594),
        "ISO_A3": (297, 420),
        "ISO_A4": (210, 297),
        "ANSI_A": (216, 279),
        "ANSI_B": (279, 432),
        "ANSI_C": (432, 559),
        "ANSI_D": (559, 864),
        "ANSI_E": (864, 1118),
    }
    assert CTB_CATALOG[0] == "monochrome.ctb" and "acad.ctb" in CTB_CATALOG
    assert SCALES["fit"] is None and SCALES["1:50"] == 0.02 and SCALES["10:1"] == 10.0


def test_canonical_media_name_uses_autocad_spelling():
    assert canonical_media_name("ISO_A3", "landscape") == "ISO_A3_(420.00_x_297.00_MM)"
    assert canonical_media_name("ISO_A3", "portrait") == "ISO_A3_(297.00_x_420.00_MM)"
    assert canonical_media_name("ANSI_B", "landscape") == "ANSI_B_(17.00_x_11.00_Inches)"


def test_paper_from_size_matches_either_orientation_within_half_a_millimetre():
    assert paper_from_size(420, 297) == "ISO_A3"
    assert paper_from_size(297, 420) == "ISO_A3"
    assert paper_from_size(431.8, 279.4) == "ANSI_B"
    assert paper_from_size(400, 300) is None


def test_scale_ratio_and_label_round_trip():
    assert scale_ratio("1:50") == (1.0, 50.0)
    assert scale_ratio("2:1") == (2.0, 1.0)
    assert scale_label(1.0, 50.0) == "1:50"
    assert scale_label(1.0, 3.0) == "1:3"
    with pytest.raises(ValueError, match="scale"):
        scale_ratio("fit")


def test_resolve_page_setup_defaults():
    setup = resolve_page_setup("ISO_A3")
    assert setup["paper"] == "ISO_A3"
    assert setup["orientation"] == "landscape"
    assert setup["size_mm"] == [420.0, 297.0]
    assert setup["canonical_media_name"] == "ISO_A3_(420.00_x_297.00_MM)"
    assert setup["plot_style"] == "monochrome.ctb" and setup["plot_style_known"] is True
    assert setup["scale"] == "fit"
    assert setup["dxf_standard_scale_type"] == 0 and setup["activex_standard_scale"] == 0
    assert setup["scale_ratio"] == [1.0, 1.0]
    assert setup["plot_area"] == "layout" and setup["plot_type"] == 5
    assert setup["device"] == "DWG To PDF.pc3"
    assert setup["margins_mm"] is None and setup["center"] is True


def test_resolve_page_setup_accepts_lowercase_and_short_paper_names():
    assert resolve_page_setup("iso_a4", "portrait")["size_mm"] == [210.0, 297.0]
    assert resolve_page_setup("A3")["paper"] == "ISO_A3"
    assert resolve_page_setup("ansi_b")["canonical_media_name"] == "ANSI_B_(17.00_x_11.00_Inches)"


def test_resolve_page_setup_scale_codes():
    one_to_five = resolve_page_setup("ISO_A3", scale="1:5")
    assert one_to_five["dxf_standard_scale_type"] is None  # DXF code 75 has no 1:5
    assert one_to_five["activex_standard_scale"] == 19  # AcPlotScale.ac1_5
    assert one_to_five["scale_ratio"] == [1.0, 5.0]
    five_to_one = resolve_page_setup("ISO_A3", scale="5:1")
    assert five_to_one["dxf_standard_scale_type"] is None
    assert five_to_one["activex_standard_scale"] is None
    assert five_to_one["scale_ratio"] == [5.0, 1.0]
    assert resolve_page_setup("ISO_A3", scale="1:50")["dxf_standard_scale_type"] == 25
    assert resolve_page_setup("ISO_A3", scale="1:50")["activex_standard_scale"] == 26


@pytest.mark.parametrize(
    "kwargs, field",
    [
        ({"paper": "ISO_A9"}, "paper"),
        ({"paper": "ISO_A3", "orientation": "sideways"}, "orientation"),
        ({"paper": "ISO_A3", "scale": "1:3"}, "scale"),
        ({"paper": "ISO_A3", "plot_area": "window"}, "plot_area"),
        ({"paper": "ISO_A3", "device": ""}, "device"),
        ({"paper": "ISO_A3", "plot_style": ""}, "plot_style"),
        ({"paper": "ISO_A3", "margins_mm": [1, 2, 3]}, "margins_mm"),
        ({"paper": "ISO_A3", "margins_mm": [-1, 0, 0, 0]}, "margins_mm"),
        ({"paper": "ISO_A3", "margins_mm": [150, 150, 0, 0]}, "margins_mm"),
        ({"paper": "ISO_A3", "center": "yes"}, "center"),
    ],
)
def test_resolve_page_setup_refuses_naming_the_field(kwargs, field):
    with pytest.raises(ValueError, match=field):
        resolve_page_setup(**kwargs)


def test_unknown_ctb_is_allowed_but_flagged():
    setup = resolve_page_setup("ISO_A3", plot_style="company.ctb")
    assert setup["plot_style"] == "company.ctb" and setup["plot_style_known"] is False


# ── the PDF reader ──────────────────────────────────────────────────────────


def test_read_mediabox_refuses_a_non_pdf(tmp_path):
    bogus = tmp_path / "x.pdf"
    bogus.write_bytes(b"not a pdf")
    with pytest.raises(ValueError, match="PDF"):
        read_mediabox(str(bogus))


def test_read_mediabox_finds_a_box_inside_a_flate_stream(tmp_path):
    body = zlib.compress(b"<< /Type /Page /MediaBox [0 0 595.276 841.89] >>")
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(
        b"%PDF-1.5\n1 0 obj\n<< /Type /ObjStm /Filter /FlateDecode >>\nstream\n"
        + body
        + b"\nendstream\nendobj\n%%EOF"
    )
    width, height = read_mediabox(str(pdf))
    assert abs(width - 210.0) < 0.05 and abs(height - 297.0) < 0.05


def test_read_mediabox_reports_a_missing_box(tmp_path):
    pdf = tmp_path / "n.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF")
    with pytest.raises(ValueError, match="MediaBox"):
        read_mediabox(str(pdf))


# ── headless engine ─────────────────────────────────────────────────────────


async def _sheet(backend, name: str = SHEET) -> str:
    result = await backend.layout_create(name)
    assert result["ok"], result
    return name


async def _mediabox_after_export(backend, tmp_path, layout: str):
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    pdf = tmp_path / f"{layout}.pdf"
    result = await backend.drawing_export_pdf(str(pdf), layout=layout)
    assert result["ok"] is True, result
    return read_mediabox(str(pdf))


async def test_apply_iso_a3_landscape_and_the_pdf_mediabox_agrees(backend, tmp_path):
    await _sheet(backend)
    result = await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", "landscape"))
    assert result["ok"] is True and result["layout"] == SHEET
    assert result["applied"]["canonical_media_name"] == "ISO_A3_(420.00_x_297.00_MM)"
    assert result["plot_style_known"] is True
    assert result["viewports_kept"] is True

    rows = await backend.page_setup_list(SHEET)
    assert len(rows) == 1
    row = rows[0]
    assert row["layout"] == SHEET
    assert row["paper"] == "ISO_A3"
    assert row["canonical_media_name"] == "ISO_A3_(420.00_x_297.00_MM)"
    assert row["size_mm"] == [420.0, 297.0]
    assert row["orientation"] == "landscape"
    assert row["plot_style"] == "monochrome.ctb"
    assert row["scale"] == "fit"
    assert row["plot_area"] == "layout"
    assert row["device"] == "DWG To PDF.pc3"
    assert row["center"] is True
    assert len(row["margins_mm"]) == 4

    width, height = await _mediabox_after_export(backend, tmp_path, SHEET)
    assert abs(width - 420.0) <= 0.5 and abs(height - 297.0) <= 0.5


async def test_apply_ansi_b(backend, tmp_path):
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ANSI_B", "landscape", scale="1:1"))
    row = (await backend.page_setup_list(SHEET))[0]
    assert row["paper"] == "ANSI_B" and row["size_mm"] == [432.0, 279.0]
    assert row["scale"] == "1:1"
    width, height = await _mediabox_after_export(backend, tmp_path, SHEET)
    assert abs(width - 432.0) <= 0.5 and abs(height - 279.0) <= 0.5


async def test_apply_portrait_a4(backend, tmp_path):
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A4", "portrait"))
    row = (await backend.page_setup_list(SHEET))[0]
    assert row["orientation"] == "portrait" and row["size_mm"] == [210.0, 297.0]
    width, height = await _mediabox_after_export(backend, tmp_path, SHEET)
    assert abs(width - 210.0) <= 0.5 and abs(height - 297.0) <= 0.5


async def test_apply_materialises_the_default_flags_before_setting_centre(backend):
    """Measured: set_flag_state starts from 0 on a fresh layout, so a naive
    plot_centered(True) writes 4 and drops lineweights/plot-styles/viewports-first."""
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", center=True))
    flags = int(backend._doc.layouts.get(SHEET).dxf_layout.dxf.plot_layout_flags)
    assert flags & 4, "centre-plot bit"
    assert flags & 32, "plot with plot styles"
    assert flags & 128, "plot entity lineweights"
    assert flags & 512, "draw viewports first"
    assert flags & 16, "use standard scale (fit)"
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", center=False))
    flags = int(backend._doc.layouts.get(SHEET).dxf_layout.dxf.plot_layout_flags)
    assert not flags & 4 and flags & 32 and flags & 128 and flags & 512


async def test_custom_scale_clears_the_standard_scale_bit_and_writes_the_ratio(backend):
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", scale="1:5"))
    dxf = backend._doc.layouts.get(SHEET).dxf_layout.dxf
    assert not int(dxf.plot_layout_flags) & 16
    assert (float(dxf.scale_numerator), float(dxf.scale_denominator)) == (1.0, 5.0)
    assert (await backend.page_setup_list(SHEET))[0]["scale"] == "1:5"


async def test_apply_keeps_the_viewports(backend):
    await _sheet(backend)
    await backend.viewport_create(SHEET, 210, 148.5, 260, 180, 0, 0, scale=1.0)
    before = (await backend.viewport_list(SHEET))["count"]
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3"))
    assert (await backend.viewport_list(SHEET))["count"] == before


async def test_changed_reports_only_what_moved(backend):
    await _sheet(backend)
    first = await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3"))
    assert first["changed"], "a fresh layout differs from an A3 setup somewhere"
    second = await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3"))
    assert second["changed"] == {}
    third = await backend.page_setup_apply(
        SHEET, resolve_page_setup("ISO_A3", plot_style="acad.ctb")
    )
    assert third["changed"] == {"plot_style": ["monochrome.ctb", "acad.ctb"]}


async def test_unknown_ctb_is_written_and_flagged(backend):
    await _sheet(backend)
    result = await backend.page_setup_apply(
        SHEET, resolve_page_setup("ISO_A3", plot_style="company.ctb")
    )
    assert result["plot_style_known"] is False
    assert (await backend.page_setup_list(SHEET))[0]["plot_style"] == "company.ctb"


async def test_refusals_happen_before_any_write(backend):
    await _sheet(backend)
    dxf = backend._doc.layouts.get(SHEET).dxf_layout.dxf
    before = dxf.get("paper_size")
    with pytest.raises(ValueError, match="Model"):
        await backend.page_setup_apply("Model", resolve_page_setup("ISO_A3"))
    with pytest.raises(ValueError, match="Nope"):
        await backend.page_setup_apply("Nope", resolve_page_setup("ISO_A3"))
    with pytest.raises(ValueError, match="canonical_media_name"):
        await backend.page_setup_apply(SHEET, {"paper": "ISO_A3"})
    assert dxf.get("paper_size") == before
    with pytest.raises(ValueError, match="Nope"):
        await backend.page_setup_list("Nope")


async def test_list_covers_every_paper_layout_and_skips_model(backend):
    await _sheet(backend, "A")
    await _sheet(backend, "B")
    names = [row["layout"] for row in await backend.page_setup_list()]
    assert "Model" not in names and {"A", "B"} <= set(names)


async def test_plot_style_list_headless_is_the_catalog(backend):
    rows = await backend.plot_style_list()
    assert [row["name"] for row in rows] == list(CTB_CATALOG)
    assert all(row["source"] == "catalog" and row["installed"] is None for row in rows)


async def test_dwt_write_capability_is_declared_false_headlessly(backend):
    assert backend.capabilities().to_dict()["features"]["dwt_write"]["supported"] is False


# ── live engine, against a fake ActiveX surface ──────────────────────────────

_MEDIA_RE = re.compile(r"\((\d+\.\d+)_x_(\d+\.\d+)_(MM|Inches)\)")


class _FakeLayout:
    """Records every property set and method call, in order."""

    def __init__(self, name, media_names):
        object.__setattr__(self, "log", [])
        object.__setattr__(self, "_media", tuple(media_names))
        object.__setattr__(
            self,
            "_props",
            {
                "Name": name,
                "ConfigName": "None",
                "CanonicalMediaName": "ISO_A4_(210.00_x_297.00_MM)",
                "StyleSheet": "",
                "PlotType": 5,
                "StandardScale": 16,
                "UseStandardScale": True,
                "PlotRotation": 0,
                "PaperUnits": 1,
                "CenterPlot": False,
                "PlotWithPlotStyles": True,
            },
        )

    def __getattr__(self, attr):
        props = object.__getattribute__(self, "_props")
        if attr in props:
            return props[attr]
        raise AttributeError(attr)

    def __setattr__(self, attr, value):
        self._props[attr] = value
        self.log.append(("set", attr, value))

    def RefreshPlotDeviceInfo(self):
        self.log.append(("call", "RefreshPlotDeviceInfo"))

    def GetCanonicalMediaNames(self):
        self.log.append(("call", "GetCanonicalMediaNames"))
        return self._media

    def GetPaperSize(self):
        match = _MEDIA_RE.search(self._props["CanonicalMediaName"])
        width, height = float(match.group(1)), float(match.group(2))
        if match.group(3) == "Inches":
            width, height = width * 25.4, height * 25.4
        return (width, height)

    def GetPaperMargins(self):
        return ((7.5, 20.0), (7.5, 20.0))

    def GetCustomScale(self):
        return (1.0, 1.0)

    def SetCustomScale(self, numerator, denominator):
        self.log.append(("call", "SetCustomScale", numerator, denominator))


class _FakeLayouts:
    def __init__(self, layouts):
        self.layouts = list(layouts)

    @property
    def Count(self):
        return len(self.layouts)

    def Item(self, key):
        if isinstance(key, int):
            return self.layouts[key]
        for layout in self.layouts:
            if layout.Name == key:
                return layout
        raise RuntimeError(f"no layout {key}")


MEDIA = (
    "ISO_A3_(420.00_x_297.00_MM)",
    "ISO_A4_(210.00_x_297.00_MM)",
    "ANSI_B_(17.00_x_11.00_Inches)",
)


@pytest.fixture
def com_backend(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    document = types.SimpleNamespace(
        Layouts=_FakeLayouts([_FakeLayout("Model", MEDIA), _FakeLayout("Layout1", MEDIA)])
    )
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, document


async def test_com_apply_issues_the_property_sequence(com_backend):
    backend, document = com_backend
    result = await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A3", "landscape"))
    assert result["ok"] is True and result["plot_style_known"] is True
    layout = document.Layouts.Item("Layout1")
    assert layout.log == [
        ("call", "RefreshPlotDeviceInfo"),
        ("set", "ConfigName", "DWG To PDF.pc3"),
        ("call", "RefreshPlotDeviceInfo"),
        ("call", "GetCanonicalMediaNames"),
        ("set", "CanonicalMediaName", "ISO_A3_(420.00_x_297.00_MM)"),
        ("set", "PaperUnits", 1),
        ("set", "PlotRotation", 0),
        ("set", "StyleSheet", "monochrome.ctb"),
        ("set", "PlotWithPlotStyles", True),
        ("set", "PlotType", 5),
        ("set", "StandardScale", 0),
        ("set", "UseStandardScale", True),
        ("set", "CenterPlot", True),
    ]
    assert result["changed"]["paper"] == ["ISO_A4", "ISO_A3"]
    assert result["changed"]["device"] == ["None", "DWG To PDF.pc3"]
    assert result["changed"]["center"] == [False, True]


async def test_com_apply_custom_scale_uses_set_custom_scale(com_backend):
    backend, document = com_backend
    await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A3", scale="5:1"))
    log = document.Layouts.Item("Layout1").log
    assert ("call", "SetCustomScale", 5.0, 1.0) in log
    assert ("set", "UseStandardScale", False) in log
    assert not any(entry[:2] == ("set", "StandardScale") for entry in log)


async def test_com_apply_refuses_margins_before_any_write(com_backend):
    backend, document = com_backend
    with pytest.raises(ValueError, match="margins_mm"):
        await backend.page_setup_apply(
            "Layout1", resolve_page_setup("ISO_A3", margins_mm=[10, 10, 10, 10])
        )
    assert document.Layouts.Item("Layout1").log == []


async def test_com_apply_restores_the_device_when_the_media_name_is_unknown(com_backend):
    backend, document = com_backend
    layout = document.Layouts.Item("Layout1")
    object.__setattr__(layout, "_media", ("ISO_A4_(210.00_x_297.00_MM)",))
    with pytest.raises(ValueError, match=r"ISO_A3_\(420.00_x_297.00_MM\)"):
        await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A3"))
    assert layout.ConfigName == "None", "the device write is rolled back"
    assert layout.CanonicalMediaName == "ISO_A4_(210.00_x_297.00_MM)"


async def test_com_apply_refuses_model_and_unknown_layouts(com_backend):
    backend, _ = com_backend
    with pytest.raises(ValueError, match="Model"):
        await backend.page_setup_apply("Model", resolve_page_setup("ISO_A3"))
    with pytest.raises(ValueError, match="Nope"):
        await backend.page_setup_apply("Nope", resolve_page_setup("ISO_A3"))


async def test_com_list_reads_the_properties(com_backend):
    backend, _ = com_backend
    rows = await backend.page_setup_list()
    assert [row["layout"] for row in rows] == ["Layout1"]
    row = rows[0]
    assert row["paper"] == "ISO_A4" and row["size_mm"] == [210.0, 297.0]
    assert row["orientation"] == "portrait"
    assert row["margins_mm"] == [20.0, 20.0, 7.5, 7.5]
    assert row["scale"] == "1:1" and row["plot_area"] == "layout"
    assert row["device"] == "None" and row["center"] is False


async def test_com_declares_dwt_write():
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends.com_backend import ComBackend

    assert ComBackend().capabilities().to_dict()["features"]["dwt_write"]["supported"] is True
