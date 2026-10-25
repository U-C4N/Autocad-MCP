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
on the file.

What a selector defines is resolved again on the file, never carried over: a
``count`` / ``absence`` requirement is counted afresh on the saved file (the
source's count cannot see a feature the file added), and a requirement whose
selectors name no handle (predicate, feature tag) is re-bound *and* resolved
afresh - a disagreement is ``unknown`` with both listed. A handle-naming
selector on a file that renumbered handles cannot be resolved afresh, so such a
requirement is ``unknown`` unless re-bound from the source. A requirement's
DIMENSION is re-bound the same way and ``bind_dimension`` runs on the file's
own; when it cannot be found again the report carries a blocking
``dimension_unverified`` finding - the annotation check is never dropped. The report carries the re-binding (the method per requirement)
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
from engineering.verification.binding import bind, bind_dimension
from engineering.verification.measure import default_epsilon, evaluate
from engineering.verification.observe import observe
from engineering.verification.report import (
    BINDING_POLICIES,
    DIMENSION_UNVERIFIED,
    MeasuredRun,
    add_finding,
    attach_dimension_evidence,
    build_report,
    dimension_evidence,
    mark_stale,
    measure_requirements,
    sha256_file,
)
from engineering.verification.types import Binding, FeatureRef, ObservedGeometry, RequirementSet

__all__ = ["ArtifactReadError", "rebind", "rebound_dimension_evidence", "verify_artifact"]

_COUNTING = ("count", "absence")
#: selector kinds that resolve without naming a handle
_HANDLE_FREE = frozenset({"predicate", "feature_tag"})


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


def _keys_of_refs(refs) -> set:
    return {
        (ref.space, tuple(ref.block_path), handle.upper()) for ref in refs for handle in ref.handles
    }


def _keys_of_features(features) -> set:
    return {(g.space, tuple(g.block_path), g.handle.upper()) for g in features}


def _handle_free(requirement) -> bool:
    """True when every selector resolves without naming a handle (a predicate, a
    feature tag): resolved afresh on the saved file, it means the same thing."""
    return all(selector.kind in _HANDLE_FREE for selector in requirement.features)


def _refs_text(keys: set) -> str:
    return ", ".join(sorted(handle for _space, _path, handle in keys)) or "nothing"


def _unknown(requirement_id: str, reason: str, candidates=()) -> Binding:
    return Binding(
        requirement_id=requirement_id,
        status="unknown",
        refs=(),
        candidates=tuple(candidates),
        reason=reason,
    )


def _with_reason(binding: Binding, reason: str) -> Binding:
    return Binding(
        requirement_id=binding.requirement_id,
        status=binding.status,
        refs=binding.refs,
        candidates=binding.candidates,
        reason=reason,
    )


def _rebind_refs(prior: Binding, after_observed, *, handles_stable, by_key, before_by_key, eps):
    """Map the source binding's features onto the saved file, one feature at a time:
    ``(refs, matched features, methods, problem, candidates)``."""
    refs: list[FeatureRef] = []
    matched_all: list = []
    methods: set[str] = set()
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
                return refs, matched_all, methods, problem, candidates
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
                return refs, matched_all, methods, problem, candidates
            matched.append(found)
        matched_all.extend(matched)
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
    return refs, matched_all, methods, None, candidates


def rebind(
    rs: RequirementSet,
    before: MeasuredRun | None,
    after_snap: Snapshot,
    after_observed: ObservedGeometry,
    *,
    binding_policy: str = "strict",
) -> tuple[tuple[Binding, ...], list[dict], bool | None]:
    """``(bindings on the saved file, one re-binding row per requirement, handles_stable)``.

    A ``count`` / ``absence`` requirement is the set its selectors define, so it
    is resolved afresh on the saved file - never carried over from the source's
    count, which cannot see a feature the file added. A requirement whose
    selectors name no handle (predicate, feature tag) is re-bound feature by
    feature *and* resolved afresh; when the two disagree it is ``unknown`` with
    both listed. A handle-naming selector (handle, DIMENSION, arch element) on a
    file whose handles were renumbered cannot be resolved afresh - a handle there
    names another entity - so such a requirement is ``unknown`` unless it is
    re-bound from the source binding."""
    fresh = {b.requirement_id: b for b in bind(rs, after_observed, policy=binding_policy)}
    bindings: list[Binding] = []
    rows: list[dict] = []

    def _add(requirement_id: str, method: str, binding: Binding) -> None:
        bindings.append(binding)
        rows.append(
            {
                "id": requirement_id,
                "method": method,
                "status": binding.status,
                "reason": binding.reason,
            }
        )

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
        prior_bound = prior is not None and prior.status == "bound"
        binding = fresh.get(requirement.id) or _unknown(requirement.id, "no binding produced")
        handle_free = _handle_free(requirement)
        counting = requirement.quantity in _COUNTING
        if not handle_free and not handles_stable and (counting or not prior_bound):
            kinds = sorted({s.kind for s in requirement.features} - _HANDLE_FREE)
            reason = (
                f"the requirement names features by {', '.join(kinds)} and the saved file "
                "renumbered handles: a handle there names another entity, so it cannot be "
                "resolved afresh"
            )
            if counting:
                reason += ", and a count is never carried over from the source document"
            _add(requirement.id, "none", _unknown(requirement.id, reason))
            continue
        if counting:
            counted = len(_keys_of_refs(binding.refs))
            source = (
                f"; the source document counted {len(_keys_of_refs(prior.refs))}"
                if prior_bound
                else ""
            )
            _add(
                requirement.id,
                "fresh",
                _with_reason(
                    binding,
                    f"counted afresh on the saved file ({counted}): {binding.reason}{source}",
                ),
            )
            continue
        if not prior_bound:
            _add(
                requirement.id,
                "fresh",
                _with_reason(
                    binding,
                    "the source binding was not bound; bound afresh on the file: " + binding.reason,
                ),
            )
            continue
        refs, matched, methods, problem, candidates = _rebind_refs(
            prior,
            after_observed,
            handles_stable=handles_stable,
            by_key=by_key,
            before_by_key=before_by_key,
            eps=eps,
        )
        method = "+".join(sorted(methods)) or "none"
        afresh = _keys_of_refs(binding.refs or binding.candidates)
        if problem is not None:
            reason = f"re-binding on the saved file failed: {problem}"
            if handle_free:
                reason += f"; afresh the selectors are {binding.status} on {_refs_text(afresh)}"
            _add(
                requirement.id,
                method,
                _unknown(requirement.id, reason, tuple(_ref_of(g) for g in candidates)),
            )
            continue
        if handle_free and (
            binding.status != "bound" or _keys_of_refs(binding.refs) != _keys_of_features(matched)
        ):
            _add(
                requirement.id,
                method,
                _unknown(
                    requirement.id,
                    f"the saved file disagrees with the source binding: re-bound by {method} "
                    f"on {_refs_text(_keys_of_features(matched))}, but afresh the selectors "
                    f"are {binding.status} on {_refs_text(afresh)}",
                    (*(_ref_of(g) for g in matched), *binding.candidates),
                ),
            )
            continue
        reason = f"re-bound on the saved file by {', '.join(sorted(methods)) or 'reference'}"
        if handle_free:
            reason += "; the selectors resolve to the same features afresh"
        _add(
            requirement.id,
            method,
            Binding(
                requirement_id=requirement.id,
                status="bound",
                refs=tuple(refs),
                candidates=(),
                reason=reason,
            ),
        )
    return tuple(bindings), rows, handles_stable


def _same_measured_points(f, g, eps: float) -> bool:
    a, b = dict(f.geometry), dict(g.geometry)
    return (
        a.get("dimtype") == b.get("dimtype")
        and _same(a.get("defpoints"), b.get("defpoints"), eps)
        and _same(a.get("angle"), b.get("angle"), eps)
    )


def _rebind_dimension(source, after: ObservedGeometry, *, handles_stable, by_key, eps):
    """``(DIMENSION or None, method, candidates)``: as any feature (handle, tag, the
    whole geometry), then by what it is drawn to alone - its type, defpoints and
    angle - so a DIMENSION whose text was edited on the way is still found and
    its text judged, not lost."""
    found, how, cands = _rebind_feature(
        source, after, handles_stable=handles_stable, by_key=by_key, eps=eps
    )
    if found is not None or cands:
        return found, how, cands
    same = [
        g
        for g in after.features
        if g.type == "dimension"
        and g.space == source.space
        and len(g.block_path) == len(source.block_path)
        and _same_measured_points(source, g, eps)
    ]
    if len(same) == 1:
        return same[0], "geometry", ()
    if len(same) > 1:
        return None, "geometry", tuple(same)
    return None, "none", ()


def rebound_dimension_evidence(
    rs: RequirementSet,
    before: MeasuredRun,
    after_snap: Snapshot,
    after_observed: ObservedGeometry,
    *,
    handles_stable: bool,
) -> tuple[dict, list[tuple[str, str]]]:
    """``(evidence, unresolved)`` for every requirement naming a DIMENSION, measured
    on the saved file's own DIMENSION.

    The source's DIMENSION is re-bound like any feature - by handle while handles
    are stable, then by feature tag, then by geometry (all of it, then its type,
    defpoints and angle alone) - and ``bind_dimension`` runs on the one found. Nothing found, or two candidates, is an ``unresolved`` entry
    ``(requirement id, why)``: the dimension check could not be made on the file,
    which blocks acceptance - never a missing check that reads as clean."""
    eps = default_epsilon(after_observed)
    by_key = {(g.handle, g.block_path): g for g in after_observed.features}
    evidence: dict[str, dict] = {}
    unresolved: list[tuple[str, str]] = []
    for requirement in rs.requirements:
        handle = next(
            (
                dict(selector.value).get("handle")
                for selector in requirement.features
                if selector.kind == "dimension"
            ),
            None,
        )
        if handle is None:
            continue
        handle = str(handle)
        source = next(
            (
                f
                for f in before.observed.features
                if f.type == "dimension" and f.handle.upper() == handle.upper()
            ),
            None,
        )
        if source is None:
            # the source has no such DIMENSION: the row is bound afresh by that
            # handle, which names the same entity only while handles are stable
            if handles_stable:
                single = RequirementSet(requirements=(requirement,), units=rs.units)
                item = dimension_evidence(single, after_snap, after_observed).get(requirement.id)
                if item is not None:
                    evidence[requirement.id] = item
            continue
        found, how, cands = _rebind_dimension(
            source, after_observed, handles_stable=handles_stable, by_key=by_key, eps=eps
        )
        if found is None:
            why = (
                f"{len(cands)} DIMENSIONs of the saved file match {handle} by {how}"
                if cands
                else f"no DIMENSION of the saved file matches {handle} by handle, tag or geometry"
            )
            evidence[requirement.id] = {
                "handle": None,
                "source_handle": source.handle,
                "rebound": how,
                "status": "unknown",
                "candidates": [g.handle for g in cands],
                "reason": why,
                "findings": [],
                "mismatch": False,
            }
            unresolved.append((requirement.id, why))
            continue
        item = dict(bind_dimension(found, after_observed, epsilon=eps))
        item["handle"] = found.handle
        item["source_handle"] = source.handle
        item["rebound"] = how
        evidence[requirement.id] = item
    return evidence, unresolved


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
        if before is None:
            dimensions, unresolved = dimension_evidence(rs, after_snap, observed), []
        else:
            dimensions, unresolved = rebound_dimension_evidence(
                rs, before, after_snap, observed, handles_stable=bool(stable)
            )
        return observed, bindings, measurements, rebinding, stable, dimensions, unresolved

    work = await asyncio.to_thread(_work)
    observed, bindings, measurements, rebinding, stable, dimensions, unresolved = work
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
    for requirement_id, why in unresolved:
        report = add_finding(
            report,
            kind=DIMENSION_UNVERIFIED,
            requirement=requirement_id,
            message=f"the DIMENSION check could not be made on the saved file: {why}",
        )
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
