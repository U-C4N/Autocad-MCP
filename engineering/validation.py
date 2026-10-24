"""Shared spec-input guards: every refusal names the offending path (B07).

Lifted from ``engineering/mech/part.py``'s private guards so the P&ID, arch,
mechanical and block-definition specs refuse the same inputs the same way:

    equipment[2].scale: must be greater than zero, got -1.0.

``SpecError`` is a ``ValueError``, so every existing ``except ValueError`` (and
the server's refusal mapping) keeps working. A value echoed back in a message
is shortened with ``reprlib`` — a 2 MB string must not become a 2 MB error.
``within_budget`` names the setting that raises its limit, because a count
over a budget is not malformed input, it is input bigger than this server was
configured to draw.

Pure: no backend, no I/O.
"""

from __future__ import annotations

import math
import reprlib
from typing import Any

__all__ = [
    "SpecError",
    "finite",
    "int_in_range",
    "non_negative",
    "positive",
    "short_repr",
    "within_budget",
]

#: The tolerance ``positive`` / ``non_negative`` allow around zero (the value
#: ``engineering/mech/part.py`` has always used).
EPS = 1e-9

_REPR = reprlib.Repr()
_REPR.maxstring = 80
_REPR.maxother = 80
_REPR.maxlong = 40
_REPR.maxlist = 6
_REPR.maxdict = 6


class SpecError(ValueError):
    """A spec value refused before anything is drawn; the message starts with its path."""


def short_repr(value: Any) -> str:
    """``repr(value)``, at most about 80 characters."""
    return _REPR.repr(value)


def finite(value: Any, path: str) -> float:
    """``value`` as a finite float; bools, strings and non-numbers are refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{path}: expected a number, got {short_repr(value)}.")
    try:
        number = float(value)
    except OverflowError:  # a 400-digit int is a number, but not a float
        raise SpecError(f"{path}: {short_repr(value)} is not a finite number.") from None
    if not math.isfinite(number):
        raise SpecError(f"{path}: {short_repr(value)} is not a finite number.")
    return number


def positive(value: Any, path: str) -> float:
    """A finite number greater than zero (more than ``EPS``)."""
    number = finite(value, path)
    if number <= EPS:
        raise SpecError(f"{path}: must be greater than zero, got {number}.")
    return number


def non_negative(value: Any, path: str) -> float:
    """A finite number that is not negative; ``-EPS..0`` reads as ``0.0``."""
    number = finite(value, path)
    if number < -EPS:
        raise SpecError(f"{path}: must not be negative, got {number}.")
    return max(number, 0.0)


def int_in_range(value: Any, path: str, lo: int, hi: int) -> int:
    """An ``int`` (never a bool, never a float) with ``lo <= value <= hi``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{path}: expected an integer, got {short_repr(value)}.")
    if not lo <= value <= hi:
        raise SpecError(f"{path}: must be from {lo} to {hi}, got {short_repr(value)}.")
    return value


def within_budget(count: int, limit: int, path: str, setting: str) -> None:
    """Refuse ``count`` above ``limit``, naming ``setting`` (the env var that
    raises the limit). ``limit <= 0`` disables the budget."""
    if limit > 0 and count > limit:
        raise SpecError(
            f"{path}: {count} is over the limit of {limit}; set {setting} higher to allow "
            "more (0 disables the limit)."
        )
