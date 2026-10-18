"""entity_extend for LINE / ARC targets against lines and circles (``engineering.extend``).

The live engine sent ``_EXTEND`` and reported success while the line stayed short (measured on
AutoCAD 2026); the headless engine took LINE+LINE only. Each case checks the target afterwards.
"""

from __future__ import annotations

import pytest

from engineering.corners import SourceArc, SourceLine
from engineering.extend import plan_extend

pytestmark = pytest.mark.asyncio


def test_plan_line_extends_the_end_nearest_the_boundary():
    plan = plan_extend(SourceLine((0, 0), (4, 0)), SourceLine((10, -5), (10, 5)))
    assert (
        plan.end == "end" and plan.q == pytest.approx((10, 0)) and plan.p == pytest.approx((0, 0))
    )


def test_plan_pick_chooses_the_end():
    plan = plan_extend(SourceLine((5, 0), (10, 0)), SourceLine((2, -5), (2, 5)), pick=(5, 0))
    assert plan.end == "start" and plan.p == pytest.approx((2, 0))


def test_plan_boundary_behind_the_end_is_refused():
    with pytest.raises(ValueError, match="behind"):
        plan_extend(SourceLine((0, 0), (4, 0)), SourceLine((-10, -5), (-10, 5)), pick=(4, 0))


def test_plan_parallel_is_refused():
    with pytest.raises(ValueError, match="parallel"):
        plan_extend(SourceLine((0, 0), (4, 0)), SourceLine((0, 5), (4, 5)))


def test_plan_arc_end_extends_to_a_line():
    plan = plan_extend(SourceArc((0, 0), 10, 0, 60), SourceLine((0, -20), (0, 20)), pick=(5, 8.66))
    assert plan.kind == "arc" and plan.end == "end"
    assert plan.a0 == pytest.approx(0.0) and plan.a1 == pytest.approx(90.0)


async def test_extend_line_to_a_circle_on_the_headless_engine(backend):
    line = await backend.entity_create_line(0, 0, 4, 0)
    circle = await backend.entity_create_circle(20, 0, 5)
    await backend.entity_extend(line.handle, circle.handle)
    end = (await backend.entity_get(line.handle)).properties["end"]
    assert end == pytest.approx([15, 0])  # first crossing of the circle past the old end


async def test_extend_arc_end_to_a_line(backend):
    arc = await backend.entity_create_arc(0, 0, 10, 0, 30)
    boundary = await backend.entity_create_line(-20, 10, 20, 10)  # crosses the R10 circle at 90 deg
    extended = await backend.entity_extend(arc.handle, boundary.handle)
    assert extended.properties["start_angle"] == pytest.approx(0.0)
    assert extended.properties["end_angle"] == pytest.approx(90.0)


async def test_extend_nothing_to_reach_is_refused(backend):
    line = await backend.entity_create_line(0, 0, 4, 0)
    boundary = await backend.entity_create_line(-10, -5, -10, 5)
    with pytest.raises(RuntimeError, match="behind"):
        await backend.entity_extend(line.handle, boundary.handle, end_x=4, end_y=0)
