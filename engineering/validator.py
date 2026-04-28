"""8-step pre-completion validator for engineering drawings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import AutoCADBackend


REAL_DIMENSION_TYPES = {
    "DIMENSION", "DIMLINEAR", "DIMALIGNED", "DIMANGULAR",
    "DIMRADIUS", "DIMDIAMETER", "DIMORDINATE",
}

# Patterns that suggest a TEXT/MTEXT entity is masquerading as a dimension.
DIMENSION_LIKE_TEXT = re.compile(
    r"(Ø|⌀|∅|%%c|%%C|R\d|±|%%p|\d+\s*[×x]\s*\d+|\d+\.\d+\s*mm)",
    re.IGNORECASE,
)


@dataclass
class ValidationFinding:
    """One issue surfaced by the validator."""

    severity: str   # "error" | "warning" | "info"
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


class DrawingValidator:
    """8-step pre-completion validator."""

    async def run(
        self,
        backend: "AutoCADBackend",
        *,
        expected: dict | None = None,
    ) -> ValidationResult:
        """Run all 8 checks; return aggregate ValidationResult."""

        findings: list[ValidationFinding] = []
        expected = expected or {}
        ents: list = []

        # 1 — fake dimensions
        try:
            ents = await backend.entity_list(limit=10000)
            text_likes = [
                e for e in ents
                if e.type in ("TEXT", "MTEXT")
                and DIMENSION_LIKE_TEXT.search(
                    getattr(e, "properties", {}).get("text", "") or ""
                )
            ]
            real_dims = [e for e in ents if e.type in REAL_DIMENSION_TYPES]
            if text_likes:
                findings.append(ValidationFinding(
                    "warning",
                    "fake_dimension_text",
                    f"{len(text_likes)} TEXT/MTEXT entities look like dimensions "
                    "(Ø, ±, R, etc.) — use dimension_* tools instead",
                    {
                        "sample_handles": [t.handle for t in text_likes[:5]],
                        "real_dim_count": len(real_dims),
                    },
                ))
        except Exception as exc:
            findings.append(ValidationFinding(
                "warning", "dim_check_failed", str(exc),
            ))

        # 2 — hidden orphans
        try:
            hidden = [e for e in ents if not e.visible]
            if hidden:
                findings.append(ValidationFinding(
                    "error",
                    "hidden_orphans",
                    f"{len(hidden)} entities have visible=False — hide-fallback "
                    "is forbidden, use entity_delete",
                    {"sample_handles": [h.handle for h in hidden[:5]]},
                ))
        except Exception:
            pass

        # 3 — linetypes
        try:
            lts = {x.lower() for x in await backend.linetype_list()}
            missing = [lt for lt in ("center", "hidden", "phantom") if lt not in lts]
            if missing:
                findings.append(ValidationFinding(
                    "error",
                    "linetypes_missing",
                    f"Standard linetypes not loaded: {', '.join(missing).upper()}",
                    {"missing": missing},
                ))
        except Exception as exc:
            findings.append(ValidationFinding(
                "warning", "linetype_check_failed", str(exc),
            ))

        # 4 — gear views (heuristic, only if expected demands)
        if expected.get("must_have_bore") or expected.get("must_have_keyway"):
            bore_circles = [
                e for e in ents
                if e.type == "CIRCLE" and e.layer == "GEOMETRY"
            ]
            if expected.get("must_have_bore") and len(bore_circles) < 1:
                findings.append(ValidationFinding(
                    "error",
                    "bore_missing",
                    "Expected bore but no circle on GEOMETRY layer found",
                ))
            keyway_polys = [
                e for e in ents
                if e.type in ("LWPOLYLINE", "POLYLINE")
                and e.layer == "GEOMETRY"
                and len(e.properties.get("points", [])) <= 6
                and e.properties.get("closed")
            ]
            if expected.get("must_have_keyway") and len(keyway_polys) < 1:
                findings.append(ValidationFinding(
                    "warning",
                    "keyway_section_unverified",
                    "Could not confirm keyway in section view via heuristic "
                    "(small closed polyline on GEOMETRY)",
                ))

        # 5 — title consistency
        helix = expected.get("helix_angle")
        if helix:
            title_texts = [
                (e.properties.get("text", "") or "").upper()
                for e in ents
                if e.type in ("TEXT", "MTEXT") and e.layer == "TEXT"
            ]
            joined = " | ".join(title_texts)
            if "HELICAL SPUR" in joined:
                findings.append(ValidationFinding(
                    "error",
                    "title_helical_spur",
                    "Title contains 'HELICAL SPUR GEAR' — invalid; use "
                    "'HELICAL GEAR' or 'SPUR GEAR'",
                ))
            elif "SPUR" in joined and "HELICAL" not in joined:
                findings.append(ValidationFinding(
                    "error",
                    "title_says_spur_for_helical",
                    f"Title contains SPUR but part is helical (helix_angle={helix})",
                    {"sample": joined[:200]},
                ))

        # 6 — saved to disk
        try:
            info = await backend.drawing_info()
            saved_attr = getattr(info, "saved", None)
            full_path = getattr(info, "full_path", "") or ""
            if not saved_attr:
                findings.append(ValidationFinding(
                    "error",
                    "not_saved",
                    "Drawing has unsaved changes — call drawing_save before finalize",
                    {"full_path": full_path},
                ))
            elif full_path and not Path(full_path).exists():
                findings.append(ValidationFinding(
                    "error",
                    "file_missing",
                    f"Drawing reports saved=True but file not on disk: {full_path}",
                ))
        except Exception as exc:
            findings.append(ValidationFinding(
                "warning", "save_check_failed", str(exc),
            ))

        # 7 — screenshot
        try:
            shot = await backend.view_screenshot()
            if not shot:
                findings.append(ValidationFinding(
                    "info",
                    "screenshot_unavailable",
                    "Screenshot returned no bytes (ezdxf needs matplotlib install)",
                ))
        except Exception as exc:
            findings.append(ValidationFinding(
                "info", "screenshot_failed", str(exc),
            ))

        # 8 — dwg path returned
        try:
            info = await backend.drawing_info()
            if not getattr(info, "full_path", ""):
                findings.append(ValidationFinding(
                    "warning",
                    "no_path",
                    "drawing_info.full_path is empty",
                ))
        except Exception:
            pass

        summary = {
            sev: sum(1 for f in findings if f.severity == sev)
            for sev in ("error", "warning", "info")
        }
        return ValidationResult(
            ok=summary["error"] == 0,
            findings=findings,
            summary=summary,
        )
