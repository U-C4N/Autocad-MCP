"""Label parsers for foreign drawings: diameters, equipment tags, panels, electrical data.

Pure functions over one string each - no ezdxf, no I/O - so every rule here is
tested on the spellings real drawings use, and the
readers above it (the network, the takeoffs, the scale check) share one
reading of a label instead of three.

What a label *says* is kept exactly as drawn. ``Ø51``, ``SMS51``, ``DN20``,
``Ø25,4`` and ``Ø25`` are five different sizes: the parser normalises only the
spelling of the diameter sign and the whitespace, never the number, and never
equates two sizing systems (an SMS size and a DN size are different series; the
drawing has to say they are the same pipe, not this module).

Two kinds of foreign-drawing noise are decoded before anything is parsed:

* **AutoCAD text codes.** ``%%c`` is AutoCAD's diameter sign, ``%%d`` the degree
  sign, ``%%p`` plus-minus; ``\\U+2205`` is a Unicode escape (AutoCAD's help
  lists U+2205 as its diameter symbol). MTEXT carries format runs
  (``{\\fArial|b0;Ø51}``, ``\\H2.5;``, ``\\P`` for a new paragraph) that are
  formatting, not content.
* **Cyrillic look-alikes in tags.** A Russian-labelled P&ID typed on a Russian
  keyboard writes ``Т4100`` with a Cyrillic Т (U+0422); it must match the
  layout's Latin ``T4100``. Eleven capitals have a Latin twin of identical
  shape (Т М С Р А В Е К Н О Х) and are folded; a Cyrillic letter without one
  (П, Щ, Ш, ...) is left alone, so a Russian word never turns into a tag.
"""

from __future__ import annotations

import re

__all__ = [
    "CYRILLIC_LOOKALIKES",
    "fold_lookalikes",
    "normalize_panel",
    "parse_diameters",
    "parse_electrical",
    "parse_tag",
    "plain",
    "wiring_target",
]

#: Cyrillic capitals drawn identically to a Latin capital, and that capital.
CYRILLIC_LOOKALIKES: dict[str, str] = {
    "\u0422": "T",  # Т
    "\u041c": "M",  # М
    "\u0421": "C",  # С
    "\u0420": "P",  # Р
    "\u0410": "A",  # А
    "\u0412": "B",  # В
    "\u0415": "E",  # Е
    "\u041a": "K",  # К
    "\u041d": "H",  # Н
    "\u041e": "O",  # О
    "\u0425": "X",  # Х
}
_LOOKALIKE_TABLE = str.maketrans(CYRILLIC_LOOKALIKES)

#: ``%%`` codes that stand for a character (AutoCAD's control codes).
_PERCENT_CHARS = {"c": "\u00d8", "d": "\u00b0", "p": "\u00b1"}
#: MTEXT codes that carry an argument up to the next ``;`` and draw nothing.
_ARG_CODES = frozenset("fFHWQTACcp")
#: MTEXT on/off toggles (underline, overline, strike-through, column break).
_TOGGLE_CODES = frozenset("LlOoKkN")

_UNICODE_ESCAPE = re.compile(r"\\[Uu]\+([0-9A-Fa-f]{4})")
_PERCENT_CODE = re.compile(r"%%(\d{3}|[cdpCDP]|[uoUOkK])")
#: Caret notation, how a TEXT or ATTRIB stores a control character: ^J a line
#: break, ^M a carriage return (read as a break), ^I a tab, and "^ " a caret.
_CARET = re.compile(r"\^([JMI ])")
_CARET_CHARS = {"J": "\n", "M": "\n", "I": "\t", " ": "^"}

#: Every spelling of the diameter sign met in drawings: Ø (U+00D8), ø (U+00F8),
#: ∅ (U+2205, AutoCAD's listed diameter symbol), ⌀ (U+2300,
#: the Unicode DIAMETER SIGN).
_DIAMETER_SIGNS = "\u00d8\u00f8\u2205\u2300"
_NUMBER = r"\d+(?:[.,]\d+)?"
_DIAMETER_RE = re.compile(
    rf"(?P<sign>[{_DIAMETER_SIGNS}])\s*(?P<sign_n>{_NUMBER})"
    rf"|(?<![A-Za-z])(?P<sms>SMS)\s*-?\s*(?P<sms_n>{_NUMBER})"
    rf"|(?<![A-Za-z])(?P<dn>DN)\s*-?\s*(?P<dn_n>\d+)"
    rf"|(?<![\d.,])(?P<a>{_NUMBER})\s*[xX\u00d7\u0445\u0425]\s*(?P<b>{_NUMBER})(?![.,]?\d)"
    r"(?!\s*(?:mm2|mm\u00b2|\u043c\u043c2|\u043c\u043c\u00b2))"
    rf"|(?<![\d.,])(?P<inch>\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?)\s*(?:\"|''|\u201d|\u2033)",
    re.IGNORECASE,
)

#: A token that may be a tag: starts with a letter, keeps letters, digits, dots
#: and hyphens (``CP-M82``, ``M47.1``).
_TAG_TOKEN = re.compile(r"[A-Za-z\u0400-\u04ff][A-Za-z0-9\u0400-\u04ff.\-]*")
#: An equipment tag: 1-4 letters, an optional second letter group after a
#: hyphen (``CP-M82``), an optional hyphen, 1-5 digits, an optional letter
#: suffix (``M41A``) and an optional ``.n`` (``M47.1``).
_TAG_RE = re.compile(r"[A-Z]{1,4}(?:-[A-Z]{1,4})?-?\d{1,5}[A-Z]?(?:\.\d{1,3})?")
#: Prefixes that look like a tag and are not one: nominal sizes (DN20, SMS51),
#: a pressure rating (PN16) and an ingress-protection code (IP65).
_NOT_TAG_PREFIXES = ("DN", "SMS", "PN", "IP")

_PANEL_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z]{1,4})\s*[-_ ]?\s*((?:[A-Z]{1,3})?\d{1,4}[A-Z]?)(?![A-Z0-9])"
)

_KW_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:kw|\u043a\u0432\u0442)(?![a-z\u0430-\u044f])", re.IGNORECASE
)
_PHASE_RE = re.compile(
    r"(?<!\d)([13])\s*(?:\u0444\b\.?|ph\b\.?|phase|faz|~|/\s*n\b)", re.IGNORECASE
)
_VOLT_RE = re.compile(
    r"(?<![\d.,])(\d{2,4})\s*(?:v(?:ac|dc)?|\u0432)(?![a-z\u0430-\u044f])", re.IGNORECASE
)
_NEUTRAL_RE = re.compile(r"(?:\+|/)\s*n(?![a-z])", re.IGNORECASE)
#: Words that say a load is driven by a frequency converter, EN/TR/RU/NL/DE.
_VFD_WORDS = (
    "vfd",
    "vsd",
    "inverter",
    "frekans",
    "\u0447\u0430\u0441\u0442\u043e\u0442\u043d",  # частотн(ый преобразователь)
    "\u0447\u0440\u043f",  # ЧРП
    "frequentieregelaar",
    "frequenzumrichter",
)
#: Phrases that introduce the panel a load is wired to, EN/TR/RU/NL/DE.
_WIRING_PHRASES = (
    "wiring to",
    "wired to",
    "cable to",
    "connected to",
    "\u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 \u043a",  # подключение к
    "\u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043a",  # подключить к
    "bedrading naar",
    "verdrahtung zu",
    "verdrahtung nach",
)
#: Turkish puts the panel first: ``CP1 panosuna`` ("to the CP1 panel").
_WIRING_PHRASES_AFTER = ("panosuna", "panosundan")
#: The bare Russian preposition к / ко ("to") with the panel as the next word,
#: optionally after "щиту" / "шкафу" / "панели" (switchboard / cabinet / panel):
#: a Russian P&ID closes a load's text with ``к CP1``. Only a whole word counts,
#: so the к of кВт is no preposition.
_RU_TO_PANEL = re.compile(
    r"(?:^|\s)ко?\s+"
    r"(?:(?:щиту|шкафу|панели)\s+)?",
    re.IGNORECASE,
)


def plain(text: str | None) -> str:
    """The text a reader sees: codes decoded, MTEXT formatting removed.

    ``\\U+XXXX`` becomes its character; ``%%c`` / ``%%d`` / ``%%p`` become Ø / ° /
    ±, ``%%nnn`` the character with that code, ``%%%`` a percent sign, and the
    ``%%u`` / ``%%o`` / ``%%k`` toggles disappear, and the caret notation of a
    TEXT (``^J`` a line break, ``^I`` a tab, ``^ `` a caret) is decoded. In
    MTEXT, ``\\P`` is a line break, ``\\~`` a space, ``\\S a^b;`` / ``\\S a/b;`` the stacked ``a/b``,
    ``\\\\`` ``\\{`` ``\\}`` the literal characters; every argument code
    (``\\f...;``, ``\\H...;``, ``\\C...;`` ...) and every toggle is dropped, and so
    are the grouping braces.
    """
    if not text:
        return ""
    source = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), str(text))
    source = _CARET.sub(lambda m: _CARET_CHARS[m.group(1)], source)
    source = source.replace("%%%", "\x00")

    def percent(match: re.Match) -> str:
        code = match.group(1)
        if code.isdigit():
            return chr(int(code))
        return _PERCENT_CHARS.get(code.lower(), "")

    source = _PERCENT_CODE.sub(percent, source).replace("\x00", "%")
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch == "\\" and i + 1 < n:
            code = source[i + 1]
            if code in "\\{}":
                out.append(code)
                i += 2
            elif code == "P":
                out.append("\n")
                i += 2
            elif code == "~":
                out.append(" ")
                i += 2
            elif code == "S":
                end = source.find(";", i + 2)
                end = n if end < 0 else end
                out.append(re.sub(r"[\^#]", "/", source[i + 2 : end]))
                i = end + 1
            elif code in _ARG_CODES:
                end = source.find(";", i + 2)
                i = n if end < 0 else end + 1
            elif code in _TOGGLE_CODES:
                i += 2
            else:
                out.append(code)
                i += 2
        elif ch in "{}":
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out).strip()


def parse_diameters(text: str | None) -> tuple[str, ...]:
    """Every size label in ``text``, in order, each once, spelled one way.

    ``Ø51`` / ``%%c51`` / ``\\U+2205 51`` / ``⌀51`` / ``ø51`` -> ``"Ø51"``;
    ``Ø25,4`` keeps its comma; ``SMS 51`` -> ``"SMS51"``; ``DN 20`` ->
    ``"DN20"``; ``20 x 27`` / ``20х27`` (Cyrillic х) -> ``"20x27"``; ``1,5"`` ->
    ``'1,5"'``. An ``a x b`` followed by mm² is a cable cross-section, not a
    pipe, and is skipped.
    """
    found: list[str] = []
    for match in _DIAMETER_RE.finditer(plain(text)):
        if match.group("sign"):
            token = "\u00d8" + match.group("sign_n")
        elif match.group("sms"):
            token = "SMS" + match.group("sms_n")
        elif match.group("dn"):
            token = "DN" + match.group("dn_n")
        elif match.group("a"):
            token = f"{match.group('a')}x{match.group('b')}"
        else:
            token = re.sub(r"\s+", "", match.group("inch")) + '"'
        if token not in found:
            found.append(token)
    return tuple(found)


def fold_lookalikes(text: str) -> str:
    """Uppercase, with the eleven Cyrillic look-alike capitals made Latin."""
    return str(text).upper().translate(_LOOKALIKE_TABLE)


def parse_tag(text: str | None) -> str | None:
    """The first equipment tag in ``text``, uppercase, look-alikes folded.

    ``"Т4100"`` (Cyrillic Т) -> ``"T4100"``; ``"pump M41A"`` -> ``"M41A"``;
    ``"CP-M82"`` stays ``"CP-M82"``. A size label (``DN20``, ``SMS51``), a
    pressure rating (``PN16``) or an IP code (``IP65``) is not a tag; a token
    that keeps a Cyrillic letter without a Latin twin is not one either, and
    neither is the tail of a word that begins with a digit - a line number
    such as ``100-P-001`` names a pipe, not the pump ``P-001``.
    """
    source = plain(text)
    for match in _TAG_TOKEN.finditer(source):
        word_start = max(source.rfind(" ", 0, match.start()), source.rfind("\n", 0, match.start()))
        if source[word_start + 1 : match.start()][:1].isdigit():
            continue
        token = fold_lookalikes(match.group(0)).rstrip(".-")
        if not _TAG_RE.fullmatch(token):
            continue
        if re.match(r"[A-Z]+", token).group(0) in _NOT_TAG_PREFIXES:
            continue
        return token
    return None


def normalize_panel(text: str | None) -> str | None:
    """A panel name spelled one way: ``CP 1`` / ``CP1`` / ``cp-1`` -> ``CP-1``.

    The letters, one hyphen, then the rest: ``CP M82`` -> ``CP-M82``. Cyrillic
    look-alikes are folded first (``СР-1`` typed in Cyrillic is ``CP-1``).
    Returns None when the text holds no panel-shaped token.
    """
    if not text:
        return None
    match = _PANEL_RE.search(fold_lookalikes(plain(text)))
    if match is None:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def parse_electrical(text: str | None) -> dict:
    """Power, phases, voltage, neutral and drive type stated in ``text``.

    ``"P = 18,5 кВт, 3 ф. 400 В + N + PE, ЧРП"`` ->
    ``{"kw": 18.5, "phases": 3, "voltage": 400, "neutral": True, "vfd": True}``.
    What the text does not state stays ``None`` / ``False``: a power that is not
    written is never estimated.
    """
    body = plain(text)
    lowered = body.lower()
    kw = _KW_RE.search(body)
    phases = _PHASE_RE.search(body)
    volts = _VOLT_RE.search(body)
    return {
        "kw": float(kw.group(1).replace(",", ".")) if kw else None,
        "phases": int(phases.group(1)) if phases else None,
        "voltage": int(volts.group(1)) if volts else None,
        "neutral": bool(_NEUTRAL_RE.search(body)),
        "vfd": any(word in lowered for word in _VFD_WORDS),
    }


def wiring_target(text: str | None) -> str | None:
    """The panel a wiring callout points to, normalised.

    ``"Wiring to CP1"`` -> ``"CP-1"``; ``"подключение к шкафу CP-1"`` ->
    ``"CP-1"``; ``"CP 2 panosuna"`` -> ``"CP-2"`` (Turkish names the panel
    first); ``"... кВт\\nк CP1"`` -> ``"CP-1"`` (the bare Russian "to", the
    panel as the next word). None when the text has no wiring phrase or no
    panel beside it.
    """
    body = plain(text)
    lowered = body.lower()
    for phrase in _WIRING_PHRASES:
        at = lowered.find(phrase)
        if at >= 0:
            panel = normalize_panel(body[at + len(phrase) :])
            if panel is not None:
                return panel
    for phrase in _WIRING_PHRASES_AFTER:
        at = lowered.find(phrase)
        if at > 0:
            matches = list(_PANEL_RE.finditer(fold_lookalikes(body[:at])))
            if matches:
                return f"{matches[-1].group(1)}-{matches[-1].group(2)}"
    for to in _RU_TO_PANEL.finditer(body):
        panel = _PANEL_RE.match(fold_lookalikes(body[to.end() :]))
        if panel is not None:
            return f"{panel.group(1)}-{panel.group(2)}"
    return None
