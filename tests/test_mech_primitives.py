"""The typed primitive descriptions every pure mechanical module emits.

Geometry is asserted on the emitted tuples to 1e-9 — never on a rendered
drawing. That is what lets the view engine be tested without a backend.
"""

from __future__ import annotations

import pytest

from engineering.mech.primitives import (
    ROLE_LAYER,
    ROLES,
    Arc,
    Circle,
    DimIntent,
    HatchArea,
    Line,
    Poly,
    Text,
    bbox,
    layer_for,
    points_of,
    rotate,
    scale,
    translate,
)

EPS = 1e-9


def test_role_layer_maps_every_role_to_an_engineering_layer():
    assert ROLE_LAYER == {
        "visible": "GEOMETRY",
        "hidden": "HIDDEN",
        "center": "CENTER",
        "phantom": "PHANTOM",
        "hatch": "HATCH",
        "dim": "DIM",
        "text": "TEXT",
    }
    assert ROLES == tuple(ROLE_LAYER)


def test_defaults_match_the_pinned_interface():
    assert Line((0.0, 0.0), (1.0, 0.0)).role == "visible"
    assert Arc((0.0, 0.0), 5.0, 0.0, 90.0).role == "visible"
    assert Circle((0.0, 0.0), 5.0).role == "visible"
    poly = Poly(((0.0, 0.0), (1.0, 0.0)))
    assert poly.closed is False and poly.role == "visible"
    assert HatchArea((((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),)).material == "steel"
    label = Text((0.0, 0.0), "M20")
    assert (label.height, label.rotation, label.role) == (3.5, 0.0, "text")
    intent = DimIntent("linear", (0.0, 0.0), (10.0, 0.0))
    assert intent.text_at is None
    assert intent.text_override is None
    assert intent.fit is None
    assert intent.tol is None
    assert intent.feature == ""


def test_layer_for_uses_the_role_map_and_a_hatch_has_no_role():
    assert layer_for(Line((0.0, 0.0), (1.0, 0.0), "hidden")) == "HIDDEN"
    assert layer_for(Text((0.0, 0.0), "A")) == "TEXT"
    assert layer_for(HatchArea((((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),))) == "HATCH"


def test_arc_bbox_includes_the_quadrant_it_sweeps_through():
    """A 0-180 arc of r=5 reaches y=+5 at 90 deg; an endpoint-only bbox misses it."""
    x0, y0, x1, y1 = bbox([Arc((0.0, 0.0), 5.0, 0.0, 180.0)])
    assert abs(x0 + 5.0) < EPS
    assert abs(x1 - 5.0) < EPS
    assert abs(y0) < EPS
    assert abs(y1 - 5.0) < EPS


def test_circle_bbox_is_the_full_square():
    assert bbox([Circle((10.0, 0.0), 2.0)]) == (8.0, -2.0, 12.0, 2.0)


def test_bbox_of_nothing_is_refused_rather_than_reported_as_zero():
    with pytest.raises(ValueError, match="no primitives"):
        bbox([])


def test_translate_moves_every_primitive_kind():
    prims = (
        Line((0.0, 0.0), (10.0, 0.0)),
        Arc((0.0, 0.0), 5.0, 0.0, 90.0),
        Circle((0.0, 0.0), 5.0),
        Poly(((0.0, 0.0), (1.0, 1.0)), closed=True, role="hidden"),
        HatchArea((((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),), material="cast_iron"),
        Text((0.0, 0.0), "A-A"),
    )
    moved = translate(prims, 3.0, -2.0)
    assert moved[0] == Line((3.0, -2.0), (13.0, -2.0))
    assert moved[1] == Arc((3.0, -2.0), 5.0, 0.0, 90.0)
    assert moved[2] == Circle((3.0, -2.0), 5.0)
    assert moved[3] == Poly(((3.0, -2.0), (4.0, -1.0)), closed=True, role="hidden")
    assert moved[4] == HatchArea((((3.0, -2.0), (4.0, -2.0), (4.0, -1.0)),), material="cast_iron")
    assert moved[5] == Text((3.0, -2.0), "A-A")


def test_rotate_turns_arc_angles_with_the_geometry():
    turned = rotate([Arc((10.0, 0.0), 5.0, 0.0, 90.0)], 90.0)[0]
    assert abs(turned.center[0]) < EPS
    assert abs(turned.center[1] - 10.0) < EPS
    assert abs(turned.start_deg - 90.0) < EPS
    assert abs(turned.end_deg - 180.0) < EPS


def test_rotate_adds_to_a_text_rotation():
    label = rotate([Text((1.0, 0.0), "N", rotation=10.0)], 90.0)[0]
    assert abs(label.at[0]) < EPS
    assert abs(label.at[1] - 1.0) < EPS
    assert abs(label.rotation - 100.0) < EPS


def test_scale_scales_radius_height_and_position_about_a_point():
    prims = scale(
        [Circle((10.0, 0.0), 2.0), Text((10.0, 0.0), "x", height=3.5)],
        2.0,
        about=(0.0, 0.0),
    )
    assert prims[0] == Circle((20.0, 0.0), 4.0)
    assert prims[1].height == 7.0


def test_scale_refuses_a_non_positive_factor():
    with pytest.raises(ValueError, match="scale factor"):
        scale([Circle((0.0, 0.0), 1.0)], 0.0)


def test_points_of_reports_the_text_anchor_only():
    assert points_of(Text((4.0, 5.0), "long label")) == ((4.0, 5.0),)
