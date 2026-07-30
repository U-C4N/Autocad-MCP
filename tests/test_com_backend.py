"""Tests for COM backend logic that runs without a live AutoCAD instance.

Strategy:
  - normalize_lineweight: imported from backends.base — no COM needed.
  - _safe_send_command: ComBackend is importable on non-Windows because the
    win32com block is guarded by ``if _WIN32_AVAILABLE``. The static method
    only calls methods on a *mock* doc object, so it runs fine on Linux/CI.
  - Live ComBackend integration tests: skipped unless AutoCAD is reachable.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from backends.base import normalize_lineweight
from backends.com_backend import ComBackend


@pytest.mark.asyncio
async def test_active_document_switch_clears_com_document_state(monkeypatch):
    backend = ComBackend()
    documents = SimpleNamespace(Count=1)
    app = SimpleNamespace(
        Documents=documents,
        ActiveDocument=SimpleNamespace(Name="First.dwg", FullName="C:/First.dwg"),
    )

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr("backends.com_backend._acad_app", lambda: app)

    await backend._ensure_document_state()
    backend._plan_spec = object()
    backend._preflight_result = object()
    backend._gdt_datums_defined = {"A"}
    app.ActiveDocument = SimpleNamespace(Name="Second.dwg", FullName="C:/Second.dwg")

    await backend._ensure_document_state()

    assert backend._plan_spec is None
    assert backend._preflight_result is None
    assert backend._gdt_datums_defined == set()


# ---------------------------------------------------------------------------
# normalize_lineweight — parametrized table
# ---------------------------------------------------------------------------

NL_TABLE: list[tuple[Any, Any]] = [
    # mm-float inputs (0 < v <= 2.05) → converted to hundredths
    (0.13, 13),
    (0.18, 18),
    (0.25, 25),
    (0.35, 35),
    (0.50, 50),
    (0.70, 70),
    (1.00, 100),
    (1.40, 140),
    (2.00, 200),
    # Already-hundredths ints (v > 2.05)
    (25, 25),
    (50, 50),
    (100, 100),
    (200, 200),
    # Ambiguous bare ints (<= 2.05) — the P0 truncation hazard: treated as mm,
    # NOT as hundredths (NEW-test-com-1). 2 -> 2.00mm -> 200, 1 -> 1.00mm -> 100.
    (1, 100),
    (2, 200),
    # Sentinels: -1 = ByLayer, -2 = ByBlock, -3 = Default
    (-1, -1),
    (-2, -2),
    (-3, -3),
    # Zero passthrough
    (0, 0),
    # None passthrough
    (None, None),
    # String-encoded floats are coerced
    ("0.25", 25),
    ("50", 50),
]


@pytest.mark.parametrize("input_val, expected", NL_TABLE)
def test_normalize_lineweight(input_val, expected):
    assert normalize_lineweight(input_val) == expected


def test_normalize_lineweight_boundary_2_05():
    """2.00 is treated as mm; 2.06 is treated as hundredths."""
    assert normalize_lineweight(2.00) == 200
    assert normalize_lineweight(2.06) == 2  # 2.06 rounds to 2 hundredths


def test_normalize_lineweight_non_numeric_passthrough():
    """Non-numeric exotic values pass through unchanged (don't raise)."""
    result = normalize_lineweight("ByLayer")  # type: ignore[arg-type]
    assert result == "ByLayer"


# ---------------------------------------------------------------------------
# _safe_send_command — CMDACTIVE polling / timeout / var-restore
# ---------------------------------------------------------------------------


def _make_mock_doc(
    cmdactive_sequence: list[int],
    modelspace_handles_pre: set[str] | None = None,
    modelspace_handles_post: set[str] | None = None,
) -> MagicMock:
    """Build a minimal mock AutoCAD document for _safe_send_command tests."""
    doc = MagicMock()

    # Simulate CMDACTIVE returning successive values on each GetVariable("CMDACTIVE") call.
    # Other variables (OSMODE, SNAPMODE, CMDECHO) return 0 by default.
    # The leading 0 answers _safe_send_command's pre-flight check ("is AutoCAD
    # already sitting at a prompt?"); `cmdactive_sequence` describes what the
    # command does after it has been sent.
    getvar_calls: list[int] = [0] + list(cmdactive_sequence)

    def _get_variable(name: str):
        if name == "CMDACTIVE":
            if getvar_calls:
                return getvar_calls.pop(0)
            return 0
        return 0  # default for OSMODE, SNAPMODE, CMDECHO

    doc.GetVariable.side_effect = _get_variable
    doc.SetVariable.return_value = None
    doc.SendCommand.return_value = None

    # Modelspace iteration for handle snapshotting
    pre = modelspace_handles_pre or set()
    post = modelspace_handles_post or set()
    call_count: list[int] = [0]

    def _iter_modelspace():
        call_count[0] += 1
        handles = pre if call_count[0] == 1 else post
        for h in handles:
            entity = MagicMock()
            entity.Handle = h
            yield entity

    doc.ModelSpace.__iter__ = lambda _: _iter_modelspace()
    return doc


def test_safe_send_command_immediate_return():
    """CMDACTIVE=0 immediately → no polling loop, returns empty new-handles list."""
    doc = _make_mock_doc(cmdactive_sequence=[0])
    result = ComBackend._safe_send_command(doc, "ZOOM E\n")
    assert result == []
    doc.SendCommand.assert_called_once()


def test_safe_send_command_waits_for_active_to_clear():
    """CMDACTIVE=1, 1, 0 → polls twice before returning."""
    doc = _make_mock_doc(cmdactive_sequence=[1, 1, 0])
    with patch("time.sleep"):  # don't actually sleep
        result = ComBackend._safe_send_command(doc, "REGEN\n")
    assert result == []


def test_safe_send_command_restores_variables_on_success():
    """OSMODE/SNAPMODE/CMDECHO are saved and restored even when command succeeds."""
    saved_osmode = 4159  # typical user OSMODE

    doc = MagicMock()
    doc.SendCommand.return_value = None

    def _get_variable(name: str):
        if name == "CMDACTIVE":
            return 0
        if name == "OSMODE":
            return saved_osmode
        return 0

    doc.GetVariable.side_effect = _get_variable
    doc.ModelSpace.__iter__ = lambda _: iter([])

    ComBackend._safe_send_command(doc, "ZOOM E\n")

    # SetVariable should have been called at least once with OSMODE restored to saved value
    set_calls = [c for c in doc.SetVariable.call_args_list if c[0][0] == "OSMODE"]
    assert any(c[0][1] == saved_osmode for c in set_calls), (
        f"OSMODE {saved_osmode} not restored; SetVariable calls: {set_calls}"
    )


def test_safe_send_command_restores_variables_on_timeout():
    """Variables are restored even when the command times out (RuntimeError)."""
    saved_osmode = 4159
    doc = MagicMock()
    doc.SendCommand.return_value = None

    cmdactive_reads = {"n": 0}

    def _get_variable(name: str):
        if name == "CMDACTIVE":
            # 0 first: the pre-flight check must see an idle AutoCAD, then the
            # command never clears and the poll loop times out.
            cmdactive_reads["n"] += 1
            return 0 if cmdactive_reads["n"] == 1 else 1
        if name == "OSMODE":
            return saved_osmode
        return 0

    doc.GetVariable.side_effect = _get_variable
    doc.ModelSpace.__iter__ = lambda _: iter([])

    with patch("time.sleep"), patch("time.monotonic", side_effect=[0.0, 0.0, 999.0]):
        with pytest.raises(RuntimeError, match="did not finish"):
            ComBackend._safe_send_command(doc, "BAD_CMD\n", deadline_s=1.0)

    # OSMODE must still be restored
    set_calls = [c for c in doc.SetVariable.call_args_list if c[0][0] == "OSMODE"]
    assert any(c[0][1] == saved_osmode for c in set_calls)


def test_safe_send_command_timeout_sends_esc():
    """On timeout, three ESC sequences are sent to unblock the command prompt."""
    doc = MagicMock()
    doc.SendCommand.return_value = None
    esc_reads = {"n": 0}

    def _cmdactive(name: str):
        if name != "CMDACTIVE":
            return 0
        esc_reads["n"] += 1  # first read is the idle pre-flight check
        return 0 if esc_reads["n"] == 1 else 1

    doc.GetVariable.side_effect = _cmdactive
    doc.ModelSpace.__iter__ = lambda _: iter([])

    with patch("time.sleep"), patch("time.monotonic", side_effect=[0.0, 0.0, 999.0]):
        with pytest.raises(RuntimeError):
            ComBackend._safe_send_command(doc, "BAD\n", deadline_s=1.0)

    # Second SendCommand call should be the ESC sequence
    calls = doc.SendCommand.call_args_list
    assert len(calls) >= 2
    assert "\x1b" in calls[-1][0][0]


def test_safe_send_command_new_handle_detection():
    """Handles created during the command appear in the return value."""
    pre_handles = {"1A", "1B"}
    post_handles = {"1A", "1B", "1C", "1D"}
    doc = _make_mock_doc(
        cmdactive_sequence=[0],
        modelspace_handles_pre=pre_handles,
        modelspace_handles_post=post_handles,
    )
    result = ComBackend._safe_send_command(doc, "LINE\n")
    assert set(result) == {"1C", "1D"}


# ---------------------------------------------------------------------------
# dimension_linear — must use AddDimRotated (GH issue #3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dimension_linear_uses_add_dim_rotated():
    """Regression for GH issue #3: the AutoCAD ActiveX API has no AddDimLinear
    method — calling it raised ``<unknown>.AddDimLinear``. Linear dimensions
    must go through AddDimRotated."""
    backend = ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    backend._run = _run_inline  # bypass the COM executor thread

    mspace = MagicMock()
    with (
        patch("backends.com_backend._msp", return_value=mspace),
        patch("backends.com_backend._apoint", side_effect=lambda *a: tuple(float(v) for v in a)),
        patch("backends.com_backend._regen"),
        patch("backends.com_backend._apply_dim_tolerance"),
        patch("backends.com_backend._entity_info", side_effect=lambda e: e),
    ):
        await backend.dimension_linear(0, 0, 100, 0, 50, 20, rotation=0.0)

    mspace.AddDimRotated.assert_called_once()
    mspace.AddDimLinear.assert_not_called()
    args = mspace.AddDimRotated.call_args[0]
    assert args[0] == (0.0, 0.0)  # ext line 1 origin
    assert args[1] == (100.0, 0.0)  # ext line 2 origin
    assert args[2] == (50.0, 20.0)  # dim line location
    assert args[3] == pytest.approx(0.0)  # rotation in radians


# ---------------------------------------------------------------------------
# Live integration — skipped unless AutoCAD is reachable
# ---------------------------------------------------------------------------

_SKIP_LIVE = pytest.mark.skipif(
    sys.platform != "win32",
    reason="COM integration tests require Windows + AutoCAD",
)


@_SKIP_LIVE
@pytest.mark.asyncio
async def test_com_backend_connects():
    """Smoke test: connect and disconnect without errors."""
    backend = ComBackend()
    await backend.connect()
    assert backend.is_connected
    await backend.disconnect()
    assert not backend.is_connected
