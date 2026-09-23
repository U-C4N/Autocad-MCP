"""SECTION 25's first four tools, called the way a client calls them.

In-memory client on a fresh headless document; the env var pins ezdxf so no
test here can reach an operator's live drawing (tests/conftest.py).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server

pytestmark = pytest.mark.asyncio

ARCH_TOOLS = {"arch_wall", "arch_opening", "arch_stair", "arch_dimension_chains"}


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def call(client, name, args) -> dict:
    return (await client.call_tool(name, args)).structured_content


async def test_the_four_tools_are_registered_under_the_architecture_group():
    tools = {tool.name: tool for tool in await server._registered_tools()}
    assert ARCH_TOOLS <= set(tools)
    groups = await server._tool_groups()
    # SECTION 25 holds the rooms and catalogue tools too since the track F merge.
    assert ARCH_TOOLS <= set(groups["architecture"])
    for name in ARCH_TOOLS:
        card = (tools[name].meta or {})["cad"]
        assert card["cost"] == "mutate"
        assert card["synonyms"], f"{name} needs an alias record"


async def test_a_plan_built_tool_by_tool(client):
    walls = await call(
        client,
        "arch_wall",
        {
            "walls": [
                {
                    "id": "shell",
                    "axis": [[0, 0], [8200, 0], [8200, 6200], [0, 6200]],
                    "thickness": 200,
                    "closed": True,
                }
            ]
        },
    )
    assert walls["added"] == ["shell"] and walls["redrawn"] == []
    partition = await call(
        client,
        "arch_wall",
        {"walls": [{"id": "p", "axis": [[4100, 0], [4100, 6200]], "thickness": 100}]},
    )
    assert partition["redrawn"] == ["shell"]
    door = await call(
        client, "arch_opening", {"wall": "shell", "offset": 5500, "width": 1000, "swing": "in"}
    )
    assert door["tag"] == "D1"
    window = await call(
        client, "arch_opening", {"wall": "shell", "kind": "window", "offset": 1500, "width": 1200}
    )
    assert window["tag"] == "W1"
    stair = await call(
        client,
        "arch_stair",
        {
            "stair_id": "s1",
            "start_x": 1000,
            "start_y": 1500,
            "width": 1000,
            "risers": 17,
            "riser_height": 170,
            "going": 290,
            "direction_deg": 90,
        },
    )
    assert stair["blondel"]["ok"] is True
    chains = await call(client, "arch_dimension_chains", {"sides": ["bottom"]})
    # 5 openings + 5 walls + 1 overall, hand-counted in tests/test_arch_dimension.py
    assert chains["count"] == 11


async def test_refusals_come_back_as_tool_errors_that_name_the_cause(client):
    with pytest.raises(ToolError, match="no architectural walls"):
        await call(client, "arch_dimension_chains", {})
    with pytest.raises(ToolError, match="'nowhere' is not a wall"):
        await call(client, "arch_opening", {"wall": "nowhere", "offset": 0, "width": 900})
    await call(
        client, "arch_wall", {"walls": [{"id": "a", "axis": [[0, 0], [5000, 0]], "thickness": 200}]}
    )
    with pytest.raises(ToolError, match="5°"):
        await call(
            client,
            "arch_wall",
            {"walls": [{"id": "b", "axis": [[-2000, -150], [2000, 0]], "thickness": 200}]},
        )
    with pytest.raises(ToolError, match="at least 4"):
        await call(
            client,
            "arch_stair",
            {
                "stair_id": "s",
                "start_x": 0,
                "start_y": 0,
                "width": 900,
                "risers": 3,
                "riser_height": 170,
                "going": 290,
                "kind": "u",
            },
        )
