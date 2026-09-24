"""drawing_diff over the MCP wire, on the headless engine, against the synthetic revisions."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from engineering.sheet.frames import SHEET_LAYER
from tests.fixtures.revisions import build_revision_pair

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


async def _total(client) -> int:
    stats = (await client.call_tool("analysis_entity_stats", {})).structured_content
    return stats["total_entities"]


async def test_the_current_document_against_its_older_revision(client, tmp_path):
    pair = build_revision_pair(tmp_path)
    await client.call_tool("drawing_open", {"path": pair["new"]})
    before = await _total(client)
    result = (
        await client.call_tool(
            "drawing_diff", {"old_path": pair["old"], "move_tol": pair["move_tol"]}
        )
    ).structured_content
    assert (result["summary"]["changed"], result["summary"]["added"]) == (3, 1)
    assert result["summary"]["removed"] == 1
    assert {row["old_handle"] for row in result["changed"]} == {
        pair["moved"],
        pair["text"],
        pair["insert"],
    }
    assert result["blocks"]["compared"] is True and result["blocks"]["changed"] == []
    assert result["truncated"] == {"changed": 0, "added": 0, "removed": 0}
    assert await _total(client) == before, "a diff without markup writes nothing"


async def test_two_paths_and_the_list_cap(client, tmp_path):
    pair = build_revision_pair(tmp_path)
    result = (
        await client.call_tool(
            "drawing_diff",
            {
                "old_path": pair["old"],
                "new_path": pair["new"],
                "move_tol": pair["move_tol"],
                "limit": 1,
            },
        )
    ).structured_content
    assert len(result["changed"]) == 1
    assert result["truncated"]["changed"] == 2
    assert result["summary"]["changed"] == 3


async def test_markup_clouds_every_model_space_cluster(client, tmp_path):
    pair = build_revision_pair(tmp_path)
    await client.call_tool("drawing_open", {"path": pair["new"]})
    result = (
        await client.call_tool(
            "drawing_diff",
            {"old_path": pair["old"], "move_tol": pair["move_tol"], "markup": True, "rev": "b"},
        )
    ).structured_content
    model = [c for c in result["clusters"] if c["space"] == "Model"]
    markup = result["markup"]
    assert markup["created"] is True and markup["rev"] == "B"
    assert len(markup["clouds"]) == len(model) >= 1
    for handle in markup["clouds"]:
        cloud = (await client.call_tool("entity_get", {"handle": handle})).structured_content
        assert (cloud["type"], cloud["layer"]) == ("LWPOLYLINE", SHEET_LAYER)


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        (
            {"markup": True, "rev": "B", "new_path": "NEW"},
            "markup draws revision clouds on the current",
        ),
        ({"markup": True}, "markup needs `rev`"),
        ({"markup": True, "rev": "TOO-LONG"}, "markup needs `rev`"),
    ],
)
async def test_markup_refusals_come_before_any_read(client, tmp_path, arguments, fragment):
    pair = build_revision_pair(tmp_path)
    if arguments.get("new_path") == "NEW":
        arguments = {**arguments, "new_path": pair["new"]}
    result = await client.call_tool(
        "drawing_diff", {"old_path": pair["old"], **arguments}, raise_on_error=False
    )
    assert result.is_error is True
    assert fragment in result.content[0].text


async def test_a_missing_revision_is_refused_by_parameter(client, tmp_path):
    result = await client.call_tool(
        "drawing_diff", {"old_path": str(tmp_path / "nowhere.dxf")}, raise_on_error=False
    )
    assert result.is_error is True
    assert "old_path:" in result.content[0].text and "does not exist" in result.content[0].text


async def test_a_revision_that_is_neither_dxf_nor_dwg_is_refused_by_parameter(client, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("not a drawing\n", encoding="utf-8")
    result = await client.call_tool("drawing_diff", {"old_path": str(notes)}, raise_on_error=False)
    assert result.is_error is True
    text = result.content[0].text
    assert "old_path:" in text and "reads a .dxf or a .dwg" in text


async def test_a_truncated_revision_is_refused_not_hung(client, tmp_path):
    import asyncio

    half = tmp_path / "half.dxf"
    half.write_bytes(b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1015\n")
    result = await asyncio.wait_for(
        client.call_tool("drawing_diff", {"old_path": str(half)}, raise_on_error=False),
        timeout=30,
    )
    assert result.is_error is True
    text = result.content[0].text
    assert "old_path:" in text and "not a readable DXF" in text


async def test_the_diff_is_advertised_as_non_destructive(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert tools["drawing_diff"].annotations.destructiveHint is False
