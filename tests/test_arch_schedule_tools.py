"""arch_schedule over the MCP wire: rooms labelled by arch_room, scheduled as a TABLE."""

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


async def draw_two_rooms(client):
    """Rooms (200,200)-(4200,5200) and (4400,200)-(8400,5200) between 200 mm walls."""
    for (x1, y1), (x2, y2) in [
        *rect(0.0, 0.0, 8600.0, 5400.0),
        *rect(200.0, 200.0, 8400.0, 5200.0),
        ((4200.0, 200.0), (4200.0, 5200.0)),
        ((4400.0, 200.0), (4400.0, 5200.0)),
    ]:
        await client.call_tool(
            "entity_create_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": WALL}
        )


async def test_a_room_schedule_reads_the_labels_arch_room_wrote(client):
    await draw_two_rooms(client)
    await client.call_tool("arch_room", {"name": "LIVING", "number": "1", "x": 2200.0, "y": 2700.0})
    await client.call_tool("arch_room", {"name": "BED", "number": "2", "x": 6400.0, "y": 2700.0})
    result = (
        await client.call_tool("arch_schedule", {"kind": "rooms", "x": 9000.0, "y": 5400.0})
    ).structured_content
    assert result["table_rows"] == 5  # title + header + two rooms + total
    assert [(r["number"], r["area"]) for r in result["rows"]] == [
        ("1", "20.00 m²"),
        ("2", "20.00 m²"),
        ("", "40.00 m²"),
    ]


async def test_nothing_to_schedule_is_refused_over_the_wire(client):
    result = await client.call_tool(
        "arch_schedule", {"kind": "doors", "x": 0.0, "y": 0.0}, raise_on_error=False
    )
    assert result.is_error is True
    assert "kind: this drawing carries no door records" in result.content[0].text
