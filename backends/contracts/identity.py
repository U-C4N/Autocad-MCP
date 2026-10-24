"""Connecting and describing the backend.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import CapabilityMap


class IdentityContract(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def capabilities(self) -> CapabilityMap: ...

    @abstractmethod
    def document_identity(self) -> dict:
        """The active (headless) or bound (live) document's identity (v1.7.0, B11).

        ``{"document_id", "revision", "name", "path", "backend"}``.
        ``document_id`` is a uuid4 hex minted when the document is registered
        - not ``$FINGERPRINTGUID``, which every template-born drawing shares,
        and not ``$VERSIONGUID``, which every snapshot save rewrites.
        ``revision`` is the document's content revision (each engine says what
        moves it). The four document fields are None when nothing is open or
        bound. Synchronous and free: it reads state the backend already keeps
        and never touches the drawing.
        """
