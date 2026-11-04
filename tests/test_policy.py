"""1.8-17 - the server's tool policy: READ_ONLY, allow/deny lists, MAX_TOOL_COST.

The policy is one rule (``policy.refusal``) checked at four points, and every
test below goes through one of them the way a client would:

1. the profile visibility transform - ``list_tools`` shrinks;
2. ``AuditMiddleware``, before an operation record is opened, on the tool a
   call really runs (the search proxy's ``call_tool`` target included);
3. ``cad_batch`` at plan time, for every step (steps skip the middleware);
4. ``security.validate_path(allow_write=True)`` - a file write is ``mutate``
   whatever its tool is labelled, so a ``read`` writer is still refused.

The defaults stay permissive: with no setting, every tool runs (1.7).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
import pytest_asyncio
from fastmcp import Client

import config
import policy
import server
from policy import PolicyRefusedError

REPO = Path(__file__).resolve().parents[1]

LINE_ARGS = {"x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0}


@pytest_asyncio.fixture(autouse=True)
async def _restore(monkeypatch):
    """Put the permissive defaults and the full catalog back after each test."""
    yield
    monkeypatch.undo()
    server._apply_discovery_mode("off")
    await server._apply_tool_profile("full")


def _set(monkeypatch, **values):
    for name, value in values.items():
        monkeypatch.setattr(config.settings, name, value)


async def _open(monkeypatch, **values):
    """A client on a fresh headless drawing, then the policy ``values`` applied.

    The drawing is created first (``drawing_new`` is a mutating tool, refused
    under most of the policies below); the policy then takes effect for the
    calls that follow, and the catalog is re-applied with it, as a restart
    with these settings would.
    """
    client = Client(server.mcp)
    await client.__aenter__()
    await client.call_tool("drawing_new", {})
    await client.call_tool("entity_create_line", LINE_ARGS)
    _set(monkeypatch, **values)
    await server._apply_tool_profile("full")
    return client


async def _status(client) -> dict:
    # detail="operations": the record list (1.8-10 made a summary the default).
    return (await client.call_tool("system_status", {"detail": "operations"})).structured_content


def _cost(name: str) -> str | None:
    card = server._tool_card(name)
    return None if card is None else card.get("cost")


# â”€â”€ the defaults â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_the_defaults_are_permissive():
    assert config.settings.read_only is False
    assert config.settings.max_tool_cost == "escape"
    assert config.settings.tool_allowlist == ()
    assert config.settings.tool_denylist == ()
    assert not policy.restrictive()
    assert policy.describe() == {
        "read_only": False,
        "max_tool_cost": "escape",
        "ceiling": "escape",
        "allowlist": [],
        "denylist": [],
    }
    for name in ("entity_create_line", "entity_delete", "drawing_purge", "system_run_command"):
        assert policy.allowed(name, _cost(name)), name


def test_settings_parse_the_environment(monkeypatch):
    monkeypatch.setenv("READ_ONLY", "true")
    monkeypatch.setenv("TOOL_ALLOWLIST", " entity_*, Drawing_Info ,entity_*")
    monkeypatch.setenv("TOOL_DENYLIST", "entity_delete*")
    monkeypatch.setenv("MAX_TOOL_COST", "Mutate")
    parsed = config.Settings()
    assert parsed.read_only is True
    assert parsed.tool_allowlist == ("entity_*", "drawing_info")
    assert parsed.tool_denylist == ("entity_delete*",)
    assert parsed.max_tool_cost == "mutate"


def test_an_unknown_max_tool_cost_falls_back_to_escape_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("MAX_TOOL_COST", "harmless")
    with caplog.at_level(logging.WARNING, logger="autocad_mcp.policy"):
        parsed = config.Settings()
    assert parsed.max_tool_cost == "escape"
    assert "harmless" in caplog.text


# â”€â”€ READ_ONLY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_read_only_refuses_entity_create_line_before_anything_runs(monkeypatch):
    client = await _open(monkeypatch, read_only=True)
    try:
        before = await _status(client)
        result = await client.call_tool("entity_create_line", LINE_ARGS, raise_on_error=False)
        after = await _status(client)
    finally:
        await client.__aexit__(None, None, None)
    payload = result.structured_content
    assert result.is_error
    assert payload["kind"] == "policy_refused" and payload["category"] == "policy"
    assert payload["policy"] == "read_only"
    assert payload["tool"] == "entity_create_line"
    assert payload["allowed_cost"] == "read" and payload["cost"] == "mutate"
    assert payload["retryable"] is False
    assert after["revision"] == before["revision"]
    assert after["policy"]["read_only"] is True and after["policy"]["ceiling"] == "read"


@pytest.mark.asyncio
async def test_read_only_refuses_a_one_create_step_batch_before_step_one(monkeypatch):
    client = await _open(monkeypatch, read_only=True)
    try:
        before = await _status(client)
        info_before = (await client.call_tool("drawing_info", {})).structured_content
        result = await client.call_tool(
            "cad_batch",
            {"steps": [{"tool": "entity_create_line", "args": LINE_ARGS}]},
            raise_on_error=False,
        )
        after = await _status(client)
        info_after = (await client.call_tool("drawing_info", {})).structured_content
        dry = await client.call_tool(
            "cad_batch",
            {"steps": [{"tool": "entity_create_line", "args": LINE_ARGS}], "dry_run": True},
        )
    finally:
        await client.__aexit__(None, None, None)
    payload = result.structured_content
    assert result.is_error
    assert payload["kind"] == "policy_refused" and payload["policy"] == "read_only"
    assert payload["tool"] == "entity_create_line"
    assert "nothing was executed" in payload["message"] and "step 0" in payload["message"]
    assert after["revision"] == before["revision"]
    assert info_after["entity_count"] == info_before["entity_count"]
    # No operation record of a step: the batch was refused at plan time.
    assert not [op for op in after["operations"] if op.get("tool") == "entity_create_line"][1:]
    row = dry.structured_content["results"][0]
    assert row["status"] == "invalid" and row["error"]["kind"] == "policy_refused"


@pytest.mark.asyncio
async def test_read_only_still_runs_a_batch_of_reads(monkeypatch):
    client = await _open(monkeypatch, read_only=True)
    try:
        result = await client.call_tool(
            "cad_batch", {"steps": [{"tool": "drawing_info", "args": {}}]}
        )
    finally:
        await client.__aexit__(None, None, None)
    assert result.structured_content["ok"] is True


@pytest.mark.asyncio
async def test_in_search_mode_call_tool_entity_delete_is_refused(monkeypatch):
    server._apply_discovery_mode("search")
    client = await _open(monkeypatch, read_only=True)
    try:
        result = await client.call_tool(
            "call_tool",
            {"name": "entity_delete", "arguments": {"handle": "1F"}},
            raise_on_error=False,
        )
        listed = {tool.name for tool in await client.list_tools()}
    finally:
        await client.__aexit__(None, None, None)
    payload = result.structured_content
    assert result.is_error
    assert payload["kind"] == "policy_refused"
    assert payload["tool"] == "entity_delete" and payload["policy"] == "read_only"
    assert {"search_tools", "call_tool"} <= listed, "the proxies themselves are never refused"


@pytest.mark.asyncio
async def test_drawing_export_pdf_is_refused_by_the_path_guard(monkeypatch, tmp_path):
    """Called around the middleware (a script, an internal caller), the write
    itself is refused: the path guard does not trust the caller's label."""
    _set(monkeypatch, read_only=True)
    target = tmp_path / "sheet.pdf"

    class _Ctx:
        lifespan_context = {"backend": object()}

        async def info(self, *_a, **_k):
            return None

        async def report_progress(self, *_a, **_k):
            return None

    with pytest.raises(PolicyRefusedError) as excinfo:
        await server.drawing_export_pdf(str(target), ctx=_Ctx())
    assert excinfo.value.policy == "read_only" and excinfo.value.cost == "mutate"
    assert not target.exists()


@pytest.mark.asyncio
async def test_a_writer_labelled_below_mutate_is_refused_by_the_path_guard(monkeypatch, tmp_path):
    """drawing_verify_requirements is `safe` and passes a `safe` ceiling; its
    report file is a write - `mutate` - refused by validate_path."""
    client = await _open(monkeypatch, max_tool_cost="safe")
    report = tmp_path / "report.json"
    try:
        result = await client.call_tool(
            "drawing_verify_requirements",
            {"requirements": {"units": "mm", "items": []}, "report_path": str(report)},
            raise_on_error=False,
        )
    finally:
        await client.__aexit__(None, None, None)
    payload = result.structured_content
    assert result.is_error and payload["kind"] == "policy_refused"
    assert payload["cost"] == "mutate" and payload["allowed_cost"] == "safe"
    assert payload["policy"] == "max_tool_cost"
    assert payload["tool"] == "drawing_verify_requirements"
    assert not report.exists()


@pytest.mark.asyncio
async def test_read_only_lists_only_read_tools(monkeypatch):
    client = await _open(monkeypatch, read_only=True)
    try:
        listed = {tool.name for tool in await client.list_tools()}
    finally:
        await client.__aexit__(None, None, None)
    assert "drawing_info" in listed and "entity_list" in listed
    assert "entity_create_line" not in listed and "entity_delete" not in listed
    for name in listed:
        assert _cost(name) == "read" or name in policy.PER_STEP_TOOLS, name


# â”€â”€ allow / deny lists and the cost ceiling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_allowlist_entity_glob_and_drawing_info(monkeypatch):
    client = await _open(monkeypatch, tool_allowlist=("entity_*", "drawing_info"))
    try:
        listed = {tool.name for tool in await client.list_tools()}
        created = await client.call_tool("entity_create_line", LINE_ARGS)
        info = await client.call_tool("drawing_info", {})
        refused = await client.call_tool("drawing_new", {}, raise_on_error=False)
        about = (
            await client.call_tool("system_about", {}, raise_on_error=False)
        ).structured_content
    finally:
        await client.__aexit__(None, None, None)
    assert listed and all(n.startswith("entity_") or n == "drawing_info" for n in listed)
    assert "entity_create_line" in listed and "cad_batch" not in listed
    assert not created.is_error and not info.is_error
    assert refused.is_error and refused.structured_content["policy"] == "tool_allowlist"
    assert about["kind"] == "policy_refused", "system_about is outside the allowlist"


@pytest.mark.asyncio
async def test_denylist_refuses_directly_and_inside_a_batch(monkeypatch):
    client = await _open(monkeypatch, tool_denylist=("entity_delete*",))
    try:
        direct = await client.call_tool("entity_delete", {"handle": "1F"}, raise_on_error=False)
        batch = await client.call_tool(
            "cad_batch",
            {"steps": [{"tool": "entity_delete_many", "args": {"handles": ["1F"]}}]},
            raise_on_error=False,
        )
        moved = await client.call_tool(
            "cad_batch",
            {"steps": [{"tool": "entity_create_line", "args": LINE_ARGS}]},
        )
    finally:
        await client.__aexit__(None, None, None)
    assert direct.structured_content["policy"] == "tool_denylist"
    assert batch.structured_content["kind"] == "policy_refused"
    assert batch.structured_content["tool"] == "entity_delete_many"
    assert moved.structured_content["ok"] is True


@pytest.mark.asyncio
async def test_max_tool_cost_safe_refuses_mutate_and_file_writes(monkeypatch, tmp_path):
    client = await _open(monkeypatch, max_tool_cost="safe")
    target = tmp_path / "copy.dwg"
    try:
        created = await client.call_tool("entity_create_line", LINE_ARGS, raise_on_error=False)
        current = await client.call_tool("layer_set_current", {"name": "0"})
        dwg = await client.call_tool(
            "drawing_export_dwg", {"path": str(target)}, raise_on_error=False
        )
    finally:
        await client.__aexit__(None, None, None)
    assert created.structured_content["policy"] == "max_tool_cost"
    assert not current.is_error
    # drawing_export_dwg is labelled `safe`; its write is still `mutate`.
    assert dwg.is_error and dwg.structured_content["kind"] == "policy_refused"
    assert dwg.structured_content["cost"] == "mutate"
    assert not target.exists()


@pytest.mark.asyncio
async def test_unknown_list_entries_warn_at_startup_and_are_reported(monkeypatch, caplog):
    _set(monkeypatch, tool_allowlist=("entity_*", "no_such_tool_xyz"))
    with caplog.at_level(logging.WARNING, logger="autocad_mcp.policy"):
        await server._apply_tool_profile("full")
    assert "no_such_tool_xyz" in caplog.text
    unmatched = policy.describe([t.name for t in server._local_tool_components()])["unmatched"]
    assert unmatched == {"allowlist": ["no_such_tool_xyz"]}


@pytest.mark.asyncio
async def test_system_about_and_status_report_the_policy(monkeypatch):
    client = await _open(monkeypatch, max_tool_cost="mutate", tool_denylist=("drawing_purge",))
    try:
        about = (await client.call_tool("system_about", {})).structured_content
        status = await _status(client)
    finally:
        await client.__aexit__(None, None, None)
    assert about["policy"]["max_tool_cost"] == "mutate"
    assert about["policy"]["denylist"] == ["drawing_purge"]
    assert about["policy"]["http"]["max_body_bytes"] == config.settings.max_http_body_bytes
    assert status["policy"]["ceiling"] == "mutate"


# â”€â”€ the audit: every writer is covered â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _writer_tools() -> dict[str, str]:
    """Tool functions in server.py that write a file through validate_path(allow_write=True),
    directly or through the engineering helpers that do, mapped to their call sites."""
    helpers = {"_batch_plot", "batch_plot", "write_csv", "_csv_target", "deliverable"}
    tree = ast.parse((REPO / "server.py").read_text(encoding="utf-8"))
    tools = {t.name for t in server._local_tool_components()}
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or node.name not in tools:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            writes = name == "validate_path" and any(
                kw.arg == "allow_write"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in call.keywords
            )
            if writes or name in helpers:
                found[node.name] = f"server.py:{call.lineno}"
    return found


@pytest.mark.asyncio
async def test_every_writer_costs_mutate_or_is_covered_by_the_path_guard(monkeypatch, tmp_path):
    writers = _writer_tools()
    # The pid list tools write through deliverables.write_csv; data_extract,
    # the takeoffs and verify_requirements directly.
    assert {
        "drawing_save_as",
        "data_extract",
        "drawing_verify_requirements",
        "pid_instrument_index",
        "batch_plot",
    } <= set(writers)
    below_mutate = {}
    for name in writers:
        cost = _cost(name)
        assert cost in policy.COSTS, f"{name} has no cost card"
        if policy.COSTS.index(cost) < policy.COSTS.index("mutate"):
            below_mutate[name] = cost
    # Each of these runs under its own cost as the ceiling - and its write is
    # refused there by the path guard, so no ceiling below mutate writes a file.
    for name, cost in below_mutate.items():
        monkeypatch.setattr(config.settings, "max_tool_cost", cost)
        assert policy.allowed(name, cost), name
        with pytest.raises(PolicyRefusedError):
            server.validate_path(str(tmp_path / "x.csv"), allow_write=True, kinds=(".csv",))
    monkeypatch.setattr(config.settings, "max_tool_cost", "mutate")
    assert server.validate_path(str(tmp_path / "x.csv"), allow_write=True, kinds=(".csv",))
    assert below_mutate, "the audit must have something below mutate to cover"


# ── the v9 matrix task ──────────────────────────────────────────────────────


def test_v9_carries_policy_read_only_and_is_the_default():
    from benchmarks import run_competitors
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v8 import TASKS_V8
    from benchmarks.tasks_v9 import DEFAULT_MATRIX, MATRICES, TASKS_V9, task_by_id

    assert TASKS_V9[: len(TASKS_V8)] == TASKS_V8
    assert task_by_id("policy_read_only", "v9").category == "security"
    assert MATRICES["v9"] == TASKS_V9 and run_competitors.MATRICES is MATRICES
    assert DEFAULT_MATRIX == "v9" and run_competitors.DEFAULT_MATRIX == "v9"  # 1.8-25
    assert hasattr(AutoCADMCPProAdapter, "_task_policy_read_only")


@pytest.mark.asyncio
async def test_the_v9_task_passes_headlessly(tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import task_by_id

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        result = await adapter.run_task(task_by_id("policy_read_only", "v9"))
    finally:
        await adapter.cleanup()
    assert result.status == "pass", result.metrics
    assert config.settings.read_only is False, "the task restores the setting"


# ── the two A/B correctness checks ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["policy_read_only_batch", "write_path_autoload_refused"])
async def test_the_correctness_checks_pass_here_and_are_appended(name):
    from benchmarks.correctness_suite import CHECKS

    function, category = CHECKS[name]
    assert category == "Security"
    assert await function() is True
    assert config.settings.read_only is False
