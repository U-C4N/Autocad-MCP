"""Shared validation and layout math for tables and multileaders."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableLayout:
    cells: list[list[str]]
    column_widths: list[float]
    row_height: float
    text_height: float
    #: The first row is a title, merged across every column as AutoCAD's
    #: TABLE draws it.
    title_row: bool = False

    @property
    def row_count(self) -> int:
        return len(self.cells)

    @property
    def column_count(self) -> int:
        return len(self.column_widths)

    @property
    def width(self) -> float:
        return sum(self.column_widths)

    @property
    def height(self) -> float:
        return self.row_count * self.row_height


def prepare_table_layout(
    rows: list[list[str]],
    headers: list[str] | None = None,
    column_widths: list[float] | None = None,
    row_height: float = 7.0,
    text_height: float = 2.5,
    title: str | None = None,
) -> TableLayout:
    """Validate table input and calculate deterministic cross-backend sizes."""
    if not rows:
        raise RuntimeError("entity_create_table: at least one data row is required")
    if len(rows) > 200:
        raise RuntimeError("entity_create_table: maximum is 200 rows")
    if row_height <= 0 or text_height <= 0:
        raise RuntimeError("entity_create_table: row_height and text_height must be positive")

    column_count = len(headers) if headers is not None else len(rows[0])
    if column_count < 1 or column_count > 30:
        raise RuntimeError("entity_create_table: table must contain between 1 and 30 columns")
    if any(len(row) != column_count for row in rows):
        raise RuntimeError("entity_create_table: all rows must have the same number of columns")
    if headers is not None and len(headers) != column_count:
        raise RuntimeError("entity_create_table: headers must match the data column count")

    data = [[str(cell) for cell in row] for row in rows]
    if headers is not None:
        data.insert(0, [str(cell) for cell in headers])
    if title:
        data.insert(0, [str(title)] + [""] * (column_count - 1))

    if column_widths is None:
        widths = []
        for column in range(column_count):
            longest = max(len(row[column]) for row in data)
            widths.append(max(10.0, longest * float(text_height) * 0.7))
    else:
        if len(column_widths) != column_count:
            raise RuntimeError("entity_create_table: column_widths must match column count")
        widths = [float(width) for width in column_widths]
        if any(width <= 0 for width in widths):
            raise RuntimeError("entity_create_table: column widths must be positive")

    return TableLayout(data, widths, float(row_height), float(text_height), title_row=bool(title))


def validate_mleader(points: list[list[float]], text: str) -> list[tuple[float, float]]:
    if len(points) < 2:
        raise RuntimeError("leader_create_mleader: at least two points are required")
    if not str(text).strip():
        raise RuntimeError("leader_create_mleader: text must not be empty")
    normalized = []
    for point in points:
        if len(point) < 2:
            raise RuntimeError("leader_create_mleader: each point must contain x and y")
        normalized.append((float(point[0]), float(point[1])))
    return normalized
