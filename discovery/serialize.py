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

:func:`describe_payload` is the other half of that trade: ``describe_tool``
renders one tool's whole card — full input schema included — so a search-mode
client asks for the optional arguments of the one tool it is about to call
instead of paying for every hit's schema up front.

Scope note: the compact payload is a real saving but it is **not** what makes
this discovery layer better than stock. Stock BM25 with this same serializer
gets the same size win. The differentiators are the alias corpus (which lets a
query find anything at all) and the risk filter — not bytes.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Sequence
from typing import Any

from fastmcp.tools.base import Tool

from discovery.aliases import aliases_for

__all__ = [
    "MAX_SUMMARY_CHARS",
    "describe_payload",
    "docstring_examples",
    "serialize_search_results",
]

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


# ---------------------------------------------------------------------------
# describe_tool: one tool's whole card
# ---------------------------------------------------------------------------

# "Example:" / "Examples:" as a label, at a line start or after a sentence —
# capitalised and followed by one colon. "for example, ..." is prose, and
# "Example::" is a literal-block marker the "::" rule below handles.
_EXAMPLE_LABEL_RE = re.compile(r"(?<![A-Za-z])Examples?:(?!:)[ \t]*(.*)$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _indented_block(lines: Sequence[str], start: int, base: int) -> tuple[str, int]:
    """The block indented deeper than ``base`` from ``start``, dedented.

    Leading blank lines are skipped, interior blank lines kept, trailing ones
    dropped. Returns ``("", start)`` when nothing is indented there, so the
    caller does not swallow the next paragraph.
    """
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    body: list[str] = []
    while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > base):
        body.append(lines[i])
        i += 1
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return "", start
    return textwrap.dedent("\n".join(body)), i


def docstring_examples(text: str | None) -> list[str]:
    """The example blocks a docstring writes itself, verbatim — never invented.

    Three forms are read, the three this catalog uses:

    * an inline label — ``Example: points=[[0,0],[100,0]]`` — gives the rest
      of its line;
    * a bare ``Example:`` / ``Examples:`` heading gives the indented block
      below it;
    * a reStructuredText literal block (a paragraph ending in ``::``) gives
      the indented block that follows, dedented.

    A docstring with none of them yields ``[]``.
    """
    if not text:
        return []
    lines = text.splitlines()
    examples: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        label = _EXAMPLE_LABEL_RE.search(line)
        if label:
            rest = label.group(1).strip()
            if rest:
                examples.append(rest)
                i += 1
                continue
            block, i = _indented_block(lines, i + 1, _indent(line))
            if block:
                examples.append(block)
            else:
                i += 1
            continue
        if line.rstrip().endswith("::"):
            block, i = _indented_block(lines, i + 1, _indent(line))
            if block:
                examples.append(block)
            else:
                i += 1
            continue
        i += 1
    return examples


def describe_payload(tool: Tool, *, pack: str | None = None) -> dict[str, Any]:
    """Everything a client needs to call ``tool``, from what it already carries.

    ``input_schema``, ``description`` and ``annotations`` are the tool's own MCP
    rendering — the same object a full-catalog ``list_tools`` sends — so the
    schema cannot drift from what the call validates against. ``cost`` is the
    ``@cad_tool`` card's, ``acad`` / ``synonyms`` the alias corpus's, and
    ``examples`` only what the docstring itself writes (:func:`docstring_examples`).
    """
    wire = tool.to_mcp_tool()
    cad = (tool.meta or {}).get("cad")
    record = aliases_for(tool.name)
    return {
        "name": tool.name,
        "description": wire.description,
        "input_schema": wire.inputSchema,
        "annotations": (
            wire.annotations.model_dump(mode="json", exclude_none=True)
            if wire.annotations is not None
            else {}
        ),
        "risk": _risk(tool),
        "cost": cad.get("cost") if isinstance(cad, dict) else None,
        "pack": pack,
        "acad": list(record.acad) if record else [],
        "synonyms": list(record.synonyms) if record else [],
        "examples": docstring_examples(tool.description),
    }
