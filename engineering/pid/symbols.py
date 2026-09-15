# engineering/pid/symbols.py
"""The P&ID symbol catalogue: symbols as data, blocks on demand.

Every symbol is a ``SymbolSpec`` — typed primitives in symbol-local mm, ATTDEFs,
named ports with an outward direction, a declared bbox — authored from
ISO 10628-2 (equipment, valves) or ISA-5.1 (instrumentation). Builders live in
``symbols_valves.py``, ``symbols_equipment.py`` and ``symbols_vessels.py`` and
register here; this module holds the framework plus the instrument bubble,
markers and connectors.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from .geometry import (
    attdef,
    bbox_of,
    circle,
    dashed_line,
    line,
    polyline,
    regular_polygon,
    solid,
)

#: Written into every INSERT's XDATA; bump on any change to shipped geometry or ports.
CATALOG_VERSION = "1"
BUBBLE_RADIUS = 5.0
FAMILY_LAYER = {
    "valve": "PROCESS-VALVES",
    "rotating": "PROCESS-EQUIPMENT",
    "vessel": "PROCESS-EQUIPMENT",
    "heat": "PROCESS-EQUIPMENT",
    "misc": "PROCESS-EQUIPMENT",
    "instrument": "INSTRUMENT-SYMBOL",
    "connector": "PROCESS-EQUIPMENT",
    "marker": "line",
}
INSTRUMENT_TYPES = ("discrete", "dcs", "computer", "plc")
INSTRUMENT_LOCATIONS = ("field", "primary", "auxiliary", "primary_rear", "auxiliary_rear")
TAG_HEIGHT = 2.5
DESC_HEIGHT = 2.0


@dataclass(frozen=True)
class Port:
    name: str
    x: float
    y: float
    direction_deg: float | None  # outward direction a line leaves along; None = radial
    kind: str = "process"  # "process" | "signal"
    radius: float = 0.0  # radial ports only

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "direction_deg": self.direction_deg,
            "kind": self.kind,
            "radius": self.radius,
        }


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    family: str
    symbol: str
    variant: str | None
    primitives: tuple[dict, ...]
    attdefs: tuple[dict, ...]
    ports: tuple[Port, ...]
    bbox: tuple[float, float, float, float]
    layer_class: str
    source: str
    parametric: bool = False
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "family": self.family,
            "symbol": self.symbol,
            "variant": self.variant,
            "ports": [p.to_dict() for p in self.ports],
            "bbox": list(self.bbox),
            "layer_class": self.layer_class,
            "source": self.source,
            "parametric": self.parametric,
            "params": dict(self.params),
        }


Builder = Callable[..., SymbolSpec]
_REGISTRY: dict[str, tuple[str, Builder, dict[str, list[str]], dict | None]] = {}


def register(
    symbol: str,
    family: str,
    builder: Builder,
    variants: dict[str, list[str]] | None = None,
    params_schema: dict | None = None,
) -> None:
    if family not in FAMILY_LAYER:
        raise ValueError(f"unknown family {family!r}")
    _REGISTRY[symbol] = (family, builder, dict(variants or {}), params_schema)


def resolve(symbol: str, **options) -> SymbolSpec:
    """Build the SymbolSpec for ``symbol`` with the given variant options."""
    if symbol not in _REGISTRY:
        raise ValueError(
            f"unknown symbol {symbol!r}; valid symbols: {', '.join(sorted(_REGISTRY))}"
        )
    family, builder, variants, schema = _REGISTRY[symbol]
    for key, value in options.items():
        if value is None:
            continue
        if key == "params":
            if schema is None:
                raise ValueError(f"{symbol} is not parametric; 'params' is only for vessels")
            continue
        if key not in variants:
            raise ValueError(f"{symbol} takes no option {key!r}")
        if value not in variants[key]:
            raise ValueError(
                f"{symbol}: {key} must be one of {', '.join(variants[key])}, got {value!r}"
            )
    return builder(**{k: v for k, v in options.items() if v is not None})


def list_symbols(family: str | None = None) -> list[dict]:
    rows = []
    for symbol, (fam, builder, variants, schema) in sorted(_REGISTRY.items()):
        if family and fam != family:
            continue
        base = builder()
        rows.append(
            {
                "symbol": symbol,
                "family": fam,
                "variants": variants,
                "ports": [p.to_dict() for p in base.ports],
                "parametric": base.parametric,
                "params_schema": schema or {},
                "source": base.source,
                "block_name": base.name,
            }
        )
    return rows


def all_specs() -> list[SymbolSpec]:
    """Every base symbol plus every enumerable variant (for tests and the contact sheet)."""
    specs: list[SymbolSpec] = []
    for _symbol, (_fam, builder, variants, _schema) in sorted(_REGISTRY.items()):
        if not variants:
            specs.append(builder())
            continue
        keys = sorted(variants)
        combos: list[dict] = [{}]
        for key in keys:
            combos = [{**c, key: v} for c in combos for v in variants[key]]
        for combo in combos:
            try:
                specs.append(builder(**combo))
            except ValueError:
                continue  # a combination the builder refuses (e.g. an actuator on a check valve)
    return specs


def transform_port(
    port: Port,
    x: float,
    y: float,
    rotation_deg: float,
    scale: float,
    y_scale: float | None = None,
) -> dict:
    """Symbol-local port -> WCS, for an INSERT at (x, y) rotated by ``rotation_deg``.

    ``scale`` is the X factor; ``y_scale`` defaults to it (a uniform INSERT). A
    negative factor is a mirror — ``entity_mirror`` on an INSERT writes
    ``y_scale = -1`` — and is applied in full, to the point, the direction and
    the radius, so the port lands where the mirrored geometry actually is. The
    magnitudes must match: a stretched symbol has no circle for a radial port to
    sit on, so it is refused instead of resolving to a point off the symbol.
    """
    sx = float(scale)
    sy = sx if y_scale is None else float(y_scale)
    if abs(abs(sx) - abs(sy)) > 1e-9:
        raise ValueError(
            f"non-uniform INSERT scale (x_scale={sx:g}, y_scale={sy:g}): P&ID symbols "
            "are placed with one scale factor (a mirror may flip its sign); re-insert "
            "the symbol with pid_symbol_insert"
        )
    a = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    lx, ly = port.x * sx, port.y * sy
    wx = x + lx * cos_a - ly * sin_a
    wy = y + lx * sin_a + ly * cos_a
    if port.direction_deg is None:
        direction = None
    else:
        # Reflect first (a flipped X negates the angle about 90°, a flipped Y
        # negates it about 0°), then rotate — exact, so the uniform case keeps
        # the same numbers it always had.
        d = float(port.direction_deg)
        if sx < 0:
            d = 180.0 - d
        if sy < 0:
            d = -d
        direction = (d + rotation_deg) % 360.0
    return {
        "name": port.name,
        "x": wx,
        "y": wy,
        "direction_deg": direction,
        "kind": port.kind,
        "radius": port.radius * abs(sx),
        "inferred": False,
    }


def params_hash(params: dict) -> str:
    """First 8 hex chars of SHA-256 over the canonical JSON of ``params`` (spec 4.5)."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:8].upper()


def tag_attdefs(ymax: float, with_desc: bool = True) -> tuple[dict, ...]:
    """``TAG`` (and ``DESC``) above the body, per spec 4.7."""
    out = [attdef("TAG", 0.0, ymax + 3.5, TAG_HEIGHT)]
    if with_desc:
        out.append(attdef("DESC", 0.0, ymax + 6.5, DESC_HEIGHT))
    return tuple(out)


def make_spec(
    name: str,
    family: str,
    symbol: str,
    variant: str | None,
    primitives,
    attdefs,
    ports,
    source: str,
    bbox=None,
    parametric: bool = False,
    params: dict | None = None,
) -> SymbolSpec:
    prims = tuple(primitives)
    return SymbolSpec(
        name=name,
        family=family,
        symbol=symbol,
        variant=variant,
        primitives=prims,
        attdefs=tuple(attdefs),
        ports=tuple(ports),
        bbox=tuple(bbox) if bbox else bbox_of(prims),
        layer_class=FAMILY_LAYER[family],
        source=source,
        parametric=parametric,
        params=dict(params or {}),
    )


# ── instrument bubble (ISA-5.1-2009 Table 5.4.1) ─────────────────────────────


def _bubble_half_width(type: str, y: float) -> float:
    """Half-width of the instrument outline at height ``y`` so a location line
    ends on the outline instead of overshooting it (circle: chord; square:
    full width; flat-topped hexagon: ``r - |y| / tan 60``)."""
    r = BUBBLE_RADIUS
    if type == "discrete":
        return math.sqrt(max(r * r - y * y, 0.0))
    if type == "computer":
        return r - abs(y) / math.tan(math.radians(60.0))
    return r  # dcs / plc: the square is the outline


def build_instrument(type: str = "discrete", location: str = "field") -> SymbolSpec:
    if type not in INSTRUMENT_TYPES:
        raise ValueError(f"instrument: type must be one of {', '.join(INSTRUMENT_TYPES)}")
    if location not in INSTRUMENT_LOCATIONS:
        raise ValueError(f"instrument: location must be one of {', '.join(INSTRUMENT_LOCATIONS)}")
    r = BUBBLE_RADIUS
    square = polyline([(-r, -r), (r, -r), (r, r), (-r, r)])
    # Outline per type (spec 4.4): discrete = circle; dcs = circle inside a square;
    # computer = hexagon; plc = diamond inside a square -- no circle in the last two.
    if type == "discrete":
        prims: list[dict] = [circle(0, 0, r)]
    elif type == "dcs":
        prims = [circle(0, 0, r), square]
    elif type == "computer":
        prims = [polyline(regular_polygon(0, 0, r, 6, 0.0))]
    else:  # plc
        prims = [square, polyline([(0, r), (r, 0), (0, -r), (-r, 0)])]
    ys = {
        "field": [],
        "primary": [0.0],
        "auxiliary": [0.8, -0.8],
        "primary_rear": [0.0],
        "auxiliary_rear": [0.8, -0.8],
    }[location]
    for y in ys:
        hw = _bubble_half_width(type, y)
        prims += dashed_line(-hw, y, hw, y) if location.endswith("_rear") else [line(-hw, y, hw, y)]
    attdefs = (
        attdef("FUNC", 0.0, 2.0, DESC_HEIGHT, align="middle_center"),
        attdef("LOOP", 0.0, -2.0, DESC_HEIGHT, align="middle_center"),
    )
    ports = (Port("signal", 0.0, 0.0, None, "signal", r),)
    return make_spec(
        f"PID_INST_{type.upper()}_{location.upper()}",
        "instrument",
        "instrument",
        f"{type}_{location}",
        prims,
        attdefs,
        ports,
        "ISA-5.1-2009 Table 5.4.1",
        bbox=(-r, -r, r, r),
    )


register(
    "instrument",
    "instrument",
    build_instrument,
    variants={"type": list(INSTRUMENT_TYPES), "location": list(INSTRUMENT_LOCATIONS)},
)


# ── markers (decoration on a line; never graph nodes) ────────────────────────

MARKERS = ("arrow_flow", "mark_pneumatic", "mark_capillary", "mark_hydraulic", "mark_data")


def build_marker(which: str) -> SymbolSpec:
    shapes = {
        "arrow_flow": [solid([(0, 0), (-3, 1.2), (-3, -1.2)])],
        "mark_pneumatic": [line(-1.5, -1.5, 0, 1.5), line(0.5, -1.5, 2, 1.5)],
        "mark_capillary": [line(-1.2, -1.2, 1.2, 1.2), line(-1.2, 1.2, 1.2, -1.2)],
        "mark_hydraulic": [line(-1, 1.5, -1, -1.5), line(-1, -1.5, 1.5, -1.5)],
        "mark_data": [circle(0, 0, 0.8)],
    }
    if which not in shapes:
        raise ValueError(f"unknown marker {which!r}; valid markers: {', '.join(MARKERS)}")
    return make_spec(
        f"PID_MARKER_{which.upper()}",
        "marker",
        which,
        None,
        shapes[which],
        (),
        (),
        "ISA-5.1-2009 Table 5.3.1",
    )


def _marker_builder(which: str) -> Builder:
    def build() -> SymbolSpec:
        return build_marker(which)

    return build


for _which in MARKERS:
    register(_which, "marker", _marker_builder(_which))


# ── connectors ───────────────────────────────────────────────────────────────


def build_connector(which: str = "offpage", direction: str = "out") -> SymbolSpec:
    if which == "offpage":
        if direction == "out":
            pts = [(-8, -3), (4, -3), (8, 0), (4, 3), (-8, 3)]
            port = Port("process", -8.0, 0.0, 180.0, "process")
        elif direction == "in":
            pts = [(8, -3), (-4, -3), (-8, 0), (-4, 3), (8, 3)]
            port = Port("process", 8.0, 0.0, 0.0, "process")
        else:
            raise ValueError(f"offpage: direction must be one of in, out, got {direction!r}")
        attdefs = (
            attdef(
                "TAG",
                -2.0 if direction == "out" else 2.0,
                0.0,
                DESC_HEIGHT,
                align="middle_center",
            ),
            attdef("LINK", 0.0, -5.0, DESC_HEIGHT, invisible=True),
        )
        return make_spec(
            f"PID_CONNECTOR_OFFPAGE_{direction.upper()}",
            "connector",
            "offpage",
            direction,
            [polyline(pts)],
            attdefs,
            (port,),
            "PIP PIC001 off-page connector",
            bbox=(-8, -3, 8, 3),
        )
    if which == "tie_in":
        return make_spec(
            "PID_CONNECTOR_TIE_IN",
            "connector",
            "tie_in",
            None,
            [circle(0, 0, 3)],
            (attdef("TAG", 0.0, 0.0, DESC_HEIGHT, align="middle_center"),),
            (Port("in", -3.0, 0.0, 180.0), Port("out", 3.0, 0.0, 0.0)),
            "PIP PIC001 tie-in point",
        )
    if which == "spec_break":
        return make_spec(
            "PID_CONNECTOR_SPEC_BREAK",
            "connector",
            "spec_break",
            None,
            [line(0, -3, 0, 3)],
            (attdef("TAG", 0.0, 4.5, DESC_HEIGHT),),
            (Port("in", -0.3, 0.0, 180.0), Port("out", 0.3, 0.0, 0.0)),
            "PIP PIC001 specification break",
            bbox=(-0.3, -3, 0.3, 4.5),
        )
    raise ValueError(f"unknown connector {which!r}")


register(
    "offpage",
    "connector",
    lambda direction="out": build_connector("offpage", direction),
    variants={"direction": ["in", "out"]},
)
register("tie_in", "connector", lambda: build_connector("tie_in"))
register("spec_break", "connector", lambda: build_connector("spec_break"))

# Builders for valves, equipment and vessels register themselves on import.
from . import symbols_equipment as _symbols_equipment  # noqa: E402,F401
from . import symbols_valves as _symbols_valves  # noqa: E402,F401
from . import symbols_vessels as _symbols_vessels  # noqa: E402,F401
