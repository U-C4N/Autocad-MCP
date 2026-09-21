"""batch_plot: every sheet to PDF, each sheet size read back from the PDF.

The composition is exercised on the headless engine end to end; the COM-only
piece of this task (the installed ctb scan) is fake-tested; the SECTION 19
tools are called directly with a minimal context.
"""

from __future__ import annotations

import types

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
            Files=types.SimpleNamespace(PrintStyleSheetPath=f"{styles};C:\\does\\not\\exist")
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
