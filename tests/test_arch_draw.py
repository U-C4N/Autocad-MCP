"""The async layer: walls, openings, stairs and chains drawn through the ezdxf backend.

The same code runs on the live engine: `engineering/arch/draw.py` composes only
generic contract members (entity_create_*, dimension_linear, entity_set_xdata,
entity_list, entity_delete), like the mechanical track's draw layer.
"""

from __future__ import annotations

import pytest

from engineering.arch.draw import (
    HANDLES_APP,
    add_opening,
    add_stair,
    add_walls,
    assign_tags,
    draw_chains,
    draw_stair,
    draw_walls,
    read_plan,
)
from engineering.arch.layers import ARCH_ROLE_LAYER
from engineering.arch.model import APP_ID, Opening, Stair, Wall

pytestmark = pytest.mark.asyncio

WALL_A = {"id": "a", "axis": [[0.0, 0.0], [5000.0, 0.0]], "thickness": 200.0}
WALL_B = {"id": "b", "axis": [[2500.0, 0.0], [2500.0, 3000.0]], "thickness": 100.0}


async def on(backend, role, type_filter=None):
    return await backend.entity_list(
        type_filter=type_filter, layer_filter=ARCH_ROLE_LAYER[role], limit=10000
    )


def ends(info) -> tuple:
    a = tuple(round(v, 6) + 0.0 for v in info.properties["start"][:2])
    b = tuple(round(v, 6) + 0.0 for v in info.properties["end"][:2])
    return tuple(sorted((a, b)))


async def test_draw_walls_puts_faces_jambs_and_poche_on_their_layers(backend):
    wall = Wall(id="a", axis=((0.0, 0.0), (5000.0, 0.0)), thickness=200.0)
    door = Opening(id="d1", wall="a", kind="door", offset=1000.0, width=900.0, tag="D1")
    result = await draw_walls(backend, [wall], [door])
    lines = await on(backend, "wall", "LINE")
    assert {ends(e) for e in lines} == {
        ((0.0, 100.0), (1000.0, 100.0)),
        ((1900.0, 100.0), (5000.0, 100.0)),
        ((0.0, -100.0), (1000.0, -100.0)),
        ((1900.0, -100.0), (5000.0, -100.0)),
        ((0.0, -100.0), (0.0, 100.0)),
        ((5000.0, -100.0), (5000.0, 100.0)),
        ((1000.0, -100.0), (1000.0, 100.0)),
        ((1900.0, -100.0), (1900.0, 100.0)),
    }
    assert len(await on(backend, "wall_hatch", "HATCH")) == 2
    assert len(await on(backend, "door", "LINE")) == 1
    assert len(await on(backend, "door", "ARC")) == 1
    assert [w["id"] for w in result["walls"]] == ["a"]
    assert [o["id"] for o in result["openings"]] == ["d1"]
    assert result["omitted"] == []


async def test_the_records_round_trip_through_xdata(backend):
    wall = Wall(id="a", axis=((0.0, 0.0), (5000.0, 0.0)), thickness=200.0, material="aac")
    window = Opening(id="w1", wall="a", kind="window", offset=2000.0, width=1200.0, tag="W1")
    drawn = await draw_walls(backend, [wall], [window])
    plan = await read_plan(backend)
    assert plan["walls"] == [wall]
    assert plan["openings"] == [window]
    assert plan["carriers"]["wall:a"] == drawn["walls"][0]["record"]
    carrier = drawn["openings"][0]["record"]
    assert plan["carriers"]["opening:w1"] == carrier
    # the window's record sits on its left-face frame line, with its four lines owned
    raw = await backend.entity_get_xdata(carrier, HANDLES_APP)
    assert len(raw["xdata"][HANDLES_APP]) == 4
    assert (await backend.entity_get_xdata(carrier, APP_ID))["xdata"][APP_ID]


async def test_add_walls_redraws_only_the_wall_the_new_one_joins(backend):
    await add_walls(
        backend,
        [WALL_A, {"id": "c", "axis": [[0.0, 2000.0], [5000.0, 2000.0]], "thickness": 200.0}],
    )
    result = await add_walls(
        backend, [{"id": "b", "axis": [[2500.0, 0.0], [2500.0, 1000.0]], "thickness": 100.0}]
    )
    assert result["added"] == ["b"]
    assert result["redrawn"] == ["a"]  # c is not touched by b
    faces = {ends(e) for e in await on(backend, "wall", "LINE")}
    # a's near face is now cut where b meets it; its far face is still one line
    assert ((0.0, 100.0), (2450.0, 100.0)) in faces
    assert ((2550.0, 100.0), (5000.0, 100.0)) in faces
    assert ((0.0, -100.0), (5000.0, -100.0)) in faces
    assert ((0.0, 100.0), (5000.0, 100.0)) not in faces  # the old, uncut face was erased
    plan = await read_plan(backend)
    assert sorted(w.id for w in plan["walls"]) == ["a", "b", "c"]


async def test_an_opening_added_later_cuts_its_wall(backend):
    await add_walls(backend, [WALL_A])
    before = {ends(e) for e in await on(backend, "wall", "LINE")}
    assert ((0.0, 100.0), (5000.0, 100.0)) in before
    result = await add_opening(
        backend, {"wall": "a", "kind": "door", "offset": 1000.0, "width": 900.0}
    )
    assert result["opening"] == "D1" and result["tag"] == "D1"
    assert result["redrawn"] == ["a"]
    after = {ends(e) for e in await on(backend, "wall", "LINE")}
    assert ((0.0, 100.0), (5000.0, 100.0)) not in after
    assert {((0.0, 100.0), (1000.0, 100.0)), ((1900.0, 100.0), (5000.0, 100.0))} <= after
    assert len(await on(backend, "door", "ARC")) == 1
    second = await add_opening(
        backend, {"wall": "a", "kind": "door", "offset": 3000.0, "width": 800.0}
    )
    assert second["tag"] == "D2"
    assert len(await on(backend, "door", "ARC")) == 2  # the first door was redrawn, not lost
    plan = await read_plan(backend)
    assert sorted(op.tag for op in plan["openings"]) == ["D1", "D2"]


async def test_an_opening_in_an_unknown_wall_or_overlapping_is_refused_untouched(backend):
    await add_walls(backend, [WALL_A])
    count = len(await backend.entity_list(limit=10000))
    with pytest.raises(ValueError, match="'zz' is not a wall"):
        await add_opening(backend, {"wall": "zz", "kind": "door", "offset": 0.0, "width": 900.0})
    await add_opening(backend, {"wall": "a", "kind": "window", "offset": 1000.0, "width": 1200.0})
    count = len(await backend.entity_list(limit=10000))
    with pytest.raises(ValueError):
        await add_opening(backend, {"wall": "a", "kind": "door", "offset": 1500.0, "width": 900.0})
    assert len(await backend.entity_list(limit=10000)) == count


async def test_a_wall_id_already_drawn_is_refused(backend):
    await add_walls(backend, [WALL_A])
    with pytest.raises(ValueError, match=r"walls\[0\]\.id: 'a' is already drawn"):
        await add_walls(backend, [WALL_A])


async def test_a_new_wall_that_would_swallow_an_opening_is_refused(backend):
    await add_walls(backend, [WALL_A])
    await add_opening(backend, {"wall": "a", "kind": "door", "offset": 2200.0, "width": 900.0})
    count = len(await backend.entity_list(limit=10000))
    with pytest.raises(ValueError, match="uncuttable"):
        await add_walls(backend, [WALL_B])
    assert len(await backend.entity_list(limit=10000)) == count


async def test_draw_stair_writes_its_record_and_reports_blondel(backend):
    stair = Stair(
        id="s1",
        start=(0.0, 0.0),
        direction_deg=0.0,
        width=1000.0,
        risers=17,
        riser_height=170.0,
        going=290.0,
    )
    result = await draw_stair(backend, stair)
    assert result["blondel"] == {"value": 630.0, "ok": True, "range": [600.0, 650.0]}
    assert result["treads"] == 16
    assert len(await on(backend, "stair", "LINE")) == 17 + 2 + 2
    assert (await read_plan(backend))["stairs"] == [stair]
    with pytest.raises(ValueError, match="'s1' is already drawn"):
        await add_stair(
            backend,
            {
                "id": "s1",
                "start": [0, 0],
                "direction_deg": 0.0,  # required by Task 1's stair_from_dict
                "width": 1000.0,
                "risers": 17,
                "riser_height": 170.0,
                "going": 290.0,
            },
        )


async def test_draw_chains_puts_real_dimensions_on_the_dimension_layer(backend):
    shell = Wall(
        id="shell",
        axis=((0.0, 0.0), (8200.0, 0.0), (8200.0, 6200.0), (0.0, 6200.0)),
        thickness=200.0,
        closed=True,
    )
    result = await draw_chains(backend, [shell], [], sides=("bottom", "left"), scale=50)
    dims = await on(backend, "dim", "DIMENSION")
    assert len(dims) == result["count"] == 8  # per side: walls (3) + overall (1)
    assert {row["row"] for row in result["dimensions"]} == {
        "bottom:walls",
        "bottom:overall",
        "left:walls",
        "left:overall",
    }


async def test_assign_tags_numbers_each_kind_in_the_chosen_language():
    ops = [
        Opening(id="1", wall="a", kind="door", offset=0.0, width=900.0),
        Opening(id="2", wall="a", kind="window", offset=2000.0, width=900.0),
        Opening(id="3", wall="a", kind="door", offset=4000.0, width=900.0, tag="D1"),
    ]
    assert [op.tag for op in assign_tags(ops)] == ["D2", "W1", "D1"]
    assert [op.tag for op in assign_tags(ops[:2], lang="tr")] == ["K1", "P1"]
