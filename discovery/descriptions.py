"""The wire description of a tool, and the full text kept beside it (1.8.0, W3).

Every tools/list entry carries its tool's description, and a client forwards it
to the model on every uncached request: the idle cost the token budget holds
(``benchmarks/token_budget.json``, plan section 4). A docstring is written for
the reader who is about to call the tool - refusals, units, enums, edge cases -
and the longest ones run to several thousand characters, most of which a model
choosing among 250 tools never needs.

So at registration (``server.cad_tool``, the one place every tool passes
through) a description over :data:`WIRE_DESCRIPTION_MAX` characters is listed
as its leading *whole paragraphs* that fit within that limit, followed by
:data:`DETAILS_POINTER`; a paragraph that starts with ``Details:`` marks the
split explicitly, even in a shorter docstring. Never a cut inside a paragraph,
so never inside a sentence; at least the first paragraph is always kept, even
when it alone is longer. A paragraph ending in ``:`` stays with the one it
introduces, and an indented block stays with the paragraph above it.

The full text is kept here, keyed by tool name - never in ``meta``, which goes
on the wire - and :func:`full_description` is how every reader that wants it
gets it: ``describe_tool`` (``discovery.serialize.describe_payload``), the
search-mode pinned card, the BM25 index. The listing a client receives - the
full catalog, the lean profile and every ``TOOL_PACKS`` surface - carries the
wire text, and ``describe_tool`` is listed beside it in both discovery modes
(``discovery.transform.DetailsTransform`` in ``off``), so the pointer always
names a tool the client can call. Nothing a call is validated against
changes: the input schema is untouched.

The split happens once, at registration, on purpose: a description changed
*after* registration (a test inflating one to prove the token gate) is what
gets listed and what :func:`full_description` returns, so the gate still sees
it.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "DETAILS_MARKER",
    "DETAILS_POINTER",
    "WIRE_DESCRIPTION_MAX",
    "full_description",
    "record_wire_description",
    "split_tool_names",
    "wire_description",
]

#: A description longer than this is listed as its leading whole paragraphs
#: within it (plan section 4's lever for the idle cells: "a Details: split of
#: docstrings over 800 characters, only for the excess").
WIRE_DESCRIPTION_MAX = 800

#: Appended, as a paragraph of its own, to a description that was split - so
#: the cut is never silent and the reader knows where the rest is. Every
#: surface that lists such a description lists ``describe_tool`` too.
DETAILS_POINTER = "Details: describe_tool."

#: A paragraph that starts with this marks the split explicitly.
DETAILS_MARKER = "Details:"

# One or more blank lines; the next paragraph starts right after, with its own
# indentation intact (an indented block is a continuation, never a split point).
_PARAGRAPH_BREAK = re.compile(r"\n(?:[ \t]*\n)+")

# name -> (wire text, full text). Keyed by name rather than stored on the
# component: a FastMCP component is a pydantic model whose every field can
# reach the wire, and ``meta`` does.
_FULL: dict[str, tuple[str, str]] = {}


def _paragraphs(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every non-blank paragraph of ``text``."""
    spans: list[tuple[int, int]] = []
    position = 0
    for found in _PARAGRAPH_BREAK.finditer(text):
        if text[position : found.start()].strip():
            spans.append((position, found.start()))
        position = found.end()
    if text[position:].strip():
        spans.append((position, len(text)))
    return spans


def _can_cut_after(text: str, spans: list[tuple[int, int]], index: int) -> bool:
    """True when the wire text may end after paragraph ``index``."""
    if index + 1 >= len(spans):
        return False  # nothing after it: no split at all
    here = text[spans[index][0] : spans[index][1]].rstrip()
    after = text[spans[index + 1][0] : spans[index + 1][1]]
    if here.endswith((":", ",", ";")):
        return False  # it introduces (or runs into) what follows
    if after[:1].isspace():
        return False  # an indented block belongs to the paragraph above it
    return True


def _cut(text: str) -> int | None:
    """Where the wire text ends, or ``None`` when the whole text is listed."""
    spans = _paragraphs(text)
    marker = next(
        (i for i, (start, _) in enumerate(spans) if i and text.startswith(DETAILS_MARKER, start)),
        None,
    )
    if marker is None and len(text.strip()) <= WIRE_DESCRIPTION_MAX:
        return None
    last = marker if marker is not None else len(spans)
    candidates = [
        k
        for k in range(1, last + 1)
        if (marker is not None and k == marker) or _can_cut_after(text, spans, k - 1)
    ]
    if not candidates:
        return None
    within = [k for k in candidates if len(text[: spans[k - 1][1]].strip()) <= WIRE_DESCRIPTION_MAX]
    k = max(within) if within else min(candidates)
    return spans[k - 1][1]


def wire_description(text: str | None) -> str | None:
    """``text`` as a tools/list entry lists it: whole leading paragraphs + pointer.

    Returns ``text`` unchanged when it needs no split.
    """
    if not text:
        return text
    cut = _cut(text)
    if cut is None:
        return text
    return f"{text[:cut].rstrip()}\n\n{DETAILS_POINTER}"


def record_wire_description(component: Any) -> bool:
    """Give a just-registered tool its wire description; keep the full text here.

    Returns whether the description was split. Called once per tool, from
    ``server.cad_tool``.
    """
    full = component.description
    wire = wire_description(full)
    if not full or wire == full:
        _FULL.pop(component.name, None)
        return False
    _FULL[component.name] = (wire, full)
    component.description = wire
    return True


def full_description(tool: Any) -> str:
    """The tool's whole description, as its docstring wrote it.

    The recorded full text while the tool still lists the wire text recorded
    for it; otherwise (a tool this module never split, or a description
    changed after registration) the tool's own description.
    """
    description = getattr(tool, "description", None) or ""
    recorded = _FULL.get(getattr(tool, "name", None) or "")
    if recorded is not None and recorded[0] == description:
        return recorded[1]
    return description


def split_tool_names() -> frozenset[str]:
    """Names of the tools whose listed description is a split one."""
    return frozenset(_FULL)
