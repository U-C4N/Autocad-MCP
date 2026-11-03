"""DIN 509 relief grooves (Freistiche), forms E and F.

SOURCE
------
DIN 509:1998-06, *Technische Zeichnungen - Freistiche*, Table 1 (forms E and F).
The edition is the one 1.7 named; the transcriber verifies it against the
purchased copy, and a copy of another edition is refused by the build until
this SOURCE line, the coverage locator and ``TableSpec.edition`` name it.

COVERAGE
--------
Rows come only from the double entry ``engineering/mech/standards/data/
din509.{a,b}.json``, generated into ``_rows.py`` by
``scripts/build_standards.py``. **This build ships no row**: the licensed copy
was not at hand, so forms E and F are deferred to 1.8.1 by name and every
lookup is refused - a wrong relief depth is a wrong workshop drawing, while a
narrow table is merely narrow.

The table carries dimensions, not a profile: a row is ``r, t1, f, g`` (and
``t2`` for form F), and the relief profile the drawing code reads (``profile``,
``(along, depth)`` pairs) is built from them by :func:`profile_from_dims` - a
documented construction, the same for every row, so nothing downstream ever
reconstructs a profile it was not handed.

What to transcribe (maintainer)
-------------------------------
From **Table 1** of DIN 509, edition **1998-06**, one row per shaft diameter
``d`` the generator must support, into the ``E`` and ``F`` tables::

    "E": {"rows": {"<d>": {"r": .., "t1": .., "f": .., "g": .., "designation": "<as printed>"}}}
    "F": {"rows": {"<d>": {"r": .., "t1": .., "f": .., "g": .., "t2": ..}}}

A row is the designation the table assigns to that diameter, with its nominal
``r`` (radius), ``t1`` (depth into the cylinder), ``f`` (width), ``g`` and, for
form F, ``t2`` (depth into the shoulder face); ``designation`` is optional and
recorded for traceability. **Before the first row ships, confirm
:func:`profile_from_dims` against the standard's figure** (how ``f`` and ``g``
are measured): if the figure reads otherwise, change the function and its tests
in the same commit as the rows. Fill ``row_source`` (``edition`` must read
``"1998-06"``, ``table``, ``page``) and ``entered_by`` in ``din509.a.json``; a
second person enters ``din509.b.json`` independently; then run
``uv run --frozen python scripts/build_standards.py``. The build checks the
invariants depth > 0 and a profile that runs monotonically.
"""

from __future__ import annotations

import math

from engineering.mech.standards import (
    coverage_from_rows,
    lookup,
    register,
    row_count,
)
from engineering.mech.standards._rows import TABLES

_BASE_SOURCE = "DIN 509:1998-06, Table 1 (forms E and F)"
_NOT_TRANSCRIBED = " - NOT TRANSCRIBED in this build"

#: The keys every DIN 509 row must carry for the drawing code to use it.
UNDERCUT_ROW_KEYS: tuple[str, ...] = ("profile",)
#: The tabulated dimensions a row is transcribed as (t2: form F only).
UNDERCUT_DIM_KEYS: tuple[str, ...] = ("r", "t1", "f", "g", "t2")

FORMS: tuple[str, ...] = ("E", "F")

#: Straight segments the radius ``r`` is drawn with.
ARC_SEGMENTS = 8

_EPS = 1e-9


def _positive(value, name: str, form: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"DIN 509 form {form}: {name} must be a number, got {value!r}.")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"DIN 509 form {form}: {name} must be finite and > 0, got {value!r}.")
    return number


def profile_from_dims(
    form: str,
    *,
    r: float,
    t1: float,
    f: float,
    g: float,
    t2: float | None = None,
    segments: int = ARC_SEGMENTS,
) -> tuple[tuple[float, float], ...]:
    """The relief profile of one DIN 509 row, as ``(along, depth)`` pairs in mm.

    Frame: ``along`` runs from the shoulder face (0) into the smaller cylinder,
    ``depth`` is measured below that cylinder's surface - the frame
    ``Undercut(profile=...)`` and ``std_feature_draw`` read. The construction:

    * the relief leaves the cylinder line at the shoulder: at ``along = 0`` for
      form E (the cylinder alone is relieved), at ``along = -t2`` for form F
      (the relief also runs ``t2`` into the shoulder face);
    * from there an arc of radius ``r`` runs down to the floor at depth ``t1``,
      tangent to the floor - its axial run is ``sqrt(2 r t1 - t1^2)``, so the
      construction needs ``t1 <= r`` (a shallow relief) and refuses otherwise;
    * the floor runs at depth ``t1`` to ``along = f - g``;
    * a straight run-out of axial length ``g`` climbs back to the cylinder line,
      which it reaches at ``along = f`` (``f`` is the relief's width at the
      cylinder line, measured from the shoulder face).

    The arc is drawn with ``segments`` chords of equal angle. The result runs
    monotonically in ``along``, never leaves the cylinder (``depth >= 0``) and
    is exactly ``t1`` deep. A row the construction cannot draw - ``t1 > r``,
    an arc that ends past ``f - g``, a form E row with a ``t2``, a form F row
    without one - is refused by name rather than bent to fit.
    """
    key = str(form or "").strip().upper()
    if key not in FORMS:
        raise ValueError(f"DIN 509 defines forms E and F; got {form!r}.")
    r = _positive(r, "r", key)
    t1 = _positive(t1, "t1", key)
    f = _positive(f, "f", key)
    g = _positive(g, "g", key)
    if isinstance(segments, bool) or not isinstance(segments, int) or segments < 1:
        raise ValueError(f"DIN 509: segments must be a positive integer, got {segments!r}.")
    if key == "E":
        if t2 not in (None, 0, 0.0):
            raise ValueError(
                f"DIN 509 form E: this construction relieves the cylinder only, but the row "
                f"carries t2 = {t2!r}. Check the row against the standard's figure (form F "
                "relieves the shoulder face as well)."
            )
        start = 0.0
    else:
        if t2 is None:
            raise ValueError("DIN 509 form F: t2 (the depth into the shoulder face) is required.")
        start = -_positive(t2, "t2", key)
    if t1 > r + _EPS:
        raise ValueError(
            f"DIN 509 form {key}: t1 = {t1:g} is deeper than r = {r:g}; the construction "
            "draws the floor radius tangent to the floor and through the shoulder line, which "
            "needs t1 <= r. Check the row against the standard's figure."
        )
    run = math.sqrt(max(0.0, 2.0 * r * t1 - t1 * t1))
    floor_start = start + run
    floor_end = f - g
    if floor_end < floor_start - _EPS:
        raise ValueError(
            f"DIN 509 form {key}: the radius reaches the floor at along = {floor_start:g} mm, "
            f"past f - g = {floor_end:g} mm where the run-out starts; the row's r, t1, f and g "
            "do not make a relief with this construction. Check the row against the figure."
        )
    centre_depth = t1 - r
    sweep = math.asin(min(1.0, run / r))
    points: list[tuple[float, float]] = [(start, 0.0)]
    for index in range(1, segments):
        angle = sweep * (1.0 - index / segments)
        points.append((floor_start - r * math.sin(angle), centre_depth + r * math.cos(angle)))
    points.append((floor_start, t1))
    if floor_end > floor_start + _EPS:
        points.append((floor_end, t1))
    points.append((f, 0.0))
    return tuple((float(a), float(max(0.0, d))) for a, d in points)


def _rows(name: str, form: str) -> dict[float, dict]:
    out: dict[float, dict] = {}
    for d, row in TABLES[name]["rows"].items():
        profile = profile_from_dims(
            form, r=row["r"], t1=row["t1"], f=row["f"], g=row["g"], t2=row.get("t2")
        )
        out[d] = {**row, "profile": [[a, depth] for a, depth in profile]}
    return out


ROWS_E: dict[float, dict] = _rows("DIN 509-E", "E")
ROWS_F: dict[float, dict] = _rows("DIN 509-F", "F")

SOURCE_E = _BASE_SOURCE + ("" if ROWS_E else _NOT_TRANSCRIBED)
SOURCE_F = _BASE_SOURCE + ("" if ROWS_F else _NOT_TRANSCRIBED)
#: The SOURCE both forms share while neither has rows (kept for callers).
SOURCE = _BASE_SOURCE + ("" if (ROWS_E or ROWS_F) else _NOT_TRANSCRIBED)

COVERAGE_E = coverage_from_rows(ROWS_E)
COVERAGE_F = coverage_from_rows(ROWS_F)

_NAMES = {"E": "DIN 509-E", "F": "DIN 509-F"}

#: Provenance per registered table, generated from the double entry.
ROW_SOURCE: dict[str, dict] = {n: dict(TABLES[n]["row_source"]) for n in _NAMES.values()}
TRANSCRIBED_BY: dict[str, str | None] = {n: TABLES[n]["transcribed_by"] for n in _NAMES.values()}
CHECKED_BY: dict[str, str | None] = {n: TABLES[n]["checked_by"] for n in _NAMES.values()}

register("DIN 509-E", ROWS_E, COVERAGE_E, SOURCE_E)
register("DIN 509-F", ROWS_F, COVERAGE_F, SOURCE_F)


def undercut_profile(form: str, d: float) -> tuple[tuple[float, float], ...]:
    """The relief profile for a shaft diameter, or a named refusal (never interpolated)."""
    key = str(form or "").strip().upper()
    if key not in FORMS:
        raise ValueError(f"DIN 509 defines forms E and F; got {form!r}.")
    name = _NAMES[key]
    if not row_count(name):
        raise ValueError(
            f"DIN 509 form {key}: no rows are transcribed in this build (deferred to 1.8.1, "
            f"awaiting the licensed copy), so no relief profile can be looked up for d = "
            f"{float(d):g} mm. Pass the profile explicitly "
            "(Undercut(profile=[[along, depth], ...])), or transcribe "
            f"{_BASE_SOURCE} into engineering/mech/standards/data/din509.a.json and .b.json "
            "(see undercuts.py). Nothing is interpolated and no profile is invented."
        )
    row = lookup(name, float(d))
    return tuple((float(a), float(b)) for a, b in row["profile"])
