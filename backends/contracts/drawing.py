"""Document lifecycle.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import DrawingInfo


class DrawingContract(ABC):
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
    async def drawing_export_dxf(self, path: str) -> dict:
        """Write a DXF copy of the open document to ``path``. The document is never
        re-pointed: its name, path and saved flag stay as they were and its content
        revision does not move (``drawing_save_as`` is the call that re-points).
        Returns ``{"ok", "path", "format", "method", "dxf_version", "document",
        "repointed": False}``."""

    @abstractmethod
    async def drawing_export_pdf(self, path: str, layout: str | None = None) -> dict: ...

    @abstractmethod
    async def drawing_purge(self) -> dict: ...
