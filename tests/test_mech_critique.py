"""Six mechanical focuses, each with a positive and a negative fixture.

All six are silent on a drawing with no ACADMCP_MECH payload on it, so
`focus=None` stays cheap for a P&ID sheet or a plain DXF.

A fixture writes the payload onto a small CIRCLE on layer 0 rather than onto a
block reference. The index reads INSERT and CIRCLE alike (a balloon is a circle
with a leader), and a circle keeps the fixtures free of block definitions —
what is under test here is the reading, not the drawing.
"""

from __future__ import annotations

import pytest

from engineering.layers import ensure_engineering_layers
from engineering.mech.critique import (
    MECH_FOCUSES,
    build_index,
    has_mech_content,
    issues_for,
)
from engineering.mech.xdata import APP_ID, to_values
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES

pytestmark = pytest.mark.asyncio

SHAFT = {
    "name": "shaft",
    "material": "steel",
    "segments": [{"length": 60.0, "d_outer": 40.0, "d_inner": 20.0}],
    "features": [],
}


async def _anchor(backend, payload, x=0.0, y=0.0):
    ent = await backend.entity_create_circle(x, y, 0.5, layer="0")
    await backend.entity_set_xdata(ent.handle, APP_ID, to_values(payload))
    return ent.handle


def _focus(index, name):
    return issues_for(name, index)


async def test_the_six_focuses_are_the_spec_ones_and_are_in_the_closed_enum():
    assert MECH_FOCUSES == (
        "mech_missing_centreline",
        "mech_unhatched_section",
        "mech_view_misaligned",
        "mech_duplicate_dimension",
        "mech_thread_unrepresented",
        "mech_bom_balloon_mismatch",
    )
    for focus in MECH_FOCUSES:
        assert focus in ALL_CRITIQUE_FOCUSES


async def test_every_focus_is_silent_on_a_drawing_with_no_mechanical_content(backend):
    """A circle with no centre mark, two identical dimensions and no hatch —
    every trigger present, and not one issue, because nothing on the sheet
    claims to be a mechanical part."""
    await ensure_engineering_layers(backend)
    await backend.entity_create_circle(0, 0, 10, layer="GEOMETRY")
    await backend.dimension_linear(0, 0, 100, 0, 50, 20, layer="DIM")
    await backend.dimension_linear(0, 0, 100, 0, 50, 20, layer="DIM")
    index = await build_index(backend)
    assert has_mech_content(index) is False
    for focus in MECH_FOCUSES:
        assert _focus(index, focus) == []


async def test_missing_centreline_fires_on_a_bare_circle(backend):
    await ensure_engineering_layers(backend)
    await _anchor(backend, {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": SHAFT})
    await backend.entity_create_circle(100.0, 100.0, 10.0, layer="GEOMETRY")
    issues = _focus(await build_index(backend), "mech_missing_centreline")
    assert len(issues) == 1
    assert issues[0].focus == "mech_missing_centreline"
    assert "centre mark" in issues[0].message


async def test_missing_centreline_is_quiet_once_the_cross_is_drawn(backend):
    await ensure_engineering_layers(backend)
    await _anchor(backend, {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": SHAFT})
    await backend.entity_create_circle(100.0, 100.0, 10.0, layer="GEOMETRY")
    await backend.entity_create_line(87.0, 100.0, 113.0, 100.0, layer="CENTER")
    await backend.entity_create_line(100.0, 87.0, 100.0, 113.0, layer="CENTER")
    assert _focus(await build_index(backend), "mech_missing_centreline") == []


async def test_unhatched_section_fires_on_a_section_view_with_no_hatch(backend):
    await ensure_engineering_layers(backend)
    await _anchor(
        backend,
        {"v": 1, "kind": "part", "id": "P1", "view": "section", "part": SHAFT},
        x=0.0,
        y=0.0,
    )
    issues = _focus(await build_index(backend), "mech_unhatched_section")
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "hatched cut face" in issues[0].message


async def test_unhatched_section_is_quiet_when_the_cut_face_is_hatched(backend):
    await ensure_engineering_layers(backend)
    await _anchor(
        backend,
        {"v": 1, "kind": "part", "id": "P1", "view": "section", "part": SHAFT},
        x=0.0,
        y=0.0,
    )
    await backend.entity_create_hatch(
        "ANSI31", [[10.0, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0]], layer="HATCH"
    )
    assert _focus(await build_index(backend), "mech_unhatched_section") == []


async def test_view_misaligned_fires_when_a_side_view_leaves_the_projection_axis(backend):
    await ensure_engineering_layers(backend)
    await _anchor(
        backend, {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": SHAFT}, 0.0, 0.0
    )
    await _anchor(
        backend, {"v": 1, "kind": "part", "id": "P1", "view": "side", "part": SHAFT}, 150.0, 5.0
    )
    issues = _focus(await build_index(backend), "mech_view_misaligned")
    assert len(issues) == 1
    assert issues[0].detail["axis"] == "y"
    assert issues[0].detail["delta_mm"] == 5.0


async def test_view_misaligned_is_quiet_on_an_aligned_pair(backend):
    await ensure_engineering_layers(backend)
    await _anchor(
        backend, {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": SHAFT}, 0.0, 0.0
    )
    await _anchor(
        backend, {"v": 1, "kind": "part", "id": "P1", "view": "side", "part": SHAFT}, 150.0, 0.0
    )
    assert _focus(await build_index(backend), "mech_view_misaligned") == []


async def test_duplicate_dimension_fires_on_the_same_dimension_twice(backend):
    await ensure_engineering_layers(backend)
    await _anchor(backend, {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": SHAFT})
    await backend.dimension_linear(0, 0, 60, 0, 30, -20, layer="DIM")
    await backend.dimension_linear(0, 0, 60, 0, 30, -20, layer="DIM")
    issues = _focus(await build_index(backend), "mech_duplicate_dimension")
    assert len(issues) == 1
    assert len(issues[0].handles) == 2


async def test_duplicate_dimension_is_quiet_on_two_different_dimensions(backend):
    await ensure_engineering_layers(backend)
    await _anchor(backend, {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": SHAFT})
    await backend.dimension_linear(0, 0, 60, 0, 30, -20, layer="DIM")
    await backend.dimension_linear(0, 0, 40, 0, 20, -35, layer="DIM")
    assert _focus(await build_index(backend), "mech_duplicate_dimension") == []


THREADED = {
    "name": "stud",
    "material": "steel",
    "segments": [{"length": 60.0, "d_outer": 20.0}],
    "features": [{"kind": "thread", "id": "t1", "designation": "M20x1.5", "segment": 0}],
}


async def test_thread_unrepresented_fires_when_only_the_major_diameter_is_drawn(backend):
    await ensure_engineering_layers(backend)
    await _anchor(backend, {"v": 1, "kind": "part", "id": "P1", "view": "side", "part": THREADED})
    await backend.entity_create_circle(0.0, 0.0, 10.0, layer="GEOMETRY")
    issues = _focus(await build_index(backend), "mech_thread_unrepresented")
    assert len(issues) == 1
    assert issues[0].detail["minor_mm"] == 16.0
    assert "ISO 6410" in issues[0].message


async def test_thread_unrepresented_is_quiet_once_the_minor_circle_is_drawn(backend):
    await ensure_engineering_layers(backend)
    await _anchor(backend, {"v": 1, "kind": "part", "id": "P1", "view": "side", "part": THREADED})
    await backend.entity_create_circle(0.0, 0.0, 10.0, layer="GEOMETRY")
    await backend.entity_create_arc(0.0, 0.0, 8.0, 0.0, 270.0, layer="GEOMETRY")
    assert _focus(await build_index(backend), "mech_thread_unrepresented") == []


BOLT = {
    "v": 1,
    "kind": "std_part",
    "designation": "ISO 4014 - M12x60",
    "standard": "ISO 4014",
    "size": "M12x60",
    "qty": 1,
    "material": "8.8",
}


async def test_bom_balloon_mismatch_fires_on_a_balloon_pointing_at_nothing(backend):
    await ensure_engineering_layers(backend)
    await _anchor(backend, BOLT, 0.0, 0.0)
    await _anchor(backend, {"v": 1, "kind": "balloon", "item": 1, "targets": ["DEAD"]}, 40.0, 40.0)
    issues = _focus(await build_index(backend), "mech_bom_balloon_mismatch")
    assert any("not a part on this sheet" in issue.message for issue in issues)


async def test_bom_balloon_mismatch_fires_on_a_part_with_no_balloon(backend):
    await ensure_engineering_layers(backend)
    bolt = await _anchor(backend, BOLT, 0.0, 0.0)
    await _anchor(
        backend, {**BOLT, "size": "M16x60", "designation": "ISO 4014 - M16x60"}, 60.0, 0.0
    )
    await _anchor(backend, {"v": 1, "kind": "balloon", "item": 1, "targets": [bolt]}, 40.0, 40.0)
    issues = _focus(await build_index(backend), "mech_bom_balloon_mismatch")
    assert any("has no balloon" in issue.message for issue in issues)


async def test_bom_balloon_mismatch_fires_when_the_declared_quantity_disagrees(backend):
    await ensure_engineering_layers(backend)
    first = await _anchor(backend, {**BOLT, "qty": 4}, 0.0, 0.0)
    await _anchor(backend, {"v": 1, "kind": "balloon", "item": 1, "targets": [first]}, 40.0, 40.0)
    issues = _focus(await build_index(backend), "mech_bom_balloon_mismatch")
    assert any("declares 4" in issue.message for issue in issues)


async def test_bom_balloon_mismatch_is_quiet_on_a_matched_sheet(backend):
    await ensure_engineering_layers(backend)
    bolt = await _anchor(backend, BOLT, 0.0, 0.0)
    await _anchor(backend, {"v": 1, "kind": "balloon", "item": 1, "targets": [bolt]}, 40.0, 40.0)
    assert _focus(await build_index(backend), "mech_bom_balloon_mismatch") == []


async def test_the_dispatch_entries_share_one_index_per_run(backend):
    """Six focuses must cost one read of the drawing, like the P&ID ones."""
    from engineering.mech.critique import MECH_DISPATCH

    await ensure_engineering_layers(backend)
    await _anchor(backend, {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": SHAFT})
    shared: dict = {}
    for focus in MECH_FOCUSES:
        check = MECH_DISPATCH[focus]
        assert check.needs_shared is True
        await check(backend, shared)
    assert list(shared) == ["mech_index"]


# ── the payloads the real writers put on the drawing ────────────────────────
#
# The fixtures above anchor a hand-written payload on a CIRCLE. The tools do
# not: mech_part_draw anchors its part payload on a POINT (layer MECH) and
# lists every view it drew under "views", std_part_insert writes onto the
# INSERT, balloon_add onto the balloon's circle. An index that read only the
# fixtures' shape would be silent on every drawing the server actually makes.

REAL_SHAFT = {
    "kind": "revolved",
    "name": "stud",
    "material": "steel",
    "segments": [
        {"length": 30.0, "d_outer": 20.0},
        {"length": 50.0, "d_outer": 30.0, "d_inner": 10.0},
    ],
    "features": [
        {"kind": "thread", "id": "t1", "designation": "M20x1.5", "segment": 0, "length": 20.0}
    ],
}
CROSS_SECTION = {"p1": [50.0, -30.0], "p2": [50.0, 30.0]}


async def _real_part(backend):
    from engineering.mech.draw import add_view, draw_part
    from engineering.mech.part import build_part

    drawn = await draw_part(backend, build_part(REAL_SHAFT), views=("front", "side"))
    await add_view(backend, drawn["part_id"], "section", plane=CROSS_SECTION)
    return drawn["part_id"]


async def test_a_part_drawn_by_the_tools_is_read_and_passes_every_focus(backend):
    part_id = await _real_part(backend)
    index = await build_index(backend)
    assert has_mech_content(index) is True
    assert [r["handle"] for r in index["records"]] == [part_id]
    assert [v["kind"] for v in index["views"]] == ["front", "side", "section"]
    for focus in MECH_FOCUSES:
        assert _focus(index, focus) == [], focus


async def test_a_real_section_whose_hatch_was_erased_is_reported(backend):
    part_id = await _real_part(backend)
    for hatch in await backend.entity_list(type_filter="HATCH"):
        await backend.entity_delete(hatch.handle)
    issues = _focus(await build_index(backend), "mech_unhatched_section")
    assert len(issues) == 1
    assert issues[0].handles == [part_id]
    assert issues[0].detail["view"] == 2


async def test_identical_parts_share_one_balloon(backend):
    """ISO 6433 balloons a parts-list row: four identical bolts, one item."""
    await ensure_engineering_layers(backend)
    first = await _anchor(backend, BOLT, 0.0, 0.0)
    await _anchor(backend, BOLT, 60.0, 0.0)
    await _anchor(backend, {"v": 1, "kind": "balloon", "item": 1, "targets": [first]}, 40.0, 40.0)
    assert _focus(await build_index(backend), "mech_bom_balloon_mismatch") == []


async def test_the_index_reads_past_the_first_page_of_entities(backend):
    """entity_list defaults to 200; a payload on entity 251 must still count."""
    await ensure_engineering_layers(backend)
    for i in range(250):
        await backend.entity_create_line(float(i), 0.0, float(i), 5.0, layer="GEOMETRY")
    await _anchor(backend, BOLT, 0.0, 50.0)
    assert has_mech_content(await build_index(backend)) is True


class _LiveTypeNames:
    """The ezdxf backend reporting entity types the way the live engine does.

    ComBackend names a type from its ActiveX ObjectName with ``AcDb`` stripped
    and upper-cased; measured on AutoCAD 2026 (2026-09-23), a block reference
    reads back as BLOCKREFERENCE and a linear dimension as ROTATEDDIMENSION.
    Only those two renames are applied -- POINT, CIRCLE, LINE and HATCH are
    already the same on both engines.
    """

    _RENAME = {"INSERT": "BLOCKREFERENCE", "DIMENSION": "ROTATEDDIMENSION"}

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def entity_list(self, *args, **kwargs):
        import dataclasses

        return [
            dataclasses.replace(ent, type=self._RENAME.get(ent.type, ent.type))
            for ent in await self._inner.entity_list(*args, **kwargs)
        ]


async def test_the_live_engine_type_names_are_read(backend):
    """A standard part on an INSERT and two stacked dimensions, seen through
    the live engine's type names: the payload is still read and the duplicate
    still found."""
    await ensure_engineering_layers(backend)
    ref = await backend.entity_create_circle(0.0, 0.0, 1.0, layer="0")
    await backend.block_create_from_entities("T15_BOLT", [ref.handle], 0.0, 0.0)
    bolt = await backend.block_insert("T15_BOLT", 0.0, 0.0, 1.0, 1.0, 0.0, None, "GEOMETRY")
    await backend.entity_set_xdata(bolt.handle, APP_ID, to_values(BOLT))
    await backend.dimension_linear(0, 0, 60, 0, 30, -20, layer="DIM")
    await backend.dimension_linear(0, 0, 60, 0, 30, -20, layer="DIM")

    index = await build_index(_LiveTypeNames(backend))
    assert [r["handle"] for r in index["records"]] == [bolt.handle]
    assert {e.type for e in index["entities"]} >= {"BLOCKREFERENCE", "ROTATEDDIMENSION"}
    duplicates = _focus(index, "mech_duplicate_dimension")
    assert len(duplicates) == 1 and len(duplicates[0].handles) == 2
