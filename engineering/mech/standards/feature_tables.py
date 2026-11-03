"""Which B40 feature tables have rows, and one probe of each for the smoke and v9.

The B40 tables (DIN 471, DIN 472, DIN 509-E/F, DIN 332-A/B, ISO 3601-2 radial
static and axial) are filled only through the double entry of
``scripts/build_standards.py``. A table without rows is *deferred* - to
:data:`~engineering.mech.standards.transcription.DEFERRED_RELEASE`, by name -
and anything that exercises the tables (``scripts/smoke_mech_com.py``, the v9
``std_tables_b40`` task, the ``b40_between_rows_refused`` correctness check)
skips it with that message instead of reporting a vacuous pass.

:func:`probe` names, for a filled table, one in-coverage size, the
``std_feature_draw`` call that draws it (``None`` for the axial O-ring housing:
no drawing path takes an axial housing in this build, so it is looked up only)
and one size that must be refused - between the two smallest rows, or past the
only row - with nothing interpolated.
"""

from __future__ import annotations

# centres / orings / rings / undercuts register the B40 tables: imported for that.
from engineering.mech.standards import _table, centres, orings, rings, undercuts  # noqa: F401
from engineering.mech.standards.transcription import DEFERRED_RELEASE, TABLE_NAMES

#: registry name -> (std_feature_draw kind, the params besides the size)
_DRAW: dict[str, tuple[str, dict] | None] = {
    "DIN 471": ("ring_groove", {"kind": "shaft", "standard": "DIN 471"}),
    "DIN 472": ("ring_groove", {"kind": "bore", "standard": "DIN 472"}),
    "DIN 509-E": ("undercut", {"form": "E"}),
    "DIN 509-F": ("undercut", {"form": "F"}),
    "DIN 332-A": ("centre_hole", {"form": "A"}),
    "DIN 332-B": ("centre_hole", {"form": "B"}),
    "ISO 3601-2": ("oring_groove", {"kind": "shaft"}),
    "ISO 3601-2-axial": None,
}


def sizes(name: str) -> tuple[float, ...]:
    """The transcribed sizes of ``name``, smallest first."""
    return tuple(sorted(float(key) for key in _table(name).rows))


def filled_tables() -> tuple[str, ...]:
    return tuple(name for name in TABLE_NAMES if sizes(name))


def deferred_tables() -> tuple[str, ...]:
    return tuple(name for name in TABLE_NAMES if not sizes(name))


def deferred_message() -> str:
    deferred = deferred_tables()
    if not deferred:
        return "every B40 table has transcribed rows"
    return (
        f"no transcribed rows (deferred to {DEFERRED_RELEASE}, awaiting the licensed copies): "
        + ", ".join(deferred)
    )


def draw_params(name: str, size: float) -> tuple[str, dict] | None:
    """``(kind, params)`` for ``std_feature_draw`` at ``size``, or None (lookup only)."""
    entry = _DRAW[name]
    if entry is None:
        return None
    kind, extra = entry
    params = dict(extra)
    if kind in ("ring_groove", "undercut"):
        params["d"] = float(size)
    elif kind == "centre_hole":
        params["size"] = float(size)
    else:  # oring_groove: a shaft wide enough for any groove shallower than the cord
        params["cord"] = float(size)
        params["d"] = max(20.0, 10.0 * float(size))
    return kind, params


def probe(name: str) -> dict:
    """One in-coverage size and one size to refuse, for a table that has rows."""
    known = sizes(name)
    if not known:
        raise ValueError(f"{name}: {deferred_message()}")
    size = known[0]
    if len(known) > 1:
        refused, why = (known[0] + known[1]) / 2.0, "between_rows"
    else:
        refused, why = known[0] * 1.5, "outside_coverage"
    return {
        "standard": name,
        "size": size,
        "draw": draw_params(name, size),
        "refused_size": refused,
        "refused_draw": draw_params(name, refused),
        "refusal": why,
    }
