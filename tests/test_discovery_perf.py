"""A tool the catalog does not list costs no catalog listing per call (1.8.0 perf).

MEASURED (``benchmarks/perf_compare.py --base v1.7.0 --aa --rounds 20``,
2026-11-05, release tree 19ada05): ``server_create_lines_2k`` - 2,000 direct
``entity_create_line`` calls through an in-memory client - took 8.5 x its
v1.7.0 time (68.7 s against 7.5 s), and 0.96 x with ``DISCOVERY_MODE=off``.
Under the search default every tool but the pinned ones is unlisted, and an
unlisted tool's call ran the whole ``tools/list`` handler on both sides: the
low-level MCP server refreshes its definition cache on a miss, and ``mcp``'s
client session lists again for a tool it has not seen. Each listing built the
three synthetic tools from scratch (``Tool.from_function``, a pydantic schema
each) and the pinned cards again: six listings a call, 26 ms a call.

Two things hold now. The synthetic tools and the pinned cards are built once
per transform (``discovery/transform.py``, "Built once"), and a call of an
unlisted tool resolves that one tool by name instead of listing
(``server._install_tool_definition_cache``). The client's own listing is the
client's (``mcp.client.session._validate_tool_result``) and stays.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.tools.base import Tool
from mcp.types import ListToolsRequest

import server
from discovery.transform import CadSearchTransform, DetailsTransform

pytestmark = pytest.mark.asyncio


def _line(x1: float, y1: float, x2: float, y2: float) -> dict:
    """Draw a line from (x1, y1) to (x2, y2)."""
    return {"handle": "1A"}


def _info(detail: bool = False) -> dict:
    """What the drawing is: units, extents, counts.

    A second paragraph, long enough to be listed short and pointed at
    describe_tool, so the pinned card is a real card and not the tool itself.
    """
    return {"ok": True}


async def _none(name, *, version=None):
    return None


# ── built once ───────────────────────────────────────────────────────────────


async def test_the_synthetic_tools_and_the_pinned_cards_are_built_once():
    transform = CadSearchTransform(always_visible=["drawing_info"])
    catalog = [
        Tool.from_function(fn=_line, name="entity_create_line"),
        Tool.from_function(fn=_info, name="drawing_info"),
    ]
    first = await transform.transform_tools(catalog)
    second = await transform.transform_tools(catalog)
    by_name = {tool.name: tool for tool in first}
    assert set(by_name) == {"drawing_info", "search_tools", "call_tool", "describe_tool"}
    for tool in second:
        assert tool is by_name[tool.name], f"{tool.name} was built again"
    assert by_name["drawing_info"] is not catalog[1], "a pinned tool is listed as its card"
    # The same objects answer a lookup by name.
    assert await transform.get_tool("search_tools", _none) is by_name["search_tools"]
    assert await transform.get_tool("call_tool", _none) is by_name["call_tool"]
    assert await transform.get_tool("describe_tool", _none) is by_name["describe_tool"]


async def test_a_pinned_card_follows_the_tool_it_was_made_from():
    transform = CadSearchTransform(always_visible=["drawing_info"])
    registered = Tool.from_function(fn=_info, name="drawing_info")
    card = transform._pinned_card(registered)
    # A listing may hand over a shallow copy (a visibility mark): same schema
    # object, same text, same card.
    assert transform._pinned_card(registered.model_copy()) is card
    # Another text or another schema is another tool: a new card.
    retold = registered.model_copy(update={"description": "Something else entirely."})
    assert transform._pinned_card(retold) is not card
    assert transform._pinned_card(registered) is not card, "the newest registration wins"


async def test_off_mode_builds_describe_tool_once(monkeypatch):
    from discovery import descriptions

    transform = DetailsTransform()
    long = Tool.from_function(fn=_info, name="drawing_info")
    wire = "Short. Details: describe_tool."
    short = long.model_copy(update={"description": wire})
    # A split description: the wire text recorded against the full one.
    monkeypatch.setitem(descriptions._FULL, "drawing_info", (wire, long.description or ""))
    first = await transform.transform_tools([short])
    second = await transform.transform_tools([short])
    assert [tool.name for tool in first] == ["drawing_info", "describe_tool"]
    assert first[-1] is second[-1]


# ── the definition cache ─────────────────────────────────────────────────────


@pytest.fixture
def search_mode():
    """The real search surface on the shared server, restored afterwards."""
    server._apply_discovery_mode("search")
    try:
        yield
    finally:
        server._apply_discovery_mode()


@pytest.fixture
def refreshes(monkeypatch):
    """Every ``tools/list`` the low-level server ran for itself (request None),
    as opposed to one a client asked for."""
    low = server.mcp._mcp_server
    handler = low.request_handlers[ListToolsRequest]
    seen: list[str] = []

    async def counting(request):
        seen.append("refresh" if request is None else "client")
        return await handler(request)

    monkeypatch.setitem(low.request_handlers, ListToolsRequest, counting)
    return seen


async def test_the_definition_cache_is_installed():
    low = server.mcp._mcp_server
    assert getattr(low._get_cached_tool_definition, "_acadmcp_by_name", False) is True
    assert isinstance(low._tool_cache, dict)


async def test_a_call_of_an_unlisted_tool_lists_nothing_on_the_server(
    search_mode, refreshes, monkeypatch
):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    low = server.mcp._mcp_server
    low._tool_cache.pop("entity_create_line", None)
    async with Client(server.mcp) as client:
        listed = {tool.name for tool in await client.list_tools()}
        assert "entity_create_line" not in listed, "the tool under test is hidden"
        await client.call_tool("drawing_new", {})
        for index in range(3):
            result = await client.call_tool(
                "entity_create_line", {"x1": 0.0, "y1": float(index), "x2": 10.0, "y2": 1.0}
            )
            assert result.data["handle"]
    assert "refresh" not in refreshes, refreshes
    assert "entity_create_line" in low._tool_cache, "resolved by name and kept"
    assert low._tool_cache["entity_create_line"].inputSchema["properties"]["x1"]


async def test_a_name_no_tool_has_takes_the_stock_path(search_mode, refreshes, monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        with pytest.raises(Exception, match="(?i)unknown tool"):
            await client.call_tool("no_such_tool_anywhere", {})
    assert refreshes.count("refresh") == 1, "the stock refresh, once, and the SDK's warning"
    assert "no_such_tool_anywhere" not in server.mcp._mcp_server._tool_cache
