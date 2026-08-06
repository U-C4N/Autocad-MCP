"""Tool-search transform that speaks AutoCAD.

Subclasses FastMCP's :class:`BM25SearchTransform` rather than replacing it, so
the ``call_tool`` proxy, the auth-filtered catalog accessor
(``_get_visible_tools``) and the re-entrancy bypass all come for free. Two
things are added on top, and only two:

**1. Vocabulary.** Stock BM25 indexes tool name + description + parameter names
+ parameter descriptions. Measured against this server's live catalog, that
leaves ``BPOLY``, ``PEDIT``, ``WBLOCK``, ``OVERKILL``, ``LAYTRANS``,
``MATCHPROP`` and ``QSELECT`` at df=0 — a search for any of them returns
nothing at all. :func:`discovery.aliases.alias_text` supplies the missing
AutoCAD command names and shop-floor synonyms; this module folds them into the
indexed document. (``FILLET`` is *not* in that list: it is already indexed,
because the tool is literally named ``entity_fillet``.)

**2. Two additive boosts**, so an exact alias match outranks an incidental
description word. See :data:`ACAD_MATCH_BOOST` / :data:`SYNONYM_MATCH_BOOST`.

BM25 itself is not reimplemented: the index object the base class already
built is reused, only with different documents.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastmcp.server.context import Context
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search.base import SearchResultSerializer
from fastmcp.server.transforms.search.base import (
    _extract_searchable_text as _stock_searchable_text,
)
from fastmcp.tools.base import Tool
from pydantic import Field

from discovery.aliases import alias_text, aliases_for
from discovery.serialize import serialize_search_results

__all__ = [
    "ACAD_MATCH_BOOST",
    "MAX_SEARCH_LIMIT",
    "RISK_CEILINGS",
    "SYNONYM_MATCH_BOOST",
    "CadSearchTransform",
    "SearchOutcome",
    "tool_risk",
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
#
# ``_BM25Index.query()`` hands back a *ranking*, not scores, so the two signals
# are fused on a rank scale instead of a raw-score scale: BM25's nth hit is
# worth ``1 / (1 + n)``. That makes the boosts below trivially explainable in
# the only units that matter here — positions.
#
#   ACAD_MATCH_BOOST     the query names an AutoCAD command this tool owns,
#                        *written as a command* — see _names_a_command below.
#                        Worth exactly as much as being BM25's top hit, so a
#                        command match pulls a tool past any number of tools
#                        that merely share a description word.
#   SYNONYM_MATCH_BOOST  the query contains one of the tool's synonym phrases
#                        verbatim ("bill of materials", "round the corner").
#                        Worth BM25's second position: strong, but it loses to
#                        an outright command match.
#
# That is the whole scorer. Both boosts are flat and apply at most once, so a
# tool with a long synonym list cannot out-shout a tool with a short one.
ACAD_MATCH_BOOST = 1.0
SYNONYM_MATCH_BOOST = 0.5

_WORD_RE = re.compile(r"[a-z0-9]+")
_RAW_WORD_RE = re.compile(r"[A-Za-z0-9]+")

# Dropped from both sides before phrase matching, so the synonym "delete a
# layer" still matches "delete the layer called GEOMETRY". Articles only —
# a general stop-word list would start matching phrases that mean other things.
_ARTICLES = frozenset({"a", "an", "the"})


def _words(text: str) -> list[str]:
    """Lowercase word list. Unlike BM25's tokenizer, single characters survive.

    ``_tokenize`` drops ``len <= 1`` tokens, which is right for an inverted
    index and wrong for phrase matching: dropping the "a" out of "round the
    corner" would silently change which phrases can match.
    """
    return _WORD_RE.findall(text.lower())


def _phrase_words(text: str) -> list[str]:
    return [word for word in _words(text) if word not in _ARTICLES]


def _names_a_command(query: str, command: str) -> bool:
    """True when ``query`` uses ``command`` as a command, not as English.

    Several AutoCAD command names are also ordinary nouns — LAYER, TABLE, LINE,
    POINT, TEXT, BOX, ARRAY. Boosting on a bare lowercase "layer" put
    ``layer_create`` / ``layer_modify`` above ``layer_delete`` for "delete the
    layer called GEOMETRY", which is exactly backwards. A drafter reaching for
    the command types it in capitals, or types nothing else:

      "BPOLY", "trace it with BPOLY"  -> command
      "bpoly"                         -> command (nothing else in the query)
      "delete the layer ..."          -> English; BM25 handles it
    """
    words = _words(query)
    if len(words) == 1 and words[0] == command.lower():
        return True
    return command in _RAW_WORD_RE.findall(query)


def _contains_phrase(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """True when ``needle`` appears as a contiguous run of words in ``haystack``.

    Word-level rather than substring, so the synonym "line" does not match
    "polyline" or "linear".
    """
    n = len(needle)
    if not n or n > len(haystack):
        return False
    first = needle[0]
    for i, word in enumerate(haystack):
        if word == first and list(haystack[i : i + n]) == list(needle):
            return True
    return False


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
#
# Derived from the MCP annotations each tool already carries, so there is no
# second risk table to drift out of step with the registrations. The ceiling a
# caller may ask for, most permissive first:
#
#   any    everything (default — a search must not quietly narrow itself)
#   write  hide tools that delete or overwrite (entity_delete_many, layer_delete,
#          transaction_rollback, drawing_finalize, ...)
#   read   only read-only query tools — the right ceiling for "how many ...?"
RISK_CEILINGS = ("any", "write", "read")

MAX_SEARCH_LIMIT = 25


def tool_risk(tool: Tool) -> str:
    """``"read"`` | ``"write"`` | ``"destructive"`` for one tool.

    ``destructiveHint`` wins over ``readOnlyHint``: no tool in this catalog
    sets both, and if one ever did, the safe reading is the destructive one.
    Tools that annotate neither are treated as ``"write"`` — the honest default
    for an unannotated tool on a server whose job is mutating drawings.
    """
    annotations = tool.annotations
    if annotations is not None:
        if annotations.destructiveHint:
            return "destructive"
        if annotations.readOnlyHint:
            return "read"
    return "write"


def _risk_allowed(ceiling: str, level: str) -> bool:
    if ceiling == "any":
        return True
    if ceiling == "write":
        return level != "destructive"
    return level == "read"


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """One search: the tools shown, plus what the risk ceiling took away.

    ``hidden`` holds only the tools the caller would otherwise have *seen* —
    blocked hits inside the unfiltered top-``limit`` — not every blocked tool
    in the catalog. That keeps the notice a fact about this answer.
    """

    hits: tuple[Tool, ...] = ()
    hidden: tuple[Tool, ...] = ()
    risk: str = "any"

    def notice(self) -> str:
        """One line naming what the risk ceiling removed, or ``""``.

        A filter that silently halves a mixed intent ("list the layers then
        delete GEOMETRY") is worse than no filter: the caller cannot tell the
        difference between "no such tool" and "not shown to you".
        """
        if not self.hidden:
            return ""
        names = ", ".join(tool.name for tool in self.hidden)
        return (
            f"Risk filter: {len(self.hidden)} higher-risk match(es) hidden by "
            f'risk="{self.risk}" ({names}). Re-run with risk="any" to include them.'
        )


class CadSearchTransform(BM25SearchTransform):
    """BM25 tool search with the AutoCAD alias corpus folded into the index.

    Args:
        max_results: Default number of tools returned per search.
        bm25_b: BM25 length-normalisation, left at the 0.75 textbook default —
            the value every measured number in this module was obtained with.
            Lowering it is defensible in principle (these "documents" are
            metadata records of deliberately unequal verbosity, and a tool with
            six synonyms is not a broader tool than one with two), but it was
            not what shipped, so re-measure the golden set before changing it.
    """

    def __init__(
        self,
        *,
        max_results: int = 5,
        bm25_b: float = 0.75,
        always_visible: list[str] | None = None,
        search_tool_name: str = "search_tools",
        call_tool_name: str = "call_tool",
        search_result_serializer: SearchResultSerializer | None = None,
    ) -> None:
        super().__init__(
            max_results=max_results,
            always_visible=always_visible,
            search_tool_name=search_tool_name,
            call_tool_name=call_tool_name,
            search_result_serializer=search_result_serializer or serialize_search_results,
        )
        self._bm25_k1 = self._index.k1
        self._bm25_b = bm25_b

    # ------------------------------------------------------------------
    # Indexed document
    # ------------------------------------------------------------------

    def _extract_searchable_text(self, tool: Tool) -> str:
        """Stock searchable text plus this tool's alias vocabulary.

        The narrowest available hook: upstream's ``_extract_searchable_text``
        is a module-level function, not a method, so it cannot be overridden in
        place — it is called and extended instead. Tools with no alias record
        (anything registered outside this server) keep the stock text
        unchanged.
        """
        extra = alias_text(tool.name)
        stock = _stock_searchable_text(tool)
        return f"{stock} {extra}" if extra else stock

    # ------------------------------------------------------------------
    # Index lifecycle
    # ------------------------------------------------------------------

    def _ensure_index(self, tools: Sequence[Tool]) -> None:
        """Rebuild the index when the catalog's indexed text changes.

        The hash is order-sensitive on purpose: results are mapped back to
        tools positionally, so a reordered catalog with identical text is still
        a different index.

        No lock: this and its only caller (:meth:`rank`) are synchronous and
        await nothing, so two concurrent searches on one event loop cannot
        interleave a rebuild with a query.
        """
        documents = [self._extract_searchable_text(t) for t in tools]
        digest = hashlib.sha256("\x00".join(documents).encode()).hexdigest()
        if digest == self._last_hash:
            return
        # Built via type() rather than importing the private _BM25Index name:
        # the base class already owns an instance, and reusing its class keeps
        # this from depending on a private module path.
        index = type(self._index)(self._bm25_k1, self._bm25_b)
        index.build(documents)
        self._index = index
        self._indexed_tools = tuple(tools)
        self._last_hash = digest

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    def _alias_boost(self, tool: Tool, query: str, phrase_words: Sequence[str]) -> float:
        record = aliases_for(tool.name)
        if record is None:
            return 0.0
        boost = 0.0
        if any(_names_a_command(query, command) for command in record.acad):
            boost += ACAD_MATCH_BOOST
        if any(_contains_phrase(phrase_words, _phrase_words(phrase)) for phrase in record.synonyms):
            boost += SYNONYM_MATCH_BOOST
        return boost

    def rank(
        self,
        tools: Sequence[Tool],
        query: str,
        *,
        limit: int | None = None,
        risk: str = "any",
    ) -> SearchOutcome:
        """Rank ``tools`` against ``query``. Pure — no Context, no I/O."""
        if risk not in RISK_CEILINGS:
            raise ValueError(f"risk must be one of {RISK_CEILINGS}, got {risk!r}")
        cutoff = self._max_results if limit is None else max(1, min(limit, MAX_SEARCH_LIMIT))
        tools = list(tools)
        if not tools:
            return SearchOutcome(risk=risk)
        self._ensure_index(tools)

        ranked = self._index.query(query, len(tools))
        bm25_points = {position: 1.0 / (1.0 + rank) for rank, position in enumerate(ranked)}
        phrase_words = _phrase_words(query)

        scored: list[tuple[float, str, Tool]] = []
        for position, tool in enumerate(tools):
            score = bm25_points.get(position, 0.0) + self._alias_boost(tool, query, phrase_words)
            if score > 0.0:
                scored.append((score, tool.name, tool))
        scored.sort(key=lambda row: (-row[0], row[1]))
        matches = [tool for _, _, tool in scored]

        # Filter before truncating, so a blocked hit frees its slot for the
        # next safe tool instead of just shortening the answer.
        allowed = [tool for tool in matches if _risk_allowed(risk, tool_risk(tool))]
        hidden = tuple(
            tool for tool in matches[:cutoff] if not _risk_allowed(risk, tool_risk(tool))
        )
        return SearchOutcome(hits=tuple(allowed[:cutoff]), hidden=hidden, risk=risk)

    async def _search(self, tools: Sequence[Tool], query: str) -> Sequence[Tool]:
        return list(self.rank(tools, query).hits)

    # ------------------------------------------------------------------
    # The synthetic search tool
    # ------------------------------------------------------------------

    async def render(self, outcome: SearchOutcome) -> Any:
        """Serialize an outcome, keeping the risk notice on its own line.

        With the default (string) serializer the notice is appended as a
        trailing line. A serializer that returns structured data instead gets
        the notice as a sibling key, because there is no line to append to.
        """
        rendered = await self._render_results(outcome.hits)
        notice = outcome.notice()
        if not notice:
            return rendered
        if isinstance(rendered, str):
            return f"{rendered}\n\n{notice}"
        return {"results": rendered, "notice": notice}

    def _make_search_tool(self) -> Tool:
        transform = self
        default_limit = self._max_results

        async def search_tools(
            query: Annotated[
                str,
                Field(
                    description=(
                        "What you want to do, in plain English or as an AutoCAD "
                        "command name (FILLET, BPOLY, QSELECT, DLI)."
                    )
                ),
            ],
            limit: Annotated[
                int,
                Field(description=(f"How many tools to return. Clamped to 1..{MAX_SEARCH_LIMIT}.")),
            ] = default_limit,
            risk: Annotated[
                Literal["any", "write", "read"],
                Field(
                    description=(
                        "Highest risk level to return. 'any' (default) is everything; "
                        "'write' hides tools that delete or overwrite; 'read' returns "
                        "only read-only query tools — use it for questions like 'how "
                        "many ...?'. Anything filtered out is reported in the result."
                    )
                ),
            ] = "any",
            ctx: Context = None,  # type: ignore[assignment]
        ) -> Any:
            """Find the AutoCAD tools that do a job.

            Accepts AutoCAD command names as well as English. Returns one
            compact line per tool: name, risk level, what it does, its required
            arguments and the AutoCAD command it corresponds to. Call a result
            with call_tool.
            """
            visible = await transform._get_visible_tools(ctx)
            outcome = transform.rank(visible, query, limit=limit, risk=risk)
            return await transform.render(outcome)

        return Tool.from_function(fn=search_tools, name=self._search_tool_name)
