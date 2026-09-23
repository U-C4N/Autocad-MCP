"""Room labels with a measured area, and the foreign-plan reader.

The pure half asserts emitted primitives and hand-computed coordinates; the
async half runs against the headless backend (the `backend` fixture in
tests/conftest.py). The wall and opening records a drawn plan carries are
written with `model.to_values` directly - the wall engine that writes them in
production is Task 4's, on another branch, and the record shape is pinned by
the skeleton.
"""

from __future__ import annotations

import re

import pytest

from engineering.arch.layers import ARCH_ROLE_LAYER
from engineering.arch.model import APP_ID, Opening, Room, Wall, decode, to_payload, to_values
from engineering.arch.rooms import (
    CONFIDENCE_FOREIGN,
    CONFIDENCE_OURS,
    detect_rooms,
    label_room,
    opening_closures,
    read_records,
    room_label_prims,
    rooms_detect,
)
from engineering.mech.primitives import Text

WALL = ARCH_ROLE_LAYER["wall"]
ROOM = ARCH_ROLE_LAYER["room"]
EPS = 1e-9


def rect(x0, y0, x1, y1):
    return [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]


def two_rooms(tag=None):
    """Two 4000 x 5000 rooms between 200 mm walls (see tests/test_arch_faces.py)."""
    segments = [
        *rect(0.0, 0.0, 8600.0, 5400.0),
        *rect(200.0, 200.0, 8400.0, 5200.0),
        ((4200.0, 200.0), (4200.0, 5200.0)),
        ((4400.0, 200.0), (4400.0, 5200.0)),
    ]
    return segments if tag is None else [(p1, p2, tag) for p1, p2 in segments]


async def draw_lines(backend, segments, layer):
    for (x1, y1), (x2, y2) in segments:
        await backend.entity_create_line(x1, y1, x2, y2, layer=layer)


async def write_record(backend, obj, layer):
    """A record the way a drawn plan carries it: XDATA on an entity of its layer."""
    carrier = await backend.entity_create_line(0.0, -1000.0, 1.0, -1000.0, layer=layer)
    await backend.entity_set_xdata(carrier.handle, APP_ID, to_values(to_payload(obj)))
    return carrier.handle


async def texts(backend, layer):
    return {
        e.properties.get("text"): e
        for e in await backend.entity_list(type_filter="TEXT", layer_filter=layer, limit=1000)
    }


# ── pure: the label ─────────────────────────────────────────────────────────


def test_label_prims_stack_name_number_area_down_from_the_point_at_plot_scale():
    room = Room(id="R1", name="LIVING", number="01", at=(2000.0, 2500.0), area=20_000_000.0)
    prims = room_label_prims(room, lang="en", scale=50)
    # 1:50 -> name 3.5 x 50 = 175, number and area 2.5 x 50 = 125;
    # baselines 2500, 2500 - 1.6 x 125 = 2300, 2300 - 1.6 x 125 = 2100
    assert prims == (
        Text(at=(2000.0, 2500.0), text="LIVING", height=175.0, rotation=0.0, role="room"),
        Text(at=(2000.0, 2300.0), text="01", height=125.0, rotation=0.0, role="room"),
        Text(at=(2000.0, 2100.0), text="20.00 m²", height=125.0, rotation=0.0, role="room"),
    )


def test_label_without_a_number_puts_the_area_on_the_second_line_and_tr_uses_a_comma():
    room = Room(id="R2", name="SALON", number=None, at=(0.0, 0.0), area=24_500_000.0)
    prims = room_label_prims(room, lang="tr", scale=100)
    # 1:100 -> name 350, area 250 at 0 - 1.6 x 250 = -400
    assert [(p.at, p.text, p.height) for p in prims] == [
        ((0.0, 0.0), "SALON", 350.0),
        ((0.0, -400.0), "24,50 m²", 250.0),
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"lang": "de"}, "lang: 'de' is not a label language; languages are en, tr"),
        ({"scale": 0}, "scale: must be a finite number greater than zero, got 0"),
    ],
)
def test_label_prims_refuse_an_unknown_language_or_scale(kwargs, message):
    room = Room(id="R1", name="A", number=None, at=(0.0, 0.0), area=1.0)
    with pytest.raises(ValueError, match=re.escape(message)):
        room_label_prims(room, **kwargs)


# ── pure: openings close their gap ──────────────────────────────────────────


def test_an_opening_is_closed_along_both_wall_faces():
    # the shared wall's axis runs up x=4300 from y=100 to y=5300, 200 thick, centred;
    # a 900 door 1100 from the axis start spans y=1200..2100.
    wall = Wall(id="w5", axis=((4300.0, 100.0), (4300.0, 5300.0)), thickness=200.0)
    door = Opening(id="d1", wall="w5", kind="door", offset=1100.0, width=900.0)
    closures, omitted = opening_closures([wall], [door])
    # u = (0, 1), left normal n = (-1, 0); face offsets -100 and +100
    assert closures == (
        ((4400.0, 1200.0), (4400.0, 2100.0)),
        ((4200.0, 1200.0), (4200.0, 2100.0)),
    )
    assert omitted == ()


def test_a_left_justified_wall_closes_its_faces_on_the_axis_and_one_thickness_left():
    wall = Wall(id="w1", axis=((0.0, 0.0), (5000.0, 0.0)), thickness=200.0, justification="left")
    window = Opening(id="p1", wall="w1", kind="window", offset=1000.0, width=1200.0)
    closures, _ = opening_closures([wall], [window])
    # u = (1, 0), n = (0, 1): faces at 0 and +200
    assert closures == (((1000.0, 0.0), (2200.0, 0.0)), ((1000.0, 200.0), (2200.0, 200.0)))


def test_an_opening_whose_wall_has_no_record_is_omitted_not_guessed():
    door = Opening(id="d9", wall="nowhere", kind="door", offset=0.0, width=900.0)
    closures, omitted = opening_closures([], [door])
    assert closures == ()
    assert omitted == (
        {
            "element": "d9",
            "reason": "host wall 'nowhere' has no record on this drawing; its gap is left open",
        },
    )


def test_a_stale_opening_past_the_end_of_its_wall_is_omitted_not_raised():
    # a record left behind after its wall was shortened: 9000 on a 5000 axis.
    # The reader is read-only and must still answer for every other room.
    wall = Wall(id="w1", axis=((0.0, 0.0), (5000.0, 0.0)), thickness=200.0)
    stale = Opening(id="d4", wall="w1", kind="door", offset=9000.0, width=900.0)
    closures, omitted = opening_closures([wall], [stale])
    assert closures == ()
    assert omitted == (
        {
            "element": "d4",
            "reason": "offset 9000.0 is outside wall w1 (axis length 5000); its gap is left open",
        },
    )


# ── pure: detect_rooms ──────────────────────────────────────────────────────


def test_detect_rooms_keeps_the_rooms_and_drops_the_wall_bodies():
    rooms = detect_rooms(two_rooms(WALL))
    # the exterior ring (mean width 2 x 5 440 000 / 54 400 = 200) and the shared
    # wall body (2 x 1 000 000 / 10 400 = 192) are wall bodies, not rooms
    assert [r["area"] for r in rooms] == [20_000_000.0, 20_000_000.0]
    assert [r["centroid"] for r in rooms] == [(2200.0, 2700.0), (6400.0, 2700.0)]
    assert {r["confidence"] for r in rooms} == {CONFIDENCE_OURS}


def test_one_foreign_edge_makes_the_face_foreign():
    segments = two_rooms(WALL)
    # the inner outline's bottom edge from (200,200) to (8400,200) becomes a plain line
    segments[4] = (segments[4][0], segments[4][1], "0")
    rooms = detect_rooms(segments)
    assert [r["confidence"] for r in rooms] == [CONFIDENCE_FOREIGN, CONFIDENCE_FOREIGN]


def test_untagged_lines_are_foreign_and_min_area_filters():
    rooms = detect_rooms(two_rooms())
    assert [r["confidence"] for r in rooms] == [0.6, 0.6]
    assert detect_rooms(two_rooms(), min_area=20_000_001.0) == ()


# ── async: label_room ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_4000_by_5000_room_between_200_mm_walls_is_labelled_20_m2(backend):
    await draw_lines(
        backend, [*rect(-200.0, -200.0, 4200.0, 5200.0), *rect(0.0, 0.0, 4000.0, 5000.0)], WALL
    )
    result = await label_room(
        backend, {"name": "LIVING", "number": "01", "at": [2000.0, 2500.0]}, lang="en", scale=50
    )
    assert result["room"]["area"] == pytest.approx(20_000_000.0, abs=EPS)
    assert result["room"]["area_text"] == "20.00 m²"
    assert result["room"]["id"] == "R1"
    assert result["loop"] == [[0.0, 0.0], [4000.0, 0.0], [4000.0, 5000.0], [0.0, 5000.0]]
    labels = await texts(backend, ROOM)
    assert set(labels) == {"LIVING", "01", "20.00 m²"}
    raw = await backend.entity_get_xdata(result["handle"], APP_ID)
    payload = decode(raw["xdata"][APP_ID])
    assert payload["kind"] == "room"
    assert payload["id"] == "R1"
    assert payload["area"] == pytest.approx(20_000_000.0, abs=EPS)


@pytest.mark.asyncio
async def test_turkish_labels_use_a_decimal_comma(backend):
    await draw_lines(
        backend, [*rect(-200.0, -200.0, 4200.0, 5200.0), *rect(0.0, 0.0, 4000.0, 5000.0)], WALL
    )
    await label_room(backend, {"name": "SALON", "at": [2000.0, 2500.0]}, lang="tr")
    assert "20,00 m²" in await texts(backend, ROOM)


@pytest.mark.asyncio
async def test_a_door_in_the_shared_wall_does_not_merge_the_two_rooms(backend):
    door_gap = [
        *rect(0.0, 0.0, 8600.0, 5400.0),
        *rect(200.0, 200.0, 8400.0, 5200.0),
        # the shared wall's faces, interrupted from y=1200 to y=2100, and its two jambs
        ((4200.0, 200.0), (4200.0, 1200.0)),
        ((4200.0, 2100.0), (4200.0, 5200.0)),
        ((4400.0, 200.0), (4400.0, 1200.0)),
        ((4400.0, 2100.0), (4400.0, 5200.0)),
        ((4200.0, 1200.0), (4400.0, 1200.0)),
        ((4200.0, 2100.0), (4400.0, 2100.0)),
    ]
    await draw_lines(backend, door_gap, WALL)
    await write_record(
        backend, Wall(id="w5", axis=((4300.0, 100.0), (4300.0, 5300.0)), thickness=200.0), WALL
    )
    await write_record(
        backend,
        Opening(id="d1", wall="w5", kind="door", offset=1100.0, width=900.0),
        ARCH_ROLE_LAYER["door"],
    )
    result = await label_room(backend, {"name": "A", "at": [2200.0, 2700.0]})
    # without the closures room A would read 20 000 000 + 20 000 000 + 900 x 200
    assert result["room"]["area"] == pytest.approx(20_000_000.0, abs=EPS)
    assert result["closures"] == 2


@pytest.mark.asyncio
async def test_label_room_refusals_leave_the_drawing_untouched(backend):
    await draw_lines(backend, rect(0.0, 0.0, 4000.0, 5000.0), WALL)
    before = await backend.entity_count()
    with pytest.raises(ValueError, match=re.escape("room.area: a room's area is measured")):
        await label_room(backend, {"name": "A", "at": [100.0, 100.0], "area": 5.0})
    with pytest.raises(ValueError, match=re.escape("room.at: (9000, 9000) lies in no closed face")):
        await label_room(backend, {"name": "A", "at": [9000.0, 9000.0]})
    with pytest.raises(ValueError, match=re.escape("lang: 'fr' is not a label language")):
        await label_room(backend, {"name": "A", "at": [100.0, 100.0]}, lang="fr")
    assert await backend.entity_count() == before
    await label_room(backend, {"id": "R7", "name": "A", "at": [100.0, 100.0]})
    with pytest.raises(ValueError, match=re.escape("room.id: 'R7' is already labelled")):
        await label_room(backend, {"id": "R7", "name": "B", "at": [200.0, 200.0]})


@pytest.mark.asyncio
async def test_a_label_point_inside_a_wall_body_is_refused(backend):
    await draw_lines(
        backend, [*rect(-200.0, -200.0, 4200.0, 5200.0), *rect(0.0, 0.0, 4000.0, 5000.0)], WALL
    )
    with pytest.raises(
        ValueError, match=re.escape("room.at: (-100, 2500) lies inside a wall body")
    ):
        await label_room(backend, {"name": "A", "at": [-100.0, 2500.0]})


# ── async: rooms_detect ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_foreign_plan_of_plain_lines_yields_the_same_areas_and_is_not_modified(backend):
    await backend.entity_create_polyline(
        [[0.0, 0.0], [8600.0, 0.0], [8600.0, 5400.0], [0.0, 5400.0]], True, layer="DUVAR"
    )
    await draw_lines(backend, two_rooms()[4:], "DUVAR")
    await draw_lines(backend, [((-500.0, -500.0), (9000.0, -500.0))], "0")  # not read
    before = await backend.entity_count()
    result = await rooms_detect(backend)
    assert result["layers"] == ["DUVAR"]
    assert [r["area"] for r in result["rooms"]] == [20_000_000.0, 20_000_000.0]
    assert [r["centroid"] for r in result["rooms"]] == [[2200.0, 2700.0], [6400.0, 2700.0]]
    assert result["confidence_min"] == CONFIDENCE_FOREIGN
    assert result["modified"] is False
    assert await backend.entity_count() == before


@pytest.mark.asyncio
async def test_our_own_walls_read_at_full_confidence_with_their_labels(backend):
    await draw_lines(backend, two_rooms(), WALL)
    await label_room(backend, {"name": "A", "number": "01", "at": [2200.0, 2700.0]})
    result = await rooms_detect(backend)
    assert result["layers"] == [WALL]
    assert [r["confidence"] for r in result["rooms"]] == [1.0, 1.0]
    first, second = result["rooms"]
    assert first["label"] == {"id": "R1", "name": "A", "number": "01", "area": 20_000_000.0}
    assert second["label"] is None


@pytest.mark.asyncio
async def test_the_record_reader_pages_the_drawing_once_not_once_per_arch_layer(
    backend, monkeypatch
):
    # a live seat pays one cross-process call per entity per scan; twelve arch
    # layers must not mean twelve scans of the whole space
    await write_record(
        backend, Wall(id="w1", axis=((0.0, 0.0), (5000.0, 0.0)), thickness=200.0), WALL
    )
    await write_record(
        backend,
        Opening(id="d1", wall="w1", kind="door", offset=1000.0, width=900.0),
        ARCH_ROLE_LAYER["door"],
    )
    await backend.entity_create_line(0.0, 0.0, 1.0, 1.0, layer="0")  # not an arch layer
    calls = []
    original = backend.entity_list

    async def counting(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(backend, "entity_list", counting)
    records = await read_records(backend)
    assert len(calls) == 1
    assert [w.id for w in records["walls"]] == ["w1"]
    assert [o.id for o in records["openings"]] == ["d1"]
    assert records["unreadable"] == 0


@pytest.mark.asyncio
async def test_rooms_detect_refuses_a_layer_that_is_not_there(backend):
    await draw_lines(backend, rect(0.0, 0.0, 4000.0, 5000.0), "WALLS")
    with pytest.raises(ValueError, match=re.escape("layers: NOPE not in this drawing")):
        await rooms_detect(backend, layers=["NOPE"])


@pytest.mark.asyncio
async def test_an_arc_on_a_wall_layer_is_reported_skipped_never_approximated(backend):
    await draw_lines(backend, rect(0.0, 0.0, 4000.0, 5000.0), "WALLS")
    arc = await backend.entity_create_arc(2000.0, 5000.0, 500.0, 0.0, 180.0, layer="WALLS")
    result = await rooms_detect(backend)
    assert [r["area"] for r in result["rooms"]] == [20_000_000.0]
    assert [s["handle"] for s in result["skipped"]] == [arc.handle]
