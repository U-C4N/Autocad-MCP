"""Deterministic engineering CAD layer for AutoCAD MCP Pro."""

from .annotation import TableLayout, prepare_table_layout, validate_mleader
from .delivery import (
    DeliveryResult,
    compare_drawing_snapshots,
    deliver_drawing,
    drawing_snapshot,
)
from .gear import (
    draw_gear_section_aa,
    draw_helical_gear_front_view,
    draw_spur_gear_front_view,
    generate_full_gear_outline,
    generate_involute_flank,
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
from .plan_spec import (
    ALL_CRITIQUE_FOCUSES,
    ISO_128_LINEWEIGHTS_MM,
    CritiqueFocus,
    DimStyle,
    Issue,
    LayerSetId,
    PlanSpec,
    SheetSize,
    SnapType,
)
from .preflight import (
    PreflightConflict,
    PreflightQuestion,
    PreflightResult,
    preflight_drawing,
)
from .refiner import RefineResult, RefineRound, RepairAction, refine_drawing
from .sheet.frames import draw_sheet_frame, frame_metrics, frame_prims
from .sheet.titleblock import apply_titleblock, titleblock_prims
from .titleblock import TitleBlockMetadata, apply_iso_a3_titleblock
from .validator import DrawingValidator, ValidationFinding, ValidationResult

__all__ = [
    "draw_helical_gear_front_view",
    "DeliveryResult",
    "compare_drawing_snapshots",
    "deliver_drawing",
    "drawing_snapshot",
    "TableLayout",
    "prepare_table_layout",
    "validate_mleader",
    "draw_spur_gear_front_view",
    "draw_gear_section_aa",
    "generate_full_gear_outline",
    "generate_involute_flank",
    "involute_xy",
    "draw_keyed_bore",
    "draw_keyway_section",
    "keyway_dimensions",
    "DIN6885_TABLE",
    "ENGINEERING_LAYERS",
    "STANDARD_LINETYPES",
    "ensure_engineering_layers",
    "ensure_standard_linetypes",
    "apply_iso_a3_titleblock",
    "TitleBlockMetadata",
    "DrawingValidator",
    "ValidationResult",
    "ValidationFinding",
    "PlanSpec",
    "Issue",
    "CritiqueFocus",
    "SheetSize",
    "LayerSetId",
    "DimStyle",
    "SnapType",
    "ALL_CRITIQUE_FOCUSES",
    "ISO_128_LINEWEIGHTS_MM",
    "PreflightConflict",
    "PreflightQuestion",
    "PreflightResult",
    "preflight_drawing",
    "RefineResult",
    "RefineRound",
    "RepairAction",
    "refine_drawing",
    "apply_titleblock",
    "draw_sheet_frame",
    "frame_metrics",
    "frame_prims",
    "titleblock_prims",
]
