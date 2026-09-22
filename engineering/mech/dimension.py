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
"""

from __future__ import annotations

import math

from engineering.fits import fit_lookup
from engineering.mech.features import Hole
from engineering.mech.part import (
    PrismaticPart,
    RevolvedPart,
    outline_bbox,
    part_length,
    segment_bounds,
)
from engineering.mech.primitives import DimIntent
from engineering.tolerances import build_dim_override

_EPS = 1e-9

DIM_STYLES: tuple[str, ...] = ("chain", "baseline", "ordinate")

#: Mean glyph width as a fraction of the text height, for measuring a label
#: into a rectangle. Deliberately generous: over-estimating the width can only
#: push two dimensions further apart.
TEXT_WIDTH_FACTOR = 0.7

#: Padding added around a measured label, in text heights.
TEXT_PADDING = 0.6


def measured_value(intent: DimIntent) -> float:
    """What the dimension measures, in drawing units."""
    p1, p2 = intent.p1, intent.p2
    if intent.kind == "linear":
        dx, dy = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
        return dx if dx >= dy else dy
    return math.dist(p1, p2)


def _format(value: float) -> str:
    return f"{round(value, 3):g}"


def intent_text(intent: DimIntent) -> str:
    """The text a dimension will carry - the override, or the measurement."""
    if intent.text_override:
        return intent.text_override
    if intent.kind == "diameter":
        return f"⌀{_format(measured_value(intent))}"
    if intent.kind == "radius":
        return f"R{_format(measured_value(intent))}"
    return _format(measured_value(intent))


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


def resolve_tolerance(intent: DimIntent, nominal: float | None = None) -> dict:
    """Turn an intent's ``fit`` / ``tol`` into the backend dimension call's arguments.

    Mirrors `server.py::_fit_to_tolerances`, including its sign convention:
    ``build_dim_override`` reads ``tol_lower`` as the *magnitude* of the minus
    deviation, so a resolved fit passes ``-deviation.lower_mm``.
    """
    has_tol = bool(intent.tol)
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
            "text_override": intent.text_override or f"<> {deviation.code}",
        }
    if has_tol:
        tol = dict(intent.tol)
        mode = str(tol.get("mode", "none"))
        upper = tol.get("upper")
        lower = tol.get("lower")
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


def _revolved_diameters(part: RevolvedPart) -> list[DimIntent]:
    intents: list[DimIntent] = []
    for (x0, x1), segment in zip(segment_bounds(part), part.segments, strict=True):
        mid = (x0 + x1) / 2.0
        radius = segment.d_outer / 2.0
        intents.append(
            DimIntent(kind="diameter", p1=(mid, radius), p2=(mid, -radius), feature="part")
        )
        if segment.d_inner > _EPS:
            bore = segment.d_inner / 2.0
            intents.append(
                DimIntent(kind="diameter", p1=(mid, bore), p2=(mid, -bore), feature="part")
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
) -> tuple[DimIntent, ...]:
    """Every dimension this view wants: the features' own, plus the part's sizes."""
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
            near = part.segments[-1]
            radius = near.d_outer / 2.0
            intents.append(
                DimIntent(kind="diameter", p1=(-radius, 0.0), p2=(radius, 0.0), feature="part")
            )
            if near.d_inner > _EPS:
                bore = near.d_inner / 2.0
                intents.append(
                    DimIntent(kind="diameter", p1=(-bore, 0.0), p2=(bore, 0.0), feature="part")
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
    if total_holes > int(hole_table_threshold):
        hole_ids = {hole.id for holes in groups.values() for hole in holes}
        intents = [i for i in intents if i.feature not in hole_ids]

    return _dedupe(intents)


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
