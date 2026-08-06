"""Abstract base class + shared data models for AutoCAD backends.

The interface itself lives in :mod:`backends.contracts`, one module per domain;
this module owns the shared data models, the refusal vocabulary, and the
composition that ties the contracts together. Callers import
``AutoCADBackend`` from here exactly as before.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backends.capability import (  # noqa: F401 — deliberate re-export, see below
    UnsupportedCapabilityError,
    _unsupported,
)
from backends.contracts import (
    AnalysisContract,
    BlockContract,
    CornerOpsContract,
    DimensionContract,
    DrawingContract,
    EntityCreationContract,
    EntityModificationContract,
    EntityQueryContract,
    GdtContract,
    IdentityContract,
    LayerContract,
    LayoutContract,
    LinetypeContract,
    PremiumContract,
    SettingsContract,
    SolidContract,
    SystemContract,
    TransactionContract,
    ViewContract,
)

if TYPE_CHECKING:
    pass

# The refusal vocabulary lives in ``backends.capability`` — the contracts need
# it and this module imports the contracts, so defining it here would be a
# cycle. Re-exported so every existing
# ``from backends.base import UnsupportedCapabilityError`` keeps working and
# there is still exactly one class object in play.


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class EntityInfo:
    handle: str
    type: str  # e.g. "LINE", "CIRCLE", "ARC"
    layer: str
    color: int  # ACI color (256=ByLayer)
    linetype: str
    visible: bool
    properties: dict = field(default_factory=dict)


@dataclass
class LayerInfo:
    name: str
    color: int
    linetype: str
    lineweight: float
    is_on: bool
    is_frozen: bool
    is_locked: bool
    is_current: bool


@dataclass
class BlockInfo:
    name: str
    origin: tuple[float, float]
    attribute_count: int
    entity_count: int
    is_xref: bool
    description: str = ""


@dataclass
class DrawingInfo:
    name: str
    full_path: str
    saved: bool
    entity_count: int
    layer_count: int
    block_count: int
    extents_min: tuple[float, float]
    extents_max: tuple[float, float]
    units: str
    version: str = ""
    backend: str = ""


@dataclass(frozen=True)
class FeatureCapability:
    supported: bool
    mode: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "mode": self.mode,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CapabilityMap:
    backend: str
    features: dict[str, FeatureCapability]

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "features": {name: value.to_dict() for name, value in self.features.items()},
        }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def normalize_lineweight(lw: float | int | None) -> int | None:
    """Coerce a lineweight to ezdxf/COM hundredths-of-a-mm integers.

    AutoCAD and ezdxf store lineweights as integer hundredths (25 == 0.25 mm),
    plus the -1/-2/-3 sentinels (ByLayer/ByBlock/Default). Callers, however,
    mix conventions: the MCP tool boundary passes hundredths (25) while the
    engineering layer sets pass millimetres (0.25). The old ``int(0.25)``
    silently truncated millimetre values to 0 and wiped the lineweight (and with
    it the whole ISO 128 discipline). Disambiguation is unambiguous because no
    ISO 128 millimetre value exceeds 2.0 and no valid hundredth is below 5, so
    the ``(0, 2.05]`` band is always millimetres.
    """
    if lw is None:
        return None
    try:
        v = float(lw)
    except (TypeError, ValueError):
        return lw  # leave exotic / already-int values untouched
    if v < 0:
        return int(round(v))  # -1/-2/-3 sentinels
    if v == 0:
        return 0
    if v <= 2.05:
        return int(round(v * 100.0))  # millimetres -> hundredths
    return int(round(v))  # already hundredths


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------


class AutoCADBackend(
    IdentityContract,
    DrawingContract,
    LayoutContract,
    SolidContract,
    EntityCreationContract,
    DimensionContract,
    EntityModificationContract,
    CornerOpsContract,
    EntityQueryContract,
    LayerContract,
    LinetypeContract,
    BlockContract,
    AnalysisContract,
    ViewContract,
    TransactionContract,
    SystemContract,
    PremiumContract,
    GdtContract,
    SettingsContract,
):
    """All backends implement this interface.

    The methods live in :mod:`backends.contracts`, one module per domain.
    This class is the composition and the import surface: everything that
    used to do ``from backends.base import AutoCADBackend`` still works, and
    ``isinstance`` against it is unchanged.
    """


def shoelace_area(points: list[list[float]]) -> float:
    """Calculate polygon area using the shoelace formula."""
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1]
        area -= points[j][0] * points[i][1]
    return abs(area) / 2.0


def deg2rad(degrees: float) -> float:
    return degrees * math.pi / 180.0


def rad2deg(radians: float) -> float:
    return radians * 180.0 / math.pi
