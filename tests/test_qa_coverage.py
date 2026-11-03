"""1.8-16: the QA coverage ledger and the compact QA summaries.

A silently skipped lane plus "0 issues" read as a clean drawing: the live
engine's dimension-legibility lane returned ``[]``, a paper layout nothing
could judge said nothing, and a P&ID focus on a mechanical sheet was as quiet
as one on a clean P&ID. Every check now writes ``checked`` or ``not_checked``
rows; ``drawing_critique(report=True)`` returns them, finalize / deliver carry
the not-checked ones as ``qa_coverage``, and ``strict_critique=True`` fails on
every not-checked row but ``domain_absent``. ``summary=True`` / ``limit``
return ``{total, by_focus, by_severity, issues[:limit]}`` with the full total.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

import server
from backends.ezdxf_backend import EzdxfBackend
from engineering.critique import run_critique
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.qa_coverage import (
    DOCUMENT,
    NOT_CHECKED_REASONS,
    CoverageLedger,
    blocking_rows,
    ledger_of,
    qa_coverage_of,
    summarize,
)


def _rows(shared, *, focus=None, layout=None, status=None):
    return [
        r
        for r in ledger_of(shared).rows
        if (focus is None or r.focus == focus)
        and (layout is None or r.layout == layout)
        and (status is None or r.status == status)
    ]


# -- the ledger itself ------------------------------------------------------------------


def test_a_reason_nobody_declared_is_refused():
    ledger = CoverageLedger()
    with pytest.raises(ValueError, match="not declared"):
        ledger.not_checked("iso128", "Model", "felt_like_it")
    ledger.not_checked("iso128", "Model", "no_viewport_scale")
    assert ledger.rows[0].reason == "no_viewport_scale"


def test_domain_absent_is_the_only_reason_strict_lets_through():
    ledger = CoverageLedger()
    ledger.not_checked("pid_duplicate_tag", DOCUMENT, "domain_absent")
    assert blocking_rows(ledger) == []
    ledger.not_checked("iso128", "Layout1", "no_viewport_scale")
    assert [r.reason for r in blocking_rows(ledger)] == ["no_viewport_scale"]
    report = ledger.report(["Model", "Layout1"])
    assert report["complete"] is False
    by_layout = {e["layout"]: e for e in report["layouts"]}
    assert by_layout["Layout1"]["status"] == "not_checked"
    assert by_layout[DOCUMENT]["domain_absent"] == ["pid_duplicate_tag"]


# -- every focus writes at least one row per run -------------------------------------------


@pytest.mark.asyncio
async def test_every_focus_writes_a_row_on_an_empty_drawing(backend):
    shared: dict = {}
    issues = await run_critique(backend, None, shared=shared)
    written = ledger_of(shared).focuses()
    assert set(ALL_CRITIQUE_FOCUSES) <= written, set(ALL_CRITIQUE_FOCUSES) - written
    reasons = {r.reason for r in ledger_of(shared).rows if r.status == "not_checked"}
    assert "check_unreported" not in reasons and "check_failed" not in reasons
    # Nothing to check is not a gap: an empty headless drawing is complete.
    assert blocking_rows(ledger_of(shared)) == [], [
        r.to_dict() for r in blocking_rows(ledger_of(shared))
    ]
    assert not [i for i in issues if i.severity == "error"]


@pytest.mark.asyncio
async def test_every_focus_writes_a_row_on_a_drawing_with_content(backend):
    await backend.entity_create_line(0, 0, 100, 0, layer="GEOMETRY")
    await backend.entity_create_line(100, 0, 100, 50, layer="GEOMETRY")
    await backend.entity_create_circle(50, 25, 10, layer="GEOMETRY")
    shared: dict = {}
    await run_critique(backend, None, shared=shared)
    for focus in ALL_CRITIQUE_FOCUSES:
        assert _rows(shared, focus=focus), f"{focus} wrote no ledger row"
    # The entity checks report every layout they read, with its entity count.
    (model,) = _rows(shared, focus="construction_left", layout="Model")
    assert model.status == "checked" and model.n == 3
    # A domain focus on a drawing without its domain says so.
    (pid,) = _rows(shared, focus="pid_duplicate_tag")
    assert pid.status == "not_checked" and pid.reason == "domain_absent"


@pytest.mark.asyncio
async def test_a_check_that_writes_nothing_is_reported(backend, monkeypatch):
    from engineering import critique

    async def silent(_backend):
        return []

    monkeypatch.setitem(critique._FOCUS_DISPATCH, "layer_color", silent)
    shared: dict = {}
    await run_critique(backend, ["layer_color"], shared=shared)
    (row,) = _rows(shared, focus="layer_color")
    assert row.status == "not_checked" and row.reason == "check_unreported"


@pytest.mark.asyncio
async def test_a_check_that_raises_is_not_checked(backend, monkeypatch):
    from engineering import critique

    async def boom(_backend):
        raise RuntimeError("kaput")

    monkeypatch.setitem(critique._FOCUS_DISPATCH, "layer_color", boom)
    shared: dict = {}
    issues = await run_critique(backend, ["layer_color"], shared=shared)
    assert issues[0].detail["code"] == "check_failed"
    (row,) = _rows(shared, focus="layer_color")
    assert row.reason == "check_failed"


@pytest.mark.asyncio
async def test_a_partial_read_is_scan_incomplete_in_the_ledger(backend, monkeypatch):
    import config

    await backend.entity_create_line(0, 0, 10, 0)
    await backend.entity_create_line(0, 5, 10, 5)
    monkeypatch.setattr(config.settings, "max_qa_entities", 1)
    shared: dict = {}
    await run_critique(backend, ["construction_left", "iso128"], shared=shared)
    reasons = {(r.focus, r.reason) for r in _rows(shared, status="not_checked")}
    assert ("construction_left", "scan_incomplete") in reasons
    assert ("iso128", "scan_incomplete") in reasons
    assert qa_coverage_of(shared)["complete"] is False


# -- legibility: the live lane and the paper layouts ---------------------------------------


@pytest.mark.asyncio
async def test_the_live_legibility_lane_is_not_checked_on_a_com_mock(monkeypatch):
    pytest.importorskip("win32com.client")
    from backends import com_backend as cb

    live = cb.ComBackend()

    async def layer_list():
        return []

    monkeypatch.setattr(live, "layer_list", layer_list)
    shared: dict = {}
    issues = await run_critique(live, ["iso128"], shared=shared)
    assert issues == []  # it was [] in 1.7 too - and read as "checked, clean"
    rows = _rows(shared, focus="iso128")
    lanes = {(r.lane, r.status, r.reason) for r in rows}
    assert ("dimension_legibility", "not_checked", "engine_live_document_template") in lanes
    assert ("layer_lineweight", "checked", None) in lanes
    coverage = qa_coverage_of(shared)
    assert coverage["complete"] is False
    assert coverage["not_checked"] == [
        {
            "focus": "iso128",
            "layout": DOCUMENT,
            "reason": "engine_live_document_template",
            "lane": "dimension_legibility",
        }
    ]


@pytest.mark.asyncio
async def test_a_paper_layout_without_a_viewport_is_no_viewport_scale(backend):
    await backend.entity_create_line(0, 0, 100, 0, layer="GEOMETRY")
    sheet = backend._doc.layouts.get("Layout1")
    sheet.add_line((0, 0), (420, 0))  # a frame line, and no window onto the model
    shared: dict = {}
    await run_critique(backend, ["iso128"], shared=shared)
    (row,) = _rows(shared, focus="iso128", layout="Layout1")
    assert row.status == "not_checked" and row.reason == "no_viewport_scale"
    report = ledger_of(shared).report(["Model", "Layout1"])
    by_layout = {e["layout"]: e for e in report["layouts"]}
    assert by_layout["Layout1"]["not_checked"] == [
        {"focus": "iso128", "reason": "no_viewport_scale", "lane": "dimension_legibility"}
    ]
    assert report["complete"] is False


@pytest.mark.asyncio
async def test_an_empty_paper_layout_is_checked_with_nothing_on_it(backend):
    shared: dict = {}
    await run_critique(backend, ["iso128"], shared=shared)
    (row,) = _rows(shared, focus="iso128", layout="Layout1")
    assert row.status == "checked" and row.n == 0


@pytest.mark.asyncio
async def test_a_viewport_onto_lettered_model_space_is_deferred_not_passed(backend):
    await backend.entity_create_line(0, 0, 100, 0, layer="GEOMETRY")
    await backend.entity_create_text("NOTE", 0, 10, height=2.5)
    sheet = backend._doc.layouts.get("Layout1")
    sheet.add_viewport(
        center=(150, 100), size=(200, 150), view_center_point=(50, 0), view_height=150
    )
    shared: dict = {}
    await run_critique(backend, ["iso128"], shared=shared)
    (row,) = _rows(shared, focus="iso128", layout="Layout1")
    assert row.status == "not_checked" and row.reason == "viewport_scale_deferred"


def test_every_reason_is_described():
    assert all(text for text in NOT_CHECKED_REASONS.values())
    assert {"engine_live_document_template", "no_viewport_scale", "domain_absent"} <= set(
        NOT_CHECKED_REASONS
    )


# -- the tool surface ------------------------------------------------------------------------


async def _served(monkeypatch, prepare=None):
    async def make():
        backend = EzdxfBackend()
        await backend.connect()
        await backend.drawing_new()
        if prepare is not None:
            prepare(backend)
        return backend

    monkeypatch.setattr(server, "_make_backend", make)
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    return Client(server.mcp)


def _data(result):
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    return content


def _paper_line(backend):
    backend._doc.layouts.get("Layout1").add_line((0, 0), (420, 0))


async def _sheet_without_viewport(client, layout="Layout1"):
    """A bootstrapped drawing, a part line in model space, and a frame line on
    ``layout`` drawn through the tools - no viewport onto the model."""
    await client.call_tool("drawing_new", {})
    await client.call_tool(
        "entity_create_line", {"x1": 0, "y1": 0, "x2": 50, "y2": 0, "layer": "GEOMETRY"}
    )
    await _paper_frame(client, layout)


async def _paper_frame(client, layout):
    await client.call_tool("layout_set_current", {"name": layout})
    await client.call_tool(
        "entity_create_line", {"x1": 0, "y1": 0, "x2": 420, "y2": 0, "layer": "TITLEBLOCK"}
    )
    await client.call_tool("layout_set_current", {"name": "Model"})


@pytest.mark.asyncio
async def test_drawing_critique_stays_a_bare_list_by_default(monkeypatch):
    async with await _served(monkeypatch, _paper_line) as client:
        result = await client.call_tool("drawing_critique", {})
        assert isinstance(_data(result), list)
        reported = _data(await client.call_tool("drawing_critique", {"report": True}))
    assert set(reported) == {"issues", "coverage"}
    assert set(reported["coverage"]) == {"layouts", "complete"}
    assert reported["coverage"]["complete"] is False
    layouts = {e["layout"]: e for e in reported["coverage"]["layouts"]}
    assert {"focus": "iso128", "reason": "no_viewport_scale", "lane": "dimension_legibility"} in (
        layouts["Layout1"]["not_checked"]
    )


def _duplicates(backend):
    msp = backend._doc.modelspace()
    for _ in range(501):  # 500 duplicate_entities issues
        msp.add_line((0, 0), (10, 0))


@pytest.mark.asyncio
async def test_a_500_issue_summary_fits_2500_characters_with_the_full_total(monkeypatch):
    async with await _served(monkeypatch, _duplicates) as client:
        full = _data(await client.call_tool("drawing_critique", {}))
        result = await client.call_tool("drawing_critique", {"summary": True})
        limited = _data(await client.call_tool("drawing_critique", {"limit": 2}))
    summary = _data(result)
    assert len(full) >= 500
    assert set(summary) == {"total", "by_focus", "by_severity", "issues"}
    assert summary["total"] == len(full)
    assert summary["by_focus"]["duplicate_entities"] == 500
    assert sum(summary["by_severity"].values()) == len(full)
    size = len(json.dumps(summary))
    assert size <= 2500, size
    assert limited["total"] == len(full) and len(limited["issues"]) == 2
    assert limited["issues"][0] == full[0], "without summary the rows are whole"


def test_summarize_never_cuts_the_counts():
    rows = [{"focus": "a", "severity": "warning", "message": "x" * 400}] * 7
    out = summarize(rows, summary=True, limit=None, key="focus")
    assert out["total"] == 7 and len(out["issues"]) == 5
    assert len(out["issues"][0]["message"]) == 160
    zero = summarize(rows, summary=False, limit=0)
    assert zero["total"] == 7 and zero["issues"] == []
    with pytest.raises(ValueError):
        summarize(rows, summary=False, limit=-1)


@pytest.mark.asyncio
async def test_validation_check_and_topology_check_summarize(monkeypatch):
    async with await _served(monkeypatch) as client:
        await client.call_tool("layer_create", {"name": "EMPTY-1"})
        await client.call_tool("layer_create", {"name": "EMPTY-2"})
        await client.call_tool("layer_create", {"name": "WATER-PIPE"})
        for x in (0, 30, 60):
            await client.call_tool(
                "entity_create_line",
                {"x1": x, "y1": 0, "x2": x + 20, "y2": 0, "layer": "WATER-PIPE"},
            )
        legacy = _data(await client.call_tool("validation_check", {}))
        compact = _data(await client.call_tool("validation_check", {"summary": True}))
        topo = _data(
            await client.call_tool(
                "drawing_topology_check", {"layers": ["WATER-PIPE"], "summary": True, "limit": 1}
            )
        )
        topo_legacy = _data(
            await client.call_tool("drawing_topology_check", {"layers": ["WATER-PIPE"]})
        )
    assert "total_issues" in legacy and "total" not in legacy
    assert compact["total"] == legacy["total_issues"]
    assert compact["by_focus"].get("empty_layers", 0) >= 2
    assert topo["total"] == sum(topo_legacy["counts"].values()) > 1
    assert len(topo["issues"]) == 1
    assert set(topo["by_focus"]) <= {"dangling", "near_miss", "crossing"}


# -- finalize / deliver ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_carries_qa_coverage_and_strict_names_the_not_checked_rows(
    monkeypatch, tmp_path
):
    async with await _served(monkeypatch) as client:
        await _sheet_without_viewport(client)
        passed = await client.call_tool(
            "drawing_finalize", {"save_path": str(tmp_path / "a.dxf")}, raise_on_error=False
        )
        strict = await client.call_tool(
            "drawing_finalize",
            {"save_path": str(tmp_path / "b.dxf"), "strict_critique": True},
            raise_on_error=False,
        )
    assert passed.is_error is False, passed.content[0].text
    coverage = passed.structured_content["qa_coverage"]
    assert coverage["complete"] is False
    assert {
        "focus": "iso128",
        "layout": "Layout1",
        "reason": "no_viewport_scale",
        "lane": "dimension_legibility",
    } in coverage["not_checked"]
    assert "pid_dangling_line" in coverage["domain_absent"]

    assert strict.is_error is True
    gate = strict.structured_content
    assert gate["kind"] == "critique_gate_failed"
    rows = [row for row in gate["blocking"] if row["code"] == "not_checked"]
    assert rows and rows[0]["focus"] == "iso128" and rows[0]["layout"] == "Layout1"
    assert rows[0]["reason"] == "no_viewport_scale"
    assert "no_viewport_scale" in rows[0]["message"]
    assert gate["qa_coverage"]["not_checked"] == coverage["not_checked"]


@pytest.mark.asyncio
async def test_strict_finalize_passes_when_only_domains_are_absent(monkeypatch, tmp_path):
    async with await _served(monkeypatch) as client:
        await client.call_tool("drawing_new", {})
        await client.call_tool(
            "entity_create_line", {"x1": 0, "y1": 0, "x2": 50, "y2": 0, "layer": "GEOMETRY"}
        )
        result = await client.call_tool(
            "drawing_finalize",
            {"save_path": str(tmp_path / "c.dxf"), "strict_critique": True},
            raise_on_error=False,
        )
    assert result.is_error is False, result.content[0].text
    coverage = result.structured_content["qa_coverage"]
    assert coverage["complete"] is True and coverage["not_checked"] == []


@pytest.mark.asyncio
async def test_the_finalize_qa_coverage_costs_at_most_300_tokens(monkeypatch, tmp_path):
    """Measured with the token suite's own estimator (chars / 4.19 for JSON)."""
    from benchmarks.token_suite import FORMAT_JSON_DATA, RatioTokenCounter

    async with await _served(monkeypatch) as client:
        await _sheet_without_viewport(client)
        for name in ("Layout2", "Layout3"):
            await client.call_tool("layout_create", {"name": name})
            await _paper_frame(client, name)
        result = await client.call_tool(
            "drawing_finalize", {"save_path": str(tmp_path / "d.dxf")}, raise_on_error=False
        )
    coverage = result.structured_content["qa_coverage"]
    assert len(coverage["not_checked"]) == 3
    tokens = RatioTokenCounter().count(json.dumps(coverage), FORMAT_JSON_DATA)
    assert tokens <= 300, tokens


@pytest.mark.asyncio
async def test_deliver_carries_qa_coverage_in_result_and_manifest(backend, tmp_path):
    from engineering.delivery import deliver_drawing
    from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes

    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    await backend.entity_create_line(0, 0, 50, 0, layer="GEOMETRY")
    _paper_line(backend)
    lenient = await deliver_drawing(
        backend, tmp_path / "a", formats=["dxf"], min_score=0, strict_critique=False
    )
    assert lenient.status == "success"
    data = lenient.to_dict()
    assert data["qa_coverage"]["not_checked"][0]["reason"] == "no_viewport_scale"
    manifest = json.loads((tmp_path / "a" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["qa_coverage"] == data["qa_coverage"]

    strict = await deliver_drawing(backend, tmp_path / "b", formats=["dxf"], min_score=0)
    assert strict.status == "failed_validation"
    assert strict.gate_failure["kind"] == "critique_gate_failed"
    assert any(row["code"] == "not_checked" for row in strict.gate_failure["blocking"])
    assert any(
        "not checked" in reason
        for reason in json.loads((tmp_path / "b" / "manifest.json").read_text(encoding="utf-8"))[
            "blocking"
        ]
    )


# -- the focus list is a resource now ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_focus_resource_lists_the_closed_enum(monkeypatch):
    from engineering.critique_catalog import FOCUS_CATALOG

    assert set(FOCUS_CATALOG) == set(ALL_CRITIQUE_FOCUSES)
    async with await _served(monkeypatch) as client:
        contents = await client.read_resource("autocad://critique/focuses")
        tools = {tool.name: tool for tool in await client.list_tools()}
    served = json.loads(contents[0].text)
    assert [row["focus"] for row in served["focuses"]] == list(ALL_CRITIQUE_FOCUSES)
    assert set(served["not_checked_reasons"]) == set(NOT_CHECKED_REASONS)
    description = tools["drawing_critique"].inputSchema["properties"]["focus"]["description"]
    assert "autocad://critique/focuses" in description
    assert len(description) < 300, len(description)
    assert "pid_unconnected_equipment" not in description


@pytest.mark.asyncio
async def test_the_correctness_check_passes_and_is_appended(monkeypatch):
    from pathlib import Path

    from benchmarks.correctness_suite import CHECKS

    assert list(CHECKS)[-1] == "qa_coverage_not_checked"
    check, category = CHECKS["qa_coverage_not_checked"]
    assert category == "Quality gate"
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    factory = server._make_backend
    assert await check() is True
    assert server._make_backend is factory


@pytest.mark.asyncio
async def test_an_unknown_focus_is_still_reported_not_run(backend):
    issues = await run_critique(backend, ["not_a_focus"], shared={})
    assert issues[0].message.startswith("Unknown critique focus")
