"""DIN 332-1 centre holes, forms A and B.

SOURCE
------
DIN 332-1:1986-04, *Centre holes 60 degrees - Forms R, A and B*, Table 1.

COVERAGE
--------
**No rows are transcribed in this build** (same rule as ``undercuts.py``).
Form R is not drawn by this build at all: its profile is an arc form whose
radius is not transcribed here, and it is refused outright rather than
approximated by the straight 60 degree cone of forms A and B.

Extending is a data edit: fill ``ROWS_A`` / ``ROWS_B`` with
``{pilot_diameter_mm: {"d1": ..., "d2": ..., "d3": ..., "t": ...}}`` (``d3``
only for form B's protective 120 degree countersink) and widen the coverage.
"""

from __future__ import annotations

from engineering.mech.standards import Coverage, register

SOURCE = "DIN 332-1:1986-04, Table 1 (forms A and B) - NOT TRANSCRIBED in this build"

#: The keys a DIN 332 row must carry; form A rows may omit "d3".
CENTRE_ROW_KEYS: tuple[str, ...] = ("d1", "d2", "d3", "t")

FORMS: tuple[str, ...] = ("A", "B")

ROWS_A: dict[float, dict] = {}
ROWS_B: dict[float, dict] = {}

COVERAGE_A = Coverage(0.0, 0.0, "mm")
COVERAGE_B = Coverage(0.0, 0.0, "mm")

register("DIN 332-A", ROWS_A, COVERAGE_A, SOURCE)
register("DIN 332-B", ROWS_B, COVERAGE_B, SOURCE)

_ROWS = {"A": ROWS_A, "B": ROWS_B}


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
    rows = _ROWS[key]
    if not rows:
        raise ValueError(
            f"DIN 332 form {key}: no rows are transcribed in this build, so no centre hole "
            f"can be looked up for d1 = {float(size):g} mm. Pass d1, d2 (and d3 for form B) "
            f"and t explicitly, or transcribe {SOURCE.split(' - ')[0]} into "
            "engineering/mech/standards/centres.py. Nothing is interpolated."
        )
    pilot = float(size)
    if pilot in rows:
        row = rows[pilot]
        return {name: (float(row[name]) if name in row else None) for name in CENTRE_ROW_KEYS}
    raise ValueError(
        f"DIN 332 form {key}: d1 = {pilot:g} mm has no transcribed row (sizes: {sorted(rows)})."
    )
