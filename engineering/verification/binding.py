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
import re

from engineering.verification.types import Binding, FeatureRef

__all__ = [
    "WILSON_Z",
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
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


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


def _at(point, observed, epsilon: float) -> list:
    return sorted(
        (
            f
            for f in observed.features
            if f.type != "dimension"
            and any(math.dist(point, p) <= epsilon for p in feature_points(f))
        ),
        key=_order,
    )


def _circles_at(centre, radius, observed, epsilon: float) -> list:
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


def _legs(a, b, observed, epsilon: float) -> list:
    out = []
    for f in observed.features:
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


def _displayed(override: str, measurement, dimlfac) -> tuple[str, float | None]:
    """What the sheet shows: the override (``<>`` standing for the value), else
    the measurement times DIMLFAC; and the value it shows.

    An override that keeps ``<>`` shows the measured value (``"4x <>"``,
    ``"<> H7"``): the value is that measurement, never the first number of the
    text, which may be a count. An override without ``<>`` replaces the value:
    its first number is what the sheet claims."""
    if measurement is None or dimlfac is None:
        value = None
        text = ""
    else:
        value = float(measurement) * float(dimlfac)
        text = f"{value:.10g}"
    if override and override != "<>":
        if "<>" in override:
            text = override.replace("<>", text)
        else:
            text = override
            found = _NUMBER.search(text.replace("\\P", " "))
            value = float(found.group().replace(",", ".")) if found else None
    return text, value


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


def bind_dimension(dimension_record, observed, *, epsilon: float) -> dict:
    """What a DIMENSION is drawn to, and whether what it shows is what is there.

    ``dimension_record`` is a ``dimension`` :class:`ObservedFeature` (or an
    ``EntityRecord`` carrying ``dimension``). Its defpoints are matched to
    feature points within ``epsilon``. ``geometric_value`` is measured on the
    matched geometry, ``dimension_measurement`` is the dimension's own
    measurement (before DIMLFAC), ``displayed_text`` / ``displayed_value`` what
    the sheet shows. A text override or a DIMLFAC that makes the sheet disagree
    with the geometry is a finding - ``mismatch`` - never a pass.

    Linear (type 0, measured along DXF angle 50) and aligned (1) dimensions
    are matched by their two measured points; diameter (3) and radius (4) by
    the one circle or arc their chord points describe; two-line angular (2) by
    its two legs. Other types answer ``unsupported``.
    """
    fields = _fields(dimension_record)
    dimtype, defpoints, measurement, override, _style, dimlfac, angle = fields
    refs: list = []
    geometric = None
    status = "bound"
    if dimtype in (0, 1):
        first = _at(defpoints[1], observed, epsilon) if defpoints[1] else []
        second = _at(defpoints[2], observed, epsilon) if defpoints[2] else []
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
    elif dimtype in (3, 4):
        if dimtype == 3:
            a, b = defpoints[0], defpoints[3]
            centre = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            radius = math.dist(a, b) / 2.0
        else:
            centre, radius = defpoints[0], math.dist(defpoints[0], defpoints[3])
        found = _circles_at(centre, radius, observed, epsilon)
        if not found:
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
        first = _legs(defpoints[1], defpoints[2], observed, epsilon)
        second = _legs(defpoints[3], defpoints[0], observed, epsilon)
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
    text, shown = _displayed(override, measurement, dimlfac)
    findings: list[str] = []
    if geometric is not None:
        if shown is None or abs(shown - geometric) > epsilon:
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
        "text_override": override if override not in ("", "<>") else None,
        "findings": findings,
        "mismatch": bool(findings),
        "reason": reason,
    }
