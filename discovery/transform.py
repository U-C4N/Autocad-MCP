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

**A third synthetic tool, ``describe_tool``.** A search card lists required
parameters only (``discovery/serialize.py`` says why), so a search-mode client
had no way to read a tool's optional arguments short of a failed call.
``describe_tool(name)`` returns one tool's full input schema, description,
annotations, risk, cost, pack, AutoCAD commands, synonyms and its docstring's
own examples; ``describe_tool(names=[...])`` returns several in one turn
(1.8-11). It resolves names against the same filtered catalog
``call_tool`` reaches, refuses a hidden or unknown name with the same
``Unknown tool`` refusal, and is itself refused by ``call_tool`` (and, being
card-less, by ``cad_batch``) like the other synthetic tools.

**``describe_tool`` in ``off`` mode too.** A description over 800 characters
is listed as its leading paragraphs and ``Details: describe_tool.``
(``discovery/descriptions.py``) on every surface, so :class:`DetailsTransform`
lists the whole catalog unchanged plus the same ``describe_tool`` - the
pointer leads somewhere in both modes.

**Built once.** The base class builds its synthetic tools on every listing
(``Tool.from_function``: a pydantic schema each). A listing is not rare: the
low-level MCP server lists again whenever a call names a tool its definition
cache has not seen, and under the search default that is every tool but the
pinned ones (``server._install_tool_definition_cache`` says what it does
about that). MEASURED 2026-11-05 (``benchmarks/perf_compare.py``, release
tree 19ada05): 2,000 direct ``entity_create_line`` calls took 8.5 x their
v1.7.0 time, six listings a call, three ``Tool.from_function`` per listing.
So the three synthetic tools and each pinned card are built once per
transform and handed out again (``tests/test_discovery_perf.py``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastmcp.exceptions import NotFoundError
from fastmcp.server.context import Context
from fastmcp.server.transforms import GetToolNext
from fastmcp.server.transforms.catalog import CatalogTransform
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search.base import SearchResultSerializer
from fastmcp.server.transforms.search.base import (
    _extract_searchable_text as _stock_searchable_text,
)
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.utilities.versions import VersionSpec
from mcp.types import ToolAnnotations
from pydantic import Field

from discovery.aliases import alias_text, aliases_for
from discovery.descriptions import DETAILS_POINTER, full_description
from discovery.serialize import (
    describe_payload,
    lean_schema,
    pinned_card,
    serialize_search_results,
)

__all__ = [
    "ACAD_MATCH_BOOST",
    "DEFAULT_PINNED",
    "MAX_DESCRIBE_NAMES",
    "MAX_SEARCH_LIMIT",
    "RISK_CEILINGS",
    "SYNONYM_MATCH_BOOST",
    "CadSearchTransform",
    "DetailsTransform",
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

#: The most tools one ``describe_tool(names=[...])`` call answers: the same
#: ceiling as one search, so "describe every hit" is always one call.
MAX_DESCRIBE_NAMES = MAX_SEARCH_LIMIT

#: DISCOVERY_PINNED's default (1.8-11): the batch executor and the two reads
#: nearly every session starts with stay listed beside the synthetic tools, so
#: they never cost a search turn. Pinned tools are left out of search results.
DEFAULT_PINNED: tuple[str, ...] = ("cad_batch", "drawing_info", "entity_list")


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
        describe_tool_name: Name of the synthetic one-tool card.
        pack_of: Maps a tool name to its ``TOOL_PACKS`` pack for
            ``describe_tool``. The pack table lives in ``server.py``, which
            imports this module, so it is handed in rather than imported;
            without it ``pack`` is ``None``.
    """

    def __init__(
        self,
        *,
        max_results: int = 5,
        bm25_b: float = 0.75,
        always_visible: list[str] | None = None,
        search_tool_name: str = "search_tools",
        call_tool_name: str = "call_tool",
        describe_tool_name: str = "describe_tool",
        pack_of: Callable[[str], str | None] | None = None,
        search_result_serializer: SearchResultSerializer | None = None,
    ) -> None:
        #: The synthetic tools by name, and each pinned card with what it was
        #: made from - both built once (module docstring, "Built once").
        self._synthetic_cache: dict[str, Tool] = {}
        self._pinned_cards: dict[str, tuple[Any, str | None, Tool]] = {}
        super().__init__(
            max_results=max_results,
            always_visible=always_visible,
            search_tool_name=search_tool_name,
            call_tool_name=call_tool_name,
            search_result_serializer=search_result_serializer or serialize_search_results,
        )
        self._bm25_k1 = self._index.k1
        self._bm25_b = bm25_b
        self._describe_tool_name = describe_tool_name
        self._pack_of = pack_of

    # ------------------------------------------------------------------
    # The synthetic surface: search_tools, call_tool, describe_tool
    # ------------------------------------------------------------------

    @property
    def synthetic_names(self) -> frozenset[str]:
        """Every name this transform answers itself rather than the catalog."""
        return frozenset({self._search_tool_name, self._call_tool_name, self._describe_tool_name})

    @property
    def pinned(self) -> frozenset[str]:
        """The ``always_visible`` names (listed only when the catalog has them)."""
        return frozenset(self._always_visible)

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        """Pinned tools (as :func:`pinned_card`), search, call, then ``describe_tool``.

        The order is fixed - pins in catalog order, then the three synthetic
        tools - so a client's prompt-cache prefix never moves between sessions.
        """
        listed = [
            self._pinned_card(tool) if tool.name in self._always_visible else tool
            for tool in await super().transform_tools(tools)
        ]
        return [*listed, self._make_describe_tool()]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        """Resolve ``describe_tool`` here and the call proxy to the guarded one."""
        if name == self._describe_tool_name:
            return self._make_describe_tool()
        return await super().get_tool(name, call_next, version=version)

    # ------------------------------------------------------------------
    # Built once
    # ------------------------------------------------------------------

    def _synthetic(self, name: str, build: Callable[[], Tool]) -> Tool:
        """``build()`` on the first listing; the same object on every later one."""
        tool = self._synthetic_cache.get(name)
        if tool is None:
            tool = build()
            self._synthetic_cache[name] = tool
        return tool

    def _pinned_card(self, tool: Tool) -> Tool:
        """:func:`pinned_card` of ``tool``, kept while the tool is what it was made
        from. A listing hands over the registered ``Tool`` or a shallow copy of
        it, so its schema dict is the same object either way; a tool registered
        again with another schema or text gets a new card."""
        hit = self._pinned_cards.get(tool.name)
        if hit is not None and hit[0] is tool.parameters and hit[1] == tool.description:
            return hit[2]
        card = pinned_card(tool)
        self._pinned_cards[tool.name] = (tool.parameters, tool.description, card)
        return card

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

        A tool listed with a split description (``discovery.descriptions``)
        is indexed on its full text, so the W3 split moves no search result.
        """
        extra = alias_text(tool.name)
        full = full_description(tool)
        if full != (tool.description or ""):
            tool = tool.model_copy(update={"description": full})
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
        return self._synthetic(self._search_tool_name, self._build_search_tool)

    def _build_search_tool(self) -> Tool:
        transform = self
        default_limit = self._max_results

        # 1.8-11: the synthetic texts are the whole idle cost of search mode, so
        # each is one line (all of them together under 1,000 characters,
        # tests/test_discovery_transform.py) and the schema carries no wire noise.
        async def search_tools(
            query: str,
            limit: int = default_limit,
            risk: Annotated[
                Literal["any", "write", "read"],
                Field(description="any|write|read; filtered hits are reported"),
            ] = "any",
            ctx: Context = None,  # type: ignore[assignment]
        ) -> Any:
            """Find tools for a job, in English or as an AutoCAD command (FILLET, QSELECT)."""
            visible = await transform._get_visible_tools(ctx)
            outcome = transform.rank(visible, query, limit=limit, risk=risk)
            return await transform.render(outcome)

        tool = _lean(Tool.from_function(fn=search_tools, name=self._search_tool_name))
        # Written here, not in the annotation: the default is this instance's.
        tool.parameters["properties"]["limit"]["description"] = (
            f"default {default_limit}, max {MAX_SEARCH_LIMIT}"
        )
        return tool

    # ------------------------------------------------------------------
    # The call proxy, guarded against every synthetic name
    # ------------------------------------------------------------------

    def _make_call_tool(self) -> Tool:
        return self._synthetic(self._call_tool_name, self._build_call_tool)

    def _build_call_tool(self) -> Tool:
        """The stock ``call_tool`` proxy, refusing ``describe_tool`` as well.

        Upstream's proxy refuses only the two names it knows about. The body is
        otherwise the stock one, message included, so a client sees the same
        refusal for all three synthetic tools.
        """
        transform = self

        async def call_tool(
            name: str,
            arguments: dict[str, Any] | None = None,
            ctx: Context = None,  # type: ignore[assignment]
        ) -> ToolResult:
            """Call a tool found by search_tools with its arguments."""
            if name in transform.synthetic_names:
                raise ValueError(
                    f"'{name}' is a synthetic search tool and cannot be called via the "
                    "call_tool proxy"
                )
            return await ctx.fastmcp.call_tool(name, arguments)

        return _lean(Tool.from_function(fn=call_tool, name=self._call_tool_name))

    # ------------------------------------------------------------------
    # The synthetic describe tool
    # ------------------------------------------------------------------

    def _make_describe_tool(self) -> Tool:
        return self._synthetic(self._describe_tool_name, lambda: _describe_tool_for(self))


def _describe_tool_for(transform: Any) -> Tool:
    """The ``describe_tool`` a transform lists: its catalog, its pack table.

    ``transform`` is a :class:`CatalogTransform` carrying ``synthetic_names``,
    ``_describe_tool_name`` and ``_pack_of`` - :class:`CadSearchTransform` in
    search mode, :class:`DetailsTransform` in ``off`` mode. One body for both,
    so the card a client reads never depends on the mode.
    """

    async def describe_tool(
        name: str | None = None,
        names: Annotated[list[str] | None, Field(max_length=MAX_DESCRIBE_NAMES)] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Full schema, risk and examples of a tool (name) or several (names)."""
        if (name is None) == (names is None):
            raise ValueError(
                "describe_tool: pass exactly one of `name` (one tool) or `names` "
                "(several, one card each)"
            )
        wanted = [name] if names is None else list(dict.fromkeys(names))
        if not wanted:
            raise ValueError("describe_tool: `names` is empty")
        synthetic = [n for n in wanted if n in transform.synthetic_names]
        if synthetic:
            raise ValueError(
                f"'{synthetic[0]}' is a synthetic search tool and cannot be described "
                "via describe_tool; list_tools already carries its full schema"
            )
        # The catalog call_tool reaches: every visibility gate (TOOL_PROFILE,
        # TOOL_PACKS, ENABLE_3D) and auth filter already applied. Pinned
        # tools are included - with their full description and schema, not
        # the shortened card search mode lists - because call_tool accepts
        # them too.
        catalog = {t.name: t for t in await transform.get_tool_catalog(ctx)}
        unknown = [n for n in wanted if n not in catalog]
        if unknown:
            # The exact refusal FastMCP raises when call_tool resolves the name;
            # several unknown names are all named, nothing is described.
            raise NotFoundError(f"Unknown tool: {', '.join(repr(n) for n in unknown)}")
        cards = [
            describe_payload(
                catalog[n],
                pack=transform._pack_of(n) if transform._pack_of is not None else None,
            )
            for n in wanted
        ]
        return cards[0] if names is None else {"tools": cards}

    return _lean(
        Tool.from_function(
            fn=describe_tool,
            name=transform._describe_tool_name,
            annotations=ToolAnnotations(readOnlyHint=True),
        )
    )


class DetailsTransform(CatalogTransform):
    """``DISCOVERY_MODE=off``: the whole catalog, plus ``describe_tool``.

    A description over 800 characters is listed as its leading paragraphs and
    ``Details: describe_tool.`` (``discovery/descriptions.py``) on *every*
    surface, the full catalog, ``TOOL_PROFILE=lean`` and each ``TOOL_PACKS``
    one included. Search mode lists ``describe_tool`` already; this transform
    is what makes the pointer lead somewhere in ``off`` mode too. It changes
    nothing else: every catalog tool is listed as it comes, in order, and
    ``describe_tool`` is appended last (a stable prompt-cache prefix) - only
    when a listed description actually points to it. Like the search-mode one
    it is synthetic: never in the registry, card-less (so ``cad_batch``
    refuses it as a step) and never counted by ``system_about``.
    """

    def __init__(
        self,
        *,
        describe_tool_name: str = "describe_tool",
        pack_of: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__()
        self._describe_tool_name = describe_tool_name
        self._pack_of = pack_of
        self._describe_cache: Tool | None = None

    @property
    def synthetic_names(self) -> frozenset[str]:
        """Every name this transform answers itself rather than the catalog."""
        return frozenset({self._describe_tool_name})

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        """The catalog unchanged, then ``describe_tool`` when a listing points to it."""
        if not any(_points_to_details(tool) for tool in tools):
            return tools
        return [*tools, self._make_describe_tool()]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        """Resolve ``describe_tool`` here; every other name passes through."""
        if name == self._describe_tool_name:
            return self._make_describe_tool()
        return await call_next(name, version=version)

    def _make_describe_tool(self) -> Tool:
        """Built once (module docstring); the same object on every listing."""
        if self._describe_cache is None:
            self._describe_cache = _describe_tool_for(self)
        return self._describe_cache


def _points_to_details(tool: Tool) -> bool:
    """True when ``tool`` lists a shortened description whose rest is elsewhere."""
    listed = tool.description or ""
    return listed.endswith(DETAILS_POINTER) and full_description(tool) != listed


class _SyntheticTool(FunctionTool):
    """A synthetic tool listed without fastmcp's ``_meta`` block.

    ``{"fastmcp": {"tags": []}}`` says nothing about a synthetic tool (it has
    no tags, no version) and costs ~30 wire characters each - about a tenth of
    search mode's whole idle budget (plan section 4: 250 tokens).
    """

    def get_meta(self) -> dict[str, Any] | None:  # type: ignore[override]
        return None


def _lean(tool: Tool) -> Tool:
    """A synthetic tool as listed: :func:`lean_schema` as its input schema, no
    output schema (structured content is still returned) and no ``_meta``."""
    fields = {name: getattr(tool, name) for name in type(tool).model_fields}
    fields["parameters"] = lean_schema(tool.parameters or {})
    fields["output_schema"] = None
    return _SyntheticTool(**fields)
