"""1.8-04 gate - a saved file remembers which document it is; B09 recovers a damaged one.

Lineage (``backends/lineage.py``): every save and open records
``{lineage_id, document_id, last_saved_sha256, revision}`` for the file's real
path in ``<STATE_DIR>/lineage`` - never in the DXF. Reopening an unchanged file
continues the ``document_id`` and revision, across a killed server too; a file
changed outside the server is ``external_change: true``, one revision more and
an ``external_edit`` journal row; the same lineage open under two paths forks
(``lineage_parent``); a revision issued to unsaved edits is never issued again.

B09: ``drawing_open(path)`` still refuses a damaged DXF (now naming
``recover=True``) and keeps the open document; ``recover=True`` reads it with
``ezdxf.recover`` and reports what came back, starts a new lineage with
``recovered_from`` and will not save over the source without
``overwrite_source=True``. The live engine does the same repair into a
private temp DXF before ``Documents.Open``.
"""

from __future__ import annotations

import os
from pathlib import Path

import ezdxf
import pytest
from fastmcp import Client

import server
from backends import com_backend as cb
from backends.dxf_recover import DrawingParseError, RecoveredSourceError
from backends.ezdxf_backend import EzdxfBackend
from backends.quarantine import DaemonExecutor
from tests.test_bound_document_com import _Seat
from tests.test_journal_durable import StdioServer, _env


async def _fresh() -> EzdxfBackend:
    backend = EzdxfBackend()
    await backend.connect()
    return backend


def _damaged(folder: Path) -> tuple[Path, Path]:
    """A 50-line DXF with its EOF and last ENDSEC cut off, and a text file named .dxf."""
    doc = ezdxf.new("R2010", units=4)
    for index in range(50):
        doc.modelspace().add_line((0, index), (10, index))
    intact = folder / "intact.dxf"
    doc.saveas(intact)
    text = intact.read_text(encoding="utf-8")
    cut = text[: text.rindex("ENDSEC")]  # the OBJECTS section loses its end and EOF
    damaged = folder / "damaged.dxf"
    damaged.write_text(cut, encoding="utf-8")
    not_dxf = folder / "not_a_drawing.dxf"
    not_dxf.write_bytes(b"this is not a drawing\n" * 8)
    return damaged, not_dxf


# ── lineage ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unchanged_reopen_continues_the_document_and_its_revision(backend, tmp_path):
    for y in range(3):
        await backend.entity_create_line(0, y, 10, y)
    path = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(path))
    before = backend.document_identity()
    assert before["revision"] == 3

    reader = await _fresh()
    try:
        opened = await reader.drawing_open(str(path))
        after = reader.document_identity()
    finally:
        await reader.disconnect()
    assert after["document_id"] == before["document_id"]
    assert after["revision"] == 3
    assert opened["external_change"] is False and opened["lineage"]["continued"] is True
    assert opened["document_id"] == before["document_id"] and opened["revision"] == 3


@pytest.mark.asyncio
async def test_nothing_of_the_lineage_is_written_into_the_dxf(backend, tmp_path):
    path = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(path))
    identity = backend.document_identity()
    opened = await backend.drawing_open(str(path))
    data = path.read_bytes()
    assert identity["document_id"].encode() not in data
    assert opened["lineage"]["lineage_id"].encode() not in data


@pytest.mark.asyncio
async def test_an_external_edit_is_reported_and_moves_the_revision_once(backend, tmp_path):
    await backend.entity_create_line(0, 0, 10, 0)
    path = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(path))
    saved = backend.document_identity()
    await backend.document_close()

    foreign = ezdxf.readfile(path)
    foreign.modelspace().add_circle((0, 0), 5)
    foreign.saveas(path)

    opened = await backend.drawing_open(str(path))
    assert opened["external_change"] is True
    assert opened["document_id"] == saved["document_id"]
    assert opened["revision"] == saved["revision"] + 1
    assert opened["previous_sha256"] != opened["sha256"]

    again = await backend.drawing_open(str(path))
    assert again["external_change"] is False, "the change is recorded once"
    assert again["revision"] == saved["revision"] + 1


@pytest.mark.asyncio
async def test_an_external_edit_is_journalled_as_its_own_row(tmp_path):
    path = tmp_path / "part.dxf"
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 10, "y2": 0})
        await client.call_tool("drawing_save_as", {"path": str(path)})
        foreign = ezdxf.readfile(path)
        foreign.modelspace().add_circle((0, 0), 5)
        foreign.saveas(path)
        opened = (await client.call_tool("drawing_open", {"path": str(path)})).structured_content
        status = (await client.call_tool("system_status", {})).structured_content
    assert opened["external_change"] is True
    (row,) = [r for r in status["operations"] if r["tool"] == "external_edit"]
    assert row["state"] == "committed" and row["document_id"] == opened["document_id"]


@pytest.mark.asyncio
async def test_the_same_lineage_open_under_two_paths_forks(backend, tmp_path):
    first, second = tmp_path / "a.dxf", tmp_path / "b.dxf"
    await backend.drawing_save_as(str(first))
    await backend.drawing_save_as(str(second))  # the document now lives at b.dxf
    holder = backend.document_identity()
    opened = await backend.drawing_open(str(first))
    assert opened["document_id"] != holder["document_id"], "two open documents, two ids"
    assert opened["lineage"]["lineage_parent"]
    assert opened["lineage"]["lineage_id"] != opened["lineage"]["lineage_parent"]
    assert opened["lineage"]["continued"] is False and opened["revision"] == 0


@pytest.mark.asyncio
async def test_unsaved_revisions_are_never_issued_again(backend, tmp_path):
    await backend.entity_create_line(0, 0, 10, 0)
    path = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(path))
    identity = backend.document_identity()
    await backend.entity_create_line(0, 5, 10, 5)
    await backend.entity_create_line(0, 9, 10, 9)
    assert backend.document_identity()["revision"] == 3
    opened = await backend.drawing_open(str(path))  # back to the saved bytes
    assert opened["document_id"] == identity["document_id"]
    assert opened["revision"] == 4 and opened["lineage"]["revision_floor"] == 3
    assert len(backend._doc.modelspace()) == 1


def test_a_killed_server_reopens_the_same_document_at_the_same_revision(outside_repo_dir, tmp_path):
    state_dir, drawings = outside_repo_dir, tmp_path
    log = drawings / "server.log"
    path = drawings / "part.dxf"
    first = StdioServer(_env(state_dir), drawings, log)
    try:
        first.call("drawing_new")
        for y in range(3):
            assert (
                first.call("entity_create_line", {"x1": 0, "y1": y, "x2": 10, "y2": y})["is_error"]
                is False
            )
        assert first.call("drawing_save", {"path": str(path)})["is_error"] is False
        before = first.call("system_status")
    finally:
        first.kill()
    assert before["revision"] >= 3  # drawing_new's layer bootstrap moves it too

    second = StdioServer(_env(state_dir), drawings, log)
    try:
        opened = second.call("drawing_open", {"path": str(path)})
        after = second.call("system_status")
    finally:
        second.kill()
    assert opened["is_error"] is False and opened["external_change"] is False
    assert after["document_id"] == before["document_id"]
    assert after["revision"] == before["revision"]


# ── B09: recover ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_damaged_dxf_is_refused_by_name_and_the_open_document_kept(backend, tmp_path):
    damaged, not_dxf = _damaged(tmp_path)
    await backend.entity_create_line(0, 0, 10, 0)
    identity = backend.document_identity()
    for path in (damaged, not_dxf):
        with pytest.raises(DrawingParseError) as refused:
            await backend.drawing_open(str(path))
        assert refused.value.kind == "damaged_drawing"
        assert "DXF" in str(refused.value) and "recover=True" in str(refused.value)
    assert backend.document_identity() == identity
    assert (await backend.drawing_info()).entity_count == 1


@pytest.mark.asyncio
async def test_recover_reads_what_it_can_and_says_what_came_back(backend, tmp_path):
    damaged, not_dxf = _damaged(tmp_path)
    before = backend.document_identity()
    opened = await backend.drawing_open(str(damaged), recover=True)
    recovered = opened["recovered"]
    assert recovered["entities_read"] == len(backend._doc.modelspace()) == 50
    assert set(recovered) >= {"fixes", "errors", "messages", "entities_read"}
    assert len(recovered["messages"]) <= 20
    assert opened["recovered_from"] == str(damaged)
    assert opened["lineage"]["recovered_from"] == str(damaged)
    assert opened["document_id"] != before["document_id"] and opened["revision"] == 0

    with pytest.raises(DrawingParseError, match="could not be recovered either"):
        await backend.drawing_open(str(not_dxf), recover=True)


@pytest.mark.asyncio
async def test_a_recovered_document_will_not_save_over_its_source_unasked(backend, tmp_path):
    damaged, _ = _damaged(tmp_path)
    source = damaged.read_bytes()
    await backend.drawing_open(str(damaged), recover=True)
    with pytest.raises(RecoveredSourceError) as refused:
        await backend.drawing_save()
    assert refused.value.kind == "recovered_source_protected"
    assert "overwrite_source=True" in str(refused.value)
    with pytest.raises(RecoveredSourceError):
        await backend.drawing_save_as(str(damaged))
    with pytest.raises(RecoveredSourceError):
        await backend.document_close(save=True)
    assert damaged.read_bytes() == source

    elsewhere = tmp_path / "repaired.dxf"
    await backend.drawing_save_as(str(elsewhere))
    assert len(ezdxf.readfile(elsewhere).modelspace()) == 50
    await backend.drawing_open(str(damaged), recover=True)
    result = await backend.drawing_save(overwrite_source=True)
    assert result["ok"] is True
    assert len(ezdxf.readfile(damaged).modelspace()) == 50


@pytest.mark.asyncio
async def test_the_tool_refusal_names_recover_and_recover_runs_through_the_server(tmp_path):
    damaged, _ = _damaged(tmp_path)
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        refused = await client.call_tool(
            "drawing_open", {"path": str(damaged)}, raise_on_error=False
        )
        assert refused.is_error and "recover=True" in refused.content[0].text
        opened = await client.call_tool("drawing_open", {"path": str(damaged), "recover": True})
        assert opened.structured_content["recovered"]["entities_read"] == 50
        blocked = await client.call_tool("drawing_save", {}, raise_on_error=False)
        assert blocked.is_error and "overwrite_source" in blocked.content[0].text
        saved = await client.call_tool("drawing_save", {"overwrite_source": True})
        assert saved.structured_content["ok"] is True


# ── the live engine (fake COM) ───────────────────────────────────────────────


@pytest.fixture
def seat(monkeypatch):
    fake = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: fake)
    return fake


@pytest.fixture
def com(seat):
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend
    backend._executor.shutdown(wait=False)
    backend._executor = None


@pytest.mark.asyncio
async def test_live_recover_opens_a_private_repaired_copy(com, seat, tmp_path):
    damaged, _ = _damaged(tmp_path)
    opened = await com.drawing_open(str(damaged), recover=True)
    full_name = Path(opened["path"])
    assert full_name.parent != tmp_path and full_name.suffix == ".dxf"
    assert len(ezdxf.readfile(full_name).modelspace()) == 50
    assert opened["recovered"]["entities_read"] == 50
    assert opened["recovered_from"] == str(damaged)
    assert seat.ActiveDocument.FullName == str(full_name)
    with pytest.raises(RecoveredSourceError):
        await com.drawing_save(str(damaged))
    with pytest.raises(RecoveredSourceError):
        await com.drawing_save_as(str(damaged), "dxf")
    assert seat.ActiveDocument.FullName == str(full_name), "nothing was saved"
    saved = await com.drawing_save(str(damaged), overwrite_source=True)
    assert saved["ok"] is True and seat.ActiveDocument.FullName == str(damaged)


@pytest.mark.asyncio
async def test_live_open_reports_an_external_change_by_fullname_and_sha(com, seat, tmp_path):
    path = tmp_path / "plan.dxf"
    ezdxf.new("R2010").saveas(path)
    first = await com.drawing_open(str(path))
    assert first["external_change"] is False and first["lineage"]["continued"] is False
    lineage_id = first["lineage"]["lineage_id"]
    again = await com.drawing_open(str(path))
    assert again["external_change"] is False and again["lineage"]["lineage_id"] == lineage_id

    foreign = ezdxf.readfile(path)
    foreign.modelspace().add_line((0, 0), (1, 1))
    foreign.saveas(path)
    changed = await com.drawing_open(str(path))
    assert changed["external_change"] is True
    assert changed["lineage"] == {"lineage_id": lineage_id, "continued": True}
    assert os.path.isfile(path)
