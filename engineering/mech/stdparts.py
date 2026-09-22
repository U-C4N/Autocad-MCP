"""The standard-parts catalogue: tables in, real blocks with attributes out.

Two halves live here.

**Standard parts.** :func:`catalogue` searches the authored tables of
``standards/fasteners.py`` and ``standards/bearings.py``; :func:`block_spec`
turns one designation and one view into the typed primitives and ATTDEFs
``block_define`` takes; :func:`insert_std_part` defines the block once, inserts
it and writes the ``std_part`` ACADMCP_MECH payload, so a parts list is a read
of the drawing rather than a naming convention. This is the Track A (P&ID)
pattern applied to fasteners and bearings.

**Standard features.** :func:`draw_std_feature` adds a thread, a DIN 509
undercut, a retaining-ring groove, a DIN 332 centre hole or an ISO 3601-2
O-ring groove to geometry that already exists - a foreign drawing being
finished. The thread is the ISO 6410 representation and its pitch comes from
ISO 261; every other feature takes its dimensions from the shared standards
registry and is refused by name when the table is absent, when the size is
outside its coverage, or when the row lacks a dimension the feature needs.
Nothing here reconstructs a standard profile it was not handed.

Primitives are the ``block_define`` spec dicts of ``backends/block_specs.py``
(``{"type": "line", "x1": ..., "layer": ...}``) with an explicit ``layer``, so
one vocabulary serves both the block definitions and the feature drawing, and
the pure half is testable by comparing emitted dicts.
"""

from __future__ import annotations

import difflib
import math
import re
from typing import TYPE_CHECKING

from engineering.mech.standards import lookup as standards_lookup
from engineering.mech.standards.bearings import (
    BALL_SYMBOL_FRACTION,
    CROSS_FRACTION,
    SOURCE_ISO_15,
    bearing,
    list_bearings,
)
from engineering.mech.standards.fasteners import (
    SOURCE_ISO_4014,
    SOURCE_ISO_4017,
    SOURCE_ISO_4032,
    SOURCE_ISO_4762,
    SOURCE_ISO_7089,
    across_corners,
    hex_head,
    hex_nut,
    sizes,
    socket_head,
    thread_length,
    thread_pitch,
    washer,
)
from engineering.mech.xdata import APP_ID, PAYLOAD_VERSION, to_values

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

LAYER_VISIBLE = "GEOMETRY"
LAYER_CENTER = "CENTER"
LAYER_HIDDEN = "HIDDEN"
TEXT_HEIGHT = 2.5
AXIS_OVERRUN = 3.0
DEFAULT_VIEW = "side"

#: ISO 6410 draws the minor diameter of a thread at 0.8 x the major diameter.
THREAD_MINOR_RATIO = 0.8

SOURCE_ISO_6410 = (
    "ISO 6410-1:1993 - technical drawings, screw threads and threaded parts, general conventions"
)

FAMILY_STANDARDS: dict[str, str] = {
    "ISO 4014": "hex_bolt",
    "ISO 4017": "hex_screw",
    "ISO 4032": "hex_nut",
    "ISO 7089": "washer",
    "ISO 4762": "socket_head_screw",
    "ISO 15": "deep_groove_bearing",
}
STANDARD_SOURCE: dict[str, str] = {
    "ISO 4014": SOURCE_ISO_4014,
    "ISO 4017": SOURCE_ISO_4017,
    "ISO 4032": SOURCE_ISO_4032,
    "ISO 7089": SOURCE_ISO_7089,
    "ISO 4762": SOURCE_ISO_4762,
    "ISO 15": SOURCE_ISO_15,
}
FAMILY_VIEWS: dict[str, tuple[str, ...]] = {
    "hex_bolt": ("side", "top"),
    "hex_screw": ("side", "top"),
    "hex_nut": ("side", "top"),
    "washer": ("side", "top"),
    "socket_head_screw": ("side", "top"),
    "deep_groove_bearing": ("simplified", "detailed"),
}
LENGTH_REQUIRED = frozenset({"hex_bolt", "hex_screw", "socket_head_screw"})

_THREAD_RE = re.compile(r"^(M\d{1,3})(?:\s*[X*]\s*(\d{1,4}(?:\.\d+)?))?$", re.IGNORECASE)
_BEARING_RE = re.compile(r"^\d{4}$")


# ── primitive helpers (block_define spec dicts, block-local mm) ─────────────


def _line(x1, y1, x2, y2, layer: str = LAYER_VISIBLE) -> dict:
    return {
        "type": "line",
        "x1": float(x1),
        "y1": float(y1),
        "x2": float(x2),
        "y2": float(y2),
        "layer": layer,
    }


def _circle(cx, cy, r, layer: str = LAYER_VISIBLE) -> dict:
    return {"type": "circle", "cx": float(cx), "cy": float(cy), "r": float(r), "layer": layer}


def _arc(cx, cy, r, start_deg, end_deg, layer: str = LAYER_VISIBLE) -> dict:
    return {
        "type": "arc",
        "cx": float(cx),
        "cy": float(cy),
        "r": float(r),
        "start_deg": float(start_deg),
        "end_deg": float(end_deg),
        "layer": layer,
    }


def _poly(points, closed: bool = True, layer: str = LAYER_VISIBLE) -> dict:
    return {
        "type": "polyline",
        "points": [[float(x), float(y)] for x, y in points],
        "closed": bool(closed),
        "layer": layer,
    }


def _hex_points(radius: float, start_deg: float = 0.0):
    return [
        (
            radius * math.cos(math.radians(start_deg + 60.0 * i)),
            radius * math.sin(math.radians(start_deg + 60.0 * i)),
        )
        for i in range(6)
    ]


def _centre_mark(cx: float, cy: float, extent: float) -> list[dict]:
    return [
        _line(cx - extent, cy, cx + extent, cy, LAYER_CENTER),
        _line(cx, cy - extent, cx, cy + extent, LAYER_CENTER),
    ]


# ── designations and the catalogue ──────────────────────────────────────────


def _normalise_standard(text: str) -> str:
    return re.sub(r"^(ISO)\s*", r"\1 ", " ".join(str(text).split()).upper())


def parse_designation(designation: str) -> dict:
    """Split a catalogue designation into standard, family, size, thread and length.

    Accepts ``'ISO 4014 - M12x60'``, ``'iso4014-m12x60'`` and a bare four-digit
    bearing number (``'6205'`` becomes ``'ISO 15 - 6205'``). Returns
    ``{"designation", "standard", "family", "size", "thread", "length"}`` with
    a canonical designation. Every refusal names what was wrong.
    """
    text = " ".join(str(designation).split())
    if not text:
        raise ValueError(
            "std part designation must be a non-empty string, e.g. 'ISO 4014 - M12x60'"
        )
    if _BEARING_RE.match(text):
        text = f"ISO 15 - {text}"
    head, separator, tail = text.partition("-")
    if not separator:
        raise ValueError(
            f"{designation!r} is not a catalogue designation: expected "
            "'<standard> - <size>', e.g. 'ISO 4014 - M12x60' or 'ISO 15 - 6205'."
        )
    standard = _normalise_standard(head)
    size = " ".join(tail.split())
    if standard not in FAMILY_STANDARDS:
        raise ValueError(
            f"{designation!r}: {standard!r} is not a catalogue standard. "
            f"The catalogue holds {', '.join(FAMILY_STANDARDS)}."
        )
    family = FAMILY_STANDARDS[standard]
    if family == "deep_groove_bearing":
        if not _BEARING_RE.match(size):
            raise ValueError(
                f"{designation!r}: an ISO 15 size is a four-digit bearing designation "
                f"such as '6205', got {size!r}."
            )
        return {
            "designation": f"{standard} - {size}",
            "standard": standard,
            "family": family,
            "size": size,
            "thread": None,
            "length": None,
        }
    match = _THREAD_RE.match(size)
    if not match:
        shape = "'M12' or 'M12x60'" if family in LENGTH_REQUIRED else "'M12'"
        raise ValueError(
            f"{designation!r}: a {standard} size is a metric thread such as {shape}, got {size!r}."
        )
    thread = match.group(1).upper()
    length = float(match.group(2)) if match.group(2) else None
    if family in LENGTH_REQUIRED and length is None:
        raise ValueError(
            f"{designation!r}: {standard} needs a nominal length - write 'M12x60'. The "
            "per-size length range is not transcribed, so any finite positive length is "
            "accepted and the thread length follows the standard's own b rule."
        )
    if family not in LENGTH_REQUIRED and length is not None:
        raise ValueError(f"{designation!r}: {standard} takes no length, got {size!r}.")
    canonical = thread if length is None else f"{thread}x{length:g}"
    return {
        "designation": f"{standard} - {canonical}",
        "standard": standard,
        "family": family,
        "size": canonical,
        "thread": thread,
        "length": length,
    }


def catalogue(query: str | None = None, standard: str | None = None) -> tuple[dict, ...]:
    """Every transcribed standard part, optionally filtered.

    ``standard`` restricts to one catalogue standard (refused by name when it
    is not one). ``query`` is matched, case-insensitively, as a substring of
    the designation, standard, size and family, so ``'M12'``, ``'ISO 4014'``,
    ``'6205'`` and ``'washer'`` all work.
    """
    rows: list[dict] = []
    getters = {
        "hex_bolt": hex_head,
        "hex_screw": hex_head,
        "hex_nut": hex_nut,
        "washer": washer,
        "socket_head_screw": socket_head,
    }
    for name, family in FAMILY_STANDARDS.items():
        if family == "deep_groove_bearing":
            for row in list_bearings():
                rows.append(
                    {
                        "designation": f"{name} - {row['designation']}",
                        "standard": name,
                        "family": family,
                        "size": row["designation"],
                        "views": list(FAMILY_VIEWS[family]),
                        "length_required": False,
                        "dims": {key: row[key] for key in ("d", "D", "B")},
                        "source": row["source"],
                    }
                )
            continue
        getter = getters[family]
        for size in sizes(name):
            dims = getter(size)
            rows.append(
                {
                    "designation": f"{name} - {size}",
                    "standard": name,
                    "family": family,
                    "size": size,
                    "views": list(FAMILY_VIEWS[family]),
                    "length_required": family in LENGTH_REQUIRED,
                    "dims": {key: value for key, value in dims.items() if key != "size"},
                    "source": STANDARD_SOURCE[name],
                }
            )
    if standard is not None:
        want = _normalise_standard(standard)
        if want not in FAMILY_STANDARDS:
            raise ValueError(
                f"catalogue: {standard!r} is not a catalogue standard; "
                f"have {', '.join(FAMILY_STANDARDS)}."
            )
        rows = [row for row in rows if row["standard"] == want]
    if query:
        needle = " ".join(str(query).split()).lower()
        rows = [
            row
            for row in rows
            if needle
            in f"{row['designation']} {row['standard']} {row['size']} {row['family']}".lower()
        ]
    return tuple(rows)


def _numeric_key(family: str, size: str) -> float | None:
    if family == "deep_groove_bearing":
        return float(size) if size.isdigit() else None
    match = _THREAD_RE.match(size)
    if not match:
        return None
    try:
        return float(match.group(1)[1:])
    except ValueError:  # pragma: no cover - the regex already fixed the shape
        return None


def _suggest(parsed: dict) -> str:
    rows = list(catalogue(standard=parsed["standard"]))
    target = _numeric_key(parsed["family"], parsed["thread"] or parsed["size"])
    if target is not None:
        ranked = sorted(
            rows,
            key=lambda row: abs((_numeric_key(row["family"], row["size"]) or 0.0) - target),
        )
    else:
        names = [row["designation"] for row in rows]
        close = difflib.get_close_matches(parsed["designation"], names, n=3, cutoff=0.3)
        ranked = [row for row in rows if row["designation"] in close]
    return ", ".join(row["designation"] for row in ranked[:3])


def _resolve_dims(parsed: dict) -> dict:
    family = parsed["family"]
    try:
        if family in ("hex_bolt", "hex_screw"):
            dims = hex_head(parsed["thread"])
        elif family == "hex_nut":
            dims = hex_nut(parsed["thread"])
        elif family == "washer":
            dims = washer(parsed["thread"])
        elif family == "socket_head_screw":
            dims = socket_head(parsed["thread"])
        else:
            dims = bearing(parsed["size"])
    except ValueError as exc:
        raise ValueError(
            f"{parsed['designation']!r} is not in the standard-parts catalogue. {exc} "
            f"Nearest entries: {_suggest(parsed)}. Nothing is drawn approximately."
        ) from exc
    if family in ("hex_bolt", "hex_screw", "hex_nut"):
        dims["e"] = across_corners(dims["s"])
    if family in LENGTH_REQUIRED:
        if family == "socket_head_screw":
            dims["b_table"] = dims["b"]
        dims["l"] = parsed["length"]
        dims["b"] = thread_length(parsed["thread"], parsed["length"], parsed["standard"])
    return dims


def _resolve_view(family: str, view) -> str:
    valid = FAMILY_VIEWS[family]
    name = " ".join(str(view).split()).lower()
    if name in valid:
        return name
    if name == DEFAULT_VIEW:
        return valid[0]
    raise ValueError(f"{family}: view must be one of {', '.join(valid)}, got {view!r}.")


def _block_name(standard: str, size: str, view: str) -> str:
    raw = f"STD_{standard}_{size}_{view}".upper()
    return re.sub(r"[^A-Z0-9]+", "_", raw).strip("_")


def _attdefs(parsed: dict, bbox: tuple[float, float, float, float]) -> list[dict]:
    top = bbox[1] - 2.0 * TEXT_HEIGHT
    rows = (
        ("DESIG", "Designation", parsed["designation"], False),
        ("STD", "Standard", parsed["standard"], True),
        ("SIZE", "Size", parsed["size"], True),
        ("MAT", "Material or property class", "", True),
    )
    return [
        {
            "tag": tag,
            "prompt": prompt,
            "default": default,
            "x": 0.0,
            "y": top - index * TEXT_HEIGHT * 1.6,
            "height": TEXT_HEIGHT,
            "align": "center",
            "invisible": invisible,
        }
        for index, (tag, prompt, default, invisible) in enumerate(rows)
    ]


# ── the view builders: (entities, bbox) in block-local mm ───────────────────


def _hex_fastener_side(dims: dict, *, threaded_to_head: bool):
    d, s, k = dims["d"], dims["s"], dims["k"]
    length, b = dims["l"], dims["b"]
    r = across_corners(s) / 2.0
    minor = THREAD_MINOR_RATIO * d / 2.0
    x0 = length - b
    ents = [
        _poly([(-k, -r), (0.0, -r), (0.0, r), (-k, r)]),
        _line(-k, r / 2.0, 0.0, r / 2.0),
        _line(-k, -r / 2.0, 0.0, -r / 2.0),
        _line(0.0, d / 2.0, length, d / 2.0),
        _line(0.0, -d / 2.0, length, -d / 2.0),
        _line(length, -d / 2.0, length, d / 2.0),
        _line(x0, minor, length, minor),
        _line(x0, -minor, length, -minor),
    ]
    if not threaded_to_head and x0 > 1e-9:
        ents.append(_line(x0, -d / 2.0, x0, d / 2.0))
    ents.append(_line(-k - AXIS_OVERRUN, 0.0, length + AXIS_OVERRUN, 0.0, LAYER_CENTER))
    return ents, (-k - AXIS_OVERRUN, -r, length + AXIS_OVERRUN, r)


def _hex_fastener_top(dims: dict):
    d, s = dims["d"], dims["s"]
    r = across_corners(s) / 2.0
    ents = [
        _poly(_hex_points(r)),
        _circle(0.0, 0.0, d / 2.0),
        _arc(0.0, 0.0, THREAD_MINOR_RATIO * d / 2.0, 90.0, 360.0),
    ]
    ents += _centre_mark(0.0, 0.0, r + AXIS_OVERRUN)
    extent = r + AXIS_OVERRUN
    return ents, (-extent, -extent, extent, extent)


def _hex_nut_side(dims: dict):
    d, s, m = dims["d"], dims["s"], dims["m"]
    r = across_corners(s) / 2.0
    minor = THREAD_MINOR_RATIO * d / 2.0
    ents = [
        _poly([(0.0, -r), (m, -r), (m, r), (0.0, r)]),
        _line(0.0, r / 2.0, m, r / 2.0),
        _line(0.0, -r / 2.0, m, -r / 2.0),
        _line(0.0, d / 2.0, m, d / 2.0, LAYER_HIDDEN),
        _line(0.0, -d / 2.0, m, -d / 2.0, LAYER_HIDDEN),
        _line(0.0, minor, m, minor, LAYER_HIDDEN),
        _line(0.0, -minor, m, -minor, LAYER_HIDDEN),
        _line(-AXIS_OVERRUN, 0.0, m + AXIS_OVERRUN, 0.0, LAYER_CENTER),
    ]
    return ents, (-AXIS_OVERRUN, -r, m + AXIS_OVERRUN, r)


def _hex_nut_top(dims: dict):
    d, s = dims["d"], dims["s"]
    r = across_corners(s) / 2.0
    ents = [
        _poly(_hex_points(r)),
        _circle(0.0, 0.0, THREAD_MINOR_RATIO * d / 2.0),
        _arc(0.0, 0.0, d / 2.0, 90.0, 360.0),
    ]
    ents += _centre_mark(0.0, 0.0, r + AXIS_OVERRUN)
    extent = r + AXIS_OVERRUN
    return ents, (-extent, -extent, extent, extent)


def _washer_side(dims: dict):
    d1, d2, h = dims["d1"], dims["d2"], dims["h"]
    ents = [
        _poly([(0.0, d1 / 2.0), (h, d1 / 2.0), (h, d2 / 2.0), (0.0, d2 / 2.0)]),
        _poly([(0.0, -d1 / 2.0), (h, -d1 / 2.0), (h, -d2 / 2.0), (0.0, -d2 / 2.0)]),
        _line(-AXIS_OVERRUN, 0.0, h + AXIS_OVERRUN, 0.0, LAYER_CENTER),
    ]
    return ents, (-AXIS_OVERRUN, -d2 / 2.0, h + AXIS_OVERRUN, d2 / 2.0)


def _washer_top(dims: dict):
    d1, d2 = dims["d1"], dims["d2"]
    ents = [_circle(0.0, 0.0, d2 / 2.0), _circle(0.0, 0.0, d1 / 2.0)]
    ents += _centre_mark(0.0, 0.0, d2 / 2.0 + AXIS_OVERRUN)
    extent = d2 / 2.0 + AXIS_OVERRUN
    return ents, (-extent, -extent, extent, extent)


def _socket_head_side(dims: dict):
    d, dk, k = dims["d"], dims["dk"], dims["k"]
    length, b = dims["l"], dims["b"]
    minor = THREAD_MINOR_RATIO * d / 2.0
    x0 = length - b
    ents = [
        _poly([(-k, -dk / 2.0), (0.0, -dk / 2.0), (0.0, dk / 2.0), (-k, dk / 2.0)]),
        _line(0.0, d / 2.0, length, d / 2.0),
        _line(0.0, -d / 2.0, length, -d / 2.0),
        _line(length, -d / 2.0, length, d / 2.0),
        _line(x0, minor, length, minor),
        _line(x0, -minor, length, -minor),
    ]
    if x0 > 1e-9:
        ents.append(_line(x0, -d / 2.0, x0, d / 2.0))
    ents.append(_line(-k - AXIS_OVERRUN, 0.0, length + AXIS_OVERRUN, 0.0, LAYER_CENTER))
    return ents, (-k - AXIS_OVERRUN, -dk / 2.0, length + AXIS_OVERRUN, dk / 2.0)


def _socket_head_top(dims: dict):
    dk, s = dims["dk"], dims["s"]
    ents = [_circle(0.0, 0.0, dk / 2.0), _poly(_hex_points(across_corners(s) / 2.0))]
    ents += _centre_mark(0.0, 0.0, dk / 2.0 + AXIS_OVERRUN)
    extent = dk / 2.0 + AXIS_OVERRUN
    return ents, (-extent, -extent, extent, extent)


def _bearing_simplified(dims: dict):
    inner, outer, half = dims["d"] / 2.0, dims["D"] / 2.0, dims["B"] / 2.0
    ents = [
        _poly([(-half, inner), (half, inner), (half, outer), (-half, outer)]),
        _poly([(-half, -inner), (half, -inner), (half, -outer), (-half, -outer)]),
    ]
    arm_x = CROSS_FRACTION * half
    arm_y = CROSS_FRACTION * (outer - inner) / 2.0
    middle = (inner + outer) / 2.0
    for centre in (middle, -middle):
        ents.append(_line(-arm_x, centre, arm_x, centre))
        ents.append(_line(0.0, centre - arm_y, 0.0, centre + arm_y))
    ents.append(_line(-half - AXIS_OVERRUN, 0.0, half + AXIS_OVERRUN, 0.0, LAYER_CENTER))
    return ents, (-half - AXIS_OVERRUN, -outer, half + AXIS_OVERRUN, outer)


def _bearing_detailed(dims: dict):
    inner, outer, half = dims["d"] / 2.0, dims["D"] / 2.0, dims["B"] / 2.0
    radius = BALL_SYMBOL_FRACTION * (outer - inner) / 2.0
    middle = (inner + outer) / 2.0
    ents = [
        _poly([(-half, inner), (half, inner), (half, outer), (-half, outer)]),
        _poly([(-half, -inner), (half, -inner), (half, -outer), (-half, -outer)]),
        _circle(0.0, middle, radius),
        _circle(0.0, -middle, radius),
        _line(-half - AXIS_OVERRUN, 0.0, half + AXIS_OVERRUN, 0.0, LAYER_CENTER),
    ]
    return ents, (-half - AXIS_OVERRUN, -outer, half + AXIS_OVERRUN, outer)


_BUILDERS = {
    ("hex_bolt", "side"): lambda dims: _hex_fastener_side(dims, threaded_to_head=False),
    ("hex_bolt", "top"): _hex_fastener_top,
    ("hex_screw", "side"): lambda dims: _hex_fastener_side(dims, threaded_to_head=True),
    ("hex_screw", "top"): _hex_fastener_top,
    ("hex_nut", "side"): _hex_nut_side,
    ("hex_nut", "top"): _hex_nut_top,
    ("washer", "side"): _washer_side,
    ("washer", "top"): _washer_top,
    ("socket_head_screw", "side"): _socket_head_side,
    ("socket_head_screw", "top"): _socket_head_top,
    ("deep_groove_bearing", "simplified"): _bearing_simplified,
    ("deep_groove_bearing", "detailed"): _bearing_detailed,
}


def block_spec(designation: str, view: str) -> dict:
    """Typed primitives and ATTDEFs for ``block_define``, in block-local mm.

    The block's origin is the part's own datum: the head bearing face for a
    bolt or cap screw, the bearing face for a nut or washer, the centre for a
    bearing, with the axis along +X. Every refusal fires before any geometry
    is built.
    """
    parsed = parse_designation(designation)
    kind = _resolve_view(parsed["family"], view)
    dims = _resolve_dims(parsed)
    entities, bbox = _BUILDERS[(parsed["family"], kind)](dims)
    return {
        "name": _block_name(parsed["standard"], parsed["size"], kind),
        "entities": entities,
        "attdefs": _attdefs(parsed, bbox),
        "base_x": 0.0,
        "base_y": 0.0,
        "bbox": bbox,
        "view": kind,
        "designation": parsed["designation"],
        "standard": parsed["standard"],
        "size": parsed["size"],
        "family": parsed["family"],
        "dims": dims,
        "source": STANDARD_SOURCE[parsed["standard"]],
    }


async def insert_std_part(
    backend: AutoCADBackend,
    designation: str,
    *,
    at,
    view: str = DEFAULT_VIEW,
    rotation: float = 0.0,
    layer: str = LAYER_VISIBLE,
    attributes: dict | None = None,
    material: str | None = None,
) -> dict:
    """Define the part's block once, insert it, write its ``std_part`` payload.

    ``at`` is the WCS point the block's own datum lands on. The default
    ``view="side"`` falls back to a family's first view (``simplified`` for a
    bearing) and the result reports which view was drawn; any other mismatch is
    refused. Refusals - an unknown designation with the nearest catalogue
    entries, a bad view, a non-finite coordinate or rotation, an attribute tag
    the block does not have - all fire before the first entity is written.
    """
    spec = block_spec(designation, view)
    try:
        x, y = float(at[0]), float(at[1])
    except (TypeError, IndexError, ValueError) as exc:
        raise ValueError(f"insert_std_part: 'at' must be two numbers, got {at!r}") from exc
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"insert_std_part: 'at' must be two finite numbers, got {at!r}")
    angle = float(rotation)
    if not math.isfinite(angle):
        raise ValueError(f"insert_std_part: rotation must be finite, got {rotation!r}")

    values = {att["tag"]: att["default"] for att in spec["attdefs"]}
    supplied = dict(attributes or {})
    if material is not None:
        supplied["MAT"] = material
    unknown = sorted(set(supplied) - set(values))
    if unknown:
        raise ValueError(
            f"insert_std_part: {spec['name']} has no attribute {', '.join(unknown)}; "
            f"valid tags: {', '.join(values)}."
        )
    values.update({str(key): str(value) for key, value in supplied.items()})

    existing = {block.name for block in await backend.block_list()}
    defined = spec["name"] not in existing
    layers_created: list[str] = []
    if defined:
        created = await backend.block_define(
            spec["name"],
            spec["entities"],
            spec["attdefs"],
            spec["base_x"],
            spec["base_y"],
            False,
            True,
        )
        layers_created = list(created.get("layers_created", []))
    ref = await backend.block_insert(spec["name"], x, y, 1.0, 1.0, angle, values, layer)
    payload = {
        "v": PAYLOAD_VERSION,
        "kind": "std_part",
        "designation": spec["designation"],
        "standard": spec["standard"],
        "size": spec["size"],
        "qty": 1,
        "material": values["MAT"],
    }
    await backend.entity_set_xdata(ref.handle, APP_ID, to_values(payload))
    return {
        "ok": True,
        "handle": ref.handle,
        "block_name": spec["name"],
        "defined": defined,
        "layers_created": layers_created,
        "designation": spec["designation"],
        "standard": spec["standard"],
        "size": spec["size"],
        "family": spec["family"],
        "view": spec["view"],
        "layer": layer,
        "at": [x, y],
        "rotation": angle,
        "dims": spec["dims"],
        "source": spec["source"],
        "payload": payload,
        "backend": backend.name,
    }


# ── standard features drawn onto geometry that already exists ───────────────
#
# Local frame for every feature: the origin is on the axis, +X runs along the
# axis in the direction the feature extends, +Y is radially outward. No feature
# draws a centre line - the geometry it is added to already has one.

STD_FEATURE_KINDS = ("thread", "undercut", "ring_groove", "centre_hole", "oring_groove")

FEATURE_SOURCE = {
    "thread": SOURCE_ISO_6410,
    "undercut": "DIN 509 relief grooves - profile from the registered DIN 509 table",
    "ring_groove": "DIN 471 / DIN 472 retaining rings - dimensions from the registered table",
    "centre_hole": "DIN 332-1 centre holes forms A and B - dimensions from the registered table",
    "oring_groove": "ISO 3601-2 O-ring housings - dimensions from the registered table",
}


def _positive(params: dict, key: str, kind: str) -> float:
    if key not in params:
        raise ValueError(f"{kind}: params[{key!r}] is required")
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{kind}: params[{key!r}] must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{kind}: params[{key!r}] must be finite and > 0, got {value!r}")
    return number


def _side(params: dict, kind: str) -> str:
    where = " ".join(str(params.get("kind", "shaft")).split()).lower()
    if where not in ("shaft", "bore"):
        raise ValueError(
            f"{kind}: params['kind'] must be 'shaft' or 'bore', got {params.get('kind')!r}"
        )
    return where


def _table_row(standard: str, size, kind: str, required: tuple[str, ...]) -> dict:
    try:
        row = dict(standards_lookup(standard, size))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{kind}: {standard} - {exc}") from exc
    missing = [key for key in required if key not in row]
    if missing:
        raise ValueError(
            f"{kind}: the {standard} row for {size} carries no {', '.join(missing)}. "
            "std_feature_draw draws the dimensions the standard's own table module "
            "publishes and never reconstructs them."
        )
    return row


def _groove_prims(surface: float, floor: float, width: float) -> tuple[dict, ...]:
    steps = (
        (0.0, surface, 0.0, floor),
        (0.0, floor, width, floor),
        (width, floor, width, surface),
    )
    return tuple(
        _line(x1, sign * y1, x2, sign * y2) for sign in (1.0, -1.0) for x1, y1, x2, y2 in steps
    )


def _thread_feature(params: dict):
    d = _positive(params, "d", "thread")
    length = _positive(params, "length", "thread")
    internal = bool(params.get("internal", False))
    size = params.get("size")
    if size:
        pitch = thread_pitch(size)
    elif "pitch" in params:
        pitch = _positive(params, "pitch", "thread")
    else:
        raise ValueError(
            "thread: give params['size'] (e.g. 'M20', whose pitch comes from ISO 261) or "
            "an explicit params['pitch'] - the pitch is never guessed."
        )
    thin = d / 2.0 if internal else THREAD_MINOR_RATIO * d / 2.0
    prims = (
        _line(0.0, thin, length, thin),
        _line(0.0, -thin, length, -thin),
        _line(length, -d / 2.0, length, d / 2.0),
    )
    dims = {"d": d, "length": length, "pitch": pitch, "internal": internal}
    return prims, dims, "ISO 6410"


def _ring_groove_feature(params: dict):
    d = _positive(params, "d", "ring_groove")
    where = _side(params, "ring_groove")
    standard = " ".join(
        str(params.get("standard") or ("DIN 471" if where == "shaft" else "DIN 472")).split()
    )
    row = _table_row(standard, d, "ring_groove", ("m", "d2"))
    width, groove_d = float(row["m"]), float(row["d2"])
    if not math.isfinite(width) or width <= 0.0:
        raise ValueError(f"ring_groove: {standard} row for d={d} has groove width m={width}.")
    if where == "shaft" and not 0.0 < groove_d < d:
        raise ValueError(
            f"ring_groove: {standard} row for d={d} has d2={groove_d}, which is not a "
            "shaft groove (a shaft groove needs d2 < d)."
        )
    if where == "bore" and not groove_d > d:
        raise ValueError(
            f"ring_groove: {standard} row for d={d} has d2={groove_d}, which is not a "
            "bore groove (a bore groove needs d2 > d)."
        )
    prims = _groove_prims(d / 2.0, groove_d / 2.0, width)
    return prims, {"d": d, "m": width, "d2": groove_d, "kind": where}, standard


def _oring_groove_feature(params: dict):
    d = _positive(params, "d", "oring_groove")
    cord = _positive(params, "cord", "oring_groove")
    where = _side(params, "oring_groove")
    standard = " ".join(str(params.get("standard") or "ISO 3601-2").split())
    row = _table_row(standard, cord, "oring_groove", ("b", "h"))
    width, depth = float(row["b"]), float(row["h"])
    if not math.isfinite(width) or width <= 0.0 or not math.isfinite(depth) or depth <= 0.0:
        raise ValueError(
            f"oring_groove: {standard} row for cord={cord} has b={width}, h={depth}; "
            "both must be finite and > 0."
        )
    if where == "shaft" and depth >= d / 2.0:
        raise ValueError(
            f"oring_groove: {standard} groove depth h={depth} is not inside a shaft of "
            f"d={d} (the floor would be at or through the axis)."
        )
    surface = d / 2.0
    floor = surface - depth if where == "shaft" else surface + depth
    prims = _groove_prims(surface, floor, width)
    return prims, {"d": d, "cord": cord, "b": width, "h": depth, "kind": where}, standard


def _centre_hole_feature(params: dict):
    form = " ".join(str(params.get("form", "A")).split()).upper()
    if form not in ("A", "B"):
        raise ValueError(
            "centre_hole: this tool draws DIN 332-1 forms A and B. Form R's radius "
            f"profile is not transcribed here and is refused rather than approximated; "
            f"got {form!r}."
        )
    if params.get("size") is None:
        raise ValueError("centre_hole: params['size'] is the DIN 332 pilot diameter d1, e.g. 2.5")
    size = params["size"]
    standard = " ".join(str(params.get("standard") or f"DIN 332-{form}").split())
    required = ("d1", "d2", "t") if form == "A" else ("d1", "d2", "d3", "t")
    row = _table_row(standard, size, "centre_hole", required)
    d1, d2, depth = float(row["d1"]), float(row["d2"]), float(row["t"])
    if not 0.0 < d1 < d2:
        raise ValueError(
            f"centre_hole: {standard} row for {size} has d1={d1}, d2={d2}; d1 < d2 is required."
        )
    # 60 degrees included -> the axial run of the countersink is (d2-d1)/2 / tan(30).
    cone = math.sqrt(3.0) * (d2 - d1) / 2.0
    start = 0.0
    prims: list[dict] = []
    dims = {"form": form, "d1": d1, "d2": d2, "t": depth, "cone": cone}
    if form == "B":
        d3 = float(row["d3"])
        if not d3 > d2:
            raise ValueError(
                f"centre_hole: {standard} row for {size} has d3={d3}; form B needs d3 > d2={d2}."
            )
        # 120 degrees included -> (d3-d2)/2 / tan(60).
        chamfer = (d3 - d2) / (2.0 * math.sqrt(3.0))
        prims += [
            _line(0.0, d3 / 2.0, chamfer, d2 / 2.0),
            _line(0.0, -d3 / 2.0, chamfer, -d2 / 2.0),
        ]
        start = chamfer
        dims["d3"] = d3
        dims["chamfer"] = chamfer
    prims += [
        _line(start, d2 / 2.0, start + cone, d1 / 2.0),
        _line(start, -d2 / 2.0, start + cone, -d1 / 2.0),
    ]
    if depth <= start + cone:
        raise ValueError(
            f"centre_hole: {standard} row for {size} has t={depth} mm, which is not deeper "
            f"than the countersink ({start + cone:.3f} mm); t is measured from the end face."
        )
    prims += [
        _line(start + cone, d1 / 2.0, depth, d1 / 2.0),
        _line(start + cone, -d1 / 2.0, depth, -d1 / 2.0),
        _line(depth, -d1 / 2.0, depth, d1 / 2.0),
    ]
    return tuple(prims), dims, standard


def _undercut_feature(params: dict):
    d = _positive(params, "d", "undercut")
    form = " ".join(str(params.get("form", "E")).split()).upper()
    if form not in ("E", "F"):
        raise ValueError(f"undercut: DIN 509 defines forms E and F; got {form!r}.")
    standard = " ".join(str(params.get("standard") or f"DIN 509-{form}").split())
    row = _table_row(standard, d, "undercut", ("profile",))
    profile = row["profile"]
    if not isinstance(profile, (list, tuple)) or len(profile) < 2:
        raise ValueError(
            f"undercut: the {standard} row for d={d} has a profile with fewer than two points."
        )
    points: list[tuple[float, float]] = []
    for index, point in enumerate(profile):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(
                f"undercut: {standard} profile[{index}] must be an [along, depth] pair, "
                f"got {point!r}"
            )
        along, depth = float(point[0]), float(point[1])
        if not (math.isfinite(along) and math.isfinite(depth)):
            raise ValueError(f"undercut: {standard} profile[{index}] must be finite.")
        if depth < 0.0 or depth >= d / 2.0:
            raise ValueError(
                f"undercut: {standard} profile[{index}] depth {depth} is not inside a shaft "
                f"of d={d} (needs 0 <= depth < {d / 2.0})."
            )
        points.append((along, d / 2.0 - depth))
    prims = (
        _poly(points, closed=False),
        _poly([(x, -y) for x, y in points], closed=False),
    )
    dims = {"d": d, "form": form, "profile": [[float(a), float(b)] for a, b in profile]}
    return prims, dims, standard


_FEATURES = {
    "thread": _thread_feature,
    "undercut": _undercut_feature,
    "ring_groove": _ring_groove_feature,
    "centre_hole": _centre_hole_feature,
    "oring_groove": _oring_groove_feature,
}


def feature_spec(kind: str, params: dict) -> dict:
    """Primitives, resolved dimensions and provenance for one standard feature."""
    name = " ".join(str(kind).split()).lower()
    if name not in STD_FEATURE_KINDS:
        raise ValueError(
            f"std_feature_draw: kind must be one of {', '.join(STD_FEATURE_KINDS)}, got {kind!r}."
        )
    prims, dims, standard = _FEATURES[name](dict(params or {}))
    return {
        "kind": name,
        "prims": prims,
        "dims": dims,
        "standard": standard,
        "source": FEATURE_SOURCE[name],
    }


def feature_prims(kind: str, params: dict) -> tuple[dict, ...]:
    """Just the primitives of :func:`feature_spec`, in the feature's local frame."""
    return feature_spec(kind, params)["prims"]


def place_prims(prims, at, rotation: float = 0.0) -> tuple[dict, ...]:
    """Map local primitives into WCS: rotate about the origin, then translate to ``at``."""
    try:
        x0, y0 = float(at[0]), float(at[1])
    except (TypeError, IndexError, ValueError) as exc:
        raise ValueError(f"place_prims: 'at' must be two numbers, got {at!r}") from exc
    if not (math.isfinite(x0) and math.isfinite(y0)):
        raise ValueError(f"place_prims: 'at' must be two finite numbers, got {at!r}")
    degrees = float(rotation)
    if not math.isfinite(degrees):
        raise ValueError(f"place_prims: rotation must be finite, got {rotation!r}")
    cosine, sine = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))

    def point(x: float, y: float) -> tuple[float, float]:
        return (x0 + x * cosine - y * sine, y0 + x * sine + y * cosine)

    placed: list[dict] = []
    for prim in prims:
        moved = dict(prim)
        kind = prim["type"]
        if kind == "line":
            moved["x1"], moved["y1"] = point(prim["x1"], prim["y1"])
            moved["x2"], moved["y2"] = point(prim["x2"], prim["y2"])
        elif kind == "circle":
            moved["cx"], moved["cy"] = point(prim["cx"], prim["cy"])
        elif kind == "arc":
            moved["cx"], moved["cy"] = point(prim["cx"], prim["cy"])
            moved["start_deg"] = prim["start_deg"] + degrees
            moved["end_deg"] = prim["end_deg"] + degrees
        elif kind == "polyline":
            moved["points"] = [list(point(px, py)) for px, py in prim["points"]]
        else:  # pragma: no cover - the builders emit nothing else
            raise ValueError(f"place_prims: cannot place a {kind!r} primitive")
        placed.append(moved)
    return tuple(placed)


async def draw_std_feature(
    backend: AutoCADBackend,
    kind: str,
    at,
    params: dict,
    *,
    rotation: float = 0.0,
    layer: str | None = None,
) -> dict:
    """Draw a standard feature onto geometry that already exists.

    ``at`` is a WCS point on the axis; ``rotation`` turns the local +X (along
    the axis, in the direction the feature extends) counter-clockwise. ``layer``
    overrides the visible primitives only - CENTER and HIDDEN primitives keep
    their own. Every refusal fires before the first entity is written.
    """
    spec = feature_spec(kind, params)
    placed = place_prims(spec["prims"], at, rotation)
    handles: list[str] = []
    layers: list[str] = []
    for prim in placed:
        target = layer if (layer and prim["layer"] == LAYER_VISIBLE) else prim["layer"]
        if prim["type"] == "line":
            info = await backend.entity_create_line(
                prim["x1"], prim["y1"], prim["x2"], prim["y2"], layer=target
            )
        elif prim["type"] == "circle":
            info = await backend.entity_create_circle(
                prim["cx"], prim["cy"], prim["r"], layer=target
            )
        elif prim["type"] == "arc":
            info = await backend.entity_create_arc(
                prim["cx"], prim["cy"], prim["r"], prim["start_deg"], prim["end_deg"], layer=target
            )
        else:
            info = await backend.entity_create_polyline(prim["points"], prim["closed"], target)
        handles.append(info.handle)
        if target not in layers:
            layers.append(target)
    return {
        "ok": True,
        "kind": spec["kind"],
        "handles": handles,
        "primitive_count": len(handles),
        "dims": spec["dims"],
        "standard": spec["standard"],
        "source": spec["source"],
        "layers": layers,
        "at": [float(at[0]), float(at[1])],
        "rotation": float(rotation),
        "backend": backend.name,
    }
