"""``CAD_PROGID`` — which COM application the live backend attaches to.

The ProgID was hardcoded twice inside ``_acad_app``, so the only CAD this server
could ever drive was AutoCAD itself. GstarCAD, ZWCAD and BricsCAD all expose the
same ActiveX object model under a different ProgID, and the connection is the
only thing standing between them and the existing 100+ COM methods.

Two things make this more delicate than a string swap, and both are tested here:

* **Nothing may connect.** These tests run on a machine with AutoCAD installed.
  The ``Dispatch`` branch is not a passive probe — it COM-launches the
  application and sets ``Visible = True``. So the fake below records the ProgID
  it was *asked* for and returns a stub; every assertion is on the recorded
  calls, never on a returned object. ``raising=False`` on the ``win32com`` patch
  is load-bearing: that name only exists in the module when the pywin32 imports
  succeeded, so without it these tests would pass on a Windows dev box and error
  on Linux CI.
* **The cache would hide the bug.** ``_acad_app`` short-circuits on
  ``_COM_STATE["app"]``, so a warm cache makes it perform zero lookups and the
  test asserts nothing at all. The fixture rebinds ``_COM_STATE`` to a fresh
  dict rather than mutating it — that also guarantees a test can never leave a
  fake application object in the real process-wide cache.

An unknown ProgID fails loudly rather than falling back to AutoCAD. Unlike
``TOOL_PROFILE`` and ``DISCOVERY_MODE``, which fall back with a warning because
they have closed enums, this is an open string: an unrecognised value is not
"invalid", it is "unverifiable by us", and it has no principled substitute. The
blast radius decides it — refusing costs a readable startup error, while falling
back launches and attaches to a live AutoCAD the user explicitly said they were
not using.
"""

from __future__ import annotations

import types

import pytest

import config
from backends import com_backend as cb

DEFAULT = "AutoCAD.Application"
OTHER = "BricscadApp.AcadApplication"


@pytest.fixture
def com_calls(monkeypatch):
    """Record every ProgID ``_acad_app`` asks for, and connect to nothing."""
    calls: list[tuple[str, str]] = []

    def _get_active_object(progid):
        calls.append(("GetActiveObject", progid))
        raise OSError("no running instance (fake)")

    def _dispatch(progid):
        calls.append(("Dispatch", progid))
        return types.SimpleNamespace(Visible=False)

    monkeypatch.setattr(
        cb,
        "win32com",
        types.SimpleNamespace(
            client=types.SimpleNamespace(
                GetActiveObject=_get_active_object,
                Dispatch=_dispatch,
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(cb, "_COM_STATE", {})
    return calls


# -- the setting ------------------------------------------------------------


def test_the_default_progid_is_autocad(monkeypatch):
    monkeypatch.delenv("CAD_PROGID", raising=False)
    assert config.settings.cad_progid == DEFAULT


def test_the_setting_is_read_live_and_stripped(monkeypatch):
    monkeypatch.setenv("CAD_PROGID", f"  {OTHER}  ")
    assert config.settings.cad_progid == OTHER


def test_case_is_preserved_because_the_value_is_shown_to_humans(monkeypatch):
    """``gstarcad.application`` in an error message reads like a typo we made."""
    monkeypatch.setenv("CAD_PROGID", "GstarCAD.Application")
    assert config.settings.cad_progid == "GstarCAD.Application"


def test_an_empty_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("CAD_PROGID", "   ")
    assert config.settings.cad_progid == DEFAULT


def test_assigning_to_the_setting_fails_loudly(monkeypatch):
    """Same reasoning as ``backend``: a silent no-op assignment reads as control."""
    monkeypatch.setenv("CAD_PROGID", OTHER)
    with pytest.raises(AttributeError):
        config.settings.cad_progid = DEFAULT
    assert config.settings.cad_progid == OTHER


# -- what the connection actually asks for ----------------------------------


def test_unset_attaches_to_autocad_on_both_paths(monkeypatch, com_calls):
    monkeypatch.delenv("CAD_PROGID", raising=False)
    cb._acad_app()
    assert com_calls == [("GetActiveObject", DEFAULT), ("Dispatch", DEFAULT)]


def test_the_configured_progid_reaches_both_call_sites(monkeypatch, com_calls):
    """The Dispatch fallback is the one that launches an application.

    Honouring the setting on ``GetActiveObject`` and then quietly launching
    AutoCAD on the fallback would be worse than ignoring the setting entirely.
    """
    monkeypatch.setenv("CAD_PROGID", OTHER)
    cb._acad_app()
    assert [progid for _, progid in com_calls] == [OTHER, OTHER]
    assert DEFAULT not in [progid for _, progid in com_calls]


def test_a_failed_connection_names_the_progid_it_tried(monkeypatch, com_calls):
    def _boom(progid):
        com_calls.append(("Dispatch", progid))
        raise OSError("class not registered (fake)")

    monkeypatch.setattr(cb.win32com.client, "Dispatch", _boom)
    monkeypatch.setenv("CAD_PROGID", "Nope.NotRegistered")

    with pytest.raises(RuntimeError) as excinfo:
        cb._acad_app()

    message = str(excinfo.value)
    assert "Nope.NotRegistered" in message, "a bare 'cannot connect to AutoCAD' misdirects"
    assert "CAD_PROGID" in message, "the message must say which knob to turn"
    assert DEFAULT not in [progid for _, progid in com_calls], "must never fall back to AutoCAD"


def test_a_failed_connection_does_not_poison_the_cache(monkeypatch, com_calls):
    def _boom(progid):
        raise OSError("class not registered (fake)")

    monkeypatch.setattr(cb.win32com.client, "Dispatch", _boom)
    monkeypatch.setenv("CAD_PROGID", "Nope.NotRegistered")

    with pytest.raises(RuntimeError):
        cb._acad_app()
    assert "app" not in cb._COM_STATE
