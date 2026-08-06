"""Views and screenshots.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ViewContract(ABC):
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
    async def view_screenshot(self, overlay_handles: bool = False) -> bytes | None: ...

    @abstractmethod
    async def view_screenshot_grounded(
        self,
        overlay_handles: bool = True,
        max_labels: int = 40,
    ) -> dict:
        """Screenshot plus the handle-to-position mapping it drew.

        Returns ``{"png", "labels", "labelled", "total", "truncated"}``.
        ``labels`` maps handle -> ``[x, y]`` in drawing units, so the picture and
        the handle space a caller can act on are the same thing. ``truncated``
        says a crowded drawing was capped rather than letting a labelled subset
        read as the whole sheet.
        """
        ...
