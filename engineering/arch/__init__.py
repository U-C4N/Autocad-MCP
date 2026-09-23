"""Architectural 2D: the plan model and the engine that draws it (track F).

Merge rule for the three track-F branches: the re-exports below are additive
and alphabetical - keep every side's names.
"""

from __future__ import annotations

from .lang import LANGS, fmt_area_m2, fmt_number, tag_prefix, vocab
from .layers import ARCH_LAYER_DEFS, ARCH_LAYER_ROLES, ARCH_LAYERS, ARCH_ROLE_LAYER
from .materials import WALL_HATCH_TABLE, WALL_MATERIALS, wall_hatch
from .model import (
    APP_ID,
    KINDS,
    Opening,
    Room,
    Stair,
    Wall,
    axis_segments,
    decode,
    encode,
    from_payload,
    opening_from_dict,
    room_from_dict,
    segment_of,
    stair_from_dict,
    to_payload,
    to_values,
    validate_openings,
    wall_from_dict,
    wall_length,
)
from .walls import (
    MIN_JOIN_ANGLE_DEG,
    WallGeometry,
    face_offsets,
    room_segments,
    wall_geometry,
    wall_geometry_by_wall,
    wall_segments,
)

__all__ = [
    "APP_ID",
    "ARCH_LAYERS",
    "ARCH_LAYER_DEFS",
    "ARCH_LAYER_ROLES",
    "ARCH_ROLE_LAYER",
    "KINDS",
    "LANGS",
    "MIN_JOIN_ANGLE_DEG",
    "Opening",
    "Room",
    "Stair",
    "WALL_HATCH_TABLE",
    "WALL_MATERIALS",
    "Wall",
    "WallGeometry",
    "axis_segments",
    "decode",
    "encode",
    "face_offsets",
    "fmt_area_m2",
    "fmt_number",
    "from_payload",
    "opening_from_dict",
    "room_from_dict",
    "room_segments",
    "segment_of",
    "stair_from_dict",
    "tag_prefix",
    "to_payload",
    "to_values",
    "validate_openings",
    "vocab",
    "wall_from_dict",
    "wall_geometry",
    "wall_geometry_by_wall",
    "wall_hatch",
    "wall_length",
    "wall_segments",
]
