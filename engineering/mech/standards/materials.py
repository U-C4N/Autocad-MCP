"""Material -> section hatch (pattern, angle, scale).

SOURCE, stated exactly, because it is not one document:

* ISO 128-50:2020, clause 5 (representation of areas on cuts and sections)
  standardises the *general* hatching only — narrow continuous lines, drawn
  preferably at 45 degrees to the principal outline — and leaves distinguishing
  materials to other conventions. ``ANSI31`` in AutoCAD 2026's ``acadiso.pat``
  is exactly that line set (45 deg, 2.245 mm apart), and it is what
  ``engineering/gear.py`` already draws for a steel section, so `steel` keeps
  it.
* The distinguishing symbols are ASME Y14.2M's section-lining symbols as
  shipped in ``acadiso.pat``, whose own descriptions name the material — read
  off the installed file on this machine (AutoCAD 2026,
  ``R25.1/enu/Support/acadiso.pat``): ``*ANSI32,ANSI Steel``,
  ``*ANSI33,ANSI Bronze, Brass, Copper``, ``*ANSI34,ANSI Plastic, Rubber``,
  ``*ANSI37,ANSI Lead, Zinc, Magnesium, Sound/Heat/Elec Insulation``,
  ``*ANSI38,ANSI Aluminum``.
* ``cast_iron`` is the one deliberate departure and says so in its own row:
  ASME's cast-iron symbol is ANSI31, which this map reserves for the ISO
  general hatching, so cast iron takes ANSI32 to stay *distinguishable* on the
  sheet. That is a repository convention, not a standard symbol — the material
  is identified by the parts-list entry, never by the pattern alone.
* ``concrete`` and ``wood`` have no mechanical section symbol in either ISO
  128-50 or ASME Y14.2M. They use AutoCAD's own ``AR-CONC`` and ``JIS_WOOD``
  (``*JIS_WOOD, WOOD JIS A 0150``), declared as such in their source line.

``angle`` is the rotation *added* to the pattern, and it is 0.0 everywhere: the
ANSI3x definitions already carry 45 degrees, so an angle of 45 would draw steel
hatching vertically. ``scale`` is 1.0 for the ANSI family (one scaled set) and,
for the two architectural patterns, is derived so the finest line in the
pattern matches ANSI31's 2.245 mm spacing — AR-CONC is drawn at building scale
and JIS_WOOD at 0.5 mm, so 1.0 would be unreadable on a 40 mm part. Both
derived numbers are recomputed from ezdxf's own pattern definitions by
``tests/test_mech_materials.py`` rather than trusted here.
"""

from __future__ import annotations

MATERIAL_TABLE: dict[str, dict] = {
    "steel": {
        "pattern": "ANSI31",
        "angle": 0.0,
        "scale": 1.0,
        "source": (
            "ISO 128-50:2020 general hatching (45 deg narrow continuous lines) = acadiso.pat ANSI31"
        ),
    },
    "cast_iron": {
        "pattern": "ANSI32",
        "angle": 0.0,
        "scale": 1.0,
        "source": (
            "acadiso.pat ANSI32 (ASME Y14.2M steel symbol); repository convention so cast "
            "iron stays distinguishable from the ISO general hatching reserved for steel"
        ),
    },
    "aluminium": {
        "pattern": "ANSI38",
        "angle": 0.0,
        "scale": 1.0,
        "source": "ASME Y14.2M section lining = acadiso.pat *ANSI38,ANSI Aluminum",
    },
    "copper_alloy": {
        "pattern": "ANSI33",
        "angle": 0.0,
        "scale": 1.0,
        "source": "ASME Y14.2M section lining = acadiso.pat *ANSI33,ANSI Bronze, Brass, Copper",
    },
    "plastic": {
        "pattern": "ANSI34",
        "angle": 0.0,
        "scale": 1.0,
        "source": "ASME Y14.2M section lining = acadiso.pat *ANSI34,ANSI Plastic, Rubber",
    },
    "rubber": {
        "pattern": "ANSI34",
        "angle": 0.0,
        "scale": 1.0,
        "source": "ASME Y14.2M section lining = acadiso.pat *ANSI34,ANSI Plastic, Rubber",
    },
    "concrete": {
        "pattern": "AR-CONC",
        "angle": 0.0,
        "scale": 0.392,
        "source": (
            "no mechanical section symbol in ISO 128-50 or ASME Y14.2M; acadiso.pat "
            "*AR-CONC, random dot and stone pattern, scaled to the ANSI31 line spacing"
        ),
    },
    "wood": {
        "pattern": "JIS_WOOD",
        "angle": 0.0,
        "scale": 4.49,
        "source": (
            "no mechanical section symbol in ISO 128-50 or ASME Y14.2M; acadiso.pat "
            "*JIS_WOOD, WOOD JIS A 0150, scaled to the ANSI31 line spacing"
        ),
    },
    "insulation": {
        "pattern": "ANSI37",
        "angle": 0.0,
        "scale": 1.0,
        "source": (
            "ASME Y14.2M section lining = acadiso.pat *ANSI37,ANSI Lead, Zinc, Magnesium, "
            "Sound/Heat/Elec Insulation"
        ),
    },
}

MATERIALS: tuple[str, ...] = tuple(MATERIAL_TABLE)

#: Spellings a caller will reasonably type, mapped to a canonical name.
ALIASES: dict[str, str] = {
    "aluminum": "aluminium",
    "alu": "aluminium",
    "brass": "copper_alloy",
    "bronze": "copper_alloy",
    "copper": "copper_alloy",
    "cast_steel": "steel",
    "grey_cast_iron": "cast_iron",
    "gg": "cast_iron",
    "ductile_iron": "cast_iron",
    "polymer": "plastic",
    "elastomer": "rubber",
}


def _canonical(material: str) -> str:
    if not isinstance(material, str):
        raise TypeError(f"material must be a name, got {type(material).__name__}")
    name = material.strip().lower().replace("-", "_").replace(" ", "_")
    name = ALIASES.get(name, name)
    if name not in MATERIAL_TABLE:
        raise ValueError(
            f"unknown material {material!r}; the ISO 128-50 section-hatch map covers "
            f"{', '.join(MATERIALS)}"
        )
    return name


def hatch_for(material: str) -> dict:
    """``{"pattern", "angle", "scale"}`` for a material. Unknown names are refused."""
    row = MATERIAL_TABLE[_canonical(material)]
    return {"pattern": row["pattern"], "angle": row["angle"], "scale": row["scale"]}
