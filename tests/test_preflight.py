from __future__ import annotations

import pytest

from engineering.preflight import preflight_drawing


def _complete_requirements() -> dict:
    return {
        "units": "mm",
        "part_type": "bracket",
        "dimensions": {"width": 80.0, "height": 50.0, "thickness": 8.0},
        "tolerance_policy": "ISO 2768-m",
    }


def test_preflight_missing_production_fields_is_not_ready():
    result = preflight_drawing("L bracket", requirements={})

    assert result.ready is False
    assert {q.code for q in result.questions} == {
        "MISSING_UNITS",
        "MISSING_PART_TYPE",
        "MISSING_DIMENSIONS",
        "MISSING_TOLERANCE_POLICY",
    }
    assert result.spec_hash.startswith("sha256:")


def test_preflight_normalizes_complete_spec_and_hash_is_deterministic():
    first = preflight_drawing(
        "  L bracket  ",
        requirements=_complete_requirements(),
        sheet_size="a3",
        layer_set_id="MECH",
    )
    second = preflight_drawing(
        "L bracket",
        requirements={
            "tolerance_policy": "ISO 2768-m",
            "dimensions": {"thickness": 8, "height": 50, "width": 80},
            "part_type": "bracket",
            "units": "MM",
        },
        sheet_size="A3",
        layer_set_id="mech",
    )

    assert first.ready is True
    assert first.normalized_spec["requirements"]["units"] == "mm"
    assert first.spec_hash == second.spec_hash


def test_preflight_question_text_is_english():
    """drawing_preflight returns this text to every user of a public English
    package; it shipped in Turkish through v1.4."""
    result = preflight_drawing("L bracket", requirements={})
    by_code = {q.code: q.question for q in result.questions}

    assert by_code["MISSING_UNITS"] == "What drawing units should be used?"
    assert by_code["MISSING_PART_TYPE"] == "What kind of part or drawing is being produced?"
    assert by_code["MISSING_DIMENSIONS"] == "What are the main production dimensions?"
    assert by_code["MISSING_TOLERANCE_POLICY"] == "Which tolerance policy should apply?"


def test_preflight_conflict_message_is_english():
    requirements = _complete_requirements()
    requirements["constraints"] = [
        {"field": "dimensions.width", "value": 80},
        {"field": "dimensions.width", "value": 90},
    ]

    result = preflight_drawing("L bracket", requirements=requirements)

    assert result.conflicts[0].message == ("Conflicting values were supplied for dimensions.width.")


def test_preflight_user_facing_text_is_ascii():
    """Guard against any future non-English string leaking back into the tool
    surface — codes and field names stay machine-readable, prose stays ASCII."""
    requirements = _complete_requirements()
    requirements["constraints"] = [
        {"field": "dimensions.width", "value": 80},
        {"field": "dimensions.width", "value": 90},
    ]
    empty = preflight_drawing("L bracket", requirements={})
    conflicting = preflight_drawing("L bracket", requirements=requirements)

    prose = [q.question for q in empty.questions]
    prose += [choice for q in empty.questions for choice in q.choices]
    prose += [c.message for c in conflicting.conflicts]

    assert prose, "expected some user-facing text to check"
    for text in prose:
        assert text.isascii(), f"non-ASCII user-facing text: {text!r}"


def test_preflight_detects_conflicting_constraints():
    requirements = _complete_requirements()
    requirements["constraints"] = [
        {"field": "dimensions.width", "value": 80},
        {"field": "dimensions.width", "value": 90},
    ]

    result = preflight_drawing("L bracket", requirements=requirements)

    assert result.ready is False
    assert result.conflicts[0].code == "CONFLICTING_REQUIREMENT"
    assert result.conflicts[0].field == "dimensions.width"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"sheet_size": "LETTER"}, "INVALID_SHEET_SIZE"),
        ({"scale": 0}, "INVALID_SCALE"),
        ({"view_count": 0}, "INVALID_VIEW_COUNT"),
        ({"dim_style": "guess"}, "INVALID_DIM_STYLE"),
        ({"layer_set_id": "custom"}, "INVALID_LAYER_SET"),
    ],
)
def test_preflight_rejects_invalid_planning_parameters(kwargs, code):
    result = preflight_drawing(
        "L bracket",
        requirements=_complete_requirements(),
        **kwargs,
    )

    assert result.ready is False
    assert code in {conflict.code for conflict in result.conflicts}


def test_preflight_does_not_invent_values_when_assumptions_disabled():
    result = preflight_drawing(
        "L bracket",
        requirements={"part_type": "bracket"},
        allow_assumptions=False,
    )

    assert result.assumptions == []
    assert "units" not in result.normalized_spec["requirements"]


@pytest.mark.asyncio
async def test_backend_plan_accepts_matching_ready_preflight(backend):
    preflight = await backend.drawing_preflight(
        "L bracket",
        requirements=_complete_requirements(),
    )

    plan = await backend.drawing_plan(
        "L bracket",
        requirements=preflight.normalized_spec["requirements"],
        spec_hash=preflight.spec_hash,
    )

    assert plan.preflight_status == "ready"
    assert plan.spec_hash == preflight.spec_hash
    assert plan.requirements["units"] == "mm"


@pytest.mark.asyncio
async def test_backend_plan_rejects_not_ready_preflight_hash(backend):
    preflight = await backend.drawing_preflight("L bracket", requirements={})

    with pytest.raises(RuntimeError, match="not ready"):
        await backend.drawing_plan("L bracket", spec_hash=preflight.spec_hash)


@pytest.mark.asyncio
async def test_backend_plan_rejects_fields_that_do_not_match_preflight_hash(backend):
    preflight = await backend.drawing_preflight(
        "L bracket",
        requirements=_complete_requirements(),
    )

    with pytest.raises(RuntimeError, match="do not match"):
        await backend.drawing_plan(
            "Different part",
            requirements=preflight.normalized_spec["requirements"],
            spec_hash=preflight.spec_hash,
        )


@pytest.mark.asyncio
async def test_legacy_plan_remains_compatible_and_marks_preflight_skipped(backend):
    plan = await backend.drawing_plan("Legacy drawing")

    assert plan.preflight_status == "skipped"
    assert plan.spec_hash is None
    assert plan.requirements == {}


@pytest.mark.asyncio
async def test_document_change_clears_plan_preflight_and_gdt_state(backend):
    preflight = await backend.drawing_preflight(
        "First drawing",
        requirements=_complete_requirements(),
    )
    await backend.drawing_plan(
        "First drawing",
        requirements=preflight.normalized_spec["requirements"],
        spec_hash=preflight.spec_hash,
    )
    backend._gdt_datums_defined = {"A"}
    backend._gdt_datums_referenced = {"A", "B"}

    await backend.drawing_new()

    assert backend.get_plan_spec() is None
    assert backend._preflight_result is None
    assert backend._gdt_datums_defined == set()
    assert backend._gdt_datums_referenced == set()
