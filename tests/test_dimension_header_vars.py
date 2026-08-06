"""DIM* header variables have to reach the dimension, not just the header.

v1.5.1 fixed exactly one of them. `system_set_variable("DIMSCALE", 4)` grew a
fold that carried `$DIMSCALE` onto the per-dimension override, because ezdxf
renders a DIMENSION from the dimstyle table entry plus that override and never
reads the document header. Every *other* DIM* variable kept the original defect:

    drawing_new()
    system_set_variable("DIMTXT", 5.0)   -> {"ok": True}
    system_set_variable("DIMASZ", 6.0)   -> {"ok": True}
    system_set_variable("DIMDEC", 1)     -> {"ok": True}
    system_set_variable("DIMDSEP", 44)   -> {"ok": True}
    system_set_variable("DIMZIN", 0)     -> {"ok": True}
    dimension_linear(0, 0, 33.333, 0, 16, -30)

Measured before the fix: five `ok: True` results, a header that reads back every
one of the five values, and a dimension still rendered at 2.5 mm text with 2.5
arrows and "33.33" — byte-identical to the one drawn before the writes. A live
AutoCAD seat builds a dimension from the current dimvars, so the same five calls
did change the drawing there: one tool call, one reported success, two engines
drawing different sheets.

These tests read the *rendered* geometry block rather than the header, because
the header was never the thing that was broken.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _rendered(backend, handle):
    """(text, text height, arrow scales) out of the dimension's geometry block.

    The override lands in the dimension's XDATA, not on `dim.dxf`, so the block
    ezdxf renders is the only honest witness. Arrow size shows up as the xscale
    of the `_CLOSEDFILLED` arrowhead INSERT — the one non-text observable here,
    which is what keeps these tests from passing on a text-only patch.
    """
    dim = backend._doc.entitydb.get(handle)
    block = backend._doc.blocks.get(dim.dxf.geometry)
    text = None
    height = None
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


# ── text height ─────────────────────────────────────────────────────────────


async def test_dimtxt_reaches_the_next_dimension(backend):
    """Asserted as a ratio, for the reason recorded in the DIMSCALE tests.

    Hardcoding 5.0 would hide the day the base height moves; hardcoding 2.5
    would make the test depend on the ISO default it is not measuring.
    """
    plain = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    _t, base, _a = _rendered(backend, plain.handle)

    await backend.system_set_variable("DIMTXT", base * 2)
    after = await backend.dimension_linear(0, 0, 33.333, 0, 16, -60)

    _t, height, _a = _rendered(backend, after.handle)
    assert height == pytest.approx(base * 2), (
        f"DIMTXT must letter the next dimension: {base} -> {height}"
    )


async def test_dimtxt_and_dimscale_still_multiply(backend):
    """`text_height = get_char_height(...) * dimscale` (ezdxf dim_base.py).

    Both fold into the same override dict, so the two must compose rather than
    the later one winning.
    """
    plain = await backend.dimension_linear(0, 0, 100, 0, 50, -30)
    _t, base, _a = _rendered(backend, plain.handle)

    await backend.system_set_variable("DIMTXT", base * 2)
    await backend.system_set_variable("DIMSCALE", 3.0)
    after = await backend.dimension_linear(0, 0, 100, 0, 50, -60)

    _t, height, _a = _rendered(backend, after.handle)
    assert height == pytest.approx(base * 6)


async def test_a_fixed_height_text_style_still_defeats_dimtxt(backend):
    """A limitation this fold cannot repair, pinned so nobody assumes it can.

    `get_char_height` reads the *text style's* height first and only falls back
    to DIMTXT when it is 0 (ezdxf dim_base.py). Measured: with the `Standard`
    text style at 3.0 mm, DIMTXT 5.0 still renders 3.0 — the header write
    reports success and the sheet does not move. Only a height-0 (variable)
    text style lets DIMTXT through, which is what `drawing_new` ships.
    """
    backend._doc.styles.get("Standard").dxf.height = 3.0
    await backend.system_set_variable("DIMTXT", 5.0)

    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -30)

    _t, height, _a = _rendered(backend, dim.handle)
    assert height == pytest.approx(3.0), "a fixed-height text style outranks DIMTXT"


# ── arrows: the one non-text observable ─────────────────────────────────────


async def test_dimasz_scales_the_arrowheads(backend):
    plain = await backend.dimension_linear(0, 0, 100, 0, 50, -30)
    _t, _h, base = _rendered(backend, plain.handle)
    assert base, "no arrowhead INSERT to measure"

    await backend.system_set_variable("DIMASZ", 6.0)
    after = await backend.dimension_linear(0, 0, 100, 0, 50, -60)

    _t, _h, arrows = _rendered(backend, after.handle)
    assert arrows == pytest.approx([6.0]), f"DIMASZ must size the arrows: {base} -> {arrows}"


# ── number formatting ───────────────────────────────────────────────────────


@pytest.mark.parametrize(("decimals", "expected"), [(1, "33.3"), (0, "33")])
async def test_dimdec_sets_the_decimal_places(backend, decimals, expected):
    await backend.system_set_variable("DIMDEC", decimals)

    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)

    text, _h, _a = _rendered(backend, dim.handle)
    assert text == expected


async def test_dimdsep_switches_the_decimal_marker(backend):
    """44 is the char code for a comma; ezdxf refuses a string in the header."""
    await backend.system_set_variable("DIMDSEP", 44)

    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)

    text, _h, _a = _rendered(backend, dim.handle)
    assert text == "33,33"


async def test_an_untouched_drawing_still_uses_a_point(backend):
    """The converse regression: ISO 129 wants a point, and ezdxf's own renderer
    fallback for `dimdsep` is 0 -> a comma. A fold keyed on the wrong baseline
    would reintroduce the comma on every dimension."""
    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)

    text, _h, _a = _rendered(backend, dim.handle)
    assert text == "33.33"


@pytest.mark.parametrize(("dimzin", "expected"), [(0, "100.000"), (8, "100")])
async def test_dimzin_controls_trailing_zeros(backend, dimzin, expected):
    await backend.system_set_variable("DIMDEC", 3)
    await backend.system_set_variable("DIMZIN", dimzin)

    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -30)

    text, _h, _a = _rendered(backend, dim.handle)
    assert text == expected


# ── all five tools, not just linear ─────────────────────────────────────────


async def test_dimtxt_reaches_all_five_dimension_tools(backend):
    """aligned and angular do not go through `build_dim_override`.

    That is exactly how they were missed when DIMSCALE was folded the first
    time (see `test_dimscale_reaches_aligned_and_angular_too`), so every tool
    is measured here rather than trusting the shared helper.
    """
    plain = {
        "linear": await backend.dimension_linear(0, 0, 100, 0, 50, -30),
        "aligned": await backend.dimension_aligned(0, 0, 60, 60, 40, 70),
        "angular": await backend.dimension_angular(0, 0, 50, 0, -30, 50, 30, 30),
        "radius": await backend.dimension_radius(0, 0, 20, 0, 10),
        "diameter": await backend.dimension_diameter(0, 0, 40, 0, 10),
    }
    base = {name: _rendered(backend, info.handle)[1] for name, info in plain.items()}

    await backend.system_set_variable("DIMTXT", 5.0)
    scaled = {
        "linear": await backend.dimension_linear(0, 0, 100, 0, 50, -60),
        "aligned": await backend.dimension_aligned(0, 0, 60, 60, 40, 90),
        "angular": await backend.dimension_angular(0, 0, 50, 0, -30, 50, 50, 50),
        "radius": await backend.dimension_radius(0, 0, 20, 0, 30),
        "diameter": await backend.dimension_diameter(0, 0, 40, 0, 30),
    }

    for name, info in scaled.items():
        _t, height, _a = _rendered(backend, info.handle)
        assert height == pytest.approx(5.0), f"{name}: {base[name]} -> {height}, wanted 5.0"


async def test_angular_text_is_a_different_knob_and_is_deliberately_not_folded(backend):
    """DIMDEC does not format an angular dimension — DIMADEC does.

    Pinned because $DIMADEC and $DIMAZIN sit at 0 in a fresh header while
    ezdxf's renderer falls back to 2 and 2. Adding them to the fold table to
    make it look "complete" would silently reformat every angular dimension
    this server has ever drawn, from 239.04 deg to 239 deg. The allowlist is
    measured, not exhaustive.
    """
    await backend.system_set_variable("DIMDEC", 0)

    dim = await backend.dimension_angular(0, 0, 50, 0, -30, 50, 30, 30)

    text, _h, _a = _rendered(backend, dim.handle)
    assert text.startswith("239.04"), f"angular text must keep DIMADEC's 2 places, got {text!r}"


# ── the fold must not litter ────────────────────────────────────────────────


async def test_a_fresh_drawing_carries_no_dimension_override(backend):
    """Generalises `test_the_default_dimscale_adds_no_override`.

    After `drawing_new` the header and the `Standard` dimstyle agree on every
    folded variable ($DIMTXT 2.5/2.5, $DIMASZ 2.5/2.5, $DIMDEC 2/2, $DIMDSEP
    46/46, $DIMSCALE and $DIMZIN unset on the style and equal to the renderer
    fallback), so the fold has nothing to say and the dimension must carry no
    XDATA override at all.
    """
    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -30)

    entity = backend._doc.entitydb.get(dim.handle)
    style = backend._doc.dimstyles.get("Standard")
    assert entity.get_acad_dstyle(style) == {}


async def test_the_basic_tolerance_box_survives_the_fold(backend):
    """`tol_mode="basic"` sets dimgap=-1.0 to draw the theoretically-exact box.

    The fold runs *after* `build_dim_override` and must only `setdefault`, so a
    caller's explicit value always outranks the header. dimgap is not in the
    table today, but this pins the ordering that protects it if it ever is.
    """
    await backend.system_set_variable("DIMTXT", 5.0)

    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -30, tol_mode="basic")

    entity = backend._doc.entitydb.get(dim.handle)
    style = backend._doc.dimstyles.get("Standard")
    overrides = entity.get_acad_dstyle(style)
    assert overrides.get("dimgap") == pytest.approx(-1.0)
    assert overrides.get("dimtxt") == pytest.approx(5.0)


# ── precedence, both directions ─────────────────────────────────────────────


async def test_setting_the_variable_back_reverts_the_next_dimension(backend):
    plain = await backend.dimension_linear(0, 0, 100, 0, 50, -30)
    _t, base, _a = _rendered(backend, plain.handle)

    await backend.system_set_variable("DIMTXT", 5.0)
    await backend.dimension_linear(0, 0, 100, 0, 50, -60)
    await backend.system_set_variable("DIMTXT", base)
    reverted = await backend.dimension_linear(0, 0, 100, 0, 50, -90)

    _t, height, _a = _rendered(backend, reverted.handle)
    assert height == pytest.approx(base)


async def test_a_rollback_restores_the_header_and_the_next_dimension(backend):
    """Transactions snapshot the whole DXF, header included."""
    plain = await backend.dimension_linear(0, 0, 100, 0, 50, -30)
    _t, base, _a = _rendered(backend, plain.handle)

    await backend.transaction_begin()
    await backend.system_set_variable("DIMTXT", 5.0)
    await backend.transaction_rollback()

    after = await backend.dimension_linear(0, 0, 100, 0, 50, -60)
    _t, height, _a = _rendered(backend, after.handle)
    assert height == pytest.approx(base)


# ── opened files ────────────────────────────────────────────────────────────


async def test_an_opened_file_honours_its_own_header(backend, tmp_path):
    """A file whose header disagrees with its dimstyle used to draw the style.

    `drawing_open` never runs `_apply_iso_dimstyle`, so before the fold the
    header of an opened template was decorative: it could claim $DIMTXT 5.0
    and every dimension still came out at the style's 2.5. AutoCAD builds a
    dimension from the current dimvars, so the file stops lying about itself.
    """
    import ezdxf

    doc = ezdxf.new(dxfversion="R2010")
    doc.dimstyles.get("Standard").dxf.dimtxt = 2.5
    doc.header["$DIMTXT"] = 5.0
    path = tmp_path / "header_says_five.dxf"
    doc.saveas(str(path))

    await backend.drawing_open(str(path))
    dim = await backend.dimension_linear(0, 0, 100, 0, 50, -30)

    _t, height, _a = _rendered(backend, dim.handle)
    assert height == pytest.approx(5.0)


# ── the fold itself ─────────────────────────────────────────────────────────


async def test_a_header_without_the_variable_does_not_break_the_fold():
    """An opened file may be missing a DIM* var, or carry junk in it."""
    from backends.ezdxf_backend import _with_header_dimvars

    class _Header:
        def get(self, name, default=None):
            return {"$DIMTXT": "not a number"}.get(name, default)

    class _Doc:
        header = _Header()
        dimstyles = None  # resolving the style must not be required either

    assert _with_header_dimvars(_Doc(), None) is None
    assert _with_header_dimvars(_Doc(), {"dimgap": -1.0}) == {"dimgap": -1.0}


async def test_the_dollar_prefixed_name_is_still_rejected(backend):
    """`system_set_variable` prefixes '$' unconditionally, so '$DIMTXT' becomes
    '$$DIMTXT' and ezdxf refuses it. Pinned because the original bug report
    reached for the dollar form and read the resulting exception as the no-op:
    a test written that way would fail for the wrong reason forever."""
    with pytest.raises(Exception, match=r"\$\$DIMTXT"):
        await backend.system_set_variable("$DIMTXT", 5.0)
