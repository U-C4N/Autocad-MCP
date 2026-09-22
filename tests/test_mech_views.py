"""The orthographic view engine: silhouette, hidden lines, centre lines, layout.

Everything is asserted as emitted primitive tuples - counts, roles, coordinates
to 1e-9. The radial profile is the single calculation both the silhouette and
(from Task 5) the section cut face come out of, so it is pinned first.
"""

from __future__ import annotations

import pytest

from engineering.mech.features import (
    AxialBore,
    Chamfer,
    CornerFillet,
    CrossHole,
    Fillet,
    Hole,
    Keyway,
    RetainingGroove,
)
from engineering.mech.part import PrismaticPart, RevolvedPart, Segment
from engineering.mech.primitives import Arc, Circle, Line
from engineering.mech.views import (
    PROJECTIONS,
    VIEW_KINDS,
    Band,
    View,
    build_view,
    layout_origin,
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


def roles(view: View, role: str):
    return [p for p in view.prims if getattr(p, "role", None) == role]


def test_the_engine_declares_three_orthographic_kinds_and_two_projections():
    assert VIEW_KINDS == ("front", "side", "top")
    assert PROJECTIONS == ("first", "third")


# -- the radial profile ------------------------------------------------------


def test_a_plain_shaft_has_one_band_per_segment():
    bands = radial_profile(shaft())
    assert bands == (
        Band(0.0, 20.0, 0.0, 12.5, 12.5),
        Band(20.0, 60.0, 0.0, 15.0, 15.0),
        Band(60.0, 75.0, 0.0, 10.0, 10.0),
    )


def test_a_taper_is_carried_as_two_end_radii():
    part = RevolvedPart(name="cone", segments=(Segment(20.0, 30.0, 0.0, 10.0),))
    (band,) = radial_profile(part)
    assert band.r_out0 == pytest.approx(15.0, abs=1e-6)
    assert band.r_out1 == pytest.approx(5.0, abs=1e-6)


def test_a_ring_groove_cuts_a_band_out_of_the_outer_profile():
    groove = RetainingGroove(id="rg", x=30.0, d=30.0, m=1.6, d2=28.6)
    bands = radial_profile(shaft(groove))
    # the cut band is the one INSIDE the 30 mm segment whose outside is pulled
    # back; segments 0 (d25) and 2 (d20) are simply thinner and are not cuts.
    cut = [b for b in bands if 20.0 <= b.x0 and b.x1 <= 60.0 and b.r_out0 < 15.0 - EPS]
    assert len(cut) == 1
    assert cut[0].x0 == pytest.approx(29.2)
    assert cut[0].x1 == pytest.approx(30.8)
    assert cut[0].r_out0 == pytest.approx(14.3)


def test_an_axial_bore_raises_the_inner_profile_over_its_depth():
    bore = AxialBore(id="ab", at="start", diameter=8.0, depth=30.0)
    bands = radial_profile(shaft(bore))
    bored = [b for b in bands if b.r_in > 0.0]
    assert [(b.x0, b.x1) for b in bored] == [(0.0, 20.0), (20.0, 30.0)]
    assert {b.r_in for b in bored} == {4.0}


# -- the revolved front view -------------------------------------------------


def test_a_plain_shaft_front_view_is_a_mirrored_silhouette_on_an_axis():
    view = build_view(shaft(), "front")
    assert view.kind == "front"
    axis = roles(view, "center")
    assert len(axis) == 1
    assert axis[0].p1[1] == 0.0 and axis[0].p2[1] == 0.0
    assert axis[0].p1[0] < 0.0 and axis[0].p2[0] > 75.0
    visible = roles(view, "visible")
    # the +Y half-outline is 7 lines (left face, 3 cylinders, 2 steps, right face)
    # and it is mirrored about the axis
    assert len(visible) == 14
    ys = sorted({round(line.p1[1], 6) for line in visible})
    assert ys == [-15.0, -12.5, -10.0, 0.0, 10.0, 12.5, 15.0]
    assert view.bbox == (pytest.approx(-7.5), -15.0, pytest.approx(82.5), 15.0)


def test_a_bore_reaches_the_front_view_as_hidden_lines():
    part = RevolvedPart(name="sleeve", segments=(Segment(30.0, 40.0, 20.0),))
    view = build_view(part, "front")
    hidden = roles(view, "hidden")
    assert len(hidden) == 2
    assert {round(line.p1[1], 6) for line in hidden} == {10.0, -10.0}
    assert hidden[0].p1[0] == 0.0 and hidden[0].p2[0] == 30.0


def test_a_chamfer_replaces_the_silhouette_corner_instead_of_being_drawn_over_it():
    chamfer = Chamfer(id="c1", at="start", size=2.0)
    view = build_view(shaft(chamfer), "front")
    upper = [line for line in roles(view, "visible") if line.p1[1] > 0 or line.p2[1] > 0]
    starts = {(round(line.p1[0], 6), round(line.p1[1], 6)) for line in upper}
    assert (0.0, 10.5) in starts
    assert (0.0, 12.5) not in starts
    assert any(line.p1 == (0.0, 10.5) and line.p2 == (2.0, 12.5) for line in upper)


def test_a_fillet_puts_a_tangent_arc_into_the_silhouette():
    fillet = Fillet(id="f1", at="step:0", radius=2.0)
    view = build_view(shaft(fillet), "front")
    arcs = [p for p in view.prims if isinstance(p, Arc)]
    assert len(arcs) == 2  # the fillet and its mirror
    upper = next(a for a in arcs if a.center[1] > 0)
    assert upper.center == (18.0, 14.5)
    assert (upper.radius, upper.start_deg, upper.end_deg) == (2.0, 270.0, 360.0)


def test_an_axisymmetric_feature_is_mirrored_and_a_keyway_is_not():
    groove = RetainingGroove(id="rg", x=30.0, d=30.0, m=1.6, d2=28.6)
    keyway = Keyway(id="kw", segment=1, length=20.0, offset=5.0)
    view = build_view(shaft(groove, keyway), "front")
    groove_lines = [
        line for line in view.prims if isinstance(line, Line) and abs(abs(line.p1[1]) - 14.3) < 1e-6
    ]
    assert {line.p1[1] > 0 for line in groove_lines} == {True, False}
    floor = 15.0 - 4.0  # DIN 6885 depth_shaft for a 30 mm shaft
    keyway_lines = [
        line for line in view.prims if isinstance(line, Line) and abs(line.p1[1] - floor) < 1e-6
    ]
    assert keyway_lines and all(line.p1[1] > 0 for line in keyway_lines)


# -- the revolved end view ---------------------------------------------------


def test_the_end_view_marks_a_diameter_behind_a_larger_one_as_hidden():
    view = build_view(shaft(), "side", side="right")
    circles = [p for p in view.prims if isinstance(p, Circle)]
    radii = {(round(c.radius, 6), c.role) for c in circles}
    assert (10.0, "visible") in radii  # the near end
    assert (15.0, "hidden") in radii  # the middle step, behind it
    assert (12.5, "hidden") not in radii  # smaller than 15, never seen
    assert len([p for p in view.prims if getattr(p, "role", None) == "center"]) == 2


def test_looking_from_the_left_reverses_which_circle_is_visible():
    view = build_view(shaft(), "side", side="left")
    circles = [p for p in view.prims if isinstance(p, Circle)]
    radii = {(round(c.radius, 6), c.role) for c in circles}
    assert (12.5, "visible") in radii
    assert (15.0, "hidden") in radii


def test_a_revolved_top_view_is_refused_because_it_repeats_the_front():
    with pytest.raises(ValueError, match="identical to its front view"):
        build_view(shaft(), "top")


# -- the prismatic views -----------------------------------------------------


def test_a_prismatic_front_view_is_the_closed_outline():
    view = build_view(plate(), "front")
    lines = roles(view, "visible")
    assert len(lines) == 4
    assert view.bbox == (0.0, 0.0, 80.0, 50.0)


def test_a_corner_fillet_replaces_an_outline_vertex_with_an_arc():
    view = build_view(plate(CornerFillet(id="cf", vertex=1, radius=10.0)), "front")
    arcs = [p for p in view.prims if isinstance(p, Arc)]
    # the substituted arc carries role "visible" too, so the straight edges are
    # counted by type: 4 outline edges remain, two of them shortened.
    lines = [p for p in roles(view, "visible") if isinstance(p, Line)]
    assert len(arcs) == 1
    assert arcs[0].center == (pytest.approx(70.0), pytest.approx(10.0))
    assert len(lines) == 4
    assert not any(abs(line.p1[0] - 80.0) < EPS and abs(line.p1[1]) < EPS for line in lines)


def test_a_prismatic_side_view_is_the_thickness_rectangle_with_a_hidden_line_per_hole():
    hole = Hole(id="h1", x=20.0, y=25.0, diameter=10.0)
    view = build_view(plate(hole), "side")
    visible = roles(view, "visible")
    hidden = roles(view, "hidden")
    assert len(visible) == 4
    assert view.bbox == (0.0, 0.0, 50.0, 8.0)
    assert len(hidden) == 2
    assert {round(line.p1[0], 6) for line in hidden} == {20.0, 30.0}


def test_a_prismatic_top_view_projects_the_other_axis():
    hole = Hole(id="h1", x=20.0, y=25.0, diameter=10.0)
    view = build_view(plate(hole), "top")
    assert view.bbox == (0.0, 0.0, 80.0, 8.0)
    hidden = roles(view, "hidden")
    assert {round(line.p1[0], 6) for line in hidden} == {15.0, 25.0}


# -- the honesty rule --------------------------------------------------------


def test_a_feature_that_projects_into_the_view_is_not_reported_as_omitted():
    side = build_view(shaft(Chamfer(id="c1", at="start", size=2.0)), "side")
    assert side.omitted == ()
    front = build_view(shaft(CrossHole(id="xh", x=30.0, diameter=6.0)), "front")
    assert front.omitted == ()
    bore = build_view(shaft(AxialBore(id="ab", at="start", diameter=8.0, depth=12.0)), "front")
    assert bore.omitted == ()


def test_a_bend_line_is_omitted_from_a_side_view_with_a_reason():
    from engineering.mech.features import BendLine

    bend = BendLine(id="b1", p1=(0.0, 25.0), p2=(80.0, 25.0), angle=90.0, inside_radius=2.0)
    view = build_view(plate(bend), "side")
    assert len(view.omitted) == 1
    assert view.omitted[0]["feature"] == "b1"
    assert "side" in view.omitted[0]["reason"]


def test_the_view_collects_the_dimension_intents_of_its_own_features():
    keyway = Keyway(id="kw", segment=1, length=20.0)
    view = build_view(shaft(keyway), "front")
    assert [intent.feature for intent in view.dims] == ["kw"]


def test_an_unknown_view_kind_is_refused_by_name():
    with pytest.raises(ValueError, match="front"):
        build_view(shaft(), "isometric")


# -- projection layout -------------------------------------------------------


def test_first_angle_puts_the_right_hand_view_on_the_left_and_the_top_below():
    front = build_view(shaft(), "front")
    end = build_view(shaft(), "side")
    left = layout_origin(front, "side", side="right", projection="first", child=end)
    width = end.bbox[2] - end.bbox[0]
    height = end.bbox[3] - end.bbox[1]
    assert left[0] == pytest.approx(front.bbox[0] - 20.0 - width)
    assert left[1] == pytest.approx((front.bbox[1] + front.bbox[3]) / 2.0 - height / 2.0)
    below = layout_origin(front, "top", projection="first", child=end)
    assert below[1] == pytest.approx(front.bbox[1] - 20.0 - height)


def test_third_angle_flips_both_placements():
    front = build_view(shaft(), "front")
    end = build_view(shaft(), "side")
    right = layout_origin(front, "side", side="right", projection="third", child=end)
    assert right[0] == pytest.approx(front.bbox[2] + 20.0)
    above = layout_origin(front, "top", projection="third", child=end)
    assert above[1] == pytest.approx(front.bbox[3] + 20.0)


def test_without_a_child_the_layout_assumes_the_parent_size():
    front = build_view(shaft(), "front")
    width = front.bbox[2] - front.bbox[0]
    left = layout_origin(front, "side", side="right", projection="first")
    assert left[0] == pytest.approx(front.bbox[0] - 20.0 - width)


def test_an_unknown_projection_is_refused():
    front = build_view(shaft(), "front")
    with pytest.raises(ValueError, match="first"):
        layout_origin(front, "side", projection="second")
