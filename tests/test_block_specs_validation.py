"""B07 on ``block_define`` / attribute values: backlog rows W1-T1-05, W1-T2-03,
W1-T2-05, W1-T2-06, W1-T2-07, W1-T2-08 and W2-T3-07."""

from __future__ import annotations

import pytest

from backends.block_specs import (
    validate_attdef_specs,
    validate_attribute_values,
    validate_block_name,
    validate_entity_specs,
)

pytestmark = pytest.mark.asyncio

LINE = {"type": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0}
TAG = {"tag": "TAG", "x": 0, "y": 0, "height": 2.5, "default": "DEF"}


# ── W1-T2-05: a non-list is not "empty" ─────────────────────────────────────


@pytest.mark.parametrize(
    "entities, kind",
    [({"type": "line"}, "dict"), ("abc", "str"), ((n for n in ()), "generator")],
)
async def test_a_non_list_entities_is_refused_for_what_it_is(entities, kind):
    """RED: all three were refused as "'entities' is empty"."""
    with pytest.raises(TypeError, match=f"'entities' must be a list of objects, got {kind}$"):
        validate_entity_specs(entities)


async def test_an_empty_list_is_still_refused_as_empty():
    with pytest.raises(ValueError, match="'entities' is empty"):
        validate_entity_specs([])


# ── W1-T2-03: the per-entity override keys are refused, never dropped ───────


@pytest.mark.parametrize("extra", [{"color": 1}, {"linetype": "DASHED"}, {"lineweight": 0.35}])
async def test_an_override_key_is_refused_by_index(extra):
    """RED: 'color' was accepted and silently dropped from the normalised spec."""
    key = next(iter(extra))
    with pytest.raises(
        TypeError, match=rf"^entities\[1\]: unknown key\(s\) '{key}'; a line takes "
    ):
        validate_entity_specs([LINE, {**LINE, **extra}])


async def test_each_type_keeps_its_own_optional_keys():
    specs = validate_entity_specs(
        [
            {"type": "polyline", "points": [[0, 0], [1, 1]], "closed": False, "bulges": [0, 0]},
            {"type": "text", "text": "A", "x": 0, "y": 0, "height": 2, "rotation_deg": 90},
            {**LINE, "layer": "GEOMETRY"},
        ]
    )
    assert [s["type"] for s in specs] == ["polyline", "text", "line"]


async def test_an_unknown_attdef_key_is_refused():
    with pytest.raises(TypeError, match=r"^attdefs\[0\]: unknown key\(s\) 'colour'; an ATTDEF"):
        validate_attdef_specs([{**TAG, "colour": 3}])


# ── W1-T2-08: 'rotation' on a line ──────────────────────────────────────────


async def test_rotation_on_a_line_is_refused_not_dropped():
    """RED: {'type': 'line', ..., 'rotation': 45} was accepted, rotation dropped."""
    with pytest.raises(
        TypeError, match=r"^entities\[0\]: unknown key\(s\) 'rotation'; a line takes "
    ):
        validate_entity_specs([{**LINE, "rotation": 45}])


# ── W1-T2-06: the 255-character layer name rule ─────────────────────────────


async def test_a_layer_name_over_255_characters_is_refused():
    """RED: a 300-character layer was accepted."""
    with pytest.raises(
        TypeError, match=r"^entities\[0\]: 'layer' is 300 characters; .* at most 255"
    ):
        validate_entity_specs([{**LINE, "layer": "L" * 300}])
    assert validate_entity_specs([{**LINE, "layer": "L" * 255}])[0]["layer"] == "L" * 255


# ── W1-T2-07: refusals do not echo megabytes ───────────────────────────────


async def test_a_refusal_never_echoes_an_unbounded_value():
    """RED: the message was 2,000,042 characters long."""
    with pytest.raises(TypeError) as excinfo:
        validate_entity_specs([{**LINE, "x1": "x" * 2_000_000}])
    message = str(excinfo.value)
    assert message.startswith("entities[0]: 'x1' must be a number, got 'xxx")
    assert len(message) < 200


# ── W1-T1-05: attribute tags are not case-sensitive ─────────────────────────


async def test_attribute_tags_are_matched_upper_case():
    assert validate_attribute_values({"tag": "X1", "Desc": 5}) == {"TAG": "X1", "DESC": "5"}
    with pytest.raises(TypeError, match="name the same attribute"):
        validate_attribute_values({"tag": "a", "TAG": "b"})


async def test_a_lower_case_tag_reaches_its_attdef(backend):
    """RED: {'tag': 'X1'} was dropped silently; the ATTRIB kept its default 'DEF'."""
    await backend.block_define("B1", [LINE], [TAG])
    ref = await backend.block_insert("B1", 0, 0, attributes={"tag": "X1"})
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "X1"}


# ── W2-T3-07: attdefs {} / '' and a non-string name ─────────────────────────


@pytest.mark.parametrize("attdefs", [{}, ""])
async def test_falsy_non_list_attdefs_are_refused(backend, attdefs):
    """RED: `attdefs or []` turned {} and '' into "no ATTDEFs"."""
    with pytest.raises(TypeError, match="'attdefs' must be a list"):
        await backend.block_define("B2", [LINE], attdefs)


async def test_a_non_string_block_name_is_refused(backend):
    """RED: block_define(123, ...) died with AttributeError: 'int' has no 'strip'."""
    with pytest.raises(TypeError, match="^block_define: 'name' must be a string, got int$"):
        await backend.block_define(123, [LINE])
    with pytest.raises(TypeError, match="got NoneType"):
        validate_block_name(None)
    assert validate_block_name("B3") == "B3"


async def test_a_foreign_lower_case_attdef_still_takes_its_value(backend):
    """The fold is applied to the drawing's tags too: a DXF written by another
    tool may carry a lower-case ATTDEF tag, which an exact match against the
    upper-cased request would now miss on insert and on set."""
    block = backend._doc.blocks.new("FOREIGN")
    block.add_line((0, 0), (10, 0))
    block.add_attdef("note", (0, 0), dxfattribs={"height": 2.5, "text": "DEF"})
    ref = await backend.block_insert("FOREIGN", 0, 0, attributes={"Note": "N1"})
    assert await backend.block_get_attributes(ref.handle) == {"note": "N1"}
    result = await backend.block_set_attributes(ref.handle, {"NOTE": "N2"})
    assert result["updated_tags"] == ["note"]
    assert await backend.block_get_attributes(ref.handle) == {"note": "N2"}
