"""DIN 509 relief grooves (Freistiche), forms E and F.

SOURCE
------
DIN 509:1998-06, *Technische Zeichnungen - Freistiche*, Table 1 (forms E and F).

COVERAGE
--------
**No rows are transcribed in this build.** The track's table rule is absolute:
a row that cannot be verified against the standard's own table is not shipped,
because a wrong relief depth is a wrong workshop drawing while a narrow table
is merely narrow. Every lookup here is therefore refused by name and names the
route to fix it.

Extending this module is a **data edit with no code change**: fill ``ROWS_E`` /
``ROWS_F`` with ``{shaft_diameter_mm: {"profile": [[along, depth], ...]}}`` and
widen ``COVERAGE_E`` / ``COVERAGE_F`` to the transcribed range. ``profile`` is
the standard's own published relief profile expressed as (distance along the
axis from the shoulder corner, depth below the smaller cylinder) pairs in
millimetres - the same ``profile`` key group S's ``std_feature_draw`` reads -
so nothing downstream ever reconstructs a profile it was not handed.
"""

from __future__ import annotations

from engineering.mech.standards import Coverage, register

SOURCE = "DIN 509:1998-06, Table 1 (forms E and F) - NOT TRANSCRIBED in this build"

#: The keys every DIN 509 row must carry for the drawing code to use it.
UNDERCUT_ROW_KEYS: tuple[str, ...] = ("profile",)

FORMS: tuple[str, ...] = ("E", "F")

ROWS_E: dict[float, dict] = {}
ROWS_F: dict[float, dict] = {}

COVERAGE_E = Coverage(0.0, 0.0, "mm")
COVERAGE_F = Coverage(0.0, 0.0, "mm")

register("DIN 509-E", ROWS_E, COVERAGE_E, SOURCE)
register("DIN 509-F", ROWS_F, COVERAGE_F, SOURCE)

_ROWS = {"E": ROWS_E, "F": ROWS_F}


def undercut_profile(form: str, d: float) -> tuple[tuple[float, float], ...]:
    """The published relief profile for a shaft diameter, or a named refusal."""
    key = str(form or "").strip().upper()
    if key not in FORMS:
        raise ValueError(f"DIN 509 defines forms E and F; got {form!r}.")
    rows = _ROWS[key]
    if not rows:
        raise ValueError(
            f"DIN 509 form {key}: no rows are transcribed in this build, so no relief "
            f"profile can be looked up for d = {float(d):g} mm. Pass the profile "
            "explicitly (Undercut(profile=[[along, depth], ...])), or transcribe "
            f"{SOURCE.split(' - ')[0]} into engineering/mech/standards/undercuts.py. "
            "Nothing is interpolated and no profile is invented."
        )
    size = float(d)
    if size in rows:
        return tuple(tuple(float(v) for v in pair) for pair in rows[size]["profile"])
    raise ValueError(
        f"DIN 509 form {key}: d = {size:g} mm has no transcribed row "
        f"(transcribed sizes: {sorted(rows)})."
    )
