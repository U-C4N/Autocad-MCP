"""The mechanical part model, view engine and standards tables.

Merge rule for the four track-B/G branches: the re-exports below are additive
and alphabetical — keep every side's names.
"""

from __future__ import annotations

from .primitives import (
    ROLE_LAYER,
    ROLES,
    Arc,
    Circle,
    DimIntent,
    HatchArea,
    Line,
    Poly,
    Prim,
    Pt,
    Role,
    Text,
    bbox,
    layer_for,
    points_of,
    rotate,
    scale,
    translate,
)

__all__ = [
    "ROLES",
    "ROLE_LAYER",
    "Arc",
    "Circle",
    "DimIntent",
    "HatchArea",
    "Line",
    "Poly",
    "Prim",
    "Pt",
    "Role",
    "Text",
    "bbox",
    "layer_for",
    "points_of",
    "rotate",
    "scale",
    "translate",
]
