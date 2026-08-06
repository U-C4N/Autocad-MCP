"""M6 — deleting, renaming and copying sheet tabs.

`layout_list/create/set_current` shipped in v1.4; the rest of the tab lifecycle
did not, so a sheet could be made and never unmade. What makes this more than
three thin wrappers is that ezdxf's own guards stop well short of where a tool
boundary has to stop, and the gaps are all silent:

* `doc.layouts.delete("")` does not refuse — it deletes the *first* paper-space
  layout and reports nothing unusual, and `doc.layouts.get("")` likewise
  resolves to the active paper space. A caller who passed an empty string by
  accident loses a sheet.
* `doc.layouts.rename()` skips the `is_valid_table_name` check that `new()`
  performs, so `Bad/Name` and even `""` are accepted and the file still audits
  clean.
* Deleting or renaming the *current* layout leaves `self._current_space`
  pointing at a name that no longer exists. `_msp()` then silently falls back to
  model space while `layout_list()` still reports the dead name — so
  `entity_create_*` lands somewhere the caller was never told about.
* The entities on a deleted layout are destroyed but stay in `entitydb`, where
  `_get_entity`'s `is None` check does not see them. Fetching a handle from a
  deleted sheet used to raise `AttributeError: 'Line' object has no attribute
  'dxf'` instead of a refusal.

ezdxf has no layout-copy API at all (`xref.load_paperspace` is cross-document
only), so `layout_copy` is a construction — and everything a construction can
get wrong here is invisible to the Auditor, which reports zero errors on a
duplicated tab order, a duplicated main viewport and a hatch whose boundary
still points into the layout it was copied from.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

SHEET = "A3-Sheet"
OTHER = "A4-Sheet"


def _names(backend) -> list[str]:
    return list(backend._doc.layouts.names_in_taborder())


def _entities_in(backend, layout_name: str) -> list[str]:
    space = backend._doc.layouts.get(layout_name)
    return sorted(e.dxftype() for e in space)


# ── layout_delete ───────────────────────────────────────────────────────────


async def test_deleting_a_layout_removes_the_tab_and_its_geometry(backend):
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    await backend.entity_create_line(0, 0, 100, 0)
    await backend.layout_set_current("Model")

    result = await backend.layout_delete(SHEET)

    assert result["ok"] is True
    assert result["deleted"] == SHEET
    assert result["entities_destroyed"] == 1
    assert SHEET not in _names(backend)


async def test_deleting_model_space_is_refused(backend):
    result = await backend.layout_delete("Model")
    assert result["ok"] is False
    assert "Model" in result["error"]
    assert "Model" in _names(backend)


async def test_a_blank_name_does_not_delete_the_first_sheet_it_finds(backend):
    """ezdxf's own delete("") destroys Layout1 and says nothing."""
    before = _names(backend)
    for blank in ("", "   "):
        result = await backend.layout_delete(blank)
        assert result["ok"] is False, f"delete({blank!r}) must refuse"
    assert _names(backend) == before


async def test_deleting_a_missing_layout_refuses_with_a_readable_error(backend):
    """ezdxf raises a bare KeyError here; the tool must not leak that."""
    result = await backend.layout_delete("NOSUCH")
    assert result["ok"] is False
    assert "NOSUCH" in result["error"]


async def test_deleting_the_last_paper_space_layout_refuses(backend):
    """A DXF document must keep at least one paper-space layout."""
    for name in _names(backend):
        if name != "Model" and name != "Layout1":
            await backend.layout_delete(name)
    remaining = [n for n in _names(backend) if n != "Model"]
    assert len(remaining) == 1, "fixture assumption: exactly one sheet left"

    result = await backend.layout_delete(remaining[0])
    assert result["ok"] is False
    assert remaining[0] in _names(backend)


async def test_deleting_the_current_layout_leaves_every_report_agreeing(backend):
    """The three-way disagreement this test exists to prevent.

    Before the fix: ``layout_list`` said the deleted sheet, ``$TILEMODE`` said
    paper space, and ``_msp()`` quietly wrote to model space. Whatever the tool
    decides the new current space is, all three must say the same thing and the
    next entity must actually land there.
    """
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)

    result = await backend.layout_delete(SHEET)
    assert result["ok"] is True

    current = (await backend.layout_list())["current"]
    assert current == result["current"]
    assert current in _names(backend)

    line = await backend.entity_create_line(0, 0, 10, 0)
    landed = backend._doc.modelspace() if current == "Model" else backend._doc.layouts.get(current)
    assert line.handle in {e.dxf.handle for e in landed}


async def test_a_handle_from_a_deleted_layout_refuses_instead_of_crashing(backend):
    """The destroyed entity stays in entitydb; `is None` does not catch it."""
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    line = await backend.entity_create_line(0, 0, 100, 0)
    await backend.layout_set_current("Model")
    await backend.layout_delete(SHEET)

    with pytest.raises(RuntimeError, match=line.handle):
        await backend.entity_get(line.handle)


# ── layout_rename ───────────────────────────────────────────────────────────


async def test_renaming_keeps_the_entities_and_their_handles(backend):
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    line = await backend.entity_create_line(0, 0, 100, 0)
    await backend.layout_set_current("Model")

    result = await backend.layout_rename(SHEET, OTHER)

    assert result["ok"] is True
    assert OTHER in _names(backend) and SHEET not in _names(backend)
    info = await backend.entity_get(line.handle)
    assert info.handle == line.handle


async def test_renaming_the_current_layout_keeps_the_caller_on_it(backend):
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)

    await backend.layout_rename(SHEET, OTHER)

    assert (await backend.layout_list())["current"] == OTHER
    line = await backend.entity_create_line(0, 0, 10, 0)
    assert line.handle in {e.dxf.handle for e in backend._doc.layouts.get(OTHER)}


@pytest.mark.parametrize("bad", ["", "   ", "Bad/Name", "Bad*Name", "Bad?Name"])
async def test_an_invalid_new_name_is_refused(backend, bad):
    """`rename()` accepts all of these and the file still audits clean."""
    await backend.layout_create(SHEET)
    result = await backend.layout_rename(SHEET, bad)
    assert result["ok"] is False
    assert SHEET in _names(backend)


async def test_renaming_onto_an_existing_name_is_refused(backend):
    await backend.layout_create(SHEET)
    await backend.layout_create(OTHER)
    result = await backend.layout_rename(SHEET, OTHER)
    assert result["ok"] is False
    assert SHEET in _names(backend) and OTHER in _names(backend)


@pytest.mark.parametrize("args", [("Model", OTHER), (SHEET, "Model")])
async def test_model_space_cannot_be_renamed_or_shadowed(backend, args):
    await backend.layout_create(SHEET)
    result = await backend.layout_rename(*args)
    assert result["ok"] is False
    assert "Model" in _names(backend)


async def test_renaming_a_missing_layout_refuses(backend):
    result = await backend.layout_rename("NOSUCH", OTHER)
    assert result["ok"] is False


# ── names are case-insensitive, like the engine underneath ──────────────────
#
# ezdxf resolves layout names case-insensitively in `get`, `new`, `rename`,
# `delete` and `set_active_layout` — but `names()` returns the *stored* case, so
# every guard written as `name in doc.layouts.names()` disagrees with the engine
# it is guarding. Both directions of that disagreement are bugs: a duplicate
# slips past the check and dies on a raw `DXFValueError` from inside ezdxf, and
# an existing layout typed in another case is reported as missing.


async def test_creating_a_layout_that_differs_only_by_case_refuses_cleanly(backend):
    await backend.layout_create(SHEET)
    result = await backend.layout_create(SHEET.upper())
    assert result["ok"] is False
    assert _names(backend).count(SHEET) == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda b, name: b.layout_delete(name),
        lambda b, name: b.layout_set_current(name),
        lambda b, name: b.viewport_list(name),
        lambda b, name: b.layout_copy(name, "Copy-Of-It"),
    ],
    ids=["delete", "set_current", "viewport_list", "copy_source"],
)
async def test_an_existing_layout_is_found_whatever_case_it_is_typed_in(backend, call):
    await backend.layout_create(SHEET)
    await backend.layout_create("Spare")  # so delete is not refused as the last sheet

    result = await call(backend, SHEET.upper())

    assert result["ok"] is True, f"{SHEET.upper()} must resolve to {SHEET}"


@pytest.mark.parametrize(
    "call",
    [
        lambda b, name: b.layout_rename("Spare", name),
        lambda b, name: b.layout_copy("Spare", name),
    ],
    ids=["rename_target", "copy_target"],
)
async def test_a_target_name_colliding_only_by_case_refuses_cleanly(backend, call):
    await backend.layout_create(SHEET)
    await backend.layout_create("Spare")

    result = await call(backend, SHEET.upper())

    assert result["ok"] is False
    assert "Spare" in _names(backend)


async def test_a_layout_can_be_renamed_to_a_different_spelling_of_itself(backend):
    """Re-casing a name is a rename onto itself, not a collision."""
    await backend.layout_create(SHEET)
    result = await backend.layout_rename(SHEET, SHEET.upper())
    assert result["ok"] is True
    assert SHEET.upper() in _names(backend)


# ── layout_copy ─────────────────────────────────────────────────────────────


async def test_a_copy_carries_the_geometry_and_leaves_the_source_alone(backend):
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    await backend.entity_create_line(0, 0, 100, 0)
    await backend.entity_create_circle(50, 50, 10)
    await backend.layout_set_current("Model")

    result = await backend.layout_copy(SHEET, OTHER)

    assert result["ok"] is True
    assert result["entities_copied"] == 2
    assert result["skipped"] == []
    assert _entities_in(backend, OTHER) == _entities_in(backend, SHEET)


async def test_the_copy_is_independent_of_its_source(backend):
    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    await backend.entity_create_line(0, 0, 100, 0)
    await backend.layout_set_current("Model")
    await backend.layout_copy(SHEET, OTHER)

    await backend.layout_delete(SHEET)

    assert _entities_in(backend, OTHER) == ["LINE"]


async def test_the_copy_survives_a_save_and_reload_with_a_clean_audit(backend, tmp_path):
    """The Auditor is not evidence on its own, so check the structure too."""
    import ezdxf
    from ezdxf.audit import Auditor

    await backend.layout_create(SHEET)
    await backend.layout_set_current(SHEET)
    await backend.entity_create_line(0, 0, 100, 0)
    await backend.layout_set_current("Model")
    await backend.layout_copy(SHEET, OTHER)

    path = tmp_path / "copied.dxf"
    await backend.drawing_save_as(str(path))
    doc = ezdxf.readfile(str(path))

    auditor = Auditor(doc)
    auditor.run()
    assert not auditor.errors and not auditor.fixes
    assert OTHER in doc.layouts.names()
    assert [e.dxftype() for e in doc.layouts.get(OTHER)] == ["LINE"]


async def test_no_two_layouts_end_up_sharing_a_tab_order(backend):
    """A delete leaves a gap; letting new() assign taborder duplicates it."""
    await backend.layout_create(SHEET)
    await backend.layout_create("Scratch")
    await backend.layout_delete("Scratch")
    await backend.layout_copy(SHEET, OTHER)

    orders = [backend._doc.layouts.get(n).dxf.taborder for n in _names(backend) if n != "Model"]
    assert len(orders) == len(set(orders)), f"duplicate taborder: {orders}"


async def test_the_copy_keeps_the_source_paper_size(backend):
    await backend.layout_create(SHEET)
    source = backend._doc.layouts.get(SHEET)
    source.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units="mm")
    expected = source.get_paper_limits()

    await backend.layout_copy(SHEET, OTHER)

    assert backend._doc.layouts.get(OTHER).get_paper_limits() == expected


async def test_copying_model_space_is_refused(backend):
    """Otherwise the construction happily dumps model geometry onto a sheet."""
    result = await backend.layout_copy("Model", OTHER)
    assert result["ok"] is False
    assert OTHER not in _names(backend)


async def test_a_blank_source_does_not_resolve_to_whatever_sheet_is_active(backend):
    """ezdxf's `layouts.get("")` returns the active paper space."""
    result = await backend.layout_copy("", OTHER)
    assert result["ok"] is False
    assert OTHER not in _names(backend)


async def test_copying_onto_an_existing_name_is_refused(backend):
    await backend.layout_create(SHEET)
    await backend.layout_create(OTHER)
    result = await backend.layout_copy(SHEET, OTHER)
    assert result["ok"] is False


async def test_an_associative_hatch_does_not_keep_pointing_into_the_source(backend):
    """A dangling cross-layout boundary reference the Auditor cannot see."""
    from ezdxf.math import Vec3

    await backend.layout_create(SHEET)
    space = backend._doc.layouts.get(SHEET)
    poly = space.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    hatch = space.add_hatch(color=2)
    path = hatch.paths.add_polyline_path(
        [(Vec3(v[0], v[1]).x, Vec3(v[0], v[1]).y) for v in [(0, 0), (10, 0), (10, 10), (0, 10)]],
        is_closed=True,
    )
    path.source_boundary_objects = [poly.dxf.handle]
    hatch.set_seed_points([(5, 5)])

    result = await backend.layout_copy(SHEET, OTHER)
    assert result["ok"] is True

    source_handles = {e.dxf.handle for e in backend._doc.layouts.get(SHEET)}
    for entity in backend._doc.layouts.get(OTHER):
        if entity.dxftype() != "HATCH":
            continue
        for boundary in entity.paths:
            leaked = set(getattr(boundary, "source_boundary_objects", [])) & source_handles
            assert not leaked, f"copy still points at the source layout: {leaked}"


async def test_the_copy_does_not_end_up_with_two_main_viewports(backend):
    """Copying a page-set-up layout duplicates the id-1 viewport silently."""
    await backend.layout_create(SHEET)
    source = backend._doc.layouts.get(SHEET)
    source.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units="mm")

    await backend.layout_copy(SHEET, OTHER)

    target = backend._doc.layouts.get(OTHER)
    mains = [vp for vp in target.viewports() if vp.dxf.get("id", 0) == 1]
    assert len(mains) <= 1, "a layout may have at most one main viewport"
    if mains:
        assert target.dxf.viewport_handle == mains[0].dxf.handle
