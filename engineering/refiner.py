"""Bounded critique -> repair -> re-critique orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from .critique import CHECK_FAILED, SCAN_INCOMPLETE
from .plan_spec import Issue
from .repairs import (
    repair_construction_left,
    repair_dim_overlap,
    repair_duplicate_entities,
    repair_iso128,
    repair_layer_color,
    repair_untrimmed_corner,
)
from .scoring import score_findings

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

RepairHandler = Callable[["AutoCADBackend", Issue], Awaitable[dict]]

log = logging.getLogger(__name__)


@dataclass
class RepairAction:
    focus: str
    fingerprint: str
    handles: list[str]
    status: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "focus": self.focus,
            "fingerprint": self.fingerprint,
            "handles": list(self.handles),
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class RefineRound:
    number: int
    score_before: float
    score_after: float
    error_count_before: int
    error_count_after: int
    transaction: str
    actions: list[RepairAction]

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "error_count_before": self.error_count_before,
            "error_count_after": self.error_count_after,
            "transaction": self.transaction,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass
class RefineResult:
    status: str
    initial_score: float
    final_score: float
    rounds: list[RefineRound]
    remaining_issues: list[dict]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "initial_score": self.initial_score,
            "final_score": self.final_score,
            "rounds": [item.to_dict() for item in self.rounds],
            "remaining_issues": list(self.remaining_issues),
        }


class RepairRegistry:
    def __init__(self, *, defaults: bool = False):
        self._handlers: dict[str, RepairHandler] = {}
        if defaults:
            self.register("construction_left", repair_construction_left)
            self.register("duplicate_entities", repair_duplicate_entities)
            self.register("layer_color", repair_layer_color)
            self.register("iso128", repair_iso128)
            self.register("untrimmed_corner", repair_untrimmed_corner)
            self.register("dim_overlap", repair_dim_overlap)

    def register(self, focus: str, handler: RepairHandler) -> None:
        self._handlers[focus] = handler

    def get(self, focus: str) -> RepairHandler | None:
        return self._handlers.get(focus)


#: ``Issue.detail["code"]`` values that are not findings: a check that raised,
#: or a read that did not cover the drawing. They carry no handles and no
#: layer, so no repair handler can act on them - sent to one, it raised and the
#: whole round rolled back, the valid repairs beside it included (W1 review).
UNREPAIRABLE_CODES = frozenset({CHECK_FAILED, SCAN_INCOMPLETE})


def unrepairable_code(issue: Issue) -> str | None:
    """The issue's ``detail["code"]`` when it is one of ``UNREPAIRABLE_CODES``."""
    code = (issue.detail or {}).get("code")
    return code if code in UNREPAIRABLE_CODES else None


def issue_fingerprint(issue: Issue) -> str:
    payload = {
        "focus": issue.focus,
        "handles": sorted(issue.handles),
        "detail": issue.detail,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _metrics(issues: list[Issue]) -> tuple[float, int]:
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = sum(issue.severity == "warning" for issue in issues)
    info = sum(issue.severity == "info" for issue in issues)
    return float(score_findings(errors, warnings, info)["score"]), errors


async def refine_drawing(
    backend: AutoCADBackend,
    max_rounds: int = 3,
    min_score: float = 95.0,
    focus: list[str] | None = None,
    allowed_repairs: list[str] | None = None,
    dry_run: bool = False,
    *,
    registry: RepairRegistry | None = None,
) -> RefineResult:
    """Apply deterministic repairs without allowing quality to regress."""
    max_rounds = max(1, min(3, int(max_rounds)))
    min_score = max(0.0, min(100.0, float(min_score)))
    registry = registry or RepairRegistry(defaults=True)
    allowed = set(allowed_repairs) if allowed_repairs is not None else None

    issues = await backend.drawing_critique(focus)
    initial_score, _initial_errors = _metrics(issues)
    rounds: list[RefineRound] = []

    if initial_score >= min_score:
        return RefineResult(
            "threshold_met", initial_score, initial_score, [], [item.to_dict() for item in issues]
        )

    if dry_run:
        actions = []
        for issue in issues:
            code = unrepairable_code(issue)
            handler = registry.get(issue.focus) if code is None else None
            permitted = allowed is None or issue.focus in allowed
            actions.append(
                RepairAction(
                    issue.focus,
                    issue_fingerprint(issue),
                    list(issue.handles),
                    "planned" if handler is not None and permitted else "manual_required",
                    {"code": code} if code is not None else {},
                )
            )
        score, errors = _metrics(issues)
        rounds.append(RefineRound(1, score, score, errors, errors, "not_started", actions))
        return RefineResult("dry_run", initial_score, score, rounds, [i.to_dict() for i in issues])

    status = "max_rounds"
    for number in range(1, max_rounds + 1):
        score_before, errors_before = _metrics(issues)
        candidates: list[tuple[Issue, RepairHandler]] = []
        # A check_failed / scan_incomplete issue is reported, never repaired:
        # the round's real repairs go ahead beside it.
        manual: list[RepairAction] = []
        for issue in issues:
            code = unrepairable_code(issue)
            if code is not None:
                manual.append(
                    RepairAction(
                        issue.focus,
                        issue_fingerprint(issue),
                        list(issue.handles),
                        "manual_required",
                        {"code": code},
                    )
                )
                continue
            handler = registry.get(issue.focus)
            if handler is not None and (allowed is None or issue.focus in allowed):
                candidates.append((issue, handler))
        if not candidates:
            status = "manual_required"
            break

        # B01: the begin runs shielded, so its outcome is always known: a
        # cancelled begin's worker would still push the checkpoint (the lease
        # follows the worker) and nothing would close it. A cancellation that
        # arrived meanwhile is delivered by the checkpoint at the top of the
        # try below, whose handler rolls the round back.
        with anyio.CancelScope(shield=True):
            transaction = await backend.transaction_begin()
        actions: list[RepairAction] = list(manual)
        if not isinstance(transaction, dict) or not transaction.get("ok"):
            await anyio.lowlevel.checkpoint_if_cancelled()
            detail = transaction if isinstance(transaction, dict) else {"result": transaction}
            actions.append(RepairAction("internal", "", [], "failed", detail))
            rounds.append(
                RefineRound(
                    number,
                    score_before,
                    score_before,
                    errors_before,
                    errors_before,
                    "not_started",
                    actions,
                )
            )
            status = "transaction_unavailable"
            break
        try:
            await anyio.lowlevel.checkpoint_if_cancelled()
            for issue, handler in candidates:
                detail = await handler(backend, issue)
                actions.append(
                    RepairAction(
                        issue.focus,
                        issue_fingerprint(issue),
                        list(issue.handles),
                        "applied",
                        detail,
                    )
                )
            after = await backend.drawing_critique(focus)
            score_after, errors_after = _metrics(after)
        except BaseException as exc:
            # B01: a cancellation is a BaseException; `except Exception` let it
            # leave the round's repairs inside an open transaction. The
            # rollback is shielded (engineering/pid/spec.py explains why) and,
            # after anything but an ordinary error, a failed rollback is
            # logged rather than raised over the cancellation.
            with anyio.CancelScope(shield=True):
                try:
                    await backend.transaction_rollback()
                except Exception as rollback_exc:
                    if isinstance(exc, Exception):
                        raise
                    log.error(
                        "drawing_refine: rollback after %s failed (%s: %s); the round's "
                        "repairs may still be in the drawing - check system_status and "
                        "transaction_rollback before retrying",
                        type(exc).__name__,
                        type(rollback_exc).__name__,
                        rollback_exc,
                    )
            if not isinstance(exc, Exception):
                raise
            actions.append(RepairAction("internal", "", [], "failed", {"error": str(exc)}))
            rounds.append(
                RefineRound(
                    number,
                    score_before,
                    score_before,
                    errors_before,
                    errors_before,
                    "rolled_back",
                    actions,
                )
            )
            status = "repair_failed"
            issues = await backend.drawing_critique(focus)
            break

        if score_after < score_before or errors_after > errors_before:
            await backend.transaction_rollback()
            rounds.append(
                RefineRound(
                    number,
                    score_before,
                    score_before,
                    errors_before,
                    errors_before,
                    "rolled_back",
                    actions,
                )
            )
            status = "rolled_back"
            issues = await backend.drawing_critique(focus)
            break

        await backend.transaction_commit()
        rounds.append(
            RefineRound(
                number,
                score_before,
                score_after,
                errors_before,
                errors_after,
                "committed",
                actions,
            )
        )
        before_fingerprints = {issue_fingerprint(item) for item in issues}
        after_fingerprints = {issue_fingerprint(item) for item in after}
        issues = after
        if score_after >= min_score:
            status = "threshold_met"
            break
        if after_fingerprints == before_fingerprints:
            status = "no_progress"
            break

    final_score, _final_errors = _metrics(issues)
    return RefineResult(
        status,
        initial_score,
        final_score,
        rounds,
        [item.to_dict() for item in issues],
    )
