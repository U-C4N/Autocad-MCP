"""1.8-02: a refused raw command or LISP expression names the typed tools instead.

The rival's AutoCAD run had its LISP refused sixteen times: the refusal said
"no" and never where to go. ``discovery.aliases.tools_for_command(verb)`` is the
reverse of the alias corpus (``TOOL_ALIASES[*].acad``), and the refusal now
carries ``alternatives: [{tool, summary}]`` - only tools the active profile and
packs advertise, never an escape hatch. An *allowed* command that a typed tool
also does carries ``typed_alternative`` (live engine only; the headless engine
refuses the raw channel as unsupported after the denylist passes).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import config
import server
from discovery.aliases import TOOL_ALIASES, tools_for_command
from security import CommandRefusedError, lisp_command_verbs, sanitize_command, sanitize_lisp


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


@pytest_asyncio.fixture
async def _restore_packs(monkeypatch):
    yield
    monkeypatch.setattr(config.settings, "tool_packs", "all")
    await server._apply_tool_profile("full")


def _tools(payload: dict) -> list[str]:
    return [row["tool"] for row in payload["alternatives"]]


def test_the_reverse_index_reads_the_alias_corpus():
    assert set(tools_for_command("ERASE")) >= {"entity_delete", "entity_delete_many"}
    assert tools_for_command("_.erase") == tools_for_command("ERASE"), "decorators and case"
    assert tools_for_command("NO_SUCH_COMMAND") == ()
    for name in tools_for_command("INSERT"):
        assert "INSERT" in TOOL_ALIASES[name].acad


def test_the_refusals_are_typed_with_their_verbs_and_unchanged_prose():
    with pytest.raises(CommandRefusedError) as raised:
        sanitize_command("_.ERASE\n_L\n")
    assert raised.value.verbs == ["ERASE"] and raised.value.channel == "command"
    assert str(raised.value).startswith("Command rejected: ERASE is a restricted command.")
    with pytest.raises(CommandRefusedError) as raised:
        sanitize_lisp('(command "_.ERASE" "_L" "")')
    assert raised.value.verbs == ["ERASE"] and raised.value.channel == "lisp"
    assert raised.value.symbols == ["command"]
    assert str(raised.value).startswith("LISP expression rejected: contains a restricted function.")
    assert lisp_command_verbs('(vl-cmdf "-LAYER" "_S" "0" "")(command-s "_ZOOM" "_E")') == [
        "LAYER",
        "ZOOM",
    ]


@pytest.mark.asyncio
async def test_a_refused_erase_names_entity_delete_and_entity_delete_many(client):
    result = await client.call_tool(
        "system_run_command", {"command": "_.ERASE"}, raise_on_error=False
    )
    payload = result.structured_content
    assert payload["kind"] == "policy_refused" and payload["verbs"] == ["ERASE"]
    assert {"entity_delete", "entity_delete_many"} <= set(_tools(payload))
    assert all(row["summary"] for row in payload["alternatives"])


@pytest.mark.asyncio
async def test_a_refused_lisp_command_form_names_the_same_tools(client):
    result = await client.call_tool(
        "system_run_lisp", {"expression": '(command "_.ERASE" "_L" "")'}, raise_on_error=False
    )
    payload = result.structured_content
    assert payload["kind"] == "policy_refused" and payload["channel"] == "lisp"
    assert {"entity_delete", "entity_delete_many"} <= set(_tools(payload))


@pytest.mark.asyncio
async def test_a_lisp_with_no_command_form_has_no_alternatives_to_invent(client):
    result = await client.call_tool(
        "system_run_lisp",
        {"expression": "(vla-get-activedocument (vlax-get-acad-object))"},
        raise_on_error=False,
    )
    payload = result.structured_content
    assert payload["kind"] == "policy_refused"
    assert payload["verbs"] == [] and payload["alternatives"] == []
    assert "vla-get-activedocument" in payload["symbols"]


@pytest.mark.asyncio
async def test_a_hidden_pack_is_never_suggested(monkeypatch, _restore_packs):
    every = set(tools_for_command("INSERT"))
    hidden = every & (
        server.PACK_TOOL_NAMES["pid"]
        | server.PACK_TOOL_NAMES["mech"]
        | server.PACK_TOOL_NAMES["arch"]
    )
    assert hidden, "INSERT must reach a vertical pack's insert for this test to mean anything"

    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        full = (
            await connected.call_tool(
                "system_run_command", {"command": "_.INSERT"}, raise_on_error=False
            )
        ).structured_content
    assert hidden <= set(_tools(full)), "with every pack on, the pack tools are offered"

    monkeypatch.setattr(config.settings, "tool_packs", "core")
    async with Client(server.mcp) as connected:
        core = (
            await connected.call_tool(
                "system_run_command", {"command": "_.INSERT"}, raise_on_error=False
            )
        ).structured_content
    offered = set(_tools(core))
    assert not (offered & hidden), f"TOOL_PACKS=core offered hidden tools: {offered & hidden}"
    assert "block_insert" in offered


@pytest.mark.asyncio
async def test_an_allowed_command_names_its_typed_alternative(monkeypatch):
    """The raw channel ran (a stand-in engine here); the answer says which tool does it typed."""
    from backends.ezdxf_backend import EzdxfBackend

    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    sent: list[str] = []

    async def run_command(command):
        sent.append(command)
        return {"ok": True, "command": command}

    monkeypatch.setattr(backend, "system_run_command", run_command)

    async def make():
        return backend

    monkeypatch.setattr(server, "_make_backend", make)
    async with Client(server.mcp) as connected:
        result = (
            await connected.call_tool("system_run_command", {"command": "_LINE 0,0 10,0\n\n"})
        ).structured_content
        plain = (
            await connected.call_tool("system_run_command", {"command": "_NO_SUCH_VERB\n"})
        ).structured_content
    assert sent == ["_LINE 0,0 10,0\n\n", "_NO_SUCH_VERB\n"]
    assert result["ok"] is True
    assert [row["tool"] for row in result["typed_alternative"]] == ["entity_create_line"]
    assert "typed_alternative" not in plain


@pytest.mark.asyncio
async def test_an_escape_hatch_is_never_an_alternative():
    alternatives = await server._command_alternatives(["ERASE", "INSERT", "SAVEAS", "LINE"])
    for row in alternatives:
        tool = await server.mcp.get_tool(row["tool"])
        card = (getattr(tool, "meta", None) or {}).get("cad") or {}
        assert card.get("cost") != "escape", row
