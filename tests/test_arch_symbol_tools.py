"""arch_grid / arch_symbol through a real MCP client."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server
from engineering.arch.layers import ARCH_ROLE_LAYER

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


async def test_grid_reports_its_axes_and_bubble_size(client):
    result = (
        await client.call_tool(
            "arch_grid", {"x_axes": [0.0, 4000.0, 8000.0], "y_axes": [0.0, 5000.0], "scale": 100}
        )
    ).structured_content
    assert [axis["label"] for axis in result["axes"]["x"]] == ["1", "2", "3"]
    assert [axis["label"] for axis in result["axes"]["y"]] == ["A", "B"]
    assert result["bubble_radius"] == 500.0
    assert len(result["handles"]) == 5 * 5
    assert result["layers"] == sorted({ARCH_ROLE_LAYER["grid"], ARCH_ROLE_LAYER["symbol"]})


async def test_symbol_level_and_north_arrow_in_turkish(client):
    result = (
        await client.call_tool(
            "arch_symbol",
            {"kind": "level", "x": 0.0, "y": 0.0, "params": {"value": 3.0}, "lang": "tr"},
        )
    ).structured_content
    assert result["counts"] == {"poly": 1, "line": 1, "text": 1}
    north = (
        await client.call_tool(
            "arch_symbol", {"kind": "north_arrow", "x": 0.0, "y": 0.0, "lang": "tr"}
        )
    ).structured_content
    assert north["layer"] == ARCH_ROLE_LAYER["symbol"]
    assert north["counts"] == {"circle": 1, "poly": 1, "text": 1}


async def test_refusals_are_tool_errors(client):
    with pytest.raises(
        ToolError, match="kinds are north_arrow, section_mark, level, elevation_mark"
    ):
        await client.call_tool("arch_symbol", {"kind": "compass", "x": 0.0, "y": 0.0})
    with pytest.raises(ToolError, match="p1 and p2 are required"):
        await client.call_tool(
            "arch_symbol", {"kind": "section_mark", "x": 0.0, "y": 0.0, "params": {"label": "A"}}
        )
    with pytest.raises(ToolError, match="y_axes: a grid needs at least one axis"):
        await client.call_tool("arch_grid", {"x_axes": [0.0], "y_axes": []})
