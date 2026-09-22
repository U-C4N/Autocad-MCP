"""AutoCAD system-variable catalogue: what each one is, where it lives, who honours it.

Authored data, pinned by ``tests/test_sysvar_catalog.py``. Every entry says
its type, range or enum, AutoCAD's default, a one-line meaning, where AutoCAD
saves it (``drawing`` / ``registry`` / ``not_saved``), whether each engine can
read *and write* it (``engines``), and the ``drawing_settings`` key that wraps
it when one exists (``friendly_key``).

``engines["ezdxf"]`` is a measured claim, not a hope: it is ``True`` only when
the headless backend has a real home for the value in the DXF file - a header
variable ezdxf will export, the active VPORT (grid/snap), the variable
dictionary (CANNOSCALE) or the model-space layout (limits). Registry-saved
and never-saved variables are ``False`` because a file has nowhere to keep
them, and ``EzdxfBackend.system_set_variable`` refuses them with
``capability: registry_sysvar`` (OSMODE is one of them: ``$OSMODE`` is an R12
header variable that ezdxf never exports for R2000+, so a headless write
looked accepted and vanished at save until it joined the refused set). A
handful of drawing-saved variables are also ``False`` because ezdxf 1.4 has no
header slot for them (CTAB, VIEWCTR, VIEWSIZE, ISOLINES, FACETRES); that
refusal is a plain ``ValueError``.

Angles are **radians** at the ``system_get_variable`` / ``system_set_variable``
boundary on both engines - the unit ActiveX ``GetVariable`` / ``SetVariable``
and AutoLISP ``getvar`` use (measured on AutoCAD 2026: ``SETVAR ANGBASE 90``
reads back 1.5707963267948966, and so does HPANG after ``SETVAR HPANG 45``).
The DXF header stores ``$ANGBASE`` in degrees (group code 50; the same
document saved by AutoCAD carries ``$ANGBASE = 90.0``), so the headless engine
translates that one variable at its boundary and the two engines report the
same number. ``drawing_settings`` speaks degrees for its friendly keys.

Sources: the AutoCAD 2026 System Variables reference (type / saved-in /
initial value columns), the DXF Reference HEADER and VPORT sections, and
``ezdxf.sections.headervars.HEADER_VAR_MAP`` for what the headless engine
exports.
"""

from __future__ import annotations

import difflib
import math
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "SAVED_IN",
    "SYSVAR_CATALOG",
    "SYSVAR_TYPES",
    "SysVar",
    "check_sysvar_value",
    "describe_sysvar",
    "search_sysvars",
]

SYSVAR_TYPES = ("int", "float", "str", "bool", "point", "enum")
SAVED_IN = ("drawing", "registry", "not_saved")


@dataclass(frozen=True)
class SysVar:
    name: str
    type: str
    range: tuple[float, float] | None
    enum: dict[int, str] | None
    default: Any
    meaning: str
    saved_in: str
    engines: dict[str, bool] = field(default_factory=lambda: {"ezdxf": True, "com": True})
    friendly_key: str | None = None
    read_only: bool = False


_BOTH = {"ezdxf": True, "com": True}
_COM_ONLY = {"ezdxf": False, "com": True}


def _v(
    name: str,
    type_: str,
    meaning: str,
    *,
    range_: tuple[float, float] | None = None,
    enum: dict[int, str] | None = None,
    default: Any = None,
    saved_in: str = "drawing",
    engines: dict[str, bool] | None = None,
    friendly_key: str | None = None,
    read_only: bool = False,
) -> SysVar:
    if engines is None:
        engines = _BOTH if saved_in == "drawing" else _COM_ONLY
    return SysVar(
        name=name,
        type=type_,
        range=range_,
        enum=enum,
        default=default,
        meaning=meaning,
        saved_in=saved_in,
        engines=dict(engines),
        friendly_key=friendly_key,
        read_only=read_only,
    )


_POSITIVE = (1e-9, 1e9)
_NON_NEGATIVE = (0.0, 1e9)
_ACI = (0, 256)
_LINEWEIGHT = (-3, 211)

_ENTRIES: tuple[SysVar, ...] = (
    # ── dimension variables (the dimstyle whitelist) ────────────────────────
    _v(
        "DIMTXT",
        "float",
        "dimension text height",
        range_=_POSITIVE,
        default=2.5,
        friendly_key="dim_text_height",
    ),
    _v(
        "DIMASZ",
        "float",
        "arrowhead size",
        range_=_POSITIVE,
        default=2.5,
        friendly_key="dim_arrow_size",
    ),
    _v(
        "DIMEXE",
        "float",
        "extension line extension beyond the dimension line",
        range_=_POSITIVE,
        default=1.25,
    ),
    _v(
        "DIMEXO",
        "float",
        "extension line offset from the origin points",
        range_=_NON_NEGATIVE,
        default=0.625,
    ),
    _v(
        "DIMGAP",
        "float",
        "gap between dimension line and text; a negative value draws a box around the text",
        default=0.625,
    ),
    _v(
        "DIMTAD",
        "enum",
        "vertical text placement relative to the dimension line",
        enum={0: "centered", 1: "above", 2: "outside", 3: "JIS", 4: "below"},
        default=1,
    ),
    _v(
        "DIMTIH",
        "bool",
        "text inside the extension lines is horizontal (1) or aligned with the line (0)",
        default=0,
    ),
    _v(
        "DIMTOH",
        "bool",
        "text outside the extension lines is horizontal (1) or aligned with the line (0)",
        default=0,
    ),
    _v(
        "DIMDEC",
        "int",
        "decimal places of the primary dimension value",
        range_=(0, 8),
        default=2,
        friendly_key="dim_decimals",
    ),
    _v(
        "DIMDSEP",
        "str",
        "decimal separator character for decimal dimensions (the headless engine stores its character code: 44 is ',')",
        default=",",
        friendly_key="decimal_separator",
    ),
    _v(
        "DIMLUNIT",
        "enum",
        "linear unit format for dimensions",
        enum={
            1: "scientific",
            2: "decimal",
            3: "engineering",
            4: "architectural",
            5: "fractional",
            6: "windows desktop",
        },
        default=2,
    ),
    _v(
        "DIMZIN",
        "int",
        "zero suppression bitmask: 1 feet, 2 inches, 4 leading, 8 trailing",
        range_=(0, 15),
        default=8,
        friendly_key="zero_suppression",
    ),
    _v("DIMBLK", "str", "arrowhead block name; empty means closed filled", default=""),
    _v("DIMTXSTY", "str", "text style used by dimension text", default="Standard"),
    _v(
        "DIMLWD",
        "int",
        "dimension line lineweight code (-3 default, -2 ByBlock, -1 ByLayer, else hundredths of mm)",
        range_=_LINEWEIGHT,
        default=-2,
    ),
    _v(
        "DIMLWE",
        "int",
        "extension line lineweight code (-3 default, -2 ByBlock, -1 ByLayer, else hundredths of mm)",
        range_=_LINEWEIGHT,
        default=-2,
    ),
    _v(
        "DIMSCALE",
        "float",
        "overall scale factor applied to dimension sizes; 0 scales to the viewport",
        range_=(0.0, 1e6),
        default=1.0,
        friendly_key="dimscale",
    ),
    _v("DIMTOL", "bool", "append tolerances to dimension text", default=0),
    _v("DIMTP", "float", "upper tolerance limit", default=0.0),
    _v("DIMTM", "float", "lower tolerance limit", default=0.0),
    _v(
        "DIMTOLJ",
        "enum",
        "vertical justification of tolerance text",
        enum={0: "bottom", 1: "middle", 2: "top"},
        default=1,
    ),
    _v(
        "DIMTFAC",
        "float",
        "tolerance text height as a factor of DIMTXT",
        range_=_POSITIVE,
        default=1.0,
    ),
    _v(
        "DIMLFAC",
        "float",
        "linear measurement scale factor; negative applies in paper space only",
        default=1.0,
    ),
    _v(
        "DIMRND",
        "float",
        "rounding increment for linear dimensions; 0 rounds nothing",
        range_=_NON_NEGATIVE,
        default=0.0,
    ),
    _v(
        "DIMATFIT",
        "enum",
        "what moves outside when text and arrows do not fit",
        enum={0: "text and arrows", 1: "arrows first", 2: "text first", 3: "best fit"},
        default=3,
    ),
    _v(
        "DIMTMOVE",
        "enum",
        "how the dimension line behaves when text is moved",
        enum={0: "move line with text", 1: "add a leader", 2: "text moves freely"},
        default=0,
    ),
    _v(
        "DIMCLRD",
        "int",
        "dimension line colour (ACI: 0 ByBlock, 256 ByLayer)",
        range_=_ACI,
        default=0,
    ),
    _v(
        "DIMCLRE",
        "int",
        "extension line colour (ACI: 0 ByBlock, 256 ByLayer)",
        range_=_ACI,
        default=0,
    ),
    _v(
        "DIMCLRT",
        "int",
        "dimension text colour (ACI: 0 ByBlock, 256 ByLayer)",
        range_=_ACI,
        default=0,
    ),
    _v("DIMSAH", "bool", "use DIMBLK1 / DIMBLK2 as separate arrowheads", default=0),
    _v("DIMBLK1", "str", "first arrowhead block when DIMSAH is on", default=""),
    _v("DIMBLK2", "str", "second arrowhead block when DIMSAH is on", default=""),
    _v(
        "DIMCEN",
        "float",
        "centre mark size for radial dimensions; 0 none, negative draws centre lines",
        default=2.5,
    ),
    # ── scales, units, precision ─────────────────────────────────────────────
    _v(
        "LTSCALE",
        "float",
        "global linetype scale factor",
        range_=_POSITIVE,
        default=1.0,
        friendly_key="ltscale",
    ),
    _v(
        "PSLTSCALE",
        "bool",
        "scale linetypes by the viewport scale in paper space",
        default=1,
        friendly_key="psltscale",
    ),
    _v(
        "INSUNITS",
        "enum",
        "drawing units for inserted content and scaling",
        enum={
            0: "unitless",
            1: "inches",
            2: "feet",
            3: "miles",
            4: "millimeters",
            5: "centimeters",
            6: "meters",
            7: "kilometers",
            8: "microinches",
            9: "mils",
            10: "yards",
            11: "angstroms",
            12: "nanometers",
            13: "microns",
            14: "decimeters",
            15: "decameters",
            16: "hectometers",
            17: "gigameters",
            18: "astronomical units",
            19: "light years",
            20: "parsecs",
        },
        default=4,
        friendly_key="units",
    ),
    _v(
        "LUNITS",
        "enum",
        "linear unit display format",
        enum={1: "scientific", 2: "decimal", 3: "engineering", 4: "architectural", 5: "fractional"},
        default=2,
        friendly_key="linear_units",
    ),
    _v(
        "LUPREC",
        "int",
        "linear unit display precision (decimal places)",
        range_=(0, 8),
        default=4,
        friendly_key="linear_precision",
    ),
    _v(
        "AUNITS",
        "enum",
        "angular unit display format",
        enum={
            0: "decimal degrees",
            1: "degrees/minutes/seconds",
            2: "grads",
            3: "radians",
            4: "surveyor",
        },
        default=0,
        friendly_key="angular_units",
    ),
    _v(
        "AUPREC",
        "int",
        "angular unit display precision (decimal places)",
        range_=(0, 8),
        default=0,
        friendly_key="angular_precision",
    ),
    _v(
        "CANNOSCALE",
        "str",
        "current annotation scale name, e.g. '1:50' (a DICTIONARYVAR in AcDbVariableDictionary, not a header variable)",
        default="1:1",
        friendly_key="annotation_scale",
    ),
    _v(
        "CANNOSCALEVALUE",
        "float",
        "current annotation scale as a number (paper / drawing), derived from CANNOSCALE",
        default=1.0,
        read_only=True,
    ),
    # ── limits, grid, snap, tracking ─────────────────────────────────────────
    _v(
        "LIMMIN",
        "point",
        "lower-left corner of the drawing limits (LIMITS)",
        default=(0.0, 0.0),
        friendly_key="limits",
    ),
    _v(
        "LIMMAX",
        "point",
        "upper-right corner of the drawing limits (LIMITS)",
        default=(420.0, 297.0),
        friendly_key="limits",
    ),
    _v(
        "GRIDMODE",
        "bool",
        "grid display on/off (stored on the active VPORT, not in the header)",
        default=0,
        friendly_key="grid",
    ),
    _v(
        "GRIDUNIT",
        "point",
        "grid spacing [x, y] (stored on the active VPORT)",
        default=(10.0, 10.0),
        friendly_key="grid_spacing",
    ),
    _v(
        "SNAPMODE",
        "bool",
        "snap on/off (stored on the active VPORT)",
        default=0,
        friendly_key="snap",
    ),
    _v(
        "SNAPUNIT",
        "point",
        "snap spacing [x, y] (stored on the active VPORT)",
        default=(10.0, 10.0),
        friendly_key="snap_spacing",
    ),
    _v(
        "ORTHOMODE",
        "bool",
        "ortho mode: cursor movement constrained to horizontal/vertical",
        default=0,
        friendly_key="ortho",
    ),
    _v(
        "POLARMODE",
        "int",
        "polar tracking bitmask: 1 relative angles, 2 polar in osnap tracking, 4 additional angles, 8 tooltips (polar on/off itself is AUTOSNAP bit 8)",
        range_=(0, 15),
        default=0,
        saved_in="registry",
    ),
    _v(
        "POLARANG",
        "float",
        "polar angle increment in radians (90 degrees = 1.5708)",
        range_=(1e-9, 2 * math.pi),
        default=math.pi / 2,
        saved_in="registry",
        friendly_key="polar_angle",
    ),
    _v(
        "AUTOSNAP",
        "int",
        "AutoSnap bitmask: 1 marker, 2 magnet, 4 tooltip, 8 polar tracking on, 16 object snap tracking, 32 tracking tooltips, 64 dynamic input aperture",
        range_=(0, 127),
        default=63,
        saved_in="registry",
        friendly_key="polar",
    ),
    _v(
        "OSMODE",
        "int",
        "running object snap bitmask (1 end, 2 mid, 4 cen, 8 nod, 16 qua, 32 int, 64 ins, 128 per, 256 tan, 512 nea, 1024 quick, 2048 app, 4096 ext, 8192 par, 16384 off); registry-saved, and a DXF R2000+ file carries no $OSMODE, so the headless engine refuses it",
        range_=(0, 32767),
        default=4133,
        saved_in="registry",
        friendly_key="osmode",
    ),
    # ── current properties ───────────────────────────────────────────────────
    _v(
        "TEXTSIZE",
        "float",
        "default height for new TEXT entities (not dimension text)",
        range_=_POSITIVE,
        default=2.5,
        friendly_key="text_size",
    ),
    _v("TEXTSTYLE", "str", "current text style name", default="Standard", friendly_key="textstyle"),
    _v(
        "DIMSTYLE",
        "str",
        "current dimension style name (read-only: change it with dimstyle_set_current or drawing_settings {'dimstyle': ...})",
        default="ISO-25",
        friendly_key="dimstyle",
        read_only=True,
    ),
    _v("CLAYER", "str", "current layer name", default="0"),
    _v(
        "CELTYPE",
        "str",
        "current entity linetype (ByLayer, ByBlock or a loaded linetype)",
        default="ByLayer",
    ),
    _v(
        "CECOLOR",
        "str",
        "current entity colour ('BYLAYER', 'BYBLOCK' or an ACI number; the headless engine stores the ACI code, 256 is ByLayer)",
        default="BYLAYER",
    ),
    _v(
        "CELWEIGHT",
        "int",
        "current entity lineweight code (-1 ByLayer, -2 ByBlock, -3 default, else hundredths of mm)",
        range_=_LINEWEIGHT,
        default=-1,
    ),
    _v(
        "PDMODE",
        "int",
        "point display mode (0-4, or those plus 32 / 64 / 96)",
        range_=(0, 100),
        default=0,
        friendly_key="point_mode",
    ),
    _v(
        "PDSIZE",
        "float",
        "point display size; 0 is 5 percent of the view, negative is a percentage of the viewport",
        default=0.0,
        friendly_key="point_size",
    ),
    _v(
        "FILLETRAD",
        "float",
        "current fillet radius",
        range_=_NON_NEGATIVE,
        default=0.0,
        friendly_key="fillet_radius",
    ),
    _v("MIRRTEXT", "bool", "mirror text with MIRROR (1) or keep it readable (0)", default=0),
    _v("ATTREQ", "bool", "prompt for attribute values on INSERT", default=1, saved_in="registry"),
    _v(
        "ATTDIA",
        "bool",
        "use a dialog for attribute values on INSERT",
        default=0,
        saved_in="registry",
    ),
    _v("PLINEWID", "float", "default width for new polylines", range_=_NON_NEGATIVE, default=0.0),
    _v(
        "ANGBASE",
        "float",
        "direction of angle 0 relative to the current UCS, in radians on both engines (90 degrees = 1.5708; the DXF header stores degrees and the headless engine translates)",
        default=0.0,
    ),
    _v("ANGDIR", "bool", "positive angle direction: 0 counter-clockwise, 1 clockwise", default=0),
    # ── layouts, views, UCS ──────────────────────────────────────────────────
    _v("TILEMODE", "bool", "1 model tab active, 0 a layout tab active", default=1),
    _v(
        "CTAB",
        "str",
        "name of the current tab (Model or a layout); headlessly use layout_list / layout_set_current",
        default="Model",
        engines=_COM_ONLY,
    ),
    _v(
        "UCSNAME",
        "str",
        "name of the current UCS; empty when unnamed or world",
        default="",
        read_only=True,
    ),
    _v(
        "VIEWCTR",
        "point",
        "centre of the current view (read-only; VPORT centre in a file)",
        default=(0.0, 0.0),
        engines=_COM_ONLY,
        read_only=True,
    ),
    _v(
        "VIEWSIZE",
        "float",
        "height of the current view in drawing units (read-only)",
        default=100.0,
        engines=_COM_ONLY,
        read_only=True,
    ),
    _v("REGENMODE", "bool", "automatic regeneration on", default=1),
    _v("LWDISPLAY", "bool", "display lineweights on screen", default=0),
    # ── file, session ────────────────────────────────────────────────────────
    _v(
        "DWGNAME",
        "str",
        "name of the current drawing file (read-only)",
        default="Drawing1.dwg",
        saved_in="not_saved",
        read_only=True,
    ),
    _v(
        "DWGPREFIX",
        "str",
        "folder of the current drawing file (read-only)",
        default="",
        saved_in="not_saved",
        read_only=True,
    ),
    _v(
        "SAVETIME",
        "int",
        "automatic save interval in minutes; 0 disables",
        range_=(0, 600),
        default=10,
        saved_in="registry",
    ),
    _v(
        "FILEDIA",
        "bool",
        "show file dialogs (1) or take file names from the command line (0)",
        default=1,
        saved_in="registry",
    ),
    _v(
        "CMDECHO",
        "bool",
        "echo prompts and input during AutoLISP command execution",
        default=1,
        saved_in="not_saved",
    ),
    _v("HIGHLIGHT", "bool", "highlight selected objects", default=1, saved_in="not_saved"),
    _v(
        "XREFCTL",
        "bool",
        "write external reference log (.xlg) files",
        default=0,
        saved_in="registry",
    ),
    _v(
        "PROXYSHOW",
        "enum",
        "how proxy objects are displayed",
        enum={0: "not shown", 1: "shown", 2: "bounding box"},
        default=1,
        saved_in="registry",
    ),
    # ── hatch ────────────────────────────────────────────────────────────────
    _v(
        "HPNAME",
        "str",
        "default hatch pattern name for HATCH",
        default="ANSI31",
        saved_in="not_saved",
    ),
    _v(
        "HPSCALE",
        "float",
        "default hatch pattern scale",
        range_=_POSITIVE,
        default=1.0,
        saved_in="not_saved",
    ),
    _v(
        "HPANG",
        "float",
        "default hatch pattern angle in radians, as ActiveX and AutoLISP hold it (45 degrees = 0.7854)",
        default=0.0,
        saved_in="not_saved",
    ),
    # ── 3D display ───────────────────────────────────────────────────────────
    _v(
        "ISOLINES",
        "int",
        "contour lines per surface on 3D solids",
        range_=(0, 2047),
        default=4,
        engines=_COM_ONLY,
    ),
    _v(
        "FACETRES",
        "float",
        "smoothness of shaded and rendered curved solids",
        range_=(0.01, 10.0),
        default=0.5,
        engines=_COM_ONLY,
    ),
    _v("DISPSILH", "bool", "display silhouette edges of 3D solids in wireframe", default=0),
)

SYSVAR_CATALOG: dict[str, SysVar] = {entry.name: entry for entry in _ENTRIES}


def _as_dict(var: SysVar) -> dict:
    out = asdict(var)
    out["known"] = True
    out["range"] = list(var.range) if var.range is not None else None
    out["default"] = list(var.default) if isinstance(var.default, tuple) else var.default
    return out


def describe_sysvar(name: str) -> dict:
    """The catalogue row for ``name`` (case-insensitive), or ``known: False`` plus the nearest names."""
    key = str(name).strip().upper().lstrip("$")
    var = SYSVAR_CATALOG.get(key)
    if var is not None:
        return _as_dict(var)
    names = sorted(SYSVAR_CATALOG)
    nearest = difflib.get_close_matches(key, names, n=5, cutoff=0.6)
    for candidate in names:
        if len(nearest) >= 5:
            break
        if key and candidate.startswith(key[:3]) and candidate not in nearest:
            nearest.append(candidate)
    return {"known": False, "name": key, "nearest": nearest}


def search_sysvars(text: str) -> list[dict]:
    """Catalogue rows whose name, meaning or friendly key contains every word of ``text``."""
    words = [word for word in str(text).lower().split() if word]
    if not words:
        return []
    hits = []
    for var in SYSVAR_CATALOG.values():
        haystack = " ".join((var.name, var.meaning, var.friendly_key or "")).lower()
        if all(word in haystack for word in words):
            hits.append(_as_dict(var))
    return sorted(hits, key=lambda row: row["name"])


_TRUTHY = {"0", "1", "on", "off", "true", "false"}


def check_sysvar_value(name: str, value: Any) -> str | None:
    """A refusal message when ``value`` cannot be ``name``'s value; ``None`` when fine or unknown.

    Soft by design: a variable the catalogue has not heard of is passed
    through untouched, so ``system_set_variable`` keeps working for the
    hundreds of variables nobody has authored a row for.
    """
    var = SYSVAR_CATALOG.get(str(name).strip().upper().lstrip("$"))
    if var is None:
        return None
    if var.read_only:
        return f"{var.name} is read-only: {var.meaning}"
    if var.type in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return f"{var.name}: expected a number, got {value!r}"
        try:
            number = float(value)
        except ValueError:
            return f"{var.name}: expected a number, got {value!r}"
        if not math.isfinite(number):
            return f"{var.name}: must be a finite number"
        if var.type == "int" and number != int(number):
            return f"{var.name}: expected an integer, got {value!r}"
        if var.range is not None:
            low, high = var.range
            if not low <= number <= high:
                return f"{var.name}: {number:g} is out of range {low:g}..{high:g} - {var.meaning}"
        return None
    if var.type == "bool":
        if value in (0, 1) or str(value).strip().lower() in _TRUTHY:
            return None
        return f"{var.name}: expected 0 or 1 (on/off), got {value!r}"
    if var.type == "enum":
        try:
            code = int(value)
        except (TypeError, ValueError):
            return f"{var.name}: expected one of {sorted(var.enum)}, got {value!r}"
        if code not in var.enum:
            choices = ", ".join(f"{k}={v}" for k, v in var.enum.items())
            return f"{var.name}: {code} is not a valid code ({choices})"
        return None
    if var.type == "point":
        try:
            float(value[0]), float(value[1])
        except (TypeError, IndexError, ValueError):
            return f"{var.name}: expected an [x, y] point, got {value!r}"
        return None
    return None
