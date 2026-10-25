"""B15 residue - drawing_preflight validates typed requirements; legacy stays read-only.

Typed items go under ``requirements["items"]``: each is validated with its
path, and the canonical form replaces the input in ``normalized_spec`` so the
spec hash covers what will be verified. A legacy ``dimensions`` object is
still accepted, kept as given and reported under ``legacy`` - and the legacy
spec and its hash are exactly what 1.6 produced (oracle written out below).
"""

from __future__ import annotations

import hashlib
import json

from engineering.preflight import preflight_drawing
from engineering.verification.requirements import parse_requirements

_ITEMS = [
    {
        "id": "D1",
        "quantity": "diameter",
        "target": {"nominal": 40},
        "features": [{"kind": "feature_tag", "part": "flange", "feature": "bore"}],
        "provenance": "explicit",
        "source": {"page": 1, "label": "Ø40"},
    },
    {
        "id": "N1",
        "quantity": "count",
        "target": {"exact": 6},
        "features": [{"kind": "predicate", "type": "circle", "radius_min": 9, "radius_max": 11}],
        "provenance": "explicit",
    },
]
_TYPED = {
    "units": "mm",
    "part_type": "flange",
    "tolerance_policy": "ISO 2768-m",
    "items": _ITEMS,
}


def test_typed_items_make_a_ready_preflight_without_legacy_dimensions():
    result = preflight_drawing("flange", requirements=_TYPED)
    assert result.ready is True
    assert result.questions == [] and result.conflicts == [] and result.legacy == []
    expected = parse_requirements({"units": "mm", "items": _ITEMS})
    assert result.requirement_set == {**expected.to_dict(), "digest": expected.digest()}
    items = result.normalized_spec["requirements"]["items"]
    assert items == expected.to_dict()["items"]
    assert items[0]["target"] == {"nominal": 40.0, "unit": "mm"}, "the set's unit, filled in"
    assert result.to_dict()["requirement_set"]["digest"] == expected.digest()


def test_a_malformed_item_is_a_conflict_named_by_its_path():
    bad = {**_TYPED, "items": [{**_ITEMS[0], "target": {"nominal": -1}}]}
    result = preflight_drawing("flange", requirements=bad)
    assert result.ready is False
    (conflict,) = [c for c in result.conflicts if c.code == "INVALID_REQUIREMENT"]
    assert conflict.field == "items[0].target.nominal"
    assert "must be greater than zero, got -1.0" in conflict.message
    assert result.requirement_set is None


def test_conflicting_sources_are_kept_and_reported_never_collapsed():
    items = [
        {**_ITEMS[0], "source": {"view": "front"}},
        {
            **_ITEMS[0],
            "id": "D2",
            "target": {"nominal": 42},
            "provenance": "conflicting",
            "source": {"view": "section A-A"},
        },
    ]
    result = preflight_drawing("flange", requirements={**_TYPED, "items": items})
    assert result.ready is False
    found = {(c.code, tuple(c.values)) for c in result.conflicts}
    assert ("CONFLICTING_PROVENANCE", ("D2",)) in found
    assert ("CONFLICTING_REQUIREMENT", ("D1", "D2")) in found
    kept = result.requirement_set["items"]
    assert [(item["id"], item["target"]["nominal"]) for item in kept] == [
        ("D1", 40.0),
        ("D2", 42.0),
    ]


def test_items_wait_for_units_and_the_question_asks_for_them():
    no_units = {key: value for key, value in _TYPED.items() if key != "units"}
    result = preflight_drawing("flange", requirements=no_units)
    assert {q.code for q in result.questions} == {"MISSING_UNITS"}
    assert not any(c.code == "INVALID_REQUIREMENT" for c in result.conflicts)
    assert result.requirement_set is None


def test_the_legacy_spec_and_its_hash_are_unchanged():
    result = preflight_drawing(
        "L bracket",
        requirements={
            "units": "MM",
            "part_type": "Bracket",
            "dimensions": {"width": 80, "height": 50},
            "tolerance_policy": "ISO 2768-m",
        },
    )
    expected_spec = {
        "intent": "L bracket",
        "requirements": {
            "dimensions": {"height": 50.0, "width": 80.0},
            "part_type": "bracket",
            "tolerance_policy": "ISO 2768-m",
            "units": "mm",
        },
        "sheet_size": "A3",
        "scale": 1.0,
        "layer_set_id": "mech",
        "view_count": 1,
        "dim_style": "chain",
    }
    text = json.dumps(expected_spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert result.normalized_spec == expected_spec
    assert result.spec_hash == "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert result.ready is True and result.requirement_set is None
    assert [(entry["field"], entry["status"]) for entry in result.legacy] == [
        ("dimensions", "read_only")
    ]
