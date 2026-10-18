"""Trim, extend, fillet, chamfer.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import EntityInfo


class CornerOpsContract(ABC):
    @abstractmethod
    async def entity_trim(
        self,
        target_handle: str,
        cutter_handle: str,
        keep_x: float,
        keep_y: float,
    ) -> EntityInfo:
        """Trim `target` against `cutter`; keep the piece of `target` containing
        (keep_x, keep_y). LINE, ARC and CIRCLE work in any pair; the cutter is an
        infinite line or a whole circle. A CIRCLE cut twice becomes the kept ARC
        (a new handle). Raises if there is no intersection on the target."""
        ...

    @abstractmethod
    async def entity_extend(
        self,
        target_handle: str,
        boundary_handle: str,
        end_x: float | None = None,
        end_y: float | None = None,
    ) -> EntityInfo:
        """Extend `target` (LINE or ARC) to meet `boundary` (a LINE, taken as
        infinite, or the whole circle of an ARC / CIRCLE). If `end_x/y` is None,
        the target endpoint nearest the boundary is auto-selected. An intersection
        behind the chosen end is not an extension and is refused."""
        ...

    @abstractmethod
    async def entity_fillet(
        self,
        handle1: str,
        handle2: str,
        radius: float,
        trim: bool = True,
        near_x: float | None = None,
        near_y: float | None = None,
    ) -> EntityInfo:
        """Fillet two entities with the given radius. Returns the new ARC.
        When `trim` is True (default), the source entities are shortened
        to the tangent points (AutoCAD default behaviour).

        LINE, ARC and CIRCLE are accepted in any pair. Two curves can have several
        tangent circles of one radius; ``near_x``/``near_y`` choose the one whose
        centre is nearest that point (default: the one needing the least extension).
        A CIRCLE is never trimmed. Two lines keep the classic corner behaviour."""
        ...

    @abstractmethod
    async def entity_chamfer(
        self,
        handle1: str,
        handle2: str,
        dist1: float,
        dist2: float | None = None,
        trim: bool = True,
    ) -> EntityInfo:
        """Chamfer two entities. When `dist2` is None it defaults to `dist1`
        (symmetric chamfer). Returns the new chamfer LINE."""
        ...

    async def entity_create_circle_ttr(
        self,
        handle1: str,
        handle2: str,
        radius: float,
        near_x: float | None = None,
        near_y: float | None = None,
        layer: str | None = None,
        color: int | None = None,
        as_arc: bool = False,
    ) -> dict:
        """AutoCAD's ``CIRCLE`` > ``Ttr``: a circle of ``radius`` tangent to two LINE / ARC / CIRCLE entities.

        Concrete, built on ``entity_get`` and ``entity_create_circle``, so both engines run the very same
        solver (``engineering.tangent``). Between two curves there are up to eight such circles;
        ``near_x``/``near_y`` choose the one whose centre is nearest that point, and without them the one
        needing the least extension of the sources wins. A LINE is treated as infinite (as CIRCLE Ttr does);
        ``extended`` says so when the tangent point falls past a real end. ``as_arc=True`` draws only
        the short arc between the two tangent points (a TTR arc), not the whole circle.
        """
        from engineering.tangent import Circ, Line, choose, fillet_arc, solve_ttr

        if str(handle1) == str(handle2):
            raise RuntimeError(
                "entity_create_circle_ttr: handle1 and handle2 cannot be the same entity."
            )
        if not float(radius) > 0:
            raise RuntimeError("entity_create_circle_ttr: radius must be > 0.")

        async def curve(handle):
            info = await self.entity_get(handle)
            kind, props = info.type.upper(), info.properties
            if kind == "LINE":
                return Line(tuple(props["start"][:2]), tuple(props["end"][:2]))
            if kind == "CIRCLE":
                return Circ(tuple(props["center"][:2]), float(props["radius"]))
            if kind == "ARC":
                return Circ(
                    tuple(props["center"][:2]),
                    float(props["radius"]),
                    float(props["start_angle"]),
                    float(props["end_angle"]),
                )
            raise RuntimeError(
                f"entity_create_circle_ttr supports LINE, ARC and CIRCLE (handle {handle} is {kind})."
            )

        a, b = await curve(handle1), await curve(handle2)
        try:
            candidates = solve_ttr(a, b, float(radius))
            near = (
                (float(near_x), float(near_y))
                if near_x is not None and near_y is not None
                else None
            )
            sol = choose(candidates, near)
        except ValueError as exc:
            raise RuntimeError(f"entity_create_circle_ttr: {exc}.") from exc
        if as_arc:
            start, end = fillet_arc(sol, float(radius))
            info = await self.entity_create_arc(
                sol.center[0], sol.center[1], float(radius), start, end, layer, color
            )
        else:
            info = await self.entity_create_circle(
                sol.center[0], sol.center[1], float(radius), layer, color
            )
        return {
            "ok": True,
            "handle": info.handle,
            "type": "ARC" if as_arc else "CIRCLE",
            "center": [sol.center[0], sol.center[1]],
            "radius": float(radius),
            "tangent_a": [sol.ta[0], sol.ta[1]],
            "tangent_b": [sol.tb[0], sol.tb[1]],
            "extended": (sol.ext_a + sol.ext_b) > 1e-9,
            "candidates": len(candidates),
        }
