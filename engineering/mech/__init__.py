"""The mechanical part model, view engine and standards tables.

Merge rule for the four track-B/G branches: the re-exports below are additive
and alphabetical — keep every side's names.
"""

from __future__ import annotations

from .part import (
    PART_KINDS,
    PrismaticPart,
    RevolvedPart,
    Segment,
    build_part,
    inner_radius_at,
    outer_radius_at,
    outline_bbox,
    part_length,
    part_max_diameter,
    part_to_dict,
    point_in_outline,
    segment_bounds,
    validate_part,
)
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
    "PART_KINDS",
    "ROLES",
    "ROLE_LAYER",
    "Arc",
    "Circle",
    "DimIntent",
    "HatchArea",
    "Line",
    "Poly",
    "Prim",
    "PrismaticPart",
    "Pt",
    "RevolvedPart",
    "Role",
    "Segment",
    "Text",
    "bbox",
    "build_part",
    "inner_radius_at",
    "layer_for",
    "outer_radius_at",
    "outline_bbox",
    "part_length",
    "part_max_diameter",
    "part_to_dict",
    "point_in_outline",
    "points_of",
    "rotate",
    "scale",
    "segment_bounds",
    "translate",
    "validate_part",
]
