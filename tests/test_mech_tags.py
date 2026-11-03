"""Mechanical dimensions and hole tables carry feature tags (v1.8.0, plan item 1.8-06).

1.7.0 tagged every primitive a view drew but left the dimensions and the hole
table bare, so nothing on the drawing said which feature a DIMENSION measured
or which table belonged to which part - a revision could only delete and
redraw. Now every DIMENSION ``mech_dimension_part`` draws is tagged
``{kind: "feature", part, feature, view, role: "dim"}`` in its create call, the
hole table ``role: "hole_table"``, and ``draw_prims`` writes the primitive's
ordinal ``i`` so one entity of a feature can be told from its siblings.
"""

from __future__ import annotations

import pytest

from engineering.mech.draw import dimension_part, draw_hole_pattern, draw_part, draw_prims
from engineering.mech.part import build_part
from engineering.mech.primitives import Circle, Line
from engineering.mech.xdata import decode

pytestmark = pytest.mark.asyncio

PLATE = {
    "kind": "prismatic",
    "name": "plate",
    "material": "aluminium",
    "thickness": 8.0,
    "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
    "features": [
        {"kind": "hole", "id": "h1", "x": 20.0, "y": 25.0, "diameter": 10.0},
        {"kind": "hole", "id": "h2", "x": 60.0, "y": 25.0, "diameter": 10.0},
    ],
}


async def _tag(backend, handle):
    raw = await backend.entity_get_xdata(handle, "ACADMCP_MECH")
    values = (raw.get("xdata") or {}).get("ACADMCP_MECH")
    return decode(values) if values else None


async def test_every_dimension_of_a_part_is_tagged_with_its_feature(backend):
    drawn = await draw_part(backend, build_part(PLATE), dimension=False)
    result = await dimension_part(backend, drawn["part_id"])
    assert result["hole_table"] is None and result["count"] > 0
    for handle, row in zip(result["handles"], result["resolved"], strict=True):
        assert (await backend.entity_get(handle)).type == "DIMENSION"
        tag = await _tag(backend, handle)
        assert tag == {
            "v": 1,
            "kind": "feature",
            "part": drawn["part_id"],
            "feature": row["feature"] or "",
            "view": "front",
            "role": "dim",
        }
    features = {(await _tag(backend, h))["feature"] for h in result["handles"]}
    assert {"h1", "h2"} <= features


async def test_the_hole_table_is_tagged_as_the_part_s_hole_table(backend):
    drawn = await draw_part(backend, build_part(PLATE), dimension=False)
    result = await dimension_part(backend, drawn["part_id"], hole_table_threshold=1)
    table = result["hole_table"]
    assert table and table in result["handles"]
    tag = await _tag(backend, table)
    assert tag["role"] == "hole_table"
    assert tag["part"] == drawn["part_id"] and tag["view"] == "front"
    assert tag["kind"] == "feature" and tag["feature"] == ""
    # the headless table is a composite: every child carries the tag, so a
    # revision can find all of it, not only the handle the payload names
    texts = await backend.entity_list(type_filter="MTEXT", layer_filter="TEXT")
    roles = [(await _tag(backend, t.handle))["role"] for t in texts]
    assert roles and set(roles) == {"hole_table"}


async def test_draw_part_with_dimensions_tags_them_too(backend):
    drawn = await draw_part(backend, build_part(PLATE))
    dims = await backend.entity_list(type_filter="DIMENSION")
    assert len(dims) == drawn["dimensions"] > 0
    roles = {(await _tag(backend, d.handle))["role"] for d in dims}
    assert roles == {"dim"}


async def test_draw_prims_writes_each_primitive_s_ordinal(backend):
    drawn = await draw_prims(
        backend,
        [
            Line((0.0, 0.0), (10.0, 0.0), "visible", "edge"),
            Circle((5.0, 5.0), 2.0, "visible", "bore"),
            Line((5.0, 0.0), (5.0, 10.0), "center", "bore"),
        ],
        feature_tag={"part": "P1", "view": "front"},
    )
    tags = [await _tag(backend, h) for h in drawn["handles"]]
    assert [t["i"] for t in tags] == [0, 1, 2]
    assert [t["feature"] for t in tags] == ["edge", "bore", "bore"]
    assert all("role" not in t for t in tags)


async def test_a_standalone_pattern_dimension_is_tagged(backend):
    result = await draw_hole_pattern(
        backend,
        pattern="linear",
        x=0.0,
        y=0.0,
        diameter=6.0,
        count=3,
        spacing=20.0,
        dimension=True,
    )
    dims = await backend.entity_list(type_filter="DIMENSION")
    assert len(dims) == 1 and dims[0].handle in result["handles"]
    tag = await _tag(backend, dims[0].handle)
    assert (tag["part"], tag["feature"], tag["view"], tag["role"]) == ("", "pattern", "", "dim")


async def test_the_mech_critique_still_reads_no_dimension_tag_as_a_record(backend):
    from engineering.mech.critique import build_index

    drawn = await draw_part(backend, build_part(PLATE))
    index = await build_index(backend)
    assert [record["handle"] for record in index["records"]] == [drawn["part_id"]]
