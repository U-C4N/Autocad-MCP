"""Gates for the AutoCAD command / synonym alias corpus (``discovery.aliases``).

FastMCP's ``BM25SearchTransform`` indexes only tool name + description +
parameter names + parameter descriptions (see
``fastmcp/server/transforms/search/base.py::_extract_searchable_text``).
AutoCAD command names appear in none of those for this server, so a drafter
searching for ``BPOLY`` or ``QSELECT`` scores df=0 and gets nothing back.

These tests both *prove* that gap is real against the live registry and lock the
corpus that closes it to the live tool list, so the corpus can never silently
drift away from the registered tools.
"""

from __future__ import annotations

import re

import pytest

import server
from discovery.aliases import (
    SHARED_ACAD_COMMANDS,
    TOOL_ALIASES,
    ToolAliases,
    alias_text,
    aliases_for,
)

# AutoCAD command tokens: uppercase alphanumerics, no spaces, at least two
# characters (single-letter aliases like "L"/"C" are deliberately excluded --
# they are pure BM25 noise and collide with ordinary prose).
_ACAD_RE = re.compile(r"^[A-Z][A-Z0-9]+$")

# Commands proven below to be absent from every live tool's searchable text.
#
# Deliberately NOT in this list, and why:
#   FILLET   -- already present: the tool is literally named ``entity_fillet``.
#   XCLIP    -- the server has no xref/clipping support at all; any mapping
#               would misroute the search, so the corpus honestly omits it.
#   REVCLOUD -- no revision-cloud tool exists; a revcloud would have to be
#               hand-built from arcs, so no single tool is the right answer.
#: AutoCAD commands that appear nowhere in the live tool text, so a
#: description-only ranker scores them df=0 and returns nothing.
#:
#: BPOLY and QSELECT used to be here and were removed in v1.5.0 (M8), when
#: `boundary_trace` and `selection_filter` landed and named those commands in
#: their own docstrings. The alias corpus is no longer the *only* way to reach
#: them, and saying otherwise would overstate what the corpus is worth. Two
#: commands moving out of this list is the honest cost of the drawing tools
#: growing to cover them — the corpus still carries the rest.
DF0_COMMANDS = (
    "PEDIT",
    "WBLOCK",
    "OVERKILL",
    "LAYTRANS",
    "MATCHPROP",
)


async def _registered_tools() -> list:
    return [t for t in await server._registered_tools() if getattr(t, "name", None)]


async def _registered_names() -> set[str]:
    return {t.name for t in await _registered_tools()}


def _searchable_text(tool) -> str:
    """Mirror of fastmcp's ``_extract_searchable_text`` -- what BM25 indexes."""
    parts = [tool.name]
    if tool.description:
        parts.append(tool.description)
    schema = getattr(tool, "parameters", None) or {}
    for param_name, param_info in (schema.get("properties") or {}).items():
        parts.append(param_name)
        if isinstance(param_info, dict) and param_info.get("description"):
            parts.append(str(param_info["description"]))
    return " ".join(parts).lower()


def _acad_owners() -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    for tool_name, record in TOOL_ALIASES.items():
        for command in record.acad:
            owners.setdefault(command, []).append(tool_name)
    for names in owners.values():
        names.sort()
    return owners


# ── coverage ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_registered_tool_has_an_alias_record():
    """Adding a tool without an alias record must fail this gate."""
    missing = sorted(await _registered_names() - set(TOOL_ALIASES))
    assert not missing, f"tools with no alias record: {missing}"


@pytest.mark.asyncio
async def test_no_alias_key_is_an_unknown_tool():
    """Catches typos and tools that were renamed or removed."""
    unknown = sorted(set(TOOL_ALIASES) - await _registered_names())
    assert not unknown, f"alias keys that are not registered tools: {unknown}"


def test_every_record_is_a_tool_aliases_instance():
    bad = sorted(k for k, v in TOOL_ALIASES.items() if not isinstance(v, ToolAliases))
    assert not bad, f"records that are not ToolAliases: {bad}"


# ── shape ───────────────────────────────────────────────────────────────────


def test_acad_commands_are_uppercase_and_space_free():
    bad: list[str] = []
    for tool_name, record in TOOL_ALIASES.items():
        for command in record.acad:
            if not _ACAD_RE.fullmatch(command):
                bad.append(f"{tool_name}:{command!r}")
    assert not bad, f"malformed AutoCAD command tokens: {bad}"


def test_acad_commands_are_unique_within_a_record():
    bad = sorted(k for k, v in TOOL_ALIASES.items() if len(set(v.acad)) != len(v.acad))
    assert not bad, f"records with a repeated AutoCAD command: {bad}"


def test_synonyms_are_lowercase_non_empty_and_trimmed():
    bad: list[str] = []
    for tool_name, record in TOOL_ALIASES.items():
        for phrase in record.synonyms:
            if not phrase or phrase != phrase.lower() or phrase != phrase.strip():
                bad.append(f"{tool_name}:{phrase!r}")
    assert not bad, f"malformed synonyms: {bad}"


def test_every_tool_has_at_least_one_synonym():
    """``acad`` may legitimately be empty; a record with nothing in it is dead weight."""
    empty = sorted(k for k, v in TOOL_ALIASES.items() if not v.synonyms)
    assert not empty, f"records with no synonyms: {empty}"


def test_synonyms_are_unique_within_a_record():
    bad = sorted(k for k, v in TOOL_ALIASES.items() if len(set(v.synonyms)) != len(v.synonyms))
    assert not bad, f"records with a repeated synonym: {bad}"


# ── uniqueness ──────────────────────────────────────────────────────────────
#
# Rule: one AutoCAD command routes to exactly one tool. The only exceptions are
# the commands in ``SHARED_ACAD_COMMANDS`` -- option/dialog-driven umbrella
# commands (LAYER, ZOOM, ARRAY, LAYOUT, MEASUREGEOM, SETVAR) and the two places
# where this server genuinely exposes one operation through two tools (INSERT ->
# block_insert / entity_create_block_ref, ERASE -> entity_delete /
# entity_delete_many). Anything else is a copy-paste bug.


def test_each_acad_command_routes_to_a_single_tool():
    unexpected = {
        command: names
        for command, names in _acad_owners().items()
        if len(names) > 1 and command not in SHARED_ACAD_COMMANDS
    }
    assert not unexpected, f"AutoCAD command claimed by several tools: {unexpected}"


def test_shared_command_allowlist_has_no_stale_entries():
    actually_shared = {c for c, names in _acad_owners().items() if len(names) > 1}
    stale = sorted(SHARED_ACAD_COMMANDS - actually_shared)
    assert not stale, f"SHARED_ACAD_COMMANDS lists commands that are not shared: {stale}"


# ── the df=0 regression guard ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_key_autocad_commands_are_absent_from_live_tool_text():
    """Proves the gap: these commands score df=0 against the live BM25 corpus."""
    tools = await _registered_tools()
    hits = {
        command: sorted(t.name for t in tools if command.lower() in _searchable_text(t))
        for command in DF0_COMMANDS
    }
    leaked = {c: names for c, names in hits.items() if names}
    assert not leaked, f"command already indexed -- drop it from DF0_COMMANDS: {leaked}"


def test_key_autocad_commands_are_covered_by_the_corpus():
    """...and proves the corpus closes it."""
    owners = _acad_owners()
    missing = sorted(c for c in DF0_COMMANDS if c not in owners)
    assert not missing, f"df=0 commands still unreachable via the corpus: {missing}"


# ── helpers ─────────────────────────────────────────────────────────────────


def test_aliases_for_returns_the_record_or_none():
    assert aliases_for("entity_fillet") is TOOL_ALIASES["entity_fillet"]
    assert aliases_for("no_such_tool") is None


def test_alias_text_contains_commands_and_synonyms():
    record = TOOL_ALIASES["entity_create_circle"]
    text = alias_text("entity_create_circle")
    for token in (*record.acad, *record.synonyms):
        assert token in text
    assert alias_text("no_such_tool") == ""
