"""The furniture and sanitary catalogue: authored outlines, search, blocks.

Pure half first (every item's primitives against its declared nominal size,
the search in both languages, the refusal), then the insert through the
headless backend - the `backend` fixture in tests/conftest.py.
"""

from __future__ import annotations

import pytest

from backends.block_specs import validate_attdef_specs, validate_entity_specs
from engineering.arch.catalogue import (
    CATALOGUE_NAMES,
    FAMILIES,
    ITEMS,
    SIZE_BASIS,
    block_spec,
    catalogue,
    insert_item,
)
from engineering.arch.layers import ARCH_LAYERS, ARCH_ROLE_LAYER
from engineering.mech.primitives import Arc, points_of


def _spec_points(spec: dict) -> list[tuple[float, float]]:
    """Every point that bounds one block_define spec - an arc by its real sweep."""
    kind = spec["type"]
    if kind == "line":
        return [(spec["x1"], spec["y1"]), (spec["x2"], spec["y2"])]
    if kind == "circle":
        cx, cy, r = spec["cx"], spec["cy"], spec["r"]
        return [(cx - r, cy), (cx + r, cy), (cx, cy - r), (cx, cy + r)]
    if kind == "arc":
        arc = Arc((spec["cx"], spec["cy"]), spec["r"], spec["start_deg"], spec["end_deg"])
        return list(points_of(arc))
    if kind == "polyline":
        return [tuple(p) for p in spec["points"]]
    raise AssertionError(f"unexpected spec type {kind!r}")


def test_the_catalogue_holds_twenty_items_in_two_families():
    assert len(ITEMS) == 20
    assert len(set(CATALOGUE_NAMES)) == 20
    assert FAMILIES == ("furniture", "sanitary")
    assert sum(item.family == "furniture" for item in ITEMS) == 13
    assert sum(item.family == "sanitary" for item in ITEMS) == 7
    assert {
        "single_bed",
        "double_bed",
        "wardrobe",
        "sofa_3_seat",
        "armchair",
        "dining_table_4",
        "desk",
        "kitchen_counter",
        "fridge",
        "bookshelf",
        "wc",
        "wall_basin",
        "shower_tray",
        "bathtub",
        "kitchen_sink",
        "washing_machine",
    } <= set(CATALOGUE_NAMES)
    for name in CATALOGUE_NAMES:
        assert name == name.lower() and " " not in name


@pytest.mark.parametrize("name", CATALOGUE_NAMES)
def test_every_item_stays_inside_its_declared_nominal_size(name):
    spec = block_spec(name)
    w, d = spec["size"]
    assert w > 0 and d > 0
    for entity in spec["entities"]:
        for x, y in _spec_points(entity):
            assert -1e-9 <= x <= w + 1e-9, (name, entity)
            assert -1e-9 <= y <= d + 1e-9, (name, entity)
    for att in spec["attdefs"]:
        assert 0.0 <= att["x"] <= w and 0.0 <= att["y"] <= d


@pytest.mark.parametrize("name", CATALOGUE_NAMES)
def test_every_block_spec_validates_against_block_define(name):
    spec = block_spec(name)
    normalised = validate_entity_specs(spec["entities"])
    assert {entity["layer"] for entity in normalised} == {"0"}
    (att,) = validate_attdef_specs(spec["attdefs"])
    assert att["tag"] == "ITEM" and att["default"] == name and att["invisible"] is True
    assert spec["name"] == "ARCH_" + name.upper()
    assert spec["size_basis"] == SIZE_BASIS


def test_every_outline_reaches_its_declared_size():
    """The declared size is the item's footprint, not a loose bound."""
    for name in CATALOGUE_NAMES:
        spec = block_spec(name)
        points = [p for entity in spec["entities"] for p in _spec_points(entity)]
        w, d = spec["size"]
        assert min(x for x, _ in points) == pytest.approx(0.0, abs=1e-9), name
        assert max(x for x, _ in points) == pytest.approx(w, abs=1e-9), name
        assert min(y for _, y in points) == pytest.approx(0.0, abs=1e-9), name
        # the wc's bowl arc stops at y = 680 inside its 700 mm nominal depth
        assert max(y for _, y in points) <= d + 1e-9, name


def test_the_wc_bowl_is_a_semicircle_joining_its_two_sides():
    spec = block_spec("wc")
    arc = next(e for e in spec["entities"] if e["type"] == "arc")
    assert (arc["cx"], arc["cy"], arc["r"], arc["start_deg"], arc["end_deg"]) == (
        190.0,
        530.0,
        150.0,
        0.0,
        180.0,
    )
    sides = [e for e in spec["entities"] if e["type"] == "line"]
    assert [(s["x1"], s["y2"]) for s in sides] == [(40.0, 530.0), (340.0, 530.0)]


def test_the_sofa_cushions_split_the_seat_into_three_equal_widths():
    spec = block_spec("sofa_3_seat")
    verticals = sorted(
        e["x1"] for e in spec["entities"] if e["type"] == "line" and e["x1"] == e["x2"]
    )
    assert verticals == [200.0, 800.0, 1400.0, 2000.0]


def test_search_by_english_word():
    names = [row["name"] for row in catalogue("bed")]
    assert names == ["single_bed", "double_bed"]
    assert [row["name"] for row in catalogue("washing machine")] == ["washing_machine"]


def test_search_by_turkish_word_with_and_without_diacritics():
    assert [row["name"] for row in catalogue("yatak")] == ["single_bed", "double_bed"]
    assert [row["name"] for row in catalogue("buzdolabı")] == ["fridge"]
    assert [row["name"] for row in catalogue("buzdolabi")] == ["fridge"]
    assert [row["name"] for row in catalogue("KLOZET")] == ["wc"]
    assert [row["name"] for row in catalogue("çamaşır")] == ["washing_machine"]
    assert [row["name"] for row in catalogue("camasir")] == ["washing_machine"]


def test_search_every_word_must_match():
    assert [row["name"] for row in catalogue("masa")] == ["dining_table_4", "desk"]
    assert [row["name"] for row in catalogue("yemek masa")] == ["dining_table_4"]
    assert catalogue("bed sink") == ()


def test_family_filter_and_the_label_language():
    sanitary = catalogue(family="sanitary")
    assert [row["name"] for row in sanitary] == [
        "wc",
        "bidet",
        "wall_basin",
        "shower_tray",
        "bathtub",
        "kitchen_sink",
        "washing_machine",
    ]
    assert all(row["layer"] == ARCH_ROLE_LAYER["sanitary"] for row in sanitary)
    (row,) = catalogue("fridge", lang="tr")
    assert row["label"] == "buzdolabı" and row["label_en"] == "fridge"
    assert row["size"] == [600.0, 650.0]
    assert row["size_basis"] == SIZE_BASIS
    assert row["block"] == "ARCH_FRIDGE"
    assert len(catalogue()) == 20


def test_unknown_family_and_language_are_refused_with_the_list():
    with pytest.raises(ValueError, match="furniture, sanitary"):
        catalogue(family="lighting")
    with pytest.raises(ValueError, match="en, tr"):
        catalogue(lang="de")


def test_an_unknown_name_is_refused_with_the_nearest_names():
    with pytest.raises(ValueError, match=r"no item 'singlebed'; nearest: single_bed"):
        block_spec("singlebed")
    with pytest.raises(ValueError, match="the catalogue holds single_bed"):
        block_spec("zzz")


def test_a_label_spelling_resolves_to_the_item():
    assert block_spec("Single Bed")["item"] == "single_bed"
    assert block_spec("washing-machine")["item"] == "washing_machine"


@pytest.mark.asyncio
async def test_insert_defines_the_block_once_across_two_inserts(backend):
    first = await insert_item(backend, "double_bed", at=(1000.0, 2000.0))
    assert first["ok"] is True
    assert first["block_name"] == "ARCH_DOUBLE_BED"
    assert first["defined"] is True
    assert first["layer"] == ARCH_ROLE_LAYER["furniture"]
    assert first["layer_created"] is True
    assert first["size"] == [1600.0, 2000.0]
    assert first["size_basis"] == SIZE_BASIS

    second = await insert_item(backend, "double_bed", at=(4000.0, 2000.0), rotation=90.0)
    assert second["defined"] is False
    assert second["layer_created"] is False
    assert second["rotation"] == 90.0

    blocks = [b.name for b in await backend.block_list() if b.name == "ARCH_DOUBLE_BED"]
    assert blocks == ["ARCH_DOUBLE_BED"]
    inserts = await backend.entity_list(type_filter="INSERT", limit=100)
    assert sorted(e.handle for e in inserts) == sorted([first["handle"], second["handle"]])
    assert {e.layer for e in inserts} == {ARCH_ROLE_LAYER["furniture"]}
    assert await backend.block_get_attributes(first["handle"]) == {"ITEM": "double_bed"}


@pytest.mark.asyncio
async def test_insert_creates_the_layer_from_its_arch_layers_row(backend):
    result = await insert_item(backend, "wc", at=(0.0, 0.0))
    layer = ARCH_ROLE_LAYER["sanitary"]
    assert result["layer"] == layer
    row = next(row for row in ARCH_LAYERS if row[0] == layer)
    info = next(info for info in await backend.layer_list() if info.name == layer)
    assert info.color == row[1]


@pytest.mark.asyncio
async def test_insert_refuses_before_writing_anything(backend):
    before = len(await backend.entity_list())
    with pytest.raises(ValueError, match="nearest: bathtub"):
        await insert_item(backend, "bathtup", at=(0.0, 0.0))
    with pytest.raises(ValueError, match="finite"):
        await insert_item(backend, "bathtub", at=(float("nan"), 0.0))
    with pytest.raises(ValueError, match="rotation must be finite"):
        await insert_item(backend, "bathtub", at=(0.0, 0.0), rotation=float("inf"))
    assert len(await backend.entity_list()) == before
    assert "ARCH_BATHTUB" not in {b.name for b in await backend.block_list()}
