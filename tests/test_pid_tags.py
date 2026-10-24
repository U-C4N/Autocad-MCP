"""ISA-5.1 identification-letter grammar, pinned against a reader-checkable fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engineering.pid.tags import (
    DEFAULT_EQUIPMENT_PREFIXES,
    describe_tables,
    parse_tag,
    split_instrument_tag,
)

FIXTURE = json.loads((Path(__file__).parent / "data" / "isa51_tags.json").read_text("utf-8"))


@pytest.mark.parametrize("tag, description, loop", FIXTURE["valid"])
def test_valid_tags_parse_with_the_standard_description(tag, description, loop):
    out = parse_tag(tag)
    assert out["valid"] is True, out["errors"]
    assert out["kind"] == "instrument"
    assert out["description"] == description
    assert out["loop"] == loop
    assert out["user_defined"] is False


@pytest.mark.parametrize("tag, description, loop", FIXTURE["user_defined"])
def test_user_choice_letters_parse_as_user_defined(tag, description, loop):
    out = parse_tag(tag)
    assert out["valid"] is True and out["user_defined"] is True
    assert out["description"] == description


@pytest.mark.parametrize("tag, fragment", FIXTURE["invalid"])
def test_invalid_tags_name_the_offending_letter(tag, fragment):
    out = parse_tag(tag, kind="instrument")
    assert out["valid"] is False
    assert out["tag"] == tag
    # The grammar stops at the first offending letter, so the fixture pins
    # exactly one error and the fragment must sit in *that* one.
    assert len(out["errors"]) == 1, out["errors"]
    assert fragment in out["errors"][0], out["errors"]


@pytest.mark.parametrize("tag, fragment", FIXTURE["invalid"])
def test_invalid_fixture_fragments_bind_the_letter_and_position(tag, fragment):
    # Every grammar error is prefixed with the tag's own letters, so a fragment
    # that is a substring of the tag ("I" for FCI) matches whatever the grammar
    # says and pins nothing. A row must quote the rule text, and a grammar row
    # must quote the reported position as well as the letter.
    assert fragment not in tag.upper(), f"{tag!r}: fragment {fragment!r} is part of the tag"
    if "format" not in fragment:
        assert "position" in fragment or "letter" in fragment or "trailing" in fragment, (
            f"{tag!r}: fragment {fragment!r} names neither a letter nor a position"
        )


@pytest.mark.parametrize("tag, area, letters, loop, suffix", FIXTURE["formats"])
def test_tag_formats(tag, area, letters, loop, suffix):
    out = parse_tag(tag)
    assert (out["area"], out["letters"], out["loop"], out["suffix"]) == (
        area,
        letters,
        loop,
        suffix,
    )


@pytest.mark.parametrize("tag, kind, number, suffix", FIXTURE["equipment"])
def test_equipment_tags(tag, kind, number, suffix):
    out = parse_tag(tag)
    assert out["kind"] == "equipment" and out["valid"] is True
    assert out["equipment_kind"] == kind and out["number"] == number and out["suffix"] == suffix


def test_equipment_prefixes_can_be_replaced_per_call():
    out = parse_tag("Q-1", kind="equipment", equipment_prefixes={"Q": "quench tower"})
    assert out["valid"] is True and out["equipment_kind"] == "quench tower"
    default = parse_tag("Q-1", kind="equipment")
    assert default["valid"] is False and "prefix" in default["errors"][0]
    assert DEFAULT_EQUIPMENT_PREFIXES["P"] == "pump"


def test_auto_kind_prefers_instrument_then_equipment_then_reports_instrument_errors():
    assert parse_tag("PT-1")["kind"] == "instrument"
    assert parse_tag("P-1")["kind"] == "equipment"
    bad = parse_tag("FCI-1")
    assert bad["kind"] == "instrument" and bad["valid"] is False


def test_split_instrument_tag_feeds_func_and_loop_attributes():
    assert split_instrument_tag("10-FIC-101A") == ("FIC", "101A")
    assert split_instrument_tag("PT 7") == ("PT", "7")
    assert split_instrument_tag("garbage") == ("GARBAGE", "")


def test_describe_tables_is_json_serialisable_and_names_every_first_letter():
    tables = describe_tables()
    json.dumps(tables)
    assert set(tables["first_letters"]) == set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


# ── backlog W1-T6-04 / -05 / -06 (1.7.0 triage, Task 8) ─────────────────────


def test_an_unknown_letter_after_a_valid_trailing_is_the_one_named():
    """W1-T6-04 — RED: LAHX reported H (a valid trailing high), not the X after it."""
    out = parse_tag("LAHX-1", kind="instrument")
    assert out["errors"] == ["LAHX: unexpected letter X at position 4"]
    out = parse_tag("PSHHX-1", kind="instrument")
    assert out["errors"] == ["PSHHX: unexpected letter X at position 5"]


def test_without_a_valid_trailing_prefix_the_first_trailing_letter_is_named():
    assert parse_tag("FQQ-1", kind="instrument")["errors"] == [
        "FQQ: unexpected letter Q at position 3"
    ]


@pytest.mark.parametrize(
    "tag, role, letter",
    [
        ("TKC-1", "modifier", "K"),  # rate of change: K before an output
        ("FK-1", "output", "K"),  # control station: K as the output
        ("KIC-1", "first", "K"),  # time: K as the first letter
    ],
)
def test_the_three_roles_of_k(tag, role, letter):
    """W1-T6-06: K is a first letter, a modifier and an output; each is pinned."""
    out = parse_tag(tag)
    assert out["valid"] is True, out["errors"]
    value = out[role]
    letters = [v["letter"] for v in value] if isinstance(value, list) else [value["letter"]]
    assert letter in letters


@pytest.mark.parametrize("tag, letter", [("LB-1", "B"), ("LN-1", "N"), ("LX-1", "X")])
def test_user_choice_readouts_are_recorded_as_readouts(tag, letter):
    out = parse_tag(tag)
    assert out["valid"] is True and out["user_defined"] is True
    assert out["readout"] == [{"letter": letter, "meaning": None}]
    assert out["output"] == []


def test_an_empty_equipment_prefix_map_replaces_the_default():
    """W1-T6-05 / W4-T19-05 — RED: {} fell back to the default map (`or`)."""
    out = parse_tag("P-101", kind="equipment", equipment_prefixes={})
    assert out["valid"] is False
    assert out["errors"] == ["'P-101': prefix P is not in the equipment prefix map"]
    assert parse_tag("P-101", kind="equipment")["equipment_kind"] == "pump"


@pytest.mark.parametrize(
    "prefixes, fragment",
    [
        ({"P": 5}, "equipment_prefixes['P']: expected text naming the equipment kind, got int"),
        ({"P": ""}, "equipment_prefixes['P']: expected text naming the equipment kind, got ''"),
        ({5: "pump"}, "equipment_prefixes: a prefix must be text, got 5"),
        ({"": "pump"}, "equipment_prefixes: a prefix must be text, got ''"),
        (["P"], "equipment_prefixes: expected a mapping of prefix to kind, got list"),
    ],
)
def test_a_malformed_equipment_prefix_map_is_refused_by_path(prefixes, fragment):
    """W1-T6-05 — RED: {'P': 5} answered equipment_kind 5."""
    with pytest.raises(ValueError) as excinfo:
        parse_tag("P-101", kind="equipment", equipment_prefixes=prefixes)
    assert str(excinfo.value) == fragment
