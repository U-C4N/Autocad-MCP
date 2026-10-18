"""entity_create_circle_ttr - AutoCAD's CIRCLE > Ttr on both engines' shared contract method."""

from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.asyncio


async def test_circle_tangent_to_two_axes_sits_in_the_corner_the_caller_points_at(backend):
    x = await backend.entity_create_line(0, 0, 50, 0)
    y = await backend.entity_create_line(0, 0, 0, 50)
    result = await backend.entity_create_circle_ttr(x.handle, y.handle, 4.0, near_x=10, near_y=10)
    assert result["ok"] and result["center"] == pytest.approx([4.0, 4.0])
    assert result["tangent_a"] == pytest.approx([4.0, 0.0]) and result[
        "tangent_b"
    ] == pytest.approx([0.0, 4.0])
    assert result["extended"] is False and result["candidates"] == 4
    made = await backend.entity_get(result["handle"])
    assert made.type == "CIRCLE" and made.properties["radius"] == 4.0


async def test_two_circles_pick_by_near(backend):
    a = await backend.entity_create_circle(0, 0, 10)
    b = await backend.entity_create_circle(25, 0, 5)
    up = await backend.entity_create_circle_ttr(a.handle, b.handle, 6.0, near_x=12, near_y=15)
    down = await backend.entity_create_circle_ttr(a.handle, b.handle, 6.0, near_x=12, near_y=-15)
    assert up["center"][1] > 0 > down["center"][1]
    for r in (up, down):
        d1 = math.dist(r["center"], (0, 0))
        d2 = math.dist(r["center"], (25, 0))
        assert min(abs(d1 - 16), abs(d1 - 4)) < 1e-6 and min(abs(d2 - 11), abs(d2 - 1)) < 1e-6


async def test_line_arc_flags_extension_when_the_tangent_point_is_past_the_segment(backend):
    short = await backend.entity_create_line(
        0, 0, 3, 0
    )  # a radius-2 circle near x=10 touches the axis far past it
    circle = await backend.entity_create_circle(10, 6, 4)
    result = await backend.entity_create_circle_ttr(
        short.handle, circle.handle, 2.0, near_x=9, near_y=2
    )
    assert result["extended"] is True


async def test_refusals_are_named(backend):
    a = await backend.entity_create_circle(0, 0, 10)
    b = await backend.entity_create_circle(25, 0, 5)
    text = await backend.entity_create_text("x", 0, 0)
    with pytest.raises(RuntimeError, match="tangent to both"):
        await backend.entity_create_circle_ttr(a.handle, b.handle, 4.0)
    with pytest.raises(RuntimeError, match="supports LINE, ARC and CIRCLE"):
        await backend.entity_create_circle_ttr(a.handle, text.handle, 4.0)
    with pytest.raises(RuntimeError, match="same entity"):
        await backend.entity_create_circle_ttr(a.handle, a.handle, 4.0)
    with pytest.raises(RuntimeError, match="radius"):
        await backend.entity_create_circle_ttr(a.handle, b.handle, 0)
