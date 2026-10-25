"""engineering/verification/measure.py (B18, B19, B20): every quantity against hand geometry.

The features are written out by hand; every expected value is worked in the
comment beside it, never taken from a generator or from the module under test.
"""

from __future__ import annotations

import math

import pytest

from engineering.verification.measure import (
    ANGLE_EPSILON_DEG,
    RELATIVE_EPSILON,
    default_epsilon,
    evaluate,
)
from engineering.verification.types import (
    Binding,
    FeatureRef,
    ObservedFeature,
    ObservedGeometry,
    Requirement,
)


def _f(handle, kind, approximation=None, **geometry):
    return ObservedFeature(
        handle=handle,
        type=kind,
        space="Model",
        layer="0",
        block_path=(),
        geometry=tuple(geometry.items()),
        approximation=approximation,
    )


def line(handle, a, b):
    return _f(handle, "line", start=a, end=b)


def circle(handle, centre, radius, **extra):
    return _f(handle, "circle", center=centre, radius=radius, **extra)


def arc(handle, centre, radius, a0, a1):
    return _f(handle, "arc", center=centre, radius=radius, start_angle=a0, end_angle=a1)


def point(handle, at):
    return _f(handle, "point", location=at)


def drawing(*features, unit="mm", inferred="same"):
    return ObservedGeometry(
        features=tuple(features),
        declared_unit=unit,
        inferred_unit=unit if inferred == "same" else inferred,
        document_id="doc",
        revision=1,
        source="test",
    )


def _binding(rid, handles, status):
    refs = tuple(
        FeatureRef(
            document_id="doc", revision=1, space="Model", block_path=(), handles=(h,), feature=None
        )
        for h in handles
    )
    return Binding(
        requirement_id=rid,
        status=status,
        refs=refs if status == "bound" else (),
        candidates=refs,
        reason="test",
    )


def req(quantity, *, nominal=None, unit="mm", tol=None, eps=None, **counts):
    target = {} if unit is None else {"unit": unit}
    if nominal is not None:
        target["nominal"] = nominal
    target.update(counts)
    return Requirement(
        id="r",
        quantity=quantity,
        target=tuple(sorted(target.items())),
        features=(),
        provenance="explicit",
        source=(),
        manufacturing_tolerance=None if tol is None else tuple(sorted(tol.items())),
        verification_epsilon=eps,
    )


def measure(requirement, observed, *handles, status="bound"):
    return evaluate(requirement, _binding(requirement.id, handles, status), observed)


def test_point_to_point_distance_is_the_3_4_5_triangle():
    observed = drawing(point("P", (3.0, 4.0)), circle("C", (0.0, 0.0), 1.0))
    m = measure(req("distance", nominal=5.0), observed, "P", "C")
    assert (m.value, m.residual, m.unit, m.method, m.status) == (
        5.0,
        0.0,
        "mm",
        "point_point",
        "pass",
    )
    assert dict(m.detail)["basis"] == "verification_epsilon"


def test_point_to_line_distance_is_the_perpendicular():
    # |(x2-x1)(y1-py) - (x1-px)(y2-y1)| / L = |3*(0+3) - (0-4)*4| / 5 = 25 / 5
    observed = drawing(line("L", (0.0, 0.0), (3.0, 4.0)), point("P", (4.0, -3.0)))
    m = measure(req("distance", nominal=5.0), observed, "P", "L")
    assert m.value == pytest.approx(5.0, abs=1e-9) and m.method == "point_line"
    assert m.status == "pass"


def test_line_to_line_distance_needs_parallel_lines():
    a = line("A", (0.0, 0.0), (10.0, 0.0))
    b = line("B", (0.0, 7.5), (10.0, 7.5))
    slanted = line("S", (0.0, 0.0), (10.0, 1.0))
    observed = drawing(a, b, slanted)
    m = measure(req("distance", nominal=7.5), observed, "A", "B")
    assert m.value == pytest.approx(7.5, abs=1e-9) and m.method == "line_line"
    m = measure(req("distance", nominal=7.5), observed, "A", "S")
    assert m.status == "unknown" and dict(m.detail)["degenerate"] == "lines are not parallel"


def test_the_manufacturing_tolerance_decides_pass_and_fail():
    observed = drawing(line("A", (0.0, 0.0), (10.0, 0.0)), line("B", (0.0, 7.5), (10.0, 7.5)))
    tight = measure(
        req("distance", nominal=7.4, tol={"upper": 0.05, "lower": -0.05}), observed, "A", "B"
    )
    assert tight.status == "fail" and tight.residual == pytest.approx(0.1, abs=1e-9)
    assert dict(tight.detail)["basis"] == "manufacturing_tolerance"
    loose = measure(
        req("distance", nominal=7.4, tol={"upper": 0.2, "lower": -0.2}), observed, "A", "B"
    )
    assert loose.status == "pass"


def test_radius_and_diameter_of_circles_and_arcs():
    observed = drawing(circle("C", (0.0, 0.0), 12.5), arc("A", (0.0, 0.0), 3.0, 0.0, 90.0))
    m = measure(req("radius", nominal=12.5), observed, "C")
    assert (m.value, m.method, m.status) == (12.5, "circle_radius", "pass")
    m = measure(req("diameter", nominal=25.0), observed, "C")
    assert (m.value, m.method, m.status) == (25.0, "circle_diameter", "pass")
    m = measure(req("radius", nominal=3.0), observed, "A")
    assert (m.value, m.method) == (3.0, "arc_radius")


def test_an_ellipse_is_not_a_circle_unless_its_ratio_is_one():
    flat = _f(
        "E",
        "ellipse",
        center=(0.0, 0.0),
        major_axis=(10.0, 0.0),
        ratio=0.5,
        start_param=0.0,
        end_param=2.0 * math.pi,
    )
    round_ = _f(
        "R",
        "ellipse",
        center=(0.0, 0.0),
        major_axis=(6.0, 8.0),
        ratio=1.0,
        start_param=0.0,
        end_param=2.0 * math.pi,
    )
    observed = drawing(flat, round_)
    m = measure(req("radius", nominal=10.0), observed, "E")
    assert m.status == "unsupported" and m.method == "radius_of_ellipse"
    m = measure(req("radius", nominal=10.0), observed, "R")
    assert m.value == pytest.approx(10.0, abs=1e-9)  # |(6, 8)|


def test_two_bound_radii_that_differ_are_ambiguous_never_averaged():
    observed = drawing(circle("A", (0.0, 0.0), 5.0), circle("B", (20.0, 0.0), 5.1))
    m = measure(req("radius", nominal=5.0), observed, "A", "B")
    assert m.status == "ambiguous" and m.value is None


def test_the_angle_between_lines_and_its_supplement():
    observed = drawing(
        line("X", (0.0, 0.0), (1.0, 0.0)), line("S", (0.0, 0.0), (1.0, math.sqrt(3.0)))
    )
    m = measure(req("angle", nominal=60.0, unit="deg"), observed, "X", "S")
    assert m.value == pytest.approx(60.0, abs=1e-9) and m.unit == "deg" and m.status == "pass"
    m = measure(req("angle", nominal=120.0, unit="deg"), observed, "X", "S")
    assert m.value == pytest.approx(120.0, abs=1e-9) and m.status == "pass"


def test_an_arc_s_sweep_is_an_angle():
    observed = drawing(arc("A", (0.0, 0.0), 5.0, 30.0, 300.0))
    m = measure(req("angle", nominal=270.0, unit="deg"), observed, "A")
    assert (m.value, m.method, m.status) == (270.0, "arc_sweep", "pass")


def test_concentric_and_coincident():
    observed = drawing(
        circle("A", (5.0, 5.0), 3.0),
        circle("B", (5.0, 5.0), 1.0),
        circle("C", (5.0, 5.002), 1.0),
        point("P", (5.0, 5.0)),
    )
    m = measure(req("concentric"), observed, "A", "B")
    assert (m.value, m.method, m.status) == (0.0, "centre_distance", "pass")
    m = measure(req("concentric"), observed, "A", "C")
    assert m.value == pytest.approx(0.002, abs=1e-9) and m.status == "fail"
    m = measure(req("concentric", tol={"upper": 0.01}), observed, "A", "C")
    assert m.status == "pass"
    m = measure(req("coincident"), observed, "P", "A")
    assert (m.value, m.method, m.status) == (0.0, "point_distance", "pass")


def test_parallel_perpendicular_horizontal_vertical():
    one_degree = line("D", (0.0, 0.0), (10.0, 10.0 * math.tan(math.radians(1.0))))
    half_degree = line("H", (0.0, 0.0), (10.0, 10.0 * math.tan(math.radians(0.5))))
    observed = drawing(
        line("A", (0.0, 0.0), (10.0, 0.0)),
        line("B", (0.0, 7.5), (10.0, 7.5)),
        line("U", (0.0, 0.0), (0.0, 5.0)),
        line("V", (3.0, 0.0), (3.0, 9.0)),
        one_degree,
        half_degree,
    )
    assert measure(req("parallel", unit=None), observed, "A", "B").status == "pass"
    m = measure(req("parallel", unit=None), observed, "A", "D")
    assert m.value == pytest.approx(1.0, abs=1e-9) and m.status == "fail"
    m = measure(req("perpendicular", unit=None), observed, "A", "U")
    assert m.value == pytest.approx(0.0, abs=1e-12) and m.status == "pass"
    assert measure(req("horizontal", unit=None), observed, "A").status == "pass"
    m = measure(req("horizontal", unit=None), observed, "H")
    assert m.value == pytest.approx(0.5, abs=1e-9) and m.status == "fail"
    assert measure(req("vertical", unit=None), observed, "V").status == "pass"


def test_line_circle_tangency_exact_near_and_secant():
    observed = drawing(
        circle("C", (0.0, 0.0), 10.0),
        line("T", (-20.0, 10.0), (20.0, 10.0)),  # distance 10 = r
        line("N", (-20.0, 10.000001), (20.0, 10.000001)),  # misses by 1e-6
        line("S", (-20.0, 9.999), (20.0, 9.999)),  # cuts by 1e-3
    )
    # default epsilon: 1e-9 x the largest coordinate (20) = 2e-8
    assert default_epsilon(observed) == pytest.approx(2e-8, abs=1e-20)
    m = measure(req("tangent"), observed, "T", "C")
    assert (m.method, m.status) == ("line_circle_tangency", "pass")
    m = measure(req("tangent"), observed, "N", "C")
    assert m.value == pytest.approx(1e-6, abs=1e-12) and m.status == "fail"
    assert measure(req("tangent", tol={"upper": 1e-5}), observed, "N", "C").status == "pass"
    m = measure(req("tangent"), observed, "S", "C")
    assert m.value == pytest.approx(1e-3, abs=1e-12) and m.status == "fail"


def test_circle_circle_tangency_external_internal_near_and_coincident():
    observed = drawing(
        circle("A", (0.0, 0.0), 3.0),
        circle("B", (5.0, 0.0), 2.0),  # d = 5 = 3 + 2: external
        circle("O", (0.0, 0.0), 5.0),
        circle("I", (2.0, 0.0), 3.0),  # d = 2 = 5 - 3: internal
        circle("N", (5.000001, 0.0), 2.0),  # misses by 1e-6
        circle("Z", (0.0, 0.0), 3.0),  # A again
    )
    assert measure(req("tangent"), observed, "A", "B").status == "pass"
    assert measure(req("tangent"), observed, "O", "I").status == "pass"
    m = measure(req("tangent"), observed, "A", "N")
    assert m.value == pytest.approx(1e-6, abs=1e-12) and m.status == "fail"
    m = measure(req("tangent"), observed, "A", "Z")
    assert m.status == "unknown" and dict(m.detail)["degenerate"] == "coincident circles"


def test_arc_continuity_smooth_and_kinked():
    smooth = drawing(line("L", (0.0, 0.0), (10.0, 0.0)), arc("A", (10.0, 5.0), 5.0, 270.0, 90.0))
    m = measure(req("tangent"), smooth, "L", "A")
    assert m.method == "arc_continuity" and m.value == pytest.approx(0.0, abs=1e-9)
    assert m.status == "pass"
    kinked = drawing(line("L", (0.0, -1.0), (10.0, 0.0)), arc("A", (10.0, 5.0), 5.0, 270.0, 90.0))
    m = measure(req("tangent"), kinked, "L", "A")
    # the arc's tangent at (10, 0) is +x; the line runs along (10, 1)
    assert m.value == pytest.approx(math.degrees(math.atan2(1.0, 10.0)), abs=1e-9)
    assert m.status == "fail"


def test_counts_exact_at_least_at_most_and_absence():
    observed = drawing(*(circle(f"C{i}", (10.0 * i, 0.0), 2.0) for i in range(3)))
    handles = ("C0", "C1", "C2")
    m = measure(req("count", unit=None, exact=3), observed, *handles)
    assert (m.value, m.residual, m.unit, m.status) == (3.0, 0.0, "count", "pass")
    assert measure(req("count", unit=None, min=4), observed, *handles).status == "fail"
    assert measure(req("count", unit=None, max=3), observed, *handles).status == "pass"
    assert measure(req("count", unit=None, min=1, max=2), observed, *handles).status == "fail"
    assert measure(req("absence", unit=None), observed).status == "pass"
    m = measure(req("absence", unit=None), observed, "C0")
    assert (m.value, m.status) == (1.0, "fail")


def test_a_zero_length_line_is_degenerate():
    observed = drawing(line("Z", (1.0, 1.0), (1.0, 1.0)), point("P", (0.0, 0.0)))
    m = measure(req("horizontal", unit=None), observed, "Z")
    assert m.status == "unknown" and dict(m.detail)["degenerate"] == "zero-length line"
    m = measure(req("distance", nominal=1.0), observed, "P", "Z")
    assert m.status == "unknown"


def test_splines_and_tilted_planes_are_unsupported():
    observed = drawing(
        _f(
            "S",
            "spline",
            degree=3,
            control_points=((0.0, 0.0), (1.0, 1.0)),
            fit_points=(),
            closed=False,
        ),
        circle("T", (0.0, 0.0), 5.0, approximation="tilted_plane"),
    )
    m = measure(req("radius", nominal=5.0), observed, "T")
    assert (m.status, m.method, m.value) == ("unsupported", "tilted_plane", None)
    m = measure(req("count", unit=None, exact=1), observed, "S")
    assert m.status == "pass"  # a count of splines is a count
    m = measure(req("distance", nominal=1.0), observed, "S", "T")
    assert m.status == "unsupported"


def test_an_unbound_requirement_is_never_a_pass():
    observed = drawing(circle("A", (0.0, 0.0), 5.0), circle("B", (20.0, 0.0), 5.0))
    m = measure(req("radius", nominal=5.0), observed, "A", "B", status="ambiguous")
    assert (m.status, m.value, m.method) == ("ambiguous", None, "binding")
    m = measure(req("radius", nominal=5.0), observed, status="unknown")
    assert (m.status, m.value) == ("unknown", None)


def test_lengths_convert_between_the_drawing_and_the_requirement_unit():
    inches = drawing(circle("C", (0.0, 0.0), 1.0), unit="in")
    m = measure(req("radius", nominal=25.4, unit="mm"), inches, "C")
    assert m.value == pytest.approx(25.4, abs=1e-12) and m.unit == "mm" and m.status == "pass"
    m = measure(req("radius", nominal=1.0, unit="in"), inches, "C")
    assert m.value == 1.0 and m.status == "pass"
    unitless = drawing(circle("C", (0.0, 0.0), 1.0), unit=None)
    m = measure(req("radius", nominal=1.0, unit="mm"), unitless, "C")
    assert m.status == "unknown" and m.method == "units"


def test_the_declared_unit_wins_over_the_inferred_one_and_the_detail_says_so():
    # declared mm, inferred in (a plan heuristic on a mechanical part): R10 is
    # 10 mm, never 254 mm; the inference is recorded, not applied
    observed = drawing(circle("C", (0.0, 0.0), 10.0), unit="mm", inferred="in")
    m = measure(req("radius", nominal=10.0, unit="mm"), observed, "C")
    assert (m.value, m.unit, m.status) == (10.0, "mm", "pass")
    detail = dict(m.detail)
    assert (detail["drawing_unit"], detail["unit_source"], detail["inferred_unit"]) == (
        "mm",
        "declared",
        "in",
    )
    # agreement records no inferred unit
    agreed = measure(
        req("radius", nominal=10.0, unit="mm"), drawing(circle("C", (0, 0), 10.0)), "C"
    )
    assert "inferred_unit" not in dict(agreed.detail)
    assert dict(agreed.detail)["unit_source"] == "declared"


def test_a_unitless_drawing_is_measured_in_its_inferred_unit_and_says_so():
    observed = drawing(circle("C", (0.0, 0.0), 1.0), unit=None, inferred="in")
    m = measure(req("radius", nominal=25.4, unit="mm"), observed, "C")
    assert m.value == pytest.approx(25.4, abs=1e-12) and m.status == "pass"
    detail = dict(m.detail)
    assert (detail["drawing_unit"], detail["unit_source"]) == ("in", "inferred")


def test_a_declared_unit_without_an_exact_factor_is_unknown_never_the_inferred_one():
    observed = drawing(circle("C", (0.0, 0.0), 1.0), unit="yd", inferred="mm")
    m = measure(req("radius", nominal=1.0, unit="mm"), observed, "C")
    assert (m.status, m.method) == ("unknown", "units")
    assert "'yd'" in dict(m.detail)["units"]
    assert dict(m.detail)["unit_source"] == "declared"


def test_a_requirement_s_own_epsilon_overrides_the_default():
    observed = drawing(line("A", (0.0, 0.0), (10.0, 0.0)), line("B", (0.0, 7.5), (10.0, 7.5)))
    # residual 1e-7 against the default 1e-9 x 10 = 1e-8: fail; against 1e-6: pass
    assert measure(req("distance", nominal=7.5000001), observed, "A", "B").status == "fail"
    assert (
        measure(req("distance", nominal=7.5000001, eps=1e-6), observed, "A", "B").status == "pass"
    )


def test_default_epsilon_scales_with_the_drawing():
    assert default_epsilon(drawing(point("P", (0.5, 0.25)))) == RELATIVE_EPSILON
    big = drawing(line("L", (-1000.0, 0.0), (10.0, 0.0)))
    assert default_epsilon(big) == pytest.approx(1000.0 * RELATIVE_EPSILON, abs=1e-18)
    assert ANGLE_EPSILON_DEG == 1e-7


# -- requirements in the shape the parser produces (B18-B20 against Task 11) -----


def _parsed(units, **item):
    from engineering.verification.requirements import parse_requirements

    item.setdefault("id", "r")
    item.setdefault("provenance", "explicit")
    return parse_requirements({"units": units, "items": [item]}).requirements[0]


def _handles(*names):
    return [{"kind": "handle", "handle": name} for name in names]


def test_an_angle_in_radians_is_measured_in_radians():
    # the parser accepts "rad"; 60 degrees is pi / 3 rad
    observed = drawing(
        line("A", (0.0, 0.0), (1.0, 0.0)), line("B", (0.0, 0.0), (1.0, math.sqrt(3.0)))
    )
    r = _parsed(
        "mm",
        quantity="angle",
        target={"nominal": math.pi / 3.0, "unit": "rad"},
        features=_handles("A", "B"),
    )
    m = measure(r, observed, "A", "B")
    assert m.unit == "rad" and m.value == pytest.approx(math.pi / 3.0, abs=1e-12)
    assert m.status == "pass"
    # the supplement, 2 pi / 3, is the same pair of lines
    r = _parsed(
        "mm",
        quantity="angle",
        target={"nominal": 2.0 * math.pi / 3.0, "unit": "rad"},
        features=_handles("A", "B"),
    )
    assert measure(r, observed, "A", "B").status == "pass"
    # 60 read as radians is not 60 degrees
    r = _parsed(
        "mm", quantity="angle", target={"nominal": 60.0, "unit": "rad"}, features=_handles("A", "B")
    )
    assert measure(r, observed, "A", "B").status == "fail"


def test_an_arc_s_sweep_in_radians():
    observed = drawing(arc("A", (0.0, 0.0), 5.0, 0.0, 90.0))
    r = _parsed(
        "mm",
        quantity="angle",
        target={"nominal": math.pi / 2.0, "unit": "rad"},
        features=_handles("A"),
    )
    m = measure(r, observed, "A")
    assert (m.unit, m.method, m.status) == ("rad", "arc_sweep", "pass")
    assert m.value == pytest.approx(math.pi / 2.0, abs=1e-12)


def test_a_parsed_constraint_reads_its_tolerance_in_the_set_s_unit():
    # the parser gives a constraint no target, so no unit: the set's unit is passed
    # in. Centres 0.002 in apart on an inch drawing = 0.0508 mm.
    inches = drawing(circle("A", (5.0, 5.0), 3.0), circle("B", (5.0, 5.002), 1.0), unit="in")
    r = _parsed(
        "mm",
        quantity="concentric",
        features=_handles("A", "B"),
        manufacturing_tolerance={"upper": 0.06, "lower": 0.0, "source": "note 3"},
    )
    assert dict(r.target) == {}
    m = evaluate(r, _binding("r", ("A", "B"), "bound"), inches, units="mm")
    assert m.unit == "mm" and m.value == pytest.approx(0.0508, abs=1e-12)
    assert m.status == "pass"
    tight = _parsed(
        "mm",
        quantity="concentric",
        features=_handles("A", "B"),
        manufacturing_tolerance={"upper": 0.05, "lower": 0.0, "source": "note 3"},
    )
    assert evaluate(tight, _binding("r", ("A", "B"), "bound"), inches, units="mm").status == "fail"
    # without the set's unit the drawing's own unit is reported, never a mm label on inches
    m = evaluate(r, _binding("r", ("A", "B"), "bound"), inches)
    assert m.unit == "in" and m.value == pytest.approx(0.002, abs=1e-12)


def test_a_fit_without_a_band_is_checked_at_the_nominal_and_says_so():
    # an ISO 286 fit is not resolved into a band here: the drawn size is held to
    # its nominal within the verification epsilon, and the detail names the fit
    observed = drawing(circle("C", (0.0, 0.0), 10.0), circle("D", (30.0, 0.0), 10.01))
    r = _parsed(
        "mm",
        quantity="diameter",
        target={"nominal": 20.0},
        features=_handles("C"),
        manufacturing_tolerance={"fit": "H7", "source": "drawing note"},
    )
    m = measure(r, observed, "C")
    assert m.status == "pass"
    assert dict(m.detail)["basis"] == "verification_epsilon" and dict(m.detail)["fit"] == "H7"
    assert measure(r, observed, "D").status == "fail"  # 20.02 is not 20 at epsilon


def test_a_bound_set_with_no_feature_is_unknown_never_a_crash():
    observed = drawing(circle("C", (0.0, 0.0), 1.0))
    m = measure(req("radius", nominal=1.0), observed)  # bound, but no refs
    assert (m.status, m.value, m.method) == ("unknown", None, "binding")


def test_a_length_requirement_in_an_angle_unit_is_unknown_and_says_why():
    observed = drawing(circle("C", (0.0, 0.0), 1.0))
    m = measure(req("radius", nominal=1.0, unit="deg"), observed, "C")
    assert (m.status, m.method) == ("unknown", "units")
    assert "deg" in dict(m.detail)["units"]
