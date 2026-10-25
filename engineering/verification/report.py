"""Acceptance report: every requirement measured on the CAD geometry, with its evidence.

Spec §5 Task 16 (B22, F02). One row per requirement - target, measured value,
delta, tolerance basis, status, the handles it was measured on and where the
requirement came from - and the denominators ``required`` / ``pass`` /
``fail`` / ``unknown`` / ``unsupported`` / ``ambiguous``. The five statuses
always add up to ``required``.

No silent pass:

* a requirement with no binding or no measurement is ``unknown``
  (``kind: missing_requirement``), whatever else was produced;
* a binding that found nothing is ``unknown`` even when an evaluator said
  ``pass``;
* a binding with two equal candidates is ``ambiguous``
  (``kind: ambiguous_binding``) - never a pick;
* a measurement the evaluators cannot make (a spline, a tilted plane) is
  ``unsupported`` (``kind: unsupported_measurement``).

The report as a whole is ``pass`` only when every requirement passes, ``fail``
when any fails, ``incomplete`` otherwise and ``no_requirements`` for a set
without a typed requirement (a legacy ``dimensions`` dict is carried read-only
and verifies nothing). ``accepted`` is true only for a ``pass`` report that is
not stale and carries no ``error`` finding; ``blocking`` says why it is not. A
DIMENSION whose displayed text or own measurement disagrees with the geometry
it annotates (a text override, a DIMLFAC) is an ``error`` finding
``dimension_mismatch``: the geometry decides the row, the finding keeps the
annotation honest.

Evidence is stale when what it was measured on moved: :func:`mark_stale`
compares the report's evidence (the document id and content revision at the
time of measurement, or an artifact's sha256) with the present.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from engineering.understand.snapshot import Snapshot, take_snapshot
from engineering.verification.binding import bind, bind_dimension
from engineering.verification.measure import default_epsilon, evaluate
from engineering.verification.observe import observe
from engineering.verification.types import (
    AcceptanceItem,
    Binding,
    FeatureRef,
    Measurement,
    ObservedGeometry,
    Requirement,
    RequirementSet,
    thaw,
)

__all__ = [
    "AMBIGUOUS_BINDING",
    "BINDING_POLICIES",
    "DIMENSION_MISMATCH",
    "MISSING_REQUIREMENT",
    "SCHEMA_VERSION",
    "STATUSES",
    "UNSUPPORTED_MEASUREMENT",
    "MeasuredRun",
    "acceptance_items",
    "add_finding",
    "attach_dimension_evidence",
    "build_report",
    "dimension_evidence",
    "document_identity_of",
    "mark_stale",
    "measure_requirements",
    "render_markdown",
    "sha256_file",
    "summarize",
    "verify_requirements",
]

SCHEMA_VERSION = 1
STATUSES = ("pass", "fail", "unknown", "unsupported", "ambiguous")
BINDING_POLICIES = ("strict", "lenient")
MISSING_REQUIREMENT = "missing_requirement"
AMBIGUOUS_BINDING = "ambiguous_binding"
UNSUPPORTED_MEASUREMENT = "unsupported_measurement"
DIMENSION_MISMATCH = "dimension_mismatch"


def sha256_file(path) -> str:
    """The sha256 of a file, read in 1 MB chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value):
    """``value`` as plain JSON: tuples become lists, dataclasses dicts, NaN a string."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return str(value)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _ref_dict(ref: FeatureRef) -> dict:
    return {
        "space": ref.space,
        "block_path": list(ref.block_path),
        "handles": list(ref.handles),
        "feature": ref.feature,
    }


def _row_status(
    requirement: Requirement, binding: Binding | None, measurement: Measurement | None
) -> tuple[str, str | None, str]:
    """``(status, kind, reason)`` of one row - the no-silent-pass rules."""
    if (
        binding is None
        or measurement is None
        or binding.requirement_id != requirement.id
        or measurement.requirement_id != requirement.id
    ):
        return (
            "unknown",
            MISSING_REQUIREMENT,
            "no binding or no measurement was produced for this requirement",
        )
    if binding.status == "ambiguous":
        return "ambiguous", AMBIGUOUS_BINDING, binding.reason
    if binding.status != "bound":
        return "unknown", MISSING_REQUIREMENT, binding.reason
    if measurement.status == "unsupported":
        return (
            "unsupported",
            UNSUPPORTED_MEASUREMENT,
            f"{measurement.method} is not a measurement the evaluators make",
        )
    if measurement.status == "ambiguous":
        return "ambiguous", AMBIGUOUS_BINDING, binding.reason
    if measurement.status in ("pass", "fail"):
        return measurement.status, None, binding.reason
    return "unknown", None, f"the evaluator could not decide ({measurement.method})"


def _delta(requirement: Requirement, measurement: Measurement | None, status: str):
    """measured - target, for a measured row (pass or fail) only."""
    if status not in ("pass", "fail") or measurement is None:
        return None
    value = _number(measurement.value)
    target = dict(requirement.target)
    reference = _number(target.get("nominal"))
    if reference is None:
        reference = _number(target.get("exact"))
    if value is None or reference is None:
        return None
    return value - reference


def _tolerance_basis(requirement: Requirement) -> str:
    if requirement.manufacturing_tolerance:
        return "manufacturing_tolerance"
    return "verification_epsilon"


def acceptance_items(rs: RequirementSet, bindings, measurements) -> tuple[AcceptanceItem, ...]:
    """One ``AcceptanceItem`` per requirement; a missing binding or measurement
    is filled with an explicit ``unknown`` placeholder, never left out."""
    by_binding = {b.requirement_id: b for b in bindings}
    by_measure = {m.requirement_id: m for m in measurements}
    items = []
    for requirement in rs.requirements:
        binding = by_binding.get(requirement.id)
        measurement = by_measure.get(requirement.id)
        status, _kind, _reason = _row_status(requirement, binding, measurement)
        if binding is None:
            binding = Binding(
                requirement_id=requirement.id,
                status="unknown",
                refs=(),
                candidates=(),
                reason="no binding was produced for this requirement",
            )
        if measurement is None:
            measurement = Measurement(
                requirement_id=requirement.id,
                value=None,
                unit=str(dict(requirement.target).get("unit", "")),
                method="none",
                residual=None,
                status="unknown",
            )
        items.append(
            AcceptanceItem(
                requirement=requirement,
                binding=binding,
                measurement=measurement,
                delta=_delta(requirement, measurement, status),
                tolerance_basis=_tolerance_basis(requirement),
            )
        )
    return tuple(items)


def _row(requirement: Requirement, binding: Binding | None, measurement: Measurement | None):
    status, kind, reason = _row_status(requirement, binding, measurement)
    refs = binding.refs if binding is not None else ()
    tolerance = requirement.manufacturing_tolerance
    return {
        "id": requirement.id,
        "quantity": requirement.quantity,
        "target": _jsonable(thaw(requirement.target)),
        "measured": None
        if measurement is None
        else {
            "value": _number(measurement.value),
            "unit": measurement.unit,
            "method": measurement.method,
            "residual": _number(measurement.residual),
            "detail": _jsonable(thaw(measurement.detail)),
        },
        "delta": _delta(requirement, measurement, status),
        "tolerance_basis": _tolerance_basis(requirement),
        "tolerance": {
            "manufacturing_tolerance": _jsonable(thaw(tolerance)) if tolerance else None,
            "verification_epsilon": requirement.verification_epsilon,
        },
        "status": status,
        "kind": kind,
        "reason": reason,
        "binding": None
        if binding is None
        else {
            "status": binding.status,
            "reason": binding.reason,
            "candidates": [_ref_dict(ref) for ref in binding.candidates],
        },
        "features": [_ref_dict(ref) for ref in refs],
        "handles": sorted({handle for ref in refs for handle in ref.handles}),
        "source": _jsonable(thaw(requirement.source)),
        "provenance": requirement.provenance,
        "datum": requirement.datum,
        "stale": False,
    }


def _overall(counts: dict) -> str:
    if counts["required"] == 0:
        return "no_requirements"
    if counts["fail"]:
        return "fail"
    if counts["pass"] == counts["required"]:
        return "pass"
    return "incomplete"


def _settle(report: dict) -> dict:
    """Recompute ``blocking`` and ``accepted`` from the rows, findings and staleness."""
    blocking: list[str] = []
    if report["status"] == "no_requirements":
        blocking.append("no_requirements: the set holds no typed requirement")
    blocking.extend(
        f"{row['status']}: {row['id']}" for row in report["rows"] if row["status"] != "pass"
    )
    blocking.extend(
        f"{finding['kind']}: {finding['requirement']}"
        for finding in report["findings"]
        if finding.get("severity") == "error"
    )
    if report["stale"]:
        blocking.append(f"stale: {report['stale_reason']}")
    report["blocking"] = blocking
    report["accepted"] = report["status"] == "pass" and not blocking
    return report


def build_report(rs, bindings, measurements, *, observed, document, artifact=None) -> dict:
    """The acceptance report (JSON, ``schema_version`` 1) of one measurement run."""
    by_binding = {b.requirement_id: b for b in bindings}
    by_measure = {m.requirement_id: m for m in measurements}
    rows = [
        _row(requirement, by_binding.get(requirement.id), by_measure.get(requirement.id))
        for requirement in rs.requirements
    ]
    counts = {"required": len(rows), **{status: 0 for status in STATUSES}}
    for row in rows:
        counts[row["status"]] += 1
    measured = counts["pass"] + counts["fail"]
    identity = dict(document or {})
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": _overall(counts),
        "accepted": False,
        "stale": False,
        "stale_reason": None,
        "counts": counts,
        "coverage": {
            "required": counts["required"],
            "measured": measured,
            "ratio": measured / counts["required"] if counts["required"] else None,
        },
        "rows": rows,
        "findings": [],
        "blocking": [],
        "document": _jsonable(identity),
        "evidence": {
            "document_id": identity.get("document_id"),
            "revision": identity.get("revision"),
            "source": observed.source,
            "measured_at": datetime.now(UTC).isoformat(),
        },
        "units": {
            "requirements": rs.units,
            "declared": observed.declared_unit,
            "inferred": observed.inferred_unit,
        },
        "requirements_digest": rs.digest(),
        "legacy": _jsonable(thaw(rs.legacy)) if rs.legacy else None,
        "artifact": _jsonable(dict(artifact)) if artifact else None,
    }
    return _settle(report)


def add_finding(
    report: dict, *, kind: str, requirement: str, message: str, severity: str = "error"
) -> dict:
    """A copy of ``report`` with one more finding; an ``error`` finding blocks acceptance."""
    out = dict(report)
    out["findings"] = [
        *report["findings"],
        {"requirement": requirement, "kind": kind, "severity": severity, "message": message},
    ]
    return _settle(out)


def attach_dimension_evidence(report: dict, evidence: dict[str, dict]) -> dict:
    """Each row's DIMENSION evidence (``bind_dimension``), and a ``dimension_mismatch``
    finding where the dimension's text or own measurement disagrees with the geometry."""
    out = dict(report)
    out["rows"] = [dict(row) for row in report["rows"]]
    out["findings"] = list(report["findings"])
    for row in out["rows"]:
        item = evidence.get(row["id"])
        if item is None:
            continue
        row["dimension"] = _jsonable(dict(item))
        if item.get("mismatch"):
            out = add_finding(
                out,
                kind=DIMENSION_MISMATCH,
                requirement=row["id"],
                message=(
                    f"dimension {item.get('handle')} displays {item.get('displayed_text')!r}; "
                    f"its own measurement is {item.get('dimension_measurement')} and the "
                    f"geometry measures {item.get('geometric_value')}"
                ),
            )
    return _settle(out)


def mark_stale(report: dict, identity: dict | None) -> dict:
    """A copy of ``report``, stale when what it was measured on has moved.

    ``identity`` is the document as it is now (``document_identity_of``); None
    skips that comparison. An artifact's file is re-hashed whenever the report
    names one.
    """
    reasons: list[str] = []
    evidence = report.get("evidence") or {}
    if identity is not None and evidence.get("document_id") is not None:
        if identity.get("document_id") != evidence["document_id"]:
            reasons.append(
                f"measured on document {evidence['document_id']}; the current document is "
                f"{identity.get('document_id')}"
            )
        elif identity.get("revision") != evidence.get("revision"):
            reasons.append(
                f"the evidence was measured at revision {evidence.get('revision')} and the "
                f"document is at revision {identity.get('revision')}"
            )
    artifact = report.get("artifact") or {}
    if artifact.get("path") and artifact.get("sha256"):
        path = Path(artifact["path"])
        if not path.is_file():
            reasons.append(f"the artifact {path.name} is gone")
        elif sha256_file(path) != artifact["sha256"]:
            reasons.append(f"the artifact {path.name} changed after it was measured")
    out = dict(report)
    out["rows"] = [dict(row, stale=bool(reasons)) for row in report["rows"]]
    out["stale"] = bool(reasons)
    out["stale_reason"] = "; ".join(reasons) or None
    return _settle(out)


def summarize(report: dict) -> dict:
    """The verdict without the rows - what a manifest or a finalize payload carries."""
    return {
        "status": report["status"],
        "accepted": report["accepted"],
        "stale": report["stale"],
        "stale_reason": report["stale_reason"],
        "counts": dict(report["counts"]),
        "coverage": dict(report["coverage"]),
        "blocking": list(report["blocking"]),
        "requirements_digest": report["requirements_digest"],
    }


def _fmt(value) -> str:
    number = _number(value)
    if number is not None:
        return format(number, ".6g")
    return "-" if value is None else str(value)


def _signed(value) -> str:
    number = _number(value)
    return "-" if number is None else format(number, "+.6g")


def _target_text(target: dict) -> str:
    unit = str(target.get("unit") or "")
    if target.get("nominal") is not None:
        return f"{_fmt(target['nominal'])} {unit}".strip()
    if target.get("exact") is not None:
        return f"exactly {_fmt(target['exact'])}"
    low, high = target.get("min"), target.get("max")
    if low is not None and high is not None:
        return f"{_fmt(low)} to {_fmt(high)}"
    if low is not None:
        return f"at least {_fmt(low)}"
    if high is not None:
        return f"at most {_fmt(high)}"
    return "-"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _markdown_row(row: dict) -> str:
    measured = row["measured"]
    source = ", ".join(f"{key}={value}" for key, value in row["source"].items()) or "-"
    cells = [
        row["id"],
        row["quantity"],
        _target_text(row["target"]),
        _fmt(measured["value"]) if measured else "-",
        _signed(row["delta"]),
        row["tolerance_basis"],
        row["status"],
        ", ".join(row["handles"]) or "-",
        source,
    ]
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def render_markdown(report: dict) -> str:
    """The report as Markdown: the verdict, the denominators, one table row per requirement."""
    counts = report["counts"]
    lines = [
        "# Acceptance report",
        "",
        f"Status: **{report['status']}** - accepted: **{'yes' if report['accepted'] else 'no'}**",
        "",
        "| required | pass | fail | unknown | unsupported | ambiguous |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {counts['required']} | {counts['pass']} | {counts['fail']} | {counts['unknown']} "
        f"| {counts['unsupported']} | {counts['ambiguous']} |",
        "",
        "| id | quantity | target | measured | delta | basis | status | handles | source |",
        "|---|---|---|---:|---:|---|---|---|---|",
    ]
    lines.extend(_markdown_row(row) for row in report["rows"])
    if report["stale"]:
        lines += ["", f"**Stale evidence:** {report['stale_reason']}"]
    if report["findings"]:
        lines += ["", "## Findings", ""]
        lines.extend(
            f"- {finding['kind']} ({finding['requirement']}): {finding['message']}"
            for finding in report["findings"]
        )
    if report["blocking"]:
        lines += ["", "## Not accepted because", ""]
        lines.extend(f"- {reason}" for reason in report["blocking"])
    return "\n".join(lines) + "\n"


async def document_identity_of(backend) -> dict:
    """``backend.document_identity()`` as a plain dict. The skeleton pins it
    synchronous; an awaitable is awaited, so either landing works."""
    value = backend.document_identity()
    if inspect.isawaitable(value):
        value = await value
    return dict(value or {})


def dimension_evidence(rs: RequirementSet, snap: Snapshot, observed: ObservedGeometry) -> dict:
    """``{requirement id: bind_dimension evidence}`` for each requirement that names
    a DIMENSION (a ``dimension`` selector carrying its ``handle``)."""
    records = {rec.handle: rec for rec in snap.records if rec.type == "DIMENSION"}
    out: dict[str, dict] = {}
    for requirement in rs.requirements:
        for selector in requirement.features:
            if selector.kind != "dimension":
                continue
            handle = dict(selector.value).get("handle")
            record = records.get(str(handle)) if handle is not None else None
            if record is None:
                continue
            # drawing units, the binder's own epsilon: a requirement's
            # verification_epsilon is in the requirement's unit and is the
            # evaluator's to apply, never a defpoint-matching tolerance
            item = dict(bind_dimension(record, observed, epsilon=default_epsilon(observed)))
            item["handle"] = record.handle
            out[requirement.id] = item
            break
    return out


@dataclass(frozen=True)
class MeasuredRun:
    """One read, binding and measurement of a requirement set."""

    snapshot: Snapshot
    observed: ObservedGeometry
    bindings: tuple[Binding, ...]
    measurements: tuple[Measurement, ...]
    document: dict
    artifact: dict | None
    dimensions: dict


def _measure(rs, snap, document, binding_policy):
    observed = observe(snap, explode_inserts=True, document=document)
    bindings = tuple(bind(rs, observed, policy=binding_policy))
    by_id = {binding.requirement_id: binding for binding in bindings}
    # the evaluator applies a requirement's own verification_epsilon (in the
    # requirement's unit) itself; `units` is the set's unit for a target that names none
    measurements = tuple(
        evaluate(requirement, by_id[requirement.id], observed, units=rs.units)
        for requirement in rs.requirements
        if requirement.id in by_id
    )
    return observed, bindings, measurements, dimension_evidence(rs, snap, observed)


async def measure_requirements(
    backend, rs: RequirementSet, *, binding_policy: str = "strict", artifact_path=None
) -> MeasuredRun:
    """Read the drawing once (the current document, or the file at ``artifact_path``),
    observe it, bind every requirement and measure it. The arithmetic runs off the
    event loop."""
    if binding_policy not in BINDING_POLICIES:
        raise ValueError(
            f"binding_policy must be one of {', '.join(BINDING_POLICIES)}; got {binding_policy!r}"
        )
    if artifact_path is None:
        document = await document_identity_of(backend)
        snap = await take_snapshot(backend, block_defs=True)
        artifact = None
    else:
        target = Path(artifact_path)
        snap = await take_snapshot(backend, str(target), block_defs=True)
        document = {
            "document_id": None,
            "revision": None,
            "name": target.name,
            "path": str(target),
            "backend": "file",
        }
        artifact = {
            "path": str(target),
            "sha256": await asyncio.to_thread(sha256_file, target),
            "size_bytes": target.stat().st_size,
        }
    observed, bindings, measurements, dimensions = await asyncio.to_thread(
        _measure, rs, snap, document, binding_policy
    )
    return MeasuredRun(
        snapshot=snap,
        observed=observed,
        bindings=bindings,
        measurements=measurements,
        document=document,
        artifact=artifact,
        dimensions=dimensions,
    )


async def verify_requirements(backend, rs, *, binding_policy="strict", artifact_path=None) -> dict:
    """Measure ``rs`` and report it. On the current document the evidence is the
    document's id and revision, checked again after the measurement (an edit that
    landed meanwhile makes the report stale); with ``artifact_path`` it is the
    file's sha256."""
    run = await measure_requirements(
        backend, rs, binding_policy=binding_policy, artifact_path=artifact_path
    )
    report = build_report(
        rs,
        run.bindings,
        run.measurements,
        observed=run.observed,
        document=run.document,
        artifact=run.artifact,
    )
    report = attach_dimension_evidence(report, run.dimensions)
    identity = None if artifact_path is not None else await document_identity_of(backend)
    return mark_stale(report, identity)
