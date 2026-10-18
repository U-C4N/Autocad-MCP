"""entity_fillet between LINE / ARC / CIRCLE in any pair (headless engine).

The practice exams (SourceCAD 2D-26, 2D-14, 2D-18 ...) are built from tangent arcs between curves
that are not lines; ``entity_fillet`` answered "V1 supports LINE+LINE only". Every assertion
checks tangency against the geometry itself, not against a remembered coordinate.
"""

from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.asyncio


def _arc_ends(props):
    cx, cy = props["center"]
    r = props["radius"]
    return [
        (cx + r * math.cos(math.radians(props[k])), cy + r * math.sin(math.radians(props[k])))
        for k in ("start_angle", "end_angle")
    ]


async def test_line_and_arc_fillet_is_tangent_to_both(backend):
    line = await backend.entity_create_line(30, -5, 30, 25)
    arc = await backend.entity_create_arc(
        20, 10, 8, 300, 60
    )  # right-hand arc of the R8 circle at (20,10)
    fillet = await backend.entity_fillet(line.handle, arc.handle, 2.0, True, near_x=28, near_y=15)
    f = fillet.properties
    assert f["radius"] == pytest.approx(2.0)
    cx, cy = f["center"]
    # tangent to the vertical line x=30: the centre is 2 away from it
    assert abs(30 - cx) == pytest.approx(2.0, abs=1e-6)
    # tangent to the R8 arc: centre-to-centre distance is 8 + 2 or 8 - 2
    d = math.dist((cx, cy), (20, 10))
    assert min(abs(d - 10), abs(d - 6)) < 1e-6
    # the arc is tangent where the sources now end: trimmed source ends sit on the fillet circle
    a1 = (await backend.entity_get(line.handle)).properties
    ends = [tuple(a1["start"]), tuple(a1["end"])]
    assert any(math.dist(p, (cx, cy)) == pytest.approx(2.0, abs=1e-6) for p in ends)


async def test_arc_arc_fillet_picks_the_solution_nearest_the_point(backend):
    a = await backend.entity_create_arc(0, 0, 10, 0, 90)
    b = await backend.entity_create_arc(25, 0, 10, 90, 180)
    fillet = await backend.entity_fillet(a.handle, b.handle, 6.0, False, near_x=12.5, near_y=16)
    cx, cy = fillet.properties["center"]
    assert math.dist((cx, cy), (0, 0)) in [
        pytest.approx(16.0, abs=1e-6),
        pytest.approx(4.0, abs=1e-6),
    ]
    assert math.dist((cx, cy), (25, 0)) in [
        pytest.approx(16.0, abs=1e-6),
        pytest.approx(4.0, abs=1e-6),
    ]
    assert cy > 0  # the point we asked for is above the axis


async def test_circle_circle_fillet_never_trims_a_circle(backend):
    c1 = await backend.entity_create_circle(0, 0, 10)
    c2 = await backend.entity_create_circle(25, 0, 5)
    fillet = await backend.entity_fillet(c1.handle, c2.handle, 6.0, True, near_x=12, near_y=8)
    assert fillet.type == "ARC"
    for h, r in ((c1.handle, 10), (c2.handle, 5)):
        assert (await backend.entity_get(h)).properties["radius"] == r


async def test_fillet_arc_is_short_and_between_the_tangent_points(backend):
    line = await backend.entity_create_line(0, 0, 40, 0)
    circle = await backend.entity_create_circle(20, 8, 6)
    fillet = await backend.entity_fillet(
        line.handle, circle.handle, 3.0, False, near_x=12, near_y=3
    )
    p = fillet.properties
    sweep = (p["end_angle"] - p["start_angle"]) % 360
    assert 0 < sweep <= 180
    start, end = _arc_ends(p)
    assert abs(start[1]) == pytest.approx(0.0, abs=1e-6) or abs(end[1]) == pytest.approx(
        0.0, abs=1e-6
    )  # one end rests on the line


async def test_two_lines_keep_the_classic_behaviour(backend):
    l1 = await backend.entity_create_line(0, 0, 20, 0)
    l2 = await backend.entity_create_line(0, 0, 0, 20)
    fillet = await backend.entity_fillet(l1.handle, l2.handle, 3.0)
    assert fillet.properties["center"] == pytest.approx([3.0, 3.0])


async def test_no_tangent_circle_is_refused_by_name(backend):
    c1 = await backend.entity_create_circle(0, 0, 10)
    c2 = await backend.entity_create_circle(25, 0, 5)
    with pytest.raises(RuntimeError, match="tangent"):
        await backend.entity_fillet(
            c1.handle, c2.handle, 4.0
        )  # a circle of diameter 8 cannot bridge a gap of 10


async def test_radius_zero_between_curves_is_refused(backend):
    c1 = await backend.entity_create_circle(0, 0, 10)
    c2 = await backend.entity_create_circle(25, 0, 5)
    with pytest.raises(RuntimeError, match="radius"):
        await backend.entity_fillet(c1.handle, c2.handle, 0.0)
