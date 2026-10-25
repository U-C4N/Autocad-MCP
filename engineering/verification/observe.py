"""Observed geometry: what a drawing actually holds, in WCS, ready to be measured.

``observe(snapshot)`` turns the records of one :class:`Snapshot` into
:class:`ObservedFeature` s - lines, circles, arcs, ellipses, polylines with
their bulges, points, splines and DIMENSIONs - in WCS and drawing units, with
the declared and the inferred unit beside them (v1.7.0, B16 / B31).

INSERTs are exploded **here and only here**: a block's geometry is carried
through every INSERT above it (the affine ``EntityRecord.transform`` read off
ezdxf's own ``matrix44``), and each exploded feature keeps the handle it has in
its block definition plus ``block_path`` - the INSERT handles from the
outermost down. ``Snapshot.records`` never sees exploded geometry, so the diff,
the takeoffs, ``drawing_understand`` and the benchmark tasks keep their inputs.

What a transform does to a curve is computed, not assumed:

* a similarity (uniform scale, rotation, mirror) keeps a circle a circle; a
  mirror reverses an arc's direction, so its counter-clockwise sweep runs from
  the image of its END angle to the image of its START angle - the true sweep
  is kept - and a polyline's bulges change sign;
* a non-uniform scale turns a circle into an ellipse (an arc into an
  elliptical arc): the feature is reported as the ellipse it now is, with
  ``approximation`` saying why. A bulged polyline under it keeps its vertices
  and says its arcs are now elliptical;
* a feature in a plane tilted out of WCS XY (its own extrusion, or an INSERT
  above it) carries ``approximation = "tilted_plane"``: its xy is a
  projection, and the evaluators answer ``unsupported`` for it.

The major axis of an observed ellipse is the first left singular vector of the
2x2 map, with a fixed sign (x >= 0, and y > 0 when x is 0), so the same
geometry always yields the same tuple.

Units: ``declared_unit`` is ``$INSUNITS`` decoded (None when unitless);
``inferred_unit`` is ``engineering/understand/units.py::infer_units`` read off
the same snapshot. HATCH boundaries are not features: a hatch's loops repeat
the outline it fills, and observing them would turn every hatched hole into
two candidates. TEXT, MTEXT and attributes are not geometry.
"""

from __future__ import annotations

import math

import numpy as np
from ezdxf import units as ezdxf_units

from engineering.understand.units import infer_units
from engineering.verification.types import ObservedFeature, ObservedGeometry

__all__ = ["MAX_NESTING", "NON_UNIFORM", "NON_UNIFORM_BULGE", "TILTED", "observe"]

Affine = tuple[float, float, float, float, float, float]
_IDENTITY: Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_TAU = 2.0 * math.pi
#: Relative tolerance of the similarity test on a 2x2 matrix.
_SIMILAR = 1e-12
#: Deepest INSERT nesting followed.
MAX_NESTING = 64
NON_UNIFORM = "non_uniform_scale: a circle under a non-uniform scale is an ellipse"
NON_UNIFORM_BULGE = "non_uniform_scale: the bulged segments are elliptical arcs"
TILTED = "tilted_plane"


def _apply(m: Affine, point) -> tuple[float, float]:
    a, b, c, d, e, f = m
    x, y = float(point[0]), float(point[1])
    return (a * x + c * y + e, b * x + d * y + f)


def _linear(m: Affine, vector) -> tuple[float, float]:
    a, b, c, d, _e, _f = m
    x, y = float(vector[0]), float(vector[1])
    return (a * x + c * y, b * x + d * y)


def _compose(outer: Affine, inner: Affine) -> Affine:
    """``outer`` after ``inner``."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _det(m: Affine) -> float:
    return m[0] * m[3] - m[2] * m[1]


def _similarity(m: Affine) -> float | None:
    """The scale factor when ``m`` is a similarity, else None."""
    a, b, c, d, _e, _f = m
    col1 = a * a + b * b
    col2 = c * c + d * d
    scale = max(col1, col2, 1e-300)
    if abs(col1 - col2) <= _SIMILAR * scale and abs(a * c + b * d) <= _SIMILAR * scale:
        return math.sqrt(col1)
    return None


def _canonical(u: tuple[float, float]) -> tuple[float, float]:
    """One of the two unit vectors along an axis, always the same one."""
    if u[0] < -1e-12 or (abs(u[0]) <= 1e-12 and u[1] < 0.0):
        return (-u[0], -u[1])
    return u


def _ellipse_of(n: np.ndarray, t0: float, t1: float) -> tuple:
    """The ellipse ``p(t) = N @ (cos t, sin t)``: ``(major_axis, ratio, start, end)``.

    The major axis is the first left singular vector of ``N`` (canonical
    sign), the ratio the quotient of the singular values; a parameter is read
    back from a point in the (major, major turned +90 deg) frame, and a
    reflecting ``N`` swaps the ends so the sweep stays counter-clockwise.
    """
    u, sigma, _vt = np.linalg.svd(n)
    s1, s2 = float(sigma[0]), float(sigma[1])
    u1 = _canonical((float(u[0, 0]), float(u[1, 0])))
    u2 = (-u1[1], u1[0])

    def param(t: float) -> float:
        qx, qy = n @ np.array([math.cos(t), math.sin(t)])
        along = (qx * u1[0] + qy * u1[1]) / s1
        across = (qx * u2[0] + qy * u2[1]) / s2
        return math.atan2(across, along) % _TAU

    full = abs((t1 - t0) % _TAU) < 1e-12 or abs(t1 - t0 - _TAU) < 1e-12
    if full:
        start, end = 0.0, _TAU
    else:
        start, end = param(t0), param(t1)
        if float(np.linalg.det(n)) < 0.0:
            start, end = end, start
    return (u1[0] * s1, u1[1] * s1), s2 / s1, start, end


def _pairs(**fields) -> tuple:
    return tuple(fields.items())


def _feature(rec, kind, geometry, *, handle, space, layer, path, notes) -> ObservedFeature:
    note = "; ".join(dict.fromkeys(n for n in notes if n)) or None
    return ObservedFeature(
        handle=handle,
        type=kind,
        space=space,
        layer=layer,
        block_path=path,
        geometry=geometry,
        tags=rec.xdata,
        approximation=note,
    )


def _round_features(rec, m: Affine, scale, common, notes) -> list:
    centre = _apply(m, rec.points[0])
    radius = float(rec.radius)
    if scale is not None:
        if rec.type == "CIRCLE":
            geometry = _pairs(center=centre, radius=radius * scale)
            return [_feature(rec, "circle", geometry, notes=notes, **common)]
        # A similarity turns every direction by theta (the image of +x); a
        # reflecting one maps a direction alpha to theta - alpha, which runs the
        # arc the other way round, so its ends swap.
        theta = math.degrees(math.atan2(m[1], m[0]))
        start, end = rec.angles
        if _det(m) > 0.0:
            a0, a1 = (start + theta) % 360.0, (end + theta) % 360.0
        else:
            a0, a1 = (theta - end) % 360.0, (theta - start) % 360.0
        geometry = _pairs(center=centre, radius=radius * scale, start_angle=a0, end_angle=a1)
        return [_feature(rec, "arc", geometry, notes=notes, **common)]
    n = np.array([[m[0], m[2]], [m[1], m[3]]]) * radius
    if rec.type == "CIRCLE":
        t0, t1 = 0.0, _TAU
    else:
        t0, t1 = math.radians(rec.angles[0]), math.radians(rec.angles[1])
    major, ratio, start, end = _ellipse_of(n, t0, t1)
    geometry = _pairs(
        center=centre, major_axis=major, ratio=ratio, start_param=start, end_param=end
    )
    return [_feature(rec, "ellipse", geometry, notes=[*notes, NON_UNIFORM], **common)]


def _features_of(rec, m: Affine, path, space, parent_layer, tilted_above) -> list:
    layer = parent_layer if (rec.layer == "0" and parent_layer is not None) else rec.layer
    notes = [TILTED if (rec.tilted or tilted_above) else None]
    common = {"handle": rec.handle, "space": space, "layer": layer, "path": path}
    kind = rec.type
    scale = _similarity(m)
    if kind == "LINE" and len(rec.points) == 2:
        geometry = _pairs(start=_apply(m, rec.points[0]), end=_apply(m, rec.points[1]))
        return [_feature(rec, "line", geometry, notes=notes, **common)]
    if kind == "POINT" and rec.points:
        geometry = _pairs(location=_apply(m, rec.points[0]))
        return [_feature(rec, "point", geometry, notes=notes, **common)]
    if kind in ("CIRCLE", "ARC") and rec.points and rec.radius is not None:
        return _round_features(rec, m, scale, common, notes)
    if kind == "ELLIPSE" and rec.ellipse is not None:
        centre, major, ratio, t0, t1 = rec.ellipse
        minor = (-major[1] * ratio, major[0] * ratio)
        a, b = _linear(m, major)
        c, d = _linear(m, minor)
        new_major, new_ratio, start, end = _ellipse_of(np.array([[a, c], [b, d]]), t0, t1)
        geometry = _pairs(
            center=_apply(m, centre),
            major_axis=new_major,
            ratio=new_ratio,
            start_param=start,
            end_param=end,
        )
        return [_feature(rec, "ellipse", geometry, notes=notes, **common)]
    if kind in ("LWPOLYLINE", "POLYLINE") and rec.points:
        bulges = rec.bulges or tuple(0.0 for _ in rec.points)
        if _det(m) < 0.0:
            bulges = tuple(-b for b in bulges)
        bulged = any(abs(b) > 1e-12 for b in bulges)
        extra = NON_UNIFORM_BULGE if scale is None and bulged else None
        geometry = _pairs(
            points=tuple(_apply(m, p) for p in rec.points),
            bulges=tuple(bulges),
            closed=bool(rec.closed),
        )
        return [_feature(rec, "polyline", geometry, notes=[*notes, extra], **common)]
    if kind == "SPLINE" and rec.spline is not None:
        degree, control, fit, closed = rec.spline
        geometry = _pairs(
            degree=degree,
            control_points=tuple(_apply(m, p) for p in control),
            fit_points=tuple(_apply(m, p) for p in fit),
            closed=closed,
        )
        return [_feature(rec, "spline", geometry, notes=notes, **common)]
    if kind == "DIMENSION" and rec.dimension is not None and not path:
        dimtype, defpoints, measurement, override, dimstyle, dimlfac, angle = rec.dimension
        geometry = _pairs(
            dimtype=dimtype,
            defpoints=defpoints,
            measurement=measurement,
            text_override=override,
            dimstyle=dimstyle,
            dimlfac=dimlfac,
            angle=angle,
            text_midpoint=rec.points[1] if len(rec.points) > 1 else None,
        )
        return [_feature(rec, "dimension", geometry, notes=notes, **common)]
    return []


def _explode(
    insert, blocks, m: Affine, path, space, tilted_above, stack, parent_layer=None
) -> list:
    name = insert.block
    if name in stack:
        chain = " > ".join((*stack, name))
        raise ValueError(f"observe: block {name!r} contains itself ({chain})")
    if len(stack) >= MAX_NESTING:
        raise ValueError(f"observe: INSERT nesting deeper than {MAX_NESTING} at {name!r}")
    if insert.transform is None:
        return []
    inner = _compose(m, insert.transform)
    here = (*path, insert.handle)
    tilted = tilted_above or insert.tilted
    # Layer 0 inside a block takes the layer of the INSERT placing it - and an
    # INSERT on 0 inside a block passes on the layer it inherited itself.
    layer = parent_layer if (insert.layer == "0" and parent_layer is not None) else insert.layer
    out: list = []
    for member in blocks.get(name, ()):
        if member.type == "INSERT":
            out.extend(_explode(member, blocks, inner, here, space, tilted, (*stack, name), layer))
        else:
            out.extend(_features_of(member, inner, here, space, layer, tilted))
    return out


def observe(
    snap, *, explode_inserts: bool = True, document: dict | None = None
) -> ObservedGeometry:
    """The drawing's measurable geometry, in WCS and drawing units.

    ``explode_inserts=True`` needs a snapshot read with ``block_defs=True``;
    one without them is refused when it holds an INSERT, because leaving a
    block's geometry out would read as "no such feature".
    ``explode_inserts=False`` leaves INSERTs out altogether. ``document`` is
    the backend's ``document_identity()``; its id and revision are carried.
    """
    has_inserts = any(r.type == "INSERT" for r in snap.records)
    if explode_inserts and has_inserts and snap.blocks is None:
        raise ValueError(
            "observe: this snapshot holds INSERTs but no block definitions; read it with "
            "block_defs=True (take_snapshot / read_snapshot / records_from_doc), or pass "
            "explode_inserts=False to leave block geometry out."
        )
    features: list = []
    for rec in snap.records:
        if rec.type == "INSERT":
            if explode_inserts:
                features.extend(_explode(rec, snap.blocks, _IDENTITY, (), rec.space, False, ()))
            continue
        features.extend(_features_of(rec, _IDENTITY, (), rec.space, None, False))
    try:
        declared = ezdxf_units.decode(int(snap.insunits)) if snap.insunits else None
    except (IndexError, ValueError, TypeError):
        declared = None
    document = document or {}
    return ObservedGeometry(
        features=tuple(features),
        declared_unit=declared,
        inferred_unit=infer_units(snap)["inferred"],
        document_id=document.get("document_id"),
        revision=document.get("revision"),
        source=snap.source,
    )
