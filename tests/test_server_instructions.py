"""The server instructions (1.8-11): short, batch-first, naming only what is advertised.

Up to 1.7.0 the instructions taught one call per entity (an ``entity_create_*``
workflow), never mentioned batching and named the stale
``titleblock_apply_iso_a3``. Since 1.8-11 ``_server_instructions(mode, profile,
packs)`` builds them from the surface a client actually sees; what is pinned
here:

* under full, lean, ``TOOL_PACKS=core`` and search, every tool the text a real
  client receives names is registered AND advertised on that client's list;
* in search mode the only tools named are the synthetic three and the pins the
  list carries;
* the text is at most 250 tokens (the token suite's ratio for text, 4.19
  characters a token), carries nothing session- or time-specific, and two
  clients of one configuration receive the same bytes;
* tools/list order is deterministic (a client's prompt-cache prefix);
* a tool the profile hides stays refused on a direct call in search mode, while
  a merely unlisted one stays callable (spike S1's open question).
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import config
import server
from benchmarks.token_suite import CHARS_PER_TOKEN, FORMAT_TEXT
from discovery.instructions import SYNTHETIC_TOOLS, build_instructions, named_tools
from discovery.transform import DEFAULT_PINNED

pytestmark = pytest.mark.asyncio

#: plan section 4: server instructions <= 250 tokens
CEILING_TOKENS = 250

#: Identifiers the instructions may name that are not tools: arguments.
ARGUMENTS = {"expected_revision"}

SURFACES = {
    "full": {"tool_profile": "full", "tool_packs": "all", "mode": "off"},
    "lean": {"tool_profile": "lean", "tool_packs": "all", "mode": "off"},
    "packs_core": {"tool_profile": "full", "tool_packs": "core", "mode": "off"},
    "lean_core": {"tool_profile": "lean", "tool_packs": "core", "mode": "off"},
    "search": {"tool_profile": "full", "tool_packs": "all", "mode": "search"},
    "search_lean": {"tool_profile": "lean", "tool_packs": "all", "mode": "search"},
}


@pytest_asyncio.fixture
async def surface(monkeypatch):
    """Apply one surface, yield a connect() that returns (instructions, tool names)."""
    applied = config.settings.discovery_mode

    async def connect(name: str) -> tuple[str, list[str]]:
        spec = SURFACES[name]
        monkeypatch.setattr(config.settings, "tool_profile", spec["tool_profile"])
        monkeypatch.setattr(config.settings, "tool_packs", spec["tool_packs"])
        server._apply_discovery_mode(spec["mode"])
        async with Client(server.mcp) as client:
            text = client.initialize_result.instructions or ""
            names = [tool.name for tool in await client.list_tools()]
        return text, names

    yield connect
    monkeypatch.undo()
    server._apply_discovery_mode(applied)
    await server._apply_tool_profile()


async def _registered() -> set[str]:
    return {tool.name for tool in await server._registered_tools()}


def _identifiers(text: str) -> set[str]:
    return set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text))


@pytest.mark.parametrize("name", sorted(SURFACES))
async def test_every_tool_named_is_registered_and_advertised(surface, name):
    text, advertised = await surface(name)
    registered = await _registered()
    candidates = registered | set(SYNTHETIC_TOOLS)
    named = named_tools(text, candidates)
    assert named, name
    assert not (named - set(advertised)), (
        f"{name}: named but not advertised {named - set(advertised)}"
    )
    # every snake_case word is a tool on that list or a known argument: a stale
    # or misspelt tool name cannot hide as "not a registered tool"
    unknown = _identifiers(text) - set(advertised) - ARGUMENTS
    assert not unknown, f"{name}: {sorted(unknown)}"


@pytest.mark.parametrize("name", sorted(SURFACES))
async def test_the_text_is_at_most_250_tokens(surface, name):
    text, _ = await surface(name)
    tokens = len(text) / CHARS_PER_TOKEN[FORMAT_TEXT].chars_per_token
    assert tokens <= CEILING_TOKENS, f"{name}: {len(text)} chars, ~{tokens:.0f} tokens"


async def test_the_full_surface_teaches_batching_first(surface):
    text, _ = await surface("full")
    assert "never one call per entity" in text
    for tool in (
        "cad_batch",
        "entity_batch_create",
        "entity_modify_where",
        "dimension_auto",
        "mech_part_from_spec",
        "pid_from_spec",
        "arch_plan_from_spec",
        "titleblock_apply",
    ):
        assert re.search(rf"\b{tool}\b", text), tool
    assert "$name" in text, "cad_batch's step references"
    assert "fields=" in text and "compact=" in text
    assert "expected_revision" in text and "hint" in text
    assert text.index("cad_batch") < text.index("entity_batch_create")


@pytest.mark.parametrize("name", sorted(SURFACES))
async def test_the_stale_title_block_alias_and_per_entity_workflow_are_gone(surface, name):
    text, _ = await surface(name)
    assert "titleblock_apply_iso_a3" not in text
    assert "entity_create_" not in text
    assert "1.7" not in text and "1.8" not in text, "no release-specific text"


async def test_search_mode_names_only_the_synthetic_tools_and_the_listed_pins(surface):
    text, advertised = await surface("search")
    named = named_tools(text, await _registered() | set(SYNTHETIC_TOOLS))
    assert set(SYNTHETIC_TOOLS) <= named
    assert named <= set(SYNTHETIC_TOOLS) | set(DEFAULT_PINNED)
    assert set(DEFAULT_PINNED) <= set(advertised)
    assert "names=[...]" in text, "describe_tool takes several names"


async def test_search_mode_without_pins_names_only_the_synthetic_tools(surface, monkeypatch):
    monkeypatch.setattr(config.settings, "discovery_pinned", ())
    text, advertised = await surface("search")
    assert advertised == ["search_tools", "call_tool", "describe_tool"]
    assert named_tools(text, await _registered() | set(SYNTHETIC_TOOLS)) == set(SYNTHETIC_TOOLS)


async def test_a_lean_surface_drops_the_lines_whose_tools_it_hides(surface):
    text, advertised = await surface("lean_core")
    assert "dimension_auto" not in advertised and "dimension_auto" not in text
    assert "entity_batch_create" not in text
    assert "pid_from_spec" not in text and "mech_part_from_spec" not in text
    # what lean keeps for batching is still taught
    assert "cad_batch" in advertised and "cad_batch" in text
    assert "entity_modify_where" in advertised and "entity_modify_where" in text


async def test_two_clients_of_one_configuration_receive_the_same_bytes(surface):
    first, _ = await surface("full")
    second, _ = await surface("full")
    assert first == second
    assert first == server._server_instructions("off", "full", set(server.TOOL_PACK_NAMES))
    # nothing session- or time-specific: no ids, dates or clock times
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-", first)
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b|\d\d:\d\d", first)


async def test_the_builder_is_pure_and_filters_by_the_advertised_set():
    every = build_instructions("off", None)
    assert "dimension_auto" in every
    only_batch = build_instructions("off", {"cad_batch"})
    assert "cad_batch" in only_batch and "dimension_auto" not in only_batch
    assert "Batch first" in only_batch
    nothing = build_instructions("off", set())
    assert "Batch first" not in nothing and "cad_batch" not in nothing
    pins_hidden = build_instructions("search", {"drawing_info"}, ("cad_batch", "drawing_info"))
    assert "Listed directly: drawing_info." in pins_hidden
    assert "cad_batch" not in pins_hidden


# -- tools/list order -------------------------------------------------------


@pytest.mark.parametrize("name", ["full", "search", "packs_core"])
async def test_tools_list_order_is_deterministic(surface, name):
    _, first = await surface(name)
    _, second = await surface(name)
    await server._apply_tool_profile()
    _, third = await surface(name)
    assert first == second == third
    assert len(first) == len(set(first))


async def test_search_mode_lists_the_pins_first_then_the_synthetic_tools(surface):
    _, names = await surface("search")
    assert names[-3:] == ["search_tools", "call_tool", "describe_tool"]
    assert set(names[:-3]) == set(DEFAULT_PINNED)


# -- direct calls in search mode (spike S1) ----------------------------------


async def test_an_unlisted_tool_stays_directly_callable_in_search_mode(surface):
    _, names = await surface("search")
    assert "drawing_new" not in names
    server._apply_discovery_mode("search")
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        line = (
            await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
        ).structured_content
    assert line["handle"]


async def test_a_profile_hidden_tool_stays_refused_on_a_direct_call_in_search_mode(surface):
    await surface("search")
    assert not config.settings.enable_3d
    server._apply_discovery_mode("search")
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("solid_box", {"x": 0, "y": 0, "z": 0, "dx": 1, "dy": 1, "dz": 1})
