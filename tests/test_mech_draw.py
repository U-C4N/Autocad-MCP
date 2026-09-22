"""The async layer: primitives to entities, the part payload, and the sheet.

Headless ezdxf against a real document - the `backend` fixture in
tests/conftest.py. The COM paths are the same code (draw.py composes the
generic contract members), which is the whole point of the pattern
`gear_draw_*` and `titleblock_apply_iso_a3` already use.
"""

from __future__ import annotations

import pytest

from engineering.mech.draw import (
    ANCHOR_LAYER,
    add_view,
    dimension_part,
    draw_from_spec,
    draw_hole_pattern,
    draw_part,
    draw_prims,
    read_part,
)
from engineering.mech.part import build_part
from engineering.mech.primitives import Arc, Circle, HatchArea, Line, Poly, Text

pytestmark = pytest.mark.asyncio

SHAFT = {
    "kind": "revolved",
    "name": "shaft",
    "material": "steel",
    "segments": [
        {"length": 20.0, "d_outer": 25.0},
        {"length": 40.0, "d_outer": 30.0},
        {"length": 15.0, "d_outer": 20.0},
    ],
    "features": [{"kind": "chamfer", "id": "c1", "at": "start", "size": 2.0}],
}

PLATE = {
    "kind": "prismatic",
    "name": "plate",
    "material": "aluminium",
    "thickness": 8.0,
    "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
    "features": [{"kind": "hole", "id": "h1", "x": 40.0, "y": 25.0, "diameter": 10.0}],
}


async def entities(backend, type_filter=None, layer=None):
    return await backend.entity_list(type_filter=type_filter, layer_filter=layer, limit=2000)


async def test_draw_prims_puts_every_role_on_its_own_layer(backend):
    result = await draw_prims(
        backend,
        [
            Line((0.0, 0.0), (10.0, 0.0), "visible"),
            Line((0.0, 5.0), (10.0, 5.0), "hidden"),
            Line((-2.0, 2.5), (12.0, 2.5), "center"),
            Circle((5.0, 2.5), 1.0, "visible"),
            Arc((5.0, 2.5), 2.0, 0.0, 90.0, "visible"),
            Poly(((0.0, 0.0), (10.0, 0.0), (10.0, 5.0)), True, "visible"),
            Text((5.0, 8.0), "A", 3.5, 0.0, "text"),
            HatchArea((((0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0)),), "steel"),
        ],
    )
    assert len(result["handles"]) == 8
    layers = {e.layer for e in await entities(backend)}
    assert {"GEOMETRY", "HIDDEN", "CENTER", "TEXT", "HATCH"} <= layers


async def test_draw_prims_places_and_rotates_into_wcs(backend):
    await draw_prims(backend, [Line((0.0, 0.0), (10.0, 0.0), "visible")], at=(100.0, 50.0))
    (line,) = await entities(backend, type_filter="LINE")
    start = line.properties["start"]
    assert (round(start[0], 6), round(start[1], 6)) == (100.0, 50.0)


async def test_draw_part_writes_the_model_back_onto_the_drawing(backend):
    part = build_part(SHAFT)
    result = await draw_part(backend, part, at=(50.0, 100.0))
    assert result["part_id"]
    assert result["views"][0]["kind"] == "front"
    assert result["handles"]

    anchors = await entities(backend, type_filter="POINT", layer=ANCHOR_LAYER)
    assert len(anchors) == 1
    assert anchors[0].handle == result["part_id"]

    read = await read_part(backend, result["part_id"])
    assert read["part"]["name"] == "shaft"
    assert [f["id"] for f in read["part"]["features"]] == ["c1"]
    assert build_part(read["part"]) == part


async def test_read_part_without_an_id_finds_the_only_part(backend):
    part = build_part(SHAFT)
    drawn = await draw_part(backend, part)
    read = await read_part(backend)
    assert read["part_id"] == drawn["part_id"]


async def test_read_part_refuses_when_there_is_more_than_one_and_no_id(backend):
    await draw_part(backend, build_part(SHAFT), at=(0.0, 0.0))
    await draw_part(backend, build_part(PLATE), at=(200.0, 0.0))
    with pytest.raises(ValueError) as excinfo:
        await read_part(backend)
    assert "part_id" in str(excinfo.value)


async def test_two_views_are_placed_in_first_angle_positions(backend):
    part = build_part(SHAFT)
    result = await draw_part(backend, part, at=(0.0, 0.0), views=("front", "side"))
    front, end = result["views"]
    assert end["kind"] == "side"
    # first angle: the view from the right sits to the LEFT of the front view
    assert end["bbox"][2] <= front["bbox"][0] + 1e-6


async def test_third_angle_puts_the_end_view_on_the_other_side(backend):
    part = build_part(SHAFT)
    result = await draw_part(
        backend, part, at=(0.0, 0.0), views=("front", "side"), projection="third"
    )
    front, end = result["views"]
    assert end["bbox"][0] >= front["bbox"][2] - 1e-6


async def test_add_view_reads_the_model_back_and_appends_a_section(backend):
    part = build_part(SHAFT)
    drawn = await draw_part(backend, part, at=(0.0, 0.0))
    added = await add_view(
        backend,
        drawn["part_id"],
        "section",
        plane={"p1": [0.0, 0.0], "p2": [100.0, 0.0], "label": "A"},
    )
    assert added["view"]["kind"] == "section"
    assert added["view"]["label"] == "A-A"
    assert len(await entities(backend, type_filter="HATCH")) == 2
    read = await read_part(backend, drawn["part_id"])
    assert [v["kind"] for v in read["views"]] == ["front", "section"]


async def test_a_detail_view_also_marks_its_circle_on_the_parent(backend):
    part = build_part(SHAFT)
    drawn = await draw_part(backend, part, at=(0.0, 0.0))
    added = await add_view(
        backend,
        drawn["part_id"],
        "detail",
        detail={"center": [20.0, 12.5], "radius": 8.0, "scale": 5.0, "label": "D"},
    )
    assert added["view"]["label"] == "D (5:1)"
    texts = [e for e in await entities(backend, type_filter="TEXT")]
    assert any(e.properties.get("text") == "D" for e in texts)


async def test_dimension_part_creates_real_dimension_entities(backend):
    part = build_part(SHAFT)
    drawn = await draw_part(backend, part, at=(0.0, 0.0), dimension=False)
    assert not await entities(backend, type_filter="DIMENSION")
    result = await dimension_part(backend, drawn["part_id"])
    assert result["count"] > 0
    dims = await entities(backend, type_filter="DIMENSION")
    assert len(dims) == result["count"]
    assert {e.layer for e in dims} == {"DIM"}


async def test_an_iso_286_fit_reaches_the_dimension_as_a_real_tolerance(backend):
    """`fits` names a dimension, and 0.025 is H7 on the 40 mm bore.

    The key is the one `engineering/mech/dimension.segment_key` mints, not the
    part's name: `dimension_intents` addresses the part's own diameters one at
    a time (`segment[0]`, `segment[0].bore`) precisely so a fit lands on the
    bore rather than on every dimension the part emits. Measured: H7 is
    +0.025/0 at 40 mm, +0.030/0 at 60 mm and +0.021/0 at the 30 mm length.
    """
    spec = {
        "kind": "revolved",
        "name": "bore",
        "segments": [{"length": 30.0, "d_outer": 60.0, "d_inner": 40.0}],
        "features": [],
    }
    part = build_part(spec)
    drawn = await draw_part(backend, part, at=(0.0, 0.0), dimension=False)
    result = await dimension_part(backend, drawn["part_id"], fits={"segment[0].bore": "H7"})
    assert result["count"] > 0
    assert any(
        row.get("fit") == "H7"
        and row["tol_upper"] == pytest.approx(0.025)
        and row["tol_lower"] == pytest.approx(0.0)
        and row["tol_mode"] == "deviation"
        and "H7" in row["text_override"]
        for row in result["resolved"]
    )
    # and the outside diameter of the same segment is untouched
    assert all(
        row["tol_mode"] == "none"
        for row in result["resolved"]
        if row["feature"] != "segment[0].bore"
    )


async def test_a_fit_that_names_nothing_is_refused_with_the_names_that_exist(backend):
    part = build_part(SHAFT)
    drawn = await draw_part(backend, part, at=(0.0, 0.0), views=("front", "side"), dimension=False)
    with pytest.raises(ValueError) as excinfo:
        await dimension_part(backend, drawn["part_id"], fits={"bearing_seat": "k6"})
    assert "bearing_seat" in str(excinfo.value)
    assert "segment[0]" in str(excinfo.value)


async def test_a_fit_on_one_view_is_not_refused_because_another_view_lacks_it(backend):
    """A part drawn front + side: `segment[0]` is dimensioned in the front view
    only, and the end view (which dimensions the *last* segment) must not turn
    a correct sheet into a refusal."""
    part = build_part(SHAFT)
    drawn = await draw_part(backend, part, at=(0.0, 0.0), views=("front", "side"), dimension=False)
    result = await dimension_part(backend, drawn["part_id"], fits={"segment[0]": "k6"})
    assert any(row.get("fit") == "k6" for row in result["resolved"])


async def test_a_hole_pattern_draws_its_holes_and_centre_marks(backend):
    result = await draw_hole_pattern(
        backend, pattern="polar", x=0.0, y=0.0, diameter=8.0, count=6, pcd=80.0
    )
    assert result["count"] == 6
    circles = await entities(backend, type_filter="CIRCLE")
    assert len(circles) == 6
    assert len(await entities(backend, layer="CENTER")) >= 12


async def test_a_hole_pattern_with_no_spacing_is_refused_before_anything_is_drawn(backend):
    with pytest.raises(ValueError, match="spacing"):
        await draw_hole_pattern(backend, pattern="linear", x=0.0, y=0.0, diameter=8.0, count=4)
    assert not await entities(backend, type_filter="CIRCLE")


async def test_draw_from_spec_draws_a_whole_sheet_in_one_transaction(backend):
    result = await draw_from_spec(
        backend,
        {
            "sheet": {"intent": "mechanical sheet", "sheet_size": "A3", "scale": 1.0},
            "parts": [
                {"part": SHAFT, "at": [40.0, 200.0], "views": ["front", "side"]},
                {"part": PLATE, "at": [40.0, 60.0], "views": ["front"]},
            ],
        },
    )
    assert len(result["parts"]) == 2
    assert len(await entities(backend, type_filter="POINT", layer=ANCHOR_LAYER)) == 2


async def test_draw_from_spec_validates_every_part_before_it_draws_anything(backend):
    bad = dict(SHAFT, segments=[{"length": 20.0, "d_outer": float("inf")}])
    with pytest.raises(ValueError) as excinfo:
        await draw_from_spec(
            backend,
            {"parts": [{"part": PLATE, "at": [0.0, 0.0]}, {"part": bad, "at": [200.0, 0.0]}]},
        )
    assert "parts[1]" in str(excinfo.value)
    assert "segments[0].d_outer" in str(excinfo.value)
    assert not await entities(backend, type_filter="POINT", layer=ANCHOR_LAYER)


async def test_draw_from_spec_rolls_back_when_a_draw_fails_midway(backend, monkeypatch):
    calls = {"n": 0}
    real = backend.entity_create_line

    async def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("the seat went away")
        return await real(*args, **kwargs)

    monkeypatch.setattr(backend, "entity_create_line", explode)
    with pytest.raises(RuntimeError, match="the seat went away"):
        await draw_from_spec(backend, {"parts": [{"part": SHAFT, "at": [0.0, 0.0]}]})
    monkeypatch.undo()
    assert not await entities(backend, type_filter="POINT", layer=ANCHOR_LAYER)
    assert not await entities(backend, type_filter="LINE")
