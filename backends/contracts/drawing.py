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
    async def drawing_open(self, path: str, recover: bool = False) -> dict:
        """Open ``path``. A file that does not parse is refused (``damaged_drawing``,
        naming ``recover=True``) and the open document is kept; ``recover=True``
        reads a damaged DXF through ``ezdxf.recover`` and reports ``recovered:
        {fixes, errors, messages, entities_read}`` and ``recovered_from`` (1.8-04,
        B09). Both engines report the file's ``lineage`` and ``external_change``."""

    @abstractmethod
    async def drawing_save(self, path: str | None = None, overwrite_source: bool = False) -> dict:
        """Save. Onto the damaged source of a ``recover=True`` document only with
        ``overwrite_source=True`` (``recovered_source_protected`` otherwise)."""

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
