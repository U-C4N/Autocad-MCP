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
    CapabilityMap,
    DrawingInfo,
    EntityInfo,
    FeatureCapability,
    LayerInfo,
    deg2rad,
    normalize_lineweight,
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

_COM_STATE: dict[str, Any] = {}  # keys: "app"


def _com_init():
    """COM thread initializer – called once by the ThreadPoolExecutor."""
    if _COM_IMPORTS_OK:
        pythoncom.CoInitialize()


def _com_teardown():
    """COM thread finalizer — drop the cached app and release the STA apartment.

    Submitted to the executor (R5) so the CoInitialize done in _com_init is
    paired with a CoUninitialize when the worker is torn down or a stuck
    SendCommand eventually unblocks, instead of leaking the apartment forever.
    """
    _COM_STATE.pop("app", None)
    if _COM_IMPORTS_OK:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


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


def _solid_3d_capability() -> FeatureCapability:
    """3D solids are opt-in (ENABLE_3D=true) even on the live COM backend."""
    if config.settings.enable_3d:
        return FeatureCapability(True, "native")
    return FeatureCapability(False, reason="opt_in_disabled:set ENABLE_3D=true")


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
                    f"Cannot connect to AutoCAD: {exc}. Make sure AutoCAD is installed and running."
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


def _get_sysvar(name: str):
    """Read a system variable. GetVariable lives on AcadDocument, not AcadApplication."""
    return _acad_doc().GetVariable(name)


def _set_sysvar(name: str, value) -> None:
    """Write a system variable. SetVariable lives on AcadDocument, not AcadApplication."""
    _acad_doc().SetVariable(name, value)


_BUILTIN_LINETYPES = {"continuous", "bylayer", "byblock"}

# ActiveX ObjectName -> DXF-style type name. Callers filter with DXF names
# ("LWPOLYLINE", "INSERT"), which is also what the ezdxf backend reports, but
# AutoCAD's COM ObjectName uses different spellings ("AcDbPolyline",
# "AcDbBlockReference"); stripping the "AcDb" prefix is not enough.
_OBJECT_NAME_TO_TYPE = {
    "AcDbPolyline": "LWPOLYLINE",
    "AcDbLWPolyline": "LWPOLYLINE",
    "AcDb2dPolyline": "POLYLINE",
    "AcDb3dPolyline": "POLYLINE",
    "AcDbBlockReference": "INSERT",
    "AcDbMInsertBlock": "INSERT",
    "AcDbAttributeDefinition": "ATTDEF",
    "AcDbRotatedDimension": "DIMENSION",
    "AcDbAlignedDimension": "DIMENSION",
    "AcDb2LineAngularDimension": "DIMENSION",
    "AcDb3PointAngularDimension": "DIMENSION",
    "AcDbRadialDimension": "DIMENSION",
    "AcDbDiametricDimension": "DIMENSION",
    "AcDbOrdinateDimension": "DIMENSION",
    "AcDbMText": "MTEXT",
    "AcDbMLeader": "MULTILEADER",
    "AcDbRasterImage": "IMAGE",
    "AcDbProxyEntity": "ACAD_PROXY_ENTITY",
    "AcDb3dSolid": "3DSOLID",
}


def _canonical_type(object_name: str) -> str:
    """Map an ActiveX ObjectName to the DXF-style type name callers filter on."""
    mapped = _OBJECT_NAME_TO_TYPE.get(object_name)
    if mapped:
        return mapped
    return object_name.replace("AcDb", "").upper()


def _ensure_linetype_loaded(name: str) -> None:
    """If `name` is not already loaded, load it through ``Linetypes.Load``.

    Called by attribute setters so users can write `linetype="CENTER"` without
    having to remember to load it first. Must run on the COM thread.

    Uses the collection method rather than SendCommand("_-LINETYPE ..."):
    SendCommand only returns once AutoCAD is idle and hangs outright on a
    freshly created document, stalling the COM thread until the call timeout.
    """
    if not name or name.lower() in _BUILTIN_LINETYPES:
        return
    doc = _acad_doc()
    existing = {doc.Linetypes.Item(i).Name.lower() for i in range(doc.Linetypes.Count)}
    if name.lower() in existing:
        return
    try:
        measurement = int(_get_sysvar("MEASUREMENT"))
    except Exception as exc:
        log.debug("MEASUREMENT read failed, defaulting to acadiso: %s", exc)
        measurement = 1
    lin_file = "acadiso.lin" if measurement == 1 else "acad.lin"
    doc.Linetypes.Load(name, lin_file)


def _ensure_layer_exists(name: str) -> None:
    """Create the layer if the drawing doesn't have it yet.

    Assigning ``entity.Layer`` to an unknown name is a hard COM error, so every
    create-with-layer call would fail unless the caller had run layer_create
    first. The ezdxf backend auto-creates, so this keeps the two in step.
    """
    if not name:
        return
    doc = _acad_doc()
    existing = {doc.Layers.Item(i).Name.lower() for i in range(doc.Layers.Count)}
    if name.lower() not in existing:
        doc.Layers.Add(name)


def _apply_entity_attrs(entity, layer: str | None, color: int | None, linetype: str | None):
    """Apply common entity attributes after creation."""
    if layer is not None:
        _ensure_layer_exists(layer)
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
        _acad_doc().Regen(0)  # 0 = acActiveViewport
    except Exception as exc:
        log.debug("Regen failed: %s", exc)


def _apply_dim_tolerance(dim, tol_upper, tol_lower, tol_mode="none", text_override=None):
    """Best-effort ISO 129 tolerance rendering on a live COM dimension.

    Maps the shared tolerance spec (see engineering/tolerances.py) to the COM
    Dimension object's per-entity properties (ToleranceDisplay / ToleranceUpper
    / ToleranceLower, or the Limits display) plus an optional TextOverride. COM
    property names vary by AutoCAD version, so each set is guarded — a missing
    property degrades gracefully instead of failing the dimension.
    """
    from engineering.tolerances import build_dim_override

    try:
        override, text = build_dim_override(tol_upper, tol_lower, tol_mode, text_override)
    except ValueError:
        raise
    if text:
        try:
            dim.TextOverride = text
        except Exception as exc:
            log.debug("dim TextOverride failed: %s", exc)
    if not override:
        return
    # acTolerance* enums: 0=None, 1=Symmetrical, 2=Deviation, 3=Limits, 4=Basic
    try:
        if "dimgap" in override:  # basic (boxed) dimension
            dim.ToleranceDisplay = 4
            return
        if override.get("dimlim"):  # limit dimension
            dim.ToleranceDisplay = 3
        elif override.get("dimtp") == override.get("dimtm"):
            dim.ToleranceDisplay = 1  # symmetrical ±
        else:
            dim.ToleranceDisplay = 2  # deviation
        if "dimtp" in override:
            dim.ToleranceUpperLimit = float(override["dimtp"])
        if "dimtm" in override:
            dim.ToleranceLowerLimit = float(override["dimtm"])
    except Exception as exc:
        log.debug("dim tolerance apply failed (version-dependent COM props): %s", exc)


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
            # Arc length (N3) — parity with ezdxf so length_range selects ARCs.
            _sweep = (entity.EndAngle - entity.StartAngle) % (2.0 * math.pi)
            props["length"] = entity.Radius * _sweep
        elif obj_name in ("AcDbPolyline", "AcDbLWPolyline", "AcDb2dPolyline"):
            coords = list(entity.Coordinates)
            pts = [[coords[i], coords[i + 1]] for i in range(0, len(coords), 2)]
            props["points"] = pts
            props["closed"] = bool(entity.Closed)
            props["length"] = entity.Length
        elif obj_name == "AcDbText":
            props["text"] = entity.TextString
            ins = entity.InsertionPoint
            props["insertion"] = [ins[0], ins[1]]
            props["height"] = entity.Height
            props["rotation"] = rad2deg(entity.Rotation)  # N5 parity with ezdxf
        elif obj_name == "AcDbMText":
            props["text"] = entity.Contents
            ins = entity.InsertionPoint
            props["insertion"] = [ins[0], ins[1]]
            # N5 parity with ezdxf: MTEXT char_height + rotation.
            props["char_height"] = entity.Height
            props["rotation"] = rad2deg(entity.Rotation)
        elif obj_name == "AcDbBlockReference":
            ins = entity.InsertionPoint
            props["insertion"] = [ins[0], ins[1]]
            props["block_name"] = entity.Name
            props["x_scale"] = entity.XScaleFactor
            props["y_scale"] = entity.YScaleFactor
            props["rotation_deg"] = rad2deg(entity.Rotation)
    except Exception as exc:
        log.debug("Type-specific entity properties extraction failed: %s", exc)

    ent_type = _canonical_type(obj_name)
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
    # R28: prefer the COM application's real main-frame handle. The EnumWindows
    # title-substring scan can match a palette / About dialog / Start tab, so it
    # is only a fallback for when the HWND read fails.
    try:
        return int(_acad_app().HWND)
    except Exception as exc:
        log.debug("Application.HWND read failed, falling back to EnumWindows: %s", exc)
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
        self._connected = False
        self._transaction_active = False
        self._document_scope_key: tuple[str, str] | None = None

    @property
    def name(self) -> str:
        return "com"

    @property
    def is_connected(self) -> bool:
        return self._connected

    def capabilities(self) -> CapabilityMap:
        return CapabilityMap(
            backend="com",
            features={
                "drawing_2d": FeatureCapability(True, "native"),
                "dxf": FeatureCapability(True, "native"),
                "pdf": FeatureCapability(True, "native"),
                "png": FeatureCapability(True, "native"),
                "transactions": FeatureCapability(True, "undo_mark"),
                "table": FeatureCapability(True, "native"),
                "mleader": FeatureCapability(True, "native"),
                "preflight": FeatureCapability(True, "shared"),
                "refiner": FeatureCapability(True, "shared"),
                "delivery": FeatureCapability(True, "shared"),
                "paper_space": FeatureCapability(True, "native"),
                "viewport_render": FeatureCapability(True, "native"),
                "solid_3d": _solid_3d_capability(),
                "lisp": FeatureCapability(True, "sanitized"),
            },
        )

    async def connect(self) -> None:
        if not _COM_IMPORTS_OK:
            raise RuntimeError("pywin32 not available. Install with: pip install pywin32")
        # Lazy connection: just set up the executor. AutoCAD is connected
        # on the first actual tool call so the server starts even if AutoCAD
        # is not open yet — it will be found as soon as the user opens it.
        self._executor = ThreadPoolExecutor(max_workers=1, initializer=_com_init)
        self._connected = True
        log.info("COM backend ready (will connect to AutoCAD on first tool call)")

    async def disconnect(self) -> None:
        if self._executor:
            try:
                self._executor.submit(_com_teardown)  # release COM apartment (R5)
            except Exception:
                pass
            self._executor.shutdown(wait=False)
            self._executor = None
        self._document_scope_key = None
        self._transaction_active = False
        self._reset_document_state()
        self._connected = False

    async def _ensure_document_state(self) -> None:
        """Reset drawing-scoped metadata when AutoCAD's active document changes."""

        def _sync():
            app = _acad_app()
            if app.Documents.Count == 0:
                key = None
            else:
                doc = app.ActiveDocument
                key = (str(doc.Name), str(doc.FullName))
            if key != self._document_scope_key:
                self._document_scope_key = key
                self._transaction_active = False
                self._reset_document_state()

        await self._run(_sync)

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
        future = loop.run_in_executor(self._executor, lambda: func(*args, **kwargs))
        try:
            if timeout > 0:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        except TimeoutError as e:
            log.error("COM call timed out after %.1fs; rebuilding executor", timeout)
            # The worker thread is still blocked inside SendCommand and cannot be
            # cancelled. Abandon it (shutdown(wait=False)) and start a fresh
            # single-thread executor so the next call doesn't queue behind the
            # stuck one and time out too. Drop the cached COM app for the same
            # reason — the new thread must re-Dispatch in its own apartment.
            stuck = self._executor
            self._executor = ThreadPoolExecutor(max_workers=1, initializer=_com_init)
            if stuck is not None:
                # When the stuck SendCommand eventually unblocks, the queued
                # teardown releases its COM apartment instead of leaking it (R5).
                try:
                    stuck.submit(_com_teardown)
                except Exception:
                    pass
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
                f"AutoCAD COM error ({hr:#010x}): {e.args[1] if len(e.args) > 1 else e}"
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
                units = unit_map.get(int(_get_sysvar("INSUNITS")), "Unknown")
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

        result = await self._run(_sync)
        await self._ensure_document_state()
        return result

    async def drawing_open(self, path: str) -> dict:
        def _sync():
            app = _acad_app()
            doc = app.Documents.Open(path)
            return {"ok": True, "name": doc.Name, "path": doc.FullName}

        result = await self._run(_sync)
        await self._ensure_document_state()
        return result

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
            # AcSaveAsType constants: 48=ac2010_dwg, 49=ac2010_dxf,
            # 50=ac2010_Template. The previous map used 12 (ac2000_dwg),
            # 61 (ac2013_dxf) and 5 (acR13_dxf, not a template at all), so
            # every save was written in the wrong version and "dwt" produced
            # an R13 DXF body under a .dwt name.
            fmt_map = {"dwg": 48, "dxf": 49, "dwt": 50}
            acad_fmt = fmt_map.get(fmt.lower(), 48)
            doc.SaveAs(path, acad_fmt)
            return {"ok": True, "path": path, "format": fmt}

        return await self._run(_sync)

    async def drawing_export_dxf(self, path: str) -> dict:
        return await self.drawing_save_as(path, "dxf")

    async def drawing_export_pdf(self, path: str, layout: str | None = None) -> dict:
        def _sync():
            doc = _acad_doc()
            previous = None
            if layout:
                previous = doc.ActiveLayout.Name
                if previous != layout:
                    doc.ActiveLayout = doc.Layouts.Item(layout)
            try:
                plot = doc.Plot
                plot.PlotToFile(path, "DWG To PDF.pc3")
                result = {"ok": True, "path": path}
                if layout:
                    result["layout"] = layout
                return result
            finally:
                if previous is not None and previous != layout:
                    doc.ActiveLayout = doc.Layouts.Item(previous)

        return await self._run(_sync)

    # ── layouts / paper space ────────────────────────────────────────────────

    async def layout_list(self) -> dict:
        def _sync():
            doc = _acad_doc()
            names = [doc.Layouts.Item(i).Name for i in range(doc.Layouts.Count)]
            return {"ok": True, "layouts": names, "current": doc.ActiveLayout.Name}

        return await self._run(_sync)

    async def layout_create(self, name: str) -> dict:
        def _sync():
            doc = _acad_doc()
            existing = {doc.Layouts.Item(i).Name for i in range(doc.Layouts.Count)}
            if name in existing:
                return {"ok": False, "error": f"Layout already exists: {name}"}
            doc.Layouts.Add(name)
            return {"ok": True, "layout": name}

        return await self._run(_sync)

    async def layout_set_current(self, name: str) -> dict:
        def _sync():
            doc = _acad_doc()
            try:
                doc.ActiveLayout = doc.Layouts.Item(name)
            except Exception:
                return {"ok": False, "error": f"Layout not found: {name}"}
            return {"ok": True, "current": name}

        return await self._run(_sync)

    async def viewport_create(
        self,
        layout: str,
        center_x: float,
        center_y: float,
        width: float,
        height: float,
        view_center_x: float,
        view_center_y: float,
        scale: float = 1.0,
    ) -> dict:
        def _sync():
            if scale <= 0:
                return {"ok": False, "error": "scale must be > 0 (paper:model, e.g. 0.5 for 1:2)"}
            doc = _acad_doc()
            if layout == "Model":
                return {"ok": False, "error": "Viewports require a paper-space layout"}
            try:
                target = doc.Layouts.Item(layout)
            except Exception:
                return {"ok": False, "error": f"Paper-space layout not found: {layout}"}
            previous = doc.ActiveLayout.Name
            if previous != layout:
                doc.ActiveLayout = target
            try:
                viewport = doc.PaperSpace.AddPViewport(
                    _apoint(center_x, center_y), float(width), float(height)
                )
                viewport.Display(True)
                try:
                    viewport.CustomScale = float(scale)
                except Exception:  # some verticals expose StandardScale only
                    pass
                # AcadPViewport has no "ViewCenter" — the model-space point the
                # viewport looks at is Target (with Direction left at the
                # default plan view). Setting the non-existent property used to
                # fail silently, leaving every viewport centred on the origin.
                view_centered = False
                try:
                    viewport.Target = _apoint(float(view_center_x), float(view_center_y), 0.0)
                    view_centered = True
                except Exception as exc:
                    log.debug("viewport_create: Target not applied: %s", exc)
                return {
                    "ok": True,
                    "handle": viewport.Handle,
                    "layout": layout,
                    "scale": scale,
                    "view_height": float(height) / float(scale),
                    "view_centered": view_centered,
                }
            finally:
                if previous != layout:
                    doc.ActiveLayout = doc.Layouts.Item(previous)

        return await self._run(_sync)

    # ── 3D solids (native ActiveX; gated behind ENABLE_3D at the tool layer) ─

    async def solid_box(
        self, cx: float, cy: float, cz: float, length: float, width: float, height: float
    ) -> dict:
        def _sync():
            doc = _acad_doc()
            solid = doc.ModelSpace.AddBox(
                _apoint(cx, cy, cz), float(length), float(width), float(height)
            )
            return {"ok": True, "handle": solid.Handle, "type": "3DSOLID"}

        return await self._run(_sync)

    async def solid_cylinder(
        self, cx: float, cy: float, cz: float, radius: float, height: float
    ) -> dict:
        def _sync():
            doc = _acad_doc()
            solid = doc.ModelSpace.AddCylinder(_apoint(cx, cy, cz), float(radius), float(height))
            return {"ok": True, "handle": solid.Handle, "type": "3DSOLID"}

        return await self._run(_sync)

    def _region_from_profile(self, doc, profile_handle: str):
        """Build an ACIS region from a closed profile entity handle."""
        profile = doc.HandleToObject(profile_handle)
        profiles = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [profile])
        regions = doc.ModelSpace.AddRegion(profiles)
        if not regions:
            raise RuntimeError(
                f"AddRegion produced no region from profile {profile_handle} "
                "(the profile must be closed)"
            )
        return regions[0]

    async def solid_extrude(
        self, profile_handle: str, height: float, taper_angle: float = 0.0
    ) -> dict:
        def _sync():
            doc = _acad_doc()
            region = self._region_from_profile(doc, profile_handle)
            solid = doc.ModelSpace.AddExtrudedSolid(
                region, float(height), math.radians(float(taper_angle))
            )
            region.Delete()
            return {"ok": True, "handle": solid.Handle, "type": "3DSOLID"}

        return await self._run(_sync)

    async def solid_revolve(
        self,
        profile_handle: str,
        axis_x1: float,
        axis_y1: float,
        axis_x2: float,
        axis_y2: float,
        angle: float = 360.0,
    ) -> dict:
        def _sync():
            doc = _acad_doc()
            region = self._region_from_profile(doc, profile_handle)
            axis_point = _apoint(axis_x1, axis_y1, 0.0)
            axis_dir = _apoint(axis_x2 - axis_x1, axis_y2 - axis_y1, 0.0)
            solid = doc.ModelSpace.AddRevolvedSolid(
                region, axis_point, axis_dir, math.radians(float(angle))
            )
            region.Delete()
            return {"ok": True, "handle": solid.Handle, "type": "3DSOLID"}

        return await self._run(_sync)

    async def solid_boolean(self, target_handle: str, tool_handle: str, operation: str) -> dict:
        def _sync():
            op = (operation or "").strip().lower()
            # AcBooleanType: acUnion=0, acIntersection=1, acSubtraction=2
            codes = {"union": 0, "intersect": 1, "intersection": 1, "subtract": 2}
            if op not in codes:
                return {
                    "ok": False,
                    "error": f"Unknown boolean operation {operation!r}. "
                    "Use union | subtract | intersect.",
                }
            doc = _acad_doc()
            target = doc.HandleToObject(target_handle)
            tool = doc.HandleToObject(tool_handle)
            target.Boolean(codes[op], tool)
            return {"ok": True, "handle": target.Handle, "operation": op, "type": "3DSOLID"}

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
            # AcadDocument.Audit(FixErrors) instead of SendCommand("_AUDIT Y"):
            # SendCommand only returns once AutoCAD is idle and hangs on a
            # freshly created document. Audit returns the error count.
            errors = doc.Audit(True)
            try:
                error_count = int(errors)
            except (TypeError, ValueError):
                error_count = 0
            return {"ok": True, "error_count": error_count, "fixed": True}

        return await self._run(_sync)

    async def drawing_close(self, save: bool = True) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.Close(save)
            return {"ok": True}

        result = await self._run(_sync)
        await self._ensure_document_state()
        return result

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
        self,
        x1,
        y1,
        x2,
        y2,
        z1=0.0,
        z2=0.0,
        layer=None,
        color=None,
        linetype=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            line = mspace.AddLine(_apoint(x1, y1, z1), _apoint(x2, y2, z2))
            _apply_entity_attrs(line, layer, color, linetype)
            _regen()
            return _entity_info(line)

        return await self._run(_sync)

    async def entity_create_circle(
        self,
        cx,
        cy,
        radius,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            circle = mspace.AddCircle(_apoint(cx, cy), float(radius))
            _apply_entity_attrs(circle, layer, color, None)
            _regen()
            return _entity_info(circle)

        return await self._run(_sync)

    async def entity_create_arc(
        self,
        cx,
        cy,
        radius,
        start_angle,
        end_angle,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            arc = mspace.AddArc(
                _apoint(cx, cy),
                float(radius),
                deg2rad(start_angle),
                deg2rad(end_angle),
            )
            _apply_entity_attrs(arc, layer, color, None)
            _regen()
            return _entity_info(arc)

        return await self._run(_sync)

    async def entity_create_polyline(
        self,
        points,
        closed=False,
        layer=None,
        color=None,
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
        self,
        text,
        x,
        y,
        height=2.5,
        rotation=0.0,
        layer=None,
        color=None,
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
        self,
        text,
        x,
        y,
        width=100.0,
        height=2.5,
        rotation=0.0,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            mt = mspace.AddMText(_apoint(x, y), float(width), text)
            mt.Height = float(height)
            if rotation:
                mt.Rotation = deg2rad(rotation)  # NEW-mtext-1: honor caller rotation
            _apply_entity_attrs(mt, layer, color, None)
            _regen()
            return _entity_info(mt)

        return await self._run(_sync)

    async def entity_create_table(
        self,
        x,
        y,
        rows,
        headers=None,
        column_widths=None,
        row_height=7.0,
        text_height=2.5,
        title=None,
        layer="TEXT",
    ) -> EntityInfo:
        from engineering.annotation import prepare_table_layout

        layout = prepare_table_layout(rows, headers, column_widths, row_height, text_height, title)

        def _sync():
            mspace = _msp()
            table = mspace.AddTable(
                _apoint(x, y),
                layout.row_count,
                layout.column_count,
                layout.row_height,
                layout.column_widths[0],
            )
            for column, width in enumerate(layout.column_widths):
                table.SetColumnWidth(column, width)
            for row_index, row in enumerate(layout.cells):
                table.SetRowHeight(row_index, layout.row_height)
                for column_index, value in enumerate(row):
                    table.SetText(row_index, column_index, value)
            try:
                table.TextHeight = layout.text_height
            except Exception as exc:
                log.debug("setting table TextHeight failed: %s", exc)
            _apply_entity_attrs(table, layer, None, None)
            _regen()
            info = _entity_info(table)
            info.type = "TABLE"
            info.layer = layer
            info.properties.update(
                {
                    "representation": "native",
                    "logical_group_id": f"table:{uuid.uuid4().hex}",
                    "child_handles": [info.handle],
                    "rows": layout.row_count,
                    "columns": layout.column_count,
                    "bounds": {
                        "min": [float(x), float(y) - layout.height],
                        "max": [float(x) + layout.width, float(y)],
                    },
                }
            )
            return info

        return await self._run(_sync)

    async def leader_create_mleader(
        self,
        points,
        text,
        text_height=2.5,
        landing_gap=1.0,
        arrow_size=2.5,
        layer="DIM",
    ) -> EntityInfo:
        from engineering.annotation import validate_mleader

        normalized = validate_mleader(points, text)
        if text_height <= 0 or landing_gap < 0 or arrow_size <= 0:
            raise RuntimeError(
                "leader_create_mleader: text_height/arrow_size must be positive and landing_gap non-negative"
            )

        def _sync():
            flat = []
            for px, py in normalized:
                flat.extend([px, py, 0.0])
            leader = _msp().AddMLeader(_av(flat), 0)
            leader.TextString = str(text)
            leader.TextHeight = float(text_height)
            leader.LandingGap = float(landing_gap)
            leader.ArrowheadSize = float(arrow_size)
            _apply_entity_attrs(leader, layer, None, None)
            _regen()
            info = _entity_info(leader)
            info.type = "MLEADER"
            info.layer = layer
            info.properties.update(
                {
                    "representation": "native",
                    "logical_group_id": f"mleader:{uuid.uuid4().hex}",
                    "child_handles": [info.handle],
                    "points": [list(point) for point in normalized],
                    "text": str(text),
                    "bounds": {
                        "min": [min(p[0] for p in normalized), min(p[1] for p in normalized)],
                        "max": [max(p[0] for p in normalized), max(p[1] for p in normalized)],
                    },
                }
            )
            return info

        return await self._run(_sync)

    async def entity_create_hatch(
        self,
        pattern,
        boundary_points,
        scale=1.0,
        angle=0.0,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            # acPatternTypePreDefined = 0
            hatch = mspace.AddHatch(0, pattern, False)
            hatch.PatternScale = float(scale)
            hatch.PatternAngle = deg2rad(angle)
            # Build outer loop as a temporary lwpolyline
            flat = []
            for pt in boundary_points:
                flat.extend([float(pt[0]), float(pt[1])])
            bnd_pline = mspace.AddLightWeightPolyline(_av(flat))
            bnd_pline.Closed = True
            outer = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [bnd_pline])
            hatch.AppendOuterLoop(outer)
            hatch.Evaluate()
            bnd_pline.Delete()
            _apply_entity_attrs(hatch, layer, color, None)
            _regen()
            return _entity_info(hatch)

        return await self._run(_sync)

    async def entity_create_spline(
        self,
        fit_points,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            flat = []
            for pt in fit_points:
                flat.extend([float(pt[0]), float(pt[1]), 0.0])
            # Null tangents let AutoCAD derive them from the fit points. A fixed
            # (0,1,0) tangent used to be passed for both ends, which bent every
            # spline vertically at its endpoints regardless of the actual data
            # and diverged from the ezdxf backend.
            sp = mspace.AddSpline(
                _av(flat),
                _apoint(0, 0),  # start tangent — none
                _apoint(0, 0),  # end tangent — none
            )
            _apply_entity_attrs(sp, layer, color, None)
            _regen()
            return _entity_info(sp)

        return await self._run(_sync)

    async def entity_create_ellipse(
        self,
        cx,
        cy,
        major_x,
        major_y,
        ratio=0.5,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            ellipse = mspace.AddEllipse(
                _apoint(cx, cy),
                _apoint(major_x, major_y),  # major axis vector
                float(ratio),  # ratio of minor to major axis
            )
            _apply_entity_attrs(ellipse, layer, color, None)
            _regen()
            return _entity_info(ellipse)

        return await self._run(_sync)

    async def entity_create_point(
        self,
        x,
        y,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            pt = mspace.AddPoint(_apoint(x, y))
            _apply_entity_attrs(pt, layer, color, None)
            _regen()
            return _entity_info(pt)

        return await self._run(_sync)

    async def entity_create_block_ref(
        self,
        name,
        x,
        y,
        scale_x=1.0,
        scale_y=1.0,
        rotation=0.0,
        layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            ref = mspace.InsertBlock(
                _apoint(x, y),
                name,
                float(scale_x),
                float(scale_y),
                1.0,
                deg2rad(rotation),
            )
            if layer:
                _ensure_layer_exists(layer)
                ref.Layer = layer
            _regen()
            return _entity_info(ref)

        return await self._run(_sync)

    # ── dimensions ────────────────────────────────────────────────────────────

    async def dimension_linear(
        self,
        x1,
        y1,
        x2,
        y2,
        dim_x,
        dim_y,
        rotation=0.0,
        layer=None,
        tol_upper=None,
        tol_lower=None,
        tol_mode="none",
        text_override=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            # The ActiveX API has no AddDimLinear — rotated linear dims are
            # created with AddDimRotated (GH issue #3).
            dim = mspace.AddDimRotated(
                _apoint(x1, y1),
                _apoint(x2, y2),
                _apoint(dim_x, dim_y),
                deg2rad(rotation),
            )
            if layer:
                _ensure_layer_exists(layer)
                dim.Layer = layer
            _apply_dim_tolerance(dim, tol_upper, tol_lower, tol_mode, text_override)
            _regen()
            return _entity_info(dim)

        return await self._run(_sync)

    async def dimension_aligned(
        self,
        x1,
        y1,
        x2,
        y2,
        dim_x,
        dim_y,
        layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimAligned(_apoint(x1, y1), _apoint(x2, y2), _apoint(dim_x, dim_y))
            if layer:
                _ensure_layer_exists(layer)
                dim.Layer = layer
            _regen()
            return _entity_info(dim)

        return await self._run(_sync)

    async def dimension_angular(
        self,
        vx,
        vy,
        x1,
        y1,
        x2,
        y2,
        tx,
        ty,
        layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimAngular(
                _apoint(vx, vy),
                _apoint(x1, y1),
                _apoint(x2, y2),
                _apoint(tx, ty),
            )
            if layer:
                _ensure_layer_exists(layer)
                dim.Layer = layer
            _regen()
            return _entity_info(dim)

        return await self._run(_sync)

    async def dimension_radius(
        self,
        cx,
        cy,
        chord_x,
        chord_y,
        leader_length=10.0,
        layer=None,
        tol_upper=None,
        tol_lower=None,
        tol_mode="none",
        text_override=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimRadial(
                _apoint(cx, cy), _apoint(chord_x, chord_y), float(leader_length)
            )
            if layer:
                _ensure_layer_exists(layer)
                dim.Layer = layer
            _apply_dim_tolerance(dim, tol_upper, tol_lower, tol_mode, text_override)
            _regen()
            return _entity_info(dim)

        return await self._run(_sync)

    async def dimension_diameter(
        self,
        x1,
        y1,
        x2,
        y2,
        leader_length=10.0,
        layer=None,
        tol_upper=None,
        tol_lower=None,
        tol_mode="none",
        text_override=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            dim = mspace.AddDimDiametric(_apoint(x1, y1), _apoint(x2, y2), float(leader_length))
            if layer:
                _ensure_layer_exists(layer)
                dim.Layer = layer
            _apply_dim_tolerance(dim, tol_upper, tol_lower, tol_mode, text_override)
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
        self,
        handle,
        x1,
        y1,
        x2,
        y2,
        delete_original=False,
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
        self,
        handle,
        distance,
        side_x=None,
        side_y=None,
    ) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)

            def _as_list(res):
                # Offset returns a variant array of entities (1 for line/arc/circle).
                try:
                    return [res[i] for i in range(len(res))]
                except TypeError:
                    return [res]

            def _center(e):
                bb_min, bb_max = e.GetBoundingBox()
                return ((bb_min[0] + bb_max[0]) / 2.0, (bb_min[1] + bb_max[1]) / 2.0)

            d = float(distance)
            # R13: honor side_x/side_y so positive distance means the same side as
            # ezdxf. Offset both ways, keep the copy nearest the side point, delete
            # the rest (also fixes the orphan-extras leak, NEW-com-offset-orphan).
            if side_x is not None and side_y is not None and abs(d) > 1e-12:
                sx, sy = float(side_x), float(side_y)
                pos = _as_list(ent.Offset(abs(d)))
                neg = _as_list(ent.Offset(-abs(d)))

                def _sq_dist(e):
                    cx, cy = _center(e)
                    return (cx - sx) ** 2 + (cy - sy) ** 2

                # A circle/arc offset both ways keeps the same bounding-box
                # centre, so the generic nearest-centre test cannot tell grow
                # from shrink. Decide from the side point's distance to the
                # centre instead, like the ezdxf backend does.
                radial = None
                if _canonical_type(ent.ObjectName) in ("CIRCLE", "ARC"):
                    try:
                        ctr = ent.Center
                        inside = math.hypot(sx - ctr[0], sy - ctr[1]) < float(ent.Radius)
                        radial = (neg, pos) if inside else (pos, neg)
                    except Exception as exc:
                        log.debug("offset: radial side test failed, using bbox: %s", exc)

                if radial is not None:
                    keep, drop = radial
                elif _sq_dist(pos[0]) <= _sq_dist(neg[0]):
                    keep, drop = pos, neg
                else:
                    keep, drop = neg, pos
                for extra in drop + keep[1:]:
                    try:
                        extra.Delete()
                    except Exception:
                        pass
                return _entity_info(keep[0])

            result = _as_list(ent.Offset(d))
            for extra in result[1:]:
                try:
                    extra.Delete()
                except Exception:
                    pass
            return _entity_info(result[0])

        return await self._run(_sync)

    async def entity_delete(self, handle) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            ent.Delete()
            return {"ok": True, "deleted_handle": handle}

        return await self._run(_sync)

    async def entity_array_rectangular(
        self,
        handle,
        rows,
        cols,
        row_spacing,
        col_spacing,
    ) -> list[EntityInfo]:
        def _sync():
            _COM_STATE["batch_mode"] = True
            try:
                doc = _acad_doc()
                ent = doc.HandleToObject(handle)
                result = ent.ArrayRectangular(
                    int(rows),
                    int(cols),
                    1,  # numLevels=1
                    float(row_spacing),
                    float(col_spacing),
                    0.0,
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
                _COM_STATE["batch_mode"] = False
                _regen()

        return await self._run(_sync)

    async def entity_array_polar(
        self,
        handle,
        count,
        fill_angle,
        center_x,
        center_y,
    ) -> list[EntityInfo]:
        def _sync():
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
        self,
        handle,
        layer=None,
        color=None,
        linetype=None,
        lineweight=None,
        visible=None,
    ) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            if layer is not None:
                _ensure_layer_exists(layer)
                ent.Layer = layer
            if color is not None:
                ent.Color = int(color)
            if linetype is not None:
                _ensure_linetype_loaded(linetype)
                ent.Linetype = linetype
            if lineweight is not None:
                ent.LineWeight = normalize_lineweight(lineweight)
            if visible is not None:
                ent.Visible = bool(visible)
            return {"ok": True, "handle": handle}

        return await self._run(_sync)

    async def entity_edit_text(
        self,
        handle,
        text=None,
        height=None,
        rotation=None,
    ) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            name = _canonical_type(ent.ObjectName)
            if name not in ("TEXT", "MTEXT"):
                raise RuntimeError(
                    f"entity_edit_text: handle {handle} is {name}, expected TEXT or MTEXT."
                )
            if text is not None:
                ent.TextString = str(text)
            if height is not None:
                ent.Height = float(height)
            if rotation is not None:
                # MTEXT uses radians for Rotation; DBText/Text uses radians too.
                ent.Rotation = deg2rad(float(rotation))
            _regen()
            return _entity_info(ent)

        return await self._run(_sync)

    async def entity_edit_geometry(
        self,
        handle,
        cx=None,
        cy=None,
        radius=None,
        x1=None,
        y1=None,
        x2=None,
        y2=None,
        start_angle=None,
        end_angle=None,
    ) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            ent = doc.HandleToObject(handle)
            name = _canonical_type(ent.ObjectName)
            if name == "CIRCLE":
                c = ent.Center
                ent.Center = _apoint(
                    float(cx) if cx is not None else c[0],
                    float(cy) if cy is not None else c[1],
                    c[2] if len(c) > 2 else 0.0,
                )
                if radius is not None:
                    ent.Radius = float(radius)
            elif name == "LINE":
                s, e = ent.StartPoint, ent.EndPoint
                ent.StartPoint = _apoint(
                    float(x1) if x1 is not None else s[0],
                    float(y1) if y1 is not None else s[1],
                    s[2] if len(s) > 2 else 0.0,
                )
                ent.EndPoint = _apoint(
                    float(x2) if x2 is not None else e[0],
                    float(y2) if y2 is not None else e[1],
                    e[2] if len(e) > 2 else 0.0,
                )
            elif name == "ARC":
                c = ent.Center
                ent.Center = _apoint(
                    float(cx) if cx is not None else c[0],
                    float(cy) if cy is not None else c[1],
                    c[2] if len(c) > 2 else 0.0,
                )
                if radius is not None:
                    ent.Radius = float(radius)
                if start_angle is not None:
                    ent.StartAngle = deg2rad(float(start_angle))
                if end_angle is not None:
                    ent.EndAngle = deg2rad(float(end_angle))
            else:
                raise RuntimeError(
                    f"entity_edit_geometry: {name} not supported (use CIRCLE, LINE, or ARC)."
                )
            _regen()
            return _entity_info(ent)

        return await self._run(_sync)

    async def entity_list(
        self,
        type_filter=None,
        layer_filter=None,
        limit=200,
        offset=0,
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
                    ent_type = _canonical_type(ent.ObjectName)
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
        self,
        name,
        color=7,
        linetype="Continuous",
        lineweight=-3,
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
            lyr.LineWeight = normalize_lineweight(lineweight)
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
        self,
        name,
        color=None,
        linetype=None,
        lineweight=None,
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
                lyr.LineWeight = normalize_lineweight(lineweight)
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
        # Loads a single linetype from a .lin file through ``Linetypes.Load``,
        # so neither the "Select Linetype File" modal nor SendCommand's
        # idle-wait can deadlock the COM thread. ISO/metric drawings need
        # acadiso.lin, imperial drawings need acad.lin — picked automatically
        # from MEASUREMENT if the caller didn't specify a file.
        def _sync():
            doc = _acad_doc()

            existing = {doc.Linetypes.Item(i).Name.lower() for i in range(doc.Linetypes.Count)}
            if name.lower() in existing:
                return {"ok": True, "name": name, "already_loaded": True}

            if file is None:
                try:
                    measurement = int(_get_sysvar("MEASUREMENT"))
                except Exception as exc:
                    log.debug("MEASUREMENT read failed, defaulting to acadiso: %s", exc)
                    measurement = 1
                lin_file = "acadiso.lin" if measurement == 1 else "acad.lin"
            else:
                lin_file = file

            doc.Linetypes.Load(name, lin_file)

            after = {doc.Linetypes.Item(i).Name.lower() for i in range(doc.Linetypes.Count)}
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
                    1
                    for j in range(blk.Count)
                    if blk.Item(j).ObjectName == "AcDbAttributeDefinition"
                )
                # S3: populate description from the block definition's .Comments.
                try:
                    description = blk.Comments or ""
                except Exception:
                    description = ""
                blocks.append(
                    BlockInfo(
                        name=blk.Name,
                        origin=(blk.Origin[0], blk.Origin[1]),
                        attribute_count=attr_count,
                        entity_count=blk.Count,
                        is_xref=bool(blk.IsXRef),
                        description=description,
                    )
                )
            return blocks

        return await self._run(_sync)

    async def block_insert(
        self,
        name,
        x,
        y,
        scale_x=1.0,
        scale_y=1.0,
        rotation=0.0,
        attributes=None,
        layer=None,
    ) -> EntityInfo:
        def _sync():
            mspace = _msp()
            ref = mspace.InsertBlock(
                _apoint(x, y),
                name,
                float(scale_x),
                float(scale_y),
                1.0,
                deg2rad(rotation),
            )
            if layer:
                _ensure_layer_exists(layer)
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
            # AcadEntity.Explode() copies the block's contents into model space
            # and leaves the original INSERT in place — the caller has to delete
            # it. Without this the drawing ends up with the block reference and
            # its exploded twin stacked on top of each other.
            produced = ent.Explode()
            handles = []
            try:
                for item in produced:
                    handles.append(item.Handle)
            except TypeError:
                if produced is not None:
                    handles.append(produced.Handle)
            ent.Delete()
            _regen()
            return {
                "ok": True,
                "exploded_handle": handle,
                "inserted_handles": handles,
                "entity_count": len(handles),
            }

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
        self,
        name,
        handles,
        base_x=0.0,
        base_y=0.0,
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
                    t = _canonical_type(ent.ObjectName)
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
        self,
        x1,
        y1,
        x2,
        y2,
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
            # Database.Extmin/Extmax are only refreshed by a zoom-extents-class
            # operation, so they lag behind entities added since the last one.
            # Measure the real geometry instead, like the ezdxf backend does.
            mspace = _msp()
            xs_min = ys_min = None
            xs_max = ys_max = None
            for i in range(mspace.Count):
                try:
                    bb_min, bb_max = mspace.Item(i).GetBoundingBox()
                except Exception as exc:
                    log.debug("analysis_bounding_box: skip entity at index %d: %s", i, exc)
                    continue
                xs_min = bb_min[0] if xs_min is None else min(xs_min, bb_min[0])
                ys_min = bb_min[1] if ys_min is None else min(ys_min, bb_min[1])
                xs_max = bb_max[0] if xs_max is None else max(xs_max, bb_max[0])
                ys_max = bb_max[1] if ys_max is None else max(ys_max, bb_max[1])

            if xs_min is None:
                return {
                    "min": [0.0, 0.0],
                    "max": [0.0, 0.0],
                    "width": 0.0,
                    "height": 0.0,
                    "empty": True,
                }
            return {
                "min": [xs_min, ys_min],
                "max": [xs_max, ys_max],
                "width": xs_max - xs_min,
                "height": ys_max - ys_min,
            }

        return await self._run(_sync)

    async def analysis_select_by_layer(self, layer_name) -> list[EntityInfo]:
        def _sync():
            doc = _acad_doc()
            ss_name = f"_BYLAYER_{uuid.uuid4().hex[:8]}"
            ss = doc.SelectionSets.Add(ss_name)
            try:
                ft = _ai([8])  # group code 8 = layer
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
                    if et_upper == _canonical_type(ent.ObjectName):
                        results.append(_entity_info(ent))
                except Exception as exc:
                    log.debug("analysis_select_by_type: skip entity at index %d: %s", i, exc)
                    continue
            return results

        return await self._run(_sync)

    async def selection_get(self) -> dict:
        def _sync():
            doc = _acad_doc()

            # PICKFIRST controls whether the noun/verb (grip) pre-selection is
            # captured at all. Surface it so an empty result is explainable.
            try:
                pickfirst = bool(doc.GetVariable("PICKFIRST"))
            except Exception as exc:
                log.debug("selection_get: PICKFIRST read failed: %s", exc)
                pickfirst = None

            try:
                ss = doc.PickfirstSelectionSet
            except Exception as exc:
                log.debug("selection_get: PickfirstSelectionSet unavailable: %s", exc)
                return {
                    "ok": False,
                    "error": f"PickfirstSelectionSet unavailable: {exc}",
                    "count": 0,
                    "handles": [],
                    "entities": [],
                    "pickfirst": pickfirst,
                }

            try:
                count = int(ss.Count)
            except Exception as exc:
                log.debug("selection_get: Count read failed: %s", exc)
                count = 0

            entities = []
            for i in range(count):
                try:
                    entities.append(_entity_info(ss.Item(i)))
                except Exception as exc:
                    log.debug("selection_get: skip selection item %d: %s", i, exc)
                    continue

            handles = [e.handle for e in entities]
            result = {
                "ok": True,
                "count": len(handles),
                "handles": handles,
                "entities": entities,  # _dc()-converted at the server layer
                "pickfirst": pickfirst,
            }
            if not handles:
                result["message"] = (
                    "No entities are pre-selected in the AutoCAD viewport. "
                    "Select entities (so grips appear) before calling, and "
                    "ensure PICKFIRST=1."
                    if pickfirst is not False
                    else "PICKFIRST is 0; the noun/verb grip selection is disabled. "
                    "Set PICKFIRST=1 (system_set_variable) and re-select."
                )
            return result

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
        if self._transaction_active:
            return {"ok": False, "error": "A transaction is already active"}

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

        # R16: clear the flag even if _run raises, so a failed commit can't leave
        # _transaction_active permanently stale.
        try:
            return await self._run(_sync)
        finally:
            self._transaction_active = False

    async def transaction_rollback(self) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.EndUndoMark()
            # R16: route UNDO B through the CMDACTIVE-aware sender instead of a raw
            # SendCommand that could deadlock if a command/prompt is active.
            self._safe_send_command(doc, "_UNDO B")
            return {"ok": True, "message": "Transaction rolled back"}

        try:
            return await self._run(_sync)
        finally:
            self._transaction_active = False

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
                    # Accurate capability list: the backend authors 2D entities
                    # (lines/arcs/circles/polylines/text/hatch/dims/blocks). It
                    # does NOT create 3D solids, so the old "all_entity_types"
                    # claim was false — report "entities_2d" instead.
                    "capabilities": [
                        "live_control",
                        "screenshot",
                        "transactions",
                        "com_api",
                        "lisp_execution",
                        "entities_2d",
                    ],
                }
            except Exception as exc:
                log.debug("system_status: _acad_app check failed: %s", exc)
                return {"backend": "com", "connected": False, "error": str(exc)}

        return await self._run(_sync)

    async def system_get_variable(self, name) -> Any:
        def _sync():
            return _get_sysvar(name)

        return await self._run(_sync)

    async def system_set_variable(self, name, value) -> dict:
        def _sync():
            # Coerce to the sysvar's actual type (NEW-com-set-variable-coercion):
            # AutoCAD rejects e.g. a string "0" for the integer OSMODE. Probe the
            # current value's type and convert to match; pass through on failure.
            coerced = value
            try:
                current = _get_sysvar(name)
                if isinstance(current, bool):
                    coerced = bool(int(value)) if isinstance(value, str) else bool(value)
                elif isinstance(current, int):
                    coerced = int(float(value)) if isinstance(value, str) else int(value)
                elif isinstance(current, float):
                    coerced = float(value)
                elif isinstance(current, (tuple, list)) and isinstance(value, (tuple, list)):
                    # Point/array sysvars (VIEWCTR, TARGET, INSBASE, LIMMIN…)
                    # need an explicit VARIANT array, a plain list is rejected.
                    coerced = _av([float(v) for v in value])
            except Exception as exc:
                log.debug("set_variable type probe for %s failed: %s", name, exc)
            _set_sysvar(name, coerced)
            if not isinstance(coerced, (bool, int, float, str)):
                coerced = list(value)
            return {"ok": True, "variable": name, "value": coerced}

        return await self._run(_sync)

    async def system_run_command(self, command) -> dict:
        # Commands ending in option menus (e.g. -LINETYPE returning to its
        # [?/Create/Load/Set] prompt) need a trailing blank line or _X to exit;
        # otherwise SendCommand returns but AutoCAD stays at a prompt and the
        # next COM call deadlocks. Example: "_-LINETYPE _LOAD CENTER acad.lin\n\n"
        def _sync():
            doc = _acad_doc()
            try:
                cmd_active = int(_get_sysvar("CMDACTIVE"))
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
            # R23: refuse to send while a command/prompt is active (raw SendCommand
            # of a prompting form would deadlock the single STA thread).
            try:
                if int(_get_sysvar("CMDACTIVE")):
                    raise RuntimeError(
                        "AutoCAD has an active command/prompt (CMDACTIVE). "
                        "Press ESC in AutoCAD and retry."
                    )
            except RuntimeError:
                raise
            except Exception as exc:
                log.debug("CMDACTIVE read failed, proceeding: %s", exc)
            # N6: SendCommand is void-returning, so the value can't be read from its
            # return. Stash it in USERS1 and read it back after the form completes
            # (CMDACTIVE-polled by _safe_send_command).
            wrapped = f'(vl-load-com)(setvar "USERS1" (vl-princ-to-string (progn {expression})))'
            self._safe_send_command(doc, wrapped)
            try:
                result = _get_sysvar("USERS1")
            except Exception:
                result = None
            return {
                "ok": True,
                "expression": expression,
                "result": str(result) if result not in (None, "") else "nil",
            }

        return await self._run(_sync)

    # ── corner ops ──────────────────────────────────────────────────────────

    @staticmethod
    def _safe_send_command(doc, cmd: str, deadline_s: float = 8.0) -> list[str]:
        """Send `cmd`, polling CMDACTIVE until the command finishes.

        Snap/grid/echo are temporarily zeroed to avoid OSNAP-driven misclicks
        and chatty echoes; everything is restored in the finally block.
        Returns a list of new entity handles created by the command (empty
        list for in-place operations like TRIM/EXTEND).
        """
        saved: dict[str, Any] = {}
        try:
            for var in ("OSMODE", "SNAPMODE", "CMDECHO"):
                try:
                    saved[var] = doc.GetVariable(var)
                    doc.SetVariable(var, 0)
                except Exception:
                    pass
            # Snapshot model-space handles (best-effort).
            try:
                pre = {e.Handle for e in doc.ModelSpace}
            except Exception:
                pre = set()
            payload = cmd if cmd.endswith("\n") else cmd + "\n"
            doc.SendCommand(payload)
            deadline = time.monotonic() + float(deadline_s)
            while True:
                try:
                    active = int(doc.GetVariable("CMDACTIVE"))
                except Exception:
                    active = 0
                if active == 0:
                    break
                if time.monotonic() > deadline:
                    try:
                        doc.SendCommand("\x1b\x1b\x1b\n")  # ESC×3
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"AutoCAD command did not finish within {deadline_s:.1f}s: {cmd!r}"
                    )
                time.sleep(0.05)
            try:
                post = {e.Handle for e in doc.ModelSpace}
            except Exception:
                post = pre
            return list(post - pre)
        finally:
            for var, val in saved.items():
                try:
                    doc.SetVariable(var, val)
                except Exception:
                    pass

    async def entity_trim(self, target_handle, cutter_handle, keep_x, keep_y) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            cmd = f'_TRIM\n(handent "{cutter_handle}")\n\n{float(keep_x)},{float(keep_y)}\n\n'
            self._safe_send_command(doc, cmd)
            ent = doc.HandleToObject(target_handle)
            return _entity_info(ent)

        return await self._run(_sync)

    async def entity_extend(
        self,
        target_handle,
        boundary_handle,
        end_x=None,
        end_y=None,
    ) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            target = doc.HandleToObject(target_handle)
            boundary = doc.HandleToObject(boundary_handle)
            if end_x is None or end_y is None:
                # Auto: pick the target endpoint nearest the boundary midpoint.
                try:
                    ts = target.StartPoint
                    te = target.EndPoint
                    bs = boundary.StartPoint
                    be_ = boundary.EndPoint
                    bm = ((bs[0] + be_[0]) / 2.0, (bs[1] + be_[1]) / 2.0)
                    ds = (ts[0] - bm[0]) ** 2 + (ts[1] - bm[1]) ** 2
                    de = (te[0] - bm[0]) ** 2 + (te[1] - bm[1]) ** 2
                    pick = ts if ds <= de else te
                    ex, ey = float(pick[0]), float(pick[1])
                except Exception:
                    ex, ey = 0.0, 0.0
            else:
                ex, ey = float(end_x), float(end_y)
            cmd = f'_EXTEND\n(handent "{boundary_handle}")\n\n{ex},{ey}\n\n'
            self._safe_send_command(doc, cmd)
            return _entity_info(doc.HandleToObject(target_handle))

        return await self._run(_sync)

    async def entity_fillet(self, handle1, handle2, radius, trim=True) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            t = "T" if trim else "N"  # T=Trim, N=No-trim
            cmd = (
                f"_FILLET\n_R\n{float(radius)}\n_T\n_{t}\n"
                f'(handent "{handle1}")\n(handent "{handle2}")\n'
            )
            new_handles = self._safe_send_command(doc, cmd)
            # Return the new ARC entity if one was created (radius > 0); else
            # fall back to handle1 (radius=0 = sharp corner, no arc).
            for h in new_handles:
                try:
                    ent = doc.HandleToObject(h)
                    if ent.ObjectName == "AcDbArc":
                        return _entity_info(ent)
                except Exception:
                    continue
            return _entity_info(doc.HandleToObject(handle1))

        return await self._run(_sync)

    async def entity_chamfer(
        self,
        handle1,
        handle2,
        dist1,
        dist2=None,
        trim=True,
    ) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            d1 = float(dist1)
            d2 = float(dist1 if dist2 is None else dist2)
            t = "T" if trim else "N"
            cmd = (
                f"_CHAMFER\n_D\n{d1}\n{d2}\n_T\n_{t}\n"
                f'(handent "{handle1}")\n(handent "{handle2}")\n'
            )
            new_handles = self._safe_send_command(doc, cmd)
            for h in new_handles:
                try:
                    ent = doc.HandleToObject(h)
                    if ent.ObjectName == "AcDbLine":
                        return _entity_info(ent)
                except Exception:
                    continue
            return _entity_info(doc.HandleToObject(handle1))

        return await self._run(_sync)

    # ── premium meta-tools live on AutoCADBackend (base.py) and are shared by
    # both backends. Only the XLINE primitive is backend-specific. ────────────
    async def _create_xline(self, x, y, dx, dy, layer) -> EntityInfo:
        def _sync():
            mspace = _msp()
            # AutoCAD ActiveX AddXline takes two points the line passes through;
            # derive the second from the base point + direction vector.
            xline = mspace.AddXline(
                _apoint(float(x), float(y)),
                _apoint(float(x) + float(dx), float(y) + float(dy)),
            )
            _apply_entity_attrs(xline, layer, None, None)
            _regen()
            return _entity_info(xline)

        return await self._run(_sync)
