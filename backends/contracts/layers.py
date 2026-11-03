"""Layers.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import LayerInfo


class LayerContract(ABC):
    @abstractmethod
    async def layer_list(self) -> list[LayerInfo]: ...

    @abstractmethod
    async def layer_create(
        self,
        name: str,
        color: int = 7,
        linetype: str = "Continuous",
        lineweight: float = -3,
    ) -> LayerInfo: ...

    @abstractmethod
    async def layer_delete(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_set_current(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_modify(
        self,
        name: str,
        color: int | None = None,
        linetype: str | None = None,
        lineweight: float | None = None,
    ) -> LayerInfo: ...

    @abstractmethod
    async def layer_freeze(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_thaw(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_lock(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_unlock(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_hide(self, name: str) -> dict: ...

    @abstractmethod
    async def layer_show(self, name: str) -> dict: ...

    # ── set-based edits (1.8-12) ─────────────────────────────────────────────

    @abstractmethod
    async def layer_rename(self, old: str, new: str) -> dict:
        """Rename a layer and every reference to its name.

        Headless: the table entry, ``dxf.layer`` of every entity in every
        layout and block definition (ATTRIBs and VERTEXes too), viewport frozen
        lists and ``$CLAYER``. Live: ``AcadLayer.Name`` (AutoCAD references a
        layer by object id, so the rest follows). Refused by name, before
        anything changes: ``0``, ``DEFPOINTS``, an xref-dependent layer, a
        missing ``old``, an existing ``new``, a case-only change.
        ``{ok, old, new, current_layer, changes}``.
        """
        ...

    @abstractmethod
    async def layer_merge(
        self, sources: list[str], target: str, delete_sources: bool = True
    ) -> dict:
        """Move every entity on ``sources`` onto ``target``, then delete the sources.

        Reassigns in model space, every paper layout and every block
        definition (ATTRIBs included), reports ``blocks_touched`` - an edited
        definition changes every INSERT of it - and ``layers_deleted``.
        Refused by name before anything changes: a missing source or target,
        ``0`` / ``DEFPOINTS`` / an xref-dependent layer as a source, the target
        among the sources. ``{ok, sources, target, reassigned, by_space,
        blocks_touched, note, layers_deleted, layers_not_deleted,
        current_layer, changes}``.
        """
        ...
