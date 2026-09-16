# tests/test_standards_presets.py
"""Provenance for the authored style presets, pinned by name.

Every ISO-25 and ANSI value is asserted against the table in spec §4.1 — a
reader with ISO 129-1 / ISO 3098-1 / ASME Y14.2 open can check each line.
The refusals are pinned too: a value the standards data lets through is a
value a backend writes, so the data is where "nothing written on refusal"
starts.
"""

from __future__ import annotations

import pytest

from engineering.standards.dimstyles import (
    DIM_VARIABLE_RANGES,
    DIM_VARIABLE_WHITELIST,
    LINEWEIGHT_CODES,
    PRESET_VARIABLES,
    PRESETS,
    check_dim_value,
    describe_preset,
    resolve_dimstyle,
    validate_overrides,
)
from engineering.standards.mleaderstyles import (
    MLEADER_KEYS,
    MLEADER_PRESETS,
    resolve_mleaderstyle,
)
from engineering.standards.textstyles import TEXT_PRESETS, resolve_font, validate_textstyle

# ── ISO-25: ISO 129-1:2018 at the 2.5 mm row of ISO 3098-1 ──────────────────

ISO_25_EXPECTED = {
    "DIMTXT": 2.5,
    "DIMASZ": 2.5,
    "DIMEXE": 1.25,
    "DIMEXO": 0.625,
    "DIMGAP": 0.625,
    "DIMTAD": 1,
    "DIMTIH": 0,
    "DIMTOH": 0,
    "DIMDEC": 2,
    "DIMDSEP": ",",
    "DIMLUNIT": 2,
    "DIMZIN": 8,
    "DIMBLK": "",
    "DIMTXSTY": "ISOCP",
    "DIMLWD": -2,
    "DIMLWE": -2,
    "DIMSCALE": 1.0,
}

# ── ANSI: ASME Y14.2-2014, metric values ────────────────────────────────────

ANSI_EXPECTED = {
    "DIMTXT": 3.0,
    "DIMASZ": 3.0,
    "DIMEXE": 1.5,
    "DIMEXO": 1.5,
    "DIMGAP": 1.0,
    "DIMTAD": 0,
    "DIMTIH": 1,
    "DIMTOH": 1,
    "DIMDEC": 2,
    "DIMDSEP": ".",
    "DIMLUNIT": 2,
    "DIMZIN": 8,
    "DIMBLK": "",
    "DIMTXSTY": "ROMANS",
    "DIMLWD": -2,
    "DIMLWE": -2,
    "DIMSCALE": 1.0,
}


@pytest.mark.parametrize(("variable", "expected"), sorted(ISO_25_EXPECTED.items()))
def test_iso25_value_by_name(variable, expected):
    assert PRESETS["iso-25"][variable] == expected
    assert type(PRESETS["iso-25"][variable]) is type(expected)


@pytest.mark.parametrize(("variable", "expected"), sorted(ANSI_EXPECTED.items()))
def test_ansi_value_by_name(variable, expected):
    assert PRESETS["ansi"][variable] == expected
    assert type(PRESETS["ansi"][variable]) is type(expected)


def test_presets_set_exactly_the_seventeen_spec_variables():
    assert len(PRESET_VARIABLES) == 17
    for name, preset in PRESETS.items():
        assert tuple(preset) == PRESET_VARIABLES, name


def test_the_whitelist_is_the_seventeen_plus_the_sixteen_spec_extras():
    extras = set(
        "DIMTOL DIMTP DIMTM DIMTOLJ DIMTFAC DIMLFAC DIMRND DIMATFIT "
        "DIMTMOVE DIMCLRD DIMCLRE DIMCLRT DIMSAH DIMBLK1 DIMBLK2 DIMCEN".split()
    )
    assert DIM_VARIABLE_WHITELIST == set(PRESET_VARIABLES) | extras
    assert set(DIM_VARIABLE_RANGES) == DIM_VARIABLE_WHITELIST


def test_iso_lineweights_are_all_legal_codes():
    for mm in (0.13, 0.18, 0.25, 0.35, 0.5, 0.7, 1.0, 1.4, 2.0):
        assert round(mm * 100) in LINEWEIGHT_CODES


def test_describe_preset_names_the_standard():
    iso = describe_preset("ISO-25")
    assert iso["name"] == "iso-25"
    assert "ISO 129-1" in iso["source"] and "ISO 3098-1" in iso["source"]
    assert iso["values"] == ISO_25_EXPECTED and iso["textstyle"] == "ISOCP"
    ansi = describe_preset("ansi")
    assert "ASME Y14.2" in ansi["source"] and ansi["textstyle"] == "ROMANS"
    with pytest.raises(ValueError, match="preset"):
        describe_preset("din")


# ── resolve_dimstyle ────────────────────────────────────────────────────────


def test_none_preset_is_the_iso25_base():
    assert resolve_dimstyle(None, None) == ISO_25_EXPECTED
    assert resolve_dimstyle("ISO-25", {}) == ISO_25_EXPECTED
    assert resolve_dimstyle("ansi", None) == ANSI_EXPECTED


def test_overrides_apply_on_top_and_are_upper_cased_and_typed():
    values = resolve_dimstyle("iso-25", {"dimtxt": 3, "DIMDEC": 3.0, "dimdsep": "."})
    assert values["DIMTXT"] == 3.0 and type(values["DIMTXT"]) is float
    assert values["DIMDEC"] == 3 and type(values["DIMDEC"]) is int
    assert values["DIMDSEP"] == "."
    assert values["DIMASZ"] == 2.5, "untouched values stay the preset's"


def test_resolve_leaves_the_preset_table_untouched():
    resolve_dimstyle("iso-25", {"DIMTXT": 9.0})
    assert PRESETS["iso-25"]["DIMTXT"] == 2.5


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"DIMFOO": 1}, "DIMFOO"),
        ({"DIMDEC": 9}, "DIMDEC"),
        ({"DIMDEC": -1}, "DIMDEC"),
        ({"DIMDEC": 2.5}, "whole number"),
        ({"DIMTXT": 0}, "DIMTXT"),
        ({"DIMASZ": -1}, "DIMASZ"),
        ({"DIMTAD": 5}, "DIMTAD"),
        ({"DIMTIH": 2}, "DIMTIH"),
        ({"DIMDSEP": ",,"}, "DIMDSEP"),
        ({"DIMDSEP": ""}, "DIMDSEP"),
        ({"DIMLWD": 27}, "DIMLWD"),
        ({"DIMLUNIT": 0}, "DIMLUNIT"),
        ({"DIMZIN": 16}, "DIMZIN"),
        ({"DIMTXSTY": ""}, "DIMTXSTY"),
        ({"DIMTXSTY": "a/b"}, "DIMTXSTY"),
        ({"DIMLFAC": 0}, "DIMLFAC"),
        ({"DIMTFAC": 0}, "DIMTFAC"),
        ({"DIMCLRT": 257}, "DIMCLRT"),
        ({"DIMRND": -0.5}, "DIMRND"),
        ({"DIMSCALE": float("nan")}, "DIMSCALE"),
    ],
)
def test_out_of_range_or_unknown_overrides_are_refused_by_name(overrides, fragment):
    with pytest.raises(ValueError, match=fragment):
        resolve_dimstyle("iso-25", overrides)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"DIMTXT": "2.5"}, "DIMTXT"),
        ({"DIMTAD": True}, "DIMTAD"),
        ({"DIMDSEP": 44}, "DIMDSEP"),
        ({"DIMBLK": 0}, "DIMBLK"),
    ],
)
def test_wrong_types_are_a_type_error_naming_the_key(overrides, fragment):
    with pytest.raises(TypeError, match=fragment):
        resolve_dimstyle("iso-25", overrides)


def test_unknown_preset_is_refused():
    with pytest.raises(ValueError, match="preset"):
        resolve_dimstyle("din", None)
    with pytest.raises(TypeError, match="overrides"):
        resolve_dimstyle("iso-25", [("DIMTXT", 2.5)])


def test_validate_overrides_alone_is_what_modify_uses():
    assert validate_overrides(None) == {}
    assert validate_overrides({"dimgap": -1}) == {"DIMGAP": -1.0}, "negative gap boxes the text"
    assert check_dim_value("dimlwe", 25) == 25
    assert check_dim_value("DIMBLK", "_OBLIQUE") == "_OBLIQUE"


# ── text styles ─────────────────────────────────────────────────────────────


def test_text_presets_are_the_four_spec_fonts():
    assert TEXT_PRESETS == {
        "ISOCP": ("isocp.shx", 1.0, 0.0),
        "ISOCPEUR": ("isocpeur.ttf", 1.0, 0.0),
        "ARIAL": ("arial.ttf", 1.0, 0.0),
        "ROMANS": ("romans.shx", 1.0, 0.0),
    }


@pytest.mark.parametrize(
    ("font", "expected"),
    [
        ("ISOCP", "isocp.shx"),
        ("isocp", "isocp.shx"),
        ("isocp.shx", "isocp.shx"),
        ("ISOCP.SHX", "isocp.shx"),
        ("ISOCPEUR", "isocpeur.ttf"),
        ("arial", "arial.ttf"),
        ("Arial.ttf", "arial.ttf"),
        ("romans.shx", "romans.shx"),
    ],
)
def test_a_preset_font_resolves_known(font, expected):
    assert resolve_font(font) == (expected, True)


def test_an_unknown_font_passes_through_unknown_never_refused():
    assert resolve_font("simplex.shx") == ("simplex.shx", False)
    assert resolve_font("  c:/fonts/custom.ttf ") == ("c:/fonts/custom.ttf", False)


def test_empty_or_control_font_names_are_refused():
    with pytest.raises(ValueError, match="font"):
        resolve_font("")
    with pytest.raises(ValueError, match="font"):
        resolve_font("bad\nfont.shx")
    with pytest.raises(TypeError, match="font"):
        resolve_font(None)


def test_validate_textstyle_types_the_request():
    spec = validate_textstyle(" ISOCP ", "isocp", 0, 1, 0)
    assert spec == {
        "name": "ISOCP",
        "font_file": "isocp.shx",
        "known": True,
        "height": 0.0,
        "width_factor": 1.0,
        "oblique_deg": 0.0,
    }


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"name": ""}, "name"),
        ({"name": "A:B"}, "name"),
        ({"height": -1}, "height"),
        ({"width_factor": 0}, "width_factor"),
        ({"oblique_deg": 86}, "oblique_deg"),
        ({"oblique_deg": -90}, "oblique_deg"),
    ],
)
def test_validate_textstyle_refuses_by_field(kwargs, fragment):
    base = {
        "name": "T",
        "font": "arial.ttf",
        "height": 0.0,
        "width_factor": 1.0,
        "oblique_deg": 0.0,
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=fragment):
        validate_textstyle(**base)


def test_validate_textstyle_refuses_wrong_types():
    with pytest.raises(TypeError, match="width_factor"):
        validate_textstyle("T", "arial.ttf", 0.0, "1", 0.0)
    with pytest.raises(TypeError, match="height"):
        validate_textstyle("T", "arial.ttf", True, 1.0, 0.0)


# ── multileader styles ──────────────────────────────────────────────────────


def test_mleader_presets_match_the_plan():
    assert MLEADER_PRESETS == {
        "iso": {"arrow_size": 2.5, "landing_gap": 1.0, "text_style": "ISOCP", "text_height": 2.5},
        "ansi": {"arrow_size": 3.0, "landing_gap": 1.5, "text_style": "ROMANS", "text_height": 3.0},
    }
    assert MLEADER_KEYS == ("arrow_size", "landing_gap", "text_style", "text_height")


def test_resolve_mleaderstyle_applies_overrides():
    values = resolve_mleaderstyle("ISO", {"arrow_size": 3, "text_style": " ARIAL "})
    assert values == {
        "arrow_size": 3.0,
        "landing_gap": 1.0,
        "text_style": "ARIAL",
        "text_height": 2.5,
    }
    assert resolve_mleaderstyle("ansi") == MLEADER_PRESETS["ansi"]
    assert resolve_mleaderstyle("iso", {"landing_gap": 0})["landing_gap"] == 0.0


@pytest.mark.parametrize(
    ("preset", "overrides", "exc", "fragment"),
    [
        ("din", None, ValueError, "preset"),
        (None, None, ValueError, "preset"),
        ("iso", {"colour": 1}, ValueError, "colour"),
        ("iso", {"arrow_size": 0}, ValueError, "arrow_size"),
        ("iso", {"text_height": -2}, ValueError, "text_height"),
        ("iso", {"text_style": ""}, ValueError, "text_style"),
        ("iso", {"text_style": "a|b"}, ValueError, "text_style"),
        ("iso", {"arrow_size": "2.5"}, TypeError, "arrow_size"),
        ("iso", ["arrow_size"], TypeError, "overrides"),
    ],
)
def test_resolve_mleaderstyle_refuses_by_name(preset, overrides, exc, fragment):
    with pytest.raises(exc, match=fragment):
        resolve_mleaderstyle(preset, overrides)
