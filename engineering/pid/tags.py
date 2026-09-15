"""ISA-5.1-2009 identification letters (Table 4.1) as a deterministic grammar.

    tag      := first modifier? readout* output* trailing?
    modifier := D | F | K | Q | S
    readout  := A | B | E | G | I | L | N | O | P | R | U | W | X
    output   := C | K | S | T | U | V | X | Y | Z
    trailing := HH | LL | H | L | M | O | C

The disambiguation rules (safety S vs switch S, trailing L vs light L, ZSO/ZSC,
ratio F only after F, rate-of-change K only before an output) are the ones in
the design spec; the fixture ``tests/data/isa51_tags.json`` pins every one of
them against the standard's own examples.
"""

from __future__ import annotations

import re

#: First letter → measured/initiating variable. ``None`` = user's choice.
FIRST_LETTERS: dict[str, str | None] = {
    "A": "Analysis",
    "B": "Burner",
    "C": None,
    "D": None,
    "E": "Voltage",
    "F": "Flow",
    "G": None,
    "H": "Hand",
    "I": "Current",
    "J": "Power",
    "K": "Time",
    "L": "Level",
    "M": None,
    "N": None,
    "O": None,
    "P": "Pressure",
    "Q": "Quantity",
    "R": "Radiation",
    "S": "Speed",
    "T": "Temperature",
    "U": "Multivariable",
    "V": "Vibration",
    "W": "Weight",
    "X": "Unclassified",
    "Y": "Event",
    "Z": "Position",
}
MODIFIERS: dict[str, str] = {
    "D": "Differential",
    "F": "Ratio",
    "K": "Rate of change",
    "Q": "Totalizing",
    "S": "Safety",
}
#: Readout / passive function letters → (adjective form, noun form). ``None`` = user's choice.
READOUT: dict[str, tuple[str, str] | None] = {
    "A": ("Alarm", "Alarm"),
    "B": None,
    "E": ("Element", "Element"),
    "G": ("Gauge", "Gauge"),
    "I": ("Indicating", "Indicator"),
    "L": ("Light", "Light"),
    "N": None,
    "O": ("Orifice", "Orifice"),
    "P": ("Test point", "Test point"),
    "R": ("Recording", "Recorder"),
    "U": ("Multifunction", "Multifunction"),
    "W": ("Well", "Well"),
    "X": None,
}
#: Output / active function letters → word. ``None`` = user's choice.
OUTPUT: dict[str, str | None] = {
    "C": "Controller",
    "K": "Control station",
    "S": "Switch",
    "T": "Transmitter",
    "U": "Multifunction",
    "V": "Valve",
    "X": None,
    "Y": "Relay",
    "Z": "Driver",
}
TRAILING: dict[str, str] = {
    "HH": "High-high",
    "LL": "Low-low",
    "H": "High",
    "L": "Low",
    "M": "Middle",
    "O": "Open",
    "C": "Closed",
}
DEFAULT_EQUIPMENT_PREFIXES: dict[str, str] = {
    "P": "pump",
    "V": "vessel",
    "T": "tank",
    "E": "exchanger",
    "K": "compressor",
    "C": "column",
    "R": "reactor",
    "F": "filter",
    "D": "drum",
    "M": "mixer",
    "H": "heater",
    "S": "separator",
    "B": "blower",
    "X": "package",
}

_INSTRUMENT_RE = re.compile(
    r"^(?:(?P<area>\d{1,4})-)?(?P<letters>[A-Z]{1,5})[- ]?(?P<loop>\d{1,5})(?:[- ]?(?P<suffix>[A-Z]))?$"
)
_EQUIPMENT_RE = re.compile(
    r"^(?:(?P<area>\d{1,4})-)?(?P<prefix>[A-Z]{1,3})-(?P<number>\d{1,5})(?P<suffix>[A-Z]{0,2})$"
)


def _normalise(tag) -> str:
    return str(tag or "").strip().upper()


def _parse_letters(letters: str) -> dict:
    """Split ``letters`` into first/modifier/readout/output/trailing or report the first error."""
    errors: list[str] = []
    first = letters[0]
    i, n = 1, len(letters)
    if n == 1:
        return {"errors": [f"{letters}: a tag needs at least one function letter after {first}"]}
    modifier = None
    if letters[i] in MODIFIERS:
        candidate = letters[i]
        following = letters[i + 1] if i + 1 < n else ""
        take = (
            candidate in ("D", "Q")
            or (candidate == "S" and following in ("V", "E"))
            or (candidate == "F" and first == "F")
            or (candidate == "K" and following in OUTPUT)
        )
        if take:
            modifier = candidate
            i += 1
    readout: list[str] = []
    while i < n and letters[i] in READOUT:
        letter = letters[i]
        if letter in ("H", "L", "M") and readout and readout[-1] == "A":
            break  # trailing high/low/middle after an alarm
        if readout and readout[-1] == letter:
            errors.append(f"{letters}: repeated letter {letter} at position {i + 1}")
            return {"errors": errors}
        readout.append(letter)
        i += 1
    output: list[str] = []
    while i < n and letters[i] in OUTPUT:
        letter = letters[i]
        if letter == "C" and output and output[-1] == "S" and i == n - 1:
            break  # ZSC: closed, not controller
        if output and output[-1] == letter:
            errors.append(f"{letters}: repeated letter {letter} at position {i + 1}")
            return {"errors": errors}
        if output and output[-1] == "V":
            # A valve is a final control element: nothing acts after it (PSVX is not a tag).
            errors.append(
                f"{letters}: unexpected letter {letter} at position {i + 1} after valve V"
            )
            return {"errors": errors}
        output.append(letter)
        i += 1
    trailing = letters[i:]
    if trailing:
        if trailing not in TRAILING:
            errors.append(f"{letters}: unexpected letter {trailing[0]} at position {i + 1}")
            return {"errors": errors}
        after_alarm = bool(readout) and readout[-1] == "A" and not output
        after_switch = bool(output) and output[-1] == "S"
        if trailing in ("O", "C") and not (after_switch and first == "Z"):
            # Open/closed are position states: ZSO / ZSC only (LSO is not a tag).
            errors.append(
                f"{letters}: trailing {trailing} (open/closed) is only valid after a switch "
                "on a position (Z) tag"
            )
            return {"errors": errors}
        if trailing in ("H", "L", "M", "HH", "LL") and not (after_alarm or after_switch):
            errors.append(
                f"{letters}: trailing {trailing} needs an alarm (A) or switch (S) before it"
            )
            return {"errors": errors}
    return {
        "first": first,
        "modifier": modifier,
        "readout": readout,
        "output": output,
        "trailing": trailing or None,
        "errors": [],
    }


def _describe(parts: dict) -> tuple[str, bool]:
    words: list[str] = []
    # A user's-choice first letter (C, D, G, M, N, O) contributes no word; X keeps
    # its "Unclassified" word but is just as much the user's own meaning.
    user_defined = parts["first"] == "X"
    first_word = FIRST_LETTERS[parts["first"]]
    if first_word is None:
        user_defined = True
    else:
        words.append(first_word)
    if parts["modifier"]:
        words.append(MODIFIERS[parts["modifier"]])
    readout, output = parts["readout"], parts["output"]
    for index, letter in enumerate(readout):
        forms = READOUT[letter]
        if forms is None:
            user_defined = True
            continue
        last_function = index == len(readout) - 1 and not output
        words.append(forms[1] if last_function else forms[0])
    for index, letter in enumerate(output):
        word = OUTPUT[letter]
        if word is None:
            user_defined = True
            continue
        if letter == "C" and index + 1 < len(output) and output[index + 1] == "V":
            word = "Control"
        words.append(word)
    if parts["trailing"]:
        words.append(TRAILING[parts["trailing"]])
    return " ".join(words), user_defined


def _parse_instrument(raw: str) -> dict:
    out = {
        "tag": raw,
        "valid": False,
        "kind": "instrument",
        "area": None,
        "letters": None,
        "first": None,
        "modifier": None,
        "readout": [],
        "output": [],
        "trailing": None,
        "loop": None,
        "suffix": None,
        "description": None,
        "user_defined": False,
        "errors": [],
    }
    tag = _normalise(raw)
    match = _INSTRUMENT_RE.match(tag)
    if not match:
        out["errors"] = [f"{raw!r}: not in [area-]LETTERS[-]loop[suffix] format"]
        return out
    out.update(
        area=match.group("area"),
        letters=match.group("letters"),
        loop=match.group("loop"),
        suffix=match.group("suffix"),
    )
    parts = _parse_letters(match.group("letters"))
    if parts["errors"]:
        out["errors"] = parts["errors"]
        return out
    description, user_defined = _describe(parts)
    out.update(
        valid=True,
        first={"letter": parts["first"], "meaning": FIRST_LETTERS[parts["first"]]},
        modifier=(
            {"letter": parts["modifier"], "meaning": MODIFIERS[parts["modifier"]]}
            if parts["modifier"]
            else None
        ),
        readout=[
            {"letter": letter, "meaning": (READOUT[letter] or (None, None))[1]}
            for letter in parts["readout"]
        ],
        output=[{"letter": letter, "meaning": OUTPUT[letter]} for letter in parts["output"]],
        trailing=parts["trailing"],
        description=description,
        user_defined=user_defined,
    )
    return out


def _parse_equipment(raw: str, prefixes: dict[str, str]) -> dict:
    out = {
        "tag": raw,
        "valid": False,
        "kind": "equipment",
        "area": None,
        "prefix": None,
        "equipment_kind": None,
        "number": None,
        "suffix": "",
        "errors": [],
    }
    match = _EQUIPMENT_RE.match(_normalise(raw))
    if not match:
        out["errors"] = [f"{raw!r}: not in [area-]PREFIX-number[suffix] format"]
        return out
    prefix = match.group("prefix")
    out.update(
        area=match.group("area"),
        prefix=prefix,
        number=match.group("number"),
        suffix=match.group("suffix") or "",
    )
    kind = prefixes.get(prefix)
    if kind is None:
        out["errors"] = [f"{raw!r}: prefix {prefix} is not in the equipment prefix map"]
        return out
    out.update(valid=True, equipment_kind=kind)
    return out


def parse_tag(tag: str, kind: str = "auto", equipment_prefixes: dict | None = None) -> dict:
    """Parse an ISA-5.1 instrument tag or an equipment tag.

    ``kind``: ``"instrument"``, ``"equipment"`` or ``"auto"`` (instrument first,
    then equipment, else the instrument parse with its errors).
    """
    prefixes = {k.upper(): v for k, v in (equipment_prefixes or DEFAULT_EQUIPMENT_PREFIXES).items()}
    if kind == "instrument":
        return _parse_instrument(tag)
    if kind == "equipment":
        return _parse_equipment(tag, prefixes)
    if kind != "auto":
        raise ValueError("kind must be 'auto', 'instrument' or 'equipment'")
    instrument = _parse_instrument(tag)
    if instrument["valid"]:
        return instrument
    equipment = _parse_equipment(tag, prefixes)
    return equipment if equipment["valid"] else instrument


def split_instrument_tag(tag: str) -> tuple[str, str]:
    """``FUNC`` and ``LOOP`` attribute texts for a bubble: letters, and loop+suffix.
    Anything unparseable goes wholesale into ``FUNC`` so nothing is dropped."""
    match = _INSTRUMENT_RE.match(_normalise(tag))
    if not match:
        return _normalise(tag), ""
    return match.group("letters"), match.group("loop") + (match.group("suffix") or "")


def describe_tables() -> dict:
    """The letter tables, for the ``autocad://standards/isa51`` resource."""
    return {
        "standard": "ISA-5.1-2009 Table 4.1",
        "grammar": "first modifier? readout* output* trailing?",
        "first_letters": {k: v or "user's choice" for k, v in FIRST_LETTERS.items()},
        "modifiers": dict(MODIFIERS),
        "readout": {k: (v[1] if v else "user's choice") for k, v in READOUT.items()},
        "output": {k: v or "user's choice" for k, v in OUTPUT.items()},
        "trailing": dict(TRAILING),
        "equipment_prefixes": dict(DEFAULT_EQUIPMENT_PREFIXES),
        "rules": [
            "S in second position is the safety modifier only before V or E (PSV, TSE); otherwise a switch.",
            "D in second position is always differential.",
            "F in second position is ratio only when the first letter is F (FFC).",
            "K in second position is rate of change only before an output letter.",
            "H/L/M after an alarm (A) or switch (S) are trailing high/low/middle; L elsewhere is a light.",
            "O/C as the last letter after a switch on a position (Z) tag are open/closed (ZSO, ZSC).",
            "V (valve) is a final element: no output letter may follow it.",
            "A first letter in C/D/G/M/N/O/X or a function letter in B/N/X marks the tag user_defined.",
        ],
    }
