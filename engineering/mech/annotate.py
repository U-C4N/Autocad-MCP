"""ISO annotation symbols: surface texture (ISO 21920-1 / ISO 1302) and welds (ISO 2553).

Pure geometry. Every function here returns primitive *descriptions* from
`engineering.mech.primitives` in a symbol-local frame whose origin is the point
the leader attaches to; `draw_*` is the only async part and the only part that
touches a backend. That split is what lets a test assert the emitted tuples
instead of a screenshot.

SOURCE - surface-texture symbol proportions: ISO 1302:2002, "Geometrical
Product Specifications (GPS) - Indication of surface texture in technical
product documentation", the table of graphical-symbol dimensions (text height
h -> line width d', short-leg height H1, minimum overall height H2). The seven
rows below are transcribed from it. ISO 21920-1:2021 superseded ISO 1302
without changing the graphical symbol, so the same proportions serve both
designations. The clause/table *number* is deliberately not quoted: the values
are what this implementer can state, the numbering is not, and a fabricated
citation is worse than none. A text height outside the seven rows is refused
by name - never interpolated.

SOURCE - direction-of-lay symbols: ISO 1302:2002, the seven symbols for the
direction of lay (=, perpendicular, X, M, C, R, P). A lay name outside those
seven is refused with the list.

SOURCE - weld symbols: ISO 2553:2019, "Welding and allied processes -
Symbolic representation on drawings - Welded joints", elementary weld symbols,
plus its rules for the reference line, the dashed identification line, the
size written before the symbol, the length and pitch written after it, the
field-weld flag and the all-around circle at the kink, and the tail carrying
the process reference. SIX elementary symbols are transcribed here: square (I),
single-V, single-bevel, single-U, single-J and fillet. The rest of the
elementary table - spot, seam, plug/slot, backing run, surfacing, edge,
steep-flanked V and bevel, fold joint, inclined joint - is NOT shipped,
because their drawn form could not be verified; a weld kind outside the six is
refused with the list of the six. A narrow catalogue is merely narrow; a
guessed weld symbol is a wrong instruction to a welder.

DECLARED, not transcribed. Neither standard dimensions these, so they are this
implementation's stated choices and are named as such wherever they are used:
the all-around circle radius, the field-weld flag, the identification line's
dash pattern and its offset from the reference line, the text line pitch and
the character-width estimate used to size the extension line and lay out the
weld annotation.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from engineering.mech.primitives import (
    ROLE_LAYER,
    Arc,
    Circle,
    Line,
    Poly,
    Prim,
    Text,
    rotate,
    translate,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backends.base import AutoCADBackend

__all__ = [
    "ISO1302_SYMBOL_PROPORTIONS",
    "LAY_SYMBOLS",
    "MACHINING_KINDS",
    "SURFACE_HEIGHTS",
    "SURFACE_STANDARDS",
    "WELD_KINDS",
    "WELD_SIDES",
    "draw_annotation_prims",
    "draw_surface_texture",
    "draw_weld_symbol",
    "leader_prims",
    "surface_texture_prims",
    "symbol_proportions",
    "weld_symbol_prims",
]

# ── ISO 1302:2002 graphical-symbol dimensions (transcribed) ─────────────────
# text height h -> (line width d', short-leg height H1, minimum height H2), mm.
ISO1302_SYMBOL_PROPORTIONS: dict[float, tuple[float, float, float]] = {
    2.5: (0.25, 3.5, 7.5),
    3.5: (0.35, 5.0, 10.5),
    5.0: (0.5, 7.0, 15.0),
    7.0: (0.7, 10.0, 21.0),
    10.0: (1.0, 14.0, 30.0),
    14.0: (1.4, 20.0, 42.0),
    20.0: (2.0, 28.0, 60.0),
}

SURFACE_HEIGHTS: tuple[float, ...] = tuple(sorted(ISO1302_SYMBOL_PROPORTIONS))

# ISO 21920-1:2021 is the current designation; ISO 1302 is accepted as an alias
# for drawings still issued under the older one. The geometry is identical.
SURFACE_STANDARDS: tuple[str, ...] = ("ISO 21920-1", "ISO 1302")

# ISO 1302:2002 direction-of-lay symbols (transcribed).
LAY_SYMBOLS: dict[str, str] = {
    "parallel": "=",
    "perpendicular": "⊥",
    "crossed": "X",
    "multidirectional": "M",
    "circular": "C",
    "radial": "R",
    "particulate": "P",
}

MACHINING_KINDS: tuple[str, ...] = ("any", "required", "prohibited")

# DECLARED layout constants (not standard values).
ALL_AROUND_RADIUS_FACTOR = 0.35  # all-around circle radius, in text heights
TEXT_GAP_FACTOR = 0.35  # gap between the extension line and the nearest text
TEXT_PITCH_FACTOR = 1.5  # line pitch for stacked annotation text
CHAR_WIDTH_FACTOR = 0.62  # mean character advance, in text heights

# tan(30 deg): the horizontal run of a 60 deg leg per unit of rise.
_LEG_RUN = 1.0 / math.sqrt(3.0)


def symbol_proportions(height: float) -> tuple[float, float, float]:
    """(d', H1, H2) for a text height, straight out of the ISO 1302 table.

    A height the standard does not tabulate is refused: interpolating would
    invent a proportion the standard never published.
    """
    key = float(height)
    if key not in ISO1302_SYMBOL_PROPORTIONS:
        listed = ", ".join(f"{h:g}" for h in SURFACE_HEIGHTS)
        raise ValueError(
            f"ISO 1302 dimensions the surface-texture symbol only for text heights "
            f"{listed} mm; {key:g} mm is outside the table and is not interpolated."
        )
    return ISO1302_SYMBOL_PROPORTIONS[key]


def surface_texture_prims(
    *,
    ra: float | None = None,
    rz: float | None = None,
    process: str | None = None,
    lay: str | None = None,
    machining: str = "any",
    all_around: bool = False,
    height: float = 3.5,
    allowance: float | None = None,
    standard: str = "ISO 21920-1",
) -> tuple[Prim, ...]:
    """The surface-texture symbol in a symbol-local frame, apex at the origin.

    The apex is the point a leader attaches to. The short leg runs up-left and
    the long leg up-right, both at 60 degrees to the horizontal, so the
    included angle is 60 degrees. `machining="required"` closes the vee with
    the bar at H1; `machining="prohibited"` inscribes the circle in the vee.
    """
    if standard not in SURFACE_STANDARDS:
        raise ValueError(
            f"surface texture standard {standard!r} is unknown; use one of "
            f"{', '.join(SURFACE_STANDARDS)}."
        )
    if machining not in MACHINING_KINDS:
        raise ValueError(
            f"machining {machining!r} is unknown; use one of {', '.join(MACHINING_KINDS)}."
        )
    if lay is not None and lay not in LAY_SYMBOLS:
        raise ValueError(
            f"lay {lay!r} is not an ISO 1302 direction of lay; use one of "
            f"{', '.join(sorted(LAY_SYMBOLS))}."
        )
    _width, h1, h2 = symbol_proportions(height)
    h = float(height)
    short_top = (-h1 * _LEG_RUN, h1)
    long_top = (h2 * _LEG_RUN, h2)

    prims: list[Prim] = [
        Line((0.0, 0.0), short_top, "dim"),
        Line((0.0, 0.0), long_top, "dim"),
    ]
    if machining == "required":
        prims.append(Line(short_top, (h1 * _LEG_RUN, h1), "dim"))
    elif machining == "prohibited":
        # Inscribed circle of the 60 deg vee: tangent to both legs and to the
        # bar height H1, so its centre sits at 2*H1/3 with radius H1/3.
        prims.append(Circle((0.0, 2.0 * h1 / 3.0), h1 / 3.0, "dim"))

    above: list[str] = []
    if ra is not None:
        above.append(f"Ra {float(ra):g}")
    if rz is not None:
        above.append(f"Rz {float(rz):g}")
    if process:
        above.append(str(process))
    below: list[str] = []
    if lay is not None:
        below.append(LAY_SYMBOLS[lay])
    if allowance is not None:
        below.append(f"{float(allowance):g}")
    below_text = "  ".join(below)

    widest = max([len(text) for text in above] + [len(below_text)])
    extension = max(2.0 * h, CHAR_WIDTH_FACTOR * h * widest + h)
    prims.append(Line(long_top, (long_top[0] + extension, h2), "dim"))

    x_text = long_top[0] + 0.5 * h
    for index, line in enumerate(above):
        prims.append(
            Text((x_text, h2 + TEXT_GAP_FACTOR * h + index * TEXT_PITCH_FACTOR * h), line, h)
        )
    if below_text:
        prims.append(Text((x_text, h2 - TEXT_GAP_FACTOR * h - h), below_text, h))
    if all_around:
        # DECLARED: the standard shows the circle at the kink between the long
        # leg and the extension line but does not dimension it.
        prims.append(Circle(long_top, ALL_AROUND_RADIUS_FACTOR * h, "dim"))
    return tuple(prims)


# ── ISO 2553:2019 weld symbols ──────────────────────────────────────────────

WELD_KINDS: tuple[str, ...] = ("square", "v", "bevel", "u", "j", "fillet")
WELD_SIDES: tuple[str, ...] = ("arrow", "other", "both")

# Symbol width per kind, in text heights: the half symbols (bevel, J) are half
# as wide as the full ones they are half of.
_WELD_WIDTH_FACTOR: dict[str, float] = {
    "square": 1.0,
    "v": 1.0,
    "bevel": 0.5,
    "u": 1.0,
    "j": 0.5,
    "fillet": 1.0,
}

# DECLARED layout constants for the weld annotation (ISO 2553 fixes none of
# these numerically; it fixes only what goes where).
IDENT_OFFSET_FACTOR = 0.4  # identification line offset below the reference line
IDENT_DASH_FACTOR = 0.8  # dash length of the identification line
IDENT_GAP_FACTOR = 0.4  # gap between its dashes
FLAG_POLE_FACTOR = 1.4  # field-weld flag pole height
KINK_CIRCLE_FACTOR = 0.4  # all-around circle radius at the kink
TAIL_FORK_FACTOR = 0.8  # tail fork length


def _weld_size_text(kind: str, size) -> str:
    """The text written before the symbol.

    A string passes through verbatim, so `z7` (leg length) stays available; a
    number on a fillet takes the `a` prefix, ISO 2553's design throat
    thickness, because a bare number on a fillet weld is ambiguous.
    """
    if size is None:
        return ""
    if isinstance(size, str):
        return size.strip()
    if kind == "fillet":
        return f"a{float(size):g}"
    return f"{float(size):g}"


def _elementary_prims(kind: str, x0: float, y0: float, sense: float, h: float) -> tuple[Prim, ...]:
    """One elementary symbol standing on (x0, y0), growing in `sense` (+1/-1)."""
    s = float(sense)
    if kind == "square":
        return (
            Line((x0 + 0.3 * h, y0), (x0 + 0.3 * h, y0 + s * h), "dim"),
            Line((x0 + 0.7 * h, y0), (x0 + 0.7 * h, y0 + s * h), "dim"),
        )
    if kind == "v":
        return (Poly(((x0, y0 + s * h), (x0 + 0.5 * h, y0), (x0 + h, y0 + s * h)), False, "dim"),)
    if kind == "bevel":
        return (
            Line((x0, y0), (x0, y0 + s * h), "dim"),
            Line((x0, y0), (x0 + 0.5 * h, y0 + s * h), "dim"),
        )
    if kind == "u":
        cy = y0 + s * 0.5 * h
        start, end = (180.0, 360.0) if s > 0 else (0.0, 180.0)
        return (
            Arc((x0 + 0.5 * h, cy), 0.5 * h, start, end, "dim"),
            Line((x0, cy), (x0, y0 + s * h), "dim"),
            Line((x0 + h, cy), (x0 + h, y0 + s * h), "dim"),
        )
    if kind == "j":
        cy = y0 + s * 0.5 * h
        start, end = (270.0, 360.0) if s > 0 else (0.0, 90.0)
        return (
            Arc((x0, cy), 0.5 * h, start, end, "dim"),
            Line((x0 + 0.5 * h, cy), (x0 + 0.5 * h, y0 + s * h), "dim"),
            Line((x0, y0), (x0, y0 + s * h), "dim"),
        )
    # fillet
    return (Poly(((x0, y0), (x0, y0 + s * h), (x0 + h, y0)), True, "dim"),)


def weld_symbol_prims(
    *,
    kind: str,
    size=None,
    length: float | None = None,
    pitch: float | None = None,
    side: str = "arrow",
    field_weld: bool = False,
    all_around: bool = False,
    process: str | None = None,
    height: float = 3.5,
) -> tuple[Prim, ...]:
    """The ISO 2553 weld annotation in a local frame, kink at the origin.

    The kink is where the arrow line meets the reference line, so it is the
    point a leader attaches to. The continuous reference line runs in +X; the
    dashed identification line runs parallel below it and is omitted for a
    symmetrical weld (`side="both"`), which ISO 2553:2019 permits. The
    arrow-side symbol stands on the reference line, the other-side symbol
    hangs below the identification line.
    """
    if kind not in WELD_KINDS:
        raise ValueError(
            f"weld kind {kind!r} is not drawn by this server. The ISO 2553 elementary "
            f"symbols transcribed here are: {', '.join(WELD_KINDS)}. The rest of the "
            "elementary table is not shipped rather than guessed."
        )
    if side not in WELD_SIDES:
        raise ValueError(f"side {side!r} is unknown; use one of {', '.join(WELD_SIDES)}.")
    h = float(height)
    if not math.isfinite(h) or h <= 0.0:
        raise ValueError(f"height must be a positive finite number; got {height!r}.")
    if pitch is not None and length is None:
        raise ValueError(
            "pitch needs length: ISO 2553 writes the pitch in brackets after the weld length."
        )

    size_text = _weld_size_text(kind, size)
    right_text = ""
    if length is not None:
        right_text = (
            f"{float(length):g}" if pitch is None else f"{float(length):g} ({float(pitch):g})"
        )

    ident_y = -IDENT_OFFSET_FACTOR * h
    cursor = (1.0 if (field_weld or all_around) else 0.5) * h
    x_size = cursor
    if size_text:
        cursor += CHAR_WIDTH_FACTOR * h * len(size_text) + 0.4 * h
    x_symbol = cursor
    cursor += _WELD_WIDTH_FACTOR[kind] * h + 0.4 * h
    x_right = cursor
    if right_text:
        cursor += CHAR_WIDTH_FACTOR * h * len(right_text)
    ref_length = max(cursor + 0.5 * h, 6.0 * h)

    prims: list[Prim] = [Line((0.0, 0.0), (ref_length, 0.0), "dim")]

    if side != "both":
        x = 0.0
        dash, gap = IDENT_DASH_FACTOR * h, IDENT_GAP_FACTOR * h
        while x < ref_length - 1e-9:
            x_end = min(x + dash, ref_length)
            prims.append(Line((x, ident_y), (x_end, ident_y), "dim"))
            x = x_end + gap

    if side in ("arrow", "both"):
        prims.extend(_elementary_prims(kind, x_symbol, 0.0, 1.0, h))
    if side in ("other", "both"):
        prims.extend(_elementary_prims(kind, x_symbol, ident_y, -1.0, h))

    if size_text:
        prims.append(Text((x_size, 0.25 * h), size_text, h))
    if right_text:
        prims.append(Text((x_right, 0.25 * h), right_text, h))

    if field_weld:
        top = FLAG_POLE_FACTOR * h
        prims.append(Line((0.0, 0.0), (0.0, top), "dim"))
        prims.append(
            Poly(
                ((0.0, top), (0.7 * h, top - 0.25 * h), (0.0, top - 0.5 * h)),
                True,
                "dim",
            )
        )
    if all_around:
        prims.append(Circle((0.0, 0.0), KINK_CIRCLE_FACTOR * h, "dim"))

    if process:
        fork = TAIL_FORK_FACTOR * h
        prims.append(Line((ref_length, 0.0), (ref_length + fork, 0.5 * h), "dim"))
        prims.append(Line((ref_length, 0.0), (ref_length + fork, -0.5 * h), "dim"))
        prims.append(Text((ref_length + h, -0.35 * h), str(process), h))
    return tuple(prims)


# ── the async layer ─────────────────────────────────────────────────────────
#
# Task 7's `engineering/mech/draw.py` is a sibling branch (group M) and is not
# importable from here, so the annotation tools carry their own small renderer.
# It is deliberately named `draw_annotation_prims`, not `draw_prims`, so the
# two coexist after the merge instead of colliding on a re-export.

_DRAWABLE = (Line, Circle, Arc, Poly, Text)


def leader_prims(
    tip: tuple[float, float],
    to: tuple[float, float],
    *,
    arrow_size: float = 2.5,
    role: str = "dim",
) -> tuple[Prim, ...]:
    """A plain leader: a line from `tip` to `to` plus a solid arrowhead at `tip`.

    `leader_create_mleader` is not used here: it refuses empty text
    (`engineering/annotation.py::validate_mleader`), and a weld or surface
    symbol needs the leader *without* an MTEXT label beside it. The arrowhead
    reproduces the proportions that backend already draws (length `arrow_size`,
    half-width 0.35 * `arrow_size`) so both routes look identical.
    """
    x0, y0 = float(tip[0]), float(tip[1])
    x1, y1 = float(to[0]), float(to[1])
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        raise ValueError(
            f"leader_to ({x0:g}, {y0:g}) and the symbol position ({x1:g}, {y1:g}) "
            "must be distinct points."
        )
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    a = float(arrow_size)
    return (
        Line((x0, y0), (x1, y1), role),
        Poly(
            (
                (x0, y0),
                (x0 + ux * a + px * a * 0.35, y0 + uy * a + py * a * 0.35),
                (x0 + ux * a - px * a * 0.35, y0 + uy * a - py * a * 0.35),
            ),
            True,
            role,
        ),
    )


async def draw_annotation_prims(
    backend: AutoCADBackend,
    prims,
    *,
    at: tuple[float, float] = (0.0, 0.0),
    rotation: float = 0.0,
    layer: str | None = None,
) -> dict:
    """Draw primitive descriptions through the generic entity contracts.

    Rotates about the symbol origin first, then translates to `at`, so every
    coordinate that reaches the backend is already WCS. Each primitive lands on
    `ROLE_LAYER[role]` unless `layer` overrides it.
    """
    placed = tuple(prims)
    for prim in placed:
        if not isinstance(prim, _DRAWABLE):
            raise TypeError(
                f"draw_annotation_prims cannot draw a {type(prim).__name__}; it draws "
                "Line, Circle, Arc, Poly and Text."
            )
    if rotation:
        placed = rotate(placed, float(rotation))
    placed = translate(placed, float(at[0]), float(at[1]))

    handles: list[str] = []
    for prim in placed:
        target = layer or ROLE_LAYER[prim.role]
        if isinstance(prim, Line):
            info = await backend.entity_create_line(
                prim.p1[0], prim.p1[1], prim.p2[0], prim.p2[1], 0.0, 0.0, target
            )
        elif isinstance(prim, Circle):
            info = await backend.entity_create_circle(
                prim.center[0], prim.center[1], prim.radius, target
            )
        elif isinstance(prim, Arc):
            info = await backend.entity_create_arc(
                prim.center[0],
                prim.center[1],
                prim.radius,
                prim.start_deg,
                prim.end_deg,
                target,
            )
        elif isinstance(prim, Poly):
            info = await backend.entity_create_polyline(
                [[float(p[0]), float(p[1])] for p in prim.points], prim.closed, target
            )
        else:  # Text
            info = await backend.entity_create_text(
                prim.text, prim.at[0], prim.at[1], prim.height, prim.rotation, target
            )
        handles.append(info.handle)
    return {"ok": True, "handles": handles, "count": len(handles)}


async def draw_surface_texture(
    backend: AutoCADBackend,
    *,
    at: tuple[float, float],
    leader_to: tuple[float, float] | None = None,
    ra: float | None = None,
    rz: float | None = None,
    process: str | None = None,
    lay: str | None = None,
    machining: str = "any",
    all_around: bool = False,
    allowance: float | None = None,
    height: float = 3.5,
    standard: str = "ISO 21920-1",
    rotation: float = 0.0,
    layer: str | None = None,
) -> dict:
    """Place a surface-texture symbol with its apex at `at`."""
    prims = surface_texture_prims(
        ra=ra,
        rz=rz,
        process=process,
        lay=lay,
        machining=machining,
        all_around=all_around,
        height=height,
        allowance=allowance,
        standard=standard,
    )
    result = await draw_annotation_prims(backend, prims, at=at, rotation=rotation, layer=layer)
    if leader_to is not None:
        leader = leader_prims(
            (float(leader_to[0]), float(leader_to[1])),
            (float(at[0]), float(at[1])),
            arrow_size=0.7 * float(height),
        )
        drawn = await draw_annotation_prims(backend, leader, layer=layer)
        result["handles"].extend(drawn["handles"])
        result["count"] = len(result["handles"])
    result["standard"] = standard
    return result


async def draw_weld_symbol(
    backend: AutoCADBackend,
    *,
    at: tuple[float, float],
    kind: str,
    leader_to: tuple[float, float] | None = None,
    size=None,
    length: float | None = None,
    pitch: float | None = None,
    side: str = "arrow",
    field_weld: bool = False,
    all_around: bool = False,
    process: str | None = None,
    height: float = 3.5,
    rotation: float = 0.0,
    layer: str | None = None,
) -> dict:
    """Place an ISO 2553 weld annotation with its kink at `at`."""
    prims = weld_symbol_prims(
        kind=kind,
        size=size,
        length=length,
        pitch=pitch,
        side=side,
        field_weld=field_weld,
        all_around=all_around,
        process=process,
        height=height,
    )
    result = await draw_annotation_prims(backend, prims, at=at, rotation=rotation, layer=layer)
    if leader_to is not None:
        leader = leader_prims(
            (float(leader_to[0]), float(leader_to[1])),
            (float(at[0]), float(at[1])),
            arrow_size=0.7 * float(height),
        )
        drawn = await draw_annotation_prims(backend, leader, layer=layer)
        result["handles"].extend(drawn["handles"])
        result["count"] = len(result["handles"])
    result["kind"] = kind
    result["side"] = side
    return result
