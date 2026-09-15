# tests/test_block_specs.py
"""block_define request validation: refuse the whole request before any write."""

from __future__ import annotations

import pytest

from backends.block_specs import (
    ALLOWED_TYPES,
    solid_vertices,
    validate_attdef_specs,
    validate_entity_specs,
)


def test_allowed_types_are_the_documented_six():
    assert ALLOWED_TYPES == ("line", "circle", "arc", "polyline", "text", "solid")


def test_empty_entities_is_refused():
    with pytest.raises(ValueError, match="empty"):
        validate_entity_specs([])


def test_each_type_normalises_to_floats_and_layer_zero():
    out = validate_entity_specs(
        [
            {"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0},
            {"type": "circle", "cx": 0, "cy": 0, "r": 2},
            {"type": "arc", "cx": 0, "cy": 0, "r": 2, "start_deg": 0, "end_deg": 180},
            {"type": "polyline", "points": [[0, 0], [1, 0], [1, 1]], "closed": True},
            {"type": "text", "text": "M", "x": 0, "y": 0, "height": 2},
            {"type": "solid", "points": [[0, 0], [1, 0], [0, 1]]},
        ]
    )
    assert [e["type"] for e in out] == list(ALLOWED_TYPES)
    assert out[0] == {"type": "line", "layer": "0", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 0.0}
    assert out[3]["points"] == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    assert out[3]["closed"] is True and out[3]["bulges"] is None
    assert out[4]["align"] == "left" and out[4]["rotation_deg"] == 0.0
    assert out[5]["points"] == [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({"type": "square"}, "type"),
        ({"type": "line", "x1": 0, "y1": 0, "x2": 1}, "y2"),
        ({"type": "circle", "cx": 0, "cy": 0, "r": 0}, "r"),
        ({"type": "circle", "cx": 0, "cy": 0, "r": float("nan")}, "finite"),
        ({"type": "text", "text": "M", "x": 0, "y": 0, "height": -1}, "height"),
        ({"type": "text", "text": 5, "x": 0, "y": 0, "height": 1}, "text"),
        ({"type": "text", "text": "M", "x": 0, "y": 0, "height": 1, "align": "top"}, "align"),
        ({"type": "polyline", "points": [[0, 0]]}, "points"),
        ({"type": "polyline", "points": [[0, 0], [1]]}, "points[1]"),
        ({"type": "polyline", "points": [[0, 0], [1, 1]], "bulges": [0.5]}, "bulges"),
        ({"type": "polyline", "points": [[0, 0], [1, 1]], "closed": "yes"}, "closed"),
        ({"type": "solid", "points": [[0, 0], [1, 0]]}, "points"),
        ({"type": "solid", "points": [[0, 0], [1, 0], [1, 1], [0, 1], [2, 2]]}, "at most 4"),
        ({"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0, "layer": ""}, "layer"),
        ({"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0, "layer": "BAD<LAYER"}, "layer"),
        ({"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0, "layer": "A;B"}, "layer"),
        ({"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0, "layer": "A\x01B"}, "layer"),
        ({"type": "line", "x1": True, "y1": 0, "x2": 1, "y2": 0}, "x1"),
    ],
)
def test_bad_entity_names_the_index_and_key(bad, fragment):
    good = {"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0}
    with pytest.raises(TypeError, match=r"entities\[1\]") as exc:
        validate_entity_specs([good, bad])
    assert fragment in str(exc.value)


def test_illegal_layer_name_is_refused_before_any_write():
    """ezdxf would raise DXFValueError on this layer mid-loop, after earlier
    primitives (and, with overwrite, the old definition) were already written.
    The gate has to be ours, typed, and indexed."""
    with pytest.raises(TypeError, match=r"entities\[0\].*'layer'") as exc:
        validate_entity_specs(
            [{"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0, "layer": "BAD<LAYER"}]
        )
    assert "'<'" in str(exc.value)
    # Legal names, including the ones with spaces and dots, still pass.
    out = validate_entity_specs(
        [{"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0, "layer": " PID.Valves-1 "}]
    )
    assert out[0]["layer"] == "PID.Valves-1"


def test_attdefs_normalise_and_default():
    out = validate_attdef_specs([{"tag": "TAG", "x": 0, "y": 3, "height": 2.5}])
    assert out == [
        {
            "tag": "TAG",
            "prompt": "TAG",
            "default": "",
            "x": 0.0,
            "y": 3.0,
            "height": 2.5,
            "rotation_deg": 0.0,
            "align": "left",
            "invisible": False,
        }
    ]


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({"tag": "tag", "x": 0, "y": 0, "height": 1}, "tag"),
        ({"tag": "T-1", "x": 0, "y": 0, "height": 1}, "tag"),
        ({"tag": "TAG\n", "x": 0, "y": 0, "height": 1}, "tag"),
        ({"tag": "TAG", "x": 0, "y": 0, "height": 0}, "height"),
        ({"tag": "TAG", "x": 0, "y": 0, "height": 1, "invisible": 1}, "invisible"),
        ({"tag": "TAG", "x": 0, "y": 0, "height": 1, "default": 3}, "default"),
    ],
)
def test_bad_attdef_names_the_index_and_key(bad, fragment):
    with pytest.raises(TypeError, match=r"attdefs\[0\]") as exc:
        validate_attdef_specs([bad])
    assert fragment in str(exc.value)


def test_duplicate_attdef_tags_are_refused():
    with pytest.raises(TypeError, match="duplicate"):
        validate_attdef_specs(
            [
                {"tag": "TAG", "x": 0, "y": 0, "height": 1},
                {"tag": "TAG", "x": 0, "y": 5, "height": 1},
            ]
        )


def test_trailing_newline_cannot_smuggle_a_duplicate_tag():
    """`$` matches before a trailing newline; ezdxf strips it on write, so
    'TAG\n' + 'TAG' would land as two ATTDEFs with the same tag and the
    attribute readers (dict keyed by tag) would silently drop one."""
    with pytest.raises(TypeError, match=r"attdefs\[0\].*'tag'"):
        validate_attdef_specs(
            [
                {"tag": "TAG\n", "x": 0, "y": 0, "height": 1},
                {"tag": "TAG", "x": 0, "y": 0, "height": 1},
            ]
        )


def test_solid_vertices_use_dxf_bowtie_order():
    quad = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert solid_vertices(quad) == [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    tri = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    assert solid_vertices(tri) == tri
