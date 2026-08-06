"""F2 — LIST: the raw DXF attribute set for one handle.

`entity_get` reports a *curated* view: the handful of derived fields the drafting
tools need, already flattened to WCS xy. `entity_measure` reports lengths and
areas. Neither answers the question AutoCAD's LIST answers — "what is actually
stored on this object?" — and that is the only reason this tool exists. It must
not restate the other two.

Four measured facts shape every test below.

* **The dump is not JSON-serialisable as it comes out of ezdxf.**
  `entity.dxfattribs()` returns `Vec3` for `start` / `end` / `insert` / `center`.
  Measured: `json.dumps` raises ``Object of type Vec3 is not JSON serializable``
  for LINE, CIRCLE, TEXT and INSERT. A tool that hands that dict back either dies
  at the wire boundary or gets silently coerced into `"Vec3(0.0, 0.0, 0.0)"` —
  a string where the caller expected a point. So `json.dumps(result)` is asserted
  outright, for all five point-carrying types, and the values are asserted to
  still be *numbers* afterwards: stringifying the Vec3 would satisfy `json.dumps`
  and destroy the payload.

* **The dump is WCS, like everything else crossing a tool boundary.**
  A mirrored CIRCLE stores `center = (30, 20)` with `extrusion = (0, 0, -1)`;
  it is drawn at (-30, 20), which is what `entity_get` reports. A raw
  `dxfattribs()` echo therefore contradicts `entity_get` in the same payload by
  60 mm — the exact defect T0.4 was opened to remove. The dump still reports the
  `extrusion` it translated *from*, because a caller who cannot see the frame
  cannot tell a translated point from an untranslated one.

* **`dxfattribs()` on an LWPOLYLINE contains no geometry at all.**
  Measured: the keys are `flags`, `handle`, `layer`, `owner` — the vertices live
  in `get_points()`, not in the attribute dict. A tool that dumps `dxfattribs()`
  and stops reports a polyline with no points and calls it a full property dump.
  The vertices must carry their **bulge** too: `entity_get` gives bare `[x, y]`
  pairs, and reconstructing a bulged outline from those loses 28.2% of the area.

* **Only stored attributes are reported.** `thickness` is absent from a fresh
  LINE and appears once set. Padding the dump with schema defaults would make an
  explicitly-set 0.0 indistinguishable from "never touched", which is the one
  question a raw attribute dump exists to answer.

The refusal: an unknown or deleted handle raises `RuntimeError`, matching
`entity_get`. Returning `{}` for a handle that is not there would report "this
entity has no properties" for an entity that has no existence.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio

#: Mirror line = the Y axis (x -> -x). This is what produces extrusion (0, 0, -1).
Y_AXIS = (0.0, 0.0, 0.0, 1.0)


def _numbers(value):
    """Assert `value` is a real numeric sequence and return its x, y.

    Accepts a 2- or 3-component point: the arity of the dump is the tool
    author's call, but "it is a sequence of numbers" is not. `bool` is excluded
    because `isinstance(True, int)` would otherwise wave a coerced flag through.
    """
    assert isinstance(value, (list, tuple)), f"expected a point, got {value!r}"
    assert len(value) in (2, 3), f"expected a 2D or 3D point, got {value!r}"
    for component in value:
        assert isinstance(component, (int, float)) and not isinstance(component, bool), (
            f"point component {component!r} is not a number"
        )
    return float(value[0]), float(value[1])


async def _one_of_each(backend):
    """A LINE, LWPOLYLINE, INSERT, TEXT and CIRCLE — every Vec3-carrying type."""
    backend._doc.blocks.new("BOLT").add_circle((0.0, 0.0), 2.0)
    return {
        "LINE": (await backend.entity_create_line(0, 0, 100, 50)).handle,
        "LWPOLYLINE": (
            await backend.entity_create_polyline([[0, 0], [10, 0], [10, 10]], closed=True)
        ).handle,
        "INSERT": (await backend.block_insert("BOLT", 10, 20, scale_x=2.0, scale_y=3.0)).handle,
        "TEXT": (await backend.entity_create_text("PART A", 40, 15, 3.0)).handle,
        "CIRCLE": (await backend.entity_create_circle(30, 20, 5)).handle,
    }


# ── the wire boundary ───────────────────────────────────────────────────────


@pytest.mark.parametrize("dxftype", ["LINE", "LWPOLYLINE", "INSERT", "TEXT", "CIRCLE"])
async def test_the_dump_survives_json_encoding(backend, dxftype):
    """Measured: raw `dxfattribs()` raises `Object of type Vec3 is not JSON serializable`."""
    handles = await _one_of_each(backend)

    result = await backend.analysis_list_properties(handles[dxftype])

    encoded = json.dumps(result)
    assert json.loads(encoded) == result, "the payload changed shape on the way through JSON"


async def test_a_lines_endpoints_are_numbers_not_stringified_vectors(backend):
    """`str(Vec3(...))` would satisfy json.dumps and hand the caller a string."""
    line = await backend.entity_create_line(0, 0, 100, 50)

    dump = (await backend.analysis_list_properties(line.handle))["dxf_attributes"]

    assert _numbers(dump["start"]) == pytest.approx((0.0, 0.0))
    assert _numbers(dump["end"]) == pytest.approx((100.0, 50.0))


async def test_a_texts_insertion_point_is_numbers_not_a_stringified_vector(backend):
    text = await backend.entity_create_text("PART A", 40, 15, 3.0)

    dump = (await backend.analysis_list_properties(text.handle))["dxf_attributes"]

    assert _numbers(dump["insert"]) == pytest.approx((40.0, 15.0))
    assert dump["text"] == "PART A"
    assert dump["height"] == pytest.approx(3.0)


async def test_an_inserts_insertion_point_is_numbers_not_a_stringified_vector(backend):
    backend._doc.blocks.new("BOLT").add_circle((0.0, 0.0), 2.0)
    insert = await backend.block_insert("BOLT", 10, 20, scale_x=2.0, scale_y=3.0)

    dump = (await backend.analysis_list_properties(insert.handle))["dxf_attributes"]

    assert _numbers(dump["insert"]) == pytest.approx((10.0, 20.0))
    assert dump["name"] == "BOLT"
    assert dump["xscale"] == pytest.approx(2.0)
    assert dump["yscale"] == pytest.approx(3.0)


# ── coordinates are WCS ─────────────────────────────────────────────────────


async def test_a_mirrored_circles_centre_is_reported_in_wcs(backend):
    """Stored (30, 20) with extrusion -Z; drawn at (-30, 20). The dump says drawn.

    Echoing `dxfattribs()` puts a 60 mm contradiction inside one payload, because
    `entity_get` on the same handle reports (-30, 20).
    """
    circle = await backend.entity_create_circle(30, 20, 5)
    mirrored = await backend.entity_mirror(circle.handle, *Y_AXIS)
    assert backend._doc.entitydb.get(mirrored.handle).dxf.center.x == pytest.approx(30.0), (
        "precondition: the DXF really does store the un-negated x"
    )

    dump = (await backend.analysis_list_properties(mirrored.handle))["dxf_attributes"]

    assert _numbers(dump["center"]) == pytest.approx((-30.0, 20.0))
    info = await backend.entity_get(mirrored.handle)
    assert _numbers(dump["center"]) == pytest.approx(tuple(info.properties["center"]))


async def test_the_dump_still_names_the_frame_it_translated_from(backend):
    """Drop the extrusion and a caller cannot tell a translated point from a raw one."""
    circle = await backend.entity_create_circle(30, 20, 5)
    mirrored = await backend.entity_mirror(circle.handle, *Y_AXIS)

    dump = (await backend.analysis_list_properties(mirrored.handle))["dxf_attributes"]

    assert list(dump["extrusion"]) == pytest.approx([0.0, 0.0, -1.0])
    assert dump["radius"] == pytest.approx(5.0), "radius is frame-invariant in a flat frame"


# ── what entity_get deliberately does not carry ─────────────────────────────


async def test_a_polylines_vertices_are_in_the_dump(backend):
    """Measured: `dxfattribs()` for an LWPOLYLINE is flags/handle/layer/owner only.

    Dumping it verbatim describes a polyline without saying where it is.
    """
    poly = await backend.entity_create_polyline([[0, 0], [10, 0], [10, 10]], closed=True)
    assert "points" not in backend._doc.entitydb.get(poly.handle).dxfattribs(), (
        "precondition: ezdxf keeps the vertices out of the attribute dict"
    )

    dump = (await backend.analysis_list_properties(poly.handle))["dxf_attributes"]

    assert [_numbers(pt) for pt in dump["points"]] == pytest.approx(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    )


async def test_a_polyline_vertex_carries_its_bulge(backend):
    """`entity_get` gives bare [x, y]; rebuilding a bulged outline from those
    loses 28.2% of the area. The full dump is where the bulge has to live."""
    poly = await backend.entity_create_polyline([[0, 0], [10, 0], [10, 10]], closed=True)
    backend._doc.entitydb.get(poly.handle).set_points(
        [(0, 0, 0.0), (10, 0, 0.5), (10, 10, 0.0)], format="xyb"
    )

    dump = (await backend.analysis_list_properties(poly.handle))["dxf_attributes"]

    bulges = [pt[2] for pt in dump["points"]]
    assert len(bulges) == 3, "every vertex reports a bulge, including the straight ones"
    assert bulges == pytest.approx([0.0, 0.5, 0.0])


async def test_text_style_and_obliquity_reach_the_caller(backend):
    """Neither appears anywhere in `entity_get`, and both change what is plotted."""
    backend._doc.styles.new("ISO", dxfattribs={"font": "isocp.shx"})
    text = await backend.entity_create_text("PART A", 40, 15, 3.0)
    entity = backend._doc.entitydb.get(text.handle)
    entity.dxf.style = "ISO"
    entity.dxf.oblique = 15.0

    dump = (await backend.analysis_list_properties(text.handle))["dxf_attributes"]

    assert dump["style"] == "ISO"
    assert dump["oblique"] == pytest.approx(15.0)
    info = await backend.entity_get(text.handle)
    assert "style" not in info.properties, "precondition: entity_get is the curated view"


async def test_the_dump_reports_what_is_stored_not_schema_defaults(backend):
    """A dump padded with defaults cannot answer "did the drafter set this?".

    That is the only question a raw attribute dump exists to answer, so an
    unset `thickness` must be absent rather than reported as 0.0.
    """
    line = await backend.entity_create_line(0, 0, 100, 50)

    before = (await backend.analysis_list_properties(line.handle))["dxf_attributes"]
    assert "thickness" not in before
    assert "ltscale" not in before

    entity = backend._doc.entitydb.get(line.handle)
    entity.dxf.thickness = 2.0
    entity.dxf.ltscale = 0.5
    after = (await backend.analysis_list_properties(line.handle))["dxf_attributes"]

    assert after["thickness"] == pytest.approx(2.0)
    assert after["ltscale"] == pytest.approx(0.5)


# ── the top level ───────────────────────────────────────────────────────────


async def test_the_type_and_layer_sit_at_the_top_level(backend):
    """A caller asking "what is this?" should not have to dig into the dump."""
    circle = await backend.entity_create_circle(30, 20, 5, layer="GEOMETRY")

    result = await backend.analysis_list_properties(circle.handle)

    assert result["ok"] is True
    assert result["handle"] == circle.handle
    assert result["type"] == "CIRCLE"
    assert result["layer"] == "GEOMETRY"
    assert result["dxf_attributes"]["layer"] == "GEOMETRY", "the two must not disagree"


async def test_the_summary_does_not_contradict_entity_get(backend):
    """`properties` restates the curated view, so it must at least agree with it.

    This is the test that argues for deleting the key: a second copy of
    `entity_get`'s output can only ever be identical or wrong.
    """
    circle = await backend.entity_create_circle(30, 20, 5)
    mirrored = await backend.entity_mirror(circle.handle, *Y_AXIS)

    result = await backend.analysis_list_properties(mirrored.handle)

    info = await backend.entity_get(mirrored.handle)
    assert result["properties"]["center"] == pytest.approx(info.properties["center"])


# ── the refusal ─────────────────────────────────────────────────────────────


async def test_an_unknown_handle_is_refused_not_answered_with_an_empty_dump(backend):
    """`{}` would read as "this entity has no properties" for a nonexistent entity."""
    with pytest.raises(RuntimeError, match="FFFFFF"):
        await backend.analysis_list_properties("FFFFFF")


async def test_a_deleted_handle_is_refused_too(backend):
    """`entitydb.get` does not filter destroyed entities, so the guard must."""
    line = await backend.entity_create_line(0, 0, 100, 50)
    await backend.entity_delete(line.handle)

    with pytest.raises(RuntimeError, match=line.handle):
        await backend.analysis_list_properties(line.handle)


# ── round trip ──────────────────────────────────────────────────────────────


async def test_the_dump_describes_what_actually_lands_in_the_file(backend, tmp_path):
    """The dump is a translation of the file, not a snapshot of in-memory state.

    The reloaded DXF still stores the OCS centre; the dump reported the WCS one.
    Both readings of the same circle must remain consistent after a save.
    """
    import ezdxf

    circle = await backend.entity_create_circle(30, 20, 5)
    mirrored = await backend.entity_mirror(circle.handle, *Y_AXIS)
    dump = (await backend.analysis_list_properties(mirrored.handle))["dxf_attributes"]

    path = tmp_path / "listed.dxf"
    await backend.drawing_save_as(str(path))
    reloaded = ezdxf.readfile(str(path)).entitydb.get(mirrored.handle)

    assert reloaded.dxf.center.x == pytest.approx(30.0), "the file keeps the OCS x"
    assert list(reloaded.dxf.extrusion) == pytest.approx(list(dump["extrusion"]))
    assert reloaded.dxf.radius == pytest.approx(dump["radius"])
    assert _numbers(dump["center"]) == pytest.approx((-reloaded.dxf.center.x, 20.0))


async def test_a_texts_dump_round_trips_through_a_saved_file(backend, tmp_path):
    import ezdxf

    text = await backend.entity_create_text("PART A", 40, 15, 3.0)
    dump = (await backend.analysis_list_properties(text.handle))["dxf_attributes"]

    path = tmp_path / "listed_text.dxf"
    await backend.drawing_save_as(str(path))
    reloaded = ezdxf.readfile(str(path)).entitydb.get(text.handle)

    assert reloaded.dxf.text == dump["text"]
    assert reloaded.dxf.height == pytest.approx(dump["height"])
    assert _numbers(dump["insert"]) == pytest.approx((reloaded.dxf.insert.x, reloaded.dxf.insert.y))
