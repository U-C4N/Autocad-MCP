"""``benchmarks/call_budget.py`` - the least-call oracles and their fixtures (1.8-14).

The fixtures are what the plan names: the messy-layers drawing (six misnamed
layers, colour overrides, 300 entities at lineweight -3) and the plate (a closed
LWPOLYLINE, six circles, two arcs). The ceilings are ``token_budget.json``'s; a
``ci`` cell with no oracle, an arm that does not do its job, or an oracle over
its ceiling fails - a pending oracle is reported, never passed.
"""

from __future__ import annotations

import ezdxf
import pytest

from benchmarks import call_budget as cb


def test_the_messy_layers_fixture_is_the_plans(tmp_path):
    doc = ezdxf.readfile(cb.build_messy_layers(tmp_path / "messy.dxf"))
    msp = list(doc.modelspace())
    layers = {layer.dxf.name for layer in doc.layers}
    assert len(cb.MESSY_LAYERS) == 6
    assert set(cb.MESSY_LAYERS) <= layers
    assert set(cb.MESSY_LAYERS.values()) <= layers
    assert len(msp) == 300
    assert all(entity.dxf.lineweight == -3 for entity in msp)
    assert all(entity.dxf.color not in (0, 256) for entity in msp), "colour overrides"
    assert {entity.dxf.layer for entity in msp} == set(cb.MESSY_LAYERS)


def test_the_plate_fixture_is_the_plans(tmp_path):
    doc = ezdxf.readfile(cb.build_plate(tmp_path / "plate.dxf"))
    kinds: dict[str, int] = {}
    for entity in doc.modelspace():
        kinds[entity.dxftype()] = kinds.get(entity.dxftype(), 0) + 1
    assert kinds == {"LWPOLYLINE": 1, "CIRCLE": 6, "ARC": 2}
    outline = next(iter(doc.modelspace().query("LWPOLYLINE")))
    assert outline.closed


def _row(name: str, naive: str = "measured", batched: str = "measured", calls: int = 1):
    arm = {"arm": "naive", "status": naive, "detail": "x", "calls": 9}
    oracle = {"arm": "batched", "status": batched, "detail": "y", "calls": calls}
    if batched == "pending_tool":
        oracle["missing_tools"] = ["some_verb"]
    return {"scenario": name, "arms": {"naive": arm, "batched": oracle}}


BUDGET = {
    "task": {
        "a": {"ceiling": 1, "gate": "ci", "owner": "1.8-14"},
        "b": {"ceiling": 3, "gate": "release", "owner": "1.8-12"},
    }
}


def test_within_the_ceilings_and_pending_on_a_release_cell_passes():
    checked = cb.check_calls([_row("a"), _row("b", batched="pending_tool")], BUDGET)
    assert checked["violations"] == []
    assert [item["cell"] for item in checked["pending"]] == ["task.b"]


@pytest.mark.parametrize(
    ("rows", "cell"),
    [
        ([_row("a", calls=2), _row("b")], "task.a"),  # over a ci ceiling
        ([_row("a", batched="pending_tool"), _row("b")], "task.a"),  # a ci cell with no oracle
        ([_row("a", naive="failed"), _row("b")], "task.a.naive"),  # an arm that did not do it
        ([_row("a")], "task.b"),  # a cell nothing measured
    ],
)
def test_what_fails_the_call_budget(rows, cell):
    checked = cb.check_calls(rows, BUDGET)
    assert cell in {item["cell"] for item in checked["violations"]}


def test_release_turns_a_pending_cell_into_a_failure():
    checked = cb.check_calls([_row("a"), _row("b", batched="pending_tool")], BUDGET, release=True)
    assert [item["cell"] for item in checked["violations"]] == ["task.b"]


def test_the_harness_passes_its_own_budget_on_this_tree(monkeypatch, capsys):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    assert cb.main([]) == 0
    out = capsys.readouterr().out
    assert "sketch_20_entities" in out
    assert "PENDING task.history_value_lookup" in out
    assert "PENDING task.layer_cleanup_300" not in out
    assert "PENDING task.dimension_plate" not in out


# -- the 1.8-13 oracles can fail ------------------------------------------------


async def _served(func):
    """Run ``func(client, work)`` against the real in-memory server, headless."""
    import tempfile
    from pathlib import Path

    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        status = (await client.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        with tempfile.TemporaryDirectory(prefix="acadmcp_cb_test_") as folder:
            return await func(client, Path(folder))


class _Lying(cb.Recorder):
    """A recorder that writes one edge dimension's text as a number the edge is not."""

    async def call(self, tool, arguments):
        if tool == "dimension_linear" and not getattr(self, "_lied", False):
            self._lied = True
            arguments = {**arguments, "text_override": "130"}
        return await super().call(tool, arguments)


@pytest.mark.asyncio
async def test_the_plate_oracle_fails_a_dimension_that_lies(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")

    async def run(client, work):
        ctx = await cb._setup_plate(client, work)
        await cb._plate_naive(_Lying(client), ctx)
        return await cb._plate_verify(client, ctx, None)

    ok, detail = await _served(run)
    assert ok is False
    assert "dimension_mismatch" in detail


@pytest.mark.asyncio
async def test_the_plate_oracle_holds_the_batched_arm_to_a_second_run(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")

    async def run(client, work):
        ctx = await cb._setup_plate(client, work)
        rec = cb.Recorder(client)
        answer = await cb._plate_batched(rec, ctx)
        verdict = await cb._plate_verify(client, ctx, answer)
        return [call.tool for call in rec.calls], verdict

    calls, (ok, detail) = await _served(run)
    assert calls == ["dimension_auto", "drawing_critique"]
    assert ok is True, detail
    assert "12 features" not in detail and "9 features bound" in detail


@pytest.mark.asyncio
async def test_the_cleanup_oracle_fails_a_half_done_job(monkeypatch):
    """Merging the layers without clearing the overrides is not the job."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")

    async def run(client, work):
        ctx = await cb._setup_messy(client, work)
        steps = [
            {"tool": "layer_merge", "args": {"sources": [old], "target": new}}
            for old, new in cb.MESSY_LAYERS.items()
        ]
        await client.call_tool("cad_batch", {"steps": steps})
        return await cb._cleanup_verify(client, ctx, None)

    ok, detail = await _served(run)
    assert ok is False
    assert "300 with an override" in detail


@pytest.mark.asyncio
async def test_the_cleanup_oracle_refuses_a_stale_count_with_nothing_changed(monkeypatch):
    """The broken-input twin: an ``expect_count`` the drawing does not hold is
    refused ``stale_revision`` before anything is written."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")

    async def run(client, work):
        await cb._setup_messy(client, work)
        before = (await client.call_tool("drawing_info", {})).structured_content["revision"]
        result = await client.call_tool(
            "entity_modify_where",
            {
                "where": {"type": "*"},
                "set": {"color": 256},
                "space": "all",
                "expect_count": cb.MESSY_ENTITIES + 1,
            },
            raise_on_error=False,
        )
        after = (await client.call_tool("drawing_info", {})).structured_content["revision"]
        return result, before, after

    result, before, after = await _served(run)
    assert result.is_error and result.structured_content["kind"] == "stale_revision"
    assert before == after
