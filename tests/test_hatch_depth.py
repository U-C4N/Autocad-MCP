"""F4 — hatch depth: gradient fill, in-place edit, curved boundary edges.

`entity_create_hatch` can do exactly one thing: stamp a pattern fill into a
closed *polyline* boundary. Everything a section drawing actually needs after
that — restyling the fill, changing the island rule, bounding the fill with an
arc — has no tool. These three fill that gap, and each carries a measured trap:

* **Gradient fill is a replacement, not a layer.** `set_gradient()` flips
  `solid_fill` to 1, rewrites `pattern_name` to `SOLID` and drops `.pattern` to
  ``None``. A tool that reported "gradient applied" while leaving the caller
  believing the ANSI31 hatching is still underneath would be describing a
  drawing that does not exist. Every gradient assertion here goes through
  saveas/readfile, because gradient data lives in its own DXF subrecord and a
  gradient that vanishes on save is the classic failure mode.

* **`pattern_scale` is two numbers, not one.** ezdxf stores the scale twice:
  as `dxf.pattern_scale` *and* baked into the pattern definition lines' offset
  vectors. Setting only the DXF attribute is the perfect silent no-op — every
  report says scale 4.0, every renderer draws scale 2.0. So the scale and angle
  tests measure the pattern line *pitch* and *angle*, not the attribute.
  Measured here: ANSI31 at scale 1.0 has a 3.175 mm pitch; scale 2.0 is 6.35.
  ezdxf's `set_pattern_scale`/`set_pattern_angle` are absolute, not relative.

* **A partial edit must stay partial.** `hatch_edit` takes five optional
  parameters; the data-loss bug is the implementation that rebuilds the hatch
  from defaults and quietly resets the four the caller did not name. Note that
  a hatch born from `entity_create_hatch` already has `hatch_style == 1`
  (OUTER), because that is ezdxf's `set_pattern_fill` default — so "normal" is
  a real change, not a no-op, and `changed` has to distinguish a parameter that
  was *supplied* from an attribute that actually *moved*.

* **Flat vertex lists straighten curves.** This is the fidelity point of
  `hatch_add_boundary`. A hatch boundary API that only accepts points turns
  every fillet and bore into a chord and still reports a boundary. So the arc
  test asserts the reloaded path is an `EdgePath` holding an `ArcEdge` with its
  radius and sweep intact — a flattened arc would come back as a `PolylinePath`
  and pass any "did a path appear?" check.
"""

from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.asyncio

SQUARE = [[0.0, 0.0], [40.0, 0.0], [40.0, 40.0], [0.0, 40.0]]

RED = [255, 0, 0]
BLUE = [0, 0, 255]


def _entity(backend, handle):
    return backend._doc.entitydb.get(handle)


async def _reloaded(backend, tmp_path, handle, name="hatch.dxf"):
    """Round-trip the whole document and hand back the same entity."""
    import ezdxf

    path = tmp_path / name
    await backend.drawing_save_as(str(path))
    return ezdxf.readfile(str(path)).entitydb.get(handle)


def _pattern_pitch(hatch):
    """Distance between the hatch pattern's definition lines, in drawing units."""
    offset = hatch.pattern.lines[0].offset
    return math.hypot(offset[0], offset[1])


def _path_types(hatch):
    return [type(path).__name__ for path in hatch.paths]


# ── gradient fill ───────────────────────────────────────────────────────────


async def test_a_gradient_survives_save_and_reload(backend, tmp_path):
    """Gradient data lives in its own subrecord; losing it on save is silent."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_set_gradient(
        created.handle, RED, BLUE, rotation=30.0, tint=0.4, name="CURVED"
    )
    assert result["ok"] is True
    assert result["gradient"]["name"] == "CURVED"
    assert list(result["gradient"]["color1"]) == RED

    reloaded = await _reloaded(backend, tmp_path, created.handle)
    gradient = reloaded.gradient
    assert gradient is not None, "the gradient did not survive the round-trip"
    assert tuple(gradient.color1) == (255, 0, 0)
    assert tuple(gradient.color2) == (0, 0, 255)
    assert gradient.rotation == pytest.approx(30.0)
    assert gradient.tint == pytest.approx(0.4)
    assert gradient.name == "CURVED"


async def test_a_gradient_replaces_the_pattern_fill_it_lands_on(backend, tmp_path):
    """A hatch is gradient-filled *or* pattern-filled. Never both."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE, scale=2.0)
    assert _entity(backend, created.handle).dxf.pattern_name == "ANSI31"

    await backend.hatch_set_gradient(created.handle, RED, BLUE)

    reloaded = await _reloaded(backend, tmp_path, created.handle)
    assert reloaded.pattern is None, "the ANSI31 line definition is still there"
    assert reloaded.dxf.solid_fill == 1
    assert reloaded.dxf.pattern_name == "SOLID"


async def test_a_gradient_keeps_the_boundary_it_fills(backend, tmp_path):
    """A gradient with no boundary paints nothing at all."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    await backend.hatch_set_gradient(created.handle, RED, BLUE)

    reloaded = await _reloaded(backend, tmp_path, created.handle)
    assert _path_types(reloaded) == ["PolylinePath"]
    assert len(reloaded.paths[0].vertices) == len(SQUARE)


async def test_a_one_colour_gradient_stores_its_tint(backend, tmp_path):
    """one_color+tint is a distinct DXF mode; dropping the tint flattens it."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_set_gradient(
        created.handle, [10, 20, 30], [0, 0, 0], one_color=True, tint=0.75, name="SPHERICAL"
    )
    assert result["ok"] is True

    gradient = (await _reloaded(backend, tmp_path, created.handle)).gradient
    assert int(gradient.one_color) == 1
    assert gradient.tint == pytest.approx(0.75)
    assert gradient.name == "SPHERICAL"


async def test_a_gradient_on_a_line_is_refused_naming_the_type(backend):
    """Only a HATCH carries gradient data; a LINE would swallow it silently."""
    line = await backend.entity_create_line(0, 0, 10, 0)

    result = await backend.hatch_set_gradient(line.handle, RED, BLUE)

    assert result["ok"] is False
    assert "LINE" in result["error"]
    assert "HATCH" in result["error"]


# ── hatch edit ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("style, expected", [("normal", 0), ("outer", 1), ("ignore", 2)])
async def test_each_island_style_maps_to_its_dxf_value(backend, tmp_path, style, expected):
    created = await backend.entity_create_hatch("ANSI31", SQUARE)
    # Start from a different style so every case is a real move, not a no-op.
    _entity(backend, created.handle).dxf.hatch_style = (expected + 1) % 3

    result = await backend.hatch_edit(created.handle, style=style)

    assert result["ok"] is True
    assert "style" in result["changed"]
    reloaded = await _reloaded(backend, tmp_path, created.handle, f"style-{style}.dxf")
    assert reloaded.dxf.hatch_style == expected


async def test_editing_the_scale_leaves_every_other_attribute_alone(backend):
    """A partial edit that resets what it was not asked about is data loss."""
    created = await backend.entity_create_hatch(
        "ANSI31", SQUARE, scale=2.0, angle=15.0, layer="HATCH", color=3
    )
    style_before = _entity(backend, created.handle).dxf.hatch_style

    await backend.hatch_edit(created.handle, scale=4.0)

    hatch = _entity(backend, created.handle)
    assert hatch.dxf.pattern_name == "ANSI31"
    assert hatch.dxf.pattern_angle == pytest.approx(15.0)
    assert hatch.dxf.color == 3
    assert hatch.dxf.layer == "HATCH"
    assert hatch.dxf.hatch_style == style_before
    assert len(hatch.paths) == 1


async def test_a_new_scale_rescales_the_pattern_definition(backend, tmp_path):
    """Setting only dxf.pattern_scale is the perfect silent no-op."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE, scale=2.0)
    assert _pattern_pitch(_entity(backend, created.handle)) == pytest.approx(6.35)

    await backend.hatch_edit(created.handle, scale=4.0)

    reloaded = await _reloaded(backend, tmp_path, created.handle)
    assert reloaded.dxf.pattern_scale == pytest.approx(4.0)
    assert _pattern_pitch(reloaded) == pytest.approx(12.7), "the lines did not move apart"


async def test_a_new_angle_rotates_the_pattern_definition(backend, tmp_path):
    """Same trap as scale: the angle is stored twice and must move twice."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE, angle=15.0)
    assert _entity(backend, created.handle).pattern.lines[0].angle == pytest.approx(60.0)

    await backend.hatch_edit(created.handle, angle=30.0)

    reloaded = await _reloaded(backend, tmp_path, created.handle)
    assert reloaded.dxf.pattern_angle == pytest.approx(30.0)
    assert reloaded.pattern.lines[0].angle == pytest.approx(75.0), "the lines did not rotate"


async def test_a_new_pattern_and_colour_round_trip(backend, tmp_path):
    created = await backend.entity_create_hatch("ANSI31", SQUARE, scale=2.0, color=3)

    result = await backend.hatch_edit(created.handle, pattern="ANSI32", color=5)

    assert result["ok"] is True
    assert set(result["changed"]) == {"pattern", "color"}
    reloaded = await _reloaded(backend, tmp_path, created.handle)
    assert reloaded.dxf.pattern_name == "ANSI32"
    assert reloaded.dxf.color == 5
    assert reloaded.dxf.pattern_scale == pytest.approx(2.0), "the scale was reset"


async def test_changed_lists_only_the_attributes_that_moved(backend):
    """ "I was asked to set it" and "it is now different" are different answers."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE, scale=2.0, angle=15.0)

    result = await backend.hatch_edit(created.handle, scale=4.0, angle=15.0)

    assert result["ok"] is True
    assert set(result["changed"]) == {"scale"}


async def test_an_edit_that_moves_nothing_reports_nothing(backend):
    created = await backend.entity_create_hatch("ANSI31", SQUARE, scale=2.0)

    result = await backend.hatch_edit(created.handle, scale=2.0)

    assert result["ok"] is True
    assert result["changed"] == []


@pytest.mark.parametrize("bad", [0.0, -1.0])
async def test_a_non_positive_pattern_scale_is_refused(backend, bad):
    """Scale 0 collapses the definition offsets to zero: an infinite line count."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_edit(created.handle, scale=bad)

    assert result["ok"] is False
    assert "scale" in result["error"]


async def test_an_unknown_island_style_is_refused_naming_the_valid_ones(backend):
    """Silently falling back to `normal` would fill islands nobody asked to fill."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_edit(created.handle, style="outermost")

    assert result["ok"] is False
    for valid in ("normal", "outer", "ignore"):
        assert valid in result["error"]


# ── boundary edges ──────────────────────────────────────────────────────────

ARC_EDGE = {
    "type": "arc",
    "center": [110.0, 0.0],
    "radius": 10.0,
    "start_angle": 0.0,
    "end_angle": 180.0,
    "ccw": True,
}
LINE_EDGE = {"type": "line", "start": [100.0, 0.0], "end": [120.0, 0.0]}


async def test_an_arc_boundary_edge_survives_as_an_arc(backend, tmp_path):
    """The fidelity point: a flattened arc comes back as a PolylinePath."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_add_boundary(created.handle, [LINE_EDGE, ARC_EDGE])
    assert result["ok"] is True

    reloaded = await _reloaded(backend, tmp_path, created.handle)
    assert _path_types(reloaded) == ["PolylinePath", "EdgePath"]
    edges = reloaded.paths[1].edges
    assert [type(edge).__name__ for edge in edges] == ["LineEdge", "ArcEdge"]
    arc = edges[1]
    assert arc.radius == pytest.approx(10.0)
    assert tuple(arc.center)[:2] == pytest.approx((110.0, 0.0))
    assert arc.start_angle == pytest.approx(0.0)
    assert arc.end_angle == pytest.approx(180.0)
    assert arc.ccw is True


async def test_the_added_path_is_counted_alongside_the_existing_one(backend):
    """path_count is the hatch's total, so the original boundary is still there."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_add_boundary(created.handle, [LINE_EDGE, ARC_EDGE])

    assert result["handle"] == created.handle
    assert result["path_count"] == 2
    assert result["edge_types"] == ["line", "arc"]


async def test_an_ellipse_boundary_edge_keeps_its_ratio(backend, tmp_path):
    """The minor/major ratio *is* the ellipse; drop it and you have a circle."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    edge = {
        "type": "ellipse",
        "center": [200.0, 100.0],
        "major_axis": [30.0, 0.0],
        "ratio": 0.4,
        "start_angle": 0.0,
        "end_angle": 360.0,
        "ccw": True,
    }
    result = await backend.hatch_add_boundary(created.handle, [edge])
    assert result["edge_types"] == ["ellipse"]

    reloaded = await _reloaded(backend, tmp_path, created.handle)
    stored = reloaded.paths[1].edges[0]
    assert type(stored).__name__ == "EllipseEdge"
    assert stored.ratio == pytest.approx(0.4)
    assert tuple(stored.major_axis)[:2] == pytest.approx((30.0, 0.0))
    assert tuple(stored.center)[:2] == pytest.approx((200.0, 100.0))


async def test_an_unknown_edge_type_is_refused_naming_the_valid_ones(backend):
    """Skipping the edge it cannot build would leave an open boundary."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_add_boundary(
        created.handle, [{"type": "bezier", "start": [0, 0], "end": [1, 1]}]
    )

    assert result["ok"] is False
    assert "bezier" in result["error"]
    for valid in ("line", "arc", "ellipse"):
        assert valid in result["error"]


async def test_an_edge_missing_a_key_is_refused_naming_the_key(backend):
    """An arc defaulted to radius 0 is a boundary that encloses nothing."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_add_boundary(
        created.handle,
        [{"type": "arc", "center": [0.0, 0.0], "start_angle": 0.0, "end_angle": 90.0}],
    )

    assert result["ok"] is False
    assert "radius" in result["error"]


async def test_a_boundary_with_no_edges_is_refused(backend):
    """An empty edge path bounds nothing and renders as a hatch with a hole."""
    created = await backend.entity_create_hatch("ANSI31", SQUARE)

    result = await backend.hatch_add_boundary(created.handle, [])

    assert result["ok"] is False
