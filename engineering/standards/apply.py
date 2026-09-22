"""One call to put a drawing on a drafting standard: styles, units, layers.

Composes only backend methods that already exist (``textstyle_*``,
``dimstyle_*``, ``system_get/set_variable``, ``drawing_apply_iso_layers``),
so it runs on both engines and needs nothing from the settings facade. Units
are the five system variables both standards share; the decimal separator is
the preset's and arrives through the dimension style. ANSI layer naming is
company-specific, so both standards use the ``mech`` layer set — only styles
and the separator differ (spec §4.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engineering.standards.dimstyles import resolve_dimstyle
from engineering.standards.textstyles import TEXT_PRESETS

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = ["STANDARDS", "STANDARD_SYSVARS", "apply_standard", "resolve_standard"]

STANDARDS: dict[str, dict[str, str]] = {
    "iso": {"preset": "iso-25", "dimstyle": "ISO-25", "textstyle": "ISOCP", "layer_set": "mech"},
    "ansi": {"preset": "ansi", "dimstyle": "ANSI", "textstyle": "ROMANS", "layer_set": "mech"},
}

#: Millimetres, decimal linear and angular units, 1:1 linetype and dimension
#: scale. Written by name so the report can say what moved.
STANDARD_SYSVARS: tuple[tuple[str, Any], ...] = (
    ("INSUNITS", 4),
    ("LUNITS", 2),
    ("AUNITS", 0),
    ("LTSCALE", 1.0),
    ("DIMSCALE", 1.0),
)


def resolve_standard(standard: str) -> tuple[str, dict[str, str]]:
    """``(key, row)`` for ``iso`` / ``ansi`` (any case), else ``ValueError``."""
    key = str(standard).strip().lower() if standard is not None else ""
    if key not in STANDARDS:
        raise ValueError(f"standard: unknown value {standard!r}; use one of {sorted(STANDARDS)}")
    return key, STANDARDS[key]


def _same(old: Any, new: Any) -> bool:
    if old is None:
        return False
    try:
        return abs(float(old) - float(new)) < 1e-9
    except (TypeError, ValueError):
        return str(old) == str(new)


async def apply_standard(
    backend: AutoCADBackend, standard: str, layers: bool = True, units: bool = True
) -> dict:
    """Styles created (or reused) and made current, units written, layers bootstrapped.

    Every item reports whether it was created or already present; ``settings``
    reports only the variables that moved. An existing style of the standard's
    name is used as it is — ``dimstyle_modify`` changes one on purpose.

    Every system variable the call will write is read *before* the first
    style is created, so a read the engine refuses is a refusal with nothing
    written. (The units loop used to run after both styles; on the live
    engine its INSUNITS read went through a member the ActiveX Application
    does not have, so the call failed with ISO-25 and ISOCP already created
    and current.)
    """
    key, row = resolve_standard(standard)
    if not isinstance(layers, bool) or not isinstance(units, bool):
        raise TypeError("layers and units must be booleans")

    current: dict[str, Any] = {}
    if units:
        for var, _value in STANDARD_SYSVARS:
            try:
                current[var] = await backend.system_get_variable(var)
            except Exception as exc:
                raise RuntimeError(
                    f"apply_standard: could not read {var} before writing "
                    f"({exc}); nothing was changed"
                ) from exc

    text_names = {entry["name"].lower() for entry in await backend.textstyle_list()}
    textstyle_created = row["textstyle"].lower() not in text_names
    if textstyle_created:
        font_file, width, oblique = TEXT_PRESETS[row["textstyle"]]
        await backend.textstyle_create(row["textstyle"], font_file, 0.0, width, oblique, False)
    await backend.textstyle_set_current(row["textstyle"])

    dim_names = {entry["name"].lower() for entry in await backend.dimstyle_list()}
    dimstyle_created = row["dimstyle"].lower() not in dim_names
    if dimstyle_created:
        await backend.dimstyle_create(
            row["dimstyle"], resolve_dimstyle(row["preset"], None), set_current=True
        )
    else:
        await backend.dimstyle_set_current(row["dimstyle"])

    changed: dict[str, list] = {}
    if units:
        for var, value in STANDARD_SYSVARS:
            old = current[var]
            if _same(old, value):
                continue
            await backend.system_set_variable(var, value)
            changed[var] = [old, value]

    layer_result = None
    if layers:
        applied = await backend.drawing_apply_iso_layers(row["layer_set"])
        layer_result = {"layer_set": row["layer_set"], "layers": applied["layers"]}

    return {
        "standard": key,
        "dimstyle": {"name": row["dimstyle"], "created": dimstyle_created, "current": True},
        "textstyle": {"name": row["textstyle"], "created": textstyle_created, "current": True},
        "settings": {"changed": changed, "applied": units},
        "layers": layer_result,
    }
