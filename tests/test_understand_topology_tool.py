"""drawing_topology_check over the MCP wire, on the headless engine."""

from __future__ import annotations

import ezdxf
import pytest
import pytest_asyncio
from fastmcp import Client

import server
from tests.fixtures.plant_pair import build_plant_pair

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def pipe_layer(tmp_path_factory) -> str:
    truth = build_plant_pair(tmp_path_factory.mktemp("plant"))
    return next(
        name for name, service in sorted(truth["layer_services"].items()) if service == "product"
    )


@pytest_asyncio.fixture
async def client(monkeypatch):
    """In-memory client on a fresh headless document - never the operator's AutoCAD."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def _line(client, layer, x1, y1, x2, y2):
    await client.call_tool(
        "entity_create_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer}
    )


async def _total(client) -> int:
    stats = (await client.call_tool("analysis_entity_stats", {})).structured_content
    return stats["total_entities"]


async def test_the_default_layers_are_the_classified_network_layers(client, pipe_layer):
    await _line(client, pipe_layer, 0, 0, 1000, 0)
    await _line(client, pipe_layer, 1003, 0, 2003, 0)
    before = await _total(client)
    result = (await client.call_tool("drawing_topology_check", {})).structured_content
    assert result["layers"] == [pipe_layer]
    assert [row["layer"] for row in result["classification"]] == [pipe_layer]
    assert result["counts"] == {"dangling": 2, "near_miss": 1, "crossing": 0}
    assert result["near_miss"][0]["gap"] == pytest.approx(3.0)
    assert await _total(client) == before, "the check never writes"


async def test_a_dxf_path_is_read_without_opening_it(client, pipe_layer, tmp_path):
    doc = ezdxf.new()
    doc.layers.add(pipe_layer)
    msp = doc.modelspace()
    msp.add_line((0, 0), (1000, 0), dxfattribs={"layer": pipe_layer})
    msp.add_line((500, -500), (500, 500), dxfattribs={"layer": pipe_layer})
    path = tmp_path / "crossing.dxf"
    doc.saveas(path)
    result = (
        await client.call_tool("drawing_topology_check", {"path": str(path), "gap": 5.0})
    ).structured_content
    assert result["counts"]["crossing"] == 1
    assert result["crossing"][0]["at"] == [500.0, 0.0]
    assert await _total(client) == 0, "the current document is untouched"


async def test_an_unknown_layer_is_refused_with_the_drawings_layers(client, pipe_layer):
    await _line(client, pipe_layer, 0, 0, 10, 0)
    result = await client.call_tool(
        "drawing_topology_check", {"layers": ["NOPE"]}, raise_on_error=False
    )
    assert result.is_error is True
    assert "layers: 'NOPE' is not a layer of this drawing" in result.content[0].text


async def test_a_drawing_with_no_network_layer_is_refused_not_passed(client):
    await _line(client, "GEOMETRY", 0, 0, 10, 0)
    result = await client.call_tool("drawing_topology_check", {}, raise_on_error=False)
    assert result.is_error is True
    assert "no layer of this drawing is classified piping or electrical" in result.content[0].text


async def test_gap_must_exceed_tol(client, pipe_layer):
    await _line(client, pipe_layer, 0, 0, 10, 0)
    result = await client.call_tool(
        "drawing_topology_check", {"gap": 0.005, "tol": 0.01}, raise_on_error=False
    )
    assert result.is_error is True
    assert "gap (0.005) must exceed tol (0.01)" in result.content[0].text


async def test_the_check_is_advertised_read_only(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["drawing_topology_check"].annotations.readOnlyHint is True
