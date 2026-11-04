"""W3 (1.8.0): a long docstring is listed as its leading paragraphs, never lost.

``discovery/descriptions.py`` cuts a description over
``WIRE_DESCRIPTION_MAX`` characters back to its leading whole paragraphs plus
``Details: describe_tool.`` at registration, keeps the full text server-side
and hands it to ``describe_tool``. These tests hold the four halves of that:

* the listing: every registered tool's wire description fits the limit, and a
  split one is a prefix of its docstring - whole paragraphs, nothing reworded;
* the full text: ``describe_tool`` returns every split tool's docstring as
  written, and the search index and the pinned cards read it too;
* the rule itself: an explicit ``Details:`` paragraph splits there, at least
  the first paragraph is kept, a paragraph ending in ``:`` and an indented
  block stay with what they introduce;
* the point of it: the idle cells measured the way ``benchmarks.token_suite``
  measures them stand under their ceilings in ``benchmarks/token_budget.json``,
  and a description inflated after registration is still what gets listed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re

import pytest
from fastmcp import Client
from fastmcp.tools.base import Tool

import config
import server
from benchmarks import token_suite as ts
from discovery import descriptions
from discovery.descriptions import (
    DETAILS_POINTER,
    WIRE_DESCRIPTION_MAX,
    full_description,
    record_wire_description,
    split_tool_names,
    wire_description,
)
from discovery.serialize import PIN_POINTER, pinned_card
from discovery.transform import DEFAULT_PINNED, MAX_DESCRIBE_NAMES

#: The most a listed description may carry: the limit, the paragraph break
#: and the pointer sentence.
WIRE_LIMIT = WIRE_DESCRIPTION_MAX + len("\n\n") + len(DETAILS_POINTER)

BUDGET = json.loads(ts.DEFAULT_BUDGET.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _restore_discovery_mode():
    yield
    server._apply_discovery_mode()


def _registered() -> dict[str, Tool]:
    return {tool.name: tool for tool in server._local_tool_components()}


def _docstring(tool: Tool) -> str:
    """The text FastMCP derives a description from: the function's own docstring."""
    return inspect.getdoc(tool.fn) or ""


# -- the listing ----------------------------------------------------------------


def test_every_registered_tool_lists_a_description_within_the_limit():
    tools = _registered()
    assert len(tools) > 200
    over = {
        name: len(t.description or "")
        for name, t in tools.items()
        if len(t.description or "") > WIRE_LIMIT
    }
    assert not over, over


def test_a_split_description_is_whole_leading_paragraphs_of_the_docstring():
    tools = _registered()
    split = split_tool_names() & set(tools)
    assert len(split) >= 40, "the long docstrings of the 1.8.0 catalog are split"
    for name in split:
        wire, doc = tools[name].description, _docstring(tools[name])
        assert wire.endswith(f"\n\n{DETAILS_POINTER}"), name
        head = wire[: -len(f"\n\n{DETAILS_POINTER}")]
        assert doc.startswith(head), name
        rest = doc[len(head) :]
        assert re.match(r"\n[ \t]*\n", rest), f"{name}: cut inside a paragraph"
        assert len(head) <= WIRE_DESCRIPTION_MAX, name


def test_an_unsplit_tool_lists_its_docstring_untouched():
    tools = _registered()
    for name, tool in tools.items():
        if name in split_tool_names():
            continue
        assert tool.description == _docstring(tool), name
        assert len(tool.description or "") <= WIRE_DESCRIPTION_MAX, name


@pytest.mark.asyncio
async def test_the_full_catalog_lean_and_packs_list_the_wire_text(monkeypatch):
    """DISCOVERY_MODE=off, TOOL_PROFILE=lean and TOOL_PACKS=core: the same text."""
    tools = _registered()
    surfaces = []
    previous_profile, previous_packs = config.settings.tool_profile, config.settings.tool_packs
    try:
        server._apply_discovery_mode("off")
        for profile, packs in (
            ("full", previous_packs),
            ("lean", previous_packs),
            ("full", "core"),
        ):
            config.settings.tool_profile = profile
            config.settings.tool_packs = packs
            async with Client(server.mcp) as client:
                surfaces.append({t.name: t.description for t in await client.list_tools()})
    finally:
        config.settings.tool_profile = previous_profile
        config.settings.tool_packs = previous_packs
        await server._apply_tool_profile(previous_profile)
    for listed in surfaces:
        assert listed
        for name, description in listed.items():
            assert description == tools[name].description, name
            assert len(description or "") <= WIRE_LIMIT, name
    assert any(name in split_tool_names() for name in surfaces[1]), "lean carries split tools"


# -- the full text ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_tool_returns_the_whole_docstring_of_every_split_tool():
    tools = _registered()
    server._apply_discovery_mode("off")
    async with Client(server.mcp) as client:
        visible = {t.name for t in await client.list_tools()}
    names = sorted(split_tool_names() & visible)
    assert names
    server._apply_discovery_mode("search")
    cards = {}
    async with Client(server.mcp) as client:
        for start in range(0, len(names), MAX_DESCRIBE_NAMES):
            chunk = names[start : start + MAX_DESCRIBE_NAMES]
            answer = await client.call_tool("describe_tool", {"names": chunk})
            cards.update({card["name"]: card for card in answer.structured_content["tools"]})
    for name in names:
        doc = _docstring(tools[name])
        assert cards[name]["description"] == doc, name
        assert len(doc) > len(tools[name].description), name
        assert DETAILS_POINTER not in doc


def test_full_description_follows_a_description_changed_after_registration(monkeypatch):
    """The 1.8-14 gate inflates a registered description: that text is what counts."""
    tool = _registered()["cad_batch"]
    assert full_description(tool) == _docstring(tool)
    monkeypatch.setattr(tool, "description", tool.description + " Inflated.")
    assert full_description(tool) == tool.description


def test_the_search_index_reads_the_full_text():
    """A word only below the split still finds its tool."""
    server._apply_discovery_mode("search")
    transform = server._discovery_transform
    tool = _registered()["drawing_finalize"]
    assert "MAX_QA_ENTITIES" not in tool.description
    assert "MAX_QA_ENTITIES" in transform._extract_searchable_text(tool)


@pytest.mark.asyncio
async def test_the_pinned_cards_are_unchanged_by_the_split():
    """1.8-11's card: the docstring's first paragraph + its pointer, as before W3."""
    tools = _registered()
    server._apply_discovery_mode("search")
    async with Client(server.mcp) as client:
        listed = {t.name: t for t in await client.list_tools()}
    for name in DEFAULT_PINNED:
        first = " ".join(re.split(r"\n\s*\n", _docstring(tools[name]).strip())[0].split())
        assert listed[name].description == f"{first} {PIN_POINTER}", name
        assert pinned_card(tools[name]).description == listed[name].description, name
    assert "cad_batch" in split_tool_names(), "the pin the split reaches"


# -- the rule ---------------------------------------------------------------------


def _paragraph(words: int, tag: str) -> str:
    return " ".join([tag] * words) + "."


def test_a_short_description_is_left_alone():
    text = "One line.\n\nA second paragraph."
    assert wire_description(text) == text


def test_an_explicit_details_marker_splits_a_short_docstring_there():
    text = "Draw a thing.\n\nWhat it returns.\n\nDetails: the refusals.\n\nMore."
    assert wire_description(text) == f"Draw a thing.\n\nWhat it returns.\n\n{DETAILS_POINTER}"


def test_a_long_description_keeps_the_leading_paragraphs_that_fit():
    paragraphs = [_paragraph(60, f"p{i}") for i in range(6)]  # ~180-240 chars each
    text = "\n\n".join(paragraphs)
    wire = wire_description(text)
    head = wire[: -len(f"\n\n{DETAILS_POINTER}")]
    assert len(text) > WIRE_DESCRIPTION_MAX >= len(head)
    kept = head.split("\n\n")
    assert kept == paragraphs[: len(kept)]
    assert len("\n\n".join(paragraphs[: len(kept) + 1])) > WIRE_DESCRIPTION_MAX


def test_the_first_paragraph_is_kept_even_when_it_alone_is_too_long():
    first, second = _paragraph(500, "a"), _paragraph(10, "b")
    assert len(first) > WIRE_DESCRIPTION_MAX
    assert wire_description(f"{first}\n\n{second}") == f"{first}\n\n{DETAILS_POINTER}"


def test_an_introducing_paragraph_and_an_indented_block_stay_together():
    lead = _paragraph(20, "lead")
    intro = "Example:"
    block = "\n".join(["    steps=[...]"] * 60)
    tail = _paragraph(200, "tail")
    wire = wire_description(f"{lead}\n\n{intro}\n\n{block}\n\n{tail}")
    assert wire == f"{lead}\n\n{DETAILS_POINTER}", "never ends on 'Example:' or splits its block"


def test_a_single_paragraph_is_never_cut():
    text = _paragraph(400, "one")
    assert len(text) > WIRE_DESCRIPTION_MAX
    assert wire_description(text) == text


def test_recording_keeps_the_full_text_off_the_wire():
    def demo() -> str:
        """Lead.

        Details: everything else."""
        return ""

    tool = Tool.from_function(fn=demo, name="w3_demo_tool")
    full = tool.description
    try:
        assert record_wire_description(tool) is True
        assert tool.description == f"Lead.\n\n{DETAILS_POINTER}"
        assert full_description(tool) == full
        wire = tool.to_mcp_tool().model_dump(by_alias=True, mode="json", exclude_none=True)
        assert "everything else" not in json.dumps(wire), "never in meta or anywhere listed"
    finally:
        descriptions._FULL.pop("w3_demo_tool", None)  # a test tool, never registered
    assert "w3_demo_tool" not in split_tool_names()


# -- the point of it ---------------------------------------------------------------


def test_the_idle_cells_stand_under_their_ceilings_on_this_tree():
    """The idle lane as ``token_suite --budget`` runs it, every idle cell, release
    cells included (``release=True``), and growth over the published report."""
    rows = asyncio.run(ts.idle_lane(ts.RatioTokenCounter()))
    published = json.loads(ts.DEFAULT_PUBLISHED.read_text(encoding="utf-8"))
    budget = {key: BUDGET[key] for key in ("growth_limit_pct", "idle")}
    checked = ts.check_budget({"idle": rows, "task": None}, budget, published, release=True)
    assert checked["ok"], checked["violations"]
    measured = {row["variant"]: row["tokens"]["value"] for row in rows}
    for name in ("default", "lean", "packs_core"):
        assert measured[name] <= BUDGET["idle"][name]["ceiling"], name
