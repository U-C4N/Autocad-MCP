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
from discovery.descriptions import full_description

__all__ = [
    "MAX_SUMMARY_CHARS",
    "PIN_PARAM_CHARS",
    "describe_payload",
    "docstring_examples",
    "lean_schema",
    "pinned_card",
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

    ``input_schema`` and ``annotations`` are the tool's own MCP rendering — the
    same object a full-catalog ``list_tools`` sends — so the schema cannot drift
    from what the call validates against. ``description`` is the *full* text
    (:func:`discovery.descriptions.full_description`): a listing carries only
    the leading paragraphs of a long docstring and points here for the rest.
    ``cost`` is the ``@cad_tool`` card's, ``acad`` / ``synonyms`` the alias
    corpus's, and ``examples`` only what the docstring itself writes
    (:func:`docstring_examples`).
    """
    wire = tool.to_mcp_tool()
    cad = (tool.meta or {}).get("cad")
    record = aliases_for(tool.name)
    full = full_description(tool)
    return {
        "name": tool.name,
        "description": full or wire.description,
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
        "examples": docstring_examples(full),
    }


# ---------------------------------------------------------------------------
# Search mode's advertised cards: pinned tools and the synthetic three
# ---------------------------------------------------------------------------
#
# A pinned tool (DISCOVERY_PINNED, 1.8-11) stays listed beside the synthetic
# tools so the commonest calls never cost a search turn. Listed with its full
# docstring it cost what the search mode exists to save: spike S1 measured the
# three default pins at ~2,230 tokens, cad_batch alone ~1,360. The listed card
# keeps everything a call is validated against - every property, its type,
# enum, default and bounds, the required list - and shortens only the prose:
# the description to its first paragraph, each parameter description to whole
# sentences within PIN_PARAM_CHARS. describe_tool still returns the untouched
# description and schema (it reads the catalog, not this listing), and the
# call itself always reaches the registered tool.

#: A pinned card's parameter description is cut back to whole sentences within
#: this many characters (at least the first sentence is always kept).
PIN_PARAM_CHARS = 250

#: Appended to a pinned card's description, so the cut is never silent.
PIN_POINTER = "Full description: describe_tool."

# A sentence ends at . ; ! or ? followed by whitespace, unless the next word
# starts in lower case (an abbreviation, "e.g. x", "1.5 mm").
_SENTENCE_END = re.compile(r"(?<=[.;!?])\s+(?=[^a-z\s])")


def _sentences_within(text: str, limit: int) -> str:
    """Whole leading sentences of ``text`` within ``limit`` characters (>= one)."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    kept = ""
    for sentence in _SENTENCE_END.split(flat):
        candidate = f"{kept} {sentence}".strip()
        if kept and len(candidate) > limit:
            break
        kept = candidate
    return kept


def _first_paragraph(text: str | None) -> str:
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        if block.strip():
            return " ".join(block.split())
    return ""


def pinned_card(tool: Tool) -> Tool:
    """``tool`` as search mode lists a pinned tool: same schema, shorter prose.

    Never changes what a call is validated against: the copy keeps every key
    of the input schema except over-long ``description`` strings, and it is
    only ever *listed* - a call resolves the registered tool.
    """
    schema = _copy_schema(tool.parameters or {})
    for prop in (schema.get("properties") or {}).values():
        if isinstance(prop, dict) and isinstance(prop.get("description"), str):
            prop["description"] = _sentences_within(prop["description"], PIN_PARAM_CHARS)
    lead = _first_paragraph(full_description(tool)) or _summary(tool)
    return tool.model_copy(update={"description": f"{lead} {PIN_POINTER}", "parameters": schema})


def _copy_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy_schema(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_schema(item) for item in value]
    return value


def lean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A synthetic tool's input schema without the wire noise pydantic adds.

    ``additionalProperties: false`` and the ``anyOf [X, null]`` an optional
    argument becomes are dropped (the default stays): the function's own
    validation still refuses an unknown or ill-typed argument, so the
    advertised schema loses no constraint a caller can rely on, only bytes.
    """
    lean = _copy_schema(schema)
    lean.pop("additionalProperties", None)
    for name, prop in list((lean.get("properties") or {}).items()):
        options = prop.get("anyOf") if isinstance(prop, dict) else None
        if isinstance(options, list) and len(options) == 2:
            kept = [o for o in options if o != {"type": "null"}]
            if len(kept) == 1:
                merged = {key: value for key, value in prop.items() if key != "anyOf"}
                merged.update(kept[0])
                merged.pop("additionalProperties", None)
                lean["properties"][name] = merged
    return lean
