"""Pure, deterministic drawing-requirement preflight validation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from numbers import Real
from typing import Any


@dataclass(frozen=True)
class PreflightQuestion:
    code: str
    field: str
    question: str
    choices: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "field": self.field,
            "question": self.question,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class PreflightConflict:
    code: str
    field: str
    values: list[Any]
    message: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "field": self.field,
            "values": list(self.values),
            "message": self.message,
        }


@dataclass
class PreflightResult:
    ready: bool
    normalized_spec: dict
    questions: list[PreflightQuestion]
    conflicts: list[PreflightConflict]
    assumptions: list[dict]
    spec_hash: str

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "normalized_spec": self.normalized_spec,
            "questions": [item.to_dict() for item in self.questions],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "assumptions": list(self.assumptions),
            "spec_hash": self.spec_hash,
        }


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key).strip(): _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Real) and not isinstance(value, bool):
        return float(value)
    return value


def _spec_hash(spec: dict) -> str:
    encoded = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def preflight_drawing(
    intent: str,
    requirements: dict | None = None,
    sheet_size: str = "A3",
    scale: float = 1.0,
    layer_set_id: str = "mech",
    view_count: int = 1,
    dim_style: str = "chain",
    *,
    allow_assumptions: bool = False,
) -> PreflightResult:
    """Normalize a drawing request and surface missing or conflicting facts."""
    req = _normalize(dict(requirements or {}))
    assumptions: list[dict] = []

    if allow_assumptions and not req.get("units"):
        req["units"] = "mm"
        assumptions.append({"field": "units", "value": "mm", "reason": "default"})
    if allow_assumptions and not req.get("tolerance_policy"):
        req["tolerance_policy"] = "general"
        assumptions.append({"field": "tolerance_policy", "value": "general", "reason": "default"})

    if isinstance(req.get("units"), str):
        req["units"] = req["units"].lower()
    if isinstance(req.get("part_type"), str):
        req["part_type"] = req["part_type"].lower()

    questions: list[PreflightQuestion] = []
    required = (
        ("units", "MISSING_UNITS", "What drawing units should be used?", ["mm", "inch"]),
        (
            "part_type",
            "MISSING_PART_TYPE",
            "What kind of part or drawing is being produced?",
            [],
        ),
        ("dimensions", "MISSING_DIMENSIONS", "What are the main production dimensions?", []),
        (
            "tolerance_policy",
            "MISSING_TOLERANCE_POLICY",
            "Which tolerance policy should apply?",
            ["ISO 2768-m", "explicit", "none"],
        ),
    )
    for field_name, code, question, choices in required:
        value = req.get(field_name)
        if value is None or value == "" or value == {} or value == []:
            questions.append(PreflightQuestion(code, field_name, question, choices))

    conflicts: list[PreflightConflict] = []
    seen_constraints: dict[str, Any] = {}
    for constraint in req.get("constraints", []) or []:
        if not isinstance(constraint, dict):
            continue
        field_name = str(constraint.get("field", "")).strip()
        if not field_name or "value" not in constraint:
            continue
        value = _normalize(constraint["value"])
        if field_name in seen_constraints and seen_constraints[field_name] != value:
            conflicts.append(
                PreflightConflict(
                    "CONFLICTING_REQUIREMENT",
                    field_name,
                    [seen_constraints[field_name], value],
                    f"Conflicting values were supplied for {field_name}.",
                )
            )
        else:
            seen_constraints[field_name] = value

    normalized_sheet = str(sheet_size).strip().upper()
    normalized_layer_set = str(layer_set_id).strip().lower()
    normalized_dim_style = str(dim_style).strip().lower()
    try:
        normalized_scale = float(scale)
    except (TypeError, ValueError):
        normalized_scale = float("nan")
    try:
        normalized_view_count = int(view_count)
    except (TypeError, ValueError):
        normalized_view_count = 0

    parameter_rules = (
        (
            normalized_sheet in {"A4", "A3", "A2", "A1", "A0"},
            "INVALID_SHEET_SIZE",
            "sheet_size",
            sheet_size,
            "sheet_size must be one of A4, A3, A2, A1, A0.",
        ),
        (
            math.isfinite(normalized_scale) and normalized_scale > 0,
            "INVALID_SCALE",
            "scale",
            scale,
            "scale must be a finite number greater than zero.",
        ),
        (
            1 <= normalized_view_count <= 20,
            "INVALID_VIEW_COUNT",
            "view_count",
            view_count,
            "view_count must be between 1 and 20.",
        ),
        (
            normalized_dim_style in {"chain", "baseline", "ordinate", "mixed"},
            "INVALID_DIM_STYLE",
            "dim_style",
            dim_style,
            "dim_style must be chain, baseline, ordinate, or mixed.",
        ),
        (
            normalized_layer_set in {"iso13567", "mech", "pid"},
            "INVALID_LAYER_SET",
            "layer_set_id",
            layer_set_id,
            "layer_set_id must be iso13567, mech, or pid.",
        ),
    )
    for valid, code, field_name, value, message in parameter_rules:
        if not valid:
            conflicts.append(PreflightConflict(code, field_name, [value], message))

    normalized_spec = {
        "intent": str(intent).strip(),
        "requirements": req,
        "sheet_size": normalized_sheet,
        "scale": normalized_scale if math.isfinite(normalized_scale) else str(scale).strip(),
        "layer_set_id": normalized_layer_set,
        "view_count": normalized_view_count,
        "dim_style": normalized_dim_style,
    }
    return PreflightResult(
        ready=not questions and not conflicts,
        normalized_spec=normalized_spec,
        questions=questions,
        conflicts=conflicts,
        assumptions=assumptions,
        spec_hash=_spec_hash(normalized_spec),
    )
