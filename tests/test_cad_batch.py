"""``cad_batch`` — the ordered multi-tool executor.

One round trip instead of N, with a later step able to reference an earlier
one's handle so the model never has to echo it back.

Where the win actually comes from, measured on a 17-step mounting-plate task
(ISO layers, 4 outline lines, 2 fillets, 2 snapped midpoints, 5 circles, 2
dimensions) against the real in-memory server:

  * **Payload alone: 1.28x** — and *0.74x* with ``verbose=True``. The batched
    request is the LARGER of the two (1,579 vs 1,357 chars: every step carries a
    ``{tool, args, bind}`` wrapper the sequential arm gets free from the protocol
    envelope). Batching does not win by making payloads smaller.
  * **Agent loop: 9.07x** — 340,447 -> 37,548 input tokens. This is the real
    mechanism, and it is *turn elimination*: 18 billed turns become 2, and each
    turn re-sends the whole conversation plus the 18,306-token tool prefix.

So the honest framing is that batching multiplies with the discovery lane
rather than beating it — search alone is 18.29x on the same task, and the two
together are 106.99x. Nothing here should claim batching is the biggest lever.

What these tests actually guard is the *dangerous* half of the idea, because
the happy path is the easy half:

  * **Privilege.** A single ``cad_batch`` grant must not reach the raw
    command/LISP escape hatches. The deny set is derived from the live
    ``@cad_tool(cost="escape")`` cards rather than hand-listed, so a future
    escape hatch is denied the day it is registered, and a denied step refuses
    the *whole* batch before anything executes — otherwise ``on_error="continue"``
    would turn the denylist into a no-op.
  * **Error typing.** The re-entrant call path reformats every non-FastMCP
    exception into ``ToolError(f"Error calling tool {name!r}: {e}")``. The
    classifier must recover the real cause from the ``__cause__`` chain and
    never from the message text.
  * **Atomicity honesty.** ezdxf rolls back by restoring a full DXF snapshot;
    COM ends an undo mark and fires ``_UNDO B`` at AutoCAD without ever
    confirming it landed. The payload must say which one it got, and must not
    claim a rollback it did not verify.
  * **dry_run.** Validates against each tool's real JSON Schema without
    executing a single step.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    """In-memory client on a fresh headless document.

    The env var is what ``server._make_backend`` reads; setting
    ``config.settings.backend`` would leave a Windows box with AutoCAD open
    talking to the operator's live drawing.
    """
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def _entity_count(client) -> int:
    info = (await client.call_tool("drawing_info", {})).structured_content or {}
    return int(info["entity_count"])


async def _batch(client, steps, **kwargs):
    payload = {"steps": steps, **kwargs}
    return (await client.call_tool("cad_batch", payload)).structured_content


def dumps(obj) -> str:
    """Compact JSON — how the payload is actually charged for."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


LINE = "entity_create_line"


def _line(x1=0.0, y1=0.0, x2=10.0, y2=0.0, **extra):
    return {"tool": LINE, "args": {"x1": x1, "y1": y1, "x2": x2, "y2": y2}, **extra}


# ---------------------------------------------------------------------------
# Registration and metadata
# ---------------------------------------------------------------------------


async def test_cad_batch_is_registered_with_a_discovery_card():
    tools = {t.name: t for t in await server._registered_tools()}
    assert "cad_batch" in tools, "cad_batch must be a registered tool"
    card = (tools["cad_batch"].meta or {})["cad"]
    assert card["cost"] == "destructive", "it can run any mutating tool, so it costs the worst case"
    assert card["summary"] and "\n" not in card["summary"]
    assert card["synonyms"], "the alias corpus must cover it"


async def test_cad_batch_is_filed_under_the_batch_group():
    groups = await server._tool_groups()
    assert "cad_batch" in groups["batch"]
    assert {"entity_batch_create", "entity_batch_modify"} <= set(groups["batch"]), (
        "the pre-existing batch tools stay: cad_batch supersedes them in scope, not in the registry"
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_three_step_batch_runs_in_one_round_trip(client):
    result = await _batch(
        client,
        [
            _line(),
            _line(y1=10.0, y2=10.0),
            {"tool": "entity_create_circle", "args": {"cx": 5, "cy": 5, "radius": 2}},
        ],
    )
    assert result["ok"] is True
    assert (result["steps"], result["executed"], result["succeeded"], result["failed"]) == (
        3,
        3,
        3,
        0,
    )
    assert [row["status"] for row in result["results"]] == ["ok", "ok", "ok"]
    assert await _entity_count(client) == 3


async def test_a_successful_step_reports_only_its_handle_by_default(client):
    result = await _batch(client, [_line()])
    payload = result["results"][0]["result"]
    assert set(payload) == {"handle"}, (
        "an EntityInfo is fully recoverable via entity_get(handle); re-emitting it "
        "in every batch row is exactly the cost cad_batch exists to remove"
    )
    assert payload["handle"]


async def test_verbose_restores_the_full_step_result(client):
    result = await _batch(client, [_line()], verbose=True)
    payload = result["results"][0]["result"]
    assert payload["handle"] and payload["type"] == "LINE"
    assert "properties" in payload


async def test_a_result_without_a_handle_is_never_trimmed(client):
    """Compaction only drops what ``entity_get(handle)`` can give back."""
    result = await _batch(client, [{"tool": "drawing_info", "args": {}}])
    payload = result["results"][0]["result"]
    assert payload["entity_count"] == 0
    assert payload["name"]


async def test_steps_run_in_the_order_given(client):
    """Reads interleaved with writes see the document as of their own position."""
    result = await _batch(
        client,
        [
            _line(),
            {"tool": "drawing_info", "args": {}},
            {"tool": "entity_create_circle", "args": {"cx": 5, "cy": 5, "radius": 2}},
            {"tool": "drawing_info", "args": {}},
        ],
    )
    assert result["ok"] is True
    assert result["results"][1]["result"]["entity_count"] == 1
    assert result["results"][3]["result"]["entity_count"] == 2


# ---------------------------------------------------------------------------
# Binding — the half that removes the handle echo
# ---------------------------------------------------------------------------


async def test_a_bound_handle_feeds_a_later_step(client):
    result = await _batch(
        client,
        [
            _line(bind="edge"),
            {"tool": "entity_move", "args": {"handle": "$edge", "dx": 5.0, "dy": 5.0}},
        ],
    )
    assert result["ok"] is True
    assert result["bindings"] == ["edge"]
    handle = result["results"][0]["result"]["handle"]
    got = (await client.call_tool("entity_get", {"handle": handle})).structured_content
    assert got["properties"]["start"][0] == pytest.approx(5.0)


async def test_a_dotted_reference_reaches_into_a_step_result(client):
    """``point_from_snap`` returns {x, y}; without ``$p.x`` that is a round trip."""
    result = await _batch(
        client,
        [
            _line(x1=0.0, y1=0.0, x2=10.0, y2=0.0, bind="edge"),
            {
                "tool": "point_from_snap",
                "args": {"handle": "$edge", "snap": "mid"},
                "bind": "mid",
            },
            {
                "tool": "entity_create_circle",
                "args": {"cx": "$mid.x", "cy": "$mid.y", "radius": 1.0},
            },
        ],
    )
    assert result["ok"] is True, result["results"]
    handle = result["results"][2]["result"]["handle"]
    circle = (await client.call_tool("entity_get", {"handle": handle})).structured_content
    assert circle["properties"]["center"][0] == pytest.approx(5.0)


async def test_a_list_index_is_a_valid_reference_segment():
    resolved = server._resolve_batch_ref("$box.handles.1", {"box": {"handles": ["A", "B", "C"]}})
    assert resolved == "B"


async def test_a_bare_reference_resolves_to_the_handle():
    resolved = server._resolve_batch_ref("$edge", {"edge": {"handle": "2F1", "type": "LINE"}})
    assert resolved == "2F1"


async def test_a_bare_reference_to_a_handleless_result_is_a_typed_error():
    with pytest.raises(server.BatchReferenceError) as excinfo:
        server._resolve_batch_ref("$info", {"info": {"entity_count": 3}})
    assert "handle" in str(excinfo.value)


async def test_a_doubled_dollar_escapes_a_literal_dollar(client):
    """Text really does start with ``$`` sometimes; ``$$`` is the way out."""
    result = await _batch(
        client,
        [{"tool": "entity_create_text", "args": {"text": "$$4.50", "x": 0, "y": 0, "height": 2.5}}],
    )
    handle = result["results"][0]["result"]["handle"]
    got = (await client.call_tool("entity_get", {"handle": handle})).structured_content
    assert got["properties"]["text"] == "$4.50"


async def test_only_a_whole_string_is_a_reference():
    """Partial interpolation is deliberately unsupported — it is ambiguous."""
    assert server._substitute_batch_refs("cost is $edge each", {"edge": {"handle": "1"}}) == (
        "cost is $edge each"
    )


# ---------------------------------------------------------------------------
# dry_run — the real JSON Schema, nothing executed
# ---------------------------------------------------------------------------


async def test_dry_run_validates_without_executing(client):
    result = await _batch(client, [_line(), _line(y1=5.0, y2=5.0)], dry_run=True)
    assert result["dry_run"] is True
    assert result["ok"] is True
    assert result["executed"] == 0
    assert [row["status"] for row in result["results"]] == ["valid", "valid"]
    assert await _entity_count(client) == 0, "a dry run must not draw anything"


async def test_dry_run_rejects_a_missing_required_argument(client):
    result = await _batch(client, [{"tool": LINE, "args": {"x1": 0, "y1": 0}}], dry_run=True)
    assert result["ok"] is False
    row = result["results"][0]
    assert row["status"] == "invalid"
    assert row["error"]["kind"] == "invalid_args"
    assert "x2" in row["error"]["message"]


async def test_dry_run_rejects_an_argument_of_the_wrong_type(client):
    result = await _batch(
        client,
        [{"tool": LINE, "args": {"x1": "over there", "y1": 0, "x2": 1, "y2": 1}}],
        dry_run=True,
    )
    assert result["results"][0]["error"]["kind"] == "invalid_args"


async def test_dry_run_rejects_an_unknown_argument(client):
    result = await _batch(
        client,
        [{"tool": LINE, "args": {"x1": 0, "y1": 0, "x2": 1, "y2": 1, "colour": 3}}],
        dry_run=True,
    )
    assert result["results"][0]["error"]["kind"] == "invalid_args"


async def test_dry_run_names_an_unknown_tool(client):
    result = await _batch(client, [{"tool": "entity_create_dragon", "args": {}}], dry_run=True)
    row = result["results"][0]
    assert row["error"]["kind"] == "unknown_tool"
    assert "entity_create_dragon" in row["error"]["message"]


async def test_dry_run_catches_a_forward_reference(client):
    """A step may only reference something an *earlier* step bound."""
    result = await _batch(
        client,
        [
            {"tool": "entity_move", "args": {"handle": "$edge", "dx": 1.0, "dy": 0.0}},
            _line(bind="edge"),
        ],
        dry_run=True,
    )
    row = result["results"][0]
    assert row["error"]["kind"] == "unresolved_ref"
    assert "edge" in row["error"]["message"]


async def test_dry_run_catches_a_reference_nobody_binds(client):
    result = await _batch(
        client,
        [{"tool": "entity_move", "args": {"handle": "$typo", "dx": 1.0, "dy": 0.0}}],
        dry_run=True,
    )
    assert result["results"][0]["error"]["kind"] == "unresolved_ref"


async def test_a_reference_stands_in_for_any_type_during_validation(client):
    """``$mid.x`` is a float at run time and a string at validation time.

    The validator must not reject it for that — but it must still check
    everything around it, which the following test proves.
    """
    result = await _batch(
        client,
        [
            _line(bind="edge"),
            {"tool": "point_from_snap", "args": {"handle": "$edge", "snap": "mid"}, "bind": "m"},
            {"tool": "entity_create_circle", "args": {"cx": "$m.x", "cy": "$m.y", "radius": 1.0}},
        ],
        dry_run=True,
    )
    assert result["ok"] is True, result["results"]


async def test_a_reference_does_not_excuse_the_rest_of_the_step(client):
    result = await _batch(
        client,
        [
            _line(bind="edge"),
            {"tool": "entity_move", "args": {"handle": "$edge", "dx": "sideways", "dy": 0.0}},
        ],
        dry_run=True,
    )
    assert result["results"][1]["error"]["kind"] == "invalid_args"


async def test_an_invalid_step_stops_a_real_batch_before_anything_runs(client):
    with pytest.raises(ToolError) as excinfo:
        await _batch(client, [_line(), {"tool": "entity_create_dragon", "args": {}}])
    assert "entity_create_dragon" in str(excinfo.value)
    assert await _entity_count(client) == 0, "validation must fail closed, before step 1 draws"


async def test_a_malformed_step_is_reported_not_crashed(client):
    result = await _batch(client, [{"args": {"x1": 0}}], dry_run=True)
    assert result["results"][0]["error"]["kind"] == "malformed_step"


# ---------------------------------------------------------------------------
# Privilege — the deny set
# ---------------------------------------------------------------------------


async def test_the_deny_set_is_derived_from_the_escape_cost_cards():
    """Hand-listing would go stale the day a new escape hatch is registered."""
    escapes = {
        tool.name
        for tool in await server._registered_tools()
        if ((tool.meta or {}).get("cad") or {}).get("cost") == "escape"
    }
    assert escapes, "the registry must still carry escape-cost tools for this to mean anything"
    assert server._batch_denied_tools() == escapes | {"cad_batch"}


async def test_the_raw_command_and_lisp_hatches_are_denied():
    denied = server._batch_denied_tools()
    assert {"system_run_command", "system_run_lisp"} <= denied


@pytest.mark.parametrize("tool", ["system_run_command", "system_run_lisp"])
async def test_a_denied_tool_refuses_the_whole_batch(client, tool):
    args = {"command": "_ZOOM E"} if tool.endswith("command") else {"expression": "(+ 1 1)"}
    with pytest.raises(ToolError) as excinfo:
        await _batch(client, [_line(), {"tool": tool, "args": args}])
    message = str(excinfo.value)
    assert tool in message
    assert "denied" in message.lower() or "not callable" in message.lower()
    assert await _entity_count(client) == 0, "a denied step must abort before step 1 draws"


async def test_a_denied_step_is_not_survivable_with_on_error_continue(client):
    """Otherwise the deny set would just be a step you skip past."""
    with pytest.raises(ToolError):
        await _batch(
            client,
            [{"tool": "system_run_lisp", "args": {"expression": "(+ 1 1)"}}, _line()],
            on_error="continue",
        )
    assert await _entity_count(client) == 0


async def test_cad_batch_cannot_nest_inside_itself(client):
    """No recursion, and no laundering a denied step through a nested executor."""
    result = await _batch(
        client, [{"tool": "cad_batch", "args": {"steps": [_line()]}}], dry_run=True
    )
    assert result["results"][0]["error"]["kind"] == "denied"
    with pytest.raises(ToolError, match="cad_batch"):
        await _batch(client, [{"tool": "cad_batch", "args": {"steps": [_line()]}}])


async def test_a_tool_with_no_discovery_card_is_not_run_blind(client):
    """Fail closed on unknown provenance: the deny set is computed from the local
    registry, but a mounted or transformed provider can supply a tool it never saw.
    Every tool this server registers carries a card (there is a coverage gate),
    so this costs the native surface nothing."""

    @server.mcp.tool(name="_batch_uncarded_probe")
    async def _uncarded(ctx=None) -> dict:
        return {"ok": True}

    try:
        result = await _batch(client, [{"tool": "_batch_uncarded_probe", "args": {}}], dry_run=True)
        assert result["results"][0]["error"]["kind"] == "denied"
        assert "card" in result["results"][0]["error"]["message"]
    finally:
        server.mcp.local_provider.remove_tool("_batch_uncarded_probe")


async def test_a_dry_run_reports_a_denied_step_rather_than_hiding_it(client):
    result = await _batch(
        client,
        [{"tool": "system_run_command", "args": {"command": "_ZOOM E"}}],
        dry_run=True,
    )
    assert result["ok"] is False
    assert result["results"][0]["error"]["kind"] == "denied"


async def test_the_refusal_points_at_the_direct_tool(client):
    """The escape hatch stays reachable — separately, where its own gate applies."""
    with pytest.raises(ToolError) as excinfo:
        await _batch(client, [{"tool": "system_run_command", "args": {"command": "_ZOOM E"}}])
    assert "directly" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The production laundering vector: DISCOVERY_MODE=search
# ---------------------------------------------------------------------------
#
# In search mode the advertised surface is just ``search_tools`` + ``call_tool``,
# and ``call_tool`` invokes any tool by name. That is a general-purpose proxy
# sitting inside the same server as a denylist — i.e. exactly the shape of a way
# around it. The deny set is derived from ``cost="escape"`` cards, and the proxy
# is not a local component, so nothing in the *denylist* stops it; what stops it
# is the fail-closed "no @cad_tool card, no execution" rule.
#
# These pin that, because the coupling is invisible: giving ``call_tool`` a card
# — an entirely reasonable thing to want, so it is costed and discoverable —
# silently reopens the escalation path with no other test failing.


@pytest_asyncio.fixture
async def search_mode(monkeypatch):
    """The real search surface, restored to ``off`` afterwards."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    server._apply_discovery_mode("search")
    try:
        async with Client(server.mcp) as connected:
            yield connected
    finally:
        server._apply_discovery_mode("off")


async def test_search_mode_advertises_the_proxy_that_makes_this_a_risk(search_mode):
    names = {tool.name for tool in await search_mode.list_tools()}
    assert names == {"search_tools", "call_tool"}, (
        "if the search surface changed, the laundering test below is aiming at the wrong target"
    )


@pytest.mark.parametrize("hatch", ["system_run_command", "system_run_lisp"])
async def test_the_search_proxy_cannot_launder_a_denied_hatch(search_mode, hatch):
    """cad_batch -> call_tool -> system_run_* must not execute."""
    result = (
        await search_mode.call_tool(
            "call_tool",
            {
                "name": "cad_batch",
                "arguments": {
                    "steps": [{"tool": "call_tool", "args": {"name": hatch, "arguments": {}}}],
                    "dry_run": True,
                },
            },
        )
    ).structured_content or {}
    assert result["ok"] is False
    assert result["results"][0]["error"]["kind"] == "denied"


async def test_the_laundered_hatch_is_refused_before_anything_executes(search_mode):
    """Not merely reported — the whole batch is refused, so the proxy never runs."""
    with pytest.raises(ToolError) as excinfo:
        await search_mode.call_tool(
            "call_tool",
            {
                "name": "cad_batch",
                "arguments": {
                    "steps": [
                        {
                            "tool": "call_tool",
                            "args": {
                                "name": "system_run_command",
                                "arguments": {"command": "_ZOOM E"},
                            },
                        }
                    ]
                },
            },
        )
    assert "refused all 1 steps - nothing was executed" in str(excinfo.value)


async def test_cad_batch_still_works_through_the_search_proxy(search_mode):
    """The denial above must be a denial, not search mode breaking batching."""
    result = (
        await search_mode.call_tool(
            "call_tool",
            {
                "name": "cad_batch",
                "arguments": {
                    "steps": [{"tool": "drawing_new", "args": {}}, _line()],
                },
            },
        )
    ).structured_content or {}
    assert result["ok"] is True
    assert result["succeeded"] == 2


async def test_a_batch_step_may_still_call_the_older_batch_tools(client):
    """The reconciliation: they compose. cad_batch orders, entity_batch_create bulks."""
    result = await _batch(
        client,
        [
            {
                "tool": "entity_batch_create",
                "args": {
                    "entities": [
                        {"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1},
                        {"type": "circle", "cx": 0, "cy": 0, "radius": 1},
                    ]
                },
            },
            _line(),
        ],
    )
    assert result["ok"] is True
    assert await _entity_count(client) == 3


async def test_a_tool_hidden_by_the_profile_is_not_reachable_through_a_batch(client):
    """cad_batch must not be a way around TOOL_PROFILE or the ENABLE_3D gate."""
    result = await _batch(client, [{"tool": "solid_box", "args": {}}], dry_run=True)
    assert result["results"][0]["error"]["kind"] == "unknown_tool"


# ---------------------------------------------------------------------------
# Error taxonomy — typed, never string-sniffed
# ---------------------------------------------------------------------------


async def test_a_backend_capability_refusal_is_typed_as_unsupported(client, tmp_path):
    result = await _batch(
        client,
        [{"tool": "drawing_save_as", "args": {"path": str(tmp_path / "part.dwg")}}],
        on_error="continue",
    )
    error = result["results"][0]["error"]
    assert error["kind"] == "unsupported"
    assert error["capability"] == "dwg", "the capability key must survive the re-entrant wrapper"


async def test_the_classifier_reads_the_cause_chain_not_the_message():
    """The proof that no message text is being sniffed: same words, different type."""
    from backends.ezdxf_backend import UnsupportedCapabilityError

    liar = RuntimeError("the headless ezdxf backend cannot write DWG (capability 'dwg')")
    assert server._classify_batch_error(liar)["kind"] == "failed"

    honest = ToolError("Error calling tool 'drawing_save_as': nothing useful here")
    honest.__cause__ = UnsupportedCapabilityError("dwg", "no DWG writer")
    classified = server._classify_batch_error(honest)
    assert classified["kind"] == "unsupported"
    assert classified["capability"] == "dwg"


async def test_a_backend_failure_is_typed_as_failed(client):
    result = await _batch(
        client, [{"tool": "entity_get", "args": {"handle": "NOSUCH"}}], on_error="continue"
    )
    assert result["results"][0]["error"]["kind"] == "failed"


async def test_a_deliberate_tool_refusal_is_typed_as_refused(client):
    result = await _batch(
        client,
        [{"tool": "drawing_open", "args": {"path": "../../etc/passwd"}}],
        on_error="continue",
    )
    assert result["results"][0]["error"]["kind"] == "refused"


async def test_a_bound_value_of_the_wrong_type_is_typed_at_run_time(client):
    """The one thing dry_run cannot check, reported precisely when it happens."""
    result = await _batch(
        client,
        [
            {"tool": "entity_create_text", "args": {"text": "T", "x": 0, "y": 0}, "bind": "t"},
            {"tool": "entity_move", "args": {"handle": "$t", "dx": "$t.type", "dy": 0.0}},
        ],
        on_error="continue",
    )
    assert result["results"][1]["error"]["kind"] == "invalid_args"


async def test_every_reported_kind_is_a_declared_kind(client, tmp_path):
    result = await _batch(
        client,
        [
            {"tool": "entity_get", "args": {"handle": "NOSUCH"}},
            {"tool": "drawing_save_as", "args": {"path": str(tmp_path / "p.dwg")}},
            _line(),
        ],
        on_error="continue",
    )
    kinds = {row["error"]["kind"] for row in result["results"] if row["status"] == "error"}
    assert kinds <= set(server.BATCH_ERROR_KINDS)
    assert kinds


# ---------------------------------------------------------------------------
# on_error
# ---------------------------------------------------------------------------


async def test_stop_is_the_default_because_both_backends_deliver_it(client):
    result = await _batch(client, [_line()])
    assert result["on_error"] == "stop"


async def test_stop_leaves_earlier_steps_applied_and_says_so(client):
    result = await _batch(
        client,
        [_line(), {"tool": "entity_get", "args": {"handle": "NOSUCH"}}, _line(y1=9.0, y2=9.0)],
        on_error="stop",
    )
    assert result["ok"] is False
    statuses = [row["status"] for row in result["results"]]
    assert statuses == ["ok", "error", "skipped"]
    assert result["skipped"] == 1
    assert await _entity_count(client) == 1
    assert result["atomicity"]["rolled_back"] is False
    assert result["atomicity"]["guarantee"] == "none"


async def test_continue_runs_every_step(client):
    result = await _batch(
        client,
        [_line(), {"tool": "entity_get", "args": {"handle": "NOSUCH"}}, _line(y1=9.0, y2=9.0)],
        on_error="continue",
    )
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    assert result["succeeded"] == 2 and result["failed"] == 1
    assert await _entity_count(client) == 2


async def test_continue_does_not_execute_a_step_whose_binding_never_landed(client):
    """A dependent step must not silently run on a stale or missing value."""
    result = await _batch(
        client,
        [
            {"tool": "entity_get", "args": {"handle": "NOSUCH"}, "bind": "ghost"},
            {"tool": "entity_move", "args": {"handle": "$ghost", "dx": 1.0, "dy": 0.0}},
            _line(),
        ],
        on_error="continue",
    )
    assert result["results"][0]["status"] == "error"
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["kind"] == "unresolved_ref"
    assert result["results"][2]["status"] == "ok"


async def test_an_unknown_on_error_mode_is_rejected(client):
    with pytest.raises(ToolError):
        await _batch(client, [_line()], on_error="pray")


# ---------------------------------------------------------------------------
# Atomicity — honest per backend
# ---------------------------------------------------------------------------


class _Caps:
    def __init__(self, mode, supported=True):
        from backends.base import CapabilityMap, FeatureCapability

        self.map = CapabilityMap(
            backend="stub", features={"transactions": FeatureCapability(supported, mode)}
        )

    def capabilities(self):
        return self.map


async def test_a_snapshot_backend_is_reported_as_a_snapshot_guarantee():
    guarantee, note = server._batch_rollback_guarantee(_Caps("snapshot"))
    assert guarantee == "snapshot"
    assert "snapshot" in note.lower()


async def test_an_undo_mark_backend_is_never_called_atomic():
    """COM ends the undo mark and fires ``_UNDO B`` without confirming it landed."""
    guarantee, note = server._batch_rollback_guarantee(_Caps("undo_mark"))
    assert guarantee == "best_effort_undo"
    lowered = note.lower()
    assert "not atomic" in lowered, "it must say so, not merely omit the word"
    assert "best-effort" in lowered
    assert "does not confirm" in lowered


async def test_a_backend_without_transactions_gets_no_guarantee():
    guarantee, _ = server._batch_rollback_guarantee(_Caps(None, supported=False))
    assert guarantee == "none"


async def test_an_unrecognised_transaction_mode_is_not_assumed_good():
    guarantee, _ = server._batch_rollback_guarantee(_Caps("time_travel"))
    assert guarantee == "unverified"


async def test_rollback_restores_the_document_on_ezdxf(client):
    await client.call_tool("entity_create_circle", {"cx": 0, "cy": 0, "radius": 1})
    result = await _batch(
        client,
        [_line(), _line(y1=4.0, y2=4.0), {"tool": "entity_get", "args": {"handle": "NOSUCH"}}],
        on_error="rollback",
    )
    assert result["ok"] is False
    assert await _entity_count(client) == 1, "the two batch lines must be gone"
    atomicity = result["atomicity"]
    assert atomicity["guarantee"] == "snapshot"
    assert atomicity["rolled_back"] is True
    assert atomicity["verified"] is True
    assert atomicity["before"] == atomicity["after"]


async def test_a_successful_rollback_batch_commits_and_keeps_its_work(client):
    result = await _batch(client, [_line(), _line(y1=4.0, y2=4.0)], on_error="rollback")
    assert result["ok"] is True
    assert result["atomicity"]["rolled_back"] is False
    assert result["atomicity"]["committed"] is True
    assert await _entity_count(client) == 2


async def test_rollback_reports_the_guarantee_it_actually_got(client):
    result = await _batch(client, [_line()], on_error="rollback")
    atomicity = result["atomicity"]
    assert atomicity["mode"] == "rollback"
    assert atomicity["guarantee"] in server.BATCH_ROLLBACK_GUARANTEES
    assert atomicity["note"]


async def test_rollback_refuses_when_the_backend_declines_the_checkpoint(client, monkeypatch):
    """A checkpoint we did not open is not one we can roll back to.

    This is the live COM path: ``ComBackend.transaction_begin`` *returns*
    ``{"ok": False, "error": "A transaction is already active"}`` rather than
    raising, so a batch that took the return value on faith would run every
    step and then report a rollback it never had. Simulated on the headless
    backend, whose own transaction stack happily nests.
    """
    from backends.ezdxf_backend import EzdxfBackend

    async def _decline(self):
        return {"ok": False, "error": "A transaction is already active"}

    monkeypatch.setattr(EzdxfBackend, "transaction_begin", _decline)
    result = await _batch(client, [_line()], on_error="rollback")
    assert result["ok"] is False
    assert result["executed"] == 0
    assert result["atomicity"]["transaction"] is False
    assert "already active" in result["atomicity"]["note"]
    assert await _entity_count(client) == 0, "nothing may run without the checkpoint"


async def test_a_rollback_batch_does_not_disturb_a_transaction_the_caller_opened(client):
    """The headless backend's checkpoints are a stack; ours must pop only its own."""
    await client.call_tool("transaction_begin", {})
    await client.call_tool("entity_create_circle", {"cx": 0, "cy": 0, "radius": 1})
    result = await _batch(
        client,
        [_line(), {"tool": "entity_get", "args": {"handle": "NOSUCH"}}],
        on_error="rollback",
    )
    assert result["atomicity"]["rolled_back"] is True
    assert await _entity_count(client) == 1, "the caller's circle survives our rollback"
    await client.call_tool("transaction_rollback", {})
    assert await _entity_count(client) == 0, "the caller's own checkpoint still works"


async def test_non_rollback_modes_make_no_atomicity_claim(client):
    """And say it in four keys, not in two paragraphs about a rollback that was
    never on the table — that boilerplate measured 61% of a short batch's reply."""
    for mode in ("stop", "continue"):
        result = await _batch(client, [_line()], on_error=mode)
        atomicity = result["atomicity"]
        assert atomicity["transaction"] is False
        assert atomicity["rolled_back"] is False
        assert atomicity["guarantee"] == "none"
        assert set(atomicity) == {"mode", "guarantee", "transaction", "rolled_back", "note"}
        assert "atomic" not in atomicity["note"].lower()
        assert len(dumps(atomicity)) < 200


async def test_a_dry_run_opens_no_transaction(client):
    result = await _batch(client, [_line()], on_error="rollback", dry_run=True)
    assert result["atomicity"]["transaction"] is False
    status = (await client.call_tool("system_status", {})).structured_content or {}
    assert status.get("transaction_depth", 0) == 0


# ---------------------------------------------------------------------------
# Limits and progress
# ---------------------------------------------------------------------------


async def test_an_empty_batch_is_rejected(client):
    with pytest.raises(ToolError):
        await _batch(client, [])


async def test_the_step_count_is_capped(client):
    with pytest.raises(ToolError) as excinfo:
        await _batch(client, [_line()] * (server.MAX_BATCH_STEPS + 1))
    assert str(server.MAX_BATCH_STEPS) in str(excinfo.value)


async def test_progress_is_reported_for_every_step(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    seen: list[tuple[float, float | None]] = []

    async def handler(progress, total, message=None):
        seen.append((progress, total))

    async with Client(server.mcp, progress_handler=handler) as connected:
        await connected.call_tool("drawing_new", {})
        await connected.call_tool(
            "cad_batch", {"steps": [_line(), _line(y1=2.0, y2=2.0), _line(y1=4.0, y2=4.0)]}
        )
    assert seen, "a long batch is the canonical case for ctx.report_progress"
    assert (3.0, 3.0) in [(p, t) for p, t in seen], "the final tick must report completion"


# ---------------------------------------------------------------------------
# The rollback claim must come from the backend, not from control flow
# ---------------------------------------------------------------------------


async def test_rollback_is_not_claimed_when_the_backend_declines_it(client):
    """A step that commits consumes the checkpoint out from under us.

    ``transaction_rollback()`` then returns ``{"ok": False, "error": "No active
    transaction to rollback"}``. Reporting ``rolled_back: True`` off control
    flow would be the same class of lie as writing DXF bytes into a .dwg.
    """
    steps = [
        _line(),
        {"tool": "transaction_commit", "args": {}},
        {"tool": "entity_get", "args": {"handle": "NOSUCH"}},
    ]
    result = await _batch(client, steps, on_error="rollback")

    assert result["ok"] is False
    assert result["atomicity"]["rolled_back"] is False, (
        "the backend refused the rollback; the payload must not claim it happened"
    )
    assert await _entity_count(client) == 1, "the line really did survive"


async def test_commit_is_not_claimed_when_the_backend_declines_it(client):
    """Same rule on the success path: a step that already committed leaves
    nothing for the batch's own commit to do."""
    steps = [_line(), {"tool": "transaction_commit", "args": {}}]
    result = await _batch(client, steps, on_error="rollback")

    assert result["ok"] is True
    assert result["atomicity"]["committed"] is False, (
        "the checkpoint was already gone; do not report a commit that did not happen"
    )


async def test_a_read_only_finding_is_not_a_failed_step(client):
    """A checker reporting findings must not be mistaken for a step that failed.

    ``validation_check`` is ``readOnlyHint=True`` and answers
    ``{"ok": len(issues) == 0}`` — ``ok: False`` there means *it found something*,
    not *it could not run*. Treating that as a refusal made a read-only check
    trigger a verified rollback that destroyed the geometry the batch had just
    drawn. A mutator's ``ok: False`` still means it did not mutate.
    """
    steps = [_line(), {"tool": "validation_check", "args": {}}]
    result = await _batch(client, steps, on_error="rollback")

    assert result["succeeded"] == 2
    assert result["failed"] == 0
    assert result["ok"] is True
    assert result["atomicity"]["rolled_back"] is False
    assert await _entity_count(client) == 1, "the read-only check must not undo the line"
