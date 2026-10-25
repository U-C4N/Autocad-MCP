"""B15 residue - typed requirements: parsed once, refused by their path, conflicts kept.

Every expected value is written out by hand; nothing the parser returns is
used to build its own oracle.
"""

from __future__ import annotations

import pytest

import config
from engineering.validation import SpecError
from engineering.verification.requirements import (
    CHUNK_SIZE,
    decode_requirements,
    encode_requirements,
    parse_requirements,
    requirement_conflicts,
)
from engineering.verification.types import FeatureSelector, Requirement, freeze, thaw


def _item(**changes) -> dict:
    item = {
        "id": "D1",
        "quantity": "diameter",
        "target": {"nominal": 40},
        "features": [{"kind": "feature_tag", "part": "flange", "feature": "bore"}],
        "provenance": "explicit",
    }
    item.update(changes)
    return item


def test_a_typed_set_is_canonicalised():
    rs = parse_requirements(
        {
            "units": "Millimetre",
            "items": [
                _item(
                    id="N1",
                    quantity="count",
                    target={"exact": 6},
                    features=[
                        {
                            "kind": "predicate",
                            "type": "circle",
                            "radius_min": 9,
                            "radius_max": 11,
                            "region": [100, 50, -100, -50],
                        }
                    ],
                ),
                _item(
                    target={"nominal": 40, "unit": "inch"},
                    features=[{"kind": "handle", "handle": "2a"}],
                    source={"page": 2, "label": "Ø40"},
                    manufacturing_tolerance={"fit": "H7", "source": "drawing note 3"},
                    verification_epsilon=0.001,
                ),
            ],
        }
    )
    assert rs.units == "mm"
    assert [r.id for r in rs.requirements] == ["D1", "N1"], "sorted by id"
    d1, n1 = rs.requirements
    assert d1 == Requirement(
        id="D1",
        quantity="diameter",
        target=freeze({"nominal": 40.0, "unit": "in"}),
        features=(FeatureSelector("handle", freeze({"handle": "2A"})),),
        provenance="explicit",
        source=freeze({"label": "Ø40", "page": 2}),
        manufacturing_tolerance=freeze({"fit": "H7", "source": "drawing note 3"}),
        verification_epsilon=0.001,
    )
    assert thaw(n1.target) == {"exact": 6}
    assert thaw(n1.features[0].value) == {
        "type": "circle",
        "radius_min": 9.0,
        "radius_max": 11.0,
        "region": [-100.0, -50.0, 100.0, 50.0],
    }
    assert parse_requirements(rs.to_dict()) == rs, "the canonical form parses back to itself"


@pytest.mark.parametrize(
    "items, message",
    [
        (
            [_item(target={"nominal": -1})],
            r"items\[0\]\.target\.nominal: must be greater than zero, got -1\.0",
        ),
        (
            [
                _item(
                    quantity="distance",
                    target={"nominal": float("nan")},
                    features=[
                        {"kind": "handle", "handle": "1A"},
                        {"kind": "handle", "handle": "1B"},
                    ],
                )
            ],
            r"items\[0\]\.target\.nominal: nan is not a finite number",
        ),
        ([_item(quantity="volume")], r"items\[0\]\.quantity: must be one of"),
        (
            [{key: value for key, value in _item().items() if key != "provenance"}],
            r"items\[0\]\.provenance: required",
        ),
        (
            [_item(quantity="count", target={"exact": 6, "min": 2})],
            r"items\[0\]\.target: 'exact' excludes 'min' and 'max'",
        ),
        (
            [_item(quantity="concentric", target=None)],
            r"items\[0\]\.features: a concentric takes 2 selector\(s\), got 1",
        ),
        (
            [_item(manufacturing_tolerance={"upper": 0.02, "lower": 0.0})],
            r"items\[0\]\.manufacturing_tolerance\.source: a manufacturing tolerance names its own",
        ),
        ([_item(), _item()], r"items\[1\]\.id: 'D1' is already items\[0\]\.id"),
        (
            [
                _item(
                    features=[
                        {"kind": "feature_tag", "part": "flange", "feature": "bore", "colour": 1}
                    ]
                )
            ],
            r"items\[0\]\.features\[0\]: unknown key\(s\) \['colour'\]",
        ),
        (
            [_item(features=[{"kind": "handle", "handle": "XYZ"}])],
            r"items\[0\]\.features\[0\]\.handle: a handle is hexadecimal",
        ),
        (
            [_item(quantity="horizontal", target={"nominal": 0})],
            r"items\[0\]\.target: a horizontal requirement takes no target",
        ),
    ],
)
def test_a_malformed_item_is_refused_by_its_path(items, message):
    with pytest.raises(SpecError, match=message):
        parse_requirements({"units": "mm", "items": items})


def test_the_selector_vocabulary_is_the_binders():
    """Task 14's binder reads exactly these keys; the parser passes them through."""
    rs = parse_requirements(
        {
            "units": "mm",
            "items": [
                _item(features=[{"kind": "handle", "handle": "2a", "block_path": ["1f", "20"]}]),
                _item(
                    id="D2",
                    features=[
                        {
                            "kind": "feature_tag",
                            "part": "flange",
                            "feature": "bore",
                            "view": "front",
                            "type": "circle",
                            "fallback": {"type": "circle", "radius_min": 19.9, "radius_max": 20.1},
                        }
                    ],
                ),
                _item(
                    id="D3",
                    features=[{"kind": "arch_element", "element": "door:D1", "type": "arc"}],
                ),
                _item(
                    id="L1",
                    quantity="distance",
                    target={"nominal": 100},
                    features=[
                        {
                            "kind": "predicate",
                            "type": "line",
                            "layer": "GEOMETRY",
                            "space": "Model",
                            "length_min": 99,
                            "length_max": 101,
                        }
                    ],
                ),
            ],
        }
    )
    d1, d2, d3, l1 = rs.requirements
    assert thaw(d1.features[0].value) == {"handle": "2A", "block_path": ["1F", "20"]}
    assert thaw(d2.features[0].value)["fallback"] == {
        "type": "circle",
        "radius_min": 19.9,
        "radius_max": 20.1,
    }
    assert thaw(d3.features[0].value) == {"element": "door:D1", "type": "arc"}
    assert thaw(l1.features[0].value) == {
        "type": "line",
        "layer": "GEOMETRY",
        "space": "Model",
        "length_min": 99.0,
        "length_max": 101.0,
    }
    with pytest.raises(
        SpecError,
        match=r"items\[0\]\.features\[0\]: radius_min 2\.0 is greater than radius_max 1\.0",
    ):
        parse_requirements(
            {
                "units": "mm",
                "items": [
                    _item(
                        features=[
                            {
                                "kind": "predicate",
                                "type": "circle",
                                "radius_min": 2,
                                "radius_max": 1,
                            }
                        ]
                    )
                ],
            }
        )


def test_units_are_required():
    with pytest.raises(SpecError, match=r"units: required"):
        parse_requirements({"items": [_item()]})


def test_nothing_to_read_is_refused():
    with pytest.raises(SpecError, match="nothing to read"):
        parse_requirements({"units": "mm"})


def test_the_item_count_is_budgeted_by_max_spec_items(monkeypatch):
    monkeypatch.setattr(config.settings, "max_spec_items", 2)
    items = [_item(id=f"D{index}") for index in range(3)]
    with pytest.raises(SpecError, match=r"items: 3 is over the limit of 2; set MAX_SPEC_ITEMS"):
        parse_requirements({"units": "mm", "items": items})


def test_a_legacy_dimensions_object_is_kept_as_given():
    rs = parse_requirements(
        {"units": "mm", "dimensions": {"width": 80, "holes": {"count": 6, "pcd": 120}}}
    )
    assert rs.requirements == ()
    assert thaw(rs.legacy) == {"width": 80, "holes": {"count": 6, "pcd": 120}}


def test_conflicts_are_reported_and_both_requirements_kept():
    rs = parse_requirements(
        {
            "units": "mm",
            "items": [_item(), _item(id="D2", target={"nominal": 42}, provenance="conflicting")],
        }
    )
    assert [r.id for r in rs.requirements] == ["D1", "D2"]
    assert [thaw(r.target)["nominal"] for r in rs.requirements] == [40.0, 42.0]
    assert [(c["code"], c["values"]) for c in requirement_conflicts(rs)] == [
        ("CONFLICTING_PROVENANCE", ["D2"]),
        ("CONFLICTING_REQUIREMENT", ["D1", "D2"]),
    ]


def test_the_stored_form_is_ascii_chunks_and_round_trips():
    items = [_item(id=f"D{index}", source={"label": "Ø40 " * 10}) for index in range(10)]
    rs = parse_requirements({"units": "mm", "items": items})
    chunks = encode_requirements(rs)
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_SIZE and chunk.isascii() for chunk in chunks)
    assert decode_requirements(chunks) == rs


def test_an_edited_record_is_refused_by_its_digest():
    rs = parse_requirements({"units": "mm", "items": [_item()]})
    text = "".join(encode_requirements(rs))
    assert '"nominal":40.0' in text
    with pytest.raises(ValueError, match="does not match"):
        decode_requirements([text.replace('"nominal":40.0', '"nominal":42.0')])
