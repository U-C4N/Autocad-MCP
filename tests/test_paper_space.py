"""M5 — drawing into the layout you selected.

`layout_set_current("A3-Sheet")` answered `{"ok": true, "current": "A3-Sheet"}`
and then every entity created afterwards went into **model space** anyway,
because `_msp()` returned `doc.modelspace()` unconditionally on both engines.
The tool reported a state change that changed nothing about where geometry
landed — proven: a line drawn straight after selecting the layout was in
modelspace and not in the layout.

That is also why the title block could not go on a sheet: it draws through the
same `entity_create_*` calls, so `apply_iso_a3_titleblock` could only ever
produce a border around model-space geometry.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

SHEET = "A3-Sheet"


async def _handles_in(backend, layout_name: str) -> set[str]:
    doc = backend._doc
    space = doc.modelspace() if layout_name == "Model" else doc.layouts.get(layout_name)
    return {e.dxf.handle for e in space}


# ── the current space is where geometry goes ────────────────────────────────


async def test_entities_land_in_the_layout_that_was_selected(backend):
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)

    line = await backend.entity_create_line(0, 0, 100, 0)

    assert line.handle in await _handles_in(backend, SHEET)
    assert line.handle not in await _handles_in(backend, "Model")


async def test_switching_back_to_model_puts_geometry_back_in_model_space(backend):
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    on_sheet = await backend.entity_create_circle(10, 10, 5)
    await backend.layout_set_current("Model")

    in_model = await backend.entity_create_circle(20, 20, 5)

    assert on_sheet.handle in await _handles_in(backend, SHEET)
    assert in_model.handle in await _handles_in(backend, "Model")


async def test_a_new_drawing_starts_in_model_space(backend):
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    await backend.drawing_new()

    line = await backend.entity_create_line(0, 0, 10, 0)
    assert line.handle in await _handles_in(backend, "Model")


async def test_selecting_a_missing_layout_does_not_move_the_current_space(backend):
    """A refused switch must leave the caller where they were, not in limbo."""
    result = await backend.layout_set_current("NOSUCH")
    assert result["ok"] is False

    line = await backend.entity_create_line(0, 0, 10, 0)
    assert line.handle in await _handles_in(backend, "Model")


async def test_entity_get_finds_an_entity_drawn_on_a_layout(backend):
    """Lookups must follow the geometry, or the handle becomes unusable."""
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    line = await backend.entity_create_line(0, 0, 100, 0)

    info = await backend.entity_get(line.handle)
    assert info.handle == line.handle
    assert info.type == "LINE"


# ── the title block, on a sheet ─────────────────────────────────────────────


async def test_titleblock_can_be_drawn_on_a_paper_space_layout(backend):
    from engineering import TitleBlockMetadata, apply_iso_a3_titleblock

    await backend.layout_create(SHEET)
    before_model = await _handles_in(backend, "Model")

    result = await apply_iso_a3_titleblock(
        backend,
        metadata=TitleBlockMetadata(title="BRACKET", drawing_no="D-001"),
        layout=SHEET,
    )

    assert result["ok"] is True
    assert result["layout"] == SHEET
    on_sheet = await _handles_in(backend, SHEET)
    assert len(on_sheet) > 5, "border, inner frame and the block's text all belong here"
    assert await _handles_in(backend, "Model") == before_model, "model space untouched"


async def test_titleblock_restores_the_space_the_caller_was_in(backend):
    from engineering import TitleBlockMetadata, apply_iso_a3_titleblock

    await backend.layout_create(SHEET)
    await apply_iso_a3_titleblock(
        backend,
        metadata=TitleBlockMetadata(title="X", drawing_no="1"),
        layout=SHEET,
    )

    line = await backend.entity_create_line(0, 0, 10, 0)
    assert line.handle in await _handles_in(backend, "Model"), (
        "drawing a sheet border must not silently move the caller onto the sheet"
    )


async def test_titleblock_without_a_layout_still_draws_in_model_space(backend):
    """The pre-1.5.0 behaviour stays the default."""
    from engineering import TitleBlockMetadata, apply_iso_a3_titleblock

    result = await apply_iso_a3_titleblock(
        backend, metadata=TitleBlockMetadata(title="X", drawing_no="1")
    )
    assert result["ok"] is True
    assert result["layout"] == "Model"
    assert len(await _handles_in(backend, "Model")) > 5


async def test_titleblock_on_a_missing_layout_refuses(backend):
    from engineering import TitleBlockMetadata, apply_iso_a3_titleblock

    with pytest.raises(Exception, match="NOSUCH"):
        await apply_iso_a3_titleblock(
            backend,
            metadata=TitleBlockMetadata(title="X", drawing_no="1"),
            layout="NOSUCH",
        )
