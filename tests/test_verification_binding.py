"""engineering/verification/binding.py (B17): the requirement's feature, or an honest no.

Review Focus 7: two identical holes where the requirement names one, and a copied
feature tag, are `ambiguous` with both candidates - never a pick. Review Focus 8:
a dimension whose text says 40 over geometry measuring 42, and a DIMLFAC of 2,
are findings, with the geometric value, the dimension's own measurement and the
displayed text kept apart.
"""

from __future__ import annotations

import json
from statistics import NormalDist

import pytest

from engineering.mech.draw import draw_part
from engineering.mech.part import build_part
from engineering.understand.snapshot import take_snapshot
from engineering.verification.binding import (
    WILSON_Z,
    bind,
    bind_dimension,
    feature_of,
    wilson_interval,
)
from engineering.verification.observe import observe
from engineering.verification.types import FeatureSelector, Requirement, RequirementSet

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
HOLE_WINDOW = {"type": "circle", "radius_min": 4.999, "radius_max": 5.001}


def _sel(kind, **value):
    return FeatureSelector(kind=kind, value=tuple(sorted(value.items())))


def _req(rid, quantity, *selectors, nominal=None):
    target = (("unit", "mm"),) if nominal is None else (("nominal", nominal), ("unit", "mm"))
    return Requirement(
        id=rid,
        quantity=quantity,
        target=target,
        features=tuple(selectors),
        provenance="explicit",
        source=(("label", rid),),
    )


def _bind(observed, *requirements, policy="strict"):
    return bind(
        RequirementSet(requirements=tuple(requirements), units="mm"), observed, policy=policy
    )


async def _observed(backend):
    snap = await take_snapshot(backend, block_defs=True)
    return observe(snap, document={"document_id": "doc", "revision": 7})


async def _circle_at(backend, x, y):
    for info in await backend.entity_list(type_filter="CIRCLE", limit=1000):
        if info.properties["center"] == [x, y]:
            return info.handle
    raise AssertionError(f"no circle at {(x, y)}")


@pytest.mark.asyncio
async def test_a_feature_tag_binds_the_one_hole_it_names(backend):
    drawn = await draw_part(backend, build_part(PLATE2), at=(0.0, 0.0), dimension=False)
    h1 = await _circle_at(backend, 40.0, 25.0)
    observed = await _observed(backend)
    (binding,) = _bind(
        observed,
        _req(
            "bore",
            "diameter",
            _sel("feature_tag", feature="h1", part=drawn["part_id"], type="circle"),
        ),
    )
    assert binding.status == "bound"
    (ref,) = binding.refs
    assert ref.handles == (h1,) and ref.feature == "h1"
    assert ref.document_id == "doc" and ref.revision == 7 and ref.space == "Model"
    assert "one candidate" in binding.reason
    assert dict(feature_of(ref, observed).geometry)["radius"] == 5.0


@pytest.mark.asyncio
async def test_two_identical_holes_named_by_size_are_ambiguous_never_a_pick(backend):
    await draw_part(backend, build_part(PLATE2), at=(0.0, 0.0), dimension=False)
    h1 = await _circle_at(backend, 40.0, 25.0)
    h2 = await _circle_at(backend, 60.0, 25.0)
    (binding,) = _bind(
        await _observed(backend), _req("bore", "diameter", _sel("predicate", **HOLE_WINDOW))
    )
    assert binding.status == "ambiguous"
    assert binding.refs == ()
    assert sorted(ref.handles[0] for ref in binding.candidates) == sorted([h1, h2])
    assert "2 equal candidates, none chosen" in binding.reason


@pytest.mark.asyncio
async def test_a_copied_feature_tag_is_ambiguous(backend):
    drawn = await draw_part(backend, build_part(PLATE2), at=(0.0, 0.0), dimension=False)
    h1 = await _circle_at(backend, 40.0, 25.0)
    copy = await backend.entity_copy(h1, 0.0, 100.0)
    (binding,) = _bind(
        await _observed(backend),
        _req(
            "bore",
            "diameter",
            _sel("feature_tag", feature="h1", part=drawn["part_id"], type="circle"),
        ),
    )
    assert binding.status == "ambiguous"
    assert {ref.handles[0] for ref in binding.candidates} == {h1, copy.handle}


@pytest.mark.asyncio
async def test_a_handle_that_is_not_there_is_unknown(backend):
    await backend.entity_create_circle(0.0, 0.0, 5.0)
    (binding,) = _bind(
        await _observed(backend), _req("x", "radius", _sel("handle", handle="FFFFF"))
    )
    assert binding.status == "unknown" and binding.refs == () and "no candidate" in binding.reason


@pytest.mark.asyncio
async def test_count_counts_every_match_and_zero_is_a_result(backend):
    await draw_part(backend, build_part(PLATE2), at=(0.0, 0.0), dimension=False)
    observed = await _observed(backend)
    counted, absent = _bind(
        observed,
        _req("holes", "count", _sel("predicate", **HOLE_WINDOW)),
        _req("no_ellipse", "absence", _sel("predicate", type="ellipse")),
    )
    assert counted.status == "bound" and len(counted.refs) == 2
    assert absent.status == "bound" and absent.refs == ()


@pytest.mark.asyncio
async def test_lenient_binds_congruent_candidates_and_still_refuses_unequal_ones(backend):
    await draw_part(backend, build_part(PLATE2), at=(0.0, 0.0), dimension=False)
    (binding,) = _bind(
        await _observed(backend),
        _req("bores", "diameter", _sel("predicate", **HOLE_WINDOW)),
        policy="lenient",
    )
    assert binding.status == "bound" and len(binding.refs) == 2
    assert "every one measured (lenient)" in binding.reason
    await backend.entity_create_circle(200.0, 0.0, 6.0)
    wide = {"type": "circle", "radius_min": 4.0, "radius_max": 7.0}
    (binding,) = _bind(
        await _observed(backend),
        _req("bores", "diameter", _sel("predicate", **wide)),
        policy="lenient",
    )
    assert binding.status == "ambiguous"


@pytest.mark.asyncio
async def test_a_drawing_without_tags_falls_back_to_the_geometric_predicate(backend):
    plain = await backend.entity_create_circle(10.0, 10.0, 5.0)
    fallback = tuple(sorted({"type": "circle", "radius_min": 4.999, "radius_max": 5.001}.items()))
    (binding,) = _bind(
        await _observed(backend),
        _req("bore", "diameter", _sel("feature_tag", feature="h1", fallback=fallback)),
    )
    assert binding.status == "bound" and binding.refs[0].handles == (plain.handle,)
    assert "fell back to the geometric predicate" in binding.reason


@pytest.mark.asyncio
async def test_an_arch_element_selects_the_entities_its_record_owns(backend):
    face = await backend.entity_create_line(0.0, 0.0, 4000.0, 0.0)
    back = await backend.entity_create_line(0.0, 200.0, 4000.0, 200.0)
    end = await backend.entity_create_line(4000.0, 0.0, 4000.0, 200.0)
    await backend.entity_create_line(0.0, 500.0, 10.0, 500.0)  # not the wall's
    record = json.dumps({"v": 1, "kind": "wall", "id": "W1"}, separators=(",", ":"))
    await backend.entity_set_xdata(face.handle, "ACADMCP_ARCH", [record])
    await backend.entity_set_xdata(face.handle, "ACADMCP_ARCH_H", [back.handle, end.handle])
    observed = await _observed(backend)
    counted, distance = _bind(
        observed,
        _req("faces", "count", _sel("arch_element", element="wall:W1", type="line")),
        _req("thickness", "distance", _sel("arch_element", element="wall:W1", type="line")),
    )
    assert {ref.handles[0] for ref in counted.refs} == {face.handle, back.handle, end.handle}
    assert distance.status == "ambiguous"


@pytest.mark.asyncio
async def test_an_exploded_feature_is_bound_by_handle_and_block_path(backend):
    block = backend._doc.blocks.new("HOLE")
    circle = block.add_circle((0, 0), 5)
    first = await backend.entity_create_block_ref("HOLE", 0.0, 0.0)
    second = await backend.entity_create_block_ref("HOLE", 100.0, 0.0)
    observed = await _observed(backend)
    named, bare = _bind(
        observed,
        _req("a", "radius", _sel("handle", handle=circle.dxf.handle, block_path=(second.handle,))),
        _req("b", "radius", _sel("handle", handle=circle.dxf.handle)),
    )
    assert named.status == "bound" and named.refs[0].block_path == (second.handle,)
    assert dict(feature_of(named.refs[0], observed).geometry)["center"] == (100.0, 0.0)
    assert bare.status == "unknown"  # the definition's own handle is in no layout
    assert first.handle != second.handle


@pytest.mark.asyncio
async def test_a_text_override_of_40_over_geometry_of_42_is_a_finding(backend):
    msp = backend._doc.modelspace()
    msp.add_line((0, 0), (0, 20))
    msp.add_line((42, 0), (42, 20))
    dim = msp.add_linear_dim(base=(0, 30), p1=(0, 0), p2=(42, 0), dimstyle="Standard")
    dim.render()
    dim.dimension.dxf.text = "40"
    observed = await _observed(backend)
    (dimension,) = [f for f in observed.features if f.type == "dimension"]
    result = bind_dimension(dimension, observed, epsilon=1e-9)
    assert result["status"] == "bound" and len(result["refs"]) == 2
    assert result["geometric_value"] == 42.0
    assert result["dimension_measurement"] == 42.0
    assert result["displayed_text"] == "40" and result["displayed_value"] == 40.0
    assert result["mismatch"] is True
    assert result["findings"] == ["text_override_disagrees_with_geometry"]


@pytest.mark.asyncio
async def test_an_override_that_keeps_the_measured_value_shows_that_value(backend):
    # "4x <>": the "<>" placeholder IS the measured value; the leading 4 is a
    # count, not the displayed dimension, so nothing disagrees with 42.
    msp = backend._doc.modelspace()
    msp.add_line((0, 0), (0, 20))
    msp.add_line((42, 0), (42, 20))
    dim = msp.add_linear_dim(base=(0, 30), p1=(0, 0), p2=(42, 0), dimstyle="Standard")
    dim.render()
    dim.dimension.dxf.text = "4x <>"
    observed = await _observed(backend)
    (dimension,) = [f for f in observed.features if f.type == "dimension"]
    result = bind_dimension(dimension, observed, epsilon=1e-9)
    assert result["displayed_text"] == "4x 42"
    assert result["displayed_value"] == 42.0
    assert result["mismatch"] is False and result["findings"] == []


@pytest.mark.asyncio
async def test_a_dimlfac_of_2_over_21_of_geometry_is_a_finding(backend):
    msp = backend._doc.modelspace()
    msp.add_line((100, 0), (100, 20))
    msp.add_line((121, 0), (121, 20))
    dim = msp.add_linear_dim(
        base=(100, 30), p1=(100, 0), p2=(121, 0), dimstyle="Standard", override={"dimlfac": 2.0}
    )
    dim.render()
    observed = await _observed(backend)
    (dimension,) = [f for f in observed.features if f.type == "dimension"]
    result = bind_dimension(dimension, observed, epsilon=1e-9)
    assert result["geometric_value"] == 21.0
    assert result["dimension_measurement"] == 21.0
    assert result["dimlfac"] == 2.0
    assert result["displayed_value"] == 42.0  # 21 x 2: the sheet says 42
    assert result["findings"] == ["dimlfac_scales_the_displayed_value"]


@pytest.mark.asyncio
async def test_rotated_and_aligned_dimensions_measure_along_their_own_direction(backend):
    msp = backend._doc.modelspace()
    msp.add_point((0, 0))
    msp.add_point((30, 40))
    msp.add_linear_dim(base=(0, 60), p1=(0, 0), p2=(30, 40), angle=0, dimstyle="Standard").render()
    msp.add_aligned_dim(p1=(0, 0), p2=(30, 40), distance=10, dimstyle="Standard").render()
    observed = await _observed(backend)
    values = sorted(
        bind_dimension(f, observed, epsilon=1e-9)["geometric_value"]
        for f in observed.features
        if f.type == "dimension"
    )
    assert values == [pytest.approx(30.0, abs=1e-9), pytest.approx(50.0, abs=1e-9)]


@pytest.mark.asyncio
async def test_a_diameter_dimension_binds_its_circle_and_a_selector_follows_it(backend):
    msp = backend._doc.modelspace()
    circle = msp.add_circle((200, 0), 10)
    msp.add_circle((300, 0), 10)
    dim = msp.add_diameter_dim(center=(200, 0), radius=10, angle=45, dimstyle="Standard")
    dim.render()
    observed = await _observed(backend)
    (dimension,) = [f for f in observed.features if f.type == "dimension"]
    result = bind_dimension(dimension, observed, epsilon=1e-9)
    assert [ref.handles for ref in result["refs"]] == [(circle.dxf.handle,)]
    assert result["geometric_value"] == pytest.approx(20.0, abs=1e-9)
    assert result["mismatch"] is False
    (binding,) = _bind(
        observed, _req("bore", "diameter", _sel("dimension", handle=dim.dimension.dxf.handle))
    )
    assert binding.status == "bound" and binding.refs[0].handles == (circle.dxf.handle,)


@pytest.mark.asyncio
async def test_an_angular_dimension_binds_both_legs(backend):
    msp = backend._doc.modelspace()
    leg1 = msp.add_line((400, 0), (420, 0))
    leg2 = msp.add_line((400, 0), (400, 20))
    msp.add_angular_dim_2l(
        base=(410, 10), line1=((400, 0), (420, 0)), line2=((400, 0), (400, 20)), dimstyle="Standard"
    ).render()
    observed = await _observed(backend)
    (dimension,) = [f for f in observed.features if f.type == "dimension"]
    result = bind_dimension(dimension, observed, epsilon=1e-9)
    assert [ref.handles[0] for ref in result["refs"]] == [leg1.dxf.handle, leg2.dxf.handle]
    assert result["geometric_value"] == pytest.approx(90.0, abs=1e-9)


def test_wilson_interval_matches_its_closed_forms():
    z = NormalDist().inv_cdf(0.975)
    assert WILSON_Z == pytest.approx(z, abs=1e-12)
    low, high = wilson_interval(10, 10)
    assert low == pytest.approx(10.0 / (10.0 + z * z), abs=1e-12)  # p = 1: n / (n + z^2)
    assert high == pytest.approx(1.0, abs=1e-12)
    low, high = wilson_interval(0, 10)
    assert low == pytest.approx(0.0, abs=1e-12)
    assert high == pytest.approx(z * z / (10.0 + z * z), abs=1e-12)  # p = 0: z^2 / (n + z^2)
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(8, 10)  # the textbook 8/10 case
    assert (low, high) == (pytest.approx(0.4902, abs=1e-4), pytest.approx(0.9433, abs=1e-4))


@pytest.mark.asyncio
async def test_an_unknown_policy_is_refused(backend):
    with pytest.raises(ValueError, match="policy"):
        bind(RequirementSet(requirements=(), units="mm"), await _observed(backend), policy="greedy")
