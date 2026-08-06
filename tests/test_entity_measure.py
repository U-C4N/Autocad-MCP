"""T0.5 — ``entity_measure``: ask the drawing, not the model's memory.

Companion to ``tests/test_measure.py``, which covers the arithmetic. This file
covers the part that was actually missing: addressing an entity **by handle**.

Before this, neither backend could answer "what is the area of the polyline I
just drew". ``analysis_measure_area`` shoelaced points the caller supplied, and
the live COM backend did the same — despite having a document, a
``HandleToObject`` lookup it already used fourteen times, and an ActiveX ``Area``
property sitting right there.

Two distinctions here are load-bearing and are asserted, not assumed:

* A LINE has no area on *any* engine, so that is a plain error — tagging it with
  a ``capability`` would falsely imply "switch backends and this works".
* A REGION has no area on the *headless* engine only, so that is a capability
  refusal naming the live backend as the way forward.
"""

from __future__ import annotations

import math

import pytest

from backends.base import UnsupportedCapabilityError
from backends.com_backend import ComBackend
from backends.ezdxf_backend import EzdxfBackend

pytestmark = pytest.mark.asyncio

#: 100x100 square, bottom edge bulged outward into a semicircle of radius 50.
BULGED_SQUARE = [(0, 0, 0, 0, 0.0), (100, 0, 0, 0, 1.0), (100, 100, 0, 0, 0.0), (0, 100, 0, 0, 0.0)]
TRUE_AREA = 10000.0 + math.pi * 50.0**2 / 2.0
TRUE_PERIMETER = 300.0 + math.pi * 50.0


def _add_bulged_square(backend, close=True):
    pline = backend._doc.modelspace().add_lwpolyline(BULGED_SQUARE, format="xyseb", close=close)
    return pline.dxf.handle


# ── the capability that did not exist ───────────────────────────────────────


async def test_measures_a_bulged_polyline_exactly(backend):
    """The 28.2% the reported-points workflow lost."""
    handle = _add_bulged_square(backend)

    result = await backend.entity_measure(handle)

    assert result["handle"] == handle
    assert result["type"] == "LWPOLYLINE"
    assert result["area"] == pytest.approx(TRUE_AREA, rel=1e-9)
    assert result["perimeter"] == pytest.approx(TRUE_PERIMETER, rel=1e-9)
    assert result["method"] == "analytic_bulge"
    assert result["exact"] is True
    assert result["flatten_tolerance"] is None
    assert result["closed"] is True
    assert result["assumed_closed"] is False
    assert result["self_intersecting"] is False
    assert result["backend"] == "ezdxf"


async def test_a_straight_polyline_reports_the_plain_analytic_method(backend):
    pline = backend._doc.modelspace().add_lwpolyline(
        [(0, 0), (10, 0), (10, 10), (0, 10)], close=True
    )
    result = await backend.entity_measure(pline.dxf.handle)

    assert result["area"] == pytest.approx(100.0)
    assert result["perimeter"] == pytest.approx(40.0)
    assert result["method"] == "analytic"
    assert result["exact"] is True


async def test_measures_a_circle(backend):
    circle = await backend.entity_create_circle(5, 5, 4)
    result = await backend.entity_measure(circle.handle)

    assert result["area"] == pytest.approx(math.pi * 16.0)
    assert result["perimeter"] == pytest.approx(2 * math.pi * 4.0)
    assert result["method"] == "analytic"
    assert result["exact"] is True
    assert result["closed"] is True


async def test_an_open_boundary_says_it_was_closed(backend):
    """AutoCAD's AREA closes an open boundary; the payload must admit it."""
    handle = _add_bulged_square(backend, close=False)

    result = await backend.entity_measure(handle)

    assert result["closed"] is False
    assert result["assumed_closed"] is True
    assert result["area"] == pytest.approx(TRUE_AREA, rel=1e-9)


async def test_a_self_intersecting_polyline_is_flagged(backend):
    """Shoelace cancels crossed lobes, so the 0.0 has to come with a warning."""
    pline = backend._doc.modelspace().add_lwpolyline(
        [(0, 0), (100, 100), (100, 0), (0, 100)], close=True
    )
    result = await backend.entity_measure(pline.dxf.handle)

    assert result["self_intersecting"] is True


# ── the two kinds of "no" ───────────────────────────────────────────────────


async def test_a_line_has_no_area_on_any_engine_so_it_is_not_a_capability_gap(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)

    with pytest.raises(RuntimeError) as excinfo:
        await backend.entity_measure(line.handle)

    assert not isinstance(excinfo.value, UnsupportedCapabilityError), (
        "tagging this `capability` would tell the user to switch backends, "
        "where no backend can give a LINE an area"
    )
    assert "LWPOLYLINE" in str(excinfo.value), "the refusal must list what IS measurable"


async def test_a_region_is_a_real_capability_boundary(backend):
    """ezdxf stores ACIS opaquely and cannot evaluate it; live AutoCAD can."""
    region = backend._doc.modelspace().new_entity("REGION", {})

    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.entity_measure(region.dxf.handle)

    assert excinfo.value.capability == "measure_area_acis"
    assert "com" in str(excinfo.value).lower(), "a refusal must name the way forward"


async def test_measure_capability_is_declared_on_both_backends():
    assert EzdxfBackend().capabilities().features["measure_area_acis"].supported is False
    assert ComBackend().capabilities().features["measure_area_acis"].supported is True


# ── HATCH: the type the refusal advertised and then refused ─────────────────
#
# `_MEASURABLE_TYPES` named HATCH, so a caller who asked for a hatch's area got
# a RuntimeError whose own text listed HATCH as measurable. Whichever half was
# right, the pair could not both be — and a hatch is the one entity whose area
# a drafter actually asks for, because it is the section they have to fill.


def _hatch_with_island(backend, *, style: int = 0):
    """20x20 outer square, 10x10 island. Filled area is 400 - 100 = 300."""
    hatch = backend._doc.modelspace().add_hatch(color=1)
    hatch.dxf.hatch_style = style
    hatch.paths.add_polyline_path(
        [(0, 0), (20, 0), (20, 20), (0, 20)], is_closed=True, flags=1 | 16
    )
    island = hatch.paths.add_edge_path(flags=0)
    island.add_line((5, 5), (15, 5))
    island.add_line((15, 5), (15, 15))
    island.add_line((15, 15), (5, 15))
    island.add_line((5, 15), (5, 5))
    return hatch.dxf.handle


async def test_a_hatch_reports_the_area_it_actually_fills(backend):
    handle = _hatch_with_island(backend)

    result = await backend.entity_measure(handle)

    assert result["type"] == "HATCH"
    assert result["area"] == pytest.approx(300.0, abs=1e-6), (
        "the island is a hole in the fill; counting it is the 33% error"
    )
    assert result["loop_count"] == 2
    assert result["hatch_style"] == "normal"
    assert result["exact"] is True, "every edge here is a straight line — nothing was flattened"


async def test_hatch_style_ignore_fills_straight_over_the_island(backend):
    """AutoCAD's HPISLAND=2 fills the island too. The number must follow."""
    handle = _hatch_with_island(backend, style=2)

    result = await backend.entity_measure(handle)

    assert result["area"] == pytest.approx(400.0, abs=1e-6)
    assert result["hatch_style"] == "ignore"


async def test_a_nested_island_alternates_under_the_normal_style(backend):
    """Depth 2 is where `normal` and `outer` stop agreeing, so it is the only
    case that proves the depths were computed rather than assumed."""
    hatch = backend._doc.modelspace().add_hatch(color=1)
    hatch.dxf.hatch_style = 0  # normal
    hatch.paths.add_polyline_path(
        [(0, 0), (20, 0), (20, 20), (0, 20)], is_closed=True, flags=1 | 16
    )
    hatch.paths.add_polyline_path([(4, 4), (16, 4), (16, 16), (4, 16)], is_closed=True, flags=0)
    hatch.paths.add_polyline_path([(8, 8), (12, 8), (12, 12), (8, 12)], is_closed=True, flags=0)

    result = await backend.entity_measure(hatch.dxf.handle)

    # 400 - 144 + 16: the innermost loop is filled again.
    assert result["area"] == pytest.approx(272.0, abs=1e-6)
    assert result["loop_count"] == 3


async def test_a_curved_hatch_edge_says_it_was_flattened(backend):
    hatch = backend._doc.modelspace().add_hatch(color=1)
    hatch.paths.add_edge_path(flags=1 | 16).add_arc((0, 0), 10, 0, 360)

    result = await backend.entity_measure(hatch.dxf.handle, flatten_tolerance=1e-5)

    assert result["area"] == pytest.approx(math.pi * 100.0, rel=1e-3)
    assert result["exact"] is False, "a flattened arc is not an exact area"
    assert result["flatten_tolerance"] == 1e-5


async def test_tightening_the_tolerance_does_not_make_a_curved_hatch_exact(backend):
    """The floor is the arc-to-Bezier conversion, not the flattening.

    A caller who sees `flatten_tolerance` naturally assumes a smaller number
    buys a better area, all the way to the truth. For hatch edges it does not:
    ezdxf hands the boundary over as cubic Beziers, and tightening the
    tolerance converges on the *Bezier's* area, which sits ~0.028% above the
    circle it stands in for. The residual does not shrink with the tolerance,
    which is why `exact` is False rather than the tolerance being presented as
    the accuracy knob.
    """
    hatch = backend._doc.modelspace().add_hatch(color=1)
    hatch.paths.add_edge_path(flags=1 | 16).add_arc((0, 0), 10, 0, 360)

    true_area = math.pi * 100.0
    coarse = await backend.entity_measure(hatch.dxf.handle, flatten_tolerance=1e-3)
    fine = await backend.entity_measure(hatch.dxf.handle, flatten_tolerance=1e-7)
    finest = await backend.entity_measure(hatch.dxf.handle, flatten_tolerance=1e-9)

    # Four orders of magnitude of extra tolerance buy nothing after the first.
    assert finest["area"] == pytest.approx(fine["area"], rel=1e-7)
    residual = abs(finest["area"] - true_area) / true_area
    assert residual > 2e-4, "the Bezier error survives any tolerance"
    assert abs(coarse["area"] - true_area) / true_area < residual, (
        "the coarse run is only closer by accident — an inscribed chord error "
        "that happens to cancel part of the Bezier overshoot"
    )
    assert finest["exact"] is False


# ── entity_get stops dropping the curve ─────────────────────────────────────


async def test_entity_get_reports_area_and_length_for_a_closed_polyline(backend):
    """``props["length"] = ent.length()`` was dead code — LWPolyline has no
    ``length()``, so the AttributeError went to a debug log and the key silently
    never appeared."""
    handle = _add_bulged_square(backend)

    info = await backend.entity_get(handle)

    assert info.properties["area"] == pytest.approx(TRUE_AREA, rel=1e-9)
    assert info.properties["length"] == pytest.approx(TRUE_PERIMETER, rel=1e-9)


async def test_entity_get_still_reports_only_xy_points(backend):
    """The bulge feeds the measurement; it must not change the points contract."""
    handle = _add_bulged_square(backend)
    info = await backend.entity_get(handle)

    assert all(len(point) == 2 for point in info.properties["points"])
