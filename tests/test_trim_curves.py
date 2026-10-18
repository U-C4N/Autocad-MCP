"""entity_trim on LINE / ARC / CIRCLE in any pair (``engineering.trim`` + the headless engine).

Found by the practice-exam run: the headless engine took LINE+LINE only, and the live engine sent
``_TRIM`` and reported success while the line stayed whole. Each case checks what is left of the
target, never the tool's say-so.
"""

from __future__ import annotations

import math

import pytest

from engineering.corners import SourceArc, SourceCircle, SourceLine
from engineering.trim import plan_trim

pytestmark = pytest.mark.asyncio


def test_plan_line_by_line_keeps_the_piece_with_the_keep_point():
    plan = plan_trim(SourceLine((0, 0), (10, 0)), SourceLine((5, -5), (5, 5)), (2, 0))
    assert (
        plan.kind == "line" and plan.p == pytest.approx((0, 0)) and plan.q == pytest.approx((5, 0))
    )
    plan = plan_trim(SourceLine((0, 0), (10, 0)), SourceLine((5, -5), (5, 5)), (9, 0))
    assert plan.p == pytest.approx((5, 0)) and plan.q == pytest.approx((10, 0))


def test_plan_line_between_two_circle_crossings_keeps_the_middle():
    plan = plan_trim(SourceLine((-20, 0), (20, 0)), SourceCircle((0, 0), 5), (0, 0))
    assert plan.p == pytest.approx((-5, 0)) and plan.q == pytest.approx((5, 0))


def test_plan_cutter_beyond_the_end_of_the_target_is_refused():
    with pytest.raises(ValueError, match="do not intersect on the target"):
        plan_trim(SourceLine((0, 0), (4, 0)), SourceLine((10, -5), (10, 5)), (1, 0))


def test_plan_arc_cut_by_a_line():
    arc = SourceArc((0, 0), 10, 0, 180)
    plan = plan_trim(arc, SourceLine((0, -20), (0, 20)), (8, 5))  # the x>0 half
    assert plan.kind == "arc" and plan.a0 == pytest.approx(0.0) and plan.a1 == pytest.approx(90.0)


def test_plan_circle_needs_two_crossings():
    with pytest.raises(ValueError, match="twice"):
        plan_trim(
            SourceCircle((0, 0), 5), SourceLine((5, -5), (5, 5)), (0, 5)
        )  # tangent: one touch


def test_plan_circle_cut_twice_keeps_the_arc_under_the_keep_point():
    plan = plan_trim(
        SourceCircle((0, 0), 5), SourceLine((-10, 3), (10, 3)), (0, 5)
    )  # the cap above y=3
    assert plan.kind == "circle_to_arc"
    assert (plan.a1 - plan.a0) % 360 == pytest.approx(2 * math.degrees(math.acos(3 / 5)), abs=1e-6)
    assert plan.a0 == pytest.approx(math.degrees(math.asin(3 / 5)), abs=1e-6)


async def test_line_trimmed_by_arc_on_the_headless_engine(backend):
    line = await backend.entity_create_line(-20, 0, 20, 0)
    circle = await backend.entity_create_circle(0, 0, 5)
    await backend.entity_trim(line.handle, circle.handle, 0, 0)
    got = (await backend.entity_get(line.handle)).properties
    assert got["start"] == pytest.approx([-5, 0]) and got["end"] == pytest.approx([5, 0])


async def test_circle_becomes_an_arc_with_the_layer_kept(backend):
    await backend.layer_create("CUT", color=1)
    circle = await backend.entity_create_circle(0, 0, 5, layer="CUT")
    line = await backend.entity_create_line(-10, 3, 10, 3)
    arc = await backend.entity_trim(circle.handle, line.handle, 0, 5)
    assert arc.type == "ARC" and arc.layer == "CUT" and arc.handle != circle.handle
    assert math.dist(arc.properties["center"], (0, 0)) == pytest.approx(0)
    with pytest.raises(RuntimeError):
        await backend.entity_get(circle.handle)


async def test_arc_trim_moves_the_right_end(backend):
    arc = await backend.entity_create_arc(0, 0, 10, 0, 180)
    cutter = await backend.entity_create_line(0, -20, 0, 20)
    trimmed = await backend.entity_trim(arc.handle, cutter.handle, 8, 5)
    assert trimmed.properties["start_angle"] == pytest.approx(0.0)
    assert trimmed.properties["end_angle"] == pytest.approx(90.0)


async def test_cutter_that_misses_the_target_is_refused(backend):
    line = await backend.entity_create_line(0, 0, 4, 0)
    cutter = await backend.entity_create_line(10, -5, 10, 5)
    with pytest.raises(RuntimeError, match="do not intersect"):
        await backend.entity_trim(line.handle, cutter.handle, 1, 0)
