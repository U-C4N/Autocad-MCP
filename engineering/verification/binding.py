"""Binding: which CAD feature a requirement is about - or an honest "cannot tell".

``bind(rs, observed)`` resolves every requirement's selectors against the
observed geometry (``engineering/verification/observe.py``) and returns one
:class:`Binding` per requirement. A selector resolves to its candidates; the
binding keeps them all, says which it bound and why, and **never picks between
equals** (v1.7.0, B17):

* exactly one candidate per selector -> ``bound``;
* two or more -> ``ambiguous`` with every candidate listed (a copied feature
  tag, two identical holes where the requirement names one) - never a random
  or a first-found pick;
* none -> ``unknown``.

Selectors (``FeatureSelector.kind`` and the keys of its ``value``):

* ``handle`` - ``{"handle", "block_path"?}``: that entity; an exploded one
  needs its ``block_path`` (the INSERT handles, outermost first; a MINSERT
  cell is ``"<handle>[<row>,<column>]"``).
* ``feature_tag`` - ``{"feature", "part"?, "view"?, "type"?, "fallback"?}``:
  the entities whose ``ACADMCP_MECH`` feature tag matches. A drawing made
  before 1.7.0 carries no tags; ``fallback`` (a predicate's pairs) is then
  used, and the reason says so.
* ``arch_element`` - ``{"element": "<kind>:<id>", "type"?}``: the entities an
  ``ACADMCP_ARCH`` record owns - its carrier and its ``ACADMCP_ARCH_H`` list.
* ``predicate`` - ``{"type"?, "layer"?, "space"?, "region"?: (x0, y0, x1, y1),
  "radius_min"?, "radius_max"?, "length_min"?, "length_max"?}``: geometry in
  a window; every anchor point (ends, centre, vertices) must lie in the region.
* ``dimension`` - ``{"handle"}``: the features a DIMENSION's defpoints sit on
  (:func:`bind_dimension`).

For ``count`` and ``absence`` a selector's matches are the set being counted:
every match is bound and none is chosen, so zero matches is a result
(``bound`` with no refs), not ``unknown``.

``policy="lenient"`` binds *all* candidates of a radius / diameter selector
when they are congruent (equal radii within epsilon) - still not a pick: the
evaluator measures each. ``"strict"`` (the default) calls them ambiguous.
"""

from __future__ import annotations

import json
import math

from engineering.verification.types import Binding, FeatureRef

__all__ = [
    "WILSON_Z",
    "DimensionIndex",
    "bind",
    "bind_dimension",
    "feature_of",
    "feature_points",
    "wilson_interval",
]

#: The two-sided 95 % normal quantile.
WILSON_Z = 1.959963984540054
_COUNTING = ("count", "absence")
_RADIAL = ("radius", "diameter")


def wilson_interval(successes: int, trials: int, z: float = WILSON_Z) -> tuple[float, float]:
    """The Wilson score interval of a proportion; ``(0.0, 1.0)`` for no trials."""
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    centre = (p + z2 / (2.0 * trials)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / trials + z2 / (4.0 * trials * trials)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def _epsilon_for(observed) -> float:
    """The binder's epsilon is the evaluators' (``measure.default_epsilon``):
    one rule, so a feature bound within epsilon is measured within the same."""
    from engineering.verification.measure import default_epsilon

    return default_epsilon(observed)


def _tag(feature, app: str) -> dict:
    for name, text in feature.tags:
        if name == app:
            try:
                payload = json.loads(text)
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {"values": payload}
    return {}


def _ref(feature, observed) -> FeatureRef:
    return FeatureRef(
        document_id=observed.document_id,
        revision=observed.revision,
        space=feature.space,
        block_path=tuple(feature.block_path),
        handles=(feature.handle,),
        feature=_tag(feature, "ACADMCP_MECH").get("feature") or None,
    )


def feature_of(ref: FeatureRef, observed):
    """The observed feature a single-handle reference names, or None."""
    for feature in observed.features:
        if (
            feature.handle == ref.handles[0]
            and tuple(feature.block_path) == tuple(ref.block_path)
            and feature.space == ref.space
        ):
            return feature
    return None


def _order(feature) -> tuple:
    return (feature.space, tuple(feature.block_path), feature.handle)


def _type_ok(feature, wanted) -> bool:
    return wanted is None or feature.type == str(wanted)


def _inside(point, region) -> bool:
    x0, y0, x1, y1 = (float(v) for v in region)
    return min(x0, x1) <= point[0] <= max(x0, x1) and min(y0, y1) <= point[1] <= max(y0, y1)


def _anchor_points(feature) -> list:
    g = dict(feature.geometry)
    if feature.type == "line":
        return [g["start"], g["end"]]
    if feature.type in ("circle", "arc", "ellipse"):
        return [g["center"]]
    if feature.type == "point":
        return [g["location"]]
    if feature.type == "polyline":
        return list(g["points"])
    if feature.type == "spline":
        return list(g["control_points"])
    return []


def _predicate_matches(feature, crit: dict) -> bool:
    if feature.type == "dimension" and crit.get("type") != "dimension":
        return False
    if not _type_ok(feature, crit.get("type")):
        return False
    if "layer" in crit and feature.layer != str(crit["layer"]):
        return False
    if "space" in crit and feature.space != str(crit["space"]):
        return False
    g = dict(feature.geometry)
    if "radius_min" in crit or "radius_max" in crit:
        if "radius" not in g:
            return False
        radius = float(g["radius"])
        if "radius_min" in crit and radius < float(crit["radius_min"]):
            return False
        if "radius_max" in crit and radius > float(crit["radius_max"]):
            return False
    if "length_min" in crit or "length_max" in crit:
        if feature.type != "line":
            return False
        length = math.dist(g["start"], g["end"])
        if "length_min" in crit and length < float(crit["length_min"]):
            return False
        if "length_max" in crit and length > float(crit["length_max"]):
            return False
    if "region" in crit:
        points = _anchor_points(feature)
        if not points or not all(_inside(p, crit["region"]) for p in points):
            return False
    return True


def _resolve(selector, observed, epsilon: float) -> tuple[list, str, bool]:
    """``(candidate features, reason, together)`` for one selector. ``together``:
    the features are bound jointly (a dimension's ends), not alternatives."""
    crit = dict(selector.value)
    kind = selector.kind
    features = list(observed.features)
    if kind == "handle":
        handle = str(crit.get("handle", "")).upper()
        path = tuple(crit.get("block_path", ()))
        found = [f for f in features if f.handle.upper() == handle and tuple(f.block_path) == path]
        where = f" under {' > '.join(path)}" if path else ""
        return found, f"handle {handle}{where}", False
    if kind == "feature_tag":
        found = []
        for f in features:
            tag = _tag(f, "ACADMCP_MECH")
            if tag.get("kind") != "feature" or tag.get("feature") != crit.get("feature"):
                continue
            if "part" in crit and tag.get("part") != crit["part"]:
                continue
            if "view" in crit and tag.get("view") != crit["view"]:
                continue
            if _type_ok(f, crit.get("type")):
                found.append(f)
        reason = f"feature tag {crit.get('feature')!r}"
        if not found and "fallback" in crit:
            fallback = dict(crit["fallback"])
            found = [f for f in features if _predicate_matches(f, fallback)]
            reason += (
                " is on no entity (a pre-1.7 drawing carries no tags); "
                "fell back to the geometric predicate"
            )
        return found, reason, False
    if kind == "arch_element":
        element = str(crit.get("element", ""))
        owned: set[str] = set()
        for f in features:
            record = _tag(f, "ACADMCP_ARCH")
            if f"{record.get('kind')}:{record.get('id')}" != element:
                continue
            owned.add(f.handle.upper())
            owned.update(str(h).upper() for h in _tag(f, "ACADMCP_ARCH_H").get("values", []))
        found = [f for f in features if f.handle.upper() in owned and _type_ok(f, crit.get("type"))]
        return found, f"arch element {element}", False
    if kind == "predicate":
        return [f for f in features if _predicate_matches(f, crit)], "geometric predicate", False
    if kind == "dimension":
        handle = str(crit.get("handle", "")).upper()
        dims = [f for f in features if f.type == "dimension" and f.handle.upper() == handle]
        if not dims:
            return [], f"no DIMENSION {handle}", False
        result = bind_dimension(dims[0], observed, epsilon=epsilon)
        reason = f"DIMENSION {handle}: {result['reason']}"
        if result["status"] not in ("bound", "ambiguous"):
            return [], reason, False
        found = [feature_of(ref, observed) for ref in result["refs"]]
        # A bound dimension's features are drawn to TOGETHER (both ends of a
        # length), not alternatives; coincident circles under it are.
        return [f for f in found if f is not None], reason, result["status"] == "bound"
    raise ValueError(f"bind: unknown selector kind {kind!r}")


def _congruent(found, epsilon: float) -> bool:
    radii = [dict(f.geometry).get("radius") for f in found]
    if any(r is None for r in radii):
        return False
    return max(radii) - min(radii) <= epsilon


def _bind_one(requirement, observed, policy: str, epsilon: float) -> Binding:
    refs: list[FeatureRef] = []
    candidates: list[FeatureRef] = []
    reasons: list[str] = []
    status = "bound"
    counting = requirement.quantity in _COUNTING
    for selector in requirement.features:
        found, reason, together = _resolve(selector, observed, epsilon)
        found = sorted(found, key=_order)
        mine = [_ref(f, observed) for f in found]
        candidates.extend(mine)
        if counting:
            refs.extend(mine)
            reasons.append(f"{reason}: {len(found)} counted")
        elif together and found:
            refs.extend(mine)
            reasons.append(f"{reason}: {len(found)} feature(s) the dimension is drawn to")
        elif not found:
            if status == "bound":
                status = "unknown"
            reasons.append(f"{reason}: no candidate")
        elif len(found) == 1:
            refs.extend(mine)
            reasons.append(f"{reason}: one candidate")
        elif policy == "lenient" and requirement.quantity in _RADIAL and _congruent(found, epsilon):
            refs.extend(mine)
            reasons.append(
                f"{reason}: {len(found)} congruent candidates, every one measured (lenient)"
            )
        else:
            status = "ambiguous"
            reasons.append(f"{reason}: {len(found)} equal candidates, none chosen")
    if not requirement.features:
        status = "unknown"
        reasons.append("the requirement names no feature")
    if status != "bound":
        refs = []
    return Binding(
        requirement_id=requirement.id,
        status=status,
        refs=tuple(refs),
        candidates=tuple(candidates),
        reason="; ".join(reasons),
    )


def bind(rs, observed, *, policy: str = "strict", epsilon: float | None = None) -> tuple:
    """One :class:`Binding` per requirement of ``rs``, in order."""
    if policy not in ("strict", "lenient"):
        raise ValueError(f"bind: policy must be 'strict' or 'lenient', got {policy!r}")
    eps = float(epsilon) if epsilon is not None else _epsilon_for(observed)
    return tuple(_bind_one(r, observed, policy, eps) for r in rs.requirements)


# -- native dimensions ----------------------------------------------------------


def feature_points(feature) -> list:
    """Every point a dimension can be drawn to: ends, centres, quadrants, locations."""
    g = dict(feature.geometry)
    if feature.type == "line":
        return [g["start"], g["end"]]
    if feature.type == "point":
        return [g["location"]]
    if feature.type in ("circle", "arc"):
        cx, cy = g["center"]
        r = float(g["radius"])
        points = [(cx, cy), (cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r)]
        if feature.type == "arc":
            for deg in (g["start_angle"], g["end_angle"]):
                rad = math.radians(deg)
                points.append((cx + r * math.cos(rad), cy + r * math.sin(rad)))
        return points
    if feature.type == "polyline":
        return list(g["points"])
    return []


class DimensionIndex:
    """A grid over every feature point, for binding many DIMENSIONs (1.8-15).

    ``bind_dimension`` used to walk every feature for every defpoint - fine
    for the handful of dimensions a requirement names, quadratic for the
    ``dimension_truth`` focus, which binds every DIMENSION of the drawing.
    The cell is ``epsilon`` (widened by one part in a million), so a point's
    partners lie in the nine cells around it; the answers are exactly the
    walk's, in the same order (``_order``).
    """

    def __init__(self, observed, epsilon: float):
        self.epsilon = float(epsilon)
        self._cell = max(self.epsilon * (1.0 + 1e-6), 1e-12)
        self._points: dict[tuple[int, int], list] = {}
        self._centres: dict[tuple[int, int], list] = {}
        for feature in observed.features:
            if feature.type == "dimension":
                continue
            for point in feature_points(feature):
                self._points.setdefault(self._key(point), []).append((feature, point))
            if feature.type in ("circle", "arc"):
                centre = dict(feature.geometry)["center"]
                self._centres.setdefault(self._key(centre), []).append(feature)

    def _key(self, point) -> tuple[int, int]:
        return (math.floor(point[0] / self._cell), math.floor(point[1] / self._cell))

    def _near(self, grid: dict, point) -> list:
        kx, ky = self._key(point)
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.extend(grid.get((kx + dx, ky + dy), ()))
        return out

    def at(self, point, epsilon: float) -> list:
        found = {}
        for feature, p in self._near(self._points, point):
            if math.dist(point, p) <= epsilon:
                found[id(feature)] = feature
        return sorted(found.values(), key=_order)

    def circles_at(self, centre, radius, epsilon: float) -> list:
        found = {}
        for feature in self._near(self._centres, centre):
            g = dict(feature.geometry)
            if (
                math.dist(g["center"], centre) <= epsilon
                and abs(float(g["radius"]) - radius) <= epsilon
            ):
                found[id(feature)] = feature
        return sorted(found.values(), key=_order)

    def lines_through(self, a, epsilon: float) -> list:
        return [f for f, _p in self._near(self._points, a) if f.type == "line"]

    def on_curve(self, point, observed, epsilon: float) -> list:
        """The features whose curve passes within ``epsilon`` of ``point``
        (:func:`_on_curve`), from a coarse grid of their boxes built on first use."""
        if not hasattr(self, "_boxes"):
            self._build_boxes(observed)
        kx = math.floor(point[0] / self._box_cell)
        ky = math.floor(point[1] / self._box_cell)
        candidates = {id(f): f for f in self._boxes.get((kx, ky), ())}
        return sorted((f for f in candidates.values() if _point_on(point, f, epsilon)), key=_order)

    def _build_boxes(self, observed) -> None:
        curves = [f for f in observed.features if f.type in _CURVE_TYPES]
        boxes = [(f, _curve_box(f, self.epsilon)) for f in curves]
        span = max([max(b[2] - b[0], b[3] - b[1]) for _f, b in boxes if b is not None] or [1.0])
        self._box_cell = max(span / 256.0, self.epsilon * 4.0, 1e-9)
        self._boxes: dict[tuple[int, int], list] = {}
        for feature, box in boxes:
            if box is None:
                continue
            x0, y0 = math.floor(box[0] / self._box_cell), math.floor(box[1] / self._box_cell)
            x1, y1 = math.floor(box[2] / self._box_cell), math.floor(box[3] / self._box_cell)
            for kx in range(x0, x1 + 1):
                for ky in range(y0, y1 + 1):
                    self._boxes.setdefault((kx, ky), []).append(feature)


#: Features a dimension's measured point may lie *on* (not only at an end).
_CURVE_TYPES = ("line", "polyline", "circle", "arc")


def _segment_distance(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = (
        0.0
        if length == 0
        else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    )
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _on_arc(p, centre, radius, start_deg, end_deg, epsilon) -> bool:
    if abs(math.dist(p, centre) - radius) > epsilon:
        return False
    angle = math.degrees(math.atan2(p[1] - centre[1], p[0] - centre[0])) % 360.0
    sweep = (end_deg - start_deg) % 360.0 or 360.0
    return (angle - start_deg) % 360.0 <= sweep + 1e-9


def _on_bulge(p, a, b, bulge, epsilon) -> bool:
    """On the arc segment ``a -> b`` of a polyline with ``bulge`` (counter-clockwise > 0)."""
    chord = math.dist(a, b)
    if chord == 0:
        return math.dist(p, a) <= epsilon
    theta = 4.0 * math.atan(bulge)
    radius = chord / (2.0 * abs(math.sin(theta / 2.0)))
    mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
    # The centre sits on the chord's normal, at the sagitta's complement.
    offset = radius * math.cos(theta / 2.0) * (1.0 if bulge > 0 else -1.0)
    nx, ny = -(b[1] - a[1]) / chord, (b[0] - a[0]) / chord
    centre = (mx + nx * offset, my + ny * offset)
    start = math.degrees(math.atan2(a[1] - centre[1], a[0] - centre[0]))
    end = math.degrees(math.atan2(b[1] - centre[1], b[0] - centre[0]))
    if bulge < 0:
        start, end = end, start
    return _on_arc(p, centre, radius, start % 360.0, end % 360.0, epsilon)


def _point_on(point, feature, epsilon: float) -> bool:
    g = dict(feature.geometry)
    if feature.type == "line":
        return _segment_distance(point, g["start"], g["end"]) <= epsilon
    if feature.type == "circle":
        return abs(math.dist(point, g["center"]) - float(g["radius"])) <= epsilon
    if feature.type == "arc":
        return _on_arc(
            point, g["center"], float(g["radius"]), g["start_angle"], g["end_angle"], epsilon
        )
    if feature.type == "polyline":
        points = list(g["points"])
        bulges = list(g["bulges"]) or [0.0] * len(points)
        pairs = list(zip(points, points[1:], bulges, strict=False))
        if g["closed"] and len(points) > 2:
            pairs.append((points[-1], points[0], bulges[-1]))
        for a, b, bulge in pairs:
            if abs(bulge) <= 1e-12:
                if _segment_distance(point, a, b) <= epsilon:
                    return True
            elif _on_bulge(point, a, b, bulge, epsilon):
                return True
    return False


def _curve_box(feature, pad: float):
    g = dict(feature.geometry)
    if feature.type == "line":
        xs, ys = (g["start"][0], g["end"][0]), (g["start"][1], g["end"][1])
    elif feature.type in ("circle", "arc"):
        (cx, cy), r = g["center"], float(g["radius"])
        xs, ys = (cx - r, cx + r), (cy - r, cy + r)
    elif feature.type == "polyline" and g["points"]:
        xs = [p[0] for p in g["points"]]
        ys = [p[1] for p in g["points"]]
        if any(abs(b) > 1e-12 for b in g["bulges"]):
            # A bulge can bow a segment out by up to half its chord.
            extra = (
                max(math.dist(a, b) for a, b in zip(g["points"], g["points"][1:], strict=False))
                if len(g["points"]) > 1
                else 0.0
            )
            return (
                min(xs) - extra - pad,
                min(ys) - extra - pad,
                max(xs) + extra + pad,
                max(ys) + extra + pad,
            )
    else:
        return None
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def _on_curve(point, observed, epsilon: float, index: DimensionIndex | None = None) -> list:
    """The features whose curve - a line, a polyline segment (bulges as arcs),
    a circle, an arc - passes within ``epsilon`` of ``point``: a dimension
    measured to a point *on* an edge, not at its end (1.8-15). The fallback of
    :func:`_at` when no feature point is there."""
    if index is not None and epsilon <= index.epsilon:
        return index.on_curve(point, observed, epsilon)
    return sorted(
        (f for f in observed.features if f.type in _CURVE_TYPES and _point_on(point, f, epsilon)),
        key=_order,
    )


def _at(point, observed, epsilon: float, index: DimensionIndex | None = None) -> list:
    if index is not None and epsilon <= index.epsilon:
        return index.at(point, epsilon)
    return sorted(
        (
            f
            for f in observed.features
            if f.type != "dimension"
            and any(math.dist(point, p) <= epsilon for p in feature_points(f))
        ),
        key=_order,
    )


def _circles_at(centre, radius, observed, epsilon: float, index=None) -> list:
    if index is not None and epsilon <= index.epsilon:
        return index.circles_at(centre, radius, epsilon)
    return sorted(
        (
            f
            for f in observed.features
            if f.type in ("circle", "arc")
            and math.dist(dict(f.geometry)["center"], centre) <= epsilon
            and abs(float(dict(f.geometry)["radius"]) - radius) <= epsilon
        ),
        key=_order,
    )


def _legs(a, b, observed, epsilon: float, index=None) -> list:
    out = []
    pool = observed.features
    if index is not None and epsilon <= index.epsilon:
        # Only the count of legs is read (one, or not a single line), so the
        # order of the pool does not matter.
        pool = list({id(f): f for f in index.lines_through(a, epsilon)}.values())
    for f in pool:
        if f.type != "line":
            continue
        g = dict(f.geometry)
        start, end = g["start"], g["end"]
        forward = math.dist(start, a) <= epsilon and math.dist(end, b) <= epsilon
        backward = math.dist(start, b) <= epsilon and math.dist(end, a) <= epsilon
        if forward or backward:
            out.append(f)
    return out


def _line_angle(a, b) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0


def _style_vars(dimension_record):
    """The record's effective dimension style variables, or None (unknown)."""
    if hasattr(dimension_record, "dimension"):
        return getattr(dimension_record, "dim_vars", None)
    return dict(dimension_record.geometry).get("style_vars")


def _fields(dimension_record) -> tuple:
    if hasattr(dimension_record, "dimension"):
        return dimension_record.dimension
    g = dict(dimension_record.geometry)
    return (
        g["dimtype"],
        g["defpoints"],
        g["measurement"],
        g["text_override"],
        g["dimstyle"],
        g["dimlfac"],
        g["angle"],
    )


def bind_dimension(
    dimension_record,
    observed,
    *,
    epsilon: float,
    index: DimensionIndex | None = None,
    across_edges: bool = False,
) -> dict:
    """What a DIMENSION is drawn to, and whether what it shows is what is there.

    ``dimension_record`` is a ``dimension`` :class:`ObservedFeature` (or an
    ``EntityRecord`` carrying ``dimension``). Its defpoints are matched to
    feature points within ``epsilon``. ``geometric_value`` is measured on the
    matched geometry, ``dimension_measurement`` is the dimension's own
    measurement (before DIMLFAC), ``displayed_text`` / ``displayed_value`` what
    the sheet shows - formatted by ``engineering/dimtext.py`` with the
    dimension's own style (DIMDEC, DIMRND, DIMZIN, DIMDSEP, DIMPOST, the Ø / R
    prefix; 1.8-15), so a 19.6 under DIMDEC 0 shows "20".
    ``display_rounding`` is what that formatting moved the value (0.4 there);
    a value shown from the measurement may sit that far from the geometry
    (up to half the last shown unit) without being a finding. A text override
    or a DIMLFAC that makes the sheet disagree with the geometry is a finding
    - ``mismatch`` - never a pass.

    Linear (type 0, measured along DXF angle 50) and aligned (1) dimensions
    are matched by their two measured points; diameter (3) and radius (4) by
    the one circle or arc their chord points describe; two-line angular (2) by
    its two legs. Other types answer ``unsupported``. ``index`` (a
    :class:`DimensionIndex` over the same ``observed``) makes binding many
    dimensions linear; the answer is the same. ``across_edges`` (the
    ``dimension_truth`` focus) also binds a diameter drawn across two edges -
    a side view's silhouette, with no circle under it - to those edges; a
    requirement keeps the circle-only rule, because its diameter is measured
    on the circle the dimension names.
    """
    from engineering.dimtext import display

    fields = _fields(dimension_record)
    dimtype, defpoints, measurement, override, _style, dimlfac, angle = fields
    refs: list = []
    geometric = None
    status = "bound"
    if dimtype in (0, 1):
        first = _at(defpoints[1], observed, epsilon, index) if defpoints[1] else []
        second = _at(defpoints[2], observed, epsilon, index) if defpoints[2] else []
        # A point measured *on* an edge rather than at its end (a step drawn to
        # a silhouette, a wall face) binds to that edge (1.8-15).
        on_edge = False
        if defpoints[1] and not first:
            first, on_edge = _on_curve(defpoints[1], observed, epsilon, index), True
        if defpoints[2] and not second:
            second, on_edge = _on_curve(defpoints[2], observed, epsilon, index), True
        if not first or not second:
            status, reason = "unknown", "a measured point lies on no feature"
        else:
            refs = [_ref(f, observed) for f in dict.fromkeys(first + second)]
            p1, p2 = defpoints[1], defpoints[2]
            if dimtype == 0:
                ux, uy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
                geometric = abs((p2[0] - p1[0]) * ux + (p2[1] - p1[1]) * uy)
            else:
                geometric = math.dist(p1, p2)
            reason = "both measured points lie on features"
            if on_edge:
                reason += " (at least one on an edge, not at its end)"
    elif dimtype in (3, 4):
        if dimtype == 3:
            a, b = defpoints[0], defpoints[3]
            centre = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            radius = math.dist(a, b) / 2.0
        else:
            centre, radius = defpoints[0], math.dist(defpoints[0], defpoints[3])
        found = _circles_at(centre, radius, observed, epsilon, index)
        if not found and dimtype == 3 and across_edges:
            # A diameter drawn across a side view's two silhouette edges: its
            # chord ends lie on edges and the chord is the measured diameter.
            a, b = defpoints[0], defpoints[3]
            ends = [
                _at(p, observed, epsilon, index) or _on_curve(p, observed, epsilon, index)
                for p in (a, b)
            ]
            if all(ends):
                refs = [_ref(f, observed) for f in dict.fromkeys(ends[0] + ends[1])]
                geometric = math.dist(a, b)
                reason = "the diameter is drawn across two edges (a side view)"
            else:
                status, reason = "unknown", "no circle, arc or pair of edges under the dimension"
        elif not found:
            status, reason = "unknown", "no circle or arc under the dimension"
        elif len(found) > 1:
            status, reason = "ambiguous", f"{len(found)} coincident circles under the dimension"
            refs = [_ref(f, observed) for f in found]
        else:
            refs = [_ref(found[0], observed)]
            r = float(dict(found[0].geometry)["radius"])
            geometric = 2.0 * r if dimtype == 3 else r
            reason = "the dimension is drawn on one circle"
    elif dimtype == 2:
        first = _legs(defpoints[1], defpoints[2], observed, epsilon, index)
        second = _legs(defpoints[3], defpoints[0], observed, epsilon, index)
        if len(first) != 1 or len(second) != 1:
            status, reason = "unknown", "the angle's legs are not single lines"
        else:
            refs = [_ref(first[0], observed), _ref(second[0], observed)]
            g1, g2 = dict(first[0].geometry), dict(second[0].geometry)
            raw = abs(_line_angle(g1["start"], g1["end"]) - _line_angle(g2["start"], g2["end"]))
            raw %= 180.0
            target = measurement if measurement is not None else raw
            geometric = min((raw, 180.0 - raw), key=lambda v: abs(v - target))
            reason = "both legs are lines"
    else:
        status, reason = "unsupported", f"dimension type {dimtype} is not bound"
    shown_by = display(fields, _style_vars(dimension_record))
    text, shown = shown_by.text, shown_by.value
    findings: list[str] = []
    # Limits and alternate units show no single number to hold to the geometry.
    unclaimed = shown_by.source in ("limits", "alternate_units")
    if geometric is not None:
        allowance = epsilon + (shown_by.tolerance if shown_by.source == "measurement" else 0.0)
        if unclaimed:
            pass
        elif shown is None or abs(shown - geometric) > allowance:
            if override and override != "<>":
                findings.append("text_override_disagrees_with_geometry")
            elif dimlfac not in (None, 1.0):
                findings.append("dimlfac_scales_the_displayed_value")
            else:
                findings.append("displayed_value_disagrees_with_geometry")
        if measurement is None or abs(float(measurement) - geometric) > epsilon:
            findings.append("measurement_disagrees_with_geometry")
    return {
        "handle": dimension_record.handle,
        "dimtype": dimtype,
        "status": status,
        "refs": tuple(refs),
        "geometric_value": geometric,
        "dimension_measurement": measurement,
        "dimlfac": dimlfac,
        "displayed_text": text,
        "displayed_value": shown,
        "display_rounding": shown_by.display_rounding,
        "display_source": shown_by.source,
        "text_override": override if override not in ("", "<>") else None,
        "findings": findings,
        "mismatch": bool(findings),
        "reason": reason,
    }
