"""The plan model: every refusal of spec §4, by path, and the ACADMCP_ARCH round trip.

Pure: no backend. A refusal is a ``ValueError`` whose message starts with the
path of the offending field, so a caller of ``arch_plan_from_spec`` is told
``walls[2].thickness`` and not "invalid wall".
"""

from __future__ import annotations

import pytest

from engineering.arch.model import (
    APP_ID,
    KINDS,
    Opening,
    Room,
    Stair,
    Wall,
    decode,
    encode,
    from_payload,
    opening_from_dict,
    room_from_dict,
    segment_of,
    stair_from_dict,
    to_payload,
    to_values,
    validate_openings,
    wall_from_dict,
    wall_length,
)

L_WALL = {"id": "W1", "axis": [[0, 0], [5000, 0], [5000, 4000]], "thickness": 200}


def _wall(**over) -> Wall:
    return wall_from_dict({**L_WALL, **over}, "walls[0]")


def _door(**over) -> Opening:
    base = {"id": "D1", "wall": "W1", "kind": "door", "offset": 1000, "width": 900}
    return opening_from_dict({**base, **over}, "openings[0]")


# -- walls -----------------------------------------------------------------------


def test_a_wall_reads_its_defaults_and_normalises_its_axis():
    wall = _wall()
    assert wall == Wall(
        id="W1",
        axis=((0.0, 0.0), (5000.0, 0.0), (5000.0, 4000.0)),
        thickness=200.0,
        justification="center",
        material="brick",
        closed=False,
    )
    assert wall_length(wall) == 9000.0


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (-200, "must be greater than zero"),
        (0, "must be greater than zero"),
        (float("nan"), "must be finite"),
        (float("inf"), "must be finite"),
        ("thick", "expected a number"),
        (True, "expected a number"),
    ],
)
def test_a_thickness_that_is_not_a_finite_positive_number_is_refused_by_path(value, reason):
    with pytest.raises(ValueError, match=rf"^walls\[2\]\.thickness: {reason}"):
        wall_from_dict({**L_WALL, "thickness": value}, "walls[2]")


def test_an_axis_needs_two_points():
    with pytest.raises(
        ValueError, match=r"^walls\[0\]\.axis: a wall needs at least 2 points, got 1"
    ):
        _wall(axis=[[0, 0]])


def test_a_closed_axis_needs_three_points():
    with pytest.raises(ValueError, match=r"^walls\[0\]\.axis: a closed wall needs at least 3"):
        _wall(axis=[[0, 0], [10, 0]], closed=True)


def test_a_zero_length_segment_is_refused_at_the_repeated_point():
    with pytest.raises(ValueError, match=r"^walls\[0\]\.axis\[2\]: repeats axis\[1\]"):
        _wall(axis=[[0, 0], [5000, 0], [5000, 0], [5000, 4000]])


def test_a_closed_axis_that_repeats_its_first_point_is_refused():
    with pytest.raises(ValueError, match=r"^walls\[0\]\.axis\[3\]: repeats axis\[0\]"):
        _wall(axis=[[0, 0], [10, 0], [10, 10], [0, 0]], closed=True)


def test_an_arc_segment_is_refused_by_name_not_approximated():
    with pytest.raises(ValueError, match=r"^walls\[0\]\.axis\[1\]: .*arc segment is refused"):
        _wall(axis=[[0, 0], [5000, 0, 0.4142], [5000, 4000]])
    with pytest.raises(ValueError, match=r"^walls\[0\]\.axis\[1\]: an arc segment is refused"):
        _wall(axis=[[0, 0], {"center": [2500, 0], "radius": 2500}, [5000, 4000]])


def test_an_unknown_material_is_refused_with_the_list():
    with pytest.raises(
        ValueError, match=r"^walls\[0\]\.material: unknown wall material 'adobe'"
    ) as err:
        _wall(material="adobe")
    assert "brick, aac, concrete, reinforced_concrete" in str(err.value)


def test_a_material_is_read_with_the_spelling_wall_hatch_accepts():
    """The model must not refuse a name the hatch table resolves (spaces, hyphens, case)."""
    assert _wall(material="Reinforced Concrete").material == "reinforced_concrete"
    assert _wall(material="gypsum-board").material == "gypsum_board"


def test_an_unknown_justification_and_an_unknown_field_are_refused():
    with pytest.raises(ValueError, match=r"^walls\[0\]\.justification: 'middle' is not one of"):
        _wall(justification="middle")
    with pytest.raises(ValueError, match=r"^walls\[0\]: unknown field\(s\) thicknes"):
        wall_from_dict({**L_WALL, "thicknes": 200}, "walls[0]")


# -- openings --------------------------------------------------------------------


def test_an_opening_reads_its_defaults():
    assert _door() == Opening(
        id="D1", wall="W1", kind="door", offset=1000.0, width=900.0, swing="in", hand="left"
    )


def test_an_opening_needs_a_positive_width_and_a_non_negative_offset():
    with pytest.raises(ValueError, match=r"^openings\[0\]\.width: must be greater than zero"):
        _door(width=0)
    with pytest.raises(ValueError, match=r"^openings\[0\]\.offset: must not be negative"):
        _door(offset=-1)
    assert _door(offset=0).offset == 0.0


def test_an_opening_on_a_wall_that_is_not_in_the_plan_is_refused():
    with pytest.raises(ValueError, match=r"opening D1: wall 'W9' is not in the plan; walls are W1"):
        validate_openings([_wall()], [_door(wall="W9")])


def test_an_opening_that_runs_past_its_wall_is_refused():
    with pytest.raises(
        ValueError, match=r"opening D1: runs past the end of wall W1 .* 9500 > axis length 9000"
    ):
        validate_openings([_wall()], [_door(offset=8600)])


def test_an_opening_that_straddles_an_axis_vertex_is_refused():
    # The axis turns at 5000 mm; 4500-5400 has a jamb on each side of the corner.
    with pytest.raises(
        ValueError, match=r"opening D1: straddles the axis vertex of wall W1 at 5000"
    ):
        validate_openings([_wall()], [_door(offset=4500)])


def test_an_opening_may_end_exactly_at_a_vertex():
    validate_openings([_wall()], [_door(offset=4100, width=900)])


def test_two_overlapping_openings_are_refused_naming_both_ids():
    door = _door()
    window = opening_from_dict(
        {"id": "W2", "wall": "W1", "kind": "window", "offset": 1500, "width": 1200}, "openings[1]"
    )
    with pytest.raises(
        ValueError, match=r"openings D1 and W2 overlap on wall W1: 1000-1900 and 1500-2700"
    ):
        validate_openings([_wall()], [door, window])


def test_two_openings_that_only_touch_are_accepted():
    window = opening_from_dict(
        {"id": "W2", "wall": "W1", "kind": "window", "offset": 1900, "width": 1200}, "openings[1]"
    )
    validate_openings([_wall()], [_door(), window])


def test_duplicate_ids_are_refused():
    with pytest.raises(ValueError, match=r"walls: two walls share the id 'W1'"):
        validate_openings([_wall(), _wall()], [])
    with pytest.raises(ValueError, match=r"openings: two openings share the id 'D1'"):
        validate_openings([_wall()], [_door(), _door(offset=3000)])


def test_segment_of_finds_the_segment_and_the_distance_along_it():
    wall = _wall()
    assert segment_of(wall, 0.0) == (0, 0.0)
    assert segment_of(wall, 1000.0) == (0, 1000.0)
    assert segment_of(wall, 5000.0) == (1, 0.0)  # a vertex belongs to the segment it starts
    assert segment_of(wall, 6500.0) == (1, 1500.0)
    assert segment_of(wall, 9000.0) == (1, 4000.0)  # the very end belongs to the last one
    with pytest.raises(ValueError, match=r"offset 9001.0 is outside wall W1 \(axis length 9000\)"):
        segment_of(wall, 9001.0)


def test_a_closed_wall_counts_its_closing_segment():
    ring = wall_from_dict(
        {
            "id": "R",
            "axis": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]],
            "thickness": 200,
            "closed": True,
        },
        "walls[0]",
    )
    assert wall_length(ring) == 14000.0
    assert segment_of(ring, 12000.0) == (3, 1000.0)


# -- stairs and rooms ------------------------------------------------------------


STAIR = {
    "id": "S1",
    "start": [1000, 500],
    "direction_deg": 90,
    "width": 1000,
    "risers": 16,
    "riser_height": 175,
    "going": 280,
}


def test_a_stair_reads_its_defaults():
    assert stair_from_dict(STAIR, "stairs[0]") == Stair(
        id="S1",
        start=(1000.0, 500.0),
        direction_deg=90.0,
        width=1000.0,
        risers=16,
        riser_height=175.0,
        going=280.0,
        kind="straight",
        turn="left",
    )


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"risers": 1}, r"^stairs\[0\]\.risers: a stair needs at least 2 risers, got 1"),
        ({"risers": 15.5}, r"^stairs\[0\]\.risers: expected a whole number of risers"),
        ({"going": -280}, r"^stairs\[0\]\.going: must be greater than zero"),
        ({"riser_height": float("nan")}, r"^stairs\[0\]\.riser_height: must be finite"),
        ({"kind": "spiral"}, r"^stairs\[0\]\.kind: 'spiral' is not one of straight, l, u"),
    ],
)
def test_a_stair_refuses_by_path(over, message):
    with pytest.raises(ValueError, match=message):
        stair_from_dict({**STAIR, **over}, "stairs[0]")


def test_a_room_refuses_a_typed_area():
    with pytest.raises(ValueError, match=r"^rooms\[0\]\.area: an area is measured .* never typed"):
        room_from_dict(
            {"id": "R1", "name": "LIVING", "at": [2000, 2000], "area": 20.0e6}, "rooms[0]"
        )


def test_a_room_reads_a_number_given_as_an_integer():
    room = room_from_dict({"id": "R1", "name": "LIVING", "number": 101, "at": [2000, 2000]})
    assert room == Room(id="R1", name="LIVING", number="101", at=(2000.0, 2000.0), area=0.0)


# -- the ACADMCP_ARCH payload ----------------------------------------------------


def test_the_codec_is_the_mechanical_one_under_our_own_application_id():
    assert APP_ID == "ACADMCP_ARCH"
    assert KINDS == ("wall", "opening", "stair", "room")
    rows = encode(to_payload(_wall()))
    assert rows[0] == (1001, "ACADMCP_ARCH")
    assert all(code == 1000 and len(value) <= 255 for code, value in rows[1:])


@pytest.mark.parametrize(
    "obj",
    [
        _wall(material="aac", justification="left"),
        _door(tag="D1", height=2100),
        stair_from_dict(STAIR),
        Room(id="R1", name="MUTFAK", number="2", at=(1.0, 2.0), area=12.5e6),
    ],
)
def test_every_plan_object_survives_the_drawing(obj):
    payload = decode(to_values(to_payload(obj)))
    assert payload["kind"] in KINDS and payload["v"] == 1
    assert from_payload(payload) == obj


def test_an_element_kind_travels_beside_the_payload_kind():
    payload = to_payload(_door())
    assert payload["kind"] == "opening" and payload["opening_kind"] == "door"
    assert to_payload(stair_from_dict(STAIR))["stair_kind"] == "straight"


def test_a_mechanical_payload_is_not_ours():
    from engineering.mech.xdata import to_values as mech_values

    part = {"v": 1, "kind": "part", "id": "P1", "view": "front", "part": {}}
    with pytest.raises(ValueError, match=r"ACADMCP_ARCH: unknown payload kind 'part'"):
        decode(mech_values(part))
    with pytest.raises(ValueError, match=r"belongs to application 'ACADMCP_MECH'"):
        decode([(1001, "ACADMCP_MECH"), (1000, "{}")])
