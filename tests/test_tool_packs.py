"""TOOL_PACKS: advertise only the vertical packs a client uses; core is always on."""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio

import config
import server

pytestmark = pytest.mark.asyncio

PID_TOOLS = {
    "pid_symbol_list",
    "pid_symbol_insert",
    "pid_line_draw",
    "pid_tag_parse",
    "pid_graph",
    "pid_instrument_index",
    "pid_line_list",
    "pid_equipment_list",
    "pid_from_spec",
}


@pytest_asyncio.fixture(autouse=True)
async def _restore(monkeypatch):
    yield
    monkeypatch.setattr(config.settings, "tool_packs", "all")
    await server._apply_tool_profile("full")


def test_pack_registry_names_real_tools_and_only_pid_ones():
    assert server.TOOL_PACK_NAMES == ("core", "pid")
    assert server.PACK_TOOL_NAMES["pid"] == frozenset(PID_TOOLS)


async def test_core_only_hides_every_pid_tool_but_keeps_block_define(monkeypatch):
    monkeypatch.setattr(config.settings, "tool_packs", "core")
    info = await server._apply_tool_profile("full")
    disabled = set(info["disabled_tools"])
    assert PID_TOOLS <= disabled
    assert "block_define" not in disabled and "entity_set_xdata" not in disabled
    assert info["tool_packs"] == {
        "enabled": ["core"],
        "available": ["core", "pid"],
        "ignored": [],
    }


async def test_all_is_the_default_and_enables_pid(monkeypatch):
    monkeypatch.setattr(config.settings, "tool_packs", "all")
    info = await server._apply_tool_profile("full")
    assert not (PID_TOOLS & set(info["disabled_tools"]))
    assert info["tool_packs"]["enabled"] == ["core", "pid"]


async def test_unknown_pack_is_ignored_with_a_warning_and_core_cannot_be_dropped(
    monkeypatch, caplog
):
    monkeypatch.setattr(config.settings, "tool_packs", "pid,mech3000")
    with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
        info = await server._apply_tool_profile("full")
    assert info["tool_packs"] == {
        "enabled": ["core", "pid"],
        "available": ["core", "pid"],
        "ignored": ["mech3000"],
    }
    assert "mech3000" in caplog.text


async def test_lean_intersects_with_packs(monkeypatch):
    assert {"pid_symbol_insert", "pid_line_draw", "pid_graph"} <= server.LEAN_TOOL_NAMES
    monkeypatch.setattr(config.settings, "tool_packs", "core")
    info = await server._apply_tool_profile("lean")
    assert "pid_graph" in info["disabled_tools"]
    assert "entity_create_line" not in info["disabled_tools"]


async def test_system_about_reports_packs():
    from fastmcp import Client

    async with Client(server.mcp) as client:
        about = (await client.call_tool("system_about", {})).structured_content
    assert about["tool_packs"]["available"] == ["core", "pid"]
