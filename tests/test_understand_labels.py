"""Label parsers: every spelling a foreign drawing uses, read one way.

Review Focus 1 (AutoCAD text codes) and 2 (Cyrillic look-alike tags) of the
track H plan live here: one test per spelling, because a parser that handles
four of five codes fails silently on the fifth drawing.
"""

from __future__ import annotations

import pytest

from engineering.understand.labels import (
    CYRILLIC_LOOKALIKES,
    normalize_panel,
    parse_diameters,
    parse_electrical,
    parse_tag,
    plain,
    wiring_target,
)

# -- plain(): AutoCAD codes and MTEXT format runs -------------------------------


def test_plain_decodes_the_percent_codes():
    assert plain("%%c51") == "\u00d851"
    assert plain("90%%d") == "90\u00b0"
    assert plain("%%p0.1") == "\u00b10.1"
    assert plain("50%%%") == "50%"
    assert plain("%%uUNDER%%u") == "UNDER"
    assert plain("%%065") == "A"


def test_plain_decodes_the_caret_notation_of_a_text_entity():
    # A TEXT or ATTRIB stores its control characters in caret notation: ^J a line
    # break, ^I a tab, "^ " a caret. Left undecoded, "6^JROOM" is one word and a
    # room label's number reads as "6^JROOM".
    assert plain("6^JROOM") == "6\nROOM"
    assert plain("A^IB") == "A\tB"
    assert plain("x^ y") == "x^y"
    assert plain("\\S1^2;") == "1/2"  # an MTEXT stack keeps its separator


def test_plain_decodes_a_unicode_escape():
    assert plain("\\U+2205 51") == "\u2205 51"
    assert plain("\\u+00D8") == "\u00d8"


def test_plain_drops_mtext_format_runs_and_keeps_the_content():
    assert plain("{\\fArial|b0|i0|c0|p34;\u00d851}") == "\u00d851"
    assert plain("\\H2.5;\\C1;SMS51") == "SMS51"
    assert plain("\\A1;\\W0.8;\\Q15;\\T1.1;DN20") == "DN20"
    assert plain("\\LUNDERLINED\\l") == "UNDERLINED"


def test_plain_turns_paragraphs_spaces_and_stacks_into_text():
    assert plain("PUMP\\PM11") == "PUMP\nM11"
    assert plain("a\\~b") == "a b"
    assert plain("\\S1/2;") == "1/2"
    assert plain("\\S+0.1^-0.2;") == "+0.1/-0.2"


def test_plain_keeps_escaped_backslashes_and_braces():
    assert plain("\\\\P") == "\\P"
    assert plain("\\{x\\}") == "{x}"


def test_plain_of_nothing_is_empty():
    assert plain(None) == ""
    assert plain("") == ""


# -- parse_diameters(): Review Focus 1, one spelling per test --------------------


def test_the_autocad_percent_code_is_a_diameter():
    assert parse_diameters("%%c51") == ("\u00d851",)


def test_a_unicode_escaped_diameter_sign_is_a_diameter():
    assert parse_diameters("\\U+2205 51") == ("\u00d851",)
    assert parse_diameters("\\U+220551") == ("\u00d851",)


def test_the_diameter_sign_u2300_is_a_diameter():
    assert parse_diameters("\u230051") == ("\u00d851",)


def test_a_lowercase_o_stroke_is_a_diameter():
    assert parse_diameters("\u00f851") == ("\u00d851",)


def test_a_diameter_inside_an_mtext_format_run_is_a_diameter():
    assert parse_diameters("{\\fArial|b0|i0|c0|p34;\u00d851}") == ("\u00d851",)
    assert parse_diameters("{\\fArial|b0;\\U+2205}51") == ("\u00d851",)


def test_a_diameter_after_a_paragraph_break_is_found():
    assert parse_diameters("PRODUCT\\P%%c51") == ("\u00d851",)


def test_labels_are_kept_as_drawn_and_never_equated():
    assert parse_diameters("\u00d825,4") == ("\u00d825,4",)
    assert parse_diameters("\u00d825") == ("\u00d825",)
    assert parse_diameters("SMS 51") == ("SMS51",)
    assert parse_diameters("dn 20") == ("DN20",)
    assert parse_diameters("DN-25") == ("DN25",)
    assert parse_diameters("20 x 27") == ("20x27",)
    assert parse_diameters("20\u044527") == ("20x27",)  # Cyrillic х
    assert parse_diameters('1,5"') == ('1,5"',)
    assert parse_diameters("1/2\u2033") == ('1/2"',)


def test_several_labels_in_one_text_come_in_order_and_once():
    assert parse_diameters("\u00d851 / SMS51 / \u00d851") == ("\u00d851", "SMS51")


def test_a_cable_cross_section_is_not_a_pipe_size():
    assert parse_diameters("5x4 mm\u00b2") == ()
    assert parse_diameters("3x2,5 mm2") == ()


def test_text_without_a_size_yields_nothing():
    assert parse_diameters("CIP SUPPLY") == ()
    assert parse_diameters("T101") == ()


# -- parse_tag(): Review Focus 2, one look-alike per test -------------------------


@pytest.mark.parametrize(("cyrillic", "latin"), sorted(CYRILLIC_LOOKALIKES.items()))
def test_every_cyrillic_lookalike_folds_to_its_latin_twin(cyrillic, latin):
    assert parse_tag(f"{cyrillic}101") == f"{latin}101"


def test_a_cyrillic_t_tag_matches_the_latin_one():
    assert parse_tag("\u04224100") == "T4100"


def test_a_cyrillic_m_tag_matches_the_latin_one():
    assert parse_tag("\u041c82") == "M82"


def test_a_tag_with_a_cyrillic_prefix_and_a_hyphen():
    assert parse_tag("\u0421\u0420-1") == "CP-1"  # С Р typed in Cyrillic


def test_a_russian_word_is_never_folded_into_a_tag():
    assert parse_tag("\u041f\u041e\u0414\u0410\u0427\u0410") is None  # ПОДАЧА
    assert parse_tag("\u041f\u041e\u04171") is None  # ПОЗ1: П has no Latin twin


def test_tag_shapes():
    assert parse_tag("T4100") == "T4100"
    assert parse_tag("pump m41a") == "M41A"
    assert parse_tag("M47.1") == "M47.1"
    assert parse_tag("CP-1") == "CP-1"
    assert parse_tag("CP-M82") == "CP-M82"
    assert parse_tag("JB-2.") == "JB-2"
    assert parse_tag("CIP TANK T101") == "T101"


def test_sizes_ratings_and_ip_codes_are_not_tags():
    assert parse_tag("DN20") is None
    assert parse_tag("SMS51") is None
    assert parse_tag("PN16") is None
    assert parse_tag("IP65") is None
    assert parse_tag("DN20 T101") == "T101"


def test_text_without_a_tag():
    assert parse_tag("ROOM 1 PROCESS HALL") is None
    assert parse_tag(None) is None


def test_a_line_number_is_not_an_equipment_tag():
    # A line number starts with its size: read as a tag, '100-P-001' would give
    # 'P-001', which can capture a valve beside the line or collide with a real
    # pump P-001. A word that begins with a digit holds no tag.
    assert parse_tag("100-P-001") is None
    assert parse_tag('2"-P-1001-A1') is None
    assert parse_tag("LINE 100-P-001 TO T101") == "T101"
    assert parse_tag("P-001") == "P-001"  # the pump itself is still a tag


# -- panels and wiring callouts ----------------------------------------------------


def test_panel_spellings_become_one():
    assert normalize_panel("CP 1") == "CP-1"
    assert normalize_panel("CP1") == "CP-1"
    assert normalize_panel("cp-1") == "CP-1"
    assert normalize_panel("CP M82") == "CP-M82"
    assert normalize_panel("\u0421\u0420 2") == "CP-2"
    assert normalize_panel("PANEL") is None
    assert normalize_panel("") is None


def test_wiring_callouts_in_four_languages():
    assert wiring_target("Wiring to CP1") == "CP-1"
    assert wiring_target("WIRING TO CP 2") == "CP-2"
    assert wiring_target("wiring to the CP-M82") == "CP-M82"
    # подключение к шкафу CP-1
    assert (
        wiring_target(
            "\u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 "
            "\u043a \u0448\u043a\u0430\u0444\u0443 CP-1"
        )
        == "CP-1"
    )
    assert wiring_target("bedrading naar CP3") == "CP-3"
    assert wiring_target("CP 2 panosuna") == "CP-2"


def test_a_callout_without_a_wiring_phrase_names_no_panel():
    assert wiring_target("CP1") is None
    assert wiring_target("Wiring to") is None


# -- electrical data ---------------------------------------------------------------


def test_russian_electrical_text():
    # P = 18,5 кВт, 3 ф. 400 В + N + PE, ЧРП
    text = "P = 18,5 \u043a\u0412\u0442, 3 \u0444. 400 \u0412 + N + PE, \u0427\u0420\u041f"
    assert parse_electrical(text) == {
        "kw": 18.5,
        "phases": 3,
        "voltage": 400,
        "neutral": True,
        "vfd": True,
    }


def test_english_and_turkish_electrical_text():
    assert parse_electrical("5.5 kW 3ph 400V VFD") == {
        "kw": 5.5,
        "phases": 3,
        "voltage": 400,
        "neutral": False,
        "vfd": True,
    }
    assert parse_electrical("2,2 kW 3 faz 400 V frekans konvert\u00f6r\u00fc")["vfd"] is True
    assert parse_electrical("3/N/PE ~ 400 V")["neutral"] is True
    assert parse_electrical("24 VDC")["voltage"] == 24


def test_a_power_that_is_not_written_is_never_invented():
    # Нагреватель, 3 ф. 400 В
    heater = parse_electrical(
        "\u041d\u0430\u0433\u0440\u0435\u0432\u0430\u0442\u0435\u043b\u044c, 3 \u0444. 400 \u0412"
    )
    assert heater["kw"] is None
    assert heater["phases"] == 3 and heater["voltage"] == 400


def test_energy_is_not_power():
    assert parse_electrical("1200 kWh")["kw"] is None
