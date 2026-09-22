"""Sections, cut faces, material hatch and detail views.

The cut face and the silhouette come out of the same radial profile, so the
tests assert that the hatched loop closes on the real geometry - including the
ISO 6410 rule that an internal thread's hatch runs to the major diameter, and
the ISO 128-3 rule that a rib cut longitudinally is not hatched.
"""

from __future__ import annotations

import math

import pytest

from engineering.mech.features import AxialBore, Hole, Pocket, Thread
from engineering.mech.part import PrismaticPart, RevolvedPart, Segment
from engineering.mech.primitives import Circle, HatchArea, Text
from engineering.mech.views import (
    CIRCLE_SEGMENTS,
    SECTION_PLANE,
    SECTION_STYLES,
    VIEW_KINDS,
    build_view,
    cut_loops,
    detail_marker_prims,
    normalise_plane,
    radial_profile,
)

EPS = 1e-9


def shaft(*features) -> RevolvedPart:
    return RevolvedPart(
        name="shaft",
        segments=(Segment(20.0, 25.0), Segment(40.0, 30.0), Segment(15.0, 20.0)),
        features=tuple(features),
    )


def plate(*features) -> PrismaticPart:
    return PrismaticPart(
        name="plate",
        outline=((0.0, 0.0), (80.0, 0.0), (80.0, 50.0), (0.0, 50.0)),
        thickness=8.0,
        features=tuple(features),
    )


def axis_plane(**kw) -> dict:
    return normalise_plane({"p1": (0.0, 0.0), "p2": (100.0, 0.0), **kw})


def polygon_area(points) -> float:
    total = 0.0
    count = len(points)
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def test_the_engine_now_declares_five_kinds_and_four_section_styles():
    assert VIEW_KINDS == ("front", "side", "top", "section", "detail")
    assert SECTION_STYLES == ("full", "half", "offset", "revolved")
    assert set(SECTION_PLANE) == {"p1", "p2", "label", "direction", "style"}


def test_normalise_plane_fills_the_pinned_shape_and_refuses_a_zero_length_plane():
    plane = normalise_plane({"p1": (0.0, 0.0), "p2": (10.0, 0.0)})
    assert plane["label"] == "A"
    assert plane["direction"] == (0.0, -1.0)
    assert plane["style"] == "full"
    with pytest.raises(ValueError, match="zero length"):
        normalise_plane({"p1": (1.0, 1.0), "p2": (1.0, 1.0)})


# -- revolved longitudinal sections ------------------------------------------


def test_a_solid_shaft_full_section_hatches_both_halves_to_the_axis():
    loops = cut_loops(shaft(), axis_plane(), style="full")
    assert len(loops) == 2
    assert all(hatched for _, hatched in loops)
    upper, lower = (points for points, _ in loops)
    assert max(y for _, y in upper) == 15.0
    assert min(y for _, y in upper) == 0.0
    assert min(y for _, y in lower) == -15.0
    # the cut face is the real cross-section: 20x12.5 + 40x15 + 15x10 per half
    assert polygon_area(upper) == pytest.approx(20 * 12.5 + 40 * 15.0 + 15 * 10.0)


def test_a_bore_is_not_hatched_because_the_loop_closes_on_it():
    part = RevolvedPart(name="sleeve", segments=(Segment(30.0, 40.0, 20.0),))
    (upper, _), (lower, _) = cut_loops(part, axis_plane(), style="full")
    assert polygon_area(upper) == pytest.approx(30.0 * (20.0 - 10.0))
    assert min(y for _, y in upper) == 10.0
    assert max(y for _, y in lower) == -10.0


def test_a_half_section_hatches_one_half_only():
    loops = cut_loops(shaft(), axis_plane(style="half"), style="half")
    assert len(loops) == 1
    assert min(y for _, y in loops[0][0]) == 0.0


def test_iso6410_an_internal_thread_hatch_runs_to_the_major_diameter():
    thread = Thread(id="th", segment=0, designation="M20", length=25.0, internal=True)
    # drilled to the ISO 6410 minor (0.8 x 20 = 16 mm); the hatch must stop at
    # the 20 mm major, not at the drilled 16.
    part = RevolvedPart(name="boss", segments=(Segment(30.0, 50.0, 16.0),), features=(thread,))
    bands = radial_profile(part)
    threaded = [b for b in bands if b.x1 <= 25.0 + EPS]
    assert {round(b.r_in, 6) for b in threaded} == {10.0}
    unthreaded = [b for b in bands if b.x0 >= 25.0 - EPS]
    assert {round(b.r_in, 6) for b in unthreaded} == {8.0}
    (upper, _), _ = cut_loops(part, axis_plane(), style="full")
    # The cut face closes on the major over the threaded span - and on the real
    # drilled bore over the 5 mm that is drilled but not threaded. Asserting the
    # minimum over the *whole* loop would be asserting that the drill does not
    # exist beyond the thread, which is a different (and wrong) drawing.
    assert min(y for x, y in upper if x < 25.0 - EPS) == pytest.approx(10.0)
    assert min(y for x, y in upper if x > 25.0 + EPS) == pytest.approx(8.0)


def test_the_section_view_carries_a_hatch_area_per_loop_with_the_parts_material():
    view = build_view(shaft(), "section", plane=axis_plane(), style="full")
    hatches = [p for p in view.prims if isinstance(p, HatchArea)]
    assert len(hatches) == 2
    assert {h.material for h in hatches} == {"steel"}
    assert view.label == "A-A"


def test_an_unknown_material_is_refused_before_a_section_is_built():
    part = RevolvedPart(name="x", segments=(Segment(10.0, 20.0),), material="unobtainium")
    with pytest.raises(ValueError, match="unobtainium"):
        build_view(part, "section", plane=axis_plane())


def test_an_offset_plane_on_a_revolved_part_is_refused_with_the_alternative():
    with pytest.raises(ValueError) as excinfo:
        build_view(shaft(), "section", plane=axis_plane(style="offset"), style="offset")
    assert "style='full'" in str(excinfo.value)


# -- revolved transverse (cross) sections ------------------------------------


def test_a_transverse_section_hatches_the_annulus_at_that_station():
    part = RevolvedPart(name="sleeve", segments=(Segment(30.0, 40.0, 20.0),))
    plane = normalise_plane({"p1": (15.0, -30.0), "p2": (15.0, 30.0), "label": "B"})
    view = build_view(part, "section", plane=plane, style="full")
    hatches = [p for p in view.prims if isinstance(p, HatchArea)]
    assert len(hatches) == 1
    outer, island = hatches[0].loops
    assert len(outer) == CIRCLE_SEGMENTS and len(island) == CIRCLE_SEGMENTS
    # An inscribed regular CIRCLE_SEGMENTS-gon is exactly
    # 1 - (n / 2pi) sin(2pi / n) = 2.0307e-4 under the circle, so the tolerance
    # is the flattening the constant declares and nothing looser.
    flattening = 1.0 - (CIRCLE_SEGMENTS / (2.0 * math.pi)) * math.sin(
        2.0 * math.pi / CIRCLE_SEGMENTS
    )
    assert flattening == pytest.approx(2.0307e-4, rel=1e-3)
    assert polygon_area(outer) == pytest.approx(math.pi * 20.0**2, rel=2.1e-4)
    assert polygon_area(island) == pytest.approx(math.pi * 10.0**2, rel=2.1e-4)
    assert view.label == "B-B"
    circles = [p for p in view.prims if isinstance(p, Circle)]
    assert {round(c.radius, 6) for c in circles} == {20.0, 10.0}


def test_a_revolved_style_cross_section_draws_its_boundary_as_phantom():
    part = RevolvedPart(name="sleeve", segments=(Segment(30.0, 40.0, 20.0),))
    plane = normalise_plane({"p1": (15.0, -30.0), "p2": (15.0, 30.0)})
    view = build_view(part, "section", plane=plane, style="revolved")
    circles = [p for p in view.prims if isinstance(p, Circle)]
    assert {c.role for c in circles} == {"phantom"}


def test_a_half_transverse_section_is_refused_with_its_reason():
    part = RevolvedPart(name="sleeve", segments=(Segment(30.0, 40.0, 20.0),))
    plane = normalise_plane({"p1": (15.0, -30.0), "p2": (15.0, 30.0)})
    with pytest.raises(ValueError, match="half section"):
        build_view(part, "section", plane=plane, style="half")


# -- prismatic sections ------------------------------------------------------


def test_a_prismatic_full_section_is_the_chord_times_the_thickness():
    plane = normalise_plane({"p1": (-10.0, 25.0), "p2": (100.0, 25.0), "label": "C"})
    view = build_view(plate(), "section", plane=plane, style="full")
    (hatch,) = [p for p in view.prims if isinstance(p, HatchArea)]
    assert polygon_area(hatch.loops[0]) == pytest.approx(80.0 * 8.0)
    assert view.bbox == (0.0, 0.0, 80.0, 8.0)


def test_a_through_hole_splits_the_cut_face_in_two():
    hole = Hole(id="h1", x=40.0, y=25.0, diameter=10.0)
    plane = normalise_plane({"p1": (-10.0, 25.0), "p2": (100.0, 25.0)})
    view = build_view(plate(hole), "section", plane=plane, style="full")
    hatches = [p for p in view.prims if isinstance(p, HatchArea)]
    assert len(hatches) == 2
    total = sum(polygon_area(h.loops[0]) for h in hatches)
    assert total == pytest.approx((80.0 - 10.0) * 8.0)


def test_a_pocket_reduces_the_cut_face_to_the_remaining_thickness():
    pocket = Pocket(
        id="p1",
        outline=((20.0, 15.0), (60.0, 15.0), (60.0, 35.0), (20.0, 35.0)),
        depth=3.0,
    )
    plane = normalise_plane({"p1": (-10.0, 25.0), "p2": (100.0, 25.0)})
    view = build_view(plate(pocket), "section", plane=plane, style="full")
    hatches = [p for p in view.prims if isinstance(p, HatchArea)]
    total = sum(polygon_area(h.loops[0]) for h in hatches)
    assert total == pytest.approx(40.0 * 8.0 + 40.0 * 5.0)


def test_iso128_3_a_rib_cut_longitudinally_is_drawn_but_not_hatched():
    rib = Pocket(
        id="rib",
        outline=((20.0, 15.0), (60.0, 15.0), (60.0, 35.0), (20.0, 35.0)),
        depth=3.0,
        no_section_hatch=True,
    )
    plane = normalise_plane({"p1": (-10.0, 25.0), "p2": (100.0, 25.0)})
    loops = cut_loops(plate(rib), plane, style="full")
    hatched = [points for points, flag in loops if flag]
    unhatched = [points for points, flag in loops if not flag]
    assert len(unhatched) == 1
    assert polygon_area(unhatched[0]) == pytest.approx(40.0 * 5.0)
    assert sum(polygon_area(p) for p in hatched) == pytest.approx(40.0 * 8.0)
    view = build_view(plate(rib), "section", plane=plane, style="full")
    assert len([p for p in view.prims if isinstance(p, HatchArea)]) == 2


def test_an_offset_prismatic_section_accumulates_along_the_dog_leg():
    plane = normalise_plane(
        {
            "p1": (-10.0, 15.0),
            "p2": (100.0, 35.0),
            "via": [[30.0, 15.0], [30.0, 35.0]],
            "style": "offset",
        }
    )
    view = build_view(plate(), "section", plane=plane, style="offset")
    hatches = [p for p in view.prims if isinstance(p, HatchArea)]
    total = sum(polygon_area(h.loops[0]) for h in hatches)
    assert total == pytest.approx(80.0 * 8.0)


def test_a_revolved_style_section_of_a_prismatic_part_is_refused():
    plane = normalise_plane({"p1": (-10.0, 25.0), "p2": (100.0, 25.0)})
    with pytest.raises(ValueError, match="revolved section"):
        build_view(plate(), "section", plane=plane, style="revolved")


def test_a_plane_that_misses_the_part_is_refused_rather_than_hatched_empty():
    plane = normalise_plane({"p1": (-10.0, 200.0), "p2": (100.0, 200.0)})
    with pytest.raises(ValueError, match="does not cut"):
        build_view(plate(), "section", plane=plane, style="full")


# -- detail views ------------------------------------------------------------


def test_a_detail_scales_the_region_and_labels_both_places():
    detail = {"center": (20.0, 12.5), "radius": 8.0, "scale": 5.0, "label": "D"}
    view = build_view(shaft(), "detail", detail=detail)
    assert view.kind == "detail"
    assert view.scale == 5.0
    assert view.label == "D (5:1)"
    width = view.bbox[2] - view.bbox[0]
    # straight geometry is trimmed to the circle, so the enlargement can never
    # be wider than the circle itself at the detail scale
    assert width <= 2.0 * 8.0 * 5.0 + 1e-6
    assert width > 8.0 * 5.0
    marker = detail_marker_prims(detail)
    circle = next(p for p in marker if isinstance(p, Circle))
    text = next(p for p in marker if isinstance(p, Text))
    assert circle.center == (20.0, 12.5) and circle.radius == 8.0
    assert text.text == "D"


def test_a_detail_reports_the_features_that_fall_outside_its_circle():
    from engineering.mech.features import RetainingGroove

    groove = RetainingGroove(id="rg", x=70.0, d=20.0, m=1.6, d2=18.6)
    detail = {"center": (5.0, 12.5), "radius": 6.0, "scale": 5.0, "label": "D"}
    view = build_view(shaft(groove), "detail", detail=detail)
    assert any(entry["feature"] == "rg" for entry in view.omitted)
    assert "detail circle D" in next(
        entry["reason"] for entry in view.omitted if entry["feature"] == "rg"
    )


def test_a_detail_without_a_radius_or_with_a_bad_scale_is_refused():
    with pytest.raises(ValueError, match="radius"):
        build_view(shaft(), "detail", detail={"center": (20.0, 12.5), "scale": 5.0})
    with pytest.raises(ValueError, match="scale"):
        build_view(shaft(), "detail", detail={"center": (20.0, 12.5), "radius": 8.0, "scale": 0.0})


def test_a_section_without_a_plane_is_refused_by_name():
    with pytest.raises(ValueError, match="plane"):
        build_view(shaft(), "section")


def test_an_axial_bore_shows_its_flat_bottom_in_the_section():
    bore = AxialBore(id="ab", at="start", diameter=8.0, depth=30.0)
    view = build_view(shaft(bore), "section", plane=axis_plane(), style="full")
    (hatch_upper, _hatch_lower) = [p for p in view.prims if isinstance(p, HatchArea)]
    assert min(y for _, y in hatch_upper.loops[0]) == 0.0
    assert 4.0 in {round(y, 6) for _, y in hatch_upper.loops[0]}
