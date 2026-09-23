"""Registry for the authored standards tables under ``engineering/mech/standards``.

Every table registers itself at import with the standard's name, its rows, the
coverage it was transcribed for and a SOURCE line naming standard, edition and
table. ``lookup`` returns a row or refuses by name: a size outside the coverage
is refused against the coverage, and a size *inside* it that is not a listed row
is refused against the rows. Nothing is ever interpolated — the rule
`engineering/fits.py` (ISO 286) already follows, applied to every table here. A
wrong row ships a wrong workshop drawing; a narrow table is merely narrow.

``rows`` maps a size key (a number in millimetres, or a designation like
``"M12"``) to a row dict, or to a tuple of row dicts when the standard has
variants at the same size (DIN 509 forms E and F, DIN 332 forms A/B/R). ``**kw``
selects among those variants and is refused when it selects none or more than
one — an ambiguous lookup never quietly picks the first row.

``rows`` may be **empty**: that is a table whose structure, coverage and SOURCE
ship before any row could be verified against the standard (DIN 509, DIN 471,
DIN 472, DIN 332-1 and ISO 3601-2 in this build). Registering it keeps the
later transcription a data edit, and every lookup against it is refused by
name — which is exactly the table rule, not an exception to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class Coverage(NamedTuple):
    low: float
    high: float
    unit: str = "mm"


@dataclass(frozen=True)
class _Table:
    standard: str
    rows: dict
    coverage: Coverage
    source: str


_REGISTRY: dict[str, _Table] = {}


def _key(value):
    if isinstance(value, bool):
        raise TypeError("standards: a size key cannot be a bool")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return value.strip().upper()
    raise TypeError(f"standards: a size key must be a number or a designation, got {value!r}")


def _variants(value) -> tuple[dict, ...]:
    if isinstance(value, dict):
        return (dict(value),)
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, dict) for v in value):
        return tuple(dict(v) for v in value)
    raise TypeError("standards: a row must be a dict, or a non-empty tuple of dicts for variants")


def register(standard: str, rows: dict, coverage: Coverage, source: str) -> None:
    """Add a table. Refuses a duplicate name and a SOURCE that names no table."""
    if not isinstance(standard, str) or not standard.strip():
        raise ValueError("standards.register: standard must be a non-empty name")
    if standard in _REGISTRY:
        raise ValueError(f"standards.register: {standard!r} is already registered")
    if not isinstance(rows, dict):
        raise ValueError(
            f"standards.register: {standard!r} must register a rows dict; an empty one is "
            "the table whose structure ships before its rows are transcribed"
        )
    if not isinstance(source, str) or "table" not in source.lower():
        raise ValueError(
            f"standards.register: {standard!r} needs a SOURCE naming the standard, its "
            "edition and the table its rows were transcribed from"
        )
    _REGISTRY[standard] = _Table(
        standard, {_key(k): _variants(v) for k, v in rows.items()}, coverage, source
    )


def standards() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def _table(standard: str) -> _Table:
    table = _REGISTRY.get(standard)
    if table is None:
        known = ", ".join(standards()) or "(none registered)"
        raise ValueError(f"standards: {standard!r} is not registered; known: {known}")
    return table


def coverage_of(standard: str) -> Coverage:
    return _table(standard).coverage


def source_of(standard: str) -> str:
    return _table(standard).source


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _listing(table: _Table) -> str:
    keys = sorted(table.rows, key=lambda k: (isinstance(k, str), k))
    shown = [k if isinstance(k, str) else _num(k) for k in keys[:12]]
    return ", ".join(shown) + (" ..." if len(keys) > 12 else "")


def _kw_text(kw: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in sorted(kw.items()))


def lookup(standard: str, size, **kw) -> dict:
    """One row, or a ValueError naming the standard and what it does cover."""
    table = _table(standard)
    key = _key(size)
    rows = table.rows.get(key)
    if rows is None:
        if isinstance(key, float) and not (table.coverage.low <= key <= table.coverage.high):
            unit = table.coverage.unit
            raise ValueError(
                f"{standard} covers {_num(table.coverage.low)}-{_num(table.coverage.high)} "
                f"{unit}; {_num(key)} {unit} is outside the table"
            )
        raise ValueError(f"{standard} has no row for {size!r}; the table lists {_listing(table)}")
    matches = [row for row in rows if all(row.get(k) == v for k, v in kw.items())]
    if not matches:
        offered = sorted({str(row.get(k)) for row in rows for k in kw})
        raise ValueError(
            f"{standard} has no {_kw_text(kw)} row for {size!r}; it lists {', '.join(offered)}"
        )
    if len(matches) > 1:
        discriminators = sorted({k for row in rows for k in row} - set(kw))
        raise ValueError(
            f"{standard} row for {size!r} is ambiguous: {len(matches)} variants match; "
            f"narrow it with one of {', '.join(discriminators)}"
        )
    return dict(matches[0])
