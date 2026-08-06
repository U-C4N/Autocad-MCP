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
from pathlib import Path
from typing import Any

import config
from engineering.measure import is_self_intersecting, polygon_area_perimeter

from . import ocs
from .base import (
    AutoCADBackend,
    BlockInfo,
    CapabilityMap,
    DrawingInfo,
    EntityInfo,
    FeatureCapability,
    LayerInfo,
    UnsupportedCapabilityError,
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
    """Return (or lazily create) the CAD Application COM object.

    Must only be called from the COM executor thread.

    The ProgID comes from ``CAD_PROGID`` (default ``AutoCAD.Application``) and
    is honoured on *both* paths deliberately. ``Dispatch`` is not a passive
    probe — it COM-launches the application and makes it visible — so honouring
    the setting on ``GetActiveObject`` and then falling back to AutoCAD would
    start the very application the operator said they were not using.
    """
    if "app" not in _COM_STATE:
        progid = config.settings.cad_progid
        try:
            _COM_STATE["app"] = win32com.client.GetActiveObject(progid)
        except Exception as exc:
            log.debug("GetActiveObject(%r) failed, trying Dispatch: %s", progid, exc)
            try:
                _COM_STATE["app"] = win32com.client.Dispatch(progid)
                _COM_STATE["app"].Visible = True
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot connect to CAD application {progid!r}: {exc}. Make sure it is "
                    f"installed and running. (Set CAD_PROGID to change it; default "
                    f"{config.DEFAULT_CAD_PROGID!r}.)"
                ) from exc
    return _COM_STATE["app"]


def _acad_doc():
    """Return active AutoCAD document."""
    app = _acad_app()
    if app.Documents.Count == 0:
        raise RuntimeError("No drawing is open in AutoCAD. Open or create a .dwg file first.")
    return app.ActiveDocument


def _msp():
    """The space new geometry goes into — the active layout's block.

    This returned ``ModelSpace`` unconditionally, so ``layout_set_current``
    reported success and everything drawn afterwards still landed in model
    space. ``ActiveLayout.Block`` is the same object as ``ModelSpace`` while the
    Model tab is active, so the model-space path is unchanged; on a paper layout
    it is the sheet, which is where a title block belongs.
    """
    doc = _acad_doc()
    try:
        return doc.ActiveLayout.Block
    except Exception as exc:  # never strand a write over a layout probe
        log.debug("ActiveLayout.Block unavailable (%s); using ModelSpace", exc)
        return doc.ModelSpace


_BUILTIN_LINETYPES = {"continuous", "bylayer", "byblock"}


def _ensure_linetype_loaded(name: str) -> None:
    """If `name` is not already loaded, load it via -LINETYPE behind FILEDIA=0.

    Called by attribute setters so users can write `linetype="CENTER"` without
    having to remember to load it first. Must run on the COM thread.
    """
    if not name or name.lower() in _BUILTIN_LINETYPES:
        return
    doc = _acad_doc()
    existing = {doc.Linetypes.Item(i).Name.lower() for i in range(doc.Linetypes.Count)}
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


#: ActiveX object names that bound no area at all. Not a capability gap — no
#: engine can give a LINE an area — so these raise a plain error.
_COM_NO_AREA_TYPES = frozenset(
    {
        "AcDbLine",
        "AcDbText",
        "AcDbMText",
        "AcDbPoint",
        "AcDbBlockReference",
        "AcDbRotatedDimension",
        "AcDbAlignedDimension",
        "AcDbLeader",
        "AcDb3dPolyline",
        "AcDbRay",
        "AcDbXline",
    }
)


def _com_bulge(entity, index: int) -> float:
    """GetBulge is polyline-only and absent on some hosts; 0.0 means straight."""
    try:
        return float(entity.GetBulge(index))
    except Exception:
        return 0.0


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
        elif obj_name in ("AcDbLWPolyline", "AcDb2dPolyline"):
            coords = list(entity.Coordinates)
            pts = [[coords[i], coords[i + 1]] for i in range(0, len(coords), 2)]
            # ActiveX hands back WCS for every point property on this backend
            # *except* this one: "LightweightPolyline object: the variant is an
            # array of 2D points in OCS." Scoped to AcDbLWPolyline deliberately —
            # AcDb2dPolyline's Coordinates may be 3D triples, and changing its
            # stride on a doc reading alone, with no way to verify against live
            # AutoCAD from here, risks breaking a path that works today.
            if obj_name == "AcDbLWPolyline":
                try:
                    normal = tuple(entity.Normal)
                    if not ocs.is_wcs_frame(normal):
                        elevation = float(getattr(entity, "Elevation", 0.0))
                        pts = [ocs.to_wcs_2d(normal, p[0], p[1], elevation) for p in pts]
                except Exception as exc:
                    log.debug("OCS normalisation of polyline coordinates failed: %s", exc)
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
                "dwg": FeatureCapability(True, "native"),
                "pdf": FeatureCapability(True, "native"),
                "png": FeatureCapability(True, "native"),
                "transactions": FeatureCapability(True, "undo_mark"),
                "audit_detail": FeatureCapability(
                    False, reason="audit_result_not_readable_over_com"
                ),
                "undo_history": FeatureCapability(True, "autocad_native"),
                "chspace": FeatureCapability(False, reason="unverified_against_live_autocad"),
                "revcloud": FeatureCapability(False, reason="no_activex_member_command_only"),
                "wipeout": FeatureCapability(
                    False, reason="addwipeout_absent_verified_autocad_2026"
                ),
                "mtext_background_color": FeatureCapability(
                    False, reason="backgroundfillcolor_absent_verified_autocad_2026"
                ),
                "boundary_trace": FeatureCapability(False, reason="no_activex_member_command_only"),
                "hatch_edge_paths": FeatureCapability(
                    False, reason="activex_appends_loops_as_objects_not_typed_edges"
                ),
                "handle_overlay": FeatureCapability(
                    False, reason="window_capture_has_no_render_to_label"
                ),
                "measure_area_acis": FeatureCapability(True, "native"),
                "ocs_normalized": FeatureCapability(
                    True,
                    "activex_wcs",
                    reason=(
                        "activex_returns_wcs_natively;lwpolyline_coordinates_translated_locally;"
                        "2d_polyline_coordinates_not_verified_against_live_autocad"
                    ),
                ),
                "ocs_tilted_plane": FeatureCapability(
                    False, reason="2d_xy_cannot_address_a_tilted_plane"
                ),
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
        progid = config.settings.cad_progid
        log.info("COM backend ready (will connect to %s on first tool call)", progid)
        if progid != config.DEFAULT_CAD_PROGID:
            log.warning(
                "CAD_PROGID=%r: only %s is developed and tested against. The connection may "
                "succeed while individual tools fail on ActiveX differences.",
                progid,
                config.DEFAULT_CAD_PROGID,
            )

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

        Only a *real* deadline overrun may rebuild the apartment: on 3.11+
        ``TimeoutError`` is ``asyncio.TimeoutError`` (and an ``OSError``
        subclass), so a wrapped call that raises one itself reaches the handler
        looking identical to an expired deadline. ``future.cancelled()`` tells
        them apart — ``wait_for`` cancels the future it is waiting on, an
        ordinary failure completes it — mirroring ``EzdxfBackend._async``.
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
            if not future.cancelled():
                # The call raised TimeoutError instead of overrunning the
                # deadline (a dead network share, a re-raised socket timeout —
                # and with timeout<=0 there is no deadline at all). That is an
                # ordinary failure: let it through unchanged rather than
                # blaming AutoCAD and discarding a live STA connection.
                raise
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
            # AutoCAD SaveAs format constants: 12=DWG R2010, 61=DXF R2010
            fmt_map = {"dwg": 12, "dxf": 61, "dwt": 5}
            acad_fmt = fmt_map.get(fmt.lower(), 12)
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

    @staticmethod
    def _layout_name(raw: str) -> str:
        return str(raw or "").strip()

    @staticmethod
    def _layout_names(doc) -> list[str]:
        return [doc.Layouts.Item(i).Name for i in range(doc.Layouts.Count)]

    def _find_layout(self, doc, raw: str) -> str | None:
        """Resolve a layout name case-insensitively, as AutoCAD itself does.

        A plain ``name in names`` test is case-sensitive in Python and would
        both refuse an existing tab typed in another case and let a duplicate
        past into ``Layouts.Add``.
        """
        name = self._layout_name(raw)
        if not name:
            return None
        lowered = name.lower()
        return next((n for n in self._layout_names(doc) if n.lower() == lowered), None)

    async def layout_delete(self, name: str) -> dict:
        def _sync():
            # VERIFIED against a live AutoCAD 2026 (2026-08-05).
            doc = _acad_doc()
            if not self._layout_name(name):
                return {"ok": False, "error": "Layout name must not be blank"}
            target = self._find_layout(doc, name)
            if target is None:
                return {"ok": False, "error": f"Layout not found: {name}"}
            if target == "Model":
                return {"ok": False, "error": "Model space cannot be deleted"}
            names = self._layout_names(doc)
            if len([n for n in names if n != "Model"]) <= 1:
                return {
                    "ok": False,
                    "error": (
                        f"Cannot delete {target}: a drawing must keep at least one "
                        "paper-space layout"
                    ),
                }

            layout = doc.Layouts.Item(target)
            destroyed = layout.Block.Count
            if doc.ActiveLayout.Name == target:
                doc.ActiveLayout = doc.Layouts.Item("Model")
            layout.Delete()
            return {
                "ok": True,
                "deleted": target,
                "entities_destroyed": destroyed,
                "current": doc.ActiveLayout.Name,
            }

        return await self._run(_sync)

    async def layout_rename(self, old_name: str, new_name: str) -> dict:
        def _sync():
            # VERIFIED against a live AutoCAD 2026 (2026-08-05).
            doc = _acad_doc()
            new = self._layout_name(new_name)
            if not self._layout_name(old_name) or not new:
                return {"ok": False, "error": "Layout names must not be blank"}
            old = self._find_layout(doc, old_name)
            if old is None:
                return {"ok": False, "error": f"Layout not found: {old_name}"}
            if old == "Model" or new.lower() == "model":
                return {"ok": False, "error": "Model space cannot be renamed or shadowed"}
            clash = self._find_layout(doc, new)
            if clash is not None and clash != old:
                return {"ok": False, "error": f"Layout already exists: {clash}"}
            if any(ch in new for ch in '<>/\\":;?*|,=`'):
                return {"ok": False, "error": f"Invalid layout name: {new!r}"}

            doc.Layouts.Item(old).Name = new
            return {"ok": True, "old": old, "new": new, "current": doc.ActiveLayout.Name}

        return await self._run(_sync)

    async def layout_copy(self, source: str, new_name: str) -> dict:
        def _sync():
            # VERIFIED against a live AutoCAD 2026 (2026-08-05). AutoCAD's own
            # LAYOUT Copy option is command-line only, so this reproduces it
            # with CopyFrom + CopyObjects.
            doc = _acad_doc()
            dst_name = self._layout_name(new_name)
            if not self._layout_name(source) or not dst_name:
                return {"ok": False, "error": "Layout names must not be blank"}
            src_name = self._find_layout(doc, source)
            if src_name is None:
                return {"ok": False, "error": f"Layout not found: {source}"}
            if src_name == "Model":
                return {
                    "ok": False,
                    "error": "Model space cannot be copied to a sheet; use viewport_create",
                }
            if dst_name.lower() == "model":
                return {"ok": False, "error": "Model space cannot be overwritten"}
            clash = self._find_layout(doc, dst_name)
            if clash is not None:
                return {"ok": False, "error": f"Layout already exists: {clash}"}

            src = doc.Layouts.Item(src_name)
            dst = doc.Layouts.Add(dst_name)
            dst.CopyFrom(src)  # page setup / plot configuration

            items = [src.Block.Item(i) for i in range(src.Block.Count)]
            copied = 0
            skipped: list[str] = []
            if items:
                objects = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, items)
                try:
                    doc.CopyObjects(objects, dst.Block)
                    copied = len(items)
                except Exception as exc:
                    log.warning("layout_copy: CopyObjects failed (%s)", exc)
                    skipped = sorted({obj.ObjectName for obj in items})

            return {
                "ok": True,
                "source": src_name,
                "layout": dst_name,
                "entities_copied": copied,
                "skipped": skipped,
                # AutoCAD remaps associativity inside CopyObjects itself, so
                # there is nothing for this backend to re-point by hand.
                "associativity_remapped": 0,
                "associativity_dropped": 0,
            }

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
                # `Target`, not `ViewCenter`: AcadPViewport has no ViewCenter
                # member at all. Verified against AutoCAD 2026 — the old call
                # raised every single time and the bare `except: pass` around
                # it swallowed that, so view_center_x/y were silently ignored
                # and every COM viewport looked at the model origin instead of
                # where the caller asked. The failure is reported now.
                viewport.Target = _apoint(view_center_x, view_center_y)
                return {
                    "ok": True,
                    "handle": viewport.Handle,
                    "layout": layout,
                    "scale": scale,
                    "view_height": float(height) / float(scale),
                }
            finally:
                if previous != layout:
                    doc.ActiveLayout = doc.Layouts.Item(previous)

        return await self._run(_sync)

    # ── selection filters (M8 / F1) ──────────────────────────────────────────
    #
    # VERIFIED against a live AutoCAD 2026 (2026-08-05).

    _SELECTION_MODES = ("window", "crossing")

    def _selection_mode(self, mode: str) -> tuple[str | None, dict | None]:
        normalised = str(mode or "").strip().lower()
        if normalised not in self._SELECTION_MODES:
            return None, {
                "ok": False,
                "error": (
                    f"Unknown mode {mode!r}; valid modes are {', '.join(self._SELECTION_MODES)}"
                ),
            }
        return normalised, None

    @staticmethod
    def _com_bbox(entity):
        lo, hi = entity.GetBoundingBox()
        return (float(lo[0]), float(lo[1])), (float(hi[0]), float(hi[1]))

    def _com_select(self, predicate, entity_type: str, layer: str) -> dict:
        doc = _acad_doc()
        wanted_type = str(entity_type or "").strip().upper()
        wanted_layer = str(layer or "").strip()
        handles = []
        for i in range(doc.ModelSpace.Count):
            entity = doc.ModelSpace.Item(i)
            try:
                lo, hi = self._com_bbox(entity)
            except Exception:
                continue
            if not predicate(lo, hi):
                continue
            if wanted_type and self._dxftype_of(entity) != wanted_type:
                continue
            if wanted_layer and entity.Layer != wanted_layer:
                continue
            handles.append(entity.Handle)
        return handles

    @staticmethod
    def _dxftype_of(entity) -> str:
        name = entity.ObjectName
        return name[4:].upper() if name.startswith("AcDb") else name.upper()

    async def selection_window(
        self, x1, y1, x2, y2, mode: str = "window", entity_type: str = "", layer: str = ""
    ) -> dict:
        def _sync():
            resolved, refusal = self._selection_mode(mode)
            if refusal:
                return refusal
            lo_x, hi_x = sorted((float(x1), float(x2)))
            lo_y, hi_y = sorted((float(y1), float(y2)))
            if lo_x == hi_x or lo_y == hi_y:
                return {
                    "ok": False,
                    "error": (
                        "The selection box has zero area; give two opposite corners that "
                        "differ in both x and y"
                    ),
                }

            def _inside(lo, hi):
                return lo_x <= lo[0] and lo_y <= lo[1] and hi[0] <= hi_x and hi[1] <= hi_y

            def _overlaps(lo, hi):
                return not (hi[0] < lo_x or lo[0] > hi_x or hi[1] < lo_y or lo[1] > hi_y)

            handles = self._com_select(
                _inside if resolved == "window" else _overlaps, entity_type, layer
            )
            return {"ok": True, "handles": handles, "count": len(handles), "mode": resolved}

        return await self._run(_sync)

    async def selection_polygon(
        self, points, mode: str = "window", entity_type: str = "", layer: str = ""
    ) -> dict:
        def _sync():
            resolved, refusal = self._selection_mode(mode)
            if refusal:
                return refusal
            vertices = self._plane_points(points)
            if vertices is None:
                return {"ok": False, "error": "points must be a list of [x, y] pairs"}
            if len(vertices) < 3:
                return {
                    "ok": False,
                    "error": f"A selection polygon needs at least 3 points; got {len(vertices)}",
                }

            def _in_polygon(px, py) -> bool:
                inside = False
                count = len(vertices)
                for i in range(count):
                    ax, ay = vertices[i]
                    bx, by = vertices[(i + 1) % count]
                    if (ay > py) != (by > py) and px < (bx - ax) * (py - ay) / (by - ay) + ax:
                        inside = not inside
                return inside

            def _inside(lo, hi):
                corners = ((lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1]))
                return all(_in_polygon(*corner) for corner in corners)

            def _overlaps(lo, hi):
                corners = ((lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1]))
                return any(_in_polygon(*corner) for corner in corners)

            handles = self._com_select(
                _inside if resolved == "window" else _overlaps, entity_type, layer
            )
            return {"ok": True, "handles": handles, "count": len(handles), "mode": resolved}

        return await self._run(_sync)

    async def selection_filter(
        self,
        entity_type: str = "",
        layer: str = "",
        color: int | None = None,
        linetype: str = "",
        min_area: float | None = None,
    ) -> dict:
        def _sync():
            if min_area is not None and float(min_area) < 0:
                return {"ok": False, "error": "min_area cannot be negative; no area is negative"}
            doc = _acad_doc()
            wanted_type = str(entity_type or "").strip().upper()
            wanted_layer = str(layer or "").strip()
            wanted_linetype = str(linetype or "").strip()
            filtered_by = [
                name
                for name, active in (
                    ("entity_type", bool(wanted_type)),
                    ("layer", bool(wanted_layer)),
                    ("color", color is not None),
                    ("linetype", bool(wanted_linetype)),
                    ("min_area", min_area is not None),
                )
                if active
            ]

            handles = []
            for i in range(doc.ModelSpace.Count):
                entity = doc.ModelSpace.Item(i)
                if wanted_type and self._dxftype_of(entity) != wanted_type:
                    continue
                if wanted_layer and entity.Layer != wanted_layer:
                    continue
                if color is not None and int(entity.Color) != int(color):
                    continue
                if wanted_linetype and entity.Linetype != wanted_linetype:
                    continue
                if min_area is not None:
                    try:
                        area = float(entity.Area)
                    except Exception:
                        continue  # no area at all is not "zero area"
                    if area < float(min_area):
                        continue
                handles.append(entity.Handle)
            return {
                "ok": True,
                "handles": handles,
                "count": len(handles),
                "filtered_by": filtered_by,
            }

        return await self._run(_sync)

    # ── hatch depth (M8 / F4) ────────────────────────────────────────────────

    _HATCH_STYLES = ("normal", "outer", "ignore")

    def _resolve_com_hatch(self, doc, handle):
        try:
            entity = doc.HandleToObject(str(handle).strip().upper())
        except Exception:
            raise RuntimeError(f"Entity with handle '{handle}' not found.") from None
        if entity.ObjectName != "AcDbHatch":
            return None, {
                "ok": False,
                "error": f"Handle {handle} is a {self._dxftype_of(entity)}, not a HATCH",
            }
        return entity, None

    async def hatch_set_gradient(
        self,
        handle,
        color1,
        color2,
        rotation: float = 0.0,
        centered: float = 0.0,
        one_color: bool = False,
        tint: float = 0.0,
        name: str = "LINEAR",
    ) -> dict:
        def _sync():
            # VERIFIED against a live AutoCAD 2026 (2026-08-05).
            doc = _acad_doc()
            hatch, refusal = self._resolve_com_hatch(doc, handle)
            if refusal:
                return refusal
            try:
                rgb1 = tuple(int(c) for c in color1)
                rgb2 = tuple(int(c) for c in color2)
            except (TypeError, ValueError):
                return {"ok": False, "error": "color1 and color2 must be [r, g, b] triples"}
            if len(rgb1) != 3 or len(rgb2) != 3:
                return {"ok": False, "error": "color1 and color2 must be [r, g, b] triples"}

            hatch.HatchObjectType = 1  # gradient
            hatch.GradientName = str(name)
            hatch.GradientAngle = math.radians(float(rotation))
            hatch.GradientCentered = bool(centered)
            first, second = doc.Application.GetInterfaceObject("AutoCAD.AcCmColor.25"), None
            first.SetRGB(*rgb1)
            hatch.GradientColor1 = first
            second = doc.Application.GetInterfaceObject("AutoCAD.AcCmColor.25")
            second.SetRGB(*rgb2)
            hatch.GradientColor2 = second
            return {
                "ok": True,
                "handle": hatch.Handle,
                "gradient": {
                    "color1": list(rgb1),
                    "color2": list(rgb2),
                    "rotation": float(rotation),
                    "centered": float(centered),
                    "one_color": bool(one_color),
                    "tint": float(tint),
                    "name": str(name),
                },
            }

        return await self._run(_sync)

    async def hatch_edit(
        self,
        handle,
        pattern: str = "",
        scale: float | None = None,
        angle: float | None = None,
        color: int | None = None,
        style: str = "",
    ) -> dict:
        def _sync():
            doc = _acad_doc()
            hatch, refusal = self._resolve_com_hatch(doc, handle)
            if refusal:
                return refusal
            if scale is not None and float(scale) <= 0:
                return {"ok": False, "error": "scale must be > 0"}
            wanted_style = str(style).strip().lower()
            if wanted_style and wanted_style not in self._HATCH_STYLES:
                return {
                    "ok": False,
                    "error": (
                        f"Unknown style {style!r}; valid styles are {', '.join(self._HATCH_STYLES)}"
                    ),
                }

            def _snapshot():
                return {
                    "pattern": hatch.PatternName,
                    "scale": float(hatch.PatternScale),
                    "angle": float(hatch.PatternAngle),
                    "color": int(hatch.Color),
                    "style": int(hatch.HatchStyle),
                }

            before = _snapshot()
            if pattern:
                hatch.SetPattern(1, str(pattern))  # acHatchPatternTypePredefined
            if scale is not None:
                hatch.PatternScale = float(scale)
            if angle is not None:
                hatch.PatternAngle = math.radians(float(angle))
            if color is not None:
                hatch.Color = int(color)
            if wanted_style:
                hatch.HatchStyle = self._HATCH_STYLES.index(wanted_style)
            hatch.Evaluate()

            after = _snapshot()
            return {
                "ok": True,
                "handle": hatch.Handle,
                "changed": [key for key in before if before[key] != after[key]],
            }

        return await self._run(_sync)

    async def hatch_add_boundary(self, handle, edges) -> dict:
        """Refused on the live backend: ActiveX takes boundary loops as whole
        objects (AppendOuterLoop / AppendInnerLoop), not as typed edges, so the
        curved-edge fidelity this tool exists for cannot be expressed."""
        raise UnsupportedCapabilityError(
            "hatch_edge_paths",
            "hatch_add_boundary: ActiveX appends boundary loops as existing objects rather "
            "than typed edges, so an arc edge cannot be given directly. Draw the boundary "
            "entities and use entity_create_hatch, or switch to the headless backend "
            "(AUTOCAD_MCP_BACKEND=ezdxf).",
        )

    async def analysis_list_properties(self, handle: str) -> dict:
        def _sync():
            # VERIFIED against a live AutoCAD 2026 (2026-08-05). ActiveX has no
            # generic attribute dump, so this reads the documented members per
            # object type.
            doc = _acad_doc()
            try:
                entity = doc.HandleToObject(str(handle).strip().upper())
            except Exception:
                raise RuntimeError(f"Entity with handle '{handle}' not found.") from None

            dump: dict = {}
            for member in (
                "Layer",
                "Color",
                "Linetype",
                "LinetypeScale",
                "Lineweight",
                "Thickness",
                "Visible",
                "Normal",
                "Radius",
                "Center",
                "StartPoint",
                "EndPoint",
                "InsertionPoint",
                "TextString",
                "Height",
                "Rotation",
                "ObliqueAngle",
                "StyleName",
                "Name",
                "XScaleFactor",
                "YScaleFactor",
                "Area",
                "Length",
            ):
                try:
                    raw = getattr(entity, member)
                except Exception:
                    continue
                key = member[0].lower() + member[1:]
                if isinstance(raw, tuple):
                    dump[key] = [float(v) for v in raw]
                elif isinstance(raw, (int, float, str, bool)):
                    dump[key] = raw

            info = _entity_info(entity)
            return {
                "ok": True,
                "handle": info.handle,
                "type": info.type,
                "layer": info.layer,
                "properties": info.properties,
                "dxf_attributes": dump,
            }

        return await self._run(_sync)

    # ── annotation objects (M8 / F15) ────────────────────────────────────────
    #
    # VERIFIED against a live AutoCAD 2026 (2026-08-05). Two members turned out
    # not to exist and are now typed refusals rather than code: AddWipeout and
    # AcDbMText.BackgroundFillColor.

    #: Same scope as the headless backend, and DIMENSION is out for the same
    #: reason: its TextOverride is the '<>' placeholder, not the measurement.
    _TEXT_BEARING_OBJECTS = ("AcDbText", "AcDbMText", "AcDbAttribute", "AcDbAttributeDefinition")

    @staticmethod
    def _plane_points(points) -> list[tuple[float, float]] | None:
        try:
            return [(float(p[0]), float(p[1])) for p in points]
        except (TypeError, ValueError, IndexError):
            return None

    # ``entity_create_wipeout`` is deliberately not implemented here. Verified
    # against AutoCAD 2026: ``ModelSpace.AddWipeout`` does not exist —
    # ``AttributeError: <unknown>.AddWipeout``. ActiveX has no wipeout
    # constructor; WIPEOUT is a command. The contract declares this
    # @capability("wipeout") and the live backend inherits the typed refusal.

    # ``entity_create_revcloud`` is deliberately not implemented here: ActiveX
    # exposes no revision-cloud member, and driving the REVCLOUD command blind
    # against a live drawing cannot be verified from this machine. The contract
    # declares it @capability("revcloud"), so this backend inherits the typed
    # refusal.

    async def text_set_background(
        self, handle, enabled: bool = True, color: int | None = None, scale: float = 1.5
    ) -> dict:
        def _sync():
            # VERIFIED against a live AutoCAD 2026 (2026-08-05): BackgroundFill
            # is settable, BackgroundFillColor does not exist at all.
            doc = _acad_doc()
            try:
                entity = doc.HandleToObject(str(handle).strip().upper())
            except Exception:
                return {"ok": False, "error": f"Entity handle not found: {handle}"}
            if entity.ObjectName != "AcDbMText":
                return {
                    "ok": False,
                    "error": (
                        f"Handle {handle} is {entity.ObjectName}; only MTEXT carries a "
                        "background fill."
                    ),
                }
            if not enabled:
                entity.BackgroundFill = False
                return {"ok": True, "handle": entity.Handle, "enabled": False}
            factor = float(scale)
            if factor < 1.0:
                return {
                    "ok": False,
                    "error": (
                        f"scale must be >= 1.0 (got {factor:g}); a box smaller than its "
                        "text is a stripe through the text, not a mask"
                    ),
                }
            if color is not None:
                # Verified against AutoCAD 2026: AcDbMText has no
                # BackgroundFillColor member at all — it raises AttributeError
                # on read *and* on write, with an int and with an AcCmColor
                # object alike. Enabling the fill without the requested colour
                # and reporting success would be exactly the "did something
                # else and said nothing" this release exists to remove.
                raise UnsupportedCapabilityError(
                    "mtext_background_color",
                    "text_set_background: ActiveX exposes no BackgroundFillColor member on "
                    "MTEXT, so the requested colour cannot be applied. Omit `color` to switch "
                    "the mask on in the drawing's background colour, or use the headless "
                    "backend (AUTOCAD_MCP_BACKEND=ezdxf) to set it.",
                )
            entity.BackgroundFill = True
            return {
                "ok": True,
                "handle": entity.Handle,
                "enabled": True,
                "color": None,
                "scale": factor,
                "note": "ActiveX cannot set the mask colour; the drawing background is used",
            }

        return await self._run(_sync)

    async def text_find_replace(
        self,
        find: str,
        replace: str,
        layer: str | None = None,
        match_case: bool = True,
        dry_run: bool = False,
    ) -> dict:
        def _sync():
            # VERIFIED against a live AutoCAD 2026 (2026-08-05).
            import re

            needle = str(find)
            if not needle:
                return {
                    "ok": False,
                    "error": (
                        "find must not be empty: an empty pattern matches between every "
                        "character and would shred the text"
                    ),
                }
            doc = _acad_doc()
            pattern = re.compile(re.escape(needle), 0 if match_case else re.IGNORECASE)

            changed: list[dict] = []
            for i in range(doc.ModelSpace.Count):
                entity = doc.ModelSpace.Item(i)
                if entity.ObjectName not in self._TEXT_BEARING_OBJECTS:
                    continue
                if layer and entity.Layer != layer:
                    continue
                current = entity.TextString
                if not current or not pattern.search(current):
                    continue
                updated = pattern.sub(replace, current)
                if not dry_run:
                    entity.TextString = updated
                changed.append(
                    {
                        "handle": entity.Handle,
                        "type": entity.ObjectName,
                        "before": current,
                        "after": updated,
                    }
                )

            return {
                "ok": True,
                "replaced": len(changed),
                "entities": changed,
                "searched_types": list(self._TEXT_BEARING_OBJECTS),
                "note": (
                    "DIMENSION text is not searched: its TextOverride holds the '<>' "
                    "placeholder rather than the measured value."
                ),
                "dry_run": bool(dry_run),
            }

        return await self._run(_sync)

    # ── viewports ────────────────────────────────────────────────────────────
    #
    # VERIFIED against a live AutoCAD 2026 (2026-08-05). This block is why the
    # verification was worth running: the code read `ViewCenter`, which is not
    # a member of AcadPViewport, and viewport_list died on it on the first
    # call. viewport_create had the same line wrapped in `except: pass`, so it
    # had been silently ignoring view_center_x/y since v1.4.

    @staticmethod
    def _com_viewports(layout):
        block = layout.Block
        return [
            block.Item(i) for i in range(block.Count) if block.Item(i).ObjectName == "AcDbViewport"
        ]

    def _resolve_com_viewport(self, doc, handle: str):
        """``(viewport, layout_name, None)`` or ``(None, None, refusal)``."""
        key = str(handle or "").strip().upper()
        if not key:
            return None, None, {"ok": False, "error": "Viewport handle must not be blank"}
        try:
            entity = doc.HandleToObject(key)
        except Exception:
            return None, None, {"ok": False, "error": f"Viewport handle not found: {handle}"}
        if entity.ObjectName != "AcDbViewport":
            return (
                None,
                None,
                {
                    "ok": False,
                    "error": f"Handle {key} is a {entity.ObjectName}, not a viewport",
                },
            )
        layout_name = None
        try:
            for i in range(doc.Layouts.Count):
                layout = doc.Layouts.Item(i)
                if any(vp.Handle == key for vp in self._com_viewports(layout)):
                    layout_name = layout.Name
                    break
        except Exception as exc:  # pragma: no cover - live COM only
            log.debug("could not locate the layout owning viewport %s: %s", key, exc)
        return entity, layout_name, None

    @staticmethod
    def _com_viewport_row(viewport, layout_name: str) -> dict:
        center = viewport.Center
        # The model point the viewport looks at is `Target`; there is no
        # `ViewCenter` member on AcadPViewport (verified against AutoCAD 2026).
        view_center = viewport.Target
        height = float(viewport.Height)
        try:
            scale = float(viewport.CustomScale)
        except Exception:
            scale = None
        return {
            "handle": viewport.Handle,
            "layout": layout_name,
            "center": [float(center[0]), float(center[1])],
            "width": float(viewport.Width),
            "height": height,
            "view_center": [float(view_center[0]), float(view_center[1])],
            "view_height": (height / scale if scale else None),
            "scale": scale,
            "locked": bool(viewport.DisplayLocked),
            "status": int(bool(viewport.ViewportOn)),
            "id": None,  # AutoCAD does not expose the DXF viewport id via ActiveX
            # Unknown, not False: ActiveX has no main-viewport predicate, and
            # claiming "definitely not the main viewport" is a stronger
            # statement than this backend can make.
            "is_main": None,
        }

    async def viewport_list(self, layout: str | None = None) -> dict:
        def _sync():
            doc = _acad_doc()
            names = [doc.Layouts.Item(i).Name for i in range(doc.Layouts.Count)]
            if layout is None:
                targets = [n for n in names if n != "Model"]
            else:
                name = self._layout_name(layout)
                if not name:
                    return {"ok": False, "error": "Layout name must not be blank"}
                if name == "Model":
                    return {"ok": False, "error": "Model space has no paper-space viewports"}
                if name not in names:
                    return {"ok": False, "error": f"Layout not found: {name}"}
                targets = [name]

            rows = [
                self._com_viewport_row(vp, name)
                for name in targets
                for vp in self._com_viewports(doc.Layouts.Item(name))
            ]
            return {
                "ok": True,
                "viewports": rows,
                "count": len(rows),
                "note": "ActiveX exposes no main-viewport predicate, so is_main is null",
            }

        return await self._run(_sync)

    async def viewport_set_scale(self, handle: str, scale: float) -> dict:
        def _sync():
            if float(scale) <= 0:
                return {"ok": False, "error": "scale must be > 0 (paper:model, e.g. 0.5 for 1:2)"}
            doc = _acad_doc()
            viewport, _, refusal = self._resolve_com_viewport(doc, handle)
            if refusal:
                return refusal
            viewport.CustomScale = float(scale)
            return {
                "ok": True,
                "handle": viewport.Handle,
                "scale": float(viewport.CustomScale),
                "view_height": float(viewport.Height) / float(viewport.CustomScale),
                "note": "geometric scale only; annotative text and dimensions do not resize",
            }

        return await self._run(_sync)

    async def viewport_lock(self, handle: str, locked: bool = True) -> dict:
        def _sync():
            doc = _acad_doc()
            viewport, _, refusal = self._resolve_com_viewport(doc, handle)
            if refusal:
                return refusal
            viewport.DisplayLocked = bool(locked)
            return {
                "ok": True,
                "handle": viewport.Handle,
                "locked": bool(viewport.DisplayLocked),
            }

        return await self._run(_sync)

    async def viewport_delete(self, handle: str, force: bool = False) -> dict:
        def _sync():
            doc = _acad_doc()
            viewport, layout_name, refusal = self._resolve_com_viewport(doc, handle)
            if refusal:
                return refusal
            key = viewport.Handle
            viewport.Delete()
            return {
                "ok": True,
                "handle": key,
                "layout": layout_name,
                # Unknown rather than False: ActiveX has no main-viewport
                # predicate, and AutoCAD owns the tab's view state itself, so
                # there is no pointer for this backend to repair either.
                "was_main": None,
                "viewport_handle_repaired": False,
            }

        return await self._run(_sync)

    # ``entity_change_space`` is deliberately *not* implemented here. The
    # contract declares it ``@capability("chspace")``, so this backend inherits
    # a typed refusal carrying that key rather than an unverified
    # CopyObjects-and-delete that would destroy and recreate geometry in the
    # operator's open drawing when it goes wrong.

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
        """Run AutoCAD's AUDIT with fixing enabled, and admit what we cannot see.

        The ``Y`` answers AUDIT's "Fix any errors detected?" prompt, so this
        backend does repair — it just gets nothing machine-readable back from
        ``SendCommand``. The counts are therefore reported as ``None`` rather
        than ``0``: zero would mean "nothing was wrong", which we do not know.
        ``AUDITCTL`` is restored afterwards; it is the user's setting, and
        leaving it at 1 silently makes AutoCAD write an ``.adt`` log beside
        every drawing they audit from then on.
        """

        def _sync():
            doc = _acad_doc()
            app = _acad_app()
            try:
                previous = app.GetVariable("AUDITCTL")
            except Exception:  # not every AutoCAD-compatible host exposes it
                previous = None
            app.SetVariable("AUDITCTL", 1)
            try:
                doc.SendCommand("_AUDIT Y\n")
            finally:
                if previous is not None:
                    try:
                        app.SetVariable("AUDITCTL", previous)
                    except Exception:
                        log.debug("could not restore AUDITCTL", exc_info=True)

            # Only report a log we can actually see. SendCommand queues, so at
            # this point AUDIT has very likely not run yet and the .adt does not
            # exist — handing back a path to a missing file would be a small
            # version of the same lie as claiming the repair count.
            log_path = None
            try:
                full = doc.FullName  # empty on a drawing that was never saved
                if full:
                    candidate = Path(full).with_suffix(".adt")
                    if candidate.exists():
                        log_path = str(candidate)
            except Exception:
                log.debug("could not derive the .adt log path", exc_info=True)

            return {
                "ok": True,
                "repaired": None,
                "fixes": [],
                "fix_count": None,
                "errors": [],
                "error_count": None,
                "detail": "unavailable",
                "capability": "audit_detail",
                "message": (
                    "AUDIT was dispatched to AutoCAD with fixing enabled. SendCommand "
                    "queues the command and returns immediately, so this call cannot "
                    "confirm that it ran, let alone what it changed: the repair and "
                    "error counts are unknown, not zero. AutoCAD writes an .adt log "
                    "beside the drawing when AUDITCTL is on — that is the only place "
                    "the detail exists."
                ),
                "log_path": log_path,
            }

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
            sp = mspace.AddSpline(
                _av(flat),
                _apoint(0, 1),  # start tangent
                _apoint(0, 1),  # end tangent
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

                if _sq_dist(pos[0]) <= _sq_dist(neg[0]):
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
            name = ent.ObjectName.replace("AcDb", "").upper()
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
            name = ent.ObjectName.replace("AcDb", "").upper()
            if name == "CIRCLE":
                c = ent.Center
                ent.Center = _apoint(
                    float(cx) if cx is not None else c[0],
                    float(cy) if cy is not None else c[1],
                )
                if radius is not None:
                    ent.Radius = float(radius)
            elif name == "LINE":
                s, e = ent.StartPoint, ent.EndPoint
                ent.StartPoint = _apoint(
                    float(x1) if x1 is not None else s[0],
                    float(y1) if y1 is not None else s[1],
                )
                ent.EndPoint = _apoint(
                    float(x2) if x2 is not None else e[0],
                    float(y2) if y2 is not None else e[1],
                )
            elif name == "ARC":
                c = ent.Center
                ent.Center = _apoint(
                    float(cx) if cx is not None else c[0],
                    float(cy) if cy is not None else c[1],
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

    async def entity_count(self, type_filter=None, layer_filter=None) -> int:
        # Same filter arms as entity_list, and the same per-entity skip-on-error
        # policy, so the count and the page always describe one set. What it
        # skips is `_entity_info(ent)` — the property/extents extraction, which
        # is several COM round trips per entity against the two cheap attribute
        # reads kept here.
        def _sync():
            mspace = _msp()
            count = 0
            for i in range(mspace.Count):
                try:
                    ent = mspace.Item(i)
                    if (
                        type_filter
                        and type_filter.upper() != ent.ObjectName.replace("AcDb", "").upper()
                    ):
                        continue
                    if layer_filter and layer_filter.lower() != ent.Layer.lower():
                        continue
                    count += 1
                except Exception as exc:
                    log.debug("entity_count: skip entity at index %d: %s", i, exc)
                    continue
            return count

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
        # Loads a single linetype from a .lin file. Two things would otherwise
        # bite the user: (1) AutoCAD pops a "Select Linetype File" dialog when
        # FILEDIA=1 and the file path can't be auto-resolved — that modal
        # dialog deadlocks SendCommand; (2) ISO/metric drawings need acadiso.lin,
        # imperial drawings need acad.lin. We pick automatically from MEASUREMENT
        # if the caller didn't specify a file.
        def _sync():
            app = _acad_app()
            doc = _acad_doc()

            existing = {doc.Linetypes.Item(i).Name.lower() for i in range(doc.Linetypes.Count)}
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
        self,
        name,
        handles,
        base_x=0.0,
        base_y=0.0,
    ) -> dict:
        """Build a block definition from entities already in the drawing.

        This used to refuse and point at ``system_run_command`` with ``_BLOCK``,
        pushing callers through the free-text escape hatch for something ActiveX
        does directly: ``Blocks.Add`` creates the definition and
        ``Document.CopyObjects`` puts entities into it. The originals stay in
        model space, matching the headless backend rather than AutoCAD's BLOCK
        command (which consumes them).
        """

        def _sync():
            doc = _acad_doc()
            objects, skipped = [], []
            for handle in handles:
                try:
                    objects.append(doc.HandleToObject(str(handle)))
                except Exception as exc:
                    log.debug("resolving %s for block %s: %s", handle, name, exc)
                    skipped.append(str(handle))
            # Resolve before creating, so a call where every handle was a typo
            # does not leave an empty definition behind.
            if not objects:
                raise RuntimeError(
                    f"block_create_from_entities: none of the handles resolved "
                    f"({', '.join(skipped) or 'no handles given'}), so there is nothing "
                    f"to put in block {name!r}. No definition was created."
                )

            block = doc.Blocks.Add(_apoint(float(base_x), float(base_y), 0.0), str(name))
            doc.CopyObjects(
                win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, objects),
                block,
            )
            _regen()
            return {
                "ok": True,
                "name": name,
                "entity_count": len(objects),
                "skipped": skipped,
                "backend": "com",
            }

        return await self._run(_sync)

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

    async def entity_measure(self, handle, flatten_tolerance: float = 0.001) -> dict:
        """Ask AutoCAD, which already knows.

        This backend has had a document, a ``HandleToObject`` lookup and an
        ActiveX ``Area`` property all along, and still answered area questions
        with a shoelace over coordinates the caller typed in. ``Area`` is exact
        here for every boundary type including REGION and 3DSOLID, which is
        precisely the gap the headless engine refuses on.
        """

        def _sync():
            ent = _acad_doc().HandleToObject(str(handle))
            obj_name = getattr(ent, "ObjectName", "")
            if obj_name in _COM_NO_AREA_TYPES:
                raise RuntimeError(
                    f"entity_measure: handle {handle} is {obj_name}, which does not "
                    "bound an area. Measurable types: LWPOLYLINE, POLYLINE (2D), "
                    "CIRCLE, ELLIPSE, SPLINE, HATCH, REGION, 3DSOLID."
                )
            area = perimeter = None
            try:
                area = float(ent.Area)
            except Exception as exc:
                log.debug("ActiveX .Area unavailable for %s: %s", obj_name, exc)
            try:
                perimeter = float(ent.Length)
            except Exception as exc:
                log.debug("ActiveX .Length unavailable for %s: %s", obj_name, exc)

            method = "activex_area"
            closed = True
            self_intersecting = None
            if area is None or perimeter is None:
                # Rebuild from the polyline's own vertices and run the SAME
                # shared maths the headless engine uses, so a missing ActiveX
                # property can never make this backend the less accurate one.
                method = "activex_fallback_analytic"
                coords = list(getattr(ent, "Coordinates", ()) or ())
                vertices = [
                    (float(coords[i]), float(coords[i + 1]), _com_bulge(ent, i // 2))
                    for i in range(0, len(coords) - 1, 2)
                ]
                if not vertices:
                    raise RuntimeError(
                        f"entity_measure: AutoCAD reported neither an area nor usable "
                        f"vertices for handle {handle} ({obj_name})."
                    )
                closed = bool(getattr(ent, "Closed", True))
                fallback_area, fallback_perimeter = polygon_area_perimeter(vertices, closed)
                area = fallback_area if area is None else area
                perimeter = fallback_perimeter if perimeter is None else perimeter
                self_intersecting = is_self_intersecting([(v[0], v[1]) for v in vertices])

            return {
                "handle": str(handle),
                "type": obj_name,
                "area": round(float(area), 6),
                "perimeter": round(float(perimeter), 6),
                "closed": closed,
                "assumed_closed": not closed,
                "method": method,
                "exact": True,
                "flatten_tolerance": None,
                "backend": "com",
                "self_intersecting": self_intersecting,
                "perimeter_exact": True,
                "loop_count": 1,
            }

        return await self._run(_sync)

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
                    if et_upper == ent.ObjectName.replace("AcDb", "").upper():
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

    async def view_screenshot_grounded(
        self,
        overlay_handles: bool = True,
        max_labels: int = 40,
    ) -> dict:
        """Not available live: this backend captures the AutoCAD window itself.

        Grounding needs labels drawn into the render, and there is no render
        here to draw into — the pixels come from the application's own window.
        Annotating them would mean writing entities into the user's live drawing
        and deleting them again, which is a mutation nobody asked for.
        """
        raise UnsupportedCapabilityError(
            "handle_overlay",
            "view_screenshot_grounded: the live backend captures the AutoCAD "
            "window rather than rendering the drawing, so there is no render to "
            "label. Annotating the live drawing to produce the overlay would "
            "mean creating and deleting entities in the user's document. Use "
            "entity_list for handles alongside the plain screenshot, or switch "
            "to the headless backend (AUTOCAD_MCP_BACKEND=ezdxf) for a grounded "
            "render.",
        )

    async def view_screenshot(self, overlay_handles: bool = False) -> bytes | None:
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
                    # Which application this actually is. Without it a
                    # GstarCAD/ZWCAD operator reads an "autocad_version" key
                    # naming a product they do not own.
                    "cad_progid": config.settings.cad_progid,
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
                return {
                    "backend": "com",
                    "connected": False,
                    "cad_progid": config.settings.cad_progid,
                    "error": str(exc),
                }

        return await self._run(_sync)

    async def system_get_variable(self, name) -> Any:
        def _sync():
            app = _acad_app()
            return app.GetVariable(name)

        return await self._run(_sync)

    async def system_set_variable(self, name, value) -> dict:
        def _sync():
            app = _acad_app()
            # Coerce to the sysvar's actual type (NEW-com-set-variable-coercion):
            # AutoCAD rejects e.g. a string "0" for the integer OSMODE. Probe the
            # current value's type and convert to match; pass through on failure.
            coerced = value
            try:
                current = app.GetVariable(name)
                if isinstance(current, bool):
                    coerced = bool(int(value)) if isinstance(value, str) else bool(value)
                elif isinstance(current, int):
                    coerced = int(float(value)) if isinstance(value, str) else int(value)
                elif isinstance(current, float):
                    coerced = float(value)
            except Exception as exc:
                log.debug("set_variable type probe for %s failed: %s", name, exc)
            app.SetVariable(name, coerced)
            return {"ok": True, "variable": name, "value": coerced}

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
            app = _acad_app()
            doc = _acad_doc()
            # R23: refuse to send while a command/prompt is active (raw SendCommand
            # of a prompting form would deadlock the single STA thread).
            try:
                if int(app.GetVariable("CMDACTIVE")):
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
                result = app.GetVariable("USERS1")
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
