"""drawing_scale_check over the MCP wire, on a fresh headless document."""

from __future__ import annotations

import ezdxf
import pytest
import pytest_asyncio
from fastmcp import Client

import server
from tests.fixtures.plant_pair import build_plant_pair

pytestmark = pytest.mark.asyncio

#: Six tags of one plant in millimetres; the closest pair is 7 211 apart.
PLAN = {
    "T4100": (0.0, 0.0),
    "T4200": (12000.0, 0.0),
    "T4300": (12000.0, 9000.0),
    "T4400": (0.0, 9000.0),
    "T4500": (6000.0, 4000.0),
    "T4600": (20000.0, 15000.0),
}


@pytest_asyncio.fixture
async def client(monkeypatch):
    """In-memory client on a fresh headless document - never the operator's AutoCAD."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


def reference_file(tmp_path):
    """The plan's tags at full size in a millimetre DXF (INSUNITS 4)."""
    doc = ezdxf.new(dxfversion="R2010", units=4)
    msp = doc.modelspace()
    for tag, (x, y) in PLAN.items():
        msp.add_text(tag, height=250.0, dxfattribs={"insert": (x, y), "layer": "TAGS"})
    path = tmp_path / "reference.dxf"
    doc.saveas(path)
    return str(path)


async def test_the_synthetic_pid_is_schematic_against_its_layout(client, tmp_path):
    truth = build_plant_pair(tmp_path)
    result = await client.call_tool(
        "drawing_scale_check", {"path": truth["pid"], "reference": truth["layout"]}
    )
    assert result.structured_content["verdict"] == truth["scale_verdict"]


async def test_the_current_document_at_half_scale_is_to_scale_with_factor_one_half(
    client, tmp_path
):
    # The same tags at half the coordinates in the current document. Its
    # header says metres (drawing_new keeps ezdxf's default INSUNITS 6), but
    # 250-unit text and a 10 000-unit spread read as millimetres, and the
    # check converts by the inferred unit: the factor stays 0.5.
    for tag, (x, y) in PLAN.items():
        await client.call_tool(
            "entity_create_text",
            {"text": tag, "x": x * 0.5, "y": y * 0.5, "height": 250.0, "layer": "TAGS"},
        )
    before = (await client.call_tool("analysis_entity_stats", {})).structured_content
    result = await client.call_tool("drawing_scale_check", {"reference": reference_file(tmp_path)})
    after = (await client.call_tool("analysis_entity_stats", {})).structured_content
    assert after == before
    report = result.structured_content
    assert report["units"]["a"]["unit"] == report["units"]["b"]["unit"] == "mm"
    assert report["verdict"] == "to_scale"
    assert report["factor"] == pytest.approx(0.5, abs=1e-12)
    assert report["pair_count"] == 15
    assert report["ambiguous"] == []


async def test_a_missing_reference_is_refused_by_name(client, tmp_path):
    result = await client.call_tool(
        "drawing_scale_check", {"reference": str(tmp_path / "none.dxf")}, raise_on_error=False
    )
    assert result.is_error is True
    assert "reference: no such file" in result.content[0].text


async def test_a_non_positive_floor_is_refused(client, tmp_path):
    result = await client.call_tool(
        "drawing_scale_check",
        {"reference": reference_file(tmp_path), "min_pair_mm": 0.0},
        raise_on_error=False,
    )
    assert result.is_error is True
    assert "min_pair_mm" in result.content[0].text


async def test_drawing_scale_check_is_advertised_read_only(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["drawing_scale_check"].annotations.readOnlyHint is True
