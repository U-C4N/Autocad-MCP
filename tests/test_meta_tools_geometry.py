"""Tests for deterministic geometry helpers: point_intersection and point_tangent."""

from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# point_intersection
# ---------------------------------------------------------------------------


class TestPointIntersection:
    async def test_line_line_perpendicular(self, backend):
        """Horizontal and vertical lines cross at (50, 50)."""
        h = await backend.entity_create_line(0, 50, 100, 50)
        v = await backend.entity_create_line(50, 0, 50, 100)
        pt = await backend.point_intersection(h.handle, v.handle)
        assert abs(pt[0] - 50.0) < 1e-9
        assert abs(pt[1] - 50.0) < 1e-9

    async def test_line_line_diagonal(self, backend):
        """y=x and y=100-x intersect at (50, 50)."""
        l1 = await backend.entity_create_line(0, 0, 100, 100)
        l2 = await backend.entity_create_line(0, 100, 100, 0)
        pt = await backend.point_intersection(l1.handle, l2.handle)
        assert abs(pt[0] - 50.0) < 1e-9
        assert abs(pt[1] - 50.0) < 1e-9

    async def test_line_line_parallel_raises(self, backend):
        """Parallel lines raise RuntimeError."""
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(0, 10, 100, 10)
        with pytest.raises(RuntimeError, match="parallel"):
            await backend.point_intersection(l1.handle, l2.handle)

    async def test_line_circle_two_intersections_ref_picks_nearest(self, backend):
        """Horizontal line through circle center gives two intersections; ref selects."""
        circ = await backend.entity_create_circle(50, 50, 20)
        line = await backend.entity_create_line(0, 50, 100, 50)
        # Left intersection (~30, 50) — ref closer to x=0
        left = await backend.point_intersection(circ.handle, line.handle, ref_x=0, ref_y=50)
        assert abs(left[0] - 30.0) < 1e-6
        # Right intersection (~70, 50) — ref closer to x=100
        right = await backend.point_intersection(line.handle, circ.handle, ref_x=100, ref_y=50)
        assert abs(right[0] - 70.0) < 1e-6

    async def test_line_circle_tangent_single_solution(self, backend):
        """Line tangent to circle gives one solution (discriminant ≈ 0)."""
        circ = await backend.entity_create_circle(50, 50, 20)
        # Horizontal line at y=70 is tangent to circle (center y=50, r=20)
        line = await backend.entity_create_line(0, 70, 100, 70)
        pt = await backend.point_intersection(circ.handle, line.handle)
        assert abs(pt[1] - 70.0) < 1e-6
        assert abs(pt[0] - 50.0) < 1e-6

    async def test_line_circle_no_intersection_raises(self, backend):
        """Line that misses the circle raises RuntimeError."""
        circ = await backend.entity_create_circle(50, 50, 10)
        line = await backend.entity_create_line(0, 200, 100, 200)
        with pytest.raises(RuntimeError, match="not intersect"):
            await backend.point_intersection(circ.handle, line.handle)

    async def test_circle_circle_two_intersections_ref_picks_nearest(self, backend):
        """Two circles: unit radius at (0,0) and (1,0); intersect at ±(0.5, √3/2)."""
        c1 = await backend.entity_create_circle(0, 0, 1)
        c2 = await backend.entity_create_circle(1, 0, 1)
        top = await backend.point_intersection(c1.handle, c2.handle, ref_x=0.5, ref_y=1)
        assert abs(top[0] - 0.5) < 1e-6
        assert abs(top[1] - math.sqrt(3) / 2.0) < 1e-6

    async def test_circle_circle_no_intersection_raises(self, backend):
        """Non-overlapping circles raise RuntimeError."""
        c1 = await backend.entity_create_circle(0, 0, 5)
        c2 = await backend.entity_create_circle(100, 0, 5)
        with pytest.raises(RuntimeError, match="not intersect"):
            await backend.point_intersection(c1.handle, c2.handle)

    async def test_unsupported_types_raise(self, backend):
        """Text has no geometry to intersect (ARC became supported with the practice-exam fixes)."""
        text = await backend.entity_create_text("x", 0, 0)
        line = await backend.entity_create_line(-5, 0, 5, 0)
        with pytest.raises(RuntimeError, match="unsupported"):
            await backend.point_intersection(text.handle, line.handle)


# ---------------------------------------------------------------------------
# point_tangent
# ---------------------------------------------------------------------------


class TestPointTangent:
    async def test_tangent_from_outside(self, backend):
        """From (0, 0) to unit circle at (5, 0): tangent point is at (25/5, ±12/5)."""
        circ = await backend.entity_create_circle(5, 0, 3)
        # from_point (0,0), circle (5,0) r=3; d=5
        # tangent_length = sqrt(25-9) = 4; cos_a = 3/5; sin_a = 4/5
        pt = await backend.point_tangent(circ.handle, 0, 0, ref_x=0, ref_y=3)
        dist = math.hypot(pt[0] - 5, pt[1] - 0)
        assert abs(dist - 3.0) < 1e-6  # point is on circle
        # Verify perpendicularity: (pt - center) · (pt - from) == 0
        dot = (pt[0] - 5) * (pt[0] - 0) + (pt[1] - 0) * (pt[1] - 0)
        assert abs(dot) < 1e-6

    async def test_tangent_ref_selects_side(self, backend):
        """ref_y > 0 selects upper tangent, ref_y < 0 selects lower."""
        circ = await backend.entity_create_circle(10, 0, 6)
        upper = await backend.point_tangent(circ.handle, 0, 0, ref_x=5, ref_y=10)
        lower = await backend.point_tangent(circ.handle, 0, 0, ref_x=5, ref_y=-10)
        assert upper[1] > 0
        assert lower[1] < 0

    async def test_point_inside_circle_raises(self, backend):
        """From-point inside circle raises RuntimeError."""
        circ = await backend.entity_create_circle(0, 0, 10)
        with pytest.raises(RuntimeError, match="inside"):
            await backend.point_tangent(circ.handle, 1, 1)

    async def test_wrong_entity_type_raises(self, backend):
        """Passing a LINE handle raises RuntimeError."""
        line = await backend.entity_create_line(0, 0, 100, 0)
        with pytest.raises(RuntimeError, match="expected CIRCLE"):
            await backend.point_tangent(line.handle, 0, 50)
