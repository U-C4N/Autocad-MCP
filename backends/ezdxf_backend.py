"""ezdxf backend – headless DXF/DWG file operations.

Works without AutoCAD installed. Ideal for file generation and analysis.
All operations are synchronous but wrapped in asyncio.to_thread for
non-blocking use in the FastMCP async context.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import config

from .base import (
    AutoCADBackend,
    BlockInfo,
    DrawingInfo,
    EntityInfo,
    LayerInfo,
    shoelace_area,
)

log = logging.getLogger(__name__)

try:
    import ezdxf
    from ezdxf import colors, units  # noqa: F401
    from ezdxf.enums import TextEntityAlignment  # noqa: F401
    from ezdxf.math import BSpline, Vec2, Vec3  # noqa: F401
    _EZDXF_OK = True
except ImportError:
    _EZDXF_OK = False
    log.warning("ezdxf not installed. ezdxf backend unavailable.")

try:
    from ezdxf import bbox as ezdxf_bbox
    _BBOX_OK = True
except ImportError:
    _BBOX_OK = False


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

_ACI_NAMES = {
    1: "Red", 2: "Yellow", 3: "Green", 4: "Cyan",
    5: "Blue", 6: "Magenta", 7: "White", 256: "ByLayer", 0: "ByBlock",
}

_BUILTIN_LINETYPES = {"continuous", "bylayer", "byblock"}

# AutoCAD-shipped linetypes that ezdxf.tools.standards does not include.
# Pattern format: [total_length, dash_1, gap_1, dash_2, gap_2, ...] (gaps negative).
# Values match acadiso.lin so drawings render the same in AutoCAD viewers.
_AUTOCAD_FALLBACK_LINETYPES: dict[str, tuple[str, list[float]]] = {
    "HIDDEN":   ("Hidden __ __ __ __ __ __ __ __ __",  [7.5, 5.0, -2.5]),
    "HIDDEN2":  ("Hidden (.5x) _ _ _ _ _ _ _ _ _",     [3.75, 2.5, -1.25]),
    "HIDDENX2": ("Hidden (2x) ____ ____ ____ ____",    [15.0, 10.0, -5.0]),
    "BORDER":   ("Border __ __ . __ __ . __ __ . __",  [22.5, 7.5, -2.5, 7.5, -2.5, 0.0, -2.5]),
    "BORDER2":  ("Border (.5x) __.__.__.__.__.__",     [11.25, 3.75, -1.25, 3.75, -1.25, 0.0, -1.25]),
    "BORDERX2": ("Border (2x) ____  __  ____  __",     [45.0, 15.0, -5.0, 15.0, -5.0, 0.0, -5.0]),
}


def _add_linetype_from_fallback(doc, name: str) -> bool:
    """Add `name` from _AUTOCAD_FALLBACK_LINETYPES if known. Returns True on success."""
    fallback = _AUTOCAD_FALLBACK_LINETYPES.get(name.upper())
    if fallback is None:
        return False
    description, pattern = fallback
    doc.linetypes.add(name.upper(), pattern=pattern, description=description)
    return True


def _ensure_linetype_loaded(doc, name: str) -> None:
    """Add `name` to doc.linetypes if not already present.

    Lookup order: ezdxf.tools.standards → AutoCAD fallback table → warn.
    Lets callers write `linetype="CENTER"` (or HIDDEN, BORDER) without
    separately loading it.
    """
    if not name or name.lower() in _BUILTIN_LINETYPES:
        return
    if name in doc.linetypes:
        return
    try:
        from ezdxf.tools import standards as ezdxf_standards
        for lt_name, description, pattern in ezdxf_standards.linetypes():
            if lt_name.lower() == name.lower():
                doc.linetypes.add(lt_name, pattern=pattern, description=description)
                return
    except ImportError:
        pass
    if _add_linetype_from_fallback(doc, name):
        return
    log.warning(
        "ezdxf backend: linetype %r is not in ezdxf's standard set or AutoCAD "
        "fallback table; name will be assigned but renders as Continuous until "
        "properly defined.",
        name,
    )


def _entity_info_dxf(ent) -> EntityInfo:
    """Convert an ezdxf entity to EntityInfo."""
    handle = ent.dxf.get("handle", "?")
    ent_type = ent.dxftype()
    layer = ent.dxf.get("layer", "0")
    color = ent.dxf.get("color", 256)
    linetype = ent.dxf.get("linetype", "ByLayer")
    visible = ent.dxf.get("invisible", False) is False

    props: dict = {}
    def _v2(v):
        """Convert Vec3/tuple to [x, y] list."""
        return [float(v.x), float(v.y)] if hasattr(v, 'x') else [float(v[0]), float(v[1])]

    try:
        if ent_type == "LINE":
            props["start"] = _v2(ent.dxf.start)
            props["end"] = _v2(ent.dxf.end)
            props["length"] = ent.dxf.start.distance(ent.dxf.end)
        elif ent_type == "CIRCLE":
            props["center"] = _v2(ent.dxf.center)
            props["radius"] = ent.dxf.radius
        elif ent_type == "ARC":
            props["center"] = _v2(ent.dxf.center)
            props["radius"] = ent.dxf.radius
            props["start_angle"] = ent.dxf.start_angle
            props["end_angle"] = ent.dxf.end_angle
        elif ent_type == "LWPOLYLINE":
            props["points"] = [[float(pt[0]), float(pt[1])] for pt in ent.get_points()]
            props["closed"] = ent.closed
            props["length"] = ent.length()
        elif ent_type == "TEXT":
            props["text"] = ent.dxf.text
            props["insertion"] = _v2(ent.dxf.insert)
            props["height"] = ent.dxf.height
            props["rotation"] = ent.dxf.get("rotation", 0.0)
        elif ent_type == "MTEXT":
            props["text"] = ent.text
            props["insertion"] = _v2(ent.dxf.insert)
            props["char_height"] = ent.dxf.char_height
        elif ent_type == "INSERT":
            props["block_name"] = ent.dxf.name
            props["insertion"] = _v2(ent.dxf.insert)
            props["x_scale"] = ent.dxf.xscale if ent.dxf.hasattr("xscale") else 1.0
            props["y_scale"] = ent.dxf.yscale if ent.dxf.hasattr("yscale") else 1.0
            props["rotation_deg"] = ent.dxf.get("rotation", 0.0)
        elif ent_type == "ELLIPSE":
            props["center"] = _v2(ent.dxf.center)
            props["major_axis"] = _v2(ent.dxf.major_axis)
            props["ratio"] = ent.dxf.ratio
        elif ent_type == "SPLINE":
            if ent.dxf.hasattr("fit_points"):
                props["fit_point_count"] = len(list(ent.fit_points))
            props["degree"] = ent.dxf.degree
        elif ent_type in ("DIMLINEAR", "DIMALIGNED", "DIMANGULAR",
                          "DIMRADIUS", "DIMDIAMETER"):
            props["dim_type"] = ent_type
    except Exception as exc:
        log.debug("extracting entity properties for %s: %s", ent_type, exc)

    return EntityInfo(
        handle=str(handle),
        type=ent_type,
        layer=layer,
        color=color,
        linetype=linetype,
        visible=visible,
        properties=props,
    )


def _layer_info_dxf(layer_obj, current_name: str) -> LayerInfo:
    lw = layer_obj.dxf.get("lineweight", -3)
    return LayerInfo(
        name=layer_obj.dxf.name,
        color=abs(layer_obj.dxf.get("color", 7)),
        linetype=layer_obj.dxf.get("linetype", "Continuous"),
        lineweight=float(lw),
        is_on=not layer_obj.is_off(),
        is_frozen=layer_obj.is_frozen(),
        is_locked=layer_obj.is_locked(),
        is_current=(layer_obj.dxf.name == current_name),
    )


# ---------------------------------------------------------------------------
# EzdxfBackend
# ---------------------------------------------------------------------------


class EzdxfBackend(AutoCADBackend):
    """File-based ezdxf backend – no live AutoCAD needed."""

    def __init__(self):
        if not _EZDXF_OK:
            raise RuntimeError("ezdxf is not installed. Run: pip install ezdxf")
        self._doc: Any = None  # ezdxf Drawing object
        self._doc_path: str | None = None
        self._dirty: bool = False
        self._current_layer: str = "0"
        self._undo_stack: list[Path] = []  # temp-file paths to DXF snapshots
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "ezdxf"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True
        log.info("ezdxf backend ready")

    async def disconnect(self) -> None:
        self._cleanup_undo_stack()
        self._connected = False

    def _cleanup_undo_stack(self):
        while self._undo_stack:
            p = self._undo_stack.pop()
            try:
                p.unlink()
            except OSError:
                pass

    def _require_doc(self):
        if self._doc is None:
            raise RuntimeError(
                "No document open. Call drawing_new() or drawing_open() first."
            )
        return self._doc

    def _msp(self):
        return self._require_doc().modelspace()

    async def _async(self, func, *args, **kwargs):
        async with self._lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    # ── drawing management ────────────────────────────────────────────────────

    async def drawing_info(self) -> DrawingInfo:
        def _sync():
            doc = self._require_doc()
            msp = self._msp()
            entities = list(msp)
            layers = list(doc.layers)

            if _BBOX_OK:
                try:
                    bb = ezdxf_bbox.extents(msp)
                    emin = (bb.extmin.x, bb.extmin.y) if bb else (0.0, 0.0)
                    emax = (bb.extmax.x, bb.extmax.y) if bb else (0.0, 0.0)
                except Exception as exc:
                    log.debug("computing drawing extents via bbox: %s", exc)
                    emin, emax = (0.0, 0.0), (0.0, 0.0)
            else:
                emin, emax = (0.0, 0.0), (0.0, 0.0)

            unit_map = {0: "Unitless", 1: "Inches", 2: "Feet", 4: "mm", 5: "cm", 6: "m"}
            try:
                ins_units = doc.header.get("$INSUNITS", 0)
                unit_str = unit_map.get(ins_units, "Unknown")
            except Exception as exc:
                log.debug("reading $INSUNITS from header: %s", exc)
                unit_str = "Unknown"

            return DrawingInfo(
                name=Path(self._doc_path).name if self._doc_path else "untitled.dxf",
                full_path=self._doc_path or "",
                saved=not self._dirty,
                entity_count=len(entities),
                layer_count=len(layers),
                block_count=len(list(doc.blocks)),
                extents_min=emin,
                extents_max=emax,
                units=unit_str,
                version=doc.dxfversion,
                backend="ezdxf",
            )
        return await self._async(_sync)

    async def drawing_new(self, template: str | None = None) -> dict:
        def _sync():
            if template and Path(template).exists():
                self._doc = ezdxf.readfile(template)
            else:
                self._doc = ezdxf.new(dxfversion="R2010")
            self._doc_path = None
            self._dirty = False
            self._current_layer = "0"
            return {"ok": True, "name": "untitled.dxf"}
        return await self._async(_sync)

    async def drawing_open(self, path: str) -> dict:
        def _sync():
            max_bytes = config.settings.max_dxf_bytes
            if max_bytes > 0:
                try:
                    size = os.path.getsize(path)
                except OSError as exc:
                    raise RuntimeError(f"Cannot stat DXF file: {exc}") from exc
                if size > max_bytes:
                    raise RuntimeError(
                        f"DXF file exceeds MAX_DXF_BYTES limit "
                        f"({size} > {max_bytes}). Set MAX_DXF_BYTES env var to override."
                    )
            self._doc = ezdxf.readfile(path)
            self._doc_path = path
            self._dirty = False
            try:
                self._current_layer = self._doc.header.get("$CLAYER", "0")
            except Exception as exc:
                log.debug("reading $CLAYER from header: %s", exc)
                self._current_layer = "0"
            return {"ok": True, "name": Path(path).name, "path": path}
        return await self._async(_sync)

    async def drawing_save(self, path: str | None = None) -> dict:
        def _sync():
            doc = self._require_doc()
            save_path = path or self._doc_path
            if not save_path:
                raise RuntimeError("No path specified and no current file path.")
            doc.saveas(save_path)
            self._doc_path = save_path
            self._dirty = False
            return {"ok": True, "path": save_path}
        return await self._async(_sync)

    async def drawing_save_as(self, path: str, fmt: str = "dxf") -> dict:
        def _sync():
            doc = self._require_doc()
            doc.saveas(path)
            self._doc_path = path
            self._dirty = False
            return {"ok": True, "path": path, "format": fmt}
        return await self._async(_sync)

    async def drawing_export_dxf(self, path: str) -> dict:
        return await self.drawing_save_as(path, "dxf")

    async def drawing_export_pdf(self, path: str) -> dict:
        """Export to PDF via ezdxf's matplotlib backend."""
        def _sync():
            doc = self._require_doc()
            msp = self._msp()
            try:
                import matplotlib.pyplot as plt
                from ezdxf.addons.drawing import Frontend, RenderContext
                from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

                fig = plt.figure()
                ax = fig.add_axes([0, 0, 1, 1])
                ctx = RenderContext(doc)
                out = MatplotlibBackend(ax)
                Frontend(ctx, out).draw_layout(msp, finalize=True)
                fig.savefig(path, dpi=150)
                plt.close(fig)
                return {"ok": True, "path": path}
            except ImportError:
                raise RuntimeError(
                    "PDF export requires matplotlib: pip install matplotlib"
                ) from None
        return await self._async(_sync)

    async def drawing_purge(self) -> dict:
        def _sync():
            doc = self._require_doc()
            purged = {"blocks": 0, "layers": 0, "linetypes": 0, "text_styles": 0}

            used_blocks: set[str] = set()
            for ent in doc.entitydb.values():
                if ent.dxftype() == "INSERT":
                    used_blocks.add(ent.dxf.name)
            for blk in list(doc.blocks):
                name = blk.name
                if name.startswith("*") or name in used_blocks:
                    continue
                try:
                    doc.blocks.delete_block(name, safe=True)
                    purged["blocks"] += 1
                except Exception:
                    pass

            used_layers: set[str] = {"0", "Defpoints"}
            for ent in doc.entitydb.values():
                if hasattr(ent.dxf, "layer"):
                    used_layers.add(ent.dxf.layer)
            for lyr in list(doc.layers):
                name = lyr.dxf.name
                if name in used_layers:
                    continue
                try:
                    doc.layers.remove(name)
                    purged["layers"] += 1
                except Exception:
                    pass

            used_linetypes: set[str] = {"BYLAYER", "BYBLOCK", "Continuous"}
            for lyr in doc.layers:
                used_linetypes.add(lyr.dxf.linetype)
            for ent in doc.entitydb.values():
                if hasattr(ent.dxf, "linetype"):
                    used_linetypes.add(ent.dxf.linetype)
            for lt in list(doc.linetypes):
                name = lt.dxf.name
                if name in used_linetypes:
                    continue
                try:
                    doc.linetypes.remove(name)
                    purged["linetypes"] += 1
                except Exception:
                    pass

            used_styles: set[str] = {"Standard"}
            for ent in doc.entitydb.values():
                if hasattr(ent.dxf, "style"):
                    used_styles.add(ent.dxf.style)
            for st in list(doc.styles):
                name = st.dxf.name
                if name in used_styles:
                    continue
                try:
                    doc.styles.remove(name)
                    purged["text_styles"] += 1
                except Exception:
                    pass

            self._mark_dirty()
            return {"ok": True, "purged": purged}
        return await self._async(_sync)

    async def drawing_audit(self) -> dict:
        def _sync():
            doc = self._require_doc()
            auditor = doc.audit()
            return {
                "ok": True,
                "errors": [str(e) for e in auditor.errors],
                "error_count": len(auditor.errors),
            }
        return await self._async(_sync)

    async def drawing_close(self, save: bool = True) -> dict:
        def _sync():
            if save and self._dirty and self._doc_path:
                self._doc.saveas(self._doc_path)
            self._cleanup_undo_stack()
            self._doc = None
            self._doc_path = None
            self._dirty = False
            return {"ok": True}
        return await self._async(_sync)

    async def drawing_undo(self) -> dict:
        if not self._undo_stack:
            return {"ok": False, "error": "Nothing to undo (no undo history in ezdxf backend)"}
        def _sync():
            p = self._undo_stack.pop()
            try:
                self._doc = ezdxf.readfile(str(p))
                self._dirty = True
            finally:
                try:
                    p.unlink()
                except OSError:
                    pass
            return {"ok": True, "message": "Undone to last saved state"}
        return await self._async(_sync)

    async def drawing_redo(self) -> dict:
        return {"ok": False, "error": "Redo not supported in ezdxf backend"}

    # ── internal: apply common attrs ──────────────────────────────────────────

    def _apply_attrs(self, entity, layer: str | None, color: int | None, linetype: str | None = None):
        if layer is not None:
            entity.dxf.layer = layer
            # Ensure layer exists
            doc = self._require_doc()
            if layer not in doc.layers:
                doc.layers.add(layer)
        if color is not None:
            entity.dxf.color = int(color)
        if linetype is not None:
            entity.dxf.linetype = linetype

    def _mark_dirty(self):
        self._dirty = True

    # ── entity creation ───────────────────────────────────────────────────────

    async def entity_create_line(
        self, x1, y1, x2, y2, z1=0.0, z2=0.0,
        layer=None, color=None, linetype=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_line(
                (float(x1), float(y1), float(z1)),
                (float(x2), float(y2), float(z2)),
            )
            self._apply_attrs(ent, layer, color, linetype)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_circle(
        self, cx, cy, radius, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_circle((float(cx), float(cy)), float(radius))
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_arc(
        self, cx, cy, radius, start_angle, end_angle, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_arc(
                (float(cx), float(cy)), float(radius),
                float(start_angle), float(end_angle),
            )
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_polyline(
        self, points, closed=False, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            pts_2d = [(float(p[0]), float(p[1])) for p in points]
            ent = msp.add_lwpolyline(pts_2d, close=closed)
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_text(
        self, text, x, y, height=2.5, rotation=0.0, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_text(
                text,
                dxfattribs={
                    "height": float(height),
                    "rotation": float(rotation),
                    "insert": (float(x), float(y)),
                },
            )
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_mtext(
        self, text, x, y, width=100.0, height=2.5, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_mtext(
                text,
                dxfattribs={"char_height": float(height), "width": float(width)},
            )
            ent.dxf.insert = (float(x), float(y), 0.0)
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_hatch(
        self, pattern, boundary_points, scale=1.0, angle=0.0,
        layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            hatch = msp.add_hatch()
            hatch.set_pattern_fill(pattern, scale=float(scale), angle=float(angle))
            pts = [(float(p[0]), float(p[1])) for p in boundary_points]
            hatch.paths.add_polyline_path(pts, is_closed=True)
            self._apply_attrs(hatch, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(hatch)
        return await self._async(_sync)

    async def entity_create_spline(
        self, fit_points, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            pts = [(float(p[0]), float(p[1]), 0.0) for p in fit_points]
            ent = msp.add_spline(fit_points=pts)
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_ellipse(
        self, cx, cy, major_x, major_y, ratio=0.5, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_ellipse(
                center=(float(cx), float(cy), 0.0),
                major_axis=(float(major_x), float(major_y), 0.0),
                ratio=float(ratio),
            )
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_point(
        self, x, y, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_point((float(x), float(y)))
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def entity_create_block_ref(
        self, name, x, y, scale_x=1.0, scale_y=1.0, rotation=0.0, layer=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_blockref(
                name,
                (float(x), float(y)),
                dxfattribs={
                    "xscale": float(scale_x),
                    "yscale": float(scale_y),
                    "rotation": float(rotation),
                },
            )
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    # ── dimensions ───────────────────────────────────────────────────────────

    async def dimension_linear(
        self, x1, y1, x2, y2, dim_x, dim_y, rotation=0.0, layer=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            dim = msp.add_linear_dim(
                base=(float(dim_x), float(dim_y)),
                p1=(float(x1), float(y1)),
                p2=(float(x2), float(y2)),
                angle=float(rotation),
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def dimension_aligned(
        self, x1, y1, x2, y2, dim_x, dim_y, layer=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            dim = msp.add_aligned_dim(
                p1=(float(x1), float(y1)),
                p2=(float(x2), float(y2)),
                dist=math.sqrt((float(dim_x) - float(x1)) ** 2 + (float(dim_y) - float(y1)) ** 2),
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def dimension_angular(
        self, vx, vy, x1, y1, x2, y2, tx, ty, layer=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            dim = msp.add_angular_dim_2l(
                center=(float(vx), float(vy)),
                p1=(float(x1), float(y1)),
                p2=(float(x2), float(y2)),
                distance=10.0,
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def dimension_radius(
        self, cx, cy, chord_x, chord_y, leader_length=10.0, layer=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            cxf, cyf = float(cx), float(cy)
            radius = math.sqrt((chord_x - cxf) ** 2 + (chord_y - cyf) ** 2)
            angle_rad = math.atan2(chord_y - cyf, chord_x - cxf)
            leader = float(leader_length)
            mpoint = (
                cxf + (radius + leader) * math.cos(angle_rad),
                cyf + (radius + leader) * math.sin(angle_rad),
            )
            dim = msp.add_radius_dim(
                center=(cxf, cyf),
                mpoint=mpoint,
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    async def dimension_diameter(
        self, x1, y1, x2, y2, leader_length=10.0, layer=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            x1f, y1f, x2f, y2f = float(x1), float(y1), float(x2), float(y2)
            cx = (x1f + x2f) / 2
            cy = (y1f + y2f) / 2
            radius = math.sqrt((x2f - x1f) ** 2 + (y2f - y1f) ** 2) / 2
            angle_rad = math.atan2(y2f - y1f, x2f - x1f)
            leader = float(leader_length)
            mpoint = (
                cx + (radius + leader) * math.cos(angle_rad),
                cy + (radius + leader) * math.sin(angle_rad),
            )
            dim = msp.add_diameter_dim(
                center=(cx, cy),
                mpoint=mpoint,
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)
        return await self._async(_sync)

    # ── entity modification ───────────────────────────────────────────────────

    def _get_entity(self, handle: str):
        doc = self._require_doc()
        ent = doc.entitydb.get(handle)
        if ent is None:
            raise RuntimeError(f"Entity with handle '{handle}' not found.")
        return ent

    async def entity_move(self, handle, dx, dy, dz=0.0) -> dict:
        def _sync():
            ent = self._get_entity(handle)
            ent.translate(float(dx), float(dy), float(dz))
            self._mark_dirty()
            return {"ok": True, "handle": handle}
        return await self._async(_sync)

    async def entity_copy(self, handle, dx, dy, dz=0.0) -> EntityInfo:
        def _sync():
            ent = self._get_entity(handle)
            copy = ent.copy()
            self._msp().add_entity(copy)
            copy.translate(float(dx), float(dy), float(dz))
            self._mark_dirty()
            return _entity_info_dxf(copy)
        return await self._async(_sync)

    async def entity_rotate(self, handle, base_x, base_y, angle_deg) -> dict:
        def _sync():
            ent = self._get_entity(handle)
            from ezdxf.math import Matrix44
            m = Matrix44.z_rotate(math.radians(float(angle_deg)))
            # Translate to origin, rotate, translate back
            bx, by = float(base_x), float(base_y)
            ent.transform(
                Matrix44.translate(-bx, -by, 0)
                @ m
                @ Matrix44.translate(bx, by, 0)
            )
            self._mark_dirty()
            return {"ok": True, "handle": handle}
        return await self._async(_sync)

    async def entity_scale(self, handle, base_x, base_y, factor) -> dict:
        def _sync():
            ent = self._get_entity(handle)
            from ezdxf.math import Matrix44
            s = float(factor)
            bx, by = float(base_x), float(base_y)
            ent.transform(
                Matrix44.translate(-bx, -by, 0)
                @ Matrix44.scale(s, s, s)
                @ Matrix44.translate(bx, by, 0)
            )
            self._mark_dirty()
            return {"ok": True, "handle": handle}
        return await self._async(_sync)

    async def entity_mirror(
        self, handle, x1, y1, x2, y2, delete_original=False,
    ) -> EntityInfo:
        def _sync():
            ent = self._get_entity(handle)
            copy = ent.copy()
            self._msp().add_entity(copy)
            from ezdxf.math import Matrix44
            # Mirror across the line defined by (x1,y1)-(x2,y2)
            dx = float(x2) - float(x1)
            dy = float(y2) - float(y1)
            length = math.sqrt(dx * dx + dy * dy)
            if length == 0:
                raise ValueError("Mirror line has zero length")
            cos2 = (dx * dx - dy * dy) / (length * length)
            sin2 = 2 * dx * dy / (length * length)
            m = Matrix44((
                cos2,  sin2, 0, 0,
                sin2, -cos2, 0, 0,
                0,     0,    1, 0,
                0,     0,    0, 1,
            ))
            tx, ty = float(x1), float(y1)
            copy.transform(
                Matrix44.translate(-tx, -ty, 0)
                @ m
                @ Matrix44.translate(tx, ty, 0)
            )
            if delete_original:
                self._msp().delete_entity(ent)
            self._mark_dirty()
            return _entity_info_dxf(copy)
        return await self._async(_sync)

    async def entity_offset(
        self, handle, distance, side_x=None, side_y=None,
    ) -> EntityInfo:
        def _sync():
            ent = self._get_entity(handle)
            ent_type = ent.dxftype()
            d = float(distance)

            if ent_type == "LINE":
                # Offset a line: move parallel
                start = Vec2(ent.dxf.start.x, ent.dxf.start.y)
                end = Vec2(ent.dxf.end.x, ent.dxf.end.y)
                direction = (end - start).normalize()
                normal = Vec2(-direction.y, direction.x) * d
                new_start = start + normal
                new_end = end + normal
                msp = self._msp()
                new_ent = msp.add_line(
                    (new_start.x, new_start.y),
                    (new_end.x, new_end.y),
                    dxfattribs={"layer": ent.dxf.layer},
                )
                self._mark_dirty()
                return _entity_info_dxf(new_ent)

            elif ent_type == "CIRCLE":
                cx, cy = ent.dxf.center.x, ent.dxf.center.y
                new_r = ent.dxf.radius + d
                if new_r <= 0:
                    raise ValueError("Offset distance too large for circle")
                msp = self._msp()
                new_ent = msp.add_circle(
                    (cx, cy), new_r,
                    dxfattribs={"layer": ent.dxf.layer},
                )
                self._mark_dirty()
                return _entity_info_dxf(new_ent)

            else:
                raise RuntimeError(f"Offset not supported for {ent_type}")
        return await self._async(_sync)

    async def entity_delete(self, handle) -> dict:
        def _sync():
            ent = self._get_entity(handle)
            self._msp().delete_entity(ent)
            self._mark_dirty()
            return {"ok": True, "deleted_handle": handle}
        return await self._async(_sync)

    async def entity_array_rectangular(
        self, handle, rows, cols, row_spacing, col_spacing,
    ) -> list[EntityInfo]:
        def _sync():
            ent = self._get_entity(handle)
            msp = self._msp()
            results = []
            for r in range(int(rows)):
                for c in range(int(cols)):
                    if r == 0 and c == 0:
                        continue  # skip original
                    copy = ent.copy()
                    msp.add_entity(copy)
                    copy.translate(c * float(col_spacing), r * float(row_spacing), 0)
                    results.append(_entity_info_dxf(copy))
            self._mark_dirty()
            return results
        return await self._async(_sync)

    async def entity_array_polar(
        self, handle, count, fill_angle, center_x, center_y,
    ) -> list[EntityInfo]:
        def _sync():
            ent = self._get_entity(handle)
            msp = self._msp()
            from ezdxf.math import Matrix44
            results = []
            cx, cy = float(center_x), float(center_y)
            step = math.radians(float(fill_angle)) / max(int(count) - 1, 1)
            for i in range(1, int(count)):
                copy = ent.copy()
                msp.add_entity(copy)
                angle = step * i
                m = (
                    Matrix44.translate(-cx, -cy, 0)
                    @ Matrix44.z_rotate(angle)
                    @ Matrix44.translate(cx, cy, 0)
                )
                copy.transform(m)
                results.append(_entity_info_dxf(copy))
            self._mark_dirty()
            return results
        return await self._async(_sync)

    # ── entity query / properties ─────────────────────────────────────────────

    async def entity_get(self, handle) -> EntityInfo:
        def _sync():
            return _entity_info_dxf(self._get_entity(handle))
        return await self._async(_sync)

    async def entity_set_properties(
        self, handle, layer=None, color=None, linetype=None,
        lineweight=None, visible=None,
    ) -> dict:
        def _sync():
            ent = self._get_entity(handle)
            if layer is not None:
                ent.dxf.layer = layer
            if color is not None:
                ent.dxf.color = int(color)
            if linetype is not None:
                _ensure_linetype_loaded(self._require_doc(), linetype)
                ent.dxf.linetype = linetype
            if lineweight is not None:
                ent.dxf.lineweight = int(lineweight)
            if visible is not None:
                ent.dxf.invisible = not bool(visible)
            self._mark_dirty()
            return {"ok": True, "handle": handle}
        return await self._async(_sync)

    async def entity_list(
        self, type_filter=None, layer_filter=None, limit=200, offset=0,
    ) -> list[EntityInfo]:
        def _sync():
            msp = self._msp()
            results = []
            skipped = 0
            for ent in msp:
                ent_type = ent.dxftype()
                ent_layer = ent.dxf.get("layer", "0")
                if type_filter and type_filter.upper() != ent_type.upper():
                    continue
                if layer_filter and layer_filter.lower() != ent_layer.lower():
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                results.append(_entity_info_dxf(ent))
                if len(results) >= limit:
                    break
            return results
        return await self._async(_sync)

    # ── layer management ──────────────────────────────────────────────────────

    async def layer_list(self) -> list[LayerInfo]:
        def _sync():
            doc = self._require_doc()
            return [
                _layer_info_dxf(lyr, self._current_layer)
                for lyr in doc.layers
            ]
        return await self._async(_sync)

    async def layer_create(
        self, name, color=7, linetype="Continuous", lineweight=-3,
    ) -> LayerInfo:
        def _sync():
            doc = self._require_doc()
            _ensure_linetype_loaded(doc, linetype)
            lyr = doc.layers.add(
                name,
                color=int(color),
                linetype=linetype,
                lineweight=int(lineweight),
            )
            self._mark_dirty()
            return _layer_info_dxf(lyr, self._current_layer)
        return await self._async(_sync)

    async def layer_delete(self, name) -> dict:
        def _sync():
            doc = self._require_doc()
            if name in doc.layers:
                doc.layers.remove(name)
                self._mark_dirty()
            return {"ok": True, "deleted": name}
        return await self._async(_sync)

    async def layer_set_current(self, name) -> dict:
        def _sync():
            doc = self._require_doc()
            if name not in doc.layers:
                doc.layers.add(name)
            self._current_layer = name
            try:
                doc.header["$CLAYER"] = name
            except Exception as exc:
                log.debug("writing $CLAYER to header: %s", exc)
            self._mark_dirty()
            return {"ok": True, "current_layer": name}
        return await self._async(_sync)

    async def layer_modify(
        self, name, color=None, linetype=None, lineweight=None,
    ) -> LayerInfo:
        def _sync():
            doc = self._require_doc()
            lyr = doc.layers.get(name)
            if lyr is None:
                raise RuntimeError(f"Layer '{name}' not found.")
            if color is not None:
                lyr.dxf.color = int(color)
            if linetype is not None:
                _ensure_linetype_loaded(doc, linetype)
                lyr.dxf.linetype = linetype
            if lineweight is not None:
                lyr.dxf.lineweight = int(lineweight)
            self._mark_dirty()
            return _layer_info_dxf(lyr, self._current_layer)
        return await self._async(_sync)

    async def layer_freeze(self, name) -> dict:
        def _sync():
            doc = self._require_doc()
            lyr = doc.layers.get(name)
            if lyr:
                lyr.freeze()
                self._mark_dirty()
            return {"ok": True, "layer": name, "frozen": True}
        return await self._async(_sync)

    async def layer_thaw(self, name) -> dict:
        def _sync():
            doc = self._require_doc()
            lyr = doc.layers.get(name)
            if lyr:
                lyr.thaw()
                self._mark_dirty()
            return {"ok": True, "layer": name, "frozen": False}
        return await self._async(_sync)

    async def layer_lock(self, name) -> dict:
        def _sync():
            doc = self._require_doc()
            lyr = doc.layers.get(name)
            if lyr:
                lyr.lock()
                self._mark_dirty()
            return {"ok": True, "layer": name, "locked": True}
        return await self._async(_sync)

    async def layer_unlock(self, name) -> dict:
        def _sync():
            doc = self._require_doc()
            lyr = doc.layers.get(name)
            if lyr:
                lyr.unlock()
                self._mark_dirty()
            return {"ok": True, "layer": name, "locked": False}
        return await self._async(_sync)

    async def layer_hide(self, name) -> dict:
        def _sync():
            doc = self._require_doc()
            lyr = doc.layers.get(name)
            if lyr:
                lyr.off()
                self._mark_dirty()
            return {"ok": True, "layer": name, "visible": False}
        return await self._async(_sync)

    async def layer_show(self, name) -> dict:
        def _sync():
            doc = self._require_doc()
            lyr = doc.layers.get(name)
            if lyr:
                lyr.on()
                self._mark_dirty()
            return {"ok": True, "layer": name, "visible": True}
        return await self._async(_sync)

    # ── linetype management ───────────────────────────────────────────────────

    async def linetype_list(self) -> list[str]:
        def _sync():
            doc = self._require_doc()
            return [lt.dxf.name for lt in doc.linetypes]
        return await self._async(_sync)

    async def linetype_load(self, name, file=None) -> dict:
        # Loads a single linetype. Lookup order:
        # 1. ezdxf.tools.standards (ISO: CENTER, DASHED, DASHDOT, PHANTOM, ...)
        # 2. AutoCAD fallback table (HIDDEN, BORDER and *2/X2 variants)
        # The `file` parameter is accepted for API parity with COM but ignored
        # — ezdxf does not ship a .lin parser.
        from ezdxf.tools import standards as ezdxf_standards
        del file
        def _sync():
            doc = self._require_doc()
            existing = {lt.dxf.name.lower() for lt in doc.linetypes}
            if name.lower() in existing:
                return {"ok": True, "name": name, "already_loaded": True}
            for lt_name, description, pattern in ezdxf_standards.linetypes():
                if lt_name.lower() == name.lower():
                    doc.linetypes.add(lt_name, pattern=pattern, description=description)
                    self._mark_dirty()
                    return {"ok": True, "name": lt_name,
                            "source": "ezdxf.tools.standards"}
            if _add_linetype_from_fallback(doc, name):
                self._mark_dirty()
                return {"ok": True, "name": name.upper(),
                        "source": "autocad_fallback"}
            raise RuntimeError(
                f"Linetype '{name}' is not in ezdxf's standard set or the "
                "AutoCAD fallback table. Available standards: CENTER*, DASHED*, "
                "DASHDOT*, PHANTOM*, DOT*, DIVIDE*. Available fallbacks: "
                "HIDDEN*, BORDER*. Custom .lin files require the COM backend."
            )
        return await self._async(_sync)

    # ── block operations ──────────────────────────────────────────────────────

    async def block_list(self) -> list[BlockInfo]:
        def _sync():
            doc = self._require_doc()
            blocks = []
            for blk in doc.blocks:
                if blk.name.startswith("*"):
                    continue
                attr_count = sum(
                    1 for e in blk if e.dxftype() == "ATTDEF"
                )
                origin = blk.block.dxf.get("base_point", (0.0, 0.0, 0.0))
                is_xref = bool(blk.block.dxf.get("xref_path", ""))
                blocks.append(BlockInfo(
                    name=blk.name,
                    origin=(origin[0], origin[1]),
                    attribute_count=attr_count,
                    entity_count=len(list(blk)),
                    is_xref=is_xref,
                ))
            return blocks
        return await self._async(_sync)

    async def block_insert(
        self, name, x, y, scale_x=1.0, scale_y=1.0, rotation=0.0,
        attributes=None, layer=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            attribs: dict = {
                "xscale": float(scale_x),
                "yscale": float(scale_y),
                "rotation": float(rotation),
            }
            if layer:
                attribs["layer"] = layer
            if attributes:
                ref = msp.add_auto_blockref(
                    name, (float(x), float(y)), attributes, dxfattribs=attribs
                )
            else:
                ref = msp.add_blockref(name, (float(x), float(y)), dxfattribs=attribs)
            self._mark_dirty()
            return _entity_info_dxf(ref)
        return await self._async(_sync)

    async def block_explode(self, handle) -> dict:
        def _sync():
            ent = self._get_entity(handle)
            if ent.dxftype() != "INSERT":
                raise RuntimeError(f"Entity {handle} is not a block reference (INSERT)")
            msp = self._msp()
            # Decompose: add individual entities to modelspace
            inserted = []
            for sub in ent.virtual_entities():
                sub_copy = sub.copy()
                msp.add_entity(sub_copy)
                inserted.append(sub_copy.dxf.handle)
            msp.delete_entity(ent)
            self._mark_dirty()
            return {"ok": True, "inserted_handles": inserted}
        return await self._async(_sync)

    async def block_get_attributes(self, handle) -> dict:
        def _sync():
            ent = self._get_entity(handle)
            if ent.dxftype() != "INSERT":
                raise RuntimeError(f"Entity {handle} is not a block reference")
            result = {}
            for attrib in ent.attribs:
                result[attrib.dxf.tag] = attrib.dxf.text
            return result
        return await self._async(_sync)

    async def block_set_attributes(self, handle, attributes) -> dict:
        def _sync():
            ent = self._get_entity(handle)
            if ent.dxftype() != "INSERT":
                raise RuntimeError(f"Entity {handle} is not a block reference")
            updated = []
            for attrib in ent.attribs:
                tag = attrib.dxf.tag
                if tag in attributes:
                    attrib.dxf.text = str(attributes[tag])
                    updated.append(tag)
            self._mark_dirty()
            return {"ok": True, "updated_tags": updated}
        return await self._async(_sync)

    async def block_create_from_entities(
        self, name, handles, base_x=0.0, base_y=0.0,
    ) -> dict:
        def _sync():
            doc = self._require_doc()
            blk = doc.blocks.new(name=name)
            blk.block.dxf.base_point = (float(base_x), float(base_y), 0.0)
            count = 0
            for handle in handles:
                try:
                    ent = self._get_entity(handle)
                    copy = ent.copy()
                    blk.add_entity(copy)
                    count += 1
                except Exception as exc:
                    log.debug("adding entity %s to block %s: %s", handle, name, exc)
            self._mark_dirty()
            return {"ok": True, "name": name, "entity_count": count}
        return await self._async(_sync)

    # ── analysis / query ──────────────────────────────────────────────────────

    async def analysis_stats(self) -> dict:
        def _sync():
            msp = self._msp()
            type_counts: dict[str, int] = {}
            layer_counts: dict[str, int] = {}
            for ent in msp:
                t = ent.dxftype()
                lyr = ent.dxf.get("layer", "0")
                type_counts[t] = type_counts.get(t, 0) + 1
                layer_counts[lyr] = layer_counts.get(lyr, 0) + 1
            total = sum(type_counts.values())
            return {
                "total_entities": total,
                "by_type": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
                "by_layer": dict(sorted(layer_counts.items(), key=lambda x: -x[1])),
            }
        return await self._async(_sync)

    async def analysis_entities_in_region(
        self, x1, y1, x2, y2,
    ) -> list[EntityInfo]:
        def _sync():
            msp = self._msp()
            results = []
            mn_x, mx_x = min(float(x1), float(x2)), max(float(x1), float(x2))
            mn_y, mx_y = min(float(y1), float(y2)), max(float(y1), float(y2))

            if _BBOX_OK:
                for ent in msp:
                    try:
                        bb = ezdxf_bbox.extents([ent])
                        if bb and bb.extmin.x >= mn_x and bb.extmax.x <= mx_x \
                                and bb.extmin.y >= mn_y and bb.extmax.y <= mx_y:
                            results.append(_entity_info_dxf(ent))
                    except Exception as exc:
                        log.debug("bbox check for entity in region: %s", exc)
                        continue
            else:
                # Fallback: check insertion points
                for ent in msp:
                    try:
                        ins = None
                        if hasattr(ent.dxf, "insert"):
                            ins = ent.dxf.insert
                        elif hasattr(ent.dxf, "start"):
                            ins = ent.dxf.start
                        if ins and mn_x <= ins[0] <= mx_x and mn_y <= ins[1] <= mx_y:
                            results.append(_entity_info_dxf(ent))
                    except Exception as exc:
                        log.debug("insertion point check for entity in region: %s", exc)
                        continue
            return results
        return await self._async(_sync)

    async def analysis_measure_distance(self, x1, y1, x2, y2) -> float:
        return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

    async def analysis_measure_area(self, points) -> float:
        return shoelace_area(points)

    async def analysis_bounding_box(self) -> dict:
        def _sync():
            msp = self._msp()
            if _BBOX_OK:
                try:
                    bb = ezdxf_bbox.extents(msp)
                    if bb:
                        return {
                            "min": [bb.extmin.x, bb.extmin.y],
                            "max": [bb.extmax.x, bb.extmax.y],
                            "width": bb.extmax.x - bb.extmin.x,
                            "height": bb.extmax.y - bb.extmin.y,
                        }
                except Exception as e:
                    return {"error": str(e)}
            return {"error": "ezdxf bbox not available"}
        return await self._async(_sync)

    async def analysis_select_by_layer(self, layer_name) -> list[EntityInfo]:
        def _sync():
            msp = self._msp()
            return [
                _entity_info_dxf(e)
                for e in msp
                if e.dxf.get("layer", "0").lower() == layer_name.lower()
            ]
        return await self._async(_sync)

    async def analysis_select_by_type(self, entity_type) -> list[EntityInfo]:
        def _sync():
            msp = self._msp()
            et = entity_type.upper()
            return [
                _entity_info_dxf(e)
                for e in msp
                if et == e.dxftype().upper()
            ]
        return await self._async(_sync)

    # ── view / screenshot ──────────────────────────────────────────────────────

    async def view_zoom_extents(self) -> dict:
        return {"ok": True, "message": "Zoom extents not applicable for ezdxf backend (no display)"}

    async def view_zoom_window(self, x1, y1, x2, y2) -> dict:
        return {"ok": True, "message": "Zoom window not applicable for ezdxf backend (no display)"}

    async def view_screenshot(self) -> bytes | None:
        """Render drawing to PNG using matplotlib."""
        def _sync():
            try:
                doc = self._require_doc()
                msp = self._msp()
                import matplotlib.pyplot as plt
                from ezdxf.addons.drawing import Frontend, RenderContext
                from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

                fig = plt.figure(figsize=(16, 9), dpi=100)
                ax = fig.add_axes([0, 0, 1, 1])
                ctx = RenderContext(doc)
                out = MatplotlibBackend(ax)
                Frontend(ctx, out).draw_layout(msp, finalize=True)

                buf = io.BytesIO()
                fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
                plt.close(fig)
                buf.seek(0)
                return buf.read()
            except ImportError:
                log.warning("matplotlib not installed – screenshot unavailable")
                return None
        return await self._async(_sync)

    # ── transactions ──────────────────────────────────────────────────────────

    async def transaction_begin(self) -> dict:
        def _sync():
            doc = self._require_doc()
            fd, tmp_path = tempfile.mkstemp(suffix=".dxf", prefix="acad_mcp_undo_")
            os.close(fd)
            try:
                doc.saveas(tmp_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._undo_stack.append(Path(tmp_path))
            max_stack = config.settings.max_undo_stack
            while len(self._undo_stack) > max_stack:
                old = self._undo_stack.pop(0)
                try:
                    old.unlink()
                except OSError:
                    pass
            return {"ok": True, "message": "Transaction begun (DXF snapshot saved for rollback)"}
        return await self._async(_sync)

    async def transaction_commit(self) -> dict:
        if not self._undo_stack:
            return {"ok": False, "error": "No active transaction"}
        p = self._undo_stack.pop()
        try:
            p.unlink()
        except OSError:
            pass
        return {"ok": True, "message": "Transaction committed (snapshot discarded)"}

    async def transaction_rollback(self) -> dict:
        if not self._undo_stack:
            return {"ok": False, "error": "No active transaction to rollback"}
        def _sync():
            p = self._undo_stack.pop()
            try:
                self._doc = ezdxf.readfile(str(p))
                self._dirty = True
            finally:
                try:
                    p.unlink()
                except OSError:
                    pass
            return {"ok": True, "message": "Transaction rolled back to snapshot"}
        return await self._async(_sync)

    # ── system ────────────────────────────────────────────────────────────────

    async def system_status(self) -> dict:
        has_doc = self._doc is not None
        return {
            "backend": "ezdxf",
            "connected": True,
            "has_document": has_doc,
            "document_path": self._doc_path,
            "unsaved_changes": self._dirty,
            "transaction_depth": len(self._undo_stack),
            "capabilities": [
                "file_read_write", "dxf_export", "pdf_export",
                "entity_creation", "layer_management", "blocks",
                "dimensions", "analysis", "screenshot_matplotlib",
            ],
        }

    async def system_get_variable(self, name) -> Any:
        def _sync():
            doc = self._require_doc()
            return doc.header.get(f"${name.upper()}", None)
        return await self._async(_sync)

    async def system_set_variable(self, name, value) -> dict:
        def _sync():
            doc = self._require_doc()
            doc.header[f"${name.upper()}"] = value
            self._mark_dirty()
            return {"ok": True, "variable": name, "value": value}
        return await self._async(_sync)

    async def system_run_command(self, command) -> dict:
        return {
            "ok": False,
            "error": "system_run_command not supported in ezdxf backend (no live AutoCAD)",
        }

    async def system_run_lisp(self, expression) -> dict:
        return {
            "ok": False,
            "error": "system_run_lisp not supported in ezdxf backend (no live AutoCAD)",
        }
