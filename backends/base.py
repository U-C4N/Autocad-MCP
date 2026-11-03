"""Abstract base class + shared data models for AutoCAD backends.

The interface itself lives in :mod:`backends.contracts`, one module per domain;
this module owns the shared data models, the refusal vocabulary, and the
composition that ties the contracts together. Callers import
``AutoCADBackend`` from here exactly as before.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

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
    EnvironmentContract,
    GdtContract,
    IdentityContract,
    LayerContract,
    LayoutContract,
    LinetypeContract,
    PageSetupContract,
    PremiumContract,
    RefsContract,
    SettingsContract,
    SolidContract,
    StylesContract,
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
    #: v1.8.0: the DXF lineweight read off the entity - "ByLayer", "ByBlock",
    #: "Default" (-3, LWDEFAULT, not ByLayer) or "0.25 mm". None when unread.
    lineweight_name: str | None = None


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
    # v1.7.0 (B11): the document's id and content revision.
    document_id: str | None = None
    revision: int | None = None


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


#: DXF lineweight sentinels (group 370). -3 is *Default* - the LWDEFAULT
#: setting - not ByLayer: 1.7.0's schemas said otherwise, and a model that set
#: -3 reported "by-layer" (plan 1.8-01, rival claim 4.5).
LINEWEIGHT_BYLAYER = -1
LINEWEIGHT_BYBLOCK = -2
LINEWEIGHT_DEFAULT = -3

#: The names a lineweight input accepts, case-insensitively, and their codes.
LINEWEIGHT_NAMES: dict[str, int] = {
    "bylayer": LINEWEIGHT_BYLAYER,
    "byblock": LINEWEIGHT_BYBLOCK,
    "default": LINEWEIGHT_DEFAULT,
}
_LINEWEIGHT_SPELLING = {
    LINEWEIGHT_BYLAYER: "ByLayer",
    LINEWEIGHT_BYBLOCK: "ByBlock",
    LINEWEIGHT_DEFAULT: "Default",
}

#: The tool-boundary type of every lineweight input: hundredths of a mm or a name.
LineweightName = Literal["ByLayer", "ByBlock", "Default"]
Lineweight = int | LineweightName

#: The one description every lineweight parameter carries (plus a layer note).
LINEWEIGHT_HELP = (
    "Lineweight in hundredths of a mm (25 = 0.25 mm; ISO 128: 13, 18, 25, 35, 50, 70, "
    "100, 140, 200) or a name: -1 = 'ByLayer', -2 = 'ByBlock', -3 = 'Default' (the "
    "LWDEFAULT setting - not ByLayer)"
)
#: What a layer's lineweight input adds: ByLayer / ByBlock are refused there.
LINEWEIGHT_LAYER_NOTE = "; a layer takes 'Default' or a value - ByLayer and ByBlock are refused"


class InvalidLineweightError(ValueError):
    """A lineweight the target cannot carry, refused by name before any write."""

    def __init__(self, value, target: str, reason: str):
        self.value = value
        self.target = target
        self.reason = reason
        super().__init__(
            f"Lineweight {value!r} refused on {'a layer' if target == 'layer' else 'an entity'}: "
            f"{reason}. Accepted: hundredths of a mm (25 = 0.25 mm) or the names "
            "'ByLayer' (-1), 'ByBlock' (-2), 'Default' (-3)"
            + (
                "; a layer takes 'Default' or a value, never ByLayer / ByBlock"
                if target == "layer"
                else ""
            )
            + "."
        )


def parse_lineweight(value, *, target: str = "entity") -> int | None:
    """The DXF integer for a lineweight input, or None when there is none.

    Takes an integer (hundredths of a mm, or a -1/-2/-3 sentinel), a
    millimetre float (the engineering layer sets' 0.25 - ``normalize_lineweight``
    tells them apart), a numeric string, or the names ``ByLayer`` /
    ``ByBlock`` / ``Default`` in any case. ``target="layer"`` refuses ByLayer and
    ByBlock - by number or by name - because a layer is what ByLayer points
    at. Anything else raises :class:`InvalidLineweightError` naming the
    accepted spellings; nothing is coerced silently.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidLineweightError(value, target, "not a number or a lineweight name")
    if isinstance(value, str):
        key = value.strip().lower()
        if key in LINEWEIGHT_NAMES:
            code = LINEWEIGHT_NAMES[key]
        else:
            try:
                float(key)
            except ValueError:
                raise InvalidLineweightError(value, target, "unknown lineweight name") from None
            code = normalize_lineweight(key)
    else:
        if not math.isfinite(float(value)):
            raise InvalidLineweightError(value, target, "not a finite number")
        code = normalize_lineweight(value)
    if code < 0 and code not in _LINEWEIGHT_SPELLING:
        raise InvalidLineweightError(value, target, "the only negative codes are -1, -2 and -3")
    if target == "layer" and code in (LINEWEIGHT_BYLAYER, LINEWEIGHT_BYBLOCK):
        raise InvalidLineweightError(
            value, target, f"{_LINEWEIGHT_SPELLING[code]} ({code}) has no meaning on a layer"
        )
    return code


def lineweight_name(value) -> str | None:
    """``ByLayer`` / ``ByBlock`` / ``Default`` for a sentinel, ``"0.25 mm"`` for 25."""
    if value is None:
        return None
    try:
        code = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if code in _LINEWEIGHT_SPELLING:
        return _LINEWEIGHT_SPELLING[code]
    return f"{code / 100.0:.2f} mm"


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------


class AutoCADBackend(
    IdentityContract,
    DrawingContract,
    LayoutContract,
    PageSetupContract,
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
    StylesContract,
    EnvironmentContract,
    RefsContract,
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
