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

    @abstractmethod
    async def view_render(
        self,
        region: list[float] | None = None,
        max_px: int = 1568,
        overlay_handles: bool = False,
    ) -> dict:
        """A PNG of ``region`` (``[x0, y0, x1, y1]``, WCS) and the scale it was drawn at (1.8-07).

        Returns ``{"png", "applied", "framing", "source", "px", "region",
        "requested_region", "px_per_unit", ...}`` (``backends/region_render.py``).
        Headless renders the current space (``source: "headless"``); live, a
        region renders the ``Document.Export`` snapshot through the same
        renderer (``source: "export_snapshot"``) and no region is the window
        capture (``source: "window"``, ``region``/``px_per_unit`` None). A
        degenerate region or ``max_px`` outside 64-4096 is ``ValueError``.
        """
        ...
