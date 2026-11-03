"""The server's ``instructions`` (1.8-11): one short text per advertised surface.

A client puts the instructions into the model's system prompt beside the tools
array, so they are an idle cost on every turn (``benchmarks/token_suite.py``'s
``instructions`` row, ceiling 250 tokens) and they are the first thing that
teaches the model how to use this server. Up to 1.7.0 they taught one call per
entity (a workflow of ``entity_create_*`` calls), never mentioned batching, and
named the stale ``titleblock_apply_iso_a3``.

The text is built from the advertised surface and nothing else:

* every tool it names is advertised under that surface - a line whose tools
  are all hidden (``TOOL_PROFILE=lean``, ``TOOL_PACKS=core``) is dropped, and a
  line's list keeps only the advertised ones (``tests/test_server_instructions.py``);
* in search mode it names only the synthetic tools and the pinned ones the
  surface lists, never a tool the client cannot see;
* it carries nothing session- or time-specific, so the same configuration
  always produces the same bytes (a client's prompt-cache prefix).

Pure: the advertised names are handed in, nothing here imports the server.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence

__all__ = ["SYNTHETIC_TOOLS", "build_instructions", "named_tools"]

#: The search-mode surface (``discovery/transform.py``).
SYNTHETIC_TOOLS: tuple[str, ...] = ("search_tools", "describe_tool", "call_tool")

_HEAD = "AutoCAD MCP Pro drives AutoCAD live (COM) or edits DXF files headless (ezdxf)."
_BATCH = "Batch first; never one call per entity:"
_SAFETY = (
    "Edit safely: a dry run returns a preview; pass its revision as expected_revision.",
    "Errors are typed: read kind and hint, fix the call, never retry it unchanged.",
)

#: ``(tools, line)``: the line is kept when any of ``tools`` is advertised;
#: ``{tools}`` in it is replaced by the advertised ones, joined.
_BATCH_LINES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("cad_batch",),
        "- cad_batch runs many tool calls in one request; bind a step and use it as "
        "$name or $name.field.",
    ),
    (("entity_batch_create",), "- entity_batch_create draws many entities in one call."),
    (
        ("entity_modify_where",),
        "- entity_modify_where changes every entity a filter selects (layer, colour, "
        "lineweight).",
    ),
    (("dimension_auto",), "- dimension_auto dimensions a whole part."),
    (
        ("mech_part_from_spec", "pid_from_spec", "arch_plan_from_spec"),
        "- {tools} build a part, P&ID or plan from one spec.",
    ),
)
_READ_LINE = (
    ("entity_list",),
    "Read narrowly: {tools} takes fields=[...] and compact=true.",
)
_FINISH_LINE = (
    ("mech_part_draw", "titleblock_apply", "drawing_finalize"),
    "Engineering output: {tools}; never hand-draw what a tool draws.",
)


def _line(tools: Sequence[str], text: str, advertised: Collection[str] | None) -> str | None:
    shown = [name for name in tools if advertised is None or name in advertised]
    if not shown:
        return None
    return text.replace("{tools}", ", ".join(shown))


def build_instructions(
    mode: str,
    advertised: Collection[str] | None = None,
    pinned: Iterable[str] = (),
) -> str:
    """The instructions for one surface.

    ``mode`` is the applied ``DISCOVERY_MODE`` (``off`` | ``search``);
    ``advertised`` the tool names the profile and packs leave visible (``None``
    before any tool is registered: nothing is filtered); ``pinned`` the
    ``DISCOVERY_PINNED`` names, listed in search mode only when advertised.
    """
    lines = [_HEAD]
    if mode == "search":
        pins = [name for name in pinned if advertised is None or name in advertised]
        lines.append(
            "Find tools with search_tools (English or an AutoCAD command), read schemas "
            "with describe_tool (names=[...] for several) and run them with call_tool."
        )
        if pins:
            lines.append(f"Listed directly: {', '.join(pins)}.")
        lines.append(
            f"{_BATCH} search for the batch, set-edit (modify where), auto-dimension and "
            "from-spec tools."
        )
        lines.append("Read narrowly: list tools take fields=[...] and compact=true.")
        lines.extend(_SAFETY)
        return "\n".join(lines)

    batch = [
        line
        for tools, text in _BATCH_LINES
        if (line := _line(tools, text, advertised)) is not None
    ]
    if batch:
        lines.append(_BATCH)
        lines.extend(batch)
    if (line := _line(*_READ_LINE, advertised)) is not None:
        lines.append(line)
    lines.extend(_SAFETY)
    if (line := _line(*_FINISH_LINE, advertised)) is not None:
        lines.append(line)
    return "\n".join(lines)


def named_tools(text: str, candidates: Iterable[str]) -> set[str]:
    """Which of ``candidates`` (tool names) ``text`` names, as whole words."""
    words = set(re.findall(r"[A-Za-z0-9_]+", text))
    return {name for name in candidates if name in words}
