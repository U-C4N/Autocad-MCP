"""8-step pre-completion validator for engineering drawings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from backends.quarantine import DOCUMENT_REFUSALS

from .qa_scan import qa_snapshot
from .understand.snapshot import dxf_version_of

if TYPE_CHECKING:
    from backends.base import AutoCADBackend


REAL_DIMENSION_TYPES = {
    "DIMENSION",
    "DIMLINEAR",
    "DIMALIGNED",
    "DIMANGULAR",
    "DIMRADIUS",
    "DIMDIAMETER",
    "DIMORDINATE",
}

# Patterns that suggest a TEXT/MTEXT entity is masquerading as a dimension.
DIMENSION_LIKE_TEXT = re.compile(
    r"(Ø|⌀|∅|%%c|%%C|R\d|±|%%p|\d+\s*[×x]\s*\d+|\d+\.\d+\s*mm)",
    re.IGNORECASE,
)

# A standard-part designation (ISO 4014 - M12x60, DIN 912 - M8x20) names a
# part, it does not measure one: the "12x60" in it is the thread and the
# nominal length the standard's designation grammar spells that way. A parts
# list drawn as text carries one per row, so the designation is taken out
# before DIMENSION_LIKE_TEXT looks - a hand-typed "Ø78" beside it still flags.
STANDARD_DESIGNATION = re.compile(
    r"\b(?:ISO|DIN|EN)\s*\d+(?:-\d+)?\s*-\s*M\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?",
    re.IGNORECASE,
)


def looks_like_dimension(text: str) -> bool:
    """Is this TEXT/MTEXT content masquerading as a dimension?"""
    return bool(DIMENSION_LIKE_TEXT.search(STANDARD_DESIGNATION.sub(" ", text or "")))


# R27 — match SPUR / HELICAL as whole words (not substrings of e.g.
# "SPURIOUS") when checking title/helix consistency.
_SPUR_RE = re.compile(r"\bSPUR\b", re.IGNORECASE)
_HELICAL_RE = re.compile(r"\bHELICAL\b", re.IGNORECASE)

# Layers that hold title-block text (scoped scan target for step 5).
_TITLE_LAYERS = {"TITLE", "TITLEBLOCK"}


@dataclass
class ValidationFinding:
    """One issue surfaced by the validator."""

    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class ValidationResult:
    """Aggregate of validator findings."""

    ok: bool
    findings: list[ValidationFinding]
    summary: dict

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "findings": [
                {
                    "severity": f.severity,
                    "code": f.code,
                    "message": f.message,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }


def _delivered_findings(path: str) -> list[ValidationFinding]:
    """Step 6 in a delivery: the bundle's canonical DXF exists and is a DXF."""
    target = Path(path)
    if not target.is_file():
        return [
            ValidationFinding(
                "error",
                "delivery_artifact_missing",
                f"The delivered DXF is not on disk: {target}",
                {"path": str(target)},
            )
        ]
    if target.stat().st_size == 0 or dxf_version_of(target) is None:
        return [
            ValidationFinding(
                "error",
                "delivery_artifact_unreadable",
                f"The delivered DXF has no $ACADVER header: {target}",
                {"path": str(target)},
            )
        ]
    return []


class DrawingValidator:
    """8-step pre-completion validator."""

    async def run(
        self,
        backend: AutoCADBackend,
        *,
        expected: dict | None = None,
        shared: dict | None = None,
        delivered_path: str | None = None,
    ) -> ValidationResult:
        """Run all 8 checks; return aggregate ValidationResult.

        Steps 1, 2, 4 and 5 read the run's all-spaces QA snapshot
        (``engineering/qa_scan.py``): every entity of every space, by its DXF
        type name on both engines. ``shared`` is that run's context - pass the
        dict ``run_critique`` will receive so a finalize reads the drawing
        once. A read that did not cover the drawing is an ``error`` finding
        ``scan_incomplete``; a step that raises is a finding naming the step;
        a quarantine refusal, or a live active-document switch, propagates
        (``DOCUMENT_REFUSALS``). Nothing is swallowed.

        ``delivered_path`` is the delivery context (``drawing_deliver``): the
        bundle's canonical DXF. Steps 6 and 8 then check that file - on disk,
        not empty, a ``$ACADVER`` header - instead of the open document's saved
        flag and path, because a delivery exports a copy and never saves or
        re-points the document.
        """

        findings: list[ValidationFinding] = []
        expected = expected or {}
        if shared is None:
            shared = {}
        records: tuple = ()

        snap, coverage = await qa_snapshot(backend, shared)
        if snap is not None:
            records = snap.records
        if not coverage.complete:
            findings.append(
                ValidationFinding(
                    "error",
                    "scan_incomplete",
                    f"The drawing was not read in full: {coverage.reason}",
                    coverage.to_dict(),
                )
            )

        # 1 — fake dimensions
        text_likes = [
            r for r in records if r.type in ("TEXT", "MTEXT") and looks_like_dimension(r.text or "")
        ]
        real_dims = [r for r in records if r.type in REAL_DIMENSION_TYPES]
        if text_likes:
            findings.append(
                ValidationFinding(
                    "warning",
                    "fake_dimension_text",
                    f"{len(text_likes)} TEXT/MTEXT entities look like dimensions "
                    "(Ø, ±, R, etc.) — use dimension_* tools instead",
                    {
                        "sample_handles": [t.handle for t in text_likes[:5]],
                        "real_dim_count": len(real_dims),
                    },
                )
            )

        # 2 — hidden orphans
        hidden = [r for r in records if not r.visible]
        if hidden:
            findings.append(
                ValidationFinding(
                    "error",
                    "hidden_orphans",
                    f"{len(hidden)} entities have visible=False — hide-fallback "
                    "is forbidden, use entity_delete",
                    {
                        "sample_handles": [h.handle for h in hidden[:5]],
                        "spaces": sorted({h.space for h in hidden}),
                    },
                )
            )

        # 3 — linetypes
        try:
            lts = {x.lower() for x in await backend.linetype_list()}
            missing = [lt for lt in ("center", "hidden", "phantom") if lt not in lts]
            if missing:
                findings.append(
                    ValidationFinding(
                        "error",
                        "linetypes_missing",
                        f"Standard linetypes not loaded: {', '.join(missing).upper()}",
                        {"missing": missing},
                    )
                )
        except DOCUMENT_REFUSALS:
            raise
        except Exception as exc:
            findings.append(
                ValidationFinding(
                    "warning",
                    "linetype_check_failed",
                    str(exc),
                )
            )

        # 4 — gear views (heuristic, only if expected demands)
        if expected.get("must_have_bore") or expected.get("must_have_keyway"):
            bore_circles = [r for r in records if r.type == "CIRCLE" and r.layer == "GEOMETRY"]
            if expected.get("must_have_bore") and len(bore_circles) < 1:
                findings.append(
                    ValidationFinding(
                        "error",
                        "bore_missing",
                        "Expected bore but no circle on GEOMETRY layer found",
                    )
                )
            # draw_keyed_bore emits the keyway notch as an OPEN 4-point polyline
            # on GEOMETRY — requiring `closed` here made this a permanent
            # false-negative. Match a small (3-6 point) polyline, open or closed.
            keyway_polys = [
                r
                for r in records
                if r.type in ("LWPOLYLINE", "POLYLINE")
                and r.layer == "GEOMETRY"
                and 3 <= len(r.points) <= 6
            ]
            if expected.get("must_have_keyway") and len(keyway_polys) < 1:
                findings.append(
                    ValidationFinding(
                        "warning",
                        "keyway_section_unverified",
                        "Could not confirm keyway via heuristic "
                        "(small 3-6 point polyline on GEOMETRY)",
                    )
                )

        # 5 — title consistency
        helix = expected.get("helix_angle")
        if helix:
            # R27 — scope the scan to title-block text where possible (TITLE /
            # TITLEBLOCK layer); fall back to all TEXT-layer text if no title
            # layer carries any text.
            title_block_texts = [
                r.text or ""
                for r in records
                if r.type in ("TEXT", "MTEXT") and r.layer in _TITLE_LAYERS
            ]
            if title_block_texts:
                title_texts = title_block_texts
            else:
                title_texts = [
                    r.text or ""
                    for r in records
                    if r.type in ("TEXT", "MTEXT") and r.layer == "TEXT"
                ]
            joined = " | ".join(title_texts)
            # R27 — whole-word matching so "SPURIOUS" no longer trips SPUR.
            has_spur = bool(_SPUR_RE.search(joined))
            has_helical = bool(_HELICAL_RE.search(joined))
            if has_spur and has_helical:
                findings.append(
                    ValidationFinding(
                        "error",
                        "title_helical_spur",
                        "Title contains 'HELICAL SPUR GEAR' — invalid; use "
                        "'HELICAL GEAR' or 'SPUR GEAR'",
                    )
                )
            elif has_spur:
                findings.append(
                    ValidationFinding(
                        "error",
                        "title_says_spur_for_helical",
                        f"Title contains SPUR but part is helical (helix_angle={helix})",
                        {"sample": joined[:200]},
                    )
                )

        # 6 — saved to disk (in a delivery: the delivered DXF)
        if delivered_path is not None:
            findings.extend(_delivered_findings(delivered_path))
        else:
            await self._saved_step(backend, findings)

        # 7 — screenshot
        try:
            shot = await backend.view_screenshot()
            if not shot:
                findings.append(
                    ValidationFinding(
                        "info",
                        "screenshot_unavailable",
                        "Screenshot returned no bytes (ezdxf needs matplotlib install)",
                    )
                )
        except DOCUMENT_REFUSALS:
            raise
        except Exception as exc:
            findings.append(
                ValidationFinding(
                    "info",
                    "screenshot_failed",
                    str(exc),
                )
            )

        # 8 — dwg path returned (in a delivery the delivered DXF is the path)
        if delivered_path is None:
            await self._path_step(backend, findings)

        summary = {
            sev: sum(1 for f in findings if f.severity == sev)
            for sev in ("error", "warning", "info")
        }
        return ValidationResult(
            ok=summary["error"] == 0,
            findings=findings,
            summary=summary,
        )

    @staticmethod
    async def _saved_step(backend: AutoCADBackend, findings: list[ValidationFinding]) -> None:
        try:
            info = await backend.drawing_info()
            saved_attr = getattr(info, "saved", None)
            full_path = getattr(info, "full_path", "") or ""
            if not saved_attr:
                findings.append(
                    ValidationFinding(
                        "error",
                        "not_saved",
                        "Drawing has unsaved changes — call drawing_save before finalize",
                        {"full_path": full_path},
                    )
                )
            elif full_path and not Path(full_path).exists():
                findings.append(
                    ValidationFinding(
                        "error",
                        "file_missing",
                        f"Drawing reports saved=True but file not on disk: {full_path}",
                    )
                )
        except DOCUMENT_REFUSALS:
            raise
        except Exception as exc:
            findings.append(
                ValidationFinding(
                    "warning",
                    "save_check_failed",
                    str(exc),
                )
            )

    @staticmethod
    async def _path_step(backend: AutoCADBackend, findings: list[ValidationFinding]) -> None:
        try:
            info = await backend.drawing_info()
            if not getattr(info, "full_path", ""):
                findings.append(
                    ValidationFinding(
                        "warning",
                        "no_path",
                        "drawing_info.full_path is empty",
                    )
                )
        except DOCUMENT_REFUSALS:
            raise
        except Exception as exc:
            findings.append(
                ValidationFinding(
                    "warning",
                    "path_check_failed",
                    str(exc),
                )
            )
