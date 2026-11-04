"""Every token claim in this release has to be measured, and labelled as what it is.

These tests guard the *method*, not the numbers. The numbers move with the catalog
(add a tool, the idle payload grows); the method must not move at all:

* characters are the primary, exact unit; tokens are always marked as an estimate
  with the divisor and its provenance attached,
* no single flat chars/token divisor is ever applied across formats (that error
  flatters whichever format the author wants to win),
* every payload is priced twice - uncached and prompt-cached - because a
  never-mutating tool prefix is cacheable and every real client caches it,
* the stock-BM25 arm of the discovery A/B is *configured* with the same compact
  serializer ours uses, so the comparison measures the ranker rather than the
  renderer,
* the suite measures the headless engine and leaves the shared server as it
  found it.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import config
import server
from benchmarks import token_suite as ts

# A small drawing keeps the module fast; the published run uses the default 300.
TEST_ENTITIES = 40

# Captured at import, i.e. before the suite has had a chance to pin the backend.
#: ``AUTOCAD_MCP_BACKEND`` straddling the suite run. Both are captured *around*
#: ``run_suite`` rather than at import or in the test body, because the suite is
#: the only thing being measured here: the conftest pins every test to the
#: headless backend, and — since pytest builds higher-scoped fixtures first —
#: that pin lands after this module-scoped fixture. Comparing the test-time value
#: against an import-time one measures the conftest, not the restore.
BACKEND_ENV_BEFORE: str | None = None
BACKEND_ENV_AFTER: str | None = None


@pytest.fixture(scope="module")
def report() -> dict:
    """One real run of the whole suite, shared by every test in this module.

    Synchronous on purpose: ``asyncio_mode = strict`` makes a module-scoped async
    fixture need its own event-loop scope, and the suite owns its loop anyway.
    """
    global BACKEND_ENV_BEFORE, BACKEND_ENV_AFTER
    BACKEND_ENV_BEFORE = os.environ.get(ts.BACKEND_ENV)
    try:
        return asyncio.run(ts.run_suite(entities=TEST_ENTITIES))
    finally:
        BACKEND_ENV_AFTER = os.environ.get(ts.BACKEND_ENV)


def _rows(report: dict) -> list[dict]:
    """Every measured row in the report, whatever lane it came from."""
    rows = list(report["idle"])
    rows.extend(row["wire"] for row in report["idle"])
    for arm in report["discovery"]["arms"]:
        rows.extend(arm["queries"])
    rows.extend(report["result"]["scenarios"])
    return rows


# -- the report contract ----------------------------------------------------


def test_the_report_is_versioned_and_attributable(report):
    assert report["schema_version"] == ts.SCHEMA_VERSION
    env = report["environment"]
    for key in ("python", "implementation", "platform", "machine", "fastmcp", "ezdxf"):
        assert env[key], f"environment fingerprint is missing {key}"


def test_the_report_is_json_serializable(report):
    assert json.loads(json.dumps(report, ensure_ascii=False)) == report


def test_the_measured_surface_is_the_headless_engine(report):
    """A run that quietly drove live AutoCAD would be measuring a different server."""
    assert report["backend"] == "ezdxf"


# -- counting ---------------------------------------------------------------


def test_the_report_states_how_it_counted(report):
    counting = report["counting"]
    assert counting["primary_unit"] == "characters"
    assert counting["token_method"]
    assert counting["tokens_are_estimates"] is True
    assert counting["method_note"]


def test_no_single_flat_divisor_is_applied_across_formats():
    """The measured error of the earlier design pass, guarded."""
    ratios = {name: ratio.chars_per_token for name, ratio in ts.CHARS_PER_TOKEN.items()}
    assert len(set(ratios.values())) > 1, ratios
    assert ratios["json_schema"] != ratios["compact_text"]
    assert 4.0 not in ratios.values(), "chars/4 is the divisor this harness exists to avoid"


def test_the_same_text_costs_differently_in_different_formats():
    counter = ts.RatioTokenCounter()
    text = "x" * 4190
    assert counter.count(text, "json_schema") != counter.count(text, "compact_text")


def test_an_unlabelled_format_is_refused_rather_than_guessed():
    counter = ts.RatioTokenCounter()
    with pytest.raises(KeyError):
        counter.count("some payload", "prose")


def test_every_ratio_carries_its_provenance_and_confidence():
    for name, ratio in ts.CHARS_PER_TOKEN.items():
        assert ratio.provenance, name
        assert ratio.confidence in ts.RATIO_CONFIDENCE, name


def test_characters_are_exact_and_tokens_are_marked_estimated():
    counter = ts.RatioTokenCounter()
    text = '{"name":"entity_create_circle"}'
    row = ts.measure(text, "json_schema", counter)
    assert row["chars"] == len(text)
    assert row["bytes_utf8"] == len(text.encode("utf-8"))
    assert row["tokens"]["estimated"] is True
    assert row["tokens"]["chars_per_token"] == ts.CHARS_PER_TOKEN["json_schema"].chars_per_token


def test_an_unavailable_real_tokenizer_fails_loudly(monkeypatch):
    """The fallback that must never be silent."""
    monkeypatch.setattr(ts, "_anthropic_client", lambda: None)
    with pytest.raises(RuntimeError, match="anthropic"):
        ts.build_counter("anthropic")


# -- cached / uncached ------------------------------------------------------


def test_every_measured_row_is_priced_cached_and_uncached(report):
    for row in _rows(report):
        cost = row["cost"]
        assert cost["uncached_input_tokens_per_request"] >= cost["cached_input_tokens_per_request"]
        assert cost["session_uncached"] > cost["session_cached"]


def test_the_cache_arithmetic_follows_the_published_multipliers():
    cost = ts.cost_model(1000, turns=10)
    assert cost["uncached_input_tokens_per_request"] == 1000
    assert cost["cached_input_tokens_per_request"] == pytest.approx(100.0)
    assert cost["session_uncached"] == pytest.approx(10_000)
    # one cache write (1.25x) plus nine cache reads (0.1x each)
    assert cost["session_cached"] == pytest.approx(1250 + 9 * 100)


# -- lane 1: idle cost ------------------------------------------------------


def test_the_idle_lane_covers_the_advertised_surfaces_and_the_instructions(report):
    """Default, lean, TOOL_PACKS=core, search - the two single-vertical clients:
    TOOL_PACKS=core,mech (the default surface minus exactly the pid, settings,
    arch and plant packs) and TOOL_PACKS=core,arch (minus pid, settings, mech
    and plant) - and, since 1.8-14, the server instructions."""
    variants = {row["variant"]: row for row in report["idle"]}
    assert set(variants) == {
        "default",
        "lean",
        "packs_core",
        "packs_core_mech",
        "packs_core_arch",
        "search",
        "search_pinned",
        "instructions",
    }
    assert variants["default"]["advertised_tools"] > 100
    # + describe_tool: off mode lists it wherever a description is split (W3)
    assert variants["lean"]["advertised_tools"] == len(server.LEAN_TOOL_NAMES) + 1
    # search_tools + call_tool + describe_tool
    assert variants["search"]["advertised_tools"] == 3
    # 1.8-11: + the three default pins (cad_batch, drawing_info, entity_list)
    assert variants["search_pinned"]["advertised_tools"] == 6
    assert variants["search"]["chars"] < variants["search_pinned"]["chars"]
    packs = server.PACK_TOOL_NAMES
    assert variants["packs_core"]["advertised_tools"] == (
        variants["default"]["advertised_tools"]
        - sum(len(packs[name]) for name in ("pid", "settings", "mech", "arch", "plant"))
    )
    assert variants["instructions"]["kind"] == "instructions"
    # measured on the full-catalog surface (the longest text the builder writes)
    assert variants["instructions"]["chars"] == len(
        server._server_instructions("off", "full", set(server.TOOL_PACK_NAMES))
    )
    assert variants["packs_core_mech"]["advertised_tools"] == (
        variants["default"]["advertised_tools"]
        - len(packs["pid"])
        - len(packs["settings"])
        - len(packs["arch"])
        - len(packs["plant"])
    )
    assert variants["packs_core_arch"]["advertised_tools"] == (
        variants["default"]["advertised_tools"]
        - len(packs["pid"])
        - len(packs["settings"])
        - len(packs["mech"])
        - len(packs["plant"])
    )
    for narrowed in ("packs_core_mech", "packs_core_arch"):
        assert (
            variants["default"]["chars"]
            > variants[narrowed]["chars"]
            > variants["lean"]["chars"]
            > variants["search"]["chars"]
        )


def test_the_idle_headline_is_what_the_model_pays_with_the_wire_beside_it(report):
    """1.8-14: the headline prices the tools array a client forwards (prompt
    payload); tools/list also carries annotations, titles and _meta, which stay
    measured as the ``wire`` column - the basis of 1.7.0's 89,226 and 503."""
    assert report["idle_basis"] == "prompt_payload"
    for row in report["idle"]:
        assert row["chars"] == row["prompt_chars"]
        assert row["wire"]["chars"] == row["wire_chars"]
        if row["kind"] == "tools_list":
            assert row["basis"] == "prompt_payload"
            assert 0 < row["prompt_chars"] < row["wire_chars"]
            assert row["prompt_cache"] == ts.cacheability(row["tokens"]["value"])


def test_the_meta_card_no_longer_carries_the_alias_lists(report, full_catalog):
    """1.8-14: acad/synonyms left meta["cad"], so the wire shrank by their size.

    Read off the full catalog: under the 1.8-11 search default ``list_tools``
    carries only the pins and the synthetic tools, which would leave every
    other tool's card unchecked.
    """

    async def _tools() -> list:
        from fastmcp import Client

        async with Client(server.mcp) as client:
            return list(await client.list_tools())

    tools = asyncio.run(_tools())
    names = {tool.name for tool in tools}
    # The check must cover the catalog, not the search-mode surface. (Off mode
    # lists describe_tool beside it since W3 - card-less, so it passes below.)
    assert not names & {"search_tools", "call_tool"}
    assert "entity_fillet" in names
    assert len(names) > 50
    for tool in tools:
        meta = dict(tool.meta or {})
        assert set(meta.get("cad") or {}) <= {"summary", "cost"}, tool.name


def test_the_search_mode_prefix_falls_below_the_prompt_cache_minimum(report):
    """Small enough that no model can cache it - true, and worth saying out loud."""
    variants = {row["variant"]: row for row in report["idle"]}
    assert variants["search"]["prompt_cache"]["cacheable"]["512"] is False
    assert variants["default"]["prompt_cache"]["cacheable"]["4096"] is True
    assert variants["lean"]["prompt_cache"]["cacheable"]["4096"] is True


# -- lane 2: per-discovery cost ---------------------------------------------


def test_the_stock_arm_is_configured_not_left_on_its_defaults(report):
    arms = {arm["arm"]: arm for arm in report["discovery"]["arms"]}
    assert set(arms) == {"cad_search", "stock_bm25_compact", "stock_bm25_default"}
    assert (
        arms["stock_bm25_compact"]["serializer"] == "discovery.serialize.serialize_search_results"
    )
    assert arms["stock_bm25_compact"]["strawman"] is False
    assert arms["stock_bm25_default"]["strawman"] is True


def test_the_serializer_not_the_transform_is_what_saves_the_bytes(report):
    """The honest read of the A/B: ours wins on finding tools, not on payload size."""
    summary = report["discovery"]["summary"]
    ours = summary["mean_chars"]["cad_search"]
    stock_compact = summary["mean_chars"]["stock_bm25_compact"]
    stock_default = summary["mean_chars"]["stock_bm25_default"]
    assert ours == pytest.approx(stock_compact, rel=0.25)
    assert ours < stock_default / 4


def test_a_zero_hit_answer_is_recorded_as_such_not_banked_as_a_saving(report):
    """Stock BM25 finds nothing for WBLOCK; the cheapest payload is the worst answer.

    This used to be asserted on BPOLY. v1.5.0's `boundary_trace` names BPOLY in
    its own docstring, so stock BM25 now finds it — the gap closed for that one
    command, and measuring it as still-open would have flattered the corpus.
    """
    arms = {arm["arm"]: arm for arm in report["discovery"]["arms"]}
    stock = {row["query"]: row for row in arms["stock_bm25_compact"]["queries"]}
    ours = {row["query"]: row for row in arms["cad_search"]["queries"]}
    assert stock["WBLOCK"]["hits"] == 0
    assert ours["WBLOCK"]["hits"] > 0
    summary = report["discovery"]["summary"]
    assert summary["comparable_queries"] < summary["queries"]
    assert summary["hit_rate"]["cad_search"] > summary["hit_rate"]["stock_bm25_compact"]


# -- lane 3: result-side cost -----------------------------------------------


def test_the_result_lane_measures_a_real_drawing(report):
    result = report["result"]
    assert result["entities_in_drawing"] == TEST_ENTITIES
    assert result["entity_types"], "the drawing mix is part of the measurement"


def test_the_result_lane_prices_what_the_model_reads_and_what_the_wire_carries(report):
    """FastMCP returns the same rows twice: a text block and structured content."""
    for row in report["result"]["scenarios"]:
        assert row["chars"] == row["text_chars"]
        assert row["structured_chars"] > 0
        assert row["wire_chars"] == row["text_chars"] + row["structured_chars"]


def test_the_result_payload_scales_with_the_rows_returned(report):
    scenarios = {row["limit"]: row for row in report["result"]["scenarios"]}
    for limit, row in scenarios.items():
        assert row["rows_returned"] == min(limit, TEST_ENTITIES)
    per_entity = [row["chars_per_entity"] for row in scenarios.values()]
    assert max(per_entity) < min(per_entity) * 1.15


def test_the_result_lane_names_the_cap_that_cannot_actually_be_reached(report):
    projection = report["result"]["projection"]
    assert projection["schema_max_limit"] == 1000
    assert projection["config_max_list_limit"] == config.settings.max_list_limit
    assert projection["extrapolated"] is True
    assert "schema" in projection["note"]


def test_the_result_lane_shows_where_the_bytes_go(report):
    """bounding_box and full-precision floats are measured, not assumed."""
    row = report["result"]["scenarios"][0]
    counterfactuals = row["counterfactuals"]
    assert counterfactuals["without_bounding_box"]["chars"] < row["chars"]
    assert counterfactuals["floats_rounded_6dp"]["chars"] <= row["chars"]
    for entry in counterfactuals.values():
        assert entry["counterfactual"] is True


# -- the suite is a guest on a shared server --------------------------------


def test_a_live_autocad_backend_is_refused_rather_than_measured():
    with pytest.raises(RuntimeError, match="ezdxf"):
        ts.require_ezdxf({"backend": "com"})


def test_the_suite_leaves_the_shared_server_as_it_found_it(report):
    assert config.settings.tool_profile == "full"
    # 1.8-11: the configured mode (search by default) is put back, never "off"
    mode = config.settings.discovery_mode
    assert ("search" if server._discovery_transform is not None else "off") == mode
    # Restored to whatever it was, rather than asserted absent: exporting
    # AUTOCAD_MCP_BACKEND=ezdxf is exactly what a developer on a Windows box with
    # AutoCAD open should do, and that must not fail this test.
    assert BACKEND_ENV_AFTER == BACKEND_ENV_BEFORE

    async def _names() -> set[str]:
        from fastmcp import Client

        async with Client(server.mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    names = asyncio.run(_names())
    if mode == "search":
        synthetic = {"search_tools", "call_tool", "describe_tool"}
        assert names == synthetic | set(config.settings.discovery_pinned)
    else:
        assert len(names) > 100
        assert "search_tools" not in names


# -- lane 4: task cost (1.8-14) ---------------------------------------------

BUDGET = json.loads(ts.DEFAULT_BUDGET.read_text(encoding="utf-8"))


def _scenarios(report: dict) -> dict[str, dict]:
    return {row["scenario"]: row for row in report["task"]["scenarios"]}


def test_the_task_lane_runs_every_scenario_both_ways_on_the_headless_engine(report):
    rows = _scenarios(report)
    assert set(rows) == {
        "layer_cleanup_300",
        "dimension_plate",
        "sketch_20_entities",
        "inspect_foreign",
        "history_value_lookup",
    }
    assert set(rows) == set(BUDGET["task"])
    for name, row in rows.items():
        naive = row["arms"]["naive"]
        assert naive["status"] == "measured", (name, naive["detail"])
        assert naive["verified"] is True
        assert naive["label"].startswith("naive: one tool per entity"), "the naive arm is labelled"
        assert naive["calls"] == len(naive["sizes"]) > 0
        assert naive["request_chars"] == sum(size[0] for size in naive["sizes"])
        assert naive["result_chars"] == sum(size[1] for size in naive["sizes"])


def test_a_batched_oracle_that_exists_meets_its_ceiling(report):
    """Where the verb exists the oracle runs, does the job and stays within the ceiling."""
    measured = 0
    for name, row in _scenarios(report).items():
        batched = row["arms"]["batched"]
        if batched["status"] == "pending_tool":
            continue
        measured += 1
        assert batched["status"] == "measured", (name, batched["detail"])
        assert batched["calls"] <= BUDGET["task"][name]["ceiling"], name
        assert batched["calls"] < row["arms"]["naive"]["calls"], name
    assert measured >= 2, "sketch_20_entities and inspect_foreign have their verbs today"


def test_a_missing_verb_is_pending_never_a_pass(report):
    """On the 1.8.0 tree every scenario's verb exists - 1.8-10's ``drawing_history``,
    1.8-12's ``entity_modify_where`` / ``layer_merge``, 1.8-13's ``dimension_auto``
    v2 - so no batched arm is pending and none is a pending cell; a tree without
    a verb names exactly the verbs it lacks (``tests/test_call_budget.py`` holds
    that a pending arm is never a pass)."""
    from benchmarks import call_budget as cb

    rows = _scenarios(report)
    for name, verbs in (
        ("history_value_lookup", {"drawing_history"}),
        ("layer_cleanup_300", {"entity_modify_where", "layer_merge"}),
        ("dimension_plate", {"dimension_auto_v2"}),
    ):
        batched = rows[name]["arms"]["batched"]
        assert batched["status"] != "pending_tool", name
        assert batched.get("missing_tools") in (None, []), name
        assert set(cb._missing(cb.scenario_by_id(name), {"cad_batch": None})) == verbs
    checked = ts.check_budget(report, BUDGET)
    pending = {item["cell"] for item in checked["pending"]}
    assert (
        not {
            "task.history_value_lookup",
            "task.layer_cleanup_300",
            "task.dimension_plate",
        }
        & pending
    )
    passed = {item["cell"] for item in checked["checked"]}
    assert "task.history_value_lookup" not in passed


def test_the_h4_and_h5_oracles_are_measured_within_their_ci_ceilings(report):
    """1.8-13 wrote both oracles with the 1.8-12 verbs and dimension_auto v2:
    the cleanup in one cad_batch + a critique, the plate in one dimension_auto
    + a critique - each verified, within its ceiling and gated on every PR."""
    rows = _scenarios(report)
    for name, calls in (("layer_cleanup_300", 2), ("dimension_plate", 2)):
        batched = rows[name]["arms"]["batched"]
        assert batched["status"] == "measured", batched["detail"]
        assert batched["verified"] is True
        assert batched["calls"] == calls <= BUDGET["task"][name]["ceiling"]
        assert BUDGET["task"][name]["gate"] == "ci"
        assert batched["session"]["turns"] == calls + 1


def test_the_history_oracle_finds_round_2_in_two_calls(report):
    """1.8-10: drawing_open (what a restarted server needs) + one drawing_history."""
    batched = _scenarios(report)["history_value_lookup"]["arms"]["batched"]
    assert batched["status"] == "measured", batched["detail"]
    assert batched["verified"] is True
    assert batched["by_tool"] == {"drawing_history": 1, "drawing_open": 1}
    assert batched["calls"] <= BUDGET["task"]["history_value_lookup"]["ceiling"] == 2
    assert BUDGET["task"]["history_value_lookup"]["gate"] == "ci", "the owner flipped it"


def test_every_measured_arm_is_priced_as_a_session(report):
    prefix = report["task"]["prefix"]["tokens"]
    assert prefix == (
        {row["variant"]: row for row in report["idle"]}["default"]["tokens"]["value"]
        + {row["variant"]: row for row in report["idle"]}["instructions"]["tokens"]["value"]
    )
    for row in report["task"]["scenarios"]:
        for arm in row["arms"].values():
            if arm["status"] == "pending_tool":
                continue
            session = arm["session"]
            assert session["turns"] == arm["calls"] + 1
            assert session["prefix_tokens"] == prefix
            assert session["session_input_uncached"] > session["session_input_cached"]
            assert session["session_input_uncached"] >= prefix * session["turns"]


def test_the_session_model_resends_every_earlier_span():
    """Three calls of 10, 20 and 30 tokens behind a 100-token prefix: four turns.

    Uncached: 4 x 100 + 10 x 3 + 20 x 2 + 30 x 1 = 500. Cached: the prefix
    written once and read three times (125 + 30), each span written once and
    read on its later turns (12.5 + 2, 25 + 2, 37.5).
    """
    session = ts.session_input([10, 20, 30], 100)
    assert session["turns"] == 4
    assert session["history_tokens"] == 60
    assert session["session_input_uncached"] == pytest.approx(500.0)
    assert session["session_input_cached"] == pytest.approx(155 + 14.5 + 27 + 37.5)


# -- the budget (token_budget.json, --budget) --------------------------------


def test_the_budget_file_is_the_plans_table():
    """Fixed before W2: ceilings, gates and owners, and the 1.7.0 baselines."""
    assert BUDGET["growth_limit_pct"] == 5.0
    baseline = BUDGET["baseline"]["v1.7.0_readme_wire"]
    assert (baseline["default"], baseline["search"], baseline["lean"], baseline["packs_core"]) == (
        89_226,
        503,
        25_066,
        61_422,
    )
    measured = BUDGET["baseline"]["v1.7.0_measured"]
    for name in ("default", "lean", "packs_core"):
        # measured 1.7.0 prompt value + 5 %, rounded down
        assert BUDGET["idle"][name]["ceiling"] == int(measured[name]["prompt_tokens"] * 1.05)
    assert BUDGET["idle"]["search"]["ceiling"] == 250
    assert BUDGET["idle"]["instructions"]["ceiling"] == 250
    assert BUDGET["idle"]["search_pinned"]["ceiling"] == 1_500
    assert "2,730" in BUDGET["idle"]["search_pinned"]["note"]
    for lane in ("idle", "result", "task"):
        for name, cell in BUDGET[lane].items():
            assert cell["gate"] in ("ci", "release"), name
            assert cell.get("owner"), name
    calls = {name: cell["ceiling"] for name, cell in BUDGET["task"].items()}
    assert calls["layer_cleanup_300"] == 3
    assert calls["dimension_plate"] == 2
    assert calls["sketch_20_entities"] == 1
    assert calls["history_value_lookup"] == 2


def _idle_report(rows: list[dict]) -> dict:
    return {"idle": rows, "task": None}


def _row(variant: str, prompt: int, wire: int | None = None, kind: str = "tools_list") -> dict:
    wire = prompt if wire is None else wire
    return {
        "variant": variant,
        "kind": kind,
        "basis": "text" if kind == "instructions" else "prompt_payload",
        "tokens": {"value": prompt},
        "wire": {"tokens": {"value": wire}},
    }


def test_a_ci_cell_over_its_ceiling_fails_and_a_release_cell_is_reported():
    budget = {
        "growth_limit_pct": 5.0,
        "idle": {
            "default": {"basis": ["prompt_payload"], "ceiling": 100, "gate": "ci", "owner": "x"},
            "lean": {"basis": ["prompt_payload"], "ceiling": 10, "gate": "release", "owner": "y"},
        },
    }
    report = _idle_report([_row("default", 101), _row("lean", 11)])
    checked = ts.check_budget(report, budget)
    assert not checked["ok"]
    assert [item["cell"] for item in checked["violations"]] == ["idle.default"]
    assert [item["cell"] for item in checked["over_ceiling"]] == ["idle.lean"]
    released = ts.check_budget(report, budget, release=True)
    assert {item["cell"] for item in released["violations"]} == {"idle.default", "idle.lean"}
    within = ts.check_budget(_idle_report([_row("default", 100), _row("lean", 10)]), budget)
    assert within["ok"]
    assert within["over_ceiling"] == []


def test_a_ci_cell_nothing_measured_fails():
    budget = {"idle": {"default": {"basis": ["prompt_payload"], "ceiling": 9, "gate": "ci"}}}
    checked = ts.check_budget(_idle_report([]), budget)
    assert checked["violations"][0]["reason"] == "not measured"


def test_growth_beyond_five_percent_against_the_published_report_fails():
    budget = {"growth_limit_pct": 5.0, "idle": {}}
    published = _idle_report(
        [_row("default", 1000), _row("instructions", 400, kind="instructions")]
    )
    grown = ts.check_budget(_idle_report([_row("default", 1051)]), budget, published)
    assert not grown["ok"]
    assert grown["violations"][0]["cell"] == "growth.default"
    held = ts.check_budget(_idle_report([_row("default", 1050)]), budget, published)
    assert held["ok"]
    assert held["growth"][0]["pct"] == 5.0


def test_a_published_report_from_before_the_prompt_headline_is_read_on_its_prompt_column():
    """A 1.7.0-shaped row (wire headline, nested prompt_payload) compares like with like."""
    old = {
        "variant": "default",
        "tokens": {"value": 89_000},
        "prompt_payload": {"tokens": {"value": 1000}},
    }
    checked = ts.check_budget(_idle_report([_row("default", 1040)]), {"idle": {}}, {"idle": [old]})
    assert checked["growth"][0]["published"] == 1000
    assert checked["ok"]


def test_a_docstring_inflated_by_two_thousand_tokens_fails_the_budget(report, monkeypatch):
    """The gate shown once: +2k tokens on one lean tool's description must fail it.

    In memory only - nothing is committed. +2k tokens makes lean grow more
    than 5 % on the report this module measured a moment ago, and it reaches
    the default surface just as much. The W3 Details: split (1.8.0) left the
    default surface ~6k tokens under its ci ceiling, which +2k (3 %) neither
    crosses nor grows 5 % past; so the same edit, sized past that headroom,
    must then fail ``idle.default`` itself. The inflation happens after
    registration, so it is exactly what the listing carries: the split never
    undoes it.
    """
    chars_per_token = ts.CHARS_PER_TOKEN["json_schema"].chars_per_token
    padding = " Inflated." * int(2_000 * chars_per_token / 10)
    # the module the suite measures: tests/test_http_auth.py re-imports server.py,
    # so this module's own `server` may be an older copy than sys.modules holds
    live = ts._load_server()
    tool = next(t for t in live._local_tool_components() if t.name == "entity_create_line")
    assert tool.name in live.LEAN_TOOL_NAMES
    original = tool.description or ""
    monkeypatch.setattr(tool, "description", original + padding)
    inflated = {"idle": asyncio.run(ts.idle_lane(ts.RatioTokenCounter())), "task": None}
    budget = {key: BUDGET[key] for key in ("growth_limit_pct", "idle")}
    clean = ts.check_budget(
        {"idle": report["idle"], "task": None}, budget, {"idle": report["idle"]}
    )
    checked = ts.check_budget(inflated, budget, {"idle": report["idle"]})
    assert clean["ok"], clean["violations"]
    assert not checked["ok"]
    failed = {item["cell"] for item in checked["violations"]}
    assert "growth.lean" in failed
    before = {row["variant"]: row["tokens"]["value"] for row in report["idle"]}
    after = {row["variant"]: row["tokens"]["value"] for row in inflated["idle"]}
    for variant in ("default", "lean"):
        assert after[variant] - before[variant] >= 1_900, variant  # the listing carries it
    # the default surface: an inflation past its remaining headroom fails its ci cell
    headroom = BUDGET["idle"]["default"]["ceiling"] - before["default"]
    assert headroom > 0, "the default surface stands under its ci ceiling"
    past = " Inflated." * int((headroom + 200) * chars_per_token / 10)
    monkeypatch.setattr(tool, "description", original + past)
    overgrown = {"idle": asyncio.run(ts.idle_lane(ts.RatioTokenCounter())), "task": None}
    checked = ts.check_budget(overgrown, budget, {"idle": report["idle"]})
    assert not checked["ok"]
    assert "idle.default" in {item["cell"] for item in checked["violations"]}


def test_ci_runs_both_budgets_on_every_pull_request():
    """T3 (-1 'a failed report drops the gate'): the budgets are a PR gate, not a report."""
    import yaml

    workflow = yaml.safe_load(
        (ts.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    triggers = workflow.get("on") or workflow.get(True)
    assert "pull_request" in triggers
    runs = "\n".join(step.get("run", "") for step in workflow["jobs"]["token-budget"]["steps"])
    assert "benchmarks.token_suite" in runs
    assert "--budget benchmarks/token_budget.json" in " ".join(runs.split())
    assert "benchmarks.call_budget" in runs


def test_the_cli_refuses_a_budget_in_another_unit(capsys):
    assert ts.main(["--tokenizer", "anthropic", "--budget", str(ts.DEFAULT_BUDGET)]) == 2
    assert "ratio" in capsys.readouterr().err


# -- the human summary ------------------------------------------------------


def test_the_summary_states_the_method_next_to_the_numbers(report):
    text = ts.summarize(report)
    assert "characters" in text
    assert "estimate" in text
    assert "cached" in text
    assert text.isascii(), "the console this runs on is cp1254"
