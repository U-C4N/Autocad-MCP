"""Dimension intents for a view, laid out so no two texts overlap.

Pure: no backend, no async, no DXF. The features declare what they want
dimensioned; this module adds the part's own sizes, removes the duplicates
(ISO 129-1: each dimension appears once), resolves ISO 286 fits and ISO 129
tolerances through the modules that already own them, and then *places* the
texts.

The placement is not a matter of taste. Every intent's text is measured into a
rectangle by :func:`text_rects`, and an intent is put on the lowest free level
whose rectangle does not overlap one already placed on that level. Two
rectangles are disjoint when they are separated along *either* axis, so the
layout separates them along both in turn: within a level, along the axis the
level runs in; between levels, along the axis the levels stack in. The second
one is why the level pitch is clamped to the extent of the text *in the
stacking direction* - which is the text height for the row of dimensions under
the view, and the text **width** for the column of diameters beside it. A pitch
clamped only to the height would let two 9.45 mm wide diameter labels sit
8 mm apart on the same centreline, overlapping by 1.45 mm.

The test asserts pairwise disjointness with the same helper, which makes "no
overlapping dimension text" measured rather than claimed - and it is the same
property the repository's `dim_overlap` critique focus checks on the finished
drawing.

Three rules the module enforces rather than assumes:

*ISO 286 fits reach the pipeline.* :func:`dimension_intents` takes
``fits={key: code}`` and stamps the code onto the intent it names, so a bearing
seat gets its k6 without anyone hand-building a ``DimIntent``. The part's own
diameters are addressable by segment (``"segment[1]"``, ``"segment[1].bore"``);
a feature's are addressable by its id. An unknown key is refused by name - a fit
that lands nowhere is a manufacturing requirement lost in silence.

*An explicit tolerance is validated as a whole.* ``DimIntent.tol`` is
``{"mode", "upper", "lower"}`` and nothing else. ``mode`` is required, because
``build_dim_override`` reads a missing mode as ``"none"`` and returns an empty
override: a +-0.05 with no ``mode`` used to resolve, report its own numbers back
and then draw a dimension carrying no tolerance at all.

*ISO 129-1: each measurement appears once.* A closed chain plus the overall
states the overall twice. :func:`redundant_linear` finds the span that the
others tile end to end, and :func:`dimension_intents` marks it as an auxiliary
(reference) dimension - ``(<>)``, drawn in parentheses - rather than emitting a
fourth plain DIMENSION that contradicts nothing and adds nothing.
"""

from __future__ import annotations

import math

from engineering.fits import fit_lookup, parse_fit_code
from engineering.mech.features import Hole
from engineering.mech.part import (
    PrismaticPart,
    RevolvedPart,
    outline_bbox,
    part_length,
    segment_bounds,
)
from engineering.mech.primitives import DimIntent
from engineering.tolerances import TOL_MODES, build_dim_override

_EPS = 1e-9

DIM_STYLES: tuple[str, ...] = ("chain", "baseline", "ordinate")

#: Mean glyph width as a fraction of the text height, for measuring a label
#: into a rectangle. Deliberately generous: over-estimating the width can only
#: push two dimensions further apart.
TEXT_WIDTH_FACTOR = 0.7

#: Padding added around a measured label, in text heights.
TEXT_PADDING = 0.6

#: How an auxiliary (reference) dimension is written - ISO 129-1 puts a
#: measurement that is already given elsewhere in parentheses. ``<>`` is the
#: DIMENSION text placeholder for the measured value, on both engines.
REFERENCE_FORMAT = "(<>)"

#: The only keys ``DimIntent.tol`` accepts.
TOL_KEYS: frozenset[str] = frozenset({"mode", "upper", "lower"})

#: Tolerance modes that write no deviation, so carrying upper/lower with them
#: would silently discard the numbers.
_NO_DEVIATION_MODES: frozenset[str] = frozenset({"none", "basic"})


def measured_value(intent: DimIntent) -> float:
    """What the dimension measures, in drawing units."""
    p1, p2 = intent.p1, intent.p2
    if intent.kind == "linear":
        dx, dy = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
        return dx if dx >= dy else dy
    return math.dist(p1, p2)


def _format(value: float) -> str:
    return f"{round(value, 3):g}"


def _with_fit(text: str, fit: str | None) -> str:
    """Append an ISO 286 code to a dimension's text, once.

    A feature that already names itself (a hole's ``⌀6``) keeps its own text and
    gains the code; without this the deviations would be written and the code
    the inspector reads would not be on the sheet.
    """
    code = (fit or "").strip()
    if not code or code in text:
        return text
    return f"{text} {code}"


def _measured_text(intent: DimIntent) -> str:
    if intent.kind == "diameter":
        return f"⌀{_format(measured_value(intent))}"
    if intent.kind == "radius":
        return f"R{_format(measured_value(intent))}"
    return _format(measured_value(intent))


def intent_text(intent: DimIntent) -> str:
    """The text a dimension will carry, as the engine will render it.

    ``<>`` is the DIMENSION placeholder for the measured value, so an override
    of ``"(<>)"`` reads ``"(75)"`` and a resolved fit's ``"<> H7"`` reads
    ``"⌀40 H7"``. Expanding it here is what makes :func:`text_rects` measure the
    string that is actually drawn - a label measured as ``"<> H7"`` and drawn as
    ``"⌀40 H7"`` is a layout computed against the wrong width.
    """
    measured = _measured_text(intent)
    base = intent.text_override.replace("<>", measured) if intent.text_override else measured
    # The fit code is appended to the text by `resolve_tolerance`; the rectangle
    # has to reserve room for it before the layout runs.
    return _with_fit(base, intent.fit)


def is_reference(intent: DimIntent) -> bool:
    """True for an auxiliary (reference) dimension - ISO 129-1 parentheses."""
    override = intent.text_override or ""
    return override.startswith("(") and override.endswith(")")


def _text_size(intent: DimIntent, height: float) -> tuple[float, float]:
    """``(width, height)`` of the rectangle this intent's label occupies."""
    label = intent_text(intent)
    return (
        (len(label) * TEXT_WIDTH_FACTOR + TEXT_PADDING) * height,
        height * (1.0 + TEXT_PADDING),
    )


def text_rects(intents, *, height: float = 3.5):
    """``(x0, y0, x1, y1)`` per placed intent, from its text and its ``text_at``."""
    rects = []
    for intent in intents:
        if intent.text_at is None:
            raise ValueError(
                f"text_rects: intent {intent.feature or intent.kind!r} has no text_at; "
                "run layout_dimensions first."
            )
        width, tall = _text_size(intent, height)
        cx, cy = float(intent.text_at[0]), float(intent.text_at[1])
        rects.append((cx - width / 2.0, cy - tall / 2.0, cx + width / 2.0, cy + tall / 2.0))
    return tuple(rects)


# -- tolerances --------------------------------------------------------------


def _explicit_tol(intent: DimIntent) -> tuple[str, float | None, float | None]:
    """Validate ``intent.tol`` as a whole and return ``(mode, upper, lower)``.

    ``build_dim_override`` alone is not enough: it defaults a missing mode to
    ``"none"`` and answers with an empty override, so ``{"upper": 0.05,
    "lower": 0.05}`` used to resolve, echo its own numbers back and draw a
    dimension with no DIMTOL/DIMTP/DIMTM at all. A tolerance that disappears
    between the spec and the sheet is the silent wrong value this repository
    refuses, so the schema is checked here, by name.
    """
    who = intent.feature or intent.kind
    tol = intent.tol
    if not isinstance(tol, dict):
        raise ValueError(
            f"{who}.tol: expected a dict with keys {sorted(TOL_KEYS)}, got {type(tol).__name__}."
        )
    unknown = set(tol) - TOL_KEYS
    if unknown:
        raise ValueError(
            f"{who}.tol: unknown key(s) {sorted(unknown)}; a tolerance takes {sorted(TOL_KEYS)}."
        )
    if "mode" not in tol:
        raise ValueError(
            f"{who}.tol: needs an explicit 'mode' (one of {list(TOL_MODES)}). Without it the "
            "deviations are carried in the payload and no tolerance reaches the drawing."
        )
    mode = str(tol["mode"]).strip().lower()
    upper = tol.get("upper")
    lower = tol.get("lower")
    if mode in _NO_DEVIATION_MODES and (upper is not None or lower is not None):
        raise ValueError(
            f"{who}.tol: tol_mode={mode!r} writes no deviation, but upper={upper!r} / "
            f"lower={lower!r} were given. Use 'symmetric', 'deviation' or 'limit', or drop "
            "the values."
        )
    return mode, upper, lower


def resolve_tolerance(intent: DimIntent, nominal: float | None = None) -> dict:
    """Turn an intent's ``fit`` / ``tol`` into the backend dimension call's arguments.

    Mirrors `server.py::_fit_to_tolerances`, including its sign convention:
    ``build_dim_override`` reads ``tol_lower`` as the *magnitude* of the minus
    deviation, so a resolved fit passes ``-deviation.lower_mm``.
    """
    has_tol = intent.tol is not None
    if intent.fit and has_tol:
        raise ValueError(
            f"{intent.feature or intent.kind}: pass either fit=<ISO 286 code> or an explicit "
            "tol, not both."
        )
    if intent.fit:
        size = float(nominal) if nominal is not None else measured_value(intent)
        if size <= _EPS:
            raise ValueError(
                f"{intent.feature or intent.kind}: fit {intent.fit!r} needs a nominal size, and "
                f"the intent measures {size}. Pass nominal= explicitly."
            )
        deviation = fit_lookup(intent.fit, size)
        return {
            "tol_upper": deviation.upper_mm,
            "tol_lower": -deviation.lower_mm,
            "tol_mode": "deviation",
            "text_override": _with_fit(intent.text_override or "<>", deviation.code),
        }
    if has_tol:
        mode, upper, lower = _explicit_tol(intent)
        build_dim_override(upper, lower, mode, intent.text_override)  # validates, or raises
        return {
            "tol_upper": upper,
            "tol_lower": lower,
            "tol_mode": mode,
            "text_override": intent.text_override,
        }
    return {
        "tol_upper": None,
        "tol_lower": None,
        "tol_mode": "none",
        "text_override": intent.text_override,
    }


# -- collecting the intents --------------------------------------------------


def _dedupe(intents) -> tuple[DimIntent, ...]:
    seen: set[tuple] = set()
    out: list[DimIntent] = []
    for intent in intents:
        key = (
            intent.kind,
            round(float(intent.p1[0]), 6),
            round(float(intent.p1[1]), 6),
            round(float(intent.p2[0]), 6),
            round(float(intent.p2[1]), 6),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(intent)
    return tuple(out)


def _linear_span(intent: DimIntent) -> tuple[str, float, float, float] | None:
    """``(axis, level, lo, hi)`` of an axis-parallel linear dimension, else None.

    ``level`` is the coordinate the dimension does *not* run along, so two
    horizontal dimensions at different heights are never compared with each
    other. A skewed linear intent has no span: it is not part of a chain.
    """
    if intent.kind != "linear":
        return None
    (ax, ay), (bx, by) = intent.p1, intent.p2
    if abs(bx - ax) >= abs(by - ay):
        if abs(by - ay) > _EPS:
            return None
        return ("x", round(float(ay), 6), round(min(ax, bx), 6), round(max(ax, bx), 6))
    if abs(bx - ax) > _EPS:
        return None
    return ("y", round(float(ax), 6), round(min(ay, by), 6), round(max(ay, by), 6))


def redundant_linear(intents) -> tuple[int, ...]:
    """Indices of the linear intents that shorter ones already tile end to end.

    ISO 129-1: a measurement appears once. A closed chain (20 + 40 + 15) that
    also carries the overall (75) states the overall twice, and the second
    statement has to be an auxiliary (reference) dimension or be left off.

    The error is one-sided on purpose. A tiling that is found is a real one -
    the pieces butt, in order, from ``lo`` to ``hi`` on the same axis and level
    - so a dimension is never called redundant when it is not. A tiling that
    the greedy walk misses simply leaves the dimension plain, which is the way
    round the repository's critique rule wants: a check that misses a bad
    drawing costs a review, a check that calls a good drawing bad gets
    switched off.
    """
    groups: dict[tuple[str, float], list[tuple[float, float, int]]] = {}
    for index, intent in enumerate(intents):
        span = _linear_span(intent)
        if span is None:
            continue
        axis, level, lo, hi = span
        groups.setdefault((axis, level), []).append((lo, hi, index))

    out: list[int] = []
    for group in groups.values():
        for lo, hi, index in group:
            pieces = sorted(
                (a, b)
                for a, b, other in group
                if other != index
                and a >= lo - 1e-6
                and b <= hi + 1e-6
                and (b - a) < (hi - lo) - 1e-6
            )
            cursor = lo
            for a, b in pieces:
                if abs(a - cursor) <= 1e-6:
                    cursor = b
            if cursor > lo + 1e-6 and abs(cursor - hi) <= 1e-6:
                out.append(index)
    return tuple(sorted(out))


def _mark_references(intents: list[DimIntent]) -> list[DimIntent]:
    """Put the redundant measurements in parentheses, ISO 129-1."""
    for index in redundant_linear(intents):
        intent = intents[index]
        if intent.text_override is None:
            intents[index] = intent._replace(text_override=REFERENCE_FORMAT)
    return intents


def _apply_fits(intents: list[DimIntent], fits, *, table_ids: frozenset[str]) -> list[DimIntent]:
    """Stamp each ``{key: ISO 286 code}`` onto the intent its key names.

    Without this the ``fit`` branch of :func:`resolve_tolerance` is reachable
    only from a hand-built ``DimIntent``: no feature and no segment carries a
    fit, so the ISO 286 path would be real in the table and absent from every
    drawing the module actually produces.
    """
    if not fits:
        return intents
    if not isinstance(fits, dict):
        raise ValueError(
            f"fits: expected a dict of {{key: ISO 286 code}}, got {type(fits).__name__}."
        )

    available = {intent.feature for intent in intents if intent.feature}
    by_key: dict[str, list[int]] = {}
    for index, intent in enumerate(intents):
        if intent.feature:
            by_key.setdefault(intent.feature, []).append(index)

    for key, code in fits.items():
        name = str(key)
        if name not in available:
            if name in table_ids:
                raise ValueError(
                    f"fits[{name!r}]: that hole's diameter moved into the hole table, which has "
                    "no tolerance column. Raise hole_table_threshold, or dimension the hole "
                    "directly."
                )
            raise ValueError(
                f"fits[{name!r}]: nothing in this view is dimensioned under that name. "
                f"Available: {sorted(available)}."
            )
        parse_fit_code(str(code))  # refuses a typo here, not at draw time
        for index in by_key[name]:
            intent = intents[index]
            if is_reference(intent):
                raise ValueError(
                    f"fits[{name!r}]: {intent_text(intent)} is an auxiliary (reference) "
                    "dimension - ISO 129-1 gives it no tolerance. Put the fit on the "
                    "dimension that states the size."
                )
            if intent.tol is not None:
                raise ValueError(
                    f"fits[{name!r}]: that dimension already carries an explicit tol. Pass "
                    "either fit=<ISO 286 code> or an explicit tol, not both."
                )
            intents[index] = intent._replace(fit=str(code))
    return intents


def _revolved_axial(part: RevolvedPart, style: str) -> list[DimIntent]:
    stations = [0.0] + [x1 for _, x1 in segment_bounds(part)]
    intents: list[DimIntent] = []
    if style == "chain":
        for a, b in zip(stations, stations[1:], strict=False):
            intents.append(DimIntent(kind="linear", p1=(a, 0.0), p2=(b, 0.0), feature="part"))
        intents.append(
            DimIntent(kind="linear", p1=(0.0, 0.0), p2=(part_length(part), 0.0), feature="part")
        )
    else:  # baseline and ordinate both measure from the left face
        for station in stations[1:]:
            intents.append(
                DimIntent(kind="linear", p1=(0.0, 0.0), p2=(station, 0.0), feature="part")
            )
    return intents


def segment_key(index: int, *, bore: bool = False) -> str:
    """The ``fits`` key that names one segment's outer diameter, or its bore.

    The part's own diameters have to be addressable one at a time: "ISO 286
    fits on the bearing seats" means a code on *segment 1*, not on every
    dimension the part emits.
    """
    return f"segment[{int(index)}].bore" if bore else f"segment[{int(index)}]"


def _revolved_diameters(part: RevolvedPart) -> list[DimIntent]:
    """Each segment's outside diameter and bore, measured across the axis.

    ``p1`` is the upper chord end: a diameter's text stands beyond ``p1`` (the
    ActiveX ``AddDimDiametric`` ChordPoint rule, which both engines follow), so
    every front-view diameter is annotated above the view, clear of the axial
    chain rows below it.
    """
    intents: list[DimIntent] = []
    bounds = zip(segment_bounds(part), part.segments, strict=True)
    for index, ((x0, x1), segment) in enumerate(bounds):
        mid = (x0 + x1) / 2.0
        radius = segment.d_outer / 2.0
        intents.append(
            DimIntent(
                kind="diameter",
                p1=(mid, radius),
                p2=(mid, -radius),
                feature=segment_key(index),
            )
        )
        if segment.d_inner > _EPS:
            bore = segment.d_inner / 2.0
            intents.append(
                DimIntent(
                    kind="diameter",
                    p1=(mid, bore),
                    p2=(mid, -bore),
                    feature=segment_key(index, bore=True),
                )
            )
    return intents


def _hole_groups(part) -> dict[tuple, list[Hole]]:
    groups: dict[tuple, list[Hole]] = {}
    for feature in getattr(part, "features", ()):
        if not isinstance(feature, Hole):
            continue
        key = (
            round(float(feature.diameter), 6),
            feature.thread or "",
            None if feature.cbore_d is None else round(float(feature.cbore_d), 6),
            None if feature.depth is None else round(float(feature.depth), 6),
        )
        groups.setdefault(key, []).append(feature)
    return groups


def dimension_intents(
    part,
    view,
    *,
    style: str = "chain",
    hole_table_threshold: int = 8,
    fits: dict | None = None,
) -> tuple[DimIntent, ...]:
    """Every dimension this view wants: the features' own, plus the part's sizes.

    ``fits`` maps a dimension's name to an ISO 286 code - a feature id, or one
    of the part's own diameters by :func:`segment_key` (``"segment[1]"``,
    ``"segment[1].bore"``). The code is stamped onto the matching intent, so
    :func:`resolve_tolerance` resolves it and :func:`layout_dimensions` reserves
    room for the widened text. A key that names nothing is refused with the
    names that exist.

    ISO 129-1 is applied last: a measurement the other dimensions already tile
    end to end is marked as an auxiliary (reference) dimension, in parentheses,
    instead of being drawn a second time as a plain DIMENSION.
    """
    name = str(style or "").strip().lower()
    if name not in DIM_STYLES:
        raise ValueError(f"style: expected one of {DIM_STYLES}, got {style!r}.")

    intents: list[DimIntent] = list(view.dims)
    kind = "front" if view.kind == "section" else view.kind

    if isinstance(part, RevolvedPart):
        if kind == "front":
            intents.extend(_revolved_axial(part, name))
            intents.extend(_revolved_diameters(part))
        elif kind == "side":
            last = len(part.segments) - 1
            near = part.segments[last]
            radius = near.d_outer / 2.0
            intents.append(
                DimIntent(
                    kind="diameter",
                    p1=(-radius, 0.0),
                    p2=(radius, 0.0),
                    feature=segment_key(last),
                )
            )
            if near.d_inner > _EPS:
                bore = near.d_inner / 2.0
                intents.append(
                    DimIntent(
                        kind="diameter",
                        p1=(-bore, 0.0),
                        p2=(bore, 0.0),
                        feature=segment_key(last, bore=True),
                    )
                )
    elif isinstance(part, PrismaticPart):
        x0, y0, x1, y1 = outline_bbox(part)
        if kind == "front":
            intents.append(DimIntent(kind="linear", p1=(x0, y0), p2=(x1, y0), feature="part"))
            intents.append(DimIntent(kind="linear", p1=(x0, y0), p2=(x0, y1), feature="part"))
        else:
            # An edge view sees the thickness, measured at the near edge of
            # whichever outline axis this view runs along.
            u0 = y0 if kind == "side" else x0
            intents.append(
                DimIntent(
                    kind="linear",
                    p1=(u0, 0.0),
                    p2=(u0, float(part.thickness)),
                    feature="part",
                )
            )

    groups = _hole_groups(part)
    total_holes = sum(len(holes) for holes in groups.values())
    table_ids: frozenset[str] = frozenset()
    if total_holes > int(hole_table_threshold):
        table_ids = frozenset(hole.id for holes in groups.values() for hole in holes)
        intents = [i for i in intents if i.feature not in table_ids]

    collected = _mark_references(list(_dedupe(intents)))
    return tuple(_apply_fits(collected, fits, table_ids=table_ids))


def hole_table_rows(part, view, *, hole_table_threshold: int = 8) -> tuple[dict, ...]:
    """One row per hole, tagged and counted by group - empty under the threshold.

    ``view`` is part of the pinned signature and is accepted unused, so a later
    view-specific filter (a hole that does not appear in this view) has one
    place to go.
    """
    del view
    groups = _hole_groups(part)
    total = sum(len(holes) for holes in groups.values())
    if total <= int(hole_table_threshold):
        return ()
    rows: list[dict] = []
    for index, key in enumerate(sorted(groups, key=lambda k: (k[0], k[1]))):
        tag = chr(ord("A") + index)
        holes = groups[key]
        diameter, thread, cbore, depth = key
        note_parts = []
        if thread:
            note_parts.append(str(thread))
        if cbore is not None:
            note_parts.append(f"c'bore ⌀{cbore:g}")
        if depth is not None:
            note_parts.append(f"deep {depth:g}")
        note = " ".join(note_parts)
        for hole in holes:
            rows.append(
                {
                    "tag": tag,
                    "x": float(hole.x),
                    "y": float(hole.y),
                    "d": float(diameter),
                    "qty": len(holes),
                    "note": note,
                }
            )
    return tuple(rows)


# -- the layout --------------------------------------------------------------


def _axis_of(intent: DimIntent) -> str:
    """ "bottom" (u = x, level moves -y), "left" (u = y, level moves -x) or "right"."""
    if intent.kind in ("diameter", "radius", "angular"):
        return "right"
    dx = abs(intent.p2[0] - intent.p1[0])
    dy = abs(intent.p2[1] - intent.p1[1])
    return "bottom" if dx >= dy else "left"


def layout_dimensions(
    view, intents, *, offset: float = 10.0, step: float = 8.0, height: float = 3.5
) -> tuple[DimIntent, ...]:
    """Give every intent a ``text_at`` such that no two text rectangles overlap."""
    x0, y0, x1, y1 = view.bbox
    del y1

    # Measure first: a level's pitch has to clear the widest text that stacks
    # across it, and that is the text WIDTH on the left/right axes, where the
    # levels march in x, not the text height.
    measured: list[tuple[DimIntent, str, float, float]] = []
    across: dict[str, float] = {}
    for intent in intents:
        axis = _axis_of(intent)
        width, tall = _text_size(intent, height)
        # ``along`` is the extent parallel to the level, ``across`` the extent
        # perpendicular to it - the one successive levels have to clear.
        along, over = (width, tall) if axis == "bottom" else (tall, width)
        measured.append((intent, axis, along, over))
        across[axis] = max(across.get(axis, 0.0), over)
    pitch = {axis: max(float(step), value + 1.0) for axis, value in across.items()}

    occupied: dict[tuple[str, int], list[tuple[float, float]]] = {}
    placed: list[DimIntent] = []

    for intent, axis, along, _over in measured:
        if axis == "bottom":
            center = (float(intent.p1[0]) + float(intent.p2[0])) / 2.0
        else:
            center = (float(intent.p1[1]) + float(intent.p2[1])) / 2.0
        span = (center - along / 2.0, center + along / 2.0)

        level = 0
        while True:
            taken = occupied.setdefault((axis, level), [])
            if all(span[1] <= a + 1e-9 or span[0] >= b - 1e-9 for a, b in taken):
                taken.append(span)
                break
            level += 1

        distance = float(offset) + level * pitch[axis]
        if axis == "bottom":
            text_at = (center, y0 - distance)
        elif axis == "left":
            text_at = (x0 - distance, center)
        else:
            text_at = (x1 + distance, center)
        placed.append(intent._replace(text_at=text_at))

    return tuple(placed)
