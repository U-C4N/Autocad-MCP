"""DIN 471 / DIN 472 retaining-ring groove dimensions.

SOURCE
------
DIN 471:2011-04, *Retaining rings for shafts*, Table 1 (groove diameter d2 and
groove width m) and DIN 472:2011-04, *Retaining rings for bores*, Table 1.

COVERAGE
--------
**No rows are transcribed in this build**, for the reason given in
``undercuts.py``: the groove diameter is the number a wrong drawing gets wrong,
and it is not written here from memory. Every lookup is refused by name.

Extending is a data edit: fill ``ROWS_471`` / ``ROWS_472`` with
``{nominal_shaft_or_bore_mm: {"m": groove_width_mm, "d2": groove_diameter_mm}}``
and widen the coverage. Those two keys are the contract group S's
``std_feature_draw`` already reads.
"""

from __future__ import annotations

from engineering.mech.standards import Coverage, register

SOURCE = (
    "DIN 471:2011-04 Table 1 (shafts) and DIN 472:2011-04 Table 1 (bores) "
    "- NOT TRANSCRIBED in this build"
)

#: The keys every DIN 471/472 row must carry.
RING_ROW_KEYS: tuple[str, ...] = ("m", "d2")

ROWS_471: dict[float, dict] = {}
ROWS_472: dict[float, dict] = {}

COVERAGE_471 = Coverage(0.0, 0.0, "mm")
COVERAGE_472 = Coverage(0.0, 0.0, "mm")

register("DIN 471", ROWS_471, COVERAGE_471, SOURCE)
register("DIN 472", ROWS_472, COVERAGE_472, SOURCE)

_ROWS = {"DIN 471": ROWS_471, "DIN 472": ROWS_472}


def ring_groove_dims(standard: str, d: float) -> dict:
    """``{"m": ..., "d2": ...}`` for a nominal size, or a named refusal."""
    key = " ".join(str(standard or "").split()).upper()
    if key not in _ROWS:
        raise ValueError(f"retaining-ring groove: expected DIN 471 or DIN 472, got {standard!r}.")
    rows = _ROWS[key]
    if not rows:
        raise ValueError(
            f"{key}: no rows are transcribed in this build, so no groove can be looked up "
            f"for d = {float(d):g} mm. Pass m and d2 explicitly, or transcribe "
            f"{SOURCE.split(' - ')[0]} into engineering/mech/standards/rings.py. "
            "Nothing is interpolated."
        )
    size = float(d)
    if size in rows:
        return {name: float(rows[size][name]) for name in RING_ROW_KEYS}
    raise ValueError(f"{key}: d = {size:g} mm has no transcribed row (sizes: {sorted(rows)}).")
