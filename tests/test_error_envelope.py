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
    status = (await client.call_tool("system_status", {})).structured_content
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
    ):
        assert kind in errors.ERROR_KINDS, kind
    assert server.BATCH_ERROR_KINDS == errors.ERROR_KINDS


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
    assert step["kind"] == kind, "the direct route and the batch step must agree"
    assert step["category"] == spec.category and isinstance(step["retryable"], bool)


@pytest.mark.asyncio
async def test_a_refused_lisp_is_policy_refused_direct_and_denied_in_a_batch(client):
    expression = '(command "_.ERASE" "_L" "")'
    _, payload = await _error(client, "system_run_lisp", {"expression": expression})
    assert payload["kind"] == "policy_refused" and payload["category"] == "policy"
    assert payload["channel"] == "lisp" and payload["verbs"] == ["ERASE"]
    assert "command" in payload["symbols"]
    # The escape hatches never run inside a batch: the step is denied before
    # the denylist is even consulted.
    assert (await _step_error(client, "system_run_lisp", {"expression": expression}))[
        "kind"
    ] == "denied"


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
    "entity_delete_many": "reports per-handle errors; all-or-nothing is plan 1.8-09",
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
async def test_every_mutating_handle_tool_answers_not_found_never_internal(client):
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
        assert payload.get("kind") in {"not_found", "invalid_args"}, (tool.name, payload)
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
