"""Moving, copying and editing geometry.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import EntityInfo


class EntityModificationContract(ABC):
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

    # ── text annotation (M8 / F15) ───────────────────────────────────────────

    @abstractmethod
    async def text_set_background(
        self,
        handle: str,
        enabled: bool = True,
        color: int | None = None,
        scale: float = 1.5,
    ) -> dict:
        """Put an opaque background box behind an MTEXT. ``{ok, handle, enabled}``.

        MTEXT only — TEXT has no background-fill attribute, so setting one on
        it would report success and change nothing. ``scale`` is a multiple of
        the text box and must be >= 1: a box smaller than its text is a stripe
        through the text, not a mask.
        """
        ...

    @abstractmethod
    async def text_find_replace(
        self,
        find: str,
        replace: str,
        layer: str | None = None,
        match_case: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Replace text across the drawing.

        Returns ``{ok, replaced, entities, searched_types, note, dry_run}``.
        ``searched_types`` is reported rather than assumed: "no matches" and
        "that type was never searched" are different answers, and an editor
        that silently skips block attributes is the classic version of this
        bug. DIMENSION text is deliberately out of scope — its ``text`` field
        holds the ``<>`` override placeholder rather than the measurement, so
        editing it would either do nothing or break the association.
        """
        ...
