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
    ARROWHEAD_BLOCKS,
    DIM_VARIABLE_RANGES,
    DIM_VARIABLE_WHITELIST,
    LINEWEIGHT_CODES,
    PRESET_VARIABLES,
    PRESETS,
    check_dim_value,
    describe_preset,
    ezdxf_arrowhead,
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
        ({"DIMTXSTY": "a\nb"}, "DIMTXSTY"),
        ({"DIMTXSTY": "a\tb"}, "DIMTXSTY"),
        ({"DIMBLK": "a/b"}, "DIMBLK"),
        ({"DIMBLK": "a,b"}, "DIMBLK"),
        ({"DIMBLK1": "x\ny"}, "DIMBLK1"),
        ({"DIMBLK2": "   "}, "DIMBLK2"),
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


# ── arrowheads: DIMBLK / DIMBLK1 / DIMBLK2 ──────────────────────────────────


def test_the_builtin_arrowheads_are_autocads_nineteen_named_blocks():
    assert ARROWHEAD_BLOCKS == frozenset(
        "_ARCHTICK _BOXBLANK _BOXFILLED _CLOSED _CLOSEDBLANK _DATUMBLANK _DATUMFILLED "
        "_DOT _DOTBLANK _DOTSMALL _INTEGRAL _NONE _OBLIQUE _OPEN _OPEN30 _OPEN90 "
        "_ORIGIN _ORIGIN2 _SMALL".split()
    )


def test_the_builtin_arrowheads_are_exactly_ezdxfs_vocabulary():
    from ezdxf.render.arrows import ARROWS

    assert ARROWHEAD_BLOCKS == {ARROWS.block_name(name) for name in ARROWS.__acad__ if name}
    assert ARROWS.block_name("") == "_CLOSEDFILLED", "closed filled is the nameless default"


@pytest.mark.parametrize(
    ("given", "canonical"),
    [
        ("", ""),
        (".", ""),
        ("CLOSEDFILLED", ""),
        ("_ClosedFilled", ""),
        ("_OBLIQUE", "_OBLIQUE"),
        ("OBLIQUE", "_OBLIQUE"),
        ("oblique", "_OBLIQUE"),
        (" _dot ", "_DOT"),
        ("Open30", "_OPEN30"),
        ("none", "_NONE"),
        ("MYARROW", "MYARROW"),
        (" my_arrow-1 ", "my_arrow-1"),
    ],
)
def test_arrowhead_names_canonicalise_to_autocads_documented_spelling(given, canonical):
    for var in ("DIMBLK", "DIMBLK1", "DIMBLK2"):
        assert check_dim_value(var, given) == canonical, var


@pytest.mark.parametrize("bad", ["a/b", "a,b", "x\ny", "x\ty", "x\rx", "a\x01b", "   ", "\t"])
def test_arrowhead_user_blocks_obey_the_symbol_name_rule(bad):
    for var in ("DIMBLK", "DIMBLK1", "DIMBLK2"):
        with pytest.raises(ValueError, match=var):
            check_dim_value(var, bad)


def test_ezdxf_arrowhead_strips_the_underscore_only_for_built_ins():
    assert ezdxf_arrowhead("") == ""
    assert ezdxf_arrowhead("_OBLIQUE") == "OBLIQUE"
    assert ezdxf_arrowhead("_DOT") == "DOT"
    assert ezdxf_arrowhead("MYARROW") == "MYARROW"
    assert ezdxf_arrowhead("_MYBLOCK") == "_MYBLOCK", (
        "a user block with an underscore is not built in"
    )


@pytest.mark.parametrize("canonical", [*sorted(ARROWHEAD_BLOCKS), ""])
def test_every_canonical_arrowhead_exports_headlessly_and_reads_back_as_itself(canonical):
    """The canonical spelling is what ezdxf itself reports after a load; only
    its *writer* wants the underscore-less name, and ``ezdxf_arrowhead`` is that
    one translation."""
    import io

    import ezdxf

    doc = ezdxf.new("R2018")
    style = doc.dimstyles.new("X")
    style.dxf.dimblk = ezdxf_arrowhead(canonical)
    style.dxf.dimblk1 = ezdxf_arrowhead(canonical)
    buffer = io.StringIO()
    doc.write(buffer)
    buffer.seek(0)
    reloaded = ezdxf.read(buffer).dimstyles.get("X")
    assert reloaded.dxf.dimblk == canonical
    assert reloaded.dxf.dimblk1 == canonical


def test_the_canonical_spelling_is_not_what_the_headless_writer_takes():
    """Why ``ezdxf_arrowhead`` exists: ``_OBLIQUE`` stored verbatim raises at
    ``doc.write``, after ``dimstyle_create`` would already have said ``ok``."""
    import io

    import ezdxf
    from ezdxf.lldxf.const import DXFValueError

    doc = ezdxf.new("R2018")
    doc.dimstyles.new("X").dxf.dimblk = "_OBLIQUE"
    with pytest.raises(DXFValueError, match="_OBLIQUE"):
        doc.write(io.StringIO())


# ── the symbol-name rule ────────────────────────────────────────────────────


def test_the_name_rule_is_the_one_security_applies():
    import security
    from engineering.standards.names import ILLEGAL_NAME_CHARS, illegal_name_chars

    assert ILLEGAL_NAME_CHARS == frozenset(security._FORBIDDEN_SYMBOL_CHARS)
    for sample in ("a\nb", "a\tb", "a\rb", "a\x01b", "a/b", "a<b>c", "ok_name-1", "ISO-25"):
        assert illegal_name_chars(sample) == security.illegal_symbol_name_chars(sample), sample


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
        ({"name": "a\nb"}, "name"),
        ({"name": "a\tb"}, "name"),
        ({"name": "a\rb"}, "name"),
        ({"height": -1}, "height"),
        ({"height": float("nan")}, "height"),
        ({"height": float("inf")}, "height"),
        ({"width_factor": 0}, "width_factor"),
        ({"width_factor": float("nan")}, "width_factor"),
        ({"width_factor": float("inf")}, "width_factor"),
        ({"oblique_deg": 86}, "oblique_deg"),
        ({"oblique_deg": -90}, "oblique_deg"),
        ({"oblique_deg": float("nan")}, "oblique_deg"),
        ({"oblique_deg": float("-inf")}, "oblique_deg"),
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
        ("iso", {"text_style": "a\nb"}, ValueError, "text_style"),
        ("iso", {"text_style": "a\tb"}, ValueError, "text_style"),
        ("iso", {"arrow_size": float("nan")}, ValueError, "arrow_size"),
        ("iso", {"arrow_size": "2.5"}, TypeError, "arrow_size"),
        ("iso", ["arrow_size"], TypeError, "overrides"),
    ],
)
def test_resolve_mleaderstyle_refuses_by_name(preset, overrides, exc, fragment):
    with pytest.raises(exc, match=fragment):
        resolve_mleaderstyle(preset, overrides)
