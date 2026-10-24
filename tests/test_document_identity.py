"""B11 - every open document has an id and a content revision.

``document_id`` is a uuid4 hex minted when a document is registered: not
``$FINGERPRINTGUID`` (every template-born drawing shares it) and not
``$VERSIONGUID`` (every internal snapshot save rewrites it). ``revision`` is a
*content* revision: it moves wherever the content changes - every point that
sets ``dirty``, undo, redo, ``transaction_rollback`` - and never on a save, an
export, a finalize or a delivery, so a delivery manifest is not stale the
moment it is written. Every expected revision below is counted by hand from
the calls the test makes (one ``_mark_dirty`` per primitive).
"""

from __future__ import annotations

import pytest
from fastmcp import Client

import config
import server
from backends.ezdxf_backend import EzdxfBackend
from engineering.delivery import deliver_drawing

pytestmark = pytest.mark.asyncio


def _is_uuid4_hex(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and set(value) <= set("0123456789abcdef")
        and value[12] == "4"
    )


async def test_a_registered_document_has_a_uuid4_id_and_revision_zero(backend):
    identity = backend.document_identity()
    assert set(identity) == {"document_id", "revision", "name", "path", "backend"}
    assert _is_uuid4_hex(identity["document_id"])
    assert identity["revision"] == 0
    assert identity["backend"] == "ezdxf"
    assert identity["name"] == "untitled-1" and identity["path"] is None


async def test_each_document_keeps_its_own_identity(backend):
    first = backend.document_identity()
    created = await backend.drawing_new()
    second = backend.document_identity()
    assert _is_uuid4_hex(second["document_id"])
    assert second["document_id"] != first["document_id"]
    assert second["revision"] == 0
    await backend.entity_create_line(0, 0, 1, 0)
    await backend.document_activate(first["name"])
    assert backend.document_identity() == first, "switching back finds the first one untouched"
    await backend.document_activate(created["document"])
    assert backend.document_identity() == {**second, "revision": 1}


async def test_no_document_means_no_identity():
    bare = EzdxfBackend()
    await bare.connect()
    try:
        assert bare.document_identity() == {
            "document_id": None,
            "revision": None,
            "name": None,
            "path": None,
            "backend": "ezdxf",
        }
    finally:
        await bare.disconnect()


async def test_revision_moves_with_content_and_only_with_content(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 5)
    await backend.drawing_new()  # a fresh history baseline at depth 5
    document_id = backend.document_identity()["document_id"]

    def revision() -> int:
        return backend.document_identity()["revision"]

    assert revision() == 0
    await backend.entity_create_line(0, 0, 10, 0)
    assert revision() == 1
    await backend.entity_create_circle(5, 5, 2)
    assert revision() == 2
    assert (await backend.drawing_undo())["ok"] is True
    assert revision() == 3, "an undo restores a snapshot: content changed"
    assert (await backend.drawing_redo())["ok"] is True
    assert revision() == 4
    assert (await backend.transaction_begin())["ok"] is True
    assert revision() == 4, "a checkpoint changes nothing"
    await backend.entity_create_line(0, 10, 10, 10)
    assert revision() == 5
    assert (await backend.transaction_rollback())["ok"] is True
    assert revision() == 6, "a rollback restores a snapshot: content changed"

    await backend.drawing_save_as(str(tmp_path / "a.dxf"))
    await backend.drawing_export_dxf(str(tmp_path / "b.dxf"))
    await backend.drawing_save()
    assert revision() == 6, "saving and exporting never move the content revision"
    delivered = await deliver_drawing(
        backend, tmp_path, formats=["dxf"], min_score=0, strict_critique=False
    )
    assert delivered.status in ("success", "failed_validation")
    assert revision() == 6, "a delivery reads and exports; it never changes the content"
    assert backend.document_identity()["document_id"] == document_id


async def test_reopening_a_file_is_a_new_registration(backend, tmp_path):
    await backend.entity_create_line(0, 0, 1, 0)
    path = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(path))
    before = backend.document_identity()
    assert before["revision"] == 1
    opened = await backend.drawing_open(str(path))
    assert opened["reloaded"] is True
    after = backend.document_identity()
    assert _is_uuid4_hex(after["document_id"]) and after["document_id"] != before["document_id"]
    assert after["revision"] == 0 and after["path"] == str(path)


async def test_list_info_and_status_carry_id_and_revision(backend):
    await backend.entity_create_line(0, 0, 1, 0)
    identity = backend.document_identity()
    (row,) = await backend.document_list()
    assert (row["document_id"], row["revision"]) == (identity["document_id"], 1)
    info = await backend.drawing_info()
    assert (info.document_id, info.revision) == (identity["document_id"], 1)
    status = await backend.system_status()
    assert (status["document_id"], status["revision"]) == (identity["document_id"], 1)


async def test_finalize_leaves_the_revision_where_it_was(tmp_path):
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        await client.call_tool(
            "entity_create_line", {"x1": 0, "y1": 0, "x2": 40, "y2": 0, "layer": "GEOMETRY"}
        )
        before = (await client.call_tool("system_status", {})).structured_content
        await client.call_tool(
            "drawing_finalize", {"save_path": str(tmp_path / "final.dxf")}, raise_on_error=False
        )
        after = (await client.call_tool("system_status", {})).structured_content
        info = (await client.call_tool("drawing_info", {})).structured_content
        rows = (await client.call_tool("document_list", {})).structured_content["documents"]
    assert _is_uuid4_hex(before["document_id"])
    assert after["document_id"] == before["document_id"]
    assert after["revision"] == before["revision"], "finalize saves and checks; it never edits"
    assert (info["document_id"], info["revision"]) == (before["document_id"], before["revision"])
    assert [r["document_id"] for r in rows] == [before["document_id"]]
