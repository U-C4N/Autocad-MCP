"""B15 residue - the shared verification types, defined once for Tasks 11-17.

The digest's oracle is the canonical JSON written out by hand below; the
vocabularies are pinned word for word, because a later task that misspells a
status must fail at construction instead of passing silently.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from engineering.verification.types import (
    BINDING_STATUSES,
    FEATURE_TYPES,
    PROVENANCES,
    QUANTITIES,
    SELECTOR_KINDS,
    STATUSES,
    AcceptanceItem,
    Binding,
    FeatureRef,
    FeatureSelector,
    Measurement,
    ObservedFeature,
    ObservedGeometry,
    Requirement,
    RequirementSet,
    freeze,
    thaw,
)


def _req(**changes) -> Requirement:
    fields = {
        "id": "D1",
        "quantity": "diameter",
        "target": freeze({"nominal": 40.0, "unit": "mm"}),
        "features": (
            FeatureSelector("feature_tag", freeze({"part": "flange", "feature": "bore"})),
        ),
        "provenance": "explicit",
        "source": freeze({"page": 1, "label": "Ø40"}),
    }
    fields.update(changes)
    return Requirement(**fields)


def test_the_vocabularies_are_closed_and_spelled_once():
    assert STATUSES == ("pass", "fail", "unknown", "unsupported", "ambiguous")
    assert PROVENANCES == (
        "explicit",
        "derived",
        "assumed",
        "conflicting",
        "unknown",
        "raster_estimate",
    )
    assert QUANTITIES == (
        "distance",
        "radius",
        "diameter",
        "angle",
        "count",
        "absence",
        "concentric",
        "coincident",
        "parallel",
        "perpendicular",
        "tangent",
        "horizontal",
        "vertical",
    )
    assert SELECTOR_KINDS == ("handle", "feature_tag", "arch_element", "predicate", "dimension")
    assert FEATURE_TYPES == (
        "line",
        "circle",
        "arc",
        "ellipse",
        "polyline",
        "point",
        "spline",
        "dimension",
    )
    assert BINDING_STATUSES == ("bound", "ambiguous", "unknown")


@pytest.mark.parametrize(
    "build, message",
    [
        (
            lambda: Measurement("D1", 40.0, "mm", "radius", 0.0, "passed"),
            "Measurement.status must be one of",
        ),
        (lambda: Binding("D1", "picked", (), (), "x"), "Binding.status must be one of"),
        (lambda: FeatureSelector("layer", ()), "FeatureSelector.kind must be one of"),
        (
            # "spline" is a feature type since Task 13; a HATCH is never observed
            lambda: ObservedFeature("1A", "hatch", "Model", "0", (), ()),
            "ObservedFeature.type must be one of",
        ),
        (lambda: _req(quantity="volume"), "Requirement.quantity must be one of"),
        (lambda: _req(provenance="guessed"), "Requirement.provenance must be one of"),
    ],
)
def test_an_unknown_word_is_refused_at_construction(build, message):
    with pytest.raises(ValueError, match=message):
        build()


def test_requirements_are_hashable_and_frozen():
    requirement = _req()
    assert {requirement, _req()} == {requirement}
    with pytest.raises(dataclasses.FrozenInstanceError):
        requirement.id = "D2"


def test_freeze_and_thaw_round_trip_nested_values():
    frozen = freeze({"b": [1, 2, {"c": 3}], "a": {"z": (4.0, 5.0)}})
    assert [key for key, _ in frozen] == ["a", "b"], "sorted by key"
    assert hash(frozen) == hash(freeze({"a": {"z": [4.0, 5.0]}, "b": [1, 2, {"c": 3}]}))
    assert thaw(frozen) == {"a": {"z": [4.0, 5.0]}, "b": [1, 2, {"c": 3}]}


def test_digest_is_the_sha256_of_the_canonical_json():
    count = Requirement(
        id="H1",
        quantity="count",
        target=freeze({"exact": 6}),
        features=(
            FeatureSelector(
                "predicate", freeze({"type": "circle", "radius_min": 9.0, "radius_max": 11.0})
            ),
        ),
        provenance="explicit",
        source=freeze({"label": "6x Ø10"}),
    )
    rs = RequirementSet(requirements=(count, _req()), units="mm")
    canonical = {
        "version": 1,
        "units": "mm",
        "legacy": None,
        "items": [
            {
                "id": "D1",
                "quantity": "diameter",
                "target": {"nominal": 40.0, "unit": "mm"},
                "features": [{"kind": "feature_tag", "part": "flange", "feature": "bore"}],
                "provenance": "explicit",
                "source": {"page": 1, "label": "Ø40"},
                "datum": None,
                "manufacturing_tolerance": None,
                "verification_epsilon": None,
            },
            {
                "id": "H1",
                "quantity": "count",
                "target": {"exact": 6},
                "features": [
                    {"kind": "predicate", "type": "circle", "radius_min": 9.0, "radius_max": 11.0}
                ],
                "provenance": "explicit",
                "source": {"label": "6x Ø10"},
                "datum": None,
                "manufacturing_tolerance": None,
                "verification_epsilon": None,
            },
        ],
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert rs.to_dict() == canonical
    assert rs.digest() == "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    reordered = RequirementSet(requirements=tuple(reversed(rs.requirements)), units="mm")
    assert reordered.digest() == rs.digest(), "input order is no part of a set"
    assert rs.by_id()["H1"] is count


def test_the_evidence_types_serialise_to_plain_json():
    ref = FeatureRef(
        document_id="a" * 32,
        revision=3,
        space="Model",
        block_path=("FLANGE",),
        handles=("2A",),
        feature="bore",
    )
    binding = Binding("D1", "bound", (ref,), (ref,), "feature tag flange/bore")
    measurement = Measurement(
        "D1", 40.02, "mm", "circle.radius*2", 0.02, "pass", detail=freeze({"epsilon": 1e-6})
    )
    item = AcceptanceItem(_req(), binding, measurement, delta=0.02, tolerance_basis="fit H7")
    feature = ObservedFeature(
        "2A",
        "circle",
        "Model",
        "GEOMETRY",
        ("FLANGE",),
        freeze({"center": [0.0, 0.0], "radius": 20.01}),
        tags=(("ACADMCP_MECH", '{"kind":"feature"}'),),
    )
    observed = ObservedGeometry((feature,), "mm", "mm", "a" * 32, 3, "memory:untitled")
    assert ref.to_dict() == {
        "document_id": "a" * 32,
        "revision": 3,
        "space": "Model",
        "block_path": ["FLANGE"],
        "handles": ["2A"],
        "feature": "bore",
    }
    assert binding.to_dict() == {
        "requirement_id": "D1",
        "status": "bound",
        "refs": [ref.to_dict()],
        "candidates": [ref.to_dict()],
        "reason": "feature tag flange/bore",
    }
    assert measurement.to_dict() == {
        "requirement_id": "D1",
        "value": 40.02,
        "unit": "mm",
        "method": "circle.radius*2",
        "residual": 0.02,
        "status": "pass",
        "detail": {"epsilon": 1e-6},
    }
    assert item.to_dict() == {
        "requirement": _req().to_dict(),
        "binding": binding.to_dict(),
        "measurement": measurement.to_dict(),
        "delta": 0.02,
        "tolerance_basis": "fit H7",
    }
    assert feature.to_dict() == {
        "handle": "2A",
        "type": "circle",
        "space": "Model",
        "layer": "GEOMETRY",
        "block_path": ["FLANGE"],
        "geometry": {"center": [0.0, 0.0], "radius": 20.01},
        "tags": [["ACADMCP_MECH", '{"kind":"feature"}']],
        "approximation": None,
    }
    assert observed.to_dict() == {
        "features": [feature.to_dict()],
        "declared_unit": "mm",
        "inferred_unit": "mm",
        "document_id": "a" * 32,
        "revision": 3,
        "source": "memory:untitled",
    }
    assert json.loads(json.dumps(item.to_dict())) == item.to_dict()
    assert json.loads(json.dumps(observed.to_dict())) == observed.to_dict()
