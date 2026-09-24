"""The `office` extra: openpyxl for the XLSX workbooks, and in `full` too.

Declared and locked is what is tested - not installed: the CI lane that
installs `[pdf]` has no openpyxl and must stay green, because every XLSX
writer refuses with the `xlsx_write` capability and writes CSV instead.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _extras() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["optional-dependencies"]


def test_the_office_extra_carries_openpyxl_and_full_carries_it_too():
    extras = _extras()
    assert extras["office"] == ["openpyxl>=3.1"]
    assert "openpyxl>=3.1" in extras["full"]


def test_the_lock_records_openpyxl_for_the_office_extra():
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert '\nname = "openpyxl"\n' in lock
    assert '{ name = "openpyxl" }' in lock
