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


def test_the_idle_lane_covers_the_three_advertised_surfaces(report):
    variants = {row["variant"]: row for row in report["idle"]}
    assert set(variants) == {"default", "lean", "search"}
    assert variants["default"]["advertised_tools"] > 100
    assert variants["lean"]["advertised_tools"] == len(server.LEAN_TOOL_NAMES)
    assert variants["search"]["advertised_tools"] == 2
    assert variants["default"]["chars"] > variants["lean"]["chars"] > variants["search"]["chars"]


def test_the_idle_lane_separates_the_wire_payload_from_what_the_model_pays(report):
    """tools/list carries annotations, titles and _meta a client need not forward."""
    for row in report["idle"]:
        assert row["chars"] == row["wire_chars"]
        assert 0 < row["prompt_chars"] < row["wire_chars"]


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
    assert config.settings.discovery_mode == "off"
    # Restored to whatever it was, rather than asserted absent: exporting
    # AUTOCAD_MCP_BACKEND=ezdxf is exactly what a developer on a Windows box with
    # AutoCAD open should do, and that must not fail this test.
    assert BACKEND_ENV_AFTER == BACKEND_ENV_BEFORE

    async def _names() -> set[str]:
        from fastmcp import Client

        async with Client(server.mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    names = asyncio.run(_names())
    assert len(names) > 100
    assert "search_tools" not in names


# -- the human summary ------------------------------------------------------


def test_the_summary_states_the_method_next_to_the_numbers(report):
    text = ts.summarize(report)
    assert "characters" in text
    assert "estimate" in text
    assert "cached" in text
    assert text.isascii(), "the console this runs on is cp1254"
