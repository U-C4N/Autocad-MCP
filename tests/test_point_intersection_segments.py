"""point_intersection says whether the point lies on both segments (B18).

A LINE is intersected as the infinite line through it; the crossing can lie
beyond a drawn end, which a caller using the point as a corner must know.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import server

pytestmark = pytest.mark.asyncio


async def test_a_crossing_inside_both_segments(backend):
    a = await backend.entity_create_line(0, 0, 10, 10)
    b = await backend.entity_create_line(0, 10, 10, 0)
    got = await backend.point_intersection_detail(a.handle, b.handle)
    assert (got["x"], got["y"]) == (pytest.approx(5.0, abs=1e-9), pytest.approx(5.0, abs=1e-9))
    assert (got["on_first"], got["on_second"], got["on_both"]) == (True, True, True)
    assert got["candidates"] == 1


async def test_a_crossing_beyond_one_segment_s_end(backend):
    a = await backend.entity_create_line(0, 0, 10, 0)
    b = await backend.entity_create_line(20, -5, 20, 5)
    got = await backend.point_intersection_detail(a.handle, b.handle)
    # the x axis meets x = 20 at (20, 0): parameter 2 on the first segment
    assert (got["x"], got["y"]) == (pytest.approx(20.0, abs=1e-9), pytest.approx(0.0, abs=1e-9))
    assert (got["on_first"], got["on_second"], got["on_both"]) == (False, True, False)


async def test_an_end_touch_is_on_the_segment(backend):
    a = await backend.entity_create_line(0, 0, 10, 0)
    b = await backend.entity_create_line(10, 0, 10, 5)
    got = await backend.point_intersection_detail(a.handle, b.handle)
    assert got["on_both"] is True


async def test_a_line_circle_crossing_beyond_the_segment(backend):
    line = await backend.entity_create_line(0, 0, 1, 0)
    circle = await backend.entity_create_circle(5, 0, 1)
    got = await backend.point_intersection_detail(line.handle, circle.handle, ref_x=4, ref_y=0)
    # the x axis meets the circle at (4, 0) and (6, 0); both lie beyond x = 1
    assert (got["x"], got["y"]) == (pytest.approx(4.0, abs=1e-9), pytest.approx(0.0, abs=1e-9))
    assert (got["on_first"], got["on_second"], got["candidates"]) == (False, True, 2)


async def test_a_tangent_touch_is_one_candidate(backend):
    line = await backend.entity_create_line(-5, 1, 5, 1)
    circle = await backend.entity_create_circle(0, 0, 1)
    got = await backend.point_intersection_detail(line.handle, circle.handle)
    # y = 1 touches the unit circle at (0, 1): the discriminant is exactly 0
    assert (got["x"], got["y"]) == (pytest.approx(0.0, abs=1e-12), pytest.approx(1.0, abs=1e-12))
    assert (got["on_both"], got["candidates"]) == (True, 1)


async def test_point_intersection_keeps_its_tuple(backend):
    a = await backend.entity_create_line(0, 0, 10, 10)
    b = await backend.entity_create_line(0, 10, 10, 0)
    assert await backend.point_intersection(a.handle, b.handle) == (
        pytest.approx(5.0, abs=1e-9),
        pytest.approx(5.0, abs=1e-9),
    )


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def test_the_tool_reports_the_flags(client):
    a = (
        await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 10, "y2": 0})
    ).structured_content
    b = (
        await client.call_tool("entity_create_line", {"x1": 20, "y1": -5, "x2": 20, "y2": 5})
    ).structured_content
    got = (
        await client.call_tool(
            "point_intersection", {"handle1": a["handle"], "handle2": b["handle"]}
        )
    ).structured_content
    assert got["x"] == pytest.approx(20.0, abs=1e-9) and got["y"] == pytest.approx(0.0, abs=1e-9)
    assert (got["on_first"], got["on_second"], got["on_both"], got["candidates"]) == (
        False,
        True,
        False,
        1,
    )
