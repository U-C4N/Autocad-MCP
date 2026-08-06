"""A quarantined document must not be able to pass the quality gate.

`backends/quarantine.py` refuses every call routed through `_async` once a
timed-out write has been abandoned on the document. But `engineering/critique.py`
wraps each check in a bare ``except Exception`` and degrades to ``[]`` — so
`DocumentQuarantineError` was swallowed and `drawing_critique` returned *zero
issues* on a document the backend had declared untrusted.

CLAUDE.md premium rule 7 makes "drawing_critique must return zero issues before
drawing_finalize" the gate an agent is told to trust. A gate that reports clean
because it could not look is worse than no gate: it is the same silent-success
failure the whole quarantine exists to stop, one layer up.

The same swallow sat in `construction_clear` (step 9 of the standard workflow),
which answered ``{"ok": true, "deleted": 0}`` — "there was nothing to clear" —
on a document it could not read.
"""

from __future__ import annotations

import threading

import pytest

import config
from backends.quarantine import DocumentQuarantineError

pytestmark = pytest.mark.asyncio


async def _quarantine(backend, monkeypatch):
    """Abandon a real write, the way a real timeout does.

    Hand-building a QuarantineRecord would test the gate against a fixture
    rather than against the state the backend actually reaches.
    """
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.2)
    entered = threading.Event()
    release = threading.Event()

    def _writer():
        entered.set()
        release.wait(5.0)
        return "too late"

    with pytest.raises(RuntimeError):
        await backend._async(_writer)
    assert entered.is_set()
    assert backend._quarantine is not None
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 10)
    return release


async def test_a_geometric_focus_refuses_rather_than_reporting_clean(backend, monkeypatch):
    from engineering.critique import run_critique

    await backend.entity_create_line(0, 0, 10, 0, layer="GEOMETRY")
    release = await _quarantine(backend, monkeypatch)

    try:
        with pytest.raises(DocumentQuarantineError):
            await run_critique(backend, focus=["duplicate_entities"])
    finally:
        release.set()


async def test_the_full_critique_refuses_too(backend, monkeypatch):
    from engineering.critique import run_critique

    release = await _quarantine(backend, monkeypatch)

    try:
        with pytest.raises(DocumentQuarantineError):
            await run_critique(backend, focus=None)
    finally:
        release.set()


async def test_the_legibility_check_does_not_read_the_document_under_quarantine(
    backend, monkeypatch
):
    """It reads `backend._doc` directly, off the event loop thread, with no
    lock — the exact unsynchronised access the quarantine exists to prevent."""
    from engineering.critique import _check_dimension_legibility

    release = await _quarantine(backend, monkeypatch)

    try:
        with pytest.raises(DocumentQuarantineError):
            _check_dimension_legibility(backend)
    finally:
        release.set()


async def test_construction_clear_does_not_report_nothing_to_clear(backend, monkeypatch):
    await backend.entity_create_line(0, 0, 10, 0, layer="CONSTRUCTION")
    release = await _quarantine(backend, monkeypatch)

    try:
        with pytest.raises(DocumentQuarantineError):
            await backend.construction_clear()
    finally:
        release.set()


async def test_an_ordinary_failure_is_still_degraded_not_raised(backend):
    """The bare excepts exist so one broken check cannot fail the whole pass.

    Only the quarantine escapes them; everything else keeps degrading.
    """
    from engineering.critique import run_critique

    async def _boom(*_a, **_k):
        raise RuntimeError("layer_list is having a bad day")

    backend.layer_list = _boom

    issues = await run_critique(backend, focus=["layer_color"])

    assert issues == []
