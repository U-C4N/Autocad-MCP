"""Safe spreadsheet cells: exported text never becomes a formula.

A drawing is foreign input. A tag, a designation or a room label that reads
``=1+1`` (or ``@SUM(...)``, ``+cmd|...``) was written verbatim into the CSV and,
through openpyxl, stored as a *formula* cell in the XLSX — a spreadsheet opening
the deliverable would evaluate it. Every CSV and XLSX the server writes goes
through this module instead, and each exporter declares what its columns hold:

- ``"text"``: in CSV a value that begins with one of :data:`FORMULA_PREFIXES`
  gets a leading ``'`` (the OWASP rule; a spreadsheet shows the text and never
  runs it). In XLSX the cell is written as a string cell (``data_type "s"``),
  which no spreadsheet evaluates, so no prefix is needed and the text stays as
  it was — except that a carriage return is written as a line feed: XML 1.0
  §2.11 turns ``\\r`` into ``\\n`` when the sheet is parsed anyway, and a
  ``\\r\\n`` came back as two line breaks (measured, openpyxl 3.1.5).
- ``"number"``: a finite ``int``/``float`` is written as a number: a numeric
  XLSX cell, and in CSV the digits with no prefix, so a genuine ``-12.5`` stays
  ``-12.5``. Anything else in a number column — a ``str`` (even ``"-12.5"``: a
  column that mixes sentences and numbers, like the takeoff's method sheet,
  must not turn a sentence's text into a number), a dash, ``nan``, a bool — is
  written under the text rule. An exporter whose numbers arrive as text
  converts them first with :func:`parse_decimal`.

``None`` is an empty cell in both formats.
"""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

__all__ = [
    "FORMULA_PREFIXES",
    "KINDS",
    "ColumnKind",
    "append_xlsx_row",
    "csv_row",
    "csv_text",
    "parse_decimal",
    "write_csv",
]

FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
ColumnKind = Literal["text", "number"]
KINDS: tuple[str, ...] = ("text", "number")

#: A plain decimal: an optional minus, digits with an optional fraction, an
#: optional exponent. ASCII digits only (``\d`` also matches other scripts'
#: digits, which ``float`` would accept); no leading ``+``, which is a formula
#: lead in a spreadsheet, not a sign.
_DECIMAL = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_INTEGER = re.compile(r"-?[0-9]+")


def _check(values: Sequence, kinds: Sequence[str]) -> None:
    if len(values) != len(kinds):
        raise ValueError(f"{len(values)} values for {len(kinds)} column kinds")
    for index, kind in enumerate(kinds):
        if kind not in KINDS:
            raise ValueError(f"kinds[{index}]: {kind!r} unknown; choose from {', '.join(KINDS)}")


def _number(value) -> int | float | None:
    """`value` when it is a finite ``int`` / ``float`` (never a bool), else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def parse_decimal(value):
    """A plain-decimal ``str`` (``"-12.5"``, ``"3"``) as its finite number; anything
    else unchanged. For an exporter whose source hands numbers over as text."""
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        number = int(value) if _INTEGER.fullmatch(value) else float(value)
        if math.isfinite(number):
            return number
    return value


def csv_text(value) -> str:
    """A text cell for CSV: ``""`` for ``None``; ``'`` + text when it starts with a prefix."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def _csv_cell(value, kind: str) -> str:
    if value is None:
        return ""
    if kind == "number" and _number(value) is not None:
        return str(value)
    return csv_text(value)


def csv_row(values: Sequence, kinds: Sequence[ColumnKind]) -> list:
    """One CSV row, each value serialized by its column's kind."""
    _check(values, kinds)
    return [_csv_cell(value, kind) for value, kind in zip(values, kinds, strict=True)]


def write_csv(
    path,
    header: Sequence[str],
    rows: Iterable[Sequence],
    kinds: Sequence[ColumnKind],
    *,
    bom: bool = False,
) -> None:
    """Write `header` and `rows` to `path` (UTF-8, with a byte-order mark when `bom`).

    The header is text. Refused before the file is opened: a header or a kind
    list that does not match, an unknown kind, and a `path` that is a directory
    (named, instead of the ``PermissionError`` Windows raises for one).
    """
    target = Path(path)
    if len(header) != len(kinds):
        raise ValueError(f"{len(header)} header cells for {len(kinds)} column kinds")
    _check(header, kinds)
    if target.is_dir():
        raise ValueError(f"{target}: is a directory; name the file to write inside it")
    with open(target, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(csv_text(cell) for cell in header)
        for row in rows:
            writer.writerow(csv_row(row, kinds))


def _xlsx_text(value) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def append_xlsx_row(sheet, values: Sequence, kinds: Sequence[ColumnKind]) -> None:
    """Append one row to an openpyxl worksheet: text as string cells, numbers as numbers.

    openpyxl stores any string that starts with ``=`` as a formula; setting
    ``data_type = "s"`` after the value is what keeps it a string, and it
    survives ``save`` / ``load_workbook``.
    """
    from openpyxl.cell import Cell

    _check(values, kinds)
    cells = []
    for value, kind in zip(values, kinds, strict=True):
        number = _number(value) if kind == "number" and value is not None else None
        if value is None:
            cells.append(None)
        elif number is not None:
            cells.append(number)
        else:
            cell = Cell(sheet, value=_xlsx_text(value))
            cell.data_type = "s"
            cells.append(cell)
    sheet.append(cells)
