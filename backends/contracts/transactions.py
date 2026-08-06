"""Transactions.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TransactionContract(ABC):
    @abstractmethod
    async def transaction_begin(self) -> dict: ...

    @abstractmethod
    async def transaction_commit(self) -> dict: ...

    @abstractmethod
    async def transaction_rollback(self) -> dict: ...
