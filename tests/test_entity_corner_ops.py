"""Tests for trim/extend/fillet/chamfer (Task #5).

Backend coverage: ezdxf only. COM tests need a live AutoCAD instance and are
skipped unless explicitly enabled.
"""

import pytest

pytestmark = pytest.mark.asyncio


def _close(a, b, tol=1e-6):
    return abs(a - b) < tol


# ─── TRIM ──────────────────────────────────────────────────────────────────


class TestTrim:
    async def test_trim_keep_left(self, backend):
        target = await backend.entity_create_line(0, 0, 100, 0)
        cutter = await backend.entity_create_line(50, -10, 50, 10)
        await backend.entity_trim(target.handle, cutter.handle, keep_x=10, keep_y=0)
        info = await backend.entity_get(target.handle)
        end = info.properties["end"]
        # End was at (100,0); should now be at the intersection (50,0).
        assert _close(end[0], 50.0)
        assert _close(end[1], 0.0)

    async def test_trim_keep_right(self, backend):
        target = await backend.entity_create_line(0, 0, 100, 0)
        cutter = await backend.entity_create_line(50, -10, 50, 10)
        await backend.entity_trim(target.handle, cutter.handle, keep_x=80, keep_y=0)
        info = await backend.entity_get(target.handle)
        start = info.properties["start"]
        assert _close(start[0], 50.0)
        assert _close(start[1], 0.0)

    async def test_trim_parallel_raises(self, backend):
        target = await backend.entity_create_line(0, 0, 100, 0)
        parallel = await backend.entity_create_line(0, 10, 100, 10)
        with pytest.raises(RuntimeError, match="parallel"):
            await backend.entity_trim(target.handle, parallel.handle, 50, 0)

    async def test_trim_same_handle_raises(self, backend):
        line = await backend.entity_create_line(0, 0, 100, 0)
        with pytest.raises(RuntimeError, match="same"):
            await backend.entity_trim(line.handle, line.handle, 50, 0)

    async def test_trim_non_line_raises(self, backend):
        line = await backend.entity_create_line(0, 0, 100, 0)
        circle = await backend.entity_create_circle(50, 0, 10)
        with pytest.raises(RuntimeError, match="LINE\\+LINE only"):
            await backend.entity_trim(line.handle, circle.handle, 80, 0)


# ─── EXTEND ────────────────────────────────────────────────────────────────


class TestExtend:
    async def test_extend_auto_endpoint(self, backend):
        # Target ends at x=40; boundary is vertical at x=80.
        target = await backend.entity_create_line(0, 0, 40, 0)
        boundary = await backend.entity_create_line(80, -10, 80, 10)
        await backend.entity_extend(target.handle, boundary.handle)
        info = await backend.entity_get(target.handle)
        end = info.properties["end"]
        assert _close(end[0], 80.0)
        assert _close(end[1], 0.0)

    async def test_extend_explicit_endpoint(self, backend):
        # Target is (50,0)-(100,0); extend the start side to x=20.
        target = await backend.entity_create_line(50, 0, 100, 0)
        boundary = await backend.entity_create_line(20, -10, 20, 10)
        await backend.entity_extend(target.handle, boundary.handle, end_x=50, end_y=0)
        info = await backend.entity_get(target.handle)
        start = info.properties["start"]
        assert _close(start[0], 20.0)

    async def test_extend_parallel_raises(self, backend):
        target = await backend.entity_create_line(0, 0, 50, 0)
        parallel = await backend.entity_create_line(0, 10, 50, 10)
        # V2 phrasing: a parallel boundary is simply never reached.
        with pytest.raises(RuntimeError, match="never reaches"):
            await backend.entity_extend(target.handle, parallel.handle)


# ─── FILLET ────────────────────────────────────────────────────────────────


class TestFillet:
    async def test_fillet_90_degree_corner(self, backend):
        # L-shape: horizontal (0,0)-(100,0) + vertical (100,0)-(100,80).
        # Inner fillet R=5 → arc center (95,5), tangent points (95,0) and (100,5).
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        arc = await backend.entity_fillet(l1.handle, l2.handle, radius=5)
        assert arc.type == "ARC"
        cx, cy = arc.properties["center"]
        assert _close(cx, 95.0)
        assert _close(cy, 5.0)
        assert _close(arc.properties["radius"], 5.0)

    async def test_fillet_trims_source_lines(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        await backend.entity_fillet(l1.handle, l2.handle, radius=5, trim=True)
        info1 = await backend.entity_get(l1.handle)
        info2 = await backend.entity_get(l2.handle)
        end1 = info1.properties["end"]
        start2 = info2.properties["start"]
        assert _close(end1[0], 95.0) and _close(end1[1], 0.0)
        assert _close(start2[0], 100.0) and _close(start2[1], 5.0)

    async def test_fillet_zero_radius(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        result = await backend.entity_fillet(l1.handle, l2.handle, radius=0)
        # No arc: result is the source line (corner-merge).
        assert result.type == "LINE"

    async def test_fillet_no_trim(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        await backend.entity_fillet(l1.handle, l2.handle, radius=5, trim=False)
        info1 = await backend.entity_get(l1.handle)
        # Source line endpoint should be unchanged (still 100,0).
        end1 = info1.properties["end"]
        assert _close(end1[0], 100.0)

    async def test_fillet_parallel_raises(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(0, 10, 100, 10)
        with pytest.raises(RuntimeError, match="parallel"):
            await backend.entity_fillet(l1.handle, l2.handle, radius=5)

    async def test_fillet_negative_radius_raises(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        with pytest.raises(RuntimeError, match=">= 0"):
            await backend.entity_fillet(l1.handle, l2.handle, radius=-1)


# ─── CHAMFER ───────────────────────────────────────────────────────────────


class TestChamfer:
    async def test_chamfer_symmetric(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        cl = await backend.entity_chamfer(l1.handle, l2.handle, dist1=4)
        assert cl.type == "LINE"
        s = cl.properties["start"]
        e = cl.properties["end"]
        # Tangent points: (96,0) and (100,4) in some order.
        pts = sorted([tuple(s[:2]), tuple(e[:2])])
        expected = sorted([(96.0, 0.0), (100.0, 4.0)])
        for got, exp in zip(pts, expected, strict=True):
            assert _close(got[0], exp[0]) and _close(got[1], exp[1])

    async def test_chamfer_asymmetric(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        cl = await backend.entity_chamfer(l1.handle, l2.handle, dist1=4, dist2=6)
        s = cl.properties["start"]
        e = cl.properties["end"]
        pts = sorted([tuple(s[:2]), tuple(e[:2])])
        expected = sorted([(96.0, 0.0), (100.0, 6.0)])
        for got, exp in zip(pts, expected, strict=True):
            assert _close(got[0], exp[0]) and _close(got[1], exp[1])

    async def test_chamfer_trims_source_lines(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        await backend.entity_chamfer(l1.handle, l2.handle, dist1=4, trim=True)
        info1 = await backend.entity_get(l1.handle)
        info2 = await backend.entity_get(l2.handle)
        assert _close(info1.properties["end"][0], 96.0)
        assert _close(info2.properties["start"][1], 4.0)

    async def test_chamfer_zero_distance_raises(self, backend):
        l1 = await backend.entity_create_line(0, 0, 100, 0)
        l2 = await backend.entity_create_line(100, 0, 100, 80)
        with pytest.raises(RuntimeError, match="> 0"):
            await backend.entity_chamfer(l1.handle, l2.handle, dist1=0)
