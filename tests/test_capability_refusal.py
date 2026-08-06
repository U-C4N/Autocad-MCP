"""T0.7 — a capability refusal has to survive the trip to the client.

The backends know precisely why they cannot do a thing: they raise
``UnsupportedCapabilityError(capability, message)``, where ``capability`` is a
key of ``capabilities().features`` so a client can pre-check the same boundary
through ``system_capabilities``. That type does not reach anybody.
``FastMCP.call_tool`` rewraps any non-``FastMCPError`` as
``ToolError(f"Error calling tool {name!r}: {e}") from e``; the ``from e`` keeps
the cause alive **in process** — which is the only reason ``cad_batch`` can
classify it — but ``__cause__`` does not serialise. Over the wire the client got
an English sentence and nothing else, so "this backend cannot do that" was
indistinguishable from "your arguments were wrong" except by substring matching.

Subclassing ``FastMCPError`` does not fix this; it changes nothing the client can
observe. The one channel that carries a machine-readable payload *and* the error
flag is ``ToolResult(structured_content=..., is_error=True)``, so a single
middleware — the last place the exception type is still knowable — converts the
refusal, and no tool function changes at all.

The other half is ``cad_batch``: a tool may decline *by value* rather than by
raising, and counting that as a succeeded step is the same class of lie as
writing DXF bytes into a ``.dwg`` or reporting a rollback the backend refused.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends.base import UnsupportedCapabilityError
from backends.ezdxf_backend import EzdxfBackend

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


# ── the wire contract ───────────────────────────────────────────────────────


async def test_refusal_reaches_the_client_as_a_machine_readable_payload(client, tmp_path):
    target = tmp_path / "part.dwg"
    result = await client.call_tool("drawing_save_as", {"path": str(target)}, raise_on_error=False)

    assert result.is_error is True
    payload = result.structured_content
    assert isinstance(payload, dict), "a refusal must not arrive as prose only"
    assert payload["ok"] is False
    assert payload["kind"] == "unsupported"
    assert payload["capability"] == "dwg"
    assert payload["tool"] == "drawing_save_as"
    assert payload["backend"] == "ezdxf"

    # The prose channel still works for clients that only read text, and it is
    # the FULL sentence — a refusal that names no way forward is not help.
    assert payload["error"] in result.content[0].text
    assert ".dxf" in payload["error"]
    assert not target.exists(), "a refusal must not leave a mislabelled file behind"


async def test_the_capability_is_a_key_a_client_can_pre_check(client, tmp_path):
    """The whole point of the key: ask before calling, get the same answer."""
    caps = (await client.call_tool("system_capabilities", {})).structured_content or {}
    features = caps["features"]

    result = await client.call_tool(
        "drawing_save_as", {"path": str(tmp_path / "x.dwg")}, raise_on_error=False
    )
    capability = result.structured_content["capability"]
    assert features[capability]["supported"] is False


async def test_ordinary_errors_are_left_exactly_as_they_were(client):
    """The middleware must not turn every failure into a capability refusal."""
    result = await client.call_tool("entity_get", {"handle": "NOSUCH"}, raise_on_error=False)
    assert result.is_error is True
    payload = result.structured_content
    assert not (isinstance(payload, dict) and payload.get("kind") == "unsupported")


async def test_a_successful_call_is_untouched(client):
    result = await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5, "y2": 0})
    assert result.is_error is False
    assert result.structured_content["handle"]


# ── one refusal mechanism, not two ──────────────────────────────────────────


async def test_solid_tools_raise_the_typed_refusal_instead_of_returning_a_dict():
    """Returning ``{"ok": False}`` puts a refusal outside the middleware's reach.

    The payload the client sees is unchanged; what flips is ``is_error``, from a
    quiet False to an honest True.
    """
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()

    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.solid_box(0, 0, 0, 10, 10, 10)

    assert excinfo.value.capability == "solid_3d"
    assert excinfo.value.to_dict() == {
        "ok": False,
        "error": str(excinfo.value),
        "capability": "solid_3d",
    }
    await backend.disconnect()


async def test_the_refusal_type_lives_on_the_shared_base():
    """Both backends must be able to raise the same class, not two look-alikes."""
    from backends.ezdxf_backend import UnsupportedCapabilityError as from_ezdxf

    assert from_ezdxf is UnsupportedCapabilityError


# ── cad_batch: a declined step is not a succeeded step ──────────────────────


async def _batch(client, steps, **kwargs):
    payload = {"steps": steps, **kwargs}
    return (await client.call_tool("cad_batch", payload)).structured_content


async def test_batch_does_not_count_a_step_that_declined_by_value_as_succeeded(client):
    """``drawing_redo`` with nothing to redo returns ``{"ok": False}`` and did nothing."""
    result = await _batch(
        client,
        [
            {"tool": "entity_create_line", "args": {"x1": 0, "y1": 0, "x2": 1, "y2": 0}},
            {"tool": "drawing_redo", "args": {}},
        ],
        on_error="continue",
    )

    assert result["succeeded"] == 1, "the redo did nothing; it cannot be a success"
    assert result["failed"] == 1
    redo_row = result["results"][1]
    assert redo_row["status"] == "error"
    assert redo_row["error"]["kind"] in {"refused", "unsupported"}
