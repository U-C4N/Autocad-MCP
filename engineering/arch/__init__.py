"""Architectural 2D: the plan model and the engine that draws it (track F).

Merge rule for the three track-F branches: the re-exports below are additive
and alphabetical - keep every side's names.
"""

from __future__ import annotations

from .faces import DEFAULT_TOL, Face, face_containing, planar_faces, tagged_faces
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
from .rooms import (
    detect_rooms,
    label_room,
    opening_closures,
    read_records,
    room_label_prims,
    rooms_detect,
)
from .schedule import SCHEDULE_KINDS, draw_schedule, schedule_rows

__all__ = [
    "APP_ID",
    "ARCH_LAYERS",
    "ARCH_LAYER_DEFS",
    "ARCH_LAYER_ROLES",
    "ARCH_ROLE_LAYER",
    "DEFAULT_TOL",
    "Face",
    "KINDS",
    "LANGS",
    "Opening",
    "Room",
    "SCHEDULE_KINDS",
    "Stair",
    "WALL_HATCH_TABLE",
    "WALL_MATERIALS",
    "Wall",
    "axis_segments",
    "decode",
    "detect_rooms",
    "draw_schedule",
    "encode",
    "face_containing",
    "fmt_area_m2",
    "fmt_number",
    "from_payload",
    "label_room",
    "opening_closures",
    "opening_from_dict",
    "planar_faces",
    "read_records",
    "room_from_dict",
    "room_label_prims",
    "rooms_detect",
    "schedule_rows",
    "segment_of",
    "stair_from_dict",
    "tag_prefix",
    "tagged_faces",
    "to_payload",
    "to_values",
    "validate_openings",
    "vocab",
    "wall_from_dict",
    "wall_hatch",
    "wall_length",
]
