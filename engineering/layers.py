"""Standard engineering layer + linetype scaffold for production drawings."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

log = logging.getLogger(__name__)


ENGINEERING_LAYERS: list[tuple[str, int, str, float, str]] = [
    # (name, color, linetype, lineweight, description)
    ("0", 7, "Continuous", 0.25, "default"),
    ("GEOMETRY", 7, "Continuous", 0.50, "visible solid edges"),
    ("HIDDEN", 3, "HIDDEN", 0.25, "hidden edges"),
    ("CENTER", 1, "CENTER", 0.18, "centerlines"),
    ("PHANTOM", 6, "PHANTOM", 0.18, "section / cutting planes"),
    ("DIM", 2, "Continuous", 0.18, "dimensions"),
    ("TEXT", 7, "Continuous", 0.25, "annotations"),
    ("HATCH", 8, "Continuous", 0.13, "section hatching"),
    ("TITLEBLOCK", 7, "Continuous", 0.50, "drawing border + title"),
    ("CONSTRUCTION", 250, "Continuous", 0.05, "scratch / construction"),
]


# P&ID standard layer set (de-facto, distilled from ISO 10628 + common practice).
PID_LAYERS: list[tuple[str, int, str, float, str]] = [
    ("0", 7, "Continuous", 0.25, "default"),
    ("PROCESS-PIPING-MAIN", 1, "Continuous", 0.70, "main process line (heavy)"),
    ("PROCESS-PIPING-SECONDARY", 2, "Continuous", 0.50, "secondary process line"),
    ("PROCESS-EQUIPMENT", 7, "Continuous", 0.50, "vessels / pumps / drums"),
    ("PROCESS-VALVES", 3, "Continuous", 0.35, "valves / fittings"),
    ("INSTRUMENT-SYMBOL", 4, "Continuous", 0.35, "instrument bubbles / FCFs"),
    ("INSTRUMENT-LINE-SIGNAL", 4, "DASHED", 0.18, "pneumatic / electrical signal"),
    ("INSTRUMENT-TAG-TEXT", 7, "Continuous", 0.25, "tag labels"),
    ("PROCESS-LINE-TEXT", 7, "Continuous", 0.25, "line numbers"),
    ("ELECTRICAL-LINE", 6, "DASHDOT", 0.18, "electrical line"),
    ("UTILITY-LINE", 5, "PHANTOM", 0.25, "utility (steam, water, air)"),
    ("INSULATION-HATCH", 8, "Continuous", 0.13, "insulation hatch"),
    ("DIM", 2, "Continuous", 0.18, "dimensions"),
    ("TEXT-NOTES", 7, "Continuous", 0.25, "free-form notes"),
    ("TITLEBLOCK", 7, "Continuous", 0.50, "drawing border + title"),
    ("CONSTRUCTION", 250, "Continuous", 0.05, "scratch / construction"),
]


# ISO 13567-style layer naming kit (Agent2-Element6-Presentation2-Status1).
# This is a *starter* set — extend as needed per discipline.
ISO13567_LAYERS: list[tuple[str, int, str, float, str]] = [
    ("0", 7, "Continuous", 0.25, "default"),
    ("M-GEOMET-E-N", 7, "Continuous", 0.50, "Mechanical / geometry / edge / new"),
    ("M-HIDDEN-E-N", 3, "HIDDEN", 0.25, "Mechanical / hidden edge"),
    ("M-CENTL-E-N", 1, "CENTER", 0.18, "Mechanical / centerlines"),
    ("M-PHANT-E-N", 6, "PHANTOM", 0.18, "Mechanical / phantom"),
    ("M-DIMEN-T-N", 2, "Continuous", 0.18, "Mechanical / dimension text"),
    ("M-TEXT-T-N", 7, "Continuous", 0.25, "Mechanical / annotation text"),
    ("M-HATCH-H-N", 8, "Continuous", 0.13, "Mechanical / hatch"),
    ("M-TITLE-T-N", 7, "Continuous", 0.50, "Title block"),
    ("M-CONST-E-N", 250, "Continuous", 0.05, "Construction (scratch)"),
]


LAYER_SET_REGISTRY: dict[str, list[tuple[str, int, str, float, str]]] = {
    "mech": ENGINEERING_LAYERS,
    "pid": PID_LAYERS,
    "iso13567": ISO13567_LAYERS,
}

# Role → layer name, per layer set. Lets dimension_auto / construction ops
# resolve the correct layer for the active standard instead of hardcoding
# "DIM"/"CONSTRUCTION" (which only exist in the mech/pid sets — iso13567 uses
# M-DIMEN-T-N / M-CONST-E-N).
LAYER_ROLES: dict[str, dict[str, str]] = {
    "mech": {"dim": "DIM", "construction": "CONSTRUCTION"},
    "pid": {"dim": "DIM", "construction": "CONSTRUCTION"},
    "iso13567": {"dim": "M-DIMEN-T-N", "construction": "M-CONST-E-N"},
}


def resolve_role_layer(layer_set_id: str | None, role: str) -> str:
    """Resolve the layer name for `role` ('dim' | 'construction') in the active
    layer set, falling back to the mech/default name when unknown."""
    roles = LAYER_ROLES.get(layer_set_id or "mech", LAYER_ROLES["mech"])
    return roles.get(role, LAYER_ROLES["mech"][role])


STANDARD_LINETYPES: list[str] = ["CENTER", "HIDDEN", "PHANTOM", "DASHED", "DASHDOT"]


async def ensure_standard_linetypes(backend: AutoCADBackend) -> dict[str, str]:
    """Idempotently load STANDARD_LINETYPES via backend.linetype_load."""
    try:
        existing = {ln.lower() for ln in await backend.linetype_list()}
    except Exception as exc:
        log.warning("linetype_list failed; assuming none loaded: %s", exc)
        existing = set()

    results: dict[str, str] = {}
    for name in STANDARD_LINETYPES:
        if name.lower() in existing:
            results[name] = "already_loaded"
            continue
        try:
            await backend.linetype_load(name)
            results[name] = "loaded"
        except Exception as exc:
            results[name] = f"failed: {exc}"
    return results


async def ensure_engineering_layers(backend: AutoCADBackend) -> dict[str, str]:
    """Idempotently create every layer in ENGINEERING_LAYERS via backend.layer_create."""
    return await apply_layer_set(backend, "mech")


async def apply_layer_set(
    backend: AutoCADBackend,
    standard: str = "mech",
) -> dict[str, str]:
    """Idempotently apply a named layer set ('mech', 'pid', 'iso13567', 'arch')."""
    layer_set = LAYER_SET_REGISTRY.get(standard)
    if layer_set is None:
        raise RuntimeError(
            f"Unknown layer set '{standard}'. Choose one of: {sorted(LAYER_SET_REGISTRY)}"
        )
    await ensure_standard_linetypes(backend)

    try:
        existing = {lyr.name.lower() for lyr in await backend.layer_list()}
    except Exception as exc:
        log.warning("layer_list failed; assuming empty: %s", exc)
        existing = set()

    results: dict[str, str] = {}
    for name, color, linetype, lineweight, _desc in layer_set:
        if name.lower() in existing:
            results[name] = "exists"
            continue
        try:
            await backend.layer_create(
                name=name,
                color=color,
                linetype=linetype,
                lineweight=lineweight,
            )
            results[name] = "created"
        except Exception as exc:
            results[name] = f"failed: {exc}"
    return results


# ── the architectural set (track F) ─────────────────────────────────────────
# The rows live with the plan model (engineering/arch/layers.py), where
# ARCH_ROLE_LAYER maps each role onto them. Registered last, after every name
# above exists: importing engineering.arch runs its package __init__, which
# pulls in the mechanical codec - and, once track F is merged, the wall engine
# - and those import from this module. A partially initialised
# engineering.layers must already hold every name they ask for.
from engineering.arch.layers import ARCH_LAYER_ROLES, ARCH_LAYERS  # noqa: E402

LAYER_SET_REGISTRY["arch"] = ARCH_LAYERS
LAYER_ROLES["arch"] = dict(ARCH_LAYER_ROLES)
