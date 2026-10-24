"""The standards coverage manifest: what each registered table holds, and from where.

One :class:`TableCoverage` record per table in the registry of
``engineering/mech/standards/__init__.py``: the standard, the edition and table
its rows were transcribed from (quoted from the table's own SOURCE line), the
sizes actually implemented, the range those sizes cover, the SOURCE locator,
who transcribed and reviewed it, and what may be redistributed.

The feature tables that ship with no rows in this build (DIN 509, DIN 471/472,
DIN 332-1, ISO 3601-2) report ``"no rows transcribed"`` and no range: their
registered ``Coverage(0, 0)`` is a placeholder, not a coverage, and reporting
it as ``0-0 mm`` would read as a table that covers nothing *by design*.

``transcriber`` and ``reviewer`` are ``None`` for every table: the repository
records no named transcriber or reviewer, and none is invented here. A table
registered without an entry in :data:`_LOCATORS` (a later table, a test
fixture) still gets its record; its edition and table locator are then parsed
from its SOURCE line, or reported as ``"not stated"``.

``std_part_list`` returns :func:`coverage_manifest` under ``coverage`` and the
static resource ``autocad://standards/coverage`` serves it without a backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Every module that registers a table is imported here, so the manifest never
# depends on which tool happened to import which table first. The names are
# referenced in _TABLE_MODULES to keep the imports explicit (and ruff quiet).
from engineering.mech.standards import (
    _REGISTRY,
    bearings,
    centres,
    fasteners,
    orings,
    rings,
    standards,
    threads,
    undercuts,
)
from engineering.mech.standards import _num as _number_text

_TABLE_MODULES = (bearings, centres, fasteners, orings, rings, threads, undercuts)

SCHEMA_VERSION = 1

NO_ROWS = "no rows transcribed"
TRANSCRIBED = "transcribed"
NOT_STATED = "not stated"

#: Edition, table locator and the quantity the coverage range measures, for every
#: table this build registers. Edition and locator are quoted verbatim from the
#: table's SOURCE line (a test holds them to it), never typed independently.
_LOCATORS: dict[str, tuple[str, str, str]] = {
    "ISO 4014": ("2011", "Table 1", "nominal thread diameter"),
    "ISO 4017": ("2014", "Table 1", "nominal thread diameter"),
    "ISO 4032": ("2012", "Table 1", "nominal thread diameter"),
    "ISO 7089": ("2000", "Table 1", "nominal size (thread diameter)"),
    "ISO 4762": ("2004", "Table 1", "nominal thread diameter"),
    "ISO 15": ("2017", "Table 4", "bore diameter"),
    "ISO 261": ("2022", "Table 1 (coarse) and Table 2 (fine)", "nominal thread diameter"),
    "DIN 509-E": ("1998-06", "Table 1 (forms E and F)", "shaft diameter"),
    "DIN 509-F": ("1998-06", "Table 1 (forms E and F)", "shaft diameter"),
    "DIN 471": ("2011-04", "Table 1 (shafts)", "nominal shaft diameter"),
    "DIN 472": ("2011-04", "Table 1 (bores)", "nominal bore diameter"),
    "DIN 332-A": ("1986-04", "Table 1 (forms A and B)", "pilot diameter"),
    "DIN 332-B": ("1986-04", "Table 1 (forms A and B)", "pilot diameter"),
    "ISO 3601-2": ("2016", "Tables 1-2 (radial static housings)", "cord diameter"),
}

_EDITION_RE = re.compile(r":\s*(\d{4}(?:-\d{2})?)")
_TABLE_RE = re.compile(r"\b(Tables?\s+[\w.\-]+(?:\s*\([^)]*\))?)", re.IGNORECASE)


@dataclass(frozen=True)
class TableCoverage:
    standard: str
    edition: str
    table: str
    unit: str
    implemented: tuple[str, ...]
    coverage: dict
    source: str
    transcriber: str | None
    reviewer: str | None
    redistribution: str

    def as_dict(self) -> dict:
        """Plain JSON: the field order of the dataclass, lists instead of tuples."""
        return {
            "standard": self.standard,
            "edition": self.edition,
            "table": self.table,
            "unit": self.unit,
            "implemented": list(self.implemented),
            "coverage": dict(self.coverage),
            "source": self.source,
            "transcriber": self.transcriber,
            "reviewer": self.reviewer,
            "redistribution": self.redistribution,
        }


def _publisher(standard: str) -> str:
    head = standard.split()[0].upper() if standard.split() else ""
    return {"ISO": "ISO", "DIN": "DIN"}.get(head, "its publisher")


def _document(standard: str, source: str) -> str:
    """The document name as SOURCE writes it: 'DIN 332-1:1986-04, ...' -> 'DIN 332-1'.

    Only when SOURCE opens with this table's own document: DIN 472 shares its
    SOURCE line with DIN 471, which opens with 'DIN 471', so DIN 472 keeps its
    registered name rather than borrowing its neighbour's.
    """
    head = source.split(":", 1)[0].strip()
    if head and head.split("-")[0] == standard.split("-")[0]:
        return head
    return standard


def _redistribution(standard: str, source: str, rows: int) -> str:
    publisher = _publisher(standard)
    document = _document(standard, source)
    if rows == 0:
        return (
            f"No values from {document} are distributed in this build: only the table's "
            "structure and its SOURCE locator ship, so a transcription from a licensed copy "
            "is a data edit."
        )
    return (
        f"Numeric values only, transcribed from {document} for drawing generation. The "
        f"standard itself is a copyrighted publication of {publisher}; its text, tables "
        "and figures are not reproduced here. Check a value against the edition named in "
        "source before relying on it."
    )


def _locator(standard: str, source: str) -> tuple[str, str, str]:
    known = _LOCATORS.get(standard)
    if known is not None:
        return known
    edition = _EDITION_RE.search(source)
    table = _TABLE_RE.search(source)
    return (
        edition.group(1) if edition else NOT_STATED,
        table.group(1) if table else NOT_STATED,
        NOT_STATED,
    )


def _size_text(key) -> str:
    return key if isinstance(key, str) else _number_text(key)


def table_coverage(standard: str) -> TableCoverage:
    """The record of one registered table (refused by name when it is not registered)."""
    table = _REGISTRY.get(standard)
    if table is None:
        raise ValueError(
            f"standards coverage: {standard!r} is not registered; known: {', '.join(standards())}"
        )
    implemented = tuple(_size_text(key) for key in table.rows)
    edition, locator, measure = _locator(standard, table.source)
    rows = len(implemented)
    coverage = {
        "status": TRANSCRIBED if rows else NO_ROWS,
        "rows": rows,
        "low": float(table.coverage.low) if rows else None,
        "high": float(table.coverage.high) if rows else None,
        "measure": measure,
    }
    return TableCoverage(
        standard=standard,
        edition=edition,
        table=locator,
        unit=table.coverage.unit,
        implemented=implemented,
        coverage=coverage,
        source=table.source,
        transcriber=None,
        reviewer=None,
        redistribution=_redistribution(standard, table.source, rows),
    )


def coverage_manifest() -> dict:
    """``{"schema_version": 1, "tables": [...], "counts": {...}, ...}`` as plain JSON."""
    records = [table_coverage(name).as_dict() for name in standards()]
    empty = sum(1 for row in records if row["coverage"]["status"] == NO_ROWS)
    return {
        "schema_version": SCHEMA_VERSION,
        "tables": records,
        "counts": {
            "tables": len(records),
            "transcribed": len(records) - empty,
            "empty": empty,
            "rows": sum(row["coverage"]["rows"] for row in records),
        },
        "rule": (
            "A size outside a table is refused by name with the nearest transcribed sizes "
            "listed; nothing is substituted and nothing is interpolated."
        ),
        "notes": {
            "transcriber": (
                "transcriber and reviewer are not recorded in the repository for any table, "
                "so both are null; none is invented"
            ),
            "coverage": (
                f"a table with no rows reports status {NO_ROWS!r} and no range; its registered "
                "0-0 placeholder is not a coverage"
            ),
            "scope": (
                "the tables of engineering/mech/standards; ISO 286 fits (engineering/fits.py) "
                "and the ISO 128-50 material map are not size tables and are not listed"
            ),
        },
    }
