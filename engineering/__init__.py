"""Deterministic engineering CAD layer for AutoCAD MCP Pro."""

from .gear import (
    draw_gear_section_aa,
    draw_helical_gear_front_view,
    draw_spur_gear_front_view,
    generate_full_gear_outline,
    generate_involute_flank,
    generate_tooth_profile,
    involute_xy,
)
from .keyway import (
    DIN6885_TABLE,
    draw_keyed_bore,
    draw_keyway_section,
    keyway_dimensions,
)
from .layers import (
    ENGINEERING_LAYERS,
    STANDARD_LINETYPES,
    ensure_engineering_layers,
    ensure_standard_linetypes,
)
from .section import apply_section_hatch, cutting_plane_line

# Agent B contributes titleblock + validator. Imports are guarded so this
# package remains usable while B is still in flight; once those modules land
# the symbols flow through transparently.
try:
    from .titleblock import TitleBlockMetadata, apply_iso_a3_titleblock
except ImportError:
    TitleBlockMetadata = None  # type: ignore[assignment]
    apply_iso_a3_titleblock = None  # type: ignore[assignment]

try:
    from .validator import DrawingValidator, ValidationFinding, ValidationResult
except ImportError:
    DrawingValidator = None  # type: ignore[assignment]
    ValidationFinding = None  # type: ignore[assignment]
    ValidationResult = None  # type: ignore[assignment]

__all__ = [
    "draw_helical_gear_front_view",
    "draw_spur_gear_front_view",
    "draw_gear_section_aa",
    "generate_full_gear_outline",
    "generate_involute_flank",
    "generate_tooth_profile",
    "involute_xy",
    "draw_keyed_bore",
    "draw_keyway_section",
    "keyway_dimensions",
    "DIN6885_TABLE",
    "cutting_plane_line",
    "apply_section_hatch",
    "ENGINEERING_LAYERS",
    "STANDARD_LINETYPES",
    "ensure_engineering_layers",
    "ensure_standard_linetypes",
    "apply_iso_a3_titleblock",
    "TitleBlockMetadata",
    "DrawingValidator",
    "ValidationResult",
    "ValidationFinding",
]
