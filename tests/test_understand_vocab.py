"""The multilingual vocabulary: layer names, direction words, room labels, block kinds.

Every layer name below is one a drafter would write; the ones in the synthetic
plant pair (tests/fixtures/plant_pair.py) are here too, because that is what the
readers above this module are measured on.
"""

from __future__ import annotations

import pytest

from engineering.understand.vocab import (
    DISCIPLINES,
    EQUIPMENT_KINDS,
    LANGUAGES,
    SERVICES,
    classify_layer,
    equipment_kind,
    fold,
    room_label,
    supply_return,
)


def test_the_closed_lists():
    assert LANGUAGES == ("en", "tr", "ru", "nl", "de")
    assert SERVICES == (
        "product",
        "cip_supply",
        "cip_return",
        "glycol_supply",
        "glycol_return",
        "steam",
        "condensate",
        "cold_water",
        "hot_water",
        "ice_water",
        "compressed_air",
        "drain",
        "electrical",
        "unknown",
    )
    assert DISCIPLINES == (
        "piping",
        "electrical",
        "architecture",
        "equipment",
        "annotation",
        "structure",
        "unknown",
    )
    assert EQUIPMENT_KINDS == ("tank", "pump", "valve", "reducer", "motor", "agitator", "panel")


def test_fold_strips_diacritics_and_the_turkish_dotless_i():
    assert fold("DÖNÜŞ") == "donus"
    assert fold("GİDİŞ") == "gidis"
    assert fold("çıkış") == "cikis"
    assert fold("RückLauf") == "rucklauf"
    assert fold("ПОДАЧА") == "подача"
    assert fold(None) == ""


@pytest.mark.parametrize(
    ("name", "service", "keyword", "language"),
    [
        ("P_product piping", "product", "product", "en"),
        ("P_cipsupplyline", "cip_supply", "cip", "en"),
        ("P_cipreturnline", "cip_return", "cip", "en"),
        ("P_ijswater", "ice_water", "ijswater", "nl"),
        ("CIP ВОЗВРАТ", "cip_return", "cip", "en"),
        ("glikol dönüş", "glycol_return", "glikol", "tr"),
        ("GLYKOL-VORLAUF", "glycol_supply", "glykol", "de"),
        ("buhar hattı", "steam", "buhar", "tr"),
        ("КОНДЕНСАТ", "condensate", "конденсат", "ru"),
        ("P_ICE WATER", "ice_water", "ice water", "en"),
        ("sıcak su", "hot_water", "sicak su", "tr"),
        ("DRUCKLUFT", "compressed_air", "druckluft", "de"),
        ("air_line", "compressed_air", "air", "en"),
        ("RIOOL", "drain", "riool", "nl"),
    ],
)
def test_service_layers(name, service, keyword, language):
    row = classify_layer(name)
    assert row["service"] == service
    assert row["discipline"] == "piping"
    assert row["confidence"] == 0.9
    assert row["keyword"] == keyword
    assert row["language"] == language


def test_electrical_layers_are_their_own_service():
    for name in ("E_power", "ELEKTRIK", "ЭЛЕКТРИКА", "kabel"):
        row = classify_layer(name)
        assert (row["discipline"], row["service"], row["confidence"]) == (
            "electrical",
            "electrical",
            0.9,
        ), name


def test_a_cip_layer_without_a_direction_is_not_guessed():
    row = classify_layer("P_CIP")
    assert row == {
        "discipline": "piping",
        "service": "unknown",
        "confidence": 0.6,
        "keyword": "cip",
        "language": "en",
    }


def test_a_cip_layer_naming_both_directions_is_not_guessed_either():
    assert classify_layer("CIP supply return")["service"] == "unknown"


def test_a_water_layer_with_no_temperature_is_piping_of_unknown_service():
    row = classify_layer("P_WATER")
    assert (row["discipline"], row["service"], row["confidence"]) == ("piping", "unknown", 0.6)


@pytest.mark.parametrize(
    ("name", "discipline", "language"),
    [
        ("KLEPPEN", "piping", "nl"),
        ("VANALAR", "piping", "tr"),
        ("WALLS", "architecture", "en"),
        ("MUREN", "architecture", "nl"),
        ("СТЕНЫ", "architecture", "ru"),
        ("EQUIPMENT", "equipment", "en"),
        ("POMPALAR", "equipment", "tr"),
        ("DIM", "annotation", "en"),
        ("ÖLÇÜ", "annotation", "tr"),
        ("KOLONLAR", "structure", "tr"),
    ],
)
def test_discipline_layers(name, discipline, language):
    row = classify_layer(name)
    assert (row["discipline"], row["service"], row["confidence"], row["language"]) == (
        discipline,
        "unknown",
        0.9,
        language,
    )


def test_a_short_keyword_must_start_a_word():
    assert classify_layer("STAIRS")["service"] != "compressed_air"
    assert classify_layer("CHAIRS")["discipline"] == "unknown"


def test_a_layer_with_no_keyword():
    assert classify_layer("0") == {
        "discipline": "unknown",
        "service": "unknown",
        "confidence": 0.0,
        "keyword": None,
        "language": None,
    }
    assert classify_layer("Defpoints")["discipline"] == "unknown"


@pytest.mark.parametrize(
    ("text", "direction"),
    [
        ("ПОДАЧА", "supply"),
        ("ВОЗВРАТ", "return"),
        ("обратка CIP", "return"),  # обратка
        ("BESLEME", "supply"),
        ("DÖNÜŞ", "return"),
        ("donus hatti", "return"),
        ("CIP SUPPLY", "supply"),
        ("return line", "return"),
        ("AANVOER", "supply"),
        ("RETOUR", "return"),
        ("Vorlauf", "supply"),
        ("RÜCKLAUF", "return"),
    ],
)
def test_supply_and_return_words_in_five_languages(text, direction):
    assert supply_return(text) == direction


def test_a_text_naming_both_or_neither_direction_is_none():
    assert supply_return("CIP SUPPLY / RETURN") is None
    assert supply_return("Ø51") is None
    assert supply_return(None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ROOM 1 PROCESS HALL", {"number": "1", "name": "PROCESS HALL", "language": "en"}),
        ("ODA 2", {"number": "2", "name": None, "language": "tr"}),
        ("MAHAL 1.05 SOĞUK ODA", {"number": "1.05", "name": "SOĞUK ODA", "language": "tr"}),
        (
            "ПОМЕЩЕНИЕ 2 МОЙКА",
            {"number": "2", "name": "МОЙКА", "language": "ru"},
        ),
        ("RUIMTE 3 opslag", {"number": "3", "name": "opslag", "language": "nl"}),
        ("Raum 4", {"number": "4", "name": None, "language": "de"}),
    ],
)
def test_room_labels_in_five_languages(text, expected):
    assert room_label(text) == expected


def test_a_text_without_a_room_word_is_not_a_room_label():
    assert room_label("T101") is None
    assert room_label("PROCESS HALL") is None
    assert room_label("") is None


@pytest.mark.parametrize(
    ("block", "kind", "language"),
    [
        ("KLEP_VLINDER", "valve", "nl"),
        ("VERLOOPSTUK", "reducer", "nl"),
        ("REDUCER_CONC", "reducer", "en"),
        ("TANK_V", "tank", "en"),
        ("POMP", "pump", "nl"),
        ("SCHAKELKAST", "panel", "nl"),
        ("НАСОС", "pump", "ru"),
        ("VANA_KELEBEK", "valve", "tr"),
    ],
)
def test_block_names_say_their_kind(block, kind, language):
    row = equipment_kind(block)
    assert row["kind"] == kind and row["language"] == language


def test_an_anonymous_block_has_no_kind():
    assert equipment_kind("*U12") is None
    assert equipment_kind(None) is None
