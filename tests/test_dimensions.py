"""Tests for dimension creation via ezdxf backend."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_dimension_linear(backend):
    info = await backend.dimension_linear(0, 0, 100, 0, 50, 20, rotation=0)
    assert info.handle
    assert info.type == "DIMENSION"


async def test_dimension_aligned(backend):
    info = await backend.dimension_aligned(0, 0, 100, 50, 50, 60)
    assert info.handle
    assert info.type == "DIMENSION"


async def test_dimension_angular(backend):
    info = await backend.dimension_angular(0, 0, 100, 0, 0, 100, 50, 50)
    assert info.handle
    assert info.type == "DIMENSION"


async def test_dimension_radius(backend):
    await backend.entity_create_circle(50, 50, 30)
    info = await backend.dimension_radius(50, 50, 80, 50, leader_length=15)
    assert info.handle
    assert info.type == "DIMENSION"


async def test_dimension_diameter(backend):
    info = await backend.dimension_diameter(20, 50, 80, 50, leader_length=10)
    assert info.handle
    assert info.type == "DIMENSION"


# ── the number on the dimension must be the number on the drawing ───────────
#
# `add_diameter_dim`/`add_radius_dim` take `mpoint` as a point ON the circle;
# `leader_length` is where the *text* sits. Passing `centre + (radius + leader)`
# as `mpoint` makes ezdxf measure to that point, so every diameter came out
# `2 x leader_length` too big and every radius `leader_length` too big -- at
# DEFAULT settings, silently, on a drawing whose whole purpose is the number.
# The live COM backend was never wrong here: AddDimDiametric takes the two
# chord points and the leader length as separate arguments.


async def test_a_diameter_dimension_measures_the_diameter(backend):
    """Two ends of a diameter of a true 40 mm circle."""
    info = await backend.dimension_diameter(-20, 0, 20, 0)

    dim = backend._doc.entitydb.get(info.handle)
    assert dim.get_measurement() == pytest.approx(40.0)


async def test_the_leader_length_moves_the_text_not_the_measurement(backend):
    """The regression in one assertion: leader_length must not be measurable."""
    near = await backend.dimension_diameter(-20, 0, 20, 0, leader_length=5.0)
    far = await backend.dimension_diameter(-20, 0, 20, 0, leader_length=40.0)

    measurements = [
        backend._doc.entitydb.get(near.handle).get_measurement(),
        backend._doc.entitydb.get(far.handle).get_measurement(),
    ]
    assert measurements == pytest.approx([40.0, 40.0])


async def test_a_radius_dimension_measures_the_radius(backend):
    info = await backend.dimension_radius(0, 0, 20, 0)

    dim = backend._doc.entitydb.get(info.handle)
    assert dim.get_measurement() == pytest.approx(20.0)


async def test_a_radius_measurement_ignores_the_leader_too(backend):
    near = await backend.dimension_radius(0, 0, 20, 0, leader_length=5.0)
    far = await backend.dimension_radius(0, 0, 20, 0, leader_length=40.0)

    measurements = [
        backend._doc.entitydb.get(near.handle).get_measurement(),
        backend._doc.entitydb.get(far.handle).get_measurement(),
    ]
    assert measurements == pytest.approx([20.0, 20.0])


async def test_an_off_axis_diameter_is_measured_from_its_own_chord(backend):
    """The two points are a diameter at any angle, not just horizontal."""
    info = await backend.dimension_diameter(0, 0, 30, 40, leader_length=12.0)

    dim = backend._doc.entitydb.get(info.handle)
    assert dim.get_measurement() == pytest.approx(50.0)
