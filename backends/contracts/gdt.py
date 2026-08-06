"""Geometric tolerancing.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations


class GdtContract:
    # Composed from LINE + TEXT primitives so the exact same frame renders on
    # both COM and ezdxf (ezdxf's native TOLERANCE entity renders blank through
    # the matplotlib frontend). Deterministic layout math lives in engineering/
    # gdt.py; datum letters are tracked so the `gdt` critique focus can verify
    # every referenced datum is actually established on the part.

    async def _place_boxed_text(
        self,
        text: str,
        cx: float,
        cy: float,
        text_h: float,
        layer: str,
    ) -> str:
        """Create a TEXT entity roughly centred on (cx, cy). Returns its handle.
        entity_create_text has no alignment arg, so approximate the centring with
        the same width heuristic the title block uses."""
        approx_w = len(text) * text_h * 0.7
        t = await self.entity_create_text(
            text,
            cx - approx_w / 2.0,
            cy - text_h / 2.0,
            height=text_h,
            layer=layer,
        )
        return t.handle

    async def draw_feature_control_frame(
        self,
        symbol: str,
        tolerance: str | float,
        x: float,
        y: float,
        datums: list[str] | None = None,
        height: float = 5.0,
        diameter: bool = False,
        modifier: str | None = None,
        layer: str | None = None,
    ) -> dict:
        """Draw an ISO 1101 feature control frame with bottom-left corner at (x, y).

        symbol: one of the 14 geometric characteristics (straightness, flatness,
        circularity, cylindricity, profile_line, profile_surface, angularity,
        perpendicularity, parallelism, position, concentricity, symmetry,
        circular_runout, total_runout). `datums` is the ordered datum reference
        list (e.g. ["A", "B"]); `diameter=True` prefixes ⌀ for a cylindrical zone;
        `modifier` ∈ {M, L, S} appends the material-condition symbol.

        Orientation/location/runout characteristics require at least one datum
        (raises otherwise). The referenced datums are recorded for the `gdt`
        critique focus.
        """
        from engineering.gdt import fcf_compartments, fcf_layout

        comps = fcf_compartments(
            symbol,
            tolerance,
            datums,
            diameter=diameter,
            modifier=modifier,
        )
        layer = layer or self._role_layer("dim")
        await self._ensure_layer(layer, color=2)
        lay = fcf_layout(comps, float(x), float(y), float(height))
        handles: list[str] = []

        box = lay["box"]
        box_poly = await self.entity_create_polyline(
            [[bx, by] for bx, by in box],
            closed=False,
            layer=layer,
        )
        handles.append(box_poly.handle)
        for (sx, sy), (ex, ey) in lay["dividers"]:
            ln = await self.entity_create_line(sx, sy, ex, ey, layer=layer)
            handles.append(ln.handle)
        th = lay["text_height"]
        for text, tcx, tcy in lay["labels"]:
            handles.append(await self._place_boxed_text(text, tcx, tcy, th, layer))

        refs = getattr(self, "_gdt_datums_referenced", None)
        if refs is None:
            refs = set()
            self._gdt_datums_referenced = refs
        for d in datums or []:
            d = str(d).strip().upper()
            if d:
                refs.add(d)

        return {
            "ok": True,
            "symbol": symbol,
            "compartments": comps,
            "handles": handles,
            "width": lay["width"],
            "height": lay["height"],
            "layer": layer,
        }

    async def draw_datum_feature(
        self,
        letter: str,
        x: float,
        y: float,
        size: float = 5.0,
        layer: str | None = None,
    ) -> dict:
        """Place an ISO 1101 datum feature symbol (filled triangle + boxed letter)
        with its apex at (x, y). Records the datum letter so a feature control
        frame that references it passes the `gdt` critique focus."""
        from engineering.gdt import datum_triangle, fcf_layout

        letter = str(letter).strip().upper()
        if not letter:
            raise RuntimeError("draw_datum_feature: a datum letter is required.")
        layer = layer or self._role_layer("dim")
        await self._ensure_layer(layer, color=2)
        size = float(size)
        handles: list[str] = []

        tri = datum_triangle(float(x), float(y), size, down=True)
        tri_poly = await self.entity_create_polyline(
            [[px, py] for px, py in tri],
            closed=True,
            layer=layer,
        )
        handles.append(tri_poly.handle)

        box_h = size * 1.4
        lay = fcf_layout([letter], float(x) - box_h / 2.0, float(y) - size - box_h, box_h)
        box_poly = await self.entity_create_polyline(
            [[bx, by] for bx, by in lay["box"]],
            closed=False,
            layer=layer,
        )
        handles.append(box_poly.handle)
        for text, tcx, tcy in lay["labels"]:
            handles.append(await self._place_boxed_text(text, tcx, tcy, lay["text_height"], layer))

        defined = getattr(self, "_gdt_datums_defined", None)
        if defined is None:
            defined = set()
            self._gdt_datums_defined = defined
        defined.add(letter)

        return {"ok": True, "datum": letter, "handles": handles, "layer": layer}
