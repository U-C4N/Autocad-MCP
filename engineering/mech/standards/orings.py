"""ISO 3601-2 O-ring housing (groove) dimensions.

SOURCE
------
ISO 3601-2:2016, *Fluid power systems - O-rings - Part 2: Housing dimensions
for general applications*, Tables 1 and 2 (radial static housings).

COVERAGE
--------
**No rows are transcribed in this build** (same rule as ``undercuts.py``): the
groove width and depth per cord diameter decide whether the seal works, and
they are not written here from memory. Every lookup is refused by name.

Extending is a data edit: fill ``ROWS`` with
``{cord_diameter_mm: {"b": groove_width_mm, "h": groove_depth_mm}}`` - the two
keys group S's ``std_feature_draw`` already reads - and widen ``COVERAGE``.
"""

from __future__ import annotations

from engineering.mech.standards import Coverage, register

SOURCE = "ISO 3601-2:2016, Tables 1-2 (radial static housings) - NOT TRANSCRIBED in this build"

#: The keys every ISO 3601-2 row must carry.
ORING_ROW_KEYS: tuple[str, ...] = ("b", "h")

KINDS: tuple[str, ...] = ("radial", "axial")

ROWS: dict[float, dict] = {}

COVERAGE = Coverage(0.0, 0.0, "mm")

register("ISO 3601-2", ROWS, COVERAGE, SOURCE)


def oring_groove_dims(cord: float, kind: str = "radial") -> dict:
    """``{"b": width, "h": depth}`` for a cord diameter, or a named refusal."""
    key = str(kind or "").strip().lower()
    if key not in KINDS:
        raise ValueError(f"O-ring housing: expected kind 'radial' or 'axial', got {kind!r}.")
    if not ROWS:
        raise ValueError(
            f"ISO 3601-2: no rows are transcribed in this build, so no {key} housing can be "
            f"looked up for a {float(cord):g} mm cord. Pass b and h explicitly, or transcribe "
            f"{SOURCE.split(' - ')[0]} into engineering/mech/standards/orings.py. "
            "Nothing is interpolated."
        )
    size = float(cord)
    if size in ROWS:
        return {name: float(ROWS[size][name]) for name in ORING_ROW_KEYS}
    raise ValueError(
        f"ISO 3601-2: cord {size:g} mm has no transcribed row (sizes: {sorted(ROWS)})."
    )
