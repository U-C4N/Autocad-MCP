"""The part model: construction, round-trip and every refusal the spec names.

Pure arithmetic - no backend, no DXF. The feature surface is exercised through
a local stub so this module's gate (placement on the part, two features
removing the same material) is pinned before `engineering/mech/features.py`
exists.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from engineering.mech.part import (
    PrismaticPart,
    RevolvedPart,
    Segment,
    build_part,
    inner_radius_at,
    outer_radius_at,
    outline_bbox,
    part_length,
    part_max_diameter,
    part_to_dict,
    point_in_outline,
    segment_bounds,
    validate_part,
)

EPS = 1e-9

SHAFT = {
    "kind": "revolved",
    "name": "shaft",
    "material": "steel",
    "segments": [
        {"length": 20.0, "d_outer": 25.0},
        {"length": 40.0, "d_outer": 30.0, "d_inner": 10.0},
        {"length": 15.0, "d_outer": 20.0, "taper_to": 16.0},
    ],
}

PLATE = {
    "kind": "prismatic",
    "name": "plate",
    "material": "aluminium",
    "thickness": 8.0,
    "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
}


@dataclass(frozen=True, kw_only=True)
class StubFeature:
    """The duck-typed surface `build_part` uses, and nothing else."""

    id: str
    segment: int = 0
    box: tuple[float, float, float, float] | None = None

    def validate(self, part) -> None:
        if not 0 <= self.segment < len(part.segments):
            raise ValueError(
                f"segment {self.segment} is outside the {len(part.segments)}-segment part "
                f"(segments 0-{len(part.segments) - 1})."
            )

    def removal_box(self, part):
        return self.box

    def to_dict(self) -> dict:
        return {"kind": "stub", "id": self.id, "segment": self.segment}


def test_build_part_makes_a_revolved_part_with_typed_segments():
    part = build_part(SHAFT)
    assert isinstance(part, RevolvedPart)
    assert part.name == "shaft"
    assert part.material == "steel"
    assert part.segments[1] == Segment(40.0, 30.0, 10.0, None)
    assert part.segments[2].taper_to == 16.0
    assert part.features == ()


def test_build_part_makes_a_prismatic_part_and_drops_a_repeated_closing_vertex():
    closed = dict(PLATE, outline=PLATE["outline"] + [[0.0, 0.0]])
    part = build_part(closed)
    assert isinstance(part, PrismaticPart)
    assert len(part.outline) == 4
    assert part.thickness == 8.0
    assert outline_bbox(part) == (0.0, 0.0, 80.0, 50.0)


@pytest.mark.parametrize("spec", [SHAFT, PLATE])
def test_part_to_dict_round_trips_through_build_part(spec):
    part = build_part(spec)
    assert build_part(part_to_dict(part)) == part


def test_geometry_helpers_report_the_real_numbers():
    part = build_part(SHAFT)
    assert part_length(part) == 75.0
    assert part_max_diameter(part) == 30.0
    assert segment_bounds(part) == ((0.0, 20.0), (20.0, 60.0), (60.0, 75.0))
    assert outer_radius_at(part, 10.0) == 12.5
    assert inner_radius_at(part, 30.0) == 5.0
    assert inner_radius_at(part, 10.0) == 0.0
    # the taper runs 20 -> 16 mm over the last 15 mm, so its mid-point is 18 mm
    assert outer_radius_at(part, 67.5) == pytest.approx(9.0, abs=EPS)
    plate = build_part(PLATE)
    assert part_length(plate) == 80.0
    assert point_in_outline(plate, 40.0, 25.0) is True
    assert point_in_outline(plate, 90.0, 25.0) is False


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 0.0, -5.0])
def test_a_non_finite_or_non_positive_number_is_refused_by_path(bad):
    spec = dict(SHAFT, segments=[dict(s) for s in SHAFT["segments"]])
    spec["segments"][2]["d_outer"] = bad
    with pytest.raises(ValueError) as excinfo:
        build_part(spec)
    assert "segments[2].d_outer" in str(excinfo.value)


def test_a_bore_that_is_not_smaller_than_the_outside_is_refused():
    spec = dict(SHAFT, segments=[dict(s) for s in SHAFT["segments"]])
    spec["segments"][1]["d_inner"] = 30.0
    with pytest.raises(ValueError) as excinfo:
        build_part(spec)
    assert "segments[1].d_inner" in str(excinfo.value)
    assert "two segments" in str(excinfo.value)


def test_an_outline_with_fewer_than_three_distinct_vertices_is_refused():
    spec = dict(PLATE, outline=[[0.0, 0.0], [10.0, 0.0], [10.0, 0.0]])
    with pytest.raises(ValueError, match="at least 3 distinct vertices"):
        build_part(spec)


def test_a_self_intersecting_outline_is_refused():
    spec = dict(PLATE, outline=[[0.0, 0.0], [10.0, 10.0], [10.0, 0.0], [0.0, 10.0]])
    with pytest.raises(ValueError, match="crosses itself"):
        build_part(spec)


def test_an_unknown_material_is_refused():
    with pytest.raises(ValueError, match="material"):
        build_part(dict(SHAFT, material="unobtainium"))


def test_a_feature_off_the_part_is_refused_by_its_own_name():
    part = RevolvedPart(
        name="shaft",
        segments=(Segment(20.0, 25.0), Segment(40.0, 30.0), Segment(15.0, 20.0)),
        features=(StubFeature(id="keyway_1", segment=5),),
    )
    with pytest.raises(ValueError) as excinfo:
        validate_part(part)
    message = str(excinfo.value)
    assert "keyway_1" in message
    assert "segment 5" in message
    assert "segments 0-2" in message


def test_two_features_removing_the_same_material_are_refused_with_both_names():
    part = RevolvedPart(
        name="shaft",
        segments=(Segment(60.0, 30.0),),
        features=(
            StubFeature(id="groove_a", box=(10.0, 12.0, 14.0, 15.0)),
            StubFeature(id="groove_b", box=(12.0, 13.0, 16.0, 15.0)),
        ),
    )
    with pytest.raises(ValueError) as excinfo:
        validate_part(part)
    message = str(excinfo.value)
    assert "groove_a" in message and "groove_b" in message
    assert "same material" in message


def test_features_that_only_touch_are_not_an_overlap():
    part = RevolvedPart(
        name="shaft",
        segments=(Segment(60.0, 30.0),),
        features=(
            StubFeature(id="groove_a", box=(10.0, 12.0, 14.0, 15.0)),
            StubFeature(id="groove_b", box=(14.0, 12.0, 18.0, 15.0)),
        ),
    )
    validate_part(part)


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="kind"):
        build_part(dict(SHAFT, kind="lofted"))


def test_an_unknown_segment_key_is_refused_by_name():
    spec = dict(SHAFT, segments=[{"length": 10.0, "d_outer": 20.0, "diameter": 20.0}])
    with pytest.raises(ValueError) as excinfo:
        build_part(spec)
    assert "segments[0]" in str(excinfo.value)
    assert "diameter" in str(excinfo.value)


def test_a_duplicate_feature_id_is_refused():
    """The only test here that reaches `feature_from_dict` (Task 3)."""
    spec = dict(
        SHAFT,
        features=[
            {"kind": "chamfer", "id": "c1", "at": "start", "size": 2.0},
            {"kind": "chamfer", "id": "c1", "at": "end", "size": 2.0},
        ],
    )
    with pytest.raises(ValueError, match="duplicate feature id"):
        build_part(spec)
