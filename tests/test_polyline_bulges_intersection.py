"""Polylines with arc segments, and point_intersection on ARCs (headless engine).

Both found missing by the practice exams: an outline with rounded ends had to be drawn as separate
LINE / ARC entities and chained by ``boundary_from_entities``; and ``point_intersection`` refused
every ARC, so a tangent corner could not be located from the drawing itself.
"""

from __future__ import annotations

import math

import pytest

from backends.contracts.entity_creation import resolve_polyline_bulges

pytestmark = pytest.mark.asyncio


async def test_slot_is_one_polyline_with_exact_area(backend):
    # 40 x 10 slot: two straight sides, two semicircular ends -> 40*10 + pi*5^2 - (the rectangle already counts 10*10 of ends)
    pl = await backend.entity_create_polyline(
        [[0, 0], [40, 0], [40, 10], [0, 10]], closed=True, bulges=[0, 1, 0, 1]
    )
    assert pl.properties["bulges"] == pytest.approx([0, 1, 0, 1])
    m = await backend.entity_measure(pl.handle)
    assert m["exact"] is True
    assert m["area"] == pytest.approx(40 * 10 + math.pi * 25, abs=1e-5)
    assert m["perimeter"] == pytest.approx(80 + 2 * math.pi * 5, abs=1e-5)


async def test_no_bulges_is_the_old_straight_polyline(backend):
    pl = await backend.entity_create_polyline([[0, 0], [10, 0], [10, 10]], closed=True)
    assert pl.properties["bulges"] == [0.0, 0.0, 0.0]


@pytest.mark.parametrize(
    ("bulges", "closed", "message"),
    [
        ([0, 1], True, "one value per point"),
        ([0, 0, "x", 0], True, "must be a number"),
        ([0, 0, float("nan"), 0], True, "finite"),
        ([0, 0, 0, 0.5], False, "closing segment"),
    ],
)
async def test_bad_bulges_are_refused_before_drawing(backend, bulges, closed, message):
    before = (await backend.drawing_info()).entity_count
    with pytest.raises(ValueError, match=message):
        await backend.entity_create_polyline(
            [[0, 0], [1, 0], [1, 1], [0, 1]], closed=closed, bulges=bulges
        )
    assert (await backend.drawing_info()).entity_count == before


async def test_resolve_polyline_bulges_defaults_to_straight():
    assert resolve_polyline_bulges([[0, 0], [1, 0]], None, False) == [0.0, 0.0]


async def test_intersection_line_arc_keeps_only_candidates_on_the_arc(backend):
    arc = await backend.entity_create_arc(0, 0, 10, 0, 90)  # first quadrant only
    line = await backend.entity_create_line(-20, 5, 20, 5)  # crosses the circle at x=+-8.66, y=5
    x, y = await backend.point_intersection(line.handle, arc.handle)
    assert (x, y) == pytest.approx((math.sqrt(75), 5.0))


async def test_intersection_beyond_the_arc_end_is_refused(backend):
    arc = await backend.entity_create_arc(0, 0, 10, 0, 30)
    line = await backend.entity_create_line(
        -20, 9, 20, 9
    )  # meets the circle at ~64 and ~116 degrees
    with pytest.raises(RuntimeError, match="beyond the end of the ARC"):
        await backend.point_intersection(line.handle, arc.handle)


async def test_intersection_arc_arc_and_arc_circle(backend):
    a = await backend.entity_create_arc(0, 0, 10, 0, 180)
    b = await backend.entity_create_circle(10, 0, 10)
    x, y = await backend.point_intersection(a.handle, b.handle, ref_x=5, ref_y=9)
    assert (x, y) == pytest.approx((5.0, math.sqrt(75)))
