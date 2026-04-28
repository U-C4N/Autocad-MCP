"""Standard engineering layer + linetype scaffold for production drawings."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

log = logging.getLogger(__name__)


ENGINEERING_LAYERS: list[tuple[str, int, str, float, str]] = [
    # (name, color, linetype, lineweight, description)
    ("0",            7,  "Continuous", 0.25, "default"),
    ("GEOMETRY",     7,  "Continuous", 0.50, "visible solid edges"),
    ("HIDDEN",       3,  "HIDDEN",     0.25, "hidden edges"),
    ("CENTER",       1,  "CENTER",     0.18, "centerlines"),
    ("PHANTOM",      6,  "PHANTOM",    0.18, "section / cutting planes"),
    ("DIM",          2,  "Continuous", 0.18, "dimensions"),
    ("TEXT",         7,  "Continuous", 0.25, "annotations"),
    ("HATCH",        8,  "Continuous", 0.13, "section hatching"),
    ("TITLEBLOCK",   7,  "Continuous", 0.50, "drawing border + title"),
    ("CONSTRUCTION", 250, "Continuous", 0.05, "scratch / construction"),
]

STANDARD_LINETYPES: list[str] = ["CENTER", "HIDDEN", "PHANTOM", "DASHED", "DASHDOT"]


async def ensure_standard_linetypes(backend: "AutoCADBackend") -> dict[str, str]:
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


async def ensure_engineering_layers(backend: "AutoCADBackend") -> dict[str, str]:
    """Idempotently create every layer in ENGINEERING_LAYERS via backend.layer_create."""
    await ensure_standard_linetypes(backend)

    try:
        existing = {lyr.name.lower() for lyr in await backend.layer_list()}
    except Exception as exc:
        log.warning("layer_list failed; assuming empty: %s", exc)
        existing = set()

    results: dict[str, str] = {}
    for name, color, linetype, lineweight, _desc in ENGINEERING_LAYERS:
        if name.lower() in existing:
            results[name] = "exists"
            continue
        try:
            await backend.layer_create(
                name=name, color=color, linetype=linetype, lineweight=lineweight,
            )
            results[name] = "created"
        except Exception as exc:
            results[name] = f"failed: {exc}"
    return results
