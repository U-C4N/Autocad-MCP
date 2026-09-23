"""The orthographic view engine: silhouette, hidden lines, centre lines, layout.

Everything is asserted as emitted primitive tuples - counts, roles, coordinates
to 1e-9. The radial profile is the single calculation both the silhouette and
(from Task 5) the section cut face come out of, so it is pinned first.
"""

from __future__ import annotations

from collections import Counter

import pytest

from engineering.mech.features import (
    AxialBore,
    Chamfer,
    CornerFillet,
    CrossHole,
    Fillet,
    Hole,
    Keyway,
    ORingGroove,
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


def coincident(view: View) -> dict:
    """Geometries drawn more than once - exactly what ``_check_duplicate_entities`` flags."""
    counts: Counter = Counter()
    for prim in view.prims:
        if isinstance(prim, Line):
            counts[tuple(sorted((prim.p1, prim.p2)))] += 1
        elif isinstance(prim, Circle):
            counts[(prim.center, round(prim.radius, 9))] += 1
    return {key: n for key, n in counts.items() if n > 1}


def test_the_engine_declares_three_orthographic_kinds_and_two_projections():
    # The three orthographic kinds come first; Task 5 appended the two cut kinds
    # (tests/test_mech_sections.py pins the full tuple from the other side).
    assert VIEW_KINDS[:3] == ("front", "side", "top")
    assert VIEW_KINDS == ("front", "side", "top", "section", "detail")
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


@pytest.mark.parametrize(
    "groove",
    [
        RetainingGroove(id="rg", x=30.0, d=30.0, m=1.6, d2=28.6),
        ORingGroove(id="or", x=40.0, cord=3.0, b=4.1, h=2.3),
    ],
    ids=["retaining", "oring"],
)
def test_a_groove_is_the_notch_in_the_silhouette_and_is_not_drawn_a_second_time(groove):
    """The spec's rule is `grooves cut a notch in the silhouette`. The notch is
    the radial profile's; the feature does not redraw it. Two coincident LINEs
    are what ``drawing_critique(focus=None)`` reports as a duplicate, and
    premium rule 7 needs that report empty before ``drawing_finalize``."""
    view = build_view(shaft(groove), "front")
    assert coincident(view) == {}
    # the notch is still there, mirrored, once per side
    floor = {round(abs(line.p1[1]), 6) for line in view.prims if isinstance(line, Line)}
    assert (14.3 in floor) or (12.7 in floor)
    assert view.omitted == ()


def test_an_axial_bore_end_circle_is_drawn_once():
    """``_end_circles`` reads the bore off the same radial profile the feature
    edited, so the feature's own circle would be the second copy of it."""
    view = build_view(shaft(AxialBore(id="ab", at="start", diameter=8.0, depth=30.0)), "side")
    assert coincident(view) == {}
    assert len([c for c in view.prims if isinstance(c, Circle) and abs(c.radius - 4.0) < EPS]) == 1


def test_a_blind_bore_keeps_the_bottom_line_the_profile_cannot_draw():
    """Suppression is `the profile already drew this`, not `this feature edits
    the profile` - the blind bottom is not on the radial profile at all."""
    view = build_view(shaft(AxialBore(id="ab", at="start", diameter=8.0, depth=30.0)), "front")
    bottoms = [
        line
        for line in roles(view, "hidden")
        if isinstance(line, Line) and abs(line.p1[0] - 30.0) < EPS and abs(line.p2[0] - 30.0) < EPS
    ]
    assert len(bottoms) == 1
    assert {round(bottoms[0].p1[1], 6), round(bottoms[0].p2[1], 6)} == {-4.0, 4.0}


def test_a_groove_straddling_a_step_leaves_one_closed_silhouette():
    """The feature's own prims read a single surface radius for a span that has
    two, so drawing them as well as the profile's notch put a groove floor
    through solid material and left the +Y path with four odd-degree nodes.
    An open path has exactly two: its two ends on the axis."""
    part = shaft(RetainingGroove(id="rg", x=20.0, d=30.0, m=1.6, d2=28.6))
    upper = [
        line
        for line in roles(build_view(part, "front"), "visible")
        if isinstance(line, Line) and line.p1[1] >= 0.0 and line.p2[1] >= 0.0
    ]
    degree: Counter = Counter()
    for line in upper:
        degree[(round(line.p1[0], 6), round(line.p1[1], 6))] += 1
        degree[(round(line.p2[0], 6), round(line.p2[1], 6))] += 1
    assert sorted(node for node, n in degree.items() if n % 2) == [(0.0, 0.0), (75.0, 0.0)]
    # nothing is drawn at the groove floor radius outside the band the profile cut
    assert not [
        line
        for line in upper
        if abs(line.p1[1] - 14.3) < EPS and abs(line.p2[1] - 14.3) < EPS and line.p1[0] < 20.0
    ]


# -- the revolved end view ---------------------------------------------------


def test_the_end_view_hides_the_smaller_diameter_and_draws_the_step_face_continuous():
    """Ground truth by ray cast along the axis of the same solid of revolution
    (viewer at +X, probe just inside each edge):

        edge r=10   at x=75 -> first surface at x=75 -> VISIBLE
        edge r=15   at x=60 -> first surface at x=60 -> VISIBLE
        edge r=12.5 at x=20 -> first surface at x=60 -> HIDDEN

    The annular step face at x=60 faces the observer and nothing at projected
    radius 15-eps stands in front of it, so the 30 mm circle is continuous. The
    25 mm circle is the one that is genuinely covered, and it is still *drawn* -
    dashed, not dropped.
    """
    view = build_view(shaft(), "side", side="right")
    circles = [p for p in view.prims if isinstance(p, Circle)]
    radii = {(round(c.radius, 6), c.role) for c in circles}
    assert radii == {(10.0, "visible"), (15.0, "visible"), (12.5, "hidden")}
    assert len([p for p in view.prims if getattr(p, "role", None) == "center"]) == 2


def test_looking_from_the_left_reverses_which_circle_is_visible():
    """From -X the near end is the 25 mm face; the 20 mm end is behind the 30 mm body."""
    view = build_view(shaft(), "side", side="left")
    circles = [p for p in view.prims if isinstance(p, Circle)]
    radii = {(round(c.radius, 6), c.role) for c in circles}
    assert radii == {(12.5, "visible"), (15.0, "visible"), (10.0, "hidden")}


def test_a_feature_circle_at_the_far_end_is_hidden_behind_the_larger_body():
    """A chamfer at x=0 seen from +X sits behind the 30 mm band, so its circle
    is dashed; seen from -X nothing is in front of it and it is continuous."""
    part = shaft(Chamfer(id="c1", at="start", size=2.0))
    right = {
        (round(c.radius, 6), c.role)
        for c in build_view(part, "side", side="right").prims
        if isinstance(c, Circle)
    }
    left = {
        (round(c.radius, 6), c.role)
        for c in build_view(part, "side", side="left").prims
        if isinstance(c, Circle)
    }
    assert (10.5, "hidden") in right
    assert (10.5, "visible") in left


def test_a_bore_that_widens_behind_a_narrower_one_is_hidden():
    """Looking into the narrow end you see the small bore; the wider bore behind
    it is covered by the wall of the narrow one. From the other end, both are
    seen down the widening hole."""
    part = RevolvedPart(
        name="stepped-bore",
        segments=(Segment(20.0, 40.0, 10.0), Segment(20.0, 40.0, 20.0)),
    )
    right = {
        (round(c.radius, 6), c.role)
        for c in build_view(part, "side", side="right").prims
        if isinstance(c, Circle)
    }
    left = {
        (round(c.radius, 6), c.role)
        for c in build_view(part, "side", side="left").prims
        if isinstance(c, Circle)
    }
    assert (10.0, "visible") in right and (5.0, "visible") in right
    assert (5.0, "visible") in left and (10.0, "hidden") in left


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
    # y=10, deliberately OFF the y=25 mirror axis of the 0..50 plate: a hole on
    # the axis cannot tell a right-hand view from a left-hand one.
    hole = Hole(id="h1", x=20.0, y=10.0, diameter=10.0)
    view = build_view(plate(hole), "side")
    visible = roles(view, "visible")
    hidden = roles(view, "hidden")
    assert len(visible) == 4
    assert view.bbox == (0.0, 0.0, 50.0, 8.0)
    assert len(hidden) == 2
    assert {round(line.p1[0], 6) for line in hidden} == {5.0, 15.0}


def test_a_prismatic_left_side_view_is_the_right_view_mirrored():
    """An observer at -X sees screen-right = -Y, so the hole at y=10 in a plate
    spanning y 0..50 lands at u = 50-15 .. 50-5. Accepting ``side='left'`` and
    returning the right-hand view puts every feature on the wrong side of the
    sheet with nothing in the drawing to reveal it."""
    hole = Hole(id="h1", x=20.0, y=10.0, diameter=10.0)
    part = plate(hole)
    right = {
        round(line.p1[0], 6) for line in roles(build_view(part, "side", side="right"), "hidden")
    }
    left = {round(line.p1[0], 6) for line in roles(build_view(part, "side", side="left"), "hidden")}
    assert right == {5.0, 15.0}
    assert left == {35.0, 45.0}
    # the thickness rectangle is its own mirror, so only the features move
    assert build_view(part, "side", side="left").bbox == (0.0, 0.0, 50.0, 8.0)


def test_a_prismatic_top_view_is_always_seen_from_above():
    """``side`` names the end an *end* view is taken from; the top view is not
    one, so it must not move with it."""
    part = plate(Hole(id="h1", x=20.0, y=10.0, diameter=10.0))
    from_right = {
        round(line.p1[0], 6) for line in roles(build_view(part, "top", side="right"), "hidden")
    }
    from_left = {
        round(line.p1[0], 6) for line in roles(build_view(part, "top", side="left"), "hidden")
    }
    assert from_right == from_left == {15.0, 25.0}


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
