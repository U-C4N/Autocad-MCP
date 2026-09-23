"""English / Turkish vocabulary for labels and schedules, and the number format.

``lang="en"`` is the default; ``lang="tr"`` switches the words *and* the
decimal separator to a comma (``24,50 m²``), the user's decision of
2026-09-23. Any other value is refused with the list, never silently
rendered in English.

The Turkish words are the ones a Turkish drawing office writes on a plan
(``MAHAL LİSTESİ`` for a room schedule, ``KOT`` for a level, ``ÇIKIŞ`` for
the up-arrow of a stair); they are vocabulary, not a standard.
"""

from __future__ import annotations

import math

LANGS: tuple[str, ...] = ("en", "tr")

_VOCAB: dict[str, dict[str, str]] = {
    "en": {
        "door_schedule": "DOOR SCHEDULE",
        "window_schedule": "WINDOW SCHEDULE",
        "room_schedule": "ROOM SCHEDULE",
        "tag": "TAG",
        "width": "WIDTH",
        "height": "HEIGHT",
        "sill": "SILL",
        "swing": "SWING",
        "hand": "HAND",
        "wall": "WALL",
        "number": "NO.",
        "name": "NAME",
        "area": "AREA",
        "total": "TOTAL",
        "up": "UP",
        "down": "DN",
        "north": "N",
        "level": "LEVEL",
        "in": "IN",
        "out": "OUT",
        "left": "LEFT",
        "right": "RIGHT",
    },
    "tr": {
        "door_schedule": "KAPI LİSTESİ",
        "window_schedule": "PENCERE LİSTESİ",
        "room_schedule": "MAHAL LİSTESİ",
        "tag": "KOD",
        "width": "GENİŞLİK",
        "height": "YÜKSEKLİK",
        "sill": "PARAPET",
        "swing": "AÇILIŞ",
        "hand": "YÖN",
        "wall": "DUVAR",
        "number": "NO",
        "name": "MAHAL ADI",
        "area": "ALAN",
        "total": "TOPLAM",
        "up": "ÇIKIŞ",
        "down": "İNİŞ",
        "north": "K",
        "level": "KOT",
        "in": "İÇE",
        "out": "DIŞA",
        "left": "SOL",
        "right": "SAĞ",
    },
}

_TAG_PREFIX: dict[str, dict[str, str]] = {
    "en": {"door": "D", "window": "W"},
    "tr": {"door": "K", "window": "P"},
}


def _lang(lang: str) -> str:
    if not isinstance(lang, str) or lang.strip().lower() not in LANGS:
        raise ValueError(f"lang: unknown language {lang!r}; languages are {', '.join(LANGS)}")
    return lang.strip().lower()


def vocab(lang: str) -> dict[str, str]:
    """Every word a label or schedule writes, in ``lang``. A copy: callers may not edit it."""
    return dict(_VOCAB[_lang(lang)])


def tag_prefix(kind: str, lang: str) -> str:
    """``door`` -> ``D`` / ``K`` (kapı), ``window`` -> ``W`` / ``P`` (pencere)."""
    table = _TAG_PREFIX[_lang(lang)]
    if kind not in table:
        raise ValueError(f"kind: unknown opening kind {kind!r}; kinds are door, window")
    return table[kind]


def fmt_number(value: float, lang: str, decimals: int = 2) -> str:
    """``24.5`` -> ``"24.50"`` (en) / ``"24,50"`` (tr). No thousands separator."""
    code = _lang(lang)
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        raise ValueError(f"decimals: expected a whole number >= 0, got {decimals!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"value: expected a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"value: must be finite, got {value!r}")
    text = f"{number:.{decimals}f}"
    if text.startswith("-") and float(text) == 0.0:
        text = text[1:]  # never print "-0.00"
    return text.replace(".", ",") if code == "tr" else text


def fmt_area_m2(area_mm2: float, lang: str) -> str:
    """An area measured in mm² written in m² with two decimals: ``"24.50 m²"`` / ``"24,50 m²"``."""
    try:
        area = float(area_mm2)
    except (TypeError, ValueError):
        raise ValueError(f"area: expected a number, got {area_mm2!r}") from None
    if not math.isfinite(area) or area < 0.0:
        raise ValueError(f"area: must be finite and not negative, got {area_mm2!r}")
    return f"{fmt_number(area / 1.0e6, lang, 2)} m²"
