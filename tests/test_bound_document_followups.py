"""B11 follow-ups on the live engine (review:task-9 minors 7-11).

The fake seat is the one ``tests/test_bound_document_com.py`` models - only
members measured on AutoCAD 2026 - plus ``Application.HWND``, measured on
AutoCAD 2026 (25.1) on 2026-10-26: an ``int``, the same on two proxies from
two ``GetActiveObject`` calls (``scripts/smoke_document_com.py`` records it).

* 7: a document is one document of one AutoCAD. An unsaved ``Drawing1.dwg``
  in a restarted AutoCAD has the key ``('Drawing1.dwg', '')`` the old one had;
  it must not inherit the old id or revision, nor be written as if bound.
* 8: ``view_screenshot`` is a read used as evidence: guarded like the others.
* 9: a call that never reached AutoCAD moves no revision (as headlessly).
* 10: the QA readers let ``ActiveDocumentChangedError`` through, typed.
* 11: ``_com_export`` hands the seat back to the bound document even when
  ``Close`` raises, and never masks the export's own error.
"""

from __future__ import annotations

import pytest

from backends import com_backend as cb
from backends.document_guard import ActiveDocumentChangedError
from backends.quarantine import DaemonExecutor
from tests.test_bound_document_com import _Doc, _Seat


class _HwndSeat(_Seat):
    def __init__(self, hwnd: int):
        super().__init__()
        self.HWND = hwnd


class _Reattached:
    """A second proxy of the same running AutoCAD (what GetActiveObject returns again)."""

    def __init__(self, seat):
        object.__setattr__(self, "_seat", seat)

    def __getattr__(self, name):
        return getattr(self._seat, name)

    def __setattr__(self, name, value):
        setattr(self._seat, name, value)


@pytest.fixture
def holder(monkeypatch):
    """``_acad_app`` returns ``holder["app"]``, so a test can swap the application."""
    state = {"app": _HwndSeat(1001)}
    monkeypatch.setattr(cb, "_acad_app", lambda: state["app"])
    return state


@pytest.fixture
def com(holder):
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend
    if backend._executor is not None:
        backend._executor.shutdown(wait=False)
        backend._executor = None


def _untitled_seat(hwnd: int) -> _HwndSeat:
    seat = _HwndSeat(hwnd)
    seat.Documents._docs.clear()
    untitled = _Doc(seat, "Drawing1.dwg", "")
    seat.Documents._docs.append(untitled)
    seat.ActiveDocument = untitled
    return seat


# ── 7: a restarted AutoCAD is another application ────────────────────────────


@pytest.mark.asyncio
async def test_a_restarted_autocad_does_not_inherit_the_old_document_id(com, holder):
    old = _untitled_seat(1001)
    holder["app"] = old
    for value in (2.0, 3.0, 4.0):
        await com.system_set_variable("LTSCALE", value)
    before = com.document_identity()
    assert before["name"] == "Drawing1.dwg" and before["revision"] == 3

    new = _untitled_seat(2002)  # AutoCAD restarted: a new empty Drawing1.dwg
    holder["app"] = new
    with pytest.raises(ActiveDocumentChangedError) as refused:
        await com.system_set_variable("LTSCALE", 9.0)
    assert new.ActiveDocument.writes == [], "nothing may land in the new application's document"
    payload = refused.value.to_dict()
    assert payload["application_changed"] is True
    assert payload["active"]["document_id"] is None, "the new document is not a known one"
    assert "no longer" in payload["error"]
    assert com.document_identity() == before, "a refused call moves no revision"

    await com.document_activate("Drawing1.dwg")
    after = com.document_identity()
    assert after["document_id"] != before["document_id"], "a new document gets a new uuid4"
    assert after["revision"] == 0
    await com.system_set_variable("LTSCALE", 5.0)
    assert new.ActiveDocument.writes == [("LTSCALE", 5.0)]
    assert com.document_identity()["revision"] == 1


@pytest.mark.asyncio
async def test_a_reattach_to_the_same_autocad_keeps_the_id_and_revision(com, holder):
    seat = holder["app"]
    await com.system_set_variable("LTSCALE", 2.0)
    before = com.document_identity()
    holder["app"] = _Reattached(seat)  # the RPC-disconnect path dropped the cached proxy
    await com.system_set_variable("LTSCALE", 3.0)
    after = com.document_identity()
    assert after["document_id"] == before["document_id"]
    assert after["revision"] == before["revision"] + 1


@pytest.mark.asyncio
async def test_system_status_reports_a_restart_as_a_changed_document(com, holder):
    holder["app"] = _untitled_seat(1001)
    await com.system_get_variable("LTSCALE")
    holder["app"] = _untitled_seat(2002)
    status = await com.system_status()
    assert status["active_document_changed"] is True


# ── 8: the screenshot is guarded ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_screenshot_after_a_switch_is_refused(com, holder, monkeypatch):
    seat = holder["app"]
    monkeypatch.setattr(cb, "_find_autocad_hwnd", lambda: 1234)
    monkeypatch.setattr(cb, "_capture_window", lambda hwnd: seat.ActiveDocument.Name.encode())
    assert await com.view_screenshot() == b"First.dwg"
    seat.ActiveDocument = seat.second
    with pytest.raises(ActiveDocumentChangedError):
        await com.view_screenshot()


@pytest.mark.asyncio
async def test_a_switch_during_the_capture_is_refused(com, holder, monkeypatch):
    seat = holder["app"]
    await com.system_get_variable("LTSCALE")  # bound to First.dwg

    def capture(hwnd):
        image = seat.ActiveDocument.Name.encode()
        seat.ActiveDocument = seat.second  # the operator clicks a tab mid-capture
        return image

    monkeypatch.setattr(cb, "_find_autocad_hwnd", lambda: 1234)
    monkeypatch.setattr(cb, "_capture_window", capture)
    with pytest.raises(ActiveDocumentChangedError):
        await com.view_screenshot()


@pytest.mark.asyncio
async def test_a_screenshot_with_the_bound_document_closed_is_refused(com, holder, monkeypatch):
    seat = holder["app"]
    await com.system_get_variable("LTSCALE")
    seat.Documents._docs.clear()
    seat.ActiveDocument = None
    monkeypatch.setattr(cb, "_find_autocad_hwnd", lambda: 1234)
    monkeypatch.setattr(cb, "_capture_window", lambda hwnd: b"empty AutoCAD")
    with pytest.raises(ActiveDocumentChangedError) as refused:
        await com.view_screenshot()
    assert refused.value.active is None


@pytest.mark.asyncio
async def test_a_screenshot_with_nothing_open_and_nothing_bound_still_captures(
    com, holder, monkeypatch
):
    seat = holder["app"]
    seat.Documents._docs.clear()
    seat.ActiveDocument = None
    monkeypatch.setattr(cb, "_find_autocad_hwnd", lambda: 1234)
    monkeypatch.setattr(cb, "_capture_window", lambda hwnd: b"empty AutoCAD")
    assert await com.view_screenshot() == b"empty AutoCAD"


# ── 9: a refusal before AutoCAD moves no revision ────────────────────────────


@pytest.mark.asyncio
async def test_an_argument_refusal_moves_no_revision(com, holder):
    await com.system_get_variable("LTSCALE")
    with pytest.raises(ValueError, match="values is empty"):
        await com.dimstyle_create("EMPTY", {})
    assert com.document_identity()["revision"] == 0


@pytest.mark.asyncio
async def test_a_rollback_with_no_transaction_moves_no_revision(com, holder):
    await com.system_get_variable("LTSCALE")
    result = await com.transaction_rollback()
    assert result["ok"] is False
    assert com.document_identity()["revision"] == 0


@pytest.mark.asyncio
async def test_a_write_that_reached_autocad_and_failed_still_moves_the_revision(com, holder):
    seat = holder["app"]
    await com.system_get_variable("LTSCALE")
    seat.first.refuse_writes = True
    with pytest.raises(RuntimeError, match="SetVariable refused"):
        await com.system_set_variable("LTSCALE", 3.0)
    assert com.document_identity()["revision"] == 1


# ── 10: the QA readers let the refusal through ───────────────────────────────


def _changed() -> ActiveDocumentChangedError:
    return ActiveDocumentChangedError(
        {"document_id": "a" * 32, "name": "First.dwg", "path": None},
        {"document_id": None, "name": "Second.dwg", "path": None},
    )


@pytest.mark.asyncio
async def test_qa_snapshot_lets_the_refusal_through(monkeypatch):
    from engineering import qa_scan

    async def refuse(backend):
        raise _changed()

    monkeypatch.setattr(qa_scan, "_read_all_spaces", refuse)
    shared: dict = {}
    with pytest.raises(ActiveDocumentChangedError):
        await qa_scan.qa_snapshot(object(), shared)
    assert qa_scan.QA_SNAPSHOT_KEY not in shared, "a refusal is not cached"


@pytest.mark.asyncio
async def test_a_critique_check_lets_the_refusal_through():
    from engineering.critique import _run_check

    async def check(backend):
        raise _changed()

    with pytest.raises(ActiveDocumentChangedError):
        await _run_check(check, "layers", object(), {})


@pytest.mark.asyncio
async def test_a_validator_step_lets_the_refusal_through():
    from engineering.validator import DrawingValidator

    class _Backend:
        async def drawing_info(self):
            raise _changed()

    with pytest.raises(ActiveDocumentChangedError):
        await DrawingValidator._path_step(_Backend(), [])


@pytest.mark.asyncio
async def test_delivery_lets_the_refusal_through(monkeypatch, tmp_path):
    from backends.ezdxf_backend import EzdxfBackend
    from engineering import delivery

    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()

    async def refuse(_backend):
        raise _changed()

    monkeypatch.setattr(delivery, "source_revision", refuse)
    with pytest.raises(ActiveDocumentChangedError):
        await delivery.deliver_drawing(backend, tmp_path / "out", formats=["dxf"])


# ── 11: the export hands the seat back on every path ─────────────────────────


class _StuckDoc(_Doc):
    """A document whose Close raises and leaves it open (and active)."""

    def Close(self, save_changes):
        raise RuntimeError("Close refused")


@pytest.mark.asyncio
async def test_a_close_that_raises_still_hands_the_seat_back(com, holder, tmp_path):
    from engineering.understand.snapshot import take_snapshot

    seat = holder["app"]
    await com.system_get_variable("LTSCALE")  # bound to First.dwg

    def open_stuck(path, read_only=False):
        doc = _StuckDoc(seat, "part.dwg", str(path))
        seat.Documents._docs.append(doc)
        seat.ActiveDocument = doc
        return doc

    seat.Documents.Open = open_stuck
    part = tmp_path / "part.dwg"
    part.write_bytes(b"stand-in")
    with pytest.raises(RuntimeError, match="Close refused"):
        await take_snapshot(com, str(part))
    assert seat.ActiveDocument is seat.first, "the bound document is active again"
    assert await com.system_get_variable("LTSCALE") == 1.0, "the next call is not refused"


@pytest.mark.asyncio
async def test_a_failing_reactivation_does_not_mask_the_export_error(com, holder, tmp_path):
    from engineering.understand.snapshot import take_snapshot

    seat = holder["app"]
    await com.system_get_variable("LTSCALE")

    def open_broken(path, read_only=False):
        doc = _Doc(seat, "part.dwg", str(path))

        def export(base, extension, selection):
            raise RuntimeError("Export refused")

        doc.Export = export
        seat.Documents._docs.append(doc)
        seat.ActiveDocument = doc
        return doc

    def reactivate(app):
        raise RuntimeError("Activate refused")

    seat.Documents.Open = open_broken
    com._reactivate_bound_sync = reactivate
    part = tmp_path / "part.dwg"
    part.write_bytes(b"stand-in")
    with pytest.raises(RuntimeError, match="Export refused"):
        await take_snapshot(com, str(part))
