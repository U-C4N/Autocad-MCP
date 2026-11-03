"""1.8-02: a failed finalize / deliver returns what its gate computed.

``drawing_finalize`` used to compute the validator, the critique and the score,
then raise a one-line ``ToolError`` and throw the rest away, so the caller had
to read the whole drawing again to learn which handles blocked it. Now the gate
raises ``GateFailure`` (still a ``ToolError`` with the same sentence) carrying
``{kind, score, blocking: [{code, severity, handles[:5], message}],
blocking_count, qa_coverage}``; the refusal middleware puts it in the error
envelope, ``cad_batch`` in the step row, and ``detail="full"`` keeps every row,
every handle and the whole report. ``drawing_deliver`` keeps returning a status
and adds the same payload as ``gate_failure``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from engineering import errors


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def _construction_lines(client, count: int) -> list[str]:
    handles = []
    for i in range(count):
        line = await client.call_tool(
            "entity_create_line",
            {"x1": 0, "y1": i, "x2": 10, "y2": i, "layer": "CONSTRUCTION"},
        )
        handles.append(line.structured_content["handle"])
    return handles


@pytest.mark.asyncio
async def test_a_failed_critique_gate_returns_its_blocking_handles(client, tmp_path):
    handles = await _construction_lines(client, 7)
    result = await client.call_tool(
        "drawing_finalize", {"save_path": str(tmp_path / "part.dxf")}, raise_on_error=False
    )
    assert result.is_error is True
    assert result.content[0].text.startswith(
        "Internal error: drawing_finalize: critique gate failed with 1 blocking issue(s). "
        "First: construction_left:"
    ), "the prose is the 1.7 sentence"
    payload = result.structured_content
    assert payload["kind"] == "critique_gate_failed" and payload["category"] == "validation"
    assert payload["retryable"] is False
    assert payload["score"]["score"] < 100
    assert payload["blocking_count"] == 1
    (row,) = payload["blocking"]
    assert row["code"] == "construction_left" and row["severity"] == "error"
    assert row["handles"] == handles[:5] and row["handle_count"] == 7
    assert payload["qa_coverage"]["complete"] is True
    assert "report" not in payload


@pytest.mark.asyncio
async def test_detail_full_keeps_every_handle_and_the_report(client, tmp_path):
    handles = await _construction_lines(client, 7)
    result = await client.call_tool(
        "drawing_finalize",
        {"save_path": str(tmp_path / "part.dxf"), "detail": "full"},
        raise_on_error=False,
    )
    payload = result.structured_content
    (row,) = payload["blocking"]
    assert row["handles"] == handles and "handle_count" not in row
    assert payload["report"]["critique"], "the whole computed report travels with detail=full"
    assert payload["report"]["score"] == payload["score"]


@pytest.mark.asyncio
async def test_a_failed_validator_is_validation_failed_direct_and_in_a_batch(client):
    await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
    result = await client.call_tool("drawing_finalize", {}, raise_on_error=False)
    payload = result.structured_content
    assert payload["kind"] == "validation_failed"
    assert payload["blocking"][0]["code"] == "not_saved"
    assert payload["blocking_count"] >= 1

    batch = (
        await client.call_tool("cad_batch", {"steps": [{"tool": "drawing_finalize", "args": {}}]})
    ).structured_content
    step = batch["results"][0]["error"]
    assert step["kind"] == "validation_failed", "both routes give one kind"
    assert step["blocking"][0]["code"] == "not_saved" and step["category"] == "validation"


@pytest.mark.parametrize(
    "tool, args",
    [
        ("drawing_finalize", {"detail": "everything"}),
        ("drawing_deliver", {"output_dir": "OUT", "detail": "everything"}),
    ],
)
@pytest.mark.asyncio
async def test_detail_is_validated_and_typed_invalid_args(client, tmp_path, tool, args):
    """A bad ``detail`` is the caller's argument (invalid_args), never the tool
    declining (refused); the sentence on the wire is the one it was."""
    args = {k: str(tmp_path / v) if k == "output_dir" else v for k, v in args.items()}
    result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.is_error is True
    payload = result.structured_content
    assert "detail must be one of" in payload["message"]
    assert payload["kind"] == "invalid_args" and payload["category"] == "input"
    assert payload["retryable"] is False
    assert payload["hint"] == errors.describe("invalid_args").hint
    assert result.content[0].text == (
        "Internal error: invalid_args: detail must be one of ('summary', 'full'), got 'everything'"
    )
    status = (await client.call_tool("system_status", {})).structured_content
    (record,) = [r for r in status["operations"] if r["operation_id"] == payload["operation_id"]]
    assert (record["state"], record["error_kind"]) == ("refused", "invalid_args")

    batch = (
        await client.call_tool("cad_batch", {"steps": [{"tool": tool, "args": args}]})
    ).structured_content
    step = batch["results"][0]["error"]
    assert step["kind"] == "invalid_args", "both routes give one kind"
    assert step["category"] == "input" and step["retryable"] is False


@pytest.mark.asyncio
async def test_a_failed_delivery_carries_the_gate_payload(client, tmp_path):
    handles = await _construction_lines(client, 2)
    result = (
        await client.call_tool("drawing_deliver", {"output_dir": str(tmp_path / "out")})
    ).structured_content
    assert result["status"] == "failed_validation"
    gate = result["gate_failure"]
    assert gate["kind"] == "validation_failed"
    codes = [row["code"] for row in gate["blocking"]]
    assert "construction_left" in codes and "score" in codes
    (construction,) = [row for row in gate["blocking"] if row["code"] == "construction_left"]
    assert construction["handles"] == handles
    assert gate["blocking_count"] == len(gate["blocking"])
    assert gate["qa_coverage"]["complete"] is True


@pytest.mark.asyncio
async def test_a_clean_delivery_has_no_gate_payload(client, tmp_path):
    await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
    result = (
        await client.call_tool(
            "drawing_deliver",
            {"output_dir": str(tmp_path / "out"), "min_score": 0, "strict_critique": False},
        )
    ).structured_content
    assert result["status"] == "success"
    assert "gate_failure" not in result


def test_the_summary_caps_rows_and_handles():
    class _Issue:
        def __init__(self, i):
            self.severity, self.focus, self.message = "error", f"focus_{i}", f"issue {i}"
            self.handles = [f"{i}{j}" for j in range(9)]
            self.detail = {}

    issues = [_Issue(i) for i in range(14)]
    summary = errors.gate_failure_payload(
        "critique_gate_failed", score={"score": 10}, issues=issues
    )
    assert summary["blocking_count"] == 14
    assert len(summary["blocking"]) == errors.GATE_BLOCKING_ROWS
    assert all(len(row["handles"]) == 5 and row["handle_count"] == 9 for row in summary["blocking"])
    full = errors.gate_failure_payload(
        "critique_gate_failed", score={"score": 10}, issues=issues, detail="full"
    )
    assert len(full["blocking"]) == 14 and len(full["blocking"][0]["handles"]) == 9
    with pytest.raises(ValueError):
        errors.gate_failure_payload("failed", score={})
