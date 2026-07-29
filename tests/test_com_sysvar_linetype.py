"""Mocked-COM regression tests for system-variable routing and linetype loading.

Two bugs are guarded here:

* ``GetVariable`` / ``SetVariable`` are methods of ``AcadDocument``, not
  ``AcadApplication`` — calling them on the application raises and took down
  ``drawing_new`` bootstrap, ``system_get_variable`` / ``system_set_variable``,
  ``drawing_audit`` and the ``INSUNITS`` lookup in ``drawing_info``.
* linetypes must be loaded through ``Linetypes.Load``; ``SendCommand`` waits for
  AutoCAD to go idle and hangs on a freshly created document.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import backends.com_backend as cb
from backends.com_backend import ComBackend


class _Linetypes:
    def __init__(self, names=("Continuous",)):
        self._names = list(names)
        self.loaded: list[tuple[str, str]] = []

    @property
    def Count(self):
        return len(self._names)

    def Item(self, i):
        return MagicMock(Name=self._names[i])

    def Load(self, name, lin_file):
        self.loaded.append((name, lin_file))
        self._names.append(name)


class _Doc:
    """Document exposing the sysvar API; SendCommand is a tripwire."""

    def __init__(self, variables=None, linetypes=("Continuous",)):
        self._vars = dict(variables or {})
        self.Linetypes = _Linetypes(linetypes)

    def GetVariable(self, name):
        return self._vars.get(name, 0)

    def SetVariable(self, name, val):
        self._vars[name] = val

    def SendCommand(self, cmd):  # pragma: no cover - must never be reached
        raise AssertionError(f"SendCommand must not be used for linetypes: {cmd!r}")


def _backend_no_executor():
    b = ComBackend()

    async def _run(func, *args, **kwargs):  # bypass the ThreadPoolExecutor
        return func(*args, **kwargs)

    b._run = _run
    return b


# ── sysvars live on the document ────────────────────────────────────────────


def test_sysvar_helpers_use_the_document_not_the_application(monkeypatch):
    doc = _Doc({"MEASUREMENT": 1})
    # An application object without GetVariable/SetVariable, as AutoCAD exposes it.
    monkeypatch.setattr(cb, "_acad_app", lambda: MagicMock(spec=["Documents"]))
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    assert cb._get_sysvar("MEASUREMENT") == 1
    cb._set_sysvar("FILEDIA", 0)
    assert doc.GetVariable("FILEDIA") == 0


@pytest.mark.asyncio
async def test_get_variable_tool_reads_through_the_document(monkeypatch):
    monkeypatch.setattr(cb, "_acad_app", lambda: MagicMock(spec=["Documents"]))
    monkeypatch.setattr(cb, "_acad_doc", lambda: _Doc({"LTSCALE": 25}))

    assert await _backend_no_executor().system_get_variable("LTSCALE") == 25


# ── linetypes load through the collection ───────────────────────────────────


def test_ensure_linetype_loaded_uses_collection_load(monkeypatch):
    doc = _Doc({"MEASUREMENT": 1})
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    cb._ensure_linetype_loaded("CENTER")

    assert doc.Linetypes.loaded == [("CENTER", "acadiso.lin")]


def test_ensure_linetype_loaded_picks_imperial_file(monkeypatch):
    doc = _Doc({"MEASUREMENT": 0})
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    cb._ensure_linetype_loaded("HIDDEN")

    assert doc.Linetypes.loaded == [("HIDDEN", "acad.lin")]


def test_ensure_linetype_loaded_skips_already_loaded(monkeypatch):
    doc = _Doc({"MEASUREMENT": 1}, linetypes=("Continuous", "CENTER"))
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    cb._ensure_linetype_loaded("center")  # case-insensitive

    assert doc.Linetypes.loaded == []


@pytest.mark.asyncio
async def test_linetype_load_tool_reports_the_file_used(monkeypatch):
    doc = _Doc({"MEASUREMENT": 1})
    monkeypatch.setattr(cb, "_acad_app", lambda: MagicMock(spec=["Documents"]))
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    out = await _backend_no_executor().linetype_load("PHANTOM")

    assert out == {"ok": True, "name": "PHANTOM", "file": "acadiso.lin"}
    assert doc.Linetypes.loaded == [("PHANTOM", "acadiso.lin")]


@pytest.mark.asyncio
async def test_linetype_load_tool_raises_when_load_is_a_no_op(monkeypatch):
    doc = _Doc({"MEASUREMENT": 1})
    doc.Linetypes.Load = lambda name, lin_file: None  # silently loads nothing
    monkeypatch.setattr(cb, "_acad_app", lambda: MagicMock(spec=["Documents"]))
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    with pytest.raises(RuntimeError, match="BOGUS"):
        await _backend_no_executor().linetype_load("BOGUS")
