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


def refuse_reserved_layer_delete(name: str, current: str) -> None:
    """The layer-delete refusals both engines share: ``0``, ``Defpoints``, current.

    AutoCAD refuses all three; the live engine's own refusal is a localised COM
    message naming no cause, and the headless engine used to allow the last
    one (leaving ``$CLAYER`` naming a layer the file no longer had).
    """
    if name.lower() in ("0", "defpoints"):
        raise ValueError(f"layer {name!r} cannot be deleted: AutoCAD reserves it")
    if name.lower() == str(current or "").lower():
        raise ValueError(
            f"layer {name!r} is the current layer; make another layer current "
            "(layer_set_current) before deleting it"
        )


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
        *,
        exist_ok: bool = False,
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

    @abstractmethod
    async def layer_set_true_color(self, name: str, rgb: tuple[int, int, int]) -> dict:
        """Give a layer a 24-bit colour. ``{ok, layer, true_color: "#RRGGBB"}``.

        Entities drawn ByLayer on it take that colour. Setting an ACI ``color``
        with ``layer_modify`` afterwards removes it again, as in AutoCAD.
        """
        ...
