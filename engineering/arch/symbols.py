"""The structural grid and the architectural symbols - pure primitives.

:func:`grid_prims` draws the axis lines of a structural grid on the ``grid``
role (whose layer carries the CENTER linetype, so no primitive sets one) with a
bubble and its label at both ends: numbers along x, letters along y, unless
``labels`` names them. :func:`symbol_prims` draws the four plan symbols: the
north arrow, the section mark, the level mark and the elevation mark.

Every size here is **paper millimetres times ``scale``** (the plot-scale
denominator: 50 means 1:50), so a 5 mm bubble is 250 drawing units at 1:50 and
reads 5 mm on the printed sheet. The paper sizes below are this module's own
drafting choices, declared as such - no standard in this repository fixes a grid
bubble's diameter or a north arrow's length.

Bubbles, labels and symbols go on the ``symbol`` role, not on ``grid``: a
bubble on the grid layer would inherit CENTER and print as a dashed circle.

Labels are centred with ``engineering.sheet.frames.centred_text`` (the
repository's one centring rule: 0.6 x height advance per character), because
``entity_create_text`` carries no alignment.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from engineering.arch.lang import fmt_number, vocab
from engineering.arch.layers import ARCH_ROLE_LAYER
from engineering.mech.primitives import Circle, Line, Poly, Prim, Pt, Text
from engineering.sheet.frames import centred_text

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

# -- declared paper sizes (mm on the printed sheet; multiplied by `scale`) -----

BUBBLE_RADIUS = 5.0
LABEL_HEIGHT = 3.5
NORTH_RADIUS = 8.0
SECTION_ARROW_LENGTH = 6.0
SECTION_ARROW_HALF_WIDTH = 2.0
LEVEL_TRIANGLE = 2.5
LEVEL_LINE = 15.0
LEVEL_TEXT_HEIGHT = 2.5
LEVEL_TEXT_GAP = 1.0

SYMBOL_KINDS: tuple[str, ...] = ("north_arrow", "section_mark", "level", "elevation_mark")
SECTION_SIDES: tuple[str, ...] = ("left", "right")
#: An elevation mark's direction in drawing terms, not compass terms: "up" is
#: +Y on the sheet whatever the north arrow says.
ELEVATION_DIRECTIONS: dict[str, float] = {"right": 0.0, "up": 90.0, "left": 180.0, "down": 270.0}

_PARAMS: dict[str, tuple[str, ...]] = {
    "north_arrow": ("rotation",),
    "section_mark": ("p1", "p2", "label", "side"),
    "level": ("value",),
    "elevation_mark": ("label", "directions"),
}


def _finite(value, where: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected a number, got {value!r}.") from None
    if not math.isfinite(number):
        raise ValueError(f"{where}: must be finite, got {value!r}.")
    return number


def _positive(value, where: str) -> float:
    number = _finite(value, where)
    if number <= 0.0:
        raise ValueError(f"{where}: must be greater than zero, got {number:g}.")
    return number


def _point(value, where: str) -> Pt:
    try:
        x, y = value
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected an [x, y] pair, got {value!r}.") from None
    return (_finite(x, f"{where}.x"), _finite(y, f"{where}.y"))


def _label(value, where: str) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        raise ValueError(f"{where}: a label must be a non-empty string.")
    return text


def _centred(cx: float, cy: float, text: str, height: float) -> Text:
    return centred_text(cx, cy, text, height)._replace(role="symbol")


def _turn(vx: float, vy: float, deg: float) -> Pt:
    c = math.cos(math.radians(deg))
    s = math.sin(math.radians(deg))
    return (vx * c - vy * s, vx * s + vy * c)


# -- the structural grid -----------------------------------------------------


def _letters(index: int) -> str:
    """0 -> A ... 25 -> Z, 26 -> AA: every letter, none skipped."""
    out = ""
    n = index + 1
    while n:
        n, rest = divmod(n - 1, 26)
        out = chr(ord("A") + rest) + out
    return out


def grid_axes(x_axes, y_axes, *, labels=None) -> dict:
    """The grid's axes sorted by position, each with its label.

    ``labels`` is ``{"x": [...], "y": [...]}`` (either key optional), one label
    per axis in the order the positions were given. Refused by path: an empty
    axis list, a non-finite or duplicate position, a label count that does not
    match, a duplicate or empty label, an unknown ``labels`` key.
    """
    out: dict[str, list[dict]] = {}
    if labels is not None and not isinstance(labels, dict):
        raise ValueError(f"labels: expected {{'x': [...], 'y': [...]}}, got {labels!r}.")
    given = {} if labels is None else dict(labels)
    unknown = sorted(set(given) - {"x", "y"})
    if unknown:
        raise ValueError(f"labels: unknown key(s) {', '.join(unknown)}; keys are x, y.")
    for key, positions, default in (
        ("x", x_axes, lambda i: str(i + 1)),
        ("y", y_axes, _letters),
    ):
        where = f"{key}_axes"
        values = [_finite(v, f"{where}[{i}]") for i, v in enumerate(positions or ())]
        if not values:
            raise ValueError(f"{where}: a grid needs at least one axis in each direction.")
        names = given.get(key)
        if names is None:
            order = sorted(range(len(values)), key=lambda i: values[i])
            names_sorted = [default(rank) for rank in range(len(values))]
            pairs = [(values[i], names_sorted[rank]) for rank, i in enumerate(order)]
        else:
            if not isinstance(names, (list, tuple)):
                raise ValueError(f"labels.{key}: expected a list of labels, got {names!r}.")
            if len(names) != len(values):
                raise ValueError(
                    f"labels.{key}: {len(names)} label(s) for {len(values)} axis position(s)."
                )
            texts = [_label(n, f"labels.{key}[{i}]") for i, n in enumerate(names)]
            if len(set(texts)) != len(texts):
                raise ValueError(f"labels.{key}: every axis label must be unique, got {texts}.")
            pairs = sorted(zip(values, texts, strict=True), key=lambda pair: pair[0])
        for (a, _), (b, _) in zip(pairs, pairs[1:], strict=False):
            if abs(b - a) <= 1e-9:
                raise ValueError(f"{where}: two axes at the same position {a:g}.")
        out[key] = [{"label": text, "position": value} for value, text in pairs]
    return out


def grid_prims(
    x_axes, y_axes, *, labels=None, extension: float = 1500.0, scale: float = 50
) -> tuple[Prim, ...]:
    """Axis lines on ``grid`` with a labelled bubble at both ends of each.

    ``x_axes`` are the x positions of the vertical axes (numbered 1, 2, 3 ...
    from left to right), ``y_axes`` the y positions of the horizontal axes
    (lettered A, B, C ... from bottom to top). Every axis runs ``extension``
    drawing units past the outermost crossing axis; the bubble (radius
    ``BUBBLE_RADIUS * scale``) sits beyond that, touching the line's end.
    Per axis the primitives are: the line, then for each end its bubble and
    its label.
    """
    ext = _positive(extension, "extension")
    k = _positive(scale, "scale")
    axes = grid_axes(x_axes, y_axes, labels=labels)
    r = BUBBLE_RADIUS * k
    h = LABEL_HEIGHT * k
    xs = [axis["position"] for axis in axes["x"]]
    ys = [axis["position"] for axis in axes["y"]]
    y_lo, y_hi = min(ys) - ext, max(ys) + ext
    x_lo, x_hi = min(xs) - ext, max(xs) + ext
    prims: list[Prim] = []
    for axis in axes["x"]:
        x = axis["position"]
        prims.append(Line((x, y_lo), (x, y_hi), "grid"))
        for cy in (y_lo - r, y_hi + r):
            prims.append(Circle((x, cy), r, "symbol"))
            prims.append(_centred(x, cy, axis["label"], h))
    for axis in axes["y"]:
        y = axis["position"]
        prims.append(Line((x_lo, y), (x_hi, y), "grid"))
        for cx in (x_lo - r, x_hi + r):
            prims.append(Circle((cx, y), r, "symbol"))
            prims.append(_centred(cx, y, axis["label"], h))
    return tuple(prims)


# -- the four symbols ----------------------------------------------------------


def _north_arrow(at: Pt, k: float, lang: str, rotation=0.0) -> tuple[Prim, ...]:
    """A circle, an arrow whose tip is north, the north letter beyond the tip.

    ``rotation`` is degrees CCW from the sheet's +Y: 0 puts north straight up.
    The letter stays upright; only its position turns with the arrow.
    """
    turn = _finite(rotation, "north_arrow.rotation")
    big_r = NORTH_RADIUS * k
    h = LABEL_HEIGHT * k
    ax, ay = at
    local = (
        (0.0, big_r),
        (-0.4 * big_r, -0.6 * big_r),
        (0.0, -0.3 * big_r),
        (0.4 * big_r, -0.6 * big_r),
    )
    arrow = tuple((ax + dx, ay + dy) for dx, dy in (_turn(x, y, turn) for x, y in local))
    lx, ly = _turn(0.0, big_r + h, turn)
    return (
        Circle((ax, ay), big_r, "symbol"),
        Poly(arrow, True, "symbol"),
        _centred(ax + lx, ay + ly, vocab(lang)["north"], h),
    )


def _section_mark(at: Pt, k: float, lang: str, p1=None, p2=None, label="A", side="left"):
    """The cut line, a labelled bubble beyond each end, an arrow at each end.

    ``p1`` and ``p2`` are relative to ``at`` (so ``at=(0, 0)`` makes them WCS).
    ``side`` is the side of the line the section looks towards, seen from
    ``p1`` looking at ``p2``.
    """
    if p1 is None or p2 is None:
        raise ValueError("section_mark: p1 and p2 are required (the two ends of the cut line).")
    a = _point(p1, "section_mark.p1")
    b = _point(p2, "section_mark.p2")
    text = _label(label, "section_mark.label")
    if side not in SECTION_SIDES:
        raise ValueError(
            f"section_mark.side: must be one of {', '.join(SECTION_SIDES)}, got {side!r}."
        )
    x1, y1 = at[0] + a[0], at[1] + a[1]
    x2, y2 = at[0] + b[0], at[1] + b[1]
    span = math.hypot(x2 - x1, y2 - y1)
    r = BUBBLE_RADIUS * k
    h = LABEL_HEIGHT * k
    length = SECTION_ARROW_LENGTH * k
    half = SECTION_ARROW_HALF_WIDTH * k
    if span <= 4.0 * half:
        raise ValueError(
            f"section_mark: the cut line is {span:g} long; it must be longer than "
            f"{4.0 * half:g} ({4.0 * SECTION_ARROW_HALF_WIDTH:g} mm at 1:{k:g}) so the two "
            "arrows do not overlap."
        )
    ux, uy = (x2 - x1) / span, (y2 - y1) / span
    dx, dy = (-uy, ux) if side == "left" else (uy, -ux)
    prims: list[Prim] = [Line((x1, y1), (x2, y2), "symbol")]
    for cx, cy in ((x1 - ux * r, y1 - uy * r), (x2 + ux * r, y2 + uy * r)):
        prims.append(Circle((cx, cy), r, "symbol"))
        prims.append(_centred(cx, cy, text, h))
    for ex, ey, sign in ((x1, y1, 1.0), (x2, y2, -1.0)):
        inner = (ex + sign * ux * 2.0 * half, ey + sign * uy * 2.0 * half)
        mid = (ex + sign * ux * half, ey + sign * uy * half)
        apex = (mid[0] + dx * length, mid[1] + dy * length)
        prims.append(Poly(((ex, ey), inner, apex), True, "symbol"))
    return tuple(prims)


def fmt_level(value: float, lang: str) -> str:
    """``±0.00`` at zero, ``+3.00`` above, ``-0.45`` below; ``tr`` writes a comma."""
    number = _finite(value, "level.value")
    if abs(number) < 0.005:
        return "±" + fmt_number(0.0, lang, 2)
    sign = "+" if number > 0 else "-"
    return sign + fmt_number(abs(number), lang, 2)


def _level(at: Pt, k: float, lang: str, value=0.0) -> tuple[Prim, ...]:
    """A triangle whose apex is the level point, a line, the value above the line.

    ``value`` is in metres, as a level is written on a plan.
    """
    text = fmt_level(value, lang)
    t = LEVEL_TRIANGLE * k
    ax, ay = at
    gap = LEVEL_TEXT_GAP * k
    return (
        Poly(((ax, ay), (ax - t, ay + t), (ax + t, ay + t)), True, "symbol"),
        Line((ax + t, ay + t), (ax + t + LEVEL_LINE * k, ay + t), "symbol"),
        Text((ax + t + gap, ay + t + gap), text, LEVEL_TEXT_HEIGHT * k, 0.0, "symbol"),
    )


def _elevation_mark(at: Pt, k: float, lang: str, label="1", directions=("up",)):
    """A circle, the label in it, and one pointer per viewing direction.

    Each pointer is the two tangents from an apex at ``r * sqrt(2)`` along the
    direction to the circle, touching it at the direction +/- 45 degrees.
    """
    text = _label(label, "elevation_mark.label")
    names = list(directions) if not isinstance(directions, str) else [directions]
    if not names:
        raise ValueError("elevation_mark.directions: name at least one direction.")
    for i, name in enumerate(names):
        if name not in ELEVATION_DIRECTIONS:
            raise ValueError(
                f"elevation_mark.directions[{i}]: must be one of "
                f"{', '.join(ELEVATION_DIRECTIONS)}, got {name!r}."
            )
    if len(set(names)) != len(names):
        raise ValueError(f"elevation_mark.directions: each direction once, got {names}.")
    r = BUBBLE_RADIUS * k
    ax, ay = at
    prims: list[Prim] = [Circle((ax, ay), r, "symbol"), _centred(ax, ay, text, LABEL_HEIGHT * k)]
    for name in names:
        theta = ELEVATION_DIRECTIONS[name]
        apex_r = r * math.sqrt(2.0)
        apex = (
            ax + apex_r * math.cos(math.radians(theta)),
            ay + apex_r * math.sin(math.radians(theta)),
        )
        side_a = (
            ax + r * math.cos(math.radians(theta - 45.0)),
            ay + r * math.sin(math.radians(theta - 45.0)),
        )
        side_b = (
            ax + r * math.cos(math.radians(theta + 45.0)),
            ay + r * math.sin(math.radians(theta + 45.0)),
        )
        prims.append(Poly((side_a, apex, side_b), False, "symbol"))
    return tuple(prims)


_BUILDERS = {
    "north_arrow": _north_arrow,
    "section_mark": _section_mark,
    "level": _level,
    "elevation_mark": _elevation_mark,
}


def symbol_prims(
    kind: str, at: Pt, *, lang: str = "en", scale: float = 50, **params
) -> tuple[Prim, ...]:
    """One architectural symbol at ``at``, sized in paper mm times ``scale``.

    ``kind`` and its ``params``: ``north_arrow`` (``rotation``),
    ``section_mark`` (``p1``, ``p2``, ``label``, ``side``), ``level``
    (``value`` in metres), ``elevation_mark`` (``label``, ``directions``).
    Refused before anything is built: an unknown kind (with the list), a
    parameter the kind does not take (with the ones it does), an unknown
    ``lang``, a non-finite or non-positive ``scale``.
    """
    if kind not in SYMBOL_KINDS:
        raise ValueError(f"unknown symbol kind {kind!r}; kinds are {', '.join(SYMBOL_KINDS)}.")
    extra = sorted(set(params) - set(_PARAMS[kind]))
    if extra:
        raise ValueError(
            f"{kind}: unknown parameter(s) {', '.join(extra)}; it takes {', '.join(_PARAMS[kind])}."
        )
    vocab(lang)
    k = _positive(scale, "scale")
    origin = _point(at, "at")
    return _BUILDERS[kind](origin, k, lang, **params)


# -- the async layer: the prims through draw_prims ------------------------------


async def draw_grid(
    backend: AutoCADBackend,
    x_axes,
    y_axes,
    *,
    labels=None,
    extension: float = 1500.0,
    scale: float = 50,
) -> dict:
    """Draw :func:`grid_prims` through ``draw_prims`` with the arch role map."""
    from engineering.mech.draw import draw_prims

    axes = grid_axes(x_axes, y_axes, labels=labels)
    prims = grid_prims(x_axes, y_axes, labels=labels, extension=extension, scale=scale)
    drawn = await draw_prims(backend, prims, role_layer=ARCH_ROLE_LAYER)
    return {
        "ok": True,
        "axes": axes,
        "bubble_radius": BUBBLE_RADIUS * float(scale),
        "handles": drawn["handles"],
        "counts": drawn["counts"],
        "layers": sorted({ARCH_ROLE_LAYER["grid"], ARCH_ROLE_LAYER["symbol"]}),
        "backend": backend.name,
    }


async def draw_symbol(
    backend: AutoCADBackend,
    kind: str,
    at: Pt,
    *,
    lang: str = "en",
    scale: float = 50,
    **params,
) -> dict:
    """Draw :func:`symbol_prims` through ``draw_prims`` with the arch role map."""
    from engineering.mech.draw import draw_prims

    prims = symbol_prims(kind, at, lang=lang, scale=scale, **params)
    drawn = await draw_prims(backend, prims, role_layer=ARCH_ROLE_LAYER)
    return {
        "ok": True,
        "kind": kind,
        "at": [float(at[0]), float(at[1])],
        "handles": drawn["handles"],
        "counts": drawn["counts"],
        "layer": ARCH_ROLE_LAYER["symbol"],
        "backend": backend.name,
    }
