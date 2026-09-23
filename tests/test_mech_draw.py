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


# -- review round: rotation, single dimensioning, honest refusals -------------

TWO_STEP = {
    "kind": "revolved",
    "name": "shaft",
    "material": "steel",
    "segments": [{"length": 20.0, "d_outer": 25.0}, {"length": 40.0, "d_outer": 30.0}],
    "features": [],
}

TUBE = {
    "kind": "revolved",
    "name": "tube",
    "material": "steel",
    "segments": [{"length": 30.0, "d_outer": 60.0, "d_inner": 40.0}],
    "features": [],
}


def _geometry_bbox(backend):
    xs: list[float] = []
    ys: list[float] = []
    for e in backend._doc.modelspace():
        if e.dxftype() == "LINE":
            xs += [e.dxf.start.x, e.dxf.end.x]
            ys += [e.dxf.start.y, e.dxf.end.y]
    return [min(xs), min(ys), max(xs), max(ys)]


def _defpoints(backend):
    return sorted(
        (round(e.dxf.get(name).x, 6), round(e.dxf.get(name).y, 6))
        for e in backend._doc.modelspace().query("DIMENSION")
        for name in ("defpoint2", "defpoint3")
        if e.dxf.hasattr(name)
    )


def _turn90(point, about):
    return (round(about[0] - (point[1] - about[1]), 6), round(about[1] + (point[0] - about[0]), 6))


async def test_rotation_turns_geometry_record_and_dimensions_about_at(backend):
    """rotation=90 about at=(100, 100): the part, its recorded box and every
    DIMENSION defpoint are the rotation=0 drawing turned about `at`."""
    from backends.ezdxf_backend import EzdxfBackend

    flat = EzdxfBackend()
    await flat.connect()
    await flat.drawing_new()
    try:
        await draw_part(flat, build_part(TWO_STEP), at=(100.0, 100.0), rotation=0.0)
        expected = sorted(_turn90(p, (100.0, 100.0)) for p in _defpoints(flat))
    finally:
        await flat.disconnect()

    result = await draw_part(backend, build_part(TWO_STEP), at=(100.0, 100.0), rotation=90.0)
    geometry = _geometry_bbox(backend)
    assert geometry == pytest.approx([70.0, 100.0, 100.0, 172.0])
    assert result["views"][0]["bbox"] == pytest.approx(geometry)
    read = await read_part(backend, result["part_id"])
    assert read["views"][0]["bbox"] == pytest.approx(geometry)
    assert read["views"][0]["frame_bbox"] == pytest.approx([100.0, 100.0, 172.0, 130.0])
    assert expected
    assert _defpoints(backend) == expected


async def test_a_view_added_to_a_rotated_part_turns_with_it(backend):
    drawn = await draw_part(
        backend, build_part(TWO_STEP), at=(0.0, 0.0), rotation=90.0, dimension=False
    )
    added = await add_view(backend, drawn["part_id"], "side")
    # first angle: the side view sits left of the front in the part's frame,
    # which is BELOW it once the sheet is turned 90 degrees CCW
    front, side = drawn["views"][0]["bbox"], added["view"]["bbox"]
    assert side[3] <= front[1] + 1e-6


async def test_draw_then_add_view_then_dimension_never_duplicates(backend):
    drawn = await draw_part(backend, build_part(SHAFT), at=(0.0, 0.0), dimension=True)
    before = len(await entities(backend, type_filter="DIMENSION"))
    assert before == drawn["dimensions"] > 0
    await add_view(backend, drawn["part_id"], "side")
    result = await dimension_part(backend, drawn["part_id"])
    assert result["views_dimensioned"] == [1]
    after = len(await entities(backend, type_filter="DIMENSION"))
    assert after == before + result["count"]
    assert result["count"] > 0

    with pytest.raises(ValueError, match="already dimensioned"):
        await dimension_part(backend, drawn["part_id"])
    with pytest.raises(ValueError, match=r"0 \(front\) already dimensioned"):
        await dimension_part(backend, drawn["part_id"], views=[0])
    assert len(await entities(backend, type_filter="DIMENSION")) == after


async def test_dimension_part_twice_is_refused_not_stacked(backend):
    drawn = await draw_part(backend, build_part(PLATE), at=(0.0, 0.0), dimension=False)
    first = await dimension_part(backend, drawn["part_id"])
    with pytest.raises(ValueError, match="every view is already dimensioned"):
        await dimension_part(backend, drawn["part_id"])
    assert len(await entities(backend, type_filter="DIMENSION")) == first["count"]


async def test_a_view_the_dimension_layer_cannot_dimension_is_reported_not_flagged(backend):
    drawn = await draw_part(backend, build_part(SHAFT), at=(0.0, 0.0))
    await add_view(
        backend,
        drawn["part_id"],
        "detail",
        detail={"center": [20.0, 12.5], "radius": 8.0, "scale": 5.0, "label": "D"},
    )
    result = await dimension_part(backend, drawn["part_id"])
    assert result["views_dimensioned"] == []
    assert any(row.get("kind") == "detail" for row in result["skipped"])
    assert result["count"] == 0


async def test_an_island_that_does_not_attach_removes_the_hatch_and_raises(backend, monkeypatch):
    drawn = await draw_part(backend, build_part(TUBE), at=(0.0, 0.0), dimension=False)
    real = backend.hatch_add_boundary

    async def refuse(handle, edges):
        return await real(handle, [])

    monkeypatch.setattr(backend, "hatch_add_boundary", refuse)
    with pytest.raises(ValueError, match="did not attach"):
        await add_view(
            backend,
            drawn["part_id"],
            "section",
            plane={"p1": [15.0, -40.0], "p2": [15.0, 40.0], "label": "B"},
        )
    assert not await entities(backend, type_filter="HATCH")


async def test_an_engine_without_hatch_edge_paths_refuses_before_drawing(backend, monkeypatch):
    from backends.base import CapabilityMap, FeatureCapability
    from backends.capability import UnsupportedCapabilityError

    drawn = await draw_part(backend, build_part(TUBE), at=(0.0, 0.0), dimension=False)
    count = len(await entities(backend))
    monkeypatch.setattr(
        backend,
        "capabilities",
        lambda: CapabilityMap(
            "com", {"hatch_edge_paths": FeatureCapability(False, reason="measured")}
        ),
    )
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await add_view(
            backend,
            drawn["part_id"],
            "section",
            plane={"p1": [15.0, -40.0], "p2": [15.0, 40.0], "label": "B"},
        )
    assert excinfo.value.capability == "hatch_edge_paths"
    assert len(await entities(backend)) == count


async def test_a_scale_that_would_only_be_recorded_is_refused(backend):
    with pytest.raises(ValueError, match="full size"):
        await draw_part(backend, build_part(PLATE), scale=0.5)
    drawn = await draw_part(backend, build_part(PLATE), dimension=False)
    with pytest.raises(ValueError, match="full size"):
        await add_view(backend, drawn["part_id"], "side", scale=2.0)


async def test_a_second_added_view_steps_along_its_own_projection_axis(backend):
    drawn = await draw_part(
        backend, build_part(PLATE), at=(0.0, 0.0), dimension=False, projection="third"
    )
    side = await add_view(backend, drawn["part_id"], "side")
    section = await add_view(
        backend,
        drawn["part_id"],
        "section",
        plane={"p1": [0.0, 25.0], "p2": [80.0, 25.0], "label": "A"},
    )
    front = drawn["views"][0]["bbox"]
    # third angle puts both to the RIGHT; the second steps further right,
    # never back onto the front view
    assert section["view"]["bbox"][0] >= side["view"]["bbox"][2] + 20.0 - 1e-6
    assert section["view"]["bbox"][0] > front[2]


async def test_a_top_view_sharing_the_x_range_is_not_shoved(backend):
    drawn = await draw_part(backend, build_part(PLATE), at=(0.0, 0.0), dimension=False)
    await add_view(backend, drawn["part_id"], "side")
    top = await add_view(backend, drawn["part_id"], "top", gap=25.0)
    front = drawn["views"][0]["bbox"]
    assert top["view"]["bbox"][0] == pytest.approx(front[0])
    assert top["view"]["bbox"][2] == pytest.approx(front[2])


@pytest.mark.parametrize("at", [[], [5.0], {"x": 1}, "0,0", [1.0, "a"]])
async def test_a_malformed_at_is_refused_with_its_part_path(backend, at):
    with pytest.raises(ValueError, match=r"parts\[0\]: at"):
        await draw_from_spec(backend, {"parts": [{"part": PLATE, "at": at}]})
    assert not await entities(backend, type_filter="POINT", layer=ANCHOR_LAYER)


async def test_a_transaction_that_cannot_open_is_a_value_error(backend, monkeypatch):
    async def busy():
        return {"ok": False, "error": "a transaction is already open"}

    monkeypatch.setattr(backend, "transaction_begin", busy)
    with pytest.raises(ValueError, match="could not open a transaction"):
        await draw_from_spec(backend, {"parts": [{"part": PLATE, "at": [0.0, 0.0]}]})


async def test_a_single_step_disk_dimensions_without_a_text_collision(backend):
    """The largest diameter's text used to land on its own segment's chain text.

    The front-view diameters start at the upper chord end, and ActiveX puts a
    diameter's text beyond that FIRST point - above the view, clear of the
    chain rows (measured on AutoCAD 2026). The headless engine put it beyond
    the second point instead, so ⌀90 landed exactly on the "15" of a 15 mm
    disk and ``dim_overlap`` fired on every single-step part the default
    ``mech_part_draw`` produced headlessly. Found by the v5 ``mech_assembly``
    benchmark and the live smoke together.
    """
    from engineering.critique import run_critique

    disk = build_part(
        {
            "kind": "revolved",
            "name": "FLANGE",
            "material": "cast_iron",
            "segments": [{"length": 15.0, "d_outer": 90.0, "d_inner": 40.0}],
        }
    )
    await draw_part(backend, disk, at=(0.0, 0.0), views=("front",), dimension=True)
    assert await run_critique(backend, ["dim_overlap"]) == []


async def test_every_layer_exists_before_an_entity_is_put_on_it(backend, monkeypatch):
    """ActiveX will not create a layer on assignment - the headless engine will.

    Measured on AutoCAD 2026 by scripts/smoke_mech_com.py: ``entity.Layer =
    "MECH"`` raised ``-0x7ffdfff7 ('Key not found')`` after every view of the
    part had been drawn, so ``mech_part_draw`` could not finish on a live
    seat. This backend is made as strict as ActiveX, and the whole part -
    views, anchor, dimensions - has to draw on it.
    """
    creators = (
        "entity_create_point",
        "entity_create_line",
        "entity_create_circle",
        "entity_create_arc",
        "entity_create_polyline",
        "entity_create_text",
        "entity_create_hatch",
    )
    for name in creators:
        real = getattr(backend, name)

        async def strict(*args, _real=real, **kwargs):
            layer = kwargs.get("layer")
            known = {row.name.lower() for row in await backend.layer_list()}
            if layer and layer.lower() not in known:
                raise RuntimeError(f"Key not found: layer {layer!r}")
            return await _real(*args, **kwargs)

        monkeypatch.setattr(backend, name, strict)

    drawn = await draw_part(
        backend, build_part(SHAFT), at=(0.0, 0.0), views=("front", "side"), dimension=True
    )
    assert drawn["part_id"]
    assert ANCHOR_LAYER.lower() in {row.name.lower() for row in await backend.layer_list()}
