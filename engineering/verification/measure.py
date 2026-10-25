"""Evaluators: one requirement, measured on CAD-read geometry only (v1.7.0, B18-B20).

``evaluate(requirement, binding, observed)`` returns a :class:`Measurement`:
the value in the requirement's unit, the method, the residual and a status.
Nothing here reads a label, a stored model or the numbers a caller typed -
only the observed features the binding names.

Status:

* ``ambiguous`` / ``unknown`` - the binding was not ``bound``: never ``pass``;
* ``unsupported`` - a spline, a feature in a tilted plane, an ellipse where a
  circle is needed, or a type the quantity cannot read;
* ``unknown`` - a degenerate case the quantity is undefined for (a zero-length
  line, two non-parallel lines asked for a distance, coincident circles asked
  for tangency) or a length requirement on a drawing whose unit is unknown;
* ``pass`` / ``fail`` - against the requirement's own manufacturing tolerance
  (``upper`` / ``lower``: signed deviations in the requirement's unit; a
  constraint reads ``upper`` as its allowed deviation) when it has one, else
  against the verification epsilon. ``detail["basis"]`` says which. An ISO
  286 ``fit`` without a band is not resolved into one here: the drawn size is
  held to its nominal at the verification epsilon and ``detail["fit"]`` names
  the fit. Arc continuity (G1) reads ``upper`` in degrees.

Quantities: ``distance`` (point-point, point-line - the perpendicular to the
line through the segment - and line-line, which must be parallel), ``radius``
and ``diameter`` (circle, arc, or an ellipse of ratio 1; several bound radii
must agree or the answer is ``ambiguous``, never an average), ``angle`` (two
lines, or an arc's sweep), ``concentric``, ``coincident`` (centres and
points), ``parallel``, ``perpendicular``, ``horizontal``, ``vertical``
(degrees off), ``tangent`` (line-circle and circle-circle by distance;
G1 continuity by the angle between the tangents when an arc shares an end
with the other curve), ``count`` (``exact`` / ``min`` / ``max``) and
``absence``. A point-like feature is a POINT or the centre of a circle or arc.

Units: the drawing's unit is its ``declared_unit`` (``$INSUNITS``); only a
unitless drawing falls back to ``observed.inferred_unit``. The inference
(``engineering/understand/units.py::infer_units``) is tuned for plans - a
quarter-circle arc of 0.5-1.5 m reads as a door swing and outweighs the header
- so a millimetre part with R25 quarter fillets infers as inches; trusting it
over the header measured that part 25.4 times too large, and a part drawn 25.4
times too small would have passed. The header is the drawing's claim and the
artifact is held to it. Every length measurement says which unit it used and
where it came from (``detail["drawing_unit"]``, ``detail["unit_source"]``:
``declared`` or ``inferred``), and when the header and the inference disagree
``detail["inferred_unit"]`` names the unit the geometry suggested - recorded,
never silently applied. A declared unit with no exact factor (yards, microns)
answers ``unknown``, never a guess. A length is converted to the requirement's
unit (its target's ``unit``, else the set's ``units`` passed to
:func:`evaluate`) with the exact factors of
``engineering/understand/units.py::MM_PER_UNIT``. An angle is
reported in degrees, or in radians when its target says ``"rad"``. Two
undirected lines meet at an angle and at its supplement; ``angle`` reports the
one nearer the nominal - the same pair of lines, not a choice between
features - and ``parallel`` / ``perpendicular`` read the acute angle.

Epsilons: lengths ``RELATIVE_EPSILON`` x the largest coordinate (at least
1e-9 drawing units); angles ``ANGLE_EPSILON_DEG``. A requirement's
``verification_epsilon`` replaces either, in its own unit.
"""

from __future__ import annotations

import math
from dataclasses import replace

from engineering.understand.units import MM_PER_UNIT
from engineering.verification.binding import feature_of
from engineering.verification.types import Measurement

__all__ = ["ANGLE_EPSILON_DEG", "RELATIVE_EPSILON", "default_epsilon", "evaluate"]

#: Angular verification epsilon, degrees: rotating exact geometry leaves about
#: 1e-12 degrees of noise; a drawn error is orders of magnitude larger.
ANGLE_EPSILON_DEG = 1e-7
#: Relative length epsilon: this fraction of the largest coordinate.
RELATIVE_EPSILON = 1e-9

_ANGULAR = ("angle", "parallel", "perpendicular", "horizontal", "vertical")


def _numbers(value):
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        yield float(value)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _numbers(item)


def default_epsilon(observed) -> float:
    """The length epsilon in drawing units: ``RELATIVE_EPSILON`` x the largest
    absolute coordinate of any feature, never below ``RELATIVE_EPSILON``."""
    largest = 1.0
    for feature in observed.features:
        for _key, value in feature.geometry:
            for x in _numbers(value):
                largest = max(largest, abs(x))
    return RELATIVE_EPSILON * largest


def _result(req, value, unit, method, residual, status, **detail) -> Measurement:
    tolerance = dict(req.manufacturing_tolerance or ())
    if "basis" in detail and "fit" in tolerance:
        # an ISO 286 fit is not resolved into a band here: the drawn size is
        # held to its nominal and the detail says which fit was not applied
        detail["fit"] = tolerance["fit"]
    return Measurement(
        requirement_id=req.id,
        value=value,
        unit=unit,
        method=method,
        residual=residual,
        status=status,
        detail=tuple(sorted(detail.items())),
    )


def _drawing_unit(observed) -> tuple[str | None, str]:
    """``(unit, source)``: the declared unit (``"declared"``); for a unitless
    drawing the inferred one (``"inferred"``); else ``(None, "unknown")``."""
    if observed.declared_unit is not None:
        return observed.declared_unit, "declared"
    if observed.inferred_unit is not None:
        return observed.inferred_unit, "inferred"
    return None, "unknown"


def _unit_detail(observed) -> dict:
    """What a length measurement records about the drawing's unit."""
    unit, source = _drawing_unit(observed)
    detail = {"drawing_unit": unit, "unit_source": source}
    inferred = observed.inferred_unit
    if source == "declared" and inferred is not None and inferred != unit:
        detail["inferred_unit"] = inferred
    return detail


def _factor(observed, unit: str | None) -> tuple[float | None, str]:
    """``(factor, why)``: drawing units -> requirement units, or ``None`` and the
    reason when the requirement's unit is not a length or the drawing's is unknown."""
    if unit is None:
        return 1.0, ""
    if unit not in MM_PER_UNIT:
        return None, f"the requirement unit {unit!r} is not a length unit"
    drawing, source = _drawing_unit(observed)
    if drawing is None:
        return None, "the drawing unit is unknown"
    if drawing not in MM_PER_UNIT:
        return None, f"the {source} drawing unit {drawing!r} has no exact length factor"
    return MM_PER_UNIT[drawing] / MM_PER_UNIT[unit], ""


def _with_units(measurement: Measurement, observed) -> Measurement:
    """``measurement`` with the drawing's unit and its source in its detail."""
    detail = dict(measurement.detail)
    detail.update(_unit_detail(observed))
    return replace(measurement, detail=tuple(sorted(detail.items())))


def _unsupported(features) -> str | None:
    for feature in features:
        if feature.approximation and "tilted_plane" in feature.approximation:
            return "tilted_plane"
        if feature.type == "spline":
            return "spline"
    return None


def _g(feature) -> dict:
    return dict(feature.geometry)


def _direction(line) -> tuple[float, float] | None:
    g = _g(line)
    dx = g["end"][0] - g["start"][0]
    dy = g["end"][1] - g["start"][1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return None
    return (dx / length, dy / length)


def _acute(u, v) -> float:
    """The acute angle between two undirected directions, degrees in [0, 90]."""
    cross = abs(u[0] * v[1] - u[1] * v[0])
    dot = abs(u[0] * v[0] + u[1] * v[1])
    return math.degrees(math.atan2(cross, dot))


def _point_of(feature) -> tuple[float, float] | None:
    g = _g(feature)
    if feature.type == "point":
        return tuple(g["location"])
    if feature.type in ("circle", "arc"):
        return tuple(g["center"])
    return None


def _point_line(point, line) -> float:
    g = _g(line)
    (x1, y1), (x2, y2) = g["start"], g["end"]
    length = math.hypot(x2 - x1, y2 - y1)
    return abs((x2 - x1) * (y1 - point[1]) - (x1 - point[0]) * (y2 - y1)) / length


def _within(req, value: float, nominal: float | None, eps: float) -> tuple[str, float | None, str]:
    """``(status, residual, basis)`` of a dimensional value against its target."""
    tol = dict(req.manufacturing_tolerance or ())
    if nominal is None:
        return "unknown", None, "no_nominal"
    residual = value - nominal
    if "upper" in tol or "lower" in tol:
        upper = float(tol.get("upper", 0.0))
        lower = float(tol.get("lower", 0.0))
        ok = lower - eps <= residual <= upper + eps
        return ("pass" if ok else "fail"), residual, "manufacturing_tolerance"
    return ("pass" if abs(residual) <= eps else "fail"), residual, "verification_epsilon"


def _deviation(req, value: float, eps: float) -> tuple[str, str]:
    """``(status, basis)`` of a constraint whose ideal value is 0."""
    tol = dict(req.manufacturing_tolerance or ())
    if "upper" in tol:
        ok = value <= float(tol["upper"]) + eps
        return ("pass" if ok else "fail"), "manufacturing_tolerance"
    return ("pass" if value <= eps else "fail"), "verification_epsilon"


def _counted(req, features) -> Measurement:
    target = dict(req.target)
    count = len(features)
    if req.quantity == "absence":
        status = "pass" if count == 0 else "fail"
        return _result(req, float(count), "count", "absence", float(count), status)
    ok = True
    if "exact" in target:
        ok = ok and count == int(target["exact"])
    if "min" in target:
        ok = ok and count >= int(target["min"])
    if "max" in target:
        ok = ok and count <= int(target["max"])
    residual = float(count - int(target["exact"])) if "exact" in target else None
    return _result(req, float(count), "count", "count", residual, "pass" if ok else "fail")


def _radial(req, features, nominal, factor, eps, unit) -> Measurement:
    radii = []
    for feature in features:
        g = _g(feature)
        if feature.type in ("circle", "arc"):
            radii.append(float(g["radius"]))
        elif feature.type == "ellipse" and abs(float(g["ratio"]) - 1.0) <= 1e-12:
            radii.append(math.hypot(*g["major_axis"]))
        else:
            return _result(
                req, None, unit, f"{req.quantity}_of_{feature.type}", None, "unsupported"
            )
    if max(radii) - min(radii) > eps / factor:
        return _result(req, None, unit, req.quantity, None, "ambiguous", radii=tuple(radii))
    value = radii[0] * factor * (2.0 if req.quantity == "diameter" else 1.0)
    status, residual, basis = _within(req, value, nominal, eps)
    method = f"{features[0].type}_{req.quantity}"
    return _result(req, value, unit, method, residual, status, basis=basis, epsilon=eps)


def _distance(req, a, b, nominal, factor, eps, unit) -> Measurement:
    pa, pb = _point_of(a), _point_of(b)
    if pa is not None and pb is not None:
        raw, method = math.dist(pa, pb), "point_point"
    elif pa is not None or pb is not None:
        at, line = (pa, b) if pa is not None else (pb, a)
        if line.type != "line":
            return _result(req, None, unit, f"distance_point_{line.type}", None, "unsupported")
        if _direction(line) is None:
            return _result(
                req, None, unit, "point_line", None, "unknown", degenerate="zero-length line"
            )
        raw, method = _point_line(at, line), "point_line"
    elif a.type == "line" and b.type == "line":
        u, v = _direction(a), _direction(b)
        if u is None or v is None:
            return _result(
                req, None, unit, "line_line", None, "unknown", degenerate="zero-length line"
            )
        if _acute(u, v) > ANGLE_EPSILON_DEG:
            return _result(
                req, None, unit, "line_line", None, "unknown", degenerate="lines are not parallel"
            )
        raw, method = _point_line(_g(a)["start"], b), "line_line"
    else:
        return _result(req, None, unit, f"distance_{a.type}_{b.type}", None, "unsupported")
    value = raw * factor
    status, residual, basis = _within(req, value, nominal, eps)
    return _result(req, value, unit, method, residual, status, basis=basis, epsilon=eps)


def _angular(req, features, nominal) -> Measurement:
    """Angles in the requirement's angle unit: degrees, or radians when the
    target says ``"rad"``. The geometry is read in degrees and the value, the
    residual and the epsilon are reported in that unit."""
    q = req.quantity
    unit = "rad" if dict(req.target).get("unit") == "rad" else "deg"
    k = math.pi / 180.0 if unit == "rad" else 1.0  # degrees -> the requirement's unit
    if req.verification_epsilon is not None:
        eps = float(req.verification_epsilon)
    else:
        eps = ANGLE_EPSILON_DEG * k
    if q in ("horizontal", "vertical"):
        if len(features) != 1 or features[0].type != "line":
            return _result(req, None, unit, q, None, "unsupported")
        u = _direction(features[0])
        if u is None:
            return _result(req, None, unit, q, None, "unknown", degenerate="zero-length line")
        axis = (1.0, 0.0) if q == "horizontal" else (0.0, 1.0)
        off = _acute(u, axis) * k
        status, basis = _deviation(req, off, eps)
        return _result(req, off, unit, f"line_{q}", off, status, basis=basis, epsilon=eps)
    if q == "angle" and len(features) == 1 and features[0].type == "arc":
        g = _g(features[0])
        sweep = ((g["end_angle"] - g["start_angle"]) % 360.0 or 360.0) * k
        status, residual, basis = _within(req, sweep, nominal, eps)
        return _result(req, sweep, unit, "arc_sweep", residual, status, basis=basis, epsilon=eps)
    if len(features) != 2 or any(f.type != "line" for f in features):
        return _result(req, None, unit, q, None, "unsupported")
    u, v = _direction(features[0]), _direction(features[1])
    if u is None or v is None:
        return _result(req, None, unit, q, None, "unknown", degenerate="zero-length line")
    acute = _acute(u, v)
    if q == "parallel":
        off = acute * k
        status, basis = _deviation(req, off, eps)
        method = "line_line_parallel"
        return _result(req, off, unit, method, off, status, basis=basis, epsilon=eps)
    if q == "perpendicular":
        off = (90.0 - acute) * k
        status, basis = _deviation(req, off, eps)
        method = "line_line_perpendicular"
        return _result(req, off, unit, method, off, status, basis=basis, epsilon=eps)
    if nominal is None:
        value = acute * k
    else:
        value = min((acute * k, (180.0 - acute) * k), key=lambda c: abs(c - nominal))
    status, residual, basis = _within(req, value, nominal, eps)
    return _result(req, value, unit, "line_line_angle", residual, status, basis=basis, epsilon=eps)


def _ends(feature) -> list:
    g = _g(feature)
    if feature.type == "line":
        return [g["start"], g["end"]]
    if feature.type == "arc":
        cx, cy = g["center"]
        r = float(g["radius"])
        return [
            (
                cx + r * math.cos(math.radians(g[key])),
                cy + r * math.sin(math.radians(g[key])),
            )
            for key in ("start_angle", "end_angle")
        ]
    return []


def _shared_end(a, b, eps: float):
    if "arc" not in (a.type, b.type):
        return None
    for p in _ends(a):
        for q in _ends(b):
            if math.dist(p, q) <= eps:
                return p
    return None


def _tangent_dir(feature, at) -> tuple[float, float]:
    if feature.type == "line":
        return _direction(feature)
    cx, cy = _g(feature)["center"]
    rx, ry = at[0] - cx, at[1] - cy
    length = math.hypot(rx, ry)
    return (-ry / length, rx / length)


def _tangent(req, a, b, factor, eps, unit) -> Measurement:
    kinds = {a.type, b.type}
    if a.type == "line" and b.type == "line":
        return _result(req, None, unit, "tangent_line_line", None, "unsupported")
    if not kinds <= {"line", "circle", "arc"}:
        return _result(req, None, unit, f"tangent_{a.type}_{b.type}", None, "unsupported")
    if (a.type == "line" and _direction(a) is None) or (b.type == "line" and _direction(b) is None):
        return _result(req, None, unit, "tangent", None, "unknown", degenerate="zero-length line")
    shared = _shared_end(a, b, eps / factor)
    if shared is not None:
        off = _acute(_tangent_dir(a, shared), _tangent_dir(b, shared))
        status, basis = _deviation(req, off, ANGLE_EPSILON_DEG)
        return _result(
            req, off, "deg", "arc_continuity", off, status, basis=basis, epsilon=ANGLE_EPSILON_DEG
        )
    if "line" in kinds:
        line, round_ = (a, b) if a.type == "line" else (b, a)
        g = _g(round_)
        value = abs(_point_line(g["center"], line) - float(g["radius"])) * factor
        status, basis = _deviation(req, value, eps)
        method = "line_circle_tangency"
        return _result(req, value, unit, method, value, status, basis=basis, epsilon=eps)
    ga, gb = _g(a), _g(b)
    d = math.dist(ga["center"], gb["center"])
    r1, r2 = float(ga["radius"]), float(gb["radius"])
    if d <= eps / factor and abs(r1 - r2) <= eps / factor:
        return _result(
            req,
            None,
            unit,
            "circle_circle_tangency",
            None,
            "unknown",
            degenerate="coincident circles",
        )
    value = min(abs(d - (r1 + r2)), abs(d - abs(r1 - r2))) * factor
    status, basis = _deviation(req, value, eps)
    method = "circle_circle_tangency"
    return _result(req, value, unit, method, value, status, basis=basis, epsilon=eps)


def evaluate(
    req, binding, observed, *, epsilon: float | None = None, units: str | None = None
) -> Measurement:
    """Measure one requirement on the features its binding names.

    ``epsilon`` (drawing units) replaces :func:`default_epsilon`; a
    requirement's own ``verification_epsilon`` (its unit) wins over both.
    ``units`` is the requirement set's length unit (``RequirementSet.units``):
    the unit of a requirement whose target names none - a parsed constraint
    (``concentric``, ``coincident``, ``tangent``) carries no target, so its
    tolerance and its reported value are in the set's unit. Without it such a
    value is reported in the drawing's own unit.
    """
    target = dict(req.target)
    unit = target.get("unit")
    if unit is None and req.quantity not in _ANGULAR:
        unit = units
    nominal = float(target["nominal"]) if target.get("nominal") is not None else None
    quantity = req.quantity
    if binding.status != "bound":
        status = "ambiguous" if binding.status == "ambiguous" else "unknown"
        return _result(req, None, unit or "", "binding", None, status, binding=binding.reason)
    features = [feature_of(ref, observed) for ref in binding.refs]
    if any(f is None for f in features):
        return _result(
            req,
            None,
            unit or "",
            "binding",
            None,
            "unknown",
            binding="a bound feature is not in this geometry",
        )
    if quantity in ("count", "absence"):
        return _counted(req, features)
    if not features:
        return _result(
            req, None, unit or "", "binding", None, "unknown", binding="no feature is bound"
        )
    why = _unsupported(features)
    if why:
        return _result(req, None, unit or "", why, None, "unsupported")
    if quantity in _ANGULAR:
        return _angular(req, features, nominal)
    return _with_units(_length(req, features, observed, unit, nominal, epsilon), observed)


def _length(req, features, observed, unit, nominal, epsilon) -> Measurement:
    """The quantities measured in length: radius, diameter, distance,
    concentric, coincident and tangent."""
    quantity = req.quantity
    factor, units_why = _factor(observed, unit)
    if factor is None:
        return _result(req, None, unit or "", "units", None, "unknown", units=units_why)
    if req.verification_epsilon is not None:
        eps = float(req.verification_epsilon)
    else:
        eps = (epsilon if epsilon is not None else default_epsilon(observed)) * factor
    out_unit = unit or (_drawing_unit(observed)[0] or "")
    if quantity in ("radius", "diameter"):
        return _radial(req, features, nominal, factor, eps, out_unit)
    if len(features) != 2:
        return _result(req, None, out_unit, quantity, None, "unknown", features=len(features))
    a, b = features
    if quantity == "distance":
        return _distance(req, a, b, nominal, factor, eps, out_unit)
    if quantity in ("concentric", "coincident"):
        pa, pb = _point_of(a), _point_of(b)
        round_pair = a.type in ("circle", "arc") and b.type in ("circle", "arc")
        if pa is None or pb is None or (quantity == "concentric" and not round_pair):
            return _result(
                req, None, out_unit, f"{quantity}_{a.type}_{b.type}", None, "unsupported"
            )
        value = math.dist(pa, pb) * factor
        status, basis = _deviation(req, value, eps)
        method = "centre_distance" if quantity == "concentric" else "point_distance"
        return _result(req, value, out_unit, method, value, status, basis=basis, epsilon=eps)
    if quantity == "tangent":
        return _tangent(req, a, b, factor, eps, out_unit)
    return _result(req, None, out_unit, quantity, None, "unsupported")
