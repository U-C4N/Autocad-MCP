"""The section-hatch map: every pattern exists in both engines, every row cites.

The ezdxf failure this guards is silent: ``Hatch.set_pattern_fill`` falls back
to ANSI31 for a name it does not know (``ezdxf/entities/polygon.py``), so a
typo'd pattern draws steel hatching on a cast-iron part and says nothing.
"""

from __future__ import annotations

import pytest
from ezdxf.tools import pattern as ezpattern

from engineering.mech.standards.materials import (
    ALIASES,
    MATERIAL_TABLE,
    MATERIALS,
    hatch_for,
)

ANSI31_SPACING = 2.2450640303  # acadiso.pat ANSI31, the ISO 128-50 general hatching


def _spacing(name: str, defs: dict) -> float:
    """The finest perpendicular line spacing in a pattern definition."""
    return min(abs(line[2][1]) for line in defs[name] if abs(line[2][1]) > 1e-9)


def test_the_map_covers_the_nine_named_materials():
    assert MATERIALS == (
        "steel",
        "cast_iron",
        "aluminium",
        "copper_alloy",
        "plastic",
        "rubber",
        "concrete",
        "wood",
        "insulation",
    )


def test_hatch_for_returns_exactly_pattern_angle_scale():
    assert set(hatch_for("steel")) == {"pattern", "angle", "scale"}


@pytest.mark.parametrize(
    ("material", "expected"),
    [
        ("steel", "ANSI31"),
        ("cast_iron", "ANSI32"),
        ("aluminium", "ANSI38"),
        ("copper_alloy", "ANSI33"),
        ("plastic", "ANSI34"),
        ("rubber", "ANSI34"),
        ("insulation", "ANSI37"),
        ("concrete", "AR-CONC"),
        ("wood", "JIS_WOOD"),
    ],
)
def test_each_material_keeps_its_pattern(material, expected):
    assert hatch_for(material)["pattern"] == expected


def test_aliases_resolve_and_are_case_and_separator_insensitive():
    assert hatch_for("Cast Iron")["pattern"] == "ANSI32"
    assert hatch_for("cast-iron")["pattern"] == "ANSI32"
    assert hatch_for("aluminum")["pattern"] == "ANSI38"
    assert hatch_for("BRASS")["pattern"] == "ANSI33"
    assert hatch_for("bronze")["pattern"] == "ANSI33"
    assert hatch_for("copper")["pattern"] == "ANSI33"
    assert set(ALIASES.values()) <= set(MATERIALS)


def test_an_unknown_material_is_refused_with_the_list():
    with pytest.raises(ValueError, match="unobtainium.*cast_iron"):
        hatch_for("unobtainium")


def test_every_pattern_exists_in_both_measurement_sets():
    """Guards ezdxf's silent ANSI31 fallback and AutoCAD's unknown-pattern error."""
    iso = ezpattern.load(measurement=1)
    imperial = ezpattern.load(measurement=0)
    for material in MATERIALS:
        name = hatch_for(material)["pattern"]
        assert name in iso, f"{material}: {name} is not an ezdxf ISO pattern"
        assert name in imperial, f"{material}: {name} is not an ezdxf imperial pattern"


def test_the_map_adds_no_rotation_because_the_patterns_already_carry_45_degrees():
    defs = ezpattern.load(measurement=1)
    for material in MATERIALS:
        row = hatch_for(material)
        assert row["angle"] == 0.0
        if row["pattern"].startswith("ANSI3"):
            assert defs[row["pattern"]][0][0] == 45.0


def test_the_ansi_rows_ship_at_the_pattern_definition_scale():
    for material in (
        "steel",
        "cast_iron",
        "aluminium",
        "copper_alloy",
        "plastic",
        "rubber",
        "insulation",
    ):
        assert hatch_for(material)["scale"] == 1.0


def test_the_two_non_ansi_patterns_are_scaled_to_the_iso_line_spacing():
    """Concrete and wood are architectural patterns: AR-CONC is defined at
    building scale and JIS_WOOD at 0.5 mm. Their scale is derived, not chosen —
    the finest line in the pattern is brought to ANSI31's spacing."""
    defs = ezpattern.load(measurement=1)
    assert abs(_spacing("ANSI31", defs) - ANSI31_SPACING) < 1e-6
    for material in ("concrete", "wood"):
        row = hatch_for(material)
        derived = round(ANSI31_SPACING / _spacing(row["pattern"], defs), 3)
        assert row["scale"] == derived


def test_every_row_states_where_its_symbol_comes_from():
    for material in MATERIALS:
        source = MATERIAL_TABLE[material]["source"]
        assert any(token in source for token in ("ISO 128-50", "ASME Y14.2M", "acadiso.pat"))
