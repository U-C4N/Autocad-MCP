"""T0.4 — coordinates that cross the MCP boundary are WCS, on both engines.

DXF does not store 2D geometry in world coordinates. A CIRCLE, ARC, LWPOLYLINE,
TEXT or INSERT lives in an Object Coordinate System derived from its ``extrusion``
vector by the Arbitrary Axis Algorithm. Mirroring flips the extrusion to
``(0, 0, -1)`` and leaves the stored x *unnegated*, so reading ``dxf.center``
raw returns a point the entity is not at.

The word "extrusion" appeared nowhere in this repository, so every such read was
wrong and silent. Measured: after the server's own ``entity_mirror`` across the Y
axis, a circle drawn at (30, 20) reported ``center [30.0, 20.0]`` while the *same
response's* ``bounding_box`` said ``min [-35, 15] / max [-25, 25]`` — the payload
contradicted itself by 60 mm, and ``point_from_snap``, ``entity_select_smart`` and
``entity_offset`` all inherited the error.

Two fences guard the obvious wrong fixes:

* ``test_wcs_entities_are_left_alone`` — LINE, MTEXT, ELLIPSE and SPLINE all carry
  an ``extrusion`` attribute too, and are already correct because ezdxf makes
  their ``ocs()`` a pass-through. Keying the fix off "has extrusion" breaks them.
* ``test_read_write_round_trip_is_a_no_op_on_a_mirrored_circle`` — the read and
  write paths were *consistently* wrong, so normalising reads without the write
  inverse would turn a harmless no-op into a real 60 mm teleport.
"""

from __future__ import annotations

import pytest

from backends.base import UnsupportedCapabilityError
from backends.com_backend import ComBackend
from backends.ezdxf_backend import EzdxfBackend

pytestmark = pytest.mark.asyncio

#: Mirror line = the Y axis, i.e. x -> -x. This is what produces extrusion (0,0,-1).
Y_AXIS = (0.0, 0.0, 0.0, 1.0)


def _assert_points(actual, expected) -> None:
    """pytest.approx refuses nested sequences, so compare point by point."""
    assert len(actual) == len(expected), f"{actual} != {expected}"
    for got, want in zip(actual, expected, strict=True):
        assert got == pytest.approx(want, abs=1e-9), f"{actual} != {expected}"


def _bbox_centre(info) -> tuple[float, float]:
    """Centre of the entity's own bounding box — an independent oracle.

    ``ezdxf.bbox.extents`` flattens the entity through the same renderer AutoCAD
    would, so it is WCS whatever the entity's frame is. It is the field that
    exposed the contradiction, and it is not computed from the OCS attributes.
    """
    box = info.properties["bounding_box"]
    return (
        (box["min"][0] + box["max"][0]) / 2.0,
        (box["min"][1] + box["max"][1]) / 2.0,
    )


# ── the fence: entities that were never wrong ───────────────────────────────


async def test_wcs_entities_are_left_alone(backend):
    """LINE/MTEXT/ELLIPSE/SPLINE carry extrusion but report WCS already."""
    line = await backend.entity_create_line(10, 5, 30, 5)
    ellipse = await backend.entity_create_ellipse(20, 40, 10, 5)
    text = await backend.entity_create_mtext("hello", 25, 60, 2.5)

    for created in (line, ellipse, text):
        mirrored = await backend.entity_mirror(created.handle, *Y_AXIS)
        info = await backend.entity_get(mirrored.handle)
        cx, cy = _bbox_centre(info)
        for key in ("start", "end", "center", "insertion"):
            point = info.properties.get(key)
            if point is None:
                continue
            assert point[0] < 0, f"{info.type}.{key} must land left of x=0 after mirroring"
        if "center" in info.properties:
            assert info.properties["center"] == pytest.approx([cx, cy], abs=1e-6)


# ── the defect ──────────────────────────────────────────────────────────────


async def test_entity_get_center_agrees_with_its_own_bounding_box(backend):
    """A response that contradicts itself is the clearest possible proof."""
    circle = await backend.entity_create_circle(30, 20, 5)
    mirrored = await backend.entity_mirror(circle.handle, *Y_AXIS)

    info = await backend.entity_get(mirrored.handle)
    assert info.properties["center"] == pytest.approx([-30.0, 20.0])
    assert info.properties["center"] == pytest.approx(list(_bbox_centre(info)), abs=1e-6)
    assert info.properties["radius"] == pytest.approx(5.0)


async def test_mirrored_arc_reports_wcs_centre_and_sweep(backend):
    """A left-handed frame reverses the sweep, so start/end also swap."""
    arc = await backend.entity_create_arc(30, 20, 5, 0, 90)
    mirrored = await backend.entity_mirror(arc.handle, *Y_AXIS)

    info = await backend.entity_get(mirrored.handle)
    assert info.properties["center"] == pytest.approx([-30.0, 20.0])

    # Mirroring the first quadrant across x=0 gives the second: the bbox says the
    # arc occupies x in [-35, -30], y in [20, 25], i.e. 90 deg -> 180 deg.
    assert info.properties["start_angle"] == pytest.approx(90.0, abs=1e-6)
    assert info.properties["end_angle"] == pytest.approx(180.0, abs=1e-6)
    # Length is frame-invariant and must not change.
    assert info.properties["length"] == pytest.approx(5 * 3.141592653589793 / 2, rel=1e-9)


async def test_mirrored_polyline_points_are_wcs(backend):
    poly = await backend.entity_create_polyline([[10, 0], [20, 0], [20, 10]], closed=False)
    mirrored = await backend.entity_mirror(poly.handle, *Y_AXIS)

    info = await backend.entity_get(mirrored.handle)
    _assert_points(info.properties["points"], [[-10, 0], [-20, 0], [-20, 10]])


async def test_mirrored_text_insertion_is_wcs(backend):
    text = await backend.entity_create_text("PART A", 40, 15, 3.0)
    mirrored = await backend.entity_mirror(text.handle, *Y_AXIS)

    info = await backend.entity_get(mirrored.handle)
    assert info.properties["insertion"] == pytest.approx([-40.0, 15.0])


# ── the trap: reads must not be normalised without the write inverse ────────


async def test_read_write_round_trip_is_a_no_op_on_a_mirrored_circle(backend):
    """Read the centre, write it straight back: the circle must not move.

    Before the fix this passed for the wrong reason — read and write were
    *consistently* wrong. It is here so that fixing only the read is caught.
    """
    circle = await backend.entity_create_circle(30, 20, 5)
    mirrored = await backend.entity_mirror(circle.handle, *Y_AXIS)

    before = await backend.entity_get(mirrored.handle)
    centre = before.properties["center"]
    await backend.entity_edit_geometry(mirrored.handle, cx=centre[0], cy=centre[1])
    after = await backend.entity_get(mirrored.handle)

    assert after.properties["center"] == pytest.approx(centre)
    assert after.properties["bounding_box"] == before.properties["bounding_box"]


async def test_entity_edit_geometry_moves_a_mirrored_circle_where_asked(backend):
    circle = await backend.entity_create_circle(30, 20, 5)
    mirrored = await backend.entity_mirror(circle.handle, *Y_AXIS)

    await backend.entity_edit_geometry(mirrored.handle, cx=-10.0, cy=40.0)

    info = await backend.entity_get(mirrored.handle)
    assert info.properties["center"] == pytest.approx([-10.0, 40.0])
    assert info.properties["center"] == pytest.approx(list(_bbox_centre(info)), abs=1e-6)


async def test_entity_offset_on_a_mirrored_circle_is_concentric_and_shrinks(backend):
    """The offset used to land 60 mm away *and* grow instead of shrink."""
    circle = await backend.entity_create_circle(30, 20, 10)
    mirrored = await backend.entity_mirror(circle.handle, *Y_AXIS)

    inner = await backend.entity_offset(mirrored.handle, 2.0, side_x=-30.0, side_y=20.0)

    info = await backend.entity_get(inner.handle)
    assert info.properties["center"] == pytest.approx([-30.0, 20.0])
    assert info.properties["radius"] == pytest.approx(8.0), "offset toward the centre shrinks"


# ── tilted planes: the one case 2D xy cannot address ────────────────────────


def _tilt(backend, handle, normal=(0.0, 0.6, 0.8)) -> None:
    """Put an entity in a plane that is not parallel to WCS XY."""
    backend._doc.entitydb.get(handle).dxf.extrusion = normal


async def test_a_tilted_entity_omits_what_xy_cannot_express(backend):
    """A tilted circle projects to an ellipse, so `radius` has no WCS-XY meaning.

    Omitting the field is silence; reporting 5.0 for a footprint of 5.0 x 3.99
    would be a fresh silent wrong number of exactly the kind this release exists
    to remove.
    """
    circle = await backend.entity_create_circle(30, 20, 5)
    _tilt(backend, circle.handle)

    info = await backend.entity_get(circle.handle)
    assert info.properties["center"] == pytest.approx(list(_bbox_centre(info)), abs=1e-6)
    assert "plane_normal" in info.properties
    assert info.properties["plane_normal"] == pytest.approx([0.0, 0.6, 0.8])
    assert "radius" not in info.properties


async def test_a_tilted_arc_omits_its_angles(backend):
    arc = await backend.entity_create_arc(30, 20, 5, 0, 90)
    _tilt(backend, arc.handle)

    info = await backend.entity_get(arc.handle)
    assert "start_angle" not in info.properties
    assert "end_angle" not in info.properties
    assert "plane_normal" in info.properties


async def test_entity_edit_geometry_refuses_a_tilted_plane_with_capability_shape(backend):
    circle = await backend.entity_create_circle(30, 20, 5)
    _tilt(backend, circle.handle)

    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.entity_edit_geometry(circle.handle, cx=1.0, cy=2.0)

    assert excinfo.value.capability == "ocs_tilted_plane"
    payload = excinfo.value.to_dict()
    assert payload["ok"] is False
    assert "entity_move" in payload["error"], "a refusal must name the way forward"


# ── the boundary is pre-checkable ───────────────────────────────────────────


async def test_ocs_capability_keys_are_declared_on_both_backends():
    for features in (
        EzdxfBackend().capabilities().features,
        ComBackend().capabilities().features,
    ):
        assert features["ocs_normalized"].supported is True
        assert features["ocs_normalized"].reason, "the residual must be named, not implied"
        assert features["ocs_tilted_plane"].supported is False


# ── COM: the one genuinely wrong read on that engine ────────────────────────


def _fake_lwpolyline(coords, normal):
    """The minimum surface ``_entity_info`` touches for a lightweight polyline."""
    import types

    return types.SimpleNamespace(
        ObjectName="AcDbLWPolyline",
        Coordinates=coords,
        Normal=normal,
        Elevation=0.0,
        Handle="2F",
        Layer="0",
        Color=256,
        Linetype="ByLayer",
        Visible=True,
        Closed=False,
        Length=30.0,
        GetBoundingBox=lambda: ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
    )


async def test_com_lwpolyline_coordinates_are_translated_to_wcs():
    """ActiveX returns WCS for everything *except* polyline Coordinates (OCS).

    Unit-tested against a fake COM object: this machine has a live AutoCAD and
    connecting to it is forbidden, so this path ships verified only at this
    level — the CHANGELOG says so.
    """
    from backends.com_backend import _entity_info

    info = _entity_info(_fake_lwpolyline((10.0, 0.0, 20.0, 0.0, 20.0, 10.0), (0.0, 0.0, -1.0)))
    _assert_points(info.properties["points"], [[-10, 0], [-20, 0], [-20, 10]])


async def test_com_lwpolyline_with_identity_normal_is_untouched():
    from backends.com_backend import _entity_info

    info = _entity_info(_fake_lwpolyline((10.0, 0.0, 20.0, 0.0), (0.0, 0.0, 1.0)))
    _assert_points(info.properties["points"], [[10, 0], [20, 0]])
