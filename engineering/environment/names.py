"""One rule for the names this package stores in the drawing."""

from __future__ import annotations


def validate_name(name, *, what: str, max_len: int = 255) -> str:
    """A non-empty, stripped string without control characters, else ``ValueError``."""
    if not isinstance(name, str):
        raise ValueError(f"{what} must be a string, got {type(name).__name__}")
    clean = name.strip()
    if not clean:
        raise ValueError(f"{what} must not be empty")
    if len(clean) > max_len:
        raise ValueError(f"{what} is longer than {max_len} characters")
    if any(ord(ch) < 32 for ch in clean):
        raise ValueError(f"{what} must not contain control characters")
    return clean
