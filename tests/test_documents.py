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
import tempfile
import types

import pytest
from fastmcp import Client

import config
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


async def test_active_layer_set_is_per_document(backend):
    """The standard ``drawing_apply_iso_layers`` bootstrapped is per drawing.

    It drives ``_role_layer`` — where ``dimension_auto`` and
    ``construction_xline`` file their output — and the critique that checks
    them. Held on the backend it bled across documents: an iso13567 sheet
    next to a mech one pushed the mech drawing's dimensions onto
    ``M-DIMEN-T-N`` (created on demand), silently, and ``drawing_critique``
    could not see it because the same stale set drove the critique.
    """
    await backend.drawing_apply_iso_layers("iso13567")
    assert backend._role_layer("dim") == "M-DIMEN-T-N"

    second = (await backend.drawing_new())["document"]
    assert backend._active_layer_set is None, "a fresh drawing has no explicit set"
    assert backend._role_layer("dim") == "DIM"
    line = await backend.entity_create_line(0, 0, 50, 0)
    dims = await backend.dimension_auto([line.handle], style="chain")
    assert dims[0].layer == "DIM"
    assert "M-DIMEN-T-N" not in [lyr.name for lyr in await backend.layer_list()]

    # Bidirectional: switching back restores the first document's set …
    await backend.document_activate("untitled-1")
    assert backend._active_layer_set == "iso13567"
    assert backend._role_layer("construction") == "M-CONST-E-N"
    # … and a set applied on B while A is open does not reach A.
    await backend.document_activate(second)
    await backend.drawing_apply_iso_layers("pid")
    assert backend._role_layer("dim") == "DIM"
    await backend.document_activate("untitled-1")
    assert backend._active_layer_set == "iso13567"


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


async def test_save_refuses_a_path_that_another_open_document_holds(backend, tmp_path):
    """One entry per file: SAVEAS onto an open drawing is refused, as AutoCAD does.

    Without the refusal the registry held two entries for one path, the
    resolver's exact-key shortcut picked the stale one on close, and the file
    ended up holding the *other* document's bytes while ``saved`` stayed True.
    """
    import ezdxf

    path = tmp_path / "gear.dxf"
    await backend.drawing_save(str(path))
    key = os.path.abspath(str(path))
    await backend.entity_create_line(0, 0, 1, 1)  # A: dirty, on disk empty
    second = (await backend.drawing_new())["document"]
    await backend.entity_create_circle(0, 0, 5)
    before = path.read_bytes()

    for call in (backend.drawing_save_as, backend.drawing_save):
        with pytest.raises(ValueError, match="gear.dxf.*already open.*document_close"):
            await call(str(path))
        assert path.read_bytes() == before, "a refusal writes nothing"

    rows = {r["key"]: r for r in await backend.document_list()}
    assert set(rows) == {key, second}, "a refusal re-keys nothing"
    assert rows[second]["path"] is None and rows[second]["saved"] is False
    assert rows[key]["saved"] is False
    # The rejected document is untouched and can still be saved elsewhere.
    other = tmp_path / "other.dxf"
    await backend.drawing_save_as(str(other))
    assert [e.dxftype() for e in ezdxf.readfile(str(other)).modelspace()] == ["CIRCLE"]
    # The by-name lookup is unambiguous and closes the entry that owns the file.
    await backend.entity_create_line(2, 2, 3, 3)
    closed = await backend.document_close("gear.dxf", save=True)
    assert closed["closed"] == key and closed["saved"] is True
    assert [e.dxftype() for e in ezdxf.readfile(str(path)).modelspace()] == ["LINE"]


async def test_save_to_the_document_own_path_is_not_a_collision(backend, tmp_path):
    path = tmp_path / "self.dxf"
    await backend.drawing_save(str(path))
    await backend.drawing_new()
    await backend.document_activate("self.dxf")
    await backend.entity_create_circle(0, 0, 1)
    assert (await backend.drawing_save())["ok"] is True
    await backend.entity_create_circle(0, 0, 2)
    assert (await backend.drawing_save(str(path)))["ok"] is True
    await backend.entity_create_circle(0, 0, 3)
    # A spelling that differs from the key still names the same file.
    spelled = str(tmp_path / "sub" / ".." / "self.dxf")
    assert (await backend.drawing_save_as(spelled))["ok"] is True
    assert len(await backend.document_list()) == 2
    assert backend._active_key == os.path.abspath(str(path))


async def test_resolver_refuses_a_shared_path_even_on_an_exact_key_hit(backend, tmp_path):
    """The save refusal keeps the registry one-entry-per-file; if that invariant
    is ever broken anyway, every lookup of the pair refuses instead of guessing."""
    path = tmp_path / "dup.dxf"
    await backend.drawing_save(str(path))
    key = os.path.abspath(str(path))
    second = (await backend.drawing_new())["document"]
    with pytest.raises(RuntimeError, match="already holds"):
        backend._doc_path = str(path)  # the setter enforces the invariant
    backend._docs[second].path = str(path)  # corrupt the registry behind its back
    for wanted in (key, second, "dup.dxf", str(path)):
        with pytest.raises(ValueError, match="share the file"):
            backend._resolve_document_key(wanted)
    with pytest.raises(ValueError, match="share the file"):
        await backend.document_close(key, save=True)
    assert len(await backend.document_list()) == 2, "a refusal closes nothing"


def _filesystem_folds_case() -> bool:
    """Whether the temp filesystem answers to a case variant of a file's name.

    Probed on disk rather than read off ``os.path.normcase``: the platform's
    ``normcase`` is the identity on macOS while the default APFS volume folds
    case, so a ``normcase`` guard skipped every case-variant test exactly
    where the old spelling-based registry identity was wrong.
    """
    with tempfile.TemporaryDirectory() as folder:
        probe = os.path.join(folder, "Probe.dxf")
        with open(probe, "w", encoding="utf-8"):
            pass
        return os.path.exists(os.path.join(folder, "probe.dxf"))


# Two spellings of one file only exist where the filesystem folds case.
_case_folding_fs = pytest.mark.skipif(
    not _filesystem_folds_case(), reason="filesystem is case-sensitive"
)


def _hard_link(target: str, link: str) -> None:
    try:
        os.link(target, link)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"hard links unavailable here: {exc}")


def _junction(target_dir: str, link_dir: str) -> None:
    """A second name for a directory: an NTFS junction, or a symlink elsewhere."""
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(target_dir, link_dir)
            return
        except (ImportError, AttributeError, OSError) as exc:
            pytest.skip(f"NTFS junctions unavailable here: {exc}")
    try:
        os.symlink(target_dir, link_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable here: {exc}")


def _modelspace_types(path) -> list[str]:
    import ezdxf

    return [e.dxftype() for e in ezdxf.readfile(str(path)).modelspace()]


@_case_folding_fs
async def test_opening_a_case_variant_of_an_open_file_reloads_it(backend, tmp_path):
    """Registration keys by file identity, not by spelling.

    ``drawing_open`` of an already-open file spelled with another case used to
    register a *second* entry (key compared as plain strings), and from there
    every lookup of either entry was refused as ambiguous.
    """
    path = tmp_path / "Gear.dxf"
    await backend.drawing_save(str(path))
    await backend.entity_create_circle(0, 0, 5)  # unsaved edit
    reopened = await backend.drawing_open(str(path).lower())
    assert reopened["reloaded"] is True
    assert reopened["open_documents"] == 1
    rows = await backend.document_list()
    assert len(rows) == 1 and rows[0]["entity_count"] == 0, "reload reads the disk"
    assert (await backend.document_activate(str(path)))["ok"] is True


@_case_folding_fs
async def test_resolver_accepts_any_spelling_of_a_registered_path(backend, tmp_path):
    """``document_activate`` / ``document_close`` compare paths as the OS does.

    The live engine lowercases before comparing (``_com_find_document``); a
    lowercase drive letter or a case variant used to be refused headless as
    'no open document named ...' - a dual-engine parity break.
    """
    path = tmp_path / "Gear.dxf"
    await backend.drawing_save(str(path))
    key = os.path.abspath(str(path))
    await backend.drawing_new()
    spelled = str(path)
    for variant in (
        spelled.lower(),
        spelled[0].lower() + spelled[1:],
        spelled[0].upper() + spelled[1:],
        spelled.upper(),
    ):
        await backend.document_activate("untitled-2")
        assert (await backend.document_activate(variant))["active"] == key, variant
    assert len(await backend.document_list()) == 2
    closed = await backend.document_close(spelled.lower())
    assert closed["closed"] == key and closed["open_documents"] == 1


@_case_folding_fs
async def test_saving_to_a_case_variant_of_the_own_path_keeps_one_entry(backend, tmp_path):
    path = tmp_path / "Self.dxf"
    await backend.drawing_save(str(path))
    key = os.path.abspath(str(path))
    await backend.entity_create_circle(0, 0, 1)
    assert (await backend.drawing_save_as(str(path).lower()))["ok"] is True
    rows = await backend.document_list()
    assert len(rows) == 1 and rows[0]["saved"] is True
    assert backend._active_key == key, "the same file keeps its key"
    assert (await backend.document_activate(key))["ok"] is True


async def test_a_hard_link_to_an_open_file_reloads_instead_of_registering_twice(backend, tmp_path):
    """Registry identity is the file (``st_dev``/``st_ino``), not the spelling.

    ``normcase(abspath)`` folded case and separators only, so a hard link -
    which ``Path.resolve()`` cannot fold either - registered a second entry:
    both rows said ``saved: True``, a save from either overwrote the other's
    bytes, and closing the 'clean' one dropped its work as ``discarded_changes:
    False``.
    """
    target = tmp_path / "gear.dxf"
    link = tmp_path / "gear_link.dxf"
    await backend.drawing_save(str(target))
    _hard_link(str(target), str(link))
    assert os.path.samefile(target, link)
    await backend.entity_create_line(0, 0, 1, 1)  # unsaved edit

    reopened = await backend.drawing_open(str(link))
    assert reopened["reloaded"] is True and reopened["open_documents"] == 1
    rows = await backend.document_list()
    assert len(rows) == 1 and rows[0]["entity_count"] == 0, "the reload reads the disk"
    key = rows[0]["key"]

    await backend.entity_create_circle(0, 0, 5)
    await backend.drawing_save()
    assert _modelspace_types(target) == ["CIRCLE"], "one entry, one file, one save"
    assert (await backend.document_activate(str(target)))["active"] == key
    assert (await backend.document_activate(str(link)))["active"] == key


async def test_a_junction_to_an_open_file_reloads_instead_of_registering_twice(backend, tmp_path):
    """Same rule through a directory junction (``mklink /J``), which
    ``os.path.abspath`` leaves untouched - the route in-process callers take."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    target = real_dir / "gear.dxf"
    await backend.drawing_save(str(target))
    _junction(str(real_dir), str(tmp_path / "via_junction"))
    aliased = tmp_path / "via_junction" / "gear.dxf"
    assert os.path.samefile(target, aliased)
    await backend.entity_create_line(0, 0, 1, 1)

    reopened = await backend.drawing_open(str(aliased))
    assert reopened["reloaded"] is True and reopened["open_documents"] == 1
    await backend.entity_create_circle(0, 0, 5)
    await backend.drawing_save()
    assert _modelspace_types(target) == ["CIRCLE"]

    await backend.drawing_new()
    with pytest.raises(ValueError, match="already open as document"):
        await backend.drawing_save_as(str(aliased))
    assert _modelspace_types(target) == ["CIRCLE"], "a refusal writes nothing"
    closed = await backend.document_close(str(aliased))
    assert closed["discarded_changes"] is False and closed["open_documents"] == 1


@_case_folding_fs
async def test_case_variant_identity_does_not_depend_on_normcase(backend, tmp_path, monkeypatch):
    """macOS: ``os.path.normcase`` is the identity, the default APFS volume
    folds case. Reproduced here by giving ``normcase`` its POSIX definition -
    the registry must still see one file, because it asks the disk."""
    monkeypatch.setattr(os.path, "normcase", lambda s: s)
    path = tmp_path / "Gear.dxf"
    await backend.drawing_save(str(path))
    await backend.entity_create_line(0, 0, 1, 1)

    reopened = await backend.drawing_open(str(path).lower())
    assert reopened["reloaded"] is True and reopened["open_documents"] == 1
    await backend.entity_create_circle(0, 0, 5)
    await backend.drawing_save()
    assert _modelspace_types(path) == ["CIRCLE"]
    assert len(await backend.document_list()) == 1


async def test_save_target_that_does_not_exist_yet_is_compared_by_spelling(backend, tmp_path):
    """A path with no file behind it cannot be stat'ed; it is still one file
    with the spelling that another entry holds (an owner whose file was
    removed from disk), and a save onto it is refused."""
    path = tmp_path / "gone.dxf"
    await backend.drawing_save(str(path))
    path.unlink()
    await backend.drawing_new()
    with pytest.raises(ValueError, match="already open as document"):
        await backend.drawing_save_as(str(path))
    assert not path.exists(), "a refusal writes nothing"
    assert (await backend.drawing_save_as(str(tmp_path / "other.dxf")))["ok"] is True


async def test_drawing_close_with_save_refuses_a_shared_path(backend, tmp_path):
    """``drawing_close`` bypasses the resolver, so the close itself must check.

    With two entries on one file, ``drawing_close(save=True)`` wrote the active
    entry's bytes over the file the other entry holds and reported
    ``saved: True`` - the surviving row then claimed the disk held its content.
    """
    path = tmp_path / "dup.dxf"
    await backend.drawing_save(str(path))
    await backend.entity_create_line(0, 0, 1, 1)  # the owner is dirty
    await backend.drawing_new()
    await backend.entity_create_circle(0, 0, 5)
    backend._docs[backend._active_key].path = str(path)  # corrupt the registry behind its back
    before = path.read_bytes()
    with pytest.raises(ValueError, match="share the file"):
        await backend.drawing_close(save=True)
    assert path.read_bytes() == before, "a refusal writes nothing"
    assert len(await backend.document_list()) == 2, "a refusal closes nothing"


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


async def _create_layout(backend):
    assert (await backend.layout_create("A3-Sheet"))["ok"] is True


async def _set_current_layout(backend):
    assert (await backend.layout_set_current("Layout1"))["ok"] is True


async def _create_viewport(backend):
    result = await backend.viewport_create("Layout1", 150, 100, 200, 150, 0, 0, 1.0)
    assert result["ok"] is True


def _layout_names(path):
    import ezdxf

    return set(ezdxf.readfile(str(path)).layouts.names())


def _layout1_has_viewport(path):
    import ezdxf

    return any(
        e.dxftype() == "VIEWPORT" and e.dxf.id != 1
        for e in ezdxf.readfile(str(path)).layouts.get("Layout1")
    )


def _tilemode(path):
    import ezdxf

    return int(ezdxf.readfile(str(path)).header.get("$TILEMODE", 1))


@pytest.mark.parametrize(
    ("mutate", "changed_on_disk"),
    [
        (_create_layout, lambda p: "A3-Sheet" in _layout_names(p)),
        (_set_current_layout, lambda p: _tilemode(p) == 0),
        (_create_viewport, _layout1_has_viewport),
    ],
    ids=["layout_create", "layout_set_current", "viewport_create"],
)
async def test_layout_and_viewport_mutators_mark_the_document_unsaved(
    monkeypatch, tmp_path, mutate, changed_on_disk
):
    """The close refusal is only as good as the dirty flag behind it.

    ``layout_create``, ``layout_set_current`` and ``viewport_create`` change
    the file (a new layout tab, ``$TILEMODE`` plus the active layout, a
    VIEWPORT) but used to leave ``_DocState.dirty`` untouched, so
    ``document_list`` reported ``saved: True``, ``document_close`` closed
    without refusing and reported ``discarded_changes: False`` while the work
    was gone — and the live engine, whose ``Saved`` is AutoCAD's DBMOD,
    refused the same sequence. Marking dirty also gives each an undo step,
    which is why undo is switched on here before the document exists (the
    S0 snapshot is seeded by ``drawing_new`` only when history is enabled).
    """
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 4)
    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_new()
        path = tmp_path / "sheet.dxf"
        await backend.drawing_save(str(path))
        assert (await backend.document_list())[0]["saved"] is True
        assert not changed_on_disk(path)

        await mutate(backend)

        assert (await backend.document_list())[0]["saved"] is False
        with pytest.raises(ValueError, match="unsaved changes"):
            await backend.document_close(None)
        assert len(await backend.document_list()) == 1, "a refusal closes nothing"

        # Every mutator pushes an undo snapshot; these must too.
        assert (await backend.drawing_undo())["undo_depth"] == 0
        assert (await backend.drawing_redo())["ok"] is True

        result = await backend.document_close(None, save=True)
        assert result["saved"] is True and result["discarded_changes"] is False
        assert changed_on_disk(path)
    finally:
        await backend.disconnect()


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


@_case_folding_fs
async def test_document_activate_accepts_a_lowercase_drive_over_the_wire(monkeypatch, tmp_path):
    """The wire canonicalises ``drawing_open`` paths (``validate_path`` resolves
    them to disk casing) but not ``document_activate`` / ``document_close``
    names, so a client spelling the drive letter in lowercase used to be
    refused headless while the live engine accepted it."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    path = tmp_path / "Gear.dxf"
    spelled = str(path)
    lowered_drive = spelled[0].lower() + spelled[1:]
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {"bootstrap": False})
        await client.call_tool("drawing_save", {"path": spelled})
        await client.call_tool("drawing_new", {"bootstrap": False})
        activated = (
            await client.call_tool("document_activate", {"name_or_path": lowered_drive})
        ).structured_content
        assert activated["name"] == "Gear.dxf" and activated["previous"] == "untitled-2"
        closed = (
            await client.call_tool("document_close", {"name_or_path": spelled.lower()})
        ).structured_content
        assert closed["closed"] == os.path.abspath(spelled) and closed["open_documents"] == 1


async def test_hard_link_over_the_wire_is_one_document(monkeypatch, tmp_path):
    """Route (a) of the review finding: ``validate_path().resolve()`` does not
    fold a hard link, so the wire hands the backend a second name for the
    open file. One entry, one save, and the disk holds the last edit."""
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    target = tmp_path / "gear.dxf"
    link = tmp_path / "gear_link.dxf"
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {"bootstrap": False})
        await client.call_tool("drawing_save", {"path": str(target)})
        _hard_link(str(target), str(link))
        await client.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 1, "y2": 1})
        opened = (await client.call_tool("drawing_open", {"path": str(link)})).structured_content
        assert opened["reloaded"] is True and opened["open_documents"] == 1
        await client.call_tool("entity_create_circle", {"cx": 0, "cy": 0, "radius": 5})
        await client.call_tool("drawing_save", {})
        rows = (await client.call_tool("document_list", {})).structured_content
        assert rows["count"] == 1 and rows["documents"][0]["saved"] is True
    assert _modelspace_types(target) == ["CIRCLE"]


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
    backend._active_layer_set = "iso13567"
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
    assert backend._active_layer_set is None, "the layer set is drawing-scoped too"
    assert backend._role_layer("dim") == "DIM"
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


# ── the busy window after a document switch ──────────────────────────────────


class _BusyDocuments(_FakeDocuments):
    """``Documents`` that answers ``RPC_E_CALL_REJECTED`` a fixed number of
    times before ``Count`` works — AutoCAD's own message filter refusing the
    incoming call while it is still switching documents (measured 2026-09-22
    by ``scripts/smoke_settings_com.py --build-dwt`` on AutoCAD 2026, twice:
    ``Documents.Count`` right after ``Documents.Open`` and after ``Close``)."""

    def __init__(self, rejections: int):
        super().__init__()
        self.rejections_left = rejections
        self.rejected = 0

    @property
    def Count(self):
        if self.rejections_left > 0:
            self.rejections_left -= 1
            self.rejected += 1
            pywintypes = pytest.importorskip("pywintypes")
            raise pywintypes.com_error(-2147418111, "Call was rejected by callee.", None, None)
        return super().Count


async def test_com_ensure_document_state_waits_out_a_rejected_call(com_backend, monkeypatch):
    backend, app = com_backend
    busy = _BusyDocuments(rejections=3)
    busy._docs.extend(app.Documents._docs)
    app.Documents = busy
    monkeypatch.setattr(backend, "_REJECTED_RETRY_FIRST_PAUSE_S", 0.001)
    await backend._ensure_document_state()
    assert busy.rejected == 3
    assert backend._document_scope_key == ("First.dwg", "C:/work/First.dwg")


async def test_com_ensure_document_state_gives_up_after_its_budget(com_backend, monkeypatch):
    """A seat that keeps rejecting (an operator mid-command) still surfaces
    the error — the retry is a window, not a wait forever."""
    backend, app = com_backend
    busy = _BusyDocuments(rejections=10_000)
    busy._docs.extend(app.Documents._docs)
    app.Documents = busy
    monkeypatch.setattr(backend, "_REJECTED_RETRY_FIRST_PAUSE_S", 0.001)
    monkeypatch.setattr(backend, "_REJECTED_RETRY_BUDGET_S", 0.02)
    pywintypes = pytest.importorskip("pywintypes")
    with pytest.raises(pywintypes.com_error) as excinfo:
        await backend._ensure_document_state()
    assert excinfo.value.args[0] == -2147418111
    assert 1 < busy.rejected < 100, "bounded by the budget, not by the rejection count"


async def test_com_ensure_document_state_does_not_retry_other_errors(com_backend):
    backend, app = com_backend
    pywintypes = pytest.importorskip("pywintypes")

    class _Broken(_FakeDocuments):
        calls = 0

        @property
        def Count(self):
            _Broken.calls += 1
            raise pywintypes.com_error(-2147417848, "disconnected", None, None)

    app.Documents = _Broken()
    with pytest.raises(pywintypes.com_error):
        await backend._ensure_document_state()
    assert _Broken.calls == 1


async def test_com_document_close_waits_out_the_rejection_after_close(com_backend, monkeypatch):
    """The third measured site: ``Documents.Count`` straight after
    ``doc.Close(False)`` inside ``document_close`` itself, before the
    ``_ensure_document_state`` read is even reached."""
    backend, app = com_backend
    busy = _BusyDocuments(rejections=0)
    busy._docs.extend(app.Documents._docs)
    app.Documents = busy
    monkeypatch.setattr(backend, "_REJECTED_RETRY_FIRST_PAUSE_S", 0.001)
    untitled = busy._docs[2]
    original_close = untitled.Close

    def _close_then_go_busy(save_changes):
        original_close(save_changes)
        busy.rejections_left = 2

    untitled.Close = _close_then_go_busy
    result = await backend.document_close("Drawing1.dwg", discard=True)
    assert busy.rejected == 2
    assert result["open_documents"] == 2 and result["active"] == "Second.dwg"
