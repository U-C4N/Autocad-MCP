"""Wall material -> the area hatch (poche) of its cut face.

SOURCE, stated row by row because it is not one document:

* ISO 128-50 (representation of areas on cuts and sections) standardises only
  the *general* hatching — narrow continuous lines at 45 degrees — and leaves
  distinguishing materials to other conventions. ``ANSI31`` in AutoCAD 2026's
  ``acadiso.pat`` is that line set, and its own description in the installed
  file (``R25.1/enu/Support/acadiso.pat``) reads ``*ANSI31,ANSI Iron, Brick,
  Stone masonry``. ``brick`` and ``generic`` take it.
* ``reinforced_concrete``, ``stone`` and ``timber`` take the JIS A 0150
  (Japanese architectural drawing conventions) symbols AutoCAD ships in the
  same file: ``*JIS_RC_15, RC JIS A 0150(@15)``, ``*JIS_STN_2.5, STONE JIS A
  0150(@2.5)`` and ``*JIS_WOOD, WOOD JIS A 0150``. A reinforced-concrete
  section cannot be ANSI31 *and* AR-CONC in one HATCH entity, so it takes the
  one symbol that names reinforced concrete.
* ``concrete`` and ``gypsum_board`` take AutoCAD's own ``*AR-CONC, random dot
  and stone pattern`` and ``*AR-SAND, random dot pattern`` — declared as
  AutoCAD patterns, not standard symbols.
* ``aac`` (autoclaved aerated concrete) has no symbol in any of these. It is
  ANSI31 at twice the brick spacing — a lighter diagonal — which is a
  repository convention and says so.
* ``timber`` adds 90 degrees so its single line family runs on the opposite
  diagonal to brick: JIS_WOOD is one 45-degree family, and unrotated it would
  be indistinguishable from brick on the sheet.

``scale`` is chosen for a 1:50 plan, the default plot scale of every ``arch``
tool (``wall_hatch`` takes no scale, as pinned): brick lines 3.175 x 20 =
63.5 mm apart, 1.27 mm on a 1:50 sheet; aac 2.54 mm; reinforced concrete one
triple-line group every 150 mm (3.0 mm on the sheet); stone 75 mm (1.5 mm);
timber 0.7071 x 90 = 63.6 mm (1.27 mm). AR-CONC and AR-SAND are defined in
building millimetres and ship at 1.0. The periods are recomputed from ezdxf's
own pattern definitions by ``tests/test_arch_foundation.py``, not trusted here.
"""

from __future__ import annotations

WALL_MATERIALS: tuple[str, ...] = (
    "brick",
    "aac",
    "concrete",
    "reinforced_concrete",
    "gypsum_board",
    "stone",
    "timber",
    "generic",
)

WALL_HATCH_TABLE: dict[str, dict] = {
    "brick": {
        "pattern": "ANSI31",
        "angle": 0.0,
        "scale": 20.0,
        "source": "ISO 128-50 general hatching = acadiso.pat *ANSI31 (Iron, Brick, Stone masonry)",
    },
    "aac": {
        "pattern": "ANSI31",
        "angle": 0.0,
        "scale": 40.0,
        "source": "repository convention: ANSI31 at twice the brick spacing; no standard AAC symbol",
    },
    "concrete": {
        "pattern": "AR-CONC",
        "angle": 0.0,
        "scale": 1.0,
        "source": "acadiso.pat *AR-CONC, random dot and stone pattern (AutoCAD, not a standard)",
    },
    "reinforced_concrete": {
        "pattern": "JIS_RC_15",
        "angle": 0.0,
        "scale": 10.0,
        "source": "acadiso.pat *JIS_RC_15, RC JIS A 0150(@15): the JIS A 0150 RC symbol",
    },
    "gypsum_board": {
        "pattern": "AR-SAND",
        "angle": 0.0,
        "scale": 1.0,
        "source": "acadiso.pat *AR-SAND, random dot pattern (AutoCAD, not a standard)",
    },
    "stone": {
        "pattern": "JIS_STN_2.5",
        "angle": 0.0,
        "scale": 30.0,
        "source": "acadiso.pat *JIS_STN_2.5, STONE JIS A 0150(@2.5): the JIS A 0150 stone symbol",
    },
    "timber": {
        "pattern": "JIS_WOOD",
        "angle": 90.0,
        "scale": 90.0,
        "source": "acadiso.pat *JIS_WOOD, WOOD JIS A 0150; turned 90 deg so it cannot read as brick",
    },
    "generic": {
        "pattern": "ANSI31",
        "angle": 0.0,
        "scale": 20.0,
        "source": "ISO 128-50 general hatching = acadiso.pat *ANSI31",
    },
}


def normalise_material(material: str) -> str:
    """The table key a material name is read as: case, spaces and hyphens folded.

    ``"Reinforced Concrete"`` and ``"reinforced-concrete"`` both read as
    ``reinforced_concrete``. The plan model and ``wall_hatch`` share this one
    reading, so the model never refuses a name the hatch table resolves. The
    result may still be unknown; callers refuse it.
    """
    return material.strip().lower().replace("-", "_").replace(" ", "_")


def wall_hatch(material: str) -> dict:
    """``{"pattern", "angle", "scale"}`` for a wall material. Unknown names are refused."""
    if not isinstance(material, str):
        raise ValueError(
            f"material: expected a name, got {type(material).__name__}; "
            f"wall materials are {', '.join(WALL_MATERIALS)}"
        )
    row = WALL_HATCH_TABLE.get(normalise_material(material))
    if row is None:
        raise ValueError(
            f"material: unknown wall material {material!r}; "
            f"wall materials are {', '.join(WALL_MATERIALS)}"
        )
    return {"pattern": row["pattern"], "angle": row["angle"], "scale": row["scale"]}
