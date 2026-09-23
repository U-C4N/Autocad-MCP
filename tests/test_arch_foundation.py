"""Track F's shared foundation: the arch layer set, the wall-material hatches,
EN/TR vocabulary, and the two generalisations of the mechanical layer
(`draw_prims(role_layer=)` and the XDATA codec's `app_id=` / `kinds=`).

The mechanical callers are the regression gate for the two generalisations:
`tests/test_mech_draw.py`, `tests/test_mech_xdata.py` and the rest of the
mechanical suite run unchanged against the new signatures.
"""

from __future__ import annotations

import math

import pytest
from ezdxf.tools import pattern as ezpattern

from engineering.arch.lang import LANGS, fmt_area_m2, fmt_number, tag_prefix, vocab
from engineering.arch.layers import ARCH_LAYERS, ARCH_ROLE_LAYER
from engineering.arch.materials import WALL_HATCH_TABLE, WALL_MATERIALS, wall_hatch
from engineering.layers import LAYER_ROLES, LAYER_SET_REGISTRY, apply_layer_set, resolve_role_layer
from engineering.mech import xdata as codec
from engineering.mech.draw import draw_prims
from engineering.mech.primitives import HatchArea, Line, Text
from engineering.plan_spec import ISO_128_LINEWEIGHTS_MM

# -- the layer set ---------------------------------------------------------------

EXPECTED_LAYERS = {
    "A-WALL-E-N": ("Continuous", 0.50),
    "A-WALL-H-N": ("Continuous", 0.13),
    "A-DOOR-E-N": ("Continuous", 0.25),
    "A-WIND-E-N": ("Continuous", 0.25),
    "A-STAIR-E-N": ("Continuous", 0.25),
    "A-FURN-E-N": ("Continuous", 0.18),
    "A-SANR-E-N": ("Continuous", 0.18),
    "A-GRID-E-N": ("CENTER", 0.18),
    "A-ROOM-T-N": ("Continuous", 0.25),
    "A-DIMEN-T-N": ("Continuous", 0.18),
    "A-SYMB-T-N": ("Continuous", 0.25),
    "A-OVHD-E-N": ("HIDDEN", 0.18),
    "A-CONST-E-N": ("Continuous", 0.05),
}


def test_the_arch_set_is_the_thirteen_pinned_layers_plus_zero():
    rows = {name: (linetype, lw) for name, _aci, linetype, lw, _d in ARCH_LAYERS}
    assert rows.pop("0") == ("Continuous", 0.25)
    assert rows == EXPECTED_LAYERS


def test_every_lineweight_but_the_construction_scratch_is_iso_128():
    for name, _aci, _lt, lineweight, _d in ARCH_LAYERS:
        if "CONST" in name:
            assert lineweight == 0.05  # skipped by the iso128 critique, cleared before finalize
            continue
        assert lineweight in ISO_128_LINEWEIGHTS_MM, name


def test_the_construction_layer_is_colour_250_like_every_other_set():
    colours = {name: aci for name, aci, _lt, _lw, _d in ARCH_LAYERS}
    assert colours["A-CONST-E-N"] == 250


def test_every_role_maps_onto_a_layer_of_the_set():
    names = {row[0] for row in ARCH_LAYERS}
    assert set(ARCH_ROLE_LAYER.values()) <= names
    assert ARCH_ROLE_LAYER == {
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


def test_the_set_is_registered_with_its_dimension_and_construction_roles():
    assert LAYER_SET_REGISTRY["arch"] is ARCH_LAYERS
    assert LAYER_ROLES["arch"] == {"dim": "A-DIMEN-T-N", "construction": "A-CONST-E-N"}
    assert resolve_role_layer("arch", "dim") == "A-DIMEN-T-N"
    assert resolve_role_layer("arch", "construction") == "A-CONST-E-N"


@pytest.mark.asyncio
async def test_apply_layer_set_arch_creates_every_layer_with_its_row(backend):
    result = await apply_layer_set(backend, "arch")
    assert all(result[name] == "created" for name in EXPECTED_LAYERS)
    layers = {lyr.name: lyr for lyr in await backend.layer_list()}
    assert layers["A-GRID-E-N"].linetype.upper() == "CENTER"
    assert layers["A-OVHD-E-N"].linetype.upper() == "HIDDEN"
    assert layers["A-CONST-E-N"].color == 250


# -- wall materials --------------------------------------------------------------


def _period(name: str) -> float:
    """The finest perpendicular line period of a pattern at scale 1 (ezdxf ISO set)."""
    periods = []
    for angle, _base, offset, _dashes in ezpattern.load(measurement=1)[name]:
        rad = math.radians(angle)
        period = abs(-offset[0] * math.sin(rad) + offset[1] * math.cos(rad))
        if period > 1e-9:
            periods.append(period)
    return min(periods)


def test_the_eight_pinned_materials_each_have_one_row():
    assert WALL_MATERIALS == (
        "brick",
        "aac",
        "concrete",
        "reinforced_concrete",
        "gypsum_board",
        "stone",
        "timber",
        "generic",
    )
    assert set(WALL_HATCH_TABLE) == set(WALL_MATERIALS)


def test_wall_hatch_returns_exactly_pattern_angle_scale():
    assert wall_hatch("brick") == {"pattern": "ANSI31", "angle": 0.0, "scale": 20.0}
    assert wall_hatch("Reinforced Concrete")["pattern"] == "JIS_RC_15"


def test_an_unknown_material_is_refused_with_the_list():
    with pytest.raises(
        ValueError, match=r"unknown wall material 'adobe'; wall materials are brick, aac"
    ):
        wall_hatch("adobe")


def test_every_pattern_exists_in_both_ezdxf_measurement_sets():
    """ezdxf silently falls back to ANSI31 for a pattern it does not know."""
    iso = ezpattern.load(measurement=1)
    imperial = ezpattern.load(measurement=0)
    for material in WALL_MATERIALS:
        name = wall_hatch(material)["pattern"]
        assert name in iso and name in imperial, material


def test_the_periods_on_a_1_to_50_sheet_are_the_documented_ones():
    """Recomputed from ezdxf's own definitions: period x scale / 50 = paper mm."""
    expected_paper_mm = {
        "brick": 1.27,
        "aac": 2.54,
        "reinforced_concrete": 3.0,
        "stone": 1.5,
        "timber": 1.273,
        "generic": 1.27,
    }
    for material, paper in expected_paper_mm.items():
        row = wall_hatch(material)
        assert round(_period(row["pattern"]) * row["scale"] / 50.0, 3) == paper, material


def test_aac_is_the_lighter_diagonal_of_brick():
    assert wall_hatch("aac")["pattern"] == wall_hatch("brick")["pattern"]
    assert wall_hatch("aac")["scale"] == 2 * wall_hatch("brick")["scale"]


def test_every_row_states_where_its_symbol_comes_from():
    for material in WALL_MATERIALS:
        source = WALL_HATCH_TABLE[material]["source"]
        assert any(
            token in source for token in ("ISO 128-50", "JIS A 0150", "acadiso.pat", "convention")
        )


# -- language --------------------------------------------------------------------


def test_both_languages_carry_the_same_keys():
    assert LANGS == ("en", "tr")
    assert (
        set(vocab("en"))
        == set(vocab("tr"))
        == {
            "door_schedule",
            "window_schedule",
            "room_schedule",
            "tag",
            "width",
            "height",
            "sill",
            "swing",
            "hand",
            "wall",
            "number",
            "name",
            "area",
            "total",
            "up",
            "down",
            "north",
            "level",
            "in",
            "out",
            "left",
            "right",
        }
    )
    assert vocab("tr")["up"] == "ÇIKIŞ"
    assert vocab("en")["up"] == "UP"


def test_tag_prefixes():
    assert (tag_prefix("door", "en"), tag_prefix("window", "en")) == ("D", "W")
    assert (tag_prefix("door", "tr"), tag_prefix("window", "tr")) == ("K", "P")


def test_turkish_switches_the_decimal_separator_to_a_comma():
    assert fmt_number(24.5, "en") == "24.50"
    assert fmt_number(24.5, "tr") == "24,50"
    assert fmt_number(-0.001, "en") == "0.00"
    assert fmt_area_m2(24_500_000.0, "en") == "24.50 m²"
    assert fmt_area_m2(24_500_000.0, "tr") == "24,50 m²"
    assert fmt_area_m2(20_000_000.0, "en") == "20.00 m²"


def test_an_unknown_language_is_refused_with_the_list():
    for call in (
        lambda: vocab("de"),
        lambda: fmt_number(1.0, "de"),
        lambda: tag_prefix("door", "fr"),
    ):
        with pytest.raises(
            ValueError, match=r"lang: unknown language '(de|fr)'; languages are en, tr"
        ):
            call()


def test_a_negative_area_is_refused():
    with pytest.raises(ValueError, match=r"area: must be finite and not negative"):
        fmt_area_m2(-1.0, "en")


# -- draw_prims(role_layer=) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_role_map_puts_each_role_on_its_architectural_layer(backend):
    square = ((0.0, 0.0), (200.0, 0.0), (200.0, 1000.0), (0.0, 1000.0))
    result = await draw_prims(
        backend,
        [
            Line((0.0, 0.0), (200.0, 0.0), "wall"),
            Line((0.0, 0.0), (0.0, 900.0), "door"),
            Text((500.0, 500.0), "LIVING", 125.0, 0.0, "room"),
            HatchArea((square,), "brick"),
        ],
        role_layer=ARCH_ROLE_LAYER,
    )
    assert len(result["handles"]) == 4
    placed = {(e.type, e.layer) for e in await backend.entity_list(limit=100)}
    assert placed == {
        ("LINE", "A-WALL-E-N"),
        ("LINE", "A-DOOR-E-N"),
        ("TEXT", "A-ROOM-T-N"),
        ("HATCH", "A-WALL-H-N"),
    }


@pytest.mark.asyncio
async def test_a_wall_hatch_takes_the_wall_material_pattern(backend):
    square = ((0.0, 0.0), (200.0, 0.0), (200.0, 1000.0), (0.0, 1000.0))
    await draw_prims(
        backend, [HatchArea((square,), "reinforced_concrete")], role_layer=ARCH_ROLE_LAYER
    )
    (hatch,) = await backend.entity_list(type_filter="HATCH", limit=10)
    entity = backend._doc.entitydb[hatch.handle]
    assert entity.dxf.pattern_name == "JIS_RC_15"
    assert math.isclose(entity.dxf.pattern_scale, 10.0)


@pytest.mark.asyncio
async def test_missing_architectural_layers_are_created_with_their_row(backend):
    await draw_prims(backend, [Line((0.0, 0.0), (1000.0, 0.0), "grid")], role_layer=ARCH_ROLE_LAYER)
    layers = {lyr.name: lyr for lyr in await backend.layer_list()}
    grid = layers["A-GRID-E-N"]
    assert grid.linetype.upper() == "CENTER"
    assert grid.color == 1


@pytest.mark.asyncio
async def test_an_unknown_role_is_refused_before_anything_is_drawn(backend):
    with pytest.raises(
        ValueError, match=r"draw_prims: unknown role 'visible'; this role map has wall"
    ):
        await draw_prims(
            backend,
            [Line((0.0, 0.0), (1.0, 0.0), "wall"), Line((0.0, 0.0), (1.0, 0.0), "visible")],
            role_layer=ARCH_ROLE_LAYER,
        )
    assert await backend.entity_list(limit=10) == []


@pytest.mark.asyncio
async def test_an_unknown_wall_material_is_refused_before_anything_is_drawn(backend):
    square = ((0.0, 0.0), (200.0, 0.0), (200.0, 1000.0), (0.0, 1000.0))
    with pytest.raises(ValueError, match=r"unknown wall material 'steel'"):
        await draw_prims(
            backend,
            [Line((0.0, 0.0), (1.0, 0.0), "wall"), HatchArea((square,), "steel")],
            role_layer=ARCH_ROLE_LAYER,
        )
    assert await backend.entity_list(limit=10) == []


@pytest.mark.asyncio
async def test_without_a_role_map_the_mechanical_layers_and_materials_are_unchanged(backend):
    square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
    await draw_prims(backend, [Line((0.0, 0.0), (1.0, 0.0)), HatchArea((square,), "steel")])
    layers = {e.layer for e in await backend.entity_list(limit=10)}
    assert layers == {"GEOMETRY", "HATCH"}
    (hatch,) = await backend.entity_list(type_filter="HATCH", limit=10)
    assert backend._doc.entitydb[hatch.handle].dxf.pattern_name == "ANSI31"


# -- the XDATA codec's app_id / kinds ----------------------------------------------


def test_the_codec_defaults_are_still_the_mechanical_ones():
    part = {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": {}}
    assert codec.encode(part)[0] == (1001, "ACADMCP_MECH")
    assert codec.decode(codec.to_values(part)) == part


def test_the_codec_writes_and_reads_another_application():
    payload = {"v": 1, "kind": "thing", "n": 3}
    rows = codec.encode(payload, app_id="ACADMCP_TEST", kinds=("thing",))
    assert rows[0] == (1001, "ACADMCP_TEST")
    assert codec.decode(rows, app_id="ACADMCP_TEST", kinds=("thing",)) == payload
    with pytest.raises(
        ValueError, match=r"ACADMCP_TEST: unknown payload kind 'part'; kinds are thing"
    ):
        codec.encode({"v": 1, "kind": "part"}, app_id="ACADMCP_TEST", kinds=("thing",))
    with pytest.raises(
        ValueError, match=r"ACADMCP_MECH: this XDATA belongs to application 'ACADMCP_TEST'"
    ):
        codec.decode(rows)


# -- the tool ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drawing_apply_iso_layers_takes_arch():
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        result = (
            await client.call_tool("drawing_apply_iso_layers", {"standard": "arch"})
        ).structured_content
    assert result["standard"] == "arch"
    assert result["layers"]["A-WALL-E-N"] == "created"
    assert result["layers"]["A-GRID-E-N"] == "created"
