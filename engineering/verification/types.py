"""The shared verification types (v1.7.0, B15 residue), defined once for Tasks 11-17.

Frozen, hashable dataclasses. A mapping is stored as sorted ``(key, value)``
pairs (``freeze``) so a requirement can be a dict key or a set member, and
``to_dict`` gives the JSON form back (``thaw``). The vocabularies are closed:
an unknown status, quantity, provenance, selector kind, feature type or
binding status is a ``ValueError`` at construction, so a typo can never
become a silent pass.

A verdict (``Status``) is one of:

    pass, fail      measured on CAD-read geometry against the target
    unknown         no binding, or nothing it could be measured on
    unsupported     a geometry the evaluator cannot measure (a spline, a tilted plane)
    ambiguous       two equal candidates; never a pick

A missing requirement is never ``pass``. ``manufacturing_tolerance`` (with its
own source) and ``verification_epsilon`` (numeric noise) are separate fields
and are never merged.

Pure: no backend, no I/O.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, get_args

__all__ = [
    "BINDING_STATUSES",
    "FEATURE_TYPES",
    "PROVENANCES",
    "QUANTITIES",
    "SCHEMA_VERSION",
    "SELECTOR_KINDS",
    "STATUSES",
    "AcceptanceItem",
    "Binding",
    "BindingStatus",
    "FeatureRef",
    "FeatureSelector",
    "FeatureType",
    "FrozenMap",
    "Measurement",
    "ObservedFeature",
    "ObservedGeometry",
    "Provenance",
    "Quantity",
    "Requirement",
    "RequirementSet",
    "SelectorKind",
    "Status",
    "freeze",
    "thaw",
]

Status = Literal["pass", "fail", "unknown", "unsupported", "ambiguous"]
Provenance = Literal["explicit", "derived", "assumed", "conflicting", "unknown", "raster_estimate"]
Quantity = Literal[
    "distance",
    "radius",
    "diameter",
    "angle",
    "count",
    "absence",
    "concentric",
    "coincident",
    "parallel",
    "perpendicular",
    "tangent",
    "horizontal",
    "vertical",
]
SelectorKind = Literal["handle", "feature_tag", "arch_element", "predicate", "dimension"]
FeatureType = Literal[
    "line", "circle", "arc", "ellipse", "polyline", "point", "spline", "dimension"
]
BindingStatus = Literal["bound", "ambiguous", "unknown"]

STATUSES: tuple[str, ...] = get_args(Status)
PROVENANCES: tuple[str, ...] = get_args(Provenance)
QUANTITIES: tuple[str, ...] = get_args(Quantity)
SELECTOR_KINDS: tuple[str, ...] = get_args(SelectorKind)
FEATURE_TYPES: tuple[str, ...] = get_args(FeatureType)
BINDING_STATUSES: tuple[str, ...] = get_args(BindingStatus)

#: The version of ``RequirementSet.to_dict()`` (and so of its digest).
SCHEMA_VERSION = 1

Pairs = tuple[tuple[str, object], ...]


class FrozenMap(tuple):
    """A frozen mapping: sorted ``(key, value)`` pairs that ``thaw`` turns back into a dict.

    A tuple, so it hashes and compares like one; its own type is what lets
    ``thaw`` tell a nested mapping from a nested list.
    """

    __slots__ = ()


def _freeze_value(value):
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(_pairs(value))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _pairs(mapping: Mapping) -> Pairs:
    return tuple((str(key), _freeze_value(mapping[key])) for key in sorted(mapping, key=str))


def freeze(mapping: Mapping | None) -> Pairs:
    """``mapping`` as sorted pairs; nested mappings become ``FrozenMap``, lists tuples."""
    if mapping is None:
        return FrozenMap(())
    if not isinstance(mapping, Mapping):
        raise TypeError(f"freeze takes a mapping, got {type(mapping).__name__}")
    return FrozenMap(_pairs(mapping))


def _thaw_value(value):
    if isinstance(value, FrozenMap):
        return {key: _thaw_value(item) for key, item in value}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def thaw(pairs: Pairs | None) -> dict:
    """The dict ``freeze`` made ``pairs`` from (tuples come back as lists)."""
    return {str(key): _thaw_value(value) for key, value in (pairs or ())}


def _check(value, choices: tuple[str, ...], what: str) -> None:
    if value not in choices:
        raise ValueError(f"{what} must be one of {list(choices)}, got {value!r}")


@dataclass(frozen=True)
class FeatureSelector:
    kind: SelectorKind
    value: Pairs

    def __post_init__(self):
        _check(self.kind, SELECTOR_KINDS, "FeatureSelector.kind")

    def to_dict(self) -> dict:
        return {"kind": self.kind, **thaw(self.value)}


@dataclass(frozen=True)
class Requirement:
    id: str
    quantity: Quantity
    target: Pairs
    features: tuple[FeatureSelector, ...]
    provenance: Provenance
    source: Pairs
    datum: str | None = None
    manufacturing_tolerance: Pairs | None = None
    verification_epsilon: float | None = None

    def __post_init__(self):
        _check(self.quantity, QUANTITIES, "Requirement.quantity")
        _check(self.provenance, PROVENANCES, "Requirement.provenance")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "quantity": self.quantity,
            "target": thaw(self.target),
            "features": [selector.to_dict() for selector in self.features],
            "provenance": self.provenance,
            "source": thaw(self.source),
            "datum": self.datum,
            "manufacturing_tolerance": None
            if self.manufacturing_tolerance is None
            else thaw(self.manufacturing_tolerance),
            "verification_epsilon": self.verification_epsilon,
        }


@dataclass(frozen=True)
class RequirementSet:
    requirements: tuple[Requirement, ...]
    units: str
    legacy: Pairs | None = None

    def to_dict(self) -> dict:
        """The canonical form: items sorted by id, so the digest ignores input order."""
        return {
            "version": SCHEMA_VERSION,
            "units": self.units,
            "items": [r.to_dict() for r in sorted(self.requirements, key=lambda r: r.id)],
            "legacy": None if self.legacy is None else thaw(self.legacy),
        }

    def digest(self) -> str:
        """``sha256:`` + the hex digest of the canonical JSON of ``to_dict()``."""
        text = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def by_id(self) -> dict[str, Requirement]:
        return {requirement.id: requirement for requirement in self.requirements}


@dataclass(frozen=True)
class ObservedFeature:
    handle: str
    type: FeatureType
    space: str
    layer: str
    block_path: tuple[str, ...]
    geometry: Pairs
    tags: tuple[tuple[str, str], ...] = ()
    approximation: str | None = None

    def __post_init__(self):
        _check(self.type, FEATURE_TYPES, "ObservedFeature.type")

    def to_dict(self) -> dict:
        return {
            "handle": self.handle,
            "type": self.type,
            "space": self.space,
            "layer": self.layer,
            "block_path": list(self.block_path),
            "geometry": thaw(self.geometry),
            "tags": [[app, payload] for app, payload in self.tags],
            "approximation": self.approximation,
        }


@dataclass(frozen=True)
class ObservedGeometry:
    features: tuple[ObservedFeature, ...]
    declared_unit: str | None
    inferred_unit: str | None
    document_id: str | None
    revision: int | None
    source: str

    def to_dict(self) -> dict:
        return {
            "features": [feature.to_dict() for feature in self.features],
            "declared_unit": self.declared_unit,
            "inferred_unit": self.inferred_unit,
            "document_id": self.document_id,
            "revision": self.revision,
            "source": self.source,
        }


@dataclass(frozen=True)
class FeatureRef:
    document_id: str | None
    revision: int | None
    space: str
    block_path: tuple[str, ...]
    handles: tuple[str, ...]
    feature: str | None

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "revision": self.revision,
            "space": self.space,
            "block_path": list(self.block_path),
            "handles": list(self.handles),
            "feature": self.feature,
        }


@dataclass(frozen=True)
class Binding:
    requirement_id: str
    status: BindingStatus
    refs: tuple[FeatureRef, ...]
    candidates: tuple[FeatureRef, ...]
    reason: str

    def __post_init__(self):
        _check(self.status, BINDING_STATUSES, "Binding.status")

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "status": self.status,
            "refs": [ref.to_dict() for ref in self.refs],
            "candidates": [ref.to_dict() for ref in self.candidates],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Measurement:
    requirement_id: str
    value: float | None
    unit: str
    method: str
    residual: float | None
    status: Status
    detail: Pairs = ()

    def __post_init__(self):
        _check(self.status, STATUSES, "Measurement.status")

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "value": self.value,
            "unit": self.unit,
            "method": self.method,
            "residual": self.residual,
            "status": self.status,
            "detail": thaw(self.detail),
        }


@dataclass(frozen=True)
class AcceptanceItem:
    requirement: Requirement
    binding: Binding
    measurement: Measurement
    delta: float | None
    tolerance_basis: str

    def to_dict(self) -> dict:
        return {
            "requirement": self.requirement.to_dict(),
            "binding": self.binding.to_dict(),
            "measurement": self.measurement.to_dict(),
            "delta": self.delta,
            "tolerance_basis": self.tolerance_basis,
        }
