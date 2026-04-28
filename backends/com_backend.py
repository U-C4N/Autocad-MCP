"""AutoCAD COM backend – live AutoCAD control via pywin32.

All COM calls are routed through a single-threaded executor to satisfy
AutoCAD's STA (Single-Threaded Apartment) COM requirement.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import config

from .base import (
    AutoCADBackend,
    BlockInfo,
    DrawingInfo,
    EntityInfo,
    LayerInfo,
    deg2rad,
    rad2deg,
    shoelace_area,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional COM imports
# ---------------------------------------------------------------------------

_WIN32_AVAILABLE = sys.platform == "win32"

if _WIN32_AVAILABLE:
    try:
        import pythoncom
        import pywintypes
        import win32com.client
        import win32con  # noqa: F401
        import win32gui
        import win32ui
        _COM_IMPORTS_OK = True
    except ImportError:
        _COM_IMPORTS_OK = False
else:
    _COM_IMPORTS_OK = False

try:
    from PIL import Image as PILImage
    _PIL_OK = True
except ImportError:
    _PIL_OK = False


# ---------------------------------------------------------------------------
# COM thread-state (accessed ONLY from the COM executor thread)
# ---------------------------------------------------------------------------

_COM_STATE: dict[str, Any] = {}   # keys: "app"


def _com_init():
    """COM thread initializer – called once by the ThreadPoolExecutor."""
    if _COM_IMPORTS_OK:
        pythoncom.CoInitialize()


def _apoint(x: float, y: float, z: float = 0.0):
    """Create a VARIANT double-array point for AutoCAD COM."""
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8,
        [float(x), float(y), float(z)],
    )


def _av(values: list[float]):
    """Create a VARIANT double-array from a flat list."""
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8,
        [float(v) for v in values],
    )


def _ai(values: list[int]):
    """Create a VARIANT short-int array."""
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_I2,
        list(values),
    )


def _acad_app():
    """Return (or lazily create) the AutoCAD Application COM object.
    Must only be called from the COM executor thread."""
    if "app" not in _COM_STATE:
        try:
            _COM_STATE["app"] = win32com.client.GetActiveObject("AutoCAD.Application")
        except Exception as exc:
            log.debug("GetActiveObject failed, trying Dispatch: %s", exc)
            try:
                _COM_STATE["app"] = win32com.client.Dispatch("AutoCAD.Application")
                _COM_STATE["app"].Visible = True
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot connect to AutoCAD: {exc}. "
                    "Make sure AutoCAD is installed and running."
                ) from exc
    return _COM_STATE["app"]


def _acad_doc():
    """Return active AutoCAD document."""
    app = _acad_app()
    if app.Documents.Count == 0:
        raise RuntimeError("No drawing is open in AutoCAD. Open or create a .dwg file first.")
    return app.ActiveDocument


def _msp():
    """Return ModelSpace of active document."""
    return _acad_doc().ModelSpace


_BUILTIN_LINETYPES = {"continuous", "bylayer", "byblock"}


def _ensure_linetype_loaded(name: str) -> None:
    """If `name` is not already loaded, load it via -LINETYPE behind FILEDIA=0.

    Called by attribute setters so users can write `linetype="CENTER"` without
    having to remember to load it first. Must run on the COM thread.
    """
    if not name or name.lower() in _BUILTIN_LINETYPES:
        return
    doc = _acad_doc()
    existing = {doc.Linetypes.Item(i).Name.lower()
                for i in range(doc.Linetypes.Count)}
    if name.lower() in existing:
        return
    app = _acad_app()
    try:
        measurement = int(app.GetVariable("MEASUREMENT"))
    except Exception as exc:
        log.debug("MEASUREMENT read failed, defaulting to acadiso: %s", exc)
        measurement = 1
    lin_file = "acadiso.lin" if measurement == 1 else "acad.lin"
    try:
        old_filedia = int(app.GetVariable("FILEDIA"))
    except Exception as exc:
        log.debug("FILEDIA read failed, assuming 1: %s", exc)
        old_filedia = 1
    try:
        app.SetVariable("FILEDIA", 0)
        doc.SendCommand(f"_-LINETYPE _LOAD {name} {lin_file}\n\n")
    finally:
        try:
            app.SetVariable("FILEDIA", old_filedia)
        except Exception as exc:
            log.debug("FILEDIA restore failed: %s", exc)


def _apply_entity_attrs(entity, layer: str | None, color: int | None, linetype: str | None):
    """Apply common entity attributes after creation."""
    if layer is not None:
        entity.Layer = layer
    if color is not None:
        entity.Color = int(color)
    if linetype is not None:
        _ensure_linetype_loaded(linetype)
        entity.Linetype = linetype


def _regen():
    """Regen the active viewport so new entities appear immediately on screen."""
    if _COM_STATE.get("batch_mode"):
        return
    try:
        _acad_doc().Regen(0)   # 0 = acActiveViewport
    except Exception as exc:
        log.debug("Regen failed: %s", exc)


def _entity_info(entity) -> EntityInfo:
    """Convert a COM entity object to EntityInfo dataclass."""
    try:
        bb_min, bb_max = entity.GetBoundingBox()
        props = {
            "bounding_box": {
                "min": [bb_min[0], bb_min[1]],
                "max": [bb_max[0], bb_max[1]],
            }
        }
    except Exception as exc:
        log.debug("GetBoundingBox failed for entity: %s", exc)
        props = {}

    # Add type-specific properties
    obj_name = entity.ObjectName
    try:
        if obj_name == "AcDbLine":
            sp = entity.StartPoint
            ep = entity.EndPoint
            props["start"] = [sp[0], sp[1]]
            props["end"] = [ep[0], ep[1]]
            props["length"] = entity.Length
        elif obj_name == "AcDbCircle":
            ctr = entity.Center
            props["center"] = [ctr[0], ctr[1]]
            props["radius"] = entity.Radius
        elif obj_name == "AcDbArc":
            ctr = entity.Center
            props["center"] = [ctr[0], ctr[1]]
            props["radius"] = entity.Radius
            props["start_angle"] = rad2deg(entity.StartAngle)
            props["end_angle"] = rad2deg(entity.EndAngle)
        elif obj_name in ("AcDbLWPolyline", "AcDb2dPolyline"):
            coords = list(entity.Coordinates)
            pts = [[coords[i], coords[i+1]] for i in range(0, len(coords), 2)]
            props["points"] = pts
            props["closed"] = bool(entity.Closed)
            props["length"] = entity.Length
        elif obj_name == "AcDbText":
            props["text"] = entity.TextString
            ins = entity.InsertionPoint
            props["insertion"] = [ins[0], ins[1]]
            props["height"] = entity.Height
        elif obj_name == "AcDbMText":
            props["text"] = entity.Contents
            ins = entity.InsertionPoint
            props["insertion"] = [ins[0], ins[1]]
        elif obj_name == "AcDbBlockReference":
            ins = entity.InsertionPoint
            props["insertion"] = [ins[0], ins[1]]
            props["block_name"] = entity.Name
            props["x_scale"] = entity.XScaleFactor
            props["y_scale"] = entity.YScaleFactor
            props["rotation_deg"] = rad2deg(entity.Rotation)
    except Exception as exc:
        log.debug("Type-specific entity properties extraction failed: %s", exc)

    ent_type = obj_name.replace("AcDb", "").upper()
    try:
        linetype = entity.Linetype
    except Exception as exc:
        log.debug("Linetype read failed, using ByLayer: %s", exc)
        linetype = "ByLayer"

    return EntityInfo(
        handle=entity.Handle,
        type=ent_type,
        layer=entity.Layer,
        color=entity.Color,
        linetype=linetype,
        visible=bool(entity.Visible),
        properties=props,
    )


def _layer_info(layer, current_layer_name: str) -> LayerInfo:
    """Convert a COM layer object to LayerInfo."""
    return LayerInfo(
        name=layer.Name,
        color=layer.Color,
        linetype=layer.Linetype,
        lineweight=layer.LineWeight,
        is_on=bool(layer.LayerOn),
        is_frozen=bool(layer.Freeze),
        is_locked=bool(layer.Lock),
        is_current=(layer.Name == current_layer_name),
    )


# ---------------------------------------------------------------------------
# Screenshot helpers (COM thread only)
# ---------------------------------------------------------------------------

def _find_autocad_hwnd() -> int | None:
    """Find the main AutoCAD window handle."""
    if not _WIN32_AVAILABLE:
        return None
    result = {"hwnd": None}

    def _enum_cb(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if "AutoCAD" in title and win32gui.IsWindowVisible(hwnd):
            result["hwnd"] = hwnd
            return False
        return True

    try:
        win32gui.EnumWindows(_enum_cb, None)
    except Exception as exc:
        log.debug("EnumWindows failed while finding AutoCAD hwnd: %s", exc)
    return result["hwnd"]


def _capture_window(hwnd: int) -> bytes | None:
    """Capture an AutoCAD window using GDI PrintWindow."""
    if not _WIN32_AVAILABLE or not _PIL_OK:
        return None
    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    save_bmp = None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bmp = win32ui.CreateBitmap()
        save_bmp.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bmp)

        # PW_RENDERFULLCONTENT = 2 (works for hardware-accelerated windows)
        import ctypes
        ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)

        bmp_str = save_bmp.GetBitmapBits(True)
        img = PILImage.frombuffer("RGB", (width, height), bmp_str, "raw", "BGRX", 0, 1)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        log.warning("screenshot_failed: %s", exc)
        return None
    finally:
        try:
            if save_bmp is not None:
                win32gui.DeleteObject(save_bmp.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception as exc:
            log.debug("GDI resource cleanup failed: %s", exc)


# ---------------------------------------------------------------------------
# ComBackend
# ---------------------------------------------------------------------------


class ComBackend(AutoCADBackend):
    """Live AutoCAD control via COM API."""

    def __init__(self):
        self._executor: ThreadPoolExecutor | None = None
        self._batch_mode = False
        self._connected = False
        self._transaction_active = False

    @property
    def name(self) -> str:
        return "com"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if not _COM_IMPORTS_OK:
            raise RuntimeError(
                "pywin32 not available. Install with: pip install pywin32"
            )
        # Lazy connection: just set up the executor. AutoCAD is connected
        # on the first actual tool call so the server starts even if AutoCAD
        # is not open yet — it will be found as soon as the user opens it.
        self._executor = ThreadPoolExecutor(max_workers=1, initializer=_com_init)
        self._connected = True
        log.info("COM backend ready (will connect to AutoCAD on first tool call)")

    async def disconnect(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
        self._connected = False

    def _sync_test_connection(self):
        """Verify AutoCAD is reachable (runs in COM thread)."""
        app = _acad_app()
        _ = app.Version  # raises if disconnected

    async def _run(self, func, *args, **kwargs):
        """Run a callable in the COM executor thread, with a hard timeout.

        Without the timeout, an unresponsive AutoCAD (modal dialog, long Regen,
        crashed COM bridge) would block the single-thread STA executor forever
        and freeze every subsequent tool call.
        """
        if self._executor is None:
            raise RuntimeError("ComBackend not connected. Call connect() first.")
        loop = asyncio.get_running_loop()
        timeout = config.settings.com_call_timeout
        future = loop.run_in_executor(
            self._executor, lambda: func(*args, **kwargs)
        )
        try:
            if timeout > 0:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        except asyncio.TimeoutError as e:
            log.error("COM call timed out after %.1fs; rebuilding executor", timeout)
            # The worker thread is still blocked inside SendCommand and cannot be
            # cancelled. Abandon it (shutdown(wait=False)) and start a fresh
            # single-thread executor so the next call doesn't queue behind the
            # stuck one and time out too. Drop the cached COM app for the same
            # reason — the new thread must re-Dispatch in its own apartment.
            stuck = self._executor
            self._executor = ThreadPoolExecutor(max_workers=1, initializer=_com_init)
            if stuck is not None:
                stuck.shutdown(wait=False)
            _COM_STATE.pop("app", None)
            self._connected = False
            raise RuntimeError(
                f"AutoCAD did not respond within {timeout:.0f}s. The application "
                "may be showing a modal dialog or have an active command prompt "
                "(press ESC in AutoCAD). Increase COM_CALL_TIMEOUT if a long "
                "operation is expected."
            ) from e
        except pywintypes.com_error as e:
            hr = e.args[0] if e.args else 0
            if hr in (-2147221246, -2147221005, -2147417842):
                _COM_STATE.pop("app", None)
                self._connected = False
            raise RuntimeError(
                f"AutoCAD COM error ({hr:#010x}): "
                f"{e.args[1] if len(e.args) > 1 else e}"
            ) from e

    # ── drawing management ────────────────────────────────────────────────────

    async def drawing_info(self) -> DrawingInfo:
        def _sync():
            doc = _acad_doc()
            app = _acad_app()
            mspace = _msp()

            try:
                ext_min = doc.Database.Extmin
                ext_max = doc.Database.Extmax
                emin = (ext_min[0], ext_min[1])
                emax = (ext_max[0], ext_max[1])
            except Exception as exc:
                log.debug("Database Extmin/Extmax read failed: %s", exc)
                emin = (0.0, 0.0)
                emax = (0.0, 0.0)

            entity_count = mspace.Count
            layer_count = doc.Layers.Count
            block_count = doc.Blocks.Count

            unit_map = {0: "Unitless", 1: "Inches", 2: "Feet", 4: "mm", 5: "cm", 6: "m"}
            try:
                units = unit_map.get(int(app.GetVariable("INSUNITS")), "Unknown")
            except Exception as exc:
                log.debug("INSUNITS variable read failed: %s", exc)
                units = "Unknown"

            return DrawingInfo(
                name=doc.Name,
                full_path=doc.FullName,
                saved=bool(doc.Saved),
                entity_count=entity_count,
                layer_count=layer_count,
                block_count=block_count,
                extents_min=emin,
                extents_max=emax,
                units=units,
                version=app.Version,
                backend="com",
            )
        return await self._run(_sync)

    async def drawing_new(self, template: str | None = None) -> dict:
        def _sync():
            app = _acad_app()
            if template:
                doc = app.Documents.Add(template)
            else:
                doc = app.Documents.Add()
            return {"ok": True, "name": doc.Name}
        return await self._run(_sync)

    async def drawing_open(self, path: str) -> dict:
        def _sync():
            app = _acad_app()
            doc = app.Documents.Open(path)
            return {"ok": True, "name": doc.Name, "path": doc.FullName}
        return await self._run(_sync)

    async def drawing_save(self, path: str | None = None) -> dict:
        def _sync():
            doc = _acad_doc()
            if path:
                doc.SaveAs(path)
            else:
                doc.Save()
            return {"ok": True, "path": doc.FullName}
        return await self._run(_sync)

    async def drawing_save_as(self, path: str, fmt: str = "dwg") -> dict:
        def _sync():
            doc = _acad_doc()
            # AutoCAD SaveAs format constants: 12=DWG R2010, 61=DXF R2010
            fmt_map = {"dwg": 12, "dxf": 61, "dwt": 5}
            acad_fmt = fmt_map.get(fmt.lower(), 12)
            doc.SaveAs(path, acad_fmt)
            return {"ok": True, "path": path, "format": fmt}
        return await self._run(_sync)

    async def drawing_export_dxf(self, path: str) -> dict:
        return await self.drawing_save_as(path, "dxf")

    async def drawing_export_pdf(self, path: str) -> dict:
        def _sync():
            doc = _acad_doc()
            plot = doc.Plot
            plot.PlotToFile(path, "DWG To PDF.pc3")
            return {"ok": True, "path": path}
        return await self._run(_sync)

    async def drawing_purge(self) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.PurgeAll()
            return {"ok": True, "message": "Drawing purged"}
        return await self._run(_sync)

    async def drawing_audit(self) -> dict:
        def _sync():
            doc = _acad_doc()
            app = _acad_app()
            app.SetVariable("AUDITCTL", 1)
            doc.SendCommand("_AUDIT Y\n")
            return {"ok": True, "message": "Audit completed"}
        return await self._run(_sync)

    async def drawing_close(self, save: bool = True) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.Close(save)
            return {"ok": True}
        return await self._run(_sync)

    async def drawing_undo(self) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.SendCommand("_UNDO 1\n")
            return {"ok": True, "message": "Undo applied"}
        return await self._run(_sync)

    async def drawing_redo(self) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.SendCommand("_REDO\n")
            return {"ok": True, "message": "Redo applied"}
        return await self._run(_sync)

    # ── entity creation ───────────────────────────────────────────────────────

    async def entity_create_line(
        self, x1, y1, x2, y2, z1=0.0, z2=0.0,
        layer=None, color=None, linetype=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            line = mspace.AddLine(_apoint(x1, y1, z1), _apoint(x2, y2, z2))
            _apply_entity_attrs(line, layer, color, linetype)
            _regen()
            return _entity_info(line)
        return await self._run(_sync)

    async def entity_create_circle(
        self, cx, cy, radius, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            circle = mspace.AddCircle(_apoint(cx, cy), float(radius))
            _apply_entity_attrs(circle, layer, color, None)
            _regen()
            return _entity_info(circle)
        return await self._run(_sync)

    async def entity_create_arc(
        self, cx, cy, radius, start_angle, end_angle, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            arc = mspace.AddArc(
                _apoint(cx, cy), float(radius),
                deg2rad(start_angle), deg2rad(end_angle),
            )
            _apply_entity_attrs(arc, layer, color, None)
            _regen()
            return _entity_info(arc)
        return await self._run(_sync)

    async def entity_create_polyline(
        self, points, closed=False, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            flat = []
            for pt in points:
                flat.extend([float(pt[0]), float(pt[1])])
            pline = mspace.AddLightWeightPolyline(_av(flat))
            pline.Closed = closed
            _apply_entity_attrs(pline, layer, color, None)
            _regen()
            return _entity_info(pline)
        return await self._run(_sync)

    async def entity_create_text(
        self, text, x, y, height=2.5, rotation=0.0, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            txt = mspace.AddText(text, _apoint(x, y), float(height))
            txt.Rotation = deg2rad(rotation)
            _apply_entity_attrs(txt, layer, color, None)
            _regen()
            return _entity_info(txt)
        return await self._run(_sync)

    async def entity_create_mtext(
        self, text, x, y, width=100.0, height=2.5, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            mt = mspace.AddMText(_apoint(x, y), float(width), text)
            mt.Height = float(height)
            _apply_entity_attrs(mt, layer, color, None)
            _regen()
            return _entity_info(mt)
        return await self._run(_sync)

    async def entity_create_hatch(
        self, pattern, boundary_points, scale=1.0, angle=0.0,
        layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            # acPatternTypePreDefined = 0
            hatch = mspace.AddHatch(0, pattern, True)
            hatch.PatternScale = float(scale)
            hatch.PatternAngle = deg2rad(angle)
            # Build outer loop as a temporary lwpolyline
            flat = []
            for pt in boundary_points:
                flat.extend([float(pt[0]), float(pt[1])])
            bnd_pline = mspace.AddLightWeightPolyline(_av(flat))
            bnd_pline.Closed = True
            outer = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [bnd_pline]
            )
            hatch.AppendOuterLoop(outer)
            hatch.Evaluate()
            bnd_pline.Delete()
            _apply_entity_attrs(hatch, layer, color, None)
            _regen()
            return _entity_info(hatch)
        return await self._run(_sync)

    async def entity_create_spline(
        self, fit_points, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            flat = []
            for pt in fit_points:
                flat.extend([float(pt[0]), float(pt[1]), 0.0])
            sp = mspace.AddSpline(
                _av(flat),
                _apoint(0, 1),   # start tangent
                _apoint(0, 1),   # end tangent
            )
            _apply_entity_attrs(sp, layer, color, None)
            _regen()
            return _entity_info(sp)
        return await self._run(_sync)

    async def entity_create_ellipse(
        self, cx, cy, major_x, major_y, ratio=0.5, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            ellipse = mspace.AddEllipse(
                _apoint(cx, cy),
                _apoint(major_x, major_y),  # major axis vector
                float(ratio),               # ratio of minor to major axis
            )
            _apply_entity_attrs(ellipse, layer, color, None)
            _regen()
            return _entity_info(ellipse)
        return await self._run(_sync)

    async def entity_create_point(
        self, x, y, layer=None, color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            pt = mspace.AddPoint(_apoint(x, y))
            _apply_entity_attrs(pt, layer, color, None)
            _regen()
            return _entity_info(pt)
        return await self._run(_sync)

    async def entity_create_block_ref(
        self, name, x, y, scale_x=1.0, scale_y=1.0, rotation=0.0, layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            ref = mspace.InsertBlock(
                _apoint(x, y), name,
                float(scale_x), float(scale_y), 1.0,
                deg2rad(rotation),
            )
            if layer:
                ref.Layer = layer
            _regen()
            return _entity_info(ref)
        return await self._run(_sync)

    # ── dimensions ────────────────────────────────────────────────────────────

    async def dimension_linear(
        self, x1, y1, x2, y2, dim_x, dim_y, rotation=0.0, layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimLinear(
                _apoint(x1, y1), _apoint(x2, y2),
                _apoint(dim_x, dim_y), deg2rad(rotation),
            )
            if layer:
                dim.Layer = layer
            _regen()
            return _entity_info(dim)
        return await self._run(_sync)

    async def dimension_aligned(
        self, x1, y1, x2, y2, dim_x, dim_y, layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimAligned(
                _apoint(x1, y1), _apoint(x2, y2), _apoint(dim_x, dim_y)
            )
            if layer:
                dim.Layer = layer
            _regen()
            return _entity_info(dim)
        return await self._run(_sync)

    async def dimension_angular(
        self, vx, vy, x1, y1, x2, y2, tx, ty, layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimAngular(
                _apoint(vx, vy),
                _apoint(x1, y1), _apoint(x2, y2),
                _apoint(tx, ty),
            )
            if layer:
                dim.Layer = layer
            _regen()
            return _entity_info(dim)
        return await self._run(_sync)

    async def dimension_radius(
        self, cx, cy, chord_x, chord_y, leader_length=10.0, layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimRadial(
                _apoint(cx, cy), _apoint(chord_x, chord_y), float(leader_length)
            )
            if layer:
                dim.Layer = layer
            _regen()
            return _entity_info(dim)
        return await self._run(_sync)

    async def dimension_diameter(
        self, x1, y1, x2, y2, leader_length=10.0, layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimDiametric(
                _apoint(x1, y1), _apoint(x2, y2), float(leader_length)
            )
            if layer:
                dim.Layer = layer
            _regen()
            return _entity_info(dim)
        return await self._run(_sync)

    # ── entity modification ───────────────────────────────────────────────────

    async def entity_move(self, handle, dx, dy, dz=0.0) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            ent.Move(_apoint(0, 0, 0), _apoint(dx, dy, dz))
            return {"ok": True, "handle": handle}
        return await self._run(_sync)

    async def entity_copy(self, handle, dx, dy, dz=0.0) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            copy = ent.Copy()
            copy.Move(_apoint(0, 0, 0), _apoint(dx, dy, dz))
            return _entity_info(copy)
        return await self._run(_sync)

    async def entity_rotate(self, handle, base_x, base_y, angle_deg) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            ent.Rotate(_apoint(base_x, base_y), deg2rad(angle_deg))
            return {"ok": True, "handle": handle}
        return await self._run(_sync)

    async def entity_scale(self, handle, base_x, base_y, factor) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            ent.ScaleEntity(_apoint(base_x, base_y), float(factor))
            return {"ok": True, "handle": handle}
        return await self._run(_sync)

    async def entity_mirror(
        self, handle, x1, y1, x2, y2, delete_original=False,
    ) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            mirrored = ent.Mirror(_apoint(x1, y1), _apoint(x2, y2))
            if delete_original:
                ent.Delete()
            return _entity_info(mirrored)
        return await self._run(_sync)

    async def entity_offset(
        self, handle, distance, side_x=None, side_y=None,
    ) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            offset_ent = ent.Offset(float(distance))
            # Offset returns a variant array of entities
            try:
                first = offset_ent[0]
            except Exception as exc:
                log.debug("Offset result indexing failed, using raw result: %s", exc)
                first = offset_ent
            return _entity_info(first)
        return await self._run(_sync)

    async def entity_delete(self, handle) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            ent.Delete()
            return {"ok": True, "deleted_handle": handle}
        return await self._run(_sync)

    async def entity_array_rectangular(
        self, handle, rows, cols, row_spacing, col_spacing,
    ) -> list[EntityInfo]:
        def _sync():
            self._batch_mode = True
            _COM_STATE["batch_mode"] = True
            try:
                doc = _acad_doc()
                ent = doc.HandleToObject(handle)
                result = ent.ArrayRectangular(
                    int(rows), int(cols), 1,  # numLevels=1
                    float(row_spacing), float(col_spacing), 0.0,
                )
                entities = []
                try:
                    for e in result:
                        entities.append(_entity_info(e))
                except Exception as exc:
                    log.debug("ArrayRectangular iteration failed, using single result: %s", exc)
                    entities.append(_entity_info(result))
                return entities
            finally:
                self._batch_mode = False
                _COM_STATE["batch_mode"] = False
                _regen()
        return await self._run(_sync)

    async def entity_array_polar(
        self, handle, count, fill_angle, center_x, center_y,
    ) -> list[EntityInfo]:
        def _sync():
            self._batch_mode = True
            _COM_STATE["batch_mode"] = True
            try:
                doc = _acad_doc()
                ent = doc.HandleToObject(handle)
                result = ent.ArrayPolar(
                    int(count), deg2rad(fill_angle), _apoint(center_x, center_y)
                )
                entities = []
                try:
                    for e in result:
                        entities.append(_entity_info(e))
                except Exception as exc:
                    log.debug("ArrayPolar iteration failed, using single result: %s", exc)
                    entities.append(_entity_info(result))
                return entities
            finally:
                self._batch_mode = False
                _COM_STATE["batch_mode"] = False
                _regen()
        return await self._run(_sync)

    # ── entity query / properties ──────────────────────────────────────────────

    async def entity_get(self, handle) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            return _entity_info(ent)
        return await self._run(_sync)

    async def entity_set_properties(
        self, handle, layer=None, color=None, linetype=None,
        lineweight=None, visible=None,
    ) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            if layer is not None:
                ent.Layer = layer
            if color is not None:
                ent.Color = int(color)
            if linetype is not None:
                _ensure_linetype_loaded(linetype)
                ent.Linetype = linetype
            if lineweight is not None:
                ent.LineWeight = int(lineweight)
            if visible is not None:
                ent.Visible = bool(visible)
            return {"ok": True, "handle": handle}
        return await self._run(_sync)

    async def entity_list(
        self, type_filter=None, layer_filter=None, limit=200, offset=0,
    ) -> list[EntityInfo]:
        def _sync():
            mspace = _msp()
            results = []
            total = mspace.Count
            collected = 0
            skipped = 0
            for i in range(total):
                try:
                    ent = mspace.Item(i)
                    ent_type = ent.ObjectName.replace("AcDb", "").upper()
                    ent_layer = ent.Layer

                    if type_filter and type_filter.upper() != ent_type:
                        continue
                    if layer_filter and layer_filter.lower() != ent_layer.lower():
                        continue

                    if skipped < offset:
                        skipped += 1
                        continue

                    results.append(_entity_info(ent))
                    collected += 1
                    if collected >= limit:
                        break
                except Exception as exc:
                    log.debug("entity_list: skip entity at index %d: %s", i, exc)
                    continue
            return results
        return await self._run(_sync)

    # ── layer management ──────────────────────────────────────────────────────

    async def layer_list(self) -> list[LayerInfo]:
        def _sync():
            doc = _acad_doc()
            current = doc.ActiveLayer.Name
            layers = []
            for i in range(doc.Layers.Count):
                lyr = doc.Layers.Item(i)
                layers.append(_layer_info(lyr, current))
            return layers
        return await self._run(_sync)

    async def layer_create(
        self, name, color=7, linetype="Continuous", lineweight=-3,
    ) -> LayerInfo:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Add(name)
            lyr.Color = int(color)
            _ensure_linetype_loaded(linetype)
            try:
                lyr.Linetype = linetype
            except Exception as exc:
                log.warning("Failed to set linetype '%s' on layer '%s': %s", linetype, name, exc)
            lyr.LineWeight = int(lineweight)
            return _layer_info(lyr, doc.ActiveLayer.Name)
        return await self._run(_sync)

    async def layer_delete(self, name) -> dict:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            lyr.Delete()
            return {"ok": True, "deleted": name}
        return await self._run(_sync)

    async def layer_set_current(self, name) -> dict:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            doc.ActiveLayer = lyr
            return {"ok": True, "current_layer": name}
        return await self._run(_sync)

    async def layer_modify(
        self, name, color=None, linetype=None, lineweight=None,
    ) -> LayerInfo:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            if color is not None:
                lyr.Color = int(color)
            if linetype is not None:
                _ensure_linetype_loaded(linetype)
                lyr.Linetype = linetype
            if lineweight is not None:
                lyr.LineWeight = int(lineweight)
            return _layer_info(lyr, doc.ActiveLayer.Name)
        return await self._run(_sync)

    async def layer_freeze(self, name) -> dict:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            lyr.Freeze = True
            return {"ok": True, "layer": name, "frozen": True}
        return await self._run(_sync)

    async def layer_thaw(self, name) -> dict:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            lyr.Freeze = False
            return {"ok": True, "layer": name, "frozen": False}
        return await self._run(_sync)

    async def layer_lock(self, name) -> dict:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            lyr.Lock = True
            return {"ok": True, "layer": name, "locked": True}
        return await self._run(_sync)

    async def layer_unlock(self, name) -> dict:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            lyr.Lock = False
            return {"ok": True, "layer": name, "locked": False}
        return await self._run(_sync)

    async def layer_hide(self, name) -> dict:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            lyr.LayerOn = False
            return {"ok": True, "layer": name, "visible": False}
        return await self._run(_sync)

    async def layer_show(self, name) -> dict:
        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            lyr.LayerOn = True
            return {"ok": True, "layer": name, "visible": True}
        return await self._run(_sync)

    # ── linetype management ───────────────────────────────────────────────────

    async def linetype_list(self) -> list[str]:
        def _sync():
            doc = _acad_doc()
            return [doc.Linetypes.Item(i).Name for i in range(doc.Linetypes.Count)]
        return await self._run(_sync)

    async def linetype_load(self, name, file=None) -> dict:
        # Loads a single linetype from a .lin file. Two things would otherwise
        # bite the user: (1) AutoCAD pops a "Select Linetype File" dialog when
        # FILEDIA=1 and the file path can't be auto-resolved — that modal
        # dialog deadlocks SendCommand; (2) ISO/metric drawings need acadiso.lin,
        # imperial drawings need acad.lin. We pick automatically from MEASUREMENT
        # if the caller didn't specify a file.
        def _sync():
            app = _acad_app()
            doc = _acad_doc()

            existing = {doc.Linetypes.Item(i).Name.lower()
                        for i in range(doc.Linetypes.Count)}
            if name.lower() in existing:
                return {"ok": True, "name": name, "already_loaded": True}

            if file is None:
                try:
                    measurement = int(app.GetVariable("MEASUREMENT"))
                except Exception as exc:
                    log.debug("MEASUREMENT read failed, defaulting to acadiso: %s", exc)
                    measurement = 1
                lin_file = "acadiso.lin" if measurement == 1 else "acad.lin"
            else:
                lin_file = file

            try:
                old_filedia = int(app.GetVariable("FILEDIA"))
            except Exception as exc:
                log.debug("FILEDIA read failed, assuming 1: %s", exc)
                old_filedia = 1
            try:
                app.SetVariable("FILEDIA", 0)
                # Double newline: first ends the file name, second exits
                # -LINETYPE's [?/Create/Load/Set] option menu.
                doc.SendCommand(f"_-LINETYPE _LOAD {name} {lin_file}\n\n")
            finally:
                try:
                    app.SetVariable("FILEDIA", old_filedia)
                except Exception as exc:
                    log.debug("FILEDIA restore failed: %s", exc)

            after = {doc.Linetypes.Item(i).Name.lower()
                     for i in range(doc.Linetypes.Count)}
            if name.lower() not in after:
                raise RuntimeError(
                    f"Failed to load linetype '{name}' from '{lin_file}'. "
                    "Check the linetype name spelling and that the .lin file is "
                    "on AutoCAD's support path."
                )
            return {"ok": True, "name": name, "file": lin_file}
        return await self._run(_sync)

    # ── block operations ──────────────────────────────────────────────────────

    async def block_list(self) -> list[BlockInfo]:
        def _sync():
            doc = _acad_doc()
            blocks = []
            for i in range(doc.Blocks.Count):
                blk = doc.Blocks.Item(i)
                if blk.Name.startswith("*"):  # skip *Model_Space, *Paper_Space, etc.
                    continue
                attr_count = sum(
                    1 for j in range(blk.Count)
                    if blk.Item(j).ObjectName == "AcDbAttributeDefinition"
                )
                blocks.append(BlockInfo(
                    name=blk.Name,
                    origin=(blk.Origin[0], blk.Origin[1]),
                    attribute_count=attr_count,
                    entity_count=blk.Count,
                    is_xref=bool(blk.IsXRef),
                ))
            return blocks
        return await self._run(_sync)

    async def block_insert(
        self, name, x, y, scale_x=1.0, scale_y=1.0, rotation=0.0,
        attributes=None, layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            ref = mspace.InsertBlock(
                _apoint(x, y), name,
                float(scale_x), float(scale_y), 1.0,
                deg2rad(rotation),
            )
            if layer:
                ref.Layer = layer
            if attributes:
                try:
                    attrs = ref.GetAttributes()
                    for attr in attrs:
                        tag = attr.TagString
                        if tag in attributes:
                            attr.TextString = str(attributes[tag])
                except Exception as exc:
                    log.debug("Block insert: GetAttributes or attribute setting failed: %s", exc)
            return _entity_info(ref)
        return await self._run(_sync)

    async def block_explode(self, handle) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            ent.Explode()
            return {"ok": True, "exploded_handle": handle}
        return await self._run(_sync)

    async def block_get_attributes(self, handle) -> dict:
        def _sync():
            doc = _acad_doc()
            ref = doc.HandleToObject(handle)
            attrs = ref.GetAttributes()
            result = {}
            for attr in attrs:
                result[attr.TagString] = attr.TextString
            return result
        return await self._run(_sync)

    async def block_set_attributes(self, handle, attributes) -> dict:
        def _sync():
            doc = _acad_doc()
            ref = doc.HandleToObject(handle)
            attrs = ref.GetAttributes()
            updated = []
            for attr in attrs:
                tag = attr.TagString
                if tag in attributes:
                    attr.TextString = str(attributes[tag])
                    updated.append(tag)
            return {"ok": True, "updated_tags": updated}
        return await self._run(_sync)

    async def block_create_from_entities(
        self, name, handles, base_x=0.0, base_y=0.0,
    ) -> dict:
        return {
            "ok": False,
            "error": "block_create_from_entities not supported in COM backend. "
                     "Use system_run_command with _BLOCK instead.",
        }

    # ── analysis / query ──────────────────────────────────────────────────────

    async def analysis_stats(self) -> dict:
        def _sync():
            mspace = _msp()
            type_counts: dict[str, int] = {}
            layer_counts: dict[str, int] = {}
            for i in range(mspace.Count):
                try:
                    ent = mspace.Item(i)
                    t = ent.ObjectName.replace("AcDb", "")
                    lyr = ent.Layer
                    type_counts[t] = type_counts.get(t, 0) + 1
                    layer_counts[lyr] = layer_counts.get(lyr, 0) + 1
                except Exception as exc:
                    log.debug("analysis_stats: skip entity at index %d: %s", i, exc)
                    continue
            return {
                "total_entities": mspace.Count,
                "by_type": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
                "by_layer": dict(sorted(layer_counts.items(), key=lambda x: -x[1])),
            }
        return await self._run(_sync)

    async def analysis_entities_in_region(
        self, x1, y1, x2, y2,
    ) -> list[EntityInfo]:
        def _sync():
            doc = _acad_doc()
            ss_name = f"_REGION_{uuid.uuid4().hex[:8]}"
            ss = doc.SelectionSets.Add(ss_name)
            try:
                ss.SelectCrossing(_apoint(x1, y1), _apoint(x2, y2))
                results = []
                for i in range(ss.Count):
                    try:
                        results.append(_entity_info(ss.Item(i)))
                    except Exception as exc:
                        log.debug("analysis_entities_in_region: skip selection item %d: %s", i, exc)
                        continue
                return results
            finally:
                try:
                    ss.Delete()
                except Exception as exc:
                    log.debug("SelectionSet cleanup failed: %s", exc)
        return await self._run(_sync)

    async def analysis_measure_distance(self, x1, y1, x2, y2) -> float:
        return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

    async def analysis_measure_area(self, points) -> float:
        return shoelace_area(points)

    async def analysis_bounding_box(self) -> dict:
        def _sync():
            doc = _acad_doc()
            try:
                emin = doc.Database.Extmin
                emax = doc.Database.Extmax
                return {
                    "min": [emin[0], emin[1]],
                    "max": [emax[0], emax[1]],
                    "width": emax[0] - emin[0],
                    "height": emax[1] - emin[1],
                }
            except Exception as exc:
                log.debug("analysis_bounding_box: Database Extmin/Extmax read failed: %s", exc)
                return {"error": str(exc)}
        return await self._run(_sync)

    async def analysis_select_by_layer(self, layer_name) -> list[EntityInfo]:
        def _sync():
            doc = _acad_doc()
            ss_name = f"_BYLAYER_{uuid.uuid4().hex[:8]}"
            ss = doc.SelectionSets.Add(ss_name)
            try:
                ft = _ai([8])   # group code 8 = layer
                fv = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, [layer_name]
                )
                ss.SelectAll(ft, fv)
                return [_entity_info(ss.Item(i)) for i in range(ss.Count)]
            finally:
                try:
                    ss.Delete()
                except Exception as exc:
                    log.debug("SelectionSet cleanup failed: %s", exc)
        return await self._run(_sync)

    async def analysis_select_by_type(self, entity_type) -> list[EntityInfo]:
        def _sync():
            _acad_doc()
            mspace = _msp()
            results = []
            et_upper = entity_type.upper()
            for i in range(mspace.Count):
                try:
                    ent = mspace.Item(i)
                    if et_upper == ent.ObjectName.replace("AcDb", "").upper():
                        results.append(_entity_info(ent))
                except Exception as exc:
                    log.debug("analysis_select_by_type: skip entity at index %d: %s", i, exc)
                    continue
            return results
        return await self._run(_sync)

    # ── view / screenshot ──────────────────────────────────────────────────────

    async def view_zoom_extents(self) -> dict:
        def _sync():
            app = _acad_app()
            app.ZoomExtents()
            _acad_doc().Regen(0)
            return {"ok": True}
        return await self._run(_sync)

    async def view_zoom_window(self, x1, y1, x2, y2) -> dict:
        def _sync():
            app = _acad_app()
            app.ZoomWindow(_apoint(x1, y1), _apoint(x2, y2))
            return {"ok": True}
        return await self._run(_sync)

    async def view_screenshot(self) -> bytes | None:
        def _sync():
            hwnd = _find_autocad_hwnd()
            if hwnd is None:
                return None
            return _capture_window(hwnd)
        return await self._run(_sync)

    # ── transactions ──────────────────────────────────────────────────────────

    async def transaction_begin(self) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.StartUndoMark()
            return {"ok": True, "message": "Transaction begun (AutoCAD undo mark set)"}
        result = await self._run(_sync)
        self._transaction_active = True
        return result

    async def transaction_commit(self) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.EndUndoMark()
            return {"ok": True, "message": "Transaction committed"}
        result = await self._run(_sync)
        self._transaction_active = False
        return result

    async def transaction_rollback(self) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.EndUndoMark()
            doc.SendCommand("_UNDO B\n")  # Back to last undo mark
            return {"ok": True, "message": "Transaction rolled back"}
        result = await self._run(_sync)
        self._transaction_active = False
        return result

    # ── system ──────────────────────────────────────────────────────────────

    async def system_status(self) -> dict:
        tx_active = self._transaction_active
        def _sync():
            try:
                app = _acad_app()
                version = app.Version
                doc_count = app.Documents.Count
                active_doc = app.ActiveDocument.Name if doc_count > 0 else None
                return {
                    "backend": "com",
                    "connected": True,
                    "autocad_version": version,
                    "open_documents": doc_count,
                    "active_document": active_doc,
                    "transaction_active": tx_active,
                    "capabilities": [
                        "live_control", "screenshot", "transactions",
                        "com_api", "lisp_execution", "all_entity_types",
                    ],
                }
            except Exception as exc:
                log.debug("system_status: _acad_app check failed: %s", exc)
                return {"backend": "com", "connected": False, "error": str(exc)}
        return await self._run(_sync)

    async def system_get_variable(self, name) -> Any:
        def _sync():
            app = _acad_app()
            return app.GetVariable(name)
        return await self._run(_sync)

    async def system_set_variable(self, name, value) -> dict:
        def _sync():
            app = _acad_app()
            app.SetVariable(name, value)
            return {"ok": True, "variable": name, "value": value}
        return await self._run(_sync)

    async def system_run_command(self, command) -> dict:
        # Commands ending in option menus (e.g. -LINETYPE returning to its
        # [?/Create/Load/Set] prompt) need a trailing blank line or _X to exit;
        # otherwise SendCommand returns but AutoCAD stays at a prompt and the
        # next COM call deadlocks. Example: "_-LINETYPE _LOAD CENTER acad.lin\n\n"
        def _sync():
            app = _acad_app()
            doc = _acad_doc()
            try:
                cmd_active = int(app.GetVariable("CMDACTIVE"))
            except Exception as exc:
                log.debug("CMDACTIVE read failed, proceeding anyway: %s", exc)
                cmd_active = 0
            if cmd_active:
                raise RuntimeError(
                    "AutoCAD has an active command or prompt (CMDACTIVE="
                    f"{cmd_active}). Press ESC in AutoCAD to cancel, then retry."
                )
            cmd = command if command.endswith("\n") else command + "\n"
            doc.SendCommand(cmd)
            return {"ok": True, "command": command}
        return await self._run(_sync)

    async def system_run_lisp(self, expression) -> dict:
        def _sync():
            doc = _acad_doc()
            result = doc.SendCommand(f"(progn {expression})\n")
            return {"ok": True, "expression": expression, "result": str(result) if result else "nil"}
        return await self._run(_sync)
