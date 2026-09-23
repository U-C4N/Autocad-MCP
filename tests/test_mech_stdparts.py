"""The standard-parts catalogue: search, designations, block specs, insertion.

Geometry is asserted as emitted primitive dicts - counts, layers, coordinates -
never as a screenshot.
"""

from __future__ import annotations

import math

import pytest

from backends.block_specs import validate_attdef_specs, validate_entity_specs
from engineering.mech.stdparts import (
    FAMILY_STANDARDS,
    FAMILY_VIEWS,
    THREAD_MINOR_RATIO,
    block_spec,
    catalogue,
    parse_designation,
)

R12 = 2.0 * 18.0 / math.sqrt(3.0) / 2.0  # across-corners radius of an M12 hex head
MINOR12 = THREAD_MINOR_RATIO * 12.0 / 2.0  # ISO 6410 minor radius of an M12 thread


def test_the_iso6410_minor_radius_is_four_point_eight():
    """Pinned as the builder computes it: 0.8 * 12 / 2 is 4.800000000000001 in binary."""
    assert MINOR12 == pytest.approx(4.8)


def test_the_catalogue_holds_every_transcribed_size():
    rows = catalogue()
    assert len(rows) == 83  # 5 fastener standards x 10 sizes + 33 bearings
    assert len(catalogue(standard="ISO 15")) == 33
    assert len(catalogue("M12")) == 5
    assert len(catalogue("6205")) == 1
    assert {row["standard"] for row in rows} == set(FAMILY_STANDARDS)


def test_a_catalogue_row_carries_what_a_drawing_needs():
    (row,) = catalogue("6205")
    assert row["designation"] == "ISO 15 - 6205"
    assert row["standard"] == "ISO 15"
    assert row["family"] == "deep_groove_bearing"
    assert row["views"] == ["simplified", "detailed"]
    assert row["length_required"] is False
    assert row["dims"] == {"d": 25.0, "D": 52.0, "B": 15.0}
    assert "ISO 15:2017" in row["source"]

    bolt = next(r for r in catalogue(standard="ISO 4014") if r["size"] == "M12")
    assert bolt["length_required"] is True
    assert bolt["dims"]["s"] == 18.0 and bolt["dims"]["k"] == 7.5


def test_catalogue_refuses_an_unknown_standard():
    with pytest.raises(ValueError, match="ISO 4014"):
        catalogue(standard="DIN 931")


def test_parse_designation_normalises_and_splits():
    assert parse_designation("iso4014 - m12x60") == {
        "designation": "ISO 4014 - M12x60",
        "standard": "ISO 4014",
        "family": "hex_bolt",
        "size": "M12x60",
        "thread": "M12",
        "length": 60.0,
    }
    assert parse_designation("6205")["designation"] == "ISO 15 - 6205"
    assert parse_designation("ISO 4032 - M12")["length"] is None


def test_parse_designation_refuses_a_missing_or_stray_length():
    with pytest.raises(ValueError, match="nominal length"):
        parse_designation("ISO 4014 - M12")
    with pytest.raises(ValueError, match="takes no length"):
        parse_designation("ISO 4032 - M12x60")
    with pytest.raises(ValueError, match="catalogue standard"):
        parse_designation("ISO 9999 - M12")


def test_an_unknown_size_is_refused_with_the_nearest_entries():
    with pytest.raises(ValueError, match=r"ISO 4014 - M12"):
        block_spec("ISO 4014 - M13x60", "side")
    with pytest.raises(ValueError, match=r"ISO 15 - 6210"):
        block_spec("ISO 15 - 6211", "simplified")
    with pytest.raises(ValueError, match="approximately"):
        block_spec("ISO 4014 - M13x60", "side")


def test_hex_bolt_side_view_is_the_iso6410_representation():
    spec = block_spec("ISO 4014 - M12x60", "side")
    assert spec["name"] == "STD_ISO_4014_M12X60_SIDE"
    assert spec["view"] == "side"
    assert spec["dims"]["b"] == pytest.approx(30.0)  # 2d + 6 for l <= 125
    ents = spec["entities"]
    assert len(ents) == 10
    assert ents[0]["type"] == "polyline" and ents[0]["closed"] is True
    assert ents[0]["points"] == [
        [-7.5, -R12],
        [0.0, -R12],
        [0.0, R12],
        [-7.5, R12],
    ]
    assert ents[3] == {
        "type": "line",
        "x1": 0.0,
        "y1": 6.0,
        "x2": 60.0,
        "y2": 6.0,
        "layer": "GEOMETRY",
    }
    # ISO 6410: minor diameter at 0.8 x major, over the useful thread length.
    assert ents[6] == {
        "type": "line",
        "x1": 30.0,
        "y1": MINOR12,
        "x2": 60.0,
        "y2": MINOR12,
        "layer": "GEOMETRY",
    }
    # ISO 6410: the thread-length limit is a line to the major diameter.
    assert ents[8] == {
        "type": "line",
        "x1": 30.0,
        "y1": -6.0,
        "x2": 30.0,
        "y2": 6.0,
        "layer": "GEOMETRY",
    }
    assert ents[9]["layer"] == "CENTER"
    assert spec["bbox"] == pytest.approx((-10.5, -R12, 63.0, R12))


def test_a_fully_threaded_screw_has_no_thread_limit_line():
    spec = block_spec("ISO 4017 - M12x60", "side")
    assert len(spec["entities"]) == 9
    assert spec["dims"]["b"] == pytest.approx(60.0)
    minor = [e for e in spec["entities"] if e.get("y1") == MINOR12]
    assert minor[0]["x1"] == 0.0 and minor[0]["x2"] == 60.0


def test_hex_top_view_uses_the_three_quarter_minor_arc():
    spec = block_spec("ISO 4014 - M12x60", "top")
    ents = spec["entities"]
    assert len(ents) == 5
    assert ents[0]["type"] == "polyline" and len(ents[0]["points"]) == 6
    assert ents[1] == {"type": "circle", "cx": 0.0, "cy": 0.0, "r": 6.0, "layer": "GEOMETRY"}
    assert ents[2] == {
        "type": "arc",
        "cx": 0.0,
        "cy": 0.0,
        "r": MINOR12,
        "start_deg": 90.0,
        "end_deg": 360.0,
        "layer": "GEOMETRY",
    }
    assert ents[3]["layer"] == "CENTER" and ents[4]["layer"] == "CENTER"


def _hexagon(spec: dict) -> list[list[float]]:
    return next(e for e in spec["entities"] if e["type"] == "polyline")["points"]


def _height(points) -> float:
    return max(y for _, y in points) - min(y for _, y in points)


def _width(points) -> float:
    return max(x for x, _ in points) - min(x for x, _ in points)


def test_the_hexagon_has_vertices_at_twelve_and_six_oclock():
    """The orientation the elevation is drawn as the projection of."""
    points = _hexagon(block_spec("ISO 4014 - M12x60", "top"))
    assert [pytest.approx(v, abs=1e-9) for v in points[0]] == [
        R12 * math.sqrt(3.0) / 2.0,
        R12 / 2.0,
    ]
    assert [pytest.approx(v, abs=1e-9) for v in points[1]] == [0.0, R12]
    assert _height(points) == pytest.approx(2.0 * R12)  # across corners e
    assert _width(points) == pytest.approx(18.0)  # across flats s


@pytest.mark.parametrize(
    "designation", ["ISO 4014 - M12x60", "ISO 4017 - M12x60", "ISO 4032 - M12"]
)
def test_the_two_views_of_one_fastener_agree_on_the_silhouette_height(designation):
    """Plan and elevation of the same part must measure the same across the vertical.

    Place the two blocks in projection and a disagreement here is a wrong
    workshop drawing: the head would read 20.78 mm in the elevation and 18 mm in
    the plan. The elevation's outline polyline spans the full head height, and
    the plan's hexagon must span the same.
    """
    elevation = _height(_hexagon(block_spec(designation, "side")))
    assert elevation == pytest.approx(2.0 * R12)
    assert _height(_hexagon(block_spec(designation, "top"))) == pytest.approx(elevation)


def test_the_socket_head_views_agree_on_the_head_diameter():
    """The socket screw's silhouette is dk in both views; its hexagon is the key socket."""
    elevation = _height(_hexagon(block_spec("ISO 4762 - M12x60", "side")))
    plan = block_spec("ISO 4762 - M12x60", "top")["entities"]
    assert plan[0]["type"] == "circle" and 2.0 * plan[0]["r"] == pytest.approx(elevation)
    socket = plan[1]["points"]
    assert _height(socket) > _width(socket)  # same orientation as every other hexagon here


def test_hex_nut_side_view_shows_the_thread_as_hidden_lines():
    spec = block_spec("ISO 4032 - M12", "side")
    ents = spec["entities"]
    assert len(ents) == 8
    hidden = [e for e in ents if e["layer"] == "HIDDEN"]
    assert len(hidden) == 4
    assert {abs(e["y1"]) for e in hidden} == {6.0, MINOR12}
    assert ents[0]["points"][1] == [10.8, -R12]  # nut height m


def test_washer_views():
    side = block_spec("ISO 7089 - M12", "side")
    assert len(side["entities"]) == 3
    assert side["entities"][0]["points"] == [[0.0, 6.5], [2.5, 6.5], [2.5, 12.0], [0.0, 12.0]]
    top = block_spec("ISO 7089 - M12", "top")
    assert top["entities"][0] == {
        "type": "circle",
        "cx": 0.0,
        "cy": 0.0,
        "r": 12.0,
        "layer": "GEOMETRY",
    }
    assert top["entities"][1]["r"] == 6.5


def test_socket_head_screw_views():
    side = block_spec("ISO 4762 - M8x30", "side")
    assert len(side["entities"]) == 8
    assert side["dims"]["b"] == pytest.approx(28.0)
    assert side["entities"][0]["points"][0] == [-8.0, -6.5]  # k = 8, dk/2 = 6.5
    top = block_spec("ISO 4762 - M8x30", "top")
    assert top["entities"][0]["r"] == 6.5
    assert len(top["entities"][1]["points"]) == 6  # the hexagon socket, s = 6


def test_bearing_simplified_is_the_iso8826_1_outline_and_cross():
    spec = block_spec("ISO 15 - 6205", "simplified")
    ents = spec["entities"]
    assert len(ents) == 7
    assert ents[0]["points"] == [[-7.5, 12.5], [7.5, 12.5], [7.5, 26.0], [-7.5, 26.0]]
    assert ents[1]["points"] == [[-7.5, -12.5], [7.5, -12.5], [7.5, -26.0], [-7.5, -26.0]]
    assert ents[2] == {
        "type": "line",
        "x1": -4.5,
        "y1": 19.25,
        "x2": 4.5,
        "y2": 19.25,
        "layer": "GEOMETRY",
    }
    assert ents[3] == {
        "type": "line",
        "x1": 0.0,
        "y1": 15.2,
        "x2": 0.0,
        "y2": 23.3,
        "layer": "GEOMETRY",
    }
    assert ents[6]["layer"] == "CENTER"


def test_bearing_detailed_draws_the_element_symbol():
    spec = block_spec("ISO 15 - 6205", "detailed")
    ents = spec["entities"]
    assert len(ents) == 5
    assert ents[2] == {
        "type": "circle",
        "cx": 0.0,
        "cy": 19.25,
        "r": 4.05,
        "layer": "GEOMETRY",
    }
    assert ents[3]["cy"] == -19.25


def test_a_view_the_family_does_not_have_is_refused_and_side_falls_back():
    assert block_spec("ISO 15 - 6205", "side")["view"] == "simplified"
    with pytest.raises(ValueError, match="simplified"):
        block_spec("ISO 15 - 6205", "top")
    with pytest.raises(ValueError, match="side"):
        block_spec("ISO 4014 - M12x60", "simplified")


@pytest.mark.parametrize(
    ("designation", "view"),
    [
        ("ISO 4014 - M12x60", "side"),
        ("ISO 4014 - M12x60", "top"),
        ("ISO 4017 - M12x60", "side"),
        ("ISO 4032 - M12", "side"),
        ("ISO 4032 - M12", "top"),
        ("ISO 7089 - M12", "side"),
        ("ISO 7089 - M12", "top"),
        ("ISO 4762 - M8x30", "side"),
        ("ISO 4762 - M8x30", "top"),
        ("ISO 15 - 6205", "simplified"),
        ("ISO 15 - 6205", "detailed"),
    ],
)
def test_every_block_spec_validates_against_block_define(designation, view):
    spec = block_spec(designation, view)
    validate_entity_specs(spec["entities"])
    validate_attdef_specs(spec["attdefs"])
    assert [att["tag"] for att in spec["attdefs"]] == ["DESIG", "STD", "SIZE", "MAT"]
    assert spec["attdefs"][0]["default"] == spec["designation"]
    assert spec["attdefs"][0]["invisible"] is False
    assert all(att["invisible"] for att in spec["attdefs"][1:])
    assert spec["bbox"][0] < spec["bbox"][2] and spec["bbox"][1] < spec["bbox"][3]
    assert set(FAMILY_VIEWS[spec["family"]]) >= {spec["view"]}


@pytest.mark.asyncio
async def test_insert_defines_the_block_once_and_writes_the_payload(backend):
    from engineering.mech.stdparts import insert_std_part
    from engineering.mech.xdata import APP_ID, decode

    result = await insert_std_part(backend, "ISO 4014 - M12x60", at=(100.0, 50.0), material="8.8")
    assert result["ok"] is True
    assert result["block_name"] == "STD_ISO_4014_M12X60_SIDE"
    assert result["defined"] is True
    assert sorted(result["layers_created"]) == ["CENTER", "GEOMETRY"]
    assert result["view"] == "side"
    assert result["layer"] == "GEOMETRY"
    assert result["dims"]["s"] == 18.0

    stored = await backend.entity_get_xdata(result["handle"], APP_ID)
    payload = decode([(1001, APP_ID)] + [(1000, value) for value in stored["xdata"][APP_ID]])
    assert payload == {
        "v": 1,
        "kind": "std_part",
        "designation": "ISO 4014 - M12x60",
        "standard": "ISO 4014",
        "size": "M12x60",
        "qty": 1,
        "material": "8.8",
    }
    assert await backend.block_get_attributes(result["handle"]) == {
        "DESIG": "ISO 4014 - M12x60",
        "STD": "ISO 4014",
        "SIZE": "M12x60",
        "MAT": "8.8",
    }

    again = await insert_std_part(backend, "ISO 4014 - M12x60", at=(200.0, 50.0))
    assert again["defined"] is False and again["layers_created"] == []
    assert again["payload"]["material"] == ""


@pytest.mark.asyncio
async def test_bearing_inserts_with_its_own_view_name(backend):
    from engineering.mech.stdparts import insert_std_part

    result = await insert_std_part(backend, "6205", at=(0.0, 0.0), rotation=90.0)
    assert result["block_name"] == "STD_ISO_15_6205_SIMPLIFIED"
    assert result["view"] == "simplified"
    assert result["rotation"] == 90.0
    assert result["dims"]["B"] == 15.0


@pytest.mark.asyncio
async def test_insert_refuses_before_writing_anything(backend):
    from engineering.mech.stdparts import insert_std_part

    before = len(await backend.entity_list())
    with pytest.raises(ValueError, match="Nearest entries"):
        await insert_std_part(backend, "ISO 4014 - M13x60", at=(0.0, 0.0))
    with pytest.raises(ValueError, match="finite"):
        await insert_std_part(backend, "ISO 4014 - M12x60", at=(float("nan"), 0.0))
    with pytest.raises(ValueError, match="no attribute"):
        await insert_std_part(
            backend, "ISO 4014 - M12x60", at=(0.0, 0.0), attributes={"TORQUE": "80"}
        )
    assert len(await backend.entity_list()) == before
