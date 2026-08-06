"""M6 — CHSPACE: moving geometry between model space and a sheet.

AutoCAD's CHSPACE moves objects *through* a viewport, scaling and translating
them by that viewport's zoom so they look identical on screen before and after.
ezdxf's `move_to_layout` does the move and applies **no geometric change at
all** — so the obvious implementation reports `ok: true`, produces a 100 mm
feature as 100 mm of paper inside a 1:2 viewport, and writes a file that audits
perfectly clean. That is the exact class of silent lie this release exists to
remove, so the transform is the tool, not a nicety.

The matrix is `viewport.get_transformation_matrix()`, not a hand-rolled
`translate(-view_center) @ scale @ translate(center)`. The hand-rolled version
ignores `view_target_point`, and with a target point set it misplaces geometry
by tens of millimetres while `is_top_view` is still True and the twist angle is
still zero — i.e. past every guard one would naturally write. The regression
test below pins the result against what ezdxf's own rendering pipeline projects.

What this refuses, and why each refusal is not pedantry:

* **Non-top views and twisted viewports** — ezdxf returns a meaningless
  top-view matrix for the first with no complaint, and rotates about the paper
  origin for the second.
* **Dimensions** — `transform` halves `get_measurement()` while the baked
  dimension-block text still reads the old value. Whichever the consumer
  trusts, the other is a lie, so a dimension moves only with
  `freeze_dimensions=True`, which bakes the pre-transform measurement into the
  text first.
* **ACIS bodies** — `3DSOLID`, `BODY`, `REGION` and `SURFACE` accept
  `transform()` and move nothing; the matrix is parked in a pending
  transformation the headless engine cannot apply.
* **Tables and proxies** — `ACAD_TABLE`, `ACAD_PROXY_ENTITY` and `OLE2FRAME`
  raise `NotImplementedError` from `transform()`, and `hasattr(e, "transform")`
  is `True` for all of them, so a `hasattr` dispatch does not filter them out.
* **Viewports** — ezdxf will happily move one into model space; AutoCAD's
  CHSPACE never does.
* **Entities already in the target space** — `move_to_layout` to the same
  layout silently succeeds, and a no-op must not read as a move.

Clipping is *flagged*, not refused: AutoCAD also lets you move geometry that
falls outside the viewport, so each moved entity reports `inside_viewport` and
`on_sheet` rather than being rejected.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

SHEET = "A3-Sheet"


async def _sheet_with_viewport(backend, *, scale=0.5, name=SHEET, **vp_dxf):
    """An A3 sheet with one 1:2 viewport centred on the page."""
    await backend.layout_create(name)
    layout = backend._doc.layouts.get(name)
    layout.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units="mm")
    created = await backend.viewport_create(name, 210, 148.5, 200, 150, 50, 25, scale=scale)
    viewport = backend._doc.entitydb.get(created["handle"])
    for key, value in vp_dxf.items():
        viewport.dxf.set(key, value)
    return created["handle"], viewport


def _layout_of(backend, handle: str) -> str:
    """The layout tab an entity lives on, by owning block record."""
    doc = backend._doc
    owner = doc.entitydb.get(handle).dxf.owner
    if owner == doc.modelspace().block_record.dxf.handle:
        return "Model"
    for name in doc.layouts.names():
        if name != "Model" and doc.layouts.get(name).block_record.dxf.handle == owner:
            return name
    return owner


# ── the transform is the point ──────────────────────────────────────────────


async def test_moving_to_paper_scales_the_geometry_by_the_viewport(backend):
    vp, _ = await _sheet_with_viewport(backend, scale=0.5)
    line = await backend.entity_create_line(50, 25, 150, 25)  # 100 mm through the view centre

    result = await backend.entity_change_space([line.handle], vp)

    assert result["ok"] is True
    assert result["moved"][0]["handle"] == line.handle
    moved = backend._doc.entitydb.get(line.handle)
    length = (moved.dxf.end - moved.dxf.start).magnitude
    assert length == pytest.approx(50.0), "a 100 mm model line is 50 mm of paper at 1:2"


async def test_the_moved_geometry_lands_where_the_renderer_projects_it(backend):
    """Regression test for the hand-rolled matrix that ignores view_target_point.

    With ``view_target_point`` set, the naive
    ``translate(-view_center) @ scale @ translate(center)`` misplaces the view
    centre by 25 mm while ``is_top_view`` is True and the twist is zero — so no
    guard catches it and only this comparison does.
    """
    from ezdxf.math import Vec3

    vp, viewport = await _sheet_with_viewport(backend, scale=0.5, view_target_point=(50, 25, 0))
    expected = viewport.get_transformation_matrix().transform(Vec3(60, 30, 0))
    line = await backend.entity_create_line(60, 30, 70, 30)

    await backend.entity_change_space([line.handle], vp)

    moved = backend._doc.entitydb.get(line.handle)
    assert moved.dxf.start.x == pytest.approx(expected.x)
    assert moved.dxf.start.y == pytest.approx(expected.y)


async def test_the_move_preserves_the_handle(backend):
    """Every tool in this server addresses entities by handle."""
    vp, _ = await _sheet_with_viewport(backend)
    line = await backend.entity_create_line(50, 25, 100, 25)

    await backend.entity_change_space([line.handle], vp)

    info = await backend.entity_get(line.handle)
    assert info.handle == line.handle
    assert _layout_of(backend, line.handle) == SHEET


async def test_a_round_trip_returns_the_original_coordinates(backend):
    vp, _ = await _sheet_with_viewport(backend, scale=0.5)
    line = await backend.entity_create_line(50, 25, 150, 25)

    await backend.entity_change_space([line.handle], vp, direction="to_paper")
    await backend.entity_change_space([line.handle], vp, direction="to_model")

    moved = backend._doc.entitydb.get(line.handle)
    assert moved.dxf.start.x == pytest.approx(50.0)
    assert moved.dxf.end.x == pytest.approx(150.0)
    assert _layout_of(backend, line.handle) == "Model"


async def test_the_reported_scale_matches_the_viewport(backend):
    vp, _ = await _sheet_with_viewport(backend, scale=0.25)
    line = await backend.entity_create_line(50, 25, 90, 25)

    result = await backend.entity_change_space([line.handle], vp)

    assert result["scale"] == pytest.approx(0.25)
    assert result["viewport"] == vp


# ── what it refuses ─────────────────────────────────────────────────────────


async def test_a_twisted_viewport_is_refused(backend):
    """ezdxf rotates about the paper origin; the alternative is unvalidatable."""
    vp, _ = await _sheet_with_viewport(backend, view_twist_angle=30.0)
    line = await backend.entity_create_line(50, 25, 100, 25)

    result = await backend.entity_change_space([line.handle], vp)

    assert result["ok"] is False
    assert "twist" in result["error"].lower()
    assert _layout_of(backend, line.handle) == "Model", "a refused move must not move anything"


async def test_a_non_top_view_viewport_is_refused(backend):
    """ezdxf hands back a meaningless top-view matrix with no complaint."""
    vp, _ = await _sheet_with_viewport(backend, view_direction_vector=(1, 1, 1))
    line = await backend.entity_create_line(50, 25, 100, 25)

    result = await backend.entity_change_space([line.handle], vp)

    assert result["ok"] is False
    assert _layout_of(backend, line.handle) == "Model"


async def test_a_viewport_handle_that_is_not_a_viewport_is_refused(backend):
    await _sheet_with_viewport(backend)
    line = await backend.entity_create_line(0, 0, 10, 0)
    result = await backend.entity_change_space([line.handle], line.handle)
    assert result["ok"] is False


async def test_an_unfrozen_dimension_is_refused(backend):
    """transform() halves the measurement while the block text keeps the old one."""
    vp, _ = await _sheet_with_viewport(backend)
    dim = await backend.dimension_linear(50, 25, 150, 25, 50, 40)

    result = await backend.entity_change_space([dim.handle], vp)

    assert result["ok"] is True
    assert result["moved"] == []
    reason = result["refused"][0]["reason"]
    assert "dimension" in reason.lower() and "freeze_dimensions" in reason


async def test_freezing_bakes_the_measurement_before_scaling(backend):
    vp, _ = await _sheet_with_viewport(backend, scale=0.5)
    dim = await backend.dimension_linear(50, 25, 150, 25, 50, 40)

    result = await backend.entity_change_space([dim.handle], vp, freeze_dimensions=True)

    assert result["ok"] is True
    row = result["moved"][0]
    assert row["frozen_text"] == "100"
    assert backend._doc.entitydb.get(dim.handle).dxf.text == "100"


@pytest.mark.parametrize("maker", ["add_body", "add_3dsolid", "add_region"])
async def test_acis_bodies_are_refused_because_transform_moves_nothing(backend, maker):
    vp, _ = await _sheet_with_viewport(backend)
    entity = getattr(backend._doc.modelspace(), maker)()

    result = await backend.entity_change_space([entity.dxf.handle], vp)

    assert result["moved"] == []
    assert entity.dxftype() in result["refused"][0]["reason"]


async def test_moving_a_viewport_itself_is_refused(backend):
    vp, _ = await _sheet_with_viewport(backend)
    other, _ = await _sheet_with_viewport(backend, name="Second")

    result = await backend.entity_change_space([other], vp)

    assert result["moved"] == []
    assert "VIEWPORT" in result["refused"][0]["reason"]


async def test_an_entity_already_in_the_target_space_is_refused(backend):
    """`move_to_layout` to the same layout silently succeeds."""
    vp, _ = await _sheet_with_viewport(backend)
    line = await backend.entity_create_line(50, 25, 100, 25)
    await backend.entity_change_space([line.handle], vp)

    result = await backend.entity_change_space([line.handle], vp)

    assert result["moved"] == []
    assert "already" in result["refused"][0]["reason"].lower()


async def test_an_entity_on_a_different_sheet_is_refused(backend):
    """`move_to_layout` raises a raw DXFValueError from deep inside ezdxf here.

    Worse, it raises *after* the transform has already been applied: the failed
    call left a line that had been scaled and translated by the viewport and
    then stayed exactly where it was, on the wrong sheet.
    """
    vp, _ = await _sheet_with_viewport(backend, scale=0.5)
    await backend.layout_create("Other-Sheet")
    await backend.layout_set_current("Other-Sheet")
    line = await backend.entity_create_line(0, 0, 10, 0)
    await backend.layout_set_current("Model")
    before = backend._doc.entitydb.get(line.handle).dxf.end

    result = await backend.entity_change_space([line.handle], vp, direction="to_model")

    assert result["moved"] == []
    reason = result["refused"][0]["reason"]
    assert "Other-Sheet" in reason
    assert backend._doc.entitydb.get(line.handle).dxf.end == before, (
        "a refused entity must not be left rescaled"
    )
    assert _layout_of(backend, line.handle) == "Other-Sheet"


async def test_one_bad_handle_does_not_rescale_the_good_ones_and_abort(backend):
    """A raw exception mid-loop would leave the batch half-applied."""
    vp, _ = await _sheet_with_viewport(backend, scale=0.5)
    await backend.layout_create("Other-Sheet")
    await backend.layout_set_current("Other-Sheet")
    stray = await backend.entity_create_line(0, 0, 10, 0)
    await backend.layout_set_current("Model")
    good = await backend.entity_create_line(50, 25, 150, 25)

    result = await backend.entity_change_space([stray.handle, good.handle], vp)

    assert [row["handle"] for row in result["moved"]] == [good.handle]
    assert result["refused"][0]["handle"] == stray.handle
    assert _layout_of(backend, good.handle) == SHEET


async def test_an_unknown_handle_is_reported_without_stopping_the_others(backend):
    vp, _ = await _sheet_with_viewport(backend)
    line = await backend.entity_create_line(50, 25, 100, 25)

    result = await backend.entity_change_space([line.handle, "DEADBEEF"], vp)

    assert [row["handle"] for row in result["moved"]] == [line.handle]
    assert result["refused"][0]["handle"] == "DEADBEEF"


async def test_an_unknown_direction_is_refused(backend):
    vp, _ = await _sheet_with_viewport(backend)
    line = await backend.entity_create_line(50, 25, 100, 25)
    result = await backend.entity_change_space([line.handle], vp, direction="sideways")
    assert result["ok"] is False


async def test_no_handles_is_refused_rather_than_reported_as_success(backend):
    vp, _ = await _sheet_with_viewport(backend)
    result = await backend.entity_change_space([], vp)
    assert result["ok"] is False


# ── what it flags rather than refuses ───────────────────────────────────────


async def test_geometry_outside_the_viewport_is_flagged_not_refused(backend):
    """AutoCAD moves it too; the caller just has to be told it left the sheet."""
    vp, _ = await _sheet_with_viewport(backend, scale=0.5)
    inside = await backend.entity_create_line(50, 25, 60, 25)
    outside = await backend.entity_create_line(800, 25, 900, 25)

    result = await backend.entity_change_space([inside.handle, outside.handle], vp)

    rows = {row["handle"]: row for row in result["moved"]}
    assert rows[inside.handle]["inside_viewport"] is True
    assert rows[inside.handle]["on_sheet"] is True
    assert rows[outside.handle]["inside_viewport"] is False
    assert rows[outside.handle]["on_sheet"] is False
