"""The ``dimension_truth`` critique focus and its repair (v1.8.0, plan 1.8-15).

A DIMENSION can lie in more ways than a typed override (v1.7.0 caught that
one, and only for dimensions a requirement named). This focus binds **every**
DIMENSION of every space with ``bind_dimension`` - the observed geometry is
built once per run and indexed on a grid (``DimensionIndex``) - and reports,
one issue per dimension and code:

* ``dim_override_disagrees`` (error) - a text override's number is not the
  geometry; an override marked ``NTS`` (not to scale) is ``info``. A value
  shown from the measurement that disagrees beyond its display rounding is
  reported under this code too (``detail.finding`` names which).
* ``dim_dimlfac_scaled`` (warning) - DIMLFAC makes the sheet show another
  number than the geometry (a scaled detail can be meant; said, not hidden).
* ``dim_render_stale`` (error) - the ``*D`` block shows another text than the
  fields and the style produce (``engineering/dimtext.py``): a style changed
  with ``rerender=False``, a transform the block did not follow.
  ``drawing_refine`` repairs it with the backend's ``dimension_rerender``.
* ``dim_group42_stale`` (warning) - DXF group 42, the measurement stored at
  the last render, is not the measurement now. Repaired the same way.
* ``dim_double_prefix`` (warning) - a diameter / radius override that puts its
  own Ø / R right before ``<>``, so the sheet shows ØØ / RR. A warning until
  spike S4 confirms the double sign live (then an error).
* ``dim_value_hidden`` (error) - the value cannot be read off the sheet: the
  text suppressed (``" "``) or an override with no digit, DIMTXT 0, 90 % or
  more transparency, a text colour equal to its own background fill
  (DIMTFILL 2, DIMTFILLCLR), the dimension invisible, its layer off, frozen or
  not plotted.
* ``dim_unbound`` (info) - the dimension's points lie on no feature (or on
  two equal ones, or its type is not bound): it is *not checked*, never a pass.

Only ``dim_render_stale`` and ``dim_group42_stale`` carry ``repairable:
true``; the refiner leaves every other code to a person (``manual_required``).
The read is the run's all-spaces QA snapshot (``engineering/qa_scan.py``); on
the live engine that is the ``Document.Export`` DXF, read by the same code.
INSERTs are exploded for binding only when the drawing holds a DIMENSION.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import TYPE_CHECKING

from .dimtext import display, double_prefix, has_digit, normalize, rendered_matches, style_dict
from .plan_spec import Issue

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

FOCUS = "dimension_truth"
DIM_OVERRIDE_DISAGREES = "dim_override_disagrees"
DIM_DIMLFAC_SCALED = "dim_dimlfac_scaled"
DIM_RENDER_STALE = "dim_render_stale"
DIM_GROUP42_STALE = "dim_group42_stale"
DIM_DOUBLE_PREFIX = "dim_double_prefix"
DIM_VALUE_HIDDEN = "dim_value_hidden"
DIM_UNBOUND = "dim_unbound"
CODES = (
    DIM_OVERRIDE_DISAGREES,
    DIM_DIMLFAC_SCALED,
    DIM_RENDER_STALE,
    DIM_GROUP42_STALE,
    DIM_DOUBLE_PREFIX,
    DIM_VALUE_HIDDEN,
    DIM_UNBOUND,
)
#: The codes ``dimension_rerender`` repairs.
REPAIRABLE = frozenset({DIM_RENDER_STALE, DIM_GROUP42_STALE})
#: ``shared`` key of the run's observed geometry.
OBSERVED_KEY = "dimension_truth_observed"
#: A transparency at or above this share hides the text (90 %).
HIDDEN_TRANSPARENCY = 0.9
#: Group 42 of an angle is in radians; this is its tolerance.
ANGLE_EPSILON = 1e-6

_NTS = re.compile(r"\bNTS\b", re.IGNORECASE)
_SEVERITY = {
    DIM_OVERRIDE_DISAGREES: "error",
    DIM_DIMLFAC_SCALED: "warning",
    DIM_RENDER_STALE: "error",
    DIM_GROUP42_STALE: "warning",
    DIM_DOUBLE_PREFIX: "warning",
    DIM_VALUE_HIDDEN: "error",
    DIM_UNBOUND: "info",
}


def _transparency_share(raw: int | None) -> float | None:
    """DXF group 440 as a share (0 opaque .. 1 clear); None for ByLayer / ByBlock."""
    if raw is None:
        return None
    raw = int(raw)
    if not raw & 0x02000000:  # not an explicit alpha (ByBlock is 0x01000000)
        return None
    return 1.0 - (raw & 0xFF) / 255.0


def hidden_reasons(record, layers: dict) -> list[str]:
    """Why the record's value cannot be read off the sheet (empty: it can)."""
    _dimtype, _defpoints, _measurement, override, *_ = record.dimension
    style = style_dict(record.dim_vars) or {}
    reasons: list[str] = []
    if override == " ":
        reasons.append("the text is suppressed (override ' ')")
    elif override and override != "<>" and "<>" not in override:
        if not has_digit(normalize(override.replace("\\P", " "))):
            reasons.append(f"the override {override!r} carries no digit")
    dimtxt = style.get("dimtxt")
    if dimtxt is not None and float(dimtxt) <= 0.0:
        reasons.append("DIMTXT is 0")
    share = _transparency_share(getattr(record, "transparency", None))
    if share is not None and share >= HIDDEN_TRANSPARENCY:
        reasons.append(f"{share:.0%} transparent")
    if int(style.get("dimtfill") or 0) == 2:
        text_colour = style.get("dimclrt")
        if text_colour in (None, 0, 256):
            text_colour = record.color
        if text_colour == 256:
            text_colour = int((layers.get(record.layer) or {}).get("color", 7))
        if text_colour == int(style.get("dimtfillclr") or 0):
            reasons.append(f"the text colour {text_colour} is its own background fill")
    if not record.visible:
        reasons.append("the dimension is invisible")
    layer = layers.get(record.layer) or {}
    if layer.get("on") is False:
        reasons.append(f"layer {record.layer!r} is off")
    if layer.get("frozen") is True:
        reasons.append(f"layer {record.layer!r} is frozen")
    if layer.get("plot") is False:
        reasons.append(f"layer {record.layer!r} does not plot")
    return reasons


def _group42_stale(record, epsilon: float) -> tuple[bool, float | None]:
    dimtype, _defpoints, measurement, *_ = record.dimension
    if record.group42 is None or measurement is None:
        return False, None
    if dimtype in (2, 5):
        expected = math.radians(float(measurement))
        return abs(record.group42 - expected) > ANGLE_EPSILON, expected
    expected = float(measurement)
    return abs(record.group42 - expected) > max(epsilon, 1e-9), expected


def _issue(code: str, record, message: str, detail: dict, *, severity: str | None = None) -> Issue:
    return Issue(
        severity=severity or _SEVERITY[code],  # type: ignore[arg-type]
        focus=FOCUS,  # type: ignore[arg-type]
        message=f"Dimension {record.handle} ({record.space}): {message}",
        handles=[record.handle],
        detail={
            "code": code,
            "handle": record.handle,
            "space": record.space,
            "layer": record.layer,
            "repairable": code in REPAIRABLE,
            **detail,
        },
    )


def dimension_issues(records, observed, layers: dict, epsilon: float) -> list[Issue]:
    """Every code of every DIMENSION record, in record order then ``CODES`` order."""
    from .verification.binding import DimensionIndex, bind_dimension

    index = DimensionIndex(observed, epsilon)
    issues: list[Issue] = []
    for record in records:
        if record.type != "DIMENSION" or record.dimension is None:
            continue
        dimtype, _defpoints, measurement, override, _style, dimlfac, _angle = record.dimension
        bound = bind_dimension(record, observed, epsilon=epsilon, index=index, across_edges=True)
        shown = display(record.dimension, record.dim_vars)
        evidence = {
            "displayed_text": shown.text,
            "displayed_value": shown.value,
            "display_rounding": shown.display_rounding,
            "geometric_value": bound["geometric_value"],
            "measurement": measurement,
            "dimlfac": dimlfac,
        }
        found: dict[str, Issue] = {}
        hidden = hidden_reasons(record, layers)
        hidden_by_text = override == " " or any("override" in r for r in hidden)
        findings = bound["findings"]

        if "text_override_disagrees_with_geometry" in findings and not hidden_by_text:
            nts = bool(_NTS.search(override or ""))
            found[DIM_OVERRIDE_DISAGREES] = _issue(
                DIM_OVERRIDE_DISAGREES,
                record,
                f"the override {override!r} shows {shown.value!r}; the geometry measures "
                f"{bound['geometric_value']:g}" + (" (marked NTS: not to scale)" if nts else ""),
                {**evidence, "finding": "text_override_disagrees_with_geometry", "nts": nts},
                severity="info" if nts else None,
            )
        elif (
            "displayed_value_disagrees_with_geometry" in findings
            or "measurement_disagrees_with_geometry" in findings
        ):
            found[DIM_OVERRIDE_DISAGREES] = _issue(
                DIM_OVERRIDE_DISAGREES,
                record,
                f"it shows {shown.text!r} where the geometry measures {bound['geometric_value']:g}",
                {
                    **evidence,
                    "finding": next(f for f in findings if f.endswith("disagrees_with_geometry")),
                    "nts": False,
                },
            )
        if "dimlfac_scales_the_displayed_value" in findings:
            found[DIM_DIMLFAC_SCALED] = _issue(
                DIM_DIMLFAC_SCALED,
                record,
                f"DIMLFAC {dimlfac:g} makes it show {shown.text!r} over "
                f"{bound['geometric_value']:g} of geometry",
                evidence,
            )
        if rendered_matches(record.rendered_text, record.dimension, record.dim_vars) is False:
            found[DIM_RENDER_STALE] = _issue(
                DIM_RENDER_STALE,
                record,
                f"its block shows {record.rendered_text!r}, its fields and style say "
                f"{shown.text!r}. drawing_refine redraws it.",
                {**evidence, "rendered_text": record.rendered_text},
            )
        stale42, expected = _group42_stale(record, epsilon)
        if stale42:
            found[DIM_GROUP42_STALE] = _issue(
                DIM_GROUP42_STALE,
                record,
                f"group 42 stores {record.group42:g}, the measurement is {expected:g}. "
                "drawing_refine redraws it.",
                {**evidence, "group42": record.group42, "expected_group42": expected},
            )
        if double_prefix(dimtype, override):
            sign = "Ø" if dimtype == 3 else "R"
            found[DIM_DOUBLE_PREFIX] = _issue(
                DIM_DOUBLE_PREFIX,
                record,
                f"the override {override!r} adds its own {sign} before <>, which already "
                f"carries one: the sheet shows {shown.text!r} (S4: live double sign pending)",
                {**evidence, "override": override},
            )
        if hidden:
            found[DIM_VALUE_HIDDEN] = _issue(
                DIM_VALUE_HIDDEN,
                record,
                "its value cannot be read off the sheet: " + "; ".join(hidden),
                {**evidence, "reasons": hidden},
            )
        if bound["status"] != "bound":
            found[DIM_UNBOUND] = _issue(
                DIM_UNBOUND,
                record,
                f"not checked against geometry ({bound['status']}: {bound['reason']}) - "
                "never a pass",
                {**evidence, "status": bound["status"], "reason": bound["reason"]},
            )
        issues.extend(found[code] for code in CODES if code in found)
    return issues


async def _block_definitions(backend, snap):
    """The block definitions the snapshot was read without, for exploding."""
    if backend.name == "com":
        from .understand.snapshot import take_snapshot

        return (await take_snapshot(backend, block_defs=True)).blocks
    from .qa_scan import _async_readonly
    from .understand.snapshot import _block_records

    return await _async_readonly(backend, lambda: _block_records(backend._require_doc(), False))


async def _observed(backend, snap):
    from .verification.observe import observe

    if snap.blocks is None and any(r.type == "INSERT" for r in snap.records):
        snap = replace(snap, blocks=await _block_definitions(backend, snap))
    return observe(snap)


async def check_dimension_truth(backend: AutoCADBackend, shared: dict) -> list[Issue]:
    """The focus: every DIMENSION of every space, bound once per run."""
    from .critique import _records
    from .qa_scan import qa_snapshot, shared_index
    from .verification.measure import default_epsilon

    records, issues = await _records(backend, shared, FOCUS)
    if issues or not any(r.type == "DIMENSION" for r in records):
        return issues
    snap, _coverage = await qa_snapshot(backend, shared)

    async def build(_backend):
        return await _observed(backend, snap)

    observed = await shared_index(shared, OBSERVED_KEY, build, backend)
    return dimension_issues(records, observed, snap.layers, default_epsilon(observed))


check_dimension_truth.needs_shared = True  # type: ignore[attr-defined]


async def repair_dimension_truth(backend: AutoCADBackend, issue: Issue) -> dict:
    """Redraw the dimension of a ``dim_render_stale`` / ``dim_group42_stale``
    issue with the backend's ``dimension_rerender``. Any other code is refused:
    the refiner never sends one (``repairable: false``)."""
    code = str(issue.detail.get("code"))
    if code not in REPAIRABLE:
        raise RuntimeError(f"dimension_truth repair: {code!r} is not repaired automatically")
    result = await backend.dimension_rerender([str(issue.detail["handle"])])
    return {
        "code": code,
        "handle": issue.detail["handle"],
        "rerendered": list(result.get("rerendered", [])),
        "verified": bool(result.get("verified", False)),
    }
