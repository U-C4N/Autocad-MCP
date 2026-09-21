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


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    """Poll an async predicate; a rollback that never lands fails here, not by hanging."""
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout:.1f}s")
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("how", ["scope", "native"])
async def test_a_cancellation_mid_run_rolls_the_sheet_back(backend, monkeypatch, how):
    """A *real* cancellation, not a ``CancelledError`` raised from a mock.

    The MCP SDK cancels a request through an anyio cancel scope, which is
    level-triggered: ``task.cancel()`` is re-issued on every loop iteration
    while the task is inside the cancelled scope, so an unshielded rollback
    was cancelled at its first ``await`` before its body ran (five symbols
    left in an open transaction). A native ``Task.cancel()`` is delivered once
    and clears — raising from the mock only ever exercised that easy case.
    """
    import asyncio

    import anyio

    from engineering.pid import spec as spec_module

    before = await backend.entity_count()
    in_flight = asyncio.Event()

    async def slow_draw_line(*args, **kwargs):
        in_flight.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(spec_module, "draw_line", slow_draw_line)

    if how == "scope":
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_spec, backend, EXAMPLE_SPEC)
            await in_flight.wait()
            assert await backend.entity_count() > before, "symbols placed before the cancel"
            tg.cancel_scope.cancel()
    else:
        task = asyncio.ensure_future(run_spec(backend, EXAMPLE_SPEC))
        await in_flight.wait()
        assert await backend.entity_count() > before, "symbols placed before the cancel"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await backend.entity_count() == before, "the placed symbols were rolled back"
    assert (await backend.system_status())["transaction_depth"] == 0, "no open transaction"


async def test_a_client_cancellation_through_the_transport_rolls_the_sheet_back(
    client, monkeypatch
):
    """The docstring's promise, measured end to end: ``notifications/cancelled``
    from a real ``fastmcp.Client`` mid-run leaves no half-drawn sheet and no
    open transaction."""
    import asyncio

    from fastmcp.exceptions import McpError

    from engineering.pid import spec as spec_module

    async def slow_draw_line(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(spec_module, "draw_line", slow_draw_line)

    async def depth() -> int:
        return (await client.call_tool("system_status", {})).structured_content["transaction_depth"]

    async def entities() -> int:
        return (await client.call_tool("analysis_entity_stats", {})).structured_content[
            "total_entities"
        ]

    before = await entities()
    # The session hands out ids sequentially, so the next one is the tool
    # call's — provided nothing else is sent before the call goes out. Yield
    # (no requests) until the counter has moved, then start polling.
    request_id = client.session._request_id
    call = asyncio.ensure_future(client.call_tool("pid_from_spec", {"spec": EXAMPLE_SPEC}))

    async def call_sent() -> bool:
        return client.session._request_id > request_id

    await _wait_until(call_sent)
    await _wait_until(lambda: _is_mid_run(depth, entities, before))
    await client.cancel(request_id)
    with pytest.raises(McpError, match="cancelled"):
        await call
    # The SDK answers the cancel before the handler's rollback has landed.
    await _wait_until(lambda: _is_clean(depth, entities, before))


async def _is_mid_run(depth, entities, before) -> bool:
    return await depth() == 1 and await entities() > before


async def _is_clean(depth, entities, before) -> bool:
    return await depth() == 0 and await entities() == before


async def test_a_cancellation_with_a_com_call_in_flight_still_closes_the_undo_mark(
    monkeypatch,
):
    """COM shape: real ``ComBackend._run``, a real single-thread executor and the
    real ``transaction_rollback``; only the ActiveX document is a recorder.

    With a COM call blocking the worker when the scope is cancelled, the
    unshielded rollback's ``_run`` was cancelled while queued behind it — so
    ``EndUndoMark`` / ``_UNDO B`` never reached AutoCAD — yet its ``finally``
    cleared ``_transaction_active``: ``system_status`` said "no transaction"
    while the undo mark was open, and the next ``transaction_begin`` nested a
    second ``StartUndoMark``.
    """
    import asyncio
    import time
    import types
    from concurrent.futures import ThreadPoolExecutor

    import anyio

    from backends import com_backend as module
    from engineering.pid import spec as spec_module

    sent: list[str] = []
    doc = types.SimpleNamespace(
        Name="Drawing1.dwg",
        FullName="",
        StartUndoMark=lambda: sent.append("StartUndoMark"),
        EndUndoMark=lambda: sent.append("EndUndoMark"),
    )
    app = types.SimpleNamespace(Documents=types.SimpleNamespace(Count=1), ActiveDocument=doc)
    monkeypatch.setattr(module, "_acad_app", lambda: app)  # _ensure_document_state
    monkeypatch.setattr(module, "_acad_doc", lambda: doc)  # begin / rollback
    monkeypatch.setattr(
        module.ComBackend,
        "_safe_send_command",
        staticmethod(lambda doc, cmd, deadline_s=8.0: sent.append(cmd) or []),
    )
    backend = module.ComBackend()
    backend._executor = ThreadPoolExecutor(max_workers=1)  # no CoInitialize: nothing is COM here
    try:
        placed = 0

        async def fake_place(backend, section, item):
            nonlocal placed
            placed += 1
            return {"handle": f"H{placed}"}

        in_flight = asyncio.Event()

        async def slow_draw_line(backend, *args, **kwargs):
            in_flight.set()
            await backend._run(time.sleep, 0.5)  # the worker is inside a COM call

        monkeypatch.setattr(spec_module, "_place", fake_place)
        monkeypatch.setattr(spec_module, "draw_line", slow_draw_line)

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_spec, backend, EXAMPLE_SPEC)
            await in_flight.wait()
            tg.cancel_scope.cancel()

        assert sent == ["StartUndoMark", "EndUndoMark", "_UNDO B"], sent
        assert backend._transaction_active is False
        # The mark really is closed, so the next transaction is a fresh one.
        assert (await backend.transaction_begin())["ok"] is True
        assert sent[-1] == "StartUndoMark"
    finally:
        backend._executor.shutdown(wait=True)
