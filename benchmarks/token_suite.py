"""Token-cost suite (v1.5.0 lane): what this server's surface costs a client, measured.

Every token claim in the release has to come from here rather than from an
estimate typed into a README. Three costs are measured against the **real
in-memory server** (a `fastmcp.Client` speaking to `server.mcp`, same code path
a stdio client drives):

1. **Idle cost** - what a client pays on connect, for the default surface,
   ``TOOL_PROFILE=lean``, ``TOOL_PACKS=core`` (and two single-vertical pack
   sets) and ``DISCOVERY_MODE=search``, plus the server instructions. Since
   1.8-14 the headline columns price the *prompt payload* - the ``tools``
   array a client forwards to the model (name, description, input schema) -
   and the ``tools/list`` wire payload, the basis of every figure up to 1.7.0,
   is the ``wire`` column beside it.
2. **Per-discovery cost** - one ``search_tools`` round trip, ours against stock
   :class:`~fastmcp.server.transforms.search.BM25SearchTransform` *configured
   with the same compact serializer ours uses*. Stock-on-its-defaults is also
   measured, and labelled ``strawman: true``, because comparing against it is
   how you accidentally credit the renderer for the ranker's work.
3. **Result-side cost** - what a realistic ``entity_list`` actually returns from
   a drawing of a few hundred entities. This is the half nobody had measured.
4. **Task cost** (1.8-14) - the scripted jobs of ``benchmarks/call_budget.py``,
   each two ways (``naive``, one tool per entity and labelled so; ``batched``,
   the least-call oracle, or ``pending_tool`` when its verb lands later):
   calls, request/result characters, and the modelled session input - per
   turn the prefix plus every previous request and result - cached and
   uncached.

``--budget benchmarks/token_budget.json`` checks the run against the 1.8.0
ceilings and against the published report (``--published``): a ``ci`` cell
over its ceiling or a row grown more than 5 % exits 1 (every PR, in CI).

Counting rules
--------------
**Characters are the primary unit.** They are exact, tokenizer-independent and
reproducible on any machine; ``chars`` and ``bytes_utf8`` are what this harness
*measures*.

**Tokens are an estimate unless a real tokenizer is present, and they are always
labelled.** Two token methods exist:

``--tokenizer ratio`` (default)
    A **per-format** chars/token divisor (:data:`CHARS_PER_TOKEN`). Per-format,
    not flat: JSON schema text runs ~4.19 chars/token while the compact pipe
    cards of ``discovery/serialize.py`` run ~3.29. Those two divisors differ by
    27%, so one flat number (the ubiquitous chars/4) silently flatters whichever
    format it is wrong about - by more than several of the differences this
    suite reports. Each ratio carries its provenance and confidence into the
    report, and the one that is *borrowed* rather than measured says so.

``--tokenizer anthropic``
    Real counts from ``POST /v1/messages/count_tokens``. Needs the ``anthropic``
    package plus credentials, neither of which is in this repo's lock, so it is
    opt-in; when it is unavailable the harness **raises** rather than quietly
    falling back to the divisors.

``tiktoken`` is deliberately *not* supported, even though it is a real
tokenizer: it is OpenAI's BPE, and Anthropic's own guidance is that it
undercounts Claude tokens by ~15-20% on prose and by more on code and JSON.
Wiring it in would reintroduce exactly the error this module exists to avoid,
only wearing a tokenizer's clothes.

**Cached and uncached are both reported.** A tool prefix that never mutates is
prompt-cacheable and every real client caches it, so a single uncached headline
is not a number anyone reproduces in practice. Cache reads are priced at 0.1x
input and the first write at 1.25x (5-minute ephemeral TTL); a payload below a
model's minimum cacheable prefix cannot cache at all, which the idle lane
reports per model tier.

Honesty boundaries
------------------
- ezdxf only. The suite pins ``AUTOCAD_MCP_BACKEND=ezdxf`` and *verifies* it via
  ``system_status`` before drawing anything - measuring (and mutating) a live
  AutoCAD document by accident would be both wrong and rude.
- Self-measurement only: no competitor server is timed or sized here.
- The suite mutates a shared singleton (tool profile, discovery transform) and
  restores every global it touches.

Run::

    python -m benchmarks.token_suite                  # human summary + JSON report
    python -m benchmarks.token_suite --json           # machine-readable on stdout
    python -m benchmarks.token_suite --entities 500   # bigger result-lane drawing
    python -m benchmarks.token_suite --lanes idle,task --budget benchmarks/token_budget.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config

ROOT = Path(__file__).resolve().parents[1]

#: 2.0 (1.8-14): the idle headline is the prompt payload (wire beside it), the
#: idle lane gains ``packs_core`` and ``instructions``, and a fourth lane, ``task``.
SCHEMA_VERSION = "token/2.0"
DEFAULT_OUTPUT = ROOT / "benchmarks" / "results" / "token-cost.json"

#: The env var ``server._make_backend`` actually reads. Note it reads the *env*,
#: not ``config.settings.backend`` - setting the settings attribute alone leaves
#: a Windows box with AutoCAD open talking to live COM.
BACKEND_ENV = "AUTOCAD_MCP_BACKEND"

#: Entities built for the result lane. A few hundred is what a real plate or
#: assembly drawing holds by the time it is dimensioned.
DEFAULT_ENTITIES = 300

#: ``entity_list`` limits worth pricing: the tool's default, a page a caller
#: would plausibly ask for, and the largest the schema permits.
LIST_LIMITS = (25, 100, 1000)

#: ``limit`` is declared ``Field(..., ge=1, le=1000)`` on the tool, so the
#: ``MAX_LIST_LIMIT`` cap (default 5000) cannot be reached through it.
SCHEMA_MAX_LIMIT = 1000

#: Turns in the modelled session. Cost per request is the honest per-turn
#: number; a session total is what a user actually pays.
SESSION_TURNS = 30


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

FORMAT_JSON_SCHEMA = "json_schema"
FORMAT_COMPACT_TEXT = "compact_text"
FORMAT_JSON_DATA = "json_data"
FORMAT_TEXT = "text"

#: How a ratio came to be. ``borrowed`` means no measurement exists for that
#: format yet and a neighbouring format's divisor is standing in - a weaker
#: estimate, and marked as one everywhere it is used.
RATIO_CONFIDENCE = ("carried-over-measurement", "borrowed")


@dataclass(frozen=True, slots=True)
class FormatRatio:
    """One format's chars/token divisor, with where it came from."""

    chars_per_token: float
    provenance: str
    confidence: str


CHARS_PER_TOKEN: dict[str, FormatRatio] = {
    FORMAT_JSON_SCHEMA: FormatRatio(
        4.19,
        "v1.5.0 design-pass measurement over this server's own tools/list payload "
        "(JSON Schema text: short ASCII keys, punctuation and prose descriptions). "
        "Not re-derived here - re-derive with --tokenizer anthropic.",
        "carried-over-measurement",
    ),
    FORMAT_COMPACT_TEXT: FormatRatio(
        3.29,
        "v1.5.0 design-pass measurement over the compact pipe cards emitted by "
        "discovery/serialize.py. Denser than schema text, hence the lower divisor - "
        "the whole reason a flat divisor is wrong.",
        "carried-over-measurement",
    ),
    FORMAT_JSON_DATA: FormatRatio(
        4.19,
        "Borrowed from json_schema; no measurement exists for dense numeric JSON. "
        "Float-heavy JSON plausibly tokenizes worse (fewer chars per token), so the "
        "token figures on result-side payloads are best read as a floor.",
        "borrowed",
    ),
    FORMAT_TEXT: FormatRatio(
        4.19,
        "Borrowed from json_schema, whose text is mostly prose descriptions; no "
        "measurement exists for the server's plain-English instructions alone.",
        "borrowed",
    ),
}

#: Anthropic's default reference model for the opt-in real-tokenizer path.
DEFAULT_TOKENIZER_MODEL = "claude-opus-5"


class RatioTokenCounter:
    """Per-format chars/token estimator. Always available; always an estimate."""

    name = "ratio/v1"
    estimated = True
    note = (
        "tokens = characters / a per-format divisor (see counting.ratios); "
        "estimates, not counts. Characters are the measured unit."
    )

    def chars_per_token(self, fmt: str) -> float:
        return CHARS_PER_TOKEN[fmt].chars_per_token

    def confidence(self, fmt: str) -> str:
        return CHARS_PER_TOKEN[fmt].confidence

    def count(self, text: str, fmt: str) -> int:
        """Estimate tokens. An unknown format raises rather than guessing.

        Floored at one token for any non-empty text: dividing a 2-character
        payload ("[]", which stock BM25 returns when it finds nothing) by 4.19
        and rounding gives zero, and a free payload is not a thing.
        """
        if not text:
            return 0
        return max(1, int(round(len(text) / CHARS_PER_TOKEN[fmt].chars_per_token)))


class AnthropicTokenCounter:
    """Real counts from ``POST /v1/messages/count_tokens``.

    The endpoint counts a *message*, while most payloads here are really a
    ``tools`` array or a tool result, so the count carries a small fixed
    message-envelope overhead. That is a known, bounded approximation and is
    recorded in the report's ``method_note`` - unlike a wrong tokenizer, it does
    not scale with payload size.
    """

    estimated = False
    note = (
        "tokens counted by POST /v1/messages/count_tokens for the recorded model; "
        "payloads are counted as a single user message, so each count carries the "
        "fixed message-envelope overhead."
    )

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model
        self.name = f"anthropic-count-tokens/{model}"

    def chars_per_token(self, fmt: str) -> float | None:
        CHARS_PER_TOKEN[fmt]  # validate the format label even when unused
        return None

    def confidence(self, fmt: str) -> str:
        CHARS_PER_TOKEN[fmt]
        return "measured"

    def count(self, text: str, fmt: str) -> int:
        CHARS_PER_TOKEN[fmt]
        response = self._client.messages.count_tokens(
            model=self._model,
            messages=[{"role": "user", "content": text}],
        )
        return int(response.input_tokens)


def _anthropic_client() -> Any | None:
    """The Anthropic SDK client, or None when the package/credentials are absent."""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        return anthropic.Anthropic()
    except Exception:  # missing credentials, bad profile, ...
        return None


def build_counter(tokenizer: str = "ratio", *, model: str = DEFAULT_TOKENIZER_MODEL) -> Any:
    """Build the requested token counter, or refuse.

    The refusal is the point: silently degrading ``--tokenizer anthropic`` into
    the ratio estimator would put unlabelled estimates under a heading that
    promised counts.
    """
    if tokenizer == "ratio":
        return RatioTokenCounter()
    if tokenizer == "anthropic":
        client = _anthropic_client()
        if client is None:
            raise RuntimeError(
                "--tokenizer anthropic needs the `anthropic` package and working "
                "credentials; neither is in this repo's lock. Refusing to fall back "
                "to the ratio estimator silently - re-run with --tokenizer ratio and "
                "read the token columns as estimates."
            )
        return AnthropicTokenCounter(client, model)
    raise ValueError(f"unknown tokenizer {tokenizer!r} (expected 'ratio' or 'anthropic')")


# ---------------------------------------------------------------------------
# Cache pricing
# ---------------------------------------------------------------------------

#: A cache read costs ~0.1x base input; the first write ~1.25x at the default
#: 5-minute ephemeral TTL.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

#: Minimum cacheable prefix, per model tier. Below it, a prefix silently does
#: not cache - no error, just no cache entry.
PROMPT_CACHE_MINIMUM_TOKENS: dict[int, tuple[str, ...]] = {
    512: ("claude-opus-5", "claude-fable-5", "claude-mythos-5"),
    1024: ("claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5"),
    2048: ("claude-opus-4-7", "claude-haiku-3-5"),
    4096: ("claude-opus-4-6", "claude-opus-4-5", "claude-haiku-4-5"),
}


def cost_model(tokens: float, turns: int = SESSION_TURNS) -> dict[str, Any]:
    """Price one re-sent span both ways, per request and over a session.

    Applies to any span a client re-sends on every request: the tool prefix
    (which never mutates) and, once it lands, a tool result sitting in history.
    Uncached, the span is paid in full every turn. Cached, it is written once at
    1.25x and read at 0.1x thereafter.
    """
    turns = max(1, int(turns))
    session_cached = tokens * CACHE_WRITE_MULTIPLIER + tokens * CACHE_READ_MULTIPLIER * (turns - 1)
    session_uncached = tokens * turns
    return {
        "unit": "input tokens (cached figures are price-equivalent: read at 0.1x, write at 1.25x)",
        "uncached_input_tokens_per_request": round(tokens, 1),
        "cached_input_tokens_per_request": round(tokens * CACHE_READ_MULTIPLIER, 1),
        "turns": turns,
        "session_uncached": round(session_uncached, 1),
        "session_cached": round(session_cached, 1),
        "session_saving_ratio": (
            round(1.0 - session_cached / session_uncached, 3) if session_uncached else 0.0
        ),
    }


def cacheability(tokens: float) -> dict[str, Any]:
    """Which model tiers can cache a prefix this size at all."""
    return {
        "cacheable": {
            str(minimum): tokens >= minimum for minimum in sorted(PROMPT_CACHE_MINIMUM_TOKENS)
        },
        "models_by_minimum": {
            str(minimum): list(models)
            for minimum, models in sorted(PROMPT_CACHE_MINIMUM_TOKENS.items())
        },
        "note": (
            "Minimum cacheable prefix by model tier. A prefix below the minimum does "
            "not cache and reports no error, so its cached column is unreachable in "
            "practice."
        ),
    }


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _dumps(obj: Any) -> str:
    """Compact JSON, matching what a JSON-RPC transport puts on the wire."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def measure(text: str, fmt: str, counter: Any, *, turns: int = SESSION_TURNS) -> dict[str, Any]:
    """One payload, priced: exact characters, labelled tokens, both cost columns."""
    tokens = counter.count(text, fmt)
    return {
        "chars": len(text),
        "bytes_utf8": len(text.encode("utf-8")),
        "tokens": {
            "value": tokens,
            "format": fmt,
            "method": counter.name,
            "estimated": bool(counter.estimated),
            "chars_per_token": (
                counter.chars_per_token(fmt)
                if counter.estimated
                else (round(len(text) / tokens, 2) if tokens else None)
            ),
            "confidence": counter.confidence(fmt),
        },
        "cost": cost_model(tokens, turns),
    }


def counting_block(counter: Any) -> dict[str, Any]:
    """The method statement that has to travel with the numbers."""
    return {
        "primary_unit": "characters",
        "token_method": counter.name,
        "tokens_are_estimates": bool(counter.estimated),
        "method_note": counter.note,
        "rejected_methods": {
            "flat_chars_per_token": (
                "One divisor for every format mis-states every cross-format "
                "comparison in this report - the schema-text and compact-text "
                "divisors differ by 27%. Within a format it would cancel; the "
                "comparisons here are between formats."
            ),
            "tiktoken": (
                "A real tokenizer, but OpenAI's: it undercounts Claude tokens by "
                "~15-20% on prose and more on code/JSON, so it would reintroduce the "
                "same class of error. Not wired in on purpose."
            ),
        },
        "ratios": {
            name: {
                "chars_per_token": ratio.chars_per_token,
                "confidence": ratio.confidence,
                "provenance": ratio.provenance,
            }
            for name, ratio in CHARS_PER_TOKEN.items()
        },
    }


# ---------------------------------------------------------------------------
# Server access
# ---------------------------------------------------------------------------


def _load_server():
    """Import ``server`` lazily.

    Same reason server.py imports its backends lazily: pulling in fastmcp and
    ezdxf costs a second, and ``--help`` should not pay it.
    """
    import server

    return server


def require_ezdxf(status: dict[str, Any]) -> None:
    """Abort unless the connected backend is the headless engine.

    ``server._make_backend`` reads the *environment*, not ``config.settings``,
    so a harness that forgot the env var would happily build a few hundred
    entities inside the operator's live AutoCAD document - and measure a
    different server while doing it.
    """
    backend = str(status.get("backend", "unknown"))
    if backend != "ezdxf":
        raise RuntimeError(
            f"refusing to measure against the {backend!r} backend: this suite is "
            f"ezdxf-only. Set {BACKEND_ENV}=ezdxf (the env var is what "
            "server._make_backend reads; config.settings.backend is not)."
        )


def _client():
    """An unconnected in-memory client for the real server. ``async with`` it."""
    from fastmcp import Client

    return Client(_load_server().mcp)


# ---------------------------------------------------------------------------
# Lane 1: idle cost (tools/list)
# ---------------------------------------------------------------------------


def _wire_payload(tools: Sequence[Any]) -> str:
    """The ``tools/list`` result exactly as the MCP SDK serializes it.

    ``mcp.shared.session`` dumps every response with
    ``model_dump(by_alias=True, mode="json", exclude_none=True)``, so this is the
    payload minus the few dozen bytes of JSON-RPC envelope.
    """
    payload = {
        "tools": [tool.model_dump(by_alias=True, mode="json", exclude_none=True) for tool in tools]
    }
    return _dumps(payload)


def _prompt_payload(tools: Sequence[Any]) -> str:
    """The subset an MCP client forwards to a model API as its ``tools`` array.

    Name, description and input schema - no annotations, titles, output schemas
    or ``_meta``. Clients differ; this is the floor of what any of them sends.
    """
    rows = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.inputSchema,
        }
        for tool in tools
    ]
    return _dumps(rows)


#: What the idle lane's headline columns (``chars`` / ``tokens`` / ``cost``)
#: measure since 1.8-14: the ``tools`` array a client forwards to the model. The
#: ``tools/list`` wire payload - the basis of every figure up to 1.7.0 (89,226
#: and 503) - stays beside it as the ``wire`` column.
IDLE_HEADLINE_BASIS = "prompt_payload"


async def _idle_row(variant: str, note: str, counter: Any, turns: int) -> dict[str, Any]:
    client = _client()
    async with client:
        tools = await client.list_tools()
    wire = _wire_payload(tools)
    prompt = _prompt_payload(tools)
    row = measure(prompt, FORMAT_JSON_SCHEMA, counter, turns=turns)
    row.update(
        {
            "variant": variant,
            "kind": "tools_list",
            "basis": IDLE_HEADLINE_BASIS,
            "note": note,
            "advertised_tools": len(tools),
            "wire_chars": len(wire),
            "prompt_chars": len(prompt),
            "wire": measure(wire, FORMAT_JSON_SCHEMA, counter, turns=turns),
            "prompt_cache": cacheability(row["tokens"]["value"]),
        }
    )
    return row


async def _instructions_row(counter: Any, turns: int) -> dict[str, Any]:
    """The server's ``instructions``, as the initialize result carries them to a client.

    A client puts them in the system prompt beside the tools array, so they are
    an idle cost of their own (plan section 4, a new row in 1.8-14).
    """
    client = _client()
    async with client:
        initialized = getattr(client, "initialize_result", None)
    text = getattr(initialized, "instructions", None)
    if text is None:
        text = _load_server().mcp.instructions or ""
    row = measure(text, FORMAT_TEXT, counter, turns=turns)
    row.update(
        {
            "variant": "instructions",
            "kind": "instructions",
            "basis": "text",
            "note": "The server instructions from the initialize result (system prompt).",
            "advertised_tools": 0,
            "wire_chars": len(text),
            "prompt_chars": len(text),
            "wire": measure(text, FORMAT_TEXT, counter, turns=turns),
            "prompt_cache": cacheability(row["tokens"]["value"]),
        }
    )
    return row


async def idle_lane(counter: Any, turns: int = SESSION_TURNS) -> list[dict[str, Any]]:
    """What a client pays on connect, for each advertised surface."""
    srv = _load_server()
    previous_profile = config.settings.tool_profile
    previous_packs = config.settings.tool_packs
    rows: list[dict[str, Any]] = []
    try:
        config.settings.tool_profile = "full"
        srv._apply_discovery_mode("off")
        rows.append(
            await _idle_row(
                "default",
                "TOOL_PROFILE=full, DISCOVERY_MODE=off - the shipped default.",
                counter,
                turns,
            )
        )
        config.settings.tool_profile = "lean"
        rows.append(
            await _idle_row(
                "lean",
                "TOOL_PROFILE=lean - the curated drafting core.",
                counter,
                turns,
            )
        )
        config.settings.tool_profile = "full"
        config.settings.tool_packs = "core"
        rows.append(
            await _idle_row(
                "packs_core",
                "TOOL_PACKS=core - no vertical pack at all (the README's core row).",
                counter,
                turns,
            )
        )
        config.settings.tool_packs = "core,mech"
        rows.append(
            await _idle_row(
                "packs_core_mech",
                "TOOL_PACKS=core,mech - a mechanical client that never opens a "
                "P&ID or a live seat.",
                counter,
                turns,
            )
        )
        config.settings.tool_packs = "core,arch"
        rows.append(
            await _idle_row(
                "packs_core_arch",
                "TOOL_PACKS=core,arch - an architectural client that never opens a "
                "P&ID, a machine part or a live seat.",
                counter,
                turns,
            )
        )
        config.settings.tool_packs = previous_packs
        srv._apply_discovery_mode("search")
        rows.append(
            await _idle_row(
                "search",
                "DISCOVERY_MODE=search - search_tools, the call_tool proxy and "
                "describe_tool; the catalog is reached through the search tool "
                "instead of advertised.",
                counter,
                turns,
            )
        )
        srv._apply_discovery_mode("off")
        rows.append(await _instructions_row(counter, turns))
    finally:
        srv._apply_discovery_mode("off")
        config.settings.tool_profile = previous_profile
        # restored here too, so a failed measurement cannot leave the process
        # on a narrowed surface
        config.settings.tool_packs = previous_packs
        await srv._apply_tool_profile(previous_profile)
    return rows


# ---------------------------------------------------------------------------
# Lane 2: per-discovery cost (one search_tools round trip)
# ---------------------------------------------------------------------------

#: Published, not cherry-picked: plain-English drafting intents plus the AutoCAD
#: command names a drafter actually types. WBLOCK is in deliberately - it is a
#: df=0 case where stock BM25 returns nothing at all, i.e. the cheapest possible
#: payload and the worst possible answer.
#:
#: BPOLY held that slot until v1.5.0 (M8). `boundary_trace` now names BOUNDARY
#: and BPOLY in its own docstring, so stock BM25 finds it and BPOLY is no
#: longer a df=0 case. Keeping it as the zero-hit example would have measured a
#: gap that had closed.
QUERIES = (
    "round the corner",
    "WBLOCK",
    "BPOLY",
    "how many entities are on the GEOMETRY layer",
    "draw a hole in the plate",
    "dimension everything with a chain",
    "save the drawing as a pdf",
    "put a leader note on the chamfer",
    "select every circle on the GEOMETRY layer",
)

#: Hits per search. The transform's own default.
SEARCH_LIMIT = 5


async def _catalog() -> list[Any]:
    """The visible catalog a search transform would rank, untransformed."""
    srv = _load_server()
    srv._apply_discovery_mode("off")
    await srv._apply_tool_profile("full")
    return list(await srv.mcp.list_tools())


async def discovery_lane(counter: Any, turns: int = SESSION_TURNS) -> dict[str, Any]:
    """One search round trip, ours against a *configured* stock BM25 transform."""
    from fastmcp.server.transforms.search import BM25SearchTransform
    from fastmcp.server.transforms.search.base import serialize_tools_for_output_json

    from discovery.serialize import serialize_search_results
    from discovery.transform import CadSearchTransform

    catalog = await _catalog()
    ours = CadSearchTransform(max_results=SEARCH_LIMIT)
    stock_compact = BM25SearchTransform(
        max_results=SEARCH_LIMIT, search_result_serializer=serialize_search_results
    )
    stock_default = BM25SearchTransform(max_results=SEARCH_LIMIT)

    arms: dict[str, dict[str, Any]] = {
        "cad_search": {
            "arm": "cad_search",
            "transform": "discovery.transform.CadSearchTransform",
            "serializer": "discovery.serialize.serialize_search_results",
            "strawman": False,
            "format": FORMAT_COMPACT_TEXT,
            "note": "Ours: alias-aware BM25 plus the risk filter, compact cards.",
            "queries": [],
        },
        "stock_bm25_compact": {
            "arm": "stock_bm25_compact",
            "transform": "fastmcp.server.transforms.search.BM25SearchTransform",
            "serializer": "discovery.serialize.serialize_search_results",
            "strawman": False,
            "format": FORMAT_COMPACT_TEXT,
            "note": (
                "Stock BM25 wired to the same compact serializer - the fair A/B. Any "
                "size difference against ours is ranking, not rendering."
            ),
            "queries": [],
        },
        "stock_bm25_default": {
            "arm": "stock_bm25_default",
            "transform": "fastmcp.server.transforms.search.BM25SearchTransform",
            "serializer": "fastmcp...serialize_tools_for_output_json",
            "strawman": True,
            "format": FORMAT_JSON_SCHEMA,
            "note": (
                "Stock BM25 on its default serializer, which re-emits each hit's full "
                "list_tools entry. Recorded to size the renderer's contribution, NOT "
                "as a baseline any claim should be made against."
            ),
            "queries": [],
        },
    }

    # ``_search`` / ``_render_results`` are FastMCP internals, and deliberately so:
    # they are the seam where the ranker hands off to the serializer, which is the
    # only place the two arms can be compared with the renderer held constant.
    # discovery/transform.py already builds on the same seam.
    for query in QUERIES:
        outcome = ours.rank(catalog, query, limit=SEARCH_LIMIT)
        payload = await ours.render(outcome)
        arms["cad_search"]["queries"].append(
            _query_row(query, outcome.hits, str(payload), FORMAT_COMPACT_TEXT, counter, turns)
        )

        hits = list(await stock_compact._search(catalog, query))
        rendered = await stock_compact._render_results(hits)
        arms["stock_bm25_compact"]["queries"].append(
            _query_row(query, hits, str(rendered), FORMAT_COMPACT_TEXT, counter, turns)
        )

        hits_default = list(await stock_default._search(catalog, query))
        raw = await stock_default._render_results(hits_default)
        text = (
            raw if isinstance(raw, str) else _dumps(serialize_tools_for_output_json(hits_default))
        )
        arms["stock_bm25_default"]["queries"].append(
            _query_row(query, hits_default, text, FORMAT_JSON_SCHEMA, counter, turns)
        )

    return {
        "limit": SEARCH_LIMIT,
        "catalog_tools": len(catalog),
        "arms": list(arms.values()),
        "summary": _discovery_summary(arms),
    }


def _query_row(
    query: str,
    hits: Sequence[Any],
    payload: str,
    fmt: str,
    counter: Any,
    turns: int,
) -> dict[str, Any]:
    row = measure(payload, fmt, counter, turns=turns)
    row.update({"query": query, "hits": len(hits), "top": [tool.name for tool in hits[:3]]})
    return row


def _discovery_summary(arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Mean payload per arm over queries every arm actually answered.

    Averaging a zero-hit answer in would score "found nothing" as the cheapest
    result, which is precisely backwards: a search that returns nothing costs
    the caller another turn.
    """
    by_query: dict[str, dict[str, dict[str, Any]]] = {}
    for name, arm in arms.items():
        for row in arm["queries"]:
            by_query.setdefault(row["query"], {})[name] = row
    comparable = [q for q, rows in by_query.items() if all(r["hits"] > 0 for r in rows.values())]

    def _mean(name: str, key: str) -> float:
        values = [by_query[q][name][key] for q in comparable]
        return round(sum(values) / len(values), 1) if values else 0.0

    return {
        "queries": len(by_query),
        "comparable_queries": len(comparable),
        "excluded_queries": sorted(set(by_query) - set(comparable)),
        "mean_chars": {name: _mean(name, "chars") for name in arms},
        "mean_tokens": {
            name: round(
                sum(by_query[q][name]["tokens"]["value"] for q in comparable) / len(comparable), 1
            )
            if comparable
            else 0.0
            for name in arms
        },
        "hit_rate": {
            name: round(
                sum(1 for row in arms[name]["queries"] if row["hits"] > 0)
                / len(arms[name]["queries"]),
                3,
            )
            for name in arms
        },
        "note": (
            "Means cover only queries every arm answered; zero-hit answers are "
            "excluded from size means and reported in hit_rate instead. The compact "
            "serializer, not the ranker, is what makes a search answer small - stock "
            "BM25 with the same serializer lands within a few percent. Ours differs "
            "in finding the tool at all."
        ),
    }


# ---------------------------------------------------------------------------
# Lane 3: result-side cost (what entity_list actually returns)
# ---------------------------------------------------------------------------


async def _build_drawing(client: Any, entities: int) -> None:
    """A mixed drawing: lines, circles, arcs, text and polylines on ISO layers."""
    await client.call_tool("drawing_new", {})
    for index in range(entities):
        x = float(index % 20) * 10.0
        y = float(index // 20) * 10.0
        shape = index % 5
        if shape == 0:
            await client.call_tool(
                "entity_create_line",
                {"x1": x, "y1": y, "x2": x + 8.0, "y2": y + 6.0, "layer": "GEOMETRY"},
            )
        elif shape == 1:
            await client.call_tool(
                "entity_create_circle",
                {"cx": x + 4.0, "cy": y + 3.0, "radius": 1.7, "layer": "GEOMETRY"},
            )
        elif shape == 2:
            await client.call_tool(
                "entity_create_arc",
                {
                    "cx": x,
                    "cy": y,
                    "radius": 2.5,
                    "start_angle": 15.0,
                    "end_angle": 200.0,
                    "layer": "HIDDEN",
                },
            )
        elif shape == 3:
            await client.call_tool(
                "entity_create_text",
                {"text": f"P{index:03d}", "x": x, "y": y + 7.0, "height": 2.5, "layer": "TEXT"},
            )
        else:
            await client.call_tool(
                "entity_create_polyline",
                {
                    "points": [[x, y], [x + 8.0, y], [x + 8.0, y + 6.0]],
                    "closed": True,
                    "layer": "GEOMETRY",
                },
            )


def _without_bounding_box(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped = []
    for row in rows:
        copy = dict(row)
        properties = dict(copy.get("properties") or {})
        properties.pop("bounding_box", None)
        copy["properties"] = properties
        stripped.append(copy)
    return stripped


def _round_floats(value: Any, digits: int) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: _round_floats(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_floats(item, digits) for item in value]
    return value


def _counterfactual(
    label: str, text: str, baseline_chars: int, counter: Any, turns: int, note: str
) -> dict[str, Any]:
    row = measure(text, FORMAT_JSON_DATA, counter, turns=turns)
    row.update(
        {
            "counterfactual": True,
            "label": label,
            "note": note,
            "saved_chars": baseline_chars - len(text),
            "saved_pct": (
                round(100.0 * (baseline_chars - len(text)) / baseline_chars, 1)
                if baseline_chars
                else 0.0
            ),
        }
    )
    return row


async def result_lane(
    counter: Any, turns: int = SESSION_TURNS, entities: int = DEFAULT_ENTITIES
) -> dict[str, Any]:
    """What one realistic ``entity_list`` call puts back into the context window."""
    client = _client()
    scenarios: list[dict[str, Any]] = []
    async with client:
        status = (await client.call_tool("system_status", {})).structured_content or {}
        require_ezdxf(status)
        await _build_drawing(client, entities)
        info = (await client.call_tool("drawing_info", {})).structured_content or {}
        entity_types: Counter[str] = Counter()

        for limit in LIST_LIMITS:
            result = await client.call_tool("entity_list", {"limit": limit})
            text = result.content[0].text
            structured = _dumps(result.structured_content)
            rows = json.loads(text)
            if len(rows) >= sum(entity_types.values()):
                # The mix of the *drawing*, read off the widest listing - summing
                # across scenarios would count the same entities several times.
                entity_types = Counter(row["type"] for row in rows)

            row = measure(text, FORMAT_JSON_DATA, counter, turns=turns)
            baseline = len(text)
            row.update(
                {
                    "scenario": f"entity_list(limit={limit})",
                    "limit": limit,
                    "rows_returned": len(rows),
                    "text_chars": len(text),
                    "structured_chars": len(structured),
                    "wire_chars": len(text) + len(structured),
                    "chars_per_entity": round(baseline / len(rows), 1) if rows else 0.0,
                    "note": (
                        "chars/tokens price the text block - what a model client feeds "
                        "the model. FastMCP also returns the same rows as "
                        "structuredContent, so the transport carries the payload twice; "
                        "wire_chars is that total."
                    ),
                    "counterfactuals": {
                        "without_bounding_box": _counterfactual(
                            "without_bounding_box",
                            _dumps(_without_bounding_box(rows)),
                            baseline,
                            counter,
                            turns,
                            "Every EntityInfo carries a nested properties.bounding_box; "
                            "this is what dropping it would save.",
                        ),
                        "floats_rounded_6dp": _counterfactual(
                            "floats_rounded_6dp",
                            _dumps(_round_floats(rows, 6)),
                            baseline,
                            counter,
                            turns,
                            "Full-precision repr (14.142135623730951) versus 6 decimals - "
                            "drawing units are mm, so the extra digits are noise.",
                        ),
                    },
                }
            )
            scenarios.append(row)

    widest = max(scenarios, key=lambda row: row["rows_returned"], default=None)
    per_entity = widest["chars_per_entity"] if widest else 0.0
    return {
        "backend": str(status.get("backend", "unknown")),
        "entities_in_drawing": int(info.get("entity_count", 0)),
        "entity_types": dict(sorted(entity_types.items())),
        "scenarios": scenarios,
        "projection": {
            "chars_per_entity": per_entity,
            "schema_max_limit": SCHEMA_MAX_LIMIT,
            "config_max_list_limit": config.settings.max_list_limit,
            "extrapolated": True,
            "extrapolated_chars_at_schema_max": round(per_entity * SCHEMA_MAX_LIMIT),
            "extrapolated_chars_at_config_cap": round(per_entity * config.settings.max_list_limit),
            "note": (
                "entity_list declares limit as Field(ge=1, le=1000), so the "
                "MAX_LIST_LIMIT cap (default 5000) is unreachable through the tool's "
                "schema - the config-cap row is an extrapolation from measured "
                "chars/entity, not a payload anyone can actually elicit."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Lane 4: task cost (scripted jobs, two arms - benchmarks/call_budget.py)
# ---------------------------------------------------------------------------


def session_input(spans: Sequence[int], prefix_tokens: float) -> dict[str, Any]:
    """Modelled session input of a job of ``len(spans)`` calls, both ways.

    Turn ``k`` (1..n) asks for call ``k``; turn ``n + 1`` reads the last
    result. Every turn re-sends the prefix (tools array + instructions) and
    every earlier request and result, so span ``i`` (call ``i``'s request plus
    its result, 0-based) is re-sent ``n - i`` times. Uncached, each re-send is
    paid in full; cached, each span is written once at 1.25x and read at 0.1x
    after - :func:`cost_model` per span, summed.
    """
    calls = len(spans)
    prefix = cost_model(prefix_tokens, calls + 1)
    uncached = prefix["session_uncached"]
    cached = prefix["session_cached"]
    for index, tokens in enumerate(spans):
        priced = cost_model(tokens, calls - index)
        uncached += priced["session_uncached"]
        cached += priced["session_cached"]
    return {
        "turns": calls + 1,
        "prefix_tokens": round(prefix_tokens, 1),
        "history_tokens": int(sum(spans)),
        "session_input_uncached": round(uncached, 1),
        "session_input_cached": round(cached, 1),
        "prefix_cache": cacheability(prefix_tokens),
        "note": (
            "per turn: the prefix plus every previous request and result. Cached: each "
            "span written once at 1.25x, read at 0.1x after."
        ),
    }


async def task_lane(counter: Any, turns: int = SESSION_TURNS) -> dict[str, Any]:
    """Each call_budget scenario, both arms, priced: calls, characters, session input.

    The prefix is the default surface's prompt payload plus the server
    instructions, measured here, so the lane stands on its own.
    """
    from benchmarks import call_budget

    srv = _load_server()
    srv._apply_discovery_mode("off")
    default = await _idle_row("default", "the task lane's prefix", counter, turns)
    instructions = await _instructions_row(counter, turns)
    prefix_tokens = default["tokens"]["value"] + instructions["tokens"]["value"]
    client = _client()
    async with client:
        status = (await client.call_tool("system_status", {})).structured_content or {}
        require_ezdxf(status)
        rows, traces = await call_budget.run_scenarios(client)
    for row in rows:
        recorders = traces.get(row["scenario"], {})
        for name, arm in row["arms"].items():
            recorder = recorders.get(name)
            if recorder is None:
                arm["session"] = None
                continue
            # counted once per text: --tokenizer anthropic pays a request per count
            requests = [counter.count(c.request_text, FORMAT_JSON_DATA) for c in recorder.calls]
            results = [counter.count(c.result_text, FORMAT_JSON_DATA) for c in recorder.calls]
            arm["request_tokens"] = sum(requests)
            arm["result_tokens"] = sum(results)
            arm["session"] = session_input(
                [ask + answer for ask, answer in zip(requests, results, strict=True)],
                prefix_tokens,
            )
    return {
        "prefix": {
            "tokens": prefix_tokens,
            "tools_tokens": default["tokens"]["value"],
            "instructions_tokens": instructions["tokens"]["value"],
            "surface": "default (TOOL_PROFILE=full, DISCOVERY_MODE=off) + instructions",
        },
        "naive_label": call_budget.NAIVE_LABEL,
        "scenarios": rows,
    }


# ---------------------------------------------------------------------------
# Budget (token_budget.json): ceilings per lane, growth against the published run
# ---------------------------------------------------------------------------

DEFAULT_BUDGET = ROOT / "benchmarks" / "token_budget.json"
DEFAULT_PUBLISHED = ROOT / "benchmarks" / "results" / "published" / "token-cost.json"


def _basis_tokens(row: dict[str, Any], basis: str) -> int | None:
    """A row's tokens on ``basis``; tolerant of reports written before 1.8-14."""
    if basis == "wire":
        wire = row.get("wire")
        if isinstance(wire, dict):
            return wire["tokens"]["value"]
        return row["tokens"]["value"] if row.get("basis") is None else None
    if basis in ("prompt_payload", "text"):
        if row.get("basis") in ("prompt_payload", "text"):
            return row["tokens"]["value"]
        nested = row.get("prompt_payload")
        return nested["tokens"]["value"] if isinstance(nested, dict) else None
    raise KeyError(f"unknown budget basis {basis!r}")


def _headline_tokens(row: dict[str, Any]) -> int | None:
    return _basis_tokens(row, "text" if row.get("kind") == "instructions" else "prompt_payload")


def check_budget(
    report: dict[str, Any],
    budget: dict[str, Any],
    published: dict[str, Any] | None = None,
    *,
    release: bool = False,
) -> dict[str, Any]:
    """The report against the budget: ``{violations, over_ceiling, pending, checked, ok}``.

    A ``ci`` cell over its ceiling, or not measured, is a violation; a
    ``release`` cell over its ceiling is reported under ``over_ceiling`` with
    its owner (never a pass) and is a violation only with ``release=True``,
    as is a release cell nothing measured yet. Every idle row may not grow more
    than ``growth_limit_pct`` over the same row of ``published``.
    """
    violations: list[dict[str, Any]] = []
    over: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []

    def _breach(item: dict[str, Any], gate: str) -> None:
        if gate == "ci" or release:
            violations.append(item)
        else:
            over.append(item)

    def _unmeasured(item: dict[str, Any], gate: str) -> None:
        if gate == "ci" or release:
            violations.append({**item, "reason": "not measured"})
        else:
            pending.append({**item, "reason": "not measured on this tree"})

    idle = {row["variant"]: row for row in report.get("idle") or []}
    for name, cell in (budget.get("idle") or {}).items():
        base = {"cell": f"idle.{name}", "ceiling": cell["ceiling"], "gate": cell["gate"]}
        base["owner"] = cell.get("owner")
        row = idle.get(name)
        if row is None:
            _unmeasured(base, cell["gate"])
            continue
        for basis in cell["basis"]:
            measured = _basis_tokens(row, basis)
            item = {**base, "basis": basis, "measured": measured}
            checked.append(item)
            if measured is None:
                _unmeasured(item, cell["gate"])
            elif measured > cell["ceiling"]:
                _breach({**item, "reason": "over the ceiling"}, cell["gate"])

    for name, cell in (budget.get("result") or {}).items():
        item = {
            "cell": f"result.{name}",
            "ceiling": cell["ceiling"],
            "gate": cell["gate"],
            "owner": cell.get("owner"),
        }
        _unmeasured(item, cell["gate"])

    task = report.get("task")
    if task is not None:
        from benchmarks.call_budget import check_calls

        calls = check_calls(task["scenarios"], budget, release=release)
        violations.extend(calls["violations"])
        pending.extend(calls["pending"])
    elif budget.get("task"):
        for name, cell in budget["task"].items():
            item = {"cell": f"task.{name}", "ceiling": cell["ceiling"], "gate": cell["gate"]}
            _unmeasured({**item, "owner": cell.get("owner")}, cell["gate"])

    limit = float(budget.get("growth_limit_pct", 5.0))
    growth: list[dict[str, Any]] = []
    if published is not None:
        before = {row["variant"]: row for row in published.get("idle") or []}
        for variant, row in idle.items():
            old = before.get(variant)
            now, then = _headline_tokens(row), _headline_tokens(old) if old else None
            if now is None or not then:
                continue
            pct = round(100.0 * (now - then) / then, 2)
            entry = {"cell": f"growth.{variant}", "published": then, "measured": now, "pct": pct}
            growth.append(entry)
            if pct > limit:
                violations.append({**entry, "reason": f"grew more than {limit:g} %"})
    return {
        "violations": violations,
        "over_ceiling": over,
        "pending": pending,
        "checked": checked,
        "growth": growth,
        "growth_limit_pct": limit,
        "release": release,
        "ok": not violations,
    }


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

#: Lanes in run order; ``--lanes`` picks a subset (the budget needs idle + task).
LANES = ("idle", "discovery", "result", "task")


def _package_version() -> str | None:
    try:
        import tomllib

        with (ROOT / "pyproject.toml").open("rb") as stream:
            return str(tomllib.load(stream)["project"]["version"])
    except Exception:
        return None


def _environment() -> dict[str, Any]:
    import ezdxf
    import fastmcp

    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "fastmcp": fastmcp.__version__,
        "ezdxf": ezdxf.__version__,
        "package_version": _package_version(),
    }


async def run_suite(
    *,
    entities: int = DEFAULT_ENTITIES,
    turns: int = SESSION_TURNS,
    counter: Any | None = None,
    lanes: Sequence[str] = LANES,
) -> dict[str, Any]:
    """Run the lanes (all four by default) and hand back the report document.

    A lane left out of ``lanes`` is ``None`` in the report. Restores every
    global it touches: the forced backend env var, the tool profile and the
    discovery transform all go back the way they were, whatever happens in
    between.
    """
    unknown = sorted(set(lanes) - set(LANES))
    if unknown:
        raise ValueError(f"unknown lane(s) {unknown}; known: {list(LANES)}")
    srv = _load_server()
    counter = counter or RatioTokenCounter()
    previous_env = os.environ.get(BACKEND_ENV)
    previous_profile = config.settings.tool_profile
    os.environ[BACKEND_ENV] = "ezdxf"
    idle = discovery = result = task = None
    try:
        async with _client() as client:
            status = (await client.call_tool("system_status", {})).structured_content or {}
        require_ezdxf(status)
        backend = str(status["backend"])
        if "idle" in lanes:
            idle = await idle_lane(counter, turns)
        if "discovery" in lanes:
            discovery = await discovery_lane(counter, turns)
        if "result" in lanes:
            result = await result_lane(counter, turns, entities)
        if "task" in lanes:
            task = await task_lane(counter, turns)
    finally:
        srv._apply_discovery_mode("off")
        config.settings.tool_profile = previous_profile
        await srv._apply_tool_profile(previous_profile)
        if previous_env is None:
            os.environ.pop(BACKEND_ENV, None)
        else:
            os.environ[BACKEND_ENV] = previous_env

    return {
        "schema_version": SCHEMA_VERSION,
        "backend": backend,
        "lanes": [lane for lane in LANES if lane in lanes],
        "idle_basis": IDLE_HEADLINE_BASIS,
        "environment": _environment(),
        "counting": counting_block(counter),
        "session_model": {
            "turns": turns,
            "cache_read_multiplier": CACHE_READ_MULTIPLIER,
            "cache_write_multiplier": CACHE_WRITE_MULTIPLIER,
            "note": (
                "Uncached: the span is re-sent at full price every turn. Cached: one "
                "write at 1.25x, then reads at 0.1x. A tool prefix never mutates, so "
                "the cached column is the one a real client experiences."
            ),
        },
        "idle": idle,
        "discovery": discovery,
        "result": result,
        "task": task,
    }


# ---------------------------------------------------------------------------
# Human summary
# ---------------------------------------------------------------------------


def summarize(report: dict[str, Any]) -> str:
    """A short ASCII summary; the console this runs on is cp1254."""
    lines: list[str] = []
    counting = report["counting"]
    env = report["environment"]
    lines.append("AutoCAD MCP Pro - token cost")
    lines.append(f"  backend={report['backend']}  python={env['python']}  fastmcp={env['fastmcp']}")
    lines.append(
        "  unit: characters (measured exactly); tokens are an "
        f"{'estimate' if counting['tokens_are_estimates'] else 'exact count'} "
        f"via {counting['token_method']}"
    )
    lines.append("")

    turns = report["session_model"]["turns"]
    if report.get("idle"):
        _summarize_idle(report, lines, turns)
    if report.get("discovery"):
        _summarize_discovery(report, lines)
    if report.get("result"):
        _summarize_result(report, lines)
    if report.get("task"):
        _summarize_task(report, lines)
    return "\n".join(lines).rstrip("\n")


def _summarize_idle(report: dict[str, Any], lines: list[str], turns: int) -> None:
    lines.append("1. Idle cost - what a client sends the model on connect (prompt payload)")
    lines.append(
        f"  {'variant':15} {'tools':>5} {'chars':>9} {'tokens':>8} {'wire tok':>9} "
        f"{'uncached/' + str(turns) + 'turns':>16} {'cached/' + str(turns) + 'turns':>15}  caches?"
    )
    for row in report["idle"]:
        cost = row["cost"]
        smallest = min(int(key) for key in row["prompt_cache"]["cacheable"])
        caches = (
            "yes" if row["prompt_cache"]["cacheable"][str(smallest)] else "NO (below min prefix)"
        )
        lines.append(
            f"  {row['variant']:15} {row['advertised_tools']:5d} {row['chars']:9,d} "
            f"{row['tokens']['value']:8,d} {row['wire']['tokens']['value']:9,d} "
            f"{cost['session_uncached']:16,.0f} {cost['session_cached']:15,.0f}  {caches}"
        )
    lines.append(
        f"  tokens are the uncached per-request cost of the tools array (wire tok: the whole "
        f"tools/list payload); over {turns} turns prompt caching cuts it "
        f"~{1 / (1 - report['idle'][0]['cost']['session_saving_ratio']):.1f}x - "
        "unless the prefix is too small to cache at all."
    )
    lines.append("")


def _summarize_discovery(report: dict[str, Any], lines: list[str]) -> None:
    summary = report["discovery"]["summary"]
    lines.append(
        f"2. Per-discovery cost - one search_tools answer, limit={report['discovery']['limit']}"
    )
    for arm in report["discovery"]["arms"]:
        name = arm["arm"]
        tag = " (strawman)" if arm["strawman"] else ""
        lines.append(
            f"  {name:19} {summary['mean_chars'][name]:9,.0f} chars  "
            f"{summary['mean_tokens'][name]:7,.0f} tokens  "
            f"hit rate {summary['hit_rate'][name]:.0%}{tag}"
        )
    lines.append(
        f"  means over {summary['comparable_queries']}/{summary['queries']} queries "
        f"every arm answered; excluded: {', '.join(summary['excluded_queries']) or 'none'}"
    )
    lines.append("")


def _summarize_result(report: dict[str, Any], lines: list[str]) -> None:
    result = report["result"]
    lines.append(f"3. Result-side cost - entity_list over {result['entities_in_drawing']} entities")
    for row in result["scenarios"]:
        bbox = row["counterfactuals"]["without_bounding_box"]["saved_pct"]
        lines.append(
            f"  limit={row['limit']:<5} rows={row['rows_returned']:<5} "
            f"{row['chars']:8,d} chars  {row['tokens']['value']:7,d} tokens  "
            f"wire {row['wire_chars']:8,d} chars (sent twice)  "
            f"bounding_box {bbox:.0f}%"
        )
    floats = result["scenarios"][-1]["counterfactuals"]["floats_rounded_6dp"]["saved_pct"]
    lines.append(
        f"  where the bytes go: bounding_box "
        f"{result['scenarios'][-1]['counterfactuals']['without_bounding_box']['saved_pct']:.0f}%, "
        f"float precision beyond 6dp {floats:.0f}%"
    )
    projection = result["projection"]
    lines.append(
        f"  {projection['chars_per_entity']:.0f} chars/entity; "
        f"MAX_LIST_LIMIT={projection['config_max_list_limit']} would be "
        f"{projection['extrapolated_chars_at_config_cap']:,} chars, but the tool's "
        f"schema caps limit at {projection['schema_max_limit']}"
    )
    lines.append("")


def _summarize_task(report: dict[str, Any], lines: list[str]) -> None:
    task = report["task"]
    lines.append(
        f"4. Task cost - scripted jobs, prefix {task['prefix']['tokens']:,} tokens; "
        "naive = one tool per entity (labelled)"
    )
    for row in task["scenarios"]:
        for arm in row["arms"].values():
            if arm["status"] == "pending_tool":
                lines.append(
                    f"  {row['scenario']:22} {arm['arm']:7} pending_tool "
                    f"{arm['missing_tools'] or 'oracle not written'} (owner {arm['owner']})"
                )
                continue
            session = arm["session"]
            lines.append(
                f"  {row['scenario']:22} {arm['arm']:7} {arm['calls']:4d} calls "
                f"{arm['request_chars'] + arm['result_chars']:9,d} chars  session "
                f"{session['session_input_uncached']:12,.0f} uncached "
                f"{session['session_input_cached']:11,.0f} cached  {arm['status']}"
            )
    lines.append("")


def summarize_budget(checked: dict[str, Any]) -> str:
    lines = [f"Budget ({'release' if checked['release'] else 'ci'} gate)"]
    for item in checked["checked"]:
        measured = item.get("measured")
        if measured is not None and measured <= item["ceiling"]:
            lines.append(
                f"  ok      {item['cell']} [{item['basis']}] {measured:,} <= {item['ceiling']:,} "
                f"(gate {item['gate']})"
            )
    for item in checked["growth"]:
        lines.append(
            f"  {item['cell']:28} {item['published']:>8,} -> {item['measured']:>8,} "
            f"({item['pct']:+.2f} %, limit {checked['growth_limit_pct']:g} %)"
        )
    for item in checked["over_ceiling"]:
        lines.append(
            f"  OVER    {item['cell']} [{item.get('basis', '')}] {item.get('measured')} > "
            f"{item['ceiling']} (gate release, owner {item.get('owner')})"
        )
    for item in checked["pending"]:
        lines.append(f"  PENDING {item['cell']}: {item['reason']} (owner {item.get('owner')})")
    for item in checked["violations"]:
        lines.append(f"  FAIL    {item['cell']}: {item['reason']} {item.get('measured', '')}")
    lines.append("  ok" if checked["ok"] else "  FAILED")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure this server's token cost.")
    parser.add_argument("--json", action="store_true", help="print the JSON report on stdout")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"where to write the JSON report (default: {DEFAULT_OUTPUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--entities", type=int, default=DEFAULT_ENTITIES, help="entities in the result-lane drawing"
    )
    parser.add_argument(
        "--turns", type=int, default=SESSION_TURNS, help="requests in the modelled session"
    )
    parser.add_argument(
        "--tokenizer",
        choices=("ratio", "anthropic"),
        default="ratio",
        help="'ratio' (default, estimates) or 'anthropic' (real counts, needs credentials)",
    )
    parser.add_argument(
        "--model", default=DEFAULT_TOKENIZER_MODEL, help="model for --tokenizer anthropic"
    )
    parser.add_argument(
        "--lanes",
        default=",".join(LANES),
        help=f"comma-separated subset of {','.join(LANES)} (default: all)",
    )
    parser.add_argument(
        "--budget",
        type=Path,
        default=None,
        help="check the run against a budget file (benchmarks/token_budget.json): exit 1 "
        "on a ci ceiling breach, an unmeasured ci cell or growth over the limit",
    )
    parser.add_argument(
        "--published",
        type=Path,
        default=DEFAULT_PUBLISHED,
        help="the published report growth is measured against "
        f"(default: {DEFAULT_PUBLISHED.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="with --budget: release-gated cells over their ceiling or unmeasured fail too",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lanes = [lane for lane in args.lanes.split(",") if lane]
    if args.budget is not None and args.tokenizer != "ratio":
        # The ceilings were written in ratio/v1 tokens; a real count is another unit.
        print(
            "token_suite: --budget compares ratio/v1 estimates; use --tokenizer ratio",
            file=sys.stderr,
        )
        return 2
    try:
        counter = build_counter(args.tokenizer, model=args.model)
    except (RuntimeError, ValueError) as exc:
        # A refused tokenizer is a usage error, not a crash: say what is missing
        # rather than printing a traceback over an unavailable dependency.
        print(f"token_suite: {exc}", file=sys.stderr)
        return 2
    report = asyncio.run(
        run_suite(entities=args.entities, turns=args.turns, counter=counter, lanes=lanes)
    )
    checked = None
    if args.budget is not None:
        budget = json.loads(args.budget.read_text(encoding="utf-8"))
        published = None
        if args.published is not None and args.published.is_file():
            published = json.loads(args.published.read_text(encoding="utf-8"))
        checked = check_budget(report, budget, published, release=args.release)
        if published is None:
            checked["violations"].append(
                {"cell": "growth", "reason": f"no published report at {args.published}"}
            )
            checked["ok"] = False
        report["budget"] = {"file": str(args.budget), **checked}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    if args.json:
        print(rendered)
    else:
        print(summarize(report))
        if checked is not None:
            print("\n" + summarize_budget(checked))
        if args.out is not None:
            print(f"\nJSON report: {args.out}")
    return 0 if checked is None or checked["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
