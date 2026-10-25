"""Auditable drawing delivery: artifacts, hashes, all-spaces reopen parity, acceptance.

``deliver_drawing`` writes the bundle (``drawing.dxf`` always, PDF / PNG on
request), validates and critiques the drawing, reads the delivered DXF back and
compares it with a fresh serialisation of the source document over every space
(:func:`compare_artifacts`: ``diff_snapshots`` + ``block_signatures``), and -
with requirements - verifies them on the delivered DXF
(``engineering.verification.artifact.verify_artifact``). ``manifest.json``
records every artifact's sha256, the source document (id, revision, path and
saved flag), the DXF export (method and version), the acceptance summary with
the requirements digest and the report hash, and the build (version, and the
git SHA of the server's own checkout when there is one).

Exporting never re-points the document (v1.7.0, B23): the live engine writes
through ``Document.Export``, the headless one writes a copy; the open document
keeps its name, path and saved flag, and those go into the manifest. The
validator's "saved" step checks the delivered DXF in this context.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backends.quarantine import DocumentQuarantineError
from version import __version__

from .scoring import combine
from .validator import DrawingValidator

if TYPE_CHECKING:
    from backends.base import AutoCADBackend
    from engineering.understand.snapshot import Snapshot
    from engineering.verification.types import RequirementSet


_FORMAT_FILENAMES = {
    "dxf": "drawing.dxf",
    "pdf": "drawing.pdf",
    "png": "preview.png",
}
#: 1.1 adds document, source_document, dxf_export, build, requirements_hash,
#: report_hash, blocking and (with requirements) acceptance.
MANIFEST_SCHEMA = "1.1"
#: Delivery parity compares coordinates within this distance (drawing units).
PARITY_TOL = 1e-6
#: Rows of each parity difference kept in the manifest; the counts are complete.
PARITY_ROWS = 50
#: The server package's own root: the repository in a source checkout,
#: site-packages in a wheel (no .git there, so no SHA).
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class DeliveryResult:
    status: str
    output_dir: str
    manifest_path: str
    artifacts: list[dict]
    score: dict
    parity: dict
    #: Set only when the delivery verified requirements (B22); a delivery
    #: without them returns exactly the six fields above.
    geometry_acceptance: dict | None = None
    drafting_quality: dict | None = None
    artifact_integrity: dict | None = None
    coverage: dict | None = None
    blocking: list[str] | None = None

    def to_dict(self) -> dict:
        data = {
            "status": self.status,
            "output_dir": self.output_dir,
            "manifest_path": self.manifest_path,
            "artifacts": list(self.artifacts),
            "score": dict(self.score),
            "parity": dict(self.parity),
        }
        if self.geometry_acceptance is not None:
            data["geometry_acceptance"] = dict(self.geometry_acceptance)
            data["drafting_quality"] = dict(self.drafting_quality or {})
            data["artifact_integrity"] = dict(self.artifact_integrity or {})
            data["coverage"] = dict(self.coverage or {})
            data["blocking"] = list(self.blocking or [])
        return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha() -> str | None:
    """The commit of the server's own checkout, or None.

    Read at the package's root, never in the working directory: a server
    started from another repository used to stamp that repository's HEAD into
    the manifest. A wheel has no ``.git`` beside the package, and a checkout
    whose top level is not the package root (a wheel installed into a venv
    that lives inside some other repository) is not ours: both give None, and
    the manifest carries the version alone.
    """
    root = _PACKAGE_ROOT
    if not (root / ".git").exists():
        return None
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        if not top or Path(top).resolve() != root.resolve():
            return None
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return sha or None


async def drawing_snapshot(backend: AutoCADBackend) -> dict[str, Any]:
    """Return the stable structural fields used for save/reopen comparison."""
    await backend._ensure_document_state()
    entities = await backend.entity_list(limit=100_000)
    info = await backend.drawing_info()
    listed_entity_count = len(entities)
    reported_entity_count = int(info.entity_count)
    return {
        "entity_count": reported_entity_count,
        "listed_entity_count": listed_entity_count,
        "inventory_complete": listed_entity_count == reported_entity_count,
        "types": dict(sorted(Counter(item.type for item in entities).items())),
        "layers": dict(sorted(Counter(item.layer for item in entities).items())),
        "extents_min": [float(value) for value in info.extents_min],
        "extents_max": [float(value) for value in info.extents_max],
    }


def compare_drawing_snapshots(
    source: dict[str, Any],
    reopened: dict[str, Any],
    *,
    tolerance: float = 1e-6,
) -> dict:
    """Compare counts exactly and drawing extents within ``tolerance``."""
    differences: list[dict] = []
    for label, snapshot in (("source", source), ("reopened", reopened)):
        if snapshot.get("inventory_complete") is False:
            differences.append(
                {
                    "field": f"{label}_inventory_complete",
                    "entity_count": snapshot.get("entity_count"),
                    "listed_entity_count": snapshot.get("listed_entity_count"),
                }
            )
    for field in ("entity_count", "types", "layers"):
        if source.get(field) != reopened.get(field):
            differences.append(
                {"field": field, "source": source.get(field), "reopened": reopened.get(field)}
            )
    for field in ("extents_min", "extents_max"):
        left = source.get(field, [])
        right = reopened.get(field, [])
        equal = len(left) == len(right) and all(
            abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right, strict=True)
        )
        if not equal:
            differences.append({"field": field, "source": left, "reopened": right})
    return {
        "ok": not differences,
        "tolerance": tolerance,
        "source": source,
        "reopened": reopened,
        "differences": differences,
    }


def _artifact_record(fmt: str, path: Path, *, status: str, error: str | None = None) -> dict:
    record = {
        "format": fmt,
        "filename": path.name,
        "status": status,
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "sha256": _sha256(path) if path.is_file() else None,
    }
    if error:
        record["error"] = error
    return record


def _space_counts(snap: Snapshot) -> dict[str, int]:
    return dict(sorted(Counter(record.space for record in snap.records).items()))


def compare_artifacts(source, reopened, *, tol: float = PARITY_TOL) -> dict:
    """Delivery parity over every space: zero changed, added or removed entities
    within ``tol``, the same entity count per space, the same block definitions.

    ``source`` and ``reopened`` are ``(Snapshot, block signatures)`` pairs
    (``read_revision``). A hole moved inside the outline is a ``changed`` row
    (``moved_by``) - the 1.6 counts-and-extents comparison let it through.
    """
    from engineering.understand.diff import CATEGORIES, block_changes, diff_snapshots

    old_snap, old_blocks = source
    new_snap, new_blocks = reopened
    diff = diff_snapshots(old_snap, new_snap, tol=tol)
    blocks = block_changes(old_blocks, new_blocks)
    counts = {"source": _space_counts(old_snap), "reopened": _space_counts(new_snap)}
    differences: list[dict] = []
    for category in CATEGORIES:
        rows = diff[category]
        if rows:
            differences.append({"field": category, "count": len(rows), "rows": rows[:PARITY_ROWS]})
    if counts["source"] != counts["reopened"]:
        differences.append({"field": "counts", **counts})
    if not blocks.get("compared"):
        differences.append({"field": "blocks", "reason": blocks.get("reason")})
    else:
        for key in ("added", "removed", "changed"):
            if blocks[key]:
                differences.append({"field": f"blocks_{key}", "names": list(blocks[key])})
    return {
        "ok": not differences,
        "tolerance": tol,
        "move_tol": diff["move_tol"],
        "handles_stable": diff["handles_stable"],
        "summary": diff["summary"],
        "counts": counts,
        "blocks": blocks,
        "differences": differences,
    }


def _parity_failure(field: str, error: str) -> dict:
    return {
        "ok": False,
        "tolerance": PARITY_TOL,
        "move_tol": None,
        "handles_stable": None,
        "summary": None,
        "counts": None,
        "blocks": None,
        "differences": [{"field": field, "error": error}],
    }


async def source_revision(
    backend: AutoCADBackend, *, tol: float = PARITY_TOL
) -> tuple[Snapshot, dict[str, str]]:
    """A fresh serialisation of the open document, read back: the parity source.

    Both engines write the document through ``drawing_export_dxf`` (which never
    re-points it) into a private folder and read that file. Not the model in
    memory: ezdxf leaves unset DXF defaults unset in memory and writes them out
    (an MTEXT without ``char_height`` reads 0.0 in memory and 2.5 after a write
    - measured on 2026-10-24), so an in-memory source would differ from every
    honest copy.
    """
    from engineering.understand.diff import read_revision

    folder = tempfile.mkdtemp(prefix="acadmcp_parity_")
    try:
        target = Path(folder) / "source.dxf"
        await backend.drawing_export_dxf(str(target))
        return await asyncio.to_thread(read_revision, str(target), tol=tol, insert_boxes=False)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _manifest_records(manifest: dict) -> list[dict]:
    """Every file record a manifest hashed: artifacts, validation, acceptance report."""
    records = [dict(item) for item in manifest.get("artifacts") or []]
    if manifest.get("validation_artifact"):
        records.append(dict(manifest["validation_artifact"]))
    acceptance = manifest.get("acceptance") or {}
    for key in ("report", "markdown"):
        if acceptance.get(key):
            records.append(dict(acceptance[key]))
    return records


def verify_manifest(path: str) -> dict:
    """Recompute every hash a delivery manifest recorded (B23 made observable).

    ``stale``: a file whose sha256 no longer matches (it was edited after the
    manifest was written). ``missing``: a recorded file that is gone.
    ``skipped``: a record that was never created (an unsupported format).
    ``inconsistent``: a record that cannot be trusted - a file name that is
    not a plain name inside the bundle (never read), a ``report_hash`` that is
    not the acceptance report's recorded sha256, or a ``requirements_hash``
    that is not the digest the acceptance report says it measured. ``ok`` only
    when stale, missing and inconsistent are all empty. Refuses (ValueError) a
    file that is not JSON or holds no ``artifacts`` list.
    """
    manifest_path = Path(path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: not a readable delivery manifest ({exc})") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), list):
        raise ValueError(f"{path}: not a delivery manifest (no artifacts list)")
    base = manifest_path.parent
    checked: list[dict] = []
    stale: list[dict] = []
    missing: list[dict] = []
    skipped: list[dict] = []
    inconsistent: list[dict] = []
    for record in _manifest_records(manifest):
        filename = str(record.get("filename") or "")
        recorded = record.get("sha256")
        row = {"filename": filename, "format": record.get("format")}
        if not filename or recorded is None:
            skipped.append({**row, "status": record.get("status")})
            continue
        if Path(filename).name != filename or filename in (".", ".."):
            inconsistent.append(
                {**row, "field": "filename", "reason": "not a plain file name inside the bundle"}
            )
            continue
        target = base / filename
        if not target.is_file():
            missing.append(row)
            continue
        row = {**row, "recorded_sha256": recorded, "sha256": _sha256(target)}
        checked.append(row)
        if row["sha256"] != recorded:
            stale.append(row)
    report_record = (manifest.get("acceptance") or {}).get("report")
    if manifest.get("report_hash") is not None and (
        not report_record or report_record.get("sha256") != manifest["report_hash"]
    ):
        inconsistent.append(
            {
                "field": "report_hash",
                "reason": "the manifest's report_hash is not the acceptance report's sha256",
            }
        )
    if manifest.get("requirements_hash") is not None and report_record:
        report_file = base / str(report_record.get("filename") or "")
        digest = None
        if (
            Path(str(report_record.get("filename") or "")).name == report_record.get("filename")
            and report_file.is_file()
        ):
            try:
                digest = json.loads(report_file.read_text(encoding="utf-8")).get(
                    "requirements_digest"
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                digest = None
        if digest != manifest["requirements_hash"]:
            inconsistent.append(
                {
                    "field": "requirements_hash",
                    "manifest": manifest["requirements_hash"],
                    "report": digest,
                }
            )
    return {
        "ok": not (stale or missing or inconsistent),
        "manifest": str(manifest_path),
        "status": manifest.get("status"),
        "checked": checked,
        "stale": stale,
        "missing": missing,
        "skipped": skipped,
        "inconsistent": inconsistent,
    }


async def _acceptance(
    backend: AutoCADBackend,
    requirements: RequirementSet,
    destination: Path,
    identity_at_start: dict,
    delivered: Path | None,
) -> dict:
    """Measure ``requirements`` on the delivered DXF (read fresh, re-bound from the
    document), write acceptance.json / acceptance.md, hash both. Without a DXF
    the document is measured and the report says why."""
    from engineering.verification.artifact import verify_artifact
    from engineering.verification.report import (
        add_finding,
        mark_stale,
        render_markdown,
        verify_requirements,
    )

    if delivered is not None:
        try:
            report = await verify_artifact(str(delivered), requirements, backend=backend)
        except (ValueError, OSError) as exc:
            report = await verify_requirements(backend, requirements)
            report = add_finding(
                report,
                kind="artifact_unread",
                requirement="*",
                message=f"the delivered DXF could not be read fresh: {exc}",
            )
    else:
        report = await verify_requirements(backend, requirements)
        report = add_finding(
            report,
            kind="artifact_unread",
            requirement="*",
            message="no canonical DXF was written, so nothing delivered was measured",
        )
    report = mark_stale(report, identity_at_start)
    json_path = destination / "acceptance.json"
    md_path = destination / "acceptance.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {
        "report": report,
        "json": _artifact_record("json", json_path, status="created"),
        "markdown": _artifact_record("md", md_path, status="created"),
    }


async def deliver_drawing(
    backend: AutoCADBackend,
    output_dir: str | Path,
    *,
    formats: list[str] | None = None,
    min_score: float = 95.0,
    strict_critique: bool = True,
    expected: dict | None = None,
    requirements: RequirementSet | None = None,
) -> DeliveryResult:
    """Export, validate, hash, read back and (with requirements) verify a bundle.

    Status: ``failed_export`` (an artifact was not written), then
    ``failed_validation`` (validator, critique, score or parity), then
    ``failed_acceptance`` (a requirement did not pass on the delivered DXF, the
    evidence went stale, or a dimension disagrees with its geometry), else
    ``success``. ``blocking`` lists every reason, whichever status won. The
    open document is never re-pointed or saved.
    """
    from engineering.verification.report import document_identity_of, summarize

    requested = list(
        dict.fromkeys(item.lower().strip() for item in (formats or ["dxf", "pdf", "png"]))
    )
    unknown = [item for item in requested if item not in _FORMAT_FILENAMES]
    if unknown:
        raise ValueError(f"Unsupported delivery format(s): {', '.join(unknown)}")
    if not 0 <= float(min_score) <= 100:
        raise ValueError("min_score must be between 0 and 100")

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise ValueError(f"Delivery output is not a directory: {destination}")

    started = time.perf_counter()
    timings: dict[str, float] = {}
    artifacts: list[dict] = []
    capabilities = backend.capabilities().to_dict()
    feature_caps = capabilities["features"]

    identity = await document_identity_of(backend)
    info = await backend.drawing_info()
    source_document = {
        "name": info.name,
        "path": info.full_path or None,
        "saved": bool(info.saved),
    }
    source = None
    source_error = None
    source_started = time.perf_counter()
    try:
        source = await source_revision(backend)
    except DocumentQuarantineError:
        raise  # a RuntimeError subclass: a quarantined document is refused, never delivered
    except (ValueError, OSError, RuntimeError) as exc:
        source_error = str(exc) or type(exc).__name__
    timings["source_ms"] = round((time.perf_counter() - source_started) * 1000, 2)

    dxf_export = None
    # DXF is always emitted: it is the canonical, re-readable parity artifact.
    export_formats = ["dxf", *(item for item in requested if item != "dxf")]
    for fmt in export_formats:
        artifact_path = destination / _FORMAT_FILENAMES[fmt]
        capability = feature_caps.get(fmt, {"supported": False, "reason": "not_declared"})
        export_started = time.perf_counter()
        if not capability.get("supported"):
            artifacts.append(
                _artifact_record(
                    fmt,
                    artifact_path,
                    status="unsupported",
                    error=str(capability.get("reason") or "backend does not support format"),
                )
            )
            continue
        try:
            if fmt == "dxf":
                exported = await backend.drawing_export_dxf(str(artifact_path))
                dxf_export = {
                    "method": exported.get("method"),
                    "dxf_version": exported.get("dxf_version"),
                    "repointed": exported.get("repointed"),
                }
            elif fmt == "pdf":
                await backend.drawing_export_pdf(str(artifact_path))
            else:
                png = await backend.view_screenshot()
                if not png:
                    raise RuntimeError("backend returned no PNG bytes")
                artifact_path.write_bytes(png)
            artifacts.append(_artifact_record(fmt, artifact_path, status="created"))
        except Exception as exc:
            artifacts.append(_artifact_record(fmt, artifact_path, status="failed", error=str(exc)))
        finally:
            timings[f"export_{fmt}_ms"] = round((time.perf_counter() - export_started) * 1000, 2)

    delivered = destination / _FORMAT_FILENAMES["dxf"]
    validation_started = time.perf_counter()
    # One run context: the validator and the critique share one all-spaces read.
    qa_shared: dict = {}
    validation = await DrawingValidator().run(
        backend, expected=expected or {}, shared=qa_shared, delivered_path=str(delivered)
    )
    critique_issues = await backend.drawing_critique(focus=None, shared=qa_shared)
    critique_summary = {severity: 0 for severity in ("error", "warning", "info")}
    for issue in critique_issues:
        critique_summary[issue.severity] += 1

    dxf_record = next(item for item in artifacts if item["format"] == "dxf")
    if dxf_record["status"] != "created":
        parity = _parity_failure("dxf", "canonical DXF was not created")
    elif source is None:
        parity = _parity_failure(
            "source", f"the source document could not be serialised: {source_error}"
        )
    else:
        from engineering.understand.diff import read_revision

        try:
            reopened = await asyncio.to_thread(
                read_revision, str(delivered), tol=PARITY_TOL, insert_boxes=False
            )
            parity = compare_artifacts(source, reopened)
        except ValueError as exc:
            parity = _parity_failure("reopen", str(exc))

    score = combine(
        validation.summary,
        critique_summary,
        {"error": 0 if parity["ok"] else 1},
    )
    timings["validation_ms"] = round((time.perf_counter() - validation_started) * 1000, 2)

    acceptance = None
    if requirements is not None:
        acceptance_started = time.perf_counter()
        acceptance = await _acceptance(
            backend,
            requirements,
            destination,
            identity,
            delivered if dxf_record["status"] == "created" else None,
        )
        timings["acceptance_ms"] = round((time.perf_counter() - acceptance_started) * 1000, 2)

    failed_export = any(item["status"] != "created" for item in artifacts)
    blocking_issues = [
        issue
        for issue in critique_issues
        if issue.severity == "error" or (strict_critique and issue.severity != "info")
    ]
    critique_blocked = bool(blocking_issues)
    validation_blocked = (
        not validation.ok
        or not parity["ok"]
        or score["score"] < float(min_score)
        or critique_blocked
    )
    blocking: list[str] = []
    if failed_export:
        blocking.append(
            "export: "
            + ", ".join(
                f"{item['format']} {item['status']}"
                for item in artifacts
                if item["status"] != "created"
            )
        )
    if not validation.ok:
        blocking.append(f"validator: {validation.summary['error']} error(s)")
    if not parity["ok"]:
        blocking.append("parity: the delivered DXF differs from the source document")
    if score["score"] < float(min_score):
        blocking.append(f"score: {score['score']} < min_score {float(min_score)}")
    if critique_blocked:
        blocking.append(f"critique: {len(blocking_issues)} blocking issue(s)")
    acceptance_blocked = acceptance is not None and not acceptance["report"]["accepted"]
    if acceptance is not None:
        blocking.extend(acceptance["report"]["blocking"])
    if failed_export:
        status = "failed_export"
    elif validation_blocked:
        status = "failed_validation"
    elif acceptance_blocked:
        status = "failed_acceptance"
    else:
        status = "success"

    validation_payload = {
        "validator": validation.to_dict(),
        "critique": [issue.to_dict() for issue in critique_issues],
        "critique_summary": critique_summary,
        "score": score,
        "parity": parity,
    }
    validation_path = destination / "validation.json"
    validation_path.write_text(
        json.dumps(validation_payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    plan = backend.get_plan_spec()
    git_sha = _git_sha()
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "status": status,
        "version": __version__,
        "git_sha": git_sha,
        "build": {"version": __version__, "git_sha": git_sha},
        "generated_at": datetime.now(UTC).isoformat(),
        "backend": backend.name,
        "capabilities": capabilities,
        "document": identity,
        "source_document": source_document,
        "dxf_export": dxf_export,
        "spec_hash": plan.get("spec_hash") if plan else None,
        "requirements_hash": requirements.digest() if requirements is not None else None,
        "report_hash": acceptance["json"]["sha256"] if acceptance is not None else None,
        "requested_formats": requested,
        "min_score": float(min_score),
        "strict_critique": strict_critique,
        "score": score,
        "validator": validation.to_dict(),
        "critique": validation_payload["critique"],
        "critique_summary": critique_summary,
        "parity": parity,
        "blocking": blocking,
        "artifacts": artifacts,
        "validation_artifact": _artifact_record("json", validation_path, status="created"),
        "timings_ms": timings,
    }
    if acceptance is not None:
        manifest["acceptance"] = {
            **summarize(acceptance["report"]),
            "report": acceptance["json"],
            "markdown": acceptance["markdown"],
        }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = DeliveryResult(
        status=status,
        output_dir=str(destination),
        manifest_path=str(manifest_path),
        artifacts=artifacts,
        score=score,
        parity=parity,
    )
    if acceptance is not None:
        report = acceptance["report"]
        result.geometry_acceptance = {
            **summarize(report),
            "report_file": acceptance["json"]["filename"],
        }
        result.drafting_quality = dict(score)
        result.artifact_integrity = {
            "ok": not failed_export and parity["ok"],
            "parity_ok": parity["ok"],
            "artifacts": [
                {
                    "format": item["format"],
                    "filename": item["filename"],
                    "status": item["status"],
                    "sha256": item["sha256"],
                }
                for item in artifacts
            ],
            "parity": {
                "ok": parity["ok"],
                "summary": parity["summary"],
                "counts": parity["counts"],
                "blocks": parity["blocks"],
            },
            "dxf_export": dxf_export,
            "reverified": report.get("artifact"),
        }
        result.coverage = dict(report["coverage"])
        result.blocking = list(blocking)
    return result
