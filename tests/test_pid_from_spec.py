"""pid_from_spec: one call, one transaction, graph + critique back."""

from __future__ import annotations

import copy

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from engineering.pid.spec import EXAMPLE_SPEC, run_spec, validate_spec

pytestmark = pytest.mark.asyncio


async def test_example_draws_a_connected_pid(backend):
    result = await run_spec(backend, EXAMPLE_SPEC)
    assert set(result["handles"]) == {"P-101", "V-201", "FCV-101", "FIC-101", "OP-1"}
    graph = result["graph"]
    assert graph["stats"]["nodes_by_kind"] == {
        "connector": 1,
        "equipment": 2,
        "instrument": 1,
        "valve": 1,
    }
    assert len(graph["edges"]) == 4 and graph["stats"]["dangling"] == 0
    assert result["critique"] == [] and result["crossings_total"] == 0
    assert backend.get_plan_spec()["layer_set_id"] == "pid"


async def test_bad_reference_rolls_back(backend):
    before = await backend.entity_count()
    bad = copy.deepcopy(EXAMPLE_SPEC)
    bad["lines"].append({"from": "P-101.discharge", "to": "V-201.NOPE", "class": "process_major"})
    with pytest.raises(ValueError, match=r"lines\[3\]"):
        await run_spec(backend, bad)
    assert await backend.entity_count() == before


async def test_dry_run_routes_in_memory_and_writes_nothing(backend):
    before = await backend.entity_count()
    result = await run_spec(backend, EXAMPLE_SPEC, dry_run=True)
    assert result["dry_run"] is True and len(result["lines"]) == 4
    assert all(len(line["vertices"]) >= 2 for line in result["lines"])
    assert await backend.entity_count() == before


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (
            lambda s: s["equipment"].append({"id": "P-101", "symbol": "fan", "x": 0, "y": 0}),
            "equipment[2].id",
        ),
        (lambda s: s["lines"][0].update({"class": "steam"}), "lines[0].class"),
        (lambda s: s["instruments"][0].pop("x"), "instruments[0].x"),
        (lambda s: s.update({"bogus": []}), "bogus"),
        (lambda s: s["connectors"][0].update({"to": "V-201.N1"}), "connectors[0]"),
    ],
)
async def test_validation_names_the_path(mutate, fragment):
    spec = copy.deepcopy(EXAMPLE_SPEC)
    mutate(spec)
    with pytest.raises(ValueError) as exc:
        validate_spec(spec)
    assert fragment in str(exc.value)


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


async def test_finalize_scores_the_example_at_ninety_or_better(client, tmp_path):
    result = (await client.call_tool("pid_from_spec", {"spec": EXAMPLE_SPEC})).structured_content
    assert result["graph"]["stats"]["dangling"] == 0
    # The validator's first step refuses an unsaved drawing (`not_saved`), so
    # finalize saves first — the same shape `tests/test_pid_critique.py` uses.
    final = (
        await client.call_tool("drawing_finalize", {"save_path": str(tmp_path / "pid.dxf")})
    ).structured_content
    assert final["score"]["score"] >= 90, final["score"]
    assert not [i for i in final.get("critique", []) if str(i.get("focus", "")).startswith("pid_")]


# ── Track A hardening (track E wave 0, Task 5) ───────────────────────────────


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda s: s["equipment"][0].update({"x": float("nan")}), "equipment[0].x"),
        (lambda s: s["valves"][0].update({"y": float("inf")}), "valves[0].y"),
        (lambda s: s["instruments"][0].update({"rotation": 10**400}), "instruments[0].rotation"),
        (lambda s: s["lines"][0].update({"stub": float("-inf")}), "lines[0].stub"),
    ],
)
async def test_non_finite_numbers_are_refused_by_path_before_any_write(backend, mutate, fragment):
    before = await backend.entity_count()
    spec = copy.deepcopy(EXAMPLE_SPEC)
    mutate(spec)
    with pytest.raises(ValueError, match="finite") as exc:
        await run_spec(backend, spec)
    assert fragment in str(exc.value)
    assert await backend.entity_count() == before
    assert (await backend.system_status())["transaction_depth"] == 0


async def test_a_cancellation_mid_run_rolls_the_sheet_back(backend, monkeypatch):
    """``CancelledError`` is a ``BaseException``; ``except Exception`` let the
    placed symbols stay inside an open transaction."""
    import asyncio

    from engineering.pid import spec as spec_module

    before = await backend.entity_count()

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(spec_module, "draw_line", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await run_spec(backend, EXAMPLE_SPEC)
    assert await backend.entity_count() == before, "the placed symbols were rolled back"
    assert (await backend.system_status())["transaction_depth"] == 0, "no open transaction"
