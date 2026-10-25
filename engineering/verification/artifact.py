"""Saved-artifact verification: the requirements measured again on the file itself.

Spec §5 Task 17 (B21). ``verify_artifact`` reads the saved file fresh - a DXF
through ``ezdxf.readfile``, a DWG on the live engine through a private copy
that AutoCAD opens read-only, exports and closes - and never the model in
memory. Every requirement is measured again on that read.

With the source document at hand (``backend``), each requirement's features
are re-bound from the source binding, one feature at a time:

1. by handle - only while handles look stable (``diff_snapshots``'
   ``handles_stable``: at least one exact match and at least half of them kept
   their handle) and only onto a feature of the same type, space and
   ``ACADMCP_*`` tags; a save that renumbers handles makes a handle name a
   stranger, so this stage then does not run;
2. by feature tag - the same ``ACADMCP_*`` payload on exactly one feature of
   the same type;
3. by geometry - exactly one feature of the same type, space and nesting depth
   whose geometry equals the source's within the epsilon.

Two candidates at a stage - a copied feature tag, two identical holes - make
the requirement ``unknown`` with both candidates listed: never a pick. Nothing
found is ``unknown`` too. Without ``backend`` the requirements are bound afresh
on the file. The report carries the re-binding (the method per requirement)
and - with ``backend`` - the before / after comparison and the source document
as its evidence, so ``mark_stale`` can tell when the document moved after the
artifact was written.

The DWG route copies the file first because a fresh read must not be the
document AutoCAD already has open (``take_snapshot`` would export that one,
unsaved edits included). Whether AutoCAD lets another process copy a DWG it
holds open is the live measurement of Task 17's live step; a copy that fails
is an ``ArtifactReadError`` naming the reason, never a silent fallback.
"""

from __future__ import annotations

import asyncio
import math
import shutil
import tempfile
from pathlib import Path

from backends.capability import UnsupportedCapabilityError
from engineering.understand.diff import diff_snapshots
from engineering.understand.snapshot import Snapshot, dxf_version_of, read_snapshot, take_snapshot
from engineering.verification.binding import bind
from engineering.verification.measure import default_epsilon, evaluate
from engineering.verification.observe import observe
from engineering.verification.report import (
    BINDING_POLICIES,
    MeasuredRun,
    attach_dimension_evidence,
    build_report,
    dimension_evidence,
    mark_stale,
    measure_requirements,
    sha256_file,
)
from engineering.verification.types import Binding, FeatureRef, ObservedGeometry, RequirementSet

__all__ = ["ArtifactReadError", "rebind", "verify_artifact"]


class ArtifactReadError(ValueError):
    """A saved artifact could not be read fresh; the message says why."""


async def _fresh_snapshot(target: Path, backend) -> tuple[Snapshot, str]:
    """``(snapshot, how it was read)`` of the file at ``target``, never of a model in memory."""
    suffix = target.suffix.lower()
    if suffix == ".dxf":
        return await asyncio.to_thread(
            read_snapshot, str(target), block_defs=True
        ), "ezdxf.readfile"
    if suffix != ".dwg":
        raise ValueError(f"{target}: a saved artifact is a .dxf or a .dwg, not {suffix or 'this'}")
    if backend is None or backend.name != "com":
        raise UnsupportedCapabilityError(
            "dwg",
            f"{target} is a DWG, which the headless engine cannot read. Verify the drawing's "
            ".dxf copy, or run the server on the live engine (AUTOCAD_MCP_BACKEND=com).",
        )
    folder = tempfile.mkdtemp(prefix="acadmcp_verify_")
    try:
        copy = Path(folder) / target.name
        try:
            await asyncio.to_thread(shutil.copy2, target, copy)
        except OSError as exc:
            raise ArtifactReadError(
                f"{target}: could not take a private copy of the saved file to read it fresh "
                f"({exc}); AutoCAD may hold it locked - save a .dxf copy and verify that"
            ) from exc
        snap = await take_snapshot(backend, str(copy), block_defs=True)
        return snap, "private copy, Documents.Open read-only + Document.Export"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _same(a, b, eps: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        fa, fb = float(a), float(b)
        return math.isfinite(fa) and math.isfinite(fb) and abs(fa - fb) <= eps
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        return len(a) == len(b) and all(_same(x, y, eps) for x, y in zip(a, b, strict=True))
    return a == b


def _same_geometry(f, g, eps: float) -> bool:
    return (
        f.type == g.type
        and f.space == g.space
        and len(f.block_path) == len(g.block_path)
        and _same(f.geometry, g.geometry, eps)
    )


def _rebind_feature(f, after: ObservedGeometry, *, handles_stable: bool, by_key: dict, eps: float):
    """``(feature or None, method, candidates)`` for one source feature."""
    if handles_stable:
        g = by_key.get((f.handle, f.block_path))
        if g is not None and g.type == f.type and g.space == f.space and g.tags == f.tags:
            return g, "handle", ()
    if f.tags:
        tagged = [g for g in after.features if g.tags == f.tags and g.type == f.type]
        if len(tagged) == 1:
            return tagged[0], "tag", ()
        if len(tagged) > 1:
            return None, "tag", tuple(tagged)
    shaped = [g for g in after.features if _same_geometry(f, g, eps)]
    if len(shaped) == 1:
        return shaped[0], "geometry", ()
    if len(shaped) > 1:
        return None, "geometry", tuple(shaped)
    return None, "none", ()


def _ref_of(feature) -> FeatureRef:
    return FeatureRef(
        document_id=None,
        revision=None,
        space=feature.space,
        block_path=feature.block_path,
        handles=(feature.handle,),
        feature=None,
    )


def rebind(
    rs: RequirementSet,
    before: MeasuredRun | None,
    after_snap: Snapshot,
    after_observed: ObservedGeometry,
    *,
    binding_policy: str = "strict",
) -> tuple[tuple[Binding, ...], list[dict], bool | None]:
    """``(bindings on the saved file, one re-binding row per requirement, handles_stable)``."""
    fresh = {b.requirement_id: b for b in bind(rs, after_observed, policy=binding_policy)}
    bindings: list[Binding] = []
    rows: list[dict] = []
    if before is None:
        for requirement in rs.requirements:
            binding = fresh.get(requirement.id)
            if binding is not None:
                bindings.append(binding)
            rows.append(
                {
                    "id": requirement.id,
                    "method": "fresh",
                    "status": binding.status if binding is not None else "unknown",
                    "reason": binding.reason if binding is not None else "no binding produced",
                }
            )
        return tuple(bindings), rows, None
    handles_stable = bool(diff_snapshots(before.snapshot, after_snap)["handles_stable"])
    by_key = {(g.handle, g.block_path): g for g in after_observed.features}
    before_by_key = {(f.handle, f.block_path): f for f in before.observed.features}
    eps = default_epsilon(after_observed)
    prior_bindings = {b.requirement_id: b for b in before.bindings}
    for requirement in rs.requirements:
        prior = prior_bindings.get(requirement.id)
        if prior is None or prior.status != "bound":
            binding = fresh.get(requirement.id)
            if binding is not None:
                bindings.append(binding)
            rows.append(
                {
                    "id": requirement.id,
                    "method": "fresh",
                    "status": binding.status if binding is not None else "unknown",
                    "reason": "the source binding was not bound; bound afresh on the file",
                }
            )
            continue
        refs: list[FeatureRef] = []
        methods: set[str] = set()
        problem = None
        candidates: list = []
        for ref in prior.refs:
            if not ref.handles:
                refs.append(ref)
                continue
            matched = []
            for handle in ref.handles:
                feature = before_by_key.get((handle, ref.block_path))
                if feature is None:
                    problem = f"feature {handle} of the source is not in its observed geometry"
                    break
                found, how, cands = _rebind_feature(
                    feature, after_observed, handles_stable=handles_stable, by_key=by_key, eps=eps
                )
                methods.add(how)
                if found is None:
                    candidates.extend(cands)
                    problem = (
                        f"{len(cands)} features of the saved file match {handle} by {how}"
                        if cands
                        else f"no feature of the saved file matches {handle}"
                    )
                    break
                matched.append(found)
            if problem is not None:
                break
            refs.append(
                FeatureRef(
                    document_id=None,
                    revision=None,
                    space=matched[0].space,
                    block_path=matched[0].block_path,
                    handles=tuple(g.handle for g in matched),
                    feature=ref.feature,
                )
            )
        if problem is not None:
            binding = Binding(
                requirement_id=requirement.id,
                status="unknown",
                refs=(),
                candidates=tuple(_ref_of(g) for g in candidates),
                reason=f"re-binding on the saved file failed: {problem}",
            )
        else:
            binding = Binding(
                requirement_id=requirement.id,
                status="bound",
                refs=tuple(refs),
                candidates=(),
                reason=f"re-bound on the saved file by {', '.join(sorted(methods))}",
            )
        bindings.append(binding)
        rows.append(
            {
                "id": requirement.id,
                "method": "+".join(sorted(methods)) or "none",
                "status": binding.status,
                "reason": binding.reason,
            }
        )
    return tuple(bindings), rows, handles_stable


def _compare(before: dict, after: dict) -> list[dict]:
    """Per requirement: the status and value on the document and on the saved file."""
    after_rows = {row["id"]: row for row in after["rows"]}
    out = []
    for row in before["rows"]:
        other = after_rows.get(row["id"])
        if other is None:
            continue
        old = (row["measured"] or {}).get("value")
        new = (other["measured"] or {}).get("value")
        moved = (old is None) != (new is None) or (
            old is not None and new is not None and abs(new - old) > 1e-9 * max(1.0, abs(old))
        )
        out.append(
            {
                "id": row["id"],
                "before": {"status": row["status"], "value": old},
                "after": {"status": other["status"], "value": new},
                "changed": row["status"] != other["status"] or moved,
            }
        )
    return out


async def verify_artifact(
    path: str, rs: RequirementSet, *, backend=None, binding_policy: str = "strict"
) -> dict:
    """The acceptance report of ``rs`` measured on the saved file at ``path``.

    Refuses a missing file (FileNotFoundError), a suffix other than .dxf / .dwg
    (ValueError), a DWG headlessly (``dwg`` capability) and a DWG the live
    engine cannot copy (``ArtifactReadError``). An unreadable DXF is refused by
    ``read_snapshot`` with the reason.
    """
    if binding_policy not in BINDING_POLICIES:
        raise ValueError(
            f"binding_policy must be one of {', '.join(BINDING_POLICIES)}; got {binding_policy!r}"
        )
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"{path}: no such file")
    sha = await asyncio.to_thread(sha256_file, target)
    after_snap, read_method = await _fresh_snapshot(target, backend)
    document = {
        "document_id": None,
        "revision": None,
        "name": target.name,
        "path": str(target),
        "backend": "file",
    }
    before = None
    if backend is not None:
        before = await measure_requirements(backend, rs, binding_policy=binding_policy)

    def _work():
        observed = observe(after_snap, explode_inserts=True, document=document)
        bindings, rebinding, stable = rebind(
            rs, before, after_snap, observed, binding_policy=binding_policy
        )
        by_id = {binding.requirement_id: binding for binding in bindings}
        # as report._measure: the evaluator applies a requirement's own
        # verification_epsilon (in the requirement's unit) itself, and ``units``
        # is the set's unit for a target that names none. ``epsilon=`` is a
        # drawing-unit override and must not receive the requirement's value.
        measurements = tuple(
            evaluate(requirement, by_id[requirement.id], observed, units=rs.units)
            for requirement in rs.requirements
            if requirement.id in by_id
        )
        dimensions = (
            dimension_evidence(rs, after_snap, observed) if before is None or stable else {}
        )
        return observed, bindings, measurements, rebinding, stable, dimensions

    observed, bindings, measurements, rebinding, stable, dimensions = await asyncio.to_thread(_work)
    record = {
        "path": str(target),
        "sha256": sha,
        "size_bytes": target.stat().st_size,
        "read": read_method,
        "dxf_version": dxf_version_of(target) if target.suffix.lower() == ".dxf" else None,
    }
    report = build_report(
        rs, bindings, measurements, observed=observed, document=document, artifact=record
    )
    report = attach_dimension_evidence(report, dimensions)
    report["rebinding"] = {"handles_stable": stable, "rows": rebinding}
    if before is not None:
        before_report = build_report(
            rs,
            before.bindings,
            before.measurements,
            observed=before.observed,
            document=before.document,
        )
        report["comparison"] = _compare(before_report, report)
        report["source_document"] = dict(before.document)
        report["evidence"] = {
            **report["evidence"],
            "document_id": before.document.get("document_id"),
            "revision": before.document.get("revision"),
        }
    else:
        report["comparison"] = None
        report["source_document"] = None
    return mark_stale(report, None)
