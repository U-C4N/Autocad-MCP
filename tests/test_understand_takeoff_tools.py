"""pipe_takeoff and cable_takeoff over the MCP wire, on the synthetic plant pair - never on a
client drawing."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from tests.fixtures.plant_pair import build_plant_pair

pytestmark = pytest.mark.asyncio

CSV_NAMES = [
    "takeoff_metraj.csv",
    "takeoff_ozet.csv",
    "takeoff_kontrol.csv",
    "takeoff_metodoloji.csv",
]


@pytest_asyncio.fixture
async def client(monkeypatch):
    """In-memory client on the headless engine - never the operator's AutoCAD."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        yield connected


@pytest.fixture
def plant(tmp_path):
    return build_plant_pair(tmp_path)


async def test_pipe_takeoff_measures_on_the_layout_and_writes_the_workbook(client, plant, tmp_path):
    output = tmp_path / "takeoff.xlsx"
    arguments = {
        "pid_path": plant["pid"],
        "layout_path": plant["layout"],
        "output": str(output),
        "lang": "tr",
    }
    result = (await client.call_tool("pipe_takeoff", arguments)).structured_content
    assert result["length_source"] == "layout"
    assert result["scale"]["verdict"] == plant["scale_verdict"]
    files = result["files"]
    assert [Path(p).name for p in files["csv"]] == CSV_NAMES
    assert all(Path(p).exists() for p in files["csv"])
    if files["xlsx"] is None:
        assert files["refused"]["capability"] == "xlsx_write"
    else:
        assert Path(files["xlsx"]) == output
        assert output.exists()


async def test_pid_lengths_on_the_schematic_pid_are_refused_without_force(client, plant):
    arguments = {"pid_path": plant["pid"], "layout_path": plant["layout"], "length_source": "pid"}
    result = await client.call_tool("pipe_takeoff", arguments, raise_on_error=False)
    assert result.is_error is True
    text = result.content[0].text
    assert "the scale check calls this P&ID schematic" in text
    assert "force=True" in text


async def test_force_measures_on_the_pid_and_marks_it_unverified(client, plant):
    arguments = {
        "pid_path": plant["pid"],
        "layout_path": plant["layout"],
        "length_source": "pid",
        "force": True,
    }
    result = (await client.call_tool("pipe_takeoff", arguments)).structured_content
    assert (result["length_source"], result["scale_verified"]) == ("pid", False)
    assert result["files"] is None  # no output named, nothing written


async def test_an_unknown_language_is_refused_before_anything_is_read(client, tmp_path):
    arguments = {"pid_path": str(tmp_path / "never_read.dxf"), "lang": "de"}
    result = await client.call_tool("pipe_takeoff", arguments, raise_on_error=False)
    assert result.is_error is True
    assert "lang: 'de' unknown; choose from tr, en, ru" in result.content[0].text


async def test_the_drawings_are_never_modified(client, plant, tmp_path):
    before = {key: Path(plant[key]).read_bytes() for key in ("pid", "layout")}
    arguments = {
        "pid_path": plant["pid"],
        "layout_path": plant["layout"],
        "output": str(tmp_path / "t.xlsx"),
    }
    await client.call_tool("pipe_takeoff", arguments)
    assert {key: Path(plant[key]).read_bytes() for key in ("pid", "layout")} == before


async def test_the_tool_says_it_writes_a_file_and_destroys_nothing(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    annotations = tools["pipe_takeoff"].annotations
    assert annotations.readOnlyHint is False
    assert annotations.destructiveHint is False


async def test_cable_takeoff_gives_every_synthetic_load_its_rounded_cable(client, plant, tmp_path):
    arguments = {
        "pid_path": plant["pid"],
        "layout_path": plant["layout"],
        "output": str(tmp_path / "cables.xlsx"),
        "lang": "ru",
    }
    result = (await client.call_tool("cable_takeoff", arguments)).structured_content
    rows = {row["tag"]: row for row in result["rows"]}
    for expected in plant["cable_rows"]:
        assert rows[expected["tag"]]["cable_m"] == expected["roundup_m"], expected["tag"]
    assert Path(result["files"]["csv"][0]).name == "cables_metraj.csv"


async def test_cable_takeoff_without_a_layout_leaves_every_length_empty(client, plant):
    result = (
        await client.call_tool("cable_takeoff", {"pid_path": plant["pid"]})
    ).structured_content
    assert result["rows"], "the synthetic P&ID carries electrical loads"
    assert all(row["cable_m"] is None for row in result["rows"])
    no_layout = {item["tag"] for item in result["open_items"] if item["item"] == "no_layout"}
    assert no_layout == {row["tag"] for row in result["rows"]}


async def test_a_malformed_section_rule_is_refused_by_path(client, plant):
    arguments = {"pid_path": plant["pid"], "section_rules": [{"max_kw": 5}]}
    result = await client.call_tool("cable_takeoff", arguments, raise_on_error=False)
    assert result.is_error is True
    assert "section_rules[0].section: expected a non-empty text" in result.content[0].text


async def test_the_cable_tool_says_it_writes_a_file_and_destroys_nothing(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    annotations = tools["cable_takeoff"].annotations
    assert annotations.readOnlyHint is False
    assert annotations.destructiveHint is False
