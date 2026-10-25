"""Per-entity ACADMCP_MECH feature tags (B17), written in the create call.

Every entity the mechanical generators draw names its part, its feature and its
view; a copy carries the tag, which is how a binder sees two candidates for one
feature.
"""

from __future__ import annotations

import pytest

from engineering.mech.draw import add_view, draw_hole_pattern, draw_part, draw_prims
from engineering.mech.part import build_part
from engineering.mech.primitives import (
    Arc,
    Circle,
    HatchArea,
    Line,
    Poly,
    Text,
    rotate,
    scale,
    translate,
    with_feature,
)
from engineering.mech.views import build_view
from engineering.mech.xdata import KINDS, decode

PLATE2 = {
    "kind": "prismatic",
    "name": "plate",
    "material": "aluminium",
    "thickness": 8.0,
    "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
    "features": [
        {"kind": "hole", "id": "h1", "x": 40.0, "y": 25.0, "diameter": 10.0},
        {"kind": "hole", "id": "h2", "x": 60.0, "y": 25.0, "diameter": 10.0},
    ],
}

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


def test_feature_is_the_last_field_and_defaults_to_empty():
    for prim in (
        Line((0.0, 0.0), (1.0, 0.0)),
        Arc((0.0, 0.0), 1.0, 0.0, 90.0),
        Circle((0.0, 0.0), 1.0),
        Poly(((0.0, 0.0), (1.0, 0.0))),
        HatchArea((((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),)),
        Text((0.0, 0.0), "A"),
    ):
        assert prim._fields[-1] == "feature" and prim.feature == ""


def test_with_feature_tags_once_and_the_transforms_keep_the_tag():
    tagged = with_feature(Circle((1.0, 0.0), 2.0), "bore")
    assert tagged.feature == "bore"
    assert with_feature(tagged, "other").feature == "bore"
    for moved in (
        translate([tagged], 5.0, 5.0),
        rotate([tagged], 30.0),
        scale([tagged], 2.0),
    ):
        assert moved[0].feature == "bore"


def test_a_view_tags_each_feature_s_primitives_with_its_id():
    view = build_view(build_part(PLATE2), "front")
    circles = {p.center: p.feature for p in view.prims if isinstance(p, Circle)}
    assert circles == {(40.0, 25.0): "h1", (60.0, 25.0): "h2"}
    outline = [p for p in view.prims if isinstance(p, Line) and p.role == "visible"]
    assert len(outline) == 4 and all(p.feature == "" for p in outline)
    centre_lines = [p for p in view.prims if isinstance(p, Line) and p.role == "center"]
    assert sorted(p.feature for p in centre_lines) == ["h1", "h1", "h2", "h2"]


def test_a_mirrored_end_view_and_a_detail_keep_the_tags():
    part = build_part(PLATE2)
    left = build_view(part, "side", side="left")
    hidden = [p for p in left.prims if isinstance(p, Line) and p.role == "hidden"]
    assert hidden and all(p.feature == "h1" for p in hidden)
    detail = build_view(
        part, "detail", detail={"center": [40.0, 25.0], "radius": 8.0, "scale": 2.0, "of": "front"}
    )
    assert {p.feature for p in detail.prims if isinstance(p, Circle)} == {"h1"}


def test_the_codec_accepts_the_feature_kind():
    assert "feature" in KINDS
    payload = {"v": 1, "kind": "feature", "part": "1A", "feature": "h1", "view": "front"}
    assert decode(['{"feature":"h1","kind":"feature","part":"1A","v":1,"view":"front"}']) == payload


async def _tags(backend, handles):
    out = {}
    for handle in handles:
        raw = await backend.entity_get_xdata(handle, "ACADMCP_MECH")
        out[handle] = decode(raw["xdata"]["ACADMCP_MECH"])
    return out


@pytest.mark.asyncio
async def test_draw_part_tags_every_entity_it_draws(backend):
    result = await draw_part(backend, build_part(PLATE2), at=(0.0, 0.0), dimension=False)
    tags = await _tags(backend, result["handles"])
    assert len(tags) == 10  # 4 outline lines, 2 circles, 4 centre lines
    assert all(t["kind"] == "feature" for t in tags.values())
    assert {t["part"] for t in tags.values()} == {result["part_id"]}
    assert {t["view"] for t in tags.values()} == {"front"}
    circles = await backend.entity_list(type_filter="CIRCLE")
    by_centre = {tuple(c.properties["center"]): c.handle for c in circles}
    assert tags[by_centre[(40.0, 25.0)]]["feature"] == "h1"
    assert tags[by_centre[(60.0, 25.0)]]["feature"] == "h2"
    assert sum(t["feature"] == "" for t in tags.values()) == 4


@pytest.mark.asyncio
async def test_the_tags_ride_in_the_create_calls(backend):
    class Recording:
        def __init__(self, inner):
            self._inner = inner
            self.calls = []

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if not name.startswith("entity_") or not callable(attr):
                return attr

            async def wrapper(*args, **kwargs):
                self.calls.append((name, kwargs.get("xdata")))
                return await attr(*args, **kwargs)

            return wrapper

    recorded = Recording(backend)
    await draw_prims(
        recorded,
        [
            Line((0.0, 0.0), (1.0, 0.0), "visible", "edge"),
            Circle((0.0, 0.0), 1.0, "visible", "bore"),
        ],
        feature_tag={"part": "P1", "view": "front"},
    )
    assert not [name for name, _x in recorded.calls if name == "entity_set_xdata"]
    created = [x for name, x in recorded.calls if name.startswith("entity_create_")]
    features = [decode(x["ACADMCP_MECH"])["feature"] for x in created]
    assert features == ["edge", "bore"]


@pytest.mark.asyncio
async def test_draw_prims_without_a_tag_writes_none(backend):
    drawn = await draw_prims(backend, [Line((0.0, 0.0), (1.0, 0.0))])
    raw = await backend.entity_get_xdata(drawn["handles"][0])
    assert raw["xdata"] == {}


@pytest.mark.asyncio
async def test_a_section_view_names_its_label(backend):
    drawn = await draw_part(backend, build_part(SHAFT), at=(0.0, 0.0), dimension=False)
    added = await add_view(
        backend,
        drawn["part_id"],
        "section",
        plane={"p1": [0.0, 0.0], "p2": [100.0, 0.0], "label": "A"},
    )
    tags = await _tags(backend, added["handles"])
    assert {t["view"] for t in tags.values()} == {"section:A-A"}
    assert {t["part"] for t in tags.values()} == {drawn["part_id"]}


@pytest.mark.asyncio
async def test_the_mech_critique_index_does_not_read_feature_tags_as_records(backend):
    from engineering.mech.critique import build_index

    result = await draw_part(backend, build_part(PLATE2), at=(0.0, 0.0), dimension=False)
    index = await build_index(backend)
    assert [record["handle"] for record in index["records"]] == [result["part_id"]]


@pytest.mark.asyncio
async def test_a_standalone_hole_pattern_is_tagged_as_one_feature(backend):
    result = await draw_hole_pattern(
        backend, pattern="linear", x=0.0, y=0.0, diameter=6.0, count=3, spacing=20.0
    )
    tags = await _tags(backend, result["handles"])
    assert {(t["part"], t["feature"]) for t in tags.values()} == {("", "pattern")}
