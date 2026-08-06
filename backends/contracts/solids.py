"""ACIS solids (live AutoCAD only).

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SolidContract(ABC):
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
    async def drawing_audit(self) -> dict:
        """Audit the drawing, applying every repair the backend can make.

        This MUTATES the drawing. Returns ``{"ok", "repaired", "fixes",
        "fix_count", "errors", "error_count"}``, where ``fixes`` are problems
        already repaired and ``errors`` are problems that could not be; both are
        lists of ``{"code": int, "name": str, "message": str, "handle": str |
        None}``.

        A backend that repairs but cannot observe its own result reports
        ``repaired``/``fix_count``/``error_count`` as ``None`` — never ``0``,
        which would claim nothing was wrong — plus ``"detail": "unavailable"``
        and ``"capability": "audit_detail"`` so a client can pre-check the
        boundary. Any implementation that repairs MUST mark the document dirty,
        or the repair is discarded the next time the document is closed.
        """
        ...

    @abstractmethod
    async def drawing_close(self, save: bool = True) -> dict: ...

    @abstractmethod
    async def drawing_undo(self) -> dict: ...

    @abstractmethod
    async def drawing_redo(self) -> dict: ...
