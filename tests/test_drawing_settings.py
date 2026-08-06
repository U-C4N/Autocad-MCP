"""Tests for the drawing_settings facade — friendly read/change of AutoCAD
system variables (units, precision, scales, osnap, …)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_read_snapshot_returns_known_keys(backend):
    snap = await backend.drawing_settings()
    assert snap["ok"] is True
    s = snap["settings"]
    for key in ("units", "linear_precision", "ltscale", "dimscale", "osmode"):
        assert key in s


async def test_set_and_read_back_units_by_name(backend):
    res = await backend.drawing_settings({"units": "mm"})
    assert res["ok"] is True
    assert res["applied"]["units"] == "mm"
    snap = await backend.drawing_settings()
    assert snap["settings"]["units"]["name"] == "mm"
    assert snap["settings"]["units"]["code"] == 4


async def test_set_units_cm_and_inch_codes(backend):
    await backend.drawing_settings({"units": "cm"})
    assert (await backend.drawing_settings())["settings"]["units"]["code"] == 5
    await backend.drawing_settings({"units": "inch"})
    assert (await backend.drawing_settings())["settings"]["units"]["code"] == 1


async def test_set_numeric_settings(backend):
    res = await backend.drawing_settings(
        {
            "dimscale": 2.0,
            "linear_precision": 3,
            "ltscale": 0.5,
        }
    )
    assert res["ok"] is True
    snap = (await backend.drawing_settings())["settings"]
    assert snap["dimscale"] == 2.0
    assert snap["linear_precision"] == 3
    assert snap["ltscale"] == 0.5


async def test_unknown_key_reports_error_without_failing_others(backend):
    res = await backend.drawing_settings({"dimscale": 1.5, "bogus_key": 1})
    assert res["applied"]["dimscale"] == 1.5
    assert "bogus_key" in res["errors"]
    assert res["ok"] is False


async def test_bad_unit_value_is_reported(backend):
    res = await backend.drawing_settings({"units": "furlongs"})
    assert "units" in res["errors"]


# ── server tool wiring ──────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, *a, **k):
        pass


async def test_server_tool_reads_and_writes(backend):
    import server

    ctx = _FakeCtx(backend)
    write = await server.drawing_settings(settings={"units": "mm", "dimscale": 1.0}, ctx=ctx)
    assert write["ok"] is True
    read = await server.drawing_settings(ctx=ctx)
    assert read["settings"]["units"]["name"] == "mm"


# ── DIMSCALE has to reach the dimension, not just the header ────────────────


def _dim_text_heights(backend, handle) -> list[float]:
    """The text heights actually rendered into the dimension's geometry block.

    The override lands in the dimension's XDATA, not on `dim.dxf`, so the only
    honest check is the drawing it produces.
    """
    dim = backend._doc.entitydb.get(handle)
    block = backend._doc.blocks.get(dim.dxf.geometry)
    heights = []
    for entity in block:
        if entity.dxftype() == "MTEXT":
            heights.append(round(float(entity.dxf.char_height), 3))
        elif entity.dxftype() == "TEXT":
            heights.append(round(float(entity.dxf.height), 3))
    return sorted(set(heights))


async def test_dimscale_changes_the_dimension_it_is_set_before(backend):
    """`drawing_settings({"dimscale": ...})` reported success and did nothing.

    It writes the `$DIMSCALE` header variable, which is what AutoCAD reads when
    it creates a dimension — so on the live backend the call worked. ezdxf
    renders dimensions from the dimstyle table entry and never consults the
    header, so headlessly the text stayed the same size however large the
    number was: one call, one reported success, two different drawings.
    """
    plain = await backend.dimension_linear(0, 0, 100, 0, 50, -30)
    await backend.drawing_settings({"dimscale": 4.0})
    scaled = await backend.dimension_linear(0, 0, 100, 0, 50, -60)

    before = _dim_text_heights(backend, plain.handle)
    after = _dim_text_heights(backend, scaled.handle)
    assert before and after
    assert after == pytest.approx([h * 4.0 for h in before]), (
        f"DIMSCALE=4 must scale the dimension text: {before} -> {after}"
    )


async def test_the_default_dimscale_adds_no_override(backend):
    """DIMSCALE 1.0 is the default; it must not litter every dimension."""
    plain = await backend.dimension_linear(0, 0, 100, 0, 50, -30)

    dim = backend._doc.entitydb.get(plain.handle)
    assert not dim.dxf.hasattr("dimscale") or dim.dxf.dimscale == pytest.approx(1.0)


async def test_dimscale_reaches_aligned_and_angular_too(backend):
    """Those two do not go through `build_dim_override`, so they were missed.

    Asserted as a *ratio* against the same dimension drawn unscaled. This used
    to hardcode 3.0, which only held because the base text height was ezdxf's
    bare 1.0 mm — so the assertion silently depended on the very defect that
    made every headless drawing non-conformant, and moving the base to the ISO
    2.5 mm broke a test that was measuring the right thing by accident.
    """
    plain_aligned = await backend.dimension_aligned(0, 0, 60, 60, 40, 70)
    plain_angular = await backend.dimension_angular(0, 0, 50, 0, 0, 50, 30, 30)
    base_aligned = _dim_text_heights(backend, plain_aligned.handle)
    base_angular = _dim_text_heights(backend, plain_angular.handle)

    await backend.drawing_settings({"dimscale": 3.0})
    aligned = await backend.dimension_aligned(0, 0, 60, 60, 40, 90)
    angular = await backend.dimension_angular(0, 0, 50, 0, 0, 50, 50, 50)

    assert _dim_text_heights(backend, aligned.handle) == pytest.approx(
        [h * 3.0 for h in base_aligned]
    )
    assert _dim_text_heights(backend, angular.handle) == pytest.approx(
        [h * 3.0 for h in base_angular]
    )


# ── the rest of the dimension variables need friendly names too ─────────────


def _dim_text(backend, handle) -> str:
    dim = backend._doc.entitydb.get(handle)
    for entity in backend._doc.blocks.get(dim.dxf.geometry):
        if entity.dxftype() == "MTEXT":
            return entity.text
        if entity.dxftype() == "TEXT":
            return entity.dxf.text
    raise AssertionError("the dimension block carries no text")


async def test_the_dimension_variables_have_friendly_names(backend):
    """DIMTXT / DIMASZ / DIMDEC / DIMDSEP / DIMZIN had no friendly key at all.

    `text_size` is TEXTSIZE — the height of a standalone TEXT entity, not of a
    dimension — so the only way to reach dimension lettering was raw
    `system_set_variable`, which is exactly the path that silently did nothing
    headlessly. A drafter should not have to know a sysvar name to set the
    height of the numbers on their own drawing.
    """
    res = await backend.drawing_settings(
        {
            "dim_text_height": 5.0,
            "dim_arrow_size": 6.0,
            "dim_decimals": 1,
            "decimal_separator": ",",
            "zero_suppression": 0,
        }
    )
    assert res["ok"] is True, res.get("errors")

    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)
    assert _dim_text(backend, dim.handle) == "33,3"
    assert _dim_text_heights(backend, dim.handle) == pytest.approx([5.0])


async def test_the_decimal_separator_reads_back_as_a_character(backend):
    """DIMDSEP is an int char code in the header — ezdxf refuses a string
    there. The friendly key is the place to translate, so a snapshot says
    ',' rather than 44."""
    await backend.drawing_settings({"decimal_separator": ","})

    snap = (await backend.drawing_settings())["settings"]

    assert snap["decimal_separator"] == ","
    assert snap["dim_text_height"] == 2.5


async def test_a_bad_decimal_separator_is_reported_and_not_written(backend):
    before = await backend.system_get_variable("DIMDSEP")

    res = await backend.drawing_settings({"decimal_separator": "x"})

    assert "decimal_separator" in res["errors"]
    assert res["ok"] is False
    assert await backend.system_get_variable("DIMDSEP") == before
