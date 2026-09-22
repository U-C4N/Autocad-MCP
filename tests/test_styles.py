"""Dimension, text and multileader styles on both engines.

Headless: real ezdxf tables, and the *rendered* dimension block as the witness
that a current style reaches the next dimension (the header alone was never
the thing that could lie — see tests/test_dimension_header_vars.py). Live:
a fake ActiveX surface that records the call shapes the COM block relies on
and models the one AutoCAD rule it depends on — making a style current
restores that style's saved DIM* settings.
"""

from __future__ import annotations

import types
from pathlib import Path

import ezdxf
import pytest

import config
from backends.capability import is_capability_default
from backends.ezdxf_backend import _current_dimstyle_name, _current_textstyle_name
from engineering.standards.dimstyles import PRESET_VARIABLES, resolve_dimstyle
from engineering.standards.mleaderstyles import resolve_mleaderstyle

pytestmark = pytest.mark.asyncio


def _rendered(backend, handle):
    """(text, text height, arrow xscales) out of the dimension's geometry block."""
    dim = backend._doc.entitydb.get(handle)
    block = backend._doc.blocks.get(dim.dxf.geometry)
    text = height = None
    arrows = []
    for entity in block:
        if entity.dxftype() == "MTEXT" and text is None:
            text, height = entity.text, round(float(entity.dxf.char_height), 4)
        elif entity.dxftype() == "TEXT" and text is None:
            text, height = entity.dxf.text, round(float(entity.dxf.height), 4)
        elif entity.dxftype() == "INSERT":
            arrows.append(round(float(entity.dxf.get("xscale", 1.0)), 4))
    if text is None:
        raise AssertionError("the dimension block carries no text")
    return text, height, sorted(set(arrows))


# ── headless engine ─────────────────────────────────────────────────────────


async def test_dimstyle_list_reports_standard_current_with_the_seventeen_values(backend):
    rows = await backend.dimstyle_list()
    assert [r["name"] for r in rows] == ["Standard"]
    assert rows[0]["current"] is True and rows[0]["values_available"] is True
    assert tuple(rows[0]["values"]) == PRESET_VARIABLES
    assert rows[0]["values"]["DIMTXT"] == 2.5
    assert rows[0]["values"]["DIMDSEP"] == ".", "drawing_new's ISO point"
    assert rows[0]["values"]["DIMTXSTY"] == "Standard", "unstored code reports the schema default"


async def test_dimstyle_create_iso25_writes_every_value_and_creates_isocp(backend):
    result = await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    assert result["ok"] is True and result["name"] == "ISO-25"
    assert result["textstyle_created"] is True and result["current"] is False
    assert result["values"]["DIMDSEP"] == "," and result["values"]["DIMTXSTY"] == "ISOCP"
    assert result["written"] == sorted(PRESET_VARIABLES)
    style = backend._doc.dimstyles.get("ISO-25")
    assert style.dxf.dimtxt == 2.5 and style.dxf.dimdsep == 44 and style.dxf.dimtad == 1
    assert style.dxf.dimtxsty == "ISOCP" and style.dxf.dimlwd == -2 and style.dxf.dimblk == ""
    assert backend._doc.styles.get("ISOCP").dxf.font == "isocp.shx"
    # ezdxf.new() writes $DIMSTYLE "ISO-25" with no such entry; creating the
    # entry must not make it current by accident.
    assert backend._doc.header["$DIMSTYLE"] == "Standard"
    assert _current_dimstyle_name(backend._doc) == "Standard"


async def test_set_current_syncs_the_header_and_the_next_dimension_renders_with_it(backend):
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None), set_current=True)
    header = backend._doc.header
    assert header["$DIMSTYLE"] == "ISO-25"
    assert header["$DIMDSEP"] == 44 and header["$DIMTXT"] == 2.5 and header["$DIMTXSTY"] == "ISOCP"

    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    assert backend._doc.entitydb.get(dim.handle).dxf.dimstyle == "ISO-25"
    assert _rendered(backend, dim.handle) == ("33,33", 2.5, [2.5])

    await backend.dimstyle_create("ANSI", resolve_dimstyle("ansi", None), set_current=True)
    dim2 = await backend.dimension_linear(0, 0, 33.333, 0, 16, -60)
    assert backend._doc.entitydb.get(dim2.handle).dxf.dimstyle == "ANSI"
    assert _rendered(backend, dim2.handle) == ("33.33", 3.0, [3.0])
    for creator, args in (
        (backend.dimension_aligned, (0, 0, 10, 10, 5, 15)),
        (backend.dimension_radius, (50, 50, 60, 50)),
        (backend.dimension_diameter, (70, 0, 90, 0)),
        (backend.dimension_angular, (0, 0, 10, 0, 0, 10, 5, 5)),
    ):
        created = await creator(*args)
        assert backend._doc.entitydb.get(created.handle).dxf.dimstyle == "ANSI", creator.__name__


async def test_dimstyle_modify_reports_only_what_moved_and_the_dimensions_using_it(backend):
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None), set_current=True)
    dim = await backend.dimension_linear(0, 0, 10, 0, 5, -10)
    result = await backend.dimstyle_modify("iso-25", {"DIMTXT": 2.5, "DIMDEC": 3, "dimrnd": 0})
    assert result["name"] == "ISO-25"
    assert result["changed"] == {"DIMDEC": [2, 3]}
    assert result["dimensions_using_style"] == [dim.handle]
    assert result["rerender_required"] is True
    assert backend._doc.header["$DIMDEC"] == 3, "the current style's header twin follows"
    assert not backend._doc.dimstyles.get("ISO-25").dxf.hasattr("dimrnd"), (
        "DIMRND 0 is 'no rounding' and must be discarded, not stored (xround(x, 0.0) rounds)"
    )
    again = await backend.dimstyle_modify("ISO-25", {"DIMDEC": 3})
    assert again["changed"] == {} and again["rerender_required"] is False
    on_standard = await backend.dimstyle_modify("Standard", {"DIMDEC": 4})
    assert on_standard["dimensions_using_style"] == [] and on_standard["rerender_required"] is False
    assert backend._doc.header["$DIMDEC"] == 3, "a non-current style does not touch the header"


async def test_refusals_happen_before_anything_is_written(backend):
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    with pytest.raises(ValueError, match="already exists"):
        await backend.dimstyle_create("iso-25", resolve_dimstyle("iso-25", None))
    with pytest.raises(ValueError, match="DIMTXSTY"):
        await backend.dimstyle_create("X", resolve_dimstyle("iso-25", {"DIMTXSTY": "NOPE"}))
    assert not backend._doc.dimstyles.has_entry("X")
    with pytest.raises(ValueError, match="DIMFOO"):
        await backend.dimstyle_create("Y", {"DIMFOO": 1})
    assert not backend._doc.dimstyles.has_entry("Y")
    with pytest.raises(ValueError, match="DIMDEC"):
        await backend.dimstyle_modify("ISO-25", {"DIMDEC": 9})
    with pytest.raises(ValueError, match="does not exist"):
        await backend.dimstyle_modify("NOPE", {"DIMDEC": 1})
    with pytest.raises(ValueError, match="empty"):
        await backend.dimstyle_modify("ISO-25", {})
    with pytest.raises(ValueError, match="does not exist"):
        await backend.dimstyle_set_current("NOPE")
    assert backend._doc.dimstyles.get("ISO-25").dxf.dimdec == 2
    # DIMDSEP is stored as ``ord(value)``: a control character or a code point
    # above the 16-bit group 278 must stop here, not reach the table.
    with pytest.raises(ValueError, match="DIMDSEP"):
        await backend.dimstyle_create("Z", {"DIMDSEP": "\n"})
    assert not backend._doc.dimstyles.has_entry("Z")
    with pytest.raises(ValueError, match="DIMDSEP"):
        await backend.dimstyle_modify("ISO-25", {"DIMDSEP": "\U0001f600"})
    assert backend._doc.dimstyles.get("ISO-25").dxf.dimdsep == 44


async def test_builtin_arrowheads_render_save_and_report_one_spelling(backend, tmp_path):
    """`validate_overrides` hands the canonical `_OBLIQUE`; stored verbatim ezdxf
    raises at render and at `doc.write`. The witness is the rendered INSERT and
    a save/reopen that reports the same name before and after."""
    values = resolve_dimstyle(
        "iso-25", {"DIMBLK": "oblique", "DIMSAH": 1, "DIMBLK1": "_DOT", "DIMBLK2": "."}
    )
    result = await backend.dimstyle_create("ISO-25", values, set_current=True)
    assert result["values"]["DIMBLK"] == "_OBLIQUE"
    style = backend._doc.dimstyles.get("ISO-25")
    assert style.dxf.dimblk == "OBLIQUE" and style.dxf.dimblk1 == "DOT" and style.dxf.dimblk2 == ""
    dim = await backend.dimension_linear(0, 0, 20, 0, 10, -10)
    block = backend._doc.blocks.get(backend._doc.entitydb.get(dim.handle).dxf.geometry)
    assert {e.dxf.name for e in block if e.dxftype() == "INSERT"} == {"_DOT", "_CLOSEDFILLED"}, (
        "DIMSAH on: the two separate arrowheads win over DIMBLK"
    )
    again = await backend.dimstyle_modify("ISO-25", {"DIMBLK": "OBLIQUE", "DIMBLK1": "_dot"})
    assert again["changed"] == {}, "re-setting a value to itself is not a change (spec §8.3)"
    # The style is current, so Restore loaded it into the $DIM* header. The
    # header is DXF-facing: a built-in is spelled with its underscore there
    # (what a live seat stores and what COM's system_get_variable returns),
    # never ezdxf's writer spelling the DIMSTYLE entry holds.
    header = backend._doc.header
    assert (header["$DIMBLK"], header["$DIMBLK1"], header["$DIMBLK2"]) == ("_OBLIQUE", "_DOT", "")
    assert await backend.system_get_variable("DIMBLK") == "_OBLIQUE", "the same answer as COM"
    path = str(tmp_path / "arrows.dxf")
    await backend.drawing_save_as(path)
    saved = ezdxf.readfile(path)
    assert saved.header["$DIMBLK"] == "_OBLIQUE" and saved.header["$DIMBLK1"] == "_DOT", (
        "the file names the block it actually holds, not a block that does not exist"
    )
    assert "_OBLIQUE" in saved.blocks and "_DOT" in saved.blocks
    await backend.drawing_open(path)
    rows = {r["name"]: r for r in await backend.dimstyle_list()}
    assert rows["ISO-25"]["values"]["DIMBLK"] == "_OBLIQUE", "reloaded, the same spelling"
    assert await backend.system_get_variable("DIMBLK") == "_OBLIQUE"
    reopened = await backend.dimstyle_modify("ISO-25", {"DIMBLK": "oblique"})
    assert reopened["changed"] == {}
    await backend.dimstyle_set_current("Standard")
    header = backend._doc.header  # drawing_open replaced the document
    assert (header["$DIMBLK"], header["$DIMBLK1"], header["$DIMBLK2"]) == ("", "", ""), (
        "closed filled is the empty string in the header, as in a fresh drawing"
    )


async def test_user_arrowhead_block_must_exist_before_anything_is_written(backend):
    with pytest.raises(ValueError, match="DIMBLK: block 'MYARROW' does not exist"):
        await backend.dimstyle_create("X", resolve_dimstyle("iso-25", {"DIMBLK": "MYARROW"}))
    assert not backend._doc.dimstyles.has_entry("X")
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    with pytest.raises(ValueError, match="DIMBLK1: block 'MYARROW' does not exist"):
        await backend.dimstyle_modify("ISO-25", {"DIMSAH": 1, "DIMBLK1": "MYARROW"})
    assert not backend._doc.dimstyles.get("ISO-25").dxf.hasattr("dimsah"), "nothing written"
    backend._doc.blocks.new("MYARROW").add_line((0, 0), (1, 1))
    result = await backend.dimstyle_modify("ISO-25", {"DIMBLK": "myarrow"})
    assert result["changed"] == {"DIMBLK": ["", "MYARROW"]}, "the block table's own spelling"
    assert backend._doc.dimstyles.get("ISO-25").dxf.dimblk == "MYARROW"


async def test_dimstyle_set_current_reports_previous_and_survives_save_reopen(backend, tmp_path):
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    result = await backend.dimstyle_set_current("iso-25")
    assert result == {"ok": True, "current": "ISO-25", "previous": "Standard", "changed": True}
    same = await backend.dimstyle_set_current("ISO-25")
    assert same["changed"] is False and same["previous"] == "ISO-25"
    path = str(tmp_path / "styles.dxf")
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)
    rows = await backend.dimstyle_list()
    assert [r["name"] for r in rows if r["current"]] == ["ISO-25"]
    assert rows[0]["name"] == "ISO-25" and rows[0]["values"]["DIMDSEP"] == ","
    # The table's report was never the thing that could lie; the render is.
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    assert _rendered(backend, dim.handle) == ("33,33", 2.5, [2.5])


async def test_a_user_dimstyle_survives_every_dxf_round_trip_without_rounding(
    backend, tmp_path, monkeypatch
):
    """ezdxf's exporter writes DIMRND 0.0 for *every* DIMSTYLE, and a stored 0.0
    makes the renderer round every dimension to a whole unit. Measured before
    `_normalise_dimstyle_rounding` walked the whole table: ISO-25 came back from
    a save/open, a `transaction_rollback` and a `drawing_undo` rendering 33.333
    as "33" and R6.35 as "R6" while `dimstyle_list` still reported DIMDEC 2."""
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 4)
    await backend.drawing_new()  # a history baseline with undo switched on
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None), set_current=True)
    await backend.dimstyle_create("ANSI", resolve_dimstyle("ansi", None))
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    assert _rendered(backend, dim.handle) == ("33,33", 2.5, [2.5])

    def _rounding_stored() -> dict[str, bool]:
        return {s.dxf.name: s.dxf.hasattr("dimrnd") for s in backend._doc.dimstyles}

    path = str(tmp_path / "roundtrip.dxf")
    await backend.drawing_save_as(path)
    assert ezdxf.readfile(path).dimstyles.get("ISO-25").dxf.get("dimrnd") == 0.0, (
        "the premise: the file on disk carries the 0.0 sentinel for a user style"
    )
    await backend.drawing_open(path)
    assert _rounding_stored() == {"Standard": False, "ISO-25": False, "ANSI": False}
    reopened = await backend.dimension_linear(0, 0, 33.333, 0, 16, -60)
    assert _rendered(backend, reopened.handle) == ("33,33", 2.5, [2.5])
    radius = await backend.dimension_radius(50, 50, 56.35, 50)
    assert _rendered(backend, radius.handle)[0] == "R6,35"
    await backend.dimstyle_set_current("ANSI")
    ansi = await backend.dimension_linear(0, 0, 12.75, 0, 6, -90)
    assert _rendered(backend, ansi.handle)[0] == "12.75"

    await backend.dimstyle_set_current("ISO-25")
    await backend.transaction_begin()
    await backend.entity_create_line(0, 0, 10, 10)
    await backend.transaction_rollback()
    assert _rounding_stored() == {"Standard": False, "ISO-25": False, "ANSI": False}
    rolled_back = await backend.dimension_linear(0, 0, 33.333, 0, 16, -120)
    assert _rendered(backend, rolled_back.handle) == ("33,33", 2.5, [2.5])

    await backend.entity_create_line(0, 0, 20, 20)
    assert (await backend.drawing_undo())["ok"] is True
    assert _rounding_stored() == {"Standard": False, "ISO-25": False, "ANSI": False}
    undone = await backend.dimension_linear(0, 0, 33.333, 0, 16, -150)
    assert _rendered(backend, undone.handle) == ("33,33", 2.5, [2.5])

    await backend.dimstyle_modify("ISO-25", {"DIMRND": 0.5})
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)
    assert backend._doc.dimstyles.get("ISO-25").dxf.dimrnd == 0.5, (
        "only the 0.0 sentinel is discarded; a rounding the drafter asked for survives"
    )


async def test_drawing_purge_keeps_what_the_style_tables_and_header_reference(backend, tmp_path):
    """`drawing_purge` followed only entity `.dxf.style`; a text style that only
    a DIMSTYLE, an MLEADERSTYLE or the header referenced was deleted, the next
    dimension silently rendered in Standard, and `drawing_save_as` raised
    `DXFTableEntryError` after leaving a truncated file on disk."""
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None), set_current=True)
    await backend.mleaderstyle_create("ANSI", resolve_mleaderstyle("ansi", None))
    await backend.textstyle_create("Notes", "arial.ttf", set_current=True)
    await backend.textstyle_create("Junk", "isocpeur.ttf")
    await backend.block_define("MYARROW", [{"type": "line", "x1": 0, "y1": 0, "x2": -1, "y2": 0.2}])
    await backend.dimstyle_create("ARROWED", resolve_dimstyle("ansi", {"DIMBLK": "MYARROW"}))

    result = await backend.drawing_purge()
    assert result["purged"]["text_styles"] == 1, "only the style nothing references goes"
    assert result["purged"]["blocks"] == 0
    names = {row["name"] for row in await backend.textstyle_list()}
    assert {"Standard", "ISOCP", "ROMANS", "Notes"} <= names and "Junk" not in names
    assert [r["name"] for r in await backend.textstyle_list() if r["current"]] == ["Notes"]
    assert "MYARROW" in backend._doc.blocks
    leader_styles = {r["name"]: r["text_style"] for r in await backend.mleaderstyle_list()}
    assert leader_styles["ANSI"] == "ROMANS"

    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    assert _rendered(backend, dim.handle) == ("33,33", 2.5, [2.5])
    path = str(tmp_path / "purged.dxf")
    await backend.drawing_save_as(path)
    reopened = ezdxf.readfile(path)
    assert reopened.dimstyles.get("ISO-25").dxf.dimtxsty == "ISOCP"
    assert reopened.header["$TEXTSTYLE"] == "Notes"


async def test_set_current_restores_every_dim_variable_not_just_the_stored_ones(backend, tmp_path):
    """A live seat's `-DIMSTYLE Restore` replaces *all* DIM* variables. Writing
    only what the target style stores left the previous style's values in the
    header: measured, ISO-25 with DIMZIN 0 / DIMSCALE 2.0 then
    `dimstyle_set_current("Standard")` rendered the next dimension at 5.0 mm as
    "33.30" and the saved file carried those next to `$DIMSTYLE Standard`."""
    from engineering.standards.dimstyles import DIM_VARIABLE_WHITELIST

    header = backend._doc.header
    fresh = {f"${var}": header.get(f"${var}", None) for var in DIM_VARIABLE_WHITELIST}
    baseline = await backend.dimension_linear(0, 0, 33.3, 0, 16, -30)

    values = resolve_dimstyle("iso-25", {"DIMZIN": 0, "DIMSCALE": 2.0})
    await backend.dimstyle_create("ISO-25", values, set_current=True)
    assert header["$DIMZIN"] == 0 and header["$DIMSCALE"] == 2.0
    assert header["$DIMTXSTY"] == "ISOCP"
    result = await backend.dimstyle_set_current("Standard")
    assert result["current"] == "Standard"
    assert {key: header.get(key, None) for key in fresh} == fresh, (
        "restoring Standard must reproduce the header drawing_new made"
    )
    dim = await backend.dimension_linear(0, 0, 33.3, 0, 16, -60)
    expected = ("33.3", 2.5, [2.5])
    assert _rendered(backend, dim.handle) == _rendered(backend, baseline.handle) == expected
    assert backend._doc.entitydb.get(dim.handle).override().dimstyle_attribs == {}, (
        "nothing stale is folded onto the dimension"
    )

    # DIMRND 0 discards the style attribute; the header twin must follow.
    await backend.dimstyle_set_current("ISO-25")
    assert header["$DIMZIN"] == 0 and header["$DIMSCALE"] == 2.0
    await backend.dimstyle_modify("ISO-25", {"DIMRND": 0.5})
    assert header["$DIMRND"] == 0.5
    cleared = await backend.dimstyle_modify("ISO-25", {"DIMRND": 0})
    assert cleared["changed"] == {"DIMRND": [0.5, 0]}
    assert header["$DIMRND"] == 0.0
    await backend.dimstyle_set_current("Standard")
    path = str(tmp_path / "restored.dxf")
    await backend.drawing_save_as(path)
    saved = ezdxf.readfile(path).header
    assert saved["$DIMSTYLE"] == "Standard"
    assert {key: saved.get(key, None) for key in fresh} == fresh


async def test_textstyle_create_list_set_current_and_new_text_uses_it(backend):
    result = await backend.textstyle_create("ISOCP", "isocp", set_current=True)
    assert result == {
        "ok": True,
        "name": "ISOCP",
        "font": "isocp.shx",
        "font_resolved": True,
        "current": True,
    }
    rows = await backend.textstyle_list()
    assert [r["name"] for r in rows] == ["ISOCP", "Standard"]
    assert rows[0] == {
        "name": "ISOCP",
        "font": "isocp.shx",
        "height": 0.0,
        "width_factor": 1.0,
        "oblique_deg": 0.0,
        "current": True,
    }
    assert backend._doc.header["$TEXTSTYLE"] == "ISOCP"
    assert _current_textstyle_name(backend._doc) == "ISOCP"
    text = await backend.entity_create_text("note", 0, 0)
    mtext = await backend.entity_create_mtext("note", 0, 10)
    assert backend._doc.entitydb.get(text.handle).dxf.style == "ISOCP"
    assert backend._doc.entitydb.get(mtext.handle).dxf.style == "ISOCP"
    back = await backend.textstyle_set_current("standard")
    assert back == {"ok": True, "current": "Standard", "previous": "ISOCP", "changed": True}
    assert (
        backend._doc.entitydb.get((await backend.entity_create_text("x", 0, 0)).handle).dxf.style
        == "Standard"
    )


async def test_textstyle_unknown_font_is_written_but_reported_unresolved(backend):
    result = await backend.textstyle_create("ODD", "acadmcp-missing-font.ttf", 3.5, 0.8, 15)
    assert result["font_resolved"] is False and result["current"] is False
    style = backend._doc.styles.get("ODD")
    assert style.dxf.font == "acadmcp-missing-font.ttf"
    assert style.dxf.height == 3.5 and style.dxf.width == 0.8 and style.dxf.oblique == 15.0


async def test_textstyle_refusals_write_nothing(backend):
    await backend.textstyle_create("ISOCP", "isocp.shx")
    with pytest.raises(ValueError, match="already exists"):
        await backend.textstyle_create("isocp", "isocp.shx")
    with pytest.raises(ValueError, match="width_factor"):
        await backend.textstyle_create("Z", "arial.ttf", width_factor=0)
    with pytest.raises(ValueError, match="oblique_deg"):
        await backend.textstyle_create("Z", "arial.ttf", oblique_deg=90)
    with pytest.raises(ValueError, match="height"):
        await backend.textstyle_create("Z", "arial.ttf", height=-1)
    with pytest.raises(ValueError, match="font"):
        await backend.textstyle_create("Z", "")
    assert not backend._doc.styles.has_entry("Z")
    with pytest.raises(ValueError, match="does not exist"):
        await backend.textstyle_set_current("NOPE")


async def test_mleaderstyle_create_iso_and_list(backend):
    result = await backend.mleaderstyle_create("ISO", resolve_mleaderstyle("iso", None))
    assert result["ok"] is True and result["textstyle_created"] is True
    assert result["values"] == {
        "arrow_size": 2.5,
        "landing_gap": 1.0,
        "text_style": "ISOCP",
        "text_height": 2.5,
    }
    style = backend._doc.mleader_styles.get("ISO")
    assert style.dxf.arrow_head_size == 2.5 and style.dxf.landing_gap_size == 1.0
    assert backend._doc.entitydb[style.dxf.text_style_handle].dxf.name == "ISOCP"
    rows = await backend.mleaderstyle_list()
    assert rows == [
        {
            "name": "ISO",
            "arrow_size": 2.5,
            "landing_gap": 1.0,
            "text_style": "ISOCP",
            "text_height": 2.5,
            "values_available": True,
        },
        {
            "name": "Standard",
            "arrow_size": 4.0,
            "landing_gap": 2.0,
            "text_style": "Standard",
            "text_height": 4.0,
            "values_available": True,
        },
    ]


async def test_mleaderstyle_refusals_write_nothing(backend):
    await backend.mleaderstyle_create("ISO", resolve_mleaderstyle("iso", None))
    with pytest.raises(ValueError, match="already exists"):
        await backend.mleaderstyle_create("iso", resolve_mleaderstyle("iso", None))
    with pytest.raises(ValueError, match="text_style"):
        await backend.mleaderstyle_create("X", resolve_mleaderstyle("iso", {"text_style": "NOPE"}))
    with pytest.raises(ValueError, match="missing"):
        await backend.mleaderstyle_create("X", {"arrow_size": 2.5})
    assert not backend._doc.mleader_styles.has_entry("X")


async def test_mleaderstyle_is_not_a_capability_boundary_on_either_engine(backend):
    """The v1.6 plan declared `mleaderstyle` COM-unsupported ("ActiveX exposes
    no MLeaderStyle collection"). The type library says otherwise: the
    ACAD_MLEADERSTYLE dictionary items are IAcadMLeaderStyle objects and
    AddObject(name, "AcDbMLeaderStyle") creates one. A refusal resting on a
    false premise is exactly what CLAUDE.md forbids `@capability` for, so
    neither map declares the key and neither engine inherits a default."""
    from backends.com_backend import ComBackend

    assert "mleaderstyle" not in backend.capabilities().to_dict()["features"]
    assert "mleaderstyle" not in ComBackend().capabilities().to_dict()["features"]
    for engine in (type(backend), ComBackend):
        assert not is_capability_default(engine, "mleaderstyle_create")
        assert not is_capability_default(engine, "mleaderstyle_list")


# ── live engine, against a fake ActiveX surface ──────────────────────────────


#: The font files the fake seat's font folder holds: the SHX presets (empty
#: files — the SHX loader is never read here) and the TrueType presets as
#: minimal sfnt files whose ``name`` table carries the family, plus a bold
#: face and txt.shx. The ``com_backend`` fixture writes them to a tmp folder.
FAKE_SHX_FONTS = ("isocp.shx", "romans.shx", "txt.shx")
FAKE_TTF_FONTS = {
    "arial.ttf": ("Arial", "Regular"),
    "arialbd.ttf": ("Arial", "Bold"),
    "isocpeur.ttf": ("ISOCPEUR", "Regular"),
    "orphan.ttf": ("Orphan Face", "Regular"),  # a file Windows has not installed
}
#: The typefaces the fake Windows font system has installed — what SetFont accepts.
FAKE_INSTALLED_FACES = {"Arial": "arial", "ISOCPEUR": "isocpeur"}


def minimal_sfnt(family: str, subfamily: str) -> bytes:
    """A TrueType file reduced to its table directory and a ``name`` table
    with Windows/Unicode (platform 3, en-US) family and subfamily records —
    enough for ``_truetype_face`` and nothing else."""
    import struct

    records, strings = b"", b""
    for name_id, text in ((1, family), (2, subfamily)):
        raw = text.encode("utf-16-be")
        records += struct.pack(">HHHHHH", 3, 1, 0x409, name_id, len(raw), len(strings))
        strings += raw
    header = struct.pack(">HHH", 0, 2, 6 + len(records))
    name_table = header + records + strings
    directory = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    directory += b"name" + struct.pack(">III", 0, 12 + 16, len(name_table))
    return directory + name_table


class _FilerError(RuntimeError):
    """``TextStyle.fontFile`` / ``SetVariable("DIMBLK", ...)`` failing the way
    ActiveX does: ``com_error (-2147352567, 'Exception occurred.', (0, None,
    'Filer error', ...))`` — a generic error the caller cannot catch by type."""


class _FakeStyle:
    """An AcadDimStyle / AcadTextStyle: ``Name``, ``CopyFrom`` and — for a text
    style — ``fontFile``, ``Width``, ``ObliqueAngle``, ``Height``. The
    ``fontFile`` setter models what AutoCAD 2026 measured: a bare SHX name on
    the support path is accepted and stored as given; a bare *TrueType* name
    is refused with a Filer error even when the file exists on the support
    path (``"arial.ttf"``, ``"isocpeur.ttf"``); a full path to an existing
    file is accepted; a file nobody has is refused. A refused write leaves
    the previous value (``""`` on a fresh entry) — the stub state the
    backend must never leave behind."""

    support_folder: Path | None = None  # set by the com_backend fixture
    installed_faces: dict[str, str] = FAKE_INSTALLED_FACES  # typeface -> file stem

    def __init__(self, name, calls):
        self.Name = name
        self._calls = calls
        self._font = "txt.shx"
        self.Width = 1.0
        self.ObliqueAngle = 0.0
        self.Height = 0.0
        self.deleted = False
        self.saved: dict | None = None  # the DIM* values this style holds, once saved

    @property
    def fontFile(self):
        return self._font

    @fontFile.setter
    def fontFile(self, value):
        self._calls.append(("fontFile", self.Name, value))
        path = Path(value)
        if path.is_absolute():
            accepted = path.is_file()
        elif path.suffix.lower() in (".ttf", ".ttc", ".otf"):
            accepted = False  # a bare TrueType name: Filer error, measured
        else:
            folder = _FakeStyle.support_folder
            accepted = folder is not None and (folder / value).is_file()
        if not accepted:
            raise _FilerError("(-2147352567, 'Exception occurred.', (0, None, 'Filer error'))")
        self._font = value

    def SetFont(self, typeface, bold, italic, charset, pitch):
        # AcadTextStyle.SetFont: a typeface Windows has installed is accepted
        # and fontFile reads back as its file name (measured: SetFont("Arial")
        # -> 'arial.ttf'); anything else is 'Invalid input'.
        self._calls.append(("SetFont", self.Name, typeface, bold, italic, charset, pitch))
        stem = self.installed_faces.get(typeface)
        if stem is None:
            raise _FilerError("(-2147352567, 'Exception occurred.', (0, None, 'Invalid input'))")
        self._font = stem + ("bd" if bold else "") + ("i" if italic else "") + ".ttf"

    def CopyFrom(self, source):
        # AcadDimStyle.CopyFrom(document) saves the document's current
        # dimension settings into this style.
        self._calls.append(("CopyFrom", self.Name, source))
        self.saved = dict(source.variables)

    def Delete(self):
        # IAcadObject.Delete: the owning collection drops the entry.
        self._calls.append(("Delete", self.Name))
        self.deleted = True


class _FakeCollection:
    def __init__(self, kind, calls, *names):
        self._kind = kind
        self._calls = calls
        self._all = [_FakeStyle(name, calls) for name in names]

    @property
    def entries(self):
        return [style for style in self._all if not style.deleted]

    @property
    def Count(self):
        return len(self.entries)

    def Item(self, index):
        return self.entries[index]

    def Add(self, name):
        self._calls.append((f"{self._kind}.Add", name))
        style = _FakeStyle(name, self._calls)
        self._all.append(style)
        return style


class _FakeBlocks:
    """``doc.Blocks``: Count / Item / Name over the block definitions."""

    def __init__(self, *names):
        self.entries = [types.SimpleNamespace(Name=name) for name in names]

    @property
    def Count(self):
        return len(self.entries)

    def Item(self, index):
        return self.entries[index]


class _FakeBlock:
    """A layout's ``Block``: ``IAcadBlock.Item`` is declared ``IAcadEntity*``,
    so each member comes back narrowed to the entity base (see
    ``_NarrowedToIAcadEntity``) — ``StyleName`` is a dimension member and is
    unreachable until un-narrowed."""

    def __init__(self, *entities):
        self.entities = list(entities)
        self.item_reads = 0

    @property
    def Count(self):
        return len(self.entities)

    def Item(self, index):
        self.item_reads += 1
        return _NarrowedToIAcadEntity(self.entities[index])


class _FakeSelectionSet:
    """An ``AcadSelectionSet`` filled by ``Select(acSelectionSetAll, ..., filter)``
    — ``ssget "X"`` over every layout's block — with the two filter groups
    the backend sends: 0 (DXF name, ``DIMENSION`` for every dimension class)
    and 3 (dimension style, a *wildcard pattern*, so the backend must hand it
    over backtick-escaped; the fake unescapes and compares literally,
    case-insensitively, the way AutoCAD does — measured live). ``Item`` is
    declared ``IAcadEntity*`` and comes back narrowed, like ``IAcadBlock.Item``."""

    def __init__(self, name, document):
        self.Name = name
        self._document = document
        self._members: list = []
        self.deleted = False

    @staticmethod
    def _unescape(pattern: str) -> str:
        out, escaped = [], False
        for char in pattern:
            if escaped:
                out.append(char)
                escaped = False
            elif char == "`":
                escaped = True
            elif char in "*?#@.~[],":
                raise AssertionError(
                    f"unescaped wildcard {char!r} in filter {pattern!r}: a style named "
                    "A.B would also select the dimensions of A_B"
                )
            else:
                out.append(char)
        return "".join(out)

    def Select(self, mode, point1, point2, filter_types, filter_values):
        assert mode == 5, "acSelectionSetAll — every layout, not the current space"
        assert point1 is None and point2 is None
        types_ = list(filter_types.value)
        values = list(filter_values.value)
        assert types_ == [0, 3], types_
        assert values[0] == "DIMENSION"
        wanted = self._unescape(values[1]).lower()
        self._document.SelectionSets.selects.append((self.Name, values[1]))
        self._members = [
            entity
            for layout_index in range(self._document.Layouts.Count)
            for entity in self._document.Layouts.Item(layout_index).Block.entities
            if "Dimension" in entity.ObjectName and str(entity.StyleName).lower() == wanted
        ]

    @property
    def Count(self):
        return len(self._members)

    def Item(self, index):
        return _NarrowedToIAcadEntity(self._members[index])

    def Delete(self):
        self.deleted = True
        self._document.SelectionSets.entries.remove(self)


class _FakeSelectionSets:
    def __init__(self, document):
        self._document = document
        self.entries: list[_FakeSelectionSet] = []
        self.selects: list[tuple[str, str]] = []  # (set name, group-3 filter value)

    def Add(self, name):
        assert all(ss.Name != name for ss in self.entries), f"selection set {name!r} exists"
        ss = _FakeSelectionSet(name, self._document)
        self.entries.append(ss)
        return ss


class _FakeLayouts:
    def __init__(self, *blocks):
        self.layouts = [types.SimpleNamespace(Block=block) for block in blocks]

    @property
    def Count(self):
        return len(self.layouts)

    def Item(self, index):
        return self.layouts[index]


class _FakeMLeaderStyle:
    """An AcadMLeaderStyle: the properties acax25enu.tlb's IAcadMLeaderStyle
    exposes that the backend reads or writes (doubles in drawing units, the
    text style by name), with AutoCAD's defaults for a fresh style."""

    PROPERTIES = ("ArrowSize", "LandingGap", "TextHeight", "TextStyle")

    def __init__(self, name, calls):
        self.Name = name
        self.ObjectName = "AcDbMLeaderStyle"
        self.ArrowSize, self.LandingGap, self.TextHeight, self.TextStyle = 4.0, 2.0, 4.0, "Standard"
        self._calls = calls  # last: property writes are recorded from here on

    def __setattr__(self, key, value):
        if key in self.PROPERTIES and "_calls" in vars(self):
            self._calls.append(("MLeaderStyle." + key, self.Name, value))
        super().__setattr__(key, value)


class _NarrowedToIAcadObject:
    """What pywin32 hands back for ``Dictionaries.Item``, ``Dictionary.Item``
    and ``Dictionary.AddObject`` once a makepy cache for the AutoCAD type
    library exists: acax25enu.tlb declares all three as returning
    ``IAcadObject*``, so the result is the gen_py ``IAcadObject`` class, which
    exposes only IAcadObject members and raises AttributeError for everything
    the runtime class (``IAcadDictionary``, ``IAcadMLeaderStyle``) adds — on a
    read *and* on a property write. The full object is reachable only through
    ``_oleobj_``, which is how the backend must un-narrow it (this is exactly
    what happened live on AutoCAD 2026 before the fix)."""

    IACADOBJECT_MEMBERS = frozenset(
        {
            "Application",
            "Database",
            "Document",
            "Handle",
            "HasExtensionDictionary",
            "ObjectID",
            "ObjectName",
            "OwnerID",
            "Delete",
            "Erase",
            "GetExtensionDictionary",
            "GetXData",
            "SetXData",
        }
    )

    def __init__(self, raw):
        object.__setattr__(self, "_oleobj_", raw)

    def __getattr__(self, key):
        if key in self.IACADOBJECT_MEMBERS:
            return getattr(self._oleobj_, key)
        raise AttributeError(
            "'<win32com.gen_py.AutoCAD 2025 Type Library.IAcadObject instance>' "
            f"object has no attribute {key!r}"
        )

    def __setattr__(self, key, value):
        # gen_py's DispatchBaseClass.__setattr__ consults _prop_map_put_, and
        # IAcadObject's is empty: every property write raises.
        raise AttributeError(
            "'<win32com.gen_py.AutoCAD 2025 Type Library.IAcadObject instance>' "
            f"object has no attribute {key!r}"
        )


class _NarrowedToIAcadEntity(_NarrowedToIAcadObject):
    """What pywin32 hands back for ``IAcadBlock.Item`` with a makepy cache:
    the gen_py ``IAcadEntity`` class — IAcadObject's members plus the entity
    base's — and nothing of the runtime class. On a dimension, ``StyleName``
    raises ``AttributeError: 'IAcadEntity' object has no attribute
    'StyleName'`` (measured live on AutoCAD 2026)."""

    IACADENTITY_MEMBERS = frozenset(
        {
            "Color",
            "EntityTransparency",
            "Hyperlinks",
            "Layer",
            "Linetype",
            "LinetypeScale",
            "Lineweight",
            "Material",
            "PlotStyleName",
            "TrueColor",
            "Visible",
            "ArrayPolar",
            "ArrayRectangular",
            "Copy",
            "GetBoundingBox",
            "Highlight",
            "IntersectWith",
            "Mirror",
            "Mirror3D",
            "Move",
            "Rotate",
            "Rotate3D",
            "ScaleEntity",
            "TransformBy",
            "Update",
        }
    )

    def __getattr__(self, key):
        if key in self.IACADENTITY_MEMBERS:
            return getattr(self._oleobj_, key)
        if key in self.IACADOBJECT_MEMBERS:
            return getattr(self._oleobj_, key)
        raise AttributeError(f"'IAcadEntity' object has no attribute {key!r}")


class _FakeDictionary:
    """The ACAD_MLEADERSTYLE dictionary: Count/Item/GetName over its objects
    and AddObject(keyword, class_name), the documented VBA route. Item and
    AddObject hand their result over narrowed to IAcadObject, as pywin32
    does."""

    def __init__(self, calls, *names):
        self._calls = calls
        self.ObjectName = "AcDbDictionary"
        self.objects = [_FakeMLeaderStyle(name, calls) for name in names]

    @property
    def Count(self):
        return len(self.objects)

    def Item(self, index):
        return _NarrowedToIAcadObject(self.objects[index])

    def GetName(self, obj):
        # ActiveX takes the object itself; a narrowed wrapper resolves to the
        # same object, so both spellings name it.
        raw = getattr(obj, "_oleobj_", obj)
        assert raw in self.objects, "GetName: object is not in this dictionary"
        return raw.Name

    def AddObject(self, keyword, class_name):
        self._calls.append(("Dictionary.AddObject", keyword, class_name))
        if class_name != "AcDbMLeaderStyle":
            raise RuntimeError(f"cannot create {class_name} here")
        style = _FakeMLeaderStyle(keyword, self._calls)
        self.objects.append(style)
        return _NarrowedToIAcadObject(style)


class _FakeDictionaries:
    """``doc.Dictionaries``: Item is declared IAcadObject* (narrowed); Add is
    declared IAcadDictionary* and comes back with its full surface."""

    def __init__(self, calls, entries):
        self._calls = calls
        self.entries = entries

    def Item(self, name):
        if name not in self.entries:
            raise RuntimeError(f"no dictionary {name}")
        return _NarrowedToIAcadObject(self.entries[name])

    def Add(self, name):
        self._calls.append(("Dictionaries.Add", name))
        if name in self.entries:
            raise RuntimeError(f"dictionary {name} already exists")
        self.entries[name] = _FakeDictionary(self._calls)
        return self.entries[name]


CURRENT_DIMVARS = {
    "DIMTXT": 2.5,
    "DIMASZ": 2.5,
    "DIMEXE": 1.25,
    "DIMEXO": 0.625,
    "DIMGAP": 0.625,
    "DIMTAD": 1,
    "DIMTIH": 0,
    "DIMTOH": 0,
    "DIMDEC": 2,
    "DIMDSEP": ".",
    "DIMLUNIT": 2,
    "DIMZIN": 8,
    "DIMBLK": "",
    "DIMTXSTY": "Standard",
    "DIMLWD": -2,
    "DIMLWE": -2,
    "DIMSCALE": 1.0,
}


class _FakeDocument:
    def __init__(self):
        self.calls: list[tuple] = []
        self.DimStyles = _FakeCollection("DimStyles", self.calls, "Standard")
        self.TextStyles = _FakeCollection("TextStyles", self.calls, "Standard")
        self.variables = dict(CURRENT_DIMVARS)
        self._active_dim = self.DimStyles.entries[0]
        self._active_dim.saved = dict(CURRENT_DIMVARS)
        self._active_text = self.TextStyles.entries[0]
        dim = types.SimpleNamespace(
            ObjectName="AcDbRotatedDimension", StyleName="ISO-25", Handle="2A"
        )
        line = types.SimpleNamespace(ObjectName="AcDbLine", Handle="2B")
        sheet_dim = types.SimpleNamespace(
            ObjectName="AcDbAlignedDimension", StyleName="Standard", Handle="2C"
        )
        dotted = types.SimpleNamespace(
            ObjectName="AcDbRadialDimension", StyleName="A.B", Handle="2E"
        )
        underscored = types.SimpleNamespace(
            ObjectName="AcDbRadialDimension", StyleName="A_B", Handle="2F"
        )
        self.Layouts = _FakeLayouts(
            _FakeBlock(dim, line, dotted), _FakeBlock(sheet_dim, underscored)
        )
        self.SelectionSets = _FakeSelectionSets(self)
        self.Blocks = _FakeBlocks("*Model_Space", "*Paper_Space", "MyArrow")
        self.Dictionaries = _FakeDictionaries(
            self.calls,
            {"ACAD_MLEADERSTYLE": _FakeDictionary(self.calls, "Standard", "Annotative")},
        )

    @property
    def mleader_dictionary(self) -> _FakeDictionary:
        """The raw ACAD_MLEADERSTYLE dictionary, for test setup and assertions
        (the backend only ever sees it narrowed through ``Dictionaries.Item``)."""
        return self.Dictionaries.entries["ACAD_MLEADERSTYLE"]

    def GetVariable(self, name):
        return self.variables[name]

    def SetVariable(self, name, value):
        self.calls.append(("SetVariable", name, value))
        if name in ("DIMBLK", "DIMBLK1", "DIMBLK2") and value and not value.startswith("_"):
            # AutoCAD refuses a user arrowhead naming no block, mid-write
            # (measured: DISP_E_EXCEPTION from SetVariable("DIMBLK", ...)).
            if value.lower() not in {b.Name.lower() for b in self.Blocks.entries}:
                raise _FilerError("(-2147352567, 'Exception occurred.', (0, None, 'Filer error'))")
        self.variables[name] = value

    @property
    def ActiveDimStyle(self):
        return self._active_dim

    @ActiveDimStyle.setter
    def ActiveDimStyle(self, style):
        # Making a style current restores its saved settings as the current
        # DIM* variables and discards unsaved overrides (AutoCAD's -DIMSTYLE
        # Restore rule; scripts/smoke_settings_com.py confirms it live). A
        # style never saved (fresh DimStyles.Add) keeps the current variables.
        self.calls.append(("ActiveDimStyle", style.Name))
        self._active_dim = style
        if style.saved is not None:
            self.variables.update(style.saved)

    @property
    def ActiveTextStyle(self):
        return self._active_text

    @ActiveTextStyle.setter
    def ActiveTextStyle(self, style):
        self.calls.append(("ActiveTextStyle", style.Name))
        self._active_text = style


class _FakeApplication:
    def __init__(self, support):
        self.Preferences = types.SimpleNamespace(
            Files=types.SimpleNamespace(SupportPath=str(support))
        )

    def __getattr__(self, name):
        if name in ("GetVariable", "SetVariable"):
            raise AttributeError(f"AutoCAD.Application.{name}")
        raise AttributeError(name)


@pytest.fixture
def com_backend(monkeypatch, tmp_path):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    # The fake seat's only font folder: the four presets plus txt.shx, as
    # empty files. The host's Windows Fonts folder is deliberately out of
    # reach so the tests measure the backend's resolution, not the machine.
    support = tmp_path / "Fonts"
    support.mkdir()
    for font in FAKE_SHX_FONTS:
        (support / font).write_bytes(b"")
    for font, (family, subfamily) in FAKE_TTF_FONTS.items():
        (support / font).write_bytes(minimal_sfnt(family, subfamily))
    monkeypatch.setattr(_FakeStyle, "support_folder", support)
    document = _FakeDocument()
    # The Application carries Preferences and nothing that reads or writes a
    # system variable: ``hasattr(app, "GetVariable") is False`` on AutoCAD
    # 2026 (measured 2026-09-22). GetVariable / SetVariable are members of
    # ``_FakeDocument`` only, so a backend path that still goes through the
    # Application fails here the way it fails live.
    app = _FakeApplication(support)
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    monkeypatch.setattr(module, "_acad_app", lambda: app)
    monkeypatch.setattr(module, "_regen", lambda: None)
    monkeypatch.setattr(module, "_com_font_folders", lambda: [support])
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, document


async def test_com_create_sets_variables_then_copies_from_the_document(com_backend):
    backend, document = com_backend
    result = await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    assert result["ok"] is True and result["current"] is False
    assert result["textstyle_created"] is True
    names = [call[0] for call in document.calls]
    assert names.index("TextStyles.Add") < names.index("DimStyles.Add"), (
        "the text style exists before DIMTXSTY names it"
    )
    assert document.calls[names.index("DimStyles.Add") + 1] == ("ActiveDimStyle", "ISO-25")
    sets = [call for call in document.calls if call[0] == "SetVariable"]
    assert len(sets) == 17
    assert ("SetVariable", "DIMDSEP", ",") in sets, "DIMDSEP is a string on ActiveX"
    assert ("SetVariable", "DIMTXSTY", "ISOCP") in sets
    assert ("SetVariable", "DIMTXT", 2.5) in sets and ("SetVariable", "DIMTAD", 1) in sets
    copy_index = names.index("CopyFrom")
    assert copy_index > names.index("SetVariable")
    assert document.calls[copy_index] == ("CopyFrom", "ISO-25", document)
    assert document.calls[-1] == ("ActiveDimStyle", "Standard"), "restored: set_current was False"
    assert result["values"]["DIMDSEP"] == "," and result["values"]["DIMTXSTY"] == "ISOCP"
    isocp = document.TextStyles.entries[-1]
    assert isocp.Name == "ISOCP" and isocp.fontFile == "isocp.shx"


async def test_com_create_set_current_keeps_the_new_style_active(com_backend):
    backend, document = com_backend
    result = await backend.dimstyle_create("ANSI", resolve_dimstyle("ansi", None), set_current=True)
    assert result["current"] is True
    activations = [c for c in document.calls if c[0] == "ActiveDimStyle"]
    assert activations == [("ActiveDimStyle", "ANSI")]
    assert document.ActiveDimStyle.Name == "ANSI"


async def test_com_create_refuses_before_touching_activex(com_backend):
    backend, document = com_backend
    with pytest.raises(ValueError, match="already exists"):
        await backend.dimstyle_create("standard", resolve_dimstyle("iso-25", None))
    with pytest.raises(ValueError, match="DIMTXSTY"):
        await backend.dimstyle_create("X", resolve_dimstyle("iso-25", {"DIMTXSTY": "NOPE"}))
    with pytest.raises(ValueError, match="DIMDEC"):
        await backend.dimstyle_create("X", {"DIMDEC": 9})
    assert document.calls == []


async def test_com_list_reports_values_for_the_current_style_only(com_backend):
    backend, document = com_backend
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    rows = await backend.dimstyle_list()
    assert [(r["name"], r["current"], r["values_available"]) for r in rows] == [
        ("ISO-25", False, False),
        ("Standard", True, True),
    ]
    assert rows[0]["values"] is None
    assert rows[1]["values"]["DIMDSEP"] == "." and tuple(rows[1]["values"]) == PRESET_VARIABLES


async def test_com_modify_switches_saves_and_switches_back(com_backend):
    backend, document = com_backend
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    document.calls.clear()
    result = await backend.dimstyle_modify("iso-25", {"DIMDEC": 3, "DIMTXT": 2.5})
    assert result["name"] == "ISO-25"
    assert result["changed"] == {"DIMDEC": [2, 3]}
    assert result["dimensions_using_style"] == ["2A"], "model and paper space, dimensions only"
    assert result["rerender_required"] is False, "AutoCAD re-renders on regen"
    assert document.calls == [
        ("ActiveDimStyle", "ISO-25"),
        ("SetVariable", "DIMDEC", 3),
        ("CopyFrom", "ISO-25", document),
        ("ActiveDimStyle", "Standard"),
    ]
    document.calls.clear()
    again = await backend.dimstyle_modify("ISO-25", {"DIMDEC": 3})
    assert again["changed"] == {}
    assert [c[0] for c in document.calls] == ["ActiveDimStyle", "ActiveDimStyle"], (
        "no CopyFrom when nothing moved"
    )
    with pytest.raises(ValueError, match="does not exist"):
        await backend.dimstyle_modify("NOPE", {"DIMDEC": 1})


async def test_com_set_current(com_backend):
    backend, document = com_backend
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    result = await backend.dimstyle_set_current("ISO-25")
    assert result == {"ok": True, "current": "ISO-25", "previous": "Standard", "changed": True}
    assert document.ActiveDimStyle.Name == "ISO-25"
    with pytest.raises(ValueError, match="does not exist"):
        await backend.dimstyle_set_current("NOPE")


async def test_com_textstyle_create_sets_font_width_oblique_height(com_backend):
    backend, document = com_backend
    result = await backend.textstyle_create("ISOCP", "isocp", 0.0, 1.0, 0.0, set_current=True)
    assert result == {
        "ok": True,
        "name": "ISOCP",
        "font": "isocp.shx",
        "font_resolved": True,
        "current": True,
    }
    style = document.TextStyles.entries[-1]
    assert (style.fontFile, style.Width, style.ObliqueAngle, style.Height) == (
        "isocp.shx",
        1.0,
        0.0,
        0.0,
    )
    assert document.ActiveTextStyle.Name == "ISOCP"
    odd = await backend.textstyle_create("ODD", "romans.shx", 3.5, 0.8, 15.0)
    assert odd["font_resolved"] is True
    assert document.TextStyles.entries[-1].ObliqueAngle == pytest.approx(0.2617993878), "radians"
    rows = await backend.textstyle_list()
    assert [(r["name"], r["current"]) for r in rows] == [
        ("ISOCP", True),
        ("ODD", False),
        ("Standard", False),
    ]
    assert rows[1]["oblique_deg"] == pytest.approx(15.0)
    with pytest.raises(ValueError, match="already exists"):
        await backend.textstyle_create("isocp", "isocp.shx")
    back = await backend.textstyle_set_current("standard")
    assert back == {"ok": True, "current": "Standard", "previous": "ISOCP", "changed": True}


async def test_com_mleaderstyle_list_reads_the_values_off_the_dictionary_objects(com_backend):
    backend, document = com_backend
    dictionary = document.mleader_dictionary
    dictionary.objects[1].ArrowSize = 3.0
    dictionary.objects[1].TextStyle = "ISOCP"
    document.calls.clear()  # fixture setup, not backend calls
    rows = await backend.mleaderstyle_list()
    assert rows == [
        {
            "name": "Annotative",
            "arrow_size": 3.0,
            "landing_gap": 2.0,
            "text_style": "ISOCP",
            "text_height": 4.0,
            "values_available": True,
        },
        {
            "name": "Standard",
            "arrow_size": 4.0,
            "landing_gap": 2.0,
            "text_style": "Standard",
            "text_height": 4.0,
            "values_available": True,
        },
    ]
    assert document.calls == [], "a list reads; it writes nothing"


async def test_com_mleaderstyle_create_adds_the_object_then_writes_its_properties(com_backend):
    backend, document = com_backend
    result = await backend.mleaderstyle_create("ISO", resolve_mleaderstyle("iso", None))
    assert result == {
        "ok": True,
        "name": "ISO",
        "values": {
            "arrow_size": 2.5,
            "landing_gap": 1.0,
            "text_style": "ISOCP",
            "text_height": 2.5,
        },
        "textstyle_created": True,
    }
    names = [call[0] for call in document.calls]
    assert names.index("TextStyles.Add") < names.index("Dictionary.AddObject"), (
        "the text style exists before TextStyle names it"
    )
    assert ("Dictionary.AddObject", "ISO", "AcDbMLeaderStyle") in document.calls
    writes = [call for call in document.calls if call[0].startswith("MLeaderStyle.")]
    assert writes == [
        ("MLeaderStyle.ArrowSize", "ISO", 2.5),
        ("MLeaderStyle.LandingGap", "ISO", 1.0),
        ("MLeaderStyle.TextHeight", "ISO", 2.5),
        ("MLeaderStyle.TextStyle", "ISO", "ISOCP"),
    ]
    rows = {row["name"]: row for row in await backend.mleaderstyle_list()}
    assert rows["ISO"] == {**result["values"], "name": "ISO", "values_available": True}


async def test_com_mleaderstyle_refusals_write_nothing(com_backend):
    backend, document = com_backend
    dictionary = document.mleader_dictionary
    with pytest.raises(ValueError, match="already exists"):
        await backend.mleaderstyle_create("standard", resolve_mleaderstyle("iso", None))
    with pytest.raises(ValueError, match="text_style"):
        await backend.mleaderstyle_create("X", resolve_mleaderstyle("iso", {"text_style": "NOPE"}))
    with pytest.raises(ValueError, match="missing"):
        await backend.mleaderstyle_create("X", {"arrow_size": 2.5})
    with pytest.raises(ValueError, match="arrow_size"):
        await backend.mleaderstyle_create("X", {**resolve_mleaderstyle("iso"), "arrow_size": 0})
    assert document.calls == [], "nothing reached ActiveX"
    assert [obj.Name for obj in dictionary.objects] == ["Standard", "Annotative"]


async def test_com_dictionary_route_is_narrowed_to_iacadobject_until_unwrapped(com_backend):
    """The failure mode as it happened live (AutoCAD 2026, pywin32 with a
    makepy cache): every object the dictionary route returns is wrapped as
    the DECLARED type, IAcadObject, so Count / Item / GetName on the
    dictionary and ArrowSize on a style raise AttributeError. The fake
    models that narrowing, and `_com_unnarrow` is the one door through it —
    a raw non-COM `_oleobj_` is handed back as-is, an object without one is
    returned unchanged."""
    from backends.com_backend import _com_unnarrow

    backend, document = com_backend
    narrowed = document.Dictionaries.Item("ACAD_MLEADERSTYLE")
    assert narrowed.ObjectName == "AcDbDictionary", "IAcadObject members still answer"
    with pytest.raises(AttributeError, match="no attribute 'Count'"):
        getattr(narrowed, "Count")  # noqa: B009 — the read itself is what raises
    with pytest.raises(AttributeError, match="no attribute 'Item'"):
        narrowed.Item(0)
    raw = document.mleader_dictionary
    assert _com_unnarrow(narrowed) is raw
    assert _com_unnarrow(raw) is raw, "an object without _oleobj_ is returned unchanged"

    item = raw.Item(0)
    assert item.ObjectName == "AcDbMLeaderStyle"
    with pytest.raises(AttributeError, match="no attribute 'ArrowSize'"):
        getattr(item, "ArrowSize")  # noqa: B009
    with pytest.raises(AttributeError, match="no attribute 'ArrowSize'"):
        item.ArrowSize = 2.5
    assert _com_unnarrow(item) is raw.objects[0]

    added = raw.AddObject("LDR-A", "AcDbMLeaderStyle")
    with pytest.raises(AttributeError, match="no attribute 'TextStyle'"):
        added.TextStyle = "ISOCP"
    assert _com_unnarrow(added) is raw.objects[-1]
    assert raw.GetName(added) == raw.GetName(raw.objects[-1]) == "LDR-A"


async def test_com_mleaderstyle_create_adds_the_dictionary_to_a_drawing_without_one(com_backend):
    """A foreign drawing may lack ACAD_MLEADERSTYLE: list reports no styles
    (and never adds the dictionary), create adds it through Dictionaries.Add
    — whose IAcadDictionary* result is un-narrowed like the rest — and then
    the style."""
    backend, document = com_backend
    del document.Dictionaries.entries["ACAD_MLEADERSTYLE"]
    assert await backend.mleaderstyle_list() == []
    assert document.calls == [] and "ACAD_MLEADERSTYLE" not in document.Dictionaries.entries
    result = await backend.mleaderstyle_create("ISO", resolve_mleaderstyle("iso", None))
    assert result["ok"] is True and result["name"] == "ISO"
    names = [call[0] for call in document.calls]
    assert names.index("Dictionaries.Add") < names.index("Dictionary.AddObject")
    assert ("Dictionaries.Add", "ACAD_MLEADERSTYLE") in document.calls
    assert [obj.Name for obj in document.mleader_dictionary.objects] == ["ISO"]
    rows = await backend.mleaderstyle_list()
    assert [row["name"] for row in rows] == ["ISO"] and rows[0]["arrow_size"] == 2.5


# ── the MCP tools (SECTION 18) ──────────────────────────────────────────────

STYLE_TOOLS = {
    "dimstyle_list",
    "dimstyle_create",
    "dimstyle_modify",
    "dimstyle_set_current",
    "textstyle_list",
    "textstyle_create",
    "textstyle_set_current",
    "mleaderstyle_list",
    "mleaderstyle_create",
}


class _Ctx:
    """Enough of a fastmcp Context for a direct tool call (tests/test_capabilities.py)."""

    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, *_args, **_kwargs):
        return None


async def test_style_tools_are_registered_and_grouped_under_styles():
    import server

    names = {t.name for t in await server._registered_tools() if getattr(t, "name", None)}
    assert STYLE_TOOLS <= names
    groups = await server._tool_groups()
    assert STYLE_TOOLS <= set(groups["styles"]), "the style tag must win over query/create/modify"
    for other in ("layers", "drawing", "entity_query", "entity_creation", "entity_modification"):
        assert not STYLE_TOOLS & set(groups.get(other, [])), other


async def test_dimstyle_tools_resolve_the_preset_then_write(backend):
    import server

    ctx = _Ctx(backend)
    created = await server.dimstyle_create(
        name="ISO-25", preset="iso-25", overrides={"dimdec": 3}, set_current=True, ctx=ctx
    )
    assert created["current"] is True and created["values"]["DIMDEC"] == 3
    listed = await server.dimstyle_list(ctx=ctx)
    assert listed["count"] == 2 and listed["current"] == "ISO-25"
    modified = await server.dimstyle_modify(name="ISO-25", overrides={"DIMTXT": 3.5}, ctx=ctx)
    assert modified["changed"] == {"DIMTXT": [2.5, 3.5]}
    switched = await server.dimstyle_set_current(name="Standard", ctx=ctx)
    assert switched["previous"] == "ISO-25" and switched["current"] == "Standard"


async def test_dimstyle_create_tool_refuses_an_unknown_override_before_writing(backend):
    import server

    ctx = _Ctx(backend)
    with pytest.raises(ValueError, match="DIMFOO"):
        await server.dimstyle_create(name="X", overrides={"DIMFOO": 1}, ctx=ctx)
    with pytest.raises(ValueError, match="preset"):
        await server.dimstyle_create(name="X", preset="din", ctx=ctx)
    assert (await server.dimstyle_list(ctx=ctx))["count"] == 1


async def test_textstyle_and_mleaderstyle_tools_round_trip(backend):
    import server

    ctx = _Ctx(backend)
    made = await server.textstyle_create(name="ISOCP", font="isocp", set_current=True, ctx=ctx)
    assert made["font_resolved"] is True
    assert (await server.textstyle_list(ctx=ctx))["current"] == "ISOCP"
    assert (await server.textstyle_set_current(name="Standard", ctx=ctx))["previous"] == "ISOCP"
    leader = await server.mleaderstyle_create(
        name="ISO", preset="iso", overrides={"arrow_size": 3}, ctx=ctx
    )
    assert leader["values"]["arrow_size"] == 3.0 and leader["textstyle_created"] is False
    assert (await server.mleaderstyle_list(ctx=ctx))["count"] == 2
    with pytest.raises(ValueError, match="preset"):
        await server.mleaderstyle_create(name="X", preset="din", ctx=ctx)


# ── live engine: the four Task 11 review findings ────────────────────────────


def _com_names(collection):
    return [entry.Name for entry in collection.entries]


async def test_com_textstyle_create_writes_truetype_by_typeface_and_shx_by_name(com_backend):
    """Measured on AutoCAD 2026: `fontFile = "arial.ttf"` raises 'Filer error'
    (bundled presets included) and a full path stores the machine path in the
    STYLE record, while `SetFont("Arial", ...)` stores the clean `arial.ttf`.
    The fake refuses a bare TrueType name the same way, so a backend that
    still wrote one could not pass this."""
    backend, document = com_backend
    arial = await backend.textstyle_create("ARIAL", "arial")
    assert arial == {
        "ok": True,
        "name": "ARIAL",
        "font": "arial.ttf",
        "font_resolved": True,
        "current": False,
    }
    eur = await backend.textstyle_create("ISOCPEUR", "isocpeur.ttf")
    bold = await backend.textstyle_create("HEAD", "arialbd.ttf", height=5.0)
    shx = await backend.textstyle_create("ISOCP", "ISOCP")
    assert eur["font"] == "isocpeur.ttf" and bold["font"] == "arialbd.ttf"
    font_writes = [c for c in document.calls if c[0] in ("fontFile", "SetFont")]
    assert font_writes == [
        ("SetFont", "ARIAL", "Arial", False, False, 0, 0),
        ("SetFont", "ISOCPEUR", "ISOCPEUR", False, False, 0, 0),
        ("SetFont", "HEAD", "Arial", True, False, 0, 0),
        ("fontFile", "ISOCP", "isocp.shx"),
    ], "TrueType by typeface (bold from the subfamily), SHX by the name given"
    rows = {row["name"]: row["font"] for row in await backend.textstyle_list()}
    assert rows == {
        "ARIAL": "arial.ttf",
        "ISOCPEUR": "isocpeur.ttf",
        "HEAD": "arialbd.ttf",
        "ISOCP": "isocp.shx",
        "Standard": "txt.shx",
    }, "what the STYLE records carry: file names, never a machine path"
    assert shx["font_resolved"] is True


async def test_com_textstyle_create_refuses_an_unlocatable_font_before_add(com_backend):
    """Before this fix `TextStyles.Add` ran first, `fontFile` then raised, and
    the table kept the name with an empty font — so a retry with a good font
    was refused 'already exists'. The refusal now precedes the Add, nothing is
    written, and the retry succeeds."""
    backend, document = com_backend
    with pytest.raises(ValueError, match=r"font: font file 'nosuchfont\.shx' is not on") as info:
        await backend.textstyle_create("PROBE", "nosuchfont.shx")
    assert "before any write" in str(info.value) and "isocp.shx" in str(info.value)
    with pytest.raises(ValueError, match="font"):
        await backend.textstyle_create("PROBE", r"C:\nosuch\folder\arial.ttf")
    assert document.calls == [], "nothing reached ActiveX"
    assert _com_names(document.TextStyles) == ["Standard"]
    retry = await backend.textstyle_create("PROBE", "romans.shx")
    assert retry["ok"] is True and document.TextStyles.entries[-1].fontFile == "romans.shx"


async def test_com_textstyle_create_removes_its_entry_when_the_write_still_fails(com_backend):
    """A TrueType file that exists but whose face Windows has not installed
    is the one refusal `SetFont` can still raise after the Add ('Invalid
    input'). The error propagates — never a silent stub — and the entry the
    backend added is deleted, so the name is free again."""
    backend, document = com_backend
    with pytest.raises(_FilerError, match="Invalid input"):
        await backend.textstyle_create("ORPHAN", "orphan.ttf")
    assert _com_names(document.TextStyles) == ["Standard"]
    assert ("Delete", "ORPHAN") in document.calls
    again = await backend.textstyle_create("ORPHAN", "arial.ttf")
    assert again["ok"] is True, "the failed create left no 'already exists' behind"
    garbage = _FakeStyle.support_folder / "broken.ttf"
    garbage.write_bytes(b"not an sfnt at all")
    with pytest.raises(
        ValueError, match=r"font: font file 'broken\.ttf' .* not a readable TrueType"
    ):
        await backend.textstyle_create("BROKEN", "broken.ttf")
    assert _com_names(document.TextStyles) == ["Standard", "ORPHAN"], (
        "an unreadable TrueType file is refused before the Add"
    )


async def test_com_dimstyle_create_creates_the_truetype_presets_for_dimtxsty(com_backend):
    """Finding 2: `DIMTXSTY: ARIAL` used to raise on the live engine and leave
    an ARIAL entry with no font, so the *second* call reported ok with a
    dimension style whose text style had no font. Every preset is created
    through the same typeface route now, first time, and a preset whose file
    the seat lacks is refused by DIMTXSTY before any write."""
    backend, document = com_backend
    result = await backend.dimstyle_create(
        "ANSI-AR", resolve_dimstyle("ansi", {"DIMTXSTY": "ARIAL"})
    )
    assert result["ok"] is True and result["textstyle_created"] is True
    assert result["values"]["DIMTXSTY"] == "ARIAL"
    arial = document.TextStyles.entries[-1]
    assert (arial.Name, arial.fontFile) == ("ARIAL", "arial.ttf")
    assert ("SetFont", "ARIAL", "Arial", False, False, 0, 0) in document.calls
    eur = await backend.mleaderstyle_create(
        "LDR", resolve_mleaderstyle("iso", {"text_style": "ISOCPEUR"})
    )
    assert eur["textstyle_created"] is True and eur["values"]["text_style"] == "ISOCPEUR"
    assert document.TextStyles.entries[-1].fontFile == "isocpeur.ttf"

    (_FakeStyle.support_folder / "romans.shx").unlink()
    document.calls.clear()
    with pytest.raises(ValueError, match=r"DIMTXSTY: font file 'romans\.shx' is not on"):
        await backend.dimstyle_create("X", resolve_dimstyle("iso-25", {"DIMTXSTY": "ROMANS"}))
    assert document.calls == [] and _com_names(document.DimStyles) == ["Standard", "ANSI-AR"]
    assert "ROMANS" not in _com_names(document.TextStyles), "no stub text style either"


async def test_com_dimstyle_create_refuses_a_missing_arrowhead_block_before_any_write(com_backend):
    """Finding 3: `SetVariable("DIMBLK", "NOSUCHBLOCK")` raises mid-write on
    the live engine, after Add and ActiveDimStyle — the half-made style was
    left as the operator's current style. Refused first now, as headlessly."""
    backend, document = com_backend
    with pytest.raises(ValueError, match=r"DIMBLK: block 'NOSUCHBLOCK' does not exist"):
        await backend.dimstyle_create("BAD", resolve_dimstyle("iso-25", {"DIMBLK": "NOSUCHBLOCK"}))
    with pytest.raises(ValueError, match="DIMBLK2"):
        await backend.dimstyle_create("BAD", {"DIMSAH": 1, "DIMBLK2": "Nope"})
    assert document.calls == [] and _com_names(document.DimStyles) == ["Standard"]
    assert document.ActiveDimStyle.Name == "Standard"
    ok = await backend.dimstyle_create(
        "ARROWS", resolve_dimstyle("iso-25", {"DIMBLK": "myarrow", "DIMBLK1": "_oblique"})
    )
    assert ok["values"]["DIMBLK"] == "MyArrow", "the block table's own spelling, as headlessly"
    assert ("SetVariable", "DIMBLK", "MyArrow") in document.calls
    assert ("SetVariable", "DIMBLK1", "_OBLIQUE") in document.calls, "a built-in needs no block"


async def test_com_dimstyle_create_rolls_back_a_write_that_fails_after_add(
    com_backend, monkeypatch
):
    """Whatever ActiveX refuses after DimStyles.Add (here: one SetVariable),
    the drawing is left as it was found — previous style current with its
    saved values, no half-made dimension style, no preset text style the
    call created for it — and the error is the caller's, not swallowed."""
    backend, document = com_backend
    real_set = document.SetVariable

    def failing_set(name, value):
        if name == "DIMSCALE":
            raise _FilerError("(-2147352567, 'Exception occurred.', (0, None, 'Filer error'))")
        real_set(name, value)

    monkeypatch.setattr(document, "SetVariable", failing_set)
    with pytest.raises(_FilerError):
        await backend.dimstyle_create("HALF", resolve_dimstyle("iso-25", None), set_current=True)
    assert document.ActiveDimStyle.Name == "Standard"
    assert document.variables == CURRENT_DIMVARS, "Restore discarded the unsaved overrides"
    assert _com_names(document.DimStyles) == ["Standard"]
    assert _com_names(document.TextStyles) == ["Standard"], "the ISOCP it created is gone too"
    tail = [c for c in document.calls if c[0] in ("ActiveDimStyle", "Delete")]
    assert tail == [
        ("ActiveDimStyle", "HALF"),
        ("ActiveDimStyle", "Standard"),
        ("Delete", "HALF"),
        ("Delete", "ISOCP"),
    ]


async def test_com_dimstyle_modify_refuses_a_missing_arrowhead_and_restores_on_failure(
    com_backend, monkeypatch
):
    backend, document = com_backend
    await backend.dimstyle_create("ISO-25", resolve_dimstyle("iso-25", None))
    document.calls.clear()
    with pytest.raises(ValueError, match=r"DIMBLK1: block 'Nope' does not exist"):
        await backend.dimstyle_modify("ISO-25", {"DIMBLK1": "Nope"})
    assert document.calls == [], "refused before the style was even made current"
    spelled = await backend.dimstyle_modify("ISO-25", {"DIMBLK": "MYARROW"})
    assert spelled["changed"] == {"DIMBLK": ["", "MyArrow"]}

    real_set = document.SetVariable

    def failing_set(name, value):
        if name == "DIMTXT":
            raise _FilerError("(-2147352567, 'Exception occurred.', (0, None, 'Filer error'))")
        real_set(name, value)

    monkeypatch.setattr(document, "SetVariable", failing_set)
    document.calls.clear()
    with pytest.raises(_FilerError):
        await backend.dimstyle_modify("ISO-25", {"DIMDEC": 4, "DIMTXT": 9.0})
    assert document.ActiveDimStyle.Name == "Standard"
    assert document.variables["DIMDEC"] == 2, "Restore discarded the DIMDEC override"
    assert [c[0] for c in document.calls] == ["ActiveDimStyle", "SetVariable", "ActiveDimStyle"], (
        "DIMDEC was written, DIMTXT refused, the previous style restored; no CopyFrom"
    )


async def test_com_dimensions_using_style_is_one_filtered_selection_not_a_walk(com_backend):
    """One ``SelectionSet.Select(acSelectionSetAll, (0 . "DIMENSION") (3 . style))``
    answers from AutoCAD's index, whatever the drawing size. The walk it
    replaced un-narrowed every entity of every layout (~8-10 ms each) and put
    `dimstyle_modify` past COM_CALL_TIMEOUT on a ~6k-entity drawing after its
    write had landed. The fake narrows ``SelectionSet.Item`` to IAcadEntity,
    as pywin32 does; ``Handle`` is an IAcadObject member and answers."""
    from backends.com_backend import _com_dimensions_using

    backend, document = com_backend
    assert _com_dimensions_using(document, "iso-25") == ["2A"], "case-insensitive"
    assert _com_dimensions_using(document, "Standard") == ["2C"], "paper space too"
    assert _com_dimensions_using(document, "NOPE") == []
    blocks = [document.Layouts.Item(i).Block for i in range(document.Layouts.Count)]
    assert [block.item_reads for block in blocks] == [0, 0], "no per-entity COM read"
    assert document.SelectionSets.entries == [], "every selection set is deleted again"
    assert len(document.SelectionSets.selects) == 3 and document.calls == [], (
        "one Select per lookup, nothing else on the document"
    )

    more = types.SimpleNamespace(ObjectName="AcDbAlignedDimension", StyleName="ISO-25", Handle="A")
    document.Layouts.Item(1).Block.entities.insert(0, more)
    assert _com_dimensions_using(document, "ISO-25") == ["A", "2A"], (
        "ascending handle order, not the selection set's report order"
    )


async def test_com_dimensions_using_style_escapes_wildcards_in_the_style_name(com_backend):
    """The group-3 filter value is a wcmatch pattern: measured live, ``A.B``
    selected the dimensions of ``A_B`` too, ``N#1`` those of ``N21``,
    ``P[1]`` those of ``P1``, ``S@X`` those of ``SAX``. Backtick-escaping
    the name makes the match literal (and the fake refuses a raw wildcard)."""
    from backends.com_backend import _com_dimensions_using, _wcmatch_literal

    backend, document = com_backend
    assert _wcmatch_literal("ISO-25") == "ISO`-25"
    assert _wcmatch_literal("A.B") == "A`.B"
    assert _wcmatch_literal("N#1 P[1] S@X T~ *?,") == "N`#1 P`[1`] S`@X T`~ `*`?`,"
    assert _wcmatch_literal("Plain_Name 2") == "Plain_Name 2"
    assert _com_dimensions_using(document, "A.B") == ["2E"], "not A_B's dimension"
    assert _com_dimensions_using(document, "a_b") == ["2F"]
    assert [value for _, value in document.SelectionSets.selects[-2:]] == ["A`.B", "a_b"]


async def test_com_dimensions_using_style_swallows_nothing_and_still_cleans_up(com_backend):
    from backends.com_backend import _com_dimensions_using

    backend, document = com_backend

    def rejected(self, *args):
        raise RuntimeError("RPC_E_CALL_REJECTED")

    original = _FakeSelectionSet.Select
    _FakeSelectionSet.Select = rejected
    try:
        with pytest.raises(RuntimeError, match="RPC_E_CALL_REJECTED"):
            _com_dimensions_using(document, "Standard")
    finally:
        _FakeSelectionSet.Select = original
    assert document.SelectionSets.entries == [], "the selection set is deleted on failure too"


async def test_truetype_face_reads_family_and_subfamily(tmp_path):
    import struct

    from backends.com_backend import _com_font_path, _truetype_face

    regular = tmp_path / "r.ttf"
    regular.write_bytes(minimal_sfnt("Segoe UI Semibold", "Regular"))
    assert _truetype_face(regular) == ("Segoe UI Semibold", False, False)
    bold_italic = tmp_path / "bi.ttf"
    bold_italic.write_bytes(minimal_sfnt("Arial", "Bold Italic"))
    assert _truetype_face(bold_italic) == ("Arial", True, True)
    collection = tmp_path / "c.ttc"
    inner = bytearray(minimal_sfnt("Cambria", "Regular"))
    struct.pack_into(">I", inner, 12 + 8, 16 + 12 + 16)  # table offsets are file-absolute
    collection.write_bytes(b"ttcf" + struct.pack(">HHII", 1, 0, 1, 16) + inner)
    assert _truetype_face(collection) == ("Cambria", False, False)
    (tmp_path / "junk.ttf").write_bytes(b"\x00\x01\x00\x00" + b"\xff" * 40)
    assert _truetype_face(tmp_path / "junk.ttf") is None
    assert _truetype_face(tmp_path / "missing.ttf") is None
    windows_arial = Path(r"C:\Windows\Fonts\arial.ttf")
    if windows_arial.is_file():
        assert _truetype_face(windows_arial) == ("Arial", False, False)
    assert _com_font_path(str(regular)) == regular
    assert _com_font_path(str(tmp_path / "missing.ttf")) is None


# ── drawing_apply_standard ──────────────────────────────────────────────────


async def test_apply_iso_then_dimension_linear_carries_iso25(backend):
    from engineering.standards.apply import apply_standard

    result = await apply_standard(backend, "iso")
    assert result["standard"] == "iso"
    assert result["dimstyle"] == {"name": "ISO-25", "created": True, "current": True}
    assert result["textstyle"] == {"name": "ISOCP", "created": True, "current": True}
    assert result["settings"]["changed"] == {"INSUNITS": [6, 4]}, "ezdxf.new() starts in metres"
    assert result["layers"]["layer_set"] == "mech"
    assert result["layers"]["layers"]["DIM"] in ("created", "exists")
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    assert _rendered(backend, dim.handle) == ("33,33", 2.5, [2.5])
    assert backend._doc.header["$TEXTSTYLE"] == "ISOCP"
    assert await backend.system_get_variable("LUNITS") == 2
    assert await backend.system_get_variable("INSUNITS") == 4


async def test_apply_ansi_then_dimension_linear_carries_ansi(backend):
    from engineering.standards.apply import apply_standard

    result = await apply_standard(backend, "ANSI", layers=False)
    assert result["dimstyle"]["name"] == "ANSI" and result["textstyle"]["name"] == "ROMANS"
    assert result["layers"] is None
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    assert _rendered(backend, dim.handle) == ("33.33", 3.0, [3.0])


async def test_second_apply_is_idempotent_and_reports_nothing_created(backend):
    from engineering.standards.apply import apply_standard

    await apply_standard(backend, "iso")
    again = await apply_standard(backend, "iso")
    assert again["dimstyle"]["created"] is False and again["textstyle"]["created"] is False
    assert again["settings"]["changed"] == {}
    assert set(again["layers"]["layers"].values()) == {"exists"}
    swapped = await apply_standard(backend, "ansi", layers=False)
    assert swapped["dimstyle"]["created"] is True
    assert (await backend.dimstyle_list())[0]["name"] == "ANSI"
    back = await apply_standard(backend, "iso", layers=False, units=False)
    assert back["dimstyle"]["created"] is False and back["settings"] == {
        "changed": {},
        "applied": False,
    }
    assert backend._doc.header["$DIMSTYLE"] == "ISO-25"


async def test_apply_refuses_an_unknown_standard_before_writing(backend):
    from engineering.standards.apply import apply_standard

    with pytest.raises(ValueError, match="standard"):
        await apply_standard(backend, "din")
    with pytest.raises(TypeError, match="layers"):
        await apply_standard(backend, "iso", layers="yes")
    assert [r["name"] for r in await backend.dimstyle_list()] == ["Standard"]
    assert await backend.system_get_variable("INSUNITS") == 6


async def test_apply_standard_tool_is_registered_in_the_styles_group(backend):
    import server

    names = {t.name for t in await server._registered_tools() if getattr(t, "name", None)}
    assert "drawing_apply_standard" in names
    styles_group = set((await server._tool_groups())["styles"])
    assert styles_group == STYLE_TOOLS | {"drawing_apply_standard"}, "SECTION 18 is exactly ten"
    result = await server.drawing_apply_standard(standard="iso", layers=False, ctx=_Ctx(backend))
    assert result["dimstyle"]["created"] is True


async def test_com_apply_standard_creates_both_styles_and_sets_units(com_backend):
    from engineering.standards.apply import apply_standard

    backend, document = com_backend
    document.variables.update(
        {"INSUNITS": 1, "LUNITS": 4, "AUNITS": 0, "LTSCALE": 1.0, "DIMSCALE": 1.0}
    )
    _record_reads(document)
    result = await apply_standard(backend, "iso", layers=False)
    assert result["dimstyle"]["created"] is True and result["textstyle"]["created"] is True
    assert result["settings"]["changed"] == {"INSUNITS": [1, 4], "LUNITS": [4, 2]}
    names = [call[0] for call in document.calls]
    assert names[:5] == ["GetVariable"] * 5, "every unit variable is read on the document first"
    assert [c[1] for c in document.calls[:5]] == [
        "INSUNITS",
        "LUNITS",
        "AUNITS",
        "LTSCALE",
        "DIMSCALE",
    ]
    assert names[5] == "TextStyles.Add" and "DimStyles.Add" in names
    assert ("ActiveTextStyle", "ISOCP") in document.calls
    assert document.ActiveDimStyle.Name == "ISO-25"
    assert document.variables["DIMDSEP"] == "," and document.variables["INSUNITS"] == 4


def _record_reads(document):
    """Log ``GetVariable`` into ``document.calls`` too, so a test can see the
    reads' place among the writes (the fake logs writes only)."""
    real_get = document.GetVariable

    def _get(name):
        document.calls.append(("GetVariable", name))
        return real_get(name)

    document.GetVariable = _get


async def test_com_apply_standard_refuses_before_writing_when_a_sysvar_read_fails(com_backend):
    """Regression: the units loop ran *after* both styles were created, and its
    INSUNITS read went through ``AcadApplication.GetVariable`` -- a member the
    Application does not have -- so on a live seat the call failed with ISO-25
    and ISOCP already written and current. Every variable the call will write
    is read first; a read the document refuses is a refusal before any write."""
    from engineering.standards.apply import apply_standard

    backend, document = com_backend
    document.variables.update({"INSUNITS": 4, "LUNITS": 2, "AUNITS": 0})  # no LTSCALE
    _record_reads(document)
    with pytest.raises(RuntimeError, match="LTSCALE"):
        await apply_standard(backend, "iso", layers=False)
    writes = [c for c in document.calls if c[0] != "GetVariable"]
    assert writes == [], "nothing was created, made current or set"
    assert [e.Name for e in document.TextStyles.entries] == ["Standard"]
    assert [e.Name for e in document.DimStyles.entries] == ["Standard"]
    # ``units=False`` writes no variable, so it reads none and needs none.
    result = await apply_standard(backend, "iso", layers=False, units=False)
    assert result["dimstyle"]["created"] is True and result["settings"] == {
        "changed": {},
        "applied": False,
    }


# ── profile decision ────────────────────────────────────────────────────────


async def test_lean_carries_the_two_set_current_tools_and_apply_standard_only():
    import server

    lean = server.LEAN_TOOL_NAMES
    assert {"dimstyle_set_current", "textstyle_set_current", "drawing_apply_standard"} <= lean
    assert not (STYLE_TOOLS - {"dimstyle_set_current", "textstyle_set_current"}) & lean, (
        "authoring and listing styles is full-profile work"
    )
    claimed = set().union(*server.PACK_TOOL_NAMES.values())
    assert not claimed & (STYLE_TOOLS | {"drawing_apply_standard"}), (
        "styles are core: no pack may claim them"
    )
