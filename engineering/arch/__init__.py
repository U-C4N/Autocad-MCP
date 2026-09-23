"""Architectural 2D: the plan model and the engine that draws it (track F).

Merge rule for the three track-F branches: the re-exports below are additive
and alphabetical - keep every side's names.
"""

from __future__ import annotations

from .dimension import (
    FIRST_OFFSET_PAPER_MM,
    ROWS,
    SIDES,
    STEP_PAPER_MM,
    exterior_chains,
)
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
from .openings import opening_prims
from .rooms import (
    detect_rooms,
    label_room,
    opening_closures,
    read_records,
    room_label_prims,
    rooms_detect,
)
from .schedule import SCHEDULE_KINDS, draw_schedule, schedule_rows
from .stairs import (
    ARROW_PAPER_MM,
    BLONDEL_RANGE,
    STAIR_KINDS,
    TEXT_PAPER_MM,
    TURNS,
    blondel,
    stair_prims,
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
    "ARROW_PAPER_MM",
    "BLONDEL_RANGE",
    "DEFAULT_TOL",
    "FIRST_OFFSET_PAPER_MM",
    "Face",
    "KINDS",
    "LANGS",
    "MIN_JOIN_ANGLE_DEG",
    "Opening",
    "ROWS",
    "Room",
    "SCHEDULE_KINDS",
    "SIDES",
    "STAIR_KINDS",
    "STEP_PAPER_MM",
    "Stair",
    "TEXT_PAPER_MM",
    "TURNS",
    "WALL_HATCH_TABLE",
    "WALL_MATERIALS",
    "Wall",
    "WallGeometry",
    "axis_segments",
    "blondel",
    "decode",
    "detect_rooms",
    "draw_schedule",
    "encode",
    "exterior_chains",
    "face_containing",
    "face_offsets",
    "fmt_area_m2",
    "fmt_number",
    "from_payload",
    "label_room",
    "opening_closures",
    "opening_from_dict",
    "opening_prims",
    "planar_faces",
    "read_records",
    "room_from_dict",
    "room_label_prims",
    "room_segments",
    "rooms_detect",
    "schedule_rows",
    "segment_of",
    "stair_from_dict",
    "stair_prims",
    "tag_prefix",
    "tagged_faces",
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
