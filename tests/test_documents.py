"""Multi-document on both engines.

The headless backend used to hold exactly one ``Drawing``: ``drawing_new``
replaced it and silently discarded whatever was there. It now keeps a
registry keyed by path (or ``untitled-N``); every later tool call targets the
active entry, and ``drawing_close`` is the active-document case of
``document_close``.

Two rules are pinned here. ``document_close`` never drops unsaved work
without being told to (``discard=True``), and it refuses ``save=True`` on a
document that has nowhere to write (an untitled one) instead of guessing a
path — on the live engine ``Close(True)`` on an untitled drawing opens
AutoCAD's Save dialog and blocks the COM thread until the timeout, so the
refusal is what keeps the server answerable. ``drawing_close`` keeps its 1.4
contract (``save=True`` saves when it can; an untitled document is closed and
the result says the changes were discarded) because two shipped tests rely on
it; the strict path is the new tool.
"""

from __future__ import annotations

import os
import types

import pytest
from fastmcp import Client

import server
from backends.ezdxf_backend import EzdxfBackend

pytestmark = pytest.mark.asyncio


# ── headless engine ─────────────────────────────────────────────────────────


async def test_fixture_document_is_registered_and_active(backend):
    rows = await backend.document_list()
    assert rows == [
        {
            "name": "untitled-1",
            "path": None,
            "key": "untitled-1",
            "active": True,
            "saved": True,
            "entity_count": 0,
        }
    ]


async def test_two_documents_hold_separate_entities(backend):
    first = (await backend.document_list())[0]["name"]
    second = (await backend.drawing_new())["document"]
    assert second == "untitled-2"
    await backend.entity_create_circle(0, 0, 5)

    rows = {r["name"]: r for r in await backend.document_list()}
    assert rows[second]["entity_count"] == 1 and rows[second]["active"] is True
    assert rows[first]["entity_count"] == 0 and rows[first]["active"] is False

    switched = await backend.document_activate(first)
    assert switched == {
        "ok": True,
        "active": first,
        "previous": second,
        "name": first,
        "path": None,
    }
    assert (await backend.drawing_info()).entity_count == 0
    await backend.entity_create_line(0, 0, 1, 1)
    await backend.document_activate(second)
    assert (await backend.drawing_info()).entity_count == 1
    rows = {r["name"]: r for r in await backend.document_list()}
    assert rows[first]["entity_count"] == 1 and rows[first]["saved"] is False


async def test_per_document_state_follows_the_active_entry(backend):
    """Layer, current space, dirty flag and undo stack are per document."""
    await backend.layer_create("A-ONLY")
    await backend.layer_set_current("A-ONLY")
    await backend.drawing_new()
    assert backend._current_layer == "0"
    assert "A-ONLY" not in [lyr.name for lyr in await backend.layer_list()]
    await backend.document_activate("untitled-1")
    assert backend._current_layer == "A-ONLY"
    assert backend._dirty is True
    await backend.document_activate("untitled-2")
    assert backend._dirty is False


async def test_opening_a_file_twice_reloads_it_instead_of_registering_twice(backend, tmp_path):
    path = tmp_path / "part.dxf"
    await backend.drawing_save(str(path))
    await backend.entity_create_circle(0, 0, 1)  # unsaved edit
    reopened = await backend.drawing_open(str(path))
    assert reopened["reloaded"] is True
    assert reopened["open_documents"] == 1
    assert (await backend.drawing_info()).entity_count == 0, "reload reads the disk, not memory"


async def test_saving_rekeys_an_untitled_document_to_its_file(backend, tmp_path):
    path = tmp_path / "gear.dxf"
    await backend.drawing_save(str(path))
    rows = await backend.document_list()
    assert rows[0]["key"] == os.path.abspath(str(path))
    assert rows[0]["name"] == "gear.dxf" and rows[0]["path"] == str(path)
    assert backend._active_key == os.path.abspath(str(path))


async def test_activate_by_basename_full_path_or_key(backend, tmp_path):
    path = tmp_path / "gear.dxf"
    await backend.drawing_save(str(path))
    key = os.path.abspath(str(path))
    await backend.drawing_new()
    assert (await backend.document_activate("gear.dxf"))["active"] == key
    await backend.document_activate("untitled-2")
    assert (await backend.document_activate(str(path)))["name"] == "gear.dxf"
    await backend.document_activate("untitled-2")
    assert (await backend.document_activate(key))["previous"] == "untitled-2"
    with pytest.raises(ValueError, match="no open document named 'nope'"):
        await backend.document_activate("nope")
    with pytest.raises(TypeError, match="name_or_path"):
        await backend.document_activate(None)


async def test_close_unsaved_refuses_unless_discard(backend):
    await backend.entity_create_line(0, 0, 1, 1)
    with pytest.raises(ValueError, match="untitled-1.*unsaved changes"):
        await backend.document_close("untitled-1")
    assert len(await backend.document_list()) == 1, "a refusal closes nothing"
    with pytest.raises(ValueError, match="never been saved"):
        await backend.document_close("untitled-1", save=True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        await backend.document_close("untitled-1", save=True, discard=True)

    result = await backend.document_close("untitled-1", discard=True)
    assert result == {
        "ok": True,
        "closed": "untitled-1",
        "saved": False,
        "discarded_changes": True,
        "active": None,
        "open_documents": 0,
    }


async def test_close_with_save_writes_the_file(backend, tmp_path):
    path = tmp_path / "saved.dxf"
    await backend.drawing_save(str(path))
    await backend.entity_create_circle(0, 0, 2)
    before = path.read_bytes()
    result = await backend.document_close(None, save=True)
    assert result["saved"] is True and result["discarded_changes"] is False
    assert path.read_bytes() != before


async def test_closing_the_last_document_leaves_the_no_document_state(backend):
    await backend.document_close("untitled-1")
    assert await backend.document_list() == []
    status = await backend.system_status()
    assert status["has_document"] is False
    assert status["open_documents"] == 0 and status["active_document"] is None
    with pytest.raises(RuntimeError, match="No document open"):
        await backend.drawing_info()
    # ...and the backend recovers exactly as before.
    assert (await backend.drawing_new())["document"] == "untitled-2"


async def test_closing_the_active_document_activates_the_most_recently_used(backend):
    await backend.drawing_new()  # untitled-2
    await backend.drawing_new()  # untitled-3
    await backend.document_activate("untitled-1")
    await backend.document_activate("untitled-3")
    result = await backend.document_close("untitled-3")
    assert result["active"] == "untitled-1", "most recently activated, not most recently created"


async def test_closing_a_background_document_keeps_the_active_one(backend):
    await backend.drawing_new()  # untitled-2, active
    await backend.entity_create_circle(0, 0, 1)
    result = await backend.document_close("untitled-1")
    assert result["active"] == "untitled-2"
    assert (await backend.drawing_info()).entity_count == 1


async def test_drawing_close_keeps_its_legacy_contract(backend, tmp_path):
    await backend.entity_create_line(0, 0, 1, 1)
    closed = await backend.drawing_close(save=True)
    assert closed["ok"] is True and closed["saved"] is False
    assert closed["discarded_changes"] is True
    assert "never been saved" in closed["warning"]
    assert await backend.document_list() == []

    await backend.drawing_new()
    path = tmp_path / "keep.dxf"
    await backend.drawing_save(str(path))
    await backend.entity_create_circle(0, 0, 3)
    before = path.read_bytes()
    closed = await backend.drawing_close(save=True)
    assert closed["saved"] is True and "warning" not in closed
    assert path.read_bytes() != before


async def test_disconnect_drops_every_document_and_its_snapshots(monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 3)
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    await backend.entity_create_line(0, 0, 1, 1)
    await backend.drawing_new()
    await backend.entity_create_line(0, 0, 2, 2)
    snapshots = [p for state in backend._docs.values() for p in state.undo_stack]
    assert len(snapshots) == 4 and all(p.exists() for p in snapshots)
    await backend.disconnect()
    assert backend._docs == {} and backend._active_key is None
    assert not any(p.exists() for p in snapshots)


async def test_document_list_tool_over_the_wire(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {"bootstrap": False})
        await client.call_tool("drawing_new", {"bootstrap": False})
        payload = (await client.call_tool("document_list", {})).structured_content
        assert payload["count"] == 2 and payload["active"] == "untitled-2"
        closed = (
            await client.call_tool("document_close", {"name_or_path": "untitled-1"})
        ).structured_content
        assert closed["closed"] == "untitled-1" and closed["active"] == "untitled-2"


# ── live engine, against a fake ActiveX surface ──────────────────────────────


class _FakeDoc:
    def __init__(self, app, name, full_name, *, saved=True, count=0):
        self._app = app
        self.Name = name
        self.FullName = full_name
        self.Saved = saved
        self.ModelSpace = types.SimpleNamespace(Count=count)
        self.calls: list[tuple[str, tuple]] = []

    def Activate(self):
        self.calls.append(("Activate", ()))
        self._app.ActiveDocument = self

    def Close(self, save_changes):
        self.calls.append(("Close", (save_changes,)))
        self._app.Documents._docs.remove(self)
        docs = self._app.Documents._docs
        self._app.ActiveDocument = docs[-1] if docs else None


class _FakeDocuments:
    def __init__(self):
        self._docs: list[_FakeDoc] = []

    @property
    def Count(self):
        return len(self._docs)

    def Item(self, index):
        return self._docs[index]


@pytest.fixture
def com_backend(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    app = types.SimpleNamespace(Documents=_FakeDocuments(), ActiveDocument=None)
    first = _FakeDoc(app, "First.dwg", "C:/work/First.dwg", saved=True, count=3)
    second = _FakeDoc(app, "Second.dwg", "C:/work/Second.dwg", saved=False, count=1)
    untitled = _FakeDoc(app, "Drawing1.dwg", "", saved=False, count=0)
    app.Documents._docs.extend([first, second, untitled])
    app.ActiveDocument = first
    monkeypatch.setattr(module, "_acad_app", lambda: app)
    monkeypatch.setattr(module, "_acad_doc", lambda: app.ActiveDocument)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, app


async def test_com_document_list_reads_the_documents_collection(com_backend):
    backend, app = com_backend
    rows = await backend.document_list()
    assert rows == [
        {
            "name": "First.dwg",
            "path": "C:/work/First.dwg",
            "active": True,
            "saved": True,
            "entity_count": 3,
        },
        {
            "name": "Second.dwg",
            "path": "C:/work/Second.dwg",
            "active": False,
            "saved": False,
            "entity_count": 1,
        },
        {"name": "Drawing1.dwg", "path": None, "active": False, "saved": False, "entity_count": 0},
    ]


async def test_com_activate_calls_activate_and_resets_document_scope(com_backend):
    backend, app = com_backend
    await backend._ensure_document_state()
    backend._plan_spec = object()
    result = await backend.document_activate("Second.dwg")
    assert result == {
        "ok": True,
        "active": "Second.dwg",
        "previous": "First.dwg",
        "name": "Second.dwg",
        "path": "C:/work/Second.dwg",
    }
    assert app.Documents._docs[1].calls == [("Activate", ())]
    assert backend._plan_spec is None, "a document switch clears drawing-scoped state"
    with pytest.raises(ValueError, match="no open document named 'Third.dwg'"):
        await backend.document_activate("Third.dwg")


async def test_com_close_refuses_before_touching_activex(com_backend):
    backend, app = com_backend
    second = app.Documents._docs[1]
    with pytest.raises(ValueError, match="Second.dwg.*unsaved changes"):
        await backend.document_close("Second.dwg")
    untitled = app.Documents._docs[2]
    with pytest.raises(ValueError, match="never been saved"):
        await backend.document_close("Drawing1.dwg", save=True)
    assert second.calls == [] and untitled.calls == []


async def test_com_close_passes_savechanges_through(com_backend):
    backend, app = com_backend
    first, second, untitled = app.Documents._docs
    saved = await backend.document_close("Second.dwg", save=True)
    assert second.calls == [("Close", (True,))]
    assert saved["saved"] is True and saved["open_documents"] == 2
    dropped = await backend.document_close("Drawing1.dwg", discard=True)
    assert untitled.calls == [("Close", (False,))]
    assert dropped["discarded_changes"] is True and dropped["open_documents"] == 1
    clean = await backend.document_close(None)  # the active, clean First.dwg
    assert first.calls == [("Close", (False,))], "nothing to save, so nothing is asked to be saved"
    assert clean["active"] is None


async def test_com_drawing_close_refuses_the_untitled_save_dialog(com_backend):
    backend, app = com_backend
    app.ActiveDocument = app.Documents._docs[2]  # the untitled one
    with pytest.raises(ValueError, match="Save dialog"):
        await backend.drawing_close(save=True)
    assert app.Documents._docs[2].calls == []
