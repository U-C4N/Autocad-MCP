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
    assert setup["margins_mm"] is None
    assert setup["center"] is None, "a layout plot has no centring (AutoCAD greys it out)"


def test_resolve_page_setup_center_applies_to_an_extents_plot_only():
    """ActiveX Reference, CenterPlot: 'This property cannot be set to True on a
    layout object whose PlotType property is set to acLayout.' The resolver
    carries the rule so both engines write the same thing: a bool for an
    extents plot, ``None`` (not applicable, never written) for a layout plot."""
    assert resolve_page_setup("ISO_A3", plot_area="extents")["center"] is True
    assert resolve_page_setup("ISO_A3", plot_area="extents", center=False)["center"] is False
    assert resolve_page_setup("ISO_A3", plot_area="layout", center=True)["center"] is None
    assert resolve_page_setup("ISO_A3", plot_area="layout", center=False)["center"] is None
    with pytest.raises(ValueError, match="center"):
        resolve_page_setup("ISO_A3", plot_area="layout", center="yes")


def test_resolve_page_setup_accepts_lowercase_and_short_paper_names():
    assert resolve_page_setup("iso_a4", "portrait")["size_mm"] == [210.0, 297.0]
    assert resolve_page_setup("A3")["paper"] == "ISO_A3"
    assert resolve_page_setup("ansi_b")["canonical_media_name"] == "ANSI_B_(17.00_x_11.00_Inches)"


def test_resolve_page_setup_scale_codes():
    one_to_five = resolve_page_setup("ISO_A3", scale="1:5")
    assert one_to_five["dxf_standard_scale_type"] is None  # DXF code 75 has no 1:5
    # The typelib declares ac1_5 = 19, but AutoCAD 2026 refuses
    # StandardScale = 19 ("Invalid input", measured live): custom route.
    assert one_to_five["activex_standard_scale"] is None
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
    assert row["center"] is None, "layout plot: centring is not applicable"
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


def _ink_columns(png_path: str) -> tuple[int, int, int]:
    """(first inked column, last inked column, image width) of a rendered PNG."""
    from PIL import Image

    image = Image.open(png_path).convert("L")
    width, height = image.size
    pixels = image.load()
    inked = [x for x in range(width) if any(pixels[x, y] < 128 for y in range(height))]
    assert inked, "the render is blank"
    return inked[0], inked[-1], width


async def test_rotated_layout_renders_the_whole_sheet(backend, tmp_path):
    """plot_rotation 1 is AutoCAD's landscape-on-portrait-media convention.

    A drawing authored in AutoCAD stores ISO A4 landscape as paper 210 x 297
    with ``plot_rotation`` 1; paper-space X then runs along the 297 mm side.
    The MediaBox alone cannot see a frame that has been clipped inside a
    correctly sized page, so this test looks at the ink: the frame's right
    edge (x = 277, the printable width) must reach the right of the sheet.
    """
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    pytest.importorskip("PIL", reason="pixel check needs Pillow")
    await _sheet(backend, "Rot")
    lay = backend._doc.layouts.get("Rot")
    lay.page_setup(
        size=(210, 297),
        margins=(20, 7.5, 20, 7.5),
        units="mm",
        rotation=1,
        scale=16,
        name="ISO_A4",
        device="DWG To PDF.pc3",
    )
    lay.add_lwpolyline([(0, 0), (277, 0), (277, 190), (0, 190)], close=True)

    row = (await backend.page_setup_list("Rot"))[0]
    assert row["size_mm"] == [297.0, 210.0] and row["orientation"] == "landscape"

    png = tmp_path / "rot.png"
    result = await backend.drawing_export_pdf(str(png), layout="Rot")
    assert result["ok"] is True, result
    assert result["paper_mm"] == [297.0, 210.0]
    assert result["rotation_applied"] is False

    first, last, width = _ink_columns(str(png))
    # x = 277 sits at 277 / 297 of the sheet from the left margin, i.e. well
    # past the 0.707-scaled box the unrotated window produced (col 1314/1753).
    assert last >= int(width * 0.9), (first, last, width)

    pdf = tmp_path / "rot.pdf"
    await backend.drawing_export_pdf(str(pdf), layout="Rot")
    mm_w, mm_h = read_mediabox(str(pdf))
    assert abs(mm_w - 297.0) <= 0.5 and abs(mm_h - 210.0) <= 0.5


async def _export_png(backend, tmp_path, layout: str, stem: str):
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    pytest.importorskip("PIL", reason="pixel check needs Pillow")
    png = tmp_path / f"{stem}.png"
    result = await backend.drawing_export_pdf(str(png), layout=layout)
    assert result["ok"] is True, result
    return result, _ink_columns(str(png))


async def test_layout_plot_honours_the_plot_scale(backend, tmp_path):
    """A 1:2 sheet holds twice the paper-space units a 1:1 sheet holds.

    The MediaBox is the same 420 x 297 either way, so it cannot see this;
    the ink can. An 800-unit-wide frame at 1:2 spans 400 mm and ends inside
    the sheet; at 1:1 it runs off the right edge. Before the fix the two
    exports were pixel-identical (both clipped at the edge) with ``ok: True``.
    """
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", scale="1:2"))
    lay = backend._doc.layouts.get(SHEET)
    lay.add_lwpolyline([(0, 0), (800, 0), (800, 500), (0, 500)], close=True)
    _, _, left_mm, _ = (await backend.page_setup_list(SHEET))[0]["margins_mm"]

    frame = backend._paper_frame(lay)
    assert frame["scale"] == "1:2" and frame["scale_applied"] is True
    assert frame["xlim"][1] - frame["xlim"][0] == pytest.approx(840.0)
    assert frame["ylim"][1] - frame["ylim"][0] == pytest.approx(594.0)

    result, (first, last, width) = await _export_png(backend, tmp_path, SHEET, "half")
    assert result["scale"] == "1:2" and result["effective_scale"] == "1:2"
    assert result["scale_applied"] is True
    assert result["plot_area"] == "layout" and result["plot_area_applied"] is True
    expected_last = int(width * (left_mm + 400.0) / 420.0)
    assert abs(last - expected_last) <= 3, (first, last, width, expected_last)

    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", scale="1:1"))
    result, (_, last_full, _) = await _export_png(backend, tmp_path, SHEET, "full")
    assert result["scale"] == "1:1"
    assert last_full >= width - 2, "at 1:1 the 800-unit frame runs off the 420 mm sheet"
    assert last < last_full - 20, "1:2 and 1:1 must not render the same"


async def test_custom_scale_sizes_the_window_too(backend):
    """1:5 has no DXF code 75, so it rides on the numerator / denominator."""
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", scale="1:5"))
    frame = backend._paper_frame(backend._doc.layouts.get(SHEET))
    assert frame["scale"] == "1:5" and frame["scale_applied"] is True
    assert frame["xlim"][1] - frame["xlim"][0] == pytest.approx(2100.0)


async def test_fit_on_a_layout_plot_is_one_to_one(backend):
    """AutoCAD greys "Fit to paper" out under the layout plot area."""
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", scale="fit"))
    frame = backend._paper_frame(backend._doc.layouts.get(SHEET))
    assert frame["scale"] == "fit" and frame["effective_scale"] == "1:1"
    assert frame["scale_applied"] is True
    assert frame["xlim"][1] - frame["xlim"][0] == pytest.approx(420.0)


async def test_extents_plot_fits_the_printable_area_and_centres(backend, tmp_path):
    """extents + fit: the paper-space extents (not the main viewport, which
    ezdxf's bbox would count) scale to the printable area and sit centred."""
    await _sheet(backend)
    await backend.page_setup_apply(
        SHEET, resolve_page_setup("ISO_A3", scale="fit", plot_area="extents", center=True)
    )
    lay = backend._doc.layouts.get(SHEET)
    lay.add_lwpolyline([(0, 0), (800, 0), (800, 500), (0, 500)], close=True)
    top, bottom, left_mm, right_mm = (await backend.page_setup_list(SHEET))[0]["margins_mm"]
    printable_w, printable_h = 420.0 - left_mm - right_mm, 297.0 - top - bottom
    mm_per_unit = min(printable_w / 800.0, printable_h / 500.0)

    frame = backend._paper_frame(lay)
    assert frame["plot_area"] == "extents" and frame["plot_area_applied"] is True
    assert frame["scale"] == "fit"
    assert frame["xlim"][1] - frame["xlim"][0] == pytest.approx(420.0 / mm_per_unit)

    result, (first, last, width) = await _export_png(backend, tmp_path, SHEET, "extents")
    assert result["effective_scale"] == scale_label(1.0, 1.0 / mm_per_unit)
    # The frame is the wider of the two: it spans the printable width exactly.
    assert abs(first - int(width * left_mm / 420.0)) <= 3, (first, last, width)
    assert abs(last - int(width * (420.0 - right_mm) / 420.0)) <= 3, (first, last, width)


async def test_extents_plot_at_a_fixed_scale_starts_at_the_plot_origin(backend):
    await _sheet(backend)
    await backend.page_setup_apply(
        SHEET, resolve_page_setup("ISO_A3", scale="1:2", plot_area="extents", center=False)
    )
    lay = backend._doc.layouts.get(SHEET)
    lay.add_lwpolyline([(100, 50), (900, 50), (900, 550), (100, 550)], close=True)
    _, _, left_mm, _ = (await backend.page_setup_list(SHEET))[0]["margins_mm"]
    frame = backend._paper_frame(lay)
    assert frame["effective_scale"] == "1:2" and frame["plot_area_applied"] is True
    # The extents' lower-left corner lands on the printable corner: sheet x0
    # is that corner less the left margin, in paper-space units at 1:2.
    assert frame["xlim"][0] == pytest.approx(100.0 - left_mm * 2.0)
    assert frame["xlim"][1] - frame["xlim"][0] == pytest.approx(840.0)


async def test_extents_plot_of_an_empty_layout_falls_back_to_the_sheet(backend):
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", plot_area="extents"))
    frame = backend._paper_frame(backend._doc.layouts.get(SHEET))
    assert frame["plot_area_applied"] is False
    assert "no entities" in frame["plot_area_note"]
    assert frame["xlim"][1] - frame["xlim"][0] == pytest.approx(420.0)


async def test_unrenderable_plot_area_is_reported_not_hidden(backend, tmp_path):
    """display / view need a screen; the headless engine plots the sheet and says so."""
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    await _sheet(backend)
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", scale="1:1"))
    lay = backend._doc.layouts.get(SHEET)
    lay.dxf_layout.dxf.plot_type = 0  # display
    lay.add_line((0, 0), (100, 100))
    result = await backend.drawing_export_pdf(str(tmp_path / "display.pdf"), layout=SHEET)
    assert result["ok"] is True
    assert result["plot_area"] == "display" and result["plot_area_applied"] is False
    assert "display" in result["plot_area_note"]
    assert result["scale_applied"] is True


async def test_apply_materialises_the_default_flags_before_setting_centre(backend):
    """Measured: set_flag_state starts from 0 on a fresh layout, so a naive
    plot_centered(True) writes 4 and drops lineweights/plot-styles/viewports-first."""
    await _sheet(backend)
    extents = resolve_page_setup("ISO_A3", plot_area="extents", center=True)
    result = await backend.page_setup_apply(SHEET, extents)
    flags = int(backend._doc.layouts.get(SHEET).dxf_layout.dxf.plot_layout_flags)
    assert flags & 4, "centre-plot bit"
    assert flags & 32, "plot with plot styles"
    assert flags & 128, "plot entity lineweights"
    assert flags & 512, "draw viewports first"
    assert flags & 16, "use standard scale (fit)"
    assert result["applied"]["center"] is True
    assert (await backend.page_setup_list(SHEET))[0]["center"] is True
    await backend.page_setup_apply(
        SHEET, resolve_page_setup("ISO_A3", plot_area="extents", center=False)
    )
    flags = int(backend._doc.layouts.get(SHEET).dxf_layout.dxf.plot_layout_flags)
    assert not flags & 4 and flags & 32 and flags & 128 and flags & 512
    assert (await backend.page_setup_list(SHEET))[0]["center"] is False


async def test_layout_plot_never_sets_the_centre_bit_and_reports_it_not_applicable(backend):
    """A layout plot is the whole sheet from its origin: AutoCAD disables
    'Center the plot' (ActiveX refuses CenterPlot=True under acLayout). The
    headless engine mirrors that: bit 4 is not written, and the read-back says
    None rather than a bool AutoCAD would ignore."""
    await _sheet(backend)
    result = await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", center=True))
    flags = int(backend._doc.layouts.get(SHEET).dxf_layout.dxf.plot_layout_flags)
    assert not flags & 4, "centre bit must not be set for a layout plot"
    assert result["applied"]["center"] is None
    assert "center" not in result["changed"], "None before (fresh layout plot) and None after"
    assert (await backend.page_setup_list(SHEET))[0]["center"] is None
    # Switching an extents+centred sheet to a layout plot clears the stale bit
    # (what AutoCAD stores for a layout plot) and reports the move honestly.
    await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3", plot_area="extents"))
    assert (await backend.page_setup_list(SHEET))[0]["center"] is True
    result = await backend.page_setup_apply(SHEET, resolve_page_setup("ISO_A3"))
    flags = int(backend._doc.layouts.get(SHEET).dxf_layout.dxf.plot_layout_flags)
    assert not flags & 4
    assert result["changed"]["center"] == [True, None]
    assert result["changed"]["plot_area"] == ["extents", "layout"]


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
    """Records every property set and method call, in order.

    Enforces the one rule the ActiveX Reference documents for this surface
    (CenterPlot Property, acadauto.chm, AutoCAD 2026): "This property cannot
    be set to True on a layout object whose PlotType property is set to
    acLayout." ``refuse`` names properties whose write raises, the way a live
    AutoCAD refuses a value it cannot take.

    Replays two behaviours measured live on AutoCAD 2026 (2026-09-17):

    * ``GetPaperSize`` / ``GetPaperMargins`` answer in millimetres whatever
      ``PaperUnits`` says (ANSI_B under PaperUnits 0 and 1 -> (431.8, 279.4)).
    * Writing ``ConfigName`` replaces ``CanonicalMediaName`` with the new
      device's default when the current media does not exist on it, and
      writing the old device back does not bring the old media back.
      ``media_names`` is therefore either one tuple (every device carries it)
      or ``{device: tuple}``; a device missing from the dict carries nothing.
    """

    AC_LAYOUT = 5

    def __init__(self, name, media_names, refuse=()):
        object.__setattr__(self, "log", [])
        media = media_names if isinstance(media_names, dict) else {None: tuple(media_names)}
        object.__setattr__(self, "_media", {k: tuple(v) for k, v in media.items()})
        object.__setattr__(self, "_refuse", tuple(refuse))
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
        if attr in self._refuse:
            raise RuntimeError(f"AutoCAD refused Layout.{attr} = {value!r}")
        if attr == "CenterPlot" and value and self._props["PlotType"] == self.AC_LAYOUT:
            raise RuntimeError("CenterPlot cannot be True while PlotType is acLayout")
        self._props[attr] = value
        self.log.append(("set", attr, value))
        if attr == "ConfigName":
            names = self._device_media()
            if names and self._props["CanonicalMediaName"] not in names:
                self._props["CanonicalMediaName"] = names[0]  # the device's default

    def _device_media(self):
        media = object.__getattribute__(self, "_media")
        return media.get(self._props["ConfigName"], media.get(None, ()))

    def snapshot(self) -> dict:
        return dict(object.__getattribute__(self, "_props"))

    def RefreshPlotDeviceInfo(self):
        self.log.append(("call", "RefreshPlotDeviceInfo"))

    def GetCanonicalMediaNames(self):
        self.log.append(("call", "GetCanonicalMediaNames"))
        return self._device_media()

    def GetPaperSize(self):
        # Millimetres whatever PaperUnits says (measured live, see the class docstring).
        match = _MEDIA_RE.search(self._props["CanonicalMediaName"])
        width, height = float(match.group(1)), float(match.group(2))
        if match.group(3) == "Inches":
            width, height = round(width * 25.4, 2), round(height * 25.4, 2)
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
    ], "a layout plot never writes CenterPlot (ActiveX refuses True under acLayout)"
    assert result["changed"]["paper"] == ["ISO_A4", "ISO_A3"]
    assert result["changed"]["device"] == ["None", "DWG To PDF.pc3"]
    assert result["applied"]["center"] is None
    assert "center" not in result["changed"]


async def test_com_apply_extents_sets_plot_type_before_center_plot(com_backend):
    backend, document = com_backend
    result = await backend.page_setup_apply(
        "Layout1", resolve_page_setup("ISO_A3", plot_area="extents", center=True)
    )
    log = document.Layouts.Item("Layout1").log
    assert log.index(("set", "PlotType", 1)) < log.index(("set", "CenterPlot", True))
    assert result["changed"]["center"] == [None, True]
    assert result["changed"]["plot_area"] == ["layout", "extents"]
    assert (await backend.page_setup_list("Layout1"))[0]["center"] is True


async def test_com_apply_clears_a_stale_center_plot_before_switching_to_layout(com_backend):
    backend, document = com_backend
    layout = document.Layouts.Item("Layout1")
    layout.PlotType = 1
    layout.CenterPlot = True
    del layout.log[:]
    result = await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A3"))
    log = layout.log
    assert log.index(("set", "CenterPlot", False)) < log.index(("set", "PlotType", 5))
    assert layout.CenterPlot is False and layout.PlotType == 5
    assert result["changed"]["center"] == [True, None]


async def test_com_apply_restores_every_write_when_autocad_refuses_a_property(com_backend):
    """Nine writes had landed before CenterPlot in the shipped sequence, with
    nothing put back. Any refusal now unwinds the journal in reverse."""
    backend, document = com_backend
    layout = _FakeLayout("Layout1", MEDIA, refuse=("PlotWithPlotStyles",))
    document.Layouts.layouts[1] = layout
    before = layout.snapshot()
    with pytest.raises(ValueError, match="PlotWithPlotStyles") as info:
        await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A3"))
    assert "restored" in str(info.value)
    assert layout.snapshot() == before, "every property AutoCAD accepted is put back"
    restores = [entry for entry in layout.log if entry[0] == "set"][5:]
    assert restores == [
        ("set", "StyleSheet", ""),
        ("set", "PlotRotation", 0),
        ("set", "PaperUnits", 1),
        ("set", "CanonicalMediaName", "ISO_A4_(210.00_x_297.00_MM)"),
        ("set", "ConfigName", "None"),
    ], "unwound in reverse write order"


async def test_com_apply_restores_the_custom_scale_too(com_backend):
    backend, document = com_backend
    layout = _FakeLayout("Layout1", MEDIA, refuse=("UseStandardScale",))
    document.Layouts.layouts[1] = layout
    with pytest.raises(ValueError, match="UseStandardScale"):
        await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A3", scale="5:1"))
    calls = [entry for entry in layout.log if entry[0] == "call" and entry[1] == "SetCustomScale"]
    assert calls == [("call", "SetCustomScale", 5.0, 1.0), ("call", "SetCustomScale", 1.0, 1.0)]


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
    object.__setattr__(layout, "_media", {None: ("ISO_A4_(210.00_x_297.00_MM)",)})
    with pytest.raises(ValueError, match=r"ISO_A3_\(420.00_x_297.00_MM\)"):
        await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A3"))
    assert layout.ConfigName == "None", "the device write is rolled back"
    assert layout.CanonicalMediaName == "ISO_A4_(210.00_x_297.00_MM)"


PDF_DEVICE = "DWG To PDF.pc3"
MS_DEVICE = "Microsoft Print to PDF"
ISO_A3_MEDIA = "ISO_A3_(420.00_x_297.00_MM)"
#: Two devices whose media do not overlap, the way the two PDF drivers on the
#: machine measured: switching to MS moves an ISO_A3 layout to psk:ISOA4, and
#: switching back lands on DWG To PDF's default, not on ISO_A3.
TWO_DEVICES = {
    PDF_DEVICE: ("ANSI_A_(11.00_x_8.50_Inches)", ISO_A3_MEDIA),
    MS_DEVICE: ("psk:ISOA4", "ISO_A0_(1189.00_x_841.00_MM)"),
}


def _layout_on_pdf_device(document, **kwargs):
    layout = _FakeLayout("Layout1", TWO_DEVICES, **kwargs)
    layout.ConfigName = PDF_DEVICE
    layout.CanonicalMediaName = ISO_A3_MEDIA
    del layout.log[:]
    document.Layouts.layouts[1] = layout
    return layout


async def test_com_apply_puts_the_media_back_when_the_device_switch_replaced_it(com_backend):
    """Measured live: refusing a media unknown on the new device used to leave
    the layout on the old device with that device's *default* media (ANSI_A),
    not the ISO_A3 it had — while the error said it was restored."""
    backend, document = com_backend
    layout = _layout_on_pdf_device(document)
    before = layout.snapshot()
    with pytest.raises(ValueError, match="has no media") as info:
        await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A2", device=MS_DEVICE))
    assert "could NOT" not in str(info.value)
    assert layout.snapshot() == before, "device back, then the original media re-applied"
    assert (await backend.page_setup_list("Layout1"))[0]["paper"] == "ISO_A3"


async def test_com_apply_late_refusal_unwinds_through_the_device_switch(com_backend):
    """The media journaled *after* the device switch is the new device's
    default; only the pre-switch snapshot can put ISO_A3 back."""
    backend, document = com_backend
    layout = _layout_on_pdf_device(document, refuse=("PlotWithPlotStyles",))
    before = layout.snapshot()
    with pytest.raises(ValueError, match="PlotWithPlotStyles") as info:
        await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A0", device=MS_DEVICE))
    assert "restored to its previous page setup" in str(info.value)
    assert layout.snapshot() == before
    sets = [entry for entry in layout.log if entry[0] == "set"]
    assert sets[-2:] == [
        ("set", "ConfigName", PDF_DEVICE),
        ("set", "CanonicalMediaName", ISO_A3_MEDIA),
    ]


class _StickyMediaLayout(_FakeLayout):
    """Accepts every write but silently drops CanonicalMediaName once the device
    has been switched back — what a driver that ignores the write looks like."""

    def __setattr__(self, attr, value):
        if attr == "CanonicalMediaName" and self._props["ConfigName"] == PDF_DEVICE and self.log:
            return
        super().__setattr__(attr, value)


async def test_com_apply_reports_what_the_unwind_could_not_put_back(com_backend):
    backend, document = com_backend
    layout = _StickyMediaLayout("Layout1", TWO_DEVICES)
    layout.ConfigName = PDF_DEVICE
    object.__getattribute__(layout, "_props")["CanonicalMediaName"] = ISO_A3_MEDIA
    del layout.log[:]
    document.Layouts.layouts[1] = layout
    with pytest.raises(ValueError, match="has no media") as info:
        await backend.page_setup_apply("Layout1", resolve_page_setup("ISO_A2", device=MS_DEVICE))
    message = str(info.value)
    assert "could NOT be fully restored" in message
    assert "canonical_media_name" in message and "paper" in message
    assert layout.ConfigName == PDF_DEVICE and layout.CanonicalMediaName != ISO_A3_MEDIA


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
    assert row["device"] == "None"
    assert row["center"] is None, "acLayout: CenterPlot is not applicable"


async def test_com_list_never_scales_the_paper_by_paper_units(com_backend):
    """GetPaperSize / GetPaperMargins are millimetres under PaperUnits=0 too
    (measured live) — the 25.4 factor listed every imperial layout as
    [10668.0, 7543.8] and lost the paper name."""
    backend, document = com_backend
    layout = document.Layouts.Item("Layout1")
    layout.PaperUnits = 0
    layout.CanonicalMediaName = "ANSI_B_(17.00_x_11.00_Inches)"
    row = (await backend.page_setup_list("Layout1"))[0]
    assert row["size_mm"] == [431.8, 279.4] and row["paper"] == "ANSI_B"
    assert row["orientation"] == "landscape"
    assert row["margins_mm"] == [20.0, 20.0, 7.5, 7.5]
    layout.CanonicalMediaName = "ISO_A3_(420.00_x_297.00_MM)"
    assert (await backend.page_setup_list("Layout1"))[0]["size_mm"] == [420.0, 297.0]


async def test_com_list_reports_center_for_an_extents_plot(com_backend):
    backend, document = com_backend
    layout = document.Layouts.Item("Layout1")
    layout.PlotType = 1
    assert (await backend.page_setup_list("Layout1"))[0]["center"] is False
    layout.CenterPlot = True
    assert (await backend.page_setup_list("Layout1"))[0]["center"] is True


async def test_com_declares_dwt_write():
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends.com_backend import ComBackend

    assert ComBackend().capabilities().to_dict()["features"]["dwt_write"]["supported"] is True
