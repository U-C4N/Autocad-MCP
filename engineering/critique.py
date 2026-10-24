"""Premium-quality critique checks for finished drawings.

Each focus runs an independent check; new focuses must be added to the
`CritiqueFocus` Literal in plan_spec.py *and* registered here. Closed enum
keeps drawing_critique scope from drifting.
"""

from __future__ import annotations

import asyncio
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
from .qa_scan import ScanCoverage, qa_snapshot

#: ``Issue.detail["code"]`` of a check that raised. A value, not a CritiqueFocus.
CHECK_FAILED = "check_failed"
#: ``Issue.detail["code"]`` of a check whose read did not cover the drawing.
#: The same word Task 10 uses as an error kind. Not a CritiqueFocus either.
SCAN_INCOMPLETE = "scan_incomplete"

if TYPE_CHECKING:
    from backends.base import AutoCADBackend


# Layer-name → expected ACI color, derived from ENGINEERING_LAYERS.
_EXPECTED_LAYER_COLOR: dict[str, int] = {
    name: color for name, color, _lt, _lw, _desc in ENGINEERING_LAYERS
}


async def _check_iso128(backend: AutoCADBackend) -> list[Issue]:
    """Layer lineweights must be in ISO_128_LINEWEIGHTS_MM (or -3=ByLayer
    sentinel meaning 'inherit')."""
    issues: list[Issue] = []
    layers = await backend.layer_list()
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

    height = float(effective_dimvar(doc, "dimtxt"))
    separator = int(effective_dimvar(doc, "dimdsep"))

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
    layers = await backend.layer_list()
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


async def _records(backend: AutoCADBackend, shared: dict, focus: str):
    """The run's snapshot records (every space), plus a ``scan_incomplete``
    issue for ``focus`` when the read did not cover the whole drawing."""
    snap, coverage = await qa_snapshot(backend, shared)
    issues = [] if coverage.complete else [scan_incomplete_issue(focus, coverage)]
    return (snap.records if snap is not None else ()), issues


def scan_incomplete_issue(focus: str, coverage: ScanCoverage) -> Issue:
    """The finding a check reports when its read did not cover the drawing."""
    return Issue(
        severity="error",
        focus=focus,  # type: ignore[arg-type]
        message=f"{focus} could not check the whole drawing: {coverage.reason}",
        detail={"code": SCAN_INCOMPLETE, **coverage.to_dict()},
    )


def _close_pairs(
    points: list[tuple[str, float, float]], tol: float
) -> list[tuple[int, int, float]]:
    """Every pair ``(i, j, d)``, ``i < j``, of points in the same space with
    ``d = hypot(p_i - p_j) <= tol``, sorted by ``(i, j)``.

    A grid hash replaces the all-pairs loop (8.9 s at 5,000 lines on the event
    loop, measured): the cell is the tolerance, so a partner lies in one of
    the nine cells around a point. The cell is widened by one part in a
    million so a float quotient at a cell boundary cannot hide a pair that is
    exactly ``tol`` apart. Sorting by ``(i, j)`` reproduces the order the
    all-pairs loop reported in.
    """
    cell = tol * (1.0 + 1e-6)
    grid: dict[tuple[str, int, int], list[int]] = {}
    keys = []
    for index, (space, x, y) in enumerate(points):
        key = (space, math.floor(x / cell), math.floor(y / cell))
        keys.append(key)
        grid.setdefault(key, []).append(index)
    pairs: list[tuple[int, int, float]] = []
    for index, (space, x, y) in enumerate(points):
        _space, cx, cy = keys[index]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((space, cx + dx, cy + dy), ()):
                    if other <= index:
                        continue
                    d = math.hypot(x - points[other][1], y - points[other][2])
                    if d <= tol:
                        pairs.append((index, other, d))
    pairs.sort()
    return pairs


async def _check_construction_left(backend: AutoCADBackend, shared: dict) -> list[Issue]:
    """No construction/scratch geometry may remain before drawing_finalize.

    Matches any layer whose name contains 'CONST' (CONSTRUCTION, M-CONST-E-N, …)
    so the check is independent of the active layer set — a leftover iso13567
    scaffold on M-CONST-E-N is caught just like a mech one on CONSTRUCTION.
    Every entity of every space is read (``engineering/qa_scan.py``).
    """
    records, issues = await _records(backend, shared, "construction_left")
    leftover = [r for r in records if "CONST" in (r.layer or "").upper()]
    if leftover:
        issues.append(
            Issue(
                severity="error",
                focus="construction_left",
                message=(
                    f"{len(leftover)} entity/entities still on a construction layer. "
                    "Call construction_clear() before drawing_finalize."
                ),
                handles=[r.handle for r in leftover[:50]],
                detail={
                    "count": len(leftover),
                    "layers": sorted({r.layer for r in leftover if r.layer}),
                    "spaces": sorted({r.space for r in leftover}),
                },
            )
        )
    return issues


#: Two LINE ends this close but not equal are an untrimmed corner (mm).
UNTRIMMED_TOL_MM = 0.5


def _untrimmed_issues(records) -> list[Issue]:
    endpoints = [
        (r.handle, r.space, point)
        for r in records
        if r.type == "LINE" and len(r.points) == 2
        for point in r.points
    ]
    pairs = _close_pairs([(space, p[0], p[1]) for _h, space, p in endpoints], UNTRIMMED_TOL_MM)
    issues: list[Issue] = []
    seen: set[frozenset[str]] = set()
    for i, j, d in pairs:
        h1, space, p1 = endpoints[i]
        h2, _space, p2 = endpoints[j]
        if h1 == h2 or d <= 1e-9:
            continue
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
                detail={"gap_mm": d, "p1": list(p1), "p2": list(p2), "space": space},
            )
        )
    return issues


async def _check_untrimmed_corner(backend: AutoCADBackend, shared: dict) -> list[Issue]:
    """Two LINE endpoints within ``UNTRIMMED_TOL_MM`` but not exactly equal usually
    means a corner where one line overshoots — TRIM/EXTEND/FILLET was forgotten.
    Ends are compared only within one space; the arithmetic runs off the event loop."""
    records, issues = await _records(backend, shared, "untrimmed_corner")
    return issues + await asyncio.to_thread(_untrimmed_issues, records)


async def _check_duplicate_entities(backend: AutoCADBackend, shared: dict) -> list[Issue]:
    """Two LINEs of one space with identical endpoints (in either direction)."""
    records, issues = await _records(backend, shared, "duplicate_entities")
    seen: dict[tuple, str] = {}
    for r in records:
        if r.type != "LINE" or len(r.points) != 2:
            continue
        (sx, sy), (ex, ey) = r.points
        k1 = (r.space, round(sx, 4), round(sy, 4), round(ex, 4), round(ey, 4))
        k2 = (r.space, k1[3], k1[4], k1[1], k1[2])
        for key in (k1, k2):
            if key in seen:
                issues.append(
                    Issue(
                        severity="warning",
                        focus="duplicate_entities",
                        message=f"Lines {seen[key]} and {r.handle} are duplicates.",
                        handles=[seen[key], r.handle],
                        detail={"space": r.space},
                    )
                )
                break
        else:
            seen[k1] = r.handle
    return issues


def _dim_ref_point(record) -> tuple[float, float] | None:
    """A dimension record's reference point: its text midpoint (the snapshot
    stores ``(defpoint, text midpoint)``, the midpoint falling back to the
    defpoint when the DXF has none), else its box centre."""
    if len(record.points) >= 2:
        return record.points[1]
    if record.points:
        return record.points[0]
    if record.bbox is not None:
        x0, y0, x1, y1 = record.bbox
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    return None


#: Two dimension reference points this close are an overlap (mm).
DIM_OVERLAP_TOL_MM = 5.0


def _dim_overlap_issues(records) -> list[Issue]:
    dims = []
    for r in records:
        if "DIMENSION" not in r.type:
            continue
        point = _dim_ref_point(r)
        if point is not None:
            dims.append((r.handle, r.space, point))
    pairs = _close_pairs([(space, p[0], p[1]) for _h, space, p in dims], DIM_OVERLAP_TOL_MM)
    issues: list[Issue] = []
    seen: set[frozenset[str]] = set()
    for i, j, d in pairs:
        h1, space, _p1 = dims[i]
        h2, _space, _p2 = dims[j]
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
                detail={"space": space},
            )
        )
    return issues


async def _check_dim_overlap(backend: AutoCADBackend, shared: dict) -> list[Issue]:
    """Two dimensions of one space whose reference points are within
    ``DIM_OVERLAP_TOL_MM`` likely overlap.

    Filters by DXF entity TYPE (not layer) so dims land-anywhere — including the
    iso13567 M-DIMEN-T-N layer — are still considered; the snapshot carries DXF
    names on both engines, so ActiveX's ROTATEDDIMENSION spelling no longer matters.
    """
    records, issues = await _records(backend, shared, "dim_overlap")
    return issues + await asyncio.to_thread(_dim_overlap_issues, records)


for _check in (
    _check_construction_left,
    _check_untrimmed_corner,
    _check_duplicate_entities,
    _check_dim_overlap,
):
    _check.needs_shared = True  # type: ignore[attr-defined]


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

from .pid.critique import PID_DISPATCH  # noqa: E402  (registered after the mechanical focuses)

_FOCUS_DISPATCH.update(PID_DISPATCH)

from .mech.critique import MECH_DISPATCH  # noqa: E402  (same reason: after the base focuses)

_FOCUS_DISPATCH.update(MECH_DISPATCH)

from .arch.critique import ARCH_DISPATCH  # noqa: E402  (same reason: after the base focuses)

_FOCUS_DISPATCH.update(ARCH_DISPATCH)

from .understand.critique import TOPO_DISPATCH  # noqa: E402  (same reason: after the base focuses)

_FOCUS_DISPATCH.update(TOPO_DISPATCH)


async def _run_check(check, focus: str, backend: AutoCADBackend, shared: dict) -> list[Issue]:
    """One check; a failure becomes an ``error`` issue of that focus, never silence.

    A ``DocumentQuarantineError`` propagates: the document is untrusted and the
    caller must be told so, not handed a finding about it.
    """
    try:
        if getattr(check, "needs_shared", False):
            return await check(backend, shared)
        return await check(backend)
    except DocumentQuarantineError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as check_failed below
        error = f"{type(exc).__name__}: {exc}"
        return [
            Issue(
                severity="error",
                focus=focus,  # type: ignore[arg-type]
                message=f"The {focus} check failed and checked nothing: {error}",
                detail={"code": CHECK_FAILED, "error": error},
            )
        ]


async def run_critique(
    backend: AutoCADBackend,
    focus: list[CritiqueFocus] | None = None,
    *,
    shared: dict | None = None,
) -> list[Issue]:
    """Dispatch each requested focus check and return aggregated issues.

    ``shared`` is the run's context: the all-spaces QA snapshot
    (``engineering/qa_scan.py``) and the domain indexes (P&ID graph,
    mechanical and architectural indexes, topology) are built once into it.
    Pass the dict the validator used to reuse its read (``drawing_finalize``
    does); a dict is only valid while the drawing does not change. None starts
    a fresh one.

    A check that raises yields one ``error`` issue with
    ``detail["code"] == "check_failed"``; a read that did not cover the whole
    drawing yields ``detail["code"] == "scan_incomplete"``. Neither code is a
    focus. A quarantine refusal propagates.
    """
    foci = list(focus) if focus is not None else list(ALL_CRITIQUE_FOCUSES)
    issues: list[Issue] = []
    if shared is None:
        shared = {}
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
        issues.extend(await _run_check(check, f, backend, shared))
    return issues
