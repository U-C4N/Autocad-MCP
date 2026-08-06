"""Trim, extend, fillet, chamfer.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import EntityInfo


class CornerOpsContract(ABC):
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
