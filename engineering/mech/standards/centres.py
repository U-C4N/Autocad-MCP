"""DIN 332-1 centre holes, forms A and B.

SOURCE
------
DIN 332-1:1986-04, *Centre holes 60 degrees - Forms R, A and B*, Table 1.

COVERAGE
--------
Rows come only from the double entry ``engineering/mech/standards/data/
din332_1.{a,b}.json``, generated into ``_rows.py`` by
``scripts/build_standards.py``. **This build ships no row**: the licensed copy
was not at hand, so forms A and B are deferred to 1.8.1 by name and every
lookup is refused. Form R is not drawn by this build at all: its profile is an
arc form whose radius is not transcribed here, and it is refused outright
rather than approximated by the straight 60 degree cone of forms A and B.

What to transcribe (maintainer)
-------------------------------
From **Table 1** of DIN 332-1, edition **1986-04**, one row per pilot diameter
``d1`` and form, into the ``A`` and ``B`` tables of the data file::

    "A": {"rows": {"<d1>": {"d2": <countersink diameter d2>, "t": <depth t>}}}
    "B": {"rows": {"<d1>": {"d2": ..., "d3": <protective countersink d3>, "t": ...}}}

The row key is ``d1``; the generated row repeats it as ``"d1"`` because that is
the key the drawing code reads. ``t`` is the depth the standard tabulates for
the drawn hole, measured from the end face. Fill ``row_source`` (``edition``
must read ``"1986-04"``, ``table``, ``page``) and ``entered_by`` in
``din332_1.a.json``; a second person enters ``din332_1.b.json`` independently;
then run ``uv run --frozen python scripts/build_standards.py``. The build checks
the invariants ``d2 > d1``, ``t > 0`` and, for form B, ``d3 > d2``.
"""

from __future__ import annotations

from engineering.mech.standards import (
    coverage_from_rows,
    lookup,
    register,
    row_count,
)
from engineering.mech.standards._rows import TABLES

_BASE_SOURCE = "DIN 332-1:1986-04, Table 1 (forms A and B)"
_NOT_TRANSCRIBED = " - NOT TRANSCRIBED in this build"

#: The keys a DIN 332 row must carry; form A rows may omit "d3".
CENTRE_ROW_KEYS: tuple[str, ...] = ("d1", "d2", "d3", "t")

FORMS: tuple[str, ...] = ("A", "B")

_NAMES = {"A": "DIN 332-A", "B": "DIN 332-B"}


def _rows(name: str) -> dict[float, dict]:
    return {k: {"d1": float(k), **v} for k, v in TABLES[name]["rows"].items()}


ROWS_A: dict[float, dict] = _rows("DIN 332-A")
ROWS_B: dict[float, dict] = _rows("DIN 332-B")

SOURCE_A = _BASE_SOURCE + ("" if ROWS_A else _NOT_TRANSCRIBED)
SOURCE_B = _BASE_SOURCE + ("" if ROWS_B else _NOT_TRANSCRIBED)
#: The SOURCE both forms share while neither has rows (kept for callers).
SOURCE = _BASE_SOURCE + ("" if (ROWS_A or ROWS_B) else _NOT_TRANSCRIBED)

COVERAGE_A = coverage_from_rows(ROWS_A)
COVERAGE_B = coverage_from_rows(ROWS_B)

#: Provenance per registered table, generated from the double entry.
ROW_SOURCE: dict[str, dict] = {n: dict(TABLES[n]["row_source"]) for n in _NAMES.values()}
TRANSCRIBED_BY: dict[str, str | None] = {n: TABLES[n]["transcribed_by"] for n in _NAMES.values()}
CHECKED_BY: dict[str, str | None] = {n: TABLES[n]["checked_by"] for n in _NAMES.values()}

register("DIN 332-A", ROWS_A, COVERAGE_A, SOURCE_A)
register("DIN 332-B", ROWS_B, COVERAGE_B, SOURCE_B)


def centre_hole_dims(form: str, size: float) -> dict:
    """``{"d1","d2","d3","t"}`` for a pilot diameter, or a named refusal."""
    key = str(form or "").strip().upper()
    if key == "R":
        raise ValueError(
            "DIN 332 form R: this build draws forms A and B only. Form R's arc profile "
            "is not transcribed here and is refused rather than approximated by the "
            "straight 60 degree cone."
        )
    if key not in FORMS:
        raise ValueError(f"DIN 332: expected form A or B, got {form!r}.")
    name = _NAMES[key]
    if not row_count(name):
        raise ValueError(
            f"DIN 332 form {key}: no rows are transcribed in this build (deferred to 1.8.1, "
            f"awaiting the licensed copy), so no centre hole can be looked up for d1 = "
            f"{float(size):g} mm. Pass d1, d2 (and d3 for form B) and t explicitly, or "
            f"transcribe {_BASE_SOURCE} into engineering/mech/standards/data/"
            "din332_1.a.json and .b.json (see centres.py). Nothing is interpolated."
        )
    row = lookup(name, float(size))
    return {k: (float(row[k]) if row.get(k) is not None else None) for k in CENTRE_ROW_KEYS}
