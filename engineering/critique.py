"""Premium-quality critique checks for finished drawings.

Each focus runs an independent check; new focuses must be added to the
`CritiqueFocus` Literal in plan_spec.py *and* registered here. Closed enum
keeps drawing_critique scope from drifting.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from backends.quarantine import DocumentQuarantineError, quarantine_refusal

from .layers import ENGINEERING_LAYERS
from .plan_spec import (
    ALL_CRITIQUE_FOCUSES,
    ISO_128_LINEWEIGHTS_MM,
    CritiqueFocus,
    Issue,
)

if TYPE_CHECKING:
    from backends.base import AutoCADBackend


# Layer-name → expected ACI color, derived from ENGINEERING_LAYERS.
_EXPECTED_LAYER_COLOR: dict[str, int] = {
    name: color for name, color, _lt, _lw, _desc in ENGINEERING_LAYERS
}

# Dimension entity types that should sit on the DIM layer.
_DIM_TYPES = {
    "DIMENSION",
    "DIMLINEAR",
    "DIMALIGNED",
    "DIMANGULAR",
    "DIMRADIUS",
    "DIMDIAMETER",
    "DIMORDINATE",
}


async def _check_iso128(backend: AutoCADBackend) -> list[Issue]:
    """Layer lineweights must be in ISO_128_LINEWEIGHTS_MM (or -3=ByLayer
    sentinel meaning 'inherit')."""
    issues: list[Issue] = []
    try:
        layers = await backend.layer_list()
    except DocumentQuarantineError:
        raise
    except Exception as exc:
        return [Issue("warning", "iso128", f"layer_list failed: {exc}")]
    allowed = set(ISO_128_LINEWEIGHTS_MM)
    for lyr in layers:
        # CONSTRUCTION-class layers are intentionally sub-ISO (0.05mm scratch
        # weight) and must be cleared before finalize — never an ISO violation.
        if "CONST" in lyr.name.upper():
            continue
        lw = float(lyr.lineweight)
        # AutoCAD sentinels: -1 (Default), -2 (ByBlock), -3 (ByLayer);
        # 0.0 = ezdxf's "use default" — not an ISO 128 violation.
        if lw <= 0:
            continue
        # Layer lineweights come through as ezdxf/COM hundredths-of-a-mm
        # (e.g. 25 == 0.25mm) — they are NOT pre-normalised to mm, so accept
        # either the raw mm value or hundredths/100.
        if lw not in allowed and (lw / 100.0) not in allowed:
            issues.append(
                Issue(
                    severity="warning",
                    focus="iso128",
                    message=(
                        f"Layer '{lyr.name}' lineweight {lw} mm is not in the "
                        f"ISO 128 set {sorted(allowed)}."
                    ),
                    detail={"layer": lyr.name, "lineweight": lw},
                )
            )
    issues.extend(_check_dimension_legibility(backend))
    return issues


#: ISO 3098 minimum lettering height for technical drawings, in mm.
ISO_3098_MIN_TEXT_MM = 2.5


def _check_dimension_legibility(backend: AutoCADBackend) -> list[Issue]:
    """Dimension text must be readable and use an ISO 129 decimal marker.

    This lane was blind: `iso128` checked layer lineweights only, so a sheet
    whose dimensions rendered at ezdxf's bare 1.0 mm with a comma separator
    critiqued clean — and "zero issues" is the claim that the drawing is
    conformant. Reading the settings rather than the rendered block keeps it
    cheap and catches the case before a dimension is even drawn.

    It reads the *effective* value — header first, then the stored dimstyle
    attribute, then ezdxf's renderer fallback — because that is the order the
    renderer resolves in since the DIM* header variables started reaching it.
    Grading the dimstyle alone made this check wrong in both directions: it
    would report a 1.0 mm defect on a sheet the header letters at 2.5, and clear
    a sheet the header letters at 1.0 because the style still said 2.5.

    Headless only by construction: a live AutoCAD document carries the
    operator's own template, and second-guessing it from here would report
    their house standard as a defect.
    """
    doc = getattr(backend, "_doc", None)
    if doc is None:  # the live backend has no ezdxf document to read
        return []

    # This is the one check that reads `_doc` directly instead of going through
    # `_async`, so it also misses `_async`'s quarantine gate — and it runs on
    # the event loop thread, which is exactly the unsynchronised access to a
    # document an abandoned worker may still be writing that the quarantine
    # exists to prevent. Refuse here rather than read.
    record = getattr(backend, "_quarantine", None)
    if record is not None:
        reap = getattr(backend, "_reap_quarantine", None)
        if reap is not None:
            reap()
        record = getattr(backend, "_quarantine", None)
        if record is not None:
            raise quarantine_refusal(record)

    from backends.ezdxf_backend import effective_dimvar

    try:
        height = float(effective_dimvar(doc, "dimtxt"))
        separator = int(effective_dimvar(doc, "dimdsep"))
    except DocumentQuarantineError:
        raise
    except Exception:
        return []

    issues: list[Issue] = []
    if height < ISO_3098_MIN_TEXT_MM:
        issues.append(
            Issue(
                severity="warning",
                focus="iso128",
                message=(
                    f"Dimension text is {height:g} mm; ISO 3098 sets the minimum "
                    f"lettering height at {ISO_3098_MIN_TEXT_MM} mm."
                ),
                detail={"dimtxt": height, "minimum": ISO_3098_MIN_TEXT_MM},
            )
        )
    # DIMDSEP 0 is not "unset" — ezdxf's `get_decimal_separator` renders it as a
    # comma — so it is the violation, not an excuse to skip the check.
    marker = "," if separator == 0 else chr(separator)
    if marker != ".":
        issues.append(
            Issue(
                severity="warning",
                focus="iso128",
                message=(f"Dimension decimal marker is {marker!r}; ISO 129 uses a point."),
                detail={"dimdsep": separator},
            )
        )
    return issues


async def _check_layer_color(backend: AutoCADBackend) -> list[Issue]:
    """Engineering-standard layers must keep their canonical ACI color."""
    issues: list[Issue] = []
    try:
        layers = await backend.layer_list()
    except DocumentQuarantineError:
        raise
    except Exception:
        return []
    for lyr in layers:
        expected = _EXPECTED_LAYER_COLOR.get(lyr.name)
        if expected is None:
            continue  # unknown layer — caller's responsibility
        if int(lyr.color) != int(expected):
            issues.append(
                Issue(
                    severity="warning",
                    focus="layer_color",
                    message=(
                        f"Layer '{lyr.name}' has color {lyr.color}; "
                        f"engineering standard expects {expected}."
                    ),
                    detail={"layer": lyr.name, "got": int(lyr.color), "expected": expected},
                )
            )
    return issues


async def _check_construction_left(backend: AutoCADBackend) -> list[Issue]:
    """No construction/scratch geometry may remain before drawing_finalize.

    Matches any layer whose name contains 'CONST' (CONSTRUCTION, M-CONST-E-N, …)
    so the check is independent of the active layer set — a leftover iso13567
    scaffold on M-CONST-E-N is caught just like a mech one on CONSTRUCTION.
    """
    try:
        ents = await backend.entity_list(limit=5000)
    except DocumentQuarantineError:
        raise
    except Exception:
        return []
    leftover = [e for e in ents if "CONST" in (e.layer or "").upper()]
    if not leftover:
        return []
    return [
        Issue(
            severity="error",
            focus="construction_left",
            message=(
                f"{len(leftover)} entity/entities still on a construction layer. "
                "Call construction_clear() before drawing_finalize."
            ),
            handles=[e.handle for e in leftover[:50]],
            detail={
                "count": len(leftover),
                "layers": sorted({e.layer for e in leftover if e.layer}),
            },
        )
    ]


async def _check_untrimmed_corner(backend: AutoCADBackend) -> list[Issue]:
    """Two LINE endpoints within `tol` but not exactly equal usually means a
    corner where one line overshoots — TRIM/EXTEND/FILLET was forgotten."""
    tol = 0.5  # mm — generous; tighten via PlanSpec.notes later
    try:
        lines = await backend.entity_list(type_filter="LINE", limit=5000)
    except DocumentQuarantineError:
        raise
    except Exception:
        return []
    endpoints: list[tuple[str, tuple[float, float]]] = []
    for ln in lines:
        s = ln.properties.get("start")
        e = ln.properties.get("end")
        if s and e:
            endpoints.append((ln.handle, (float(s[0]), float(s[1]))))
            endpoints.append((ln.handle, (float(e[0]), float(e[1]))))
    issues: list[Issue] = []
    n = len(endpoints)
    seen: set[frozenset[str]] = set()
    for i in range(n):
        h1, p1 = endpoints[i]
        for j in range(i + 1, n):
            h2, p2 = endpoints[j]
            if h1 == h2:
                continue
            d = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
            if 1e-9 < d <= tol:
                key = frozenset((h1, h2))
                if key in seen:
                    continue
                seen.add(key)
                issues.append(
                    Issue(
                        severity="warning",
                        focus="untrimmed_corner",
                        message=(
                            f"Lines {h1} and {h2} have endpoints {d:.3f} mm apart — "
                            "likely an untrimmed corner (use entity_trim or entity_fillet)."
                        ),
                        handles=[h1, h2],
                        detail={"gap_mm": d, "p1": list(p1), "p2": list(p2)},
                    )
                )
    return issues


async def _check_duplicate_entities(backend: AutoCADBackend) -> list[Issue]:
    """Two LINEs with identical endpoints (in either direction) are duplicates."""
    try:
        lines = await backend.entity_list(type_filter="LINE", limit=5000)
    except DocumentQuarantineError:
        raise
    except Exception:
        return []
    seen: dict[tuple, str] = {}
    issues: list[Issue] = []
    for ln in lines:
        s = ln.properties.get("start")
        e = ln.properties.get("end")
        if not (s and e):
            continue
        k1 = (round(s[0], 4), round(s[1], 4), round(e[0], 4), round(e[1], 4))
        k2 = (k1[2], k1[3], k1[0], k1[1])
        for key in (k1, k2):
            if key in seen:
                issues.append(
                    Issue(
                        severity="warning",
                        focus="duplicate_entities",
                        message=f"Lines {seen[key]} and {ln.handle} are duplicates.",
                        handles=[seen[key], ln.handle],
                    )
                )
                break
        else:
            seen[k1] = ln.handle
    return issues


def _dim_ref_point(e) -> tuple[float, float] | None:
    """Best-effort reference point for a dimension entity, tolerant of the
    per-backend property shape: explicit text/def points first, then the
    bounding-box centre (COM seeds `bounding_box` for every entity)."""
    for key in ("text_position", "defpoint", "insertion_point", "insertion"):
        ip = e.properties.get(key)
        if ip and len(ip) >= 2:
            return (float(ip[0]), float(ip[1]))
    bb = e.properties.get("bounding_box")
    try:
        if isinstance(bb, dict):
            mn, mx = bb.get("min"), bb.get("max")
            return ((mn[0] + mx[0]) / 2.0, (mn[1] + mx[1]) / 2.0)
        if bb and len(bb) == 2 and hasattr(bb[0], "__len__"):
            return ((bb[0][0] + bb[1][0]) / 2.0, (bb[0][1] + bb[1][1]) / 2.0)
        if bb and len(bb) >= 4:
            return ((bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0)
    except (TypeError, IndexError, KeyError):
        return None
    return None


async def _check_dim_overlap(backend: AutoCADBackend) -> list[Issue]:
    """Two dimensions whose reference points are within `tol` likely overlap.

    Filters by entity TYPE (not layer) so dims land-anywhere — including the
    iso13567 M-DIMEN-T-N layer — are still considered.
    """
    tol = 5.0  # mm
    try:
        ents = await backend.entity_list(limit=5000)
    except DocumentQuarantineError:
        raise
    except Exception:
        return []
    dim_points: list[tuple[str, tuple[float, float]]] = []
    for e in ents:
        if e.type not in _DIM_TYPES and "DIM" not in e.type:
            continue
        ip = _dim_ref_point(e)
        if ip is not None:
            dim_points.append((e.handle, ip))
    issues: list[Issue] = []
    seen: set[frozenset[str]] = set()
    for i, (h1, p1) in enumerate(dim_points):
        for j in range(i + 1, len(dim_points)):
            h2, p2 = dim_points[j]
            d = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
            if d <= tol:
                key = frozenset((h1, h2))
                if key in seen:
                    continue
                seen.add(key)
                issues.append(
                    Issue(
                        severity="warning",
                        focus="dim_overlap",
                        message=f"Dimensions {h1} and {h2} are {d:.2f} mm apart — likely overlapping.",
                        handles=[h1, h2],
                    )
                )
    return issues


async def _check_gdt(backend: AutoCADBackend) -> list[Issue]:
    """Every datum referenced by a feature control frame must have a matching
    datum feature symbol.

    ISO 1101 / ASME Y14.5: a position/orientation/runout FCF that cites datum A
    is meaningless unless datum A is actually established on the part. The base
    backend records referenced/defined datum letters as `draw_feature_control_frame`
    and `draw_datum_feature` run, so this check needs no geometry parsing and is
    identical on both engines.
    """
    referenced = set(getattr(backend, "_gdt_datums_referenced", None) or set())
    defined = set(getattr(backend, "_gdt_datums_defined", None) or set())
    missing = sorted(referenced - defined)
    if not missing:
        return []
    return [
        Issue(
            severity="error",
            focus="gdt",
            message=(
                f"Feature control frame(s) reference datum(s) {missing} with no "
                "matching datum feature. Add draw_datum_feature for each datum."
            ),
            detail={
                "missing_datums": missing,
                "defined": sorted(defined),
                "referenced": sorted(referenced),
            },
        )
    ]


_FOCUS_DISPATCH = {
    "iso128": _check_iso128,
    "layer_color": _check_layer_color,
    "dim_overlap": _check_dim_overlap,
    "untrimmed_corner": _check_untrimmed_corner,
    "duplicate_entities": _check_duplicate_entities,
    "construction_left": _check_construction_left,
    "gdt": _check_gdt,
}


async def run_critique(
    backend: AutoCADBackend,
    focus: list[CritiqueFocus] | None = None,
) -> list[Issue]:
    """Dispatch each requested focus check and return aggregated issues."""
    foci = list(focus) if focus is not None else list(ALL_CRITIQUE_FOCUSES)
    issues: list[Issue] = []
    for f in foci:
        check = _FOCUS_DISPATCH.get(f)
        if check is None:
            issues.append(
                Issue(
                    severity="warning",
                    focus=f,  # type: ignore[arg-type]
                    message=f"Unknown critique focus '{f}' (closed enum).",
                )
            )
            continue
        issues.extend(await check(backend))
    return issues
