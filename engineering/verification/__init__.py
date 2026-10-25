"""Verification engine (v1.7.0): requirements bound to CAD features, measured, reported.

Task 11 lands the shared types (``types``) and the requirement parser and
store (``requirements``); ``observe``, ``binding``, ``measure``, ``report``
and ``artifact`` join in Tasks 13-17.
"""

from engineering.verification.requirements import (
    RequirementError,
    decode_requirements,
    encode_requirements,
    load_requirements,
    parse_requirements,
    requirement_conflicts,
    store_requirements,
)
from engineering.verification.types import (
    BINDING_STATUSES,
    FEATURE_TYPES,
    PROVENANCES,
    QUANTITIES,
    SCHEMA_VERSION,
    SELECTOR_KINDS,
    STATUSES,
    AcceptanceItem,
    Binding,
    FeatureRef,
    FeatureSelector,
    Measurement,
    ObservedFeature,
    ObservedGeometry,
    Requirement,
    RequirementSet,
    freeze,
    thaw,
)

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
    "FeatureRef",
    "FeatureSelector",
    "Measurement",
    "ObservedFeature",
    "ObservedGeometry",
    "Requirement",
    "RequirementSet",
    "freeze",
    "thaw",
    "RequirementError",
    "decode_requirements",
    "encode_requirements",
    "load_requirements",
    "parse_requirements",
    "requirement_conflicts",
    "store_requirements",
]
