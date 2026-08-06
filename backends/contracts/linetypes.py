"""Linetypes.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LinetypeContract(ABC):
    @abstractmethod
    async def linetype_list(self) -> list[str]: ...

    @abstractmethod
    async def linetype_load(
        self,
        name: str,
        file: str | None = None,
    ) -> dict: ...
