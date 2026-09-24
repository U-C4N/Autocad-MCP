"""drawing_understand over the MCP wire, on a fresh headless document."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from tests.fixtures.plant_pair import build_plant_pair

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    """In-memory client on a fresh headless document - never the operator's AutoCAD."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def test_drawing_understand_reads_the_synthetic_layout_file(client, tmp_path):
    truth = build_plant_pair(tmp_path)
    result = await client.call_tool("drawing_understand", {"path": truth["layout"]})
    report = result.structured_content
    assert report["units"]["declared"]["code"] == truth["layout_insunits"]
    assert report["units"]["inferred"] == truth["layout_true_unit"]
    assert report["extents"]["outliers"][0]["handle"] == truth["outlier_handle"]
    assert len(report["clusters"]) == truth["cluster_count"]


async def test_drawing_understand_reads_the_current_document_and_leaves_it_alone(client):
    # A 6400 x 4400 outline on WALL (perimeter 2 x (6400 + 4400) = 21 600) and
    # one tag.
    corners = [(0.0, 0.0), (6400.0, 0.0), (6400.0, 4400.0), (0.0, 4400.0)]
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1], strict=True):
        await client.call_tool(
            "entity_create_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": "WALL"}
        )
    await client.call_tool(
        "entity_create_text",
        {"text": "T4100", "x": 1000.0, "y": 1000.0, "height": 250.0, "layer": "TAGS"},
    )
    before = (await client.call_tool("analysis_entity_stats", {})).structured_content
    report = (await client.call_tool("drawing_understand", {})).structured_content
    after = (await client.call_tool("analysis_entity_stats", {})).structured_content
    assert after == before
    layers = {row["name"]: row for row in report["layers"]}
    assert layers["WALL"]["entities"] == 4
    assert layers["WALL"]["length"] == pytest.approx(21600.0, abs=1e-9)
    assert [t["tag"] for t in report["equipment_tags"]] == ["T4100"]


async def test_a_missing_file_is_refused_by_path(client, tmp_path):
    missing = tmp_path / "nowhere.dxf"
    result = await client.call_tool(
        "drawing_understand", {"path": str(missing)}, raise_on_error=False
    )
    assert result.is_error is True
    assert "path: no such file" in result.content[0].text
    assert "nowhere.dxf" in result.content[0].text


async def test_a_file_the_engine_cannot_parse_is_refused(client, tmp_path):
    # Not a DXF: the snapshot's parse error comes back as a tool error that
    # names the file, never as a traceback.
    garbage = tmp_path / "not_a_drawing.dxf"
    garbage.write_text("this is not a drawing", encoding="utf-8")
    result = await client.call_tool(
        "drawing_understand", {"path": str(garbage)}, raise_on_error=False
    )
    assert result.is_error is True
    assert "not_a_drawing.dxf" in result.content[0].text


async def test_drawing_understand_is_advertised_read_only(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["drawing_understand"].annotations.readOnlyHint is True
