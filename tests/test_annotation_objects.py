"""M8/F15 — annotation objects: wipeout, revision cloud, MTEXT mask, find/replace.

Four tools that fill real gaps rather than restating the 141 already here. Each
carries a measured caveat:

* **WIPEOUT** is a real DXF entity (`add_wipeout`) that round-trips.
* **REVCLOUD** is genuinely available: `ezdxf.revcloud.add_entity()` emits the
  bulged LWPOLYLINE that AutoCAD's REVCLOUD *is*, stamps the `RevcloudProps`
  xdata and picks the bulge sign from the winding direction — so the result is
  recognised by `is_revcloud()` rather than merely resembling a cloud. The trap
  is the segment length: pass one larger than the shortest edge and you get a
  "cloud" with no arcs at all, which looks like a plain rectangle and reports
  success.
* **MTEXT background mask** round-trips (`bg_fill`, `box_fill_scale`) — proven
  through saveas/readfile, because a mask that vanishes on save is worse than
  no mask.
* **find/replace** is the one with a real scope problem. TEXT, MTEXT, ATTRIB
  and ATTDEF are all readable *and* writable, so all four are in scope. A
  DIMENSION's `dxf.text` is `'<>'` — the override placeholder, not the
  measurement — so replacing text inside dimensions would either do nothing or
  destroy the association with the measured value. The tool reports which types
  it searched instead of silently skipping some, because "0 replacements" and
  "I did not look there" are different answers.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

SQUARE = [[0.0, 0.0], [100.0, 0.0], [100.0, 60.0], [0.0, 60.0]]


# ── wipeout ─────────────────────────────────────────────────────────────────


async def test_a_wipeout_is_created_and_reports_its_handle(backend):
    result = await backend.entity_create_wipeout(SQUARE)

    assert result["ok"] is True
    entity = backend._doc.entitydb.get(result["handle"])
    assert entity.dxftype() == "WIPEOUT"


async def test_a_wipeout_survives_save_and_reload(backend, tmp_path):
    import ezdxf

    created = await backend.entity_create_wipeout(SQUARE)
    path = tmp_path / "wipeout.dxf"
    await backend.drawing_save_as(str(path))

    reloaded = ezdxf.readfile(str(path)).entitydb.get(created["handle"])
    assert reloaded is not None and reloaded.dxftype() == "WIPEOUT"


@pytest.mark.parametrize("bad", [[], [[0.0, 0.0]], [[0.0, 0.0], [1.0, 1.0]]])
async def test_a_wipeout_needs_at_least_three_points(backend, bad):
    """Two points enclose no area; a zero-area mask masks nothing."""
    result = await backend.entity_create_wipeout(bad)
    assert result["ok"] is False


# ── revision cloud ──────────────────────────────────────────────────────────


async def test_a_revision_cloud_is_a_bulged_polyline(backend):
    """The arcs are the cloud. A revcloud without bulges is a rectangle."""
    result = await backend.entity_create_revcloud(SQUARE, segment_length=10.0)

    assert result["ok"] is True
    entity = backend._doc.entitydb.get(result["handle"])
    assert entity.dxftype() == "LWPOLYLINE"
    bulges = [point[4] for point in entity.get_points()]
    assert all(abs(b) > 0.1 for b in bulges), "every segment must carry an arc"
    assert result["segments"] == len(bulges)


async def test_the_cloud_is_recognisable_as_one(backend):
    from ezdxf.revcloud import is_revcloud

    result = await backend.entity_create_revcloud(SQUARE, segment_length=10.0)
    assert is_revcloud(backend._doc.entitydb.get(result["handle"]))


async def test_a_segment_longer_than_the_path_is_refused(backend):
    """Otherwise the result is a plain rectangle reported as a cloud."""
    result = await backend.entity_create_revcloud(SQUARE, segment_length=10_000.0)
    assert result["ok"] is False
    assert "segment_length" in result["error"]


@pytest.mark.parametrize("bad", [0.0, -5.0])
async def test_a_non_positive_segment_length_is_refused(backend, bad):
    result = await backend.entity_create_revcloud(SQUARE, segment_length=bad)
    assert result["ok"] is False


# ── MTEXT background mask ───────────────────────────────────────────────────


async def test_a_background_mask_round_trips(backend, tmp_path):
    """A mask that vanishes on save is worse than no mask."""
    import ezdxf

    mtext = await backend.entity_create_mtext("SECTION A-A", 10, 10)

    result = await backend.text_set_background(mtext.handle, color=2, scale=1.5)
    assert result["ok"] is True

    path = tmp_path / "masked.dxf"
    await backend.drawing_save_as(str(path))
    reloaded = ezdxf.readfile(str(path)).entitydb.get(mtext.handle)
    assert reloaded.dxf.bg_fill == 1
    assert reloaded.dxf.box_fill_scale == pytest.approx(1.5)


async def test_a_background_mask_can_be_turned_off_again(backend):
    mtext = await backend.entity_create_mtext("X", 10, 10)
    await backend.text_set_background(mtext.handle, color=2)

    result = await backend.text_set_background(mtext.handle, enabled=False)

    assert result["ok"] is True
    assert result["enabled"] is False
    assert backend._doc.entitydb.get(mtext.handle).dxf.get("bg_fill", 0) == 0


async def test_a_background_mask_on_plain_text_is_refused(backend):
    """Only MTEXT carries a background fill; TEXT silently would not."""
    text = await backend.entity_create_text("PLAIN", 0, 0)
    result = await backend.text_set_background(text.handle, color=2)
    assert result["ok"] is False
    assert "MTEXT" in result["error"]


@pytest.mark.parametrize("bad", [0.5, 0.0, -1.0])
async def test_a_mask_scale_below_one_is_refused(backend, bad):
    """A box smaller than the text is not a mask, it is a stripe."""
    mtext = await backend.entity_create_mtext("X", 0, 0)
    result = await backend.text_set_background(mtext.handle, color=2, scale=bad)
    assert result["ok"] is False


# ── find / replace ──────────────────────────────────────────────────────────


async def _tagged_block(backend):
    doc = backend._doc
    block = doc.blocks.new("TAGGED")
    block.add_attdef("PART", (0, 0), text="OLD")
    ref = doc.modelspace().add_blockref("TAGGED", (0, 0))
    ref.add_auto_attribs({"PART": "OLD"})
    return ref


async def test_find_replace_covers_text_mtext_and_block_attributes(backend):
    """Silently skipping attributes inside blocks is this tool's honesty trap.

    Four hits, not three: the ATTDEF in the block *definition* carries the
    default value the next insert will use, so leaving it behind would fix the
    drawing and reintroduce the old text on the next block reference.
    """
    text = await backend.entity_create_text("OLD value", 0, 0)
    mtext = await backend.entity_create_mtext("OLD value", 0, 10)
    await _tagged_block(backend)

    result = await backend.text_find_replace("OLD", "NEW")

    assert result["ok"] is True
    assert result["replaced"] == 4
    changed = {row["type"] for row in result["entities"]}
    assert changed == {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
    assert backend._doc.entitydb.get(text.handle).dxf.text == "NEW value"
    assert backend._doc.entitydb.get(mtext.handle).text == "NEW value"


async def test_it_reports_which_types_it_searched(backend):
    """ "0 replacements" and "I never looked there" are different answers."""
    result = await backend.text_find_replace("NOTHING", "X")

    assert result["ok"] is True
    assert result["replaced"] == 0
    assert set(result["searched_types"]) == {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
    assert "DIMENSION" in result["note"]


async def test_a_dry_run_changes_nothing(backend):
    text = await backend.entity_create_text("OLD", 0, 0)

    result = await backend.text_find_replace("OLD", "NEW", dry_run=True)

    assert result["replaced"] == 1
    assert result["dry_run"] is True
    assert backend._doc.entitydb.get(text.handle).dxf.text == "OLD"


async def test_it_can_be_scoped_to_a_layer(backend):
    await backend.entity_create_text("OLD", 0, 0, layer="TEXT")
    other = await backend.entity_create_text("OLD", 0, 10, layer="DIM")

    result = await backend.text_find_replace("OLD", "NEW", layer="TEXT")

    assert result["replaced"] == 1
    assert backend._doc.entitydb.get(other.handle).dxf.text == "OLD"


async def test_an_empty_search_string_is_refused(backend):
    """Replacing "" matches between every character and destroys the text."""
    result = await backend.text_find_replace("", "X")
    assert result["ok"] is False


async def test_case_sensitivity_is_the_callers_choice(backend):
    text = await backend.entity_create_text("Old Value", 0, 0)

    result = await backend.text_find_replace("old", "NEW", match_case=False)

    assert result["replaced"] == 1
    assert backend._doc.entitydb.get(text.handle).dxf.text == "NEW Value"
