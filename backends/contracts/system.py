"""System variables and raw commands.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SystemContract(ABC):
    @abstractmethod
    async def system_status(self) -> dict: ...

    @abstractmethod
    async def system_get_variable(self, name: str) -> Any: ...

    @abstractmethod
    async def system_set_variable(self, name: str, value: Any) -> dict: ...

    @abstractmethod
    async def system_run_command(self, command: str) -> dict: ...

    @abstractmethod
    async def system_run_lisp(self, expression: str) -> dict: ...
