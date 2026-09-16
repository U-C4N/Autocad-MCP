"""pid_tag_parse tool, the two P&ID resources, the rewritten prompt, the contact sheet."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastmcp import Client

import server


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        yield connected


@pytest.mark.asyncio
async def test_tag_parse_tool(client):
    out = (await client.call_tool("pid_tag_parse", {"tag": "10-FIC-101A"})).structured_content
    assert out["valid"] is True and out["description"] == "Flow Indicating Controller"
    out = (
        await client.call_tool(
            "pid_tag_parse",
            {"tag": "Q-1", "kind": "equipment", "equipment_prefixes": {"Q": "quench"}},
        )
    ).structured_content
    assert out["equipment_kind"] == "quench"


@pytest.mark.asyncio
async def test_resources_serve_the_catalogue_and_the_letter_tables(client):
    uris = {str(r.uri) for r in await client.list_resources()}
    assert {"autocad://pid/symbols", "autocad://standards/isa51"} <= uris
    symbols = json.loads((await client.read_resource("autocad://pid/symbols"))[0].text)
    assert symbols["catalog_version"] == "1" and any(
        s["symbol"] == "gate" for s in symbols["symbols"]
    )
    isa = json.loads((await client.read_resource("autocad://standards/isa51"))[0].text)
    assert isa["first_letters"]["F"] == "Flow" and "rules" in isa


@pytest.mark.asyncio
async def test_prompt_walks_the_tool_workflow(client):
    prompt = await client.get_prompt(
        "prompt_pid_diagram", {"project_name": "Unit 7", "revision": "B"}
    )
    text = "\n".join(m.content.text for m in prompt.messages)
    for needle in (
        "drawing_plan",
        'layer_set_id="pid"',
        "pid_symbol_insert",
        "pid_line_draw",
        "pid_graph",
        "drawing_critique",
        "drawing_finalize",
        "pid_from_spec",
        "Unit 7",
        "Ø10",
    ):
        assert needle in text, needle
    assert "circle with triangle" not in text, "no hand-drawn symbols"


def test_catalog_contact_sheet_renders_every_symbol(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    import engineering.pid.insert as insert_module
    from engineering.pid.symbols import all_specs
    from scripts.render_pid_catalog import render

    result = render(tmp_path / "sheet.png")
    assert result["blank"] == [], "every catalogue symbol must leave geometry on the sheet"
    assert result["symbols"] == len(all_specs()) >= 100 and result["bytes"] > 10_000

    # The evidence has to come from the drawing and the picture, not from the
    # catalogue's length: a builder that draws nothing yields a labels-only
    # sheet whose PNG is well past the byte threshold, so the symbol count and
    # the ink share are what tell the two apart.
    async def draw_nothing(*_args, **_kwargs):
        return {"handle": "0", "ports": {}}

    monkeypatch.setattr(insert_module, "insert_symbol", draw_nothing)
    labels_only = render(tmp_path / "labels_only.png")
    assert labels_only["symbols"] == 0 and len(labels_only["blank"]) == len(all_specs())
    assert labels_only["bytes"] > 10_000, "bytes alone would not have caught this"
    assert result["ink"] > labels_only["ink"] * 1.1, (result["ink"], labels_only["ink"])
