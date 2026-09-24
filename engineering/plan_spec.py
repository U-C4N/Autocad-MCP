"""Premium drawing planning + critique data models.

PlanSpec is committed up-front by the LLM via `drawing_plan(...)` before any
geometry is created; CritiqueFocus is a closed enum to keep
`drawing_critique(...)` scope from drifting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SheetSize = Literal["A4", "A3", "A2", "A1", "A0"]
LayerSetId = Literal["iso13567", "mech", "pid", "arch"]
DimStyle = Literal["chain", "baseline", "ordinate", "mixed"]
CritiqueFocus = Literal[
    "iso128",
    "layer_color",
    "dim_overlap",
    "untrimmed_corner",
    "duplicate_entities",
    "construction_left",
    "gdt",
    "pid_dangling_line",
    "pid_duplicate_tag",
    "pid_incompatible_connection",
    "pid_untagged_instrument",
    "pid_illegal_tag",
    "pid_unconnected_equipment",
    "mech_missing_centreline",
    "mech_unhatched_section",
    "mech_view_misaligned",
    "mech_duplicate_dimension",
    "mech_thread_unrepresented",
    "mech_bom_balloon_mismatch",
    "arch_room_unlabelled",
    "arch_wall_gap",
    "arch_opening_clash",
    "topo_dangling_endpoint",
    "topo_near_miss",
    "topo_interior_crossing",
]
SnapType = Literal["end", "mid", "center", "quad", "int", "perp", "near"]

ALL_CRITIQUE_FOCUSES: tuple[CritiqueFocus, ...] = (
    "iso128",
    "layer_color",
    "dim_overlap",
    "untrimmed_corner",
    "duplicate_entities",
    "construction_left",
    "gdt",
    "pid_dangling_line",
    "pid_duplicate_tag",
    "pid_incompatible_connection",
    "pid_untagged_instrument",
    "pid_illegal_tag",
    "pid_unconnected_equipment",
    "mech_missing_centreline",
    "mech_unhatched_section",
    "mech_view_misaligned",
    "mech_duplicate_dimension",
    "mech_thread_unrepresented",
    "mech_bom_balloon_mismatch",
    "arch_room_unlabelled",
    "arch_wall_gap",
    "arch_opening_clash",
    "topo_dangling_endpoint",
    "topo_near_miss",
    "topo_interior_crossing",
)


@dataclass
class PlanSpec:
    """Drawing intent committed before geometry creation.

    Returned by `drawing_plan(...)` and replayed by `drawing_critique(...)` to
    verify the finished drawing matches the original intent.
    """

    intent: str
    sheet_size: SheetSize
    scale: float
    layer_set_id: LayerSetId
    view_count: int = 1
    dim_style: DimStyle = "chain"
    notes: list[str] = field(default_factory=list)
    requirements: dict = field(default_factory=dict)
    spec_hash: str | None = None
    preflight_status: Literal["ready", "skipped"] = "skipped"

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "sheet_size": self.sheet_size,
            "scale": self.scale,
            "layer_set_id": self.layer_set_id,
            "view_count": self.view_count,
            "dim_style": self.dim_style,
            "notes": list(self.notes),
            "requirements": dict(self.requirements),
            "spec_hash": self.spec_hash,
            "preflight_status": self.preflight_status,
        }


@dataclass
class Issue:
    """One problem reported by `drawing_critique(...)`."""

    severity: Literal["error", "warning", "info"]
    focus: CritiqueFocus
    message: str
    handles: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "focus": self.focus,
            "message": self.message,
            "handles": list(self.handles),
            "detail": self.detail,
        }


# ISO 128 lineweight set (mm). Any lineweight outside this set is flagged.
ISO_128_LINEWEIGHTS_MM: tuple[float, ...] = (
    0.13,
    0.18,
    0.25,
    0.35,
    0.50,
    0.70,
    1.00,
    1.40,
    2.00,
)
