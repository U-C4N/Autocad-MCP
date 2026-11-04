"""1.8-02: one typed error envelope for every tool call, direct or as a batch step.

Before 1.8.0 only ``cad_batch`` typed its failures; a direct call that failed
reached the client as ``Internal error: ...`` prose. Now the refusal middleware
types every exception with ``engineering.errors.classify`` - the function
``cad_batch`` types a step with - and returns ``ToolResult(is_error=True)``
whose structured content is ``{ok: false, kind, category, retryable, hint,
tool, backend, operation_id, message, ...}``; the text content is what the
client read before, byte for byte. ``retryable`` is true only when the
operation record is ``refused`` / ``cancelled``: nothing was written.

The live engine is covered on a fake seat (no AutoCAD): ``HandleToObject``
failing is ``not_found``, and ``RPC_E_CALL_REJECTED`` at the job's first touch
is ``application_busy`` - retryable, revision unmoved.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest
import pytest_asyncio
from fastmcp import Client

import config
import server
from backends import com_backend as cb
from backends.quarantine import DaemonExecutor
from engineering import errors

_RPC_E_CALL_REJECTED = -2147418111


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def _error(client, tool: str, args: dict):
    result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.is_error is True, f"{tool} did not fail"
    payload = result.structured_content
    assert isinstance(payload, dict), f"{tool}: an error must not arrive as prose only"
    return result, payload


async def _step_error(client, tool: str, args: dict) -> dict:
    """The step row's error. A step the batch refuses before running anything
    (schema, deny set) is read off the dry run, where its row is reported."""
    steps = [{"tool": tool, "args": args}]
    result = await client.call_tool(
        "cad_batch", {"steps": steps, "on_error": "continue"}, raise_on_error=False
    )
    if result.is_error:
        result = await client.call_tool("cad_batch", {"steps": steps, "dry_run": True})
    (row,) = result.structured_content["results"]
    assert row["status"] in {"error", "invalid"}, row
    return row["error"]


async def _record(client, operation_id: str) -> dict:
    status = (await client.call_tool("system_status", {"detail": "operations"})).structured_content
    (row,) = [r for r in status["operations"] if r["operation_id"] == operation_id]
    return row


# ── the table ───────────────────────────────────────────────────────────────


def test_the_table_declares_every_kind_once_with_a_known_category():
    assert len(set(errors.ERROR_KINDS)) == len(errors.ERROR_KINDS)
    assert set(errors.ERROR_TABLE) == set(errors.ERROR_KINDS)
    for kind, spec in errors.ERROR_TABLE.items():
        assert spec.category in errors.CATEGORIES, kind
        assert spec.hint and len(spec.hint) <= 90, kind
    assert errors.ERROR_KINDS[: len(errors.BATCH_KINDS_1_7)] == errors.BATCH_KINDS_1_7
    for kind in (
        "not_found",
        "unknown_handle",
        "path_refused",
        "policy_refused",
        "file_not_found",
        "application_busy",
        "command_incomplete",
        "internal",
        "validation_failed",
        "critique_gate_failed",
        "failed_acceptance",
        "damaged_drawing",
        "recover_unsupported",
        "recovered_source_protected",
        "save_locked",
        "save_verify_failed",
        "save_failed",
        "process_exit",
    ):
        assert kind in errors.ERROR_KINDS, kind
    assert server.BATCH_ERROR_KINDS == errors.ERROR_KINDS


def test_every_kind_attribute_the_code_raises_is_declared():
    """A ``.kind`` an exception class carries reaches the wire through
    ``classify``; an undeclared one would read as ``internal``'s row."""
    from backends import atomic_write, dwg_read, dxf_recover, operations

    raised = [
        dwg_read.DwgConversionError,
        dxf_recover.DrawingParseError,
        dxf_recover.RecoveredSourceError,
        dxf_recover.RecoverUnsupportedError,
        atomic_write.AtomicWriteError,
        atomic_write.SaveVerifyError,
        atomic_write.SaveLockedError,
        operations.OperationConflictError,
        operations.RequiresReconcileError,
        errors.EntityNotFoundError,
        errors.ApplicationBusyError,
        errors.CommandIncompleteError,
        errors.InvalidArgsError,
        errors.PathRefusedError,
    ]
    for cls in raised:
        assert cls.kind in errors.ERROR_KINDS, cls


#: 1.8-04's kinds: (record state, retryable once refused, category). Verified
#: against the code: a damaged / DWG source is refused before anything is
#: opened, a recovered source before anything is written, a locked target is
#: left as it was by the atomic protocol; a save that failed its check or broke
#: was attempted.
FILE_KIND_OUTCOMES = {
    "damaged_drawing": ("refused", False, "file"),
    "recover_unsupported": ("refused", False, "file"),
    "recovered_source_protected": ("refused", False, "policy"),
    "save_locked": ("refused", True, "file"),
    "save_verify_failed": ("failed", False, "file"),
    "save_failed": ("failed", False, "file"),
    # K1: no converter route produced a DXF - nothing was opened.
    "dwg_conversion_failed": ("refused", False, "dependency"),
}


@pytest.mark.parametrize("kind", sorted(FILE_KIND_OUTCOMES))
def test_a_file_kind_has_its_record_state_and_retryable(kind):
    state, retryable, category = FILE_KIND_OUTCOMES[kind]
    assert server._STATE_OF_KIND[kind] == state
    assert errors.retryable_for(kind, state) is retryable
    assert errors.describe(kind).category == category
    # The journal's own kind for a dead owner's row.
    assert server._STATE_OF_KIND["process_exit"] == "unknown_commit"
    assert errors.retryable_for("process_exit", "unknown_commit") is False


def test_the_file_exceptions_classify_by_type_through_the_wrap():
    from fastmcp.exceptions import ToolError

    from backends import atomic_write, dxf_recover

    cases = {
        dxf_recover.DrawingParseError("x.dxf could not be read"): "damaged_drawing",
        dxf_recover.RecoverUnsupportedError("x.dwg is a DWG"): "recover_unsupported",
        dxf_recover.recovered_source_refusal("drawing_save", "x", "x.dxf"): (
            "recovered_source_protected"
        ),
        atomic_write.SaveLockedError("locked"): "save_locked",
        atomic_write.SaveVerifyError("mismatch"): "save_verify_failed",
        atomic_write.AtomicWriteError("broke"): "save_failed",
    }
    for exc, kind in cases.items():
        wrapped = ToolError(f"Error calling tool 'drawing_save': {exc}")
        wrapped.__cause__ = exc
        assert errors.classify(wrapped)["kind"] == kind, exc
        assert errors.classify(exc)["kind"] == kind, exc
    # A ValueError that merely says the words stays what it was.
    assert errors.classify(ValueError("could not be read as a DXF file"))["kind"] == "failed"


def test_retryable_is_only_ever_true_when_nothing_was_written():
    for kind in errors.ERROR_KINDS:
        for state in ("committed", "partial", "failed", "quarantined", "unknown_commit"):
            assert errors.retryable_for(kind, state) is False, (kind, state)
    assert errors.retryable_for("application_busy", "refused") is True
    assert errors.retryable_for("not_found", "refused") is False


def test_a_bug_is_internal_but_a_library_lookup_error_is_not():
    assert errors.classify(AttributeError("'NoneType' has no attribute 'dxf'"))["kind"] == (
        "internal"
    )

    class LibraryKeyError(KeyError):
        pass

    assert errors.classify(LibraryKeyError("layer"))["kind"] == "failed"
    # The server raises these on purpose to refuse a caller's argument.
    for refusal in (TypeError("DIMTXT: expected a number"), KeyError("x1"), IndexError("0")):
        assert errors.classify(refusal)["kind"] == "failed", refusal
    assert errors.classify(RuntimeError("Entity with handle 'X' not found."))["kind"] == (
        "failed"
    ), "typed, never read off the text"


# ── the provoked table, direct and as a batch step ──────────────────────────


async def _provoke(client, tmp_path, monkeypatch, case: str) -> tuple[str, dict]:
    if case == "missing_handle":
        return "entity_move", {"handle": "FFFFFF", "dx": 1.0, "dy": 0.0}
    if case == "schema":
        return "entity_create_line", {"x1": "a", "y1": 0, "x2": 1, "y2": 1}
    if case == "outside_allowed_paths":
        allowed = tmp_path / "allowed"
        allowed.mkdir(exist_ok=True)
        monkeypatch.setattr(config.settings, "allowed_paths", [allowed.resolve()])
        return "drawing_open", {"path": str(tmp_path / "elsewhere.dxf")}
    if case == "unsupported":
        return "drawing_save_as", {"path": str(tmp_path / "part.dwg")}
    if case == "stale_preview":
        await client.call_tool("entity_create_text", {"text": "OLD", "x": 0, "y": 0})
        status = (await client.call_tool("system_status", {})).structured_content
        return "text_find_replace", {
            "find": "OLD",
            "replace": "NEW",
            "expected_revision": int(status["revision"]) + 7,
        }
    if case == "missing_file":
        return "drawing_open", {"path": str(tmp_path / "missing.dxf")}
    raise AssertionError(case)


CASES = {
    "missing_handle": "not_found",
    "schema": "invalid_args",
    "outside_allowed_paths": "path_refused",
    "unsupported": "unsupported",
    "stale_preview": "stale_revision",
    "missing_file": "file_not_found",
}


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.asyncio
async def test_a_provoked_error_has_one_kind_direct_and_as_a_batch_step(
    client, tmp_path, monkeypatch, case
):
    tool, args = await _provoke(client, tmp_path, monkeypatch, case)
    _, payload = await _error(client, tool, args)
    kind = CASES[case]
    assert payload["kind"] == kind
    assert payload["ok"] is False and payload["tool"] == tool and payload["backend"] == "ezdxf"
    spec = errors.describe(kind)
    assert payload["category"] == spec.category and payload["hint"] == spec.hint
    assert isinstance(payload["retryable"], bool)
    assert payload["message"]
    record = await _record(client, payload["operation_id"])
    assert record["error_kind"] == kind
    if payload["retryable"]:
        assert record["state"] in {"refused", "cancelled"}

    step = await _step_error(client, tool, args)
    assert step["kind"] == STEP_KINDS.get(case, kind), "the direct route and the batch step agree"
    assert step["category"] == spec.category and isinstance(step["retryable"], bool)


#: 1.8-09: a LITERAL handle that names nothing never reaches its step - the
#: batch's pre-check refuses the whole batch first, as unknown_handle (same
#: category, not retryable either). A handle that goes missing while the batch
#: runs still reaches its step, and is not_found there, as directly.
STEP_KINDS = {"missing_handle": "unknown_handle"}


@pytest.mark.asyncio
async def test_a_handle_missing_at_run_time_is_not_found_in_a_step_too(client):
    result = await client.call_tool(
        "cad_batch",
        {
            "steps": [
                {"tool": "entity_create_line", "args": _LINE, "bind": "gone"},
                {"tool": "entity_delete", "args": {"handle": "$gone"}},
                {"tool": "entity_move", "args": {"handle": "$gone", "dx": 1.0, "dy": 0.0}},
            ],
            "on_error": "continue",
        },
    )
    step = result.structured_content["results"][2]["error"]
    assert step["kind"] == "not_found"
    assert step["category"] == errors.describe("not_found").category


@pytest.mark.parametrize(
    "tool, args, channel",
    [
        ("system_run_lisp", {"expression": '(command "_.ERASE" "_L" "")'}, "lisp"),
        ("system_run_command", {"command": "_.ERASE"}, "command"),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_lisp_or_command_is_policy_refused_on_both_routes(
    client, tool, args, channel
):
    _, payload = await _error(client, tool, args)
    assert payload["kind"] == "policy_refused" and payload["category"] == "policy"
    assert payload["channel"] == channel and payload["verbs"] == ["ERASE"]
    if channel == "lisp":
        assert "command" in payload["symbols"]
    step = await _step_error(client, tool, args)
    assert step["kind"] == "policy_refused", "the direct route and the batch step must agree"
    assert step["category"] == "policy" and step["hint"] == payload["hint"]
    assert step["verbs"] == payload["verbs"] and step["channel"] == channel
    assert step["alternatives"] == payload["alternatives"]
    assert any(row["tool"] == "entity_delete" for row in step["alternatives"])


@pytest.mark.asyncio
async def test_an_allowed_lisp_is_still_denied_inside_a_batch(client):
    """The escape hatches never run inside a batch; ``denied`` is for a string a
    direct call would run, so its hint (call it directly) is true."""
    step = await _step_error(client, "system_run_lisp", {"expression": '(setvar "LTSCALE" 2.0)'})
    assert step["kind"] == "denied"


@pytest.mark.parametrize(
    "tool, args",
    [
        ("block_define", {"name": "B1", "entities": [{"type": "line"}]}),
        ("dimstyle_create", {"name": "D1", "overrides": {"DIMTXT": "big"}}),
    ],
)
@pytest.mark.asyncio
async def test_a_deliberate_type_error_refusal_is_never_called_a_server_bug(client, tool, args):
    """block_specs / dimstyles refuse a wrong-typed value with TypeError: the
    caller's argument, ``failed`` as in 1.7 - never ``internal``."""
    _, payload = await _error(client, tool, args)
    assert payload["kind"] == "failed", payload
    assert payload["category"] != "internal" and "server bug" not in payload["hint"]
    record = await _record(client, payload["operation_id"])
    assert record["error_kind"] == "failed"
    step = await _step_error(client, tool, args)
    assert step["kind"] == "failed", "the direct route and the batch step must agree"


def _damaged_dxf(folder) -> str:
    """A 20-line DXF cut before its last ENDSEC and EOF: ezdxf refuses it,
    its recover reader reads it back."""
    doc = ezdxf.new("R2010")
    for index in range(20):
        doc.modelspace().add_line((0, index), (10, index))
    intact = folder / "intact.dxf"
    doc.saveas(intact)
    text = intact.read_text(encoding="utf-8")
    damaged = folder / "damaged.dxf"
    damaged.write_text(text[: text.rindex("ENDSEC")], encoding="utf-8")
    return str(damaged)


async def _routes_agree(client, tool: str, args: dict, kind: str) -> dict:
    """The direct envelope and the batch step row: one kind, category, retryable."""
    _, payload = await _error(client, tool, args)
    spec = errors.describe(kind)
    assert payload["kind"] == kind, payload
    assert payload["category"] == spec.category and payload["hint"] == spec.hint
    record = await _record(client, payload["operation_id"])
    assert record["error_kind"] == kind
    assert record["state"] == server._STATE_OF_KIND[kind]
    step = await _step_error(client, tool, args)
    assert step["kind"] == kind, "the direct route and the batch step must agree"
    assert step["category"] == payload["category"] and step["hint"] == payload["hint"]
    assert step["retryable"] is payload["retryable"]
    (step_record,) = [
        r
        for r in (
            await client.call_tool("system_status", {"detail": "operations"})
        ).structured_content["operations"]
        if r["operation_id"] == step["operation_id"]
    ]
    assert (step_record["state"], step_record["error_kind"]) == (record["state"], kind)
    return payload


@pytest.mark.asyncio
async def test_a_damaged_drawing_is_one_kind_direct_and_as_a_batch_step(client, tmp_path):
    damaged = _damaged_dxf(tmp_path)
    payload = await _routes_agree(client, "drawing_open", {"path": damaged}, "damaged_drawing")
    assert payload["retryable"] is False and payload["category"] == "file"
    assert "recover=True" in payload["message"]


@pytest.mark.asyncio
async def test_a_protected_recovered_source_is_one_kind_direct_and_as_a_batch_step(
    client, tmp_path
):
    damaged = _damaged_dxf(tmp_path)
    source = Path(damaged).read_bytes()
    await client.call_tool("drawing_open", {"path": damaged, "recover": True})
    payload = await _routes_agree(client, "drawing_save", {}, "recovered_source_protected")
    assert payload["retryable"] is False and payload["category"] == "policy"
    assert "overwrite_source=True" in payload["message"]
    assert Path(damaged).read_bytes() == source, "refused: the source was not written"


@pytest.mark.asyncio
async def test_a_locked_target_is_save_locked_refused_and_retryable(client, tmp_path, monkeypatch):
    """The atomic protocol leaves a locked target as it was and removes its tmp:
    nothing was written, so the envelope invites the retry - and it runs."""
    import os

    from backends import atomic_write

    target = tmp_path / "part.dxf"
    target.write_bytes(b"previous bytes\n")
    real_replace = os.replace
    locked = {"on": True}

    def replace(src, dst, *a, **kw):
        if locked["on"] and os.path.exists(dst) and os.path.samefile(dst, target):
            raise PermissionError(13, "The process cannot access the file", str(dst))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(atomic_write, "REPLACE_PAUSE_S", 0.0)
    monkeypatch.setattr(os, "replace", replace)
    await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
    args = {"path": str(target)}
    payload = await _routes_agree(client, "drawing_save_as", args, "save_locked")
    assert payload["retryable"] is True and payload["category"] == "file"
    assert target.read_bytes() == b"previous bytes\n", "the target is untouched"
    assert not list(tmp_path.glob("part.dxf.acadmcp-*.tmp")), "the tmp was removed"

    meta = {"operation_id": "locked-save-1"}
    first = await client.call_tool("drawing_save_as", args, raise_on_error=False, meta=meta)
    assert first.structured_content["kind"] == "save_locked"
    locked["on"] = False  # the other program let go
    retry = await client.call_tool("drawing_save_as", args, raise_on_error=False, meta=meta)
    assert retry.is_error is False, retry.structured_content
    assert not (retry.structured_content or {}).get("replayed")
    assert ezdxf.readfile(target).modelspace().query("LINE")


@pytest.mark.asyncio
async def test_a_save_that_fails_its_check_is_failed_not_retryable(client, tmp_path, monkeypatch):
    from backends import atomic_write

    def mismatch(doc, tmp, data, mode):
        raise atomic_write.SaveVerifyError("save: the written file failed its check (test)")

    monkeypatch.setattr(atomic_write, "_verify", mismatch)
    target = tmp_path / "part.dxf"
    payload = await _routes_agree(
        client, "drawing_save_as", {"path": str(target)}, "save_verify_failed"
    )
    assert payload["retryable"] is False
    assert not target.exists(), "nothing was put in place"


# ── the journal's own refusals (AuditMiddleware) ────────────────────────────


def _assert_journal_envelope(result, kind: str, text: str, tool: str) -> dict:
    assert result.is_error is True
    assert result.content[0].text == text, "the 1.7 sentence, byte for byte"
    payload = result.structured_content
    spec = errors.describe(kind)
    assert payload["ok"] is False and payload["kind"] == kind
    assert payload["category"] == spec.category and payload["hint"] == spec.hint
    assert payload["retryable"] is False
    assert payload["tool"] == tool and payload["backend"] == "ezdxf"
    assert payload["message"] == payload["error"] == text
    return payload


_LINE = {"x1": 0, "y1": 0, "x2": 10, "y2": 0}


@pytest.mark.asyncio
async def test_operation_conflict_carries_the_envelope(client):
    from backends.operations import OperationConflictError

    meta = {"operation_id": "conflict-1"}
    await client.call_tool("entity_create_line", _LINE, meta=meta)
    other = await client.call_tool(
        "entity_create_line", {**_LINE, "x2": 20}, raise_on_error=False, meta=meta
    )
    text = str(
        OperationConflictError(
            "conflict-1", reason="different_request", recorded_tool="entity_create_line"
        )
    )
    payload = _assert_journal_envelope(other, "operation_conflict", text, "entity_create_line")
    assert payload["operation_id"] == "conflict-1" and payload["reason"] == "different_request"


@pytest.mark.asyncio
async def test_requires_reconcile_carries_the_envelope_and_a_declared_process_exit(
    client, monkeypatch
):
    from backends.operations import OperationJournal, RequiresReconcileError

    journal = OperationJournal()
    record, _ = journal.begin("entity_create_line", dict(_LINE), operation_id="lost-1")
    journal.finish(record, "unknown_commit", error_kind="process_exit")
    monkeypatch.setattr(server, "_operations_journal", lambda lifespan: journal)
    result = await client.call_tool(
        "entity_create_line", _LINE, raise_on_error=False, meta={"operation_id": "lost-1"}
    )
    text = str(RequiresReconcileError(record))
    payload = _assert_journal_envelope(result, "requires_reconcile", text, "entity_create_line")
    assert payload["operation_id"] == "lost-1"
    assert payload["record"]["error_kind"] == "process_exit"
    assert payload["record"]["error_kind"] in errors.ERROR_KINDS, "every kind on the wire"


@pytest.mark.asyncio
async def test_a_malformed_operation_id_carries_the_envelope(client):
    from backends.operations import validate_operation_id

    with pytest.raises(ValueError) as expected:
        validate_operation_id("op\n1")
    result = await client.call_tool(
        "entity_create_line", _LINE, raise_on_error=False, meta={"operation_id": "op\n1"}
    )
    payload = _assert_journal_envelope(
        result, "invalid_args", str(expected.value), "entity_create_line"
    )
    assert "operation_id" not in payload, "the id was refused; nothing names it"


@pytest.mark.asyncio
async def test_the_wire_text_is_byte_for_byte_what_it_was(client, tmp_path):
    """The prose a 1.7 client read, measured on 1.7.0 before the change."""
    result, _ = await _error(client, "entity_get", {"handle": "FFFFFF"})
    assert result.content[0].text == (
        "Internal error: Error calling tool 'entity_get': Entity with handle 'FFFFFF' not found."
    )
    result, _ = await _error(client, "system_run_command", {"command": "_.ERASE"})
    assert result.content[0].text.startswith("Internal error: Command rejected: ERASE is a")
    result, payload = await _error(client, "drawing_save_as", {"path": str(tmp_path / "x.dwg")})
    assert result.content[0].text == payload["error"] == payload["message"]


# ── the registry scan ───────────────────────────────────────────────────────

#: Tools that decline a missing handle *by value* ({"ok": false, ...}) instead
#: of raising, and why. Anything else must raise a typed not_found.
DECLINE_BY_VALUE = {
    "boundary_from_entities": "refuses fewer than two resolvable handles by value",
    "viewport_set_scale": "a viewport lookup returns {ok: false}",
    "viewport_lock": "a viewport lookup returns {ok: false}",
    "viewport_delete": "a viewport lookup returns {ok: false}",
    "entity_change_space": "a viewport lookup returns {ok: false}",
}


def _fill(schema: dict, key: str):
    if "anyOf" in schema:
        for alternative in schema["anyOf"]:
            if alternative.get("type") != "null":
                return _fill(alternative, key)
        return None
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if "handle" in key:
        return ["FFFFFF", "FFFFFE"] if kind == "array" else "FFFFFF"
    if kind == "string":
        return "X"
    if kind == "number":
        return max(1.0, float(schema.get("minimum", schema.get("exclusiveMinimum", 0)) or 0) + 1)
    if kind == "integer":
        return max(2, int(schema.get("minimum", 0) or 0) + 1)
    if kind == "boolean":
        return False
    if kind == "array":
        item = schema.get("items") or {}
        return [_fill(item, key)] if item else []
    if kind == "object":
        return {}
    return 1


@pytest.mark.asyncio
async def test_every_mutating_handle_tool_answers_not_found_never_internal(full_catalog, client):
    tools = await client.list_tools()
    scanned = []
    for tool in tools:
        properties = tool.inputSchema.get("properties", {})
        handle_keys = [key for key in properties if "handle" in key]
        read_only = bool(tool.annotations and tool.annotations.readOnlyHint)
        if not handle_keys or read_only:
            continue
        args = {key: _fill(properties[key], key) for key in tool.inputSchema.get("required", [])}
        args.update({key: _fill(properties[key], key) for key in handle_keys})
        await client.call_tool("drawing_new", {})
        result = await client.call_tool(tool.name, args, raise_on_error=False)
        payload = result.structured_content if isinstance(result.structured_content, dict) else {}
        scanned.append(tool.name)
        if tool.name in DECLINE_BY_VALUE:
            assert result.is_error or payload.get("ok") is not True or payload.get("errors"), (
                tool.name
            )
            continue
        assert result.is_error is True, f"{tool.name} accepted handle FFFFFF: {payload}"
        # unknown_handle: the 1.8-09 pre-check (entity_delete_many refuses every
        # missing handle before deleting anything).
        assert payload.get("kind") in {"not_found", "invalid_args", "unknown_handle"}, (
            tool.name,
            payload,
        )
        assert payload["kind"] != "internal"
    assert len(scanned) >= 25, scanned


# ── the live engine, on a fake seat ─────────────────────────────────────────


class _FakeComError(Exception):
    """Stands in for pywintypes.com_error: args = (hresult, text, excepinfo, argerr)."""


class _Space:
    Count = 0


class _Doc:
    def __init__(self, seat):
        self._seat = seat
        self.Name = "First.dwg"
        self.FullName = "C:\\work\\First.dwg"
        self.Saved = True
        self.ModelSpace = _Space()
        self.vars = {"LTSCALE": 1.0, "CMDACTIVE": 0}
        self.moved: list[str] = []

    def GetVariable(self, name):
        return self.vars.get(name, 0)

    def SetVariable(self, name, value):
        self.vars[name] = value

    def HandleToObject(self, handle):
        raise _FakeComError(-2145386484, "Invalid input", None, None)

    def Export(self, base, extension, selection):
        ezdxf.new("R2010").saveas(f"{base}.dxf")


class _Docs:
    def __init__(self, seat):
        self._seat = seat
        self.rejecting = False

    @property
    def Count(self):
        if self.rejecting:
            raise _FakeComError(_RPC_E_CALL_REJECTED, "Call was rejected by callee.", None, None)
        return 1

    def Item(self, index):
        return self._seat.ActiveDocument


class _Seat:
    Version = "25.1s (test seat)"

    def __init__(self):
        self.Documents = _Docs(self)
        self.ActiveDocument = _Doc(self)


@pytest.fixture
def seat(monkeypatch):
    fake = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: fake)
    monkeypatch.setattr(cb, "_COM_ERROR", (_FakeComError,))
    return fake


@pytest.fixture
def com(seat):
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend
    if backend._executor is not None:
        backend._executor.shutdown(wait=False)
        backend._executor = None


@pytest.mark.asyncio
async def test_live_handle_to_object_failing_is_not_found(com, seat):
    await com.system_get_variable("LTSCALE")  # binds the document
    with pytest.raises(errors.EntityNotFoundError) as raised:
        await com.entity_move("FFFFFF", 1.0, 0.0)
    assert str(raised.value) == "Entity with handle 'FFFFFF' not found."
    assert isinstance(raised.value, RuntimeError), "every except RuntimeError still holds"
    assert errors.classify(raised.value)["kind"] == "not_found"


@pytest.mark.asyncio
async def test_live_handle_lookup_keeps_a_connection_error_as_it_was(seat):
    doc = seat.ActiveDocument

    def rejected(handle):
        raise _FakeComError(_RPC_E_CALL_REJECTED, "Call was rejected by callee.", None, None)

    doc.HandleToObject = rejected
    with pytest.raises(_FakeComError):
        cb._handle_to_object(doc, "1A")


@pytest.mark.asyncio
async def test_a_busy_seat_before_the_first_touch_is_application_busy(com, seat):
    await com.system_get_variable("LTSCALE")
    revision = com.document_identity()["revision"]
    seat.Documents.rejecting = True
    with pytest.raises(errors.ApplicationBusyError) as raised:
        await com.system_set_variable("LTSCALE", 2.0)
    assert "Nothing was written" in str(raised.value)
    assert com.document_identity()["revision"] == revision, "a busy refusal moves no revision"
    assert errors.classify(raised.value)["kind"] == "application_busy"


def test_a_busy_seat_after_the_job_touched_autocad_is_not_application_busy(monkeypatch):
    """The real ``_acad_app`` marks the job; a rejection after that may follow a write."""
    fake = _Seat()
    monkeypatch.setattr(cb, "_COM_ERROR", (_FakeComError,))
    monkeypatch.setitem(cb._COM_STATE, "app", fake)
    guard = cb._JobGuard(backend=None)
    cb._JOB_STATE.guard = guard
    try:
        assert cb._acad_app() is fake  # the job held the Application proxy
        assert guard.touched is True
        fake.Documents.rejecting = True
        with pytest.raises(_FakeComError):
            cb._acad_doc()
    finally:
        cb._JOB_STATE.guard = None


@pytest.mark.asyncio
async def test_application_busy_reaches_the_client_retryable(com, seat, monkeypatch):
    await com.system_get_variable("LTSCALE")

    async def make():
        return com

    monkeypatch.setattr(server, "_make_backend", make)
    async with Client(server.mcp) as connected:
        seat.Documents.rejecting = True
        result = await connected.call_tool(
            "system_set_variable", {"name": "LTSCALE", "value": 2.0}, raise_on_error=False
        )
        seat.Documents.rejecting = False
        payload = result.structured_content
        assert result.is_error is True
        assert payload["kind"] == "application_busy" and payload["category"] == "dependency"
        assert payload["retryable"] is True and payload["backend"] == "com"
        record = await _record(connected, payload["operation_id"])
        assert (record["state"], record["error_kind"]) == ("refused", "application_busy")


@pytest.mark.asyncio
async def test_a_retryable_refusal_runs_again_under_the_same_operation_id(com, seat, monkeypatch):
    """retryable: true is honoured on the idempotent route: the refusal is not
    replayed, so the retry the envelope invites actually runs."""
    await com.system_get_variable("LTSCALE")

    async def make():
        return com

    monkeypatch.setattr(server, "_make_backend", make)
    meta = {"operation_id": "retry-me-1"}
    args = {"name": "LTSCALE", "value": 2.0}
    async with Client(server.mcp) as connected:
        seat.Documents.rejecting = True
        first = await connected.call_tool(
            "system_set_variable", args, raise_on_error=False, meta=meta
        )
        assert first.structured_content["kind"] == "application_busy"
        assert first.structured_content["retryable"] is True
        seat.Documents.rejecting = False  # the operator pressed ESC
        retry = await connected.call_tool(
            "system_set_variable", args, raise_on_error=False, meta=meta
        )
        assert retry.is_error is False, retry.structured_content
        assert not (retry.structured_content or {}).get("replayed")
        assert seat.ActiveDocument.vars["LTSCALE"] == 2.0
        record = await _record(connected, "retry-me-1")
        assert record["state"] == "committed"
        # A committed answer under that id now replays, as before.
        again = await connected.call_tool(
            "system_set_variable", args, raise_on_error=False, meta=meta
        )
        assert again.structured_content.get("replayed") is True


@pytest.mark.asyncio
async def test_a_non_retryable_refusal_still_replays_under_its_operation_id(client):
    meta = {"operation_id": "not-found-1"}
    args = {"handle": "FFFFFF", "dx": 1.0, "dy": 0.0}
    first = await client.call_tool("entity_move", args, raise_on_error=False, meta=meta)
    assert first.structured_content["kind"] == "not_found"
    assert first.structured_content["retryable"] is False
    again = await client.call_tool("entity_move", args, raise_on_error=False, meta=meta)
    assert again.structured_content["replayed"] is True
    assert again.structured_content["retryable"] is False
