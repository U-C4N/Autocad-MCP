"""Vertex lists for a lightweight polyline, validated once for both engines.

``entity_set_vertices``, ``entity_edit_geometry(points=, bulges=)`` and
``entity_create_polyline(bulges=)`` all take WCS ``[x, y]`` points and an
optional parallel list of bulges (``tan(sweep / 4)`` of the segment leaving
each vertex, counter-clockwise positive - the DXF convention ``entity_get``
reports). Everything that can be refused is refused here, before either engine
touches the drawing, so a bad list changes nothing on either of them.

Pure: no engine, no document. The frame translation (a mirrored polyline's
stored OCS) is the backends' business, through ``backends/ocs.py``.
"""

from __future__ import annotations

import math

#: Fewer than two vertices is not a polyline: AutoCAD refuses to create one and
#: a one-vertex LWPOLYLINE written headlessly is a dot nothing can select.
MIN_VERTICES = 2


def _number(value, where: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{where}: expected a number, got {value!r}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected a number, got {value!r}.") from None
    if not math.isfinite(number):
        raise ValueError(f"{where}: must be finite, got {value!r}.")
    return number


def prepare_points(points, where: str) -> list[tuple[float, float]]:
    """``[[x, y], ...]`` as floats; a third (z) entry is accepted and ignored,
    exactly as ``entity_create_polyline`` always has."""
    if isinstance(points, (str, bytes)) or not isinstance(points, (list, tuple)):
        raise ValueError(f"{where}: points: expected a list of [x, y] pairs, got {points!r}.")
    if len(points) < MIN_VERTICES:
        raise ValueError(
            f"{where}: points: a polyline needs at least {MIN_VERTICES} vertices, "
            f"got {len(points)}."
        )
    out: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        if isinstance(point, (str, bytes)) or not isinstance(point, (list, tuple)):
            raise ValueError(f"{where}: points[{index}]: expected [x, y], got {point!r}.")
        if len(point) not in (2, 3):
            raise ValueError(f"{where}: points[{index}]: expected [x, y], got {len(point)} values.")
        out.append(
            (
                _number(point[0], f"{where}: points[{index}][0]"),
                _number(point[1], f"{where}: points[{index}][1]"),
            )
        )
    return out


def prepare_bulges(bulges, count: int, where: str) -> list[float] | None:
    """One bulge per vertex, or ``None`` when the caller gave none."""
    if bulges is None:
        return None
    if isinstance(bulges, (str, bytes)) or not isinstance(bulges, (list, tuple)):
        raise ValueError(f"{where}: bulges: expected a list of numbers, got {bulges!r}.")
    if len(bulges) != count:
        raise ValueError(
            f"{where}: bulges: one per vertex - {count} vertices, {len(bulges)} bulges. "
            "The bulge at index i shapes the segment from vertex i to vertex i + 1; "
            "0 is a straight segment."
        )
    return [_number(value, f"{where}: bulges[{index}]") for index, value in enumerate(bulges)]


def prepare_vertices(
    points, bulges, where: str
) -> tuple[list[tuple[float, float]], list[float] | None]:
    """Points and their bulges, checked against each other."""
    pts = prepare_points(points, where)
    return pts, prepare_bulges(bulges, len(pts), where)


def prepare_position(x, y, where: str) -> tuple[float | None, float | None]:
    """A label's new WCS position; either coordinate may be left out."""
    return (
        None if x is None else _number(x, f"{where}: x"),
        None if y is None else _number(y, f"{where}: y"),
    )


def edit_geometry_route(entity_type: str, classic: dict, points, bulges, x, y) -> str:
    """Which vocabulary ``entity_edit_geometry`` applies to an entity type.

    ``"vertices"`` (an LWPOLYLINE: points / bulges), ``"text"`` (TEXT / MTEXT:
    x / y) or ``"classic"`` (CIRCLE / LINE / ARC and the 1.7 arguments).
    ``entity_type`` is the DXF name. An argument the type does not take is
    refused by name rather than ignored: a polyline handed ``cx`` that came
    back unchanged would read as an edit that happened.
    """
    given = sorted(name for name, value in classic.items() if value is not None)
    vertex = [n for n, v in (("points", points), ("bulges", bulges)) if v is not None]
    position = [n for n, v in (("x", x), ("y", y)) if v is not None]
    if entity_type == "LWPOLYLINE":
        stray = given + position
        if stray:
            raise RuntimeError(
                f"entity_edit_geometry: an LWPOLYLINE takes points / bulges; "
                f"{', '.join(stray)} does not apply to it."
            )
        if not vertex:
            raise RuntimeError(
                "entity_edit_geometry: an LWPOLYLINE takes points / bulges; none was given."
            )
        return "vertices"
    if entity_type in ("TEXT", "MTEXT"):
        stray = given + vertex
        if stray:
            raise RuntimeError(
                f"entity_edit_geometry: a {entity_type} takes x / y; "
                f"{', '.join(stray)} does not apply to it (entity_edit_text edits its "
                "content, height and rotation)."
            )
        if not position:
            raise RuntimeError(
                f"entity_edit_geometry: a {entity_type} takes x / y; none was given."
            )
        return "text"
    if vertex:
        raise RuntimeError(
            f"entity_edit_geometry: {', '.join(vertex)} apply to an LWPOLYLINE, "
            f"not a {entity_type}."
        )
    if position:
        raise RuntimeError(
            f"entity_edit_geometry: x / y apply to a TEXT or MTEXT, not a {entity_type}."
        )
    return "classic"


def resolved_bulges(
    requested: list[float] | None, new_count: int, old_bulges: list[float]
) -> list[float]:
    """The bulges a vertex write ends with.

    Given: exactly those. Not given: the polyline's own bulges when the vertex
    count is unchanged (moving a filleted outline's corners keeps its arcs),
    otherwise straight segments - an old bulge has no vertex to belong to once
    the count changes. The result is reported back by ``entity_get``'s
    ``bulges``, so neither branch is silent.
    """
    if requested is not None:
        return list(requested)
    if len(old_bulges) == new_count:
        return [float(b) for b in old_bulges]
    return [0.0] * new_count
