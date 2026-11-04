"""``mech_part_revise``: change a parameter, rebuild in place (plan item 1.8-19, B24-B28).

The gate: a dimensioned flange drawn in three views (front, end, section A-A)
with a bolt circle; the bore goes 20 -> 24, the length 60 -> 70, a chamfer is
added and the step radius removed. Every tagged LINE / CIRCLE / ARC whose key
survives keeps its handle and carries the new geometry; the drawing's entity
signatures (``drawing_diff``'s, handles excluded) equal a fresh
``mech_part_draw`` of the revised part; the dimensions read 24 and 70; the
stored bore requirement passes. A tagged line moved by hand refuses the
revision (``hand_edited_tagged_entity``) with every fingerprint unchanged.
"""

from __future__ import annotations

import copy
from collections import Counter

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends.ezdxf_backend import EzdxfBackend
from engineering.changes.preview import StaleRevisionError, entity_fingerprints
from engineering.errors import (
    HandEditedTaggedEntityError,
    InvalidArgsError,
    ModelDriftError,
    RevisionRefusedError,
    classify,
)
from engineering.mech import drift as D
from engineering.mech.draw import add_view, draw_part, read_part
from engineering.mech.part import build_part
from engineering.mech.revise import apply_changes, keyed, relayout, revise_part
from engineering.mech.xdata import APP_ID, to_values
from engineering.understand.diff import signature
from engineering.understand.snapshot import take_snapshot
from engineering.verification.requirements import parse_requirements, store_requirements

pytestmark = pytest.mark.asyncio

FLANGE = {
    "kind": "revolved",
    "name": "flange",
    "material": "steel",
    "segments": [
        {"length": 20.0, "d_outer": 120.0},
        {"length": 40.0, "d_outer": 60.0},
    ],
    "features": [
        {"kind": "axial_bore", "id": "bore", "diameter": 20.0},
        {"kind": "hole_circle", "id": "hc", "x": 10.0, "pcd": 90.0, "count": 4, "diameter": 11.0},
        {"kind": "fillet", "id": "r1", "at": "step:0", "radius": 3.0},
    ],
}
PLANE = {"p1": [-5.0, 0.0], "p2": [80.0, 0.0], "label": "A"}
CHAMFER = {"kind": "chamfer", "id": "c2", "at": "end", "size": 1.5}
CHANGES = {
    "features.bore.diameter": 24.0,
    "segments[1].length": 50.0,
    "features+": CHAMFER,
    "features.r1": None,
}


def revised_spec() -> dict:
    spec = copy.deepcopy(FLANGE)
    spec["features"][0]["diameter"] = 24.0
    spec["segments"][1]["length"] = 50.0
    spec["features"] = [f for f in spec["features"] if f["id"] != "r1"] + [dict(CHAMFER)]
    return spec


async def _mm_backend() -> EzdxfBackend:
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    await b.system_set_variable("INSUNITS", 4)
    return b


async def draw_sheet(b, spec) -> str:
    drawn = await draw_part(b, build_part(spec), at=(0.0, 0.0), views=("front", "side"))
    await add_view(b, drawn["part_id"], "section", plane=PLANE)
    return drawn["part_id"]


@pytest_asyncio.fixture
async def flange():
    b = await _mm_backend()
    pid = await draw_sheet(b, FLANGE)
    yield b, pid
    await b.disconnect()


def _signatures(snap) -> Counter:
    return Counter(signature(rec) for rec in snap.records)


async def _all_fingerprints(b) -> dict:
    snap = await take_snapshot(b)
    return await entity_fingerprints(b, [rec.handle for rec in snap.records])


BORE_REQUIREMENT = {
    "units": "mm",
    "items": [
        {
            "id": "BORE",
            "quantity": "diameter",
            "target": {"nominal": 24.0},
            "provenance": "explicit",
            # the only GEOMETRY circle between R8 and R15 is the bore, in the end view
            "features": [
                {
                    "kind": "predicate",
                    "type": "circle",
                    "layer": "GEOMETRY",
                    "radius_min": 8.0,
                    "radius_max": 15.0,
                }
            ],
        }
    ],
}


# -- the gate ---------------------------------------------------------------------------


async def test_a_revision_keeps_handles_and_draws_what_a_fresh_draw_draws(flange):
    b, pid = flange
    await store_requirements(b, parse_requirements(BORE_REQUIREMENT))

    payload = await read_part(b, pid)
    old = D.compare(pid, build_part(payload["part"]), payload, (await take_snapshot(b)).records)
    assert old.model_is_current and old.counts()["ok"] == len(old.expected)
    old_keys = keyed([(e.view, e.index, e.prim) for e in old.expected if not e.marker])
    handle_of = {
        key: old.matched[(key[0], index, False)].handle for key, (index, _p) in old_keys.items()
    }

    result = await revise_part(b, pid, CHANGES)
    assert result["changed"] is True and result["rev"] == 1
    assert result["model_is_current"] is True  # read back, not claimed

    # 100 % of the tagged LINE / CIRCLE / ARC handles whose key survives stand,
    # carrying the new geometry
    new_part = build_part(revised_spec())
    layout = relayout(new_part, await read_part(b, pid))
    new_keys = keyed(
        (D.record_view_tag(r), i, p)
        for r, prims in zip(layout.records, layout.prims, strict=True)
        for i, p in enumerate(prims)
    )
    survivors = [k for k in old_keys if k in new_keys and k[2] in ("LINE", "CIRCLE", "ARC")]
    assert len(survivors) >= 40
    snap = await take_snapshot(b)
    by_handle = {rec.handle: rec for rec in snap.records}
    eps = D.epsilon_for([p for _i, p in new_keys.values()])
    for key in survivors:
        rec = by_handle.get(handle_of[key])
        assert rec is not None, f"{key}: handle {handle_of[key]} did not survive"
        assert D.matches(rec, new_keys[key][1], eps), f"{key}: not the new geometry"
    assert set(result["in_place"]) >= {handle_of[k] for k in survivors}
    assert any(row["reason"] == "removed" for row in result["handle_map"])  # the fillet arcs

    # the drawing is exactly a fresh draw of the revised part
    fresh = await _mm_backend()
    await draw_sheet(fresh, revised_spec())
    assert _signatures(snap) == _signatures(await take_snapshot(fresh))

    texts = [rec.rendered_text or "" for rec in snap.records if rec.type == "DIMENSION"]
    assert any("24" in t and "⌀" in t for t in texts), texts
    assert any("70" in t for t in texts), texts
    assert not any("20" == t.strip("⌀Ø") for t in texts if t.startswith(("⌀", "Ø")))
    assert result["dimensions"]["deleted"] == len(result["dimensions"]["created"]) == 10

    assert result["verify"]["status"] == "pass"
    assert result["verify"]["counts"]["pass"] == 1
    after = await D.inspect_drift(b, pid)
    assert after.model_is_current


async def test_the_bore_requirement_failed_before_the_revision(flange):
    from engineering.verification.report import verify_requirements

    b, _pid = flange
    report = await verify_requirements(b, parse_requirements(BORE_REQUIREMENT))
    assert report["status"] == "fail"


async def test_a_hand_moved_tagged_line_is_refused_with_every_fingerprint_unchanged(flange):
    b, pid = flange
    snap = await take_snapshot(b)
    line = next(t for t in D.tagged_entities(snap.records, pid) if t.type == "LINE")
    await b.entity_move(line.handle, 1.0, 0.0)
    before = await _all_fingerprints(b)
    revision = b.document_identity()["revision"]

    with pytest.raises(HandEditedTaggedEntityError) as caught:
        await revise_part(b, pid, CHANGES)
    fields = caught.value.fields()
    assert fields["handles"] == [line.handle]
    assert fields["drift"][0]["status"] == "moved"
    envelope = classify(caught.value)
    assert envelope["kind"] == "hand_edited_tagged_entity"
    assert envelope["handles"] == [line.handle]

    assert await _all_fingerprints(b) == before
    assert b.document_identity()["revision"] == revision

    # the same edit in a dry run is refused the same way
    with pytest.raises(HandEditedTaggedEntityError):
        await revise_part(b, pid, CHANGES, dry_run=True)

    # overwrite=True replaces the hand edit with the model
    done = await revise_part(b, pid, CHANGES, overwrite=True)
    assert [row["handle"] for row in done["overwritten"]] == [line.handle]
    assert (await D.inspect_drift(b, pid)).model_is_current


async def test_a_layer_and_colour_edit_is_not_drift_and_is_kept(flange):
    b, pid = flange
    snap = await take_snapshot(b)
    circle = next(
        t for t in D.tagged_entities(snap.records, pid) if t.type == "CIRCLE" and t.feature == "hc"
    )
    await b.entity_set_properties(circle.handle, color=1)
    await revise_part(b, pid, {"features.bore.diameter": 24.0})
    entity = await b.entity_get(circle.handle)
    assert entity.color == 1


async def test_a_part_drawn_before_1_8_is_model_drift_until_overwrite(flange):
    b, pid = flange
    payload = await read_part(b, pid)
    legacy = {k: v for k, v in payload.items() if k not in ("part_id", "gen")}
    legacy["views"] = [{k: v for k, v in r.items() if k != "sha"} for r in legacy["views"]]
    await b.entity_set_xdata(pid, APP_ID, to_values(legacy))
    before = await _all_fingerprints(b)

    with pytest.raises(ModelDriftError) as caught:
        await revise_part(b, pid, {"segments[1].length": 50.0})
    assert caught.value.fields()["reason"] == "unstamped"
    assert classify(caught.value)["kind"] == "model_drift"
    assert await _all_fingerprints(b) == before

    await revise_part(b, pid, {"segments[1].length": 50.0}, overwrite=True)
    assert (await D.inspect_drift(b, pid)).model_is_current


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        ({"features.h9.diameter": 10}, "no feature 'h9'"),
        ({"segments[5].length": 10}, "index 5 is out of range"),
        ({"kind": "prismatic"}, "cannot change kind"),
        ({"colour": "red"}, "the part has no 'colour'"),
        ({"features.bore.diameter": 70.0}, "features.bore.diameter"),
        ({}, "non-empty object"),
        ({"segments[1].length": -5}, "segments[1].length"),
    ],
)
async def test_a_refused_path_names_itself_and_changes_nothing(flange, changes, needle):
    b, pid = flange
    before = await _all_fingerprints(b)
    with pytest.raises(InvalidArgsError) as caught:
        await revise_part(b, pid, changes)
    assert needle in str(caught.value) or needle in str(caught.value.__cause__)
    assert classify(caught.value)["kind"] == "invalid_args"
    assert await _all_fingerprints(b) == before


async def test_the_dry_run_previews_and_a_stale_apply_is_refused(flange):
    b, pid = flange
    dry = await revise_part(b, pid, CHANGES, dry_run=True)
    assert dry["dry_run"] is True and dry["counts"]["edited_in_place"] > 0
    preview = dry["preview"]
    assert preview["revision"] == b.document_identity()["revision"]
    assert len(preview["preconditions"]) > 1

    # an unrelated edit moves the revision: the preview is stale
    await b.entity_create_line(500.0, 500.0, 510.0, 500.0)
    before = await _all_fingerprints(b)
    with pytest.raises(StaleRevisionError):
        await revise_part(b, pid, CHANGES, expected_revision=preview["revision"])
    assert await _all_fingerprints(b) == before

    fresh = await revise_part(b, pid, CHANGES, dry_run=True)
    done = await revise_part(
        b,
        pid,
        CHANGES,
        expected_revision=fresh["preview"]["revision"],
        preconditions=fresh["preview"]["preconditions"],
    )
    assert done["changed"] is True


async def test_a_failure_part_way_rolls_the_whole_revision_back(flange, monkeypatch):
    b, pid = flange
    before = await _all_fingerprints(b)
    calls = {"n": 0}
    real = b.entity_edit_geometry

    async def breaking(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("the fifth edit broke")
        return await real(*args, **kwargs)

    monkeypatch.setattr(b, "entity_edit_geometry", breaking)
    signatures = _signatures(await take_snapshot(b))
    with pytest.raises(RuntimeError, match="fifth edit"):
        await revise_part(b, pid, CHANGES)
    monkeypatch.undo()
    after = await _all_fingerprints(b)
    assert set(after) == set(before), "the same handles, none added or lost"
    # The headless rollback reloads the transaction's DXF snapshot; a HATCH's
    # exported tags do not survive that round trip bit for bit (measured: the
    # two section hatches), every other entity does.
    hatches = {r.handle for r in (await take_snapshot(b)).records if r.type == "HATCH"}
    assert {h: v for h, v in after.items() if h not in hatches} == {
        h: v for h, v in before.items() if h not in hatches
    }
    assert _signatures(await take_snapshot(b)) == signatures
    assert (await D.inspect_drift(b, pid)).model_is_current
    assert (await read_part(b, pid))["part"]["segments"][1]["length"] == 40.0


async def test_an_unchanged_model_changes_nothing(flange):
    b, pid = flange
    before = await _all_fingerprints(b)
    result = await revise_part(b, pid, {"features.bore.diameter": 20.0})
    assert result["changed"] is False and result["handle_map"] == []
    assert await _all_fingerprints(b) == before


async def test_path_edits_record_old_and_new():
    old = build_part(FLANGE)
    from engineering.mech.part import part_to_dict

    new, diff = apply_changes(part_to_dict(old), CHANGES)
    assert diff["features.bore.diameter"] == [20.0, 24.0]
    assert diff["segments[1].length"] == [40.0, 50.0]
    assert diff["features.c2"] == [None, CHAMFER]
    assert diff["features.r1"][0]["kind"] == "fillet" and diff["features.r1"][1] is None
    assert build_part(new)  # builds


async def test_a_detail_view_is_refused_by_name(flange):
    b, pid = flange
    await add_view(
        b,
        pid,
        "detail",
        detail={"center": [20.0, 40.0], "radius": 8.0, "scale": 2.0, "of": "front"},
    )
    before = await _all_fingerprints(b)
    with pytest.raises(RevisionRefusedError, match="detail view") as caught:
        await revise_part(b, pid, {"features.bore.diameter": 24.0})
    # refused before anything was written - not "failed" (attempted and broke)
    envelope = classify(caught.value)
    assert envelope["kind"] == "refused" and envelope["reason"] == "detail_view"
    assert await _all_fingerprints(b) == before


async def test_a_repeated_view_tag_is_refused_by_name():
    b = await _mm_backend()
    drawn = await draw_part(b, build_part(FLANGE), views=("front", "side"))
    await add_view(b, drawn["part_id"], "side", side="left")
    with pytest.raises(RevisionRefusedError, match="more than once") as caught:
        await revise_part(b, drawn["part_id"], {"features.bore.diameter": 24.0})
    envelope = classify(caught.value)
    assert envelope["kind"] == "refused" and envelope["reason"] == "repeated_view_tag"


async def test_a_payload_that_does_not_build_or_has_no_view_is_refused(flange):
    b, pid = flange
    payload = await read_part(b, pid)
    stored = {k: v for k, v in payload.items() if k != "part_id"}

    broken = copy.deepcopy(stored)
    broken["part"]["segments"][0]["length"] = -1.0
    await b.entity_set_xdata(pid, APP_ID, to_values(broken))
    with pytest.raises(RevisionRefusedError, match="does not build") as caught:
        await revise_part(b, pid, {"features.bore.diameter": 24.0})
    assert classify(caught.value)["kind"] == "refused"

    await b.entity_set_xdata(pid, APP_ID, to_values({**stored, "views": []}))
    with pytest.raises(RevisionRefusedError, match="no view") as caught:
        await revise_part(b, pid, {"features.bore.diameter": 24.0})
    assert classify(caught.value)["kind"] == "refused"


async def test_a_handle_with_no_part_is_invalid_args(flange):
    b, _pid = flange
    line = await b.entity_create_line(0.0, 0.0, 1.0, 0.0)
    with pytest.raises(InvalidArgsError, match="part payload") as caught:
        await revise_part(b, line.handle, {"features.bore.diameter": 24.0})
    assert classify(caught.value)["kind"] == "invalid_args"


# -- hand-set properties on a redrawn entity -------------------------------------------


async def test_a_layer_and_colour_set_by_hand_on_a_redrawn_hatch_are_kept(flange):
    b, pid = flange
    await b.layer_create("MYLAYER", color=3)
    snap = await take_snapshot(b)
    hatch = next(t for t in D.tagged_entities(snap.records, pid) if t.type == "HATCH")
    line = next(t for t in D.tagged_entities(snap.records, pid) if t.type == "LINE")
    for handle in (hatch.handle, line.handle):
        await b.entity_set_properties(handle, layer="MYLAYER", color=5, linetype="HIDDEN")

    result = await revise_part(b, pid, {"features.bore.diameter": 24.0, "segments[1].length": 50.0})
    row = next(r for r in result["handle_map"] if r["old"] == hatch.handle)
    assert row["reason"] == "recreated:HATCH" and row["new"]
    redrawn = await b.entity_get(row["new"])
    assert (redrawn.layer, redrawn.color, redrawn.linetype) == ("MYLAYER", 5, "HIDDEN")
    assert row["kept"] == {"layer": "MYLAYER", "color": 5, "linetype": "HIDDEN"}
    # the in-place LINE keeps them too, untouched
    kept = await b.entity_get(line.handle)
    assert (kept.layer, kept.color) == ("MYLAYER", 5)
    assert result["model_is_current"] is True


# -- a part drawn by 1.7.0: untagged dimensions and hole table ---------------------------


def _make_1_7(b, pid: str) -> None:
    """Make the drawing what 1.7.0 drew: feature tags without ``i``, the
    dimensions and the hole table untagged, no gen / sha / dim on the payload."""
    import json

    doc = b._require_doc()
    for entity in doc.modelspace():
        if not entity.has_xdata(APP_ID):
            continue
        text = "".join(str(t.value) for t in entity.get_xdata(APP_ID) if t.code == 1000)
        try:
            tag = json.loads(text)
        except ValueError:
            continue
        if tag.get("kind") != "feature":
            continue
        if tag.get("role"):
            entity.discard_xdata(APP_ID)
        else:
            tag.pop("i", None)
            entity.set_xdata(APP_ID, [(1000, chunk) for chunk in to_values(tag)])


async def _legacy_sheet(spec) -> tuple[EzdxfBackend, str]:
    b = await _mm_backend()
    drawn = await draw_part(b, build_part(spec), views=("front", "side"))
    pid = drawn["part_id"]
    payload = await read_part(b, pid)
    legacy = {k: v for k, v in payload.items() if k not in ("part_id", "gen")}
    legacy["views"] = [
        {k: v for k, v in r.items() if k not in ("sha", "dim")} for r in legacy["views"]
    ]
    await b.entity_set_xdata(pid, APP_ID, to_values(legacy))
    _make_1_7(b, pid)
    assert not D.annotations_of((await take_snapshot(b)).records, pid)
    return b, pid


def _dimension_texts(snap) -> list[str]:
    return sorted(rec.rendered_text or "" for rec in snap.records if rec.type == "DIMENSION")


async def test_overwrite_on_a_1_7_part_replaces_its_untagged_dimensions():
    b, pid = await _legacy_sheet(FLANGE)
    old_dims = {rec.handle for rec in (await take_snapshot(b)).records if rec.type == "DIMENSION"}
    with pytest.raises(ModelDriftError):
        await revise_part(b, pid, {"features.bore.diameter": 24.0})

    changes = {"features.bore.diameter": 24.0, "segments[1].length": 50.0}
    result = await revise_part(b, pid, changes, overwrite=True)
    assert result["dimensions"]["legacy"]["dimensions"] == len(old_dims)
    assert result["dimensions"]["legacy"]["left"] == []
    assert result["dimensions"]["views"] == [0, 1]
    snap = await take_snapshot(b)
    assert not old_dims & {rec.handle for rec in snap.records}
    texts = _dimension_texts(snap)
    assert "⌀20" not in texts and any("24" in t for t in texts), texts

    # exactly what a fresh draw of the revised part draws, dimensions included
    spec = copy.deepcopy(FLANGE)
    spec["features"][0]["diameter"] = 24.0
    spec["segments"][1]["length"] = 50.0
    fresh = await _mm_backend()
    await draw_part(fresh, build_part(spec), views=("front", "side"))
    fresh_snap = await take_snapshot(fresh)
    assert texts == _dimension_texts(fresh_snap)
    assert _signatures(snap) == _signatures(fresh_snap)
    payload = await read_part(b, pid)
    assert [v["dimensioned"] for v in payload["views"]] == [True, True]
    assert result["model_is_current"] is True


async def test_overwrite_on_a_1_7_part_replaces_its_whole_untagged_hole_table():
    b, pid = await _legacy_sheet(PLATE)
    before = await take_snapshot(b)
    assert sum(rec.text == "HOLE TABLE" for rec in before.records) == 1

    result = await revise_part(b, pid, {"features.h4.diameter": 12.0}, overwrite=True)
    assert result["dimensions"]["legacy"]["hole_table_pieces"] > 1
    assert result["hole_table"]
    snap = await take_snapshot(b)
    assert sum(rec.text == "HOLE TABLE" for rec in snap.records) == 1
    assert "12" in {rec.text for rec in snap.records}
    assert (await read_part(b, pid))["hole_table"] == result["hole_table"]

    spec = copy.deepcopy(PLATE)
    spec["features"][4]["diameter"] = 12.0
    fresh = await _mm_backend()
    await draw_part(fresh, build_part(spec), views=("front", "side"))
    assert _signatures(snap) == _signatures(await take_snapshot(fresh))


# -- layouts, the hole table and balloons (added when the item was resumed) ---------------

SHAFT = {
    "kind": "revolved",
    "name": "shaft",
    "material": "steel",
    "segments": [{"length": 30.0, "d_outer": 40.0}, {"length": 25.0, "d_outer": 28.0}],
    "features": [{"kind": "chamfer", "id": "c1", "at": "start", "size": 1.0}],
}


async def _rotated_sheet(b, spec) -> str:
    """A part placed off the origin and turned 30 deg, with a view added by
    ``mech_view_add`` on the far side and its own gap - the layout path a
    revision must reproduce besides ``mech_part_draw``'s."""
    drawn = await draw_part(b, build_part(spec), at=(15.0, -40.0), rotation=30.0, views=("front",))
    await add_view(b, drawn["part_id"], "side", side="left", gap=35.0)
    await add_view(
        b, drawn["part_id"], "section", plane={"p1": [-5.0, 0.0], "p2": [70.0, 0.0]}, gap=12.0
    )
    return drawn["part_id"]


async def test_a_rotated_part_with_placed_views_revises_to_a_fresh_draw():
    b = await _mm_backend()
    pid = await _rotated_sheet(b, SHAFT)
    result = await revise_part(
        b, pid, {"segments[0].d_outer": 46.0, "segments[1].length": 32.0, "features.c1": None}
    )
    assert result["changed"] is True and result["in_place"]
    assert (await D.inspect_drift(b, pid)).model_is_current

    spec = copy.deepcopy(SHAFT)
    spec["segments"][0]["d_outer"] = 46.0
    spec["segments"][1]["length"] = 32.0
    spec["features"] = []
    fresh = await _mm_backend()
    await _rotated_sheet(fresh, spec)
    assert _signatures(await take_snapshot(b)) == _signatures(await take_snapshot(fresh))


PLATE = {
    "kind": "prismatic",
    "name": "plate",
    "material": "steel",
    "thickness": 10.0,
    "outline": [[0.0, 0.0], [120.0, 0.0], [120.0, 90.0], [0.0, 90.0]],
    "features": [
        {
            "kind": "hole",
            "id": f"h{n}",
            "x": 20.0 + 40.0 * (n % 3),
            "y": 20.0 + 25.0 * (n // 3),
            "diameter": 8.0,
        }
        for n in range(9)
    ],
}


async def test_the_hole_table_is_rewritten_with_the_new_sizes():
    b = await _mm_backend()
    drawn = await draw_part(b, build_part(PLATE), views=("front", "side"))
    pid = drawn["part_id"]
    old_table = (await read_part(b, pid))["hole_table"]
    # headless, a table is drawn as tagged lines and texts (the handle is the
    # first of them); live it is one TABLE - either way every tagged piece goes
    old_pieces = {
        rec.handle
        for rec, _tag in D.annotations_of((await take_snapshot(b)).records, pid, "hole_table")
    }
    assert old_table in old_pieces

    changes = {
        "features.h4.diameter": 12.0,
        "outline[1]": [130.0, 0.0],
        "outline[2]": [130.0, 90.0],
    }
    result = await revise_part(b, pid, changes)
    snap = await take_snapshot(b)
    alive = {rec.handle for rec in snap.records}
    new_pieces = {rec.handle for rec, _tag in D.annotations_of(snap.records, pid, "hole_table")}
    assert not old_pieces & alive
    assert result["hole_table"] in new_pieces and len(new_pieces) == len(old_pieces)
    assert (await read_part(b, pid))["hole_table"] == result["hole_table"]
    table_texts = {rec.text for rec in snap.records if rec.handle in new_pieces and rec.text}
    assert "12" in table_texts and "8" in table_texts

    spec = copy.deepcopy(PLATE)
    spec["features"][4]["diameter"] = 12.0
    spec["outline"][1] = [130.0, 0.0]
    spec["outline"][2] = [130.0, 90.0]
    fresh = await _mm_backend()
    await draw_part(fresh, build_part(spec), views=("front", "side"))
    assert _signatures(snap) == _signatures(await take_snapshot(fresh))
    assert (await D.inspect_drift(b, pid)).model_is_current


async def test_a_balloon_on_a_redrawn_entity_follows_it(flange):
    from engineering.sheet.bom import add_balloon, read_balloons

    b, pid = flange
    snap = await take_snapshot(b)
    hatch = next(t for t in D.tagged_entities(snap.records, pid) if t.type == "HATCH")
    await add_balloon(
        b, item=1, at=(200.0, 150.0), leader_to=(150.0, 100.0), targets=[hatch.handle]
    )

    result = await revise_part(b, pid, {"features.bore.diameter": 24.0})
    moved = {row["old"]: row["new"] for row in result["handle_map"] if row["old"]}
    assert hatch.handle in moved and moved[hatch.handle]
    (balloon,) = await read_balloons(b)
    assert balloon["targets"] == [moved[hatch.handle]]
    assert result["balloons_relinked"][0]["was"] == [hatch.handle]


# -- through the server -------------------------------------------------------------------


async def test_the_tool_revises_and_types_its_refusals(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        status = (await client.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf"
        await client.call_tool("drawing_new", {})
        drawn = (
            await client.call_tool("mech_part_draw", {"spec": FLANGE, "views": ["front", "side"]})
        ).structured_content
        pid = drawn["part_id"]
        done = (
            await client.call_tool(
                "mech_part_revise", {"part_id": pid, "changes": {"features.bore.diameter": 24}}
            )
        ).structured_content
        assert done["changed"] is True and done["rev"] == 1
        inspected = (
            await client.call_tool(
                "mech_part_inspect",
                {"part_id": pid, "drift": True, "param": "features.bore.diameter"},
            )
        ).structured_content
        assert inspected["model_is_current"] is True and inspected["drift"] == []
        assert [v["new"] for v in inspected["param"]["values"]] == [24]

        lines = (
            await client.call_tool(
                "entity_list", {"type_filter": "LINE", "layer_filter": "GEOMETRY"}
            )
        ).structured_content
        rows = lines.get("result", lines) if isinstance(lines, dict) else lines
        handle = rows[0]["handle"]
        await client.call_tool("entity_move", {"handle": handle, "dx": 2.0, "dy": 0.0})
        refused = await client.call_tool(
            "mech_part_revise",
            {"part_id": pid, "changes": {"features.bore.diameter": 22}},
            raise_on_error=False,
        )
        assert refused.is_error
        payload = refused.structured_content
        assert payload["kind"] == "hand_edited_tagged_entity"
        assert payload["handles"] == [handle]
        assert payload["category"] == "geometry" and payload["retryable"] is False


async def test_the_tool_types_a_layout_refusal_as_refused_not_failed(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        drawn = (
            await client.call_tool("mech_part_draw", {"spec": FLANGE, "views": ["front", "side"]})
        ).structured_content
        pid = drawn["part_id"]
        await client.call_tool("mech_view_add", {"part_id": pid, "kind": "side", "side": "left"})
        refused = await client.call_tool(
            "mech_part_revise",
            {"part_id": pid, "changes": {"features.bore.diameter": 22}},
            raise_on_error=False,
        )
        assert refused.is_error
        payload = refused.structured_content
        assert payload["kind"] == "refused" and payload["reason"] == "repeated_view_tag"
        assert payload["category"] == "policy"


async def test_the_tool_is_registered_in_the_mech_pack():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert tools["mech_part_revise"].meta["cad"]["cost"] == "destructive"
    assert server._tool_pack("mech_part_revise") == "mech"
    assert "mech_part_revise" not in server.LEAN_TOOL_NAMES
    from discovery.aliases import aliases_for

    assert aliases_for("mech_part_revise") is not None
