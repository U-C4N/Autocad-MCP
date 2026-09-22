"""Every feature of spec section 5: validation, emitted primitives, dimensions.

Geometry is asserted as emitted primitive tuples - counts, roles, coordinates
to 1e-9 - never as a screenshot. The two features with a real table
(`Keyway` -> DIN 6885 via engineering.keyway, `GearTeeth` -> the involute
generator in engineering.gear) are pinned against those modules rather than
against numbers restated here; the four not-transcribed tables are exercised
through their refusals.
"""

from __future__ import annotations

import math

import pytest

from engineering.gear import generate_full_gear_outline
from engineering.keyway import keyway_dimensions
from engineering.mech.features import (
    FEATURE_KINDS,
    AxialBore,
    BendLine,
    CentreHole,
    Chamfer,
    CornerChamfer,
    CornerFillet,
    CrossHole,
    Fillet,
    GearTeeth,
    Hole,
    HoleCircle,
    HolePattern,
    Keyway,
    ORingGroove,
    Pocket,
    RetainingGroove,
    Slot,
    Thread,
    Undercut,
    feature_from_dict,
    mirror_about_x,
)
from engineering.mech.part import PrismaticPart, RevolvedPart, Segment, build_part
from engineering.mech.primitives import Arc, Circle, Line, Poly, Text, bbox

CTX: dict = {"side": "right", "scale": 1.0, "projection": "first"}


def shaft(*features) -> RevolvedPart:
    return RevolvedPart(
        name="shaft",
        segments=(Segment(20.0, 25.0), Segment(40.0, 30.0, 10.0), Segment(15.0, 20.0)),
        features=tuple(features),
    )


def plate(*features) -> PrismaticPart:
    return PrismaticPart(
        name="plate",
        outline=((0.0, 0.0), (80.0, 0.0), (80.0, 50.0), (0.0, 50.0)),
        thickness=8.0,
        features=tuple(features),
    )


def test_the_catalogue_is_the_nineteen_kinds_spec_section_5_names():
    assert FEATURE_KINDS == (
        "axial_bore",
        "bend_line",
        "centre_hole",
        "chamfer",
        "corner_chamfer",
        "corner_fillet",
        "cross_hole",
        "fillet",
        "gear_teeth",
        "hole",
        "hole_circle",
        "hole_pattern",
        "keyway",
        "oring_groove",
        "pocket",
        "retaining_groove",
        "slot",
        "thread",
        "undercut",
    )


def test_feature_from_dict_round_trips_every_kind_it_builds():
    payload = {"kind": "chamfer", "id": "c1", "at": "start", "size": 2.0, "angle": 45.0}
    feature = feature_from_dict(payload)
    assert isinstance(feature, Chamfer)
    assert feature.to_dict() == payload


def test_feature_from_dict_refuses_an_unknown_kind_and_an_unknown_key():
    with pytest.raises(ValueError, match="unknown feature"):
        feature_from_dict({"kind": "spline", "id": "s"})
    with pytest.raises(ValueError, match="unknown key"):
        feature_from_dict({"kind": "chamfer", "id": "c", "at": "start", "size": 1.0, "deg": 30})


def test_mirror_about_x_flips_every_primitive_type():
    line, arc, circle, poly, text = mirror_about_x(
        [
            Line((0.0, 5.0), (10.0, 5.0)),
            Arc((0.0, 4.0), 2.0, 30.0, 90.0),
            Circle((1.0, 3.0), 2.0),
            Poly(((0.0, 1.0), (2.0, 3.0)), True),
            Text((1.0, 2.0), "A"),
        ]
    )
    assert line.p1 == (0.0, -5.0) and line.p2 == (10.0, -5.0)
    assert arc.center == (0.0, -4.0)
    assert (arc.start_deg, arc.end_deg) == (270.0, 330.0)
    assert circle.center == (1.0, -3.0)
    assert poly.points == ((0.0, -1.0), (2.0, -3.0))
    assert text.at == (1.0, -2.0)


# -- chamfer and fillet ------------------------------------------------------


def test_a_45_degree_chamfer_at_the_start_replaces_the_corner_with_equal_legs():
    feature = Chamfer(id="c1", at="start", size=2.0)
    part = shaft(feature)
    edit = feature.corner_edit(part)
    assert edit["corner"] == (0.0, 12.5)
    assert edit["before"] == (0.0, 10.5)
    assert edit["after"] == (2.0, 12.5)
    assert edit["arc"] is None
    assert feature.removal_box(part) == (0.0, 10.5, 2.0, 12.5)


def test_a_30_degree_chamfer_at_the_end_cuts_toward_minus_x():
    """The two replacement points sit on the two surfaces the corner joined.

    The silhouette runs left to right, so ``before`` is the point left on the
    cylinder (x = 75 - size, r unchanged) and ``after`` is the point left on
    the end face (x = 75, r reduced by the radial leg). Neither of them is the
    corner itself.
    """
    feature = Chamfer(id="c2", at="end", size=3.0, angle=30.0)
    part = shaft(feature)
    edit = feature.corner_edit(part)
    radial = 3.0 * math.tan(math.radians(30.0))
    assert edit["corner"] == (75.0, 10.0)
    assert edit["before"][0] == pytest.approx(72.0)
    assert edit["before"][1] == pytest.approx(10.0)
    assert edit["after"][0] == pytest.approx(75.0)
    assert edit["after"][1] == pytest.approx(10.0 - radial)
    assert edit["arc"] is None


def test_a_chamfer_shows_as_an_extra_circle_in_the_end_view():
    feature = Chamfer(id="c1", at="end", size=2.0)
    part = shaft(feature)
    (circle,) = feature.prims(part, "side", CTX)
    assert isinstance(circle, Circle)
    assert circle.radius == pytest.approx(8.0)


def test_a_chamfer_bigger_than_the_step_is_refused_by_name():
    feature = Chamfer(id="c1", at="start", size=40.0)
    with pytest.raises(ValueError, match="c1|size"):
        feature.validate(shaft(feature))


def test_a_chamfer_whose_axial_leg_outruns_its_segment_is_refused_by_name():
    """The radial guard only bites near 45 deg; a shallow lead-in slips past it.

    The end segment of `shaft()` is 15 mm long at r = 10. A 30 mm chamfer at
    10 deg takes only 5.29 mm off the radius, so `radial >= radius` is happy,
    and the old part-length bound (75 mm) was happy too - yet `before` landed
    at x = 45, two segments away, anchored at a radius that segment does not
    have.
    """
    feature = Chamfer(id="c1", at="end", size=30.0, angle=10.0)
    with pytest.raises(ValueError) as excinfo:
        feature.validate(shaft(feature))
    message = str(excinfo.value)
    assert "30" in message and "15" in message and "segment 2" in message


def test_a_shallow_chamfer_that_fits_inside_its_segment_is_still_accepted():
    """The new bound refuses only what leaves the segment; 14 < 15 mm stays."""
    feature = Chamfer(id="c1", at="end", size=14.0, angle=10.0)
    part = shaft(feature)
    feature.validate(part)
    edit = feature.corner_edit(part)
    assert edit["before"][0] == pytest.approx(61.0)
    assert edit["before"][0] >= 60.0  # inside segment 2, which spans x 60..75


def test_a_chamfer_at_a_step_is_bounded_by_the_segment_it_cuts_into():
    """`shaft()` steps UP at x = 20, so the chamfer cuts into segment 1 (40 mm).

    A 45 mm leg outruns that segment even though the part is 75 mm long.
    """
    feature = Chamfer(id="c1", at="step:0", size=45.0, angle=5.0)
    with pytest.raises(ValueError) as excinfo:
        feature.validate(shaft(feature))
    assert "segment 1" in str(excinfo.value) and "40" in str(excinfo.value)


def test_a_fillet_at_a_step_up_emits_a_tangent_arc_in_the_lower_right_quadrant():
    feature = Fillet(id="f1", at="step:0", radius=2.0)
    part = shaft(feature)
    edit = feature.corner_edit(part)
    assert edit["corner"] == (20.0, 12.5)
    assert edit["before"] == (18.0, 12.5)
    assert edit["after"] == (20.0, 14.5)
    arc = edit["arc"]
    assert isinstance(arc, Arc)
    assert arc.center == (18.0, 14.5)
    assert (arc.radius, arc.start_deg, arc.end_deg) == (2.0, 270.0, 360.0)


def test_a_fillet_at_a_step_down_puts_the_arc_in_the_lower_left_quadrant():
    feature = Fillet(id="f2", at="step:1", radius=1.5)
    part = shaft(feature)
    edit = feature.corner_edit(part)
    assert edit["corner"] == (60.0, 10.0)
    assert edit["before"] == (60.0, 11.5)
    assert edit["after"] == (61.5, 10.0)
    assert edit["arc"].center == (61.5, 11.5)
    assert (edit["arc"].start_deg, edit["arc"].end_deg) == (180.0, 270.0)


def test_a_fillet_is_only_defined_at_a_step():
    feature = Fillet(id="f3", at="start", radius=1.0)
    with pytest.raises(ValueError, match="step"):
        feature.validate(shaft(feature))


# -- keyway (DIN 6885 through engineering.keyway) ----------------------------


def test_a_keyway_takes_its_width_and_depth_from_din_6885():
    feature = Keyway(id="kw", segment=1, length=25.0, offset=5.0)
    part = shaft(feature)
    feature.validate(part)
    table = keyway_dimensions(30.0)
    prims = feature.prims(part, "front", CTX)
    assert len(prims) == 3
    assert all(isinstance(p, Line) and p.role == "visible" for p in prims)
    bottom = 15.0 - table["depth_shaft"]
    assert prims[0].p1 == (25.0, 15.0) and prims[0].p2 == (25.0, bottom)
    assert prims[1].p1 == (25.0, bottom) and prims[1].p2 == (50.0, bottom)
    assert prims[2].p1 == (50.0, bottom) and prims[2].p2 == (50.0, 15.0)
    assert feature.removal_box(part) == (25.0, bottom, 50.0, 15.0)


def test_a_keyway_end_view_breaks_the_circle_and_draws_the_notch():
    """The arc is the shaft that SURVIVES the slot, not the cap over its mouth.

    `Arc` is CCW start -> end, so the sweep has to be the long way round: it
    leaves the left notch wall, goes over the bottom of the shaft and arrives
    at the right one. Emitting the two angles the other way round draws a short
    chord-like cap straight across the keyway opening - geometrically a legal
    arc, which is why only the sweep and the endpoints catch it. Measured on
    AutoCAD 2026: the 30.93 deg arc is 8.098 mm long, the 329.07 deg one is
    86.150 mm, and the full r=15 circle is 94.248 mm.
    """
    feature = Keyway(id="kw", segment=1, length=25.0)
    part = shaft(feature)
    prims = feature.prims(part, "side", CTX)
    arcs = [p for p in prims if isinstance(p, Arc)]
    lines = [p for p in prims if isinstance(p, Line)]
    assert len(arcs) == 1 and len(lines) == 3
    width = keyway_dimensions(30.0)["width"]
    assert lines[1].p1[0] == pytest.approx(-width / 2.0)
    assert lines[1].p2[0] == pytest.approx(width / 2.0)

    arc = arcs[0]
    radius = 15.0
    half = width / 2.0
    top = math.sqrt(radius * radius - half * half)
    # start at the LEFT wall, end at the RIGHT one: everything but the opening.
    assert arc.radius == pytest.approx(radius)
    assert arc.start_deg == pytest.approx(math.degrees(math.atan2(top, -half)))
    assert arc.end_deg == pytest.approx(math.degrees(math.atan2(top, half)))
    sweep = (arc.end_deg - arc.start_deg) % 360.0
    assert sweep == pytest.approx(329.068, abs=1e-3)
    assert math.radians(sweep) * radius == pytest.approx(86.150, abs=1e-3)
    # and the end view therefore spans the whole shaft, not just the notch:
    # full width and full depth, topped by the notch walls because the slot has
    # taken the crown of the circle away.
    assert bbox(prims) == pytest.approx((-radius, -radius, radius, top))


def test_a_keyway_longer_than_its_segment_is_refused_by_name():
    feature = Keyway(id="kw", segment=1, length=50.0)
    with pytest.raises(ValueError) as excinfo:
        feature.validate(shaft(feature))
    assert "40" in str(excinfo.value)


def test_a_keyway_on_a_segment_that_does_not_exist_is_refused_by_range():
    feature = Keyway(id="kw", segment=5, length=10.0)
    with pytest.raises(ValueError) as excinfo:
        feature.validate(shaft(feature))
    assert "segments 0-2" in str(excinfo.value)


def test_a_keyway_outside_the_din_6885_bore_range_is_refused():
    part = RevolvedPart(name="pin", segments=(Segment(20.0, 4.0),))
    feature = Keyway(id="kw", segment=0, length=10.0)
    with pytest.raises(ValueError, match="DIN 6885"):
        feature.validate(part)


# -- thread ------------------------------------------------------------------


def test_an_external_thread_draws_the_iso6410_minor_lines_and_the_limit_line():
    feature = Thread(id="th", segment=2, designation="M20", length=12.0)
    part = shaft(feature)
    feature.validate(part)
    prims = feature.prims(part, "front", CTX)
    assert len(prims) == 3
    assert prims[0].p1 == (60.0, 8.0) and prims[0].p2 == (72.0, 8.0)
    assert prims[2].p1 == (72.0, -10.0) and prims[2].p2 == (72.0, 10.0)


def test_a_thread_whose_diameter_does_not_match_its_segment_is_refused_with_both_numbers():
    feature = Thread(id="th", segment=1, designation="M20", length=10.0)
    with pytest.raises(ValueError) as excinfo:
        feature.validate(shaft(feature))
    message = str(excinfo.value)
    assert "M20" in message and "30" in message


def test_an_internal_thread_enlarges_the_bore_to_the_major_for_the_section_hatch():
    feature = Thread(id="th", segment=0, designation="M10", length=20.0, internal=True)
    part = RevolvedPart(
        name="boss",
        segments=(Segment(30.0, 40.0, 8.0),),  # drilled to the ISO 6410 minor, 0.8 x 10
        features=(feature,),
    )
    feature.validate(part)
    (edit,) = feature.radial_edit(part)
    assert edit == {"kind": "inner", "x0": 0.0, "x1": 20.0, "radius": 5.0}


def test_a_thread_designation_outside_iso_261_is_refused_by_the_table():
    feature = Thread(id="th", segment=2, designation="M7", length=5.0)
    with pytest.raises(ValueError, match="ISO 261"):
        feature.validate(shaft(feature))


# -- the four not-transcribed tables, through their features -----------------


def test_an_undercut_without_an_explicit_profile_is_refused_by_din_509():
    feature = Undercut(id="uc", at="step:0", form="E")
    with pytest.raises(ValueError, match="DIN 509"):
        feature.validate(shaft(feature))


def test_an_undercut_draws_exactly_the_profile_it_is_handed():
    profile = ((0.0, 0.0), (0.5, 0.4), (2.0, 0.4), (2.5, 0.0))
    feature = Undercut(id="uc", at="step:0", form="E", profile=profile)
    part = shaft(feature)
    feature.validate(part)
    (poly,) = feature.prims(part, "front", CTX)
    assert isinstance(poly, Poly) and poly.closed is False
    assert poly.points[0] == (20.0, 12.5)
    assert poly.points[1] == (19.5, 12.1)
    assert poly.points[-1] == (17.5, 12.5)
    assert feature.removal_box(part) == (17.5, 12.1, 20.0, 12.5)


def test_a_retaining_groove_without_explicit_dimensions_is_refused_by_din_471():
    feature = RetainingGroove(id="rg", x=30.0, d=30.0)
    with pytest.raises(ValueError, match="DIN 471"):
        feature.validate(shaft(feature))


def test_a_retaining_groove_with_explicit_dimensions_cuts_the_profile():
    feature = RetainingGroove(id="rg", x=30.0, d=30.0, m=1.6, d2=28.6)
    part = shaft(feature)
    feature.validate(part)
    (edit,) = feature.radial_edit(part)
    assert edit["kind"] == "outer"
    assert edit["radius"] == pytest.approx(14.3)
    assert (edit["x0"], edit["x1"]) == (pytest.approx(29.2), pytest.approx(30.8))
    lines = feature.prims(part, "front", CTX)
    assert len(lines) == 3
    assert lines[1].p1[1] == pytest.approx(14.3)


def test_a_centre_hole_without_explicit_dimensions_is_refused_by_din_332():
    feature = CentreHole(id="ch", at="end", form="A", size=2.5)
    with pytest.raises(ValueError, match="DIN 332"):
        feature.validate(shaft(feature))


def test_a_centre_hole_form_r_is_refused_outright():
    feature = CentreHole(id="ch", at="end", form="R", size=2.5, d1=2.5, d2=5.3, t=5.0)
    with pytest.raises(ValueError, match="form R"):
        feature.validate(shaft(feature))


def test_a_centre_hole_with_explicit_dimensions_draws_a_hidden_60_degree_cone():
    feature = CentreHole(id="ch", at="end", form="A", size=2.5, d1=2.5, d2=5.3, t=5.0)
    part = shaft(feature)
    feature.validate(part)
    prims = feature.prims(part, "front", CTX)
    assert {p.role for p in prims} == {"hidden"}
    cone_depth = (5.3 - 2.5) / 2.0 / math.tan(math.radians(30.0))
    assert prims[0].p1 == (75.0, 2.65)
    assert prims[0].p2[0] == pytest.approx(75.0 - cone_depth)
    assert prims[0].p2[1] == pytest.approx(1.25)


def test_an_oring_groove_without_explicit_dimensions_is_refused_by_iso_3601():
    feature = ORingGroove(id="og", x=30.0, cord=3.53)
    with pytest.raises(ValueError, match="ISO 3601-2"):
        feature.validate(shaft(feature))


def test_an_axial_oring_groove_is_refused_rather_than_drawn_on_the_silhouette():
    feature = ORingGroove(id="og", x=30.0, cord=3.53, housing="axial", b=4.7, h=2.7)
    with pytest.raises(ValueError, match="face groove"):
        feature.validate(shaft(feature))


def test_an_oring_groove_round_trips_because_its_field_is_not_called_kind():
    """``to_dict`` writes the feature kind under ``kind``.

    A field of the same name would overwrite the discriminator - the payload
    would come back as ``{"kind": "radial", ...}`` and ``feature_from_dict``
    would refuse it - so the housing kind is carried by ``housing``.
    """
    feature = ORingGroove(id="og", x=30.0, cord=3.53, b=4.7, h=2.7)
    payload = feature.to_dict()
    assert payload["kind"] == "oring_groove"
    assert payload["housing"] == "radial"
    assert feature_from_dict(payload) == feature


# -- holes, bores, bolt circles, teeth ---------------------------------------


def test_a_cross_hole_draws_two_hidden_lines_through_the_silhouette():
    feature = CrossHole(id="xh", x=30.0, diameter=6.0)
    part = shaft(feature)
    feature.validate(part)
    prims = feature.prims(part, "front", CTX)
    assert len(prims) == 2
    assert {p.role for p in prims} == {"hidden"}
    assert prims[0].p1 == (27.0, -15.0) and prims[0].p2 == (27.0, 15.0)
    assert prims[1].p1 == (33.0, -15.0) and prims[1].p2 == (33.0, 15.0)


def test_an_inclined_cross_hole_is_refused_rather_than_drawn_as_if_radial():
    feature = CrossHole(id="xh", x=30.0, diameter=6.0, angle=45.0)
    with pytest.raises(ValueError, match="angle"):
        feature.validate(shaft(feature))


def test_an_axial_bore_edits_the_radial_profile_and_is_not_flagged_as_omitted():
    feature = AxialBore(id="ab", at="start", diameter=8.0, depth=12.0)
    part = shaft(feature)
    feature.validate(part)
    (edit,) = feature.radial_edit(part)
    assert edit == {"kind": "inner", "x0": 0.0, "x1": 12.0, "radius": 4.0}
    assert feature.removal_box(part) is None


def test_two_axial_bores_from_the_same_end_are_refused_with_both_names():
    a = AxialBore(id="ab1", at="start", diameter=8.0, depth=12.0)
    b = AxialBore(id="ab2", at="start", diameter=6.0, depth=20.0)
    part = shaft(a, b)
    with pytest.raises(ValueError) as excinfo:
        a.validate(part)
    assert "ab1" in str(excinfo.value) and "ab2" in str(excinfo.value)


def test_a_bolt_circle_draws_its_holes_and_its_pitch_circle_in_the_end_view():
    feature = HoleCircle(id="bc", x=10.0, pcd=36.0, count=6, diameter=5.0)
    part = RevolvedPart(name="flange", segments=(Segment(20.0, 60.0),), features=(feature,))
    feature.validate(part)
    prims = feature.prims(part, "side", CTX)
    holes = [p for p in prims if isinstance(p, Circle) and p.role == "visible"]
    pitch = [p for p in prims if isinstance(p, Circle) and p.role == "center"]
    assert len(holes) == 6 and len(pitch) == 1
    assert pitch[0].radius == pytest.approx(18.0)
    assert holes[0].center == (pytest.approx(18.0), pytest.approx(0.0))
    assert holes[1].center[0] == pytest.approx(18.0 * math.cos(math.radians(60.0)))


def test_a_bolt_circle_that_does_not_fit_the_flange_is_refused_by_name():
    feature = HoleCircle(id="bc", x=10.0, pcd=58.0, count=6, diameter=5.0)
    part = RevolvedPart(name="flange", segments=(Segment(20.0, 60.0),), features=(feature,))
    with pytest.raises(ValueError, match="bc|pcd"):
        feature.validate(part)


def test_gear_teeth_reuse_the_repository_involute_generator():
    feature = GearTeeth(id="g1", segment=0, module=2.0, teeth=20)
    part = RevolvedPart(name="pinion", segments=(Segment(15.0, 44.0),), features=(feature,))
    feature.validate(part)
    (poly, pitch) = feature.prims(part, "side", CTX)[:2]
    expected = generate_full_gear_outline(2.0, 20, 20.0, 0.0, "RH", (0.0, 0.0))
    assert poly.points == tuple(expected)
    assert poly.closed is True
    assert pitch.radius == pytest.approx(20.0)


def test_gear_teeth_whose_tip_diameter_disagrees_with_the_segment_are_refused():
    feature = GearTeeth(id="g1", segment=0, module=2.0, teeth=20)
    part = RevolvedPart(name="pinion", segments=(Segment(15.0, 40.0),), features=(feature,))
    with pytest.raises(ValueError) as excinfo:
        feature.validate(part)
    assert "44" in str(excinfo.value) and "40" in str(excinfo.value)


# -- prismatic features ------------------------------------------------------


def test_a_hole_draws_a_circle_in_true_shape_and_hidden_lines_in_the_side_view():
    feature = Hole(id="h1", x=20.0, y=25.0, diameter=10.0)
    part = plate(feature)
    feature.validate(part)
    front = feature.prims(part, "front", CTX)
    circle = next(p for p in front if isinstance(p, Circle))
    assert circle.center == (20.0, 25.0) and circle.radius == 5.0
    assert sum(1 for p in front if isinstance(p, Line) and p.role == "center") == 2
    side = feature.prims(part, "side", CTX)
    assert {p.role for p in side} == {"hidden"}
    # a through hole runs the whole thickness, drawn from the machined face down
    assert side[0].p1 == (20.0, 8.0) and side[0].p2 == (20.0, 0.0)
    top = feature.prims(part, "top", CTX)
    assert top[0].p1 == (15.0, 8.0)


def test_a_blind_hole_and_a_pocket_are_cut_from_the_same_face():
    """Both enter at v = thickness - the face the front view looks at.

    Nothing downstream can catch a disagreement here: two blind features cut
    from opposite faces of the same plate are each a legal drawing on their
    own, `omission()` returns None for both, and the validator sees only
    hidden lines inside the material. So it has to be pinned at the source.
    """
    hole = Hole(id="h1", x=20.0, y=25.0, diameter=6.0, depth=5.0)
    pocket = Pocket(
        id="p1",
        outline=((40.0, 10.0), (70.0, 10.0), (70.0, 40.0), (40.0, 40.0)),
        depth=5.0,
    )
    part = plate(hole, pocket)
    hole.validate(part)
    pocket.validate(part)
    for view in ("side", "top"):
        entry = {round(p.p1[1], 9) for p in hole.prims(part, view, CTX)} | {
            round(p.p2[1], 9) for p in hole.prims(part, view, CTX)
        }
        pocket_entry = {round(p.p1[1], 9) for p in pocket.prims(part, view, CTX)} | {
            round(p.p2[1], 9) for p in pocket.prims(part, view, CTX)
        }
        # the same two v values: the face at 8.0 and a floor 5 mm below it
        assert entry == {8.0, 3.0}
        assert pocket_entry == {8.0, 3.0}
    (left, right, floor) = hole.prims(part, "side", CTX)
    assert left.p1 == (22.0, 8.0) and left.p2 == (22.0, 3.0)
    assert right.p1 == (28.0, 8.0) and right.p2 == (28.0, 3.0)
    assert floor.p1 == (22.0, 3.0) and floor.p2 == (28.0, 3.0)


def test_a_counterbore_is_stepped_down_from_the_machined_face_too():
    feature = Hole(id="h1", x=20.0, y=25.0, diameter=6.0, cbore_d=12.0, cbore_depth=3.0)
    part = plate(feature)
    feature.validate(part)
    # the side view runs u = part Y, so the hole sits at u = 25 and the
    # counterbore walls at 25 +/- 6.
    side = feature.prims(part, "side", CTX)
    cbore = [p for p in side if {19.0, 31.0} & {p.p1[0], p.p2[0]}]
    assert len(cbore) == 3
    assert {round(p.p1[1], 9) for p in cbore} | {round(p.p2[1], 9) for p in cbore} == {8.0, 5.0}
    # and the bore itself still runs the full thickness from that same face
    bore = [p for p in side if {22.0, 28.0} & {p.p1[0], p.p2[0]}]
    assert [(p.p1, p.p2) for p in bore] == [
        ((22.0, 8.0), (22.0, 0.0)),
        ((28.0, 8.0), (28.0, 0.0)),
    ]


def test_a_hole_outside_the_outline_is_refused_by_name():
    feature = Hole(id="h1", x=200.0, y=25.0, diameter=10.0)
    with pytest.raises(ValueError, match="outline"):
        feature.validate(plate(feature))


def test_a_tapped_hole_draws_the_minor_circle_and_a_three_quarter_major_arc():
    feature = Hole(id="h1", x=20.0, y=25.0, diameter=10.0, thread="M10")
    part = plate(feature)
    feature.validate(part)
    front = feature.prims(part, "front", CTX)
    circles = [p for p in front if isinstance(p, Circle)]
    arcs = [p for p in front if isinstance(p, Arc)]
    assert circles[0].radius == pytest.approx(4.0)
    assert len(arcs) == 1 and arcs[0].radius == pytest.approx(5.0)


def test_a_slot_is_an_obround_of_two_lines_and_two_semicircles():
    feature = Slot(id="s1", x=40.0, y=25.0, length=30.0, width=10.0)
    part = plate(feature)
    feature.validate(part)
    prims = feature.prims(part, "front", CTX)
    lines = [p for p in prims if isinstance(p, Line) and p.role == "visible"]
    arcs = [p for p in prims if isinstance(p, Arc)]
    assert len(lines) == 2 and len(arcs) == 2
    assert lines[0].p1 == (pytest.approx(30.0), pytest.approx(30.0))
    assert arcs[0].center == (pytest.approx(50.0), pytest.approx(25.0))
    assert feature.removal_box(part) == (25.0, 20.0, 55.0, 30.0)


def test_a_slot_narrower_than_it_is_long_is_required():
    feature = Slot(id="s1", x=40.0, y=25.0, length=5.0, width=10.0)
    with pytest.raises(ValueError, match="length"):
        feature.validate(plate(feature))


def test_a_pocket_can_declare_itself_unhatched_in_a_longitudinal_section():
    feature = Pocket(
        id="p1",
        outline=((10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)),
        depth=4.0,
        no_section_hatch=True,
    )
    part = plate(feature)
    feature.validate(part)
    (poly,) = feature.prims(part, "front", CTX)
    assert poly.closed is True and poly.role == "visible"
    assert feature.no_section_hatch is True
    assert feature.removal_box(part) == (10.0, 10.0, 30.0, 30.0)


def test_a_pocket_deeper_than_the_plate_is_refused():
    feature = Pocket(
        id="p1",
        outline=((10.0, 10.0), (30.0, 10.0), (30.0, 30.0)),
        depth=20.0,
    )
    with pytest.raises(ValueError, match="thickness"):
        feature.validate(plate(feature))


def test_a_corner_fillet_replaces_a_vertex_with_two_tangent_points_and_an_arc():
    feature = CornerFillet(id="cf", vertex=1, radius=10.0)
    part = plate(feature)
    feature.validate(part)
    edit = feature.outline_edit(part)
    assert edit["vertex"] == 1
    assert edit["before"] == (pytest.approx(70.0), pytest.approx(0.0))
    assert edit["after"] == (pytest.approx(80.0), pytest.approx(10.0))
    assert edit["arc"].center == (pytest.approx(70.0), pytest.approx(10.0))
    assert edit["arc"].radius == pytest.approx(10.0)


def test_a_corner_chamfer_replaces_a_vertex_with_two_points_and_no_arc():
    feature = CornerChamfer(id="cc", vertex=1, size=6.0)
    part = plate(feature)
    feature.validate(part)
    edit = feature.outline_edit(part)
    assert edit["before"] == (pytest.approx(74.0), pytest.approx(0.0))
    assert edit["after"] == (pytest.approx(80.0), pytest.approx(6.0))
    assert edit["arc"] is None


def test_a_corner_radius_larger_than_its_edges_is_refused():
    feature = CornerFillet(id="cf", vertex=1, radius=60.0)
    with pytest.raises(ValueError, match="edge"):
        feature.validate(plate(feature))


def test_a_bend_line_is_representation_only_and_says_so():
    feature = BendLine(id="b1", p1=(0.0, 25.0), p2=(80.0, 25.0), angle=90.0, inside_radius=2.0)
    part = plate(feature)
    feature.validate(part)
    line, text = feature.prims(part, "front", CTX)
    assert isinstance(line, Line) and line.role == "phantom"
    assert isinstance(text, Text)
    assert "R2" in text.text and "90" in text.text
    assert feature.dims(part, "front", CTX) == ()


def test_a_hole_pattern_expands_before_it_reaches_the_part():
    spec = {
        "kind": "prismatic",
        "name": "plate",
        "thickness": 8.0,
        "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
        "features": [
            {
                "kind": "hole_pattern",
                "id": "hp",
                "pattern": "grid",
                "base": {"x": 10.0, "y": 10.0, "diameter": 6.0},
                "count": 3,
                "spacing": 20.0,
                "count_y": 2,
                "spacing_y": 25.0,
            }
        ],
    }
    part = build_part(spec)
    assert len(part.features) == 6
    assert all(isinstance(f, Hole) for f in part.features)
    assert [f.id for f in part.features][:3] == ["hp_0_0", "hp_1_0", "hp_2_0"]
    assert part.features[2].x == pytest.approx(50.0)
    assert part.features[3].y == pytest.approx(35.0)


def test_a_polar_hole_pattern_places_its_holes_on_the_pitch_circle():
    pattern = HolePattern(
        id="hp",
        pattern="polar",
        base={"x": 40.0, "y": 25.0, "diameter": 5.0},
        count=4,
        pcd=30.0,
    )
    holes = pattern.expand()
    assert len(holes) == 4
    assert holes[0].x == pytest.approx(55.0) and holes[0].y == pytest.approx(25.0)
    assert holes[1].x == pytest.approx(40.0) and holes[1].y == pytest.approx(40.0)


def test_a_linear_pattern_without_a_spacing_is_refused():
    pattern = HolePattern(
        id="hp",
        pattern="linear",
        base={"x": 10.0, "y": 10.0, "diameter": 6.0},
        count=3,
    )
    with pytest.raises(ValueError, match="spacing"):
        pattern.expand()


def test_every_feature_declares_its_dimension_intents_against_its_own_id():
    features = (
        Chamfer(id="c1", at="start", size=2.0),
        Keyway(id="kw", segment=1, length=25.0),
        Thread(id="th", segment=2, designation="M20", length=12.0),
        CrossHole(id="xh", x=30.0, diameter=6.0),
    )
    part = shaft(*features)
    for feature in features:
        for intent in feature.dims(part, "front", CTX):
            assert intent.feature == feature.id
