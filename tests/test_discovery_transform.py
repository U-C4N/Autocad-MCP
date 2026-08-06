"""The discovery search transform must find tools a drafter can actually name.

Stock ``BM25SearchTransform`` indexes tool name + description + parameter names
+ parameter descriptions and nothing else. Measured against this server's live
catalog, that means ``PEDIT``, ``WBLOCK``, ``OVERKILL``,
``LAYTRANS`` and ``MATCHPROP`` all score df=0 and return *nothing*,
and "how many entities are on the GEOMETRY layer" returns ``entity_delete_many``
as its top hit. Both are proved here against the real catalog before anything is
asserted about the replacement.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search.base import serialize_tools_for_output_json

import config
import server
from discovery.serialize import serialize_search_results
from discovery.transform import CadSearchTransform, tool_risk
from tests.data.discovery_golden import ALL_CASES, HOLDOUT_CASES, TUNING_CASES

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def catalog():
    """The real, untransformed 131-tool catalog.

    Synchronous on purpose: ``_registered_tools`` only reads the in-process
    component registry, and a module-scoped *async* fixture would need its own
    event-loop scope under ``asyncio_mode = strict``.
    """
    return asyncio.run(server._registered_tools())


@pytest.fixture
def transform():
    return CadSearchTransform()


@pytest.fixture
def stock():
    return BM25SearchTransform()


def _names(tools) -> list[str]:
    return [t.name for t in tools]


# ── the df=0 vocabulary gap ─────────────────────────────────────────────────

# Verified against the live catalog: stock BM25 returns *zero* hits for every
# one of these. FILLET is deliberately absent — it is already indexed (df=2,
# the tool is literally named entity_fillet), so citing it would overstate the
# gap.
#
# BPOLY and QSELECT left this list in v1.5.0 (M8): `boundary_trace` and
# `selection_filter` name those commands in their own docstrings, so stock BM25
# now finds them and the gap is genuinely closed for those two. Keeping them
# here would have made the corpus look more load-bearing than it is. The
# remaining five are still unreachable without it.
DF_ZERO_COMMANDS = [
    ("PEDIT", "entity_edit_geometry"),
    ("WBLOCK", "block_create_from_entities"),
    ("OVERKILL", "drawing_refine"),
    ("LAYTRANS", "template_apply_layers"),
    ("MATCHPROP", "entity_set_properties"),
]


@pytest.mark.parametrize(("command", "expected"), DF_ZERO_COMMANDS)
async def test_stock_bm25_cannot_find_these_commands_at_all(stock, catalog, command, expected):
    """Guard the guard: the gap is real, not an artefact of a weak assertion."""
    assert await stock._search(catalog, command) == []


@pytest.mark.parametrize(("command", "expected"), DF_ZERO_COMMANDS)
async def test_autocad_command_names_resolve(transform, catalog, command, expected):
    assert _names(transform.rank(catalog, command).hits)[0] == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("DLI", "dimension_linear"),
        ("REC", "entity_create_rectangle"),
        ("CHA", "entity_chamfer"),
        ("TR", "entity_trim"),
        ("MI", "entity_mirror"),
        ("XL", "construction_xline"),
    ],
)
async def test_short_command_aliases_resolve(transform, catalog, command, expected):
    """Two- and three-letter aliases are what drafters actually type."""
    assert _names(transform.rank(catalog, command).hits)[0] == expected


async def test_searchable_text_extends_the_stock_document(transform, catalog):
    """The hook adds alias vocabulary; it never drops the stock text."""
    from fastmcp.server.transforms.search.base import _extract_searchable_text

    tool = next(t for t in catalog if t.name == "entity_create_polyline")
    text = transform._extract_searchable_text(tool)
    assert _extract_searchable_text(tool) in text
    assert "PLINE" in text
    assert "closed profile" in text


async def test_a_tool_with_no_alias_record_is_still_indexed(transform, catalog):
    """An un-aliased tool must stay findable, not vanish from the index."""
    tool = next(t for t in catalog if t.name == "entity_create_line")
    stripped = tool.model_copy(update={"name": "zzz_unaliased_probe"})
    hits = transform.rank([*catalog, stripped], "zzz_unaliased_probe").hits
    assert _names(hits)[0] == "zzz_unaliased_probe"


# ── the two boosts ──────────────────────────────────────────────────────────


async def test_an_exact_command_outranks_tools_that_share_a_description_word(transform, catalog):
    """ "CIRCLE" is the command; a dozen tools merely mention circles."""
    hits = _names(transform.rank(catalog, "CIRCLE", limit=5).hits)
    assert hits[0] == "entity_create_circle"


async def test_a_command_name_that_is_also_an_english_word_stays_english(transform, catalog):
    """LAYER is a command *and* a noun.

    Boosting on a bare lowercase "layer" made layer_modify / layer_list /
    layer_create the top three for this query and pushed layer_delete out of
    the answer entirely — exactly backwards. Measured regression, not a
    hypothetical.
    """
    hits = _names(transform.rank(catalog, "delete the layer called GEOMETRY", limit=8).hits)
    assert "layer_delete" in hits
    assert not {"layer_create", "layer_modify", "layer_list"} & set(hits[:3])


async def test_the_same_command_in_capitals_is_treated_as_a_command(transform, catalog):
    """The other half of the rule: LAYER typed as a command routes to LAYER tools."""
    hits = set(_names(transform.rank(catalog, "LAYER", limit=5).hits))
    assert {"layer_create", "layer_list", "layer_modify"} <= hits


async def test_a_lone_lowercase_command_still_counts_as_a_command(transform, catalog):
    """Nobody shouts when the command is the whole message."""
    assert _names(transform.rank(catalog, "bpoly").hits)[0] == "boundary_trace"


async def test_articles_do_not_break_synonym_phrase_matching(transform, catalog):
    """The corpus says "delete a layer"; users say "delete the layer"."""
    assert "layer_delete" in _names(transform.rank(catalog, "delete the layer", limit=5).hits)


# ── the risk filter ─────────────────────────────────────────────────────────

COUNTING_QUERY = "how many entities are on the GEOMETRY layer"


async def test_stock_bm25_answers_a_counting_question_with_destructive_tools(stock, catalog):
    """Guard the guard: the destructive-hit problem is measured, not assumed.

    This asserted the top hit was ``entity_delete_many`` until
    ``analysis_measure_entity`` was added and displaced it to second. The
    ranking order is incidental; what the risk ceiling exists to prevent is a
    read-only question being answered with tools that destroy geometry, and that
    is still exactly what stock BM25 does here.
    """
    hits = await stock._search(catalog, COUNTING_QUERY)
    risky = [t.name for t in hits if tool_risk(t) == "destructive"]
    assert risky, f"expected destructive hits among {_names(hits)}"
    assert "entity_delete_many" in risky


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("drawing_info", "read"),
        ("analysis_entity_stats", "read"),
        ("entity_create_line", "write"),
        ("entity_delete_many", "destructive"),
        ("layer_delete", "destructive"),
        ("transaction_rollback", "destructive"),
    ],
)
async def test_risk_is_derived_from_the_mcp_annotations(catalog, name, expected):
    tool = next(t for t in catalog if t.name == name)
    assert tool_risk(tool) == expected


async def test_read_ceiling_keeps_destructive_tools_out_of_a_counting_answer(transform, catalog):
    outcome = transform.rank(catalog, COUNTING_QUERY, risk="read")
    assert [tool_risk(t) for t in outcome.hits] == ["read"] * len(outcome.hits)
    assert "analysis_entity_stats" in _names(outcome.hits)


async def test_write_ceiling_allows_mutation_but_not_destruction(transform, catalog):
    outcome = transform.rank(catalog, "delete the entities on the GEOMETRY layer", risk="write")
    assert "destructive" not in {tool_risk(t) for t in outcome.hits}


async def test_the_filter_frees_slots_rather_than_shrinking_the_answer(transform, catalog):
    """Filtering must promote lower-ranked safe tools, not just delete rows."""
    unfiltered = transform.rank(catalog, COUNTING_QUERY, limit=5)
    filtered = transform.rank(catalog, COUNTING_QUERY, limit=5, risk="read")
    assert len(unfiltered.hits) == 5
    assert len(filtered.hits) == 5
    assert set(_names(filtered.hits)) - set(_names(unfiltered.hits))


async def test_the_filter_records_exactly_what_it_hid(transform, catalog):
    outcome = transform.rank(catalog, COUNTING_QUERY, limit=5, risk="read")
    assert "entity_delete_many" in _names(outcome.hidden)
    # Only tools the caller would otherwise have seen — not the whole catalog.
    assert len(outcome.hidden) <= 5


async def test_nothing_is_recorded_as_hidden_when_the_filter_is_off(transform, catalog):
    assert transform.rank(catalog, COUNTING_QUERY, limit=5).hidden == ()


# ── the risk filter must be visible in the payload ──────────────────────────


class _FakeServer:
    def __init__(self, tools):
        self._tools = list(tools)

    async def list_tools(self, run_middleware: bool = True):
        return list(self._tools)


class _FakeContext:
    """Just enough Context for ``_get_visible_tools`` to reach the catalog."""

    def __init__(self, tools):
        self.fastmcp = _FakeServer(tools)


async def _search_payload(transform, catalog, query: str, **kwargs):
    tool = transform._make_search_tool()
    return await tool.fn(query=query, ctx=_FakeContext(catalog), **kwargs)


async def test_the_payload_says_when_the_risk_filter_removed_hits(transform, catalog):
    payload = await _search_payload(transform, catalog, COUNTING_QUERY, risk="read")
    notice = [line for line in payload.splitlines() if line.startswith("Risk filter:")]
    assert len(notice) == 1
    assert "entity_delete_many" in notice[0]
    assert 'risk="any"' in notice[0]


async def test_a_mixed_intent_is_never_silently_halved(transform, catalog):
    """ "list the layers then delete GEOMETRY" is half a read and half a delete."""
    payload = await _search_payload(
        transform, catalog, "list the layers then delete GEOMETRY", risk="read"
    )
    assert "layer_list" in payload
    notice = next(line for line in payload.splitlines() if line.startswith("Risk filter:"))
    assert "entity_delete" in notice


async def test_no_notice_when_the_filter_removed_nothing(transform, catalog):
    payload = await _search_payload(transform, catalog, COUNTING_QUERY, risk="any")
    assert "Risk filter:" not in payload


async def test_a_structured_serializer_gets_the_notice_as_a_sibling_key(catalog):
    """There is no line to append to when the serializer returns JSON."""
    structured = CadSearchTransform(search_result_serializer=serialize_tools_for_output_json)
    payload = await _search_payload(structured, catalog, COUNTING_QUERY, risk="read")
    assert isinstance(payload["results"], list)
    assert "entity_delete_many" in payload["notice"]


async def test_a_structured_serializer_returns_bare_results_when_nothing_was_hidden(catalog):
    structured = CadSearchTransform(search_result_serializer=serialize_tools_for_output_json)
    payload = await _search_payload(structured, catalog, COUNTING_QUERY, risk="any")
    assert isinstance(payload, list)


# ── the search tool's own schema ────────────────────────────────────────────


async def test_search_tool_advertises_limit_and_risk(transform):
    properties = transform._make_search_tool().parameters["properties"]
    assert set(properties) == {"query", "limit", "risk"}
    assert properties["risk"]["enum"] == ["any", "write", "read"]
    assert properties["limit"]["default"] == transform._max_results


@pytest.mark.parametrize(("asked", "served"), [(999, 25), (0, 1), (-4, 1), (3, 3)])
async def test_limit_is_clamped_to_a_usable_range(transform, catalog, asked, served):
    outcome = transform.rank(catalog, "line", limit=asked)
    assert len(outcome.hits) == served


async def test_an_unknown_risk_value_is_rejected_rather_than_ignored(transform, catalog):
    with pytest.raises(ValueError, match="risk"):
        transform.rank(catalog, "line", risk="mostly-harmless")


# ── the compact serializer ──────────────────────────────────────────────────


def _tools(catalog, *names):
    by_name = {t.name: t for t in catalog}
    return [by_name[n] for n in names]


async def test_one_card_per_tool_on_one_line_each(catalog):
    tools = _tools(catalog, "entity_create_circle", "entity_delete", "drawing_info")
    lines = serialize_search_results(tools).splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("entity_create_circle")


async def test_a_card_carries_name_summary_and_required_params(catalog):
    (line,) = serialize_search_results(_tools(catalog, "entity_create_circle")).splitlines()
    assert "entity_create_circle" in line
    assert "req: cx, cy, radius" in line


async def test_a_card_marks_the_risk_level(catalog):
    text = serialize_search_results(
        _tools(catalog, "drawing_info", "entity_create_circle", "entity_delete")
    )
    assert "drawing_info (read)" in text
    assert "entity_create_circle (write)" in text
    assert "entity_delete (destructive)" in text


async def test_a_card_carries_the_autocad_command(catalog):
    (line,) = serialize_search_results(_tools(catalog, "entity_chamfer")).splitlines()
    assert "acad: CHAMFER" in line


async def test_the_cad_meta_summary_wins_over_the_docstring(catalog):
    """@cad_tool exists to give a hit a hand-written one-liner."""
    tool = next(t for t in catalog if t.name == "transaction_begin")
    summary = (tool.meta or {})["cad"]["summary"]
    assert summary in serialize_search_results([tool])


async def test_the_first_docstring_line_is_the_fallback_summary(catalog):
    """A card still reads well for a tool that carries no @cad_tool summary.

    The fallback is exercised by stripping the meta channel off a real tool
    rather than by naming one that happens to lack it: the @cad_tool rollout
    is filling the catalog in, so "a tool without cad meta" is not a stable
    fixture, while the serializer's fallback branch has to keep working for
    any tool a transform hands it (a mounted server's, say).
    """
    tool = next(t for t in catalog if t.name == "entity_create_circle")
    bare = tool.model_copy(update={"meta": None})
    first_line = bare.description.strip().splitlines()[0]
    assert first_line in serialize_search_results([bare])


async def test_a_long_summary_is_truncated_on_a_word_boundary(catalog):
    tool = next(t for t in catalog if t.name == "entity_create_circle")
    verbose = tool.model_copy(update={"description": "word " * 200, "meta": None})
    (line,) = serialize_search_results([verbose]).splitlines()
    assert len(line) < 250
    assert line.count("...") == 1


async def test_a_zero_argument_tool_says_so(catalog):
    (line,) = serialize_search_results(_tools(catalog, "drawing_info")).splitlines()
    assert "req: (none)" in line


async def test_an_empty_result_set_is_explicit():
    assert serialize_search_results([]) == "No tools matched the query."


# Budgets read off the measured output, not chosen first and enforced onto the
# renderer. Over the 15 REALISTIC_QUERIES below at limit=5 the worst rendered
# payload measured 798 characters and the worst single card in the whole
# 131-tool catalog measured 191 (p50 123, p90 160). The thresholds sit ~30%
# above those so an added tool with a long docstring does not turn into a
# spurious CI failure — a real regression would be a multiple, not a nudge.
MAX_CARD_CHARS = 256
MAX_PAYLOAD_CHARS = 1024

REALISTIC_QUERIES = [
    "FILLET",
    "BPOLY",
    "round the corner",
    "bill of materials",
    "how many entities are on the GEOMETRY layer",
    "draw a hole in the plate",
    "put a leader note on the chamfer",
    "dimension everything with a chain",
    "make a title block",
    "save the drawing as a pdf",
    "select every circle on the GEOMETRY layer",
    "gear",
    "keyway in the hub bore",
    "screenshot",
    "check the drawing before finalising",
]


async def test_no_single_card_exceeds_the_measured_budget(catalog):
    """Checked over every registered tool, not just the ones a query happens to hit."""
    worst = max(catalog, key=lambda tool: len(serialize_search_results([tool])))
    assert len(serialize_search_results([worst])) <= MAX_CARD_CHARS, worst.name


@pytest.mark.parametrize("query", REALISTIC_QUERIES)
async def test_a_five_hit_payload_stays_inside_the_measured_budget(transform, catalog, query):
    hits = transform.rank(catalog, query, limit=5).hits
    assert len(serialize_search_results(hits)) <= MAX_PAYLOAD_CHARS


async def test_the_serializer_is_far_smaller_than_stock_json(transform, catalog):
    """Payload size is a real gain, but it is *not* the differentiator.

    Stock BM25 with this same serializer would close most of the gap; what it
    cannot do is find BPOLY at all. Asserted as a ratio so it stays honest.
    """
    hits = transform.rank(catalog, "round the corner", limit=5).hits
    compact = len(serialize_search_results(hits))
    stock_json = len(json.dumps(serialize_tools_for_output_json(hits)))
    assert compact < stock_json / 5


# ── the golden set ──────────────────────────────────────────────────────────

TOP_K = 3

# Measured on the tuning set with the scorer frozen: top-1 47/51 (92.2%),
# top-3 50/51 (98.0%). The bars sit below those, not on them.
TUNING_TOP1_BAR = 0.85
TUNING_TOP3_BAR = 0.94

# Declared *before* the holdout set was measured for the first time, and not
# revised afterwards. Lower than the tuning bar on purpose: a ranker that
# scores the same on data it never saw is the exception, not the expectation.
HOLDOUT_TOP3_BAR = 0.80

# A tuning case that fails for a reason this module cannot fix. "smaller" and
# "by half" appear nowhere in discovery/aliases.py (which has "shrink" but not
# "smaller"), so no scoring change reaches entity_scale — only a corpus edit
# does, and the corpus is a separate deliverable. Left in the set rather than
# quietly deleted: it is the honest ceiling of an alias-driven approach.
KNOWN_CORPUS_GAPS = {"make the whole shape smaller by half"}


def _tuning_params():
    return [
        pytest.param(
            case,
            marks=(
                [pytest.mark.xfail(reason="vocabulary missing from discovery/aliases.py")]
                if case.query in KNOWN_CORPUS_GAPS
                else []
            ),
            id=str(case),
        )
        for case in TUNING_CASES
    ]


async def _accuracy(transform, stock, catalog, cases):
    """(mine_top1, mine_topk, stock_top1, stock_topk, misses) over ``cases``."""
    mine1 = mine_k = stock1 = stock_k = 0
    misses = []
    for case in cases:
        # risk="any" for both, so the A/B never credits the risk filter.
        ours = _names(transform.rank(catalog, case.query, limit=5).hits)
        theirs = _names(await stock._search(catalog, case.query))
        mine1 += ours[:1] == [case.expect]
        stock1 += theirs[:1] == [case.expect]
        if case.expect in ours[:TOP_K]:
            mine_k += 1
        else:
            misses.append(f"{case.query!r} want={case.expect} got={ours[:TOP_K]}")
        stock_k += case.expect in theirs[:TOP_K]
    return mine1, mine_k, stock1, stock_k, misses


@pytest.mark.parametrize("case", _tuning_params())
async def test_tuning_case_finds_its_tool(transform, catalog, case):
    hits = _names(transform.rank(catalog, case.query, limit=5).hits)
    assert case.expect in hits[:TOP_K], hits


async def test_tuning_accuracy_holds_its_measured_bar(transform, stock, catalog):
    mine1, mine_k, _, _, misses = await _accuracy(transform, stock, catalog, TUNING_CASES)
    n = len(TUNING_CASES)
    assert mine1 / n >= TUNING_TOP1_BAR, f"top-1 {mine1}/{n}"
    assert mine_k / n >= TUNING_TOP3_BAR, f"top-{TOP_K} {mine_k}/{n}; missed {misses}"


async def test_holdout_accuracy_clears_the_bar_declared_before_it_was_measured(
    transform, stock, catalog
):
    _, mine_k, _, _, misses = await _accuracy(transform, stock, catalog, HOLDOUT_CASES)
    n = len(HOLDOUT_CASES)
    assert mine_k / n >= HOLDOUT_TOP3_BAR, f"top-{TOP_K} {mine_k}/{n}; missed {misses}"


async def test_it_beats_stock_bm25_on_the_whole_golden_set(transform, stock, catalog):
    """The A/B. Both sides run unfiltered, so this measures vocabulary and
    ranking only — none of the credit goes to the risk filter.

    Scored on top-1, not a top-`TOP_K` ratio. A ratio between two scores over a
    shared corpus is not a property of this transform: improving an unrelated
    tool's docstring adds vocabulary to *both* indexes, so a `mine_k >= 2 *
    stock_k` assertion moves when stock gets **better**, which is a good outcome
    being reported as a regression. It fired exactly that way when the undo/redo
    docstrings were written. Top-1 is also the number that matters — a search
    tool returns one answer to act on.
    """
    mine1, mine_k, stock1, stock_k, _ = await _accuracy(transform, stock, catalog, ALL_CASES)
    n = len(ALL_CASES)
    report = (
        f"top-1 mine {mine1}/{n} vs stock {stock1}/{n}; "
        f"top-{TOP_K} mine {mine_k}/{n} vs stock {stock_k}/{n}"
    )
    assert mine1 >= 2 * stock1, report
    assert mine_k > stock_k, report


async def test_stock_bm25_cannot_answer_a_fifth_of_the_set_at_all(transform, stock, catalog):
    """The thesis, stated as the thing no amount of tuning can change.

    The corpus gap is *data*, not parameters: AutoCAD command names appear in
    none of the tool descriptions, so those queries score df=0 and stock returns
    an empty list. No serializer and no BM25 constant invents vocabulary that is
    not in the index — which is why the alias corpus, not the ranking, is what
    this transform actually contributes.
    """
    empty_stock = [c.query for c in ALL_CASES if not await stock._search(catalog, c.query)]
    empty_mine = [c.query for c in ALL_CASES if not transform.rank(catalog, c.query, limit=5).hits]

    assert len(empty_stock) >= 15, f"only {len(empty_stock)} empty — has the corpus changed?"
    assert empty_mine == [], f"the alias corpus must cover every case; empty: {empty_mine}"


COUNTING_CASES = [case for case in ALL_CASES if case.kind == "counting"]


@pytest.mark.parametrize("case", COUNTING_CASES, ids=str)
async def test_a_counting_question_never_answers_with_a_destructive_tool(transform, catalog, case):
    outcome = transform.rank(catalog, case.query, limit=5, risk=case.risk)
    assert case.risk == "read"
    assert "destructive" not in {tool_risk(t) for t in outcome.hits}
    assert case.expect in _names(outcome.hits)[:TOP_K]


async def test_the_risk_ceiling_is_what_removes_the_delete_tool(transform, catalog):
    """Not the ranking — the ranking still surfaces it, and says that it did."""
    query = "how many entities are on the GEOMETRY layer"
    assert "entity_delete_many" in _names(transform.rank(catalog, query, limit=5).hits)
    filtered = transform.rank(catalog, query, limit=5, risk="read")
    assert "entity_delete_many" not in _names(filtered.hits)
    assert "entity_delete_many" in filtered.notice()


# ── DISCOVERY_MODE ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _restore_discovery_mode():
    """Attaching a transform mutates the shared server; always put it back."""
    yield
    server._apply_discovery_mode("off")


async def test_discovery_is_off_by_default(monkeypatch):
    """v1.5.0 ships the flag, not the switch: turning it on is a wire-level
    breaking change for every connected client and needs its own migration."""
    monkeypatch.delenv("DISCOVERY_MODE", raising=False)
    assert config.Settings().discovery_mode == "off"


async def test_the_env_var_selects_search_mode(monkeypatch):
    monkeypatch.setenv("DISCOVERY_MODE", "SEARCH")
    assert config.Settings().discovery_mode == "search"


async def test_off_leaves_the_whole_catalog_on_the_wire():
    server._apply_discovery_mode("off")
    async with Client(server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert len(names) > 100
    assert "search_tools" not in names


async def test_search_replaces_the_catalog_with_the_two_synthetic_tools():
    server._apply_discovery_mode("search")
    async with Client(server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"search_tools", "call_tool"}


async def test_search_mode_is_reversible_and_attaches_only_once():
    server._apply_discovery_mode("search")
    server._apply_discovery_mode("search")
    server._apply_discovery_mode("off")
    async with Client(server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert len(names) > 100


async def test_an_unknown_discovery_mode_falls_back_to_off():
    assert server._apply_discovery_mode("semantic-vector-magic") == "off"
    async with Client(server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert len(names) > 100


async def test_the_search_tool_answers_on_the_wire():
    """End to end: a real client, a real AutoCAD command, a real payload."""
    server._apply_discovery_mode("search")
    async with Client(server.mcp) as client:
        result = await client.call_tool("search_tools", {"query": "BPOLY"})
    assert "boundary_trace" in result.content[0].text


async def test_the_risk_notice_survives_to_the_wire():
    server._apply_discovery_mode("search")
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "search_tools",
            {"query": "how many entities are on the GEOMETRY layer", "risk": "read"},
        )
    text = result.content[0].text
    assert "analysis_entity_stats" in text
    assert "Risk filter:" in text
    assert "entity_delete_many" in text


async def test_search_cannot_surface_a_tool_the_profile_disabled():
    """Capability-aware discovery has to survive the transform.

    The 3D solid tools are registered but disabled unless ENABLE_3D=true, so
    searching for one must come back empty rather than advertising a call that
    would only be rejected. This works because the search reads the *filtered*
    catalog through _get_visible_tools, not the raw registry.
    """
    server._apply_discovery_mode("search")
    async with Client(server.mcp) as client:
        result = await client.call_tool("search_tools", {"query": "BOX", "limit": 10})
    assert "solid_box" not in result.content[0].text


async def test_limit_reaches_the_wire():
    server._apply_discovery_mode("search")
    async with Client(server.mcp) as client:
        result = await client.call_tool("search_tools", {"query": "layer", "limit": 2})
    assert len(result.content[0].text.splitlines()) == 2


async def test_search_mode_does_not_shrink_what_the_server_reports_about_itself():
    """system_about reads the untransformed registry; prove it under the transform."""
    server._apply_discovery_mode("search")
    assert await server._registered_tool_count() > 100
    assert len(await server._tool_groups()) > 5
