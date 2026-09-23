"""Door and window symbols in plan, hosted in their wall.

The wall engine (`walls.py`) cuts the gap; this module draws what stands in it.
Both read the opening's place from the same numbers - the wall's axis, its face
offsets and ``model.segment_of`` - so the symbol cannot drift off its gap.

Conventions (this repository's, stated because plan symbols vary by office):

* ``swing="in"`` opens to the **left** of the wall's axis, looking along it
  from its first point; ``"out"`` to the right. A perimeter drawn
  counter-clockwise has its inside on the left, so "in" means into the building.
* ``hand`` names the jamb that carries the hinges, seen by a person standing on
  the side the door swings towards and facing the wall: ``"left"`` hinges on
  their left-hand jamb.
* A door is drawn **open at 90°**: the leaf is a line as long as the opening is
  wide, square to the wall from the hinge, on the swing-side face; the swing is
  a quarter arc about the hinge from the closed position to the open one.
* A window is a frame line on each wall face across the opening and two
  glazing lines between them at the thirds of the wall thickness (a drafting
  convention, not a standard).

The first primitive is the one `draw.py` writes the opening record on: the
door leaf, or the window's frame line on the left face.
"""

from __future__ import annotations

import math

from engineering.arch.model import Opening, Wall, segment_of
from engineering.arch.walls import face_offsets, wall_segments
from engineering.mech.primitives import Arc, Line, Prim, Pt


def _deg(v: Pt) -> float:
    return math.degrees(math.atan2(v[1], v[0])) % 360.0


def opening_prims(wall: Wall, opening: Opening) -> tuple[Prim, ...]:
    """The door leaf and swing, or the window frame and glazing, in WCS."""
    if opening.wall != wall.id:
        raise ValueError(
            f"opening {opening.id!r} is hosted in wall {opening.wall!r}, not in {wall.id!r}"
        )
    width = float(opening.width)
    index, along = segment_of(wall, float(opening.offset))
    (ax, ay), (bx, by) = wall_segments(wall)[index]
    length = math.hypot(bx - ax, by - ay)
    u = ((bx - ax) / length, (by - ay) / length)
    n = (-u[1], u[0])
    left, right = face_offsets(wall)

    def at(s: float, offset: float) -> Pt:
        return (ax + u[0] * s + n[0] * offset, ay + u[1] * s + n[1] * offset)

    s0, s1 = along, along + width
    if opening.kind == "window":
        third = (left - right) / 3.0
        return (
            Line(at(s0, left), at(s1, left), "window"),
            Line(at(s0, right), at(s1, right), "window"),
            Line(at(s0, right + third), at(s1, right + third), "window"),
            Line(at(s0, right + 2.0 * third), at(s1, right + 2.0 * third), "window"),
        )
    if opening.kind != "door":
        raise ValueError(f"opening {opening.id!r}: kind {opening.kind!r}; kinds are door, window")
    if opening.swing not in ("in", "out"):
        raise ValueError(f"opening {opening.id!r}: swing {opening.swing!r}; swings are in, out")
    if opening.hand not in ("left", "right"):
        raise ValueError(f"opening {opening.id!r}: hand {opening.hand!r}; hands are left, right")

    side = 1.0 if opening.swing == "in" else -1.0
    face = left if side > 0 else right
    # Facing the wall from the swing side, the viewer's left hand points along
    # +u when they stand on the axis's left, and along -u when on its right.
    toward = side if opening.hand == "left" else -side
    hinge_s, free_s = (s1, s0) if toward > 0 else (s0, s1)
    hinge = at(hinge_s, face)
    closed = at(free_s, face)
    opened = (hinge[0] + n[0] * side * width, hinge[1] + n[1] * side * width)
    a_closed = _deg((closed[0] - hinge[0], closed[1] - hinge[1]))
    a_open = _deg((opened[0] - hinge[0], opened[1] - hinge[1]))
    if abs(((a_open - a_closed) % 360.0) - 90.0) < 1e-6:
        swing = Arc(hinge, width, a_closed, a_open, "door")
    else:
        swing = Arc(hinge, width, a_open, a_closed, "door")
    return (Line(hinge, opened, "door"), swing)
