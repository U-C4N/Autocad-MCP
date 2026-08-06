"""Tool-profile (TOOL_PROFILE=lean/full) behavior.

v1.5.0 removed the `core` profile: the discovery layer (tool search + the
AutoCAD command aliases) is the answer to a crowded surface, so a third
hand-maintained deny-list had no reason to exist. `core` must still *start*
the server — it falls back to `full` with a deprecation warning that names it.
"""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from fastmcp import Client

import config
import server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _restore_full_profile():
    """Profiles mutate global server enablement; always restore full."""
    yield
    await server._apply_tool_profile("full")


async def _registered_names() -> set[str]:
    return {tool.name for tool in await server._registered_tools() if getattr(tool, "name", None)}


async def test_lean_names_all_exist_in_registry():
    registered = await _registered_names()
    missing = server.LEAN_TOOL_NAMES - registered
    assert not missing, f"LEAN_TOOL_NAMES references unknown tools: {sorted(missing)}"


async def test_only_lean_and_full_profiles_exist():
    """M1.1 — `core` is gone, deny-list and all."""
    assert server.TOOL_PROFILES == ("lean", "full")
    assert not hasattr(server, "CORE_EXCLUDED_TOOL_NAMES")


async def test_full_profile_hides_only_gated_solids():
    """Full hides nothing except the opt-in 3D tools while ENABLE_3D=false."""
    info = await server._apply_tool_profile("full")
    assert info["profile"] == "full"
    assert set(info["disabled_tools"]) == set(server.SOLID_TOOL_NAMES)
    assert info["enabled_count"] == info["registered_count"] - len(server.SOLID_TOOL_NAMES)
    # An accepted profile is applied as requested, so there is nothing to report.
    assert "requested" not in info


async def test_invalid_profile_falls_back_to_full(caplog):
    with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
        info = await server._apply_tool_profile("does-not-exist")
    assert info["profile"] == "full"
    assert info["requested"] == "does-not-exist"
    assert set(info["disabled_tools"]) == set(server.SOLID_TOOL_NAMES)
    text = caplog.text
    assert "does-not-exist" in text
    # An unknown value is not a removed one; don't tell the user we deleted it.
    assert "removed" not in text.lower()


async def test_removed_core_profile_falls_back_to_full_with_a_deprecation(caplog):
    """TOOL_PROFILE=core must keep the server bootable, loudly."""
    with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
        info = await server._apply_tool_profile("core")
    assert info["profile"] == "full"
    assert set(info["disabled_tools"]) == set(server.SOLID_TOOL_NAMES)
    text = caplog.text
    assert "core" in text
    assert "removed" in text.lower()
    # The warning has to say what to use instead.
    assert "lean" in text and "full" in text


async def test_removed_core_profile_is_reported_by_system_about():
    """A log line on a STDIO server is easy to miss; system_about must show it."""
    await server._apply_tool_profile("core")
    ctx = _AboutCtx()
    about = await server.system_about(ctx=ctx)
    assert about["tool_profile"]["profile"] == "full"
    assert about["tool_profile"]["requested"] == "core"


class _AboutCtx:
    """system_about only reads lifespan_context; no backend needed."""

    def __init__(self):
        self.lifespan_context = {"backend": None}


async def test_core_env_value_still_serves_the_full_surface(monkeypatch):
    """The removal must not brick an existing TOOL_PROFILE=core deployment."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")  # setattr here was a no-op (T0.8-B)
    monkeypatch.setattr(config.settings, "tool_profile", "core")
    async with Client(server.mcp) as client:
        visible = {tool.name for tool in await client.list_tools()}
    registered = await _registered_names()
    assert visible == registered - server.SOLID_TOOL_NAMES
    # The escape hatches core used to hide are back on the wire.
    assert "system_run_command" in visible


async def test_lean_profile_filters_client_view(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")  # setattr here was a no-op (T0.8-B)
    monkeypatch.setattr(config.settings, "tool_profile", "lean")
    async with Client(server.mcp) as client:
        visible = {tool.name for tool in await client.list_tools()}
    assert visible == set(server.LEAN_TOOL_NAMES)


async def test_profile_switch_is_idempotent():
    await server._apply_tool_profile("lean")
    info = await server._apply_tool_profile("full")
    assert set(info["disabled_tools"]) == set(server.SOLID_TOOL_NAMES)
    lean_again = await server._apply_tool_profile("lean")
    assert lean_again["enabled_count"] == len(server.LEAN_TOOL_NAMES)
