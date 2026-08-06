"""OCS (Object Coordinate System) to WCS translation, shared by both backends.

DXF does not store 2D geometry in world coordinates. CIRCLE, ARC, LWPOLYLINE,
TEXT, INSERT and a dimension's text position live in a per-entity frame derived
from the entity's ``extrusion`` vector by the Arbitrary Axis Algorithm. Mirroring
flips extrusion to ``(0, 0, -1)`` and leaves the stored x *unnegated*, so a raw
``dxf.center`` read reports a point the entity is not at — by 60 mm in the
measured case, while the same response's bounding box (which ezdxf computes
through the renderer, in WCS) said otherwise.

The rule this module exists to enforce: **everything crossing the MCP boundary is
WCS.** OCS never leaves the backends.

Both engines share this. ``ezdxf`` is a core dependency, not an extra, so the COM
backend may import it too — and should, rather than hand-rolling the Arbitrary
Axis Algorithm a second time. The functions take a raw extrusion/normal triple
rather than an entity, because a COM object exposes ``.Normal`` where an ezdxf
one exposes ``.dxf.extrusion``.

Not every entity that *carries* extrusion is affected: ezdxf makes ``ocs()`` a
pass-through for LINE, MTEXT, ELLIPSE, SPLINE and POINT, which are WCS-native.
Keying any fix off "has an extrusion attribute" breaks those.
"""

from __future__ import annotations

import math

from ezdxf.math import OCS, Vec3

_IDENTITY = (0.0, 0.0, 1.0)


def is_wcs_frame(extrusion) -> bool:
    """True when the entity's frame is the identity — nothing to translate."""
    try:
        return OCS(Vec3(extrusion)).uz.isclose(_IDENTITY)
    except Exception:
        return True  # an unreadable extrusion is not evidence of a rotated frame


def is_flat_frame(extrusion) -> bool:
    """True when the entity's plane is parallel to WCS XY (extrusion is +/-Z).

    Only these frames admit an exact 2D write: given a tilted plane, an ``(x, y)``
    pair does not determine a point in it, so writing one would displace the
    entity out of its own plane by an amount nobody supplied.
    """
    try:
        uz = OCS(Vec3(extrusion)).uz
    except Exception:
        return True
    return abs(uz.x) < 1e-12 and abs(uz.y) < 1e-12


def to_wcs_2d(extrusion, x, y, z=0.0) -> list[float]:
    """OCS ``(x, y, z)`` to WCS ``[x, y]``.

    ``z`` is ``dxf.elevation`` for an LWPOLYLINE (whose vertices carry no z of
    their own) and the point's own z otherwise.
    """
    v = OCS(Vec3(extrusion)).to_wcs(Vec3(float(x), float(y), float(z)))
    return [float(v.x), float(v.y)]


def from_wcs(extrusion, x, y, z=0.0) -> tuple[float, float, float]:
    """WCS ``(x, y, z)`` to OCS ``(x, y, z)`` — the inverse of :func:`to_wcs_2d`.

    Needed on every write path. The read and write halves used to be *equally*
    wrong, which made a read-then-write round trip an accidental no-op; fixing
    reads alone would have converted that no-op into a real move.
    """
    v = OCS(Vec3(extrusion)).from_wcs(Vec3(float(x), float(y), float(z)))
    return (float(v.x), float(v.y), float(v.z))


def wcs_arc_angles(extrusion, center_ocs, radius, start_deg, end_deg) -> tuple[float, float]:
    """OCS sweep angles to CCW-in-WCS angles about the WCS centre.

    Computed by mapping the two endpoints through the frame rather than by
    arithmetic on the angles, so it stays right for any frame. A left-handed
    frame (``uz.z < 0``, which is what a mirror produces) reverses the direction
    of the sweep, so the endpoints swap roles.
    """
    ocs = OCS(Vec3(extrusion))
    c0 = Vec3(center_ocs)
    centre = ocs.to_wcs(c0)
    angles = []
    for angle in (float(start_deg), float(end_deg)):
        point = ocs.to_wcs(Vec3.from_deg_angle(angle, abs(float(radius))) + c0)
        angles.append(math.degrees(math.atan2(point.y - centre.y, point.x - centre.x)) % 360.0)
    if ocs.uz.z < 0:
        angles.reverse()
    return angles[0], angles[1]


def plane_normal(extrusion) -> list[float]:
    """The entity's plane normal, for reporting a frame that xy cannot express."""
    uz = OCS(Vec3(extrusion)).uz
    return [float(uz.x), float(uz.y), float(uz.z)]
