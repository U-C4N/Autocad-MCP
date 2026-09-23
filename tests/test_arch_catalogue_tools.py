"""arch_catalogue_list / arch_catalogue_insert through a real MCP client."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server
from engineering.arch.catalogue import SIZE_BASIS
from engineering.arch.layers import ARCH_ROLE_LAYER

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


async def test_list_searches_in_turkish_and_states_the_size_basis(client):
    result = (
        await client.call_tool("arch_catalogue_list", {"query": "yatak", "lang": "tr"})
    ).structured_content
    assert result["count"] == 2
    assert [row["name"] for row in result["items"]] == ["single_bed", "double_bed"]
    assert result["items"][0]["label"] == "tek kişilik yatak"
    assert result["size_basis"] == SIZE_BASIS
    assert result["families"] == ["furniture", "sanitary"]


async def test_insert_twice_defines_once(client):
    first = (
        await client.call_tool("arch_catalogue_insert", {"name": "wc", "x": 0.0, "y": 0.0})
    ).structured_content
    second = (
        await client.call_tool(
            "arch_catalogue_insert", {"name": "wc", "x": 1000.0, "y": 0.0, "rotation": 180.0}
        )
    ).structured_content
    assert first["defined"] is True and second["defined"] is False
    assert first["block_name"] == second["block_name"] == "ARCH_WC"
    assert first["layer"] == ARCH_ROLE_LAYER["sanitary"]


async def test_an_unknown_item_is_a_tool_error_naming_the_nearest(client):
    with pytest.raises(ToolError, match="nearest: wardrobe"):
        await client.call_tool("arch_catalogue_insert", {"name": "wardrob", "x": 0.0, "y": 0.0})
    with pytest.raises(ToolError, match="furniture, sanitary"):
        await client.call_tool("arch_catalogue_list", {"family": "garden"})
