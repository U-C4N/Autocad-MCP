"""Abstract base class + shared data models for AutoCAD backends."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engineering.plan_spec import (
        CritiqueFocus,
        DimStyle,
        Issue,
        LayerSetId,
        PlanSpec,
        SheetSize,
        SnapType,
    )

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class EntityInfo:
    handle: str
    type: str  # e.g. "LINE", "CIRCLE", "ARC"
    layer: str
    color: int  # ACI color (256=ByLayer)
    linetype: str
    visible: bool
    properties: dict = field(default_factory=dict)


@dataclass
class LayerInfo:
    name: str
    color: int
    linetype: str
    lineweight: float
    is_on: bool
    is_frozen: bool
    is_locked: bool
    is_current: bool


@dataclass
class BlockInfo:
    name: str
    origin: tuple[float, float]
    attribute_count: int
    entity_count: int
    is_xref: bool
    description: str = ""


@dataclass
class DrawingInfo:
    name: str
    full_path: str
    saved: bool
    entity_count: int
    layer_count: int
    block_count: int
    extents_min: tuple[float, float]
    extents_max: tuple[float, float]
    units: str
    version: str = ""
    backend: str = ""


@dataclass(frozen=True)
class FeatureCapability:
    supported: bool
    mode: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "mode": self.mode,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CapabilityMap:
    backend: str
    features: dict[str, FeatureCapability]

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "features": {name: value.to_dict() for name, value in self.features.items()},
        }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def normalize_lineweight(lw: float | int | None) -> int | None:
    """Coerce a lineweight to ezdxf/COM hundredths-of-a-mm integers.

    AutoCAD and ezdxf store lineweights as integer hundredths (25 == 0.25 mm),
    plus the -1/-2/-3 sentinels (ByLayer/ByBlock/Default). Callers, however,
    mix conventions: the MCP tool boundary passes hundredths (25) while the
    engineering layer sets pass millimetres (0.25). The old ``int(0.25)``
    silently truncated millimetre values to 0 and wiped the lineweight (and with
    it the whole ISO 128 discipline). Disambiguation is unambiguous because no
    ISO 128 millimetre value exceeds 2.0 and no valid hundredth is below 5, so
    the ``(0, 2.05]`` band is always millimetres.
    """
    if lw is None:
        return None
    try:
        v = float(lw)
    except (TypeError, ValueError):
        return lw  # leave exotic / already-int values untouched
    if v < 0:
        return int(round(v))  # -1/-2/-3 sentinels
    if v == 0:
        return 0
    if v <= 2.05:
        return int(round(v * 100.0))  # millimetres -> hundredths
    return int(round(v))  # already hundredths


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------


class AutoCADBackend(ABC):
    """All backends implement this interface."""

    # ── identity ────────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def capabilities(self) -> CapabilityMap: ...

    # ── drawing management ───────────────────────────────────────────────────
    @abstractmethod
    async def drawing_info(self) -> DrawingInfo: ...

    @abstractmethod
    async def drawing_new(self, template: str | None = None) -> dict: ...

    @abstractmethod
    async def drawing_open(self, path: str) -> dict: ...

    @abstractmethod
    async def drawing_save(self, path: str | None = None) -> dict: ...

    @abstractmethod
    async def drawing_save_as(self, path: str, fmt: str = "dwg") -> dict: ...

    @abstractmethod
    async def drawing_export_dxf(self, path: str) -> dict: ...

    @abstractmethod
    async def drawing_export_pdf(self, path: str, layout: str | None = None) -> dict: ...

    @abstractmethod
    async def drawing_purge(self) -> dict: ...

    # ── layouts / paper space ────────────────────────────────────────────────
    @abstractmethod
    async def layout_list(self) -> dict:
        """Return {ok, layouts: [names], current: name} incl. 'Model'."""
        ...

    @abstractmethod
    async def layout_create(self, name: str) -> dict:
        """Create a new paper-space layout."""
        ...

    @abstractmethod
    async def layout_set_current(self, name: str) -> dict:
        """Activate a layout tab ('Model' or a paper-space layout name)."""
        ...

    @abstractmethod
    async def viewport_create(
        self,
        layout: str,
        center_x: float,
        center_y: float,
        width: float,
        height: float,
        view_center_x: float,
        view_center_y: float,
        scale: float = 1.0,
    ) -> dict:
        """Place a scaled model-space viewport on a paper-space layout.

        ``scale`` is paper:model (1.0 = 1:1, 0.5 = 1:2). The viewport shows
        the model window centered at (view_center_x, view_center_y) with
        view height = height / scale.
        """
        ...

    # ── 3D solids (opt-in; native on live AutoCAD only) ─────────────────────
    @abstractmethod
    async def solid_box(
        self, cx: float, cy: float, cz: float, length: float, width: float, height: float
    ) -> dict:
        """Create a 3D solid box centered at (cx, cy, cz)."""
        ...

    @abstractmethod
    async def solid_cylinder(
        self, cx: float, cy: float, cz: float, radius: float, height: float
    ) -> dict:
        """Create a 3D solid cylinder centered at (cx, cy, cz)."""
        ...

    @abstractmethod
    async def solid_extrude(
        self, profile_handle: str, height: float, taper_angle: float = 0.0
    ) -> dict:
        """Extrude a closed profile (circle/closed polyline) into a solid."""
        ...

    @abstractmethod
    async def solid_revolve(
        self,
        profile_handle: str,
        axis_x1: float,
        axis_y1: float,
        axis_x2: float,
        axis_y2: float,
        angle: float = 360.0,
    ) -> dict:
        """Revolve a closed profile around an axis into a solid."""
        ...

    @abstractmethod
    async def solid_boolean(self, target_handle: str, tool_handle: str, operation: str) -> dict:
        """Boolean two solids: operation is union | subtract | intersect."""
        ...

    @abstractmethod
    async def drawing_audit(self) -> dict: ...

    @abstractmethod
    async def drawing_close(self, save: bool = True) -> dict: ...

    @abstractmethod
    async def drawing_undo(self) -> dict: ...

    @abstractmethod
    async def drawing_redo(self) -> dict: ...

    # ── entity creation ──────────────────────────────────────────────────────
    @abstractmethod
    async def entity_create_line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        z1: float = 0.0,
        z2: float = 0.0,
        layer: str | None = None,
        color: int | None = None,
        linetype: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_circle(
        self,
        cx: float,
        cy: float,
        radius: float,
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_arc(
        self,
        cx: float,
        cy: float,
        radius: float,
        start_angle: float,
        end_angle: float,
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_polyline(
        self,
        points: list[list[float]],
        closed: bool = False,
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_text(
        self,
        text: str,
        x: float,
        y: float,
        height: float = 2.5,
        rotation: float = 0.0,
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_mtext(
        self,
        text: str,
        x: float,
        y: float,
        width: float = 100.0,
        height: float = 2.5,
        rotation: float = 0.0,
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_table(
        self,
        x: float,
        y: float,
        rows: list[list[str]],
        headers: list[str] | None = None,
        column_widths: list[float] | None = None,
        row_height: float = 7.0,
        text_height: float = 2.5,
        title: str | None = None,
        layer: str = "TEXT",
    ) -> EntityInfo: ...

    @abstractmethod
    async def leader_create_mleader(
        self,
        points: list[list[float]],
        text: str,
        text_height: float = 2.5,
        landing_gap: float = 1.0,
        arrow_size: float = 2.5,
        layer: str = "DIM",
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_hatch(
        self,
        pattern: str,
        boundary_points: list[list[float]],
        scale: float = 1.0,
        angle: float = 0.0,
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_spline(
        self,
        fit_points: list[list[float]],
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_ellipse(
        self,
        cx: float,
        cy: float,
        major_x: float,
        major_y: float,
        ratio: float = 0.5,
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_point(
        self,
        x: float,
        y: float,
        layer: str | None = None,
        color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_block_ref(
        self,
        name: str,
        x: float,
        y: float,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        rotation: float = 0.0,
        layer: str | None = None,
    ) -> EntityInfo: ...

    # ── dimensions ───────────────────────────────────────────────────────────
    @abstractmethod
    async def dimension_linear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        dim_x: float,
        dim_y: float,
        rotation: float = 0.0,
        layer: str | None = None,
        tol_upper: float | None = None,
        tol_lower: float | None = None,
        tol_mode: str = "none",
        text_override: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_aligned(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        dim_x: float,
        dim_y: float,
        layer: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_angular(
        self,
        vx: float,
        vy: float,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        tx: float,
        ty: float,
        layer: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_radius(
        self,
        cx: float,
        cy: float,
        chord_x: float,
        chord_y: float,
        leader_length: float = 10.0,
        layer: str | None = None,
        tol_upper: float | None = None,
        tol_lower: float | None = None,
        tol_mode: str = "none",
        text_override: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_diameter(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        leader_length: float = 10.0,
        layer: str | None = None,
        tol_upper: float | None = None,
        tol_lower: float | None = None,
        tol_mode: str = "none",
        text_override: str | None = None,
    ) -> EntityInfo: ...

    # ── entity modification ──────────────────────────────────────────────────
    @abstractmethod
    async def entity_move(self, handle: str, dx: float, dy: float, dz: float = 0.0) -> dict: ...

    @abstractmethod
    async def entity_copy(
        self, handle: str, dx: float, dy: float, dz: float = 0.0
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_rotate(
        self,
        handle: str,
        base_x: float,
        base_y: float,
        angle_deg: float,
    ) -> dict: ...

    @abstractmethod
    async def entity_scale(
        self,
        handle: str,
        base_x: float,
        base_y: float,
        factor: float,
    ) -> dict: ...

    @abstractmethod
    async def entity_mirror(
        self,
        handle: str,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        delete_original: bool = False,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_offset(
        self,
        handle: str,
        distance: float,
        side_x: float | None = None,
        side_y: float | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_delete(self, handle: str) -> dict: ...

    @abstractmethod
    async def entity_array_rectangular(
        self,
        handle: str,
        rows: int,
        cols: int,
        row_spacing: float,
        col_spacing: float,
    ) -> list[EntityInfo]: ...

    @abstractmethod
    async def entity_array_polar(
        self,
        handle: str,
        count: int,
        fill_angle: float,
        center_x: float,
        center_y: float,
    ) -> list[EntityInfo]: ...

    # ── corner operations (trim/extend/fillet/chamfer) ───────────────────────
    @abstractmethod
    async def entity_trim(
        self,
        target_handle: str,
        cutter_handle: str,
        keep_x: float,
        keep_y: float,
    ) -> EntityInfo:
        """Trim `target` against `cutter`; keep the segment containing
        (keep_x, keep_y). Raises ToolError if no intersection."""
        ...

    @abstractmethod
    async def entity_extend(
        self,
        target_handle: str,
        boundary_handle: str,
        end_x: float | None = None,
        end_y: float | None = None,
    ) -> EntityInfo:
        """Extend `target` to meet `boundary`. If `end_x/y` is None, the
        target endpoint nearest the boundary is auto-selected."""
        ...

    @abstractmethod
    async def entity_fillet(
        self,
        handle1: str,
        handle2: str,
        radius: float,
        trim: bool = True,
    ) -> EntityInfo:
        """Fillet two entities with the given radius. Returns the new ARC.
        When `trim` is True (default), the source entities are shortened
        to the tangent points (AutoCAD default behaviour)."""
        ...

    @abstractmethod
    async def entity_chamfer(
        self,
        handle1: str,
        handle2: str,
        dist1: float,
        dist2: float | None = None,
        trim: bool = True,
    ) -> EntityInfo:
        """Chamfer two entities. When `dist2` is None it defaults to `dist1`
        (symmetric chamfer). Returns the new chamfer LINE."""
        ...

    # ── entity query/properties ───────────────────────────────────────────────
    @abstractmethod
    async def entity_get(self, handle: str) -> EntityInfo: ...

    @abstractmethod
    async def entity_set_properties(
        self,
        handle: str,
        layer: str | None = None,
        color: int | None = None,
        linetype: str | None = None,
        lineweight: float | None = None,
        visible: bool | None = None,
    ) -> dict: ...

    @abstractmethod
    async def entity_edit_text(
        self,
        handle: str,
        text: str | None = None,
        height: float | None = None,
        rotation: float | None = None,
    ) -> EntityInfo:
        """Edit an existing TEXT or MTEXT entity in place. Any argument left
        None is unchanged. Returns the updated EntityInfo. Raises if the handle
        is not a TEXT/MTEXT entity."""
        ...

    @abstractmethod
    async def entity_edit_geometry(
        self,
        handle: str,
        cx: float | None = None,
        cy: float | None = None,
        radius: float | None = None,
        x1: float | None = None,
        y1: float | None = None,
        x2: float | None = None,
        y2: float | None = None,
        start_angle: float | None = None,
        end_angle: float | None = None,
    ) -> EntityInfo:
        """Edit the defining geometry of an existing entity in place.
        - CIRCLE: cx / cy / radius
        - LINE: x1 / y1 (start), x2 / y2 (end)
        - ARC: cx / cy / radius / start_angle / end_angle (degrees)
        Any argument left None is unchanged. Raises for unsupported types."""
        ...

    @abstractmethod
    async def entity_list(
        self,
        type_filter: str | None = None,
        layer_filter: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[EntityInfo]: ...

    # ── layer management ─────────────────────────────────────────────────────
    @abstractmethod
    async def layer_list(self) -> list[LayerInfo]: ...

    @abstractmethod
    async def layer_create(
        self,
        name: str,
        color: int = 7,
        linetype: str = "Continuous",
        lineweight: float = -3,
    ) -> LayerInfo: ...

    @abstractmethod
    async def layer_delete(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_set_current(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_modify(
        self,
        name: str,
        color: int | None = None,
        linetype: str | None = None,
        lineweight: float | None = None,
    ) -> LayerInfo: ...

    @abstractmethod
    async def layer_freeze(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_thaw(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_lock(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_unlock(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_hide(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_show(self, name: str) -> dict: ...

    # ── linetype management ──────────────────────────────────────────────────
    @abstractmethod
    async def linetype_list(self) -> list[str]: ...

    @abstractmethod
    async def linetype_load(
        self,
        name: str,
        file: str | None = None,
    ) -> dict: ...

    # ── block operations ─────────────────────────────────────────────────────
    @abstractmethod
    async def block_list(self) -> list[BlockInfo]: ...

    @abstractmethod
    async def block_insert(
        self,
        name: str,
        x: float,
        y: float,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        rotation: float = 0.0,
        attributes: dict | None = None,
        layer: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def block_explode(self, handle: str) -> dict: ...

    @abstractmethod
    async def block_get_attributes(self, handle: str) -> dict: ...

    @abstractmethod
    async def block_set_attributes(self, handle: str, attributes: dict) -> dict: ...

    @abstractmethod
    async def block_create_from_entities(
        self,
        name: str,
        handles: list[str],
        base_x: float = 0.0,
        base_y: float = 0.0,
    ) -> dict: ...

    # ── analysis / query ─────────────────────────────────────────────────────
    @abstractmethod
    async def analysis_stats(self) -> dict: ...

    @abstractmethod
    async def analysis_entities_in_region(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> list[EntityInfo]: ...

    @abstractmethod
    async def analysis_measure_distance(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> float: ...

    @abstractmethod
    async def analysis_measure_area(self, points: list[list[float]]) -> float: ...

    @abstractmethod
    async def analysis_bounding_box(self) -> dict: ...

    @abstractmethod
    async def analysis_select_by_layer(self, layer_name: str) -> list[EntityInfo]: ...

    @abstractmethod
    async def analysis_select_by_type(self, entity_type: str) -> list[EntityInfo]: ...

    @abstractmethod
    async def selection_get(self) -> dict:
        """Read the live viewport's implied ("pickfirst") selection set.

        COM-only / meaningful only with live AutoCAD. Returns a dict::

            {
                "ok": bool,                # True on COM even for an empty pick
                "count": int,
                "handles": list[str],      # entity handles to act on
                "entities": list[EntityInfo],  # _dc()-converted by the server
                "pickfirst": bool | None,  # state of the PICKFIRST sysvar
                "message" / "error": str,  # optional guidance / failure reason
            }

        The ezdxf headless backend has no viewport, so it returns ``ok=False``
        with an empty ``handles`` list (same shape, never raises).
        """
        ...

    # ── view / screenshot ────────────────────────────────────────────────────
    @abstractmethod
    async def view_zoom_extents(self) -> dict: ...

    @abstractmethod
    async def view_zoom_window(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> dict: ...

    @abstractmethod
    async def view_screenshot(self) -> bytes | None: ...

    # ── transactions ─────────────────────────────────────────────────────────
    @abstractmethod
    async def transaction_begin(self) -> dict: ...

    @abstractmethod
    async def transaction_commit(self) -> dict: ...

    @abstractmethod
    async def transaction_rollback(self) -> dict: ...

    # ── system ───────────────────────────────────────────────────────────────
    @abstractmethod
    async def system_status(self) -> dict: ...

    @abstractmethod
    async def system_get_variable(self, name: str) -> Any: ...

    @abstractmethod
    async def system_set_variable(self, name: str, value: Any) -> dict: ...

    @abstractmethod
    async def system_run_command(self, command: str) -> dict: ...

    @abstractmethod
    async def system_run_lisp(self, expression: str) -> dict: ...

    # ── premium meta-tools (planning + critique + snap + construction) ──────
    # These are CONCRETE and backend-agnostic: they orchestrate the abstract
    # primitives above plus the shared EntityInfo.properties contract (start/
    # end/center/radius/start_angle/end_angle — populated identically by both
    # backends), so the ezdxf and COM backends inherit identical premium
    # behaviour. The only backend-specific primitive is `_create_xline` — XLINE
    # has no cross-backend equivalent among the standard entity_create_* tools.

    _plan_spec: PlanSpec | None = None
    _preflight_result: Any = None

    def _reset_document_state(self) -> None:
        """Clear metadata that is valid only for the current drawing."""
        self._plan_spec = None
        self._preflight_result = None
        self._gdt_datums_defined = set()
        self._gdt_datums_referenced = set()
        # handle of a composite's first child -> handles of every child
        self._composites: dict[str, list[str]] = {}

    def _register_composite(self, handles: list[str]) -> None:
        """Remember a multi-entity composite so single-handle ops can cascade."""
        if handles:
            self._composites[str(handles[0])] = [str(h) for h in handles]

    def _composite_handles(self, handle) -> list[str]:
        """Every handle belonging to `handle`'s composite (itself if standalone)."""
        return list(getattr(self, "_composites", {}).get(str(handle), [str(handle)]))

    async def _ensure_document_state(self) -> None:
        """Allow live backends to detect an out-of-band document switch."""
        return None

    async def drawing_preflight(
        self,
        intent: str,
        requirements: dict | None = None,
        sheet_size: SheetSize = "A3",
        scale: float = 1.0,
        layer_set_id: LayerSetId = "mech",
        view_count: int = 1,
        dim_style: DimStyle = "chain",
        allow_assumptions: bool = False,
    ):
        from engineering.preflight import preflight_drawing

        await self._ensure_document_state()

        result = preflight_drawing(
            intent,
            requirements,
            sheet_size,
            scale,
            layer_set_id,
            view_count,
            dim_style,
            allow_assumptions=allow_assumptions,
        )
        self._preflight_result = result
        return result

    async def drawing_plan(
        self,
        intent: str,
        sheet_size: SheetSize = "A3",
        scale: float = 1.0,
        layer_set_id: LayerSetId = "mech",
        view_count: int = 1,
        dim_style: DimStyle = "chain",
        notes: list[str] | None = None,
        requirements: dict | None = None,
        spec_hash: str | None = None,
    ) -> PlanSpec:
        """Commit a drawing intent before any geometry is created.
        Returns a PlanSpec and stores it on the backend; read it back with
        `get_plan_spec()`. (N8: the previous claim that `drawing_critique`
        replays this plan was untrue — critique runs geometric checks, not a
        plan diff.)"""
        from engineering.plan_spec import PlanSpec

        await self._ensure_document_state()
        preflight_status = "skipped"
        normalized_plan: dict[str, Any] | None = None
        if spec_hash is not None:
            from engineering.preflight import preflight_drawing

            preflight = getattr(self, "_preflight_result", None)
            if preflight is None or preflight.spec_hash != spec_hash:
                raise RuntimeError("drawing_plan: spec_hash does not match the latest preflight.")
            if not preflight.ready:
                raise RuntimeError(
                    "drawing_plan: preflight is not ready; resolve questions/conflicts first."
                )
            candidate = preflight_drawing(
                intent,
                requirements
                if requirements is not None
                else preflight.normalized_spec["requirements"],
                sheet_size,
                scale,
                layer_set_id,
                view_count,
                dim_style,
            )
            if candidate.spec_hash != spec_hash:
                raise RuntimeError(
                    "drawing_plan: supplied fields do not match the hashed preflight spec."
                )
            normalized_plan = preflight.normalized_spec
            preflight_status = "ready"

        if normalized_plan is not None:
            intent = normalized_plan["intent"]
            sheet_size = normalized_plan["sheet_size"]
            scale = normalized_plan["scale"]
            layer_set_id = normalized_plan["layer_set_id"]
            view_count = normalized_plan["view_count"]
            dim_style = normalized_plan["dim_style"]
            requirements = normalized_plan["requirements"]

        plan = PlanSpec(
            intent=str(intent),
            sheet_size=sheet_size,
            scale=float(scale),
            layer_set_id=layer_set_id,
            view_count=int(view_count),
            dim_style=dim_style,
            notes=list(notes) if notes else [],
            requirements=dict(requirements or {}),
            spec_hash=spec_hash,
            preflight_status=preflight_status,
        )
        self._plan_spec = plan
        return plan

    async def drawing_critique(
        self,
        focus: list[CritiqueFocus] | None = None,
    ) -> list[Issue]:
        """Run premium-quality geometric checks; return zero issues before
        `drawing_finalize`. `focus=None` runs all checks. (N8: this does NOT
        replay the committed PlanSpec — it inspects the live drawing geometry.)"""
        from engineering.critique import run_critique

        await self._ensure_document_state()
        return await run_critique(self, focus)

    def get_plan_spec(self) -> dict | None:
        """Return the PlanSpec committed by `drawing_plan` as a dict, or None
        if no plan has been committed. (N8: makes the stored `_plan_spec`
        actually readable instead of being write-only dead state.)"""
        plan = getattr(self, "_plan_spec", None)
        return plan.to_dict() if plan is not None else None

    async def point_from_snap(
        self,
        handle: str,
        snap: SnapType,
        ref_x: float | None = None,
        ref_y: float | None = None,
    ) -> tuple[float, float]:
        """Return a deterministic snap point on `handle`.
        snap ∈ {end, mid, center, quad, perp, near}. For `near/perp`,
        `ref_x/y` is the reference point. Sources geometry from the shared
        EntityInfo.properties contract — works on every backend."""
        ent = await self.entity_get(handle)
        et = ent.type
        p = ent.properties
        ref = (float(ref_x), float(ref_y)) if ref_x is not None and ref_y is not None else None

        def _line_pts():
            s = p.get("start")
            e = p.get("end")
            if s is None or e is None:
                raise RuntimeError(f"point_from_snap: entity {handle} ({et}) has no start/end.")
            return (float(s[0]), float(s[1])), (float(e[0]), float(e[1]))

        def _circle_geom():
            c = p.get("center")
            r = p.get("radius")
            if c is None or r is None:
                raise RuntimeError(f"point_from_snap: entity {handle} ({et}) has no center/radius.")
            return float(c[0]), float(c[1]), float(r)

        def _arc_angles():
            # N7: use .get() + descriptive RuntimeError (matching _line_pts /
            # _circle_geom) instead of bracket indexing that raises an opaque
            # KeyError when start_angle/end_angle are missing.
            sa = p.get("start_angle")
            ea = p.get("end_angle")
            if sa is None or ea is None:
                raise RuntimeError(
                    f"point_from_snap: entity {handle} ({et}) has no start_angle/end_angle."
                )
            return math.radians(float(sa)), math.radians(float(ea))

        if snap == "end":
            if et == "LINE":
                s, e = _line_pts()
                if ref is None:
                    return s
                ds = (s[0] - ref[0]) ** 2 + (s[1] - ref[1]) ** 2
                de = (e[0] - ref[0]) ** 2 + (e[1] - ref[1]) ** 2
                return s if ds <= de else e
            if et == "ARC":
                cx, cy, r = _circle_geom()
                sa, ea = _arc_angles()
                sp = (cx + r * math.cos(sa), cy + r * math.sin(sa))
                ep = (cx + r * math.cos(ea), cy + r * math.sin(ea))
                if ref is None:
                    return sp
                ds = (sp[0] - ref[0]) ** 2 + (sp[1] - ref[1]) ** 2
                de = (ep[0] - ref[0]) ** 2 + (ep[1] - ref[1]) ** 2
                return sp if ds <= de else ep
            raise RuntimeError(f"point_from_snap: 'end' snap not supported on {et}")

        if snap == "mid":
            if et == "LINE":
                s, e = _line_pts()
                return ((s[0] + e[0]) / 2.0, (s[1] + e[1]) / 2.0)
            if et == "ARC":
                cx, cy, r = _circle_geom()
                sa, ea = _arc_angles()
                if ea < sa:
                    ea += 2 * math.pi
                ma = (sa + ea) / 2.0
                return (cx + r * math.cos(ma), cy + r * math.sin(ma))
            raise RuntimeError(f"point_from_snap: 'mid' snap not supported on {et}")

        if snap == "center":
            if et in ("CIRCLE", "ARC", "ELLIPSE"):
                c = p.get("center")
                if c is None:
                    raise RuntimeError(f"point_from_snap: entity {handle} ({et}) has no center.")
                return (float(c[0]), float(c[1]))
            raise RuntimeError(f"point_from_snap: 'center' snap not supported on {et}")

        # NEW-snap-quad-arc / NEW-snap-near-arc: an ARC only covers part of the
        # full circle, so quad/near candidates derived from the circle may fall
        # outside the [start_angle, end_angle] sweep. Restrict (quad) / clamp
        # (near) the projected angle to the sweep, with 2π wraparound.
        def _arc_sweep():
            sa, ea = _arc_angles()
            if ea < sa:
                ea += 2 * math.pi
            return sa, ea

        def _angle_in_sweep(theta: float, sa: float, ea: float) -> bool:
            # Normalise theta into [sa, sa+2π) and test against [sa, ea].
            t = sa + ((theta - sa) % (2 * math.pi))
            return t <= ea + 1e-9

        def _arc_endpoints(cx: float, cy: float, r: float, sa: float, ea: float):
            return (
                (cx + r * math.cos(sa), cy + r * math.sin(sa)),
                (cx + r * math.cos(ea), cy + r * math.sin(ea)),
            )

        if snap == "quad":
            if et == "CIRCLE":
                cx, cy, r = _circle_geom()
                pts = [(cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r)]
                if ref is None:
                    return pts[0]
                return min(pts, key=lambda q: (q[0] - ref[0]) ** 2 + (q[1] - ref[1]) ** 2)
            if et == "ARC":
                cx, cy, r = _circle_geom()
                sa, ea = _arc_sweep()
                quad_angles = (0.0, math.pi / 2.0, math.pi, 3 * math.pi / 2.0)
                pts = [
                    (cx + r * math.cos(a), cy + r * math.sin(a))
                    for a in quad_angles
                    if _angle_in_sweep(a, sa, ea)
                ]
                if not pts:
                    # No quadrant lies on the sweep — fall back to arc endpoints.
                    pts = list(_arc_endpoints(cx, cy, r, sa, ea))
                if ref is None:
                    return pts[0]
                return min(pts, key=lambda q: (q[0] - ref[0]) ** 2 + (q[1] - ref[1]) ** 2)
            raise RuntimeError(f"point_from_snap: 'quad' snap not supported on {et}")

        if snap == "perp":
            if ref is None:
                raise RuntimeError("point_from_snap: 'perp' requires ref_x/ref_y.")
            if et == "LINE":
                s, e = _line_pts()
                dx = e[0] - s[0]
                dy = e[1] - s[1]
                L2 = dx * dx + dy * dy
                if L2 < 1e-18:
                    raise RuntimeError("point_from_snap: line has zero length.")
                t = ((ref[0] - s[0]) * dx + (ref[1] - s[1]) * dy) / L2
                return (s[0] + t * dx, s[1] + t * dy)
            raise RuntimeError(f"point_from_snap: 'perp' snap not supported on {et}")

        if snap == "near":
            if ref is None:
                raise RuntimeError("point_from_snap: 'near' requires ref_x/ref_y.")
            if et == "LINE":
                s, e = _line_pts()
                dx = e[0] - s[0]
                dy = e[1] - s[1]
                L2 = dx * dx + dy * dy
                if L2 < 1e-18:
                    return s
                t = ((ref[0] - s[0]) * dx + (ref[1] - s[1]) * dy) / L2
                t = max(0.0, min(1.0, t))
                return (s[0] + t * dx, s[1] + t * dy)
            if et == "CIRCLE":
                cx, cy, r = _circle_geom()
                vx, vy = ref[0] - cx, ref[1] - cy
                L = math.hypot(vx, vy)
                if L < 1e-18:
                    return (cx + r, cy)
                return (cx + r * vx / L, cy + r * vy / L)
            if et == "ARC":
                cx, cy, r = _circle_geom()
                sa, ea = _arc_sweep()
                vx, vy = ref[0] - cx, ref[1] - cy
                L = math.hypot(vx, vy)
                if L < 1e-18:
                    # Degenerate ref at center: return the start endpoint.
                    return (cx + r * math.cos(sa), cy + r * math.sin(sa))
                theta = math.atan2(vy, vx)
                if _angle_in_sweep(theta, sa, ea):
                    return (cx + r * math.cos(theta), cy + r * math.sin(theta))
                # Projected angle is off the sweep — fall back to the nearer endpoint.
                sp, ep = _arc_endpoints(cx, cy, r, sa, ea)
                ds = (sp[0] - ref[0]) ** 2 + (sp[1] - ref[1]) ** 2
                de = (ep[0] - ref[0]) ** 2 + (ep[1] - ref[1]) ** 2
                return sp if ds <= de else ep
            raise RuntimeError(f"point_from_snap: 'near' snap not supported on {et}")

        if snap == "int":
            raise RuntimeError(
                "point_from_snap: 'int' (intersection) requires a 2nd entity; "
                "compute via two endpoints. V2 will accept a 2nd handle."
            )

        raise RuntimeError(
            f"point_from_snap: unknown snap type '{snap}'. "
            "Use one of: end, mid, center, quad, perp, near."
        )

    async def point_intersection(
        self,
        handle1: str,
        handle2: str,
        ref_x: float | None = None,
        ref_y: float | None = None,
    ) -> tuple[float, float]:
        """Return the intersection point of two geometry entities.

        Supports LINE-LINE, LINE-CIRCLE, and CIRCLE-CIRCLE cases. When
        multiple intersections exist (e.g. two circles), ``ref_x``/``ref_y``
        selects the candidate nearest to that reference point. If omitted the
        first candidate is returned.

        Raises RuntimeError when no real intersection exists or entity types
        are unsupported.
        """
        info1 = await self.entity_get(handle1)
        info2 = await self.entity_get(handle2)
        t1, t2 = info1.type.upper(), info2.type.upper()
        props1, props2 = info1.properties, info2.properties

        candidates: list[tuple[float, float]] = []

        if t1 == "LINE" and t2 == "LINE":
            # Line-line intersection via parametric form
            x1, y1 = props1["start"][0], props1["start"][1]
            x2, y2 = props1["end"][0], props1["end"][1]
            x3, y3 = props2["start"][0], props2["start"][1]
            x4, y4 = props2["end"][0], props2["end"][1]
            dx1, dy1 = x2 - x1, y2 - y1
            dx2, dy2 = x4 - x3, y4 - y3
            denom = dx1 * dy2 - dy1 * dx2
            if abs(denom) < 1e-12:
                raise RuntimeError("point_intersection: lines are parallel or coincident.")
            t = ((x3 - x1) * dy2 - (y3 - y1) * dx2) / denom
            candidates = [(x1 + t * dx1, y1 + t * dy1)]

        elif {t1, t2} == {"LINE", "CIRCLE"}:
            line_p = props1 if t1 == "LINE" else props2
            circ_p = props2 if t1 == "LINE" else props1
            lx1, ly1 = line_p["start"][0], line_p["start"][1]
            lx2, ly2 = line_p["end"][0], line_p["end"][1]
            cx, cy = circ_p["center"][0], circ_p["center"][1]
            r = circ_p["radius"]
            dx, dy = lx2 - lx1, ly2 - ly1
            fx, fy = lx1 - cx, ly1 - cy
            a = dx * dx + dy * dy
            b = 2.0 * (fx * dx + fy * dy)
            c = fx * fx + fy * fy - r * r
            disc = b * b - 4.0 * a * c
            if disc < 0:
                raise RuntimeError("point_intersection: line does not intersect circle.")
            sq = math.sqrt(disc)
            for sign in (1, -1):
                t = (-b + sign * sq) / (2.0 * a)
                candidates.append((lx1 + t * dx, ly1 + t * dy))

        elif t1 == "CIRCLE" and t2 == "CIRCLE":
            cx1, cy1, r1 = props1["center"][0], props1["center"][1], props1["radius"]
            cx2, cy2, r2 = props2["center"][0], props2["center"][1], props2["radius"]
            dx, dy = cx2 - cx1, cy2 - cy1
            d = math.hypot(dx, dy)
            if d < 1e-12:
                raise RuntimeError("point_intersection: circles are concentric.")
            if d > r1 + r2 + 1e-9 or d < abs(r1 - r2) - 1e-9:
                raise RuntimeError("point_intersection: circles do not intersect.")
            a = (r1 * r1 - r2 * r2 + d * d) / (2.0 * d)
            h_sq = r1 * r1 - a * a
            h = math.sqrt(max(h_sq, 0.0))
            mx = cx1 + a * dx / d
            my = cy1 + a * dy / d
            candidates = [
                (mx + h * dy / d, my - h * dx / d),
                (mx - h * dy / d, my + h * dx / d),
            ]

        else:
            raise RuntimeError(
                f"point_intersection: unsupported entity combination {t1}+{t2}. "
                "Supported: LINE-LINE, LINE-CIRCLE, CIRCLE-CIRCLE."
            )

        if len(candidates) == 1:
            return candidates[0]
        if ref_x is not None and ref_y is not None:
            return min(
                candidates,
                key=lambda p: (p[0] - float(ref_x)) ** 2 + (p[1] - float(ref_y)) ** 2,
            )
        return candidates[0]

    async def point_tangent(
        self,
        circle_handle: str,
        from_x: float,
        from_y: float,
        ref_x: float | None = None,
        ref_y: float | None = None,
    ) -> tuple[float, float]:
        """Return the tangent point on a circle from an external point.

        When two tangent points exist, ``ref_x``/``ref_y`` selects the
        nearer one; without it, the first is returned (counter-clockwise from
        the from-point—circle-center line).

        Raises RuntimeError if the point is inside the circle.
        """
        info = await self.entity_get(circle_handle)
        if info.type.upper() != "CIRCLE":
            raise RuntimeError(
                f"point_tangent: handle {circle_handle!r} is {info.type}, expected CIRCLE."
            )
        cx, cy = info.properties["center"][0], info.properties["center"][1]
        r = info.properties["radius"]
        fx, fy = float(from_x), float(from_y)
        dx, dy = cx - fx, cy - fy
        d = math.hypot(dx, dy)
        if d < r - 1e-9:
            raise RuntimeError(
                f"point_tangent: from-point ({fx}, {fy}) is inside the circle "
                f"(center ({cx}, {cy}), radius {r})."
            )
        if d < 1e-12:
            raise RuntimeError("point_tangent: from-point coincides with circle center.")
        # Tangent point T lies on the circle such that (T-F)⊥(T-C).
        # Solving cos(θ - α) = -r/d where α = atan2(C-F) gives the two
        # circle angles θ at which the tangent touches the perimeter.
        alpha = math.atan2(dy, dx)  # direction from F to C
        # NEW-base-1: clamp the acos argument to [-1, 1] so a from-point in the
        # narrow band [r-1e-9, r) (past the internal-point guard but with
        # |-r/d| marginally > 1 from float error) degenerates gracefully to the
        # tangent-at-perimeter case instead of raising a raw ValueError
        # ("math domain error"). Genuine internal points (d < r) are already
        # rejected by the RuntimeError guard above.
        acos_neg = math.acos(max(-1.0, min(1.0, -r / d)))  # (π/2, π] for external pt
        candidates: list[tuple[float, float]] = [
            (cx + r * math.cos(alpha + acos_neg), cy + r * math.sin(alpha + acos_neg)),
            (cx + r * math.cos(alpha - acos_neg), cy + r * math.sin(alpha - acos_neg)),
        ]
        if ref_x is not None and ref_y is not None:
            return min(
                candidates,
                key=lambda p: (p[0] - float(ref_x)) ** 2 + (p[1] - float(ref_y)) ** 2,
            )
        return candidates[0]

    @abstractmethod
    async def _create_xline(
        self,
        x: float,
        y: float,
        dx: float,
        dy: float,
        layer: str,
    ) -> EntityInfo:
        """Create an infinite construction line (XLINE) through (x, y) with
        unit direction (dx, dy) on `layer`. Backend-specific: XLINE has no
        cross-backend entity_create_* equivalent."""
        ...

    async def _ensure_layer(self, name: str, color: int = 7) -> None:
        """Idempotently make sure `name` exists (no-op if already present)."""
        try:
            existing = {lyr.name for lyr in await self.layer_list()}
        except Exception:
            existing = set()
        if name not in existing:
            try:
                await self.layer_create(name, color=color)
            except Exception:
                pass  # already created / racy create — harmless

    def _active_layer_set_id(self) -> str:
        """Best-effort active layer set: an explicit `drawing_apply_iso_layers`
        wins, else the committed PlanSpec, else 'mech' (the default bootstrap)."""
        explicit = getattr(self, "_active_layer_set", None)
        if explicit:
            return explicit
        plan = getattr(self, "_plan_spec", None)
        if plan is not None:
            return getattr(plan, "layer_set_id", "mech")
        return "mech"

    def _role_layer(self, role: str) -> str:
        """Resolve the layer name for a role ('dim' | 'construction') in the
        active layer set, so dims/scaffolding land on the right layer for
        iso13567 (M-DIMEN-T-N / M-CONST-E-N) not just mech/pid (DIM/CONSTRUCTION)."""
        from engineering.layers import resolve_role_layer

        return resolve_role_layer(self._active_layer_set_id(), role)

    async def construction_xline(
        self,
        x: float,
        y: float,
        angle_deg: float,
        layer: str | None = None,
    ) -> EntityInfo:
        """Create an infinite construction line (XLINE) on the active layer set's
        construction layer (auto-created with color 250 = lightest if missing)."""
        layer = layer or self._role_layer("construction")
        await self._ensure_layer(layer, color=250)
        ang = math.radians(float(angle_deg))
        return await self._create_xline(
            float(x),
            float(y),
            math.cos(ang),
            math.sin(ang),
            layer,
        )

    async def construction_clear(
        self,
        layer: str | None = None,
    ) -> dict:
        """Delete every entity on the active layer set's construction layer.
        Must be called before `drawing_finalize`."""
        layer = layer or self._role_layer("construction")
        try:
            ents = await self.entity_list(layer_filter=layer, limit=5000)
        except Exception:
            ents = []
        deleted = 0
        for ent in ents:
            try:
                await self.entity_delete(ent.handle)
                deleted += 1
            except Exception:
                continue
        return {"ok": True, "layer": layer, "deleted": deleted}

    async def drawing_apply_iso_layers(
        self,
        standard: LayerSetId = "mech",
    ) -> dict:
        """Bootstrap a full ISO-conformant layer set with correct colors
        and lineweights. Idempotent."""
        from engineering.layers import apply_layer_set

        result = await apply_layer_set(self, standard)
        self._active_layer_set = standard
        return {"ok": True, "standard": standard, "layers": result}

    async def dimension_auto(
        self,
        handles: list[str],
        style: DimStyle = "chain",
        offset: float = 10.0,
        layer: str | None = None,
    ) -> list[EntityInfo]:
        """Generate ISO 129 dimensions across `handles` in the chosen style
        (chain | baseline | ordinate). `offset` is the dimension-line
        distance from the reference geometry (mm). `layer` defaults to the
        active layer set's dimension layer (DIM, or M-DIMEN-T-N for iso13567)."""
        if not handles:
            return []
        dim_layer = layer or self._role_layer("dim")
        if style not in ("chain", "baseline", "ordinate"):
            raise RuntimeError(
                f"dimension_auto: unknown style '{style}'. Use 'chain', 'baseline', or 'ordinate'."
            )
        segs: list[tuple[float, float, float, float]] = []
        for h in handles:
            ent = await self.entity_get(h)
            if ent.type != "LINE":
                raise RuntimeError(
                    f"dimension_auto V1: only LINE entities supported (handle {h} is {ent.type})."
                )
            s = ent.properties.get("start")
            e = ent.properties.get("end")
            if s is None or e is None:
                raise RuntimeError(f"dimension_auto: line {h} missing start/end.")
            segs.append((float(s[0]), float(s[1]), float(e[0]), float(e[1])))

        off = float(offset)
        results: list[EntityInfo] = []

        if style == "chain":
            # Each line gets its own linear (rotated) dimension, offset clear of
            # the geometry along its own perpendicular.
            for sx, sy, ex, ey in segs:
                mx = (sx + ex) / 2.0
                my = (sy + ey) / 2.0
                dx, dy = ex - sx, ey - sy
                L = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / L, dx / L
                dim = await self.dimension_linear(
                    sx,
                    sy,
                    ex,
                    ey,
                    mx + nx * off,
                    my + ny * off,
                    rotation=math.degrees(math.atan2(dy, dx)),
                    layer=dim_layer,
                )
                results.append(dim)

        elif style == "baseline":
            sx0, sy0, ex0, ey0 = segs[0]
            dx, dy = ex0 - sx0, ey0 - sy0
            L0 = math.hypot(dx, dy) or 1.0
            ux, uy = dx / L0, dy / L0
            base_rot = math.degrees(math.atan2(dy, dx))
            for i, (_sx, _sy, ex, ey) in enumerate(segs):
                dim = await self.dimension_linear(
                    sx0,
                    sy0,
                    ex,
                    ey,
                    sx0 - uy * (off + i * off),
                    sy0 + ux * (off + i * off),
                    # Dimension line must run parallel to the (possibly rotated)
                    # baseline, not horizontal.
                    rotation=base_rot,
                    layer=dim_layer,
                )
                results.append(dim)

        else:  # ordinate — stacked X/Y distances; each feature offset so dims don't overlap
            for i, (sx, sy, ex, ey) in enumerate(segs):
                rung = off + i * off  # stagger per feature to avoid coincident dims
                if abs(ex - sx) > 1e-9:
                    dim = await self.dimension_linear(
                        sx,
                        sy,
                        ex,
                        sy,
                        sx + (ex - sx) / 2.0,
                        max(sy, ey) + rung,
                        layer=dim_layer,
                    )
                    results.append(dim)
                if abs(ey - sy) > 1e-9:
                    dim = await self.dimension_linear(
                        ex,
                        sy,
                        ex,
                        ey,
                        max(sx, ex) + rung,
                        (sy + ey) / 2.0,
                        rotation=90.0,
                        layer=dim_layer,
                    )
                    results.append(dim)

        return results

    async def entity_select_smart(
        self,
        predicate: dict,
    ) -> list[EntityInfo]:
        """Semantic entity selection. Predicate keys (all optional, AND-ed):
        - type: "LINE" | "CIRCLE" | ...
        - layer: layer name
        - near: [x, y, radius] — entity reference point within circle
        - length_range: [min, max] — applies to LINE/ARC
        - color: ACI int
        """
        if not isinstance(predicate, dict):
            raise RuntimeError("entity_select_smart: predicate must be a dict.")
        ptype = predicate.get("type")
        player = predicate.get("layer")
        pnear = predicate.get("near")  # [x, y, radius]
        plen = predicate.get("length_range")  # [min, max]
        pcolor = predicate.get("color")

        ents = await self.entity_list(
            type_filter=ptype.upper() if isinstance(ptype, str) else None,
            layer_filter=player if isinstance(player, str) else None,
            limit=5000,
        )

        def _ok(e: EntityInfo) -> bool:
            if pcolor is not None and int(e.color) != int(pcolor):
                return False
            if pnear is not None:
                try:
                    nx, ny, nr = float(pnear[0]), float(pnear[1]), float(pnear[2])
                except Exception:
                    return False
                pt = (
                    e.properties.get("start")
                    or e.properties.get("center")
                    or e.properties.get("insertion")
                    or e.properties.get("insertion_point")
                )
                if pt is None:
                    return False
                if (float(pt[0]) - nx) ** 2 + (float(pt[1]) - ny) ** 2 > nr**2:
                    return False
            if plen is not None and e.type in ("LINE", "ARC"):
                try:
                    lmin, lmax = float(plen[0]), float(plen[1])
                except Exception:
                    return False
                length = e.properties.get("length")
                if length is None or not (lmin <= float(length) <= lmax):
                    return False
            return True

        return [e for e in ents if _ok(e)]

    # ── GD&T (ISO 1101 / ASME Y14.5) ─────────────────────────────────────────
    # Composed from LINE + TEXT primitives so the exact same frame renders on
    # both COM and ezdxf (ezdxf's native TOLERANCE entity renders blank through
    # the matplotlib frontend). Deterministic layout math lives in engineering/
    # gdt.py; datum letters are tracked so the `gdt` critique focus can verify
    # every referenced datum is actually established on the part.

    async def _place_boxed_text(
        self,
        text: str,
        cx: float,
        cy: float,
        text_h: float,
        layer: str,
    ) -> str:
        """Create a TEXT entity roughly centred on (cx, cy). Returns its handle.
        entity_create_text has no alignment arg, so approximate the centring with
        the same width heuristic the title block uses."""
        approx_w = len(text) * text_h * 0.7
        t = await self.entity_create_text(
            text,
            cx - approx_w / 2.0,
            cy - text_h / 2.0,
            height=text_h,
            layer=layer,
        )
        return t.handle

    async def draw_feature_control_frame(
        self,
        symbol: str,
        tolerance: str | float,
        x: float,
        y: float,
        datums: list[str] | None = None,
        height: float = 5.0,
        diameter: bool = False,
        modifier: str | None = None,
        layer: str | None = None,
    ) -> dict:
        """Draw an ISO 1101 feature control frame with bottom-left corner at (x, y).

        symbol: one of the 14 geometric characteristics (straightness, flatness,
        circularity, cylindricity, profile_line, profile_surface, angularity,
        perpendicularity, parallelism, position, concentricity, symmetry,
        circular_runout, total_runout). `datums` is the ordered datum reference
        list (e.g. ["A", "B"]); `diameter=True` prefixes ⌀ for a cylindrical zone;
        `modifier` ∈ {M, L, S} appends the material-condition symbol.

        Orientation/location/runout characteristics require at least one datum
        (raises otherwise). The referenced datums are recorded for the `gdt`
        critique focus.
        """
        from engineering.gdt import fcf_compartments, fcf_layout

        comps = fcf_compartments(
            symbol,
            tolerance,
            datums,
            diameter=diameter,
            modifier=modifier,
        )
        layer = layer or self._role_layer("dim")
        await self._ensure_layer(layer, color=2)
        lay = fcf_layout(comps, float(x), float(y), float(height))
        handles: list[str] = []

        box = lay["box"]
        box_poly = await self.entity_create_polyline(
            [[bx, by] for bx, by in box],
            closed=False,
            layer=layer,
        )
        handles.append(box_poly.handle)
        for (sx, sy), (ex, ey) in lay["dividers"]:
            ln = await self.entity_create_line(sx, sy, ex, ey, layer=layer)
            handles.append(ln.handle)
        th = lay["text_height"]
        for text, tcx, tcy in lay["labels"]:
            handles.append(await self._place_boxed_text(text, tcx, tcy, th, layer))

        refs = getattr(self, "_gdt_datums_referenced", None)
        if refs is None:
            refs = set()
            self._gdt_datums_referenced = refs
        for d in datums or []:
            d = str(d).strip().upper()
            if d:
                refs.add(d)

        return {
            "ok": True,
            "symbol": symbol,
            "compartments": comps,
            "handles": handles,
            "width": lay["width"],
            "height": lay["height"],
            "layer": layer,
        }

    async def draw_datum_feature(
        self,
        letter: str,
        x: float,
        y: float,
        size: float = 5.0,
        layer: str | None = None,
    ) -> dict:
        """Place an ISO 1101 datum feature symbol (filled triangle + boxed letter)
        with its apex at (x, y). Records the datum letter so a feature control
        frame that references it passes the `gdt` critique focus."""
        from engineering.gdt import datum_triangle, fcf_layout

        letter = str(letter).strip().upper()
        if not letter:
            raise RuntimeError("draw_datum_feature: a datum letter is required.")
        layer = layer or self._role_layer("dim")
        await self._ensure_layer(layer, color=2)
        size = float(size)
        handles: list[str] = []

        tri = datum_triangle(float(x), float(y), size, down=True)
        tri_poly = await self.entity_create_polyline(
            [[px, py] for px, py in tri],
            closed=True,
            layer=layer,
        )
        handles.append(tri_poly.handle)

        box_h = size * 1.4
        lay = fcf_layout([letter], float(x) - box_h / 2.0, float(y) - size - box_h, box_h)
        box_poly = await self.entity_create_polyline(
            [[bx, by] for bx, by in lay["box"]],
            closed=False,
            layer=layer,
        )
        handles.append(box_poly.handle)
        for text, tcx, tcy in lay["labels"]:
            handles.append(await self._place_boxed_text(text, tcx, tcy, lay["text_height"], layer))

        defined = getattr(self, "_gdt_datums_defined", None)
        if defined is None:
            defined = set()
            self._gdt_datums_defined = defined
        defined.add(letter)

        return {"ok": True, "datum": letter, "handles": handles, "layer": layer}

    # ── drawing settings (friendly system-variable facade) ───────────────────
    # A user-facing wrapper over system_get_variable / system_set_variable that
    # maps memorable names ("units", "dimscale", …) to AutoCAD system variables.
    # Concrete + backend-agnostic: both engines accept the bare sysvar name, so
    # the same call reads/writes on live COM and headless ezdxf alike.

    async def drawing_settings(self, settings: dict | None = None) -> dict:
        """Read (no args) or apply (with args) common drawing settings.

        With ``settings=None`` returns a snapshot of every known setting. With a
        dict, applies each provided key and returns ``{applied, errors, current}``.
        Friendly keys: units, linear_precision, angular_precision, ltscale,
        dimscale, text_size, point_mode, point_size, osmode, fillet_radius.
        `units` accepts mm/cm/m/inch/feet (mapped to the INSUNITS code)."""
        if not settings:
            snapshot: dict = {}
            for key, (var, kind) in _SETTING_MAP.items():
                try:
                    raw = await self.system_get_variable(var)
                except Exception as exc:
                    snapshot[key] = {"error": str(exc)}
                    continue
                snapshot[key] = _decode_setting(key, kind, raw)
            return {"ok": True, "settings": snapshot}

        applied: dict = {}
        errors: dict = {}
        for key, value in settings.items():
            spec = _SETTING_MAP.get(key)
            if spec is None:
                errors[key] = f"unknown setting (valid: {sorted(_SETTING_MAP)})"
                continue
            var, kind = spec
            try:
                encoded = _encode_setting(key, kind, value)
                await self.system_set_variable(var, encoded)
                applied[key] = value
            except Exception as exc:
                errors[key] = str(exc)

        result = {"ok": not errors, "applied": applied}
        if errors:
            result["errors"] = errors
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Friendly setting name -> (AutoCAD system variable, value kind).
_SETTING_MAP: dict[str, tuple[str, str]] = {
    "units": ("INSUNITS", "units"),
    "linear_precision": ("LUPREC", "int"),
    "angular_precision": ("AUPREC", "int"),
    "ltscale": ("LTSCALE", "float"),
    "dimscale": ("DIMSCALE", "float"),
    "text_size": ("TEXTSIZE", "float"),
    "point_mode": ("PDMODE", "int"),
    "point_size": ("PDSIZE", "float"),
    "osmode": ("OSMODE", "int"),
    "fillet_radius": ("FILLETRAD", "float"),
}

# INSUNITS code table (AutoCAD $INSUNITS): friendly name <-> integer code.
_UNIT_TO_CODE: dict[str, int] = {
    "unitless": 0,
    "inch": 1,
    "inches": 1,
    "in": 1,
    "feet": 2,
    "ft": 2,
    "foot": 2,
    "mm": 4,
    "millimeter": 4,
    "millimeters": 4,
    "cm": 5,
    "centimeter": 5,
    "centimeters": 5,
    "m": 6,
    "meter": 6,
    "meters": 6,
}
_CODE_TO_UNIT: dict[int, str] = {0: "unitless", 1: "inch", 2: "feet", 4: "mm", 5: "cm", 6: "m"}


def _encode_setting(key: str, kind: str, value: Any) -> Any:
    """Coerce a friendly value into the raw system-variable value."""
    if kind == "units":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        code = _UNIT_TO_CODE.get(str(value).strip().lower())
        if code is None:
            raise ValueError(
                f"units: unknown value {value!r}. Use one of "
                f"{sorted(set(_UNIT_TO_CODE))} or an INSUNITS integer."
            )
        return code
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    return value


def _decode_setting(key: str, kind: str, raw: Any) -> Any:
    """Present a raw system-variable value in friendly form."""
    if raw is None:
        return None
    if kind == "units":
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return raw
        return {"code": code, "name": _CODE_TO_UNIT.get(code, "unknown")}
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def shoelace_area(points: list[list[float]]) -> float:
    """Calculate polygon area using the shoelace formula."""
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1]
        area -= points[j][0] * points[i][1]
    return abs(area) / 2.0


def deg2rad(degrees: float) -> float:
    return degrees * math.pi / 180.0


def rad2deg(radians: float) -> float:
    return radians * 180.0 / math.pi
