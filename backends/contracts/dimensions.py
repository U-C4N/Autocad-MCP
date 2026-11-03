"""ISO 129 dimensions.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import EntityInfo


class DimensionContract(ABC):
    # ``xdata={app: values}`` (v1.8.0, plan item 1.8-06; the B17 pattern of
    # ``EntityCreationContract``): linear, aligned, radius and diameter - the
    # four the mechanical drawer calls - write XDATA onto the DIMENSION entity
    # itself (never its anonymous ``*D`` block) inside the create call's own
    # job, validated by ``backends/xdata_specs.py::prepare_xdata`` before the
    # dimension exists, so a refused tag creates nothing.

    @abstractmethod
    async def dimension_linear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        dim_x: float,
        dim_y: float,
        rotation: float = 0.0,
        layer: str | None = None,
        tol_upper: float | None = None,
        tol_lower: float | None = None,
        tol_mode: str = "none",
        text_override: str | None = None,
        xdata: dict[str, list] | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_aligned(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        dim_x: float,
        dim_y: float,
        layer: str | None = None,
        xdata: dict[str, list] | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_angular(
        self,
        vx: float,
        vy: float,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        tx: float,
        ty: float,
        layer: str | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_radius(
        self,
        cx: float,
        cy: float,
        chord_x: float,
        chord_y: float,
        leader_length: float = 10.0,
        layer: str | None = None,
        tol_upper: float | None = None,
        tol_lower: float | None = None,
        tol_mode: str = "none",
        text_override: str | None = None,
        xdata: dict[str, list] | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def dimension_diameter(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        leader_length: float = 10.0,
        layer: str | None = None,
        tol_upper: float | None = None,
        tol_lower: float | None = None,
        tol_mode: str = "none",
        text_override: str | None = None,
        xdata: dict[str, list] | None = None,
    ) -> EntityInfo: ...
