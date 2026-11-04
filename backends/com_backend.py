"""AutoCAD COM backend – live AutoCAD control via pywin32.

All COM calls are routed through a single-threaded executor to satisfy
AutoCAD's STA (Single-Threaded Apartment) COM requirement.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextlib
import contextvars
import functools
import io
import logging
import math
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import config
from engineering.errors import (
    ApplicationBusyError,
    CommandIncompleteError,
    EntityNotFoundError,
    classify,
)
from engineering.measure import is_self_intersecting, polygon_area_perimeter
from security import sanitize_macro_argument, sanitize_symbol_name, validate_path

from . import lineage, ocs
from .atomic_write import atomic_write_dxf, file_sha256, resolve_pending
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
    lineweight_name,
    parse_lineweight,
    rad2deg,
    shoelace_area,
)
from .contracts.entity_modification import TEXT_SEARCH_SCOPE
from .contracts.settings import SUMMARY_FIELDS, validate_drawing_properties
from .document_guard import ActiveDocumentChangedError
from .dxf_recover import recover_dxf, recovered_source_refusal
from .operations import CURRENT_OPERATION
from .quarantine import DaemonExecutor, real_call_name
from .region_render import (
    DEFAULT_MAX_PX,
    check_max_px,
    downscale_png,
    normalize_region,
    png_size,
    render_dxf_file,
)
from .session_metrics import RunBreakdown
from .vertices import (
    edit_geometry_route,
    prepare_bulges,
    prepare_points,
    prepare_position,
    prepare_vertices,
    resolved_bulges,
)
from .xdata_specs import prepare_xdata

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
        import win32process
        import win32ui

        _COM_IMPORTS_OK = True
    except ImportError:
        _COM_IMPORTS_OK = False
else:
    _COM_IMPORTS_OK = False

#: What `_run` may catch as a COM failure. A *tuple*, because where pywin32 is
#: absent the right value is the empty one: `except ()` catches nothing, which
#: is exactly correct when no `com_error` type exists to be raised.
#:
#: `except pywintypes.com_error` used to be written directly, and Python
#: evaluates that name on the way out of every failed call — not only COM ones.
#: On any host without pywin32 (all of Linux, and Windows installed without the
#: `[com]` extra) it raised `NameError: name 'pywintypes' is not defined` and
#: buried the real exception. A placeholder Exception subclass would be worse
#: than the bug: it would silently swallow unrelated errors.
_COM_ERROR: tuple[type[BaseException], ...] = (pywintypes.com_error,) if _COM_IMPORTS_OK else ()

#: ``RPC_E_CALL_REJECTED``: the callee's message filter refused the incoming
#: call *before* running it, so nothing happened on the seat. AutoCAD answers
#: it for a moment after ``Documents.Open`` / ``Add`` / ``Close`` while it is
#: still switching documents — measured twice on 2026-09-22 (AutoCAD 2026,
#: ``scripts/smoke_settings_com.py --build-dwt``: ``Documents.Count`` right
#: after ``Documents.Open`` and right after ``Close(False)``) — and for as long
#: as an operator or another client leaves a command prompt open. pywin32 has
#: no ``CoRegisterMessageFilter``, the client-side hook that would retry this
#: per call, so the read-only reads that follow a switch wait it out themselves.
_RPC_E_CALL_REJECTED = -2147418111


def _wait_out_rejected_call(read: Callable[[], Any], *, budget_s: float, first_pause_s: float):
    """Run a read-only COM callable again while it answers ``RPC_E_CALL_REJECTED``.

    Only for callables that change nothing on the seat (the rejection means
    the refused call did not run, but an earlier call inside ``read`` did).
    Pauses double from ``first_pause_s`` up to one second and stop at
    ``budget_s``; any other ``com_error`` is re-raised on the first attempt.
    """
    deadline = time.monotonic() + budget_s
    pause = first_pause_s
    while True:
        try:
            return read()
        except _COM_ERROR as exc:
            hr = exc.args[0] if exc.args else 0
            if hr != _RPC_E_CALL_REJECTED or time.monotonic() + pause > deadline:
                raise
            time.sleep(pause)
            pause = min(pause * 2, 1.0)


try:
    from PIL import Image as PILImage

    _PIL_OK = True
except ImportError:
    _PIL_OK = False


# ---------------------------------------------------------------------------
# COM thread-state (accessed ONLY from the COM executor thread)
# ---------------------------------------------------------------------------

_COM_STATE: dict[str, Any] = {}  # keys: "app", "app_identity"

#: Per-thread state of the executor job running now (B11): ``guard`` is the
#: ``_JobGuard`` that ``_acad_doc()`` consults. Thread-local, not in
#: ``_COM_STATE``: a stuck job on an abandoned executor thread must never
#: clear the guard of the job running on its replacement.
_JOB_STATE = threading.local()


class _JobGuard:
    """One job's check of the bound document, run at its first ``_acad_doc()``.

    The checked Document is reused by the rest of the job and dropped on the
    COM thread when the job ends (a COM proxy must not be released elsewhere).
    """

    __slots__ = ("backend", "doc", "touched", "space")

    def __init__(self, backend):
        self.backend = backend
        self.doc = None
        #: Set by ``_acad_app()``: the job has held the Application proxy, so
        #: it may have written something (1.8-02, ``application_busy``).
        self.touched = False
        #: ``(layout name, block ObjectID)`` once ``_msp()`` handed out a paper
        #: layout's block in this job (1.8-05): ``_entity_info`` names that
        #: space on an entity it owns.
        self.space = None

    def __call__(self, app):
        if self.doc is None:
            began = time.perf_counter()
            counted = _job_clock_get("counter")
            try:
                self.doc = self.backend._guard(app)
            finally:
                # the operator-edit counter runs inside the guard; it is booked
                # as ``counter`` (``_timed_counter_start``), never twice
                spent = (time.perf_counter() - began) * 1000.0
                _job_clock_add("guard", spent - (_job_clock_get("counter") - counted))
        return self.doc


def _job_clock_add(bucket: str, ms: float) -> None:
    """Add ``ms`` to the running job's ``bucket`` (1.8-14); a no-op outside a job."""
    clock = getattr(_JOB_STATE, "clock", None)
    if clock is not None:
        clock[bucket] = clock.get(bucket, 0.0) + ms


def _job_clock_get(bucket: str) -> float:
    clock = getattr(_JOB_STATE, "clock", None)
    return clock.get(bucket, 0.0) if clock is not None else 0.0


def _com_init():
    """COM thread initializer – called once by the STA worker thread."""
    if _COM_IMPORTS_OK:
        pythoncom.CoInitialize()


def _com_teardown():
    """COM thread finalizer — drop the cached app and release the STA apartment.

    Submitted to the executor (R5) so the CoInitialize done in _com_init is
    paired with a CoUninitialize when the worker is torn down or a stuck
    SendCommand eventually unblocks, instead of leaking the apartment forever.
    """
    _drop_app()
    if _COM_IMPORTS_OK:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


# AcCoordinateSystem, as Utility.TranslateCoordinates takes it (From / To).
_AC_WORLD = 0
_AC_UCS = 1


def _apoint(x: float, y: float, z: float = 0.0):
    """Create a VARIANT double-array point for AutoCAD COM."""
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8,
        [float(x), float(y), float(z)],
    )


def _image_pixels(path: str) -> tuple[int, int]:
    """Pixel size of a raster; the placed size is pixels x scale on both engines.

    Copied from the ezdxf backend rather than imported: the two backends must
    not depend on each other.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow ships with [pdf]
        raise ValueError(
            "image_attach needs Pillow to read the image's pixel size; "
            'install it with: pip install -e ".[pdf]"'
        ) from exc
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception as exc:
        raise ValueError(
            f"image_attach: {path} is not an image this server can read ({exc})"
        ) from exc


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


def _avar(values):
    """Create a VARIANT array of VARIANTs (what SetXRecordData takes for its values)."""
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_VARIANT,
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
    guard = getattr(_JOB_STATE, "guard", None)
    if guard is not None:
        guard.touched = True
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


def _drop_app() -> None:
    """Forget the cached Application proxy (and the identity read from it)."""
    _COM_STATE.pop("app", None)
    _COM_STATE.pop("app_identity", None)
    _COM_STATE.pop("vl", None)
    _COM_STATE.pop("vl_docs", None)


def _app_identity(app) -> tuple:
    """Which running AutoCAD ``app`` is: ``(HWND, process id)`` of its main window.

    A document is one document of one application (B11): an unsaved
    ``Drawing1.dwg`` has the key ``('Drawing1.dwg', '')`` in every AutoCAD, so
    after a restart the new one must not inherit the old one's id. Measured on
    AutoCAD 2026 (25.1) on 2026-10-26: ``Application.HWND`` is an ``int`` and
    two proxies from two ``GetActiveObject`` calls report the same one
    (scripts/smoke_document_com.py). Read once per proxy and cached beside it,
    so the guard costs no extra round trip. A proxy whose HWND cannot be read
    is identified by itself - conservative: a re-attach is then refused as a
    changed application once, and ``document_activate`` binds again.
    """
    cached = _COM_STATE.get("app_identity")
    if cached is not None and cached[0] is app:
        return cached[1]
    try:
        hwnd = int(app.HWND)
    except Exception as exc:  # noqa: BLE001 - identified by the proxy instead
        log.debug("Application.HWND unreadable (%s); identifying the proxy itself", exc)
        identity: tuple = ("proxy", id(app))
    else:
        pid = None
        if _COM_IMPORTS_OK:
            try:
                pid = int(win32process.GetWindowThreadProcessId(hwnd)[1]) or None
            except Exception as exc:  # noqa: BLE001 - the HWND alone still tells two apart
                log.debug("GetWindowThreadProcessId(%s) failed: %s", hwnd, exc)
        identity = ("hwnd", hwnd, pid)
    _COM_STATE["app_identity"] = (app, identity)
    return identity


# ---------------------------------------------------------------------------
# The operator-edit counter (v1.7.0, B11)
# ---------------------------------------------------------------------------
#
# A transient AutoLISP database reactor in the bound document's own namespace
# counts every object appended, modified, erased or unerased in that
# document's database; the server evaluates two fixed forms through
# ``VL.Application.16`` - no COM event sink, no SendCommand. Measured on
# AutoCAD 2026 (25.1) on 2026-10-26 on scratch Documents.Add() documents
# (scripts/smoke_operator_edits_com.py): see ``ComBackend._edit_counter_start``.

#: Installs the reactor (once: ``vlr-added-p`` guards a second one), adds the
#: document id to the namespace's ``acadmcp:edit-ids`` and answers
#: ``"<id>:<count>"``. Not ``vlr-pers``: the reactor lives in the session and
#: is never saved. ``all-documents`` keeps it counting while another drawing is
#: active. The ids are a list, not one tag: two servers bound to one document
#: (two clients on one seat) share the namespace and its count, and each must
#: find its own id there - a single tag would make each one's install look,
#: to the other, like a removed reactor, and their reads would move each
#: other's revision for ever.
_EDIT_COUNTER_INSTALL = (
    "(progn (vl-load-com)"
    " (if (null acadmcp:edit-n) (setq acadmcp:edit-n 0))"
    " (defun acadmcp:edit-bump (r a) (setq acadmcp:edit-n (1+ acadmcp:edit-n)))"
    " (if (not (and acadmcp:edit-r (vlr-added-p acadmcp:edit-r)))"
    " (progn (setq acadmcp:edit-r (vlr-acdb-reactor nil '("
    "(:vlr-objectAppended . acadmcp:edit-bump) (:vlr-objectModified . acadmcp:edit-bump)"
    " (:vlr-objectErased . acadmcp:edit-bump) (:vlr-objectUnErased . acadmcp:edit-bump))))"
    " (vlr-set-notification acadmcp:edit-r 'all-documents)))"
    ' (if (not (member "{document_id}" acadmcp:edit-ids))'
    ' (setq acadmcp:edit-ids (cons "{document_id}" acadmcp:edit-ids)))'
    ' (strcat "{document_id}" ":" (itoa acadmcp:edit-n)))'
)

#: ``"<id>:<count>"``, or nil when no reactor is installed (a namespace the
#: server never installed into), it was removed (``vlr-remove-all``) or this
#: id was never added to the namespace (it is another document's, or a
#: reopened one's).
_EDIT_COUNTER_READ = (
    '(if (and acadmcp:edit-r (vlr-added-p acadmcp:edit-r) (member "{document_id}" acadmcp:edit-ids))'
    ' (strcat "{document_id}" ":" (itoa acadmcp:edit-n)))'
)

_VL_PROGID = "VL.Application.16"


def _dispatch(obj):
    """The raw ``PyIDispatch`` behind ``obj``, or None for a plain Python object."""
    ole = getattr(obj, "_oleobj_", None)
    if ole is not None:
        return ole
    if hasattr(obj, "GetIDsOfNames") and hasattr(obj, "Invoke"):
        return obj
    return None


def _vl_get(obj, name: str):
    """Property ``name`` of a VL object.

    The ``VL.Application.16`` proxy has type info (``DVlApplication``: Name,
    Documents, ActiveDocument) but win32com's dynamic dispatch raised
    ``AttributeError`` for ``ActiveDocument`` on AutoCAD 2026 (measured), so
    the member is invoked by its DISPID.
    """
    ole = _dispatch(obj)
    if ole is None:
        return getattr(obj, name)
    return ole.Invoke(ole.GetIDsOfNames(name), 0, pythoncom.DISPATCH_PROPERTYGET, 1)


def _vl_call(obj, name: str, *args):
    ole = _dispatch(obj)
    if ole is None:
        return getattr(obj, name)(*args)
    flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
    return ole.Invoke(ole.GetIDsOfNames(name), 0, flags, 1, *args)


def _vl_application(app):
    """``app``'s VL application, cached beside the proxy; raises when it has none."""
    cached = _COM_STATE.get("vl")
    if cached is not None and cached[0] is app:
        if isinstance(cached[1], BaseException):
            raise cached[1]
        return cached[1]
    try:
        vl = app.GetInterfaceObject(_VL_PROGID)
    except Exception as exc:  # noqa: BLE001 - no Visual LISP: the counter is off
        _COM_STATE["vl"] = (app, exc)
        _COM_STATE.pop("vl_docs", None)
        raise
    _COM_STATE["vl"] = (app, vl)
    _COM_STATE["vl_docs"] = {}
    return vl


def _vl_document_forms(app, document_id: str, *, fresh: bool):
    """``(eval, parsed read form)`` for the namespace of the document active NOW.

    Cached per document id: fetching ``ActiveDocument.Functions`` and the two
    function objects cost ~6.6 ms live, one ``eval`` of a parsed form ~1 ms.
    ``fresh`` re-fetches - the cached objects may belong to a closed namespace.
    """
    vl = _vl_application(app)
    docs = _COM_STATE.setdefault("vl_docs", {})
    if not fresh and document_id in docs:
        return docs[document_id]
    functions = _vl_get(_vl_get(vl, "ActiveDocument"), "Functions")
    reader = _vl_call(functions, "Item", "read")
    evaluator = _vl_call(functions, "Item", "eval")
    source = _EDIT_COUNTER_READ.format(document_id=document_id)
    forms = (reader, evaluator, _vl_call(reader, "funcall", source))
    docs[document_id] = forms
    return forms


def _parse_edit_count(value, document_id: str) -> int | None:
    """The count from ``"<id>:<count>"`` when it is this document's, else None."""
    if not isinstance(value, str):
        return None
    tag, _, count = value.partition(":")
    if tag != document_id:
        return None
    try:
        return int(count)
    except ValueError:
        return None


#: ``AcSaveAsType`` template constants, newest first: ac2018_Template,
#: ac2013_Template, ac2010_Template. A seat older than the first refuses it
#: with a COM error and the next one is tried.
_TEMPLATE_SAVE_FORMATS = (66, 62, 50)


def _template_cache_dir() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / "acadmcp-templates"


def _template_as_dwt(app, source: Path) -> tuple[Path, bool]:
    """A real ``.dwt`` for ``source``, built once through AutoCAD and cached.

    ``Documents.Add(<file>)`` accepts only a genuine DWT: measured on AutoCAD
    2026, ``Add(templates/iso_a3_mech.dxf)`` returned the default acadiso
    drawing (layers ``['0']``, tabs ``Layout1``/``Layout2``) exactly as
    ``Add()`` and ``Add(<nonexistent.dwt>)`` did, and the DXF bytes renamed
    ``.dwt`` fared no better. Opening the file and saving it as a template
    (``SaveAs(path, ac2018_Template)``) is what yields the eleven layers and
    the ``A3`` tab, so that is done here — once per file content, keyed on the
    path, size and mtime, under the temp folder — and the ``.dwt`` is what
    ``Add`` is handed. Returns ``(dwt_path, cached)``.

    Must only be called from the COM executor thread. The conversion document
    is opened read-only and closed without saving in a ``finally``; the
    ``.dwt`` on disk is the only thing it leaves behind.
    """
    import hashlib

    source = Path(source).resolve()
    stat = source.stat()
    digest = hashlib.sha1(
        f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", "surrogateescape")
    ).hexdigest()[:12]
    cache_dir = _template_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    dwt = cache_dir / f"{source.stem}-{digest}.dwt"
    if dwt.is_file() and dwt.stat().st_size > 0:
        return dwt, True
    doc = app.Documents.Open(str(source), True)
    try:
        last_exc: Exception | None = None
        for fmt in _TEMPLATE_SAVE_FORMATS:
            try:
                doc.SaveAs(str(dwt), fmt)
                break
            except _COM_ERROR as exc:
                last_exc = exc
                log.debug("SaveAs(%s, %d) refused: %s", dwt, fmt, exc)
        else:
            raise RuntimeError(
                f"could not save {source.name} as a .dwt template: {last_exc}"
            ) from last_exc
    finally:
        try:
            doc.Close(False)
        except Exception as exc:  # the temp document must never stay open
            log.warning("template conversion document did not close: %s", exc)
    if not dwt.is_file() or dwt.stat().st_size == 0:
        raise RuntimeError(f"AutoCAD reported the template saved but {dwt} is not on disk")
    return dwt, False


def _acad_doc():
    """The active AutoCAD document - inside a ComBackend call, the bound one.

    ``ComBackend._run`` installs a per-job ``_JobGuard`` (B11): the first call
    here in a job checks that AutoCAD's active document is still the one this
    session is bound to, and refuses with ``ActiveDocumentChangedError`` before
    any work when the operator switched; later calls in the same job reuse the
    checked document. Outside a job (a test calling a ``_sync`` directly) it is
    the plain active document, as before.

    1.8-02: when AutoCAD answers ``RPC_E_CALL_REJECTED`` (a command prompt or a
    modal dialog is open) to the job's *first* touch of the application - the
    ``Documents.Count`` / guard reads here, before the job held the
    Application proxy for anything else - nothing can have been written, and
    the refusal is ``ApplicationBusyError`` (kind ``application_busy``,
    retryable). A rejection later in a job stays the COM error it was: the job
    may have written before it.
    """
    guard = getattr(_JOB_STATE, "guard", None)
    pristine = guard is not None and guard.doc is None and not guard.touched
    try:
        app = _acad_app()
        if app.Documents.Count == 0:
            raise RuntimeError("No drawing is open in AutoCAD. Open or create a .dwg file first.")
        if guard is not None:
            return guard(app)
        return app.ActiveDocument
    except _COM_ERROR as exc:
        hr = exc.args[0] if exc.args else 0
        if pristine and hr == _RPC_E_CALL_REJECTED:
            raise ApplicationBusyError(
                "AutoCAD refused the call (RPC_E_CALL_REJECTED): it is waiting at a command "
                "prompt or showing a dialog. Nothing was written. Press ESC in AutoCAD (or "
                "close the dialog) and send the same call again."
            ) from exc
        raise


#: HRESULTs ``HandleToObject`` may answer that are about the connection, not
#: the handle: the call was refused or the server went away, so the handle is
#: not known to be missing. The three ``ComBackend._run`` drops the
#: Application proxy on, plus RPC_E_CALL_REJECTED, RPC_E_DISCONNECTED
#: (0x80010108) and RPC_S_SERVER_UNAVAILABLE (0x800706BA).
_NOT_A_HANDLE_ANSWER = frozenset(
    {
        _RPC_E_CALL_REJECTED,
        -2147221246,
        -2147221005,
        -2147417842,
        -2147417848,
        -2147023174,
    }
)


def _handle_to_object(doc, handle):
    """``doc.HandleToObject(handle)``, or ``EntityNotFoundError`` (1.8-02).

    The live twin of the headless ``_get_entity``: a handle AutoCAD does not
    know is kind ``not_found``, not a generic "AutoCAD COM error". Which
    HRESULT AutoCAD 2026 answers for an unknown handle is not measured yet, so
    every COM error is read as "not found" except the connection-level ones in
    ``_NOT_A_HANDLE_ANSWER``, which are re-raised untouched.
    """
    try:
        return doc.HandleToObject(handle)
    except _COM_ERROR as exc:
        hr = exc.args[0] if exc.args else 0
        if hr in _NOT_A_HANDLE_ANSWER:
            raise
        raise EntityNotFoundError(handle) from exc


def _job_named(job, method: str):
    """``job`` reported under ``method`` (1.8-23's create job builders).

    ``real_call_name`` names a call by the method its ``_sync`` closure was
    defined in; a builder's closure lives in ``_create_<type>_job``, so the
    call_outcomes rows and timeouts would name the builder instead of the
    tool. The qualname is set to what it was before the split.
    """
    job.__qualname__ = f"ComBackend.{method}.<locals>._sync"
    return job


def _reraise_if_connection_lost(exc: BaseException) -> None:
    """Re-raise a COM error that is about the connection (``_NOT_A_HANDLE_ANSWER``).

    A bulk job's per-item ``try`` (1.8-23) types one item's failure into its
    row; a refused call or a server that went away is not one item's problem -
    every later item would fail the same way - so it ends the job and reaches
    ``_run`` as the COM error it is.
    """
    if isinstance(exc, _COM_ERROR):
        hr = exc.args[0] if exc.args else 0
        if hr in _NOT_A_HANDLE_ANSWER:
            raise exc


def _batch_error(index: int, exc: BaseException, *, type, before_job: bool = False) -> dict:
    """``entity_batch_create``'s error row: the contract's keys plus the 1.8-02 ``kind``.

    A spec refused before the job (a wrong argument, a lineweight or XDATA
    the validators refuse) wrote nothing and is ``invalid_args`` unless
    ``classify`` names something more specific.
    """
    kind = classify(exc)["kind"]
    if before_job and kind in ("failed", "internal") and isinstance(exc, (TypeError, ValueError)):
        kind = "invalid_args"
    return {"index": index, "type": type, "error": str(exc), "kind": kind}


#: The share of ``COM_CALL_TIMEOUT`` one slice of a bulk job may spend on its
#: items (1.8-23). ``_run`` holds every job to that one deadline and treats an
#: overrun as a hung AutoCAD - the executor is abandoned while the job keeps
#: writing - so a long but healthy list must never be one job. A slice stops
#: after the item that crosses this budget (at least one item per slice), which
#: leaves three quarters of the deadline for the queue, the guard, the one
#: refresh and the operator counter.
_BULK_SLICE_SHARE = 0.25


def _bulk_slice_budget() -> float | None:
    """Seconds of item work one bulk slice may take; None without a deadline."""
    timeout = config.settings.com_call_timeout
    return timeout * _BULK_SLICE_SHARE if timeout > 0 else None


def _target_space_name() -> str:
    """The bound document's target space: ``"Model"`` or a paper layout's name.

    Read from the backend the running job belongs to (1.8-05); outside a job
    (a test calling a ``_sync`` directly) it is ``"Model"``.
    """
    guard = getattr(_JOB_STATE, "guard", None)
    bound = getattr(guard.backend, "_bound", None) if guard is not None else None
    return str((bound or {}).get("space") or "Model")


def _msp():
    """The space new geometry goes into - the server's target space (1.8-05).

    Model space unless ``layout_set_current`` chose a paper layout for the
    bound document - never the tab the operator happens to have active. Up to
    1.7.0 this returned ``ActiveLayout.Block``, so a drawing saved with Layout1
    active, or a tab click between two calls, put the next ``entity_create_*``
    on the sheet (§4.6 of the rival's report: the circle in paper space). The
    headless engine has always started in Model (``EzdxfBackend._msp``).

    A target layout that no longer exists (deleted in AutoCAD by hand) falls
    back to model space and resets the target, with a warning in the log - the
    same rule as the headless engine.
    """
    doc = _acad_doc()
    name = _target_space_name()
    if name.lower() == "model":
        return doc.ModelSpace
    try:
        block = doc.Layouts.Item(name).Block
    except Exception as exc:  # the operator deleted the sheet under the session
        log.warning("target layout %r is gone (%s); falling back to model space", name, exc)
        guard = getattr(_JOB_STATE, "guard", None)
        bound = getattr(guard.backend, "_bound", None) if guard is not None else None
        if bound is not None:
            bound["space"] = "Model"
        return doc.ModelSpace
    guard = getattr(_JOB_STATE, "guard", None)
    if guard is not None:
        try:
            guard.space = (name, block.ObjectID)
        except Exception as exc:  # the space tag is evidence, never a failure
            log.debug("target block ObjectID unreadable: %s", exc)
    return block


def _delete_on_layer_all_layouts(doc, layer: str) -> dict:
    """Delete every entity on ``layer`` in every layout's block of ``doc``.

    Measured on AutoCAD 2026 on 2026-10-24 in a scratch ``Documents.Add()``
    document closed with ``Close(False)``: ``Layouts.Item(i).Block.Item(j)``
    answers ``ObjectName`` / ``Handle`` / ``Layer`` for a LINE in model space
    and on an unactivated Layout1 alike, and ``HandleToObject(h).Delete()``
    removed both (Block.Count Model 2 -> 1, Layout1 2 -> 1, the other-layer
    line kept). The handles are collected first and deleted afterwards, so the
    indices being walked never shift. A delete that fails (a locked layer) is
    reported with its reason, never skipped. ``doc`` is taken once by the
    caller: the active document can change between two calls.
    """
    wanted = str(layer).casefold()
    layouts = doc.Layouts
    victims: list[str] = []
    for i in range(int(layouts.Count)):
        block = layouts.Item(i).Block
        for j in range(int(block.Count)):
            entity = block.Item(j)
            if str(entity.Layer).casefold() == wanted:
                victims.append(str(entity.Handle))
    deleted: list[str] = []
    failed: list[dict] = []
    for handle in victims:
        try:
            doc.HandleToObject(handle).Delete()
        except Exception as exc:  # noqa: BLE001 - reported per entity below
            failed.append({"handle": handle, "error": str(exc)})
        else:
            deleted.append(handle)
    return {"deleted": deleted, "failed": failed}


#: The ActiveX object names ``text_find_replace`` reads and writes
#: (``TextString``): TEXT, MTEXT, ATTRIB, ATTDEF.
_COM_TEXT_OBJECTS = ("AcDbText", "AcDbMText", "AcDbAttribute", "AcDbAttributeDefinition")
#: A block reference and a MINSERT (``block_explode`` measured both names).
_COM_REFERENCE_OBJECTS = ("AcDbBlockReference", "AcDbMInsertBlock")


def _com_text_candidates(doc):
    """Every object ``text_find_replace`` reads on the live engine, in the
    headless engine's scope (``TEXT_SEARCH_SCOPE``): model space and every paper
    layout (``Layouts.Item(i).Block``, in ``TabOrder``), every block definition
    that is neither a layout block nor an xref (``Blocks.Item(i).IsLayout`` /
    ``IsXRef``), and the ATTRIBs of every block reference found in any of them
    (``GetAttributes()``, which answers an empty array for a reference without
    attributes). It searched ``ModelSpace`` alone, so an attribute on a sheet,
    a title-block field or a block definition's ATTDEF default was never found
    on the live engine while the headless one replaced it.

    An anonymous block (``Name`` starting with ``*``) is not searched: a
    dimension's ``*D`` block, a table's ``*T`` and a dynamic or hatch ``*U`` /
    ``*X`` hold a rendering AutoCAD regenerates from the owning object, so a
    match there rewrote a dimension's displayed measurement while the
    dimension still measured the old value (measured on AutoCAD 2026: an
    ``AddDimAligned`` of 50 mm owns ``*D1`` and its MText ``50`` was matched).
    The headless engine skips the same blocks by the same rule.

    Every item is yielded through ``_com_concrete_entity``: with a makepy cache
    ``Block.Item(i)`` is the gen_py ``IAcadEntity`` wrapper, which has no
    ``TextString`` (measured on AutoCAD 2026: ``hasattr(item, "TextString")``
    is False on a TEXT, and the walk raised ``AttributeError``).

    Members measured on AutoCAD 2026 by Task 12's probe (a scratch
    ``Documents.Add()`` document closed with ``Close(False)``).
    """
    layouts = doc.Layouts
    spaces = []
    for index in range(int(layouts.Count)):
        layout = layouts.Item(index)
        spaces.append((int(layout.TabOrder), layout.Block))
    containers = [block for _order, block in sorted(spaces, key=lambda row: row[0])]
    blocks = doc.Blocks
    for index in range(int(blocks.Count)):
        block = blocks.Item(index)
        if bool(block.IsLayout) or bool(block.IsXRef) or str(block.Name).startswith("*"):
            continue
        containers.append(block)
    for container in containers:
        for index in range(int(container.Count)):
            entity = _com_concrete_entity(container.Item(index))
            yield entity
            if str(entity.ObjectName) in _COM_REFERENCE_OBJECTS:
                for attribute in entity.GetAttributes():
                    yield _com_concrete_entity(attribute)


def _com_text_find_replace(doc, pattern, replace: str, layer: str | None, dry_run: bool) -> list:
    """``[{handle, type, before, after}]`` for every match in the live scope;
    written through ``TextString`` unless ``dry_run``. Each handle once."""
    changed: list[dict] = []
    seen: set[str] = set()
    for entity in _com_text_candidates(doc):
        object_name = str(entity.ObjectName)
        if object_name not in _COM_TEXT_OBJECTS:
            continue
        handle = str(entity.Handle)
        if handle in seen:
            continue
        seen.add(handle)
        if layer and str(entity.Layer) != layer:
            continue
        current = str(entity.TextString or "")
        if not current or not pattern.search(current):
            continue
        updated = pattern.sub(replace, current)
        if not dry_run:
            entity.TextString = updated
        changed.append({"handle": handle, "type": object_name, "before": current, "after": updated})
    return changed


def _int_member(obj, name: str) -> int | None:
    """``int(obj.<name>)`` or ``None`` when the member is absent or not a number."""
    try:
        return int(getattr(obj, name))
    except Exception:
        return None


def _owner_layout_block(doc, ent, handle):
    """The block table record that owns ``ent``, refused unless it is a layout.

    ``Explode()`` places the members in the owner space, so anything this
    method adds alongside them must go there too — never ``ActiveLayout``,
    which is whatever tab the user happens to have open. An owner that is a
    block definition means ``ent`` is a nested reference; exploding it in place
    would redefine the block under every other reference, so it is refused
    before any call is dispatched. An owner that cannot be read at all is
    refused too rather than guessed.
    """
    try:
        owner = doc.ObjectIdToObject(ent.OwnerID)
        is_layout = bool(owner.IsLayout)
    except Exception as exc:
        raise RuntimeError(
            f"Entity {handle} has no resolvable owner layout ({exc}); refusing to "
            "explode into a guessed space"
        ) from exc
    if not is_layout:
        try:
            name = str(owner.Name)
        except Exception:
            name = "?"
        raise RuntimeError(
            f"Entity {handle} is nested inside block definition {name!r}; explode the "
            "outer reference instead (exploding here would redefine the block under "
            "every other reference)"
        )
    return owner


def _require_block_defined(doc, name):
    """``doc.Blocks.Item(name)``, or a ``ValueError`` before ``InsertBlock`` runs.

    ``InsertBlock`` with an unknown name raises a bare COM error after the call
    has already been dispatched; refusing here names the block, costs one
    ``Blocks.Item`` probe and matches the headless engine's message. A layout
    block (``IsLayout``) cannot be inserted into itself — same refusal by name.
    Shared by ``block_insert`` and ``entity_create_block_ref``.
    """
    if not isinstance(name, str) or not name.strip():
        raise TypeError("block name must be a non-empty string")
    try:
        block = doc.Blocks.Item(name)
    except Exception as exc:
        raise ValueError(f"block {name!r} is not defined") from exc
    try:
        is_layout = bool(block.IsLayout)
    except Exception:
        is_layout = False
    if is_layout:
        raise ValueError(f"block {name!r} is a layout block and cannot be inserted")
    return block


def _block_names(doc) -> set[str]:
    """Every block name the drawing holds, lower-cased.

    ``doc.Blocks.Count`` / ``doc.Blocks.Item(index)`` / ``block.Name`` are the
    measured members ``xref_manage`` already walks. The set is lower-cased
    because AutoCAD symbol-table names are case-insensitive, so ``BASE`` and
    ``Base`` are one block and re-attaching under either name hits the same
    definition.
    """
    names: set[str] = set()
    for index in range(doc.Blocks.Count):
        try:
            names.add(str(doc.Blocks.Item(index).Name).lower())
        except Exception:  # noqa: BLE001 - a block that cannot name itself cannot collide
            continue
    return names


_BUILTIN_LINETYPES = {"continuous", "bylayer", "byblock"}

#: `drawing_properties_*` field → `IAcadSummaryInfo` property.
_SUMMARY_ATTRS = {field: field.capitalize() for field in SUMMARY_FIELDS}

#: `IAcadSummaryInfo` failure codes, as the `scode` in a `com_error`'s
#: excepinfo (measured on AutoCAD 2026; the description next to them is
#: English while the HRESULT text is localised, so the code is matched first).
_SUMMARYINFO_DUPLICATE_KEY = -2145386475  # AddCustomInfo: 'Duplicate key'
_SUMMARYINFO_KEY_NOT_FOUND = -2145386476  # Set/RemoveCustomByKey: 'Key not found'


def _summaryinfo_error_is(exc: BaseException, scode: int, description: str) -> bool:
    """True when `exc` is the `IAcadSummaryInfo` failure `scode` / `description`.

    `com_error.args[2]` is the excepinfo tuple ``(wCode, source, description,
    helpfile, helpcontext, scode)``; a missing or foreign excepinfo is never a
    match, so an unrelated COM failure keeps propagating.
    """
    info = exc.args[2] if len(exc.args) > 2 else None
    if not isinstance(info, tuple):
        return False
    code = info[5] if len(info) > 5 else None
    desc = str(info[2] or "") if len(info) > 2 else ""
    return code == scode or desc.strip().casefold() == description.casefold()


def _com_error_description(exc: BaseException) -> str:
    """The English excepinfo description of a `com_error`, else ``str(exc)``.

    The HRESULT text is localised (``'Özel durum oluştu.'`` on a Turkish
    seat) while the excepinfo description is AutoCAD's own English message
    (``'Undefined linetype'``, ``'File system error'``), so the latter is
    what a caller can act on.
    """
    info = exc.args[2] if len(exc.args) > 2 else None
    if isinstance(info, tuple) and len(info) > 2 and info[2]:
        return str(info[2]).strip()
    return str(exc)


def _default_lin_file(doc) -> str:
    """``acadiso.lin`` on a metric seat, ``acad.lin`` on an imperial one.

    MEASUREMENT is a document system variable: ``GetVariable`` is a member
    of AcadDocument and the Application has none (measured on AutoCAD 2026,
    ``hasattr(app, "GetVariable") is False``). Read through the Application
    inside a try/except this always failed and every seat was "metric"; a
    read the document itself refuses is an error now, not a silent default.
    """
    return "acadiso.lin" if int(doc.GetVariable("MEASUREMENT")) == 1 else "acad.lin"


def _load_linetype(doc, name: str, lin_file: str) -> None:
    """``Linetypes.Load(name, file)`` — the ActiveX member for the job.

    The previous route, ``SendCommand("_-LINETYPE _LOAD <name> <file>\\n\\n")``
    behind FILEDIA=0, was measured on AutoCAD 2026 (2026-09-22, scratch
    document): with FILEDIA=0 the macro loads nothing and leaves ``-LINETYPE``
    at ``Enter an option [?/Create/Load/Set]:`` — the whole seat then rejects
    every COM call (RPC_E_CALL_REJECTED) until someone presses ESC — and with
    FILEDIA=1 it opens the modal "Select Linetype File" dialog. ``Load``
    returns at once, touches no system variable, and raises a `com_error`
    whose excepinfo names the cause: ``'Undefined linetype'`` for a name the
    file does not carry, ``'File system error'`` for a file that is not on
    the support path, ``'Duplicate record name'`` for one already loaded
    (the callers check the table first, so that one is not reached).
    """
    try:
        doc.Linetypes.Load(name, lin_file)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load linetype '{name}' from '{lin_file}': "
            f"{_com_error_description(exc)}. Check the linetype name spelling "
            "and that the .lin file is on AutoCAD's support path."
        ) from exc


def _ensure_linetype_loaded(name: str) -> None:
    """If `name` is not already loaded, load it with ``Linetypes.Load``.

    Called by attribute setters so users can write `linetype="CENTER"` without
    having to remember to load it first. Must run on the COM thread.
    """
    if not name or name.lower() in _BUILTIN_LINETYPES:
        return
    # Before any COM call: a linetype is a symbol-table name, and the DXF
    # rule refuses what AutoCAD could never load (and what a SendCommand
    # macro would have executed as a second command).
    name = sanitize_symbol_name(name, kind="linetype")
    doc = _acad_doc()
    existing = {doc.Linetypes.Item(i).Name.lower() for i in range(doc.Linetypes.Count)}
    if name.lower() in existing:
        return
    _load_linetype(doc, name, _default_lin_file(doc))


def _apply_entity_attrs(entity, layer: str | None, color: int | None, linetype: str | None):
    """Apply common entity attributes after creation."""
    if layer is not None:
        entity.Layer = layer
    if color is not None:
        entity.color = int(color)
    if linetype is not None:
        _ensure_linetype_loaded(linetype)
        entity.Linetype = linetype


def _attach_xdata(entity, prepared) -> None:
    """XDATA validated by ``prepare_xdata``, written onto an entity this same COM
    job just created (v1.7.0, B17) - no extra round trip per entity. The APPID
    is registered first, exactly as ``entity_set_xdata`` does."""
    if not prepared:
        return
    doc = _acad_doc()
    for app, tags in prepared:
        try:
            doc.RegisteredApplications.Add(app)
        except Exception as exc:  # already registered
            log.debug("RegisteredApplications.Add(%s): %s", app, exc)
        codes = [1001] + [code for code, _ in tags]
        payload = [app] + [_apoint(*value) if code == 1010 else value for code, value in tags]
        entity.SetXData(
            _ai(codes),
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, payload),
        )


# ── in-place edits (v1.8.0, plan item 1.8-06) ───────────────────────────────


def _com_dxf_type(entity) -> str:
    """The DXF name of a live entity: ``AcDbPolyline`` is an LWPOLYLINE."""
    name = str(entity.ObjectName)
    if name in _COM_LWPOLYLINE_NAMES:
        return "LWPOLYLINE"
    return name.replace("AcDb", "").upper()


def _com_normal(entity) -> tuple[float, float, float]:
    """``entity.Normal``; an entity that exposes none lies in WCS XY."""
    try:
        return tuple(float(v) for v in entity.Normal)
    except Exception as exc:
        log.debug("Normal unreadable on %s, taken as WCS: %s", entity.ObjectName, exc)
        return (0.0, 0.0, 1.0)


def _refuse_tilted(where: str, kind: str, handle, normal) -> None:
    raise UnsupportedCapabilityError(
        "ocs_tilted_plane",
        f"{where}: {kind} {handle} lies in a plane tilted out of WCS XY "
        f"(normal={tuple(round(float(v), 4) for v in normal)}); WCS [x, y] values do not "
        "determine points in that plane, so writing them would move the entity out of its "
        "own plane by an amount this server cannot infer. Use entity_move, which keeps the "
        "plane, or edit it in AutoCAD.",
    )


#: What a polyline recreated by :func:`_com_replace_polyline` takes over from
#: the one it replaces, besides its XDATA (the typelib spellings - see the
#: note above ``_layer_info``).
_POLYLINE_CARRIED = ("Layer", "color", "Linetype", "LinetypeScale", "Lineweight")


def _com_write_vertices(doc, ent, handle, pts, bulges, closed, where: str) -> EntityInfo:
    """Rewrite a live LWPOLYLINE's vertices: ``Coordinates`` + ``SetBulge`` + ``Closed``.

    ``Coordinates`` is OCS on a lightweight polyline (the one ActiveX point
    property that is), so a mirrored frame is written through ``backends/ocs.py``
    and a tilted one refused before any write. Whether ActiveX takes a vertex
    list of a different length in place is unmeasured (spike S4): the write is
    tried, and when AutoCAD refuses it - or answers without changing the count -
    the polyline is recreated by :func:`_com_replace_polyline` and the result
    says so in ``replaced``. A failure at an unchanged count is raised: it is
    not the S4 question and replacing would hide it.
    """
    if _com_dxf_type(ent) != "LWPOLYLINE":
        raise RuntimeError(
            f"{where}: handle {handle} is {_com_dxf_type(ent)}, expected LWPOLYLINE (a "
            "lightweight polyline); edit a LINE / CIRCLE / ARC with entity_edit_geometry."
        )
    normal = _com_normal(ent)
    framed = not ocs.is_wcs_frame(normal)
    if framed and not ocs.is_flat_frame(normal):
        _refuse_tilted(where, "polyline", handle, normal)
    old_count = len(list(ent.Coordinates)) // 2
    keep = bulges is None and old_count == len(pts)
    final = None if keep else resolved_bulges(bulges, len(pts), [])
    flat: list[float] = []
    for px, py in pts:
        ox, oy = ocs.from_wcs(normal, px, py)[:2] if framed else (px, py)
        flat.extend([ox, oy])
    try:
        ent.Coordinates = _av(flat)
    except Exception as exc:
        if old_count == len(pts):
            raise
        log.info(
            "%s: Coordinates refused a %d -> %d vertex write on %s (%s); recreating it",
            where,
            old_count,
            len(pts),
            handle,
            exc,
        )
        return _com_replace_polyline(doc, ent, handle, pts, final, closed, normal, where)
    if old_count != len(pts) and len(list(ent.Coordinates)) // 2 != len(pts):
        log.info(
            "%s: Coordinates kept %d vertices on %s after a %d-vertex write; recreating it",
            where,
            old_count,
            handle,
            len(pts),
        )
        return _com_replace_polyline(doc, ent, handle, pts, final, closed, normal, where)
    if final is not None:
        for index, bulge in enumerate(final):
            ent.SetBulge(index, ocs.wcs_bulge(normal, bulge) if framed else bulge)
    if closed is not None:
        ent.Closed = bool(closed)
    _regen(ent)
    return _entity_info(ent)


def _com_replace_polyline(doc, old, handle, pts, bulges, closed, normal, where) -> EntityInfo:
    """The S4 fallback: the same polyline, recreated in the old one's owner.

    Drawn in WCS from the WCS points and bulges (so a mirrored frame becomes a
    plain one with the same geometry), at the old elevation, with the old
    layer, colour, linetype, linetype scale, lineweight, closed flag and XDATA.
    The old polyline is deleted only after all of that landed; anything that
    could not be carried deletes the new one and raises, leaving the drawing as
    it was - a replacement without its feature tag is the loss this whole item
    exists to prevent. Groups, hyperlinks and draw order are not carried.
    """
    owner = doc.ObjectIdToObject(old.OwnerID)
    new = owner.AddLightWeightPolyline(_av([c for p in pts for c in p]))
    try:
        new.Closed = bool(old.Closed) if closed is None else bool(closed)
        elevation = float(old.Elevation)
        if elevation:
            new.Elevation = elevation if float(normal[2]) > 0 else -elevation
        for index, bulge in enumerate(bulges or ()):
            if bulge:
                new.SetBulge(index, float(bulge))
        for name in _POLYLINE_CARRIED:
            setattr(new, name, getattr(old, name))
        # GetXData("") hands back every app's stream with its 1001 markers
        # inline (see entity_get_xdata), which is exactly what SetXData takes.
        codes, values = old.GetXData("")
        codes, values = list(codes or []), list(values or [])
        if codes:
            new.SetXData(
                _ai(codes),
                win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_VARIANT,
                    [_apoint(*v) if isinstance(v, (tuple, list)) else v for v in values],
                ),
            )
        old.Delete()
    except Exception as exc:
        try:
            new.Delete()
        except Exception as cleanup:  # pragma: no cover - the cause is the error to raise
            log.error("%s: could not remove the half-made replacement: %s", where, cleanup)
        raise RuntimeError(
            f"{where}: AutoCAD would not take {len(pts)} vertices on polyline {handle} in "
            f"place, and recreating it failed ({type(exc).__name__}: {exc}); the polyline "
            "was left as it was."
        ) from exc
    _regen(new)
    info = _entity_info(new)
    info.properties["replaced"] = [{"old": str(handle), "new": info.handle}]
    return info


def _com_move_text(ent, handle, tx, ty, where: str) -> None:
    """Move a live TEXT / MTEXT so its ``InsertionPoint`` lands on (tx, ty).

    ``Move`` translates the whole entity, so an aligned TEXT keeps its
    alignment point in step; ``InsertionPoint`` is WCS over ActiveX. A TEXT in
    a tilted plane is refused, as headlessly.
    """
    if tx is None and ty is None:
        return
    if _com_dxf_type(ent) == "TEXT":
        normal = _com_normal(ent)
        if not ocs.is_flat_frame(normal):
            _refuse_tilted(where, "text", handle, normal)
    at = ent.InsertionPoint
    dx = tx - float(at[0]) if tx is not None else 0.0
    dy = ty - float(at[1]) if ty is not None else 0.0
    ent.Move(_apoint(0.0, 0.0, 0.0), _apoint(dx, dy, 0.0))


class _Refresh:
    """One COM job's pending display refresh (1.8-23).

    Up to 1.8.0 every create path ended in ``Regen(acActiveViewport)`` - a
    regeneration of the whole viewport, O(drawing), once per entity, so a
    200-entity batch regenerated a 5,000-line drawing 200 times. ``_regen``
    now only records the request here; ``ComBackend._run``'s job flushes it
    once, in its ``finally``, through ``_flush_refresh``.
    """

    __slots__ = ("requests", "last", "anonymous", "done")

    def __init__(self):
        #: ``_regen`` calls in this job (one per entity a create path drew).
        self.requests = 0
        #: The entity the latest request named.
        self.last = None
        #: A request named no entity (a layer change, an explode, an array):
        #: only a Regen repaints that.
        self.anonymous = False
        #: What the flush did: ``"update"`` / ``"regen"`` / None (nothing asked).
        self.done = None


def _regen(entity=None):
    """Ask for a display refresh of ``entity`` (or of the viewport) - per job, not per call.

    Inside a ``ComBackend._run`` job the request is recorded and the job's
    ``finally`` refreshes once (``_flush_refresh``): ``entity.Update()`` when
    the job asked at most ``COM_REFRESH_UPDATE_MAX`` times (default 1) and
    every request named its entity, one ``Regen(acActiveViewport)`` otherwise
    or with ``COM_REFRESH_MODE=regen``. Outside a job (a test calling a
    ``_sync`` body directly) the viewport is regenerated at once, as up to
    1.8.0.
    """
    if _COM_STATE.get("batch_mode"):
        return
    pending = getattr(_JOB_STATE, "refresh", None)
    if pending is None:
        _regen_viewport()
        return
    pending.requests += 1
    if entity is None:
        pending.anonymous = True
    else:
        pending.last = entity


def _regen_viewport() -> None:
    """``Regen(acActiveViewport)`` on the job's document, booked as ``refresh``; never raises."""
    try:
        doc = _acad_doc()
        began = time.perf_counter()
        try:
            doc.Regen(0)  # 0 = acActiveViewport
        finally:
            _job_clock_add("refresh", (time.perf_counter() - began) * 1000.0)
    except Exception as exc:
        log.debug("Regen failed: %s", exc)


def _flush_refresh(pending: _Refresh | None) -> str | None:
    """Refresh once for the whole job (COM thread; never raises). What it did, or None.

    S4 (``Entity.Update()`` display) is still unmeasured, so the choice is the
    plan's safe default: ``Update()`` only for a job of at most
    ``com_refresh_update_max`` named entities (default 1 - a single create),
    one Regen above that, and one Regen whenever ``Update()`` raises.
    """
    if pending is None or not pending.requests:
        return None
    settings = config.settings
    if (
        settings.com_refresh_mode == "auto"
        and not pending.anonymous
        and pending.last is not None
        and pending.requests <= settings.com_refresh_update_max
    ):
        began = time.perf_counter()
        try:
            pending.last.Update()
        except Exception as exc:  # falls back to the viewport Regen below
            log.debug("Update() failed (%s); regenerating the viewport instead", exc)
        else:
            pending.done = "update"
            return pending.done
        finally:
            _job_clock_add("refresh", (time.perf_counter() - began) * 1000.0)
    _regen_viewport()
    pending.done = "regen"
    return pending.done


# ── styles (track E): module helpers ─────────────────────────────────────────
#
# The ActiveX object model has no per-style getters or setters for dimension
# variables: `AcadDimStyle` exposes `Name` and `CopyFrom` and nothing else. A
# style's values are therefore written the way AutoCAD's own DIMSTYLE command
# writes them — set the DIM* system variables on the document (which makes
# them overrides of the *current* style) and `CopyFrom(document)` to save
# those settings into the target style. Assigning `ActiveDimStyle` restores
# that style's saved settings and discards unsaved overrides (AutoCAD's
# `-DIMSTYLE Restore` rule; the fake in tests/test_styles.py models exactly
# this, and scripts/smoke_settings_com.py confirms it live). Reading is
# one-sided: `GetVariable` reads the *current* style only, so `dimstyle_list`
# reports values for the current style and `values_available: False` for the
# rest rather than cycling the active style behind the operator's back.


def _com_named(collection, name: str):
    """A collection member by name, case-insensitive (AutoCAD's rule), or None."""
    wanted = name.strip().lower()
    for index in range(collection.Count):
        item = collection.Item(index)
        if str(item.Name).lower() == wanted:
            return item
    return None


def _com_dimvar_for_write(var: str, value: Any) -> Any:
    """ActiveX takes DIMDSEP and the arrowhead/text-style names as strings."""
    if var in ("DIMDSEP", "DIMBLK", "DIMBLK1", "DIMBLK2", "DIMTXSTY"):
        return str(value)
    if isinstance(value, float):
        return float(value)
    return int(value)


def _com_dimvar_for_report(var: str, raw: Any) -> Any:
    if var == "DIMDSEP":
        if isinstance(raw, int):
            return "," if raw == 0 else chr(raw)
        text = str(raw)
        return text[:1] if text else ","
    if var in ("DIMBLK", "DIMBLK1", "DIMBLK2"):
        # AutoCAD reports the block name (``_OBLIQUE``, ``""`` for closed
        # filled) — already canonical; the same read-side rule as ezdxf keeps
        # the two engines' rows spelled identically.
        from engineering.standards.dimstyles import reported_arrowhead

        return reported_arrowhead(raw)
    if var == "DIMTXSTY":
        return str(raw)
    return raw


def _same_dimvar(old: Any, new: Any) -> bool:
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(float(old) - float(new)) < 1e-9
    return str(old) == str(new)


#: ``SelectionSet.Select`` mode ``acSelectionSetAll`` (AcSelect enum): every
#: entity in the database, in every layout — ``ssget "X"``.
_AC_SELECTION_SET_ALL = 5

#: Characters an ``ssget`` filter string treats as wildcards (AutoCAD's
#: ``wcmatch`` grammar). Symbol names may carry most of them: a dimension
#: style called ``A.B`` also matched ``A_B``, ``N#1`` matched ``N21``,
#: ``P[1]`` matched ``P1`` and ``S@X`` matched ``SAX`` until the name was
#: escaped (measured live, AutoCAD 2026).
_WCMATCH_SPECIALS = frozenset("*?#@.~[]-,`")


def _wcmatch_literal(text: str) -> str:
    """``text`` spelled so an ``ssget`` filter matches it literally.

    Every wildcard-significant character is escaped with a backtick, the
    grammar's own escape. A symbol name can never contain a backtick itself
    (AutoCAD refuses one), so the escaped form is unambiguous; the match stays
    case-insensitive, as any symbol-table name comparison is.
    """
    return "".join("`" + char if char in _WCMATCH_SPECIALS else char for char in text)


def _com_require_dimensions(doc, handles) -> list:
    """The live DIMENSION objects of ``handles``; a handle that is not one is
    refused by name before any of them is touched."""
    objects = []
    for handle in handles:
        entity = _handle_to_object(doc, str(handle))
        if not str(entity.ObjectName).endswith("Dimension"):
            raise ValueError(
                f"dimension_rerender: {handle} is a {_com_dxf_type(entity)}, not a DIMENSION"
            )
        objects.append(entity)
    return objects


def _rerender_report(handles, before: dict, after: dict) -> dict:
    """What a live redraw did, read back (1.8-15; spike S4 pending).

    ``after`` / ``before`` map a handle to ``(rendered_text, matches)`` - the
    ``*D`` text read through ``Document.Export`` and whether it says what the
    fields and style say (``engineering/dimtext.py::rendered_matches``).
    ``rerendered``: the text changed and is now right; ``already_current``: it
    did not change and was right already; ``stale``: it is not right after
    ``Update()`` - the live regen claim is not confirmed for it.
    """
    rerendered, current, stale = [], [], []
    for handle in handles:
        key = str(handle).upper()
        text, ok = after.get(key, (None, None))
        if ok is False or (ok is None and text is None):
            stale.append(str(handle))
        elif text != before.get(key, (None, None))[0]:
            rerendered.append(str(handle))
        else:
            current.append(str(handle))
    return {
        "ok": True,
        "rerendered": rerendered,
        "already_current": current,
        "stale": stale,
        "text_kept": [],
        "verified": not stale,
    }


def _com_dimensions_using(doc, style_name: str) -> list[str]:
    """Handles of every DIMENSION, in every layout, whose style is ``style_name``.

    One filtered ``SelectionSet.Select(acSelectionSetAll)`` — the ``ssget "X"``
    route with ``(0 . "DIMENSION") (3 . <style>)`` — answers from AutoCAD's own
    index, so the cost does not grow with the drawing. The walk this replaced
    read ``ObjectName`` on every entity of every layout through a dynamic
    proxy (~8-10 ms each), which put ``dimstyle_modify`` past the 60 s
    ``COM_CALL_TIMEOUT`` on a drawing of ~6k entities — after its write had
    already landed. Measured live (AutoCAD 2026, 1000 LINEs + 14 dimensions
    across model and paper space): walk 12.7-15.8 s, this 0.02-0.09 s, same
    handles. The style name is escaped as a literal (``_wcmatch_literal``)
    because the filter is a wildcard pattern; the ``DIMENSION`` group-0 name
    keeps parity with the headless engine's ``query("DIMENSION")``.

    Handles come back in ascending handle order (database order), whatever
    order the selection set reports them in. Nothing is swallowed: a
    selection that fails is an error the caller sees, not an empty list
    reported as fact.
    """
    ss = doc.SelectionSets.Add(f"_DIMSTYLE_{uuid.uuid4().hex[:8]}")
    try:
        filter_types = _ai([0, 3])
        filter_values = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_VARIANT,
            ["DIMENSION", _wcmatch_literal(style_name)],
        )
        ss.Select(_AC_SELECTION_SET_ALL, None, None, filter_types, filter_values)
        # ``Handle`` is an IAcadObject member, so it answers on the narrowed
        # ``IAcadEntity`` wrapper ``SelectionSet.Item`` hands back.
        handles = [str(ss.Item(index).Handle) for index in range(ss.Count)]
    finally:
        try:
            ss.Delete()
        except Exception as exc:
            log.debug("SelectionSet cleanup failed: %s", exc)
    return sorted(handles, key=lambda handle: int(handle, 16))


def _com_select_all(doc, codes: list[int], values: list) -> list:
    """The members of one ``SelectionSet.Select(acSelectionSetAll, ...)`` (1.8-12).

    ``ssget "X"``: every layout, never the screen - a crossing or window
    selection only finds what the current view shows. ``codes`` / ``values``
    are the DXF group-code filter (``backends.set_edits.com_filter``); none
    selects everything. The set is deleted again whatever happens; a failed
    selection raises (never an empty list reported as fact).
    """
    ss = doc.SelectionSets.Add(f"_WHERE_{uuid.uuid4().hex[:8]}")
    try:
        if codes:
            filter_values = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, list(values)
            )
            ss.Select(_AC_SELECTION_SET_ALL, None, None, _ai(codes), filter_values)
        else:
            ss.Select(_AC_SELECTION_SET_ALL)
        return [ss.Item(index) for index in range(ss.Count)]
    finally:
        try:
            ss.Delete()
        except Exception as exc:
            log.debug("SelectionSet cleanup failed: %s", exc)


#: File suffixes AutoCAD hands to the Windows font system rather than its
#: SHX loader (see ``_com_locate_font``).
_TRUETYPE_SUFFIXES = frozenset({".ttf", ".ttc", ".otf"})


def _com_font_folders() -> list[Path]:
    """Every folder a font file is looked for, in order.

    AutoCAD's support path (``Preferences.Files.SupportPath``, which carries
    the install's ``fonts`` folder with the SHX shape files), then the Windows
    font folders — machine-wide ``%WINDIR%\\Fonts`` (where AutoCAD's installer
    puts ISOCPEUR next to Arial; measured) and the per-user
    ``%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts``.
    """
    folders: list[Path] = []
    try:
        support = str(_acad_app().Preferences.Files.SupportPath)
    except Exception:
        support = ""
    folders.extend(Path(folder) for folder in support.split(";") if folder)
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        folders.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        folders.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return folders


def _com_font_path(font_file: str) -> Path | None:
    """The file ``font_file`` names on this machine, or None when nobody has it.

    An absolute path must exist as given; a bare name is searched over
    ``_com_font_folders``, and a name without a suffix also as ``.shx`` (the
    SHX loader appends it: ``fontFile = "isocp"`` is accepted live and stored
    as given).
    """
    path = Path(font_file)
    if path.is_absolute():
        return path if path.is_file() else None
    names = [font_file] if path.suffix else [font_file, font_file + ".shx"]
    for folder in _com_font_folders():
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    return None


def _truetype_face(path: Path) -> tuple[str, bool, bool] | None:
    """``(family, bold, italic)`` from a TrueType/OpenType file's ``name`` table.

    The family (name ID 1) is the typeface Windows GDI — and therefore
    AutoCAD's ``TextStyle.SetFont`` — knows the font by; the subfamily (ID 2)
    says whether it is the bold and/or italic face, which ``SetFont`` takes
    as flags. Windows/Unicode strings (platform 3, en-US preferred) win over
    Macintosh Roman ones. A collection (``ttcf``) is read by its first font.
    None for a file that is not an sfnt or carries no family name.
    """
    import struct

    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        offset = struct.unpack(">I", data[12:16])[0] if data[:4] == b"ttcf" else 0
        num_tables = struct.unpack(">H", data[offset + 4 : offset + 6])[0]
        name_table = None
        for index in range(num_tables):
            record = offset + 12 + 16 * index
            if data[record : record + 4] == b"name":
                name_table = struct.unpack(">II", data[record + 8 : record + 16])
                break
        if name_table is None:
            return None
        table_offset, _length = name_table
        count, strings = struct.unpack(">HH", data[table_offset + 2 : table_offset + 6])
        found: dict[tuple[int, int], str] = {}  # (name id, priority) -> text
        for index in range(count):
            record = table_offset + 6 + 12 * index
            platform, _encoding, language, name_id, length, start = struct.unpack(
                ">HHHHHH", data[record : record + 12]
            )
            if name_id not in (1, 2):
                continue
            raw = data[table_offset + strings + start : table_offset + strings + start + length]
            if platform == 3:
                priority = 0 if language == 0x409 else 1
                text = raw.decode("utf-16-be", "replace")
            elif platform == 1:
                priority = 2
                text = raw.decode("mac-roman", "replace")
            else:
                continue
            found.setdefault((name_id, priority), text)
    except (struct.error, IndexError):
        return None
    family = next((found[key] for key in sorted(found) if key[0] == 1), "").strip()
    if not family:
        return None
    subfamily = next((found[key] for key in sorted(found) if key[0] == 2), "").lower()
    return family, "bold" in subfamily, "italic" in subfamily or "oblique" in subfamily


class _ComFont:
    """A font file located for the live engine: where it is and, for
    TrueType, the typeface ``SetFont`` needs (None for an SHX)."""

    __slots__ = ("font_file", "path", "face")

    def __init__(self, font_file: str, path: Path, face: tuple[str, bool, bool] | None):
        self.font_file = font_file
        self.path = path
        self.face = face


def _com_font_refusal(key: str, font_file: str, detail: str) -> ValueError:
    from engineering.standards.textstyles import TEXT_PRESETS

    presets = sorted(font for font, _w, _o in TEXT_PRESETS.values())
    return ValueError(
        f"{key}: font file {font_file!r} {detail}; ActiveX refuses a font it cannot open, "
        "so the live engine refuses it before any write (headlessly it would be written and "
        "reported font_resolved false) — copy the file to a support-path or Windows Fonts "
        f"folder, or use a bundled preset ({presets})"
    )


def _com_locate_font(key: str, font_file: str) -> _ComFont:
    """The font ``font_file`` names, resolved *before* ``TextStyles.Add`` — or a refusal.

    Asked first because ``TextStyle.fontFile`` on a file AutoCAD cannot open
    raises ``DISP_E_EXCEPTION 'Filer error'`` (measured on AutoCAD 2026 for
    ``nosuchfont.shx``) *after* the table already holds the new name with an
    empty font — a stub a retry then meets as "already exists". A TrueType
    file must also carry a readable family name, because that — not the file
    name — is what the write uses (``_com_write_font``).
    """
    path = _com_font_path(font_file)
    if path is None:
        raise _com_font_refusal(
            key, font_file, "is not on AutoCAD's support path or in the Windows Fonts folder"
        )
    face = None
    if path.suffix.lower() in _TRUETYPE_SUFFIXES:
        face = _truetype_face(path)
        if face is None:
            raise _com_font_refusal(key, font_file, f"({path}) is not a readable TrueType file")
    return _ComFont(font_file, path, face)


def _com_write_font(style, font: _ComFont) -> None:
    """Set ``style``'s font from a ``_com_locate_font`` result.

    Measured on AutoCAD 2026: ``fontFile = "isocp.shx"`` (an SHX on the
    support path) is accepted and stored as given, so an SHX is written by
    the name the caller gave. A bare TrueType name — ``"arial.ttf"``,
    ``"isocpeur.ttf"``, bundled presets included — raises ``'Filer error'``
    although the files exist; the full path is accepted but the STYLE record
    then carries the machine path (``fontFile`` reads back
    ``C:\\Windows\\Fonts\\arial.ttf``); ``SetFont("Arial", bold, italic, 0, 0)``
    — AutoCAD's own STYLE-dialog route — is accepted and reads back as the
    clean ``arial.ttf``. A TrueType font is therefore written by its typeface,
    and ``SetFont`` refusing a face Windows has not installed (``'Invalid
    input'``) is the one failure that can still land after ``Add``; the
    caller removes the entry it added.
    """
    if font.face is None:
        style.fontFile = font.font_file
    else:
        family, bold, italic = font.face
        style.SetFont(family, bold, italic, 0, 0)


def _com_delete_quietly(obj, what: str) -> None:
    """Best-effort ``Delete`` of a half-made table entry on an error path."""
    try:
        obj.Delete()
    except Exception as exc:
        log.warning("could not remove half-made %s: %s", what, exc)


def _com_create_textstyle(
    doc, name: str, font_file: str, *, width: float, oblique_deg: float, height: float, key: str
):
    """``TextStyles.Add`` plus its font, width, oblique and height — or nothing.

    The font is located first (``_com_locate_font``) so a refusal precedes
    the ``Add``; a write that still fails removes the entry it added, because
    a STYLE row with an empty font that blocks a retry as "already exists" is
    exactly the half-made state measured before this helper existed.
    """
    font = _com_locate_font(key, font_file)
    style = doc.TextStyles.Add(name)
    try:
        _com_write_font(style, font)
        style.Width = float(width)
        style.ObliqueAngle = deg2rad(oblique_deg)
        style.Height = float(height)
    except Exception:
        _com_delete_quietly(style, f"text style {name!r}")
        raise
    return style


def _com_require_arrowhead_blocks(doc, values: dict[str, Any]) -> None:
    """Refuse a user arrowhead block the drawing does not define — before any write.

    The live twin of the headless ``_require_arrowhead_blocks``: a built-in
    (``ARROWHEAD_BLOCKS``) and closed filled (``""``) need no block, a user
    name must be in ``doc.Blocks`` — AutoCAD's ``SetVariable("DIMBLK",
    "NOSUCHBLOCK")`` raises ``DISP_E_EXCEPTION`` mid-write, after
    ``DimStyles.Add`` and ``ActiveDimStyle`` (measured: the half-made style was
    left *current*). The name is rewritten to the block table's own spelling,
    the rule the headless engine follows.
    """
    from engineering.standards.dimstyles import ARROWHEAD_BLOCKS

    for var in ("DIMBLK", "DIMBLK1", "DIMBLK2"):
        name = values.get(var)
        if not name or name in ARROWHEAD_BLOCKS:
            continue
        block = _com_named(doc.Blocks, name)
        if block is None:
            raise ValueError(
                f"{var}: block {name!r} does not exist in this drawing; define it first "
                "with block_define, or name one of AutoCAD's built-in arrowheads"
            )
        values[var] = str(block.Name)


_MLEADERSTYLE_DICTIONARY = "ACAD_MLEADERSTYLE"
_MLEADERSTYLE_CLASS = "AcDbMLeaderStyle"


def _com_unnarrow(obj):
    """Re-dispatch ``obj`` on its raw ``IDispatch`` so its *runtime* class is reachable.

    acax25enu.tlb declares ``IAcadDictionaries.Item``, ``IAcadDictionary.Item``
    and ``IAcadDictionary.AddObject`` as returning ``IAcadObject*``. Whenever a
    makepy cache for the AutoCAD type library exists (it does on any machine
    where ``win32com.client.gencache`` ever ran against AutoCAD), pywin32 wraps
    such a return in the gen_py class of the *declared* type, and that wrapper
    exposes only ``IAcadObject`` members: ``.Count`` / ``.Item`` / ``.GetName``
    on the dictionary and ``.ArrowSize`` / ``.TextStyle`` on a leader style all
    raise ``AttributeError`` even though the live object answers them.
    (``HandleToObject`` is declared ``IDispatch*``, which keeps the concrete
    class; ``ModelSpace.Item`` is declared ``IAcadEntity*`` and meets the same
    problem - ``_entity_info`` un-narrows it through ``_com_concrete_entity``.)

    ``win32com.client.dynamic.Dispatch`` on the raw interface builds a
    late-bound proxy from the object's own type info, so every member of the
    concrete class resolves — verified live on AutoCAD 2026 — and, unlike
    ``CastTo``, it never calls ``gencache.EnsureDispatch`` and never writes the
    makepy cache. A wrapper whose ``_oleobj_`` is not a COM interface (the
    fake in tests/test_styles.py) is unwrapped to that object as-is; anything
    without ``_oleobj_`` is returned unchanged.
    """
    ole = getattr(obj, "_oleobj_", None)
    if ole is None:
        return obj
    if _COM_IMPORTS_OK and isinstance(ole, pythoncom.TypeIIDs[pythoncom.IID_IDispatch]):
        return win32com.client.dynamic.Dispatch(ole)
    return ole


def _com_mleaderstyle_dictionary(doc, *, create: bool = False):
    """The ``ACAD_MLEADERSTYLE`` dictionary, or None when the drawing has none.

    ActiveX exposes no ``MLeaderStyles`` *collection*, but the dictionary's
    items are full ``IAcadMLeaderStyle`` objects (acax25enu.tlb: ``Name``,
    ``ArrowSize``, ``LandingGap``, ``TextHeight``, ``TextStyle``, ...), and
    ``IAcadDictionary.AddObject(keyword, "AcDbMLeaderStyle")`` — the
    documented VBA route — creates one. Every drawing AutoCAD makes carries
    the dictionary; ``create`` adds it to a foreign file that does not.

    Every object this route hands over goes through ``_com_unnarrow`` (see
    there): ``Dictionaries.Item`` is declared ``IAcadObject*`` and comes back
    as a wrapper with no ``Count``. Verified live on AutoCAD 2026 (list and
    create on a scratch document); scripts/smoke_settings_com.py re-runs it.
    """
    try:
        dictionary = doc.Dictionaries.Item(_MLEADERSTYLE_DICTIONARY)
    except Exception:
        if not create:
            return None
        dictionary = doc.Dictionaries.Add(_MLEADERSTYLE_DICTIONARY)
    return _com_unnarrow(dictionary)


def _com_mleaderstyle_rows(dictionary) -> list[tuple[str, Any]]:
    """``(name, AcadMLeaderStyle)`` for every item, named by the dictionary key.

    Each ``Item(i)`` is un-narrowed: declared ``IAcadObject*``, it would
    otherwise carry no ``ArrowSize``.
    """
    rows = []
    for index in range(dictionary.Count):
        style = _com_unnarrow(dictionary.Item(index))
        rows.append((str(dictionary.GetName(style)), style))
    return rows


def _ensure_com_textstyle(doc, name: str, *, refusal_key: str) -> tuple[str, bool]:
    """``(name as AutoCAD spells it, created)``; a preset is created, anything else missing is refused."""
    from engineering.standards.textstyles import TEXT_PRESETS

    existing = _com_named(doc.TextStyles, name)
    if existing is not None:
        return str(existing.Name), False
    preset = TEXT_PRESETS.get(name.upper())
    if preset is None:
        raise ValueError(
            f"{refusal_key}: text style {name!r} does not exist in this drawing and is not a "
            f"bundled preset ({sorted(TEXT_PRESETS)}); create it first with textstyle_create"
        )
    font_file, width, oblique = preset
    _com_create_textstyle(
        doc, name.upper(), font_file, width=width, oblique_deg=oblique, height=0.0, key=refusal_key
    )
    return name.upper(), True


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
#: Solids carry a volume but no surface area over ActiveX. Verified on a live
#: AutoCAD 2026 (2026-08-06): AcDb3dSolid answers `.Volume` and raises on `.Area`.
_COM_NO_SURFACE_AREA_TYPES = frozenset({"AcDb3dSolid", "AcDbBody"})

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


def _com_bulges(entity, coords, closed: bool, length, stride: int = 2) -> list[float]:
    """Every vertex's bulge, read only when ``Length`` says an arc exists.

    ``coords`` is the flat ``Coordinates`` variant: (x, y) pairs on a
    lightweight polyline, (x, y, z) triples on a heavy 2D one (``stride``). A
    circular arc is strictly longer than its chord, so when AutoCAD's own
    ``Length`` equals the chord walk the polyline is straight everywhere and
    the per-vertex COM round trips are skipped.
    """
    count = len(coords) // stride
    pts = [(float(coords[i]), float(coords[i + 1])) for i in range(0, stride * count, stride)]
    chord = sum(math.dist(a, b) for a, b in zip(pts, pts[1:], strict=False))
    if closed and count > 1:
        chord += math.dist(pts[-1], pts[0])
    try:
        straight = abs(float(length) - chord) <= 1e-9 * max(1.0, chord)
    except (TypeError, ValueError):
        straight = False
    if straight:
        return [0.0] * count
    return [_com_bulge(entity, i) for i in range(count)]


def _is_right_angle(rotation: float) -> bool:
    """True when ``rotation`` (radians) is a multiple of a quarter turn."""
    quarter = rotation / (math.pi / 2.0)
    return abs(quarter - round(quarter)) <= 1e-9


def _arc_extent_points(cx, cy, radius, start, end):
    """The points that bound a counter-clockwise circular arc: its two ends
    and every quadrant point (0, 90, 180, 270 degrees) the sweep passes."""
    sweep = (end - start) % (2.0 * math.pi)
    pts = [
        (cx + radius * math.cos(start), cy + radius * math.sin(start)),
        (cx + radius * math.cos(end), cy + radius * math.sin(end)),
    ]
    for q in range(4):
        angle = q * math.pi / 2.0
        if (angle - start) % (2.0 * math.pi) <= sweep:
            pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return pts


def _com_member_extent_points(member, xform, sx, sy, rotation):
    """WCS points whose extents are exactly the transformed member's extents,
    for the members a P&ID symbol is drawn from; None when the member's
    geometry is not one this reads (text, hatch, spline, ellipse, a nested
    reference) so the caller falls back to its box and says so.

    A LINE, a POINT and a straight polyline are bounded by their vertices
    under any affine map. A CIRCLE becomes an ellipse under an axis scale, and
    its extents after a rotation are closed-form. An ARC and a bulged
    polyline segment stay circular only under a uniform scale; a reflection
    (``sx * sy < 0``) reverses their sense.
    """
    name = member.ObjectName
    uniform = abs(abs(sx) - abs(sy)) <= 1e-12
    mirrored = sx * sy < 0
    if name == "AcDbLine":
        s, e = member.StartPoint, member.EndPoint
        return [xform(s[0], s[1]), xform(e[0], e[1])]
    if name == "AcDbPoint":
        c = member.Coordinates
        return [xform(c[0], c[1])]
    if name == "AcDbCircle":
        c = member.Center
        r = float(member.Radius)
        cx, cy = xform(c[0], c[1])
        a, b = r * abs(sx), r * abs(sy)
        cos_r, sin_r = math.cos(rotation), math.sin(rotation)
        hx = math.hypot(a * cos_r, b * sin_r)
        hy = math.hypot(a * sin_r, b * cos_r)
        return [(cx - hx, cy - hy), (cx + hx, cy + hy)]
    if name == "AcDbArc":
        if not uniform:
            return None
        c = member.Center
        r = float(member.Radius)
        cx, cy = xform(c[0], c[1])
        ends = []
        for angle in (float(member.StartAngle), float(member.EndAngle)):
            px, py = xform(c[0] + r * math.cos(angle), c[1] + r * math.sin(angle))
            ends.append(math.atan2(py - cy, px - cx))
        if mirrored:
            ends.reverse()
        return _arc_extent_points(cx, cy, r * abs(sx), ends[0], ends[1])
    if name in _COM_LWPOLYLINE_NAMES:
        coords = list(member.Coordinates)
        closed = bool(member.Closed)
        bulges = _com_bulges(member, coords, closed, member.Length)
        pts = [xform(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
        if not any(bulges):
            return pts
        if not uniform:
            return None
        from ezdxf.math import Vec2, bulge_to_arc

        segments = list(zip(pts, pts[1:], bulges, strict=False))
        if closed and len(pts) > 1:
            segments.append((pts[-1], pts[0], bulges[len(pts) - 1]))
        out = list(pts)
        for a, b, bulge in segments:
            if not bulge or math.dist(a, b) < 1e-12:
                continue
            centre, start, end, radius = bulge_to_arc(
                Vec2(a), Vec2(b), -bulge if mirrored else bulge
            )
            out.extend(_arc_extent_points(centre.x, centre.y, radius, start, end))
        return out
    return None


def _com_geometry_bbox(entity) -> dict | None:
    """WCS extents of a block reference's *drawn* geometry, attributes excluded.

    ``GetBoundingBox`` on an INSERT takes its ATTRIBs with it, so a TAG lettered
    above a valve pushes the box past the body — a line ending on the body's
    real edge then lies *inside* the box. The definition's members are read in
    block space (skipping ``AcDbAttributeDefinition``) and carried through the
    INSERT (origin, scale, rotation).

    At a right-angle rotation (the P&ID case) each member's own box maps to
    an axis-aligned box, so carrying its corners is exact. Off a right angle
    the box of a rotated box is too large — a 45-degree-turned circle of
    radius 5 would read as 7.07 — so each member's real geometry is measured
    (`_com_member_extent_points`); a member that cannot be, or refuses, is
    carried by its corners and the result is marked ``approximate: True`` so
    no reader mistakes it for the extents the ezdxf engine reports. None when
    there is nothing drawn or ActiveX refuses — the caller then omits the key.
    """
    try:
        try:
            doc = entity.Document
        except Exception:
            doc = _acad_doc()
        blk = doc.Blocks.Item(entity.Name)
        try:
            origin = tuple(blk.Origin)
        except Exception:
            origin = (0.0, 0.0, 0.0)
        ins = entity.InsertionPoint
        sx, sy = float(entity.XScaleFactor), float(entity.YScaleFactor)
        rot = float(entity.Rotation)
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        right_angle = _is_right_angle(rot)

        def xform(bx, by):
            lx = (float(bx) - float(origin[0])) * sx
            ly = (float(by) - float(origin[1])) * sy
            return (
                float(ins[0]) + lx * cos_r - ly * sin_r,
                float(ins[1]) + lx * sin_r + ly * cos_r,
            )

        xs: list[float] = []
        ys: list[float] = []
        approximate = False
        for i in range(blk.Count):
            member = blk.Item(i)
            if member.ObjectName == "AcDbAttributeDefinition":
                continue
            pts = None
            if not right_angle:
                try:
                    pts = _com_member_extent_points(member, xform, sx, sy, rot)
                except Exception as exc:
                    log.debug("exact extents of block member failed: %s", exc)
                    pts = None
            if pts is None:
                try:
                    lo, hi = member.GetBoundingBox()
                except Exception:
                    continue
                corners = ((lo[0], lo[1]), (hi[0], lo[1]), (lo[0], hi[1]), (hi[0], hi[1]))
                pts = [xform(bx, by) for bx, by in corners]
                approximate = approximate or not right_angle
            xs.extend(x for x, _ in pts)
            ys.extend(y for _, y in pts)
        if not xs:
            return None
        box = {"min": [min(xs), min(ys)], "max": [max(xs), max(ys)]}
        if approximate:
            box["approximate"] = True
        return box
    except Exception as exc:
        log.debug("geometry bbox failed for block reference: %s", exc)
        return None


# What a lightweight polyline calls itself over ActiveX. The live name is
# ``AcDbPolyline`` (measured on AutoCAD 2026, 2026-08-06 — the member profile in
# tests/test_com_backend.py); ``AcDbLWPolyline`` is the DXF-flavoured spelling
# this code once matched on alone, which left every live LWPOLYLINE without
# ``points`` because the branch never fired. Both are accepted so the vertices
# come through whichever spelling a seat reports.
_COM_LWPOLYLINE_NAMES = ("AcDbPolyline", "AcDbLWPolyline")


# A tool filters by the DXF entity name; ActiveX names three of them differently
# (measured on AutoCAD 2026: AcDbBlockReference, AcDbPolyline and one class per
# dimension kind). The reported type stays the ActiveX spelling, so a caller
# already filtering by it is unaffected; the DXF name simply finds them too.
_DXF_TYPE_ALIASES: dict[str, frozenset[str]] = {
    "INSERT": frozenset({"BLOCKREFERENCE", "MINSERTBLOCK"}),
    "LWPOLYLINE": frozenset({"POLYLINE"}),
}


def _type_filter_matches(type_filter: str, ent_type: str) -> bool:
    """True when an entity whose ActiveX type is ``ent_type`` answers ``type_filter``."""
    wanted = type_filter.upper()
    if ent_type == wanted or ent_type in _DXF_TYPE_ALIASES.get(wanted, ()):
        return True
    # DXF has one DIMENSION entity; ActiveX has AcDbRotatedDimension,
    # AcDbAlignedDimension, AcDbDiametricDimension, AcDb2LineAngularDimension, …
    return wanted == "DIMENSION" and ent_type.endswith("DIMENSION")


def _prepare_hatch_edges(edges):
    """Validate a typed edge list the way the headless engine does.

    Returns ``(prepared, None)`` or ``(None, refusal)``; nothing is created
    until every edge has passed.
    """
    if not edges:
        return None, {"ok": False, "error": "edges must not be empty"}
    required = {
        "line": ("start", "end"),
        "arc": ("center", "radius", "start_angle", "end_angle"),
        "ellipse": ("center", "major_axis", "ratio"),
    }
    prepared = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            return None, {"ok": False, "error": f"edge {index} is not an object"}
        kind = str(edge.get("type", "")).strip().lower()
        if kind not in required:
            return None, {
                "ok": False,
                "error": (
                    f"edge {index}: unknown type {edge.get('type')!r}; valid types are "
                    "line, arc, ellipse"
                ),
            }
        missing = [key for key in required[kind] if key not in edge]
        if missing:
            return None, {"ok": False, "error": f"edge {index} ({kind}) is missing {missing[0]!r}"}
        keys = list(required[kind])
        if kind == "ellipse":
            keys += [k for k in ("start_angle", "end_angle") if k in edge]
        numbers = []
        try:
            for key in keys:
                value = edge[key]
                if isinstance(value, (list, tuple)):
                    numbers.extend(float(v) for v in value[:2])
                else:
                    numbers.append(float(value))
        except (TypeError, ValueError):
            return None, {"ok": False, "error": f"edge {index} ({kind}) has a non-numeric value"}
        if not all(math.isfinite(v) for v in numbers):
            return None, {"ok": False, "error": f"edge {index} ({kind}) has a non-finite value"}
        if kind == "arc" and float(edge["radius"]) <= 0:
            return None, {"ok": False, "error": f"edge {index} (arc) radius must be > 0"}
        if kind == "ellipse" and not 0 < float(edge["ratio"]) <= 1:
            return None, {"ok": False, "error": f"edge {index} (ellipse) ratio must be in (0, 1]"}
        prepared.append((kind, edge))
    return prepared, None


def _closed_line_chain(prepared, tol: float = 1e-6):
    """The loop's vertices when it is only lines, each starting where the last
    ended and the last closing on the first; otherwise ``None``."""
    if any(kind != "line" for kind, _ in prepared):
        return None
    points = [(float(e["start"][0]), float(e["start"][1])) for _, e in prepared]
    ends = [(float(e["end"][0]), float(e["end"][1])) for _, e in prepared]
    for i, end in enumerate(ends):
        nxt = points[(i + 1) % len(points)]
        if abs(end[0] - nxt[0]) > tol or abs(end[1] - nxt[1]) > tol:
            return None
    return points if len(points) >= 3 else None


def _temporary_edge_entity(space, kind: str, edge: dict):
    """One typed edge as a real ActiveX entity: LINE, ARC or ELLIPSE."""
    if kind == "line":
        (x1, y1), (x2, y2) = edge["start"][:2], edge["end"][:2]
        return space.AddLine(_apoint(x1, y1), _apoint(x2, y2))
    cx, cy = edge["center"][:2]
    ccw = bool(edge.get("ccw", True))
    if kind == "arc":
        start = math.radians(float(edge["start_angle"]))
        end = math.radians(float(edge["end_angle"]))
        if not ccw:  # an ActiveX arc runs counter-clockwise from start to end
            start, end = end, start
        return space.AddArc(_apoint(cx, cy), float(edge["radius"]), start, end)
    mx, my = edge["major_axis"][:2]
    ellipse = space.AddEllipse(_apoint(cx, cy), _apoint(mx, my), float(edge["ratio"]))
    start = float(edge.get("start_angle", 0.0))
    end = float(edge.get("end_angle", 360.0))
    if abs((end - start) % 360.0) > 1e-9:
        if not ccw:
            start, end = end, start
        # AcadEllipse.StartAngle/EndAngle are angles from the major axis, the
        # quantity the headless engine's ellipse edge carries in degrees.
        ellipse.StartAngle = math.radians(start)
        ellipse.EndAngle = math.radians(end)
    return ellipse


#: The gen_py class names pywin32 gives a result whose *declared* type is an
#: ActiveX base interface. ``ModelSpace.Item``, ``SelectionSet.Item`` and
#: ``IAcadBlock.Item`` are declared ``IAcadEntity*`` (acax25enu.tlb).
_COM_NARROWED_WRAPPERS = frozenset({"IAcadEntity", "IAcadObject"})


def _com_concrete_entity(entity):
    """The entity through its runtime class when a makepy wrapper narrowed it.

    With a makepy cache for the AutoCAD type library, ``ModelSpace.Item(i)``
    comes back as the gen_py ``IAcadEntity`` class, which answers only the
    entity base's members. Measured on AutoCAD 2026 (2026-09-23): on an
    AcDbLine, ``type(item).__name__ == "IAcadEntity"`` and ``item.StartPoint``
    raises ``AttributeError`` - so every ``entity_list`` row lost its
    type-specific properties (a LINE kept only its bounding box) and the room
    reader saw no wall segments at all. Only that wrapper is re-dispatched:
    ``_com_unnarrow`` walks the object's type info across the process
    boundary, and a concrete wrapper (``HandleToObject`` is declared
    ``IDispatch*``) needs none of it.
    """
    if type(entity).__name__ in _COM_NARROWED_WRAPPERS:
        return _com_unnarrow(entity)
    return entity


def _entity_info(entity) -> EntityInfo:
    """Convert a COM entity object to EntityInfo dataclass."""
    entity = _com_concrete_entity(entity)
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
        elif obj_name in _COM_LWPOLYLINE_NAMES or obj_name == "AcDb2dPolyline":
            coords = list(entity.Coordinates)
            # Measured on AutoCAD 2026 (25.1), backlog W3-T13-09: a heavy 2D
            # polyline's Coordinates are (x, y, z) triples - AddPolyline of three
            # vertices answers nine values - while a lightweight polyline's are
            # (x, y) pairs.
            stride = 3 if obj_name == "AcDb2dPolyline" else 2
            pts = [[coords[i], coords[i + 1]] for i in range(0, len(coords), stride)]
            # ActiveX hands back WCS for every point property on this backend
            # *except* the lightweight polyline: "LightweightPolyline object: the
            # variant is an array of 2D points in OCS." A heavy 2D polyline's
            # frame is not translated: that reading is still unverified live, and
            # the `ocs_normalized` capability reason says so.
            normal = None
            if obj_name in _COM_LWPOLYLINE_NAMES:
                try:
                    normal = tuple(entity.Normal)
                    if not ocs.is_wcs_frame(normal):
                        elevation = float(getattr(entity, "Elevation", 0.0))
                        pts = [ocs.to_wcs_2d(normal, p[0], p[1], elevation) for p in pts]
                    else:
                        normal = None
                except Exception as exc:
                    normal = None
                    log.debug("OCS normalisation of polyline coordinates failed: %s", exc)
            props["points"] = pts
            props["closed"] = bool(entity.Closed)
            props["length"] = entity.Length
            # Per-vertex bulges, parallel to `points` (ezdxf parity). GetBulge
            # is one cross-process call per vertex, so it is only paid when
            # the polyline can carry an arc: an arc is always longer than its
            # chord, so a `Length` equal to the chord walk means every bulge
            # is zero and no call is needed. GetBulge answers in the same OCS
            # as Coordinates, so a translated `points` list needs the bulges
            # translated with it — a mirrored frame reverses the arc's sense.
            bulges = _com_bulges(
                entity, coords, bool(entity.Closed), props["length"], stride=stride
            )
            if normal is not None:
                bulges = [ocs.wcs_bulge(normal, b) for b in bulges]
            props["bulges"] = bulges
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
            # ezdxf parity: a block reference whose Normal is -Z in a FLAT frame
            # is drawn on the mirror image (x -> -x) about its insertion point; a
            # tilted Normal with a negative z is no mirror (W3-T14-19).
            try:
                normal = tuple(float(v) for v in entity.Normal)
            except Exception:
                normal = (0.0, 0.0, 1.0)
            flat = ocs.is_flat_frame(normal)
            props["mirrored"] = flat and normal[2] < 0
            if not flat:
                props["plane_normal"] = ocs.plane_normal(normal)
            geometry_bbox = _com_geometry_bbox(entity)
            if geometry_bbox is not None:
                props["geometry_bbox"] = geometry_bbox
        elif "Dimension" in obj_name:
            # ezdxf parity: where the dimension's text stands. Without it the
            # dim_overlap critique fell back to the bounding-box centre, which
            # for a diameter dimension is the circle's centre - so an outside
            # diameter and its bore, whose texts stand 15 mm apart, read as
            # "0.00 mm apart" (measured on AutoCAD 2026 by
            # scripts/smoke_mech_com.py; TextPosition read live on
            # AcDbDiametricDimension, WCS). ModelSpace.Item hands the entity
            # over narrowed to IAcadEntity, which has no TextPosition - measured
            # AttributeError - so it is read through the concrete class.
            text = _com_unnarrow(entity).TextPosition
            props["text_position"] = [text[0], text[1]]
    except Exception as exc:
        log.debug("Type-specific entity properties extraction failed: %s", exc)

    ent_type = obj_name.replace("AcDb", "").upper()
    try:
        linetype = entity.Linetype
    except Exception as exc:
        log.debug("Linetype read failed, using ByLayer: %s", exc)
        linetype = "ByLayer"
    # The same DXF integers as headless (acLnWtByLayer -1, ByBlock -2,
    # ByLwDefault -3); the typelib spelling, see the note below.
    try:
        weight = entity.Lineweight
        weight_name = lineweight_name(weight) if isinstance(weight, (int, float)) else None
    except Exception as exc:
        log.debug("Lineweight read failed: %s", exc)
        weight_name = None

    # 1.8-05: an entity this job built in a paper-space target names its
    # space; model space (the default) is never announced.
    space = getattr(getattr(_JOB_STATE, "guard", None), "space", None)
    if space is not None:
        try:
            if entity.OwnerID == space[1]:
                props["space"] = space[0]
        except Exception as exc:  # evidence only
            log.debug("OwnerID read failed: %s", exc)

    return EntityInfo(
        handle=entity.Handle,
        type=ent_type,
        layer=entity.Layer,
        color=entity.color,
        linetype=linetype,
        visible=bool(entity.Visible),
        properties=props,
        lineweight_name=weight_name,
    )


#: Two ActiveX members this backend spells the way the type library does and
#: not the way the documentation capitalises them: ``IAcadEntity`` / ``IAcadLayer``
#: declare ``color`` and ``Lineweight`` (acax25enu.tlb). Late-bound dispatch
#: resolves names case-insensitively, so ``.Color`` worked on a seat without a
#: makepy cache; once ``win32com.client.gencache`` has run against AutoCAD
#: (any tool calling ``EnsureDispatch`` leaves it behind, and every object a
#: method returns is then wrapped in the generated class) the lookup is exact
#: and ``.Color`` / ``.LineWeight`` raise ``AttributeError: ... no attribute
#: 'Color'. Did you mean: 'color'?`` — measured 2026-09-22 on AutoCAD 2026 by
#: ``scripts/smoke_settings_com.py`` (``layer_list`` on the reopened template)
#: and on a scratch document's ``AddLine``. The typelib spelling works on both
#: kinds of wrapper; the fakes in tests/ model the same names.


def _layer_info(layer, current_layer_name: str) -> LayerInfo:
    """Convert a COM layer object to LayerInfo."""
    return LayerInfo(
        name=layer.Name,
        color=layer.color,
        linetype=layer.Linetype,
        lineweight=layer.Lineweight,
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
# Commands sent to the command line (1.8-05)
# ---------------------------------------------------------------------------

#: How many created entities a command report lists; ``created_count`` has them all.
COMMAND_CREATED_ROWS = 50

#: The warning a command report carries when it created something outside
#: the bound document's target space.
CREATED_OUTSIDE_TARGET = "created_outside_target_space"


@dataclass
class CommandOutcome:
    """What ``ComBackend._safe_send_command`` saw a completed command do.

    ``handles`` is every entity created, oldest first (the corner ops pick
    their new ARC / LINE from it); ``created`` the first
    ``COMMAND_CREATED_ROWS`` as ``{handle, type, space}``; ``created_count``
    None when no space could be read. ``entities_before`` / ``_after`` are the
    totals over every layout block (model space and each sheet), or the owner
    block's alone when the command was sent with ``owner`` (1.8-23).
    """

    command: str
    target_space: str
    handles: list
    created: list
    created_count: int | None
    last_prompt: str | None
    entities_before: int | None = None
    entities_after: int | None = None

    @property
    def outside_target(self) -> list[str]:
        wanted = self.target_space.lower()
        return [row["handle"] for row in self.created if str(row["space"]).lower() != wanted]

    def report(self) -> dict:
        """The tool-facing half: ``{completed, created, created_count, last_prompt, ...}``."""
        row = {
            "completed": True,
            "created": list(self.created),
            "created_count": self.created_count,
            "last_prompt": self.last_prompt,
            "target_space": self.target_space,
        }
        outside = self.outside_target
        if outside:
            row["warning"] = CREATED_OUTSIDE_TARGET
            row["outside_target"] = outside
        return row


@contextlib.contextmanager
def _target_tab(doc):
    """Run a command-line corner op with the target space's tab active (1.8-05).

    TRIM / EXTEND / FILLET / CHAMFER are commands, and AutoCAD runs a command
    in the active space: with the operator on Layout1 and the target in model
    space, ``(handent ...)`` of a model-space line is "not in current space"
    and nothing is trimmed. The target's tab is activated for the command and
    the operator's tab restored afterwards, also when it fails.
    """
    name = _target_space_name()
    try:
        previous = str(doc.ActiveLayout.Name)
    except Exception as exc:  # no layout members: run where AutoCAD is
        log.debug("ActiveLayout unreadable (%s); running in the active space", exc)
        previous = None
    switch = previous is not None and previous.lower() != name.lower()
    if switch:
        doc.ActiveLayout = doc.Layouts.Item(name)
    try:
        yield
    finally:
        if switch:
            try:
                doc.ActiveLayout = doc.Layouts.Item(previous)
            except Exception as exc:
                log.warning("could not restore the operator's layout tab %r: %s", previous, exc)


def _read_last_prompt(doc) -> str | None:
    """AutoCAD's ``LASTPROMPT``, verbatim (localised), or None when unreadable."""
    try:
        value = doc.GetVariable("LASTPROMPT")
    except Exception as exc:  # evidence only
        log.debug("LASTPROMPT unreadable: %s", exc)
        return None
    return None if value is None else str(value)


def _space_snapshot(doc) -> tuple[int, int] | None:
    """``(entities over every layout block, highest handle among their last items)``.

    New objects take handles above every existing one (AutoCAD hands handles
    out upwards from ``HANDSEED``) and are appended to the end of their block,
    so after a command each block is read backwards only until a handle at or
    below this mark: the cost is the number of new entities, not the drawing.
    None when the layouts cannot be read (a seat without the members).
    """
    try:
        layouts = doc.Layouts
        total = 0
        top = -1
        for index in range(int(layouts.Count)):
            count, last = _block_mark(layouts.Item(index).Block)
            total += count
            top = max(top, last)
        return total, top
    except Exception as exc:  # the report then falls back to the owner block
        log.debug("layout snapshot unavailable: %s", exc)
        return None


def _block_mark(block) -> tuple[int, int]:
    """``(block.Count, the handle value of its last item - or -1 when empty)``.

    Two reads whatever the block holds: the ``Count`` and its last index.
    """
    count = int(block.Count)
    return count, int(str(block.Item(count - 1).Handle), 16) if count else -1


def _block_tail(block, top: int, space: str) -> tuple[list[tuple[int, str, Any, str]], int]:
    """The entities appended to ``block`` with a handle above ``top``, and its ``Count``.

    Read backwards from the last index and stopped at the first handle at or
    below ``top`` (1.8-23): the cost is the number of new entities, whatever
    the block held before. Walking from the end rather than from the old
    ``Count`` keeps an entity the command added after it erased another one
    (the indices shift down). Rows are ``(handle value, handle, entity,
    space)``, oldest first.
    """
    count = int(block.Count)
    rows: list[tuple[int, str, Any, str]] = []
    for position in range(count - 1, -1, -1):
        entity = block.Item(position)
        handle = str(entity.Handle)
        value = int(handle, 16)
        if value <= top:
            break
        rows.append((value, handle, entity, space))
    rows.reverse()
    return rows, count


def _owner_block(doc, space: str) -> tuple[Any, str]:
    """``(block, its space name)`` for ``space`` (``"Model"`` or a paper layout) in
    ``doc``; model space when the layout cannot be read. Never ``ActiveLayout``:
    the target, not the tab the operator has open."""
    if space.lower() == "model":
        return doc.ModelSpace, "Model"
    try:
        return doc.Layouts.Item(space).Block, space
    except Exception as exc:  # the operator deleted the sheet; _msp() falls back alike
        log.debug("layout %r unreadable (%s); reading model space", space, exc)
        return doc.ModelSpace, "Model"


def _created_since(doc, top: int) -> tuple[list[tuple[int, str, Any, str]], int]:
    """Entities with a handle above ``top`` in every layout block, and the new total.

    Rows are ``(handle value, handle, entity, space)``, oldest first. The
    layouts are read again, so a sheet the command itself added is included;
    each block is read only from its end (``_block_tail``).
    """
    layouts = doc.Layouts
    rows: list[tuple[int, str, Any, str]] = []
    total = 0
    for index in range(int(layouts.Count)):
        layout = layouts.Item(index)
        found, count = _block_tail(layout.Block, top, str(layout.Name))
        rows.extend(found)
        total += count
    rows.sort(key=lambda row: row[0])
    return rows, total


def _created_rows(found) -> tuple[list[str], list[dict]]:
    """``(every handle, the first COMMAND_CREATED_ROWS as {handle, type, space})``."""
    handles = [row[1] for row in found]
    created = []
    for _value, handle, entity, space in found[:COMMAND_CREATED_ROWS]:
        try:
            kind = str(entity.ObjectName).replace("AcDb", "").upper()
        except Exception as exc:  # evidence only
            log.debug("ObjectName unreadable for %s: %s", handle, exc)
            kind = None
        created.append({"handle": handle, "type": kind, "space": space})
    return handles, created


# ---------------------------------------------------------------------------
# ComBackend
# ---------------------------------------------------------------------------


#: The ``_mutating`` call running in this task: ``_run`` marks it ``reached``
#: when it hands a job to the COM thread. A task copies the context, so a
#: ``gather``-ed sub-call marks the same holder.
_MUTATION_PROBE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "acadmcp_mutation_probe", default=None
)


def _note_reached() -> None:
    """The running ``_mutating`` call handed work to AutoCAD (see ``_run``)."""
    probe = _MUTATION_PROBE.get()
    if probe is not None:
        probe["reached"] = True


def _dbmod_of(doc) -> int | None:
    """``DBMOD`` of ``doc`` (COM thread); None when it cannot be read.

    0 means the document holds what its file holds - the only state in which
    a lineage record may name the file's sha256 (1.8-04).
    """
    try:
        return int(doc.GetVariable("DBMOD"))
    except Exception:  # noqa: BLE001 - evidence only; no record is written without it
        return None


def _mutating(method):
    """Bump the bound document's content revision after ``method`` (B11).

    Only when the call handed work to AutoCAD - on success and on failure: a
    write that raised may have landed in part, so the revision moves. Never on
    ``ActiveDocumentChangedError``, which is raised before anything reaches
    AutoCAD, and never for a call that returned or raised before its first
    job (an argument refusal, ``transaction_rollback`` with no transaction),
    which is what the headless revision does too. A refusal raised *inside* a
    job still moves it: from outside a job a refusal and a partial write look
    alike, and the safe error is one stale_revision too many. Applied by name
    from ``_MUTATING_METHODS``, never from a tool's cost class: that class
    counts save and deliver as mutations, and ``cad_batch`` steps bypass it.
    """

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        probe = {"reached": False}
        outer = _MUTATION_PROBE.get()
        token = _MUTATION_PROBE.set(probe)
        refused = False
        try:
            return await method(self, *args, **kwargs)
        except (ActiveDocumentChangedError, ApplicationBusyError):
            # Both are raised inside the job before it wrote anything (1.8-02
            # adds the busy seat): the revision stays.
            refused = True
            raise
        finally:
            _MUTATION_PROBE.reset(token)
            if probe["reached"] and outer is not None:
                outer["reached"] = True  # a nested mutating call reached AutoCAD for both
            if probe["reached"] and not refused:
                self._bump_revision()

    wrapper.__acadmcp_mutating__ = True
    return wrapper


class ComBackend(AutoCADBackend):
    """Live AutoCAD control via COM API."""

    #: How long ``_ensure_document_state`` waits out ``RPC_E_CALL_REJECTED``
    #: after a document switch, well inside ``COM_CALL_TIMEOUT`` (60 s).
    _REJECTED_RETRY_BUDGET_S = 20.0
    _REJECTED_RETRY_FIRST_PAUSE_S = 0.25

    #: ``call_outcomes`` keeps this many rows; ``system_status`` reports the last few.
    _MAX_CALL_OUTCOMES = 50
    _STATUS_CALL_OUTCOMES = 10

    def __init__(self):
        self._executor: concurrent.futures.Executor | None = None
        # B01: cancellations recorded by `_run`, newest last; Task 10 reads it.
        self.call_outcomes: collections.deque = collections.deque(maxlen=self._MAX_CALL_OUTCOMES)
        # 1.8-14: every job's queue/guard/work/refresh/counter ms (run_metrics).
        self._run_breakdown = RunBreakdown()
        self._connected = False
        self._transaction_active = False
        self._document_scope_key: tuple[str, str] | None = None
        # v1.7.0 (B11): the bound document and every document this session has
        # bound, keyed by (application, Name, FullName) -> {"document_id",
        # "revision"}; the application is ``_app_identity``, so a restarted
        # AutoCAD's documents are new documents whatever their names.
        self._bound: dict | None = None
        self._known_documents: dict[tuple, dict] = {}
        # B11: how operator edits reach the revision (``operator_edits_status``).
        self._edit_counter: dict = {
            "counted": None,
            "method": "lisp_database_reactor",
            "reason": "no call has touched a document yet",
        }

    @property
    def name(self) -> str:
        return "com"

    @property
    def is_connected(self) -> bool:
        return self._connected

    def document_identity(self) -> dict:
        """The bound document's ``{document_id, revision, name, path, backend}`` (B11).

        ``revision`` counts this server's own content-changing calls on the
        document (``_mutating``: on success and on failure, never on a refusal
        or a save) plus one for every gap between two server calls in which
        the operator's edit counter moved (``_edit_counter_start``; what it
        catches and misses is in backends/document_guard.py). Answered from
        memory: ``refresh_document_identity`` folds in a hand edit made since
        the last call. All None until a document is bound.
        """
        bound = self._bound
        if bound is None:
            return {
                "document_id": None,
                "revision": None,
                "name": None,
                "path": None,
                "backend": "com",
            }
        entry = self._known_documents.get(self._bound_entry_key()) or {}
        return {
            "document_id": bound["document_id"],
            "revision": int(entry.get("revision", 0)),
            "name": bound["name"],
            "path": bound["full_name"] or None,
            "backend": "com",
        }

    async def refresh_document_identity(self) -> None:
        """Fold an operator edit made since the last call into the revision now.

        ``document_identity()`` answers from memory; the edit counter is read
        inside a guarded job. A preview and a precondition check await this
        before they read the revision (engineering/changes/preview.py), so a
        hand edit with no server call after it still moves the revision they
        compare. One job (two ~1 ms counter reads); unbound, or refused because
        another document is active, it changes nothing - the next guarded call
        refuses that switch by itself.
        """
        if self._bound is None or self._executor is None:
            return
        try:
            await self._run(self._check_bound_sync)
        except Exception as exc:  # noqa: BLE001 - an identity read never fails a call
            log.debug("refresh_document_identity: %s", exc)

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
                    True,
                    "native",
                    reason="typed_edges_become_temporary_line_arc_ellipse;append_inner_loop",
                ),
                "handle_overlay": FeatureCapability(
                    False, reason="window_capture_has_no_render_to_label"
                ),
                "measure_area_acis": FeatureCapability(True, "native"),
                "explode_opaque_members": FeatureCapability(
                    True, "native", reason="autocad_explode;ole_and_proxy_members_unverified_live"
                ),
                "ocs_normalized": FeatureCapability(
                    True,
                    "activex_wcs",
                    reason=(
                        "activex_returns_wcs_natively;lwpolyline_coordinates_translated_locally;"
                        "2d_polyline_xyz_stride_measured;2d_polyline_ocs_not_verified_live"
                    ),
                ),
                "ocs_tilted_plane": FeatureCapability(
                    False, reason="2d_xy_cannot_address_a_tilted_plane"
                ),
                "hatch_loops": FeatureCapability(
                    True,
                    "dxf_export",
                    reason="entity_get_reads_one_document_export;block_definition_members_not_read",
                ),
                "table": FeatureCapability(True, "native"),
                "mleader": FeatureCapability(True, "native"),
                "preflight": FeatureCapability(True, "shared"),
                "refiner": FeatureCapability(True, "shared"),
                "delivery": FeatureCapability(True, "shared"),
                "paper_space": FeatureCapability(True, "native"),
                "dwt_write": FeatureCapability(True, "native"),
                "viewport_render": FeatureCapability(True, "native"),
                "solid_3d": _solid_3d_capability(),
                "lisp": FeatureCapability(True, "sanitized"),
                "registry_sysvar": FeatureCapability(True, "native"),
                "dwgprops": FeatureCapability(True, "native"),
                "documents": FeatureCapability(True, "native"),
                # Spelling pinned by F Task 24's test_layer_states_are_declared_as_ours_not_autocads
                # and quoted by the README and CLAUDE.md: mode "xrecord", this reason verbatim.
                "layer_states": FeatureCapability(
                    True,
                    "xrecord",
                    reason="portable_acadmcp_xrecord;not_listed_in_autocad_layer_states_manager",
                ),
                "named_views": FeatureCapability(True, "native"),
                "ucs": FeatureCapability(
                    True, "native", reason="world_restore_via_ucs_command;tool_coordinates_stay_wcs"
                ),
                "live_application": FeatureCapability(True, "native"),
                "preferences": FeatureCapability(True, "native", reason="whitelisted_keys_only"),
                "interactive_prompt": FeatureCapability(
                    True, "native", reason="cancel_returns_cancelled_true;timeout_returns_timed_out"
                ),
                # Track G keys.
                "dwg_write": FeatureCapability(True, "native", reason="document_saveas"),
                "xref_live": FeatureCapability(
                    True, "native", reason="block_reload_bind_detach_on_the_live_seat"
                ),
                # K1 (1.8.0): AutoCAD opens a DWG itself (Documents.Open).
                "dwg_read": FeatureCapability(True, "native"),
                "xlsx_write": (
                    FeatureCapability(True, "openpyxl")
                    if find_spec("openpyxl") is not None
                    else FeatureCapability(False, reason="optional_dependency_missing:openpyxl")
                ),
            },
        )

    async def connect(self) -> None:
        if not _COM_IMPORTS_OK:
            raise RuntimeError("pywin32 not available. Install with: pip install pywin32")
        # Lazy connection: just set up the executor. AutoCAD is connected
        # on the first actual tool call so the server starts even if AutoCAD
        # is not open yet — it will be found as soon as the user opens it.
        self._executor = self._new_executor()
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

    @staticmethod
    def _new_executor() -> concurrent.futures.Executor:
        """The single STA thread every COM call runs on (B01: a daemon thread,
        so a call stuck inside AutoCAD never holds the server process open)."""
        return DaemonExecutor(max_workers=1, name="com-sta", initializer=_com_init)

    async def disconnect(self) -> None:
        executor, self._executor = self._executor, None
        if executor is not None:
            teardown = None
            try:
                teardown = executor.submit(_com_teardown)  # release COM apartment (R5)
            except Exception:
                pass
            executor.shutdown(wait=False)
            if teardown is not None:
                # B01: wait a bounded SHUTDOWN_GRACE for the call ahead of the
                # teardown; a worker still inside AutoCAD after it is logged and
                # left on its daemon thread. `asyncio.wait` never cancels the
                # teardown, so it still runs when that call returns.
                grace = config.settings.shutdown_grace
                waiter = asyncio.wrap_future(teardown)
                if grace > 0:
                    await asyncio.wait([waiter], timeout=grace)
                if not teardown.done():
                    waiter.cancel()
                    log.warning(
                        "disconnect: the COM worker is still inside a call after "
                        "SHUTDOWN_GRACE=%gs; it runs on a daemon thread and will not hold "
                        "the process open",
                        grace,
                    )
        self._document_scope_key = None
        self._bound = None
        self._known_documents.clear()
        self._transaction_active = False
        self._reset_document_state()
        self._connected = False

    def _wait_out_rejected_call(self, read: Callable[[], Any]):
        """``_wait_out_rejected_call`` with this backend's budget (COM thread only)."""
        return _wait_out_rejected_call(
            read,
            budget_s=self._REJECTED_RETRY_BUDGET_S,
            first_pause_s=self._REJECTED_RETRY_FIRST_PAUSE_S,
        )

    # ── the bound document (v1.7.0, B11) ─────────────────────────────────────
    #
    # One document per session: (Name, FullName) plus a uuid4 id, bound by
    # ``_rebind`` after a call that chose a document - drawing_new /
    # drawing_open / document_activate, system_launch(open_path=...), and a
    # document_close of the bound document itself - and by ``_guard`` at the
    # first call that touches a document. Closing some other document, or a
    # system_launch that only attaches, keeps the binding: whatever AutoCAD
    # has active then is the operator's choice, not the call's, so the guard
    # still refuses it (and an open UNDO mark stays rollback-able).
    # backends/document_guard.py says why the operator's own edits are not
    # counted.

    async def _ensure_document_state(self) -> None:
        """The guard as a call of its own: bind on first contact, else refuse a switch.

        Before v1.7.0 this reset drawing-scoped metadata whenever AutoCAD's
        active document had changed - silently following the operator to
        another drawing. A switch is now refused with
        ``ActiveDocumentChangedError``; ``document_activate`` is how a session
        moves. The read waits out ``RPC_E_CALL_REJECTED`` (the window right
        after Documents.Add / Open / Close).
        """
        await self._run(self._check_bound_sync)

    def _check_bound_sync(self) -> None:
        """``_guard`` on the COM thread; returns nothing (a proxy must not leave the STA)."""
        self._guard(_acad_app())

    def _active_key_sync(self, app) -> tuple[Any, tuple[str, str] | None]:
        """``(ActiveDocument, (Name, FullName))``, or ``(None, None)`` with nothing open."""

        def _read():
            if int(app.Documents.Count) == 0:
                return None, None
            doc = app.ActiveDocument
            return doc, (str(doc.Name), str(doc.FullName))

        return self._wait_out_rejected_call(_read)

    def _guard(self, app) -> Any:
        """The bound Document, checked against AutoCAD's active one (COM thread).

        Unbound: the active document is adopted - the first call that touches
        a document binds it. Bound: a different active document, or none, is
        refused with ``ActiveDocumentChangedError`` before anything is written.
        """
        doc, key = self._active_key_sync(app)
        application = _app_identity(app)
        if self._bound is None:
            if key is None:
                raise RuntimeError(
                    "No drawing is open in AutoCAD. Open or create a .dwg file first."
                )
            self._bind_key(key, application)
            self._timed_counter_start(app)
            return doc
        restarted = self._bound["app"] != application
        if restarted or key != (self._bound["name"], self._bound["full_name"]):
            raise ActiveDocumentChangedError(
                self._bound_view(),
                self._view_of(key, application),
                application_changed=restarted,
            )
        self._timed_counter_start(app)
        return doc

    def _timed_counter_start(self, app) -> None:
        """``_edit_counter_start``, its time booked as ``counter``, not ``guard`` (1.8-14)."""
        began = time.perf_counter()
        try:
            self._edit_counter_start(app)
        finally:
            _job_clock_add("counter", (time.perf_counter() - began) * 1000.0)

    # -- operator edits (B11) ---------------------------------------------------

    def operator_edits_status(self) -> dict:
        """Whether the live revision counts the operator's own edits, and how.

        ``{"counted": True, "method": "lisp_database_reactor", "reason": None}``
        once the counter answered; ``counted: False`` with the error when
        ``VL.Application.16`` is missing or failed (the revision then counts
        this server's writes only, as before); ``counted: None`` until a call
        has touched a document.
        """
        return dict(self._edit_counter)

    def _edit_counter_start(self, app) -> None:
        """Fold an edit made outside this server into the revision (COM thread).

        Runs inside the guard, after it found the bound document active - so
        the active namespace is the bound document's. The count is compared
        with the one the previous job left (``_edit_counter_end``): a
        difference is one more revision, however many objects changed. A
        reactor that was watching and is gone (``vlr-remove-all``, a document
        reopened under the same key) is an unwatched gap and counts too; it is
        installed again. The first install sets the baseline and moves nothing.
        What is and is not counted is in backends/document_guard.py. Any
        failure turns the counter off for this attach and leaves the revision
        counting the server's own writes; the next working read counts the
        gap once.
        """
        entry = self._known_documents.get(self._bound_entry_key())
        if entry is None:
            return
        document_id = entry["document_id"]
        try:
            count = self._read_edit_count(app, document_id)
            gap = False
            if count is None:
                gap = "edits" in entry  # it was watching; something removed it
                count = self._install_edit_counter(app, document_id)
        except Exception as exc:  # noqa: BLE001 - the counter is evidence, never a failure
            self._edit_counter = {
                "counted": False,
                "method": "lisp_database_reactor",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            if "edits" in entry:
                entry["edits"] = None  # unknown: the next working read counts the gap
            log.debug("operator-edit counter unavailable: %s", exc)
            return
        if gap or ("edits" in entry and entry["edits"] != count):
            entry["revision"] += 1
            log.info(
                "document %s changed outside this server (edit counter %s -> %s)",
                document_id,
                entry.get("edits"),
                count,
            )
        entry["edits"] = count
        self._edit_counter = {"counted": True, "method": "lisp_database_reactor", "reason": None}

    def _edit_counter_end(self, app) -> None:
        """Remember the count this job leaves, so its own writes are not the operator's.

        COM thread, at the end of a job whose guard passed. Read through the
        cached forms only; a failure or another document's answer keeps the
        previous count, so the next job counts this one's writes as an outside
        edit - one revision too many, never one too few.
        """
        entry = self._known_documents.get(self._bound_entry_key())
        if entry is None or entry.get("edits") is None:
            return
        try:
            count = self._read_edit_count(app, entry["document_id"], refetch=False)
        except Exception as exc:  # noqa: BLE001
            log.debug("operator-edit counter: end-of-job read failed: %s", exc)
            return
        if count is not None:
            entry["edits"] = count

    def _read_edit_count(self, app, document_id: str, *, refetch: bool = True) -> int | None:
        """The bound namespace's count, or None when no reactor of ours answers there."""

        def _once(fresh: bool):
            _reader, evaluator, form = _vl_document_forms(app, document_id, fresh=fresh)
            value = self._wait_out_rejected_call(lambda: _vl_call(evaluator, "funcall", form))
            return _parse_edit_count(value, document_id)

        try:
            count = _once(False)
        except Exception:
            if not refetch:
                raise
            count = None
        if count is None and refetch:
            count = _once(True)  # the cached objects may belong to a closed namespace
        return count

    def _install_edit_counter(self, app, document_id: str) -> int:
        reader, evaluator, _form = _vl_document_forms(app, document_id, fresh=True)
        source = _EDIT_COUNTER_INSTALL.format(document_id=document_id)
        value = self._wait_out_rejected_call(
            lambda: _vl_call(evaluator, "funcall", _vl_call(reader, "funcall", source))
        )
        count = _parse_edit_count(value, document_id)
        if count is None:
            raise RuntimeError(f"the edit counter did not install (answered {value!r})")
        return count

    def _bind_key(self, key: tuple[str, str], application: tuple | None = None) -> bool:
        """Bind ``key`` in ``application``; True when that is another document than before."""
        entry_key = (application, *key)
        entry = self._known_documents.get(entry_key)
        if entry is None:
            entry = {"document_id": uuid.uuid4().hex, "revision": 0}
            self._known_documents[entry_key] = entry
        previous = None if self._bound is None else self._bound["document_id"]
        # 1.8-05: every (re)binding - drawing_new / drawing_open /
        # document_activate, the first contact - starts in model space;
        # only layout_set_current moves the target.
        self._bound = {
            "document_id": entry["document_id"],
            "name": key[0],
            "full_name": key[1],
            "app": application,
            "space": "Model",
        }
        self._document_scope_key = key
        return previous != entry["document_id"]

    def _bound_entry_key(self) -> tuple | None:
        bound = self._bound
        if bound is None:
            return None
        return (bound["app"], bound["name"], bound["full_name"])

    def _bind_active_sync(self) -> bool:
        """Bind whatever AutoCAD now has active (COM thread); True when that changed."""
        app = _acad_app()
        _doc, key = self._active_key_sync(app)
        if key is None:
            changed = self._bound is not None
            self._bound = None
            self._document_scope_key = None
            return changed
        return self._bind_key(key, _app_identity(app))

    async def _rebind(self) -> None:
        """After a call that chose a document: bind the active one, reset state on a change.

        Only for a call that chose the document it leaves active (see the
        section comment above): binding whatever AutoCAD has active after any
        other call would follow the operator's tab switch silently - and drop
        the open transaction of the document the session was bound to.
        """
        changed = await self._run(self._bind_active_sync)
        if changed:
            self._transaction_active = False
            self._reset_document_state()

    def _rekey_bound_sync(self, doc) -> None:
        """Our own SaveAs renamed the bound document: same document, same id (COM thread)."""
        bound = self._bound
        if bound is None:
            return
        old = (bound["name"], bound["full_name"])
        new = self._wait_out_rejected_call(lambda: (str(doc.Name), str(doc.FullName)))
        if new == old:
            return
        application = bound["app"]
        entry = self._known_documents.pop((application, *old), None) or {
            "document_id": bound["document_id"],
            "revision": 0,
        }
        self._known_documents[(application, *new)] = entry
        self._bound = {
            "document_id": entry["document_id"],
            "name": new[0],
            "full_name": new[1],
            "app": application,
            "space": bound.get("space", "Model"),  # a SaveAs keeps the target
        }
        self._document_scope_key = new

    def _reactivate_bound_sync(self, app) -> bool:
        """Make the bound document active again after a job opened and closed another.

        COM thread. True when it had to activate it; False when it already was,
        when nothing is bound, or when the bound document is gone - closed, or
        in an AutoCAD that is no longer the one attached (the next guarded call
        then refuses by name).
        """
        bound = self._bound
        if bound is None or bound["app"] != _app_identity(app):
            return False
        key = (bound["name"], bound["full_name"])

        def _find():
            count = int(app.Documents.Count)
            if count == 0:
                return None
            active = app.ActiveDocument
            if (str(active.Name), str(active.FullName)) == key:
                return None
            for index in range(count):
                doc = app.Documents.Item(index)
                if (str(doc.Name), str(doc.FullName)) == key:
                    return doc
            return None

        doc = self._wait_out_rejected_call(_find)
        if doc is None:
            return False
        doc.Activate()
        self._wait_out_rejected_call(lambda: str(app.ActiveDocument.Name))
        return True

    def _bump_revision(self) -> None:
        """One more content revision on the bound document (see ``_mutating``)."""
        if getattr(self, "_bound", None) is None:
            return
        entry = self._known_documents.get(self._bound_entry_key())
        if entry is not None:
            entry["revision"] += 1

    def _identity_of_key(self, key: tuple[str, str], application: tuple | None = None) -> dict:
        entry = self._known_documents.get((application, *key))
        return {
            "document_id": entry["document_id"] if entry else None,
            "revision": entry["revision"] if entry else None,
        }

    def _bound_view(self) -> dict:
        bound = self._bound
        return {
            "document_id": bound["document_id"],
            "name": bound["name"],
            "path": bound["full_name"] or None,
        }

    def _view_of(
        self, key: tuple[str, str] | None, application: tuple | None = None
    ) -> dict | None:
        if key is None:
            return None
        return {
            "document_id": self._identity_of_key(key, application)["document_id"],
            "name": key[0],
            "path": key[1] or None,
        }

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

        R32 deliberately stops short of the ezdxf document quarantine here. There
        is no ``self._doc`` to replace: the drawing belongs to the operator, lives
        in AutoCAD's process, and AutoCAD's own single-threaded command processor
        arbitrates the abandoned call — not us. The only "reopen" available would
        close the operator's drawing and discard unsaved work the server never
        created. What COM is exposed to is idempotency, so the message says the
        call may still land.

        **The bound document (v1.7.0, B11).** Each job carries a
        ``_JobGuard``: the first ``_acad_doc()`` of the job compares AutoCAD's
        active document with the bound one inside this same job, before any
        work, and refuses with ``ActiveDocumentChangedError`` when the
        operator switched. A call that never asks for the document
        (``Documents.Add`` / ``Open``, ``document_list``, ``system_status``)
        writes into none and is not guarded.
        """
        if self._executor is None:
            raise RuntimeError("ComBackend not connected. Call connect() first.")
        loop = asyncio.get_running_loop()
        timeout = config.settings.com_call_timeout
        started = time.time()
        document = self._document_label()
        guard = _JobGuard(self)
        # B12: a thread pool copies no context, so the operation id travels into
        # the STA job explicitly - code on the COM thread and the call_outcomes
        # rows below see the id the caller runs under.
        operation_id = CURRENT_OPERATION.get()
        submitted = time.perf_counter()

        def _job():
            # B11: the first _acad_doc() of this job checks the bound document,
            # inside the job, before any document work.
            began = time.perf_counter()
            clock = _JOB_STATE.clock = {}
            token = CURRENT_OPERATION.set(operation_id)
            _JOB_STATE.guard = guard
            # 1.8-23: the create paths' _regen() calls are collected here and
            # flushed once below - an Update() or one Regen per job.
            pending = _JOB_STATE.refresh = _Refresh()
            guard.touched = False
            try:
                return func(*args, **kwargs)
            finally:
                # Also after a failure: what a batch wrote before it is drawn.
                _JOB_STATE.refresh = None
                _flush_refresh(pending)
                pending.last = None  # a COM proxy is dropped on the COM thread
                ran = (time.perf_counter() - began) * 1000.0
                ended = time.perf_counter()
                if guard.doc is not None:
                    try:
                        self._edit_counter_end(_acad_app())
                    except Exception as exc:  # noqa: BLE001 - never mask the job's outcome
                        log.debug("operator-edit counter: %s", exc)
                _JOB_STATE.guard = None
                _JOB_STATE.clock = None
                guard.doc = None
                CURRENT_OPERATION.reset(token)
                self._record_breakdown(submitted, began, ended, ran, clock)

        work = self._executor.submit(_job)
        _note_reached()  # B11: from here a `_mutating` call may have written
        future = asyncio.wrap_future(work, loop=loop)
        try:
            if timeout > 0:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        except asyncio.CancelledError:
            # B01: one STA thread, FIFO, so a cancelled call cannot overlap the
            # next one — but whether it ran matters to Task 10's
            # `unknown_commit`. `work.cancel()` is True only for a call no
            # worker ever took (it never runs); False means it is running or done.
            self._record_cancel(func, work, started, document, operation_id)
            raise
        except TimeoutError as e:
            if not future.cancelled():
                # The call raised TimeoutError instead of overrunning the
                # deadline (a dead network share, a re-raised socket timeout —
                # and with timeout<=0 there is no deadline at all). That is an
                # ordinary failure: let it through unchanged rather than
                # blaming AutoCAD and discarding a live STA connection.
                raise
            log.error("COM call timed out after %.1fs; rebuilding executor", timeout)
            # B12: the worker is still inside AutoCAD; whether the call lands
            # is unknown, and the middleware reads this row to say so.
            self.call_outcomes.append(
                {
                    "call": real_call_name(func),
                    "outcome": "abandoned_deadline",
                    "started": started,
                    "ended": None,
                    "document": document,
                    "operation_id": operation_id,
                }
            )
            # The worker thread is still blocked inside SendCommand and cannot be
            # cancelled. Abandon it (shutdown(wait=False)) and start a fresh
            # single-thread executor so the next call doesn't queue behind the
            # stuck one and time out too. Drop the cached COM app for the same
            # reason — the new thread must re-Dispatch in its own apartment.
            stuck = self._executor
            self._executor = self._new_executor()
            if stuck is not None:
                # When the stuck SendCommand eventually unblocks, the queued
                # teardown releases its COM apartment instead of leaking it (R5).
                try:
                    stuck.submit(_com_teardown)
                except Exception:
                    pass
                stuck.shutdown(wait=False)
            _drop_app()
            self._connected = False
            raise RuntimeError(
                f"AutoCAD did not respond within {timeout:.0f}s. The application "
                "may be showing a modal dialog or have an active command prompt "
                "(press ESC in AutoCAD). The abandoned call may still complete in "
                "AutoCAD after this error - the worker is blocked inside an "
                "uncancellable COM call, not stopped - so verify the drawing before "
                "retrying: a retry can double-apply the operation. Increase "
                "COM_CALL_TIMEOUT if a long operation is expected."
            ) from e
        except _COM_ERROR as e:
            hr = e.args[0] if e.args else 0
            if hr in (-2147221246, -2147221005, -2147417842):
                _drop_app()
                self._connected = False
            raise RuntimeError(
                f"AutoCAD COM error ({hr:#010x}): {e.args[1] if len(e.args) > 1 else e}"
            ) from e

    def _record_breakdown(
        self, submitted: float, began: float, ended: float, ran_ms: float, clock: dict
    ) -> None:
        """Book one job's milliseconds (1.8-14; COM thread, never raises).

        queue: submitted -> started on the STA thread. guard: the bound-document
        check without the operator-edit counter inside it. refresh: ``_regen``.
        counter: the edit counter at the guard and at the end of the job. work:
        the rest of the job's own code.
        """
        try:
            closing = (time.perf_counter() - ended) * 1000.0
            guard = clock.get("guard", 0.0)
            refresh = clock.get("refresh", 0.0)
            counter = clock.get("counter", 0.0)
            self._run_breakdown.record(
                queue=(began - submitted) * 1000.0,
                guard=guard,
                work=ran_ms - guard - refresh - counter,
                refresh=refresh,
                counter=counter + closing,
            )
        except Exception as exc:  # noqa: BLE001 - a timing is evidence, never a failure
            log.debug("run breakdown: %s", exc)

    def run_metrics(self) -> dict:
        """Per-job queue/guard/work/refresh/counter ms (``system_status(metrics=True)``)."""
        return self._run_breakdown.snapshot()

    def _document_label(self) -> str | None:
        """The bound document's FullName (or Name), captured when a call starts."""
        key = self._document_scope_key
        if key is None:
            return None
        name, full_name = key
        return full_name or name

    def _record_cancel(
        self,
        func,
        work,
        started: float,
        document: str | None,
        operation_id: str | None = None,
    ) -> None:
        queued = work.cancel()
        row = {
            "call": real_call_name(func),
            "outcome": "cancelled_queued" if queued else "cancelled_running",
            "started": started,
            "ended": time.time() if queued else None,
            "document": document,
            "operation_id": operation_id,
        }
        self.call_outcomes.append(row)
        if not queued:
            # Runs on the worker thread when the call returns (at once if it
            # already has); a dict store is atomic under the GIL.
            work.add_done_callback(lambda _work: row.__setitem__("ended", time.time()))

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
            # INSUNITS is read on the document (the Application has no
            # GetVariable). ``Unknown`` is the honest default for an optional
            # read the document refuses, or a code the map does not carry.
            try:
                units = unit_map.get(int(doc.GetVariable("INSUNITS")), "Unknown")
            except Exception as exc:
                log.debug("doc.GetVariable(INSUNITS) failed, reporting Unknown: %s", exc)
                units = "Unknown"

            identity = self.document_identity()
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
                document_id=identity["document_id"],
                revision=identity["revision"],
            )

        return await self._run(_sync)

    async def drawing_new(self, template: str | None = None) -> dict:
        """``Documents.Add()``, or ``Documents.Add(<dwt>)`` from a template file.

        A template that is not already a ``.dwt`` (the bundled ``.dxf`` files,
        an explicit ``.dxf`` path) is first converted into one through AutoCAD
        itself — see ``_template_as_dwt`` for the measurement that makes this
        necessary — and the result reports ``template_dwt: {path, cached}``.
        A template path that is not a file is refused here rather than handed
        to ``Add``, which would silently create the default drawing instead.
        """

        def _sync():
            app = _acad_app()
            if not template:
                doc = app.Documents.Add()
                return {"ok": True, "name": self._wait_out_rejected_call(lambda: doc.Name)}
            source = Path(template)
            if not source.is_file():
                raise FileNotFoundError(
                    f"template file not found: {template} (Documents.Add would silently "
                    "create the default drawing instead)"
                )
            result: dict[str, Any] = {"ok": True}
            if source.suffix.lower() == ".dwt":
                dwt = source
            else:
                dwt, cached = _template_as_dwt(app, source)
                result["template_dwt"] = {"path": str(dwt), "cached": cached}
            doc = app.Documents.Add(str(dwt))
            result["name"] = self._wait_out_rejected_call(lambda: doc.Name)
            return result

        result = await self._run(_sync)
        await self._rebind()
        return result

    # ── lineage and recovery on the live engine (1.8-04) ─────────────────────
    #
    # The bind keeps its own uuid (B11): a reopened DWG is a new document here.
    # The lineage record (backends/lineage.py) is keyed by FullName and the
    # file's sha256, written only while DBMOD is 0 - the file then holds what
    # the document holds - so a drawing_open of a file someone changed since
    # reports external_change. SaveAs stays AutoCAD's own save protocol.

    def _bound_entry(self) -> dict | None:
        key = self._bound_entry_key()
        return None if key is None else self._known_documents.get(key)

    @staticmethod
    def _same_path(a: str, b: str) -> bool:
        try:
            return os.path.samefile(a, b)
        except OSError:
            return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def _refuse_recovered_source(self, op: str, path: str | None, overwrite_source: bool) -> None:
        entry = self._bound_entry() or {}
        source = entry.get("recovered_from")
        if source and path and not overwrite_source and self._same_path(path, source):
            name = (self._bound or {}).get("name") or Path(source).name
            raise recovered_source_refusal(op, name, source)

    async def _record_lineage(self, full_name: str | None, dbmod: int | None) -> dict | None:
        """Write the bound document's lineage record for ``full_name`` while DBMOD is 0."""
        entry = self._bound_entry()
        if entry is None or not full_name or not os.path.isfile(full_name):
            return None
        sha = await asyncio.to_thread(file_sha256, full_name)
        lineage_id = entry.setdefault("lineage_id", lineage.new_lineage_id())
        if dbmod == 0 and sha:
            lineage.write_record(
                full_name,
                {
                    "lineage_id": lineage_id,
                    "document_id": self.document_identity()["document_id"],
                    "last_saved_sha256": sha,
                    "revision": int(entry.get("revision", 0)),
                    "engine": "com",
                },
            )
        return {"lineage_id": lineage_id, "sha256": sha}

    async def drawing_open(self, path: str, recover: bool = False) -> dict:
        """``Documents.Open``; ``recover=True`` repairs a damaged DXF first (B09).

        The repair is ezdxf's recover reader, written into a private temp DXF
        that AutoCAD then opens: the document's FullName is that copy, and a
        save onto the damaged source needs ``overwrite_source=True``.
        """
        recovered = None
        open_path = path
        save_recovery: list[dict] = []
        if recover:
            import tempfile

            repaired, recovered = await asyncio.to_thread(recover_dxf, path)
            folder = tempfile.mkdtemp(prefix="acadmcp-recover-")
            open_path = os.path.join(folder, f"{Path(path).stem}.recovered.dxf")
            await asyncio.to_thread(atomic_write_dxf, repaired, open_path, state=None)
        else:
            save_recovery = await asyncio.to_thread(resolve_pending, path)

        def _sync():
            app = _acad_app()
            doc = app.Documents.Open(open_path)
            name, full_name = self._wait_out_rejected_call(lambda: (doc.Name, doc.FullName))
            return {"ok": True, "name": name, "path": full_name}, _dbmod_of(doc)

        result, dbmod = await self._run(_sync)
        await self._rebind()
        entry = self._bound_entry()
        if recover:
            line = {"lineage_id": lineage.new_lineage_id(), "recovered_from": path}
            if entry is not None:
                entry["lineage_id"] = line["lineage_id"]
                entry["recovered_from"] = path
            result.update(recovered=recovered, recovered_from=path, lineage=line)
            result["external_change"] = False
        else:
            full_name = result["path"]
            record = lineage.read_record(full_name) if full_name else None
            if entry is not None and record is not None:
                entry["lineage_id"] = record["lineage_id"]
            written = await self._record_lineage(full_name, dbmod)
            sha = (written or {}).get("sha256")
            recorded = (record or {}).get("last_saved_sha256")
            changed = bool(sha and recorded and sha != recorded)
            result["external_change"] = changed
            if written is not None:
                result["lineage"] = {
                    "lineage_id": written["lineage_id"],
                    "continued": record is not None,
                }
                result["sha256"] = sha
            if changed:
                result["previous_sha256"] = recorded
        identity = self.document_identity()
        result["document_id"], result["revision"] = identity["document_id"], identity["revision"]
        if save_recovery:
            result["save_recovery"] = save_recovery
        return result

    async def drawing_save(self, path: str | None = None, overwrite_source: bool = False) -> dict:
        self._refuse_recovered_source("drawing_save", path, overwrite_source)
        source = (self._bound_entry() or {}).get("recovered_from")

        def _sync():
            doc = _acad_doc()
            if path:
                doc.SaveAs(path)
            else:
                doc.Save()
            self._rekey_bound_sync(doc)
            return {"ok": True, "path": doc.FullName}, _dbmod_of(doc)

        result, dbmod = await self._run(_sync)
        entry = self._bound_entry()
        if source and path and entry is not None and self._same_path(path, source):
            entry.pop("recovered_from", None)  # the caller chose to replace the damaged source
        await self._record_lineage(result["path"], dbmod)
        return result

    async def drawing_save_as(self, path: str, fmt: str = "dwg") -> dict:
        self._refuse_recovered_source("drawing_save_as", path, False)

        def _sync():
            doc = _acad_doc()
            # AutoCAD AcSaveAsType: 12 = ac2000_dwg, 61 = ac2013_dxf, 66 = ac2018_Template.
            # "dwt" was 5 (acR13_dxf) until v1.6: a DWT request wrote an R13 DXF
            # under a .dwt name.
            fmt_map = {"dwg": 12, "dxf": 61, "dwt": 66}
            acad_fmt = fmt_map.get(fmt.lower(), 12)
            doc.SaveAs(path, acad_fmt)
            self._rekey_bound_sync(doc)
            return {"ok": True, "path": path, "format": fmt}, _dbmod_of(doc)

        result, dbmod = await self._run(_sync)
        await self._record_lineage(path, dbmod)
        return result

    async def drawing_export_dxf(self, path: str) -> dict:
        """A DXF copy through ``Document.Export``; the open document is not re-pointed.

        ``SaveAs(path, 61)`` - what this method did until 1.7.0 - makes the
        open document the DXF. ``Export`` does not. Measured on AutoCAD 2026
        (25.1s) on 2026-10-24 in a scratch document: ``Export(<base>, "DXF",
        <selection set of all>)`` writes ``<base>.DXF`` (upper-case extension,
        ``$ACADVER`` AC1032 = R2018) and leaves ``Name``, ``FullName`` and
        ``Saved`` as they were; ``".dxf"`` as the extension argument is
        refused. The file is written into a private folder beside the target
        and renamed onto it, so the name is the caller's and a file already
        there is replaced in one step - AutoCAD is never asked about it. The
        version reported is the one read back from the file's header.
        """
        target = Path(path)
        if target.suffix.lower() != ".dxf":
            raise ValueError(
                f"drawing_export_dxf writes DXF; name the file .dxf, got {target.name!r}"
            )

        def _sync():
            import shutil
            import tempfile

            from engineering.understand.snapshot import dxf_version_of

            doc = _acad_doc()  # inside a job: the bound document, guarded (Task 9)
            name, full_name = str(doc.Name), str(doc.FullName)
            folder = tempfile.mkdtemp(prefix=".acadmcp_export_", dir=str(target.parent))
            try:
                selection = doc.SelectionSets.Add(f"_EXP_{uuid.uuid4().hex[:8]}")
                try:
                    selection.Select(_AC_SELECTION_SET_ALL)
                    # "DXF", never ".dxf" (measured: "Invalid argument").
                    doc.Export(os.path.join(folder, "export"), "DXF", selection)
                finally:
                    try:
                        selection.Delete()
                    except Exception:  # a selection set left behind is harmless; the export is not
                        pass
                written = sorted(Path(folder).glob("export.*"))
                if not written:
                    raise RuntimeError(
                        f"AutoCAD reported the export of {name} done but wrote no file"
                    )
                os.replace(written[0], target)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            return {
                "ok": True,
                "path": str(target),
                "format": "dxf",
                "method": "Document.Export",
                "dxf_version": dxf_version_of(target),
                "document": {"name": name, "full_name": full_name},
                "repointed": False,
            }

        return await self._run(_sync)

    # ── external references, rasters and DWG (v1.6 track G) ──────────────────
    #
    # Measured on AutoCAD 2026 (AutoCAD.Application 25.1s), from the live
    # seat's type library:
    #   ModelSpace.AttachExternalReference(PathName, Name, InsertionPoint,
    #       Xscale, Yscale, Zscale, Rotation, bOverlay, Password)
    #   ModelSpace.AddRaster(imageFileName, InsertionPoint, ScaleFactor,
    #       RotationAngle)
    #   Block: IsXRef, Path, Name, Reload(), Unload(), Bind(bPrefixName),
    #       Detach(), XRefDatabase
    #   Document.SaveAs(FullFileName, SaveAsType, vSecurityParams)
    # ActiveX angles are radians; the tool boundary is degrees, so every
    # rotation crosses here and nowhere else.

    #: AcSaveAsType, read off the same type library.
    _DWG_SAVE_AS = {
        "R2000": 12,
        "R2004": 24,
        "R2007": 36,
        "R2010": 48,
        "R2013": 60,
        "R2018": 64,
    }

    async def xref_attach(self, path, at, scale=1.0, rotation=0.0, kind="attach") -> dict:
        from backends.contracts.refs import XREF_KINDS

        if kind not in XREF_KINDS:
            raise ValueError(f"xref kind must be one of {XREF_KINDS}, got {kind!r}")
        source = Path(path)
        if not source.is_file():
            raise ValueError(f"xref_attach: {path} does not exist")
        x, y = float(at[0]), float(at[1])
        for label, value in (("at.x", x), ("at.y", y), ("scale", scale), ("rotation", rotation)):
            if not math.isfinite(float(value)):
                raise ValueError(f"xref_attach: {label} must be finite, got {value!r}")
        if float(scale) <= 0:
            raise ValueError(f"xref_attach: scale must be positive, got {scale!r}")
        name = source.stem.upper()
        factor = float(scale)
        radians = math.radians(float(rotation))

        def _sync():
            doc = _acad_doc()
            # The contract and the tool docstring both promise this refusal,
            # and the ezdxf engine has always had it (`if name in doc.blocks`).
            # Without it a second attach under a name the drawing already holds
            # either re-points the existing definition or fails inside AutoCAD,
            # while the payload still claims the new `path` was attached.
            # Block names are case-insensitive in AutoCAD, so the comparison is.
            if name.lower() in _block_names(doc):
                raise ValueError(
                    f"xref_attach: the drawing already holds a block named {name!r}; "
                    "detach it first with xref_manage(action='detach')"
                )
            reference = doc.ModelSpace.AttachExternalReference(
                str(source),
                name,
                _apoint(x, y),
                factor,
                factor,
                factor,
                radians,
                kind == "overlay",
            )
            return {
                "ok": True,
                "name": name,
                "path": str(source),
                "kind": kind,
                "handle": reference.Handle,
                "at": [x, y],
                "scale": factor,
                "rotation": float(rotation),
                "backend": self.name,
            }

        return await self._run(_sync)

    async def xref_manage(self, name, action, new_path=None) -> dict:
        from backends.contracts.refs import XREF_ACTIONS

        if action not in XREF_ACTIONS:
            raise ValueError(f"xref action must be one of {XREF_ACTIONS}, got {action!r}")
        if action == "path" and not (new_path or "").strip():
            raise ValueError("xref_manage(action='path') needs new_path")

        def _sync():
            doc = _acad_doc()

            def _xref_blocks():
                found = []
                for index in range(doc.Blocks.Count):
                    block = doc.Blocks.Item(index)
                    try:
                        if block.IsXRef:
                            found.append(block)
                    except Exception:  # noqa: BLE001 - a block that cannot answer is not an xref
                        continue
                return found

            if action == "list":
                # `kind` is null, not "attach". Measured from the seat's own
                # registered type library (acax25*.tlb, "AutoCAD 2025 Type
                # Library", shipped with AutoCAD 2026): IAcadBlock's xref
                # members are exactly IsXRef, Path, Name, Reload, Unload,
                # Bind, Detach, XRefDatabase -- there is no overlay indicator
                # anywhere on the interface. An overlay (which xref_attach
                # itself creates, kind='overlay' -> bOverlay=True) is
                # therefore not knowable here, and a constant "attach" would
                # read to the caller as measured. Same honesty as `inserts`.
                rows = [
                    {"name": b.Name, "path": str(b.Path or ""), "kind": None, "inserts": None}
                    for b in _xref_blocks()
                ]
                rows.sort(key=lambda row: row["name"])
                return {"ok": True, "xrefs": rows, "backend": self.name}

            key = str(name).strip()
            block = next((b for b in _xref_blocks() if b.Name == key), None)
            if block is None:
                raise ValueError(
                    f"xref_manage: {key!r} is not an external reference in this drawing"
                )
            if action == "reload":
                block.Reload()
                return {"ok": True, "name": key, "action": action, "backend": self.name}
            if action == "bind":
                block.Bind(False)
                return {"ok": True, "name": key, "action": action, "backend": self.name}
            if action == "detach":
                block.Detach()
                return {"ok": True, "name": key, "inserts_removed": None, "backend": self.name}
            block.Path = str(new_path)
            return {"ok": True, "name": key, "path": str(new_path), "backend": self.name}

        return await self._run(_sync)

    async def image_attach(self, path, at, scale=1.0, rotation=0.0) -> dict:
        source = Path(path)
        if not source.is_file():
            raise ValueError(f"image_attach: {path} does not exist")
        x, y = float(at[0]), float(at[1])
        for label, value in (("at.x", x), ("at.y", y), ("scale", scale), ("rotation", rotation)):
            if not math.isfinite(float(value)):
                raise ValueError(f"image_attach: {label} must be finite, got {value!r}")
        if float(scale) <= 0:
            raise ValueError(f"image_attach: scale must be positive, got {scale!r}")
        px, py = _image_pixels(str(source))
        factor = float(scale)
        radians = math.radians(float(rotation))

        def _sync():
            doc = _acad_doc()
            raster = doc.ModelSpace.AddRaster(str(source), _apoint(x, y), factor, radians)
            return {
                "ok": True,
                "path": str(source),
                "handle": raster.Handle,
                "at": [x, y],
                "scale": factor,
                "rotation": float(rotation),
                "pixels": [px, py],
                "size_mm": [px * factor, py * factor],
                "backend": self.name,
            }

        return await self._run(_sync)

    async def drawing_export_dwg(self, path, version="R2018") -> dict:
        from backends.contracts.refs import DWG_VERSIONS

        if version not in DWG_VERSIONS:
            raise ValueError(f"DWG version must be one of {DWG_VERSIONS}, got {version!r}")
        save_as_type = self._DWG_SAVE_AS[version]

        def _sync():
            doc = _acad_doc()
            doc.SaveAs(path, save_as_type)
            self._rekey_bound_sync(doc)
            target = Path(path)
            return {
                "ok": True,
                "path": path,
                "version": version,
                "bytes": target.stat().st_size if target.exists() else None,
                "backend": self.name,
            }

        return await self._run(_sync)

    async def drawing_export_pdf(self, path: str, layout: str | None = None) -> dict:
        """Plot one layout to PDF through ``Plot.PlotToFile`` and confirm the file.

        ``layout=None`` means model space, as the tool advertises — the Model
        tab is made current for the plot (and the previous tab restored), so
        the row is never silently the sheet that happened to be active.

        Measured on AutoCAD 2026 with the operator's default BACKGROUNDPLOT=2:
        ``PlotToFile`` returns True immediately, the job is queued to the
        background plotter, and no file exists when the call comes back —
        seconds later the folder was still empty, and a foreground plot
        attempted after such a stranded job raised E_FAIL. So the plot is
        forced to the foreground (``BACKGROUNDPLOT=0``, restored afterwards),
        the method's boolean is honoured, and ``ok`` is reported only once the
        file is on disk with a size.
        """

        def _sync():
            doc = _acad_doc()
            target = layout.strip() if layout and layout.strip() else "Model"
            previous = doc.ActiveLayout.Name
            switched = False
            if previous.lower() != target.lower():
                doc.ActiveLayout = doc.Layouts.Item(target)
                switched = True
            old_background = None
            try:
                try:
                    old_background = int(doc.GetVariable("BACKGROUNDPLOT"))
                except Exception as exc:
                    log.debug("BACKGROUNDPLOT unreadable (%s); plotting as configured", exc)
                if old_background not in (None, 0):
                    doc.SetVariable("BACKGROUNDPLOT", 0)
                plotted = doc.Plot.PlotToFile(path, "DWG To PDF.pc3")
                written = Path(path)
                if not plotted:
                    return {
                        "ok": False,
                        "path": path,
                        "layout": target,
                        "error": "PlotToFile returned False: AutoCAD refused the plot "
                        "(check the layout's plot area and the 'DWG To PDF.pc3' device)",
                    }
                if not written.is_file():
                    return {
                        "ok": False,
                        "path": path,
                        "layout": target,
                        "error": "PlotToFile reported success but wrote no file at "
                        f"{path} (BACKGROUNDPLOT was {old_background})",
                    }
                return {
                    "ok": True,
                    "path": path,
                    "layout": target,
                    "bytes": written.stat().st_size,
                }
            finally:
                if old_background not in (None, 0):
                    try:
                        doc.SetVariable("BACKGROUNDPLOT", old_background)
                    except Exception as exc:
                        log.warning("could not restore BACKGROUNDPLOT=%s: %s", old_background, exc)
                if switched:
                    try:
                        doc.ActiveLayout = doc.Layouts.Item(previous)
                    except Exception as exc:
                        log.warning("could not restore the active layout %r: %s", previous, exc)

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
        """Make ``name`` the server's target space and activate its tab (1.8-05).

        The target - where ``entity_create_*`` and every other ``_msp()``
        writer puts geometry - is kept on the bound document, so the operator
        clicking another tab afterwards does not move it; the tab is still
        activated so the operator sees where the session draws. Resolved
        case-insensitively to the layout's own spelling, as AutoCAD does.
        """

        def _sync():
            doc = _acad_doc()
            resolved = self._find_layout(doc, name)
            if resolved is None:
                return {"ok": False, "error": f"Layout not found: {name}"}
            doc.ActiveLayout = doc.Layouts.Item(resolved)
            self._set_target_space(resolved)
            return {"ok": True, "current": resolved}

        return await self._run(_sync)

    def _set_target_space(self, name: str) -> None:
        """Point the bound document's target at ``name`` (COM thread, after the guard)."""
        if self._bound is not None:
            self._bound["space"] = "Model" if name.lower() == "model" else name

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
            if self._bound is not None and self._bound.get("space") == target:
                self._set_target_space("Model")  # 1.8-05: the target went with it
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
            if self._bound is not None and self._bound.get("space") == old:
                self._set_target_space(new)  # 1.8-05: the target follows its sheet
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

    # ---------------------------------------------------------------------------
    # ── environment (track E) ───────────────────────────────────────────────────
    # ---------------------------------------------------------------------------

    @staticmethod
    def _com_documents(app) -> list:
        return [app.Documents.Item(i) for i in range(int(app.Documents.Count))]

    @staticmethod
    def _com_document_row(doc, active) -> dict:
        full_name = str(doc.FullName)
        return {
            "name": str(doc.Name),
            "path": full_name or None,
            "active": active is not None
            and (str(doc.Name), full_name) == (str(active.Name), str(active.FullName)),
            "saved": bool(doc.Saved),
            "entity_count": int(doc.ModelSpace.Count),
        }

    def _com_find_document(self, app, name_or_path: str):
        """Match a key the way ``_resolve_document_key`` does: full path, then name."""
        if not isinstance(name_or_path, str):
            raise TypeError(f"name_or_path must be a string, got {type(name_or_path).__name__}")
        wanted = name_or_path.strip()
        if not wanted:
            raise ValueError("name_or_path must not be empty")
        docs = self._com_documents(app)
        try:
            as_path = str(Path(wanted).resolve()).lower()
        except (OSError, ValueError):
            as_path = wanted.lower()
        by_path = [
            d
            for d in docs
            if str(d.FullName) and str(Path(str(d.FullName)).resolve()).lower() == as_path
        ]
        by_name = [d for d in docs if str(d.Name).lower() == wanted.lower()]
        hits = by_path or by_name
        if len(hits) == 1:
            return hits[0]
        names = [str(d.Name) for d in docs]
        if len(hits) > 1:
            raise ValueError(f"document {wanted!r} is ambiguous: {names}; pass the full path")
        raise ValueError(f"no open document named {wanted!r}; open documents: {names}")

    async def document_list(self) -> list[dict]:
        def _sync():
            app = _acad_app()
            active = app.ActiveDocument if int(app.Documents.Count) else None
            return [
                {
                    **self._com_document_row(doc, active),
                    **self._identity_of_key((str(doc.Name), str(doc.FullName))),
                }
                for doc in self._com_documents(app)
            ]

        return await self._run(_sync)

    async def document_activate(self, name_or_path: str) -> dict:
        def _sync():
            app = _acad_app()
            previous = str(app.ActiveDocument.Name) if int(app.Documents.Count) else None
            doc = self._com_find_document(app, name_or_path)
            doc.Activate()
            return {
                "ok": True,
                "active": str(doc.Name),
                "previous": previous,
                "name": str(doc.Name),
                "path": str(doc.FullName) or None,
            }

        result = await self._run(_sync)
        await self._rebind()
        return result

    async def document_close(
        self, name_or_path: str | None = None, save: bool = False, discard: bool = False
    ) -> dict:
        if save and discard:
            raise ValueError("document_close: save and discard are mutually exclusive")

        def _sync():
            app = _acad_app()
            doc = (
                _acad_doc() if name_or_path is None else self._com_find_document(app, name_or_path)
            )
            name = str(doc.Name)
            path = str(doc.FullName) or None
            bound = self._bound
            closes_bound = (
                bound is not None
                and bound["app"] == _app_identity(app)
                and (name, str(doc.FullName)) == (bound["name"], bound["full_name"])
            )
            dirty = not bool(doc.Saved)
            if dirty:
                if save:
                    if not path:
                        raise ValueError(
                            f"document_close: {name!r} has never been saved; Close(True) would "
                            "open AutoCAD's Save dialog and block the COM thread. "
                            "drawing_save_as(path) first, or pass discard=True"
                        )
                elif not discard:
                    where = f" to {path}" if path else " (after drawing_save_as)"
                    raise ValueError(
                        f"document_close: {name!r} has unsaved changes; pass save=True to "
                        f"write them{where}, or discard=True to drop them"
                    )
            # A clean or untitled document is never asked to save: Close(True)
            # on a clean file is a no-op write, on an untitled one a dialog.
            doc.Close(bool(save and dirty and path))

            def _read_after_close():
                # AutoCAD still rejects calls for a moment after Close
                # (see _RPC_E_CALL_REJECTED); this read changes nothing.
                count = int(app.Documents.Count)
                return count, (str(app.ActiveDocument.Name) if count else None)

            count, active = self._wait_out_rejected_call(_read_after_close)
            return closes_bound, {
                "ok": True,
                "closed": name,
                "path": path,
                "saved": bool(save and dirty and path),
                "discarded_changes": bool(dirty and not save),
                "active": active,
                "open_documents": count,
                "backend": "com",
            }

        closes_bound, result = await self._run(_sync)
        # B11: only closing the bound document moves the session. Closing some
        # other one keeps the binding, so a tab the operator switched to is
        # still refused by the guard rather than adopted here.
        if closes_bound:
            await self._rebind()
        return result

    # ── layer states ──────────────────────────────────────────────────────────

    @staticmethod
    def _com_layer_states(doc, *, create: bool):
        """The ``ACADMCP_LAYERSTATES`` dictionary, or None when absent.

        ``Dictionaries.Item`` is declared ``IAcadObject*`` (see
        ``_com_unnarrow``): with a makepy cache the wrapper has no ``Count``,
        ``Item`` or ``AddXRecord`` — measured 2026-09-22 on AutoCAD 2026 by
        ``layer_state_restore`` in ``scripts/smoke_settings_com.py`` (the save
        before it had gone through ``Dictionaries.Add``, which is typed, so
        only the reopen paths failed). Every object the dictionary hands out
        goes through ``_com_unnarrow`` for the same reason.
        """
        from engineering.environment.layer_states import DICT_NAME

        try:
            return _com_unnarrow(doc.Dictionaries.Item(DICT_NAME))
        except Exception as exc:  # absent
            log.debug("Dictionaries.Item(%s): %s", DICT_NAME, exc)
        if not create:
            return None
        return _com_unnarrow(doc.Dictionaries.Add(DICT_NAME))

    @staticmethod
    def _com_dict_entries(states) -> list[tuple[str, Any]]:
        if states is None:
            return []
        out = []
        for index in range(int(states.Count)):
            obj = _com_unnarrow(states.Item(index))  # IAcadDictionary.Item is IAcadObject* too
            out.append((str(states.GetName(obj)), obj))
        return out

    def _com_layer_state_entry(self, states, name: str):
        for key, obj in self._com_dict_entries(states):
            if key.lower() == name.lower():
                return key, obj
        return None, None

    @staticmethod
    def _com_xrecord_chunks(xrecord) -> list[str]:
        # pywin32 returns the two [out] parameters as a tuple, exactly as
        # GetBoundingBox() / GetXData() do elsewhere in this file.
        codes, values = xrecord.GetXRecordData()
        return [
            str(value)
            for code, value in zip(list(codes or []), list(values or []), strict=False)
            if int(code) == 1000
        ]

    @staticmethod
    def _com_layer_snapshot(doc, description):
        from engineering.environment.layer_states import snapshot_from_layers

        current = str(doc.ActiveLayer.Name)
        layers, plot = [], {}
        for index in range(int(doc.Layers.Count)):
            lyr = doc.Layers.Item(index)
            layers.append(_layer_info(lyr, current))
            plot[str(lyr.Name)] = bool(lyr.Plottable)
        return snapshot_from_layers(layers, current, plot=plot, description=description)

    async def layer_state_save(self, name, description=None) -> dict:
        from engineering.environment.layer_states import encode_state, validate_state_name

        clean = validate_state_name(name)
        if description is not None and not isinstance(description, str):
            raise TypeError("layer_state_save: description must be a string")

        def _sync():
            doc = _acad_doc()
            state = self._com_layer_snapshot(doc, description)
            chunks = encode_state(state)
            states = self._com_layer_states(doc, create=True)
            key, xrecord = self._com_layer_state_entry(states, clean)
            replaced = xrecord is not None
            if xrecord is None:
                xrecord = states.AddXRecord(clean)
                key = clean
            xrecord.SetXRecordData(_ai([1000] * len(chunks)), _avar(chunks))
            return {
                "ok": True,
                "name": key,
                "layer_count": len(state["layers"]),
                "replaced": replaced,
                "chunks": len(chunks),
                "backend": "com",
            }

        return await self._run(_sync)

    async def layer_state_restore(self, name, properties=None) -> dict:
        from engineering.environment.layer_states import (
            decode_state,
            diff_snapshot,
            validate_properties,
            validate_state_name,
        )

        clean = validate_state_name(name)
        props = validate_properties(properties)

        def _sync():
            doc = _acad_doc()
            states = self._com_layer_states(doc, create=False)
            key, xrecord = self._com_layer_state_entry(states, clean)
            if xrecord is None:
                raise ValueError(
                    f"layer_state_restore: no layer state named {clean!r}; "
                    f"saved states: {[k for k, _ in self._com_dict_entries(states)]}"
                )
            state = decode_state(self._com_xrecord_chunks(xrecord))
            current_name = str(doc.ActiveLayer.Name)
            layers = [
                _layer_info(doc.Layers.Item(i), current_name) for i in range(int(doc.Layers.Count))
            ]
            missing, new = diff_snapshot(state, layers)
            current = None
            warnings: list[str] = []
            # Current first: AutoCAD refuses to freeze the active layer, so the
            # active one must already be the state's before the loop freezes.
            # And it refuses to make a *frozen* layer current (measured, AutoCAD
            # 2026: ``doc.ActiveLayer = <frozen>`` raises 'Error setting active
            # layer'), so when the state thaws its own current layer that thaw
            # comes before the ActiveLayer write instead of after it in the
            # loop. Only a requested property is touched: without "frozen" the
            # refusal stands and lands in ``warnings`` like every other one.
            # ``Freeze`` is written only when it changes the layer: layer 0
            # refuses the put with 'Invalid layer' whatever the value, even
            # ``False`` while thawed (measured 2026-09-22, AutoCAD 2026), and
            # a state saved with 0 current came back with two warnings.
            if "current" in props and state["current_layer"] not in missing:
                target_name = state["current_layer"]
                target = doc.Layers.Item(target_name)
                try:
                    if (
                        "frozen" in props
                        and not bool(state["layers"][target_name]["frozen"])
                        and bool(target.Freeze)
                    ):
                        target.Freeze = False
                    doc.ActiveLayer = target
                    current = target_name
                except Exception as exc:  # e.g. the state's current layer is frozen
                    warnings.append(f"{target_name}: cannot be made current: {exc}")
            applied = 0
            for layer_name, snap in state["layers"].items():
                if layer_name in missing:
                    continue
                lyr = doc.Layers.Item(layer_name)
                try:
                    if "color" in props:
                        lyr.color = int(snap["color"])
                    if "linetype" in props:
                        _ensure_linetype_loaded(snap["linetype"])
                        lyr.Linetype = snap["linetype"]
                    if "lineweight" in props:
                        lyr.Lineweight = int(snap["lineweight"])
                    if "plot" in props:
                        lyr.Plottable = bool(snap["plot"])
                    if "on" in props:
                        lyr.LayerOn = bool(snap["on"])
                    if "frozen" in props and bool(lyr.Freeze) != bool(snap["frozen"]):
                        lyr.Freeze = bool(snap["frozen"])
                    if "locked" in props:
                        lyr.Lock = bool(snap["locked"])
                except Exception as exc:  # e.g. freezing the active layer
                    warnings.append(f"{layer_name}: {exc}")
                    continue
                applied += 1
            _regen()
            result = {
                "ok": True,
                "name": key,
                "applied": {
                    "layers": applied,
                    "properties": list(props),
                    "current_layer": current,
                },
                "missing_layers": missing,
                "new_layers": new,
                "backend": "com",
            }
            if warnings:
                result["warnings"] = warnings
            return result

        return await self._run(_sync)

    async def layer_state_list(self) -> list[dict]:
        from engineering.environment.layer_states import decode_state

        def _sync():
            doc = _acad_doc()
            states = self._com_layer_states(doc, create=False)
            rows = []
            for key, xrecord in self._com_dict_entries(states):
                state = decode_state(self._com_xrecord_chunks(xrecord))
                rows.append(
                    {
                        "name": key,
                        "description": state.get("description"),
                        "layer_count": len(state["layers"]),
                    }
                )
            return rows

        return await self._run(_sync)

    async def layer_state_delete(self, name) -> dict:
        from engineering.environment.layer_states import validate_state_name

        clean = validate_state_name(name)

        def _sync():
            doc = _acad_doc()
            states = self._com_layer_states(doc, create=False)
            key, xrecord = self._com_layer_state_entry(states, clean)
            if xrecord is None:
                raise ValueError(
                    f"layer_state_delete: no layer state named {clean!r}; "
                    f"saved states: {[k for k, _ in self._com_dict_entries(states)]}"
                )
            removed = states.Remove(key)
            try:
                removed.Delete()
            except Exception as exc:  # Remove already detached it
                log.debug("XRecord.Delete after Remove: %s", exc)
            return {"ok": True, "deleted": key, "backend": "com"}

        return await self._run(_sync)

    # ── named views ───────────────────────────────────────────────────────────

    @staticmethod
    def _com_view(doc, name: str):
        try:
            return doc.Views.Item(name)
        except Exception:
            return None

    @staticmethod
    def _com_display_aspect(doc) -> float:
        """Width / height of the current viewport's display, from SCREENSIZE."""
        size = doc.GetVariable("SCREENSIZE")
        sx, sy = float(size[0]), float(size[1])
        return sx / sy if sx > 0 and sy > 0 else 1.0

    @staticmethod
    def _com_view_row(view) -> dict:
        center = view.Center
        return {
            "name": str(view.Name),
            "center": [float(center[0]), float(center[1])],
            "height": float(view.Height),
            "width": float(view.Width),
        }

    async def view_named_save(self, name, center=None, height=None, width=None) -> dict:
        from engineering.environment.names import validate_name
        from engineering.environment.views import resolve_view_args

        clean = validate_name(name, what="view name")
        args = resolve_view_args(center, height, width)

        def _sync():
            doc = _acad_doc()
            # "The current view" is VIEWCTR / VIEWSIZE at the display's aspect
            # (SCREENSIZE), which is what AutoCAD's own `-VIEW _S` records.
            # NOT ``doc.ActiveViewport.Center/Height/Width``: that object does
            # not track the display — measured (AutoCAD 2026) it still
            # answered the document's initial view after `_.ZOOM _C 5,6 20`,
            # after ``app.ZoomCenter`` and after its own values were written.
            if args["center"] is None or args["height"] is None or args["width"] is None:
                view_center = doc.GetVariable("VIEWCTR")
                view_height = float(doc.GetVariable("VIEWSIZE")) or 1.0
                aspect = self._com_display_aspect(doc)
            else:
                view_center, view_height, aspect = None, 1.0, 1.0
            cx, cy = args["center"] or (float(view_center[0]), float(view_center[1]))
            h = args["height"] or view_height
            w = args["width"] or h * aspect
            view = self._com_view(doc, clean)
            replaced = view is not None
            if view is None:
                # Measured (AutoCAD 2026): Views.Add creates a plan view —
                # Direction (0, 0, 1), Target (0, 0, 0) — so unlike ezdxf's
                # VIEW default of (1, 1, 1) nothing has to be forced here.
                view = doc.Views.Add(clean)
            view.Center = _av([cx, cy])
            view.Height = float(h)
            view.Width = float(w)
            return {
                "ok": True,
                "name": str(view.Name),
                "center": [float(cx), float(cy)],
                "height": float(h),
                "width": float(w),
                "replaced": replaced,
                "backend": "com",
            }

        return await self._run(_sync)

    async def view_named_restore(self, name) -> dict:
        from engineering.environment.names import validate_name

        clean = validate_name(name, what="view name")

        def _sync():
            doc = _acad_doc()
            view = self._com_view(doc, clean)
            if view is None:
                names = [str(doc.Views.Item(i).Name) for i in range(int(doc.Views.Count))]
                raise ValueError(
                    f"view_named_restore: no named view {clean!r}; saved views: {names}"
                )
            row = self._com_view_row(view)
            # NOT ``vport.Center/Height/Width`` + ``doc.ActiveViewport = vport``:
            # AutoCAD reconciles a width that does not match the display aspect
            # by anchoring the viewport's lower-left corner and widening, so the
            # view lands off-centre (measured, AutoCAD 2026, display aspect
            # 2.013: a 10x20 view at (5, 6) restored to VIEWCTR (20.134, 6)).
            # `-VIEW _R` centres the saved window and fits it — VIEWCTR = the
            # saved centre, VIEWSIZE = max(height, width / aspect) — and so
            # does ZOOM Window on the same rectangle, without a SendCommand.
            (cx, cy), h, w = row["center"], row["height"], row["width"]
            _acad_app().ZoomWindow(
                _apoint(cx - w / 2.0, cy - h / 2.0), _apoint(cx + w / 2.0, cy + h / 2.0)
            )
            result = {"ok": True, **row, "applied": "zoom_window", "backend": "com"}
            try:  # the read-back is what a reviewer measures the tool against
                ctr = doc.GetVariable("VIEWCTR")
                result["viewctr"] = [float(ctr[0]), float(ctr[1])]
                result["viewsize"] = float(doc.GetVariable("VIEWSIZE"))
            except Exception as exc:
                log.debug("VIEWCTR/VIEWSIZE read-back failed: %s", exc)
            return result

        return await self._run(_sync)

    async def view_named_list(self) -> list[dict]:
        def _sync():
            doc = _acad_doc()
            return [self._com_view_row(doc.Views.Item(i)) for i in range(int(doc.Views.Count))]

        return await self._run(_sync)

    # ── UCS ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _com_ucs(doc, name: str):
        try:
            return doc.UserCoordinateSystems.Item(name)
        except Exception:
            return None

    @staticmethod
    def _com_ucs_row(ucs, current: str) -> dict:
        return {
            "name": str(ucs.Name),
            "origin": [float(c) for c in ucs.Origin],
            "x_axis": [float(c) for c in ucs.XVector],
            "y_axis": [float(c) for c in ucs.YVector],
            "current": current != "" and str(ucs.Name).lower() == current.lower(),
        }

    async def ucs_list(self) -> list[dict]:
        from engineering.environment.ucs import WORLD, WORLD_ORIGIN, WORLD_X_AXIS, WORLD_Y_AXIS

        def _sync():
            # GetVariable is an AcadDocument member; AcadApplication has none
            # (measured: ``hasattr(app, "GetVariable") is False``).
            doc = _acad_doc()
            current = str(doc.GetVariable("UCSNAME") or "")
            rows = [
                {
                    "name": WORLD,
                    "origin": list(WORLD_ORIGIN),
                    "x_axis": list(WORLD_X_AXIS),
                    "y_axis": list(WORLD_Y_AXIS),
                    "current": False,
                }
            ]
            collection = doc.UserCoordinateSystems
            for index in range(int(collection.Count)):
                rows.append(self._com_ucs_row(collection.Item(index), current))
            if any(row["current"] for row in rows[1:]):
                return rows
            # No saved entry is current. UCSNAME is empty for an unnamed UCS
            # (UCS Origin / 3P without saving) as well as for WCS — verified
            # live (AutoCAD 2026: after `_.UCS _O 10,10,0`, UCSNAME="" and
            # WORLDUCS=0). WORLDUCS is AutoCAD's own answer to "is this WCS".
            if int(doc.GetVariable("WORLDUCS")) == 1:
                rows[0]["current"] = True
            else:
                rows.append(
                    {
                        "name": None,
                        "origin": [float(c) for c in doc.GetVariable("UCSORG")],
                        "x_axis": [float(c) for c in doc.GetVariable("UCSXDIR")],
                        "y_axis": [float(c) for c in doc.GetVariable("UCSYDIR")],
                        "current": True,
                    }
                )
            return rows

        return await self._run(_sync)

    async def ucs_set(self, name, origin, x_axis, y_axis) -> dict:
        from engineering.environment.names import validate_name
        from engineering.environment.ucs import WORLD, resolve_ucs_axes

        clean = validate_name(name, what="UCS name")
        if clean.lower() == WORLD:
            raise ValueError("ucs_set: 'world' is reserved; ucs_restore('world') resets to WCS")
        axes = resolve_ucs_axes(origin, x_axis, y_axis)  # refuses before any ActiveX call

        def _sync():
            doc = _acad_doc()
            o, x, y = axes["origin"], axes["x_axis"], axes["y_axis"]
            ucs = self._com_ucs(doc, clean)
            replaced = ucs is not None
            if ucs is None:
                # ActiveX takes POINTS on the axes, not direction vectors.
                ucs = doc.UserCoordinateSystems.Add(
                    _apoint(*o),
                    _apoint(o[0] + x[0], o[1] + x[1], o[2] + x[2]),
                    _apoint(o[0] + y[0], o[1] + y[1], o[2] + y[2]),
                    clean,
                )
            else:
                ucs.Origin = _apoint(*o)
                ucs.XVector = _apoint(*x)
                ucs.YVector = _apoint(*y)
            doc.ActiveUCS = ucs
            return {
                "ok": True,
                "name": str(ucs.Name),
                "origin": list(o),
                "x_axis": list(x),
                "y_axis": list(y),
                "replaced": replaced,
                "current": True,
                "backend": "com",
            }

        return await self._run(_sync)

    async def ucs_restore(self, name) -> dict:
        from engineering.environment.names import validate_name
        from engineering.environment.ucs import WORLD

        clean = validate_name(name, what="UCS name")

        def _sync():
            doc = _acad_doc()
            if clean.lower() == WORLD:
                # No ActiveX member selects WCS; the command does. Same guard as
                # system_run_command: never send into an active prompt. The
                # read is a document member, and a failed read REFUSES: a
                # swallowed error here once forced cmd_active to 0 and sent
                # `_.UCS _W` into whatever prompt was open.
                try:
                    cmd_active = int(doc.GetVariable("CMDACTIVE"))
                except Exception as exc:
                    raise RuntimeError(
                        "ucs_restore: cannot verify AutoCAD is idle (CMDACTIVE read "
                        f"failed: {exc}); nothing was sent"
                    ) from exc
                if cmd_active:
                    raise RuntimeError(
                        "AutoCAD has an active command or prompt (CMDACTIVE="
                        f"{cmd_active}). Press ESC in AutoCAD to cancel, then retry."
                    )
                doc.SendCommand("_.UCS _W\n")
                return {"ok": True, "name": WORLD, "current": True, "backend": "com"}
            ucs = self._com_ucs(doc, clean)
            if ucs is None:
                names = [
                    str(doc.UserCoordinateSystems.Item(i).Name)
                    for i in range(int(doc.UserCoordinateSystems.Count))
                ]
                raise ValueError(
                    f"ucs_restore: no UCS named {clean!r}; saved: {names} (or 'world')"
                )
            doc.ActiveUCS = ucs
            return {"ok": True, "name": str(ucs.Name), "current": True, "backend": "com"}

        return await self._run(_sync)

    # ── application, preferences, operator prompts ────────────────────────────

    async def system_launch(self, visible: bool = True, open_path: str | None = None) -> dict:
        """Attach to the running application, or start it; optionally open a file.

        Honours ``CAD_PROGID`` on both paths, like ``_acad_app``: ``Dispatch``
        launches the application, so falling back to AutoCAD would start the
        very product the operator said they were not using. The path is
        validated before any COM call.
        """
        path = str(validate_path(open_path, allow_write=False)) if open_path else None

        def _sync():
            progid = config.settings.cad_progid
            app = _COM_STATE.get("app")
            launched = False
            if app is None:
                try:
                    app = win32com.client.GetActiveObject(progid)
                except Exception as exc:
                    log.debug("GetActiveObject(%r) failed, dispatching: %s", progid, exc)
                    app = win32com.client.Dispatch(progid)
                    launched = True
                _COM_STATE["app"] = app
            try:
                app.Visible = bool(visible)
            except Exception as exc:  # some hosts refuse to hide
                log.debug("Visible=%s refused: %s", visible, exc)
            document = None
            if path:
                document = str(app.Documents.Open(path).Name)
            elif int(app.Documents.Count):
                document = str(app.ActiveDocument.Name)
            return {
                "launched": launched,
                "attached": not launched,
                "version": str(app.Version),
                "document": document,
                "visible": bool(visible),
                "progid": progid,
                "backend": "com",
            }

        result = await self._run(_sync)
        # B11: only a file this call opened is a document the call chose; an
        # attach keeps the binding (and an open transaction) as it was.
        if path:
            await self._rebind()
        return result

    async def preferences_get(self, keys: list[str] | None = None) -> dict:
        from engineering.environment.preferences import (
            PREFERENCE_KEYS,
            READ_ONLY_KEYS,
            decode_value,
            known_keys,
            split_key,
        )

        if keys is None:
            wanted = known_keys()
        else:
            if isinstance(keys, str) or not isinstance(keys, (list, tuple)):
                raise TypeError("preferences_get: keys must be a list of preference names")
            unknown = [k for k in keys if k not in PREFERENCE_KEYS and k not in READ_ONLY_KEYS]
            if unknown:
                raise ValueError(
                    f"preferences_get: unknown preference keys {unknown}; known: {known_keys()}"
                )
            wanted = list(keys)

        def _sync():
            prefs = _acad_app().Preferences
            values = {}
            for key in wanted:
                section, member = split_key(key)
                values[key] = decode_value(key, getattr(getattr(prefs, section), member))
            return {
                "values": values,
                "read_only": [k for k in wanted if k in READ_ONLY_KEYS],
                "backend": "com",
            }

        return await self._run(_sync)

    async def preferences_set(self, key: str, value) -> dict:
        from engineering.environment.preferences import (
            decode_value,
            split_key,
            validate_preference,
        )

        coerced = validate_preference(key, value)  # read-only / unknown / range: refused here
        section, member = split_key(key)

        def _sync():
            target = getattr(_acad_app().Preferences, section)
            old = getattr(target, member)
            setattr(target, member, coerced)
            new = getattr(target, member)
            return {
                "ok": True,
                "key": key,
                "old": decode_value(key, old),
                "new": decode_value(key, new),
                "changed": old != new,
                "backend": "com",
            }

        return await self._run(_sync)

    @staticmethod
    def _prompt_text(prompt, where: str, max_len: int = 255) -> str:
        from engineering.environment.names import validate_name

        return validate_name(prompt, what=f"{where}: prompt", max_len=max_len)

    @staticmethod
    def _com_cancel_reason(exc) -> str | None:
        """The operator's cancel as ActiveX reports it, or None for any other COM error.

        ESC in GetPoint / GetEntity / SelectOnScreen raises DISP_E_EXCEPTION
        (-2147352567) with AutoCAD's description ("User input is a keyword",
        "Function cancelled"). Anything else is a real failure and propagates.
        """
        args = getattr(exc, "args", ())
        if not args or args[0] != -2147352567:
            return None
        detail = args[2] if len(args) > 2 and isinstance(args[2], tuple) else ()
        description = detail[2] if len(detail) > 2 and detail[2] else "cancelled"
        return str(description)

    async def _interactive(self, func):
        """Run an operator prompt; a wall-clock timeout is an answer, not a crash.

        ``_run`` has already rebuilt the STA executor by the time its
        "did not respond" error surfaces (the worker is still blocked inside
        the prompt); the next call re-attaches lazily, exactly as after any
        other timeout. ``COM_CALL_TIMEOUT`` is the budget the operator has.
        """
        try:
            return await self._run(func)
        except RuntimeError as exc:
            if str(exc).startswith("AutoCAD did not respond"):
                return {
                    "timed_out": True,
                    "timeout_s": config.settings.com_call_timeout,
                    "error": str(exc),
                }
            raise

    async def user_pick_point(self, prompt: str) -> dict:
        text = self._prompt_text(prompt, "user_pick_point")

        def _sync():
            doc = _acad_doc()
            try:
                point = doc.Utility.GetPoint(pythoncom.Missing, f"\n{text}")
            except _COM_ERROR as exc:
                reason = self._com_cancel_reason(exc)
                if reason is None:
                    raise
                return {"cancelled": True, "reason": reason, "backend": "com"}
            return {
                "cancelled": False,
                "x": float(point[0]),
                "y": float(point[1]),
                "z": float(point[2]) if len(point) > 2 else 0.0,
                "backend": "com",
            }

        return await self._interactive(_sync)

    async def user_select(self, prompt: str, mode: str = "single") -> dict:
        if mode not in ("single", "multiple"):
            raise ValueError(f"user_select: mode must be 'single' or 'multiple', got {mode!r}")
        text = self._prompt_text(prompt, "user_select")

        def _sync():
            doc = _acad_doc()
            if mode == "single":
                try:
                    obj, picked = doc.Utility.GetEntity(f"\n{text}")
                except _COM_ERROR as exc:
                    reason = self._com_cancel_reason(exc)
                    if reason is None:
                        raise
                    return {"cancelled": True, "reason": reason, "handles": [], "backend": "com"}
                # MEASURED (AutoCAD 2026): GetEntity's PickedPoint is in the
                # *current UCS* — under a UCS at (100,50) rotated 90 deg, a pick
                # on a circle at WCS (105,70) came back as (20,-5,0) — whereas
                # Utility.GetPoint already answers in WCS. Every coordinate out
                # of a tool is WCS, so translate acUCS → acWorld. The point must
                # be a VT_ARRAY|VT_R8 VARIANT: the tuple GetEntity hands back is
                # refused as "Invalid argument Point".
                world = doc.Utility.TranslateCoordinates(
                    _apoint(picked[0], picked[1], picked[2] if len(picked) > 2 else 0.0),
                    _AC_UCS,
                    _AC_WORLD,
                    False,
                )
                return {
                    "cancelled": False,
                    "mode": "single",
                    "handles": [str(obj.Handle)],
                    "picked": [float(world[0]), float(world[1])],
                    "backend": "com",
                }
            doc.Utility.Prompt(f"\n{text}\n")
            ss = doc.SelectionSets.Add(f"_PICK_{uuid.uuid4().hex[:8]}")
            try:
                try:
                    ss.SelectOnScreen()
                except _COM_ERROR as exc:
                    reason = self._com_cancel_reason(exc)
                    if reason is None:
                        raise
                    return {"cancelled": True, "reason": reason, "handles": [], "backend": "com"}
                handles = [str(ss.Item(i).Handle) for i in range(int(ss.Count))]
                # Enter with nothing selected is an empty answer, not a cancel.
                return {
                    "cancelled": False,
                    "mode": "multiple",
                    "handles": handles,
                    "count": len(handles),
                    "backend": "com",
                }
            finally:
                try:
                    ss.Delete()
                except Exception as exc:
                    log.debug("SelectionSet cleanup failed: %s", exc)

        return await self._interactive(_sync)

    async def system_prompt_message(self, text: str) -> dict:
        clean = self._prompt_text(text, "system_prompt_message", max_len=2000)

        def _sync():
            _acad_doc().Utility.Prompt(f"\n{clean}\n")
            return {"ok": True, "text": clean, "backend": "com"}

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
                if color is not None and int(entity.color) != int(color):
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
            raise EntityNotFoundError(handle) from None
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
                    "color": int(hatch.color),
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
                hatch.color = int(color)
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
        """Add one boundary path to a HATCH from typed edges, as an inner loop.

        ActiveX takes a loop as whole objects, so every typed edge becomes a
        real temporary entity in the hatch's own space - a LINE, a true ARC or
        an ELLIPSE, never a chord chain - appended with ``AppendInnerLoop``,
        evaluated, then deleted. A loop made only of chained lines becomes one
        closed lightweight polyline: one COM call instead of one per edge (a
        flattened bore is 180 of them). Measured on AutoCAD 2026 (25.1s): a
        20 mm island built from two arcs leaves exactly 10000 - 100*pi of a
        100 x 100 hatch, and an open loop is refused by AutoCAD with E_FAIL.

        The edge list is validated in full before anything is created, with the
        headless engine's messages, and a malformed list answers
        ``{"ok": False}``; a loop AutoCAD refuses is taken back out, temporary
        entities included.
        """
        prepared, refusal = _prepare_hatch_edges(edges)
        if refusal:
            return refusal

        def _sync():
            doc = _acad_doc()
            hatch, refused = self._resolve_com_hatch(doc, handle)
            if refused:
                return refused
            try:
                space = _com_unnarrow(doc.ObjectIdToObject(hatch.OwnerID))
            except Exception as exc:
                log.debug("hatch owner lookup failed, using model space: %s", exc)
                space = _msp()
            temporary = []
            try:
                chain = _closed_line_chain(prepared)
                if chain is not None:
                    flat = [c for point in chain for c in point]
                    loop = space.AddLightWeightPolyline(_av(flat))
                    loop.Closed = True
                    temporary.append(loop)
                else:
                    for kind, edge in prepared:
                        temporary.append(_temporary_edge_entity(space, kind, edge))
                objects = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, temporary
                )
                hatch.AppendInnerLoop(objects)
                hatch.Evaluate()
            except Exception as exc:
                return {
                    "ok": False,
                    "error": (
                        f"AutoCAD did not accept the boundary as a closed loop ({exc}); "
                        "nothing was added to the hatch."
                    ),
                }
            finally:
                for entity in temporary:
                    try:
                        entity.Delete()
                    except Exception as exc:
                        log.debug("temporary hatch edge not deleted: %s", exc)
            _regen()
            return {
                "ok": True,
                "handle": hatch.Handle,
                "path_count": int(hatch.NumberOfLoops),
                "edge_types": [kind for kind, _ in prepared],
            }

        return await self._run(_sync)

    async def analysis_list_properties(self, handle: str) -> dict:
        def _sync():
            # VERIFIED against a live AutoCAD 2026 (2026-08-05). ActiveX has no
            # generic attribute dump, so this reads the documented members per
            # object type.
            doc = _acad_doc()
            try:
                entity = doc.HandleToObject(str(handle).strip().upper())
            except Exception:
                raise EntityNotFoundError(handle) from None

            dump: dict = {}
            for member in (
                "Layer",
                "color",
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
    _TEXT_BEARING_OBJECTS = _COM_TEXT_OBJECTS

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
            # The bound document: inside a _run job Task 9's _JobGuard makes
            # _acad_doc() return it (or refuse), never whatever is active.
            doc = _acad_doc()
            pattern = re.compile(re.escape(needle), 0 if match_case else re.IGNORECASE)
            changed = _com_text_find_replace(doc, pattern, replace, layer, dry_run)
            return {
                "ok": True,
                "replaced": len(changed),
                "entities": changed,
                "searched_types": list(self._TEXT_BEARING_OBJECTS),
                "searched_scope": list(TEXT_SEARCH_SCOPE),
                "note": (
                    "DIMENSION text is not searched: its TextOverride holds the '<>' "
                    "placeholder rather than the measured value; nor is any anonymous "
                    "block ('*D' holds a dimension's rendered measurement, '*T' a table's)."
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

    # ── page setup (track E) ─────────────────────────────────────────────────
    #
    # Layout is an IAcadPlotConfiguration. Constants from the AutoCAD 2026
    # typelib: AcPlotPaperUnits acMillimeters=1; AcPlotRotation ac0degrees=0;
    # AcPlotType acExtents=1 / acLayout=5; AcPlotScale acScaleToFit=0, ac1_1=16
    # (the full table is engineering.standards.papers.ACTIVEX_PLOT_SCALE).
    # CenterPlot (ActiveX Reference, acadauto.chm): "This property cannot be
    # set to True on a layout object whose PlotType property is set to
    # acLayout" — so a layout plot never writes it (resolve_page_setup hands
    # over None), an extents plot writes it *after* PlotType, and a stale True
    # is cleared *before* PlotType moves to acLayout. Every write is journaled
    # and unwound in reverse when AutoCAD refuses one, so a failed apply leaves
    # the layout as it was — and the read-back after the unwind is compared to
    # the read-back before the first write, so a value that did not go back is
    # named rather than claimed restored.
    #
    # Two behaviours measured live (AutoCAD 2026, 2026-09-17) that the ActiveX
    # Reference does not document:
    # * GetPaperSize / GetPaperMargins answer in millimetres whatever
    #   PaperUnits says (ISO_A3 under PaperUnits=0 -> (420.0, 297.0); ANSI_B
    #   under 0 and 1 -> (431.8, 279.4) both times). They are never scaled.
    #   PaperUnits itself still governs what one paper-space unit means at
    #   the plot scale, so the row reports it (``paper_units``) and an apply
    #   that moves it says so in ``changed`` and ``warnings``.
    # * Both answer in the *media's* frame (the PLOTSETTINGS fields the DXF
    #   row reads); under PlotRotation 1/3 the row turns size and margins
    #   together through ``papers.turned_sheet``, the one mapping the headless
    #   row and renderer use. The rotated read-back is not yet measured live
    #   (scripts/smoke_settings_com.py, Task 24, is where it gets measured).
    # * Writing ConfigName replaces CanonicalMediaName with the new device's
    #   default when the current media does not exist on it, and writing the
    #   old device back does *not* bring the old media with it (DWG To PDF /
    #   ISO_A3 -> Microsoft Print to PDF -> 'psk:ISOA4' -> DWG To PDF ->
    #   'ANSI_A_(11.00_x_8.50_Inches)'). The device write therefore journals
    #   one combined undo: device back, then the media / units / rotation
    #   snapshotted before the switch re-applied.
    #
    # Fake-tested in tests/test_page_setup.py (the fake enforces the CenterPlot
    # rule and replays both measurements); executed live by
    # scripts/smoke_settings_com.py (Task 24).

    _AC_NO_ROTATION = 0
    #: AcSaveAsType, AutoCAD 2026 typelib: ac2018_Template = 66 (acNative = 64,
    #: ac2013_dxf = 61). The spec's "acTemplateDwg" is not a member of the enum.
    _AC_SAVEAS_TEMPLATE = 66
    _AC_SAVEAS_DXF = 61
    #: Layout properties a ConfigName write can move as a side effect (measured
    #: live for CanonicalMediaName; units / rotation are re-applied on the same
    #: evidence-before-trust basis) — snapshotted before the device switch and
    #: put back by its undo.
    _DEVICE_SIDE_EFFECTS = ("CanonicalMediaName", "PaperUnits", "PlotRotation")

    @staticmethod
    def _com_page_setup_row(layout) -> dict:
        from engineering.standards.papers import (
            PLOT_TYPE_NAMES,
            activex_scale_label,
            center_applies,
            paper_from_size,
            paper_units_name,
            scale_label,
            turned_sheet,
        )

        def _get(attr):
            try:
                return getattr(layout, attr)
            except Exception as exc:
                log.debug("Layout.%s read failed: %s", attr, exc)
                return None

        # GetPaperSize / GetPaperMargins are millimetres regardless of
        # PaperUnits (measured live; see the block comment above) — no factor.
        size = None
        try:
            width, height = layout.GetPaperSize()
            size = [round(float(width), 2), round(float(height), 2)]
        except Exception as exc:
            log.debug("Layout.GetPaperSize failed: %s", exc)
        margins = None
        try:
            (left, bottom), (right, top) = layout.GetPaperMargins()
            margins = [round(float(v), 3) for v in (top, bottom, left, right)]
        except Exception as exc:
            log.debug("Layout.GetPaperMargins failed: %s", exc)
        rotation = _get("PlotRotation") or 0
        if size and margins:
            size, margins = turned_sheet(size[0], size[1], margins, rotation)
            size = [round(v, 2) for v in size]
            margins = [round(v, 3) for v in margins]
        elif size and int(rotation) in (1, 3):
            size = [size[1], size[0]]
        if _get("UseStandardScale"):
            scale = activex_scale_label(_get("StandardScale") or 0)
        else:
            try:
                numerator, denominator = layout.GetCustomScale()
                scale = scale_label(float(numerator), float(denominator))
            except Exception as exc:
                log.debug("Layout.GetCustomScale failed: %s", exc)
                scale = None
        media = _get("CanonicalMediaName")
        plot_type = _get("PlotType")
        return {
            "layout": layout.Name,
            "paper": (paper_from_size(*size) if size else None) or media,
            "canonical_media_name": media,
            "size_mm": size,
            "orientation": (
                None if not size else ("landscape" if size[0] >= size[1] else "portrait")
            ),
            "plot_style": _get("StyleSheet"),
            "scale": scale,
            "plot_area": PLOT_TYPE_NAMES.get(plot_type, plot_type),
            "device": _get("ConfigName"),
            "margins_mm": margins,
            "paper_units": paper_units_name(_get("PaperUnits")),
            "center": bool(_get("CenterPlot")) if center_applies(plot_type) else None,
        }

    def _com_paper_layout(self, doc, raw: str, operation: str):
        resolved = self._find_layout(doc, raw)
        if resolved is None:
            available = [n for n in self._layout_names(doc) if n != "Model"]
            raise ValueError(
                f"{operation}: layout {raw!r} not found (paper-space layouts: "
                f"{', '.join(available) or 'none'})"
            )
        if resolved == "Model":
            raise ValueError(
                f"{operation}: Model space has no sheet; plot it with "
                "drawing_export_pdf(layout=None) or apply the setup to a paper-space layout"
            )
        return doc.Layouts.Item(resolved)

    async def page_setup_list(self, layout: str | None = None) -> list[dict]:
        def _sync():
            doc = _acad_doc()
            if layout is not None and self._layout_name(layout):
                target = self._com_paper_layout(doc, layout, "page_setup_list")
                return [self._com_page_setup_row(target)]
            return [
                self._com_page_setup_row(doc.Layouts.Item(name))
                for name in self._layout_names(doc)
                if name != "Model"
            ]

        return await self._run(_sync)

    async def page_setup_apply(self, layout: str, setup: dict) -> dict:
        from engineering.standards.papers import (
            PAPER_UNITS,
            page_setup_warnings,
            require_page_setup,
        )

        resolved = require_page_setup(setup)
        if resolved["margins_mm"] is not None:
            raise ValueError(
                "page_setup_apply: margins_mm cannot be written on the COM backend — AutoCAD "
                "takes the printable margins from the plotter configuration (.pc3). Omit "
                "margins_mm; the device's margins are read back in the result."
            )

        def _sync():
            doc = _acad_doc()
            target = self._com_paper_layout(doc, layout, "page_setup_apply")
            before = self._com_page_setup_row(target)
            # Journal of (undo callable, label) for every write that landed,
            # unwound in reverse when a later one is refused.
            journal: list[tuple[Callable[[], None], str]] = []

            def _set(attr: str, value):
                previous = getattr(target, attr)
                setattr(target, attr, value)
                journal.append((lambda: setattr(target, attr, previous), attr))

            def _set_custom_scale(numerator: float, denominator: float):
                previous = tuple(float(v) for v in target.GetCustomScale())
                target.SetCustomScale(numerator, denominator)
                journal.append((lambda: target.SetCustomScale(*previous), "SetCustomScale"))

            def _set_device(device: str):
                # A device switch moves the media (and can move units /
                # rotation) as a side effect, and switching back does not
                # move them back — so the undo re-applies what was there.
                snapshot = {attr: getattr(target, attr) for attr in self._DEVICE_SIDE_EFFECTS}
                previous = target.ConfigName
                target.ConfigName = device

                def _undo():
                    target.ConfigName = previous
                    target.RefreshPlotDeviceInfo()
                    for attr, value in snapshot.items():
                        if getattr(target, attr) != value:
                            setattr(target, attr, value)

                journal.append((_undo, "ConfigName"))

            step = "ConfigName"
            try:
                target.RefreshPlotDeviceInfo()
                _set_device(resolved["device"])
                target.RefreshPlotDeviceInfo()
                media = resolved["canonical_media_name"]
                names = [str(n) for n in (target.GetCanonicalMediaNames() or ())]
                if media not in names:
                    nearest = [n for n in names if n.split("_(")[0] == media.split("_(")[0]]
                    raise ValueError(
                        f"page_setup_apply: device {resolved['device']!r} has no media "
                        f"{media!r} (same paper on this device: "
                        f"{', '.join(nearest) or 'none'}; {len(names)} media in total)"
                    )
                step = "CanonicalMediaName"
                _set("CanonicalMediaName", media)
                if resolved["paper_units"] is not None:
                    # None keeps the layout's units — what AutoCAD's own Page
                    # Setup does when a media is chosen.
                    step = "PaperUnits"
                    _set("PaperUnits", PAPER_UNITS[resolved["paper_units"]])
                step = "PlotRotation"
                _set("PlotRotation", self._AC_NO_ROTATION)
                step = "StyleSheet"
                _set("StyleSheet", resolved["plot_style"])
                step = "PlotWithPlotStyles"
                _set("PlotWithPlotStyles", True)
                center = resolved["center"]
                if center is None and self._com_center_plot_is_set(target):
                    # Clearing is legal under any PlotType; the layout plot
                    # then carries the flag AutoCAD stores for acLayout.
                    step = "CenterPlot"
                    _set("CenterPlot", False)
                step = "PlotType"
                _set("PlotType", int(resolved["plot_type"]))
                code = resolved["activex_standard_scale"]
                if code is not None:
                    step = "StandardScale"
                    _set("StandardScale", int(code))
                    step = "UseStandardScale"
                    _set("UseStandardScale", True)
                else:
                    numerator, denominator = (float(v) for v in resolved["scale_ratio"])
                    step = "SetCustomScale"
                    _set_custom_scale(numerator, denominator)
                    step = "UseStandardScale"
                    _set("UseStandardScale", False)
                if center is not None:
                    # Extents plot: PlotType is already acExtents, so True is legal.
                    step = "CenterPlot"
                    _set("CenterPlot", bool(center))
            except Exception as exc:
                failed = self._com_unwind_page_setup(journal)
                # The unwind's own word is not evidence: read the layout back
                # and name every field that is not what it was.
                failed += [
                    key
                    for key, value in self._com_page_setup_row(target).items()
                    if key != "layout" and value != before.get(key) and key not in failed
                ]
                if isinstance(exc, ValueError) and not failed:
                    raise  # our own refusal (unknown media), nothing left changed
                restored = (
                    f"layout {target.Name!r} restored to its previous page setup"
                    if not failed
                    else f"layout {target.Name!r} could NOT be fully restored "
                    f"(still changed: {', '.join(failed)})"
                )
                if isinstance(exc, ValueError):
                    raise ValueError(f"{exc}; {restored}") from exc
                raise ValueError(
                    f"page_setup_apply: AutoCAD refused Layout.{step} ({exc}); {restored}"
                ) from exc
            after = self._com_page_setup_row(target)
            changed = {
                key: [before[key], after[key]]
                for key in after
                if key != "layout" and before.get(key) != after[key]
            }
            return {
                "ok": True,
                "layout": target.Name,
                "applied": dict(resolved),
                "changed": changed,
                "warnings": page_setup_warnings(changed),
                "plot_style_known": bool(resolved["plot_style_known"]),
                "viewports_kept": True,
                "backend": "com",
            }

        return await self._run(_sync)

    @staticmethod
    def _com_center_plot_is_set(layout) -> bool:
        """Whether the layout carries CenterPlot=True (a failed read counts as no)."""
        try:
            return bool(layout.CenterPlot)
        except Exception as exc:
            log.debug("Layout.CenterPlot read failed: %s", exc)
            return False

    @staticmethod
    def _com_unwind_page_setup(journal: list[tuple[Callable[[], None], str]]) -> list[str]:
        """Undo every journaled page-setup write in reverse; return what would not go back."""
        failed: list[str] = []
        for undo, label in reversed(journal):
            try:
                undo()
            except Exception as exc:
                log.warning("page_setup_apply: could not restore Layout.%s: %s", label, exc)
                failed.append(label)
        return failed

    async def plot_style_list(self) -> list[dict]:
        """The ctb catalogue, marked against the files in AutoCAD's plot style path.

        ``Preferences.Files.PrinterStyleSheetPath`` is a ``;``-separated list of
        folders. Every read is guarded: a folder that cannot be listed is
        skipped, and when the Preferences object is unreachable the catalogue
        rows come back with ``installed: None`` rather than a fabricated False.
        """
        from engineering.standards.papers import CTB_CATALOG

        def _sync():
            rows = [{"name": name, "source": "catalog", "installed": None} for name in CTB_CATALOG]
            try:
                raw = str(_acad_app().Preferences.Files.PrinterStyleSheetPath or "")
            except Exception as exc:
                log.debug("Preferences.Files.PrinterStyleSheetPath unreadable: %s", exc)
                return rows
            installed: dict[str, str] = {}
            for folder in (part.strip() for part in raw.split(";")):
                if not folder:
                    continue
                try:
                    entries = sorted(Path(folder).iterdir())
                except OSError as exc:
                    log.debug("plot style folder %r not listable: %s", folder, exc)
                    continue
                for entry in entries:
                    if entry.suffix.lower() in (".ctb", ".stb"):
                        installed.setdefault(entry.name.lower(), entry.name)
            catalog_lower = {row["name"].lower() for row in rows}
            for row in rows:
                row["installed"] = row["name"].lower() in installed
            for key, name in installed.items():
                if key not in catalog_lower:
                    rows.append({"name": name, "source": "installed", "installed": True})
            return rows

        return await self._run(_sync)

    async def drawing_template_save(self, path, name=None, description=None) -> dict:
        """``SaveAs(path, ac2018_Template)`` for ``.dwt``, the DXF code for ``.dxf``.

        AutoCAD's template description is its summary-info Comments field, so
        ``description`` is written there first (``description_written`` says
        whether it took). SaveAs rebinds the active document to the new file,
        as the SAVEAS command does — reported as ``document_rebound``.
        Fake-tested; executed live by ``scripts/smoke_settings_com.py --build-dwt``.
        """
        suffix = Path(path).suffix.lower()
        if suffix not in (".dwt", ".dxf"):
            raise ValueError(f"drawing_template_save: path must end in .dwt or .dxf, got {path!r}")

        def _sync():
            doc = _acad_doc()
            description_written = False
            if description is not None:
                try:
                    doc.SummaryInfo.Comments = str(description)
                    description_written = True
                except Exception as exc:
                    log.debug("SummaryInfo.Comments write failed: %s", exc)
            code = self._AC_SAVEAS_TEMPLATE if suffix == ".dwt" else self._AC_SAVEAS_DXF
            doc.SaveAs(path, code)
            self._rekey_bound_sync(doc)
            return {
                "ok": True,
                "path": path,
                "format": suffix[1:],
                "backend": "com",
                "name": name or Path(path).stem,
                "description_written": description_written,
                "document_rebound": True,
            }

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
        profile = _handle_to_object(doc, profile_handle)
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
            target = _handle_to_object(doc, target_handle)
            tool = _handle_to_object(doc, tool_handle)
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
            # AUDITCTL is a document sysvar (the Application has no
            # GetVariable / SetVariable). The read is optional only for a host
            # that does not expose the variable at all; then nothing is set
            # and nothing needs restoring.
            try:
                previous = doc.GetVariable("AUDITCTL")
            except Exception as exc:  # not every AutoCAD-compatible host exposes it
                log.debug("doc.GetVariable(AUDITCTL) failed; AUDIT runs unlogged: %s", exc)
                previous = None
            if previous is not None:
                doc.SetVariable("AUDITCTL", 1)
            try:
                doc.SendCommand("_AUDIT Y\n")
            finally:
                if previous is not None:
                    try:
                        doc.SetVariable("AUDITCTL", previous)
                    except Exception:
                        log.debug("could not restore AUDITCTL on the document", exc_info=True)

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
        """The active-document case of ``document_close``.

        ``Close(True)`` on a drawing that has never been saved opens AutoCAD's
        Save dialog and blocks the single STA thread until ``COM_CALL_TIMEOUT``;
        that is refused here by name rather than waited out.
        """
        return await self.document_close(None, save=save, discard=not save)

    async def drawing_undo(self) -> dict:
        return await self._run(self._undo_step_sync, "_UNDO 1", "Undo")

    async def drawing_redo(self) -> dict:
        return await self._run(self._undo_step_sync, "_REDO", "Redo")

    def _undo_step_sync(self, command: str, label: str) -> dict:
        """One ``_UNDO 1`` / ``_REDO`` through ``_safe_send_command``, verified (1.8-05).

        Up to 1.7.0 the raw ``SendCommand`` returned before AutoCAD had run
        it, so ``ok: True`` said nothing. Now the command is waited on (an
        undo left at a prompt is ``command_incomplete``) and ``verified`` says
        whether the drawing observably changed: the entity total over every
        layout block moved, or the bound document's edit counter (the
        database reactor behind the live revision) counted a change. ``False``
        is "no change seen" - nothing was left to undo, or (without Visual
        LISP) an undo that only modified entities - never a claim it failed.
        """
        doc = _acad_doc()
        entry = self._known_documents.get(self._bound_entry_key()) or {}
        edits_before = entry.get("edits")
        outcome = self._safe_send_command(doc, command)
        edits_after = None
        if edits_before is not None and entry.get("document_id"):
            try:
                edits_after = self._read_edit_count(
                    _acad_app(), entry["document_id"], refetch=False
                )
            except Exception as exc:  # evidence only
                log.debug("edit counter unreadable after %s: %s", command, exc)
        counted = edits_before is not None and edits_after is not None
        before, after = outcome.entities_before, outcome.entities_after
        verified = (before is not None and after is not None and before != after) or (
            counted and edits_after != edits_before
        )
        message = (
            f"{label} applied"
            if verified
            else f"{label} sent and completed; no change observed in the drawing"
        )
        return {
            "ok": True,
            "message": message,
            "verified": bool(verified),
            "evidence": {
                "entities_before": before,
                "entities_after": after,
                "edit_counter_before": edits_before if counted else None,
                "edit_counter_after": edits_after if counted else None,
            },
            "last_prompt": outcome.last_prompt,
        }

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
        xdata=None,
    ) -> EntityInfo:
        return await self._run(
            self._create_line_job(x1, y1, x2, y2, z1, z2, layer, color, linetype, xdata)
        )

    # 1.8-23: each batchable create is split into a job builder - the
    # arguments are checked on the caller's thread, the returned ``_sync`` is
    # the COM-thread body - so ``entity_batch_create`` runs many of them in
    # ONE ``_run`` job and the single tool runs one, with the same code.

    def _create_line_job(
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
        xdata=None,
    ):
        prepared = prepare_xdata(xdata)

        def _sync():
            mspace = _msp()
            line = mspace.AddLine(_apoint(x1, y1, z1), _apoint(x2, y2, z2))
            _apply_entity_attrs(line, layer, color, linetype)
            _attach_xdata(line, prepared)
            _regen(line)
            return _entity_info(line)

        return _job_named(_sync, "entity_create_line")

    async def entity_create_circle(
        self,
        cx,
        cy,
        radius,
        layer=None,
        color=None,
        xdata=None,
    ) -> EntityInfo:
        return await self._run(self._create_circle_job(cx, cy, radius, layer, color, xdata))

    def _create_circle_job(self, cx, cy, radius, layer=None, color=None, xdata=None):
        prepared = prepare_xdata(xdata)

        def _sync():
            mspace = _msp()
            circle = mspace.AddCircle(_apoint(cx, cy), float(radius))
            _apply_entity_attrs(circle, layer, color, None)
            _attach_xdata(circle, prepared)
            _regen(circle)
            return _entity_info(circle)

        return _job_named(_sync, "entity_create_circle")

    async def entity_create_arc(
        self,
        cx,
        cy,
        radius,
        start_angle,
        end_angle,
        layer=None,
        color=None,
        xdata=None,
    ) -> EntityInfo:
        return await self._run(
            self._create_arc_job(cx, cy, radius, start_angle, end_angle, layer, color, xdata)
        )

    def _create_arc_job(
        self, cx, cy, radius, start_angle, end_angle, layer=None, color=None, xdata=None
    ):
        prepared = prepare_xdata(xdata)

        def _sync():
            mspace = _msp()
            arc = mspace.AddArc(
                _apoint(cx, cy),
                float(radius),
                deg2rad(start_angle),
                deg2rad(end_angle),
            )
            _apply_entity_attrs(arc, layer, color, None)
            _attach_xdata(arc, prepared)
            _regen(arc)
            return _entity_info(arc)

        return _job_named(_sync, "entity_create_arc")

    async def entity_create_polyline(
        self,
        points,
        closed=False,
        layer=None,
        color=None,
        xdata=None,
        bulges=None,
    ) -> EntityInfo:
        return await self._run(
            self._create_polyline_job(points, closed, layer, color, xdata, bulges)
        )

    def _create_polyline_job(
        self, points, closed=False, layer=None, color=None, xdata=None, bulges=None
    ):
        prepared = prepare_xdata(xdata)
        checked_bulges = None
        if bulges is not None:
            _pts, checked_bulges = prepare_vertices(points, bulges, "entity_create_polyline")

        def _sync():
            mspace = _msp()
            flat = []
            for pt in points:
                flat.extend([float(pt[0]), float(pt[1])])
            pline = mspace.AddLightWeightPolyline(_av(flat))
            pline.Closed = closed
            for index, bulge in enumerate(checked_bulges or ()):
                if bulge:  # a new polyline's segments are straight already
                    pline.SetBulge(index, bulge)
            _apply_entity_attrs(pline, layer, color, None)
            _attach_xdata(pline, prepared)
            _regen(pline)
            return _entity_info(pline)

        return _job_named(_sync, "entity_create_polyline")

    async def entity_create_text(
        self,
        text,
        x,
        y,
        height=2.5,
        rotation=0.0,
        layer=None,
        color=None,
        xdata=None,
    ) -> EntityInfo:
        return await self._run(
            self._create_text_job(text, x, y, height, rotation, layer, color, xdata)
        )

    def _create_text_job(
        self, text, x, y, height=2.5, rotation=0.0, layer=None, color=None, xdata=None
    ):
        prepared = prepare_xdata(xdata)

        def _sync():
            mspace = _msp()
            txt = mspace.AddText(text, _apoint(x, y), float(height))
            txt.Rotation = deg2rad(rotation)
            _apply_entity_attrs(txt, layer, color, None)
            _attach_xdata(txt, prepared)
            _regen(txt)
            return _entity_info(txt)

        return _job_named(_sync, "entity_create_text")

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
        return await self._run(
            self._create_mtext_job(text, x, y, width, height, rotation, layer, color)
        )

    def _create_mtext_job(
        self, text, x, y, width=100.0, height=2.5, rotation=0.0, layer=None, color=None
    ):
        def _sync():
            mspace = _msp()
            mt = mspace.AddMText(_apoint(x, y), float(width), text)
            mt.Height = float(height)
            if rotation:
                mt.Rotation = deg2rad(rotation)  # NEW-mtext-1: honor caller rotation
            _apply_entity_attrs(mt, layer, color, None)
            _regen(mt)
            return _entity_info(mt)

        return _job_named(_sync, "entity_create_mtext")

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
        xdata=None,
    ) -> EntityInfo:
        from engineering.annotation import prepare_table_layout

        prepared = prepare_xdata(xdata)
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
            # MEASURED on AutoCAD 2026: IAcadTable has no TextHeight property
            # (the old `table.TextHeight = h` raised and was swallowed, leaving
            # the Standard style's 6.0 / 4.5 - illegible at 1:50). The member
            # is SetCellTextHeight(row, col, h), set per cell.
            for row_index, row in enumerate(layout.cells):
                table.SetRowHeight(row_index, layout.row_height)
                for column_index, value in enumerate(row):
                    table.SetText(row_index, column_index, value)
                    table.SetCellTextHeight(row_index, column_index, layout.text_height)
            _apply_entity_attrs(table, layer, None, None)
            _attach_xdata(table, prepared)
            _regen(table)
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
            _regen(leader)
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
        xdata=None,
    ) -> EntityInfo:
        return await self._run(
            self._create_hatch_job(pattern, boundary_points, scale, angle, layer, color, xdata)
        )

    def _create_hatch_job(
        self, pattern, boundary_points, scale=1.0, angle=0.0, layer=None, color=None, xdata=None
    ):
        prepared = prepare_xdata(xdata)

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
            _attach_xdata(hatch, prepared)
            _regen(hatch)
            return _entity_info(hatch)

        return _job_named(_sync, "entity_create_hatch")

    async def entity_create_spline(
        self,
        fit_points,
        layer=None,
        color=None,
    ) -> EntityInfo:
        return await self._run(self._create_spline_job(fit_points, layer, color))

    def _create_spline_job(self, fit_points, layer=None, color=None):
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
            _regen(sp)
            return _entity_info(sp)

        return _job_named(_sync, "entity_create_spline")

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
        return await self._run(
            self._create_ellipse_job(cx, cy, major_x, major_y, ratio, layer, color)
        )

    def _create_ellipse_job(self, cx, cy, major_x, major_y, ratio=0.5, layer=None, color=None):
        def _sync():
            mspace = _msp()
            ellipse = mspace.AddEllipse(
                _apoint(cx, cy),
                _apoint(major_x, major_y),  # major axis vector
                float(ratio),  # ratio of minor to major axis
            )
            _apply_entity_attrs(ellipse, layer, color, None)
            _regen(ellipse)
            return _entity_info(ellipse)

        return _job_named(_sync, "entity_create_ellipse")

    async def entity_create_point(
        self,
        x,
        y,
        layer=None,
        color=None,
    ) -> EntityInfo:
        return await self._run(self._create_point_job(x, y, layer, color))

    def _create_point_job(self, x, y, layer=None, color=None):
        def _sync():
            mspace = _msp()
            pt = mspace.AddPoint(_apoint(x, y))
            _apply_entity_attrs(pt, layer, color, None)
            _regen(pt)
            return _entity_info(pt)

        return _job_named(_sync, "entity_create_point")

    # ── bulk tools in one job (1.8-23) ─────────────────────────────────────────

    #: ``entity_batch_create``'s types -> the job builder of each (the same
    #: ten the contract's loop maps to ``entity_create_*``).
    _BATCH_JOBS = {
        "line": "_create_line_job",
        "circle": "_create_circle_job",
        "arc": "_create_arc_job",
        "polyline": "_create_polyline_job",
        "text": "_create_text_job",
        "mtext": "_create_mtext_job",
        "point": "_create_point_job",
        "hatch": "_create_hatch_job",
        "spline": "_create_spline_job",
        "ellipse": "_create_ellipse_job",
    }

    async def entity_batch_create(self, specs, *, progress=None) -> dict:
        """The specs in as few STA jobs as the deadline allows (1.8-23), the contract's shape.

        Before the jobs, on the caller's thread: each spec's type, lineweight
        and arguments are checked by the same job builders the single tools
        use, so a refused spec draws nothing. The planned specs then run in
        slices: one job takes items until their time crosses
        ``_bulk_slice_budget()`` (a quarter of ``COM_CALL_TIMEOUT``), so a
        long but healthy list is never mistaken for a hung AutoCAD - one
        ``_run`` deadline over the whole list would abandon the executor while
        the job kept writing. Per slice: one bound-document guard (and so one
        operator-counter begin / end), one display refresh at its end
        (``_flush_refresh``: one Regen for more than ``COM_REFRESH_UPDATE_MAX``
        entities); per spec a ``try`` whose error row carries the 1.8-02
        ``kind``. A list that fits the budget is one job. A failure in the
        middle leaves the earlier entities written - atomicity is
        ``cad_batch``'s (``transaction=True``) - except a lost connection,
        which ends the call. The call is one revision step however many
        slices it took; it can be cancelled between slices, not inside one.
        """
        total = len(specs)
        if progress is not None:
            await progress(0, total)
        planned: list = []
        errors: list[dict] = []
        for index, raw in enumerate(specs):
            spec = dict(raw)
            kind = spec.pop("type", None)
            if not kind:
                errors.append(
                    {"index": index, "error": "Missing 'type' key", "kind": "invalid_args"}
                )
                continue
            builder = self._BATCH_JOBS.get(str(kind).lower())
            if builder is None:
                errors.append(
                    {"index": index, "error": f"Unknown type: {kind}", "kind": "invalid_args"}
                )
                continue
            try:
                weight = parse_lineweight(spec.pop("lineweight", None))
                job = getattr(self, builder)(**spec)
            except Exception as exc:  # noqa: BLE001 - one spec's refusal is its own row
                errors.append(_batch_error(index, exc, type=kind, before_job=True))
                continue
            planned.append((index, kind, job, weight))

        budget = _bulk_slice_budget()

        def _sync(start):
            doc = _acad_doc()  # the slice's guard: a refusal here wrote nothing
            began = time.perf_counter()
            rows: list[tuple[int, dict]] = []
            failed: list[dict] = []
            position = start
            while position < len(planned):
                index, kind, job, weight = planned[position]
                position += 1
                try:
                    info = job()
                    if weight is not None:
                        _handle_to_object(doc, info.handle).Lineweight = weight
                        info.lineweight_name = lineweight_name(weight)
                except Exception as exc:  # noqa: BLE001 - typed into the item's row
                    _reraise_if_connection_lost(exc)
                    failed.append(_batch_error(index, exc, type=kind))
                else:
                    rows.append((index, asdict(info)))
                if budget is not None and time.perf_counter() - began >= budget:
                    break  # the rest is the next slice, inside its own deadline
            return position, rows, failed

        rows: list[tuple[int, dict]] = []
        position = 0
        while position < len(planned):
            if position and progress is not None:
                await progress(planned[position][0], total)
            position, done, failed = await self._run(_sync, position)
            rows += done
            errors += failed
        errors = sorted(errors, key=lambda row: row["index"])
        return {
            "created": len(rows),
            "errors": errors,
            "entities": [info for _index, info in rows],
        }

    async def entity_delete_many(self, handles, *, progress=None, stop_on_error=False) -> dict:
        """The handles deleted in as few STA jobs as the deadline allows (1.8-23).

        The contract's result shape. Slices as ``entity_batch_create`` does:
        one job deletes until its time crosses ``_bulk_slice_budget()``, so a
        10k-handle cleanup is never one job over ``COM_CALL_TIMEOUT``; per
        slice one guard and one operator-counter begin / end. A handle
        AutoCAD does not know is that item's ``kind: "unknown_handle"``; any
        other failure its 1.8-02 kind. ``stop_on_error`` ends the call at the
        first failure (the all-or-nothing caller rolls everything back
        anyway); a lost connection always does.
        """
        names = list(handles)
        if progress is not None:
            await progress(0, len(names))
        budget = _bulk_slice_budget()

        def _sync(start):
            doc = _acad_doc()  # the slice's guard: a refusal here deleted nothing
            began = time.perf_counter()
            deleted = 0
            errors: list[dict] = []
            position = start
            while position < len(names):
                handle = names[position]
                position += 1
                try:
                    _handle_to_object(doc, handle).Delete()
                except EntityNotFoundError as exc:
                    errors.append({"handle": handle, "error": str(exc), "kind": "unknown_handle"})
                except Exception as exc:  # noqa: BLE001 - typed into the item's row
                    _reraise_if_connection_lost(exc)
                    errors.append(
                        {"handle": handle, "error": str(exc), "kind": classify(exc)["kind"]}
                    )
                else:
                    deleted += 1
                if errors and stop_on_error:
                    return len(names), deleted, errors
                if budget is not None and time.perf_counter() - began >= budget:
                    break  # the rest is the next slice, inside its own deadline
            return position, deleted, errors

        deleted = 0
        errors: list[dict] = []
        position = 0
        while position < len(names):
            if position and progress is not None:
                await progress(position, len(names))
            position, done, failed = await self._run(_sync, position)
            deleted += done
            errors += failed
        return {"deleted": deleted, "errors": errors}

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
            _require_block_defined(_acad_doc(), name)
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
            _regen(ref)
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
        xdata=None,
    ) -> EntityInfo:
        prepared = prepare_xdata(xdata)

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
            _attach_xdata(dim, prepared)
            _regen(dim)
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
        xdata=None,
    ) -> EntityInfo:
        prepared = prepare_xdata(xdata)

        def _sync():
            mspace = _msp()
            dim = mspace.AddDimAligned(_apoint(x1, y1), _apoint(x2, y2), _apoint(dim_x, dim_y))
            if layer:
                dim.Layer = layer
            _attach_xdata(dim, prepared)
            _regen(dim)
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
            _regen(dim)
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
        xdata=None,
    ) -> EntityInfo:
        prepared = prepare_xdata(xdata)

        def _sync():
            mspace = _msp()
            dim = mspace.AddDimRadial(
                _apoint(cx, cy), _apoint(chord_x, chord_y), float(leader_length)
            )
            if layer:
                dim.Layer = layer
            _apply_dim_tolerance(dim, tol_upper, tol_lower, tol_mode, text_override)
            _attach_xdata(dim, prepared)
            _regen(dim)
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
        xdata=None,
    ) -> EntityInfo:
        prepared = prepare_xdata(xdata)

        def _sync():
            mspace = _msp()
            dim = mspace.AddDimDiametric(_apoint(x1, y1), _apoint(x2, y2), float(leader_length))
            if layer:
                dim.Layer = layer
            _apply_dim_tolerance(dim, tol_upper, tol_lower, tol_mode, text_override)
            _attach_xdata(dim, prepared)
            _regen(dim)
            return _entity_info(dim)

        return await self._run(_sync)

    # ── entity modification ───────────────────────────────────────────────────

    async def entity_move(self, handle, dx, dy, dz=0.0) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            ent.Move(_apoint(0, 0, 0), _apoint(dx, dy, dz))
            return {"ok": True, "handle": handle}

        return await self._run(_sync)

    async def entity_copy(self, handle, dx, dy, dz=0.0) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            copy = ent.Copy()
            copy.Move(_apoint(0, 0, 0), _apoint(dx, dy, dz))
            return _entity_info(copy)

        return await self._run(_sync)

    async def entity_rotate(self, handle, base_x, base_y, angle_deg) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            ent.Rotate(_apoint(base_x, base_y), deg2rad(angle_deg))
            return {"ok": True, "handle": handle}

        return await self._run(_sync)

    async def entity_scale(self, handle, base_x, base_y, factor) -> dict:
        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            ent.ScaleEntity(_apoint(base_x, base_y), float(factor))
            dimension = str(ent.ObjectName).endswith("Dimension")
            if dimension:
                ent.Update()  # 1.8-15; whether AutoCAD redraws on its own is S4
            return {"ok": True, "handle": handle}, dimension

        result, dimension = await self._run(_sync)
        if dimension:
            report = _rerender_report([handle], {}, await self._dimension_display([handle]))
            result["rerendered"] = report["rerendered"] + report["already_current"]
            result["stale"] = report["stale"]
        return result

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
            ent = _handle_to_object(doc, handle)
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
            ent = _handle_to_object(doc, handle)

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
            ent = _handle_to_object(doc, handle)
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
                ent = _handle_to_object(doc, handle)
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
                ent = _handle_to_object(doc, handle)
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
            ent = _handle_to_object(doc, handle)
            return _entity_info(ent)

        info = await self._run(_sync)
        if info.type == "HATCH" or "DIMENSION" in info.type:
            await self._detail_from_export(info)
        return info

    async def _detail_from_export(self, info: EntityInfo) -> None:
        """A HATCH's loops and a DIMENSION's fields, read from the drawing's DXF export.

        ActiveX has no loop reader that leaves a non-associative hatch
        untouched and no defpoint reader for every dimension class, so the
        drawing is exported once (``Document.Export``, measured on AutoCAD 2026 -
        ``engineering/understand/snapshot.py``) and the record of this handle is
        read with the same code the headless engine runs. One export per call,
        never per ``entity_list`` row. A member of a block definition is not in
        a layout of the export: it says ``detail_unavailable`` instead.
        """
        from backends.entity_fields import dimension_props, hatch_props
        from engineering.understand import snapshot as snapshot_module

        snap = await snapshot_module.take_snapshot(self)
        wanted = str(info.handle).upper()
        record = next((r for r in snap.records if r.handle.upper() == wanted), None)
        if record is None:
            info.properties["detail_unavailable"] = (
                f"{info.handle} is not in a layout of the exported drawing (a block "
                "definition's member is not read); no loops or defpoints are reported"
            )
            return
        info.properties["detail_source"] = "dxf_export"
        if record.tilted:
            info.properties["tilted"] = True
            return
        if info.type == "HATCH":
            if not record.hatch_loops:  # a hatch has a loop: none means "not read"
                info.properties["detail_unavailable"] = (
                    f"{info.handle}'s boundary loops could not be read from the export; "
                    "no loops or area are reported"
                )
                return
            info.properties.update(hatch_props(record.hatch_loops, record.hatch_style))
        elif record.dimension is not None:
            info.properties["dimension"] = dimension_props(record.dimension)

    async def entity_set_properties(
        self,
        handle,
        layer=None,
        color=None,
        linetype=None,
        lineweight=None,
        visible=None,
    ) -> dict:
        code = parse_lineweight(lineweight)  # refused by name before the job

        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            if layer is not None:
                ent.Layer = layer
            if color is not None:
                ent.color = int(color)
            if linetype is not None:
                _ensure_linetype_loaded(linetype)
                ent.Linetype = linetype
            if code is not None:
                ent.Lineweight = code
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
        x=None,
        y=None,
    ) -> EntityInfo:
        tx, ty = prepare_position(x, y, "entity_edit_text")

        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            name = ent.ObjectName.replace("AcDb", "").upper()
            if name not in ("TEXT", "MTEXT"):
                raise RuntimeError(
                    f"entity_edit_text: handle {handle} is {name}, expected TEXT or MTEXT."
                )
            # refused (tilted plane) before anything else is written
            _com_move_text(ent, handle, tx, ty, "entity_edit_text")
            if text is not None:
                ent.TextString = str(text)
            if height is not None:
                ent.Height = float(height)
            if rotation is not None:
                # MTEXT uses radians for Rotation; DBText/Text uses radians too.
                ent.Rotation = deg2rad(float(rotation))
            _regen(ent)
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
        points=None,
        bulges=None,
        x=None,
        y=None,
    ) -> EntityInfo:
        classic = {
            "cx": cx,
            "cy": cy,
            "radius": radius,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "start_angle": start_angle,
            "end_angle": end_angle,
        }
        if points is not None:
            prepare_points(points, "entity_edit_geometry")  # refused before the job
        tx, ty = prepare_position(x, y, "entity_edit_geometry")

        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            route = edit_geometry_route(_com_dxf_type(ent), classic, points, bulges, x, y)
            if route == "vertices":
                current = points
                if current is None:
                    current = _entity_info(ent).properties.get("points") or []
                pts = prepare_points(current, "entity_edit_geometry")
                checked = prepare_bulges(bulges, len(pts), "entity_edit_geometry")
                return _com_write_vertices(
                    doc, ent, handle, pts, checked, None, "entity_edit_geometry"
                )
            if route == "text":
                _com_move_text(ent, handle, tx, ty, "entity_edit_geometry")
                _regen(ent)
                return _entity_info(ent)
            if route == "dimension":
                # 1.8-15: AcadDimRotated / AcadDimAligned expose their measured
                # points as ExtLine1Point / ExtLine2Point (read / write); the
                # live write and Update() are S4 / smoke_settings_com territory.
                kind = str(ent.ObjectName)
                if kind not in ("AcDbRotatedDimension", "AcDbAlignedDimension"):
                    raise RuntimeError(
                        f"entity_edit_geometry: DIMENSION {handle} is a {kind}; only a linear "
                        "or aligned dimension's measured points (x1, y1, x2, y2) can be edited."
                    )
                for prop, px, py in (("ExtLine1Point", x1, y1), ("ExtLine2Point", x2, y2)):
                    if px is None and py is None:
                        continue
                    old = getattr(ent, prop)
                    setattr(
                        ent,
                        prop,
                        _apoint(
                            float(px) if px is not None else old[0],
                            float(py) if py is not None else old[1],
                        ),
                    )
                ent.Update()
                _regen(ent)
                return _entity_info(ent)
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
                    f"entity_edit_geometry: {name} not supported "
                    "(use CIRCLE, LINE, ARC, LWPOLYLINE, TEXT or MTEXT)."
                )
            _regen(ent)
            return _entity_info(ent)

        return await self._run(_sync)

    async def entity_set_vertices(self, handle, points, bulges=None, closed=None) -> EntityInfo:
        pts, checked = prepare_vertices(points, bulges, "entity_set_vertices")

        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            return _com_write_vertices(
                doc, ent, handle, pts, checked, closed, "entity_set_vertices"
            )

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

                    if type_filter and not _type_filter_matches(type_filter, ent_type):
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
                    if type_filter and not _type_filter_matches(
                        type_filter, ent.ObjectName.replace("AcDb", "").upper()
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
        code = parse_lineweight(lineweight, target="layer")
        if code is None:
            code = -3

        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Add(name)
            lyr.color = int(color)
            _ensure_linetype_loaded(linetype)
            try:
                lyr.Linetype = linetype
            except Exception as exc:
                log.warning("Failed to set linetype '%s' on layer '%s': %s", linetype, name, exc)
            lyr.Lineweight = code
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
        code = parse_lineweight(lineweight, target="layer")

        def _sync():
            doc = _acad_doc()
            lyr = doc.Layers.Item(name)
            if color is not None:
                lyr.color = int(color)
            if linetype is not None:
                _ensure_linetype_loaded(linetype)
                lyr.Linetype = linetype
            if code is not None:
                lyr.Lineweight = code
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

    # ── set-based edits (1.8-12; the grammar is backends/set_edits.py) ──────────

    async def entity_select_where(self, where, set_, space="model") -> dict:
        from backends import set_edits as se

        query, base, scope = se.parse_where(where), se.parse_set(set_), se.parse_space(space)

        def _sync():
            doc = _acad_doc()
            spec, _missing = se.resolve_layer(base, se.com_layer_names(doc))
            found, skipped = se.com_select(doc, query, scope, _com_select_all)
            handles = [str(e.Handle) for e in found]
            return {
                "ok": True,
                "dry_run": True,
                "space": scope,
                "where": query.to_dict(),
                "set": spec.to_dict(),
                "count": len(found),
                "would_change": sum(1 for e in found if se.com_differs(e, spec)),
                "handles": handles[: se.HANDLE_SAMPLE],
                "truncated": len(handles) > se.HANDLE_SAMPLE,
                "skipped_locked": se.skipped_payload(skipped),
            }

        return await self._run(_sync)

    async def entity_modify_where(self, where, set_, space="model", expect_count=None) -> dict:
        from backends import set_edits as se
        from engineering.changes.preview import StaleRevisionError

        query, base, scope = se.parse_where(where), se.parse_set(set_), se.parse_space(space)

        def _sync():
            # One STA job: select, check the count, write - nothing else runs
            # on the document between the three.
            doc = _acad_doc()
            spec, missing = se.resolve_layer(base, se.com_layer_names(doc))
            found, skipped = se.com_select(doc, query, scope, _com_select_all)
            if expect_count is not None and len(found) != int(expect_count):
                raise StaleRevisionError(
                    f"stale_revision: expect_count={int(expect_count)} but where now matches "
                    f"{len(found)} entities; nothing was changed. Run the dry run again.",
                    changed=[
                        {"count": "where", "expected": int(expect_count), "actual": len(found)}
                    ],
                )
            changing = [e for e in found if se.com_differs(e, spec)]
            created = []
            if missing and changing:
                # ActiveX will not create a layer on assignment (Key not found).
                doc.Layers.Add(spec.layer)
                created.append(spec.layer)
            if spec.linetype is not None and changing:
                _ensure_linetype_loaded(spec.linetype)
            for entity in changing:
                se.com_write(entity, spec)
            if changing:
                _regen()
            handles = [str(e.Handle) for e in changing]
            return {
                "ok": True,
                "space": scope,
                "where": query.to_dict(),
                "set": spec.to_dict(),
                "matched": len(found),
                "changed": len(changing),
                "unchanged": len(found) - len(changing),
                "handles": handles[: se.HANDLE_SAMPLE],
                "truncated": len(handles) > se.HANDLE_SAMPLE,
                "skipped_locked": se.skipped_payload(skipped),
                "layers_created": created,
                "changes": se.changes_payload(modified=len(changing)),
            }

        return await self._run(_sync)

    async def layer_rename(self, old, new) -> dict:
        from backends import set_edits as se

        def _sync():
            doc = _acad_doc()
            stored, target = se.rename_checks(old, new, se.com_layer_names(doc))
            doc.Layers.Item(stored).Name = target
            return {
                "ok": True,
                "old": stored,
                "new": target,
                # AutoCAD references a layer by object id: renaming the table
                # record renames it everywhere and rewrites no entity.
                "entities": None,
                "by_space": None,
                "blocks_touched": None,
                "note": "Live: entities reference the layer by object id; none was rewritten.",
                "current_layer": str(doc.ActiveLayer.Name),
                "changes": se.changes_payload(modified=0, layers_renamed=1),
            }

        return await self._run(_sync)

    async def layer_merge(self, sources, target, delete_sources=True) -> dict:
        from backends import set_edits as se

        target = se.merge_target(target)
        names = se.merge_sources(sources, target)

        def _sync():
            doc = _acad_doc()
            table = {}
            for index in range(doc.Layers.Count):
                layer = doc.Layers.Item(index)
                table[str(layer.Name).lower()] = layer
            missing = [n for n in (*names, target) if n.lower() not in table]
            se.refuse_missing("layer_merge", missing, [str(lyr.Name) for lyr in table.values()])
            stored = tuple(str(table[n.lower()].Name) for n in names)
            goal = str(table[target.lower()].Name)
            keys = {n.lower() for n in stored}
            # A locked layer refuses every write to its entities; unlock the
            # sources (about to go) and the target for the move, relock after.
            relock = [n for n in (*stored, goal) if bool(table[n.lower()].Lock)]
            for name in relock:
                table[name.lower()].Lock = False
            deleted: list[str] = []
            not_deleted: list[dict] = []
            try:
                count, by_space, blocks = se.com_reassign(doc, keys, goal, _com_select_all)
                moved_current = str(doc.ActiveLayer.Name).lower() in keys
                if moved_current:
                    doc.ActiveLayer = table[goal.lower()]
                if delete_sources:
                    for name in stored:
                        try:
                            table[name.lower()].Delete()
                            deleted.append(name)
                        except Exception as exc:  # reported per layer, never swallowed
                            not_deleted.append({"layer": name, "reason": str(exc)})
            finally:
                for name in relock:
                    if name not in deleted:
                        try:
                            table[name.lower()].Lock = True
                        except Exception as exc:  # noqa: BLE001 - logged; the merge stands
                            log.warning("layer_merge: could not relock %r: %s", name, exc)
            if count:
                _regen()
            return {
                "ok": True,
                "sources": list(stored),
                "target": goal,
                "reassigned": count,
                "by_space": by_space,
                "blocks_touched": blocks,
                "note": se.block_note(blocks),
                "layers_deleted": deleted,
                "layers_not_deleted": not_deleted,
                "current_layer": str(doc.ActiveLayer.Name),
                "changes": se.changes_payload(modified=count, layers_deleted=len(deleted)),
            }

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
        #
        # Both arguments are checked before the COM thread is entered: the
        # name against the DXF symbol-table rule, the path against the same
        # allowlist every other file-taking tool uses. This was the one
        # path-taking tool that reached the filesystem with neither (and,
        # until v1.6, both went into a SendCommand macro).
        safe_name = sanitize_symbol_name(name, kind="linetype")
        safe_file = None
        if file is not None:
            resolved = validate_path(str(file))
            safe_file = sanitize_macro_argument(str(resolved), kind="linetype file")

        def _sync():
            doc = _acad_doc()

            existing = {doc.Linetypes.Item(i).Name.lower() for i in range(doc.Linetypes.Count)}
            if safe_name.lower() in existing:
                return {"ok": True, "name": safe_name, "already_loaded": True}

            # The default file follows MEASUREMENT, read on the document; the
            # load itself is ``Linetypes.Load`` (see _load_linetype for why
            # the -LINETYPE macro behind FILEDIA=0 is gone).
            lin_file = _default_lin_file(doc) if safe_file is None else safe_file
            _load_linetype(doc, safe_name, lin_file)

            after = {doc.Linetypes.Item(i).Name.lower() for i in range(doc.Linetypes.Count)}
            if safe_name.lower() not in after:
                raise RuntimeError(
                    f"Failed to load linetype '{safe_name}' from '{lin_file}'. "
                    "Check the linetype name spelling and that the .lin file is "
                    "on AutoCAD's support path."
                )
            return {"ok": True, "name": safe_name, "file": lin_file}

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
        from backends.block_specs import validate_attribute_values

        values = validate_attribute_values(attributes)

        def _sync():
            _require_block_defined(_acad_doc(), name)
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
            if values:
                try:
                    attrs = ref.GetAttributes()
                    for attr in attrs:
                        tag = attr.TagString
                        # `values` is keyed upper-case: tags are not case-sensitive.
                        if tag.upper() in values:
                            attr.TextString = values[tag.upper()]
                except Exception as exc:
                    log.debug("Block insert: GetAttributes or attribute setting failed: %s", exc)
            return _entity_info(ref)

        return await self._run(_sync)

    #: ActiveX twins of the headless ``_ATTRIB_TO_TEXT`` carry, written onto
    #: the TEXT in this order and *before* ``Rotation``: ``Normal`` is the
    #: frame ``Rotation`` is measured in, ``Thickness`` rides on the frame,
    #: and the style members are frame-independent. ``Alignment`` /
    #: ``TextAlignmentPoint`` (halign/valign + align_point) and ``Color`` are
    #: handled apart because ActiveX orders them against the insertion point.
    _ATTRIB_FRAME_MEMBERS = (
        "Normal",
        "Thickness",
        "StyleName",
        "ObliqueAngle",
        "ScaleFactor",
        "Backward",
        "UpsideDown",
    )
    _ATTRIB_TEXT_MEMBERS = _ATTRIB_FRAME_MEMBERS + ("Alignment", "TextAlignmentPoint", "color")

    @classmethod
    def _capture_attrib_text_members(cls, attr) -> dict:
        """Read the optional ATTRIB members a TEXT can take, skipping absent ones.

        ``Normal`` is a WCS unit vector and is only worth carrying when it is
        not already +Z; ``TextAlignmentPoint`` is meaningless (and refused by
        ActiveX on write) for a left-aligned text, so it is dropped when
        ``Alignment`` is ``acAlignmentLeft`` (0).
        """
        out: dict = {}
        for name in cls._ATTRIB_TEXT_MEMBERS:
            try:
                raw = getattr(attr, name)
            except Exception:
                continue
            if name in ("Normal", "TextAlignmentPoint"):
                try:
                    raw = tuple(float(v) for v in raw)
                except (TypeError, ValueError):
                    continue
                if name == "Normal" and ocs.is_wcs_frame(raw):
                    continue
            elif name in ("Alignment", "color"):
                try:
                    raw = int(raw)
                except (TypeError, ValueError):
                    continue
            elif name in ("Backward", "UpsideDown"):
                raw = bool(raw)
            elif name == "StyleName":
                raw = str(raw)
            else:
                try:
                    raw = float(raw)
                except (TypeError, ValueError):
                    continue
            out[name] = raw
        if out.get("Alignment", 0) == 0:
            out.pop("TextAlignmentPoint", None)
        return out

    #: ``AcadText.Alignment`` -> ``AcadMText.AttachmentPoint``, the inverse of
    #: the map ezdxf applies when it embeds an MTEXT into an ATTRIB (the
    #: attachment becomes ``halign``/``valign`` and both ``insert`` and
    #: ``align_point`` become the MTEXT insert). Rows: Top 1-3, Middle 4-6,
    #: Bottom 7-9; the baseline alignments (Left/Center/Right/Aligned/Middle/
    #: Fit), which an embedded MTEXT never produces, fall to the nearest row.
    _TEXT_ALIGNMENT_TO_MTEXT_ATTACHMENT = {
        0: 7,  # acAlignmentLeft -> BottomLeft
        1: 8,  # acAlignmentCenter -> BottomCenter
        2: 9,  # acAlignmentRight -> BottomRight
        3: 7,  # acAlignmentAligned -> BottomLeft
        4: 5,  # acAlignmentMiddle -> MiddleCenter
        5: 7,  # acAlignmentFit -> BottomLeft
        6: 1,  # acAlignmentTopLeft
        7: 2,  # acAlignmentTopCenter
        8: 3,  # acAlignmentTopRight
        9: 4,  # acAlignmentMiddleLeft
        10: 5,  # acAlignmentMiddleCenter
        11: 6,  # acAlignmentMiddleRight
        12: 7,  # acAlignmentBottomLeft
        13: 8,  # acAlignmentBottomCenter
        14: 9,  # acAlignmentBottomRight
    }

    @classmethod
    def _capture_attrib(cls, attr) -> dict:
        """Everything a TEXT (or MTEXT) needs to stand in for ``attr`` (an
        ATTRIB or ATTDEF).

        A multi-line attribute (``MTextAttribute`` true) keeps its content in
        ``MTextAttributeContent`` -- ``TextString`` is one flattened string --
        and its box width in ``MTextBoundaryWidth``; ``mtext`` marks it so the
        writer builds an MTEXT. An object that does not expose *that* member
        is single-line (the member is younger than the ActiveX surface, and a
        ``TextString`` fallback loses nothing).

        ``Invisible`` is *not* guessed: a read failure used to default to
        visible, so a hidden attribute's value (a link, a cost code) became
        visible text with ``ok: True``. The failure propagates, and the
        caller of this helper reads it before anything is written.
        """
        invisible = bool(attr.Invisible)
        try:
            is_mtext = bool(attr.MTextAttribute)
        except Exception:
            is_mtext = False
        item = {
            "text": str(attr.TextString),
            "mtext": is_mtext,
            "insertion": tuple(attr.InsertionPoint),
            "height": float(attr.Height),
            "rotation": float(attr.Rotation),
            "layer": str(attr.Layer),
            "invisible": invisible,
            "members": cls._capture_attrib_text_members(attr),
        }
        if is_mtext:
            try:
                item["text"] = str(attr.MTextAttributeContent)
            except Exception as exc:
                log.debug("MTextAttributeContent unreadable, keeping TextString: %s", exc)
            try:
                item["width"] = float(attr.MTextBoundaryWidth)
            except Exception:
                item["width"] = 0.0  # unbounded box, as AddMText takes it
        return item

    @classmethod
    def _add_mtext_from_capture(cls, owner, item: dict):
        """``owner.AddMText`` carrying a captured multi-line ATTRIB/ATTDEF.

        The MTEXT's insertion point *is* its attachment anchor, so it is the
        ATTRIB's ``TextAlignmentPoint`` when the attribute is box-aligned and
        the ``InsertionPoint`` otherwise (ezdxf writes both from the embedded
        MTEXT's insert, so they agree). Height, style and frame go on before
        ``Rotation``, then the attachment (the ATTRIB's ``Alignment`` mapped
        through ``_TEXT_ALIGNMENT_TO_MTEXT_ATTACHMENT``), and the WCS anchor
        is written *last*: ActiveX ``AttachmentPoint`` keeps the text body
        where it is and relocates ``InsertionPoint`` to the new corner, so an
        anchor written before it left every non-TopLeft attribute laid out
        TopLeft-at-anchor with only the label moved (verified live on AutoCAD
        2026: a BottomLeft two-line note landed two lines low; re-asserting
        the anchor after the attachment moves the body). A frame change
        (``Normal``) moves the OCS origin the same way, so the one late write
        covers both. ``Thickness``, oblique, width factor and the generation
        flags are TEXT-only members and stay behind.
        """
        members = item["members"]
        anchor = item["insertion"]
        if members.get("Alignment", 0) != 0 and "TextAlignmentPoint" in members:
            anchor = members["TextAlignmentPoint"]
        point = _apoint(anchor[0], anchor[1], anchor[2] if len(anchor) > 2 else 0.0)
        mtext = owner.AddMText(point, float(item.get("width", 0.0)), item["text"])
        mtext.Height = item["height"]
        if "StyleName" in members:
            mtext.StyleName = members["StyleName"]
        if "Normal" in members:
            value = members["Normal"]
            mtext.Normal = _apoint(value[0], value[1], value[2] if len(value) > 2 else 0.0)
        mtext.Rotation = item["rotation"]
        mtext.AttachmentPoint = cls._TEXT_ALIGNMENT_TO_MTEXT_ATTACHMENT.get(
            members.get("Alignment", 0), 1
        )
        mtext.InsertionPoint = point  # after the attachment and the frame: both relocate it
        mtext.Layer = item["layer"]
        if "color" in members:
            mtext.color = members["color"]
        if item["invisible"]:
            mtext.Visible = False
        return mtext

    @classmethod
    def _add_text_from_capture(cls, owner, item: dict):
        """``owner.AddText`` carrying a captured ATTRIB/ATTDEF; returns the TEXT
        (or, for a multi-line capture, the MTEXT ``_add_mtext_from_capture``
        builds -- a TEXT of ``TextString`` would show ``\\P`` literally).

        Every point or vector goes through ``_apoint`` (``VT_ARRAY|VT_R8``):
        ActiveX refuses a plain Python tuple (marshalled ``VT_ARRAY|VT_VARIANT``)
        with ``E_INVALIDARG``, and ``Normal`` is written after the explode and
        the ATTDEF deletes, so a tuple there tore the drawing.
        """
        if item.get("mtext"):
            return cls._add_mtext_from_capture(owner, item)
        ins = item["insertion"]
        point = _apoint(ins[0], ins[1], ins[2] if len(ins) > 2 else 0.0)
        text = owner.AddText(item["text"], point, item["height"])
        members = item["members"]
        for name in cls._ATTRIB_FRAME_MEMBERS:
            if name not in members:
                continue
            value = members[name]
            if name == "Normal":
                value = _apoint(value[0], value[1], value[2] if len(value) > 2 else 0.0)
            setattr(text, name, value)
        text.Rotation = item["rotation"]
        if "Normal" in members:
            # The frame moved the OCS origin; the anchor was WCS.
            text.InsertionPoint = point
        if "Alignment" in members:
            text.Alignment = members["Alignment"]
            if "TextAlignmentPoint" in members:
                tap = members["TextAlignmentPoint"]
                text.TextAlignmentPoint = _apoint(tap[0], tap[1], tap[2] if len(tap) > 2 else 0.0)
        text.Layer = item["layer"]
        if "color" in members:
            text.color = members["color"]
        if item["invisible"]:
            text.Visible = False
        return text

    @staticmethod
    def _undo_explode(exploded, texts) -> None:
        """Delete what a failed burst has already written (best effort).

        An object the burst deleted itself (an ATTDEF placeholder) raises on a
        second ``Delete``; that is skipped, every other failure is logged and
        the next object is tried, so the original exception -- the one the
        caller is about to re-raise -- stays the one the caller sees.
        """
        for obj in tuple(texts) + tuple(exploded):
            try:
                obj.Delete()
            except Exception as exc:
                log.debug("explode rollback: %r not deleted: %s", obj, exc)

    @staticmethod
    def _is_constant_attdef(obj, constant_tags: set) -> bool:
        """Whether an exploded ``AcDbAttributeDefinition`` is a constant attribute.

        ``Constant`` is the documented member; when the object does not expose
        it the tag is matched against ``GetConstantAttributes()`` read before
        the explode, so a constant attribute is never mistaken for a value-less
        placeholder and deleted.
        """
        try:
            return bool(obj.Constant)
        except Exception:
            pass
        return str(obj.TagString) in constant_tags

    async def block_explode(self, handle) -> dict:
        """Explode an INSERT through ActiveX; ATTRIB values survive as TEXT.

        AutoCAD's EXPLODE keeps the attribute *definitions* (the tag names as
        ATTDEF entities) and discards the values; ActiveX ``Explode()`` does
        the same and, unlike the command, leaves the original reference in
        place. This is the BURST rule instead: each ATTRIB's value, placement,
        height, rotation, layer, frame and text style are read before the
        explode, the ATTDEFs the explode returns are deleted, one ``AddText``
        per ATTRIB carries the value (an invisible ATTRIB becomes an invisible
        TEXT), and the reference itself is deleted. Unit-tested against a fake
        ActiveX surface; ``Explode()``'s return shape is the ActiveX documented
        one (an array of the new objects) and is exercised live by the
        settings smoke.

        A *constant* attribute has no ATTRIB: ``GetAttributes()`` excludes it
        (``GetConstantAttributes()`` lists it) and ``Explode()`` hands it back
        as an ``AcDbAttributeDefinition`` already at its WCS placement showing
        its value. Deleting every ATTDEF therefore destroyed the constant text
        with ``ok: True``; BURST converts it to TEXT, so a constant ATTDEF from
        the explode is captured the same way an ATTRIB is, then replaced by a
        TEXT, and only the value-less placeholders are just deleted.

        An ATTRIB is an OCS entity and ``AddText`` builds a +Z TEXT, so the
        frame is carried the way the headless engine carries ``extrusion`` /
        ``thickness``: ``Normal`` is written *before* ``Rotation`` (the
        group-50 angle only means the same thing inside the same frame -- a
        headless-authored mirrored reference's ATTRIB sits on ``(0, 0, -1)``;
        AutoCAD's own MIRROR keeps +Z with ``XScaleFactor -1``) and the WCS
        ``InsertionPoint`` is re-asserted *after* it, because a frame change
        moves the OCS origin under a point that was handed over in WCS. The
        members ezdxf's ``_ATTRIB_TO_TEXT`` names have their ActiveX twins in
        ``_ATTRIB_TEXT_MEMBERS``; each is optional and skipped when the object
        does not expose it, never written as a guess. ``Normal`` is written as
        a ``VT_ARRAY|VT_R8`` VARIANT like every other point in this file; a
        plain tuple is ``E_INVALIDARG`` live (verified on AutoCAD 2026).

        A *multi-line* attribute (``MTextAttribute`` true) is captured from
        ``MTextAttributeContent`` and written back with ``AddMText`` -- its
        ``TextString`` is one flattened line and a TEXT of it would show the
        ``\\P`` breaks literally -- see ``_add_mtext_from_capture``.

        The TEXTs go into the reference's *owner* block
        (``ObjectIdToObject(OwnerID)``), which is where ``Explode()`` puts the
        members; ``HandleToObject`` resolves a handle in any layout, so a title
        block on a sheet exploded while Model was active used to get its tag
        text in model space with ``ok: True``. Refused before the explode is
        dispatched: a reference nested inside a block definition (owner
        ``IsLayout`` false), a MINSERT (``AcDbMInsertBlock``, which used to
        fail the INSERT check with the misleading "not a block reference"),
        and an xref (``Blocks.Item(Name).IsXRef``; ActiveX ``Explode()`` raises
        on one, the headless engine used to delete it) -- the same three
        refusals the headless engine names.

        No read that decides what gets written is guessed: ``GetAttributes()``,
        ``GetConstantAttributes()`` and each attribute's ``Invisible`` are read
        before ``Explode()`` and a failure propagates with nothing written
        (a swallowed ``GetAttributes()`` used to burst *no* value and report
        ``ok: True``). A failure after the explode -- a constant ATTDEF whose
        members cannot be read, an ``AddText`` that is refused -- undoes what
        the explode added and the TEXTs written so far and leaves the
        reference in place (``_undo_explode``), so the drawing is never left
        half-burst under an exception.
        """

        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, handle)
            if ent.ObjectName == "AcDbMInsertBlock":
                rows = _int_member(ent, "Rows")
                cols = _int_member(ent, "Columns")
                grid = f"{rows}x{cols} grid" if rows and cols else "grid"
                raise RuntimeError(
                    f"Entity {handle} is a MINSERT ({grid} of {str(ent.Name)!r}); a "
                    "multi-insert cannot be exploded"
                )
            if ent.ObjectName != "AcDbBlockReference":
                raise RuntimeError(f"Entity {handle} is not a block reference (INSERT)")
            name = str(ent.Name)
            try:
                is_xref = bool(doc.Blocks.Item(name).IsXRef)
            except Exception as exc:
                log.debug("IsXRef probe failed for %r, treating as a local block: %s", name, exc)
                is_xref = False
            if is_xref:
                raise RuntimeError(
                    f"Entity {handle} is an external reference {name!r}; an xref cannot "
                    "be exploded (bind it into the drawing first)"
                )
            owner = _owner_layout_block(doc, ent, handle)
            # Every read that decides what the burst writes happens *before*
            # ``Explode()`` and propagates: swallowing a failing
            # ``GetAttributes()`` (a transient ``RPC_E_CALL_REJECTED`` while
            # AutoCAD is busy is enough) went on to explode, delete every
            # ATTDEF placeholder and the reference, and report ``ok: True``
            # with ``attribute_texts: []`` -- every value destroyed silently,
            # the defect this method exists to remove. Nothing has been
            # written yet, so raising here costs nothing.
            attrs = ent.GetAttributes()
            captured = [self._capture_attrib(attr) for attr in attrs]
            constant_tags: set = {str(attr.TagString) for attr in ent.GetConstantAttributes() or ()}
            exploded = tuple(ent.Explode() or ())
            inserted = []
            attribute_texts = []
            texts = []
            try:
                for obj in exploded:
                    if obj.ObjectName == "AcDbAttributeDefinition":
                        if self._is_constant_attdef(obj, constant_tags):
                            # A constant attribute's only text; already WCS here.
                            captured.append(self._capture_attrib(obj))
                        obj.Delete()  # the tag placeholder EXPLODE leaves; the value is below
                        continue
                    inserted.append(str(obj.Handle))
                for item in captured:
                    text = self._add_text_from_capture(owner, item)
                    texts.append(text)
                    attribute_texts.append(str(text.Handle))
            except Exception:
                # ActiveX ``Explode()`` leaves the reference in place, so the
                # drawing is put back to exactly that: the members it added and
                # the TEXTs written so far are removed, the reference stays,
                # and the failure propagates instead of a half-burst symbol.
                self._undo_explode(exploded, texts)
                raise
            ent.Delete()
            _regen()
            return {
                "ok": True,
                "exploded_handle": handle,
                "inserted_handles": inserted,
                "attribute_texts": attribute_texts,
                "backend": "com",
            }

        return await self._run(_sync)

    async def block_get_attributes(self, handle) -> dict:
        def _sync():
            doc = _acad_doc()
            ref = _handle_to_object(doc, handle)
            attrs = ref.GetAttributes()
            result = {}
            for attr in attrs:
                result[attr.TagString] = attr.TextString
            return result

        return await self._run(_sync)

    async def block_set_attributes(self, handle, attributes) -> dict:
        from backends.block_specs import validate_attribute_values

        values = validate_attribute_values(attributes)

        def _sync():
            doc = _acad_doc()
            ref = _handle_to_object(doc, handle)
            attrs = ref.GetAttributes()
            updated = []
            for attr in attrs:
                tag = attr.TagString
                # `values` is keyed upper-case: tags are not case-sensitive.
                if tag.upper() in values:
                    attr.TextString = values[tag.upper()]
                    updated.append(tag)
            return {"ok": True, "updated_tags": updated}

        return await self._run(_sync)

    # ── extended entity data (XDATA) ─────────────────────────────────────────

    async def entity_get_xdata(self, handle, app_name=None) -> dict:
        from backends.xdata_specs import split_by_app, validate_app_name

        wanted = validate_app_name(app_name) if app_name else ""

        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, str(handle).strip().upper())
            # GetXData("") returns every app's stream with the 1001 markers
            # inline; a named app returns just that app's slice.
            codes, values = ent.GetXData(wanted)
            grouped = split_by_app(list(codes or []), list(values or []))
            if wanted:
                grouped = {k: v for k, v in grouped.items() if k == wanted}
            return {"handle": handle, "xdata": grouped, "backend": "com"}

        return await self._run(_sync)

    async def entity_set_xdata(self, handle, app_name, values) -> dict:
        from backends.xdata_specs import (
            app_size,
            check_entity_budget,
            encode_values,
            split_tags_by_app,
            validate_app_name,
        )

        # Validate and type every value before any ActiveX call: a bad
        # values[i] raises here and nothing is written.
        app = validate_app_name(app_name)
        tags = encode_values(values)

        def _sync():
            doc = _acad_doc()
            ent = _handle_to_object(doc, str(handle).strip().upper())
            if tags:
                # The 16 KB limit is per entity across every application, the
                # same gate the headless engine runs: read what is already
                # there and refuse before the APPID is registered or SetXData
                # gets the chance to fail half-way with AutoCAD's own error.
                codes, values_ = ent.GetXData("")
                existing = split_tags_by_app(list(codes or []), list(values_ or []))
                check_entity_budget(app, tags, existing)
            try:
                doc.RegisteredApplications.Add(app)
            except Exception as exc:  # already registered
                log.debug("RegisteredApplications.Add(%s): %s", app, exc)
            # A bare 1001 marker with no data is how ActiveX removes an app's
            # XDATA, so the empty case is the same call with an empty tail.
            codes = [1001] + [code for code, _ in tags]
            payload = [app] + [_apoint(*value) if code == 1010 else value for code, value in tags]
            ent.SetXData(
                _ai(codes),
                win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, payload),
            )
            return {
                "ok": True,
                "handle": handle,
                "app_name": app,
                "value_count": len(tags),
                "bytes": app_size(app, tags) if tags else 0,
                "removed": not tags,
                "backend": "com",
            }

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
                raise EntityNotFoundError(
                    ", ".join(skipped),
                    f"block_create_from_entities: none of the handles resolved "
                    f"({', '.join(skipped) or 'no handles given'}), so there is nothing "
                    f"to put in block {name!r}. No definition was created.",
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

    async def block_define(
        self,
        name,
        entities,
        attdefs=None,
        base_x=0.0,
        base_y=0.0,
        overwrite=False,
        create_layers=False,
    ) -> dict:
        """A block definition with ATTDEFs from typed specs, through ActiveX.

        ``Blocks.Add`` creates the definition; each primitive is one ``Add*``
        call on the Block object, each ATTDEF one ``AddAttribute``. Overwrite
        deletes the existing definition's members and re-adds — the name and
        its INSERTs survive. The base point must be finite (a NaN would reach
        AutoCAD as a VARIANT) and every primitive layer must exist
        (``Layers.Item`` probe before ``Blocks.Add`` — ``obj.Layer = name`` for
        an unknown layer would otherwise fail mid-loop with the definition half
        written) unless ``create_layers`` adds them first. Unit-tested against
        a fake ActiveX surface; executed live by ``scripts/smoke_pid_com.py``.
        """
        from backends.block_specs import (
            referenced_layers,
            solid_vertices,
            validate_attdef_specs,
            validate_base_point,
            validate_block_name,
            validate_entity_specs,
        )

        # The anonymous-name rule comes first: ``*`` is also a forbidden DXF
        # symbol character, so ``sanitize_symbol_name`` would refuse ``*U9``
        # with the generic message before this one could name the real reason.
        validate_block_name(name)
        if name.strip().startswith("*"):
            raise ValueError("block_define: anonymous block names (*...) are refused")
        clean_name = sanitize_symbol_name(name, kind="block")
        ents = validate_entity_specs(entities)
        atts = validate_attdef_specs(attdefs)
        base_xy = validate_base_point(base_x, base_y)
        wanted_layers = referenced_layers(ents)
        # acAlignmentLeft / Center / Right / MiddleCenter
        align_map = {"left": 0, "center": 1, "right": 2, "middle_center": 10}

        def _style(obj, layer: str) -> None:
            obj.Layer = layer
            obj.color = 0  # acByBlock
            obj.Linetype = "ByBlock"
            # acLnWtByBlock; measured accepted on AcDbLine and an
            # AcDbAttributeDefinition on AutoCAD 2026 (reads back -2).
            obj.Lineweight = -2

        def _sync():
            doc = _acad_doc()
            try:
                existing = doc.Blocks.Item(clean_name)
            except Exception:
                existing = None
            if existing is not None and not overwrite:
                raise ValueError(
                    f"block_define: block {clean_name!r} already exists; "
                    "pass overwrite=true to replace its contents"
                )
            replaced = existing is not None
            missing_layers = []
            # ``Layers.Item`` matches case-insensitively, like the table itself.
            for layer in wanted_layers:
                try:
                    doc.Layers.Item(layer)
                except Exception:
                    missing_layers.append(layer)
            if missing_layers and not create_layers:
                raise ValueError(
                    "block_define: layer(s) "
                    + ", ".join(repr(layer) for layer in missing_layers)
                    + " do not exist in the drawing; create them with layer_create "
                    "or pass create_layers=true"
                )
            for layer in missing_layers:
                doc.Layers.Add(layer)
            base = _apoint(base_xy[0], base_xy[1], 0.0)
            if existing is None:
                block = doc.Blocks.Add(base, clean_name)
            else:
                block = existing
                members = [block.Item(i) for i in range(block.Count)]
                for member in members:
                    member.Delete()
                block.Origin = base
            for spec in ents:
                kind = spec["type"]
                if kind == "line":
                    obj = block.AddLine(
                        _apoint(spec["x1"], spec["y1"]), _apoint(spec["x2"], spec["y2"])
                    )
                elif kind == "circle":
                    obj = block.AddCircle(_apoint(spec["cx"], spec["cy"]), float(spec["r"]))
                elif kind == "arc":
                    obj = block.AddArc(
                        _apoint(spec["cx"], spec["cy"]),
                        float(spec["r"]),
                        deg2rad(spec["start_deg"]),
                        deg2rad(spec["end_deg"]),
                    )
                elif kind == "polyline":
                    flat = [coord for pt in spec["points"] for coord in pt]
                    obj = block.AddLightWeightPolyline(_av(flat))
                    obj.Closed = bool(spec["closed"])
                    for index, bulge in enumerate(spec["bulges"] or []):
                        if bulge:
                            obj.SetBulge(index, float(bulge))
                elif kind == "text":
                    obj = block.AddText(
                        spec["text"], _apoint(spec["x"], spec["y"]), float(spec["height"])
                    )
                    obj.Rotation = deg2rad(spec["rotation_deg"])
                    if spec["align"] != "left":
                        obj.Alignment = align_map[spec["align"]]
                        obj.TextAlignmentPoint = _apoint(spec["x"], spec["y"])
                elif kind == "solid":
                    verts = solid_vertices(spec["points"])
                    verts = verts + [verts[-1]] * (4 - len(verts))
                    obj = block.AddSolid(*[_apoint(x, y) for x, y in verts])
                _style(obj, spec["layer"])
            for att in atts:
                mode = 1 if att["invisible"] else 0  # acAttributeModeInvisible / Normal
                obj = block.AddAttribute(
                    float(att["height"]),
                    mode,
                    att["prompt"],
                    _apoint(att["x"], att["y"]),
                    att["tag"],
                    att["default"],
                )
                obj.Rotation = deg2rad(att["rotation_deg"])
                if att["align"] != "left":
                    obj.Alignment = align_map[att["align"]]
                    obj.TextAlignmentPoint = _apoint(att["x"], att["y"])
                _style(obj, "0")
            _regen()
            return {
                "ok": True,
                "name": clean_name,
                "entity_count": len(ents),
                "attdef_count": len(atts),
                "replaced": replaced,
                "layers_created": missing_layers,
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
            ent = _handle_to_object(_acad_doc(), str(handle))
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
            # Measured on a live AutoCAD 2026 (2026-08-06): the perimeter lives
            # under a different member per type, and on some types nowhere.
            # AcDbPolyline -> .Length, AcDbCircle -> .Circumference,
            # AcDbRegion -> .Perimeter, AcDbHatch -> none of them.
            for member in ("Length", "Circumference", "Perimeter"):
                try:
                    perimeter = float(getattr(ent, member))
                    break
                except Exception as exc:
                    log.debug("ActiveX .%s unavailable for %s: %s", member, obj_name, exc)

            method = "activex_area"
            closed = True
            self_intersecting = None
            # Only the AREA decides whether we need the fallback. This used to
            # read `area is None or perimeter is None`, which coupled two
            # independent property reads: HATCH, REGION and 3DSOLID expose
            # `.Area` but no `.Length`, so a perfectly good area was thrown
            # away, the fallback demanded a `.Coordinates` only the polyline
            # types carry, and the call died claiming AutoCAD "reported neither
            # an area nor usable vertices" — while holding the area.
            if area is None:
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
                    if obj_name in _COM_NO_SURFACE_AREA_TYPES:
                        # Verified on a live AutoCAD 2026 (2026-08-06): AcDb3dSolid
                        # exposes `.Volume` and no `.Area` whatsoever. The generic
                        # message would send the caller hunting for geometry that
                        # was never the problem.
                        raise RuntimeError(
                            f"entity_measure: handle {handle} is {obj_name}, and ActiveX "
                            "exposes no surface-area member for it — only .Volume. "
                            "AutoCAD's own MASSPROP reports the surface area; this server "
                            "does not wrap it."
                        )
                    raise RuntimeError(
                        f"entity_measure: AutoCAD reported neither an area nor usable "
                        f"vertices for handle {handle} ({obj_name})."
                    )
                closed = bool(getattr(ent, "Closed", True))
                fallback_area, fallback_perimeter = polygon_area_perimeter(vertices, closed)
                area = fallback_area
                perimeter = fallback_perimeter if perimeter is None else perimeter
                self_intersecting = is_self_intersecting([(v[0], v[1]) for v in vertices])

            elif self_intersecting is None:
                # The bowtie warning was only ever computed inside the fallback,
                # so a live LWPOLYLINE bowtie — which has both `.Area` and
                # `.Length` and therefore never reaches it — came back
                # `area: 0.0, self_intersecting: null`. That is precisely the
                # confident-zero this field exists to flag, going out unflagged.
                coords = list(getattr(ent, "Coordinates", ()) or ())
                if len(coords) >= 6:
                    self_intersecting = is_self_intersecting(
                        [
                            (float(coords[i]), float(coords[i + 1]))
                            for i in range(0, len(coords) - 1, 2)
                        ]
                    )

            # A perimeter we do not have is reported as absent, not invented.
            # `round(None, 6)` was also a TypeError waiting for the first type
            # that answers `.Area` and nothing else.
            return {
                "handle": str(handle),
                "type": obj_name,
                "area": round(float(area), 6),
                "perimeter": None if perimeter is None else round(float(perimeter), 6),
                "closed": closed,
                "assumed_closed": not closed,
                "method": method,
                "exact": True,
                "flatten_tolerance": None,
                "backend": "com",
                "self_intersecting": self_intersecting,
                "perimeter_exact": perimeter is not None,
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
        """The AutoCAD window - of the bound document, or refused (B11).

        A screenshot is evidence about this session's document, so it is
        guarded like any read: checked before the capture, and checked again
        after it, because the pixels come from whatever drawing the window
        shows and an operator's tab click during the capture would hand back
        another drawing's image. With nothing bound and nothing open the empty
        application window is captured as before.
        """

        def _sync():
            app = _acad_app()
            guarded = self._bound is not None or int(app.Documents.Count) > 0
            if guarded:
                self._guard(app)  # bound with nothing open is refused typed, too
            hwnd = _find_autocad_hwnd()
            if hwnd is None:
                return None
            image = _capture_window(hwnd)
            if guarded:
                self._guard(app)
            return image

        return await self._run(_sync)

    async def view_render(
        self,
        region: list[float] | None = None,
        max_px: int = DEFAULT_MAX_PX,
        overlay_handles: bool = False,
    ) -> dict:
        """A region from the exported snapshot; no region is the window capture (1.8-07).

        A window capture cannot say which drawing coordinates its pixels show -
        AutoCAD's own frame, ribbon and the viewport's aspect sit between them -
        so a region request does not zoom and capture: it exports the bound
        document (``_com_export``, the QA snapshot route, guarded by
        ``_acad_doc()`` inside the job) and draws that DXF's model space with
        the headless renderer, at a stated scale. The export is seconds on a
        large drawing, so it runs only when a region is asked for.
        """
        wanted = normalize_region(region)
        size = check_max_px(max_px)
        if overlay_handles:
            # Same boundary as view_screenshot_grounded; labels on the export
            # render are 1.8.1 (highlighting) scope.
            raise UnsupportedCapabilityError(
                "handle_overlay",
                "view_render: the live backend does not label handles on its images. "
                "Use entity_list for handles beside the picture, or the headless "
                "backend (AUTOCAD_MCP_BACKEND=ezdxf) for a grounded render.",
            )
        if wanted is None:
            png = await self.view_screenshot()
            if png is None:
                return {
                    "png": None,
                    "applied": False,
                    "source": "window",
                    "error": "no AutoCAD window was found to capture",
                }
            png = downscale_png(png, size)
            width, height = png_size(png)
            return {
                "png": png,
                "applied": True,
                "framing": "current_view",
                "source": "window",
                "px": [width, height],
                "region": None,
                "requested_region": None,
                "px_per_unit": None,
                "note": (
                    "a window capture includes AutoCAD's own frame, so its drawing-unit "
                    "scale is unknown; pass region=[x0, y0, x1, y1] for a measured render"
                ),
            }
        import shutil

        from engineering.understand.snapshot import _com_export

        written, folder, snapshot = await self._run(_com_export, self, None)
        try:
            report = await asyncio.to_thread(render_dxf_file, written, region=wanted, max_px=size)
        except ImportError:
            return {
                "png": None,
                "applied": False,
                "source": "export_snapshot",
                "error": "matplotlib is not installed (pip install autocad-mcp-pro[pdf])",
            }
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        report["source"] = "export_snapshot"
        report["snapshot"] = snapshot
        return report

    # ── transactions ──────────────────────────────────────────────────────────

    async def transaction_begin(self) -> dict:
        if self._transaction_active:
            return {"ok": False, "error": "A transaction is already active"}

        def _sync():
            doc = _acad_doc()
            # `_UNDO _M` below is typed at the command line; with a command or
            # prompt already active it would be swallowed as that command's
            # input and no mark would exist for the rollback to go back to.
            try:
                cmd_active = int(doc.GetVariable("CMDACTIVE"))
            except Exception as exc:
                log.debug("CMDACTIVE read failed, proceeding anyway: %s", exc)
                cmd_active = 0
            if cmd_active:
                return {
                    "ok": False,
                    "error": (
                        f"AutoCAD has an active command or prompt (CMDACTIVE={cmd_active}); "
                        "press ESC in AutoCAD before transaction_begin."
                    ),
                }
            # R32: record the mark from inside the worker, immediately before
            # setting it. `_transaction_active = True` used to run only after
            # `_run` returned, so a timed-out begin left StartUndoMark open in
            # AutoCAD while the server recorded no transaction, and the next
            # transaction_begin nested a second mark nobody could see — the
            # exact inverse of the R16 fix transaction_commit already carries.
            # The abandoned STA thread cannot be cancelled, so the flag has to
            # be written where the call happens; and it is written *before*
            # the marks because a call that hangs inside them may still land.
            # A `_acad_doc()` that fails leaves the flag alone, which is right:
            # nothing reached AutoCAD.
            self._transaction_active = True
            # Two marks, deliberately. StartUndoMark/EndUndoMark form an undo
            # *group* (one UNDO step), not an UNDO Mark: on the live AutoCAD
            # 2026 `_UNDO B` with no Mark answers "This will undo everything.
            # OK? <Y>" and SendCommand blocks at that prompt until a human
            # presses ESC (measured: rollback timed out at 60 s, the entities
            # stayed, every COM client was rejected meanwhile). The Mark is set
            # *before* the group so `_UNDO _B` in transaction_rollback lands
            # exactly at the transaction's start — also when the group is
            # empty, where `_UNDO 1` would be one step too many. Measured on
            # AutoCAD 2026: Mark + group + 2 entities + `_UNDO _B` -> entities
            # gone, CMDACTIVE 0, 47 ms; empty group -> nothing else undone.
            # 1.8-05: through _safe_send_command, so a Mark left at a prompt is
            # command_incomplete (cancelled with ESC) instead of a transaction
            # recorded over a mark that does not exist.
            try:
                self._safe_send_command(doc, "_.UNDO _M")
            except CommandIncompleteError:
                self._transaction_active = False
                raise
            doc.StartUndoMark()
            return {"ok": True, "message": "Transaction begun (AutoCAD undo mark set)"}

        return await self._run(_sync)

    async def transaction_commit(self) -> dict:
        def _sync():
            doc = _acad_doc()
            doc.EndUndoMark()
            return {"ok": True, "message": "Transaction committed"}

        # R16: clear the flag even if _run raises, so a failed commit can't leave
        # _transaction_active permanently stale. B11: except for a refusal -
        # nothing reached AutoCAD and the mark is still open in the bound document.
        try:
            result = await self._run(_sync)
        except ActiveDocumentChangedError:
            raise
        except BaseException:
            self._transaction_active = False
            raise
        self._transaction_active = False
        return result

    async def transaction_rollback(self) -> dict:
        # Without a transaction there is no Mark to go back to, and `_UNDO _B`
        # would then prompt "This will undo everything. OK? <Y>" and block the
        # STA worker at it (see transaction_begin). Refuse the same way the
        # headless engine does.
        if not self._transaction_active:
            return {"ok": False, "error": "No active transaction to rollback"}

        def _sync():
            doc = _acad_doc()
            doc.EndUndoMark()
            # R16: route UNDO Back through the CMDACTIVE-aware sender instead of
            # a raw SendCommand that could deadlock if a command/prompt is active.
            # Back to the Mark transaction_begin set before the group, so this
            # never prompts and never undoes anything older than the transaction.
            self._safe_send_command(doc, "_.UNDO _B")
            return {"ok": True, "message": "Transaction rolled back"}

        # B11: `_UNDO _B` goes to the bound document or nowhere - a switch is
        # refused before anything is sent and the flag stays set, so the
        # rollback can be retried once the bound document is active again.
        try:
            result = await self._run(_sync)
        except ActiveDocumentChangedError:
            raise
        except BaseException:
            self._transaction_active = False
            raise
        self._transaction_active = False
        return result

    def transaction_depth(self) -> int | None:
        # A nested transaction_begin is refused here, so the depth is 0 or 1.
        return int(bool(self._transaction_active))

    # ── system ──────────────────────────────────────────────────────────────

    async def system_status(self) -> dict:
        tx_active = self._transaction_active

        def _sync():
            try:
                app = _acad_app()
                version = app.Version
                doc_count = app.Documents.Count
                active = app.ActiveDocument if doc_count > 0 else None
                active_key = None if active is None else (str(active.Name), str(active.FullName))
                bound = self._bound
                return {
                    "backend": "com",
                    "connected": True,
                    # Which application this actually is. Without it a
                    # GstarCAD/ZWCAD operator reads an "autocad_version" key
                    # naming a product they do not own.
                    "cad_progid": config.settings.cad_progid,
                    "autocad_version": version,
                    "open_documents": doc_count,
                    "active_document": None if active_key is None else active_key[0],
                    # B11: the document this session writes into, and whether
                    # the operator has switched away from it (the next guarded
                    # call would be refused with active_document_changed).
                    "bound_document": None
                    if bound is None
                    else {"name": bound["name"], "path": bound["full_name"] or None},
                    "active_document_changed": bound is not None
                    and (
                        active_key != (bound["name"], bound["full_name"])
                        or bound["app"] != _app_identity(app)
                    ),
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

        status = await self._run(_sync)
        identity = self.document_identity()
        status["document_id"] = identity["document_id"]
        status["revision"] = identity["revision"]
        status["operator_edits"] = self.operator_edits_status()
        status["call_outcomes"] = [dict(row) for row in self.call_outcomes][
            -self._STATUS_CALL_OUTCOMES :
        ]
        return status

    async def system_get_variable(self, name) -> Any:
        # GetVariable / SetVariable are members of AcadDocument, not of
        # AcadApplication: `AutoCAD.Application.GetVariable` raises
        # AttributeError on a live seat before anything reaches AutoCAD.
        def _sync():
            doc = _acad_doc()
            return doc.GetVariable(name)

        return await self._run(_sync)

    async def system_set_variable(self, name, value) -> dict:
        def _sync():
            doc = _acad_doc()  # the sysvar host is the document (see system_get_variable)
            # Coerce to the sysvar's actual type (NEW-com-set-variable-coercion):
            # AutoCAD rejects e.g. a string "0" for the integer OSMODE. Probe the
            # current value's type and convert to match; pass through on failure.
            coerced = value
            # `payload` is what SetVariable receives. It differs from `coerced`
            # only for point variables, whose VARIANT wrapper must never reach
            # the returned dict: pydantic cannot serialise
            # win32com.client.VARIANT, so the tool would report failure *after*
            # AutoCAD had already applied the write.
            payload = value
            try:
                current = doc.GetVariable(name)
                if isinstance(current, bool):
                    coerced = bool(int(value)) if isinstance(value, str) else bool(value)
                elif isinstance(current, int):
                    coerced = int(float(value)) if isinstance(value, str) else int(value)
                elif isinstance(current, float):
                    coerced = float(value)
                elif isinstance(current, (tuple, list)):
                    # 2D/3D point variables (LIMMIN, GRIDUNIT, SNAPUNIT, ...) travel
                    # as VARIANT double arrays of the length AutoCAD itself reports;
                    # a bare Python tuple is marshalled as VT_VARIANT and rejected.
                    coords = [float(v) for v in value]
                    coords += [0.0] * (len(current) - len(coords))
                    coerced = coords[: len(current)]
                payload = _av(coerced) if isinstance(current, (tuple, list)) else coerced
            except Exception as exc:
                log.debug("set_variable type probe for %s failed: %s", name, exc)
            doc.SetVariable(name, payload)
            return {"ok": True, "variable": name, "value": coerced}

        return await self._run(_sync)

    async def system_run_command(self, command) -> dict:
        # Commands ending in option menus (e.g. -LINETYPE returning to its
        # [?/Create/Load/Set] prompt) need a trailing blank line or _X to exit;
        # otherwise SendCommand returns but AutoCAD stays at a prompt and the
        # next COM call deadlocks. Example: "_-LINETYPE _LOAD CENTER acad.lin\n\n"
        #
        # 1.8-05: through _safe_send_command, which waits for CMDACTIVE to clear
        # (COMMAND_DEADLINE_S), reports what the command created in which space
        # and raises command_incomplete with LASTPROMPT when it stays at a prompt.
        # Up to 1.7.0 this answered ok: True the moment SendCommand returned.
        def _sync():
            doc = _acad_doc()
            # CMDACTIVE is read on the document (the Application has no
            # GetVariable); through the Application this guard never fired.
            cmd_active = int(doc.GetVariable("CMDACTIVE"))
            if cmd_active:
                raise RuntimeError(
                    "AutoCAD has an active command or prompt (CMDACTIVE="
                    f"{cmd_active}). Press ESC in AutoCAD to cancel, then retry."
                )
            outcome = self._safe_send_command(doc, command)
            return {"ok": True, "command": command, **outcome.report()}

        return await self._run(_sync)

    async def system_run_lisp(self, expression) -> dict:
        def _sync():
            doc = _acad_doc()
            # R23: refuse to send while a command/prompt is active (raw SendCommand
            # of a prompting form would deadlock the single STA thread). CMDACTIVE
            # is a document sysvar; read through the Application (which has no
            # GetVariable) this guard never fired.
            if int(doc.GetVariable("CMDACTIVE")):
                raise RuntimeError(
                    "AutoCAD has an active command/prompt (CMDACTIVE). "
                    "Press ESC in AutoCAD and retry."
                )
            # N6: SendCommand is void-returning, so the value can't be read from its
            # return. Stash it in USERS1 and read it back after the form completes
            # (CMDACTIVE-polled by _safe_send_command).
            wrapped = f'(vl-load-com)(setvar "USERS1" (vl-princ-to-string (progn {expression})))'
            self._safe_send_command(doc, wrapped)
            try:
                result = doc.GetVariable("USERS1")
            except Exception as exc:  # optional read: the expression already ran
                log.debug("doc.GetVariable(USERS1) failed, reporting nil: %s", exc)
                result = None
            return {
                "ok": True,
                "expression": expression,
                "result": str(result) if result not in (None, "") else "nil",
            }

        return await self._run(_sync)

    # ── styles (track E) ─────────────────────────────────────────────────────
    #
    # Fake-tested in tests/test_styles.py; executed live once by
    # scripts/smoke_settings_com.py. See the module helper block for the
    # ActiveX rules every method here relies on.

    async def dimstyle_list(self) -> list[dict]:
        from engineering.standards.dimstyles import PRESET_VARIABLES

        def _sync():
            doc = _acad_doc()
            current = str(doc.ActiveDimStyle.Name)
            rows = []
            for index in range(doc.DimStyles.Count):
                name = str(doc.DimStyles.Item(index).Name)
                is_current = name.lower() == current.lower()
                values = (
                    {
                        var: _com_dimvar_for_report(var, doc.GetVariable(var))
                        for var in PRESET_VARIABLES
                    }
                    if is_current
                    else None
                )
                rows.append(
                    {
                        "name": name,
                        "current": is_current,
                        "values": values,
                        "values_available": is_current,
                    }
                )
            rows.sort(key=lambda row: row["name"].lower())
            return rows

        return await self._run(_sync)

    async def dimstyle_create(self, name: str, values: dict, set_current: bool = False) -> dict:
        from engineering.standards.dimstyles import PRESET_VARIABLES, validate_overrides

        clean = sanitize_symbol_name(name, kind="dimstyle")
        typed = validate_overrides(values)
        if not typed:
            raise ValueError("dimstyle_create: values is empty; resolve a preset first")

        def _sync():
            doc = _acad_doc()
            if _com_named(doc.DimStyles, clean) is not None:
                raise ValueError(
                    f"dimstyle_create: dimension style {clean!r} already exists; "
                    "use dimstyle_modify to change it"
                )
            # Every refusal precedes the first write: a user arrowhead block
            # must exist (SetVariable("DIMBLK", ...) would otherwise raise
            # mid-loop, after Add and ActiveDimStyle), and the text style must
            # exist before DIMTXSTY can name it.
            _com_require_arrowhead_blocks(doc, typed)
            txsty, textstyle_created = _ensure_com_textstyle(
                doc, str(typed.get("DIMTXSTY", "Standard")), refusal_key="DIMTXSTY"
            )
            previous = doc.ActiveDimStyle
            style = doc.DimStyles.Add(clean)
            try:
                doc.ActiveDimStyle = style
                for var, value in {**typed, "DIMTXSTY": txsty}.items():
                    doc.SetVariable(var, _com_dimvar_for_write(var, value))
                style.CopyFrom(doc)
                written = {
                    var: _com_dimvar_for_report(var, doc.GetVariable(var))
                    for var in PRESET_VARIABLES
                }
                if not set_current:
                    doc.ActiveDimStyle = previous
            except Exception:
                # Restoring the previous style discards the unsaved overrides
                # (AutoCAD's own Restore rule) and frees the new entry for
                # Delete, so the drawing is left exactly as it was found —
                # the half-made style is never the operator's current style.
                doc.ActiveDimStyle = previous
                _com_delete_quietly(style, f"dimension style {clean!r}")
                if textstyle_created:
                    created = _com_named(doc.TextStyles, txsty)
                    if created is not None:
                        _com_delete_quietly(created, f"text style {txsty!r}")
                raise
            _regen()
            return {
                "ok": True,
                "name": clean,
                "values": written,
                "written": sorted(typed),
                "current": bool(set_current),
                "textstyle_created": textstyle_created,
            }

        return await self._run(_sync)

    async def _dimension_display(self, handles=None) -> dict:
        """``{HANDLE: (rendered_text, matches)}`` of the DIMENSIONs ``handles``
        (every one when None), read back through the ``Document.Export``
        snapshot - the ``*D`` blocks AutoCAD actually holds (1.8-15)."""
        from engineering.dimtext import rendered_matches
        from engineering.understand.snapshot import take_snapshot

        snap = await take_snapshot(self)
        wanted = None if handles is None else {str(h).upper() for h in handles}
        out: dict = {}
        for record in snap.records:
            if record.type != "DIMENSION" or record.dimension is None:
                continue
            key = record.handle.upper()
            if wanted is None or key in wanted:
                matches = rendered_matches(record.rendered_text, record.dimension, record.dim_vars)
                out[key] = (record.rendered_text, matches)
        return out

    async def dimension_rerender(self, handles=None, force: bool = False) -> dict:
        # ``force`` is accepted for the contract: AutoCAD's Update() redraws a
        # dimension in place and never moves a text its author placed.
        before = await self._dimension_display(handles)
        targets = [str(h) for h in handles] if handles is not None else sorted(before)

        def _sync():
            doc = _acad_doc()
            for entity in _com_require_dimensions(doc, targets):
                entity.Update()
            _regen()

        await self._run(_sync)
        after = await self._dimension_display(targets)
        return _rerender_report(targets, before, after)

    async def dimstyle_modify(
        self, name: str, values: dict, rerender: bool = True, force: bool = False
    ) -> dict:
        from engineering.standards.dimstyles import validate_overrides

        typed = validate_overrides(values)
        if not typed:
            raise ValueError("dimstyle_modify: overrides is empty; name at least one DIM* variable")
        before: dict = {}
        if rerender:
            users = await self._run(lambda: _com_dimensions_using(_acad_doc(), str(name)))
            if users:
                before = await self._dimension_display(users)

        def _sync():
            doc = _acad_doc()
            style = _com_named(doc.DimStyles, name)
            if style is None:
                raise ValueError(
                    f"dimstyle_modify: dimension style {name!r} does not exist; "
                    "dimstyle_list names the styles this drawing holds"
                )
            if "DIMTXSTY" in typed and _com_named(doc.TextStyles, typed["DIMTXSTY"]) is None:
                raise ValueError(
                    f"DIMTXSTY: text style {typed['DIMTXSTY']!r} does not exist in this drawing; "
                    "create it first with textstyle_create"
                )
            _com_require_arrowhead_blocks(doc, typed)
            previous = doc.ActiveDimStyle
            switched = str(previous.Name).lower() != str(style.Name).lower()
            if switched:
                doc.ActiveDimStyle = style
            try:
                before = {var: _com_dimvar_for_report(var, doc.GetVariable(var)) for var in typed}
                changed: dict[str, list] = {}
                for var, value in typed.items():
                    if _same_dimvar(before[var], value):
                        continue
                    doc.SetVariable(var, _com_dimvar_for_write(var, value))
                    changed[var] = [before[var], value]
                if changed:
                    style.CopyFrom(doc)
            except Exception:
                # Re-activating a style discards its unsaved overrides, so a
                # failed write leaves the target style as it was saved and the
                # operator's current style as it was found.
                doc.ActiveDimStyle = style if not switched else previous
                raise
            if switched:
                doc.ActiveDimStyle = previous
            using = _com_dimensions_using(doc, str(style.Name))
            if changed and using and rerender:
                # S4 pending: "AutoCAD re-renders on regen" is not measured, so
                # each dimension is Update()d and its *D text read back below.
                for entity in _com_require_dimensions(doc, using):
                    entity.Update()
            if changed:
                _regen()
            return {
                "ok": True,
                "name": str(style.Name),
                "changed": changed,
                "dimensions_using_style": using,
                "rerendered": [],
                "text_kept": [],
                "rerender_required": bool(changed and using and not rerender),
            }

        result = await self._run(_sync)
        if rerender and result["changed"] and result["dimensions_using_style"]:
            using = result["dimensions_using_style"]
            report = _rerender_report(using, before, await self._dimension_display(using))
            result["rerendered"] = report["rerendered"]
            result["already_current"] = report["already_current"]
            result["stale"] = report["stale"]
            result["rerender_required"] = bool(report["stale"])
        return result

    async def dimstyle_set_current(self, name: str) -> dict:
        def _sync():
            doc = _acad_doc()
            style = _com_named(doc.DimStyles, name)
            if style is None:
                raise ValueError(
                    f"dimstyle_set_current: dimension style {name!r} does not exist; "
                    "dimstyle_list names the styles this drawing holds"
                )
            previous = str(doc.ActiveDimStyle.Name)
            changed = previous.lower() != str(style.Name).lower()
            if changed:
                doc.ActiveDimStyle = style
            return {
                "ok": True,
                "current": str(style.Name),
                "previous": previous,
                "changed": changed,
            }

        return await self._run(_sync)

    async def textstyle_list(self) -> list[dict]:
        def _sync():
            doc = _acad_doc()
            current = str(doc.ActiveTextStyle.Name)
            rows = []
            for index in range(doc.TextStyles.Count):
                style = doc.TextStyles.Item(index)
                rows.append(
                    {
                        "name": str(style.Name),
                        "font": str(style.fontFile),
                        "height": float(style.Height),
                        "width_factor": float(style.Width),
                        "oblique_deg": rad2deg(float(style.ObliqueAngle)),
                        "current": str(style.Name).lower() == current.lower(),
                    }
                )
            rows.sort(key=lambda row: row["name"].lower())
            return rows

        return await self._run(_sync)

    async def textstyle_create(
        self,
        name: str,
        font: str,
        height: float = 0.0,
        width_factor: float = 1.0,
        oblique_deg: float = 0.0,
        set_current: bool = False,
    ) -> dict:
        from engineering.standards.textstyles import validate_textstyle

        spec = validate_textstyle(name, font, height, width_factor, oblique_deg)

        def _sync():
            doc = _acad_doc()
            if _com_named(doc.TextStyles, spec["name"]) is not None:
                raise ValueError(f"textstyle_create: text style {spec['name']!r} already exists")
            # Unlike the headless engine, which stores a name nobody can find
            # and says `font_resolved: false`, ActiveX refuses a font it
            # cannot open — so the file is located before TextStyles.Add and
            # a font AutoCAD cannot see is refused with nothing written.
            style = _com_create_textstyle(
                doc,
                spec["name"],
                spec["font_file"],
                width=spec["width_factor"],
                oblique_deg=spec["oblique_deg"],
                height=spec["height"],
                key="font",
            )
            if set_current:
                doc.ActiveTextStyle = style
            return {
                "ok": True,
                "name": spec["name"],
                "font": spec["font_file"],
                # Always true here: a font the live engine could not locate
                # was refused above, never written.
                "font_resolved": True,
                "current": bool(set_current),
            }

        return await self._run(_sync)

    async def textstyle_set_current(self, name: str) -> dict:
        def _sync():
            doc = _acad_doc()
            style = _com_named(doc.TextStyles, name)
            if style is None:
                raise ValueError(
                    f"textstyle_set_current: text style {name!r} does not exist; "
                    "textstyle_list names the styles this drawing holds"
                )
            previous = str(doc.ActiveTextStyle.Name)
            changed = previous.lower() != str(style.Name).lower()
            if changed:
                doc.ActiveTextStyle = style
            return {
                "ok": True,
                "current": str(style.Name),
                "previous": previous,
                "changed": changed,
            }

        return await self._run(_sync)

    async def mleaderstyle_list(self) -> list[dict]:
        def _sync():
            doc = _acad_doc()
            dictionary = _com_mleaderstyle_dictionary(doc)
            if dictionary is None:
                return []
            rows = []
            for name, style in _com_mleaderstyle_rows(dictionary):
                rows.append(
                    {
                        "name": name,
                        "arrow_size": float(style.ArrowSize),
                        "landing_gap": float(style.LandingGap),
                        "text_style": str(style.TextStyle),
                        "text_height": float(style.TextHeight),
                        "values_available": True,
                    }
                )
            rows.sort(key=lambda row: row["name"].lower())
            return rows

        return await self._run(_sync)

    async def mleaderstyle_create(self, name: str, values: dict) -> dict:
        from engineering.standards.mleaderstyles import validate_mleaderstyle

        clean = sanitize_symbol_name(name, kind="mleaderstyle")
        typed = validate_mleaderstyle(values)

        def _sync():
            doc = _acad_doc()
            dictionary = _com_mleaderstyle_dictionary(doc, create=True)
            wanted = clean.lower()
            if any(
                existing.lower() == wanted for existing, _ in _com_mleaderstyle_rows(dictionary)
            ):
                raise ValueError(f"mleaderstyle_create: leader style {clean!r} already exists")
            # The text style must exist before TextStyle can name it — and a
            # name that is neither present nor a preset is refused here,
            # before AddObject, so a refusal leaves no half-made style behind.
            text_style_name, textstyle_created = _ensure_com_textstyle(
                doc, typed["text_style"], refusal_key="text_style"
            )
            # AddObject is declared IAcadObject* too: un-narrowed, or the
            # first property write below raises AttributeError.
            style = _com_unnarrow(dictionary.AddObject(clean, _MLEADERSTYLE_CLASS))
            style.ArrowSize = typed["arrow_size"]
            style.LandingGap = typed["landing_gap"]
            style.TextHeight = typed["text_height"]
            style.TextStyle = text_style_name
            return {
                "ok": True,
                "name": clean,
                "values": {**typed, "text_style": text_style_name},
                "textstyle_created": textstyle_created,
            }

        return await self._run(_sync)

    # ── corner ops ──────────────────────────────────────────────────────────

    @staticmethod
    def _safe_send_command(
        doc,
        cmd: str,
        deadline_s: float | None = None,
        target_space: str | None = None,
        owner: str | None = None,
    ) -> CommandOutcome:
        """Send `cmd`, polling CMDACTIVE until the command finishes.

        Snap/grid/echo are temporarily zeroed to avoid OSNAP-driven misclicks
        and chatty echoes; everything is restored in the finally block.

        1.8-05 - what the command did, not only that it was sent:

        * **Before:** the entity total over every layout block and the highest
          handle among their last items (``_space_snapshot``).
        * **Overrun:** CMDACTIVE still set after ``deadline_s`` (default
          ``COMMAND_DEADLINE_S``, 8 s) - ``LASTPROMPT`` is read first (the
          prompt it stopped at), then ESC x3, then ``CommandIncompleteError``
          (kind ``command_incomplete``, ``{command, last_prompt}``).
        * **Completed:** a ``CommandOutcome`` with every entity created, in
          which space (``{handle, type, space}``, the first 50 plus
          ``created_count``), ``LASTPROMPT``, and the totals before and after;
          ``report()`` adds ``warning: "created_outside_target_space"`` when a
          new entity is not in ``target_space`` (default: the bound
          document's target, ``_target_space_name()``).

        1.8-23 - **the owner block, O(new).** ``owner`` names the one space the
        command can write into (a corner op runs in the target space's tab
        and FILLET / CHAMFER / TRIM add to the space of the objects they
        pick): only that block is read - its ``Count`` and last handle before,
        its tail after (``_block_tail``) - plus, for a paper owner, model
        space's mark and tail (so an entity the command wrote there is still
        ``created_outside_target_space``); no other layout is touched and
        ``entities_before`` / ``_after`` are the owner block's. Without ``owner``
        every layout block is read the same way (``system_run_command``'s
        paper-space report, the undo evidence). A seat whose layouts cannot be
        read falls back to the target space's own block, read the same way -
        no longer a walk over every model-space entity as in 1.7; with
        neither, ``created_count`` is None - unknown, never zero.
        """
        if deadline_s is None:
            deadline_s = config.settings.command_deadline_s
        if target_space is None:
            target_space = _target_space_name()
        command = cmd.rstrip("\n")
        saved: dict[str, Any] = {}
        try:
            for var in ("OSMODE", "SNAPMODE", "CMDECHO"):
                try:
                    saved[var] = doc.GetVariable(var)
                    doc.SetVariable(var, 0)
                except Exception:
                    pass
            snapshot = None if owner is not None else _space_snapshot(doc)
            block = space = mark = model_mark = None
            if snapshot is None:
                try:
                    block, space = _owner_block(doc, owner or target_space)
                    mark = _block_mark(block)
                except Exception as exc:  # unknown, never zero
                    log.debug("owner block unreadable: %s", exc)
                    mark = None
                if mark is not None and space != "Model":
                    # A paper owner: model space is marked too (two reads), so
                    # an entity the command wrote there still reaches the
                    # report as created_outside_target_space (1.8-05).
                    try:
                        model_mark = _block_mark(doc.ModelSpace)
                    except Exception as exc:  # the owner block's report stands
                        log.debug("model-space mark unreadable: %s", exc)
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
                    # Where it stopped, before the ESCs replace it with *Cancel*.
                    last_prompt = _read_last_prompt(doc)
                    try:
                        doc.SendCommand("\x1b\x1b\x1b\n")  # ESC×3
                    except Exception:
                        pass
                    raise CommandIncompleteError(command, last_prompt, float(deadline_s))
                time.sleep(0.05)
            last_prompt = _read_last_prompt(doc)
            outcome = CommandOutcome(
                command=command,
                target_space=target_space,
                handles=[],
                created=[],
                created_count=None,
                last_prompt=last_prompt,
            )
            if snapshot is not None:
                before, top = snapshot
                try:
                    found, after = _created_since(doc, top)
                except Exception as exc:  # unknown, never zero
                    log.debug("post-command layout read failed: %s", exc)
                else:
                    outcome.handles, outcome.created = _created_rows(found)
                    outcome.created_count = len(found)
                    outcome.entities_before, outcome.entities_after = before, after
            elif mark is not None:
                before, top = mark
                try:
                    found, after = _block_tail(block, top, space)
                except Exception as exc:  # unknown, never zero
                    log.debug("post-command owner-block read failed: %s", exc)
                else:
                    if model_mark is not None:
                        try:
                            strays, _count = _block_tail(doc.ModelSpace, model_mark[1], "Model")
                        except Exception as exc:  # the owner block's report stands
                            log.debug("post-command model-space read failed: %s", exc)
                        else:
                            found = sorted(found + strays, key=lambda row: row[0])
                    outcome.handles, outcome.created = _created_rows(found)
                    outcome.created_count = len(found)
                    outcome.entities_before, outcome.entities_after = before, after
            return outcome
        finally:
            for var, val in saved.items():
                try:
                    doc.SetVariable(var, val)
                except Exception:
                    pass

    async def entity_trim(self, target_handle, cutter_handle, keep_x, keep_y) -> EntityInfo:
        def _sync():
            doc = _acad_doc()
            # 1.8-05: ``*x,y`` is a WCS point whatever UCS is current, so a
            # ucs_set cannot shift the pick (tool coordinates are WCS).
            cmd = f'_TRIM\n(handent "{cutter_handle}")\n\n*{float(keep_x)},{float(keep_y)}\n\n'
            with _target_tab(doc):
                # 1.8-23: only the target space's block is read, O(new)
                self._safe_send_command(doc, cmd, owner=_target_space_name())
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
            target = _handle_to_object(doc, target_handle)
            boundary = _handle_to_object(doc, boundary_handle)
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
            cmd = f'_EXTEND\n(handent "{boundary_handle}")\n\n*{ex},{ey}\n\n'  # WCS pick
            with _target_tab(doc):
                # 1.8-23: only the target space's block is read, O(new)
                self._safe_send_command(doc, cmd, owner=_target_space_name())
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
            with _target_tab(doc):
                # 1.8-23: only the target space's block is read, O(new)
                owner = _target_space_name()
                new_handles = self._safe_send_command(doc, cmd, owner=owner).handles
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
            with _target_tab(doc):
                # 1.8-23: only the target space's block is read, O(new)
                owner = _target_space_name()
                new_handles = self._safe_send_command(doc, cmd, owner=owner).handles
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
    async def _delete_layer_entities(self, layer: str) -> dict:
        def _sync():
            return _delete_on_layer_all_layouts(_acad_doc(), layer)

        return await self._run(_sync)

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
            _regen(xline)
            return _entity_info(xline)

        return await self._run(_sync)

    # ── settings (track E, group C) ───────────────────────────────────────────

    async def drawing_properties_get(self) -> dict:
        def _sync():
            info = _acad_doc().SummaryInfo
            summary = {
                field: str(getattr(info, attr) or "") for field, attr in _SUMMARY_ATTRS.items()
            }
            custom: dict[str, str] = {}
            for index in range(int(info.NumCustomInfo())):
                key, value = info.GetCustomByIndex(index)
                custom[str(key)] = str(value)
            return {
                "summary": summary,
                "summary_available": True,
                "custom": custom,
                "backend": "com",
            }

        return await self._run(_sync)

    async def drawing_properties_set(
        self, summary: dict | None = None, custom: dict | None = None
    ) -> dict:
        # Validated before any ActiveX call: a bad field or value raises here
        # and SummaryInfo is never touched.
        written, to_write, to_delete = validate_drawing_properties(summary, custom)

        def _sync():
            info = _acad_doc().SummaryInfo
            # AutoCAD matches custom keys by its own simple per-character case
            # compare (measured on 2026: AddCustomInfo("PROJECT") over an
            # existing "Project" is 'Duplicate key', yet "Straße" and
            # "STRASSE" are two keys -- Python's casefold() merges them, and a
            # routing built on it sent "STRASSE" to SetCustomByKey, which
            # failed 'Key not found' after the summary fields and the earlier
            # keys were already applied). So only an exact spelling is routed
            # from here; for everything else AutoCAD decides: AddCustomInfo's
            # 'Duplicate key' means the key is there under another spelling
            # and it is set instead, RemoveCustomByKey's 'Key not found' means
            # it is not there and the delete is reported as not done. No
            # routing guess can then half-write.
            existing: set[str] = set()
            for index in range(int(info.NumCustomInfo())):
                key, _value = info.GetCustomByIndex(index)
                existing.add(str(key))
            for field, value in written.items():
                setattr(info, _SUMMARY_ATTRS[field], value)
            for key, value in to_write.items():
                if key in existing:
                    info.SetCustomByKey(key, value)
                    continue
                try:
                    info.AddCustomInfo(key, value)
                except _COM_ERROR as exc:
                    if not _summaryinfo_error_is(exc, _SUMMARYINFO_DUPLICATE_KEY, "Duplicate key"):
                        raise
                    info.SetCustomByKey(key, value)
            deleted = []
            for key in to_delete:
                try:
                    info.RemoveCustomByKey(key)
                except _COM_ERROR as exc:
                    if not _summaryinfo_error_is(exc, _SUMMARYINFO_KEY_NOT_FOUND, "Key not found"):
                        raise
                    continue
                deleted.append(key)
            return {
                "ok": True,
                "summary_written": sorted(written),
                "custom_written": sorted(to_write),
                "custom_deleted": deleted,
                "backend": "com",
            }

        return await self._run(_sync)


#: The ComBackend methods that change a document's content (B11): the headless
#: engine's dirty-setting methods that exist here, the undo / redo the headless
#: engine implements as snapshot restores, the live-only 3D solids and the two
#: escape hatches. tests/test_bound_document_com.py derives the same set from
#: the headless source and holds this one to it. ``entity_batch_create`` and
#: ``entity_delete_many`` (1.8-23) are one job each here, so one revision step
#: per call; headlessly they are the contract's loops over the creates and
#: deletes, which move the revision once per item.
_MUTATING_METHODS: frozenset[str] = frozenset(
    {
        "_create_xline",
        "_delete_layer_entities",
        "block_create_from_entities",
        "block_define",
        "block_explode",
        "block_insert",
        "block_set_attributes",
        "dimension_aligned",
        "dimension_angular",
        "dimension_diameter",
        "dimension_linear",
        "dimension_radius",
        "dimension_rerender",
        "dimstyle_create",
        "dimstyle_modify",
        "dimstyle_set_current",
        "drawing_audit",
        "drawing_properties_set",
        "drawing_purge",
        "drawing_redo",
        "drawing_undo",
        "entity_array_polar",
        "entity_array_rectangular",
        "entity_batch_create",
        "entity_chamfer",
        "entity_copy",
        "entity_create_arc",
        "entity_create_block_ref",
        "entity_create_circle",
        "entity_create_ellipse",
        "entity_create_hatch",
        "entity_create_line",
        "entity_create_mtext",
        "entity_create_point",
        "entity_create_polyline",
        "entity_create_spline",
        "entity_create_table",
        "entity_create_text",
        "entity_delete",
        "entity_delete_many",
        "entity_edit_geometry",
        "entity_edit_text",
        "entity_extend",
        "entity_fillet",
        "entity_mirror",
        "entity_modify_where",
        "entity_move",
        "entity_offset",
        "entity_rotate",
        "entity_scale",
        "entity_set_properties",
        "entity_set_vertices",
        "entity_set_xdata",
        "entity_trim",
        "hatch_add_boundary",
        "hatch_edit",
        "hatch_set_gradient",
        "image_attach",
        "layer_create",
        "layer_delete",
        "layer_freeze",
        "layer_hide",
        "layer_lock",
        "layer_merge",
        "layer_modify",
        "layer_rename",
        "layer_set_current",
        "layer_show",
        "layer_state_delete",
        "layer_state_restore",
        "layer_state_save",
        "layer_thaw",
        "layer_unlock",
        "layout_copy",
        "layout_create",
        "layout_delete",
        "layout_rename",
        "layout_set_current",
        "leader_create_mleader",
        "linetype_load",
        "mleaderstyle_create",
        "page_setup_apply",
        "solid_boolean",
        "solid_box",
        "solid_cylinder",
        "solid_extrude",
        "solid_revolve",
        "system_run_command",
        "system_run_lisp",
        "system_set_variable",
        "text_find_replace",
        "text_set_background",
        "textstyle_create",
        "textstyle_set_current",
        "transaction_rollback",
        "ucs_restore",
        "ucs_set",
        "view_named_restore",
        "view_named_save",
        "viewport_create",
        "viewport_delete",
        "viewport_lock",
        "viewport_set_scale",
        "xref_attach",
        "xref_manage",
    }
)

for _name in sorted(_MUTATING_METHODS):
    # ``__dict__``, not getattr: a name the class does not define itself fails
    # loudly at import instead of wrapping an inherited contract default.
    setattr(ComBackend, _name, _mutating(ComBackend.__dict__[_name]))
del _name
