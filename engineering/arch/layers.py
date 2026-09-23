"""The ``arch`` layer set and the role -> layer map every architectural module uses.

Names follow the ISO 13567 field order the repository's ``iso13567`` set
already uses — agent (``A`` = architect), element, presentation (``E`` element
graphics, ``H`` hatching, ``T`` text/annotation), status (``N`` new) — with the
element field in its common short spelling (``WALL``, ``DOOR``, ``WIND``); the
fields are not padded to ISO 13567-2's fixed widths, exactly as the existing
``M-GEOMET-E-N`` rows are not.

Lineweights are ISO 128 values only (0.13 / 0.18 / 0.25 / 0.50). The one
exception is ``A-CONST-E-N`` at 0.05 mm, the scratch weight every construction
layer in this repository carries: the ``iso128`` critique skips a layer whose
name contains ``CONST``, and ``construction_left`` requires it to be empty
before finalize. Colours are a repository convention — ISO 13567 fixes names,
not colours.

``ARCH_ROLE_LAYER`` is the one place a role becomes a layer name, so no tool
hardcodes one.
"""

from __future__ import annotations

#: (name, ACI colour, linetype, lineweight mm, description) — the row shape of
#: every set in ``engineering/layers.py``.
ARCH_LAYERS: list[tuple[str, int, str, float, str]] = [
    ("0", 7, "Continuous", 0.25, "default"),
    ("A-WALL-E-N", 7, "Continuous", 0.50, "Architecture / walls, cut outline / new"),
    ("A-WALL-H-N", 8, "Continuous", 0.13, "Architecture / walls, poche hatch / new"),
    ("A-DOOR-E-N", 3, "Continuous", 0.25, "Architecture / doors, leaf and swing / new"),
    ("A-WIND-E-N", 4, "Continuous", 0.25, "Architecture / windows, frame and glazing / new"),
    ("A-STAIR-E-N", 6, "Continuous", 0.25, "Architecture / stairs / new"),
    ("A-FURN-E-N", 5, "Continuous", 0.18, "Architecture / furniture / new"),
    ("A-SANR-E-N", 5, "Continuous", 0.18, "Architecture / sanitary fixtures / new"),
    ("A-GRID-E-N", 1, "CENTER", 0.18, "Architecture / structural grid / new"),
    ("A-ROOM-T-N", 7, "Continuous", 0.25, "Architecture / room labels / new"),
    ("A-DIMEN-T-N", 2, "Continuous", 0.18, "Architecture / dimensions / new"),
    ("A-SYMB-T-N", 7, "Continuous", 0.25, "Architecture / symbols and marks / new"),
    ("A-OVHD-E-N", 3, "HIDDEN", 0.18, "Architecture / overhead, above the cut plane / new"),
    ("A-CONST-E-N", 250, "Continuous", 0.05, "Architecture / construction (scratch)"),
]

#: Architectural role -> layer. ``wall_hatch`` is where a ``HatchArea`` lands
#: when ``draw_prims`` is given this map (a HatchArea carries no role of its own).
ARCH_ROLE_LAYER: dict[str, str] = {
    "wall": "A-WALL-E-N",
    "wall_hatch": "A-WALL-H-N",
    "door": "A-DOOR-E-N",
    "window": "A-WIND-E-N",
    "stair": "A-STAIR-E-N",
    "furniture": "A-FURN-E-N",
    "sanitary": "A-SANR-E-N",
    "grid": "A-GRID-E-N",
    "room": "A-ROOM-T-N",
    "dim": "A-DIMEN-T-N",
    "symbol": "A-SYMB-T-N",
    "overhead": "A-OVHD-E-N",
}

#: The two roles ``engineering/layers.py::resolve_role_layer`` answers for.
ARCH_LAYER_ROLES: dict[str, str] = {"dim": "A-DIMEN-T-N", "construction": "A-CONST-E-N"}

#: name -> (colour, linetype, lineweight): what ``draw_prims`` creates a missing
#: architectural layer with.
ARCH_LAYER_DEFS: dict[str, tuple[int, str, float]] = {
    name: (color, linetype, lineweight) for name, color, linetype, lineweight, _d in ARCH_LAYERS
}
