"""Headless dimensions must be readable ISO drawings, not ezdxf defaults.

`drawing_new` called `ezdxf.new(dxfversion=...)` without `setup=True`, and
nothing in the repo ever created or configured a dimstyle. So every headless
dimension rendered through ezdxf's bare `Standard` style: **1.0 mm** text with a
**comma** decimal marker. ISO 3098 puts the minimum lettering height at 2.5 mm,
and ISO 129 wants a point.

Two things made that worse than a cosmetic default:

* The document's own header already said ``$DIMTXT 2.5``. ezdxf renders from the
  dimstyle table entry and never reads the header, so the file asserted one
  height and drew another.
* A live AutoCAD seat renders from whatever its template carries, so the two
  engines printed a different text height *and* a different decimal marker for
  byte-identical tool calls, with no capability key or test recording it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

#: ISO 3098 minimum lettering height for technical drawings.
ISO_MIN_TEXT_MM = 2.5


def _rendered(backend, handle):
    """The text and height a drafter actually sees, out of the dimension block."""
    dim = backend._doc.entitydb.get(handle)
    block = backend._doc.blocks.get(dim.dxf.geometry)
    for entity in block:
        if entity.dxftype() == "MTEXT":
            return entity.text, float(entity.dxf.char_height)
        if entity.dxftype() == "TEXT":
            return entity.dxf.text, float(entity.dxf.height)
    raise AssertionError("the dimension block carries no text")


async def test_a_headless_dimension_is_lettered_at_iso_height(backend):
    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -20, layer="DIM")

    _text, height = _rendered(backend, dim.handle)

    assert height >= ISO_MIN_TEXT_MM, (
        f"rendered at {height} mm; ISO 3098 minimum is {ISO_MIN_TEXT_MM} mm"
    )


async def test_the_decimal_marker_is_a_point(backend):
    """ISO 129 wants a point. ezdxf's bare default is a comma."""
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -20, layer="DIM")

    text, _height = _rendered(backend, dim.handle)

    assert "," not in text, f"rendered {text!r} with a comma decimal marker"
    assert "33.33" in text


async def test_the_header_and_the_dimstyle_agree_about_text_height(backend):
    """The file used to assert $DIMTXT 2.5 and draw 1.0."""
    header = float(backend._doc.header.get("$DIMTXT"))
    style = backend._doc.dimstyles.get("Standard")

    assert float(style.dxf.dimtxt) == pytest.approx(header)


async def test_a_reopened_drawing_keeps_the_style(backend, tmp_path):
    """The style has to be in the file, not applied in memory on the way out."""
    import ezdxf

    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -20, layer="DIM")
    path = tmp_path / "styled.dxf"
    await backend.drawing_save_as(str(path))

    reopened = ezdxf.readfile(str(path))
    style = reopened.dimstyles.get("Standard")
    assert float(style.dxf.dimtxt) >= ISO_MIN_TEXT_MM
    assert int(style.dxf.dimdsep) == ord(".")
    assert reopened.entitydb.get(dim.handle) is not None


# ── the gate has to be able to see it ───────────────────────────────────────


async def test_the_iso_critique_catches_sub_iso_dimension_text(backend):
    """A drawing whose dimensions are 1.0 mm used to critique clean.

    Fixing the default stops this server *producing* such a drawing, but the
    gate still has to catch one — a template, an opened file, or a caller who
    sets the style back can all reintroduce it, and `drawing_critique`
    returning [] is the claim that the sheet is ISO-conformant.

    Both the style and the header are pushed down here. Since the DIM* header
    fold, the header outranks the style at render time, so setting the style
    alone no longer produces a 1.0 mm drawing — it would leave this test
    asserting a defect that is not on the sheet.
    """
    from engineering.critique import run_critique

    backend._doc.dimstyles.get("Standard").dxf.dimtxt = 1.0
    await backend.system_set_variable("DIMTXT", 1.0)
    await backend.dimension_linear(0, 0, 100, 0, 50, -20, layer="DIM")

    issues = await run_critique(backend, focus=["iso128"])

    assert issues, "1.0 mm dimension text is below the ISO 3098 minimum"
    assert any("2.5" in issue.message for issue in issues)


async def test_the_iso_critique_catches_a_comma_decimal_marker(backend):
    from engineering.critique import run_critique

    backend._doc.dimstyles.get("Standard").dxf.dimdsep = ord(",")
    await backend.system_set_variable("DIMDSEP", ord(","))
    await backend.dimension_linear(0, 0, 33.333, 0, 16, -20, layer="DIM")

    issues = await run_critique(backend, focus=["iso128"])

    assert any("decimal" in issue.message.lower() for issue in issues)


async def test_a_conformant_drawing_still_critiques_clean(backend):
    """The rule must not fire on the styles this server now ships."""
    from engineering.critique import run_critique

    await backend.drawing_apply_iso_layers("mech")
    await backend.dimension_linear(0, 0, 100, 0, 50, -20, layer="DIM")

    assert await run_critique(backend, focus=["iso128"]) == []


async def test_the_critique_grades_what_will_actually_be_drawn(backend):
    """When the header and the style disagree, the header is what gets drawn.

    Reading the style alone made `drawing_critique` able to be wrong in both
    directions once DIM* header variables started reaching the renderer: it
    would report a 1.0 mm defect on a sheet lettered at 2.5, and clear a sheet
    lettered at 1.0 because the style still said 2.5. The gate has to resolve
    the same header-then-style order the renderer does.
    """
    from engineering.critique import run_critique

    # Style below the minimum, header above it -> the sheet is conformant.
    backend._doc.dimstyles.get("Standard").dxf.dimtxt = 1.0
    await backend.system_set_variable("DIMTXT", 2.5)
    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -20, layer="DIM")
    assert _rendered(backend, dim.handle)[1] == pytest.approx(2.5)
    assert await run_critique(backend, focus=["iso128"]) == []

    # Style above it, header below -> the sheet is not.
    backend._doc.dimstyles.get("Standard").dxf.dimtxt = 2.5
    await backend.system_set_variable("DIMTXT", 1.0)
    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -60, layer="DIM")
    assert _rendered(backend, dim.handle)[1] == pytest.approx(1.0)
    assert await run_critique(backend, focus=["iso128"])


# ── the DIM* header fold must not fight the ISO defaults ────────────────────


async def test_the_iso_defaults_survive_the_header_fold(backend):
    """The regression that fails if the fold is keyed on the wrong baseline.

    The fold skips a variable when the header already agrees with what the
    renderer would use. That baseline is ezdxf's *renderer* fallback (dimtxt
    1.0, dimasz 0.25, dimdsep 0 -> comma), not the DXF schema default (2.5,
    2.5, 44). Key it on the schema defaults and a fresh drawing starts folding
    values it should leave alone — most visibly reintroducing the comma this
    file exists to keep out.
    """
    from engineering.critique import run_critique

    await backend.drawing_apply_iso_layers("mech")
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -20, layer="DIM")

    text, height = _rendered(backend, dim.handle)
    assert height == pytest.approx(ISO_MIN_TEXT_MM)
    assert text == "33.33"
    block = backend._doc.blocks.get(backend._doc.entitydb.get(dim.handle).dxf.geometry)
    arrows = {round(float(e.dxf.get("xscale", 1.0)), 3) for e in block if e.dxftype() == "INSERT"}
    assert arrows == {2.5}
    assert await run_critique(backend, focus=["iso128"]) == []


# ── DIMRND 0.0 means "no rounding", not "round to whole units" ──────────────


async def _reopened(backend, tmp_path):
    path = tmp_path / "roundtrip.dxf"
    await backend.drawing_save_as(str(path))
    await backend.drawing_open(str(path))
    return path


def _text(backend, handle) -> str:
    dim = backend._doc.entitydb.get(handle)
    for entity in backend._doc.blocks.get(dim.dxf.geometry):
        if entity.dxftype() == "MTEXT":
            return entity.text
        if entity.dxftype() == "TEXT":
            return entity.dxf.text
    raise AssertionError("the dimension block carries no text")


async def test_a_reopened_drawing_does_not_round_every_dimension_to_a_whole_unit(backend, tmp_path):
    """A saved file stores `dimrnd = 0.0`, and ezdxf rounds on it.

    AutoCAD's DIMRND 0 means *no* rounding. ezdxf applies `xround(value,
    dimrnd)` whenever the attribute is present at all, and `xround(33.333, 0.0)`
    is 33 — so every dimension on any reopened file was silently rounded to a
    whole unit *before* DIMDEC ever formatted it. Measured on a file this
    server saved itself: a 12.75 mm feature dimensioned as 13.
    """
    await _reopened(backend, tmp_path)

    await backend.drawing_settings({"dim_decimals": 2})
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)

    assert _text(backend, dim.handle) == "33.33"


async def test_dim_decimals_still_works_after_a_reopen(backend, tmp_path):
    await _reopened(backend, tmp_path)

    for decimals, expected in ((0, "33"), (1, "33.3"), (3, "33.333")):
        await backend.drawing_settings({"dim_decimals": decimals})
        dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30 * (decimals + 2))
        assert _text(backend, dim.handle) == expected


async def test_a_real_rounding_value_is_left_alone(backend, tmp_path):
    """Only the 0.0 sentinel is discarded — a drafter who asked for 0.5 mm
    rounding must keep it."""
    backend._doc.dimstyles.get("Standard").dxf.dimrnd = 0.5
    await _reopened(backend, tmp_path)

    assert float(backend._doc.dimstyles.get("Standard").dxf.dimrnd) == 0.5
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    # 33.5, not 33.33: the rounding survived. The trailing zero is gone because
    # DIMZIN defaults to 8, which suppresses it — that is formatting, and it is
    # a different knob from the rounding this test is about.
    assert _text(backend, dim.handle) == "33.5"
