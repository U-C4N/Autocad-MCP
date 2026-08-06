"""Per-domain slices of the backend contract.

One 1854-line ABC with 112 abstract methods made every contract change a
whole-file edit, and made a half-finished change break both backends at
instantiation at once. The seams here are the section markers that were
already in the file, so the split moved code without re-deciding anything.
"""

from __future__ import annotations

from backends.contracts.analysis import AnalysisContract
from backends.contracts.blocks import BlockContract
from backends.contracts.corner_ops import CornerOpsContract
from backends.contracts.dimensions import DimensionContract
from backends.contracts.drawing import DrawingContract
from backends.contracts.entity_creation import EntityCreationContract
from backends.contracts.entity_modification import EntityModificationContract
from backends.contracts.entity_query import EntityQueryContract
from backends.contracts.gdt import GdtContract
from backends.contracts.identity import IdentityContract
from backends.contracts.layers import LayerContract
from backends.contracts.layouts import LayoutContract
from backends.contracts.linetypes import LinetypeContract
from backends.contracts.premium import PremiumContract
from backends.contracts.settings import SettingsContract
from backends.contracts.solids import SolidContract
from backends.contracts.system import SystemContract
from backends.contracts.transactions import TransactionContract
from backends.contracts.view import ViewContract

__all__ = [
    "AnalysisContract",
    "BlockContract",
    "CornerOpsContract",
    "DimensionContract",
    "DrawingContract",
    "EntityCreationContract",
    "EntityModificationContract",
    "EntityQueryContract",
    "GdtContract",
    "IdentityContract",
    "LayerContract",
    "LayoutContract",
    "LinetypeContract",
    "PremiumContract",
    "SettingsContract",
    "SolidContract",
    "SystemContract",
    "TransactionContract",
    "ViewContract",
]
