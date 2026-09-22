"""The AutoCAD preferences a model may read and write, as data.

``Preferences.*`` lives in the running application (registry-backed), not in
a drawing, so the headless engine refuses with ``capability: "preferences"``.
The whitelist is deliberately short: every key has a type and a range so a
write is refused *by name* before any ActiveX call, and the four ``Files.*``
paths are read-only because rewriting a support path is how an installation
gets broken. ``OpenSave.SaveAsType`` speaks AutoCAD's ``AcSaveAsType`` enum
by name in both directions.
"""

from __future__ import annotations

#: AcSaveAsType: each release adds 12; dwg, dxf (+1), template (+2).
SAVE_AS_TYPES: dict[str, int] = {
    "ac2000_dwg": 12,
    "ac2000_dxf": 13,
    "ac2000_template": 14,
    "ac2004_dwg": 24,
    "ac2004_dxf": 25,
    "ac2004_template": 26,
    "ac2007_dwg": 36,
    "ac2007_dxf": 37,
    "ac2007_template": 38,
    "ac2010_dwg": 48,
    "ac2010_dxf": 49,
    "ac2010_template": 50,
    "ac2013_dwg": 60,
    "ac2013_dxf": 61,
    "ac2013_template": 62,
    "ac2018_dwg": 64,
    "ac2018_dxf": 65,
    "ac2018_template": 66,
}

#: key -> (kind, spec). kind: "bool" | "int" (spec = (lo, hi)) | "str" | "enum" (spec = name -> value)
PREFERENCE_KEYS: dict[str, tuple[str, object]] = {
    "OpenSave.SaveAsType": ("enum", SAVE_AS_TYPES),
    "OpenSave.AutoSaveInterval": ("int", (0, 600)),
    "OpenSave.CreateBackup": ("bool", None),
    "OpenSave.IncrementalSavePercent": ("int", (0, 100)),
    "Display.CursorSize": ("int", (1, 100)),
    "Drafting.AutoSnapMarkerSize": ("int", (1, 20)),
    "Drafting.AutoSnapTooltip": ("bool", None),
    "Selection.PickBoxSize": ("int", (0, 50)),
    "Output.DefaultPlotStyleTable": ("str", None),
    "Output.DefaultOutputDevice": ("str", None),
}

#: Every key is ``<Preferences member>.<its member>`` verbatim, so ``split_key``
#: reaches the ActiveX property without a translation table. The spec spelled
#: the third one ``PrintStyleSheetPath``; ``IAcadPreferencesFiles`` has no such
#: member (measured on AutoCAD 2026 / R25.1: ``PrinterStyleSheetPath``), and a
#: misspelling here would make ``preferences_get()`` raise on every live call.
READ_ONLY_KEYS: tuple[str, ...] = (
    "Files.SupportPath",
    "Files.TemplateDwgPath",
    "Files.PrinterStyleSheetPath",
    "Files.PrinterConfigPath",
)


def split_key(key: str) -> tuple[str, str]:
    """``"OpenSave.AutoSaveInterval"`` → ``("OpenSave", "AutoSaveInterval")``."""
    section, _, member = key.partition(".")
    return section, member


def known_keys() -> list[str]:
    return list(PREFERENCE_KEYS) + list(READ_ONLY_KEYS)


def validate_preference(key: str, value):
    """The value to write, coerced to the key's type — or a refusal naming the key.

    Writes nothing; both engines call it before touching anything.
    """
    if key in READ_ONLY_KEYS:
        raise ValueError(f"preference {key!r} is read-only")
    if key not in PREFERENCE_KEYS:
        raise ValueError(
            f"unknown preference {key!r}; writable keys: {list(PREFERENCE_KEYS)}; "
            f"read-only keys: {list(READ_ONLY_KEYS)}"
        )
    kind, spec = PREFERENCE_KEYS[key]
    if kind == "bool":
        if not isinstance(value, bool):
            raise TypeError(f"preference {key!r} takes a bool, got {value!r}")
        return value
    if kind == "int":
        whole = isinstance(value, int) and not isinstance(value, bool)
        whole = whole or (isinstance(value, float) and value.is_integer())
        if not whole:
            raise TypeError(f"preference {key!r} takes an int, got {value!r}")
        number = int(value)
        lo, hi = spec
        if not lo <= number <= hi:
            raise ValueError(f"preference {key!r} must be between {lo} and {hi}, got {number}")
        return number
    if kind == "str":
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"preference {key!r} takes a non-empty string, got {value!r}")
        return value.strip()
    # enum
    if isinstance(value, str):
        name = value.strip().lower()
        if name in spec:
            return spec[name]
        raise ValueError(f"preference {key!r} must be one of {sorted(spec)}, got {value!r}")
    if isinstance(value, bool) or not isinstance(value, int) or value not in spec.values():
        raise ValueError(
            f"preference {key!r} must be one of {sorted(spec)} (or their values), got {value!r}"
        )
    return value


def decode_value(key: str, raw):
    """What the application returned, in the whitelist's vocabulary (enum names, real bools)."""
    kind, spec = PREFERENCE_KEYS.get(key, (None, None))
    if kind == "enum":
        for name, code in spec.items():
            if code == raw:
                return name
        return raw
    if kind == "bool":
        return bool(raw)
    if kind == "int":
        return int(raw)
    return raw


def describe_preferences() -> list[dict]:
    rows = []
    for key, (kind, spec) in PREFERENCE_KEYS.items():
        rows.append(
            {
                "key": key,
                "kind": kind,
                "range": list(spec) if kind == "int" else None,
                "enum": sorted(spec) if kind == "enum" else None,
                "writable": True,
            }
        )
    for key in READ_ONLY_KEYS:
        rows.append({"key": key, "kind": "str", "range": None, "enum": None, "writable": False})
    return rows
