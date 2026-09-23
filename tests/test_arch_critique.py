"""Three architectural focuses, each with a positive and a negative fixture.

All three are silent on a drawing with no ACADMCP_ARCH record, so
`focus=None` stays cheap - and honest - on a mechanical sheet, a P&ID or a
foreign plan of plain lines.

A fixture writes the records onto entities of the layers they live on in a
real plan (a wall record on a wall line, an opening record on a door line, a
room record on its label), then asks the index. The geometry of the gaps and
clashes is computed by hand in each test.
"""

from __future__ import annotations

import pytest

from engineering.arch.critique import (
    ARCH_FOCUSES,
    GAP_MM,
    body_distance,
    build_index,
    has_arch_content,
    issues_for,
    opening_clashes,
    point_in_loop,
    wall_gaps,
)
from engineering.arch.layers import ARCH_ROLE_LAYER
from engineering.arch.model import (
    APP_ID,
    Room,
    opening_from_dict,
    to_payload,
    to_values,
    wall_from_dict,
)
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES

WALL_LAYER = ARCH_ROLE_LAYER["wall"]


def _wall(wall_id, axis, thickness=200.0, **kw):
    return wall_from_dict({"id": wall_id, "axis": axis, "thickness": thickness, **kw})


def _opening(opening_id, wall, offset, width, kind="door"):
    return opening_from_dict(
        {"id": opening_id, "wall": wall, "kind": kind, "offset": offset, "width": width}
    )


async def _record(backend, obj, layer, at=(0.0, 0.0)):
    """A short line on ``layer`` carrying ``obj``'s ACADMCP_ARCH record."""
    ent = await backend.entity_create_line(at[0], at[1], at[0] + 10.0, at[1], layer=layer)
    await backend.entity_set_xdata(ent.handle, APP_ID, to_values(to_payload(obj)))
    return ent.handle


async def _square(backend, x0, y0, x1, y1, layer=WALL_LAYER):
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    handles = []
    for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
        ent = await backend.entity_create_line(a[0], a[1], b[0], b[1], layer=layer)
        handles.append(ent.handle)
    return handles


# -- the closed enum and the silence rule ---------------------------------------


def test_the_three_focuses_are_the_spec_ones_and_are_in_the_closed_enum():
    assert ARCH_FOCUSES == ("arch_room_unlabelled", "arch_wall_gap", "arch_opening_clash")
    for focus in ARCH_FOCUSES:
        assert focus in ALL_CRITIQUE_FOCUSES
    assert GAP_MM == 50.0


@pytest.mark.asyncio
async def test_every_focus_is_silent_on_a_drawing_with_no_arch_record(backend):
    """A closed room of plain lines on a WALL layer, nothing labelled, two
    wall-like lines 30 mm apart - every trigger present, and not one issue,
    because nothing on the drawing claims to be our plan."""
    await _square(backend, 0.0, 0.0, 4000.0, 4000.0, layer="WALLS")
    await backend.entity_create_line(5000.0, 0.0, 9000.0, 0.0, layer="WALLS")
    await backend.entity_create_line(9030.0, 0.0, 12000.0, 0.0, layer="WALLS")
    index = await build_index(backend)
    assert has_arch_content(index) is False
    for focus in ARCH_FOCUSES:
        assert issues_for(focus, index) == []


# -- arch_wall_gap -----------------------------------------------------------------


def test_body_distance_is_zero_inside_and_on_the_edge():
    # a 200 mm centre wall from (0, 0) to (4000, 0): body x 0..4000, y -100..100
    band = (-100.0, 100.0)
    assert body_distance((2000.0, 50.0), (0.0, 0.0), (4000.0, 0.0), band) == 0.0
    assert body_distance((4000.0, 100.0), (0.0, 0.0), (4000.0, 0.0), band) == 0.0
    assert body_distance((4040.0, 0.0), (0.0, 0.0), (4000.0, 0.0), band) == pytest.approx(40.0)
    # 30 past the end and 40 past the face: the corner distance, 50
    assert body_distance((4030.0, 140.0), (0.0, 0.0), (4000.0, 0.0), band) == pytest.approx(50.0)


def test_two_collinear_walls_40_mm_apart_are_one_gap():
    walls = [_wall("W1", [[0, 0], [4000, 0]]), _wall("W2", [[4040, 0], [8000, 0]])]
    (row,) = wall_gaps(walls)
    assert row["walls"] == ("W1", "W2")
    assert row["gap_mm"] == pytest.approx(40.0, abs=1e-9)


def test_an_l_corner_and_a_t_junction_are_joined():
    l_corner = [_wall("W1", [[0, 0], [4000, 0]]), _wall("W2", [[4000, 0], [4000, 3000]])]
    # the stem ends on the crossing wall's axis, inside its body
    t_junction = [_wall("W1", [[0, 0], [6000, 0]]), _wall("W2", [[3000, 0], [3000, 3000]], 100)]
    assert wall_gaps(l_corner) == []
    assert wall_gaps(t_junction) == []


def test_a_stem_stopping_30_mm_short_of_the_crossing_face_is_a_gap():
    # crossing wall body y -100..100; the stem starts at y = 130
    walls = [_wall("W1", [[0, 0], [6000, 0]]), _wall("W2", [[3000, 130], [3000, 3000]], 100)]
    (row,) = wall_gaps(walls)
    assert row["end"] == "W2.start" and row["other"] == "W1"
    assert row["gap_mm"] == pytest.approx(30.0, abs=1e-9)


def test_a_wall_further_than_50_mm_away_is_not_a_gap():
    walls = [_wall("W1", [[0, 0], [4000, 0]]), _wall("W2", [[4051, 0], [8000, 0]])]
    assert wall_gaps(walls) == []


def test_a_wall_that_nearly_closes_on_itself_leaks():
    # the last point stops 40 mm above the first segment's face (y = 100)
    ring = _wall("W1", [[0, 0], [4000, 0], [4000, 3000], [0, 3000], [0, 140]])
    (row,) = wall_gaps([ring])
    assert row["walls"] == ("W1", "W1") and row["end"] == "W1.end"
    assert row["gap_mm"] == pytest.approx(40.0, abs=1e-9)


@pytest.mark.asyncio
async def test_wall_gap_fires_on_the_drawing(backend):
    await _record(backend, _wall("W1", [[0, 0], [4000, 0]]), WALL_LAYER)
    await _record(backend, _wall("W2", [[4040, 0], [8000, 0]]), WALL_LAYER, at=(4040.0, 0.0))
    issues = issues_for("arch_wall_gap", await build_index(backend))
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].detail["gap_mm"] == pytest.approx(40.0)
    assert len(issues[0].handles) == 2


@pytest.mark.asyncio
async def test_wall_gap_is_quiet_on_a_joined_pair(backend):
    await _record(backend, _wall("W1", [[0, 0], [4000, 0]]), WALL_LAYER)
    await _record(backend, _wall("W2", [[4000, 0], [4000, 3000]]), WALL_LAYER, at=(4000.0, 0.0))
    assert issues_for("arch_wall_gap", await build_index(backend)) == []


# -- arch_opening_clash ------------------------------------------------------------


def test_overlap_and_overrun_are_both_reported():
    wall = _wall("W1", [[0, 0], [5000, 0]])
    rows = opening_clashes(
        [wall],
        [
            _opening("D1", "W1", 1000, 900),
            _opening("W2", "W1", 1500, 1200, "window"),
            _opening("D3", "W1", 4500, 900),
        ],
    )
    assert [row["openings"] for row in rows] == [("D1", "W2"), ("D3",)]
    assert "overlap (1000-1900 and 1500-2700)" in rows[0]["reason"]
    assert "runs 400 mm past the end" in rows[1]["reason"]


def test_an_opening_across_an_axis_vertex_and_one_on_a_missing_wall():
    wall = _wall("W1", [[0, 0], [5000, 0], [5000, 4000]])
    rows = opening_clashes([wall], [_opening("D1", "W1", 4500, 900), _opening("D2", "W9", 0, 900)])
    assert "across the axis vertex at 5000" in rows[0]["reason"]
    assert "'W9' has no record" in rows[1]["reason"]


@pytest.mark.asyncio
async def test_opening_clash_fires_on_the_drawing(backend):
    await _record(backend, _wall("W1", [[0, 0], [5000, 0]]), WALL_LAYER)
    await _record(backend, _opening("D1", "W1", 1000, 900), ARCH_ROLE_LAYER["door"])
    await _record(backend, _opening("D2", "W1", 1500, 900), ARCH_ROLE_LAYER["door"], (1500.0, 0.0))
    (issue,) = issues_for("arch_opening_clash", await build_index(backend))
    assert issue.severity == "error"
    assert issue.detail == {"openings": ["D1", "D2"], "wall": "W1"}
    assert len(issue.handles) == 2


@pytest.mark.asyncio
async def test_opening_clash_is_quiet_on_two_openings_that_only_touch(backend):
    await _record(backend, _wall("W1", [[0, 0], [5000, 0]]), WALL_LAYER)
    await _record(backend, _opening("D1", "W1", 1000, 900), ARCH_ROLE_LAYER["door"])
    await _record(backend, _opening("D2", "W1", 1900, 900), ARCH_ROLE_LAYER["door"], (1900.0, 0.0))
    assert issues_for("arch_opening_clash", await build_index(backend)) == []


# -- arch_room_unlabelled --------------------------------------------------------------


def test_point_in_loop():
    loop = [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]
    assert point_in_loop((2000.0, 1500.0), loop) is True
    assert point_in_loop((4000.0, 1500.0), loop) is True  # on an edge
    assert point_in_loop((4100.0, 1500.0), loop) is False


async def _ring(backend):
    """A 200 mm ring on axis (0,0)-(4000,4000): its faces drawn as lines on the
    wall layer, its record carried by the first outer face line - where a real
    plan carries it. Interior 3800 x 3800."""
    outer = await _square(backend, -100.0, -100.0, 4100.0, 4100.0)
    await _square(backend, 100.0, 100.0, 3900.0, 3900.0)
    ring = _wall("W1", [[0, 0], [4000, 0], [4000, 4000], [0, 4000]], closed=True)
    await backend.entity_set_xdata(outer[0], APP_ID, to_values(to_payload(ring)))


@pytest.mark.asyncio
async def test_room_unlabelled_fires_on_a_closed_room_with_no_label(backend):
    await _ring(backend)
    index = await build_index(backend)
    (issue,) = issues_for("arch_room_unlabelled", index)
    assert issue.severity == "warning"
    assert issue.detail["area_mm2"] == pytest.approx(3800.0 * 3800.0)
    assert "14.44 m²" in issue.message


@pytest.mark.asyncio
async def test_room_unlabelled_is_quiet_once_the_room_carries_a_label(backend):
    await _ring(backend)
    room = Room(id="R1", name="ROOM", number="01", at=(2000.0, 2000.0), area=3800.0 * 3800.0)
    await _record(backend, room, ARCH_ROLE_LAYER["room"], at=(2000.0, 2000.0))
    assert issues_for("arch_room_unlabelled", await build_index(backend)) == []


# -- the dispatch --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dispatch_entries_share_one_index_per_run(backend):
    from engineering.arch.critique import ARCH_DISPATCH

    await _record(backend, _wall("W1", [[0, 0], [4000, 0]]), WALL_LAYER)
    shared: dict = {}
    for focus in ARCH_FOCUSES:
        check = ARCH_DISPATCH[focus]
        assert check.needs_shared is True
        await check(backend, shared)
    assert list(shared) == ["arch_index"]


@pytest.mark.asyncio
async def test_run_critique_reaches_the_arch_focuses(backend):
    from engineering.critique import run_critique

    await _record(backend, _wall("W1", [[0, 0], [4000, 0]]), WALL_LAYER)
    await _record(backend, _wall("W2", [[4040, 0], [8000, 0]]), WALL_LAYER, at=(4040.0, 0.0))
    (issue,) = await run_critique(backend, ["arch_wall_gap"])
    assert issue.focus == "arch_wall_gap" and issue.severity == "error"
    assert "stops 40 mm short of wall" in issue.message
