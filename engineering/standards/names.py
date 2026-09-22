"""The DXF symbol-table name rule, in one place, for the standards data.

``security.illegal_symbol_name_chars`` is the same rule at the tool layer; it
sits behind a fastmcp import and this package stays free of fastmcp, so the
rule is restated here and ``tests/test_standards_presets.py`` pins the two
character sets equal. A style, block or font name that passes here is one
ezdxf will save and AutoCAD will load; ezdxf's own check fires at
``doc.write``, long after a create tool has already said ``ok``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ILLEGAL_NAME_CHARS", "check_name", "illegal_name_chars"]

#: Characters DXF forbids in a symbol-table name, plus the whitespace that
#: ends a SendCommand segment — byte-for-byte ``security._FORBIDDEN_SYMBOL_CHARS``.
ILLEGAL_NAME_CHARS: frozenset[str] = frozenset('<>/\\":;?*|,=`') | frozenset("\n\r\t")


def illegal_name_chars(name: str) -> list[str]:
    """The characters in ``name`` a DXF symbol-table name cannot hold, sorted."""
    return sorted({c for c in name if c in ILLEGAL_NAME_CHARS or ord(c) < 32})


def check_name(key: str, value: Any, *, what: str = "name") -> str:
    """``value`` as a stripped, non-empty, legal symbol-table name, or a
    ``TypeError`` / ``ValueError`` naming ``key`` (``what`` words the refusal)."""
    if not isinstance(value, str):
        raise TypeError(f"{key}: expected a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError(f"{key}: a {what} cannot be empty")
    bad = illegal_name_chars(text)
    if bad:
        raise ValueError(f"{key}: {text!r} contains characters DXF forbids in a name: {bad}")
    return text
