"""Blocks and block references.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import BlockInfo, EntityInfo


class BlockContract(ABC):
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
