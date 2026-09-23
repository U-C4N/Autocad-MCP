"""arch_room and arch_rooms_detect over the MCP wire, on a fresh headless document."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from engineering.arch.layers import ARCH_ROLE_LAYER

pytestmark = pytest.mark.asyncio

WALL = ARCH_ROLE_LAYER["wall"]


def rect(x0, y0, x1, y1):
    return [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]


@pytest_asyncio.fixture
async def client(monkeypatch):
    """In-memory client on a fresh headless document - never the operator's AutoCAD."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def draw_room(client, layer):
    """A 4000 x 5000 room between 200 mm walls: outer and inner outline."""
    for (x1, y1), (x2, y2) in [
        *rect(-200.0, -200.0, 4200.0, 5200.0),
        *rect(0.0, 0.0, 4000.0, 5000.0),
    ]:
        await client.call_tool(
            "entity_create_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer}
        )


async def test_arch_room_labels_the_measured_area(client):
    await draw_room(client, WALL)
    result = await client.call_tool(
        "arch_room", {"name": "LIVING", "number": "01", "x": 2000.0, "y": 2500.0}
    )
    room = result.structured_content["room"]
    assert room["area"] == pytest.approx(20_000_000.0)
    assert room["area_text"] == "20.00 m²"
    assert result.structured_content["layer"] == ARCH_ROLE_LAYER["room"]


async def test_arch_rooms_detect_reads_the_room_back_without_drawing(client):
    await draw_room(client, "DUVAR")
    before = (await client.call_tool("analysis_entity_stats", {})).structured_content
    result = (await client.call_tool("arch_rooms_detect", {})).structured_content
    assert result["layers"] == ["DUVAR"]
    assert [r["area"] for r in result["rooms"]] == [pytest.approx(20_000_000.0)]
    assert result["confidence_min"] == 0.6
    after = (await client.call_tool("analysis_entity_stats", {})).structured_content
    assert after["total_entities"] == before["total_entities"]


async def test_a_point_outside_every_room_is_refused_over_the_wire(client):
    await draw_room(client, WALL)
    result = await client.call_tool(
        "arch_room", {"name": "X", "x": 9000.0, "y": 9000.0}, raise_on_error=False
    )
    assert result.is_error is True
    assert "room.at: (9000, 9000) lies in no closed face" in result.content[0].text


async def test_the_detector_is_advertised_read_only(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["arch_rooms_detect"].annotations.readOnlyHint is True
    assert tools["arch_room"].annotations.destructiveHint is False
