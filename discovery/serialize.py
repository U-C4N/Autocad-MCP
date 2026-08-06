"""Compact one-line cards for tool-search results.

Passed to a search transform as ``search_result_serializer``. FastMCP's default
(``serialize_tools_for_output_json``) re-emits each hit's full ``list_tools``
entry, JSON Schema and all — for this server that is roughly 1.5 kB per hit,
and a five-hit answer costs more than the crowded catalog the search was meant
to replace.

One line per hit::

    entity_fillet (write) | Round a corner between two entities. | req: handle_a, handle_b, radius | acad: FILLET, F

``req:`` lists *required* parameters only. Optional parameters and types are
deliberately absent — the honest cost of this format is that a caller must
either already know the optional arguments or read the error from a failed
call. That trade is worth it at five hits per search; it would not be at one.

Scope note: the compact payload is a real saving but it is **not** what makes
this discovery layer better than stock. Stock BM25 with this same serializer
gets the same size win. The differentiators are the alias corpus (which lets a
query find anything at all) and the risk filter — not bytes.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastmcp.tools.base import Tool

from discovery.aliases import aliases_for

__all__ = ["MAX_SUMMARY_CHARS", "serialize_search_results"]

# Long enough for every first docstring line in the live catalog (measured max
# 96 characters), short enough that a rambling one cannot dominate the payload.
MAX_SUMMARY_CHARS = 110

# Beyond three, AutoCAD command names stop disambiguating and start padding.
MAX_ACAD_COMMANDS = 3

EMPTY_RESULT = "No tools matched the query."


def _risk(tool: Tool) -> str:
    """Same derivation as ``discovery.transform.tool_risk``, inlined.

    Imported the other way round would make ``discovery.serialize`` depend on
    the transform, and the transform already depends on this module for its
    default serializer.
    """
    annotations = tool.annotations
    if annotations is not None:
        if annotations.destructiveHint:
            return "destructive"
        if annotations.readOnlyHint:
            return "read"
    return "write"


def _truncate(text: str) -> str:
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    clipped = text[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return f"{clipped}..."


def _summary(tool: Tool) -> str:
    """One line describing the tool.

    Prefers the hand-written ``@cad_tool`` summary in the MCP ``meta`` channel;
    falls back to the first line of the docstring, which for this server is
    already written as a standalone sentence.
    """
    cad = (tool.meta or {}).get("cad")
    if isinstance(cad, dict) and cad.get("summary"):
        return _truncate(str(cad["summary"]).strip())
    for line in (tool.description or "").strip().splitlines():
        if line.strip():
            return _truncate(line.strip())
    return "(no description)"


def _required(tool: Tool) -> str:
    schema = tool.parameters or {}
    required = schema.get("required")
    if not isinstance(required, list) or not required:
        return "(none)"
    return ", ".join(str(name) for name in required)


def _acad(tool: Tool) -> str:
    record = aliases_for(tool.name)
    if record is None or not record.acad:
        return ""
    return ", ".join(record.acad[:MAX_ACAD_COMMANDS])


def _card(tool: Tool) -> str:
    parts = [
        f"{tool.name} ({_risk(tool)})",
        _summary(tool),
        f"req: {_required(tool)}",
    ]
    acad = _acad(tool)
    if acad:
        parts.append(f"acad: {acad}")
    return " | ".join(parts)


def serialize_search_results(tools: Sequence[Tool]) -> str:
    """Render search hits as one compact card per line."""
    if not tools:
        return EMPTY_RESULT
    return "\n".join(_card(tool) for tool in tools)
