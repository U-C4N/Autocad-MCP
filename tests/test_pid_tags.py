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
    assert any(fragment in err for err in out["errors"]), out["errors"]


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
