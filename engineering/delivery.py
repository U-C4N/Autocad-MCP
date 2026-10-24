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

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "output_dir": self.output_dir,
            "manifest_path": self.manifest_path,
            "artifacts": list(self.artifacts),
            "score": dict(self.score),
            "parity": dict(self.parity),
        }


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
) -> DeliveryResult:
    """Export, validate, hash and re-open a release-ready drawing bundle."""
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

    failed_export = any(item["status"] != "created" for item in artifacts)
    critique_blocked = any(
        issue.severity == "error" or (strict_critique and issue.severity != "info")
        for issue in critique_issues
    )
    validation_blocked = (
        not validation.ok
        or not parity["ok"]
        or score["score"] < float(min_score)
        or critique_blocked
    )
    if failed_export:
        status = "failed_export"
    elif validation_blocked:
        status = "failed_validation"
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
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return DeliveryResult(
        status=status,
        output_dir=str(destination),
        manifest_path=str(manifest_path),
        artifacts=artifacts,
        score=score,
        parity=parity,
    )
