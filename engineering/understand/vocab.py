"""Multilingual keyword tables for reading foreign drawings: EN / TR / RU / NL / DE.

A foreign drawing says what a layer carries in its name and in the texts on it,
in whatever language the office that drew it speaks: a P&ID may name
its piping layers in English and Dutch (``P_cipsupplyline``,
``KLEPPEN``) and label its lines in Russian. These tables are what lets the
reader tell a CIP supply line from a product line without a human.

They are **vocabulary, not a standard**: words a drafter writes, each with the
language it belongs to. A classification made from them carries a confidence
(0.9 for a direct keyword, 0.6 for weaker evidence) and the keyword that
decided it, so a reader can always see why.

Matching is done on a *folded* spelling - casefolded, diacritics stripped,
Turkish dotless ı made i - so ``DÖNÜŞ``, ``dönüş`` and ``donus`` are one word.
Cyrillic keeps its letters (``подача`` stays ``подача``). A keyword of five
letters or more matches anywhere in a layer name written without separators
(``cipsupplyline`` holds ``supply``); a shorter one must start a word, so
``air`` finds ``air_line`` and not ``stairs``.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "DISCIPLINES",
    "EQUIPMENT_KINDS",
    "LANGUAGES",
    "SERVICES",
    "classify_layer",
    "equipment_kind",
    "fold",
    "room_label",
    "supply_return",
]

LANGUAGES = ("en", "tr", "ru", "nl", "de")

SERVICES: tuple[str, ...] = (
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

DISCIPLINES: tuple[str, ...] = (
    "piping",
    "electrical",
    "architecture",
    "equipment",
    "annotation",
    "structure",
    "unknown",
)

EQUIPMENT_KINDS: tuple[str, ...] = (
    "tank",
    "pump",
    "valve",
    "reducer",
    "motor",
    "agitator",
    "panel",
)

#: Confidence of a classification decided by one direct keyword.
DIRECT = 0.9
#: Confidence of a classification from weaker evidence: a CIP or glycol layer
#: whose name says no direction, a generic water layer.
WEAK = 0.6
#: A keyword this long matches anywhere in a separator-free name.
_SUBSTRING_MIN = 5

# (keyword, language) rows. Order matters inside a table: the first row that
# matches decides, so a longer or more specific word comes before a word it
# contains.

_SUPPLY_WORDS = (
    ("supply", "en"),
    ("besleme", "tr"),
    ("gidis", "tr"),
    ("подач", "ru"),  # подача: supply
    ("подающ", "ru"),  # подающий: supplying
    ("aanvoer", "nl"),
    ("vorlauf", "de"),
)
_RETURN_WORDS = (
    ("return", "en"),
    ("donus", "tr"),
    ("возврат", "ru"),  # return
    ("обрат", "ru"),  # обратка / обратный: return
    ("retour", "nl"),
    ("rucklauf", "de"),
)

_CIP_WORDS = (
    ("cip", "en"),
    ("мойк", "ru"),  # мойка: washing, CIP
)
_GLYCOL_WORDS = (
    ("glycol", "en"),
    ("glikol", "tr"),
    ("гликол", "ru"),  # гликоль: glycol
    ("glykol", "de"),
)

#: Services a layer name can state directly (CIP and glycol are decided with
#: their direction words above).
_SERVICE_WORDS = (
    ("product", "product", "en"),
    ("product", "urun", "tr"),
    ("product", "продукт", "ru"),  # product
    ("product", "produkt", "de"),
    ("steam", "steam", "en"),
    ("steam", "buhar", "tr"),
    ("steam", "пар", "ru"),  # steam
    ("steam", "stoom", "nl"),
    ("steam", "dampf", "de"),
    ("condensate", "condensate", "en"),
    ("condensate", "kondensat", "de"),
    ("condensate", "kondens", "tr"),
    ("condensate", "конденсат", "ru"),  # condensate
    ("condensate", "condensaat", "nl"),
    ("ice_water", "ice water", "en"),
    ("ice_water", "buzlu su", "tr"),
    ("ice_water", "ледяная вода", "ru"),
    ("ice_water", "ijswater", "nl"),
    ("ice_water", "eiswasser", "de"),
    ("hot_water", "hot water", "en"),
    ("hot_water", "sicak su", "tr"),
    ("hot_water", "горячая вода", "ru"),
    ("hot_water", "warm water", "nl"),
    ("hot_water", "warmwasser", "de"),
    ("cold_water", "cold water", "en"),
    ("cold_water", "soguk su", "tr"),
    ("cold_water", "холодная вода", "ru"),
    ("cold_water", "koud water", "nl"),
    ("cold_water", "kaltwasser", "de"),
    ("compressed_air", "compressed air", "en"),
    ("compressed_air", "basincli hava", "tr"),
    ("compressed_air", "сжатый воздух", "ru"),
    ("compressed_air", "perslucht", "nl"),
    ("compressed_air", "druckluft", "de"),
    ("compressed_air", "air", "en"),
    ("compressed_air", "hava", "tr"),
    ("compressed_air", "воздух", "ru"),  # air
    ("drain", "drain", "en"),
    ("drain", "sewer", "en"),
    ("drain", "drenaj", "tr"),
    ("drain", "atik su", "tr"),
    ("drain", "дренаж", "ru"),  # drainage
    ("drain", "канализац", "ru"),  # канализация: sewer
    ("drain", "riool", "nl"),
    ("drain", "abwasser", "de"),
)

_ELECTRICAL_WORDS = (
    ("electrical", "en"),
    ("electric", "en"),
    ("power", "en"),
    ("cable", "en"),
    ("elektrik", "tr"),
    ("kablo", "tr"),
    ("электр", "ru"),  # электрика: electrical
    ("кабел", "ru"),  # кабель: cable
    ("elektra", "nl"),
    ("kabel", "nl"),
    ("elektro", "de"),
)

#: Water words that name no temperature: a piping layer of unknown service.
_WATER_WORDS = (
    ("water", "en"),
    ("вода", "ru"),  # water
    ("wasser", "de"),
)

_DISCIPLINE_WORDS = (
    ("piping", "piping", "en"),
    ("piping", "pipe", "en"),
    ("piping", "valve", "en"),
    ("piping", "boru", "tr"),
    ("piping", "tesisat", "tr"),
    ("piping", "vana", "tr"),
    ("piping", "труб", "ru"),  # трубопровод: pipeline
    ("piping", "клапан", "ru"),  # valve
    ("piping", "арматур", "ru"),  # арматура: valves and fittings
    ("piping", "leiding", "nl"),
    ("piping", "klep", "nl"),
    ("piping", "afsluiter", "nl"),
    ("piping", "rohr", "de"),
    ("piping", "ventil", "de"),
    ("piping", "armatur", "de"),
    ("architecture", "wall", "en"),
    ("architecture", "door", "en"),
    ("architecture", "window", "en"),
    ("architecture", "duvar", "tr"),
    ("architecture", "kapi", "tr"),
    ("architecture", "pencere", "tr"),
    ("architecture", "стен", "ru"),  # стена: wall
    ("architecture", "двер", "ru"),  # дверь: door
    ("architecture", "окн", "ru"),  # окно: window
    ("architecture", "muur", "nl"),
    ("architecture", "muren", "nl"),
    ("architecture", "deur", "nl"),
    ("architecture", "raam", "nl"),
    ("architecture", "wand", "de"),
    ("architecture", "fenster", "de"),
    ("equipment", "equipment", "en"),
    ("equipment", "tank", "en"),
    ("equipment", "pump", "en"),
    ("equipment", "ekipman", "tr"),
    ("equipment", "pompa", "tr"),
    ("equipment", "оборудован", "ru"),  # оборудование: equipment
    ("equipment", "насос", "ru"),  # pump
    ("equipment", "apparatuur", "nl"),
    ("equipment", "pomp", "nl"),
    ("equipment", "ausrustung", "de"),
    ("equipment", "pumpe", "de"),
    ("annotation", "text", "en"),
    ("annotation", "dim", "en"),
    ("annotation", "anno", "en"),
    ("annotation", "note", "en"),
    ("annotation", "title", "en"),
    ("annotation", "yazi", "tr"),
    ("annotation", "olcu", "tr"),
    ("annotation", "antet", "tr"),
    ("annotation", "текст", "ru"),  # text
    ("annotation", "размер", "ru"),  # dimension
    ("annotation", "штамп", "ru"),  # title block
    ("annotation", "tekst", "nl"),
    ("annotation", "maat", "nl"),
    ("annotation", "bemassung", "de"),
    ("structure", "column", "en"),
    ("structure", "beam", "en"),
    ("structure", "steel", "en"),
    ("structure", "grid", "en"),
    ("structure", "kolon", "tr"),
    ("structure", "kiris", "tr"),
    ("structure", "celik", "tr"),
    ("structure", "aks", "tr"),
    ("structure", "колонн", "ru"),  # колонна: column
    ("structure", "балк", "ru"),  # балка: beam
    ("structure", "kolom", "nl"),
    ("structure", "balk", "nl"),
    ("structure", "staal", "nl"),
    ("structure", "stutze", "de"),
    ("structure", "trager", "de"),
    ("structure", "stahl", "de"),
)

_ROOM_WORDS = (
    ("room", "en"),
    ("oda", "tr"),
    ("mahal", "tr"),
    ("помещ", "ru"),  # помещение: room
    ("комнат", "ru"),  # комната: room
    ("ruimte", "nl"),
    ("kamer", "nl"),
    ("raum", "de"),
    ("zimmer", "de"),
)

_EQUIPMENT_KIND_WORDS = (
    ("reducer", "reducer", "en"),
    ("reducer", "reduksiyon", "tr"),
    ("reducer", "переход", "ru"),  # reducer
    ("reducer", "verloop", "nl"),
    ("reducer", "reduzier", "de"),
    ("valve", "valve", "en"),
    ("valve", "vlv", "en"),
    ("valve", "vana", "tr"),
    ("valve", "клапан", "ru"),  # valve
    ("valve", "кран", "ru"),  # valve (cock)
    ("valve", "klep", "nl"),
    ("valve", "afsluiter", "nl"),
    ("valve", "ventil", "de"),
    ("valve", "armatur", "de"),
    ("agitator", "agitator", "en"),
    ("agitator", "mixer", "en"),
    ("agitator", "karistirici", "tr"),
    ("agitator", "мешалк", "ru"),  # мешалка: agitator
    ("agitator", "roerwerk", "nl"),
    ("agitator", "ruhrwerk", "de"),
    ("pump", "pump", "en"),
    ("pump", "pompa", "tr"),
    ("pump", "насос", "ru"),  # pump
    ("pump", "pomp", "nl"),
    ("pump", "pumpe", "de"),
    ("tank", "tank", "en"),
    ("tank", "vessel", "en"),
    ("tank", "емкост", "ru"),  # емкость: vessel
    ("tank", "бак", "ru"),  # tank
    ("tank", "vat", "nl"),
    ("tank", "behalter", "de"),
    ("motor", "motor", "en"),
    ("motor", "мотор", "ru"),  # motor
    ("motor", "электродвиг", "ru"),  # электродвигатель: electric motor
    ("panel", "panel", "en"),
    ("panel", "cabinet", "en"),
    ("panel", "pano", "tr"),
    ("panel", "шкаф", "ru"),  # cabinet
    ("panel", "щит", "ru"),  # switchboard
    ("panel", "schakelkast", "nl"),
    ("panel", "kast", "nl"),
    ("panel", "schrank", "de"),
)


def fold(text: str | None) -> str:
    """Casefolded, diacritics stripped, Turkish ı made i; Cyrillic letters kept."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text).casefold().replace("ı", "i"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _words(folded: str) -> list[str]:
    return [w for w in re.split(r"[^\w]+|_|\d+", folded) if w]


def _compact(folded: str) -> str:
    return "".join(w for w in _words(folded))


def _matches(keyword: str, folded: str) -> bool:
    key = fold(keyword)
    if " " in key or len(key) >= _SUBSTRING_MIN:
        return key.replace(" ", "") in _compact(folded)
    return any(word.startswith(key) for word in _words(folded))


def _first(rows, folded: str):
    for row in rows:
        if _matches(row[-2], folded):
            return row
    return None


def _direction(folded: str) -> tuple[str, str, str] | None:
    """("supply" | "return", keyword, language), or None when neither or both."""
    supply = _first(_SUPPLY_WORDS, folded)
    ret = _first(_RETURN_WORDS, folded)
    if (supply is None) == (ret is None):
        return None
    if supply is not None:
        return ("supply", *supply)
    return ("return", *ret)


def _result(discipline, service, confidence, keyword, language) -> dict:
    return {
        "discipline": discipline,
        "service": service,
        "confidence": confidence,
        "keyword": keyword,
        "language": language,
    }


def classify_layer(name: str) -> dict:
    """What a layer most likely carries, decided by the words in its name.

    ``{"discipline", "service", "confidence", "keyword", "language"}``. Order:
    electrical words; CIP and glycol with a direction word (``P_cipsupplyline``
    -> ``cip_supply``; without one, the service stays ``unknown`` at 0.6 - the
    layer alone is not trusted to say which way a CIP line runs); the stated
    services; a water word with no temperature (piping, ``unknown``, 0.6); the
    discipline words. Nothing matched: ``unknown`` / ``unknown`` at 0.0.
    """
    folded = fold(name)
    row = _first(_ELECTRICAL_WORDS, folded)
    if row is not None:
        return _result("electrical", "electrical", DIRECT, *row)
    for rows, stem in ((_CIP_WORDS, "cip"), (_GLYCOL_WORDS, "glycol")):
        row = _first(rows, folded)
        if row is None:
            continue
        direction = _direction(folded)
        if direction is None:
            return _result("piping", "unknown", WEAK, *row)
        return _result("piping", f"{stem}_{direction[0]}", DIRECT, row[0], row[1])
    row = _first(_SERVICE_WORDS, folded)
    if row is not None:
        return _result("piping", row[0], DIRECT, row[1], row[2])
    row = _first(_WATER_WORDS, folded)
    if row is not None:
        return _result("piping", "unknown", WEAK, *row)
    row = _first(_DISCIPLINE_WORDS, folded)
    if row is not None:
        return _result(row[0], "unknown", DIRECT, row[1], row[2])
    return _result("unknown", "unknown", 0.0, None, None)


def supply_return(text: str | None) -> str | None:
    """``"supply"`` or ``"return"`` when the text says one of them, else None.

    ``ПОДАЧА`` / ``BESLEME`` / ``SUPPLY`` / ``AANVOER`` / ``VORLAUF`` are supply;
    ``ВОЗВРАТ`` / ``DÖNÜŞ`` / ``RETURN`` / ``RETOUR`` / ``RÜCKLAUF`` return. A
    text naming both (``CIP SUPPLY / RETURN``) says neither and is None.
    """
    direction = _direction(fold(text))
    return None if direction is None else direction[0]


def room_label(text: str | None) -> dict | None:
    """A room label read: ``{"number", "name", "language"}``, or None.

    A text is a room label when one of its words is a room word (*room*,
    *oda*, *mahal*, *помещение*, *комната*, *ruimte*, *kamer*, *Raum*,
    *Zimmer*). The number is the first word holding a digit; the name is what
    is left, in the text's own spelling, or None. ``"ROOM 1 PROCESS HALL"`` ->
    ``{"number": "1", "name": "PROCESS HALL", "language": "en"}``.
    """
    if not text:
        return None
    words = str(text).split()
    for index, word in enumerate(words):
        folded = fold(word)
        for keyword, language in _ROOM_WORDS:
            if folded.startswith(fold(keyword)):
                rest = words[:index] + words[index + 1 :]
                number = next((w for w in rest if any(ch.isdigit() for ch in w)), None)
                name = " ".join(w for w in rest if w != number).strip() or None
                return {"number": number, "name": name, "language": language}
    return None


def equipment_kind(block_name: str | None) -> dict | None:
    """The kind of equipment a block name says it is: ``{"kind", "keyword", "language"}``.

    ``KLEP_VLINDER`` -> valve (nl); ``VERLOOPSTUK`` -> reducer (nl); an
    anonymous ``*U12`` or a name with no keyword -> None.
    """
    folded = fold(block_name)
    row = _first(_EQUIPMENT_KIND_WORDS, folded)
    if row is None:
        return None
    return {"kind": row[0], "keyword": row[1], "language": row[2]}
