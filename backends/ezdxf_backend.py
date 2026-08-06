"""ezdxf backend – headless DXF file operations.

Works without AutoCAD installed. Ideal for file generation and analysis.
All operations are synchronous but wrapped in asyncio.to_thread for
non-blocking use in the FastMCP async context.

DXF only: ezdxf has no DWG writer, so every save path that would put DXF bytes
behind a ``.dwg`` name refuses instead (capability ``dwg``). Real DWG I/O needs
the live COM backend.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import tempfile
import uuid
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import config
from engineering import measure

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
    _unsupported,
    normalize_lineweight,
    shoelace_area,
)
from .quarantine import (
    AbandonedCall,
    DocumentQuarantineError,
    QuarantineRecord,
    quarantine_refusal,
    real_call_name,
)

__all__ = [
    "DocumentQuarantineError",
    "EzdxfBackend",
    "UnsupportedCapabilityError",
    "_unsupported",
]

#: How many abandoned calls ``system_status`` keeps. The interesting one is
#: always the most recent; older entries exist so a support transcript shows a
#: document that has been abandoned on repeatedly rather than once.
_MAX_ABANDONED_RECORDS = 10

log = logging.getLogger(__name__)


def _new_agg_figure(*, figsize=None, dpi=100):
    """Create a matplotlib Figure bound to the headless Agg canvas.

    Rendering runs inside an ``asyncio.to_thread`` worker. ``pyplot.figure``
    selects a *GUI* backend from global state (Qt/Tk), and instantiating a GUI
    canvas off the main thread can hard-crash the interpreter (SIGSEGV). Using
    ``Figure`` + ``FigureCanvasAgg`` directly bypasses pyplot's global backend
    selection entirely and is thread-safe; ``savefig`` still dispatches to the
    PDF backend by file extension when needed.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=figsize, dpi=dpi)
    FigureCanvasAgg(fig)  # binds fig.canvas to Agg (no pyplot, no GUI)
    return fig


try:
    import ezdxf
    from ezdxf import colors, units  # noqa: F401
    from ezdxf.audit import AuditError
    from ezdxf.enums import TextEntityAlignment  # noqa: F401
    from ezdxf.math import BSpline, Vec2, Vec3  # noqa: F401

    _EZDXF_OK = True
except ImportError:
    AuditError = None
    _EZDXF_OK = False
    log.warning("ezdxf not installed. ezdxf backend unavailable.")

try:
    from ezdxf import bbox as ezdxf_bbox

    _BBOX_OK = True
except ImportError:
    _BBOX_OK = False


# ---------------------------------------------------------------------------
# Capability refusals
# ---------------------------------------------------------------------------


# Re-exported, not redefined: server.py and the batch classifier import the
# refusal type from here, and an isinstance check against a second class object
# with the same name would quietly stop matching.


# ezdxf has no DWG writer. Writing DXF bytes to a .dwg path (the pre-1.5.0
# behaviour of drawing_save) produces a file AutoCAD refuses or misreads, so the
# save paths refuse instead and name both escape hatches.
_DWG_WRITE_REFUSAL = (
    "{operation}: the headless ezdxf backend cannot write DWG. ezdxf has no DWG "
    "writer, and writing DXF bytes to '{path}' would produce a file AutoCAD "
    "refuses or misreads. Save with a .dxf extension instead, or switch to the "
    "live COM backend (AUTOCAD_MCP_BACKEND=com, needs Windows + AutoCAD) for "
    "real DWG output."
)


def _is_dwg_path(path: str) -> bool:
    """True when `path` names a DWG file. The extension is authoritative (N2)."""
    return Path(path).suffix.lower() == ".dwg"


def _refuse_dwg(operation: str, path: str) -> UnsupportedCapabilityError:
    return UnsupportedCapabilityError(
        "dwg", _DWG_WRITE_REFUSAL.format(operation=operation, path=path)
    )


def _audit_entry(entry) -> dict:
    """Flatten an ezdxf ``ErrorEntry`` into something a client can read.

    ``ErrorEntry`` defines no ``__str__``, so the pre-1.5.0
    ``[str(e) for e in auditor.errors]`` handed users
    ``'<ezdxf.audit.ErrorEntry object at 0x...>'`` — a count of problems with no
    way to learn what they were. ``code`` is the numeric ``AuditError`` value and
    ``name`` its symbolic form (100/UNDEFINED_LINETYPE, 104/
    INVALID_BLOCK_REFERENCE_CYCLE, ...).
    """
    entity = getattr(entry, "entity", None)
    handle = None
    if entity is not None and getattr(entity, "is_alive", True):
        try:
            handle = entity.dxf.get("handle", None)
        except Exception:  # a trashed entity has no usable namespace
            handle = None
    code = int(entry.code)
    try:
        name = AuditError(code).name
    except ValueError:
        # A code ezdxf added after this release. str(code) would put "999" in a
        # field called `name`, which is not a name.
        name = "UNKNOWN"
    return {
        "code": code,
        "name": name,
        "message": entry.message,
        "handle": handle,
    }


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

_BUILTIN_LINETYPES = {"continuous", "bylayer", "byblock"}

# AutoCAD-shipped linetypes that ezdxf.tools.standards does not include.
# Pattern format: [total_length, dash_1, gap_1, dash_2, gap_2, ...] (gaps negative).
# Values match acadiso.lin so drawings render the same in AutoCAD viewers.
_AUTOCAD_FALLBACK_LINETYPES: dict[str, tuple[str, list[float]]] = {
    "HIDDEN": ("Hidden __ __ __ __ __ __ __ __ __", [7.5, 5.0, -2.5]),
    "HIDDEN2": ("Hidden (.5x) _ _ _ _ _ _ _ _ _", [3.75, 2.5, -1.25]),
    "HIDDENX2": ("Hidden (2x) ____ ____ ____ ____", [15.0, 10.0, -5.0]),
    "BORDER": ("Border __ __ . __ __ . __ __ . __", [22.5, 7.5, -2.5, 7.5, -2.5, 0.0, -2.5]),
    "BORDER2": ("Border (.5x) __.__.__.__.__.__", [11.25, 3.75, -1.25, 3.75, -1.25, 0.0, -1.25]),
    "BORDERX2": ("Border (2x) ____  __  ____  __", [45.0, 15.0, -5.0, 15.0, -5.0, 0.0, -5.0]),
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


#: Types whose area lives in ACIS solid data. ezdxf stores SAT/SAB opaquely and
#: cannot evaluate or generate it; live AutoCAD exposes .Area on all of these.
#: This is an ENGINE boundary, so it refuses with a capability.
_ACIS_AREA_TYPES = frozenset({"REGION", "3DSOLID", "BODY", "SURFACE", "PLANESURFACE"})

_MEASURABLE_TYPES = (
    "LWPOLYLINE, POLYLINE (2D), CIRCLE, ELLIPSE, SPLINE, HATCH, SOLID, TRACE, 3DFACE"
)


def _measure_dxf_entity(ent, tolerance: float) -> dict:
    """Area and perimeter of one entity, with its accuracy stated.

    The two failure modes are deliberately different exception types. A LINE
    bounds no area on any engine, so that is a plain ``RuntimeError`` — a
    capability tag would tell the user to switch backends for something no
    backend can do. An ACIS type is a real headless-engine boundary and refuses
    with ``UnsupportedCapabilityError``, naming the live backend.
    """
    ent_type = ent.dxftype()
    handle = ent.dxf.get("handle", "?")
    result = {
        "handle": str(handle),
        "type": ent_type,
        "closed": True,
        "assumed_closed": False,
        "method": "analytic",
        "exact": True,
        "flatten_tolerance": None,
        "backend": "ezdxf",
        "self_intersecting": None,
        "perimeter_exact": True,
        "loop_count": 1,
    }

    if ent_type in _ACIS_AREA_TYPES:
        raise UnsupportedCapabilityError(
            "measure_area_acis",
            f"entity_measure: {handle} is a {ent_type}. Its area lives in ACIS "
            "solid data, which the headless ezdxf backend cannot evaluate — ezdxf "
            "stores SAT/SAB opaquely and cannot generate it. Switch to the live "
            "COM backend (AUTOCAD_MCP_BACKEND=com, needs Windows + AutoCAD).",
        )

    if ent_type == "CIRCLE":
        radius = float(ent.dxf.radius)
        result["area"] = math.pi * radius * radius
        result["perimeter"] = 2.0 * math.pi * radius
        return _round_measure(result)

    if ent_type == "ELLIPSE":
        major = Vec2(ent.dxf.major_axis).magnitude
        span = abs(float(ent.dxf.end_param) - float(ent.dxf.start_param))
        if abs(span - 2.0 * math.pi) < 1e-9:
            result["area"] = measure.ellipse_area(major, float(ent.dxf.ratio))
            result["perimeter"] = measure.ellipse_perimeter_ramanujan(major, float(ent.dxf.ratio))
            result["method"] = "analytic_ellipse"
            # Exact area, approximate perimeter — the two are reported separately
            # so neither claim borrows credibility from the other.
            result["perimeter_exact"] = False
            return _round_measure(result)
        return _round_measure(_flatten_measure(ent, tolerance, result))

    if ent_type == "SPLINE":
        return _round_measure(_flatten_measure(ent, tolerance, result))

    if ent_type == "HATCH":
        return _measure_hatch(ent, tolerance, result)

    vertices = None
    if ent_type == "LWPOLYLINE":
        vertices = [(float(x), float(y), float(b)) for x, y, b in ent.get_points(format="xyb")]
        result["closed"] = bool(ent.closed)
    elif ent_type == "POLYLINE":
        mode = ent.get_mode()
        if mode != "AcDb2dPolyline":
            raise RuntimeError(
                f"entity_measure: handle {handle} is a {mode}, which does not bound "
                f"a planar area. Measurable types: {_MEASURABLE_TYPES}."
            )
        vertices = [
            (
                float(v.dxf.location.x),
                float(v.dxf.location.y),
                float(v.dxf.get("bulge", 0.0) or 0.0),
            )
            for v in ent.vertices
        ]
        result["closed"] = bool(ent.is_closed)
    elif ent_type in ("SOLID", "TRACE", "3DFACE"):
        corners = [ent.dxf.get(name) for name in ("vtx0", "vtx1", "vtx3", "vtx2")]
        vertices = [(float(c.x), float(c.y), 0.0) for c in corners if c is not None]

    if vertices is None:
        raise RuntimeError(
            f"entity_measure: handle {handle} is {ent_type}, which does not bound an "
            f"area. Measurable types: {_MEASURABLE_TYPES}."
        )

    result["assumed_closed"] = not result["closed"]
    result["area"], result["perimeter"] = measure.polygon_area_perimeter(vertices, result["closed"])
    result["method"] = "analytic_bulge" if any(abs(v[2]) > 1e-12 for v in vertices) else "analytic"
    result["self_intersecting"] = measure.is_self_intersecting([(v[0], v[1]) for v in vertices])
    return _round_measure(result)


def _flatten_measure(ent, tolerance: float, result: dict) -> dict:
    """Fall back to a dense polyline approximation, and say that is what it is."""
    from ezdxf import path as _path

    pth = _path.make_path(ent)
    points = [(float(v.x), float(v.y)) for v in pth.flattening(distance=tolerance)]
    if len(points) < 3:
        raise RuntimeError(
            f"entity_measure: handle {result['handle']} produced no measurable outline."
        )
    result["closed"] = bool(pth.is_closed)
    result["assumed_closed"] = not result["closed"]
    result["area"], result["perimeter"] = measure.polygon_area_perimeter(
        [(x, y, 0.0) for x, y in points], result["closed"]
    )
    result["method"] = "flattened"
    result["exact"] = False
    result["perimeter_exact"] = False
    result["flatten_tolerance"] = tolerance
    result["self_intersecting"] = measure.is_self_intersecting(points)
    return result


#: ISO-conformant dimension style for a new headless document, in mm.
#:
#: ezdxf's bare `Standard` dimstyle leaves every one of these unset, and the
#: renderer then falls back to its own defaults rather than to the DXF schema
#: defaults -- 1.0 mm text with a *comma* decimal marker. ISO 3098 puts the
#: minimum lettering height at 2.5 mm and ISO 129 wants a point, so every
#: headless drawing this server made was non-conformant on both counts while
#: the document header claimed `$DIMTXT 2.5`.
#:
#: `ezdxf.new(setup=True)` is not the fix: measured, it renders a 100 mm
#: dimension as "10000" at 0.25 mm text, because its metric styles carry a
#: length factor. Setting the attributes explicitly is what reaches the
#: renderer, because only a *stored* value does.
_ISO_DIMSTYLE = {
    "dimtxt": 2.5,  # ISO 3098 minimum lettering height
    "dimasz": 2.5,  # arrowhead, matched to the text
    "dimdsep": ord("."),  # ISO 129 decimal marker; ezdxf defaults to a comma
    "dimdec": 2,
    "dimexe": 1.25,  # extension line beyond the dimension line
    "dimexo": 0.625,  # offset from the measured feature
    "dimgap": 0.625,  # gap around the text
}


def _normalise_dimstyle_rounding(doc) -> None:
    """Discard a stored ``dimrnd`` of 0.0, which AutoCAD means as *no* rounding.

    ezdxf applies ``xround(value, dimrnd)`` whenever the attribute is present at
    all, and ``xround(33.333, 0.0)`` is ``33``. Saving a drawing writes the
    attribute out, so every reopened file — including one this server saved
    itself — silently rounded every dimension to a whole unit *before* DIMDEC
    got to format it: a 12.75 mm feature dimensioned as 13, R6.35 as R6.

    Only the 0.0 sentinel is removed. A drafter who genuinely asked for 0.5 mm
    rounding keeps it.
    """
    try:
        style = doc.dimstyles.get("Standard")
    except Exception:
        return
    if style.dxf.hasattr("dimrnd") and float(style.dxf.dimrnd) == 0.0:
        style.dxf.discard("dimrnd")


def _apply_iso_dimstyle(doc) -> None:
    """Store ISO defaults on the document's `Standard` dimension style."""
    try:
        style = doc.dimstyles.get("Standard")
    except Exception:  # a template without a Standard style is the caller's own
        return
    for attribute, value in _ISO_DIMSTYLE.items():
        style.dxf.set(attribute, value)
    doc.header["$DIMTXT"] = _ISO_DIMSTYLE["dimtxt"]
    doc.header["$DIMASZ"] = _ISO_DIMSTYLE["dimasz"]
    doc.header["$DIMDSEP"] = _ISO_DIMSTYLE["dimdsep"]
    doc.header["$DIMDEC"] = _ISO_DIMSTYLE["dimdec"]


#: AutoCAD's HPISLANDDETECTION, by the code DXF stores in group 75.
_HATCH_STYLE_NAMES = {0: "normal", 1: "outer", 2: "ignore"}


def _nesting_depths(loops: list[list[tuple[float, float]]]) -> list[int]:
    """How many other loops each loop sits inside.

    Read off containment rather than off the boundary-path flags, because the
    flags do not carry it: DXF marks a path external or outermost, never "two
    levels down". Depth is precisely where AutoCAD's three island styles stop
    agreeing, so guessing it would make `normal` and `outer` return the same
    number and quietly be wrong for one of them.

    Hatch boundary loops do not cross, so any vertex of the inner loop settles
    the question — the first one that is not sitting exactly on the other
    loop's edge decides.
    """
    from ezdxf import edgesmith
    from ezdxf.math import Vec2

    rings = [[Vec2(x, y) for x, y in points] for points in loops]
    depths = []
    for index, points in enumerate(loops):
        depth = 0
        for other, ring in enumerate(rings):
            if other == index:
                continue
            for x, y in points:
                verdict = edgesmith.is_point_in_polygon_2d(Vec2(x, y), ring)
                if verdict != 0:
                    depth += verdict > 0
                    break
        depths.append(depth)
    return depths


def _measure_hatch(ent, tolerance: float, result: dict) -> dict:
    """The area a hatch actually fills: outer loops minus the islands in them.

    Counting the islands as filled is a 25% error on the test fixture here and
    an unbounded one on a real section view, which is mostly holes. Which loops
    count is not a house rule — it is `hatch_style`, reported back so the number
    can be read against the drawing's own island setting.

    On a curved edge `flatten_tolerance` is not the accuracy knob it looks
    like. ezdxf hands boundary paths over as cubic Beziers, so a circular edge
    already carries the ~0.027% Bezier-vs-circle error before anything is
    flattened, and tightening the tolerance converges on that wrong number
    rather than on the true one. `exact` goes False for exactly this reason.
    """
    from ezdxf import path as _path

    outlines: list[list[tuple[float, float]]] = []
    perimeter = 0.0
    curved = False
    open_path = False
    for boundary in ent.paths:
        pth = _path.from_hatch_boundary_path(boundary)
        curved = curved or pth.has_curves
        open_path = open_path or not pth.is_closed
        points = [(float(v.x), float(v.y)) for v in pth.flattening(distance=tolerance)]
        if len(points) < 3:
            continue
        outlines.append(points)
        perimeter += measure.polygon_area_perimeter([(x, y, 0.0) for x, y in points], True)[1]

    if not outlines:
        raise RuntimeError(
            f"entity_measure: handle {result['handle']} is a HATCH whose boundary paths "
            "enclose nothing. A hatch that bounds no loop fills no area."
        )

    style_code = int(ent.dxf.get("hatch_style", 0) or 0)
    style = _HATCH_STYLE_NAMES.get(style_code, "normal")
    depths = _nesting_depths(outlines)

    area = 0.0
    for points, depth in zip(outlines, depths, strict=True):
        if style == "ignore" and depth > 0:
            continue  # HPISLAND=2 fills straight over every island
        if style == "outer" and depth > 1:
            continue  # only the outermost ring and the holes directly in it
        signed = -1.0 if depth % 2 else 1.0
        area += signed * abs(
            measure.polygon_area_perimeter([(x, y, 0.0) for x, y in points], True)[0]
        )

    verdicts = [measure.is_self_intersecting(points) for points in outlines]
    result["area"] = abs(area)
    result["perimeter"] = perimeter
    result["method"] = "hatch_loops"
    result["hatch_style"] = style
    result["loop_count"] = len(outlines)
    result["closed"] = True
    result["assumed_closed"] = open_path
    result["exact"] = not curved
    result["perimeter_exact"] = not curved
    result["flatten_tolerance"] = tolerance if curved else None
    result["self_intersecting"] = (
        True
        if any(v is True for v in verdicts)
        else (False if any(v is False for v in verdicts) else None)
    )
    return _round_measure(result)


#: The dimension style every headless dimension is rendered from.
#:
#: `msp.add_*_dim` defaults to `dimstyle="EZDXF"` (`"EZ_RADIUS"` for the radial
#: ones) and ezdxf silently substitutes `Standard` when that entry is missing —
#: which it is in every document this server creates, so the five call sites got
#: `Standard` by accident rather than by name. Naming it keeps three things
#: pointed at one style: `_apply_iso_dimstyle`, which writes it; the `iso128`
#: critique, which grades it; and the header fold below, which compares against
#: it. A file authored by `ezdxf.new(setup=True)` *does* carry an `EZDXF` entry,
#: and its metric styles carry a length factor — measured, that renders a 100 mm
#: dimension as "10000" — so on an opened file the substitution would have
#: pointed the dimension at a style nobody here configured and left both the
#: fold and the ISO defaults inert.
_DIMSTYLE_NAME = "Standard"


#: DIM* header variables folded onto the dimension being created, as
#: ``dimstyle attribute -> (header variable, renderer fallback, coercion)``.
#:
#: The third column is what ezdxf's *renderer* resolves the attribute to when
#: the dimstyle stores nothing — `DimStyleOverride.get(attr, default)` with the
#: defaults spelled out in `render/dim_base.py` and `get_decimal_separator`.
#: Deliberately NOT the DXF schema default, which differs for three of the six:
#: dimtxt 1.0 vs 2.5, dimasz 0.25 vs 2.5, dimdsep 0 (a comma) vs 44. Keying the
#: "header already agrees, nothing to fold" test on the schema default is the
#: invisible way to get this wrong — it switches the fold off for exactly the
#: opened-file case it exists for, and switches it *on* for a fresh drawing,
#: where folding dimdsep would put ezdxf's comma back onto a sheet ISO 129 wants
#: a point on.
#:
#: This is an allowlist, not "every DIM* variable". $DIMADEC and $DIMAZIN sit at
#: 0 in a fresh header while the renderer falls back to 2 and 2, so completing
#: the table would silently reformat every angular dimension this server has
#: ever drawn (measured: 239.04° becomes 239°). $DIMGAP is the other one that
#: looks safe and is not: `build_dim_override` sets dimgap=-1.0 for
#: `tol_mode="basic"` to draw the theoretically-exact box, and a fold would be
#: competing with it for the same key.
_HEADER_DIMVARS: dict[str, tuple[str, Any, Any]] = {
    "dimscale": ("$DIMSCALE", 1.0, float),
    "dimtxt": ("$DIMTXT", 1.0, float),
    "dimasz": ("$DIMASZ", 0.25, float),
    "dimdec": ("$DIMDEC", 2, int),
    "dimdsep": ("$DIMDSEP", 0, int),  # a character code; 0 renders as a comma
    "dimzin": ("$DIMZIN", 8, int),
}


def _coerce_dimvar(value, coerce):
    """The value as the override dict needs it, or None if it is junk.

    ezdxf validates the *header* (a string in `$DIMDSEP` raises DXFValueError)
    but not the override dict, so a float 46.0 or a stray string reaching
    `dimdsep` renders garbage instead of raising. Coercing here is what keeps an
    opened file's odd header out of the drawing.
    """
    try:
        return coerce(value)
    except (TypeError, ValueError):
        return None


def _header_dimvar(doc, header_var: str, coerce):
    """The header's value for a DIM* variable, or None if it has none usable."""
    try:
        raw = doc.header.get(header_var, None)
    except Exception:  # an opened document may not have a readable header
        return None
    return None if raw is None else _coerce_dimvar(raw, coerce)


def _style_dimvar(doc, attribute: str, fallback, coerce):
    """What the renderer would use with no header and no override in play."""
    try:
        style = doc.dimstyles.get(_DIMSTYLE_NAME)
        if style.dxf.hasattr(attribute):
            stored = _coerce_dimvar(style.dxf.get(attribute), coerce)
            if stored is not None:
                return stored
    except Exception:  # a document without a Standard style is the caller's own
        pass
    return fallback


def effective_dimvar(doc, attribute: str):
    """The value the next dimension will actually be rendered with.

    Header first, then the stored dimstyle attribute, then ezdxf's renderer
    fallback — the order `_with_header_dimvars` produces below, and the order a
    live seat uses (the header DIMVARS *are* the current settings; the dimstyle
    table entry is only the base underneath them). `drawing_critique` has to
    resolve the same value, or it reports a defect the sheet does not have and
    clears one it does.
    """
    header_var, fallback, coerce = _HEADER_DIMVARS[attribute]
    from_header = _header_dimvar(doc, header_var, coerce)
    if from_header is not None:
        return from_header
    return _style_dimvar(doc, attribute, fallback, coerce)


def _with_header_dimvars(doc, override: dict | None) -> dict | None:
    """Carry the DIM* header variables onto the dimension being created.

    `system_set_variable("DIMTXT", 5.0)` writes the header variable, which is
    what AutoCAD consults when it builds a dimension — so the call worked on the
    live backend. ezdxf renders from the dimstyle table entry plus the
    per-dimension override and never reads the header, so headlessly the same
    call reported success and changed nothing. Measured on v1.5.1: after
    DIMTXT=5.0, DIMASZ=6.0, DIMDEC=1, DIMDSEP=44 and DIMZIN=0 the header read
    all five back, and the next dimension was byte-identical to the one before
    them — still 2.5 mm text, 2.5 arrows, "33.33". Folding them into the
    override makes one call mean one thing on both engines.

    v1.5.1 folded `$DIMSCALE` alone, which is why this is a table rather than a
    fifth copy of the same six lines.

    A variable whose header value already matches what the renderer would use is
    skipped, so an untouched `drawing_new` drawing gains no override at all —
    header and style agree on all six there. `setdefault` is what keeps the fold
    below `build_dim_override`: an explicit tolerance/limit/basic key, or any
    value a caller passed, always outranks the header.
    """
    merged = override
    for attribute, (header_var, fallback, coerce) in _HEADER_DIMVARS.items():
        from_header = _header_dimvar(doc, header_var, coerce)
        if from_header is None:
            continue
        if attribute == "dimscale" and not from_header:
            # DIMSCALE 0 means "scale to the paper-space viewport". There is no
            # viewport to scale to here, and folding a literal 0 would render
            # the dimension to nothing; 1.0 is what the renderer already uses.
            continue
        stored = _style_dimvar(doc, attribute, fallback, coerce)
        if isinstance(from_header, float):
            if abs(from_header - float(stored)) < 1e-9:
                continue
        elif from_header == stored:
            continue
        if merged is override:  # only allocate once something is actually folded
            merged = dict(override or {})
        merged.setdefault(attribute, from_header)
    return merged


def _round_measure(result: dict) -> dict:
    """Six decimal places, matching analysis_measure_distance."""
    result["area"] = round(float(result["area"]), 6)
    result["perimeter"] = round(float(result["perimeter"]), 6)
    return result


#: How many handle labels a grounded screenshot will draw before it stops and
#: says so. Past this the labels bury the geometry they exist to explain.
DEFAULT_MAX_HANDLE_LABELS = 40


def _label_anchor(ent) -> tuple[float, float] | None:
    """Where an entity's handle label goes — the centre of its own extents.

    Uses the renderer's own bbox rather than the entity's stored coordinates, so
    the label lands where the entity is *drawn* even in a rotated frame (T0.4).
    """
    try:
        from ezdxf import bbox as _bbox

        box = _bbox.extents([ent])
        if not box.has_data:
            return None
        return (
            (float(box.extmin.x) + float(box.extmax.x)) / 2.0,
            (float(box.extmin.y) + float(box.extmax.y)) / 2.0,
        )
    except Exception as exc:
        log.debug("no label anchor for %s: %s", ent.dxftype(), exc)
        return None


_IDENTITY_EXTRUSION = (0.0, 0.0, 1.0)

#: The DXF types whose stored geometry is in the entity's own frame. LINE, MTEXT,
#: ELLIPSE, SPLINE and POINT carry an `extrusion` attribute too, and are already
#: correct — ezdxf overrides their `ocs()` to a pass-through — so any fix keyed on
#: "has extrusion" would break them.
_OCS_ENTITY_TYPES = frozenset(
    {
        "CIRCLE",
        "ARC",
        "LWPOLYLINE",
        "TEXT",
        "INSERT",
        "DIMENSION",
        "DIMLINEAR",
        "DIMALIGNED",
        "DIMANGULAR",
        "DIMRADIUS",
        "DIMDIAMETER",
        "DIMORDINATE",
    }
)


def _entity_info_dxf(ent) -> EntityInfo:
    """Convert an ezdxf entity to EntityInfo. Coordinates out are always WCS."""
    handle = ent.dxf.get("handle", "?")
    ent_type = ent.dxftype()
    layer = ent.dxf.get("layer", "0")
    color = ent.dxf.get("color", 256)
    linetype = ent.dxf.get("linetype", "ByLayer")
    visible = ent.dxf.get("invisible", False) is False

    props: dict = {}

    # T0.4 — everything leaving this function is WCS. CIRCLE/ARC/LWPOLYLINE/TEXT/
    # INSERT and a dimension's text position are stored in the entity's own frame,
    # so a raw read is not where the entity is; `bounding_box` below already
    # reports WCS, which is how the contradiction was found. `flat` distinguishes
    # the two kinds of non-identity frame: a mirrored entity (extrusion -Z) still
    # lies in the WCS XY plane and every field survives translation, while a
    # tilted one projects to something xy cannot describe.
    extrusion = _IDENTITY_EXTRUSION
    try:
        if ent.dxf.is_supported("extrusion"):
            extrusion = tuple(ent.dxf.get("extrusion", _IDENTITY_EXTRUSION))
    except Exception as exc:
        log.debug("reading extrusion for %s: %s", ent_type, exc)
    needs_ocs = ent_type in _OCS_ENTITY_TYPES and not ocs.is_wcs_frame(extrusion)
    flat = ocs.is_flat_frame(extrusion)

    def _v2(v):
        """Convert Vec3/tuple to [x, y] list."""
        return [float(v.x), float(v.y)] if hasattr(v, "x") else [float(v[0]), float(v[1])]

    def _pt(v):
        """A stored point as WCS [x, y] — the only spelling that leaves here."""
        x, y, z = (
            (float(v.x), float(v.y), float(getattr(v, "z", 0.0)))
            if hasattr(v, "x")
            else (float(v[0]), float(v[1]), float(v[2]) if len(v) > 2 else 0.0)
        )
        return ocs.to_wcs_2d(extrusion, x, y, z) if needs_ocs else [x, y]

    try:
        if ent_type == "LINE":
            props["start"] = _v2(ent.dxf.start)
            props["end"] = _v2(ent.dxf.end)
            props["length"] = ent.dxf.start.distance(ent.dxf.end)
        elif ent_type == "CIRCLE":
            props["center"] = _pt(ent.dxf.center)
            # A tilted circle projects to an ellipse (measured: radius 5.0 has a
            # 5.0 x 3.99 WCS-XY footprint), so `radius` has no meaning in the
            # frame this dict is expressed in. Omitting it is silence; reporting
            # it would be a fresh silent wrong number.
            if flat:
                props["radius"] = ent.dxf.radius
        elif ent_type == "ARC":
            props["center"] = _pt(ent.dxf.center)
            if flat:
                props["radius"] = ent.dxf.radius
                props["start_angle"], props["end_angle"] = (
                    ocs.wcs_arc_angles(
                        extrusion,
                        ent.dxf.center,
                        ent.dxf.radius,
                        ent.dxf.start_angle,
                        ent.dxf.end_angle,
                    )
                    if needs_ocs
                    else (ent.dxf.start_angle, ent.dxf.end_angle)
                )
            # Arc length (N3) so entity_select_smart length_range works on ARCs.
            # Computed from the ORIGINAL sweep: length is frame-invariant, and the
            # WCS pair above is swapped for a left-handed frame.
            _sweep = (ent.dxf.end_angle - ent.dxf.start_angle) % 360.0
            props["length"] = ent.dxf.radius * math.radians(_sweep)
        elif ent_type == "LWPOLYLINE":
            # LWPOLYLINE vertices carry no z of their own; the whole polyline
            # sits at dxf.elevation within its frame.
            _elev = float(ent.dxf.get("elevation", 0.0))
            props["points"] = [
                (
                    ocs.to_wcs_2d(extrusion, pt[0], pt[1], _elev)
                    if needs_ocs
                    else [float(pt[0]), float(pt[1])]
                )
                for pt in ent.get_points()
            ]
            props["closed"] = ent.closed
            # `ent.length()` used to be here. LWPolyline has no such method, so
            # the AttributeError went into the debug log below and the key simply
            # never appeared — and a caller shoelacing the xy points above to
            # recover it lost 28.2% of the area on bulged geometry. Both come
            # from the same exact, bulge-aware maths as entity_measure.
            _verts = [(float(x), float(y), float(b)) for x, y, b in ent.get_points(format="xyb")]
            _area, _length = measure.polygon_area_perimeter(_verts, bool(ent.closed))
            props["length"] = round(_length, 6)
            if ent.closed:
                props["area"] = round(_area, 6)
        elif ent_type == "TEXT":
            props["text"] = ent.dxf.text
            props["insertion"] = _pt(ent.dxf.insert)
            props["height"] = ent.dxf.height
            # `rotation` stays in the entity's own frame. A mirrored TEXT is
            # mirror-imaged, and no single scalar angle expresses that — measured
            # 29.999 reported against a true WCS 150.0 — so normalising it would
            # replace one wrong number with another. Named in the
            # `ocs_normalized` capability reason instead.
            props["rotation"] = ent.dxf.get("rotation", 0.0)
        elif ent_type == "MTEXT":
            props["text"] = ent.text
            props["insertion"] = _v2(ent.dxf.insert)
            props["char_height"] = ent.dxf.char_height
            props["rotation"] = ent.dxf.get("rotation", 0.0)
        elif ent_type == "INSERT":
            props["block_name"] = ent.dxf.name
            props["insertion"] = _pt(ent.dxf.insert)
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
        elif ent_type in (
            "DIMENSION",
            "DIMLINEAR",
            "DIMALIGNED",
            "DIMANGULAR",
            "DIMRADIUS",
            "DIMDIAMETER",
            "DIMORDINATE",
        ):
            props["dim_type"] = ent_type
            # Reference points for dim_overlap critique. ezdxf dimensions carry
            # a definition point and (usually) the text midpoint.
            try:
                # defpoint is DXF group code 10 and WCS already — ezdxf's
                # Dimension.transform partitions exactly here, sending defpoint
                # through the raw matrix and only text_midpoint through the OCS.
                # Translating it would corrupt a correct field.
                props["defpoint"] = _v2(ent.dxf.defpoint)
            except Exception:
                pass
            try:
                tm = ent.dxf.get("text_midpoint", None)
                if tm is not None:
                    props["text_position"] = _pt(tm)  # group code 11 — OCS
            except Exception:
                pass
    except Exception as exc:
        log.debug("extracting entity properties for %s: %s", ent_type, exc)

    if needs_ocs and not flat:
        # The frame cannot be flattened into xy without losing information, so
        # say which plane the entity is in rather than leaving the omissions
        # above unexplained.
        props["plane_normal"] = ocs.plane_normal(extrusion)

    # bounding_box parity with the COM backend (same {min,max} shape) so clients
    # reading properties["bounding_box"] work on both engines (N5).
    try:
        from ezdxf import bbox as _bbox

        _bb = _bbox.extents([ent])
        if _bb.has_data:
            props["bounding_box"] = {
                "min": [float(_bb.extmin.x), float(_bb.extmin.y)],
                "max": [float(_bb.extmax.x), float(_bb.extmax.y)],
            }
    except Exception as exc:
        log.debug("bbox extents failed for %s: %s", ent_type, exc)

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
        self._undo_stack: list[Path] = []  # user-facing undo history
        self._redo_stack: list[Path] = []  # states stepped off by an undo
        self._current_space: str = "Model"  # where entity_create_* writes
        self._transaction_stack: list[Path] = []  # isolated transaction snapshots
        self._connected = False
        self._lock = asyncio.Lock()
        # R32: the document a timed-out call was abandoned on is untrusted until
        # it is replaced. See backends/quarantine.py for what that buys.
        self._quarantine: QuarantineRecord | None = None
        self._abandoned_calls: list[QuarantineRecord] = []

    @property
    def name(self) -> str:
        return "ezdxf"

    @property
    def is_connected(self) -> bool:
        return self._connected

    def capabilities(self) -> CapabilityMap:
        render_available = find_spec("matplotlib") is not None
        render_capability = (
            FeatureCapability(True, "rendered")
            if render_available
            else FeatureCapability(False, reason="optional_dependency_missing:matplotlib")
        )
        return CapabilityMap(
            backend="ezdxf",
            features={
                "drawing_2d": FeatureCapability(True, "native"),
                "dxf": FeatureCapability(True, "native"),
                "dwg": FeatureCapability(False, reason="no_dwg_writer_requires_live_autocad"),
                "pdf": render_capability,
                "png": render_capability,
                "transactions": FeatureCapability(True, "snapshot"),
                "audit_detail": FeatureCapability(True, "native"),
                "handle_overlay": render_capability,
                "revcloud": FeatureCapability(True, "native", reason="ezdxf_revcloud_helper"),
                "wipeout": FeatureCapability(True, "native"),
                "mtext_background_color": FeatureCapability(True, "native"),
                "boundary_trace": FeatureCapability(
                    True,
                    "computed",
                    reason="straight_edges_split_at_intersections;curved_edges_not_split",
                ),
                "hatch_edge_paths": FeatureCapability(True, "native"),
                "chspace": FeatureCapability(
                    True,
                    "computed",
                    reason=(
                        "top_view_untwisted_viewports_only;dimensions_refused_unless_frozen;"
                        "acis_proxy_table_refused;viewport_clipping_reported_not_applied"
                    ),
                ),
                "undo_history": (
                    FeatureCapability(True, "snapshot")
                    if config.settings.ezdxf_undo_depth > 0
                    else FeatureCapability(False, reason="set_EZDXF_UNDO_DEPTH_to_enable")
                ),
                "measure_area_acis": FeatureCapability(
                    False, reason="acis_evaluation_requires_live_autocad"
                ),
                "ocs_normalized": FeatureCapability(
                    True,
                    "wcs",
                    reason=(
                        "reported_xy_is_wcs;z_component_not_reported;"
                        "text_rotation_stays_in_entity_frame;hatch_boundaries_not_reported"
                    ),
                ),
                "ocs_tilted_plane": FeatureCapability(
                    False, reason="2d_xy_cannot_address_a_tilted_plane"
                ),
                "table": FeatureCapability(True, "composite"),
                "mleader": FeatureCapability(True, "composite"),
                "preflight": FeatureCapability(True, "shared"),
                "refiner": FeatureCapability(True, "shared"),
                "delivery": FeatureCapability(True, "shared"),
                "paper_space": FeatureCapability(True, "native"),
                # Measured, not assumed: a sheet holding nothing but a viewport
                # renders the model geometry that viewport looks at, at its
                # scale and position, clipped to its window. v1.4 declared this
                # COM-only and sent callers to the live backend for something
                # the headless one already did.
                "viewport_render": (
                    FeatureCapability(True, "rendered", reason="viewport_borders_not_drawn")
                    if render_available
                    else FeatureCapability(False, reason="optional_dependency_missing:matplotlib")
                ),
                "solid_3d": FeatureCapability(
                    False, reason="acis_generation_requires_live_autocad"
                ),
                "lisp": FeatureCapability(False, reason="live_com_only"),
            },
        )

    async def connect(self) -> None:
        self._connected = True
        log.info("ezdxf backend ready")

    async def disconnect(self) -> None:
        self._cleanup_undo_stack()
        self._reset_document_state()
        # The document goes away with the backend, so nothing is left to protect.
        # The record stays in _abandoned_calls for anyone reading the transcript.
        self._clear_quarantine("disconnect")
        self._connected = False

    def _cleanup_undo_stack(self):
        for stack in (self._undo_stack, self._redo_stack, self._transaction_stack):
            while stack:
                p = stack.pop()
                try:
                    p.unlink()
                except OSError:
                    pass

    def _require_doc(self):
        if self._doc is None:
            raise RuntimeError("No document open. Call drawing_new() or drawing_open() first.")
        return self._doc

    def _msp(self):
        """The space new geometry goes into — model space, or the current layout.

        This returned ``doc.modelspace()`` unconditionally, so
        ``layout_set_current("A3-Sheet")`` reported success and then every
        entity created afterwards still landed in model space. Following the
        current layout is also what makes a title block on a sheet possible at
        all: the title block draws through the same ``entity_create_*`` calls.
        """
        doc = self._require_doc()
        if self._current_space == "Model":
            return doc.modelspace()
        try:
            return doc.layouts.get(self._current_space)
        except Exception as exc:
            # A layout deleted out from under us must not strand every later
            # write; fall back to model space and say why.
            log.warning(
                "current layout %r is gone (%s); falling back to model space",
                self._current_space,
                exc,
            )
            self._current_space = "Model"
            return doc.modelspace()

    @staticmethod
    def _busy_error(timeout: float) -> RuntimeError:
        return RuntimeError(
            "ezdxf backend busy: another call has held the document lock for more "
            f"than {timeout:g}s. Retry once it finishes, or raise EZDXF_CALL_TIMEOUT "
            "(seconds; 0 disables the timeout entirely)."
        )

    @staticmethod
    def _timeout_error(call: str, timeout: float) -> RuntimeError:
        return RuntimeError(
            f"ezdxf call '{call}' timed out after {timeout:g}s. asyncio.to_thread "
            "cannot be cancelled, so the worker thread was abandoned and may still be "
            "writing to the drawing. Every later call on this document is refused - "
            "reads included - until drawing_open(path), drawing_new() or "
            "drawing_close() rebinds it, or until the abandoned call is seen to finish "
            "cleanly, which lifts the refusal by itself. system_status() stays "
            "answerable throughout. Raise EZDXF_CALL_TIMEOUT (seconds; 0 disables the "
            "timeout entirely) if this operation is legitimately slow."
        )

    # ── document quarantine (R32) ─────────────────────────────────────────────

    def _reap_quarantine(self) -> None:
        """The free exit: an abandoned call that finished cleanly is no corruption.

        The common real trigger for the deadline is a legitimately slow
        ``drawing_audit`` or a big ``drawing_export_pdf`` against the 120s
        default, not a hang. If the ``_sync`` body ran to completion the document
        holds exactly the state that call intended, and forcing a reopen for that
        would be its own defect.

        This inference is sound *only* because every other caller was refused in
        the meantime, so "the call finished" really does mean "the document is
        what that call intended". Add one mutating exemption to the gate and the
        lift becomes a lie.

        The lift is not silent: the caller never received the return value, so a
        late-completing ``entity_create_*`` leaves an entity whose handle nobody
        holds. That fact survives in the record.
        """
        record = self._quarantine
        if record is None or not record.watcher.finished:
            return
        if record.watcher.error is not None:
            if record.outcome == "running":
                error = record.watcher.error
                record.outcome = "failed_after_deadline"
                record.detail = f"{type(error).__name__}: {error}"
                log.error(
                    "abandoned ezdxf call %r raised %s after its deadline; the document "
                    "stays quarantined - it was left mid-write",
                    record.call,
                    record.detail,
                )
            return
        record.outcome = "completed_after_deadline"
        record.capture_lost_result()
        self._quarantine = None
        log.warning(
            "abandoned ezdxf call %r completed %.1fs after its deadline; lifting the "
            "document quarantine. Its return value never reached the caller (%s), so "
            "anything it created is in the drawing with no handle reported",
            record.call,
            record.age(),
            record.result_repr,
        )

    def _clear_quarantine(self, released_by: str, new_path: str | None = None):
        """Release the quarantine because ``self._doc`` was just rebound.

        Only the three calls that construct a *fresh* ``Drawing`` may do this:
        the runaway keeps the object it captured, so its remaining writes land on
        an orphan nobody reads. That holds for a ``_sync`` which captured
        doc/msp once at entry — the shape of nearly every closure in this file.
        One that re-reads ``self._msp()`` inside its own loop would follow the
        rebind into the fresh document, and ``_async`` cannot prevent that, which
        is why a still-live runaway is said out loud rather than implied away.
        """
        record = self._quarantine
        if record is None:
            return None
        self._quarantine = None
        record.outcome = "released"
        record.released_by = released_by
        if record.thread_alive():
            record.detail = "the abandoned worker was still running at recovery time"
            log.warning(
                "%s cleared the document quarantine while the abandoned %r worker is "
                "STILL RUNNING; its writes go to the replaced Drawing unless its _sync "
                "re-reads self._msp() inside its own loop",
                released_by,
                record.call,
            )
        if new_path and record.document_path:
            try:
                same = os.path.abspath(new_path) == os.path.abspath(record.document_path)
            except (OSError, ValueError):  # a path this OS cannot normalise
                same = new_path == record.document_path
            if same:
                log.warning(
                    "%s is reopening %s, the same path the abandoned %r call was working "
                    "against; if that call is inside doc.saveas the file is being written "
                    "right now and this read may be of a truncated document",
                    released_by,
                    new_path,
                    record.call,
                )
        return record

    def _quarantine_document(self, func, watcher, timeout: float) -> str:
        """Record an abandoned call and refuse the document until it is replaced.

        ``func`` is the caller's closure (it carries the qualname the message
        needs); ``watcher`` is the wrapper the worker thread is actually running,
        and the only thing that can report whether it ever finished.
        """
        call = real_call_name(func)
        record = QuarantineRecord(
            call,
            timeout,
            watcher,
            document_path=self._doc_path,
            document_id=id(self._doc),
        )
        self._quarantine = record
        self._abandoned_calls.append(record)
        del self._abandoned_calls[:-_MAX_ABANDONED_RECORDS]
        return call

    async def _async(self, func, *args, quarantine_exit: bool = False, **kwargs):
        """Run a blocking ezdxf call in a worker thread, serialised and timed out.

        The ezdxf ``Drawing`` is not thread-safe, so every backend method funnels
        through one lock. Without a timeout a single hung call held that lock
        forever and every later tool call on the server deadlocked behind it.

        ``EZDXF_CALL_TIMEOUT`` (seconds, ``0`` disables) bounds the *whole* wait —
        queueing behind an in-flight call plus the call itself — mirroring
        ``ComBackend._run``, where the executor queue wait sits inside the same
        ``wait_for``.

        Lock ownership on timeout: ``asyncio.to_thread`` cannot be cancelled, so
        the worker thread outlives the timeout and may still be mutating the
        document. The timed-out call therefore deliberately **never releases its
        lock**; the backend swaps in a fresh ``asyncio.Lock`` and leaves the
        abandoned call holding the orphaned one. The runaway thread can then never
        hand the lock to new work mid-write, and the next tool call proceeds
        immediately instead of deadlocking — the same abandon-and-rebuild shape the
        COM backend uses for its stuck STA executor. Callers already queued on the
        orphaned lock get the explicit "backend busy" error rather than silently
        interleaving with the runaway thread. Timing out while merely *waiting*
        for the lock does not abandon it: that lock is held by a live call which
        will release it normally. Outside cancellation (client disconnect) also
        releases normally, as it did before the timeout existed.

        R32 — swapping the lock is only half the answer, and on its own it is the
        more dangerous half: it removes the mutual exclusion the lock existed
        for, so the *next* call runs against the same ``Drawing`` the runaway is
        still mutating. Measured, that returned a fresh circle handle, an entity
        count for a modelspace 50 entities further on, and a valid DXF of a state
        that never existed. So the abandonment quarantines the document, and
        every later call is refused **before** ``lock.acquire()`` — after the
        acquire the refusal would queue behind an unrelated in-flight call and
        the caller would wait the full timeout to be told the document is
        untrusted. Reads are refused too: a read of a container another thread is
        appending to does not tear loudly, it runs unbounded — ``drawing_info``
        blew its own 2.0s deadline at 2.03s and abandoned a *second* worker.

        ``quarantine_exit=True`` is for the three calls that rebind ``self._doc``
        to a freshly constructed object. They still take the lock, because two
        concurrent recoveries would otherwise both rebind it and one fresh
        document would be silently discarded with its caller's work in it.
        """
        # The gate, before any await: a refusal must not queue.
        if self._quarantine is not None:
            self._reap_quarantine()
            if self._quarantine is not None and not quarantine_exit:
                raise quarantine_refusal(self._quarantine)

        timeout = config.settings.ezdxf_call_timeout
        if timeout <= 0:
            # No deadline means no abandon branch, so nothing here can quarantine.
            async with self._lock:
                return await asyncio.to_thread(func, *args, **kwargs)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        lock = self._lock
        # One `asyncio.timeout_at` for both phases rather than two
        # `asyncio.wait_for`s. `wait_for` wraps its awaitable in a Task, so the
        # old shape cost two extra Tasks and two timer handles on *every* call
        # — 4,000 of each to draw 2,000 lines, which is where the throughput
        # this release lost was going. `acquired` keeps the two failure modes
        # distinguishable, which is the whole reason they were separate.
        acquired = False
        call: asyncio.Future | None = None
        # The only place an abandoned thread's outcome is observable is inside
        # the thread: `call` is CANCELLED by the deadline, so its done-callback
        # reports the await ending and never the worker. See AbandonedCall.
        watched = AbandonedCall(func)
        try:
            async with asyncio.timeout_at(deadline):
                await lock.acquire()
                acquired = True
                call = asyncio.ensure_future(asyncio.to_thread(watched, *args, **kwargs))
                result = await call
        except TimeoutError as exc:
            if not acquired:
                # Timed out queueing behind someone else's live call. That lock
                # is held by a call that will release it normally; do not
                # abandon it.
                raise self._busy_error(timeout) from exc
            if call is None or not call.cancelled():
                # The call itself raised TimeoutError (an OSError subclass,
                # and the same type the deadline raises) rather than overrunning
                # the deadline. That is an ordinary failure: release the lock
                # and let it through unchanged instead of blaming the deadline.
                lock.release()
                raise
            if self._lock is lock:
                self._lock = asyncio.Lock()
            # Same handler, same branch as the lock swap: the two other exits
            # above touched nothing, and quarantining on either would refuse the
            # whole document for a failure that never reached it.
            name = self._quarantine_document(func, watched, timeout)
            log.error(
                "ezdxf call %r exceeded EZDXF_CALL_TIMEOUT=%gs; abandoning the worker "
                "thread and the lock it holds, and quarantining the document it may "
                "still be writing to",
                name,
                timeout,
            )
            raise self._timeout_error(name, timeout) from exc
        except BaseException:
            # Only ours to release if we ever took it — cancellation can land
            # while still queueing, and releasing someone else's lock there
            # would hand the document to two callers at once.
            if acquired:
                lock.release()
            raise
        lock.release()
        return result

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
                _normalise_dimstyle_rounding(self._doc)
            else:
                self._doc = ezdxf.new(dxfversion="R2010")
                _apply_iso_dimstyle(self._doc)
            # R32: the rebind above is the release — the runaway keeps the
            # Drawing it captured, so its remaining writes land on an orphan.
            # Clear before _reset_history_baseline, which snapshots the new doc.
            released = self._clear_quarantine("drawing_new")
            self._doc_path = None
            self._dirty = False
            self._current_layer = "0"
            self._reset_document_state()
            self._current_space = "Model"
            self._reset_history_baseline()
            result = {"ok": True, "name": "untitled.dxf"}
            if released is not None:
                result["quarantine_cleared"] = released.to_dict()
            return result

        return await self._async(_sync, quarantine_exit=True)

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
            _normalise_dimstyle_rounding(self._doc)
            # R32: only reached if the read succeeded. Reopening the very path a
            # runaway doc.saveas is writing either fails loudly here (acceptable)
            # or hands back a truncated document (not) — _clear_quarantine
            # compares the two paths and says so.
            released = self._clear_quarantine("drawing_open", path)
            self._doc_path = path
            self._dirty = False
            try:
                self._current_layer = self._doc.header.get("$CLAYER", "0")
            except Exception as exc:
                log.debug("reading $CLAYER from header: %s", exc)
                self._current_layer = "0"
            self._reset_document_state()
            self._current_space = "Model"
            self._reset_history_baseline()
            result = {"ok": True, "name": Path(path).name, "path": path}
            if released is not None:
                result["quarantine_cleared"] = released.to_dict()
            return result

        return await self._async(_sync, quarantine_exit=True)

    async def drawing_save(self, path: str | None = None) -> dict:
        save_path = path or self._doc_path
        # `doc.saveas` derives the format from the extension and ezdxf can only
        # write DXF, so a .dwg destination here would silently mislabel the file.
        if save_path and _is_dwg_path(save_path):
            raise _refuse_dwg("drawing_save", save_path)

        def _sync():
            doc = self._require_doc()
            if not save_path:
                raise RuntimeError("No path specified and no current file path.")
            doc.saveas(save_path)
            self._doc_path = save_path
            self._dirty = False
            return {"ok": True, "path": save_path}

        return await self._async(_sync)

    async def drawing_save_as(self, path: str, fmt: str = "dxf") -> dict:
        # The extension is authoritative (N2): a .dwg name is a DWG request even
        # when the caller passes fmt="dxf", because the bytes must match the name.
        if fmt.lower() == "dwg" or _is_dwg_path(path):
            raise _refuse_dwg("drawing_save_as", path)

        def _sync():
            doc = self._require_doc()
            doc.saveas(path)
            self._doc_path = path
            self._dirty = False
            return {"ok": True, "path": path, "format": "dxf"}

        return await self._async(_sync)

    async def drawing_export_dxf(self, path: str) -> dict:
        return await self.drawing_save_as(path, "dxf")

    async def drawing_export_pdf(self, path: str, layout: str | None = None) -> dict:
        """Export to PDF via ezdxf's matplotlib backend.

        With ``layout`` set to a paper-space layout name, the layout's own
        entities *and* the model content each viewport looks at are rendered,
        at the viewport's scale and position and clipped to its window.

        This docstring used to say model projection was COM-only. It was
        measured and it is not: a sheet holding nothing but a viewport renders
        the geometry that viewport shows, and geometry outside the window is
        correctly left out. What is genuinely missing is the viewport *border*,
        which the headless renderer does not draw.
        """

        def _sync():
            doc = self._require_doc()
            resolved = self._find_layout(layout) if layout else None
            if layout and layout.strip().lower() != "model":
                if resolved is None:
                    return {"ok": False, "error": f"Layout not found: {layout}"}
                target = doc.layouts.get(resolved)
            else:
                target = self._msp()
            try:
                from ezdxf.addons.drawing import Frontend, RenderContext
                from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

                fig = _new_agg_figure()
                ax = fig.add_axes([0, 0, 1, 1])
                ctx = RenderContext(doc)
                out = MatplotlibBackend(ax)
                Frontend(ctx, out).draw_layout(target, finalize=True)
                fig.savefig(path, dpi=150)
                result = {"ok": True, "path": path}
                if resolved is not None and resolved != "Model":
                    result["layout"] = resolved
                    result["note"] = (
                        "Paper-space entities are rendered, and model content is projected "
                        "through each viewport at its scale and clipped to its window. "
                        "Viewport borders themselves are not drawn."
                    )
                return result
            except ImportError:
                raise RuntimeError(
                    "PDF export requires matplotlib: pip install matplotlib"
                ) from None

        return await self._async(_sync)

    # ── layouts / paper space ────────────────────────────────────────────────

    @staticmethod
    def _layout_name(raw: str) -> str:
        """Names arrive from tool input, so blanks have to die at the door.

        ``doc.layouts.delete("")`` deletes the first paper-space layout and
        ``doc.layouts.get("")`` returns the active one — neither refuses, so an
        empty string is a live footgun rather than a no-op.
        """
        return str(raw or "").strip()

    def _find_layout(self, raw: str) -> str | None:
        """Resolve a layout name the way the engine does, or ``None``.

        ezdxf matches layout names case-insensitively in ``get``, ``new``,
        ``rename``, ``delete`` and ``set_active_layout``, but ``names()``
        returns the *stored* spelling — so every guard written as
        ``name in doc.layouts.names()`` disagrees with the code it guards, in
        both directions: a duplicate walks past the check and dies on a raw
        ``DXFValueError`` from inside ezdxf, and an existing layout typed in
        another case is reported as missing. Returns the stored spelling so
        callers compare and store one canonical form.
        """
        name = self._layout_name(raw)
        if not name:
            return None
        lowered = name.lower()
        return next(
            (n for n in self._require_doc().layouts.names() if n.lower() == lowered),
            None,
        )

    def _free_layout_name(self, seed: str) -> str:
        """A layout name nothing currently holds, derived from ``seed``."""
        for suffix in range(1, 1000):
            candidate = f"{seed}~{suffix}"
            if self._find_layout(candidate) is None:
                return candidate
        raise RuntimeError("cannot find a free temporary layout name")

    def _resync_space(self) -> None:
        """Re-assert ``_current_space`` over ``$TILEMODE`` and the active layout.

        Called after a lifecycle change so the three ways a caller can ask
        "where am I?" cannot drift apart.
        """
        doc = self._require_doc()
        if self._current_space == "Model":
            doc.header["$TILEMODE"] = 1
        else:
            doc.layouts.set_active_layout(self._current_space)
            doc.header["$TILEMODE"] = 0

    async def layout_list(self) -> dict:
        def _sync():
            doc = self._require_doc()
            names = list(doc.layouts.names_in_taborder())
            # DXF stores the current tab as $TILEMODE (1 = Model) plus the
            # active paper-space layout binding (*Paper_Space block record).
            if int(doc.header.get("$TILEMODE", 1)):
                current = "Model"
            else:
                current = doc.active_layout().name
            return {"ok": True, "layouts": names, "current": current}

        return await self._async(_sync)

    async def layout_create(self, name: str) -> dict:
        def _sync():
            doc = self._require_doc()
            wanted = self._layout_name(name)
            if not wanted:
                return {"ok": False, "error": "Layout name must not be blank"}
            existing = self._find_layout(wanted)
            if existing is not None:
                return {"ok": False, "error": f"Layout already exists: {existing}"}
            doc.layouts.new(wanted)
            return {"ok": True, "layout": wanted}

        return await self._async(_sync)

    async def layout_set_current(self, name: str) -> dict:
        def _sync():
            doc = self._require_doc()
            resolved = self._find_layout(name)
            if resolved is None:
                return {"ok": False, "error": f"Layout not found: {name}"}
            if resolved == "Model":
                doc.header["$TILEMODE"] = 1
            else:
                # Rebind *Paper_Space so AutoCAD treats it as the active
                # paper-space layout; TILEMODE=0 selects the paper tab.
                doc.layouts.set_active_layout(resolved)
                doc.header["$TILEMODE"] = 0
            # The whole point: subsequent entity_create_* calls write here.
            # Stored in the canonical spelling so _msp and _resync_space
            # compare against one form.
            self._current_space = resolved
            return {"ok": True, "current": resolved}

        return await self._async(_sync)

    async def layout_delete(self, name: str) -> dict:
        def _sync():
            doc = self._require_doc()
            if not self._layout_name(name):
                return {"ok": False, "error": "Layout name must not be blank"}
            target = self._find_layout(name)
            if target is None:
                return {"ok": False, "error": f"Layout not found: {name}"}
            if target == "Model":
                return {"ok": False, "error": "Model space cannot be deleted"}
            paper = [n for n in doc.layouts.names() if n != "Model"]
            if len(paper) <= 1:
                return {
                    "ok": False,
                    "error": (
                        f"Cannot delete {target}: a drawing must keep at least one "
                        "paper-space layout"
                    ),
                }

            destroyed = len(list(doc.layouts.get(target)))
            doc.layouts.delete(target)
            # Destroyed entities stay in entitydb, where a plain `is None` check
            # walks straight past them and the next attribute access explodes.
            doc.entitydb.purge()
            if self._current_space == target:
                self._current_space = "Model"
            self._resync_space()
            self._mark_dirty()
            return {
                "ok": True,
                "deleted": target,
                "entities_destroyed": destroyed,
                "current": self._current_space,
            }

        return await self._async(_sync)

    async def layout_rename(self, old_name: str, new_name: str) -> dict:
        def _sync():
            from ezdxf.lldxf.validator import is_valid_table_name

            doc = self._require_doc()
            new = self._layout_name(new_name)
            if not self._layout_name(old_name) or not new:
                return {"ok": False, "error": "Layout names must not be blank"}
            old = self._find_layout(old_name)
            if old is None:
                return {"ok": False, "error": f"Layout not found: {old_name}"}
            if "Model" in (old, new) or new.lower() == "model":
                return {"ok": False, "error": "Model space cannot be renamed or shadowed"}
            # Re-spelling a layout's own name is a rename onto itself, not a
            # collision — only a *different* layout blocks the new name.
            clash = self._find_layout(new)
            if clash is not None and clash != old:
                return {"ok": False, "error": f"Layout already exists: {clash}"}
            # `rename()` skips the name check `new()` performs, so `Bad/Name`
            # lands in the table and the file still audits clean.
            if not is_valid_table_name(new):
                return {"ok": False, "error": f"Invalid layout name: {new!r}"}

            if clash == old and new != old:
                # Pure re-spelling. ezdxf's own `rename` tests `new_name in
                # self` case-insensitively, which matches the layout against
                # itself, so a direct call refuses something AutoCAD allows.
                # Two renames through a name nothing else holds get there.
                staging = self._free_layout_name(new)
                doc.layouts.rename(old, staging)
                doc.layouts.rename(staging, new)
            else:
                doc.layouts.rename(old, new)
            if self._current_space == old:
                self._current_space = new
            self._resync_space()
            self._mark_dirty()
            return {"ok": True, "old": old, "new": new, "current": self._current_space}

        return await self._async(_sync)

    #: Attributes that identify a layout rather than describe it. Copying these
    #: onto a clone would point it back at the source's block record.
    _LAYOUT_IDENTITY_ATTRIBS = frozenset(
        {"handle", "owner", "name", "taborder", "block_record_handle", "viewport_handle"}
    )

    @staticmethod
    def _remap_boundaries(layout, handle_map: dict[str, str]) -> tuple[int, int]:
        """Re-point cloned hatch boundaries at the cloned entities.

        A copied associative hatch keeps the *source* layout's handles in
        ``source_boundary_objects``. Nothing complains — both the live and the
        reloaded document audit clean — right up until the source layout is
        deleted and the reference dangles.
        """
        remapped = dropped = 0
        for entity in layout:
            if entity.dxftype() != "HATCH":
                continue
            for path in entity.paths:
                refs = list(getattr(path, "source_boundary_objects", []) or [])
                if not refs:
                    continue
                kept = []
                for handle in refs:
                    mapped = handle_map.get(handle)
                    if mapped is None:
                        dropped += 1
                    else:
                        kept.append(mapped)
                        remapped += 1
                path.source_boundary_objects = kept
        return remapped, dropped

    async def layout_copy(self, source: str, new_name: str) -> dict:
        def _sync():
            from ezdxf.entities.copy import CopyNotSupported
            from ezdxf.lldxf.validator import is_valid_table_name

            doc = self._require_doc()
            dst_name = self._layout_name(new_name)
            if not self._layout_name(source) or not dst_name:
                return {"ok": False, "error": "Layout names must not be blank"}
            src_name = self._find_layout(source)
            if src_name is None:
                return {"ok": False, "error": f"Layout not found: {source}"}
            if src_name == "Model":
                return {
                    "ok": False,
                    "error": "Model space cannot be copied to a sheet; use viewport_create",
                }
            if dst_name.lower() == "model":
                return {"ok": False, "error": "Model space cannot be overwritten"}
            clash = self._find_layout(dst_name)
            if clash is not None:
                return {"ok": False, "error": f"Layout already exists: {clash}"}
            if not is_valid_table_name(dst_name):
                return {"ok": False, "error": f"Invalid layout name: {dst_name!r}"}

            src = doc.layouts.get(src_name)
            # A deleted layout leaves a gap in the tab order and `new()` reuses
            # it, so two tabs end up sharing an order and surviving reload.
            orders = [
                int(doc.layouts.get(n).dxf.get("taborder", 0) or 0)
                for n in doc.layouts.names()
                if n != "Model"
            ]
            dst = doc.layouts.new(dst_name, dxfattribs={"taborder": max(orders, default=0) + 1})
            # Set after `new()`, not through it: `new()` resets the paper limits
            # *after* applying dxfattribs, so a page setup passed in looks
            # copied and is not — the sheet then simply plots wrong.
            for key, value in src.dxf.all_existing_dxf_attribs().items():
                if key not in self._LAYOUT_IDENTITY_ATTRIBS:
                    dst.dxf.set(key, value)

            handle_map: dict[str, str] = {}
            skipped: list[str] = []
            for entity in list(src):
                try:
                    clone = doc.entitydb.duplicate_entity(entity)
                except (CopyNotSupported, NotImplementedError):
                    skipped.append(entity.dxftype())
                    continue
                dst.add_entity(clone)
                handle_map[entity.dxf.handle] = clone.dxf.handle

            remapped, dropped = self._remap_boundaries(dst, handle_map)

            # The copy brought the source's main viewport with it; the new tab
            # has to point at *that* one, not at the source's handle.
            main = dst.main_viewport()
            if main is not None:
                dst.set_current_viewport_handle(main.dxf.handle)
            else:
                dst.dxf.discard("viewport_handle")

            self._mark_dirty()
            return {
                "ok": True,
                "source": src_name,
                "layout": dst_name,
                "entities_copied": len(handle_map),
                "skipped": skipped,
                "associativity_remapped": remapped,
                "associativity_dropped": dropped,
            }

        return await self._async(_sync)

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
            doc = self._require_doc()
            if scale <= 0:
                return {"ok": False, "error": "scale must be > 0 (paper:model, e.g. 0.5 for 1:2)"}
            if layout == "Model" or layout not in doc.layouts.names():
                return {"ok": False, "error": f"Paper-space layout not found: {layout}"}
            target = doc.layouts.get(layout)
            view_height = height / scale
            viewport = target.add_viewport(
                center=(center_x, center_y),
                size=(width, height),
                view_center_point=(view_center_x, view_center_y),
                view_height=view_height,
            )
            return {
                "ok": True,
                "handle": viewport.dxf.handle,
                "layout": layout,
                "scale": scale,
                "view_height": view_height,
            }

        return await self._async(_sync)

    # ── viewports ────────────────────────────────────────────────────────────

    #: DXF viewport flag bit for "display locked" (VSF_LOCK_ZOOM).
    _VP_LOCK_ZOOM = 0x4000

    #: R12 has no slot for view_height or flags, so both are dropped on export.
    _R12_NOTE = "R12 stores neither viewport scale nor display lock; both are reported as null"

    def _paper_layouts(self) -> list[str]:
        return [n for n in self._require_doc().layouts.names_in_taborder() if n != "Model"]

    def _resolve_viewport(self, handle: str):
        """``(viewport, None)`` or ``(None, refusal)``.

        Liveness is checked before the type, because a deleted entity stays in
        ``entitydb`` and still answers ``dxftype() == "VIEWPORT"`` — so a
        type-check-first resolver hands back a corpse.
        """
        doc = self._require_doc()
        key = str(handle or "").strip().upper()
        entity = doc.entitydb.get(key)
        if entity is None or not entity.is_alive:
            return None, {"ok": False, "error": f"Viewport handle not found: {handle}"}
        if entity.dxftype() != "VIEWPORT":
            return None, {
                "ok": False,
                "error": f"Handle {key} is a {entity.dxftype()}, not a VIEWPORT",
            }
        return entity, None

    @staticmethod
    def _is_main_viewport(viewport) -> bool:
        """Ask the layout, rather than testing ``id == 1``.

        ezdxf resolves the main viewport by ``status == 1`` first and falls back
        to ``id == 1``, and the two can disagree.
        """
        try:
            layout = viewport.get_layout()
        except Exception:
            return False
        main = layout.main_viewport() if layout is not None else None
        return main is not None and main.dxf.handle == viewport.dxf.handle

    def _is_r12(self) -> bool:
        return self._require_doc().dxfversion == "AC1009"

    def _viewport_row(self, viewport, layout_name: str, r12: bool) -> dict:
        center = viewport.dxf.center
        view_center = viewport.dxf.get("view_center_point", None)
        view_height = viewport.dxf.get("view_height", None)
        flags = viewport.dxf.get("flags", None)
        return {
            "handle": viewport.dxf.handle,
            "layout": layout_name,
            "center": [float(center.x), float(center.y)],
            "width": float(viewport.dxf.width),
            "height": float(viewport.dxf.height),
            "view_center": (
                [float(view_center.x), float(view_center.y)] if view_center is not None else None
            ),
            "view_height": float(view_height) if view_height is not None else None,
            # get_scale() divides by a view_height the file does not have and
            # returns a number anyway; refuse to pass that on as a scale.
            "scale": (None if (r12 or not view_height) else float(viewport.get_scale())),
            # On R2000+ an absent flags field means "no flags set", i.e.
            # unlocked. On R12 the field cannot exist at all, so the honest
            # answer is "this file cannot say".
            "locked": (None if r12 else bool(int(flags or 0) & self._VP_LOCK_ZOOM)),
            "status": viewport.dxf.get("status", None),
            "id": viewport.dxf.get("id", None),
            "is_main": self._is_main_viewport(viewport),
        }

    async def viewport_list(self, layout: str | None = None) -> dict:
        def _sync():
            doc = self._require_doc()
            if layout is None:
                names = self._paper_layouts()
            else:
                if not self._layout_name(layout):
                    return {"ok": False, "error": "Layout name must not be blank"}
                resolved = self._find_layout(layout)
                if resolved is None:
                    return {"ok": False, "error": f"Layout not found: {layout}"}
                if resolved == "Model":
                    return {"ok": False, "error": "Model space has no paper-space viewports"}
                names = [resolved]

            r12 = self._is_r12()
            rows = [
                self._viewport_row(vp, name, r12)
                for name in names
                for vp in doc.layouts.get(name).viewports()
            ]
            result = {"ok": True, "viewports": rows, "count": len(rows)}
            if r12:
                result["note"] = self._R12_NOTE
            return result

        return await self._async(_sync)

    async def viewport_set_scale(self, handle: str, scale: float) -> dict:
        def _sync():
            if float(scale) <= 0:
                return {"ok": False, "error": "scale must be > 0 (paper:model, e.g. 0.5 for 1:2)"}
            viewport, refusal = self._resolve_viewport(handle)
            if refusal:
                return refusal
            if self._is_r12():
                return {
                    "ok": False,
                    "error": (
                        "R12 drops view_height on export, so the scale would be lost on save. "
                        "Save as R2000 or newer first."
                    ),
                }
            if self._is_main_viewport(viewport):
                return {
                    "ok": False,
                    "error": (
                        "This is the layout's main viewport: its view height is the tab's own "
                        "pan/zoom state, not a drafting scale. Create a viewport with "
                        "viewport_create and scale that."
                    ),
                }
            height = float(viewport.dxf.height)
            if height <= 0:
                return {"ok": False, "error": "Viewport has zero height; its scale is undefined"}

            viewport.dxf.view_height = height / float(scale)
            self._mark_dirty()
            return {
                "ok": True,
                "handle": viewport.dxf.handle,
                "scale": float(viewport.get_scale()),
                "view_height": float(viewport.dxf.view_height),
                "note": "geometric scale only; annotative text and dimensions do not resize",
            }

        return await self._async(_sync)

    async def viewport_lock(self, handle: str, locked: bool = True) -> dict:
        def _sync():
            viewport, refusal = self._resolve_viewport(handle)
            if refusal:
                return refusal
            if self._is_r12():
                return {
                    "ok": False,
                    "error": (
                        "R12 has no viewport flags field, so the lock would be lost on save. "
                        "Save as R2000 or newer first."
                    ),
                }
            # Read-modify-write: `flags` also carries the UCS-icon and grid
            # bits, and a bare assignment drops them and still saves cleanly.
            flags = int(viewport.dxf.get("flags", 0) or 0)
            viewport.dxf.flags = (
                flags | self._VP_LOCK_ZOOM if locked else flags & ~self._VP_LOCK_ZOOM
            )
            self._mark_dirty()
            return {
                "ok": True,
                "handle": viewport.dxf.handle,
                "locked": bool(viewport.dxf.flags & self._VP_LOCK_ZOOM),
            }

        return await self._async(_sync)

    async def viewport_delete(self, handle: str, force: bool = False) -> dict:
        def _sync():
            viewport, refusal = self._resolve_viewport(handle)
            if refusal:
                return refusal
            was_main = self._is_main_viewport(viewport)
            if was_main and not force:
                return {
                    "ok": False,
                    "error": (
                        "This is the layout's main viewport; deleting it leaves the tab without "
                        "its own view state. Pass force=true if that is what you want."
                    ),
                }

            layout = viewport.get_layout()
            layout_name = layout.name if layout is not None else None
            layout.delete_entity(viewport)

            repaired = False
            if was_main and layout is not None:
                survivor = next(iter(layout.viewports()), None)
                if survivor is not None:
                    layout.set_current_viewport_handle(survivor.dxf.handle)
                else:
                    # A pointer at a destroyed entity audits clean here and
                    # breaks in AutoCAD; drop it instead.
                    layout.dxf.discard("viewport_handle")
                repaired = True

            self._mark_dirty()
            return {
                "ok": True,
                "handle": str(handle).strip().upper(),
                "layout": layout_name,
                "was_main": was_main,
                "viewport_handle_repaired": repaired,
            }

        return await self._async(_sync)

    # ── change space (CHSPACE) ───────────────────────────────────────────────

    #: Types whose ``transform()`` is accepted and does nothing: the matrix is
    #: parked in a pending transformation only an ACIS kernel could apply.
    _CHSPACE_ACIS_TYPES = frozenset({"3DSOLID", "BODY", "REGION", "SURFACE", "EXTRUDEDSURFACE"})

    #: Types whose ``transform()`` raises. ``hasattr(e, "transform")`` is True
    #: for all of them, so a capability probe does not filter them out.
    _CHSPACE_UNTRANSFORMABLE = frozenset(
        {"ACAD_TABLE", "ACAD_PROXY_ENTITY", "OLE2FRAME", "OLEFRAME"}
    )

    async def entity_change_space(
        self,
        handles: list[str],
        viewport_handle: str,
        direction: str = "to_paper",
        freeze_dimensions: bool = False,
    ) -> dict:
        def _sync():
            from ezdxf.math import Vec3

            doc = self._require_doc()
            if direction not in ("to_paper", "to_model"):
                return {
                    "ok": False,
                    "error": f"direction must be to_paper or to_model: {direction!r}",
                }
            if not handles:
                return {"ok": False, "error": "No entity handles given"}

            viewport, refusal = self._resolve_viewport(viewport_handle)
            if refusal:
                return refusal
            # ezdxf hands back a top-view matrix for a non-top view without
            # complaining, and rotates about the paper origin for a twisted
            # one. Both produce plausible-looking, wrong geometry.
            if not viewport.is_top_view:
                return {
                    "ok": False,
                    "error": (
                        "Viewport is not a plan (top) view; CHSPACE through it would place "
                        "geometry using a projection this engine cannot reproduce."
                    ),
                }
            if float(viewport.dxf.get("view_twist_angle", 0.0) or 0.0):
                return {
                    "ok": False,
                    "error": (
                        "Viewport has a view twist angle; this engine rotates about the paper "
                        "origin rather than the view centre, so the result would be wrong."
                    ),
                }

            sheet = viewport.get_layout()
            if sheet is None:
                return {"ok": False, "error": "Viewport is not on a paper-space layout"}
            model = doc.modelspace()
            to_paper = direction == "to_paper"
            source, target = (model, sheet) if to_paper else (sheet, model)

            to_paper_matrix = viewport.get_transformation_matrix()
            if to_paper:
                matrix = to_paper_matrix
            else:
                # Matrix44.inverse() mutates in place and returns None.
                matrix = to_paper_matrix.copy()
                matrix.inverse()

            scale = float(viewport.get_scale())
            vp_center = viewport.dxf.center
            half_w, half_h = float(viewport.dxf.width) / 2, float(viewport.dxf.height) / 2
            paper_limits = sheet.get_paper_limits()

            moved: list[dict] = []
            refused: list[dict] = []
            for raw in handles:
                key = str(raw or "").strip().upper()
                entity = doc.entitydb.get(key)
                if entity is None or not entity.is_alive:
                    refused.append({"handle": raw, "reason": f"Entity handle not found: {raw}"})
                    continue

                dxftype = entity.dxftype()
                if dxftype == "VIEWPORT":
                    refused.append(
                        {"handle": key, "reason": "VIEWPORT entities do not change space"}
                    )
                    continue
                if dxftype in self._CHSPACE_ACIS_TYPES:
                    refused.append(
                        {
                            "handle": key,
                            "reason": (
                                f"{dxftype}: this engine accepts the transform and moves no "
                                "geometry (no ACIS kernel), so the move would be a lie"
                            ),
                        }
                    )
                    continue
                if dxftype in self._CHSPACE_UNTRANSFORMABLE:
                    refused.append(
                        {"handle": key, "reason": f"{dxftype} cannot be transformed by this engine"}
                    )
                    continue

                # Membership is checked before anything is written, because
                # `move_to_layout` raises from deep inside ezdxf *after* the
                # transform has been applied — leaving the entity rescaled by a
                # viewport it never moved through.
                owner = entity.dxf.owner
                if owner == target.block_record.dxf.handle:
                    refused.append(
                        {"handle": key, "reason": "Entity is already in the target space"}
                    )
                    continue
                if owner != source.block_record.dxf.handle:
                    refused.append(
                        {
                            "handle": key,
                            "reason": (
                                f"Entity is in {self._space_name(owner)}, not in the space this "
                                f"move starts from ({'Model' if to_paper else sheet.name})"
                            ),
                        }
                    )
                    continue

                frozen_text = None
                if dxftype == "DIMENSION":
                    if not freeze_dimensions:
                        refused.append(
                            {
                                "handle": key,
                                "reason": (
                                    "DIMENSION: scaling changes the measurement while the baked "
                                    "dimension text keeps the old value. Pass "
                                    "freeze_dimensions=true to bake the current measurement in "
                                    "first."
                                ),
                            }
                        )
                        continue
                    frozen_text = self._format_measurement(entity.get_measurement())
                    entity.dxf.text = frozen_text

                entity.transform(matrix)
                source.move_to_layout(entity, target)

                # Where the entity now sits, expressed in paper coordinates.
                # Clipping is reported, not enforced: AutoCAD lets you move
                # geometry the viewport does not show, and so does this — but
                # the caller has to be told the drawing left the sheet.
                anchor = _label_anchor(entity)
                inside = on_sheet = None
                if anchor is not None:
                    point = Vec3(anchor)
                    if not to_paper:
                        point = to_paper_matrix.transform(point)
                    inside = (
                        abs(point.x - vp_center.x) <= half_w
                        and abs(point.y - vp_center.y) <= half_h
                    )
                    on_sheet = (
                        paper_limits[0].x <= point.x <= paper_limits[1].x
                        and paper_limits[0].y <= point.y <= paper_limits[1].y
                    )
                row = {
                    "handle": key,
                    "type": dxftype,
                    "from": "Model" if to_paper else sheet.name,
                    "to": sheet.name if to_paper else "Model",
                    "inside_viewport": inside,
                    "on_sheet": on_sheet,
                }
                if frozen_text is not None:
                    row["frozen_text"] = frozen_text
                moved.append(row)

            if moved:
                self._mark_dirty()
            return {
                "ok": True,
                "moved": moved,
                "refused": refused,
                "scale": scale,
                "viewport": viewport.dxf.handle,
                "layout": sheet.name,
            }

        return await self._async(_sync)

    def _space_name(self, block_record_handle: str) -> str:
        """Name the layout tab an owning block record belongs to."""
        doc = self._require_doc()
        if block_record_handle == doc.modelspace().block_record.dxf.handle:
            return "Model"
        for name in doc.layouts.names():
            if name == "Model":
                continue
            if doc.layouts.get(name).block_record.dxf.handle == block_record_handle:
                return name
        return "a block definition"

    @staticmethod
    def _format_measurement(value: float) -> str:
        """Render a dimension measurement the way a dimension text reads."""
        text = f"{float(value):.4f}".rstrip("0").rstrip(".")
        return text or "0"

    # ── 3D solids (unsupported headlessly — honest capability boundary) ─────

    @staticmethod
    def _solid_unsupported(operation: str) -> UnsupportedCapabilityError:
        """Raise, don't return: a refusal returned as a value is invisible.

        The message is unchanged, so the payload a client sees is identical; what
        changes is that ``is_error`` stops being quietly ``False`` on a call that
        did nothing, and that the one refusal middleware can see this at all.
        """
        return UnsupportedCapabilityError(
            "solid_3d",
            f"{operation}: 3D ACIS solids cannot be generated headlessly; "
            "use the live COM backend with ENABLE_3D=true",
        )

    async def solid_box(
        self, cx: float, cy: float, cz: float, length: float, width: float, height: float
    ) -> dict:
        raise self._solid_unsupported("solid_box")

    async def solid_cylinder(
        self, cx: float, cy: float, cz: float, radius: float, height: float
    ) -> dict:
        raise self._solid_unsupported("solid_cylinder")

    async def solid_extrude(
        self, profile_handle: str, height: float, taper_angle: float = 0.0
    ) -> dict:
        raise self._solid_unsupported("solid_extrude")

    async def solid_revolve(
        self,
        profile_handle: str,
        axis_x1: float,
        axis_y1: float,
        axis_x2: float,
        axis_y2: float,
        angle: float = 360.0,
    ) -> dict:
        raise self._solid_unsupported("solid_revolve")

    async def solid_boolean(self, target_handle: str, tool_handle: str, operation: str) -> dict:
        raise self._solid_unsupported("solid_boolean")

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
        """Audit and repair, then report both halves of what happened.

        ``Drawing.audit()`` is not a read: it fixes every fixable problem in
        place, so each entry in ``auditor.fixes`` is a mutation that has already
        been applied. Reporting only ``errors`` therefore said "0 problems"
        about a document the call had just rewritten. Marking dirty is the
        load-bearing half — ``drawing_close`` saves only ``if save and
        self._dirty``, so without it the repairs are discarded on close and the
        report becomes a promise the file never keeps.
        """

        def _sync():
            doc = self._require_doc()
            auditor = doc.audit()
            fixes = [_audit_entry(e) for e in auditor.fixes]
            errors = [_audit_entry(e) for e in auditor.errors]
            if fixes:
                self._mark_dirty()
            return {
                "ok": True,
                "repaired": bool(fixes),
                "fixes": fixes,
                "fix_count": len(fixes),
                "errors": errors,
                "error_count": len(errors),
            }

        return await self._async(_sync)

    async def drawing_close(self, save: bool = True) -> dict:
        def _sync():
            # R32: closing is a way out of a quarantine, but saving on the way is
            # not. That save is the exact defect measured — a valid DXF holding
            # 15200 of the 60000 entities the runaway went on to write, a
            # considered document of a state that never existed. Say what was
            # dropped instead of writing it.
            quarantined = self._quarantine is not None
            if save and self._dirty and self._doc_path and not quarantined:
                self._doc.saveas(self._doc_path)
            self._cleanup_undo_stack()
            self._doc = None
            released = self._clear_quarantine("drawing_close")
            self._doc_path = None
            self._dirty = False
            self._reset_document_state()
            result = {"ok": True}
            if released is not None:
                result["quarantine_cleared"] = released.to_dict()
                result["saved"] = False
                result["warning"] = (
                    "the document was quarantined after a call was abandoned mid-write, "
                    "so unsaved changes were discarded rather than serialised out of a "
                    "drawing another thread may still have been mutating"
                )
            return result

        return await self._async(_sync, quarantine_exit=True)

    async def drawing_undo(self) -> dict:
        self._require_undo_history("drawing_undo")

        # NEW-undo-1: read+mutate _undo_stack under self._lock (via _async) so
        # the empty-check and the pop are serialized with all other sync work.
        def _sync():
            # Two entries minimum: the top is the current state, and undo means
            # restoring the one below it.
            if len(self._undo_stack) < 2:
                return {
                    "ok": False,
                    "error": "Nothing to undo — this is the oldest state in the history.",
                    "undo_depth": max(0, len(self._undo_stack) - 1),
                }
            self._redo_stack.append(self._undo_stack.pop())
            self._restore_snapshot(self._undo_stack[-1])
            return {
                "ok": True,
                "message": "Stepped back one operation.",
                "undo_depth": len(self._undo_stack) - 1,
                "redo_depth": len(self._redo_stack),
            }

        return await self._async(_sync)

    async def drawing_redo(self) -> dict:
        self._require_undo_history("drawing_redo")

        def _sync():
            if not self._redo_stack:
                return {
                    "ok": False,
                    "error": "Nothing to redo — nothing has been undone, or a "
                    "later edit discarded the branch.",
                    "redo_depth": 0,
                }
            snapshot = self._redo_stack.pop()
            self._undo_stack.append(snapshot)
            self._restore_snapshot(snapshot)
            return {
                "ok": True,
                "message": "Reapplied the operation you undid.",
                "undo_depth": len(self._undo_stack) - 1,
                "redo_depth": len(self._redo_stack),
            }

        return await self._async(_sync)

    # ── internal: apply common attrs ──────────────────────────────────────────

    def _apply_attrs(
        self, entity, layer: str | None, color: int | None, linetype: str | None = None
    ):
        if layer is not None:
            entity.dxf.layer = layer
            # Ensure layer exists
            doc = self._require_doc()
            if layer not in doc.layers:
                doc.layers.add(layer)
        if color is not None:
            entity.dxf.color = int(color)
        if linetype is not None:
            # R24: load HIDDEN/CENTER/etc. on demand before assignment so the
            # linetype actually renders instead of silently falling back to
            # Continuous. Same loader used in entity_set_properties/layer_*.
            _ensure_linetype_loaded(self._require_doc(), linetype)
            entity.dxf.linetype = linetype

    def _mark_dirty(self):
        self._dirty = True
        self._push_undo_snapshot()

    # ── undo / redo ───────────────────────────────────────────────────────────
    #
    # ezdxf keeps no journal, so a history step is a whole DXF snapshot. The
    # stack holds *post*-mutation states — S0 from drawing_new, then one per
    # mutation — because _mark_dirty is the single hook every mutating method
    # already calls, and it runs after the change. Undo therefore restores the
    # entry below the top rather than the top itself.

    def _snapshot_doc(self) -> Path | None:
        """Write the current document to a temp DXF, or None if there is none."""
        if self._doc is None:
            return None
        fd, tmp_path = tempfile.mkstemp(suffix=".dxf", prefix="acad_mcp_undo_")
        os.close(fd)
        try:
            self._doc.saveas(tmp_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return Path(tmp_path)

    @staticmethod
    def _discard(paths: list[Path]) -> None:
        while paths:
            try:
                paths.pop().unlink()
            except OSError:
                pass

    def _push_undo_snapshot(self) -> None:
        depth = config.settings.ezdxf_undo_depth
        if depth <= 0 or self._doc is None:
            return
        snapshot = self._snapshot_doc()
        if snapshot is None:
            return
        self._undo_stack.append(snapshot)
        # A mutation after an undo abandons the branch the user stepped off.
        # Keeping it would let redo restore a state that never existed: geometry
        # they had removed reappearing beside geometry they drew afterwards.
        self._discard(self._redo_stack)
        # depth+1 because the bottom entry is the state *before* the first
        # undoable step, and restoring it is what the first undo does.
        while len(self._undo_stack) > depth + 1:
            try:
                self._undo_stack.pop(0).unlink()
            except OSError:
                pass

    def _restore_snapshot(self, path: Path) -> None:
        self._doc = ezdxf.readfile(str(path))
        _normalise_dimstyle_rounding(self._doc)
        self._current_layer = str(self._doc.header.get("$CLAYER", "0"))
        self._dirty = True

    def _reset_history_baseline(self) -> None:
        """Start the history at the document as opened or created.

        Without this the stack's oldest entry is the state *after* the first
        edit, so that edit could never be undone — the history would silently be
        one step shorter than the user asked for.
        """
        self._cleanup_undo_stack()
        self._push_undo_snapshot()

    def _require_undo_history(self, operation: str) -> None:
        if config.settings.ezdxf_undo_depth > 0:
            return
        raise UnsupportedCapabilityError(
            "undo_history",
            f"{operation}: the headless backend keeps no history by default. ezdxf "
            "has no journal, so every step is a full DXF snapshot — measured at "
            "3 ms for 10 entities but 130 ms and 658 KB at 5000, paid on every "
            "mutating call. Set EZDXF_UNDO_DEPTH to the number of steps you want "
            "(e.g. 10) to switch it on, or use transaction_begin/rollback for a "
            "single explicit checkpoint. The live COM backend uses AutoCAD's own "
            "undo and needs no setting.",
        )

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
        self,
        cx,
        cy,
        radius,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_circle((float(cx), float(cy)), float(radius))
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

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
            msp = self._msp()
            ent = msp.add_arc(
                (float(cx), float(cy)),
                float(radius),
                float(start_angle),
                float(end_angle),
            )
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

    async def entity_create_polyline(
        self,
        points,
        closed=False,
        layer=None,
        color=None,
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
            msp = self._msp()
            ent = msp.add_mtext(
                text,
                dxfattribs={"char_height": float(height), "width": float(width)},
            )
            ent.dxf.insert = (float(x), float(y), 0.0)
            if rotation:
                ent.dxf.rotation = float(rotation)  # NEW-mtext-1: honor caller rotation
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

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
            msp = self._msp()
            children = []
            x0, y0 = float(x), float(y)
            x_positions = [x0]
            for width in layout.column_widths:
                x_positions.append(x_positions[-1] + width)

            for row_index in range(layout.row_count + 1):
                yy = y0 - row_index * layout.row_height
                line = msp.add_line((x0, yy), (x0 + layout.width, yy))
                self._apply_attrs(line, layer, None)
                children.append(line)
            for xx in x_positions:
                line = msp.add_line((xx, y0), (xx, y0 - layout.height))
                self._apply_attrs(line, layer, None)
                children.append(line)

            padding = min(1.0, layout.row_height * 0.15)
            for row_index, row in enumerate(layout.cells):
                for column_index, value in enumerate(row):
                    if not value:
                        continue
                    text_entity = msp.add_mtext(
                        value,
                        dxfattribs={
                            "char_height": layout.text_height,
                            "width": max(1.0, layout.column_widths[column_index] - 2 * padding),
                        },
                    )
                    text_entity.dxf.insert = (
                        x_positions[column_index] + padding,
                        y0 - row_index * layout.row_height - padding,
                        0.0,
                    )
                    self._apply_attrs(text_entity, layer, None)
                    children.append(text_entity)

            self._mark_dirty()
            child_handles = [str(entity.dxf.handle) for entity in children]
            group_id = f"table:{uuid.uuid4().hex}"
            return EntityInfo(
                handle=child_handles[0],
                type="TABLE",
                layer=layer,
                color=256,
                linetype="ByLayer",
                visible=True,
                properties={
                    "representation": "composite",
                    "logical_group_id": group_id,
                    "child_handles": child_handles,
                    "rows": layout.row_count,
                    "columns": layout.column_count,
                    "bounds": {
                        "min": [x0, y0 - layout.height],
                        "max": [x0 + layout.width, y0],
                    },
                },
            )

        return await self._async(_sync)

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
            msp = self._msp()
            path = msp.add_lwpolyline(normalized)
            self._apply_attrs(path, layer, None)

            (x0, y0), (x1, y1) = normalized[0], normalized[1]
            dx, dy = x1 - x0, y1 - y0
            length = math.hypot(dx, dy)
            if length <= 1e-12:
                raise RuntimeError("leader_create_mleader: first two points must be distinct")
            ux, uy = dx / length, dy / length
            px, py = -uy, ux
            size = float(arrow_size)
            arrow = msp.add_lwpolyline(
                [
                    (x0, y0),
                    (x0 + ux * size + px * size * 0.35, y0 + uy * size + py * size * 0.35),
                    (x0 + ux * size - px * size * 0.35, y0 + uy * size - py * size * 0.35),
                ],
                close=True,
            )
            self._apply_attrs(arrow, layer, None)

            tx, ty = normalized[-1]
            label = msp.add_mtext(
                str(text),
                dxfattribs={"char_height": float(text_height), "width": 100.0},
            )
            label.dxf.insert = (tx + float(landing_gap), ty, 0.0)
            self._apply_attrs(label, layer, None)
            self._mark_dirty()

            children = [path, arrow, label]
            child_handles = [str(entity.dxf.handle) for entity in children]
            return EntityInfo(
                handle=child_handles[0],
                type="MLEADER",
                layer=layer,
                color=256,
                linetype="ByLayer",
                visible=True,
                properties={
                    "representation": "composite",
                    "logical_group_id": f"mleader:{uuid.uuid4().hex}",
                    "child_handles": child_handles,
                    "points": [list(point) for point in normalized],
                    "text": str(text),
                    "bounds": {
                        "min": [
                            min(point[0] for point in normalized),
                            min(point[1] for point in normalized),
                        ],
                        "max": [
                            max(point[0] for point in normalized),
                            max(point[1] for point in normalized),
                        ],
                    },
                },
            )

        return await self._async(_sync)

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
        self,
        fit_points,
        layer=None,
        color=None,
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
        self,
        x,
        y,
        layer=None,
        color=None,
    ) -> EntityInfo:
        def _sync():
            msp = self._msp()
            ent = msp.add_point((float(x), float(y)))
            self._apply_attrs(ent, layer, color)
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

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

    # ── hatch depth (M8 / F4) ────────────────────────────────────────────────

    #: Island style names, in DXF ``hatch_style`` order.
    _HATCH_STYLES = ("normal", "outer", "ignore")

    def _resolve_hatch(self, handle: str):
        entity = self._get_entity(handle)
        if entity.dxftype() != "HATCH":
            return None, {
                "ok": False,
                "error": f"Handle {handle} is a {entity.dxftype()}, not a HATCH",
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
            hatch, refusal = self._resolve_hatch(handle)
            if refusal:
                return refusal
            try:
                rgb1 = tuple(int(c) for c in color1)
                rgb2 = tuple(int(c) for c in color2)
            except (TypeError, ValueError):
                return {"ok": False, "error": "color1 and color2 must be [r, g, b] triples"}
            if len(rgb1) != 3 or len(rgb2) != 3:
                return {"ok": False, "error": "color1 and color2 must be [r, g, b] triples"}

            hatch.set_gradient(
                color1=rgb1,
                color2=rgb2,
                rotation=float(rotation),
                centered=float(centered),
                one_color=1 if one_color else 0,
                tint=float(tint),
                name=str(name),
            )
            self._mark_dirty()
            gradient = hatch.gradient
            return {
                "ok": True,
                "handle": hatch.dxf.handle,
                "gradient": {
                    "color1": list(gradient.color1),
                    "color2": list(gradient.color2),
                    "rotation": float(gradient.rotation),
                    "centered": float(gradient.centered),
                    "one_color": bool(gradient.one_color),
                    "tint": float(gradient.tint),
                    "name": gradient.name,
                },
            }

        return await self._async(_sync)

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
            hatch, refusal = self._resolve_hatch(handle)
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

            # Every write is conditional on the parameter being given: a hatch
            # edit that resets the attributes the caller did not mention is
            # silent data loss. `changed` then reports what actually *moved*,
            # not what was passed — re-setting a value to what it already was
            # is a no-op and saying otherwise would make the field useless for
            # deciding whether anything happened.
            def _snapshot():
                return {
                    "pattern": hatch.dxf.pattern_name,
                    "scale": float(hatch.dxf.pattern_scale),
                    "angle": float(hatch.dxf.pattern_angle),
                    "color": int(hatch.dxf.color),
                    "style": int(hatch.dxf.hatch_style),
                }

            before = _snapshot()
            if pattern:
                hatch.set_pattern_fill(
                    str(pattern),
                    scale=float(scale) if scale is not None else before["scale"],
                    angle=float(angle) if angle is not None else before["angle"],
                    color=int(color) if color is not None else before["color"],
                )
            else:
                if scale is not None:
                    hatch.set_pattern_scale(float(scale))
                if angle is not None:
                    hatch.set_pattern_angle(float(angle))
            if color is not None:
                hatch.dxf.color = int(color)
            if wanted_style:
                hatch.dxf.hatch_style = self._HATCH_STYLES.index(wanted_style)

            after = _snapshot()
            changed = [key for key in before if before[key] != after[key]]
            if changed:
                self._mark_dirty()
            return {"ok": True, "handle": hatch.dxf.handle, "changed": changed}

        return await self._async(_sync)

    async def hatch_add_boundary(self, handle, edges) -> dict:
        def _sync():
            hatch, refusal = self._resolve_hatch(handle)
            if refusal:
                return refusal
            if not edges:
                return {"ok": False, "error": "edges must not be empty"}

            def _need(edge, *keys):
                missing = [k for k in keys if k not in edge]
                return missing[0] if missing else None

            prepared: list[tuple[str, dict]] = []
            for index, edge in enumerate(edges):
                if not isinstance(edge, dict):
                    return {"ok": False, "error": f"edge {index} is not an object"}
                kind = str(edge.get("type", "")).strip().lower()
                if kind == "line":
                    missing = _need(edge, "start", "end")
                elif kind == "arc":
                    missing = _need(edge, "center", "radius", "start_angle", "end_angle")
                elif kind == "ellipse":
                    missing = _need(edge, "center", "major_axis", "ratio")
                else:
                    return {
                        "ok": False,
                        "error": (
                            f"edge {index}: unknown type {edge.get('type')!r}; valid types are "
                            "line, arc, ellipse"
                        ),
                    }
                if missing:
                    return {"ok": False, "error": f"edge {index} ({kind}) is missing {missing!r}"}
                prepared.append((kind, edge))

            # Validated in full before a single edge is written: a half-built
            # boundary path is worse than a refusal.
            path = hatch.paths.add_edge_path()
            for kind, edge in prepared:
                if kind == "line":
                    path.add_line(tuple(edge["start"][:2]), tuple(edge["end"][:2]))
                elif kind == "arc":
                    path.add_arc(
                        center=tuple(edge["center"][:2]),
                        radius=float(edge["radius"]),
                        start_angle=float(edge["start_angle"]),
                        end_angle=float(edge["end_angle"]),
                        ccw=bool(edge.get("ccw", True)),
                    )
                else:
                    path.add_ellipse(
                        center=tuple(edge["center"][:2]),
                        major_axis=tuple(edge["major_axis"][:2]),
                        ratio=float(edge["ratio"]),
                        start_angle=float(edge.get("start_angle", 0.0)),
                        end_angle=float(edge.get("end_angle", 360.0)),
                        ccw=bool(edge.get("ccw", True)),
                    )

            self._mark_dirty()
            return {
                "ok": True,
                "handle": hatch.dxf.handle,
                "path_count": len(hatch.paths),
                "edge_types": [kind for kind, _ in prepared],
            }

        return await self._async(_sync)

    # ── annotation objects (M8 / F15) ────────────────────────────────────────

    @staticmethod
    def _plane_points(points) -> list[tuple[float, float]] | None:
        """Coerce tool input to 2D vertices, or ``None`` if it is not shaped like points."""
        try:
            return [(float(p[0]), float(p[1])) for p in points]
        except (TypeError, ValueError, IndexError):
            return None

    async def entity_create_wipeout(self, points, layer=None) -> dict:
        def _sync():
            vertices = self._plane_points(points)
            if vertices is None:
                return {"ok": False, "error": "points must be a list of [x, y] pairs"}
            if len(vertices) < 3:
                return {
                    "ok": False,
                    "error": (
                        f"A wipeout needs at least 3 points to enclose an area; got {len(vertices)}"
                    ),
                }
            msp = self._msp()
            entity = msp.add_wipeout(vertices)
            if layer:
                entity.dxf.layer = layer
            self._mark_dirty()
            return {
                "ok": True,
                "handle": entity.dxf.handle,
                "points": [[x, y] for x, y in vertices],
                "layer": entity.dxf.layer,
            }

        return await self._async(_sync)

    async def entity_create_revcloud(
        self, points, segment_length, layer=None, closed: bool = True
    ) -> dict:
        def _sync():
            from ezdxf.math import Vec2
            from ezdxf.revcloud import add_entity as add_revcloud

            vertices = self._plane_points(points)
            if vertices is None:
                return {"ok": False, "error": "points must be a list of [x, y] pairs"}
            if len(vertices) < 2:
                return {"ok": False, "error": "A revision cloud needs at least 2 points"}
            length = float(segment_length)
            if length <= 0:
                return {"ok": False, "error": "segment_length must be > 0"}

            # The arcs are the cloud. ezdxf emits a straight segment when the
            # requested length exceeds the edge, so an oversized segment_length
            # produces a plain polyline that still reports success.
            path = [Vec2(v) for v in vertices]
            edges = list(zip(path, path[1:] + ([path[0]] if closed else []), strict=False))
            shortest = min((a - b).magnitude for a, b in edges) if edges else 0.0
            if length > shortest:
                return {
                    "ok": False,
                    "error": (
                        f"segment_length {length:g} exceeds the shortest edge ({shortest:.4g}); "
                        "the result would carry no arcs and would not be a revision cloud"
                    ),
                }

            # ezdxf's own helper, not a hand-rolled polyline: it stamps the
            # REVCLOUD_PROPS xdata and picks the bulge sign from the winding
            # direction, which is what makes the result recognisable as a
            # revision cloud rather than a wobbly polyline.
            msp = self._msp()
            entity = add_revcloud(msp, vertices, length)
            if layer:
                entity.dxf.layer = layer
            self._mark_dirty()
            return {
                "ok": True,
                "handle": entity.dxf.handle,
                "segments": len(entity),
                "segment_length": length,
                "layer": entity.dxf.layer,
            }

        return await self._async(_sync)

    # ── dimensions ───────────────────────────────────────────────────────────

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
            from engineering.tolerances import build_dim_override

            override, text = build_dim_override(tol_upper, tol_lower, tol_mode, text_override)
            override = _with_header_dimvars(self._require_doc(), override)
            msp = self._msp()
            dim = msp.add_linear_dim(
                base=(float(dim_x), float(dim_y)),
                p1=(float(x1), float(y1)),
                p2=(float(x2), float(y2)),
                angle=float(rotation),
                text=text if text is not None else "<>",
                dimstyle=_DIMSTYLE_NAME,
                override=override or None,
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

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
            msp = self._msp()
            dim = msp.add_aligned_dim(
                p1=(float(x1), float(y1)),
                p2=(float(x2), float(y2)),
                distance=math.sqrt(
                    (float(dim_x) - float(x1)) ** 2 + (float(dim_y) - float(y1)) ** 2
                ),
                dimstyle=_DIMSTYLE_NAME,
                override=_with_header_dimvars(self._require_doc(), None),
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

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
            msp = self._msp()
            vxf, vyf = float(vx), float(vy)
            dim = msp.add_angular_dim_2l(
                base=(float(tx), float(ty)),
                line1=((vxf, vyf), (float(x1), float(y1))),
                line2=((vxf, vyf), (float(x2), float(y2))),
                location=(float(tx), float(ty)),
                dimstyle=_DIMSTYLE_NAME,
                override=_with_header_dimvars(self._require_doc(), None),
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

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
            from engineering.tolerances import build_dim_override

            override, text = build_dim_override(tol_upper, tol_lower, tol_mode, text_override)
            override = _with_header_dimvars(self._require_doc(), override)
            msp = self._msp()
            cxf, cyf = float(cx), float(cy)
            radius = math.sqrt((chord_x - cxf) ** 2 + (chord_y - cyf) ** 2)
            angle_rad = math.atan2(chord_y - cyf, chord_x - cxf)
            leader = float(leader_length)
            # Same correction as dimension_diameter: the size comes from
            # `radius`, and `location` only places the text.
            dim = msp.add_radius_dim(
                center=(cxf, cyf),
                radius=radius,
                location=(
                    cxf + (radius + leader) * math.cos(angle_rad),
                    cyf + (radius + leader) * math.sin(angle_rad),
                ),
                text=text if text is not None else "<>",
                dimstyle=_DIMSTYLE_NAME,
                override=override or None,
            )
            dim.render()
            ent = dim.dimension
            if layer:
                ent.dxf.layer = layer
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

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
            from engineering.tolerances import build_dim_override

            override, text = build_dim_override(tol_upper, tol_lower, tol_mode, text_override)
            override = _with_header_dimvars(self._require_doc(), override)
            msp = self._msp()
            x1f, y1f, x2f, y2f = float(x1), float(y1), float(x2), float(y2)
            cx = (x1f + x2f) / 2
            cy = (y1f + y2f) / 2
            radius = math.sqrt((x2f - x1f) ** 2 + (y2f - y1f) ** 2) / 2
            angle_rad = math.atan2(y2f - y1f, x2f - x1f)
            leader = float(leader_length)
            # The measured size comes from `radius`; `location` only places the
            # text. This used to pass centre + (radius + leader) as `mpoint`,
            # which ezdxf measures to — so every diameter came out
            # 2 x leader_length too large. 60 on a true 40, at default
            # settings, with nothing on the drawing to show for it.
            dim = msp.add_diameter_dim(
                center=(cx, cy),
                radius=radius,
                location=(
                    cx + (radius + leader) * math.cos(angle_rad),
                    cy + (radius + leader) * math.sin(angle_rad),
                ),
                text=text if text is not None else "<>",
                dimstyle=_DIMSTYLE_NAME,
                override=override or None,
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
        # `entitydb.get` is documented not to filter destroyed entities, so a
        # handle from a deleted layout used to pass this guard and then die on
        # the first attribute access with an AttributeError.
        if ent is None or not ent.is_alive:
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
            ent.transform(Matrix44.translate(-bx, -by, 0) @ m @ Matrix44.translate(bx, by, 0))
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
        self,
        handle,
        x1,
        y1,
        x2,
        y2,
        delete_original=False,
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
            m = Matrix44(
                (
                    cos2,
                    sin2,
                    0,
                    0,
                    sin2,
                    -cos2,
                    0,
                    0,
                    0,
                    0,
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                )
            )
            tx, ty = float(x1), float(y1)
            copy.transform(Matrix44.translate(-tx, -ty, 0) @ m @ Matrix44.translate(tx, ty, 0))
            if delete_original:
                self._msp().delete_entity(ent)
            self._mark_dirty()
            return _entity_info_dxf(copy)

        return await self._async(_sync)

    async def entity_offset(
        self,
        handle,
        distance,
        side_x=None,
        side_y=None,
    ) -> EntityInfo:
        def _sync():
            ent = self._get_entity(handle)
            ent_type = ent.dxftype()
            d = float(distance)

            if ent_type == "LINE":
                # Offset a line: move parallel; side_x/side_y selects which side
                start = Vec2(ent.dxf.start.x, ent.dxf.start.y)
                end = Vec2(ent.dxf.end.x, ent.dxf.end.y)
                direction = (end - start).normalize()
                left_normal = Vec2(-direction.y, direction.x)
                # Determine sign: positive = left side (default); flip if side point is on right
                sign = 1.0
                if side_x is not None and side_y is not None:
                    mid = (start + end) * 0.5
                    side_vec = Vec2(float(side_x), float(side_y)) - mid
                    if left_normal.dot(side_vec) < 0:
                        sign = -1.0
                normal = left_normal * (d * sign)
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
                # WCS centre, because side_x/side_y are WCS — the caller got them
                # from entity_get or point_from_snap. Comparing a WCS side point
                # against an OCS centre put the new circle 60 mm away *and* got
                # the inside/outside test backwards, so it grew when asked to
                # shrink. The new circle is created with identity extrusion, so
                # writing WCS into it is correct and the two end up concentric.
                _extrusion = tuple(ent.dxf.get("extrusion", _IDENTITY_EXTRUSION))
                _c = ent.dxf.center
                cx, cy = ocs.to_wcs_2d(_extrusion, _c.x, _c.y, _c.z)
                # side_x/side_y selects inside (shrink) or outside (grow); default = outside
                sign = 1.0
                if side_x is not None and side_y is not None:
                    dist_to_center = math.sqrt(
                        (float(side_x) - cx) ** 2 + (float(side_y) - cy) ** 2
                    )
                    if dist_to_center < ent.dxf.radius:
                        sign = -1.0
                new_r = ent.dxf.radius + d * sign
                if new_r <= 0:
                    raise ValueError("Offset distance too large for circle")
                msp = self._msp()
                new_ent = msp.add_circle(
                    (cx, cy),
                    new_r,
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
        self,
        handle,
        rows,
        cols,
        row_spacing,
        col_spacing,
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
        self,
        handle,
        count,
        fill_angle,
        center_x,
        center_y,
    ) -> list[EntityInfo]:
        def _sync():
            ent = self._get_entity(handle)
            msp = self._msp()
            from ezdxf.math import Matrix44

            results = []
            cx, cy = float(center_x), float(center_y)
            fa = float(fill_angle)
            n = int(count)
            # 360° full circle: n copies evenly spaced, no duplicate at 0°
            divisor = n if abs(fa % 360.0) < 1e-6 else max(n - 1, 1)
            step = math.radians(fa) / divisor
            for i in range(1, n):
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
        self,
        handle,
        layer=None,
        color=None,
        linetype=None,
        lineweight=None,
        visible=None,
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
                ent.dxf.lineweight = normalize_lineweight(lineweight)
            if visible is not None:
                ent.dxf.invisible = not bool(visible)
            self._mark_dirty()
            return {"ok": True, "handle": handle}

        return await self._async(_sync)

    async def entity_edit_text(
        self,
        handle,
        text=None,
        height=None,
        rotation=None,
    ) -> EntityInfo:
        def _sync():
            ent = self._get_entity(handle)
            et = ent.dxftype()
            if et == "TEXT":
                if text is not None:
                    ent.dxf.text = str(text)
                if height is not None:
                    ent.dxf.height = float(height)
                if rotation is not None:
                    ent.dxf.rotation = float(rotation)
            elif et == "MTEXT":
                if text is not None:
                    ent.text = str(text)
                if height is not None:
                    ent.dxf.char_height = float(height)
                if rotation is not None:
                    ent.dxf.rotation = float(rotation)
            else:
                raise RuntimeError(
                    f"entity_edit_text: handle {handle} is {et}, expected TEXT or MTEXT."
                )
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

    # ── text annotation (M8 / F15) ───────────────────────────────────────────

    #: Entity types whose text this server can both read and write. DIMENSION is
    #: deliberately absent: its ``text`` field holds the ``<>`` override
    #: placeholder rather than the measurement.
    _TEXT_BEARING_TYPES = ("TEXT", "MTEXT", "ATTRIB", "ATTDEF")

    _DIMENSION_NOTE = (
        "DIMENSION text is not searched: its text field holds the '<>' override "
        "placeholder rather than the measured value, so editing it would either do "
        "nothing or detach the dimension from what it measures. Use "
        "dimension_* text_override for that."
    )

    async def text_set_background(
        self, handle, enabled: bool = True, color: int | None = None, scale: float = 1.5
    ) -> dict:
        def _sync():
            entity = self._get_entity(handle)
            if entity.dxftype() != "MTEXT":
                return {
                    "ok": False,
                    "error": (
                        f"Handle {handle} is {entity.dxftype()}; only MTEXT carries a "
                        "background fill. Setting one on TEXT would report success and "
                        "change nothing."
                    ),
                }
            if not enabled:
                entity.dxf.discard("bg_fill")
                entity.dxf.discard("bg_fill_color")
                entity.dxf.discard("box_fill_scale")
                self._mark_dirty()
                return {"ok": True, "handle": entity.dxf.handle, "enabled": False}

            factor = float(scale)
            if factor < 1.0:
                return {
                    "ok": False,
                    "error": (
                        f"scale must be >= 1.0 (got {factor:g}); a box smaller than its "
                        "text is a stripe through the text, not a mask"
                    ),
                }
            entity.set_bg_color(int(color) if color is not None else None, scale=factor)
            self._mark_dirty()
            return {
                "ok": True,
                "handle": entity.dxf.handle,
                "enabled": True,
                "color": color,
                "scale": factor,
            }

        return await self._async(_sync)

    async def text_find_replace(
        self,
        find: str,
        replace: str,
        layer: str | None = None,
        match_case: bool = True,
        dry_run: bool = False,
    ) -> dict:
        def _sync():
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
            doc = self._require_doc()
            pattern = re.compile(re.escape(needle), 0 if match_case else re.IGNORECASE)

            def _candidates():
                # Block *definitions* carry ATTDEF and any text drawn inside the
                # block; a search that only walks layouts silently misses them.
                yield from doc.modelspace()
                for layout_name in doc.layouts.names():
                    if layout_name != "Model":
                        yield from doc.layouts.get(layout_name)
                for block in doc.blocks:
                    yield from block
                for entity in doc.modelspace().query("INSERT"):
                    yield from entity.attribs

            changed: list[dict] = []
            seen: set[str] = set()
            for entity in _candidates():
                dxftype = entity.dxftype()
                if dxftype not in self._TEXT_BEARING_TYPES:
                    continue
                key = entity.dxf.get("handle", None)
                if key in seen:
                    continue
                seen.add(key)
                if layer and entity.dxf.get("layer", "0") != layer:
                    continue
                current = entity.text if dxftype == "MTEXT" else entity.dxf.get("text", "")
                if not current or not pattern.search(current):
                    continue
                updated = pattern.sub(replace, current)
                if not dry_run:
                    if dxftype == "MTEXT":
                        entity.text = updated
                    else:
                        entity.dxf.text = updated
                changed.append(
                    {
                        "handle": key,
                        "type": dxftype,
                        "before": current,
                        "after": updated,
                    }
                )

            if changed and not dry_run:
                self._mark_dirty()
            return {
                "ok": True,
                "replaced": len(changed),
                "entities": changed,
                # Reported, not implied: "no matches" and "never looked there"
                # are different answers, and the caller can only tell them apart
                # if the scope is on the response.
                "searched_types": list(self._TEXT_BEARING_TYPES),
                "note": self._DIMENSION_NOTE,
                "dry_run": bool(dry_run),
            }

        return await self._async(_sync)

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
            ent = self._get_entity(handle)
            et = ent.dxftype()
            # cx/cy arrive in WCS, because that is what entity_get reports. For a
            # mirrored entity the stored attribute is not WCS, so the value has to
            # be mapped back into the entity's own frame before it is written.
            # Read and write used to be *equally* wrong, which made a round trip an
            # accidental no-op; normalising only the read would have turned that
            # no-op into a real 60 mm move.
            new_cx, new_cy = cx, cy
            if et in ("CIRCLE", "ARC"):
                extrusion = tuple(ent.dxf.get("extrusion", _IDENTITY_EXTRUSION))
                if not ocs.is_wcs_frame(extrusion):
                    if not ocs.is_flat_frame(extrusion):
                        raise UnsupportedCapabilityError(
                            "ocs_tilted_plane",
                            f"entity_edit_geometry: entity {handle} lies in a plane "
                            f"tilted out of WCS XY (normal="
                            f"{tuple(round(float(v), 4) for v in extrusion)}). A 2D "
                            "(cx, cy) does not determine a point in that plane, so "
                            "writing one would displace the entity out of its own "
                            "plane by an amount this server cannot infer. Use "
                            "entity_move, which transforms the whole entity and "
                            "preserves its plane, or edit it in AutoCAD.",
                        )
                    centre = ent.dxf.center
                    current = ocs.to_wcs_2d(extrusion, centre.x, centre.y, centre.z)
                    target_x = float(cx) if cx is not None else current[0]
                    target_y = float(cy) if cy is not None else current[1]
                    new_cx, new_cy = ocs.from_wcs(extrusion, target_x, target_y, centre.z)[:2]
            if et == "CIRCLE":
                c = ent.dxf.center
                ent.dxf.center = (
                    float(new_cx) if new_cx is not None else c[0],
                    float(new_cy) if new_cy is not None else c[1],
                    c[2] if len(c) > 2 else 0.0,
                )
                if radius is not None:
                    ent.dxf.radius = float(radius)
            elif et == "LINE":
                s, e = ent.dxf.start, ent.dxf.end
                ent.dxf.start = (
                    float(x1) if x1 is not None else s[0],
                    float(y1) if y1 is not None else s[1],
                    s[2] if len(s) > 2 else 0.0,
                )
                ent.dxf.end = (
                    float(x2) if x2 is not None else e[0],
                    float(y2) if y2 is not None else e[1],
                    e[2] if len(e) > 2 else 0.0,
                )
            elif et == "ARC":
                c = ent.dxf.center
                ent.dxf.center = (
                    float(new_cx) if new_cx is not None else c[0],
                    float(new_cy) if new_cy is not None else c[1],
                    c[2] if len(c) > 2 else 0.0,
                )
                if radius is not None:
                    ent.dxf.radius = float(radius)
                if start_angle is not None:
                    ent.dxf.start_angle = float(start_angle)
                if end_angle is not None:
                    ent.dxf.end_angle = float(end_angle)
            else:
                raise RuntimeError(
                    f"entity_edit_geometry: {et} not supported (use CIRCLE, LINE, or ARC)."
                )
            self._mark_dirty()
            return _entity_info_dxf(ent)

        return await self._async(_sync)

    # ── selection filters (M8 / F1) ──────────────────────────────────────────

    _SELECTION_MODES = ("window", "crossing")

    def _select_shape(self, shape, mode: str, entity_type: str, layer: str) -> dict:
        """Run a selection shape over the current space and shape the response."""
        from ezdxf import select

        picker = select.bbox_inside if mode == "window" else select.bbox_overlap
        wanted_type = str(entity_type or "").strip().upper()
        wanted_layer = str(layer or "").strip()

        handles = []
        for entity in picker(shape, self._msp()):
            if wanted_type and entity.dxftype() != wanted_type:
                continue
            if wanted_layer and entity.dxf.get("layer", "0") != wanted_layer:
                continue
            handles.append(entity.dxf.handle)
        return {"ok": True, "handles": handles, "count": len(handles), "mode": mode}

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

    async def selection_window(
        self, x1, y1, x2, y2, mode: str = "window", entity_type: str = "", layer: str = ""
    ) -> dict:
        def _sync():
            from ezdxf import select

            resolved, refusal = self._selection_mode(mode)
            if refusal:
                return refusal
            lo_x, hi_x = sorted((float(x1), float(x2)))
            lo_y, hi_y = sorted((float(y1), float(y2)))
            if lo_x == hi_x or lo_y == hi_y:
                # An empty answer here would read as "nothing is there" rather
                # than "you asked for a box that cannot contain anything".
                return {
                    "ok": False,
                    "error": (
                        "The selection box has zero area; give two opposite corners that "
                        "differ in both x and y"
                    ),
                }
            return self._select_shape(
                select.Window((lo_x, lo_y), (hi_x, hi_y)), resolved, entity_type, layer
            )

        return await self._async(_sync)

    async def selection_polygon(
        self, points, mode: str = "window", entity_type: str = "", layer: str = ""
    ) -> dict:
        def _sync():
            from ezdxf import select

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
            return self._select_shape(select.Polygon(vertices), resolved, entity_type, layer)

        return await self._async(_sync)

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

            def _area(entity) -> float | None:
                """None means "this shape has no area", not "its area is zero".

                Answered by the same measurement `entity_measure` uses, so the
                two cannot disagree about the same entity. They did: this used
                to read ``entity.dxf.get("area", 0.0)`` for SOLID, TRACE and
                HATCH, none of which carry an ``area`` DXF attribute, so every
                one of them measured 0.0 and any positive threshold silently
                excluded every hatch in the drawing while ``filtered_by``
                reported that the filter had run.

                The two refusals stay distinguishable. A type that bounds no
                area on any engine raises RuntimeError and is *excluded*; a
                type this engine cannot evaluate (ACIS) refuses with a
                capability and is also excluded, because "I could not measure
                it" must never read as "it is big enough".
                """
                try:
                    return float(_measure_dxf_entity(entity, 0.001)["area"])
                except (UnsupportedCapabilityError, RuntimeError, ValueError, TypeError):
                    return None

            handles = []
            for entity in self._msp():
                if wanted_type and entity.dxftype() != wanted_type:
                    continue
                if wanted_layer and entity.dxf.get("layer", "0") != wanted_layer:
                    continue
                if color is not None and int(entity.dxf.get("color", 256)) != int(color):
                    continue
                if wanted_linetype and entity.dxf.get("linetype", "ByLayer") != wanted_linetype:
                    continue
                if min_area is not None:
                    area = _area(entity)
                    if area is None or area < float(min_area):
                        continue
                handles.append(entity.dxf.handle)

            return {
                "ok": True,
                "handles": handles,
                "count": len(handles),
                "filtered_by": filtered_by,
            }

        return await self._async(_sync)

    async def entity_list(
        self,
        type_filter=None,
        layer_filter=None,
        limit=200,
        offset=0,
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

    async def entity_count(self, type_filter=None, layer_filter=None) -> int:
        # Deliberately a mirror of entity_list's filter arms rather than a call
        # into it: the whole value of the count is that it never constructs an
        # EntityInfo (bounding-box extents alone are the expensive part). The
        # comparisons are kept character-for-character identical to the ones
        # above so `total` can never be measured against a different set than
        # the page it describes.
        def _sync():
            msp = self._msp()
            count = 0
            for ent in msp:
                if type_filter and type_filter.upper() != ent.dxftype().upper():
                    continue
                if layer_filter and layer_filter.lower() != ent.dxf.get("layer", "0").lower():
                    continue
                count += 1
            return count

        return await self._async(_sync)

    # ── layer management ──────────────────────────────────────────────────────

    async def layer_list(self) -> list[LayerInfo]:
        def _sync():
            doc = self._require_doc()
            return [_layer_info_dxf(lyr, self._current_layer) for lyr in doc.layers]

        return await self._async(_sync)

    async def layer_create(
        self,
        name,
        color=7,
        linetype="Continuous",
        lineweight=-3,
    ) -> LayerInfo:
        def _sync():
            doc = self._require_doc()
            _ensure_linetype_loaded(doc, linetype)
            lyr = doc.layers.add(
                name,
                color=int(color),
                linetype=linetype,
                lineweight=normalize_lineweight(lineweight),
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
        self,
        name,
        color=None,
        linetype=None,
        lineweight=None,
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
                lyr.dxf.lineweight = normalize_lineweight(lineweight)
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
                    return {"ok": True, "name": lt_name, "source": "ezdxf.tools.standards"}
            if _add_linetype_from_fallback(doc, name):
                self._mark_dirty()
                return {"ok": True, "name": name.upper(), "source": "autocad_fallback"}
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
                attr_count = sum(1 for e in blk if e.dxftype() == "ATTDEF")
                origin = blk.block.dxf.get("base_point", (0.0, 0.0, 0.0))
                is_xref = bool(blk.block.dxf.get("xref_path", ""))
                # S3: surface the block definition's description instead of always "".
                description = blk.block.dxf.get("description", "")
                blocks.append(
                    BlockInfo(
                        name=blk.name,
                        origin=(origin[0], origin[1]),
                        attribute_count=attr_count,
                        entity_count=len(list(blk)),
                        is_xref=is_xref,
                        description=description,
                    )
                )
            return blocks

        return await self._async(_sync)

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
        self,
        name,
        handles,
        base_x=0.0,
        base_y=0.0,
    ) -> dict:
        def _sync():
            doc = self._require_doc()
            # Resolve first. Creating the definition and then discovering every
            # handle was a typo left an empty block behind, and reporting
            # entity_count without naming what was dropped told the caller three
            # of their five entities made it and nothing about the other two.
            entities, skipped = [], []
            for handle in handles:
                try:
                    entities.append(self._get_entity(handle))
                except Exception as exc:
                    log.debug("resolving %s for block %s: %s", handle, name, exc)
                    skipped.append(str(handle))
            if not entities:
                raise RuntimeError(
                    f"block_create_from_entities: none of the handles resolved "
                    f"({', '.join(skipped) or 'no handles given'}), so there is nothing "
                    f"to put in block {name!r}. No definition was created."
                )

            blk = doc.blocks.new(name=name)
            blk.block.dxf.base_point = (float(base_x), float(base_y), 0.0)
            for ent in entities:
                blk.add_entity(ent.copy())
            self._mark_dirty()
            return {
                "ok": True,
                "name": name,
                "entity_count": len(entities),
                "skipped": skipped,
                "backend": "ezdxf",
            }

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
        self,
        x1,
        y1,
        x2,
        y2,
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
                        if (
                            bb
                            and bb.extmin.x >= mn_x
                            and bb.extmax.x <= mx_x
                            and bb.extmin.y >= mn_y
                            and bb.extmax.y <= mx_y
                        ):
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

    async def entity_measure(self, handle, flatten_tolerance: float = 0.001) -> dict:
        def _sync():
            return _measure_dxf_entity(self._get_entity(handle), float(flatten_tolerance))

        return await self._async(_sync)

    # ── boundary tracing (M8 / F3) ───────────────────────────────────────────

    #: How many seed-adjacent edges to try as loop starts. Bounded because the
    #: work is per candidate; unbounded it would degrade on a large drawing
    #: without finding a better answer than the nearest edges already give.
    _BOUNDARY_SEED_CANDIDATES = 64

    #: Above this many edges the O(n^2) intersection split is skipped. Reported
    #: on the response rather than silently degrading the answer.
    _BOUNDARY_SPLIT_LIMIT = 400

    @classmethod
    def _split_edges_at_intersections(cls, edges, gap_tol: float):
        """Split straight edges where they cross, so crossings become junctions.

        ``edgeminer`` connects edges by shared *endpoints*. A construction line
        drawn across a square touches it at two points that are not vertices of
        anything, so in the edge graph the line is not connected to the square
        at all and the halves it makes are invisible. AutoCAD's BOUNDARY
        computes the intersections; this does the same for straight edges.

        Curved edges are left whole: splitting an arc mid-span needs the arc's
        own parameters, and a wrong split would move geometry rather than just
        miss a loop. ``split`` on the response says whether this ran.
        """
        from ezdxf.math import Vec2, intersection_line_line_2d

        straight = []
        curved = []
        for edge in edges:
            payload = getattr(edge, "payload", None)
            dxftype = payload.dxftype() if payload is not None else ""
            (straight if dxftype in ("LINE", "") else curved).append(edge)

        cuts: dict[int, list] = {index: [] for index in range(len(straight))}
        for i, a in enumerate(straight):
            for j in range(i + 1, len(straight)):
                b = straight[j]
                point = intersection_line_line_2d(
                    (Vec2(a.start), Vec2(a.end)), (Vec2(b.start), Vec2(b.end)), virtual=False
                )
                if point is None:
                    continue
                for index, edge in ((i, a), (j, b)):
                    if (point - Vec2(edge.start)).magnitude > gap_tol and (
                        point - Vec2(edge.end)
                    ).magnitude > gap_tol:
                        cuts[index].append(point)

        from ezdxf import edgeminer as em

        result = list(curved)
        for index, edge in enumerate(straight):
            points = cuts[index]
            if not points:
                result.append(edge)
                continue
            start, end = Vec2(edge.start), Vec2(edge.end)
            ordered = sorted(set(points), key=lambda p: (p - start).magnitude)
            previous = start
            for point in [*ordered, end]:
                if (point - previous).magnitude > gap_tol:
                    result.append(em.make_edge(previous, point, payload=edge.payload))
                previous = point
        return result

    @staticmethod
    def _edge_bulge(edge) -> float:
        """The bulge that reproduces a curved edge as a polyline segment.

        Without this the traced boundary of two semicircles is a straight line
        between two points — the shape is gone and only the area report would
        have noticed.
        """
        from ezdxf import edgesmith
        from ezdxf.math import Vec2

        payload = getattr(edge, "payload", None)
        if payload is None or payload.dxftype() not in ("ARC", "CIRCLE"):
            return 0.0
        try:
            span = edgesmith.arc_angle_span_deg(
                float(payload.dxf.start_angle), float(payload.dxf.end_angle)
            )
            bulge = edgesmith.bulge_from_arc_angle(math.radians(span))
        except Exception:
            return 0.0
        # The chain may walk the arc backwards; the bulge follows the walk.
        center = Vec2(payload.dxf.center)
        start_is_arc_start = (
            Vec2(edge.start)
            - (
                center
                + Vec2.from_deg_angle(float(payload.dxf.start_angle), float(payload.dxf.radius))
            )
        ).magnitude <= 1e-6
        return bulge if start_is_arc_start else -bulge

    @classmethod
    def _chain_to_polyline(cls, space, chain, tolerance: float):
        """Turn a chain of edges into a genuinely closed LWPOLYLINE.

        ``edgesmith.lwpolyline_from_chain`` emits n+1 points with the last
        duplicating the first and ``closed`` left False — a polyline that looks
        shut on screen and behaves open to everything that asks: hatch
        association, area queries, ``entity_offset``. The duplicate is dropped
        and the flag set here instead, and each curved edge keeps its bulge.
        """
        from ezdxf.math import Vec2

        points = []
        for edge in chain:
            start = Vec2(edge.start)
            points.append((start.x, start.y, 0.0, 0.0, cls._edge_bulge(edge)))
        last = Vec2(chain[-1].end)
        if (last - Vec2(chain[0].start)).magnitude > max(tolerance, 1e-9):
            points.append((last.x, last.y, 0.0, 0.0, 0.0))

        polyline = space.add_lwpolyline(points, format="xyseb", close=True)
        polyline.closed = True
        return polyline, points

    @staticmethod
    def _loop_area(chain) -> float:
        """Unsigned area enclosed by a chain of edges, arcs included.

        Not ``edgesmith.loop_area``: it works off the edge *endpoints*, so two
        semicircular arcs closing into a circle measure 0.0 against a true
        314.159 — the curvature is exactly the part it drops. Flattening the
        chain through its own path keeps the curvature at a small, bounded cost
        (314.12 against 314.159 at a 0.01 sag).

        This is the **ranking** area: `boundary_trace` uses it to pick the
        smallest loop enclosing the seed, where a 0.01% error cannot change the
        winner. What the caller is told comes from `_polyline_area` instead —
        see there for why the two must not be the same number.
        """
        from ezdxf import edgesmith

        from engineering.measure import polygon_area_perimeter

        points = [[v.x, v.y, 0.0] for v in edgesmith.path2d_from_chain(chain).flattening(0.01)]
        if len(points) < 3:
            return 0.0
        return abs(polygon_area_perimeter(points, closed=True)[0])

    @staticmethod
    def _polyline_area(vertices) -> float:
        """Unsigned area of the polyline these tools actually stored.

        The reported area used to come from `_loop_area`, a copy of the shape
        flattened at a fixed 0.01 sag, while `entity_measure` read the stored
        bulges analytically. Same figure, two tools, 139.2177 against 139.2699,
        and neither payload said which one was the approximation. The vertices
        handed to `add_lwpolyline` carry exact bulges, so the exact answer was
        always there for the taking.
        """
        from engineering.measure import polygon_area_perimeter

        # Two vertices is not a degenerate case here: a loop of two arcs is a
        # circle, and it stores as exactly that.
        points = [(x, y, bulge) for x, y, _s, _e, bulge in vertices]
        if len(points) < 2:
            return 0.0
        # Six decimals, the same as `entity_measure` and
        # `analysis_measure_distance`. Agreeing to full float precision and
        # then printing different tails is still two answers.
        return round(abs(polygon_area_perimeter(points, closed=True)[0]), 6)

    async def boundary_trace(self, x, y, layer=None, tolerance: float = 1e-9) -> dict:
        def _sync():
            from ezdxf import edgeminer as em
            from ezdxf import edgesmith
            from ezdxf.math import Vec2

            space = self._msp()
            candidates = [
                e
                for e in space
                if (not layer or e.dxf.get("layer", "0") == layer) and e.dxftype() != "POINT"
            ]
            edges = list(edgesmith.edges_from_entities_2d(candidates))
            if not edges:
                return {"ok": False, "error": "No linear geometry to trace a boundary from"}

            gap_tol = max(float(tolerance), 1e-9)
            split = len(edges) <= self._BOUNDARY_SPLIT_LIMIT
            if split:
                edges = self._split_edges_at_intersections(edges, gap_tol)

            seed = Vec2((float(x), float(y)))
            deposit = em.Deposit(edges, gap_tol=gap_tol)
            # Walk loops outward from the edges nearest the seed. Enumerating
            # every loop first would be O(n!); one loop per candidate edge is
            # linear in the edges actually tried.
            # Every candidate is scored and the SMALLEST enclosing loop wins:
            # with nested regions the outer one also contains the seed, and
            # stopping at the first hit would hand back the wrong boundary.
            # Bounded to the nearest edges so a large drawing stays linear.
            best = None
            nearest = sorted(edges, key=lambda e: (Vec2(e.start) - seed).magnitude)
            for edge in nearest[: self._BOUNDARY_SEED_CANDIDATES]:
                for clockwise in (True, False):
                    try:
                        chain = em.find_loop_by_edge(deposit, edge, clockwise=clockwise)
                    except Exception:
                        continue
                    if not chain:
                        continue
                    ring = [Vec2(link.start) for link in chain]
                    if len(ring) < 3 or edgesmith.is_point_in_polygon_2d(seed, ring) < 0:
                        continue
                    area = self._loop_area(chain)
                    if area > 0 and (best is None or area < best[1]):
                        best = (chain, area)

            if best is None:
                # "These do not form a loop" is not actionable on its own. If
                # an endpoint is dangling, the refusal names where, because
                # that is the coordinate the caller has to go and fix.
                gap_start, gap_end = self._dangling_endpoint(edges, max(float(tolerance), 1e-9))
                if (gap_start - gap_end).magnitude > max(float(tolerance), 1e-9):
                    return {
                        "ok": False,
                        "error": (
                            f"No closed loop encloses ({float(x):g}, {float(y):g}): there is a "
                            f"gap at ({gap_start.x:g}, {gap_start.y:g}) -> "
                            f"({gap_end.x:g}, {gap_end.y:g})"
                        ),
                    }
                return {
                    "ok": False,
                    "error": (
                        f"No closed loop encloses ({float(x):g}, {float(y):g}); the seed point "
                        "is outside every closed region"
                    ),
                }

            chain, _ = best
            polyline, vertices = self._chain_to_polyline(space, chain, float(tolerance))
            if layer:
                polyline.dxf.layer = layer
            sources = sorted(
                {
                    handle
                    for handle in (
                        getattr(getattr(link.payload, "dxf", None), "handle", None)
                        for link in chain
                    )
                    if handle
                }
            )
            self._mark_dirty()
            return {
                "ok": True,
                "handle": polyline.dxf.handle,
                "vertices": len(vertices),
                "area": self._polyline_area(vertices),
                "closed": True,
                "source_handles": sources,
            }

        return await self._async(_sync)

    async def boundary_from_entities(self, handles, tolerance: float = 1e-9) -> dict:
        def _sync():
            from ezdxf import edgeminer as em
            from ezdxf import edgesmith

            if not handles or len(handles) < 2:
                return {"ok": False, "error": "At least 2 entity handles are needed to form a loop"}

            doc = self._require_doc()
            entities = []
            for raw in handles:
                key = str(raw or "").strip().upper()
                entity = doc.entitydb.get(key)
                if entity is None or not entity.is_alive:
                    return {"ok": False, "error": f"Entity handle not found: {raw}"}
                if not edgesmith.is_pure_2d_entity(entity):
                    return {
                        "ok": False,
                        "error": (
                            f"Handle {key} is a {entity.dxftype()}; a boundary can only be "
                            "chained from 2D linear entities"
                        ),
                    }
                entities.append(entity)

            edges = list(edgesmith.edges_from_entities_2d(entities))
            gap_tol = max(float(tolerance), 1e-9)
            # The handles arrive in drawing order, not chain order — putting
            # them in order is the tool's job, so the loop is searched for
            # rather than assumed.
            try:
                chain = em.find_loop(em.Deposit(edges, gap_tol=gap_tol), timeout=5.0)
            except Exception:
                chain = []
            if not chain or len(chain) != len(edges):
                start, end = self._dangling_endpoint(edges, gap_tol)
                return {
                    "ok": False,
                    "error": (
                        f"These entities do not close: there is a gap at ({start.x:g}, "
                        f"{start.y:g}) -> ({end.x:g}, {end.y:g})"
                    ),
                }

            polyline, vertices = self._chain_to_polyline(self._msp(), chain, gap_tol)
            self._mark_dirty()
            return {
                "ok": True,
                "handle": polyline.dxf.handle,
                "vertices": len(vertices),
                "area": self._polyline_area(vertices),
                "closed": True,
            }

        return await self._async(_sync)

    @staticmethod
    def _dangling_endpoint(edges, gap_tol: float):
        """The first endpoint with no partner, so the refusal can name the gap."""
        from ezdxf.math import Vec2

        endpoints = []
        for edge in edges:
            endpoints.extend([Vec2(edge.start), Vec2(edge.end)])
        for point in endpoints:
            matches = sum(1 for other in endpoints if (point - other).magnitude <= gap_tol)
            if matches < 2:
                nearest = min(
                    (p for p in endpoints if (p - point).magnitude > gap_tol),
                    key=lambda p: (p - point).magnitude,
                    default=point,
                )
                return point, nearest
        return endpoints[0], endpoints[-1]

    async def analysis_list_properties(self, handle: str) -> dict:
        def _sync():
            entity = self._get_entity(handle)
            dxftype = entity.dxftype()
            extrusion = entity.dxf.get("extrusion", (0.0, 0.0, 1.0))
            needs_ocs = dxftype in _OCS_ENTITY_TYPES and not ocs.is_wcs_frame(extrusion)

            def _value(key, raw):
                # Vec3 and friends are not JSON-serialisable, and a raw OCS
                # coordinate would contradict every other coordinate this
                # server reports. `extrusion` itself stays untranslated — it
                # is the frame, not a point in it.
                if hasattr(raw, "x") and hasattr(raw, "y"):
                    if key == "extrusion":
                        return [float(raw.x), float(raw.y), float(getattr(raw, "z", 0.0))]
                    if needs_ocs:
                        return ocs.to_wcs_2d(extrusion, raw.x, raw.y, getattr(raw, "z", 0.0))
                    return [float(raw.x), float(raw.y), float(getattr(raw, "z", 0.0))]
                if isinstance(raw, (list, tuple)):
                    return [_value(key, item) for item in raw]
                if isinstance(raw, (int, float, str, bool)) or raw is None:
                    return raw
                return str(raw)

            dump = {key: _value(key, raw) for key, raw in entity.dxfattribs().items()}
            if dxftype == "LWPOLYLINE":
                # Every vertex reports its bulge, including the straight ones:
                # a points list that drops zero bulges cannot be read back
                # positionally.
                dump["points"] = [
                    [*ocs.to_wcs_2d(extrusion, p[0], p[1]), p[4]]
                    if needs_ocs
                    else [float(p[0]), float(p[1]), float(p[4])]
                    for p in entity.get_points()
                ]
            if dxftype == "MTEXT":
                dump.setdefault("text", entity.text)

            info = _entity_info_dxf(entity)
            return {
                "ok": True,
                "handle": info.handle,
                "type": dxftype,
                "layer": info.layer,
                "properties": info.properties,
                "dxf_attributes": dump,
            }

        return await self._async(_sync)

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
            return [_entity_info_dxf(e) for e in msp if et == e.dxftype().upper()]

        return await self._async(_sync)

    async def selection_get(self) -> dict:
        # COM-only: headless ezdxf has no viewport / pickfirst grip selection.
        # Return a not-supported result but keep the same shape (empty handles)
        # so callers that iterate the selection do not KeyError.
        return {
            "ok": False,
            "error": "selection_get not supported in ezdxf backend (no live AutoCAD viewport)",
            "count": 0,
            "handles": [],
            "entities": [],
            "pickfirst": None,
        }

    # ── view / screenshot ──────────────────────────────────────────────────────

    async def view_zoom_extents(self) -> dict:
        # R26/N9: ezdxf has no viewport, so framing is never actually applied.
        # Return applied=False with a consistent shape so a client cannot mistake
        # the no-op for real framing.
        return {
            "ok": True,
            "applied": False,
            "message": "Zoom extents not applicable for ezdxf backend (no display/viewport)",
        }

    async def view_zoom_window(self, x1, y1, x2, y2) -> dict:
        # R26/N9: window region is ignored (no viewport). Consistent shape with
        # view_zoom_extents; applied=False signals the framing was not honored.
        return {
            "ok": True,
            "applied": False,
            "message": "Zoom window not applicable for ezdxf backend (no display/viewport)",
        }

    async def view_screenshot(self, overlay_handles: bool = False) -> bytes | None:
        """Render drawing to PNG using matplotlib."""
        result = await self.view_screenshot_grounded(
            overlay_handles=overlay_handles, max_labels=DEFAULT_MAX_HANDLE_LABELS
        )
        return result["png"]

    async def view_screenshot_grounded(
        self,
        overlay_handles: bool = True,
        max_labels: int = DEFAULT_MAX_HANDLE_LABELS,
    ) -> dict:
        """Render to PNG, optionally labelling each entity with its handle.

        Handle grounding: a screenshot shows what the drawing looks like and
        nothing the caller can act on, because every modify tool takes a handle.
        Drawing the handle at the entity's own bounding-box centre makes "the
        circle at the top-left" and "handle 2F" the same statement, and returns
        the label positions so the mapping is machine-readable too.

        Labels are ink on the drawing, so a crowded sheet is capped and the
        image says so — labelling a subset silently would let the picture read
        as the whole drawing.
        """

        def _sync():
            try:
                doc = self._require_doc()
                space = self._msp()
                from ezdxf.addons.drawing import Frontend, RenderContext
                from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

                fig = _new_agg_figure(figsize=(16, 9), dpi=100)
                ax = fig.add_axes([0, 0, 1, 1])
                ctx = RenderContext(doc)
                out = MatplotlibBackend(ax)
                Frontend(ctx, out).draw_layout(space, finalize=True)

                labels: dict[str, list[float]] = {}
                entities = list(space)
                total = len(entities)
                truncated = False
                if overlay_handles and total:
                    limit = max(0, int(max_labels))
                    truncated = total > limit
                    for ent in entities[:limit]:
                        anchor = _label_anchor(ent)
                        if anchor is None:
                            continue
                        handle = str(ent.dxf.get("handle", "?"))
                        labels[handle] = [anchor[0], anchor[1]]
                        ax.annotate(
                            handle,
                            anchor,
                            color="#d62728",
                            fontsize=7,
                            ha="center",
                            va="center",
                            bbox={
                                "boxstyle": "round,pad=0.15",
                                "facecolor": "white",
                                "edgecolor": "#d62728",
                                "linewidth": 0.4,
                                "alpha": 0.85,
                            },
                            annotation_clip=False,
                        )
                    if truncated:
                        ax.set_title(
                            f"handles shown for {len(labels)} of {total} entities",
                            color="#d62728",
                            fontsize=9,
                        )

                buf = io.BytesIO()
                fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
                buf.seek(0)
                return {
                    "png": buf.read(),
                    "labels": labels,
                    "labelled": len(labels),
                    "total": total,
                    "truncated": truncated,
                }
            except ImportError:
                log.warning("matplotlib not installed – screenshot unavailable")
                return {
                    "png": None,
                    "labels": {},
                    "labelled": 0,
                    "total": 0,
                    "truncated": False,
                }

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
            self._transaction_stack.append(Path(tmp_path))
            max_stack = config.settings.max_undo_stack
            while len(self._transaction_stack) > max_stack:
                old = self._transaction_stack.pop(0)
                try:
                    old.unlink()
                except OSError:
                    pass
            return {"ok": True, "message": "Transaction begun (DXF snapshot saved for rollback)"}

        return await self._async(_sync)

    async def transaction_commit(self) -> dict:
        # NEW-undo-1: guard the _undo_stack read+pop with self._lock (via _async)
        # instead of touching it outside any lock.
        def _sync():
            if not self._transaction_stack:
                return {"ok": False, "error": "No active transaction"}
            p = self._transaction_stack.pop()
            try:
                p.unlink()
            except OSError:
                pass
            return {"ok": True, "message": "Transaction committed (snapshot discarded)"}

        return await self._async(_sync)

    async def transaction_rollback(self) -> dict:
        # NEW-undo-1: read+mutate _undo_stack under self._lock (via _async) so the
        # empty-check and the pop/readfile are serialized with other sync work.
        def _sync():
            if not self._transaction_stack:
                return {"ok": False, "error": "No active transaction to rollback"}
            p = self._transaction_stack.pop()
            try:
                self._doc = ezdxf.readfile(str(p))
                _normalise_dimstyle_rounding(self._doc)
                self._current_layer = str(self._doc.header.get("$CLAYER", "0"))
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
        # R32: deliberately NOT routed through _async. It reads self._doc
        # directly, which is what keeps it answerable while the document is
        # quarantined; putting it behind the lock would make the diagnostic
        # channel the first thing the quarantine closes, leaving the user with a
        # refusal and no way to ask what happened.
        self._reap_quarantine()
        has_doc = self._doc is not None
        return {
            "backend": "ezdxf",
            "connected": True,
            "has_document": has_doc,
            "document_path": self._doc_path,
            "unsaved_changes": self._dirty,
            "transaction_depth": len(self._transaction_stack),
            "quarantine": self._quarantine.to_dict() if self._quarantine else None,
            "abandoned_calls": [record.to_dict() for record in self._abandoned_calls],
            "capabilities": [
                "file_read_write",
                "dxf_export",
                "pdf_export",
                "entity_creation",
                "layer_management",
                "blocks",
                "dimensions",
                "analysis",
                "screenshot_matplotlib",
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

    # ── corner ops ──────────────────────────────────────────────────────────

    @staticmethod
    def _line_endpoints(ent) -> tuple[tuple[float, float], tuple[float, float]]:
        return (
            (float(ent.dxf.start.x), float(ent.dxf.start.y)),
            (float(ent.dxf.end.x), float(ent.dxf.end.y)),
        )

    @staticmethod
    def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    async def entity_trim(self, target_handle, cutter_handle, keep_x, keep_y) -> EntityInfo:
        def _sync():
            from ezdxf.math import Vec2, intersection_line_line_2d

            target = self._get_entity(target_handle)
            cutter = self._get_entity(cutter_handle)
            if target.dxftype() != "LINE" or cutter.dxftype() != "LINE":
                raise RuntimeError(
                    f"entity_trim V1 supports LINE+LINE only "
                    f"(got {target.dxftype()}+{cutter.dxftype()}). V2 will add LINE+ARC."
                )
            if target_handle == cutter_handle:
                raise RuntimeError("entity_trim: target and cutter cannot be the same entity.")
            t_start, t_end = self._line_endpoints(target)
            c_start, c_end = self._line_endpoints(cutter)
            # virtual=True so cutter can extend in the target's direction
            # (AutoCAD's default trim behaviour = "implied extend").
            ip = intersection_line_line_2d(
                (Vec2(*t_start), Vec2(*t_end)),
                (Vec2(*c_start), Vec2(*c_end)),
                virtual=True,
            )
            if ip is None:
                raise RuntimeError(
                    "entity_trim: target and cutter are parallel or do not intersect."
                )
            ix, iy = float(ip.x), float(ip.y)
            keep = (float(keep_x), float(keep_y))
            d_start = self._dist2(t_start, keep)
            d_end = self._dist2(t_end, keep)
            if d_start <= d_end:
                target.dxf.end = (ix, iy, 0.0)
            else:
                target.dxf.start = (ix, iy, 0.0)
            self._mark_dirty()
            return _entity_info_dxf(target)

        return await self._async(_sync)

    async def entity_extend(
        self,
        target_handle,
        boundary_handle,
        end_x=None,
        end_y=None,
    ) -> EntityInfo:
        def _sync():
            from ezdxf.math import Vec2, intersection_line_line_2d

            target = self._get_entity(target_handle)
            boundary = self._get_entity(boundary_handle)
            if target.dxftype() != "LINE" or boundary.dxftype() != "LINE":
                raise RuntimeError(
                    f"entity_extend V1 supports LINE+LINE only "
                    f"(got {target.dxftype()}+{boundary.dxftype()}). V2 will add LINE+ARC."
                )
            if target_handle == boundary_handle:
                raise RuntimeError("entity_extend: target and boundary cannot be the same entity.")
            t_start, t_end = self._line_endpoints(target)
            b_start, b_end = self._line_endpoints(boundary)
            ip = intersection_line_line_2d(
                (Vec2(*t_start), Vec2(*t_end)),
                (Vec2(*b_start), Vec2(*b_end)),
                virtual=True,  # treat lines as infinite for extend
            )
            if ip is None:
                raise RuntimeError(
                    "entity_extend: target and boundary are parallel; no extension possible."
                )
            ix, iy = float(ip.x), float(ip.y)
            if end_x is None or end_y is None:
                # auto: extend the endpoint nearest the boundary midpoint
                b_mid = ((b_start[0] + b_end[0]) / 2, (b_start[1] + b_end[1]) / 2)
                ref = b_mid
            else:
                ref = (float(end_x), float(end_y))
            d_start = self._dist2(t_start, ref)
            d_end = self._dist2(t_end, ref)
            if d_start <= d_end:
                target.dxf.start = (ix, iy, 0.0)
            else:
                target.dxf.end = (ix, iy, 0.0)
            self._mark_dirty()
            return _entity_info_dxf(target)

        return await self._async(_sync)

    def _fillet_chamfer_setup(self, handle1: str, handle2: str, op: str):
        """Shared geometry setup for fillet/chamfer.
        Returns (line1, line2, P, d1, d2, A_far, B_far, theta) where d1/d2 are
        unit vectors from intersection P toward the far endpoint of each line."""
        from ezdxf.math import Vec2, intersection_line_line_2d

        line1 = self._get_entity(handle1)
        line2 = self._get_entity(handle2)
        if line1.dxftype() != "LINE" or line2.dxftype() != "LINE":
            raise RuntimeError(
                f"entity_{op} V1 supports LINE+LINE only "
                f"(got {line1.dxftype()}+{line2.dxftype()}). V2 will add LINE+ARC."
            )
        if handle1 == handle2:
            raise RuntimeError(f"entity_{op}: handle1 and handle2 cannot be the same.")
        a_start, a_end = self._line_endpoints(line1)
        b_start, b_end = self._line_endpoints(line2)
        P = intersection_line_line_2d(
            (Vec2(*a_start), Vec2(*a_end)),
            (Vec2(*b_start), Vec2(*b_end)),
            virtual=True,
        )
        if P is None:
            raise RuntimeError(f"entity_{op}: lines are parallel; no intersection.")
        Pv = (float(P.x), float(P.y))
        # Pick the far endpoint of each line (relative to P).
        A_far = a_start if self._dist2(a_start, Pv) > self._dist2(a_end, Pv) else a_end
        B_far = b_start if self._dist2(b_start, Pv) > self._dist2(b_end, Pv) else b_end
        # Unit vectors from P toward the far endpoints.
        ax, ay = A_far[0] - Pv[0], A_far[1] - Pv[1]
        bx, by = B_far[0] - Pv[0], B_far[1] - Pv[1]
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        if la < 1e-12 or lb < 1e-12:
            raise RuntimeError(f"entity_{op}: degenerate line (zero length).")
        d1 = (ax / la, ay / la)
        d2 = (bx / lb, by / lb)
        cos_t = max(-1.0, min(1.0, d1[0] * d2[0] + d1[1] * d2[1]))
        theta = math.acos(cos_t)
        if theta < 1e-9 or abs(math.pi - theta) < 1e-9:
            raise RuntimeError(
                f"entity_{op}: lines are collinear or anti-parallel; cannot resolve corner."
            )
        return line1, line2, Pv, d1, d2, A_far, B_far, theta

    async def entity_fillet(self, handle1, handle2, radius, trim=True) -> EntityInfo:
        def _sync():
            line1, line2, P, d1, d2, A_far, B_far, theta = self._fillet_chamfer_setup(
                handle1, handle2, "fillet"
            )
            r = float(radius)
            if r < 0:
                raise RuntimeError("entity_fillet: radius must be >= 0.")
            half = theta / 2.0
            s = r / math.tan(half) if r > 0 else 0.0  # tangent distance from P
            T1 = (P[0] + s * d1[0], P[1] + s * d1[1])
            T2 = (P[0] + s * d2[0], P[1] + s * d2[1])
            msp = self._msp()
            arc_handle = None
            if r > 0:
                # Bisector unit vector (interior of the corner).
                bx, by = d1[0] + d2[0], d1[1] + d2[1]
                bl = math.hypot(bx, by)
                if bl < 1e-12:
                    raise RuntimeError("entity_fillet: bisector is degenerate.")
                bux, buy = bx / bl, by / bl
                C = (P[0] + (r / math.sin(half)) * bux, P[1] + (r / math.sin(half)) * buy)
                # Arc start/end angles in degrees, CCW. Pick the orientation
                # that yields the short arc (interior fillet) using the cross
                # product sign of (T1-C) × (T2-C).
                a1 = math.degrees(math.atan2(T1[1] - C[1], T1[0] - C[0]))
                a2 = math.degrees(math.atan2(T2[1] - C[1], T2[0] - C[0]))
                cross = (T1[0] - C[0]) * (T2[1] - C[1]) - (T1[1] - C[1]) * (T2[0] - C[0])
                if cross >= 0:
                    start_a, end_a = a1, a2
                else:
                    start_a, end_a = a2, a1
                arc = msp.add_arc(
                    center=(C[0], C[1]),
                    radius=r,
                    start_angle=start_a,
                    end_angle=end_a,
                    dxfattribs={"layer": line1.dxf.layer},
                )
                arc_handle = arc.dxf.handle
            if trim:
                # Shorten line1 so its near-end becomes T1; same for line2 → T2.
                a_start, a_end = self._line_endpoints(line1)
                if self._dist2(a_start, P) <= self._dist2(a_end, P):
                    line1.dxf.start = (T1[0], T1[1], 0.0)
                else:
                    line1.dxf.end = (T1[0], T1[1], 0.0)
                b_start, b_end = self._line_endpoints(line2)
                if self._dist2(b_start, P) <= self._dist2(b_end, P):
                    line2.dxf.start = (T2[0], T2[1], 0.0)
                else:
                    line2.dxf.end = (T2[0], T2[1], 0.0)
            self._mark_dirty()
            if arc_handle is not None:
                return _entity_info_dxf(self._get_entity(arc_handle))
            # Zero-radius fillet: no arc; return line1 (the modified first line).
            return _entity_info_dxf(line1)

        return await self._async(_sync)

    async def entity_chamfer(
        self,
        handle1,
        handle2,
        dist1,
        dist2=None,
        trim=True,
    ) -> EntityInfo:
        def _sync():
            line1, line2, P, d1, d2, A_far, B_far, theta = self._fillet_chamfer_setup(
                handle1, handle2, "chamfer"
            )
            d1v = float(dist1)
            d2v = float(dist1 if dist2 is None else dist2)
            if d1v <= 0 or d2v <= 0:
                raise RuntimeError("entity_chamfer: distances must be > 0.")
            T1 = (P[0] + d1v * d1[0], P[1] + d1v * d1[1])
            T2 = (P[0] + d2v * d2[0], P[1] + d2v * d2[1])
            msp = self._msp()
            chamfer_line = msp.add_line(
                (T1[0], T1[1]),
                (T2[0], T2[1]),
                dxfattribs={"layer": line1.dxf.layer},
            )
            if trim:
                a_start, a_end = self._line_endpoints(line1)
                if self._dist2(a_start, P) <= self._dist2(a_end, P):
                    line1.dxf.start = (T1[0], T1[1], 0.0)
                else:
                    line1.dxf.end = (T1[0], T1[1], 0.0)
                b_start, b_end = self._line_endpoints(line2)
                if self._dist2(b_start, P) <= self._dist2(b_end, P):
                    line2.dxf.start = (T2[0], T2[1], 0.0)
                else:
                    line2.dxf.end = (T2[0], T2[1], 0.0)
            self._mark_dirty()
            return _entity_info_dxf(chamfer_line)

        return await self._async(_sync)

    # ── premium meta-tools live on AutoCADBackend (base.py) and are shared by
    # both backends. Only the XLINE primitive is backend-specific. ────────────
    async def _create_xline(self, x, y, dx, dy, layer) -> EntityInfo:
        def _sync():
            self._require_doc()
            msp = self._msp()
            xline = msp.add_xline(
                (float(x), float(y), 0.0),
                (float(dx), float(dy), 0.0),
                dxfattribs={"layer": layer},
            )
            self._mark_dirty()
            return _entity_info_dxf(xline)

        return await self._async(_sync)
