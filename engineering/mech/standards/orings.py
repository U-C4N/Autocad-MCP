"""ISO 3601-2 O-ring housing (groove) dimensions, radial static and axial.

SOURCE
------
ISO 3601-2:2016, *Fluid power systems - O-rings - Part 2: Housing dimensions
for general applications*: Tables 1-2 (radial static housings), and the table
of axial housings, whose number is confirmed against the licensed copy at
transcription and recorded in ``row_source``.

COVERAGE
--------
Two tables, one per housing kind, each keyed by the cord diameter ``d2`` and
each with its own coverage: ``ROWS_RADIAL_STATIC`` (registered as
``ISO 3601-2``, the table ``std_feature_draw``'s ``oring_groove`` reads) and
``ROWS_AXIAL`` (registered as ``ISO 3601-2-axial``). Rows come only from the
double entry ``engineering/mech/standards/data/iso3601_2.{a,b}.json``, generated
into ``_rows.py`` by ``scripts/build_standards.py``. **This build ships no row
of either table**: the licensed copy was not at hand, so both are deferred to
1.8.1 by name and every lookup is refused - the groove width and depth decide
whether the seal works, and they are never written from memory or from a seal
maker's catalogue.

What to transcribe (maintainer)
-------------------------------
From ISO 3601-2, edition **2016**, one row per cord diameter ``d2`` into the
``radial_static`` table (the radial static housing table) and the ``axial``
table (the axial housing table)::

    "rows": {"<cord d2>": {"b": <groove width b>, "h": <groove depth h>}}

``b`` and ``h`` are the nominal housing width and depth as tabulated for that
cord. Where the standard tabulates a dimension per application (piston / rod,
internal / external pressure), transcribe the column ``std_feature_draw``
draws - a groove whose floor is ``h`` below the sealed surface - and name the
column in ``row_source.table``. Fill ``row_source`` (``edition`` must read
``"2016"``, ``table``, ``page``) and ``entered_by`` in ``iso3601_2.a.json``; a
second person enters ``iso3601_2.b.json`` independently; then run
``uv run --frozen python scripts/build_standards.py``. The build checks the
invariants ``h < d2`` (the ring is squeezed) and ``b > d2`` (it has room).
"""

from __future__ import annotations

from engineering.mech.standards import (
    coverage_from_rows,
    lookup,
    register,
    row_count,
)
from engineering.mech.standards._rows import TABLES

_NOT_TRANSCRIBED = " - NOT TRANSCRIBED in this build"
_SOURCE_RADIAL_STATIC = "ISO 3601-2:2016, Tables 1-2 (radial static housings)"
_SOURCE_AXIAL = (
    "ISO 3601-2:2016, Table of axial housings (number confirmed against the licensed copy)"
)

#: The keys every ISO 3601-2 row must carry.
ORING_ROW_KEYS: tuple[str, ...] = ("b", "h")

#: Housing kinds, and the registered table each one reads.
KINDS: tuple[str, ...] = ("radial", "axial")
TABLE_BY_KIND: dict[str, str] = {"radial": "ISO 3601-2", "axial": "ISO 3601-2-axial"}

ROWS_RADIAL_STATIC: dict[float, dict] = {
    k: dict(v) for k, v in TABLES["ISO 3601-2"]["rows"].items()
}
ROWS_AXIAL: dict[float, dict] = {k: dict(v) for k, v in TABLES["ISO 3601-2-axial"]["rows"].items()}
#: The radial static table under its 1.7 name.
ROWS = ROWS_RADIAL_STATIC

SOURCE_RADIAL_STATIC = _SOURCE_RADIAL_STATIC + ("" if ROWS_RADIAL_STATIC else _NOT_TRANSCRIBED)
SOURCE_AXIAL = _SOURCE_AXIAL + ("" if ROWS_AXIAL else _NOT_TRANSCRIBED)
SOURCE = SOURCE_RADIAL_STATIC

COVERAGE_RADIAL_STATIC = coverage_from_rows(ROWS_RADIAL_STATIC)
COVERAGE_AXIAL = coverage_from_rows(ROWS_AXIAL)
COVERAGE = COVERAGE_RADIAL_STATIC
#: Coverage per housing kind.
COVERAGE_BY_KIND = {"radial": COVERAGE_RADIAL_STATIC, "axial": COVERAGE_AXIAL}

#: Provenance per registered table, generated from the double entry.
ROW_SOURCE: dict[str, dict] = {n: dict(TABLES[n]["row_source"]) for n in TABLE_BY_KIND.values()}
TRANSCRIBED_BY: dict[str, str | None] = {
    n: TABLES[n]["transcribed_by"] for n in TABLE_BY_KIND.values()
}
CHECKED_BY: dict[str, str | None] = {n: TABLES[n]["checked_by"] for n in TABLE_BY_KIND.values()}

register("ISO 3601-2", ROWS_RADIAL_STATIC, COVERAGE_RADIAL_STATIC, SOURCE_RADIAL_STATIC)
register("ISO 3601-2-axial", ROWS_AXIAL, COVERAGE_AXIAL, SOURCE_AXIAL)

_BASE = {"radial": _SOURCE_RADIAL_STATIC, "axial": _SOURCE_AXIAL}


def oring_groove_dims(cord: float, kind: str = "radial") -> dict:
    """``{"b": width, "h": depth}`` for a cord diameter and housing kind, or a named refusal.

    ``kind`` picks the table (``radial``: radial static, ``axial``); a cord
    between two rows or outside the kind's coverage is refused with the nearest
    transcribed cords named. Nothing is interpolated.
    """
    key = str(kind or "").strip().lower()
    if key not in KINDS:
        raise ValueError(f"O-ring housing: expected kind 'radial' or 'axial', got {kind!r}.")
    name = TABLE_BY_KIND[key]
    if not row_count(name):
        raise ValueError(
            f"{name}: no rows are transcribed in this build (deferred to 1.8.1, awaiting the "
            f"licensed copy), so no {key} housing can be looked up for a {float(cord):g} mm "
            f"cord. Pass b and h explicitly, or transcribe {_BASE[key]} into "
            "engineering/mech/standards/data/iso3601_2.a.json and .b.json (see orings.py). "
            "Nothing is interpolated."
        )
    row = lookup(name, float(cord))
    return {field: float(row[field]) for field in ORING_ROW_KEYS}
