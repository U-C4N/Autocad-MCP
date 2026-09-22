"""Parts-list rows to CSV (always) and XLSX (with openpyxl)."""

from __future__ import annotations

import csv
from importlib.util import find_spec

import pytest

from backends.base import UnsupportedCapabilityError
from engineering.sheet.extract import EXTRACT_FORMATS, write_rows, xlsx_available


def _rows():
    return (
        {
            "item": 1,
            "qty": 2,
            "designation": "ISO 4014 - M12x60",
            "standard": "ISO 4014",
            "material": "8.8",
            "handles": ("A1", "A2"),
        },
        {
            "item": 2,
            "qty": 1,
            "designation": "SHAFT 40h6",
            "standard": "",
            "material": "C45",
            "handles": ("B1",),
        },
    )


def test_the_formats_are_closed_and_csv_is_always_one_of_them():
    assert EXTRACT_FORMATS == ("csv", "xlsx")


def test_csv_writes_the_heading_row_and_one_row_per_part(tmp_path):
    target = tmp_path / "bom.csv"
    result = write_rows(_rows(), str(target), fmt="csv")
    assert result["ok"] is True and result["rows"] == 2 and result["format"] == "csv"
    with open(target, newline="", encoding="utf-8") as handle:
        written = list(csv.reader(handle))
    assert written[0] == ["POS", "QTY", "DESIGNATION", "STANDARD", "MATERIAL"]
    assert written[1] == ["1", "2", "ISO 4014 - M12x60", "ISO 4014", "8.8"]
    assert written[2][2] == "SHAFT 40h6"
    assert len(written) == 3


def test_a_chosen_column_set_is_honoured(tmp_path):
    target = tmp_path / "short.csv"
    write_rows(_rows(), str(target), columns=("item", "designation"), fmt="csv")
    with open(target, newline="", encoding="utf-8") as handle:
        written = list(csv.reader(handle))
    assert written[0] == ["POS", "DESIGNATION"]
    assert all(len(row) == 2 for row in written)


def test_an_unknown_format_is_refused_with_the_list(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        write_rows(_rows(), str(tmp_path / "x.ods"), fmt="ods")
    message = str(excinfo.value)
    assert "ods" in message and "csv" in message and "xlsx" in message


def test_no_rows_is_refused_rather_than_writing_an_empty_file(tmp_path):
    target = tmp_path / "empty.csv"
    with pytest.raises(ValueError):
        write_rows((), str(target), fmt="csv")
    assert not target.exists()


def test_xlsx_available_agrees_with_the_installed_package():
    assert xlsx_available() is (find_spec("openpyxl") is not None)


def test_xlsx_refuses_with_the_capability_key_and_names_the_extra(tmp_path):
    if xlsx_available():
        result = write_rows(_rows(), str(tmp_path / "bom.xlsx"), fmt="xlsx")
        assert result["ok"] is True and (tmp_path / "bom.xlsx").exists()
        return
    target = tmp_path / "bom.xlsx"
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        write_rows(_rows(), str(target), fmt="xlsx")
    assert excinfo.value.capability == "xlsx_write"
    assert "openpyxl" in str(excinfo.value)
    assert "csv" in str(excinfo.value), "a refusal that does not name the way out is half a refusal"
    assert not target.exists()
