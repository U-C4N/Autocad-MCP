"""Auditable drawing delivery with artifact hashes and DXF reopen parity."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from version import __version__

from .scoring import combine
from .validator import DrawingValidator

if TYPE_CHECKING:
    from backends.base import AutoCADBackend
    from engineering.verification.types import RequirementSet


_FORMAT_FILENAMES = {
    "dxf": "drawing.dxf",
    "pdf": "drawing.pdf",
    "png": "preview.png",
}


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
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


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
    not a plain name inside the bundle is never read. ``ok`` only when all
    three of stale / missing / inconsistent are empty. Refuses (ValueError) a
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
    identity_at_start: dict | None,
) -> dict:
    """Measure ``requirements``, write acceptance.json / acceptance.md, hash both."""
    from engineering.verification.report import mark_stale, render_markdown, verify_requirements

    report = await verify_requirements(backend, requirements)
    if identity_at_start is not None:
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


async def _reopen_parity(source: dict, dxf_path: Path) -> dict:
    from backends.ezdxf_backend import EzdxfBackend

    reopened_backend = EzdxfBackend()
    await reopened_backend.connect()
    try:
        await reopened_backend.drawing_open(str(dxf_path))
        reopened = await drawing_snapshot(reopened_backend)
    finally:
        await reopened_backend.disconnect()
    return compare_drawing_snapshots(source, reopened)


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
    """Export, validate, hash and re-open a release-ready drawing bundle.

    With ``requirements`` every requirement is measured on the CAD geometry
    (``engineering.verification.report``); a requirement that does not pass,
    stale evidence or a dimension that disagrees with its geometry makes the
    status ``failed_acceptance`` - after ``failed_export`` and
    ``failed_validation``, whatever the score. ``blocking`` lists every reason.
    """
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
    identity_at_start = None
    if requirements is not None:
        from engineering.verification.report import document_identity_of

        identity_at_start = await document_identity_of(backend)
    source = await drawing_snapshot(backend)

    # DXF is always emitted: it is the canonical, re-openable parity artifact.
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
                await backend.drawing_export_dxf(str(artifact_path))
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

    validation_started = time.perf_counter()
    # One run context: the validator and the critique share one all-spaces read.
    qa_shared: dict = {}
    validation = await DrawingValidator().run(backend, expected=expected or {}, shared=qa_shared)
    critique_issues = await backend.drawing_critique(focus=None, shared=qa_shared)
    critique_summary = {severity: 0 for severity in ("error", "warning", "info")}
    for issue in critique_issues:
        critique_summary[issue.severity] += 1

    dxf_record = next(item for item in artifacts if item["format"] == "dxf")
    if dxf_record["status"] == "created":
        try:
            parity = await _reopen_parity(source, destination / dxf_record["filename"])
        except Exception as exc:
            parity = {
                "ok": False,
                "tolerance": 1e-6,
                "source": source,
                "reopened": None,
                "differences": [{"field": "reopen", "error": str(exc)}],
            }
    else:
        parity = {
            "ok": False,
            "tolerance": 1e-6,
            "source": source,
            "reopened": None,
            "differences": [{"field": "dxf", "error": "canonical DXF was not created"}],
        }

    score = combine(
        validation.summary,
        critique_summary,
        {"error": 0 if parity["ok"] else 1},
    )
    timings["validation_ms"] = round((time.perf_counter() - validation_started) * 1000, 2)

    acceptance = None
    if requirements is not None:
        acceptance_started = time.perf_counter()
        acceptance = await _acceptance(backend, requirements, destination, identity_at_start)
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
        blocking.append("parity: the reopened DXF differs from the source")
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
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    manifest = {
        "schema_version": "1.0",
        "status": status,
        "version": __version__,
        "git_sha": _git_sha(),
        "generated_at": datetime.now(UTC).isoformat(),
        "backend": backend.name,
        "capabilities": capabilities,
        "spec_hash": plan.get("spec_hash") if plan else None,
        "requested_formats": requested,
        "min_score": float(min_score),
        "strict_critique": strict_critique,
        "score": score,
        "validator": validation.to_dict(),
        "critique": validation_payload["critique"],
        "critique_summary": critique_summary,
        "parity": parity,
        "artifacts": artifacts,
        "validation_artifact": _artifact_record("json", validation_path, status="created"),
        "timings_ms": timings,
    }
    if acceptance is not None:
        from engineering.verification.report import summarize

        manifest["acceptance"] = {
            **summarize(acceptance["report"]),
            "report": acceptance["json"],
            "markdown": acceptance["markdown"],
        }
        manifest["blocking"] = blocking
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
        }
        result.coverage = dict(report["coverage"])
        result.blocking = list(blocking)
    return result
