"""batch_plot: every sheet to PDF, each sheet size read back from the PDF.

The composition is exercised on the headless engine end to end; the COM-only
pieces of this task (the installed ctb scan, and the plot call's shape —
foreground, by layout name, confirmed on disk) are fake-tested; the SECTION 19
tools are called directly with a minimal context.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import server
from engineering.standards.papers import CTB_CATALOG, resolve_page_setup
from engineering.standards.plot import batch_plot, safe_filename

pytestmark = pytest.mark.asyncio


class _Ctx:
    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, message):
        pass

    async def warning(self, message):
        pass

    async def report_progress(self, progress, total):
        pass


async def _two_sheets(backend):
    """Exactly two paper layouts: the default tab renamed, plus one more."""
    await backend.layout_rename("Layout1", "A3")
    await backend.layout_create("B")
    await backend.page_setup_apply("A3", resolve_page_setup("ISO_A3", "landscape"))
    await backend.page_setup_apply("B", resolve_page_setup("ANSI_B", "landscape"))
    await backend.entity_create_line(0, 0, 100, 50)


def test_safe_filename_strips_what_a_filesystem_refuses():
    assert safe_filename("Sheet 1: A3/rev*B") == "Sheet_1_A3_rev_B"
    assert safe_filename("Layout1") == "Layout1"


async def test_every_paper_layout_is_plotted_and_measured(backend, tmp_path):
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    await _two_sheets(backend)
    result = await batch_plot(backend, None, str(tmp_path / "out"))
    assert result["ok"] is True and result["format"] == "pdf"
    assert result["count"] == 2
    by_layout = {row["layout"]: row for row in result["sheets"]}
    assert set(by_layout) == {"A3", "B"}, "Model is not plotted unless named"
    a3, b = by_layout["A3"], by_layout["B"]
    assert a3["ok"] and a3["bytes"] > 0 and a3["path"].endswith("untitled-A3.pdf")
    assert abs(a3["mediabox_mm"][0] - 420) <= 0.5 and abs(a3["mediabox_mm"][1] - 297) <= 0.5
    assert a3["paper"] == "ISO_A3"
    assert abs(b["mediabox_mm"][0] - 432) <= 0.5 and abs(b["mediabox_mm"][1] - 279) <= 0.5
    assert b["paper"] == "ANSI_B"


async def test_model_is_included_only_when_named(backend, tmp_path):
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    await _two_sheets(backend)
    result = await batch_plot(backend, ["Model", "A3"], str(tmp_path))
    names = [row["layout"] for row in result["sheets"]]
    assert names == ["Model", "A3"]
    model = result["sheets"][0]
    assert model["ok"] and model["mediabox_mm"] is not None
    assert model["paper"] is None, "model space has no sheet; the size is reported, not named"


async def test_refusals_write_nothing(backend, tmp_path):
    await _two_sheets(backend)
    out = tmp_path / "nothing"
    with pytest.raises(ValueError, match="Nope"):
        await batch_plot(backend, ["Nope"], str(out))
    with pytest.raises(ValueError, match="layout"):
        await batch_plot(backend, None, str(out), file_pattern="all.pdf")
    with pytest.raises(ValueError, match="file_pattern"):
        await batch_plot(backend, None, str(out), file_pattern="sub/{layout}.pdf")
    with pytest.raises(ValueError, match="layouts"):
        await batch_plot(backend, [], str(out))
    assert not out.exists() or not any(out.iterdir())


async def test_a_fresh_drawing_plots_its_one_default_sheet_at_the_dxf_default_size(
    backend, tmp_path
):
    """No page setup ever applied: the sheet still plots, at ezdxf's stored
    default (A3, 420 x 297), and the row says so rather than guessing."""
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    result = await batch_plot(backend, None, str(tmp_path), file_pattern="{layout}.pdf")
    assert [row["layout"] for row in result["sheets"]] == ["Layout1"]
    row = result["sheets"][0]
    assert row["path"].endswith("Layout1.pdf") and row["paper"] == "ISO_A3"


async def _model_circle_and_sheet_rectangle(backend):
    """Distinct geometry per space, told apart by the PDF's aspect ratio: a
    circle in model space (a square page) and a 390 x 270 rectangle on the A3
    sheet (a landscape page)."""
    await backend.entity_create_circle(50, 50, 25)
    await backend.layout_rename("Layout1", "A3")
    await backend.page_setup_apply("A3", resolve_page_setup("ISO_A3", "landscape"))
    await backend.layout_set_current("A3")
    await backend.entity_create_polyline([[10, 10], [400, 10], [400, 280], [10, 280]], closed=True)


def _aspect(mediabox_mm):
    width, height = mediabox_mm
    return width / height


async def test_the_model_row_is_model_space_even_when_a_sheet_is_current(backend, tmp_path):
    """The Model row must be model space whichever tab is current — proven by
    content, not by the row's shape. The headless export used to route the
    model target through ``_msp()`` (the *current* tab), so with a sheet
    current the "Model" PDF was that sheet's paper-space content: measured
    176.1 x 121.9 mm (the rectangle's aspect) against 121.9 x 121.9 (the
    circle) with Model current. COM had the mirror defect (``ActiveLayout``
    for ``layout=None``). batch_plot names the layout for every row, Model
    included, and the engine honours the name."""
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    await _model_circle_and_sheet_rectangle(backend)
    seen: list[str | None] = []
    original = backend.drawing_export_pdf

    async def _recording(path, layout=None):
        seen.append(layout)
        return await original(path, layout=layout)

    backend.drawing_export_pdf = _recording
    with_sheet_current = await batch_plot(backend, ["Model", "A3"], str(tmp_path / "sheet"))
    assert seen == ["Model", "A3"]
    await backend.layout_set_current("Model")
    with_model_current = await batch_plot(backend, ["Model"], str(tmp_path / "model"))

    model_row = with_sheet_current["sheets"][0]
    assert with_sheet_current["ok"] and model_row["paper"] is None
    assert model_row["mediabox_mm"] == pytest.approx(
        with_model_current["sheets"][0]["mediabox_mm"], abs=0.05
    ), "the Model row changed with the current tab"
    assert _aspect(model_row["mediabox_mm"]) == pytest.approx(1.0, abs=0.02), (
        "the Model row is the circle (square page), not the sheet's rectangle"
    )
    a3_row = with_sheet_current["sheets"][1]
    assert a3_row["paper"] == "ISO_A3"
    assert _aspect(a3_row["mediabox_mm"]) == pytest.approx(420 / 297, abs=0.02)


async def test_export_pdf_without_a_layout_is_model_space_even_when_a_sheet_is_current(
    backend, tmp_path
):
    """``drawing_export_pdf(path)`` advertises model space as its default; a
    current sheet must not turn it into that sheet."""
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    from engineering.standards.plot import read_mediabox

    await _model_circle_and_sheet_rectangle(backend)
    default = tmp_path / "default.pdf"
    named = tmp_path / "named.pdf"
    assert (await backend.drawing_export_pdf(str(default)))["ok"]
    assert (await backend.drawing_export_pdf(str(named), layout="Model"))["ok"]
    assert read_mediabox(str(default)) == pytest.approx(read_mediabox(str(named)), abs=0.05)
    assert _aspect(read_mediabox(str(default))) == pytest.approx(1.0, abs=0.02)


async def test_a_sheet_the_engine_cannot_plot_is_a_failed_row_not_a_lost_batch(backend, tmp_path):
    """COM raises RuntimeError for an AutoCAD error (E_FAIL after a stranded
    background job, say). That is one row's failure; the sheets already
    written stay in the report with their evidence."""
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    await _two_sheets(backend)
    original = backend.drawing_export_pdf

    async def _flaky(path, layout=None):
        if layout == "B":
            raise RuntimeError("AutoCAD COM error (-0x7ffdfff7): Unspecified error")
        return await original(path, layout=layout)

    backend.drawing_export_pdf = _flaky
    result = await batch_plot(backend, None, str(tmp_path))
    assert result["ok"] is False and result["count"] == 2
    a3, b = result["sheets"]
    assert a3["ok"] and a3["paper"] == "ISO_A3"
    assert b == {
        "layout": "B",
        "ok": False,
        "path": str(tmp_path / "untitled-B.pdf"),
        "error": "AutoCAD COM error (-0x7ffdfff7): Unspecified error",
    }


async def test_a_plot_reported_ok_without_a_file_is_a_failed_row(backend, tmp_path):
    """A background plot answers ok before any file exists; the row says so
    instead of dying on ``path.stat()`` with an unhandled FileNotFoundError."""
    await _two_sheets(backend)

    async def _phantom(path, layout=None):
        return {"ok": True, "path": path}

    backend.drawing_export_pdf = _phantom
    result = await batch_plot(backend, ["A3"], str(tmp_path))
    assert result["ok"] is False
    row = result["sheets"][0]
    assert row["ok"] is False and "no file" in row["error"]
    assert "bytes" not in row and "mediabox_mm" not in row


# ── the tools ───────────────────────────────────────────────────────────────


async def test_page_setup_tools_round_trip(backend):
    ctx = _Ctx(backend)
    # A fresh drawing already carries Layout1; renaming it keeps exactly one
    # sheet, so the unfiltered listing has one row and it is the A3.
    await backend.layout_rename("Layout1", "A3")
    applied = await server.page_setup_apply("A3", "ISO_A3", ctx=ctx)
    assert applied["ok"] and applied["applied"]["paper"] == "ISO_A3"
    listed = await server.page_setup_list(ctx=ctx)
    assert listed["count"] == 1 and listed["layouts"][0]["paper"] == "ISO_A3"
    only = await server.page_setup_list("A3", ctx=ctx)
    assert only["layouts"][0]["layout"] == "A3"


async def test_page_setup_apply_tool_refuses_before_writing(backend):
    ctx = _Ctx(backend)
    await backend.layout_create("A3")
    with pytest.raises(ToolError, match="scale"):
        await server.page_setup_apply("A3", "ISO_A3", scale="1:3", ctx=ctx)
    with pytest.raises(ToolError, match="Model"):
        await server.page_setup_apply("Model", "ISO_A3", ctx=ctx)
    row = (await backend.page_setup_list("A3"))[0]
    assert row["device"] == "", "nothing was written"


async def test_plot_style_list_tool(backend):
    result = await server.plot_style_list(ctx=_Ctx(backend))
    assert result["ok"] and result["count"] == len(CTB_CATALOG)
    assert result["backend"] == "ezdxf"


async def test_batch_plot_tool_validates_the_output_directory(backend, tmp_path):
    pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
    await _two_sheets(backend)
    with pytest.raises(ToolError):
        await server.batch_plot("../../outside", ctx=_Ctx(backend))
    with pytest.raises(ToolError, match="format"):
        await server.batch_plot(str(tmp_path), format="png", ctx=_Ctx(backend))
    result = await server.batch_plot(str(tmp_path), layouts=["A3"], ctx=_Ctx(backend))
    assert result["count"] == 1 and result["sheets"][0]["paper"] == "ISO_A3"


# ── COM: the installed ctb scan ─────────────────────────────────────────────


@pytest.fixture
def com_backend(monkeypatch, tmp_path):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    styles = tmp_path / "Plot Styles"
    styles.mkdir()
    (styles / "monochrome.ctb").write_bytes(b"")
    (styles / "Anka.ctb").write_bytes(b"")
    (styles / "readme.txt").write_bytes(b"")
    app = types.SimpleNamespace(
        Preferences=types.SimpleNamespace(
            Files=types.SimpleNamespace(PrinterStyleSheetPath=f"{styles};C:\\does\\not\\exist")
        )
    )
    monkeypatch.setattr(module, "_acad_app", lambda: app)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, app


async def test_com_plot_style_list_marks_installed_and_adds_local_files(com_backend):
    backend, _ = com_backend
    rows = await backend.plot_style_list()
    by_name = {row["name"]: row for row in rows}
    assert by_name["monochrome.ctb"] == {
        "name": "monochrome.ctb",
        "source": "catalog",
        "installed": True,
    }
    assert by_name["acad.ctb"]["installed"] is False
    assert by_name["Anka.ctb"] == {"name": "Anka.ctb", "source": "installed", "installed": True}
    assert "readme.txt" not in by_name


async def test_com_plot_style_list_survives_an_unreachable_preferences_object(
    com_backend, monkeypatch
):
    backend, _ = com_backend
    from backends import com_backend as module

    def _boom():
        raise RuntimeError("no application")

    monkeypatch.setattr(module, "_acad_app", _boom)
    rows = await backend.plot_style_list()
    assert [row["name"] for row in rows] == list(CTB_CATALOG)
    assert all(row["installed"] is None for row in rows)


# ── COM: the plot call's shape ──────────────────────────────────────────────


@pytest.fixture
def com_plot(monkeypatch):
    """A fake document whose ``Plot.PlotToFile`` records the active layout and
    BACKGROUNDPLOT at the moment of the plot — the two things AutoCAD acts on
    and the reviewer measured wrong: with a sheet current, ``layout=None``
    plotted that sheet; with the operator's BACKGROUNDPLOT=2 the file did not
    exist when the call returned."""
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    state = {
        "vars": {"BACKGROUNDPLOT": 2},
        "sets": [],
        "active": "Layout1",
        "plots": [],
        "write_file": True,
        "plotted": True,
    }
    names = ["Model", "Layout1", "Layout2"]

    class _Layout:
        def __init__(self, name):
            self.Name = name

    class _Layouts:
        def Item(self, key):
            for name in names:
                if name.lower() == str(key).lower():
                    return _Layout(name)
            raise KeyError(key)

    class _Plot:
        def PlotToFile(self, path, config):
            state["plots"].append((state["active"], config, state["vars"]["BACKGROUNDPLOT"]))
            if state["write_file"]:
                Path(path).write_bytes(b"%PDF-1.4 fake")
            return state["plotted"]

    class _Doc:
        Layouts = _Layouts()
        Plot = _Plot()

        @property
        def ActiveLayout(self):
            return _Layout(state["active"])

        @ActiveLayout.setter
        def ActiveLayout(self, layout):
            state["active"] = layout.Name

        def GetVariable(self, name):
            return state["vars"][name]

        def SetVariable(self, name, value):
            state["sets"].append((name, value))
            state["vars"][name] = value

    monkeypatch.setattr(module, "_acad_doc", lambda: _Doc())
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, state


async def test_com_export_pdf_plots_model_space_by_name_in_the_foreground(com_plot, tmp_path):
    backend, state = com_plot
    out = tmp_path / "model.pdf"
    result = await backend.drawing_export_pdf(str(out))
    assert state["plots"] == [("Model", "DWG To PDF.pc3", 0)], (
        "layout=None is model space, plotted with BACKGROUNDPLOT forced to 0"
    )
    assert result == {"ok": True, "path": str(out), "layout": "Model", "bytes": out.stat().st_size}
    assert state["active"] == "Layout1", "the sheet that was current is current again"
    assert state["vars"]["BACKGROUNDPLOT"] == 2, "the operator's setting is restored"
    assert state["sets"] == [("BACKGROUNDPLOT", 0), ("BACKGROUNDPLOT", 2)]


async def test_com_export_pdf_plots_the_named_sheet_and_restores_the_tab(com_plot, tmp_path):
    backend, state = com_plot
    result = await backend.drawing_export_pdf(str(tmp_path / "l2.pdf"), layout="layout2")
    assert [row[0] for row in state["plots"]] == ["Layout2"]
    assert result["ok"] and result["layout"] == "layout2"
    assert state["active"] == "Layout1"


async def test_com_export_pdf_leaves_backgroundplot_alone_when_already_foreground(
    com_plot, tmp_path
):
    backend, state = com_plot
    state["vars"]["BACKGROUNDPLOT"] = 0
    await backend.drawing_export_pdf(str(tmp_path / "m.pdf"))
    assert state["sets"] == []


async def test_com_export_pdf_reports_a_refused_plot(com_plot, tmp_path):
    backend, state = com_plot
    state["plotted"] = False
    state["write_file"] = False
    result = await backend.drawing_export_pdf(str(tmp_path / "no.pdf"), layout="Layout1")
    assert result["ok"] is False and "PlotToFile returned False" in result["error"]
    assert state["vars"]["BACKGROUNDPLOT"] == 2


async def test_com_export_pdf_reports_a_plot_that_left_no_file(com_plot, tmp_path):
    """PlotToFile answering True with nothing on disk is the background-plot
    signature; it is a failure, not an ok."""
    backend, state = com_plot
    state["write_file"] = False
    result = await backend.drawing_export_pdf(str(tmp_path / "gone.pdf"))
    assert result["ok"] is False and "wrote no file" in result["error"]
    assert "bytes" not in result
