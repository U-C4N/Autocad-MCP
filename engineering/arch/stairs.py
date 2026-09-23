"""Stairs in plan: straight, L and U flights, the walking line and the Blondel check.

Geometry, in the stair's own frame (x up the first flight, y to its left):

* ``start`` is the midpoint of the bottom riser; ``direction_deg`` turns the
  frame into WCS.
* ``risers`` counts every rise from the lower floor to the upper one. A flight
  of ``m`` risers has ``m - 1`` treads of depth ``going``; every riser is drawn
  once as a line across the flight, so a straight stair of 17 risers shows 17
  lines, the first at the start and the last at the top.
* An L or a U splits the risers into two flights, ``(risers + 1) // 2`` in the
  first, and turns ``turn`` on a square landing ``width`` deep. The U's two
  flights lie side by side with no stairwell between them.
* The walking line runs up the middle of every flight and across the landing,
  with an open arrow at the top; the "up" word of the chosen language sits on
  the first tread.

`blondel` reports 2R + G against 600-650 mm. That band is the step-length rule
attributed to François Blondel (Cours d'architecture, 1675), reported as a
design rule: it is not a building code, and no national stair regulation is
checked here.
"""

from __future__ import annotations

import math

from engineering.arch.lang import vocab
from engineering.arch.model import Stair
from engineering.mech.primitives import Line, Poly, Prim, Pt, Text, rotate, translate

BLONDEL_RANGE: tuple[float, float] = (600.0, 650.0)
STAIR_KINDS: tuple[str, ...] = ("straight", "l", "u")
TURNS: tuple[str, ...] = ("left", "right")
#: Paper sizes, multiplied by the plot-scale denominator. Drafting choices of
#: this repository, not standard values.
TEXT_PAPER_MM = 2.5
ARROW_PAPER_MM = 3.0


def _positive(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: expected a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{where}: must be a finite number above zero, got {value!r}")
    return number


def blondel(riser_height: float, going: float) -> dict:
    """``{"value": 2R + G, "ok": 600 <= value <= 650, "range": [600.0, 650.0]}`` in mm."""
    rise = _positive(riser_height, "riser_height")
    tread = _positive(going, "going")
    value = 2.0 * rise + tread
    low, high = BLONDEL_RANGE
    return {"value": value, "ok": low <= value <= high, "range": [low, high]}


def _validate(stair: Stair, scale) -> tuple[float, float, int, float]:
    width = _positive(stair.width, f"stair {stair.id!r}: width")
    going = _positive(stair.going, f"stair {stair.id!r}: going")
    _positive(stair.riser_height, f"stair {stair.id!r}: riser_height")
    factor = _positive(scale, "scale")
    if stair.kind not in STAIR_KINDS:
        raise ValueError(
            f"stair {stair.id!r}: kind {stair.kind!r}; kinds are {', '.join(STAIR_KINDS)}"
        )
    if stair.turn not in TURNS:
        raise ValueError(f"stair {stair.id!r}: turn {stair.turn!r}; turns are {', '.join(TURNS)}")
    risers = stair.risers
    if isinstance(risers, bool) or not isinstance(risers, int):
        raise ValueError(f"stair {stair.id!r}: risers must be a whole number, got {risers!r}")
    least = 2 if stair.kind == "straight" else 4
    if risers < least:
        raise ValueError(
            f"stair {stair.id!r}: risers {risers}; a {stair.kind} stair needs at least {least} "
            "(two per flight)"
        )
    for where, value in (
        ("direction_deg", stair.direction_deg),
        ("start[0]", stair.start[0]),
        ("start[1]", stair.start[1]),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"stair {stair.id!r}: {where} must be finite, got {value!r}")
    return width, going, risers, factor


def _flights(kind: str, width: float, going: float, risers: int):
    """Riser lines, outline lines and the walking-line path, turning left."""
    half = width / 2.0
    if kind == "straight":
        run = (risers - 1) * going
        treads = [Line((k * going, -half), (k * going, half), "stair") for k in range(risers)]
        outline = [
            Line((0.0, -half), (run, -half), "stair"),
            Line((0.0, half), (run, half), "stair"),
        ]
        return treads, outline, [(0.0, 0.0), (run, 0.0)]
    first = (risers + 1) // 2
    second = risers - first
    landing = (first - 1) * going
    treads = [Line((k * going, -half), (k * going, half), "stair") for k in range(first)]
    if kind == "l":
        top = half + (second - 1) * going
        treads += [
            Line((landing, half + k * going), (landing + width, half + k * going), "stair")
            for k in range(second)
        ]
        outline = [
            Line((0.0, -half), (landing + width, -half), "stair"),
            Line((landing + width, -half), (landing + width, top), "stair"),
            Line((0.0, half), (landing, half), "stair"),
            Line((landing, half), (landing, top), "stair"),
        ]
        return treads, outline, [(0.0, 0.0), (landing + half, 0.0), (landing + half, top)]
    back = landing - (second - 1) * going
    treads += [
        Line((landing - k * going, half), (landing - k * going, 3.0 * half), "stair")
        for k in range(second)
    ]
    outline = [
        Line((0.0, -half), (landing + width, -half), "stair"),
        Line((landing + width, -half), (landing + width, 3.0 * half), "stair"),
        Line((landing + width, 3.0 * half), (back, 3.0 * half), "stair"),
        Line((min(0.0, back), half), (landing, half), "stair"),
    ]
    path = [(0.0, 0.0), (landing + half, 0.0), (landing + half, width), (back, width)]
    return treads, outline, path


def _mirror(p: Pt) -> Pt:
    return (p[0], -p[1])


def stair_prims(stair: Stair, *, lang: str = "en", scale=50) -> tuple[Prim, ...]:
    """Treads, outline, walking line with its arrow, and the "up" label - in WCS, role "stair"."""
    width, going, risers, factor = _validate(stair, scale)
    label = vocab(lang)["up"]
    treads, outline, path = _flights(stair.kind, width, going, risers)
    if stair.turn == "right":
        treads = [line._replace(p1=_mirror(line.p1), p2=_mirror(line.p2)) for line in treads]
        outline = [line._replace(p1=_mirror(line.p1), p2=_mirror(line.p2)) for line in outline]
        path = [_mirror(p) for p in path]

    (px, py), (ex, ey) = path[-2], path[-1]
    length = math.hypot(ex - px, ey - py)
    d = ((ex - px) / length, (ey - py) / length)
    p = (-d[1], d[0])
    arrow = ARROW_PAPER_MM * factor
    tail = (ex - d[0] * arrow, ey - d[1] * arrow)
    head = [
        Line((tail[0] + p[0] * arrow / 3.0, tail[1] + p[1] * arrow / 3.0), (ex, ey), "stair"),
        Line((tail[0] - p[0] * arrow / 3.0, tail[1] - p[1] * arrow / 3.0), (ex, ey), "stair"),
    ]
    height = TEXT_PAPER_MM * factor
    text = Text((0.15 * going, -0.25 * width - 0.5 * height), label, height, 0.0, "stair")
    local = (*treads, *outline, Poly(tuple(path), False, "stair"), *head, text)
    turned = rotate(local, float(stair.direction_deg), (0.0, 0.0))
    return translate(turned, float(stair.start[0]), float(stair.start[1]))
