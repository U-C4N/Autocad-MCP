"""Abstract base class + shared data models for AutoCAD backends."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    ok: bool
    payload: Any = None
    error: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            d["payload"] = self.payload
        else:
            d["error"] = self.error
        return d


@dataclass
class EntityInfo:
    handle: str
    type: str          # e.g. "LINE", "CIRCLE", "ARC"
    layer: str
    color: int         # ACI color (256=ByLayer)
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
    async def drawing_export_pdf(self, path: str) -> dict: ...

    @abstractmethod
    async def drawing_purge(self) -> dict: ...

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
        self, x1: float, y1: float, x2: float, y2: float,
        z1: float = 0.0, z2: float = 0.0,
        layer: str | None = None, color: int | None = None,
        linetype: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_circle(
        self, cx: float, cy: float, radius: float,
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_arc(
        self, cx: float, cy: float, radius: float,
        start_angle: float, end_angle: float,
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_polyline(
        self, points: list[list[float]], closed: bool = False,
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_text(
        self, text: str, x: float, y: float,
        height: float = 2.5, rotation: float = 0.0,
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_mtext(
        self, text: str, x: float, y: float,
        width: float = 100.0, height: float = 2.5,
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_hatch(
        self, pattern: str, boundary_points: list[list[float]],
        scale: float = 1.0, angle: float = 0.0,
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_spline(
        self, fit_points: list[list[float]],
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_ellipse(
        self, cx: float, cy: float,
        major_x: float, major_y: float, ratio: float = 0.5,
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_point(
        self, x: float, y: float,
        layer: str | None = None, color: int | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_create_block_ref(
        self, name: str, x: float, y: float,
        scale_x: float = 1.0, scale_y: float = 1.0, rotation: float = 0.0,
        layer: str | None = None,
    ) -> EntityInfo: ...

    # ── dimensions ───────────────────────────────────────────────────────────
    @abstractmethod
    async def dimension_linear(
        self, x1: float, y1: float, x2: float, y2: float,
        dim_x: float, dim_y: float, rotation: float = 0.0,
        layer: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_aligned(
        self, x1: float, y1: float, x2: float, y2: float,
        dim_x: float, dim_y: float,
        layer: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_angular(
        self, vx: float, vy: float,
        x1: float, y1: float, x2: float, y2: float,
        tx: float, ty: float,
        layer: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_radius(
        self, cx: float, cy: float, chord_x: float, chord_y: float,
        leader_length: float = 10.0, layer: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_diameter(
        self, x1: float, y1: float, x2: float, y2: float,
        leader_length: float = 10.0, layer: str | None = None,
    ) -> EntityInfo: ...

    # ── entity modification ──────────────────────────────────────────────────
    @abstractmethod
    async def entity_move(self, handle: str, dx: float, dy: float, dz: float = 0.0) -> dict: ...

    @abstractmethod
    async def entity_copy(self, handle: str, dx: float, dy: float, dz: float = 0.0) -> EntityInfo: ...

    @abstractmethod
    async def entity_rotate(
        self, handle: str, base_x: float, base_y: float, angle_deg: float,
    ) -> dict: ...

    @abstractmethod
    async def entity_scale(
        self, handle: str, base_x: float, base_y: float, factor: float,
    ) -> dict: ...

    @abstractmethod
    async def entity_mirror(
        self, handle: str,
        x1: float, y1: float, x2: float, y2: float,
        delete_original: bool = False,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_offset(
        self, handle: str, distance: float,
        side_x: float | None = None, side_y: float | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_delete(self, handle: str) -> dict: ...

    @abstractmethod
    async def entity_array_rectangular(
        self, handle: str,
        rows: int, cols: int, row_spacing: float, col_spacing: float,
    ) -> list[EntityInfo]: ...

    @abstractmethod
    async def entity_array_polar(
        self, handle: str,
        count: int, fill_angle: float,
        center_x: float, center_y: float,
    ) -> list[EntityInfo]: ...

    # ── entity query/properties ───────────────────────────────────────────────
    @abstractmethod
    async def entity_get(self, handle: str) -> EntityInfo: ...

    @abstractmethod
    async def entity_set_properties(
        self, handle: str,
        layer: str | None = None,
        color: int | None = None,
        linetype: str | None = None,
        lineweight: float | None = None,
        visible: bool | None = None,
    ) -> dict: ...

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
        self, name: str,
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
        self, name: str,
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
        self, name: str, file: str | None = None,
    ) -> dict: ...

    # ── block operations ─────────────────────────────────────────────────────
    @abstractmethod
    async def block_list(self) -> list[BlockInfo]: ...

    @abstractmethod
    async def block_insert(
        self, name: str, x: float, y: float,
        scale_x: float = 1.0, scale_y: float = 1.0, rotation: float = 0.0,
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
        self, name: str, handles: list[str],
        base_x: float = 0.0, base_y: float = 0.0,
    ) -> dict: ...

    # ── analysis / query ─────────────────────────────────────────────────────
    @abstractmethod
    async def analysis_stats(self) -> dict: ...

    @abstractmethod
    async def analysis_entities_in_region(
        self, x1: float, y1: float, x2: float, y2: float,
    ) -> list[EntityInfo]: ...

    @abstractmethod
    async def analysis_measure_distance(
        self, x1: float, y1: float, x2: float, y2: float,
    ) -> float: ...

    @abstractmethod
    async def analysis_measure_area(self, points: list[list[float]]) -> float: ...

    @abstractmethod
    async def analysis_bounding_box(self) -> dict: ...

    @abstractmethod
    async def analysis_select_by_layer(self, layer_name: str) -> list[EntityInfo]: ...

    @abstractmethod
    async def analysis_select_by_type(self, entity_type: str) -> list[EntityInfo]: ...

    # ── view / screenshot ────────────────────────────────────────────────────
    @abstractmethod
    async def view_zoom_extents(self) -> dict: ...

    @abstractmethod
    async def view_zoom_window(
        self, x1: float, y1: float, x2: float, y2: float,
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

    # ── concrete helpers (use existing primitives) ──────────────────────────
    async def set_layer_active(self, name: str) -> None:
        """Convenience: alias for layer_set_current. Concrete; backends need not override."""
        await self.layer_set_current(name)

    async def ensure_linetypes(
        self, names: list[str], file: str | None = None,
    ) -> dict[str, str]:
        """Idempotently load each linetype in `names`. Returns {name: status}.
        Status is one of: 'already_loaded', 'loaded', 'failed: <msg>'.
        """
        try:
            current = {ln.lower() for ln in await self.linetype_list()}
        except Exception:
            current = set()
        results: dict[str, str] = {}
        for n in names:
            if n.lower() in current:
                results[n] = "already_loaded"
                continue
            try:
                await self.linetype_load(n, file=file)
                results[n] = "loaded"
            except Exception as exc:
                results[n] = f"failed: {exc}"
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
