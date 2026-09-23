"""Architectural 2D: the plan model and the engine that draws it (track F).

Merge rule for the three track-F branches: the re-exports below are additive
and alphabetical - keep every side's names.
"""

from __future__ import annotations

from .catalogue import (
    CATALOGUE_NAMES,
    FAMILIES,
    ITEMS,
    SIZE_BASIS,
    CatalogueItem,
    block_spec,
    catalogue,
    insert_item,
)
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
from .symbols import (
    SYMBOL_KINDS,
    draw_grid,
    draw_symbol,
    fmt_level,
    grid_axes,
    grid_prims,
    symbol_prims,
)

__all__ = [
    "APP_ID",
    "ARCH_LAYERS",
    "ARCH_LAYER_DEFS",
    "ARCH_LAYER_ROLES",
    "ARCH_ROLE_LAYER",
    "CATALOGUE_NAMES",
    "CatalogueItem",
    "FAMILIES",
    "ITEMS",
    "KINDS",
    "LANGS",
    "Opening",
    "Room",
    "SIZE_BASIS",
    "SYMBOL_KINDS",
    "Stair",
    "WALL_HATCH_TABLE",
    "WALL_MATERIALS",
    "Wall",
    "axis_segments",
    "block_spec",
    "catalogue",
    "decode",
    "draw_grid",
    "draw_symbol",
    "encode",
    "fmt_area_m2",
    "fmt_level",
    "fmt_number",
    "from_payload",
    "grid_axes",
    "grid_prims",
    "insert_item",
    "opening_from_dict",
    "room_from_dict",
    "segment_of",
    "stair_from_dict",
    "symbol_prims",
    "tag_prefix",
    "to_payload",
    "to_values",
    "validate_openings",
    "vocab",
    "wall_from_dict",
    "wall_hatch",
    "wall_length",
]
