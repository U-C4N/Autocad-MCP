"""DIN 471 / DIN 472 retaining-ring groove dimensions.

SOURCE
------
DIN 471:2011-04, *Retaining rings for shafts*, Table 1 (groove diameter d2 and
groove width m) and DIN 472:2011-04, *Retaining rings for bores*, Table 1.
The table and page are confirmed against the licensed copy at transcription
and recorded in ``row_source``.

COVERAGE
--------
Rows come only from the double entry ``engineering/mech/standards/data/
din471.{a,b}.json`` and ``din472.{a,b}.json``, generated into ``_rows.py`` by
``scripts/build_standards.py``. **This build ships no row of either table**: the
licensed copies were not at hand, so both are deferred to 1.8.1 by name and
every lookup is refused - the groove diameter is the number a wrong drawing gets
wrong, and it is never written from memory or from a ring maker's catalogue.

What to transcribe (maintainer)
-------------------------------
From **Table 1** of each standard, edition **2011-04**, one row per
nominal size ``d1`` (shaft diameter for DIN 471, bore diameter for DIN 472)::

    "rows": {"<d1>": {"m": <groove width m>, "d2": <groove diameter d2>,
                      "n": <minimum edge distance n, optional>}}

``m`` is the groove width as tabulated (the nominal value, not its tolerance),
``d2`` the groove diameter (nominal), ``n`` optional. Fill ``row_source``
(``edition`` must read ``"2011-04"``, ``table`` e.g. ``"Table 1"``, ``page`` the
printed page) and ``entered_by`` in ``din471.a.json``; a second person enters
the same rows independently into ``din471.b.json``; then run
``uv run --frozen python scripts/build_standards.py``. The build refuses any
difference between the entries by path, and checks the invariants DIN 471
``d2 < d1`` with ``m`` not decreasing as ``d1`` grows, DIN 472 ``d2 > d1``.
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
_SOURCE_471 = "DIN 471:2011-04, Table 1 (shafts)"
_SOURCE_472 = "DIN 472:2011-04, Table 1 (bores)"

#: The keys every DIN 471/472 row must carry (the contract the drawing code reads).
RING_ROW_KEYS: tuple[str, ...] = ("m", "d2")
#: Optional columns a transcribed row may carry.
RING_OPTIONAL_KEYS: tuple[str, ...] = ("n",)

ROWS_471: dict[float, dict] = {k: dict(v) for k, v in TABLES["DIN 471"]["rows"].items()}
ROWS_472: dict[float, dict] = {k: dict(v) for k, v in TABLES["DIN 472"]["rows"].items()}

SOURCE_471 = _SOURCE_471 + ("" if ROWS_471 else _NOT_TRANSCRIBED)
SOURCE_472 = _SOURCE_472 + ("" if ROWS_472 else _NOT_TRANSCRIBED)
#: Both tables' SOURCE in one line (kept for callers that quote the pair).
SOURCE = f"{SOURCE_471}; {SOURCE_472}"

COVERAGE_471 = coverage_from_rows(ROWS_471)
COVERAGE_472 = coverage_from_rows(ROWS_472)

#: Provenance per registered table, generated from the double entry.
ROW_SOURCE: dict[str, dict] = {n: dict(TABLES[n]["row_source"]) for n in ("DIN 471", "DIN 472")}
TRANSCRIBED_BY: dict[str, str | None] = {
    n: TABLES[n]["transcribed_by"] for n in ("DIN 471", "DIN 472")
}
CHECKED_BY: dict[str, str | None] = {n: TABLES[n]["checked_by"] for n in ("DIN 471", "DIN 472")}

register("DIN 471", ROWS_471, COVERAGE_471, SOURCE_471)
register("DIN 472", ROWS_472, COVERAGE_472, SOURCE_472)

_DATA = {"DIN 471": ("din471", _SOURCE_471), "DIN 472": ("din472", _SOURCE_472)}


def ring_groove_dims(standard: str, d: float) -> dict:
    """``{"m": ..., "d2": ...}`` for a nominal size, or a named refusal.

    A size inside the table that is not a listed row, and a size outside it,
    are refused by the registry with the nearest transcribed sizes named;
    nothing is interpolated.
    """
    key = " ".join(str(standard or "").split()).upper()
    if key not in _DATA:
        raise ValueError(f"retaining-ring groove: expected DIN 471 or DIN 472, got {standard!r}.")
    stem, source = _DATA[key]
    if not row_count(key):
        raise ValueError(
            f"{key}: no rows are transcribed in this build (deferred to 1.8.1, awaiting the "
            f"licensed copy), so no groove can be looked up for d = {float(d):g} mm. Pass m "
            f"and d2 explicitly, or transcribe {source} into "
            f"engineering/mech/standards/data/{stem}.a.json and .b.json (see rings.py). "
            "Nothing is interpolated."
        )
    row = lookup(key, float(d))
    return {name: float(row[name]) for name in RING_ROW_KEYS}
