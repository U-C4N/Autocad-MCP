"""1.8-14: per-connection session metrics behind ``system_status(metrics=True)``.

The rival exposes ``get_metrics`` (calls, time, payload size). Ours is no new
tool: ``AuditMiddleware`` counts every call of a connection - calls, calls per
tool, the bytes of text it returned, errors by kind, p50/p95 latency - and
``system_status`` reports them only when asked, so the default status payload
(and the idle catalog) pay nothing for it. The live engine's ``_run`` breakdown
under the same flag is tested on a fake seat in tests/test_session_metrics_com.py.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

import server
from backends import session_metrics as sm


def _text_bytes(result) -> int:
    return sum(
        len(block.text.encode("utf-8"))
        for block in (result.content or [])
        if getattr(block, "text", None) is not None
    )


@pytest.fixture
def ezdxf_env(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")


@pytest.mark.asyncio
async def test_five_calls_are_five_calls_and_their_text_is_counted(ezdxf_env):
    async with Client(server.mcp) as client:
        results = [
            await client.call_tool("drawing_new", {}),
            await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 10, "y2": 0}),
            await client.call_tool("entity_create_circle", {"cx": 5, "cy": 5, "radius": 2}),
            await client.call_tool("entity_list", {"limit": 10}),
            await client.call_tool("drawing_info", {}),
        ]
        status = await client.call_tool("system_status", {"metrics": True})
    metrics = status.structured_content["metrics"]["session"]
    assert metrics["calls"] == 5
    assert metrics["result_bytes"] == sum(_text_bytes(r) for r in results)
    assert metrics["result_bytes"] > 0
    assert metrics["by_tool"] == {
        "drawing_info": 1,
        "drawing_new": 1,
        "entity_create_circle": 1,
        "entity_create_line": 1,
        "entity_list": 1,
    }
    assert metrics["errors_by_kind"] == {}
    assert 0.0 <= metrics["p50_ms"] <= metrics["p95_ms"]
    assert metrics["window"] == 5


@pytest.mark.asyncio
async def test_errors_are_counted_by_their_envelope_kind(ezdxf_env):
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        failed = await client.call_tool("entity_get", {"handle": "FFFFF"}, raise_on_error=False)
        assert failed.is_error
        kind = failed.structured_content["kind"]
        status = await client.call_tool("system_status", {"metrics": True})
    metrics = status.structured_content["metrics"]["session"]
    assert metrics["calls"] == 2
    assert metrics["errors_by_kind"] == {kind: 1}


@pytest.mark.asyncio
async def test_a_call_through_the_search_proxy_counts_once_under_the_tool_it_ran(ezdxf_env):
    server._apply_discovery_mode("search")
    try:
        async with Client(server.mcp) as client:
            await client.call_tool("call_tool", {"name": "drawing_new", "arguments": {}})
            status = await client.call_tool(
                "call_tool", {"name": "system_status", "arguments": {"metrics": True}}
            )
    finally:
        server._apply_discovery_mode()
    metrics = status.structured_content["metrics"]["session"]
    assert metrics["calls"] == 1
    assert metrics["by_tool"] == {"drawing_new": 1}


@pytest.mark.asyncio
async def test_a_second_connection_starts_from_zero(ezdxf_env):
    async with Client(server.mcp) as first:
        await first.call_tool("drawing_new", {})
        await first.call_tool("drawing_info", {})
    async with Client(server.mcp) as second:
        status = await second.call_tool("system_status", {"metrics": True})
    assert status.structured_content["metrics"]["session"]["calls"] == 0


@pytest.mark.asyncio
async def test_with_the_flag_off_the_status_payload_is_unchanged(ezdxf_env):
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        plain = (await client.call_tool("system_status", {})).structured_content
        off = (await client.call_tool("system_status", {"metrics": False})).structured_content
    assert "metrics" not in plain
    assert set(plain) == set(off)
    # the volatile parts aside (the journal grew by one call), the same document
    for volatile in ("operations", "state_store"):
        plain.pop(volatile, None)
        off.pop(volatile, None)
    assert plain == off


@pytest.mark.asyncio
async def test_the_headless_engine_says_it_has_no_com_breakdown(ezdxf_env):
    async with Client(server.mcp) as client:
        status = await client.call_tool("system_status", {"metrics": True})
    metrics = status.structured_content["metrics"]
    assert metrics["com_run"] is None
    assert "headless" in metrics["com_run_reason"]


# ── the pure aggregates ─────────────────────────────────────────────────────


def test_percentiles_are_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert sm.percentile(values, 50) == 50.0
    assert sm.percentile(values, 95) == 95.0
    assert sm.percentile([7.0], 95) == 7.0
    assert sm.percentile([], 50) is None


def test_the_registry_forgets_the_oldest_connection_first():
    registry = sm.SessionMetricsRegistry(max_sessions=2)
    registry.record("a", "drawing_info", 1.0, 10)
    registry.record("b", "drawing_info", 1.0, 10)
    registry.record("c", "drawing_info", 1.0, 10)
    assert registry.snapshot("a")["calls"] == 0
    assert registry.snapshot("c")["calls"] == 1


def test_an_unknown_session_reads_as_zero_never_raises():
    snapshot = sm.SessionMetricsRegistry().snapshot("never-seen")
    assert snapshot["calls"] == 0
    assert snapshot["p50_ms"] is None


def test_the_run_breakdown_aggregate_reports_every_bucket():
    breakdown = sm.RunBreakdown()
    assert breakdown.snapshot() == {"jobs": 0, "ms": {}}
    breakdown.record(queue=1.0, guard=2.0, work=3.0, refresh=4.0, counter=5.0)
    breakdown.record(queue=3.0, guard=2.0, work=1.0, refresh=0.0, counter=1.0)
    snapshot = breakdown.snapshot()
    assert snapshot["jobs"] == 2
    assert list(snapshot["ms"]) == list(sm.RUN_BUCKETS)
    assert snapshot["ms"]["queue"] == {"p50": 1.0, "p95": 3.0, "total": 4.0}
