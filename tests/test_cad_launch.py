"""#19: ``CAD_LAUNCH`` - whether the live backend may start the CAD application.

Before 1.8.0 ``_acad_app()`` fell back from a failed ``GetActiveObject`` to
``Dispatch`` and set ``Visible = True`` inside the same ``try``: there was no
way to forbid the launch, and when ``Visible`` was refused by an AutoCAD still
starting the error was "Cannot connect..." while the instance ``Dispatch`` had
started kept running, uncached and windowless. These tests pin:

* ``CAD_LAUNCH=never``: ``Dispatch`` is never called; the refusal is
  ``CadNotRunningError`` (kind ``cad_not_running``, dependency, retryable),
  on the first call, on ``system_launch`` and through the error envelope.
* ``CAD_LAUNCH=auto`` (default): "not ready" answers to ``Visible = True`` are
  waited out; past the budget the started instance is quit once and never
  cached.

Nothing here connects to anything: ``win32com`` is a fake (``raising=False``,
as in ``tests/test_cad_progid.py``, so the name exists on a host without
pywin32) and ``_COM_STATE`` is a fresh dict per test.
"""

from __future__ import annotations

import types

import pytest
from fastmcp import Client

import config
import server
from backends import com_backend as cb
from backends.quarantine import DaemonExecutor
from engineering import errors

_RPC_E_CALL_REJECTED = -2147418111
_RPC_E_SERVERCALL_RETRYLATER = -2147417846
_E_FAIL = -2147467259


class _FakeComError(Exception):
    """Stands in for ``pywintypes.com_error``: ``args[0]`` is the HRESULT."""


class _LaunchedApp:
    """What ``Dispatch`` returns: ``Visible`` answers ``answers`` in order
    (an exception instance is raised, anything else accepted), then accepts."""

    def __init__(self, answers=()):
        self._answers = list(answers)
        self.visible_attempts = 0
        self.quit_calls = 0
        self.visible = False
        self.Version = "25.1s (fake)"
        self.Documents = types.SimpleNamespace(Count=0)

    @property
    def Visible(self):  # noqa: N802 - the ActiveX name
        return self.visible

    @Visible.setter
    def Visible(self, value):  # noqa: N802
        self.visible_attempts += 1
        if self._answers:
            answer = self._answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
        self.visible = bool(value)

    def Quit(self):  # noqa: N802
        self.quit_calls += 1


@pytest.fixture
def seat(monkeypatch):
    """A machine with no CAD running: GetActiveObject fails, Dispatch is recorded."""
    state = types.SimpleNamespace(calls=[], launched=_LaunchedApp(), sleeps=[])

    def _get_active_object(progid):
        state.calls.append(("GetActiveObject", progid))
        raise _FakeComError(-2147221021, "Operation unavailable", None, None)

    def _dispatch(progid):
        state.calls.append(("Dispatch", progid))
        return state.launched

    monkeypatch.setattr(
        cb,
        "win32com",
        types.SimpleNamespace(
            client=types.SimpleNamespace(GetActiveObject=_get_active_object, Dispatch=_dispatch)
        ),
        raising=False,
    )
    monkeypatch.setattr(cb, "_COM_STATE", {})
    monkeypatch.setattr(cb, "_COM_ERROR", (_FakeComError,))
    monkeypatch.setattr(cb, "_launch_sleep", state.sleeps.append)
    monkeypatch.delenv("CAD_PROGID", raising=False)
    return state


# -- the setting ------------------------------------------------------------


def test_the_default_is_auto_and_never_is_read_live(monkeypatch):
    monkeypatch.delenv("CAD_LAUNCH", raising=False)
    assert config.settings.cad_launch == "auto"
    monkeypatch.setenv("CAD_LAUNCH", "  Never ")
    assert config.settings.cad_launch == "never"
    with pytest.raises(AttributeError):
        config.settings.cad_launch = "auto"


def test_an_unknown_value_is_refused_by_name_at_startup(monkeypatch):
    """A fallback would be ``auto``: a misspelt ``never`` would launch AutoCAD."""
    monkeypatch.setenv("CAD_LAUNCH", "nevr")
    with pytest.raises(ValueError, match=r"CAD_LAUNCH='nevr'.*auto, never"):
        config.Settings()
    with pytest.raises(ValueError, match="CAD_LAUNCH"):
        config.settings.cad_launch  # noqa: B018 - the read is the refusal


# -- never: attach only -----------------------------------------------------


def test_never_refuses_without_calling_dispatch(monkeypatch, seat):
    monkeypatch.setenv("CAD_LAUNCH", "never")
    with pytest.raises(errors.CadNotRunningError) as raised:
        cb._acad_app()
    assert seat.calls == [("GetActiveObject", config.DEFAULT_CAD_PROGID)], "no Dispatch"
    assert "app" not in cb._COM_STATE
    assert isinstance(raised.value, RuntimeError), "every except RuntimeError still holds"
    assert config.DEFAULT_CAD_PROGID in str(raised.value)
    row = errors.classify(raised.value)
    assert row["kind"] == "cad_not_running"
    spec = errors.describe("cad_not_running")
    assert (spec.category, spec.retryable) == ("dependency", True)
    assert "CAD_LAUNCH=auto" in spec.hint
    assert errors.retryable_for("cad_not_running", "refused") is True


def test_never_still_attaches_to_a_running_application(monkeypatch, seat):
    running = _LaunchedApp()
    monkeypatch.setattr(cb.win32com.client, "GetActiveObject", lambda progid: running)
    monkeypatch.setenv("CAD_LAUNCH", "never")
    assert cb._acad_app() is running
    assert running.visible_attempts == 0, "an attach never touches Visible"


# -- auto: a launch that waits, and one that cleans up ----------------------


def test_auto_waits_out_a_starting_application_then_caches_it(monkeypatch, seat):
    monkeypatch.delenv("CAD_LAUNCH", raising=False)
    rejected = _FakeComError(_RPC_E_CALL_REJECTED, "Call was rejected by callee.", None, None)
    seat.launched = _LaunchedApp([rejected, rejected])
    app = cb._acad_app()
    assert app is seat.launched and cb._COM_STATE["app"] is app
    assert app.visible is True and app.visible_attempts == 3
    assert seat.sleeps == [cb._LAUNCH_RETRY_PAUSE_S] * 2
    assert app.quit_calls == 0


def test_auto_retries_the_attribute_error_pywin32_makes_of_a_refused_lookup(monkeypatch, seat):
    """The issue's own message: ``Property '...Visible' can not be set.``"""
    monkeypatch.delenv("CAD_LAUNCH", raising=False)
    refused = AttributeError("Property 'AutoCAD.Application.Visible' can not be set.")
    later = _FakeComError(_RPC_E_SERVERCALL_RETRYLATER, "retry later", None, None)
    seat.launched = _LaunchedApp([refused, later])
    assert cb._acad_app() is seat.launched
    assert seat.launched.visible_attempts == 3 and seat.launched.quit_calls == 0


def test_auto_quits_the_instance_it_started_when_visible_never_lands(monkeypatch, seat):
    monkeypatch.delenv("CAD_LAUNCH", raising=False)
    now = [1000.0]
    monkeypatch.setattr(cb, "_launch_clock", lambda: now[0])
    monkeypatch.setattr(cb, "_launch_sleep", lambda s: now.__setitem__(0, now[0] + s))
    refused = AttributeError("Property 'AutoCAD.Application.Visible' can not be set.")
    seat.launched = _LaunchedApp([refused] * 10_000)
    with pytest.raises(RuntimeError) as raised:
        cb._acad_app()
    assert not isinstance(raised.value, errors.CadNotRunningError)
    assert seat.launched.quit_calls == 1, "the instance this server started is quit, once"
    assert "app" not in cb._COM_STATE, "a failed launch is never cached"
    message = str(raised.value)
    assert "started it" in message and "quit it" in message
    assert "CAD_LAUNCH=never" in message and config.DEFAULT_CAD_PROGID in message
    budget = cb._launch_visible_budget()
    assert budget <= 30.0
    assert now[0] - 1000.0 <= budget, "the retries stop at the budget"
    assert seat.launched.visible_attempts > 2


def test_auto_quits_at_once_on_an_answer_that_is_not_a_not_ready_one(monkeypatch, seat):
    monkeypatch.delenv("CAD_LAUNCH", raising=False)
    seat.launched = _LaunchedApp([_FakeComError(_E_FAIL, "Unspecified error", None, None)])
    with pytest.raises(RuntimeError, match="started it"):
        cb._acad_app()
    assert seat.launched.visible_attempts == 1 and seat.launched.quit_calls == 1
    assert seat.sleeps == []


def test_a_quit_that_fails_is_reported_not_raised_instead(monkeypatch, seat):
    monkeypatch.delenv("CAD_LAUNCH", raising=False)
    seat.launched = _LaunchedApp([_FakeComError(_E_FAIL, "Unspecified error", None, None)])

    def _quit():
        raise _FakeComError(-2147023174, "The RPC server is unavailable.", None, None)

    seat.launched.Quit = _quit
    with pytest.raises(RuntimeError, match=r"tried to quit it \(Quit failed"):
        cb._acad_app()
    assert "app" not in cb._COM_STATE


def test_the_visible_budget_is_inside_the_job_deadline(monkeypatch):
    monkeypatch.setattr(config.settings, "com_call_timeout", 60.0)
    assert cb._launch_visible_budget() == 30.0
    monkeypatch.setattr(config.settings, "com_call_timeout", 20.0)
    assert cb._launch_visible_budget() == 10.0
    monkeypatch.setattr(config.settings, "com_call_timeout", 0.0)
    assert cb._launch_visible_budget() == 30.0


# -- the backend and the wire -----------------------------------------------


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
async def test_system_launch_never_refuses_instead_of_launching(monkeypatch, com, seat):
    monkeypatch.setenv("CAD_LAUNCH", "never")
    with pytest.raises(errors.CadNotRunningError):
        await com.system_launch()
    assert [call for call, _ in seat.calls] == ["GetActiveObject"]
    assert "app" not in cb._COM_STATE


@pytest.mark.asyncio
async def test_status_and_capabilities_report_the_setting(monkeypatch, com, seat):
    monkeypatch.setenv("CAD_LAUNCH", "never")
    status = await com.system_status()
    assert status["connected"] is False and status["cad_launch"] == "never"
    assert status["error_kind"] == "cad_not_running"
    live = com.capabilities().to_dict()["features"]["live_application"]
    assert live["supported"] is True and live["mode"] == "attach_only"
    assert "never_starts" in live["reason"]
    monkeypatch.delenv("CAD_LAUNCH", raising=False)
    live = com.capabilities().to_dict()["features"]["live_application"]
    assert (live["mode"], live["reason"]) == ("native", None), "auto reports what 1.7 did"


@pytest.mark.asyncio
async def test_never_reaches_the_client_as_cad_not_running(monkeypatch, com, seat):
    monkeypatch.setenv("CAD_LAUNCH", "never")

    async def make():
        return com

    monkeypatch.setattr(server, "_make_backend", make)
    async with Client(server.mcp) as connected:
        result = await connected.call_tool(
            "system_get_variable", {"name": "LTSCALE"}, raise_on_error=False
        )
        payload = result.structured_content
        assert result.is_error is True
        assert payload["kind"] == "cad_not_running" and payload["category"] == "dependency"
        assert payload["retryable"] is True and payload["backend"] == "com"
        assert payload["hint"] == errors.describe("cad_not_running").hint
    assert [call for call, _ in seat.calls] == ["GetActiveObject"], "nothing was launched"
