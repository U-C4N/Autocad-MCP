"""Reading entities back.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import EntityInfo


class EntityQueryContract(ABC):
    # ── selection filters (M8 / F1) ──────────────────────────────────────────

    @abstractmethod
    async def selection_window(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        mode: str = "window",
        entity_type: str = "",
        layer: str = "",
    ) -> dict:
        """AutoCAD's ssget window/crossing. ``{ok, handles, count, mode}``.

        ``window`` returns only entities wholly inside the box; ``crossing``
        also returns the ones that straddle its edge. Getting those backwards
        is silent and wrong, so ``mode`` is reported back and defaults to the
        narrower one. A zero-area box is refused rather than answered with an
        empty list, which would read as "nothing there".

        Selection is by *drawn* position: an entity in a mirrored frame is
        found where ``entity_get`` reports it, not where the file stores it.
        """
        ...

    @abstractmethod
    async def selection_polygon(
        self,
        points: list[list[float]],
        mode: str = "window",
        entity_type: str = "",
        layer: str = "",
    ) -> dict:
        """Window/crossing selection against an arbitrary polygon.

        A polygon is not its bounding box — the difference is the whole point
        of the tool, so the implementation must test against the shape itself.
        """
        ...

    @abstractmethod
    async def selection_filter(
        self,
        entity_type: str = "",
        layer: str = "",
        color: int | None = None,
        linetype: str = "",
        min_area: float | None = None,
    ) -> dict:
        """Property filter. Returns ``{ok, handles, count, filtered_by}``.

        Named parameters rather than a query string, deliberately: ezdxf's
        query language answers an unknown attribute with an empty result, so a
        typo is indistinguishable from "no matches". Here a typo is a
        ``TypeError`` at the boundary, and ``filtered_by`` tells the caller
        which filters actually ran — "I filtered by layer and found none" and
        "I never filtered by layer" are different answers.
        """
        ...

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

    @abstractmethod
    async def entity_count(
        self,
        type_filter: str | None = None,
        layer_filter: str | None = None,
    ) -> int:
        """How many entities ``entity_list`` would match, ignoring limit/offset.

        Exists so a *paged* listing can report an honest total without paying
        for the rows it is not going to return. ``entity_list`` stops building
        ``EntityInfo`` objects the moment it has ``limit`` of them, so it can
        never tell a caller whether anything followed the page — and a
        truncated list read as complete is a correctness bug, not a token one.

        Two obligations, both load-bearing:

        * apply **exactly** the filter semantics of ``entity_list`` (same
          case-insensitive type/layer comparison), or ``truncated`` would be
          computed against a different set than the one that was paged;
        * do not build ``EntityInfo`` rows. A count implemented by listing
          would cost more than the listing it is meant to make cheap.
        """
        ...
