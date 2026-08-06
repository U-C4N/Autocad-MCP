"""Honesty guarantees for the headless ezdxf backend.

T0.1 — the DWG lie. ezdxf has no DWG writer, so any save that would put DXF
       bytes behind a ``.dwg`` name must refuse with the machine-readable
       ``{"ok": False, "error": ..., "capability": ...}`` shape instead, and the
       boundary must be discoverable up-front through the capability map.

T0.3 — the audit lie, which ran the *other* way. ``Drawing.audit()`` repairs the
       document in place, so ``auditor.fixes`` is a log of mutations already
       applied. ``drawing_audit`` threw that list away, never marked the document
       dirty (so ``drawing_close(save=True)`` discarded the repairs it had just
       made), and rendered the errors it did report as ``<ErrorEntry object at
       0x...>``. The tool said "0 errors" about a file it had silently altered.

T0.6 — no per-call timeout. Every ezdxf method funnels through ``_async``, which
       serialises on a single ``asyncio.Lock``; one hung sync call used to hold
       that lock forever and deadlock every later tool call on the server.
"""

from __future__ import annotations

import asyncio
import threading
import time
import types

import pytest

import config
from backends.com_backend import ComBackend
from backends.ezdxf_backend import EzdxfBackend

pytestmark = pytest.mark.asyncio


# ── T0.1 — DWG refusal ──────────────────────────────────────────────────────


async def test_save_as_dwg_refuses_with_capability_tagged_shape(backend, tmp_path):
    target = tmp_path / "part.dwg"
    with pytest.raises(RuntimeError) as excinfo:
        await backend.drawing_save_as(str(target), "dwg")

    payload = excinfo.value.to_dict()
    assert payload["ok"] is False
    assert payload["capability"] == "dwg"
    assert excinfo.value.capability == "dwg"

    message = payload["error"]
    assert message == str(excinfo.value)
    lowered = message.lower()
    assert "dwg" in lowered
    assert ".dxf" in lowered  # points at the headless alternative
    assert "com" in lowered  # ...and at the live backend
    assert not target.exists(), "refusal must not leave a mislabelled file behind"


async def test_save_as_refuses_dwg_extension_even_when_fmt_says_dxf(backend, tmp_path):
    """The bytes must match the name: a .dwg path is a DWG request, whatever fmt says."""
    target = tmp_path / "sneaky.dwg"
    with pytest.raises(RuntimeError) as excinfo:
        await backend.drawing_save_as(str(target), "dxf")
    assert excinfo.value.capability == "dwg"
    assert not target.exists()


async def test_drawing_save_refuses_dwg_path(backend, tmp_path):
    """drawing_save(path) took any path and wrote DXF bytes into it."""
    target = tmp_path / "saved.dwg"
    with pytest.raises(RuntimeError) as excinfo:
        await backend.drawing_save(str(target))
    assert excinfo.value.capability == "dwg"
    assert not target.exists()


async def test_dxf_save_still_works(backend, tmp_path):
    import ezdxf

    await backend.entity_create_line(0, 0, 10, 0)

    as_path = tmp_path / "ok_saveas.dxf"
    result = await backend.drawing_save_as(str(as_path), "dxf")
    assert result["ok"] is True
    assert result["format"] == "dxf"
    assert ezdxf.readfile(str(as_path)) is not None

    save_path = tmp_path / "ok_save.dxf"
    assert (await backend.drawing_save(str(save_path)))["ok"] is True
    assert save_path.exists()

    export_path = tmp_path / "ok_export.dxf"
    assert (await backend.drawing_export_dxf(str(export_path)))["ok"] is True
    assert export_path.exists()


async def test_capability_map_reports_the_dwg_boundary(backend):
    features = backend.capabilities().to_dict()["features"]
    assert features["dxf"]["supported"] is True
    assert features["dwg"]["supported"] is False
    assert features["dwg"]["reason"]


async def test_both_backends_declare_the_same_capability_keys():
    """A capability only helps clients if both maps answer the same question."""
    ezdxf_keys = set(EzdxfBackend().capabilities().features)
    com_keys = set(ComBackend().capabilities().features)
    assert ezdxf_keys == com_keys
    assert "dwg" in com_keys
    assert ComBackend().capabilities().features["dwg"].supported is True


# ── T0.6 — per-call timeout / anti-deadlock ─────────────────────────────────


async def test_slow_call_times_out_and_backend_stays_usable(backend, monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.2)
    entered = threading.Event()

    def _hang():
        entered.set()
        time.sleep(2.0)
        return "too late"

    started = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        await backend._async(_hang)
    assert entered.is_set()
    assert time.monotonic() - started < 1.5, "must abandon the call, not wait it out"

    message = str(excinfo.value)
    assert "EZDXF_CALL_TIMEOUT" in message
    assert "0.2" in message

    # The anti-deadlock guarantee: the NEXT call must not queue behind the
    # abandoned worker thread (which is still sleeping).
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 10)
    info = await asyncio.wait_for(backend.drawing_info(), timeout=3)
    assert info.backend == "ezdxf"
    line = await asyncio.wait_for(backend.entity_create_line(0, 0, 1, 1), timeout=3)
    assert line.handle


async def test_waiting_on_a_busy_backend_times_out_without_stealing_the_lock(backend, monkeypatch):
    """A live call owns its lock: queueing past the timeout must not abandon it."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.2)
    live = backend._lock
    await live.acquire()  # stand in for a call that is still running
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="busy"):
            await backend._async(lambda: "never runs")
        assert time.monotonic() - started < 1.0
        assert backend._lock is live, "the in-flight call's lock must not be swapped out"
    finally:
        live.release()

    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 5)
    assert await asyncio.wait_for(backend._async(lambda: "after"), timeout=2) == "after"


async def test_zero_timeout_disables_the_check(backend, monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0)

    def _slow():
        time.sleep(0.3)
        return "finished"

    assert await backend._async(_slow) == "finished"


async def test_async_still_serialises_calls(backend):
    """The timeout rewrite must not drop mutual exclusion on the ezdxf document."""
    trace: list[str] = []

    def _work(tag):
        trace.append(f"enter:{tag}")
        time.sleep(0.05)
        trace.append(f"exit:{tag}")
        return tag

    results = await asyncio.gather(*(backend._async(_work, tag) for tag in "abc"))
    assert sorted(results) == ["a", "b", "c"]
    assert trace == [step for tag in results for step in (f"enter:{tag}", f"exit:{tag}")]


async def test_a_call_raising_TimeoutError_is_not_reported_as_the_deadline(backend, monkeypatch):
    """A TimeoutError *from the call* is a normal failure, not our deadline.

    ``TimeoutError`` is ``asyncio.TimeoutError`` on 3.11+ and an ``OSError``
    subclass, so an ezdxf read off a dead network share raises exactly what
    ``wait_for`` raises. Blaming the deadline for it would tell the user their
    drawing "may be left mid-write" and orphan a lock, for a call that never
    ran long at all.
    """
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 30)
    live = backend._lock

    def _boom():
        raise TimeoutError("the network share went away")

    with pytest.raises(TimeoutError, match="network share"):
        await backend._async(_boom)

    assert backend._lock is live, "a call that merely failed must not abandon its lock"
    assert await asyncio.wait_for(backend._async(lambda: "next"), timeout=2) == "next"


async def test_errors_release_the_lock(backend, monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 5)

    def _boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await backend._async(_boom)
    assert await asyncio.wait_for(backend._async(lambda: "next"), timeout=2) == "next"


# ── T0.3 — audit reports what it repaired, and keeps it ─────────────────────


def _write_damaged_dxf(path) -> None:
    """A DXF referencing a linetype and a text style that were never defined.

    Both are defects ezdxf's ``Auditor`` *repairs* — UNDEFINED_LINETYPE (100)
    and UNDEFINED_TEXT_STYLE (102) — by discarding the dangling attribute. The
    names are chosen to survive as literal bytes in the saved file so a test can
    prove the repair reached disk.
    """
    import ezdxf

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"linetype": "GHOSTLINE"})
    msp.add_text("hello", dxfattribs={"style": "NOSUCHSTYLE"})
    doc.saveas(str(path))


def _write_block_cycle_dxf(path) -> None:
    """Two blocks that insert each other — a defect the Auditor CANNOT repair.

    This is the only way to get entries into ``auditor.errors`` (as opposed to
    ``auditor.fixes``), which is what makes the error-rendering assertion
    possible at all.
    """
    import ezdxf

    doc = ezdxf.new("R2010")
    block_a = doc.blocks.new("BLK_A")
    block_b = doc.blocks.new("BLK_B")
    block_a.add_blockref("BLK_B", (0, 0))
    block_b.add_blockref("BLK_A", (0, 0))
    doc.modelspace().add_blockref("BLK_A", (0, 0))
    doc.saveas(str(path))


async def test_audit_reports_the_repairs_it_applied(backend, tmp_path):
    """``doc.audit()`` mutates; a tool that hides those mutations is lying."""
    target = tmp_path / "damaged.dxf"
    _write_damaged_dxf(target)
    await backend.drawing_open(str(target))

    result = await backend.drawing_audit()

    assert result["repaired"] is True
    assert result["fix_count"] == 2
    assert {fix["code"] for fix in result["fixes"]} == {100, 102}
    for fix in result["fixes"]:
        assert isinstance(fix["message"], str) and fix["message"]
        assert fix["name"]


async def test_audit_marks_the_document_dirty_when_it_repaired(backend, tmp_path):
    """The load-bearing half: a reported repair that close() throws away is worse.

    ``drawing_close`` saves only ``if save and self._dirty``. Reporting the fixes
    without marking the document dirty would promise three repairs and then lose
    every one of them.
    """
    target = tmp_path / "damaged.dxf"
    _write_damaged_dxf(target)
    before = target.read_bytes()
    assert b"GHOSTLINE" in before and b"NOSUCHSTYLE" in before

    await backend.drawing_open(str(target))
    await backend.drawing_audit()

    info = await backend.drawing_info()
    assert info.saved is False, "audit mutated the document; it is no longer 'saved'"

    await backend.drawing_close(save=True)
    after = target.read_bytes()
    assert after != before, "the repair must reach disk, not be discarded on close"
    assert b"GHOSTLINE" not in after
    assert b"NOSUCHSTYLE" not in after


async def test_audit_clean_document_reports_no_repair(backend):
    """The opposite direction of the same lie: never claim a repair not made."""
    result = await backend.drawing_audit()
    assert result["repaired"] is False
    assert result["fix_count"] == 0
    assert result["fixes"] == []
    assert (await backend.drawing_info()).saved is True


async def test_audit_errors_are_readable_messages_not_object_reprs(backend, tmp_path):
    """``ErrorEntry`` has no ``__str__``; ``str(e)`` yielded a memory address."""
    target = tmp_path / "cycle.dxf"
    _write_block_cycle_dxf(target)
    await backend.drawing_open(str(target))

    result = await backend.drawing_audit()

    assert result["error_count"] >= 2
    for err in result["errors"]:
        assert isinstance(err, dict)
        assert err["code"] == 104
        assert "block reference cycle" in err["message"].lower()
        assert "ErrorEntry object at" not in err["message"]


async def test_audit_detail_capability_is_declared_per_backend():
    """A client must be able to tell "nothing was wrong" from "we cannot see"."""
    assert EzdxfBackend().capabilities().features["audit_detail"].supported is True

    com_audit = ComBackend().capabilities().features["audit_detail"]
    assert com_audit.supported is False
    assert com_audit.reason == "audit_result_not_readable_over_com"


async def test_audit_discloses_an_entity_it_deleted(backend):
    """The most destructive repair is the one that names no handle.

    ``Auditor.check_owner_exist`` *deletes* an entity whose owner handle points
    nowhere, and calls ``fixed_error`` without ``dxf_entity=`` — so ``handle`` is
    ``None`` for exactly the fix a user most needs to know about. Before the
    dirty-flag fix the missing ``_mark_dirty()`` accidentally shielded people
    from this; now the deletion reaches disk on the next save, so the report has
    to carry it.
    """
    await backend.entity_create_line(0, 0, 5, 5)
    await backend.entity_create_circle(1, 1, 2)
    doomed = next(iter(backend._doc.modelspace()))
    doomed.dxf.owner = "DEAD"  # the damage a truncated write leaves behind

    result = await backend.drawing_audit()

    assert result["repaired"] is True
    deletions = [fix for fix in result["fixes"] if "Deleted" in fix["message"]]
    assert deletions, f"a deletion must be reported, got {result['fixes']}"
    assert "LINE" in deletions[0]["message"]
    assert deletions[0]["handle"] is None, (
        "ezdxf reports this fix without the entity, so the handle is genuinely "
        "unknown — inventing one would be worse than admitting it"
    )
    assert len(list(backend._doc.modelspace())) == 1, "the entity really is gone"
    assert (await backend.drawing_info()).saved is False


async def test_audit_entry_names_an_unknown_code_honestly():
    """A code outside AuditError must not be dressed up as a name."""
    from backends.ezdxf_backend import _audit_entry

    entry = types.SimpleNamespace(code=999, message="something new", entity=None)
    assert _audit_entry(entry) == {
        "code": 999,
        "name": "UNKNOWN",
        "message": "something new",
        "handle": None,
    }
