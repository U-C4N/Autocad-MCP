"""Parts-list rows to a file: CSV always, XLSX when openpyxl is installed.

CSV needs nothing but the standard library, so the deliverable is never blocked
-- which is exactly why `xlsx_write` can be a capability rather than a hard
dependency. The refusal names both the missing package and the format that
always works.
"""

from __future__ import annotations

import csv
from importlib.util import find_spec
from pathlib import Path

from backends.capability import UnsupportedCapabilityError
from engineering.sheet.bom import BOM_COLUMNS, COLUMN_LABELS, DEFAULT_COLUMNS

__all__ = ["EXTRACT_FORMATS", "write_rows", "xlsx_available"]

EXTRACT_FORMATS = ("csv", "xlsx")


def xlsx_available() -> bool:
    """Is openpyxl importable right now? Re-evaluated per call, so installing
    the package changes the answer without a restart."""
    return find_spec("openpyxl") is not None


def _prepare(rows, columns):
    chosen = tuple(DEFAULT_COLUMNS if columns is None else columns)
    unknown = [c for c in chosen if c not in BOM_COLUMNS]
    if unknown:
        raise ValueError(
            f"unknown parts-list column(s) {', '.join(unknown)}; "
            f"available: {', '.join(BOM_COLUMNS)}"
        )
    body = [[str(row.get(column, "")) for column in chosen] for row in rows]
    if not body:
        raise ValueError("data_extract: there are no parts-list rows to write")
    return chosen, [COLUMN_LABELS[c] for c in chosen], body


def write_rows(rows, path: str, *, columns=None, fmt: str = "csv") -> dict:
    """Write the rows to `path`. Refuses before the file is created."""
    if fmt not in EXTRACT_FORMATS:
        raise ValueError(f"format must be one of {', '.join(EXTRACT_FORMATS)}; got {fmt!r}")
    chosen, heading, body = _prepare(rows, columns)
    if fmt == "xlsx" and not xlsx_available():
        raise UnsupportedCapabilityError(
            "xlsx_write",
            "data_extract(format='xlsx') needs openpyxl, which is not installed. "
            'Install it with: pip install openpyxl (or pip install -e ".[full]"), '
            "or ask for format='csv', which always works and carries the same rows.",
        )

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        with open(target, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(heading)
            writer.writerows(body)
    else:
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "Parts list"
        sheet.append(heading)
        for row in body:
            sheet.append(row)
        book.save(target)

    return {
        "ok": True,
        "path": str(target),
        "format": fmt,
        "columns": list(chosen),
        "rows": len(body),
        "bytes": target.stat().st_size,
    }
