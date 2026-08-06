"""Planning, critique, snapping and construction geometry.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from backends.quarantine import DocumentQuarantineError

if TYPE_CHECKING:
    from backends.base import EntityInfo
    from engineering.plan_spec import (
        CritiqueFocus,
        DimStyle,
        Issue,
        LayerSetId,
        PlanSpec,
        SheetSize,
        SnapType,
    )


class PremiumContract(ABC):
    # These are CONCRETE and backend-agnostic: they orchestrate the abstract
    # primitives above plus the shared EntityInfo.properties contract (start/
    # end/center/radius/start_angle/end_angle — populated identically by both
    # backends), so the ezdxf and COM backends inherit identical premium
    # behaviour. The only backend-specific primitive is `_create_xline` — XLINE
    # has no cross-backend equivalent among the standard entity_create_* tools.

    _plan_spec: PlanSpec | None = None
    _preflight_result: Any = None

    def _reset_document_state(self) -> None:
        """Clear metadata that is valid only for the current drawing."""
        self._plan_spec = None
        self._preflight_result = None
        self._gdt_datums_defined = set()
        self._gdt_datums_referenced = set()

    async def _ensure_document_state(self) -> None:
        """Allow live backends to detect an out-of-band document switch."""
        return None

    async def drawing_preflight(
        self,
        intent: str,
        requirements: dict | None = None,
        sheet_size: SheetSize = "A3",
        scale: float = 1.0,
        layer_set_id: LayerSetId = "mech",
        view_count: int = 1,
        dim_style: DimStyle = "chain",
        allow_assumptions: bool = False,
    ):
        from engineering.preflight import preflight_drawing

        await self._ensure_document_state()

        result = preflight_drawing(
            intent,
            requirements,
            sheet_size,
            scale,
            layer_set_id,
            view_count,
            dim_style,
            allow_assumptions=allow_assumptions,
        )
        self._preflight_result = result
        return result

    async def drawing_plan(
        self,
        intent: str,
        sheet_size: SheetSize = "A3",
        scale: float = 1.0,
        layer_set_id: LayerSetId = "mech",
        view_count: int = 1,
        dim_style: DimStyle = "chain",
        notes: list[str] | None = None,
        requirements: dict | None = None,
        spec_hash: str | None = None,
    ) -> PlanSpec:
        """Commit a drawing intent before any geometry is created.
        Returns a PlanSpec and stores it on the backend; read it back with
        `get_plan_spec()`. (N8: the previous claim that `drawing_critique`
        replays this plan was untrue — critique runs geometric checks, not a
        plan diff.)"""
        from engineering.plan_spec import PlanSpec

        await self._ensure_document_state()
        preflight_status = "skipped"
        normalized_plan: dict[str, Any] | None = None
        if spec_hash is not None:
            from engineering.preflight import preflight_drawing

            preflight = getattr(self, "_preflight_result", None)
            if preflight is None or preflight.spec_hash != spec_hash:
                raise RuntimeError("drawing_plan: spec_hash does not match the latest preflight.")
            if not preflight.ready:
                raise RuntimeError(
                    "drawing_plan: preflight is not ready; resolve questions/conflicts first."
                )
            candidate = preflight_drawing(
                intent,
                requirements
                if requirements is not None
                else preflight.normalized_spec["requirements"],
                sheet_size,
                scale,
                layer_set_id,
                view_count,
                dim_style,
            )
            if candidate.spec_hash != spec_hash:
                raise RuntimeError(
                    "drawing_plan: supplied fields do not match the hashed preflight spec."
                )
            normalized_plan = preflight.normalized_spec
            preflight_status = "ready"

        if normalized_plan is not None:
            intent = normalized_plan["intent"]
            sheet_size = normalized_plan["sheet_size"]
            scale = normalized_plan["scale"]
            layer_set_id = normalized_plan["layer_set_id"]
            view_count = normalized_plan["view_count"]
            dim_style = normalized_plan["dim_style"]
            requirements = normalized_plan["requirements"]

        plan = PlanSpec(
            intent=str(intent),
            sheet_size=sheet_size,
            scale=float(scale),
            layer_set_id=layer_set_id,
            view_count=int(view_count),
            dim_style=dim_style,
            notes=list(notes) if notes else [],
            requirements=dict(requirements or {}),
            spec_hash=spec_hash,
            preflight_status=preflight_status,
        )
        self._plan_spec = plan
        return plan

    async def drawing_critique(
        self,
        focus: list[CritiqueFocus] | None = None,
    ) -> list[Issue]:
        """Run premium-quality geometric checks; return zero issues before
        `drawing_finalize`. `focus=None` runs all checks. (N8: this does NOT
        replay the committed PlanSpec — it inspects the live drawing geometry.)"""
        from engineering.critique import run_critique

        await self._ensure_document_state()
        return await run_critique(self, focus)

    def get_plan_spec(self) -> dict | None:
        """Return the PlanSpec committed by `drawing_plan` as a dict, or None
        if no plan has been committed. (N8: makes the stored `_plan_spec`
        actually readable instead of being write-only dead state.)"""
        plan = getattr(self, "_plan_spec", None)
        return plan.to_dict() if plan is not None else None

    async def point_from_snap(
        self,
        handle: str,
        snap: SnapType,
        ref_x: float | None = None,
        ref_y: float | None = None,
    ) -> tuple[float, float]:
        """Return a deterministic snap point on `handle`.
        snap ∈ {end, mid, center, quad, perp, near}. For `near/perp`,
        `ref_x/y` is the reference point. Sources geometry from the shared
        EntityInfo.properties contract — works on every backend."""
        ent = await self.entity_get(handle)
        et = ent.type
        p = ent.properties
        ref = (float(ref_x), float(ref_y)) if ref_x is not None and ref_y is not None else None

        def _line_pts():
            s = p.get("start")
            e = p.get("end")
            if s is None or e is None:
                raise RuntimeError(f"point_from_snap: entity {handle} ({et}) has no start/end.")
            return (float(s[0]), float(s[1])), (float(e[0]), float(e[1]))

        def _circle_geom():
            c = p.get("center")
            r = p.get("radius")
            if c is None or r is None:
                raise RuntimeError(f"point_from_snap: entity {handle} ({et}) has no center/radius.")
            return float(c[0]), float(c[1]), float(r)

        def _arc_angles():
            # N7: use .get() + descriptive RuntimeError (matching _line_pts /
            # _circle_geom) instead of bracket indexing that raises an opaque
            # KeyError when start_angle/end_angle are missing.
            sa = p.get("start_angle")
            ea = p.get("end_angle")
            if sa is None or ea is None:
                raise RuntimeError(
                    f"point_from_snap: entity {handle} ({et}) has no start_angle/end_angle."
                )
            return math.radians(float(sa)), math.radians(float(ea))

        if snap == "end":
            if et == "LINE":
                s, e = _line_pts()
                if ref is None:
                    return s
                ds = (s[0] - ref[0]) ** 2 + (s[1] - ref[1]) ** 2
                de = (e[0] - ref[0]) ** 2 + (e[1] - ref[1]) ** 2
                return s if ds <= de else e
            if et == "ARC":
                cx, cy, r = _circle_geom()
                sa, ea = _arc_angles()
                sp = (cx + r * math.cos(sa), cy + r * math.sin(sa))
                ep = (cx + r * math.cos(ea), cy + r * math.sin(ea))
                if ref is None:
                    return sp
                ds = (sp[0] - ref[0]) ** 2 + (sp[1] - ref[1]) ** 2
                de = (ep[0] - ref[0]) ** 2 + (ep[1] - ref[1]) ** 2
                return sp if ds <= de else ep
            raise RuntimeError(f"point_from_snap: 'end' snap not supported on {et}")

        if snap == "mid":
            if et == "LINE":
                s, e = _line_pts()
                return ((s[0] + e[0]) / 2.0, (s[1] + e[1]) / 2.0)
            if et == "ARC":
                cx, cy, r = _circle_geom()
                sa, ea = _arc_angles()
                if ea < sa:
                    ea += 2 * math.pi
                ma = (sa + ea) / 2.0
                return (cx + r * math.cos(ma), cy + r * math.sin(ma))
            raise RuntimeError(f"point_from_snap: 'mid' snap not supported on {et}")

        if snap == "center":
            if et in ("CIRCLE", "ARC", "ELLIPSE"):
                c = p.get("center")
                if c is None:
                    raise RuntimeError(f"point_from_snap: entity {handle} ({et}) has no center.")
                return (float(c[0]), float(c[1]))
            raise RuntimeError(f"point_from_snap: 'center' snap not supported on {et}")

        # NEW-snap-quad-arc / NEW-snap-near-arc: an ARC only covers part of the
        # full circle, so quad/near candidates derived from the circle may fall
        # outside the [start_angle, end_angle] sweep. Restrict (quad) / clamp
        # (near) the projected angle to the sweep, with 2π wraparound.
        def _arc_sweep():
            sa, ea = _arc_angles()
            if ea < sa:
                ea += 2 * math.pi
            return sa, ea

        def _angle_in_sweep(theta: float, sa: float, ea: float) -> bool:
            # Normalise theta into [sa, sa+2π) and test against [sa, ea].
            t = sa + ((theta - sa) % (2 * math.pi))
            return t <= ea + 1e-9

        def _arc_endpoints(cx: float, cy: float, r: float, sa: float, ea: float):
            return (
                (cx + r * math.cos(sa), cy + r * math.sin(sa)),
                (cx + r * math.cos(ea), cy + r * math.sin(ea)),
            )

        if snap == "quad":
            if et == "CIRCLE":
                cx, cy, r = _circle_geom()
                pts = [(cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r)]
                if ref is None:
                    return pts[0]
                return min(pts, key=lambda q: (q[0] - ref[0]) ** 2 + (q[1] - ref[1]) ** 2)
            if et == "ARC":
                cx, cy, r = _circle_geom()
                sa, ea = _arc_sweep()
                quad_angles = (0.0, math.pi / 2.0, math.pi, 3 * math.pi / 2.0)
                pts = [
                    (cx + r * math.cos(a), cy + r * math.sin(a))
                    for a in quad_angles
                    if _angle_in_sweep(a, sa, ea)
                ]
                if not pts:
                    # No quadrant lies on the sweep — fall back to arc endpoints.
                    pts = list(_arc_endpoints(cx, cy, r, sa, ea))
                if ref is None:
                    return pts[0]
                return min(pts, key=lambda q: (q[0] - ref[0]) ** 2 + (q[1] - ref[1]) ** 2)
            raise RuntimeError(f"point_from_snap: 'quad' snap not supported on {et}")

        if snap == "perp":
            if ref is None:
                raise RuntimeError("point_from_snap: 'perp' requires ref_x/ref_y.")
            if et == "LINE":
                s, e = _line_pts()
                dx = e[0] - s[0]
                dy = e[1] - s[1]
                L2 = dx * dx + dy * dy
                if L2 < 1e-18:
                    raise RuntimeError("point_from_snap: line has zero length.")
                t = ((ref[0] - s[0]) * dx + (ref[1] - s[1]) * dy) / L2
                return (s[0] + t * dx, s[1] + t * dy)
            raise RuntimeError(f"point_from_snap: 'perp' snap not supported on {et}")

        if snap == "near":
            if ref is None:
                raise RuntimeError("point_from_snap: 'near' requires ref_x/ref_y.")
            if et == "LINE":
                s, e = _line_pts()
                dx = e[0] - s[0]
                dy = e[1] - s[1]
                L2 = dx * dx + dy * dy
                if L2 < 1e-18:
                    return s
                t = ((ref[0] - s[0]) * dx + (ref[1] - s[1]) * dy) / L2
                t = max(0.0, min(1.0, t))
                return (s[0] + t * dx, s[1] + t * dy)
            if et == "CIRCLE":
                cx, cy, r = _circle_geom()
                vx, vy = ref[0] - cx, ref[1] - cy
                L = math.hypot(vx, vy)
                if L < 1e-18:
                    return (cx + r, cy)
                return (cx + r * vx / L, cy + r * vy / L)
            if et == "ARC":
                cx, cy, r = _circle_geom()
                sa, ea = _arc_sweep()
                vx, vy = ref[0] - cx, ref[1] - cy
                L = math.hypot(vx, vy)
                if L < 1e-18:
                    # Degenerate ref at center: return the start endpoint.
                    return (cx + r * math.cos(sa), cy + r * math.sin(sa))
                theta = math.atan2(vy, vx)
                if _angle_in_sweep(theta, sa, ea):
                    return (cx + r * math.cos(theta), cy + r * math.sin(theta))
                # Projected angle is off the sweep — fall back to the nearer endpoint.
                sp, ep = _arc_endpoints(cx, cy, r, sa, ea)
                ds = (sp[0] - ref[0]) ** 2 + (sp[1] - ref[1]) ** 2
                de = (ep[0] - ref[0]) ** 2 + (ep[1] - ref[1]) ** 2
                return sp if ds <= de else ep
            raise RuntimeError(f"point_from_snap: 'near' snap not supported on {et}")

        if snap == "int":
            raise RuntimeError(
                "point_from_snap: 'int' (intersection) requires a 2nd entity; "
                "compute via two endpoints. V2 will accept a 2nd handle."
            )

        raise RuntimeError(
            f"point_from_snap: unknown snap type '{snap}'. "
            "Use one of: end, mid, center, quad, perp, near."
        )

    async def point_intersection(
        self,
        handle1: str,
        handle2: str,
        ref_x: float | None = None,
        ref_y: float | None = None,
    ) -> tuple[float, float]:
        """Return the intersection point of two geometry entities.

        Supports LINE-LINE, LINE-CIRCLE, and CIRCLE-CIRCLE cases. When
        multiple intersections exist (e.g. two circles), ``ref_x``/``ref_y``
        selects the candidate nearest to that reference point. If omitted the
        first candidate is returned.

        Raises RuntimeError when no real intersection exists or entity types
        are unsupported.
        """
        info1 = await self.entity_get(handle1)
        info2 = await self.entity_get(handle2)
        t1, t2 = info1.type.upper(), info2.type.upper()
        props1, props2 = info1.properties, info2.properties

        candidates: list[tuple[float, float]] = []

        if t1 == "LINE" and t2 == "LINE":
            # Line-line intersection via parametric form
            x1, y1 = props1["start"][0], props1["start"][1]
            x2, y2 = props1["end"][0], props1["end"][1]
            x3, y3 = props2["start"][0], props2["start"][1]
            x4, y4 = props2["end"][0], props2["end"][1]
            dx1, dy1 = x2 - x1, y2 - y1
            dx2, dy2 = x4 - x3, y4 - y3
            denom = dx1 * dy2 - dy1 * dx2
            if abs(denom) < 1e-12:
                raise RuntimeError("point_intersection: lines are parallel or coincident.")
            t = ((x3 - x1) * dy2 - (y3 - y1) * dx2) / denom
            candidates = [(x1 + t * dx1, y1 + t * dy1)]

        elif {t1, t2} == {"LINE", "CIRCLE"}:
            line_p = props1 if t1 == "LINE" else props2
            circ_p = props2 if t1 == "LINE" else props1
            lx1, ly1 = line_p["start"][0], line_p["start"][1]
            lx2, ly2 = line_p["end"][0], line_p["end"][1]
            cx, cy = circ_p["center"][0], circ_p["center"][1]
            r = circ_p["radius"]
            dx, dy = lx2 - lx1, ly2 - ly1
            fx, fy = lx1 - cx, ly1 - cy
            a = dx * dx + dy * dy
            b = 2.0 * (fx * dx + fy * dy)
            c = fx * fx + fy * fy - r * r
            disc = b * b - 4.0 * a * c
            if disc < 0:
                raise RuntimeError("point_intersection: line does not intersect circle.")
            sq = math.sqrt(disc)
            for sign in (1, -1):
                t = (-b + sign * sq) / (2.0 * a)
                candidates.append((lx1 + t * dx, ly1 + t * dy))

        elif t1 == "CIRCLE" and t2 == "CIRCLE":
            cx1, cy1, r1 = props1["center"][0], props1["center"][1], props1["radius"]
            cx2, cy2, r2 = props2["center"][0], props2["center"][1], props2["radius"]
            dx, dy = cx2 - cx1, cy2 - cy1
            d = math.hypot(dx, dy)
            if d < 1e-12:
                raise RuntimeError("point_intersection: circles are concentric.")
            if d > r1 + r2 + 1e-9 or d < abs(r1 - r2) - 1e-9:
                raise RuntimeError("point_intersection: circles do not intersect.")
            a = (r1 * r1 - r2 * r2 + d * d) / (2.0 * d)
            h_sq = r1 * r1 - a * a
            h = math.sqrt(max(h_sq, 0.0))
            mx = cx1 + a * dx / d
            my = cy1 + a * dy / d
            candidates = [
                (mx + h * dy / d, my - h * dx / d),
                (mx - h * dy / d, my + h * dx / d),
            ]

        else:
            raise RuntimeError(
                f"point_intersection: unsupported entity combination {t1}+{t2}. "
                "Supported: LINE-LINE, LINE-CIRCLE, CIRCLE-CIRCLE."
            )

        if len(candidates) == 1:
            return candidates[0]
        if ref_x is not None and ref_y is not None:
            return min(
                candidates,
                key=lambda p: (p[0] - float(ref_x)) ** 2 + (p[1] - float(ref_y)) ** 2,
            )
        return candidates[0]

    async def point_tangent(
        self,
        circle_handle: str,
        from_x: float,
        from_y: float,
        ref_x: float | None = None,
        ref_y: float | None = None,
    ) -> tuple[float, float]:
        """Return the tangent point on a circle from an external point.

        When two tangent points exist, ``ref_x``/``ref_y`` selects the
        nearer one; without it, the first is returned (counter-clockwise from
        the from-point—circle-center line).

        Raises RuntimeError if the point is inside the circle.
        """
        info = await self.entity_get(circle_handle)
        if info.type.upper() != "CIRCLE":
            raise RuntimeError(
                f"point_tangent: handle {circle_handle!r} is {info.type}, expected CIRCLE."
            )
        cx, cy = info.properties["center"][0], info.properties["center"][1]
        r = info.properties["radius"]
        fx, fy = float(from_x), float(from_y)
        dx, dy = cx - fx, cy - fy
        d = math.hypot(dx, dy)
        if d < r - 1e-9:
            raise RuntimeError(
                f"point_tangent: from-point ({fx}, {fy}) is inside the circle "
                f"(center ({cx}, {cy}), radius {r})."
            )
        if d < 1e-12:
            raise RuntimeError("point_tangent: from-point coincides with circle center.")
        # Tangent point T lies on the circle such that (T-F)⊥(T-C).
        # Solving cos(θ - α) = -r/d where α = atan2(C-F) gives the two
        # circle angles θ at which the tangent touches the perimeter.
        alpha = math.atan2(dy, dx)  # direction from F to C
        # NEW-base-1: clamp the acos argument to [-1, 1] so a from-point in the
        # narrow band [r-1e-9, r) (past the internal-point guard but with
        # |-r/d| marginally > 1 from float error) degenerates gracefully to the
        # tangent-at-perimeter case instead of raising a raw ValueError
        # ("math domain error"). Genuine internal points (d < r) are already
        # rejected by the RuntimeError guard above.
        acos_neg = math.acos(max(-1.0, min(1.0, -r / d)))  # (π/2, π] for external pt
        candidates: list[tuple[float, float]] = [
            (cx + r * math.cos(alpha + acos_neg), cy + r * math.sin(alpha + acos_neg)),
            (cx + r * math.cos(alpha - acos_neg), cy + r * math.sin(alpha - acos_neg)),
        ]
        if ref_x is not None and ref_y is not None:
            return min(
                candidates,
                key=lambda p: (p[0] - float(ref_x)) ** 2 + (p[1] - float(ref_y)) ** 2,
            )
        return candidates[0]

    @abstractmethod
    async def _create_xline(
        self,
        x: float,
        y: float,
        dx: float,
        dy: float,
        layer: str,
    ) -> EntityInfo:
        """Create an infinite construction line (XLINE) through (x, y) with
        unit direction (dx, dy) on `layer`. Backend-specific: XLINE has no
        cross-backend entity_create_* equivalent."""
        ...

    async def _ensure_layer(self, name: str, color: int = 7) -> None:
        """Idempotently make sure `name` exists (no-op if already present)."""
        try:
            existing = {lyr.name for lyr in await self.layer_list()}
        except Exception:
            existing = set()
        if name not in existing:
            try:
                await self.layer_create(name, color=color)
            except Exception:
                pass  # already created / racy create — harmless

    def _active_layer_set_id(self) -> str:
        """Best-effort active layer set: an explicit `drawing_apply_iso_layers`
        wins, else the committed PlanSpec, else 'mech' (the default bootstrap)."""
        explicit = getattr(self, "_active_layer_set", None)
        if explicit:
            return explicit
        plan = getattr(self, "_plan_spec", None)
        if plan is not None:
            return getattr(plan, "layer_set_id", "mech")
        return "mech"

    def _role_layer(self, role: str) -> str:
        """Resolve the layer name for a role ('dim' | 'construction') in the
        active layer set, so dims/scaffolding land on the right layer for
        iso13567 (M-DIMEN-T-N / M-CONST-E-N) not just mech/pid (DIM/CONSTRUCTION)."""
        from engineering.layers import resolve_role_layer

        return resolve_role_layer(self._active_layer_set_id(), role)

    async def construction_xline(
        self,
        x: float,
        y: float,
        angle_deg: float,
        layer: str | None = None,
    ) -> EntityInfo:
        """Create an infinite construction line (XLINE) on the active layer set's
        construction layer (auto-created with color 250 = lightest if missing)."""
        layer = layer or self._role_layer("construction")
        await self._ensure_layer(layer, color=250)
        ang = math.radians(float(angle_deg))
        return await self._create_xline(
            float(x),
            float(y),
            math.cos(ang),
            math.sin(ang),
            layer,
        )

    async def construction_clear(
        self,
        layer: str | None = None,
    ) -> dict:
        """Delete every entity on the active layer set's construction layer.
        Must be called before `drawing_finalize`."""
        layer = layer or self._role_layer("construction")
        try:
            ents = await self.entity_list(layer_filter=layer, limit=5000)
        except DocumentQuarantineError:
            # "deleted: 0" here would read as "there was nothing to clear" on a
            # document the backend has declared untrusted -- and this is step 9
            # of the standard workflow, immediately before finalize.
            raise
        except Exception:
            ents = []
        deleted = 0
        for ent in ents:
            try:
                await self.entity_delete(ent.handle)
                deleted += 1
            except Exception:
                continue
        return {"ok": True, "layer": layer, "deleted": deleted}

    async def drawing_apply_iso_layers(
        self,
        standard: LayerSetId = "mech",
    ) -> dict:
        """Bootstrap a full ISO-conformant layer set with correct colors
        and lineweights. Idempotent."""
        from engineering.layers import apply_layer_set

        result = await apply_layer_set(self, standard)
        self._active_layer_set = standard
        return {"ok": True, "standard": standard, "layers": result}

    async def dimension_auto(
        self,
        handles: list[str],
        style: DimStyle = "chain",
        offset: float = 10.0,
        layer: str | None = None,
    ) -> list[EntityInfo]:
        """Generate ISO 129 dimensions across `handles` in the chosen style
        (chain | baseline | ordinate). `offset` is the dimension-line
        distance from the reference geometry (mm). `layer` defaults to the
        active layer set's dimension layer (DIM, or M-DIMEN-T-N for iso13567)."""
        if not handles:
            return []
        dim_layer = layer or self._role_layer("dim")
        if style not in ("chain", "baseline", "ordinate"):
            raise RuntimeError(
                f"dimension_auto: unknown style '{style}'. Use 'chain', 'baseline', or 'ordinate'."
            )
        segs: list[tuple[float, float, float, float]] = []
        for h in handles:
            ent = await self.entity_get(h)
            if ent.type != "LINE":
                raise RuntimeError(
                    f"dimension_auto V1: only LINE entities supported (handle {h} is {ent.type})."
                )
            s = ent.properties.get("start")
            e = ent.properties.get("end")
            if s is None or e is None:
                raise RuntimeError(f"dimension_auto: line {h} missing start/end.")
            segs.append((float(s[0]), float(s[1]), float(e[0]), float(e[1])))

        off = float(offset)
        results: list[EntityInfo] = []

        if style == "chain":
            # Each line gets its own linear (rotated) dimension, offset clear of
            # the geometry along its own perpendicular.
            for sx, sy, ex, ey in segs:
                mx = (sx + ex) / 2.0
                my = (sy + ey) / 2.0
                dx, dy = ex - sx, ey - sy
                L = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / L, dx / L
                dim = await self.dimension_linear(
                    sx,
                    sy,
                    ex,
                    ey,
                    mx + nx * off,
                    my + ny * off,
                    rotation=math.degrees(math.atan2(dy, dx)),
                    layer=dim_layer,
                )
                results.append(dim)

        elif style == "baseline":
            sx0, sy0, ex0, ey0 = segs[0]
            dx, dy = ex0 - sx0, ey0 - sy0
            L0 = math.hypot(dx, dy) or 1.0
            ux, uy = dx / L0, dy / L0
            base_rot = math.degrees(math.atan2(dy, dx))
            for i, (_sx, _sy, ex, ey) in enumerate(segs):
                dim = await self.dimension_linear(
                    sx0,
                    sy0,
                    ex,
                    ey,
                    sx0 - uy * (off + i * off),
                    sy0 + ux * (off + i * off),
                    # Dimension line must run parallel to the (possibly rotated)
                    # baseline, not horizontal.
                    rotation=base_rot,
                    layer=dim_layer,
                )
                results.append(dim)

        else:  # ordinate — stacked X/Y distances; each feature offset so dims don't overlap
            for i, (sx, sy, ex, ey) in enumerate(segs):
                rung = off + i * off  # stagger per feature to avoid coincident dims
                if abs(ex - sx) > 1e-9:
                    dim = await self.dimension_linear(
                        sx,
                        sy,
                        ex,
                        sy,
                        sx + (ex - sx) / 2.0,
                        max(sy, ey) + rung,
                        layer=dim_layer,
                    )
                    results.append(dim)
                if abs(ey - sy) > 1e-9:
                    dim = await self.dimension_linear(
                        ex,
                        sy,
                        ex,
                        ey,
                        max(sx, ex) + rung,
                        (sy + ey) / 2.0,
                        rotation=90.0,
                        layer=dim_layer,
                    )
                    results.append(dim)

        return results

    async def entity_select_smart(
        self,
        predicate: dict,
    ) -> list[EntityInfo]:
        """Semantic entity selection. Predicate keys (all optional, AND-ed):
        - type: "LINE" | "CIRCLE" | ...
        - layer: layer name
        - near: [x, y, radius] — entity reference point within circle
        - length_range: [min, max] — applies to LINE/ARC
        - color: ACI int
        """
        if not isinstance(predicate, dict):
            raise RuntimeError("entity_select_smart: predicate must be a dict.")
        ptype = predicate.get("type")
        player = predicate.get("layer")
        pnear = predicate.get("near")  # [x, y, radius]
        plen = predicate.get("length_range")  # [min, max]
        pcolor = predicate.get("color")

        ents = await self.entity_list(
            type_filter=ptype.upper() if isinstance(ptype, str) else None,
            layer_filter=player if isinstance(player, str) else None,
            limit=5000,
        )

        def _ok(e: EntityInfo) -> bool:
            if pcolor is not None and int(e.color) != int(pcolor):
                return False
            if pnear is not None:
                try:
                    nx, ny, nr = float(pnear[0]), float(pnear[1]), float(pnear[2])
                except Exception:
                    return False
                pt = (
                    e.properties.get("start")
                    or e.properties.get("center")
                    or e.properties.get("insertion")
                    or e.properties.get("insertion_point")
                )
                if pt is None:
                    return False
                if (float(pt[0]) - nx) ** 2 + (float(pt[1]) - ny) ** 2 > nr**2:
                    return False
            if plen is not None and e.type in ("LINE", "ARC"):
                try:
                    lmin, lmax = float(plen[0]), float(plen[1])
                except Exception:
                    return False
                length = e.properties.get("length")
                if length is None or not (lmin <= float(length) <= lmax):
                    return False
            return True

        return [e for e in ents if _ok(e)]
