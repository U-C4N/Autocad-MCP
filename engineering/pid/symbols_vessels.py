# engineering/pid/symbols_vessels.py
"""Parametric vessels (ISO 10628-2): size and nozzles come from the caller.

Unlike the fixed-size catalogue, a vessel's block definition depends on its
parameters, so the block name carries an 8-hex hash of the validated parameter
set (spec 4.5): identical parameters share one definition, different ones get
their own. Nozzles become stub lines plus named ports; the default set is
``N1`` top, ``N2`` bottom, ``N3`` left (0.7 up the wall), ``N4`` right (0.3).
"""

from __future__ import annotations

from .geometry import arc, line, polyline
from .symbols import Port, SymbolSpec, make_spec, params_hash, register, tag_attdefs

VESSELS = ("vertical_vessel", "horizontal_vessel", "tank", "column", "reactor", "hopper")
SIDES = ("top", "bottom", "left", "right")
DEFAULT_NOZZLES = [
    {"name": "N1", "side": "top", "fraction": 0.5},
    {"name": "N2", "side": "bottom", "fraction": 0.5},
    {"name": "N3", "side": "left", "fraction": 0.7},
    {"name": "N4", "side": "right", "fraction": 0.3},
]
_DEFAULTS: dict[str, dict] = {
    "vertical_vessel": {"width": 20.0, "height": 40.0},
    "horizontal_vessel": {"width": 40.0, "height": 20.0},
    "tank": {"width": 24.0, "height": 24.0, "roof": "flat"},
    "column": {"width": 16.0, "height": 60.0, "trays": 0},
    "reactor": {"width": 24.0, "height": 36.0, "jacketed": False},
    "hopper": {"width": 24.0, "height": 24.0},
}
PARAMS_SCHEMA = {
    "width": "float > 0 (mm)",
    "height": "float > 0 (mm)",
    "nozzles": "[{name, side: top|bottom|left|right, fraction: 0..1}]",
    "roof": "tank only: flat | cone",
    "trays": "column only: int >= 0",
    "jacketed": "reactor only: bool",
}
#: Nozzle stub length beyond the vessel wall (symbol-local mm).
STUB = 3.0
#: Gap between a jacketed reactor's wall and its jacket line.
JACKET = 2.0


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_vessel_params(symbol: str, params: dict | None) -> dict:
    """Merge ``params`` over the vessel's defaults and validate every key.

    Raises ``ValueError`` naming the offending key (and nozzle index) before
    anything is built; the returned dict is the canonical parameter set the
    block name is hashed from.
    """
    if symbol not in VESSELS:
        raise ValueError(f"unknown vessel {symbol!r}; valid: {', '.join(VESSELS)}")
    merged = dict(_DEFAULTS[symbol])
    merged["nozzles"] = [dict(n) for n in DEFAULT_NOZZLES]
    if symbol == "hopper":
        merged["nozzles"] = [dict(n) for n in DEFAULT_NOZZLES[:2]]
    if params is not None and not isinstance(params, dict):
        raise ValueError(f"{symbol}: params must be a dict")
    for key, value in (params or {}).items():
        if key not in merged:
            raise ValueError(
                f"{symbol}: unknown parameter {key!r}; valid: {', '.join(sorted(merged))}"
            )
        merged[key] = value
    for key in ("width", "height"):
        v = merged[key]
        if not _is_number(v) or v <= 0:
            raise ValueError(f"{symbol}: {key} must be a number > 0")
        merged[key] = float(v)
    if "roof" in merged and merged["roof"] not in ("flat", "cone"):
        raise ValueError(f"{symbol}: roof must be 'flat' or 'cone'")
    if "trays" in merged:
        trays = merged["trays"]
        if isinstance(trays, bool) or not isinstance(trays, int) or trays < 0:
            raise ValueError(f"{symbol}: trays must be an int >= 0")
    if "jacketed" in merged and not isinstance(merged["jacketed"], bool):
        raise ValueError(f"{symbol}: jacketed must be a bool")
    nozzles = merged["nozzles"]
    if not isinstance(nozzles, list):
        raise ValueError(f"{symbol}: nozzles must be a list")
    names: set[str] = set()
    clean = []
    for i, n in enumerate(nozzles):
        if not isinstance(n, dict) or not isinstance(n.get("name"), str) or not n["name"]:
            raise ValueError(f"{symbol}: nozzles[{i}].name must be a non-empty string")
        if n["name"] in names:
            raise ValueError(f"{symbol}: nozzles[{i}].name {n['name']!r} is repeated")
        names.add(n["name"])
        if n.get("side") not in SIDES:
            raise ValueError(f"{symbol}: nozzles[{i}].side must be one of {', '.join(SIDES)}")
        f = n.get("fraction", 0.5)
        if not _is_number(f) or not 0.0 <= f <= 1.0:
            raise ValueError(f"{symbol}: nozzles[{i}].fraction must be within 0..1")
        clean.append({"name": n["name"], "side": n["side"], "fraction": float(f)})
    merged["nozzles"] = clean
    return merged


def _nozzles(
    params: dict, half_w: float, half_h: float, extra: float = 0.0
) -> tuple[list[dict], list[Port]]:
    """Nozzle stub primitives and ports on a box of ±half_w × ±half_h.

    ``extra`` lengthens the stubs (a jacketed reactor's nozzles pass through
    the jacket) so every port still sits on the symbol's outer edge.
    """
    prims: list[dict] = []
    ports: list[Port] = []
    stub = STUB + extra
    for n in params["nozzles"]:
        side, f = n["side"], n["fraction"]
        if side == "top":
            x, y0, y1 = -half_w + f * 2 * half_w, half_h, half_h + stub
            prims.append(line(x, y0, x, y1))
            ports.append(Port(n["name"], x, y1, 90.0))
        elif side == "bottom":
            x, y0, y1 = -half_w + f * 2 * half_w, -half_h, -half_h - stub
            prims.append(line(x, y0, x, y1))
            ports.append(Port(n["name"], x, y1, 270.0))
        elif side == "left":
            y, x0, x1 = -half_h + f * 2 * half_h, -half_w, -half_w - stub
            prims.append(line(x0, y, x1, y))
            ports.append(Port(n["name"], x1, y, 180.0))
        else:  # right
            y, x0, x1 = -half_h + f * 2 * half_h, half_w, half_w + stub
            prims.append(line(x0, y, x1, y))
            ports.append(Port(n["name"], x1, y, 0.0))
    return prims, ports


def _capsule_vertical(w: float, h: float) -> list[dict]:
    """Cylinder with dished ends, axis vertical: two walls and two half-circle heads."""
    r = w / 2
    body = h / 2 - r
    return [
        line(-r, -body, -r, body),
        line(r, -body, r, body),
        arc(0, body, r, 0, 180),
        arc(0, -body, r, 180, 360),
    ]


def _capsule_horizontal(w: float, h: float) -> list[dict]:
    r = h / 2
    body = w / 2 - r
    return [
        line(-body, -r, body, -r),
        line(-body, r, body, r),
        arc(body, 0, r, 270, 90),
        arc(-body, 0, r, 90, 270),
    ]


def build_vessel(symbol: str = "vertical_vessel", params: dict | None = None) -> SymbolSpec:
    p = validate_vessel_params(symbol, params)
    w, h = p["width"], p["height"]
    half_w, half_h = w / 2, h / 2
    extra = 0.0
    if symbol == "vertical_vessel":
        prims = _capsule_vertical(w, h)
    elif symbol == "horizontal_vessel":
        prims = _capsule_horizontal(w, h)
    elif symbol == "tank":
        roof = w / 6 if p["roof"] == "cone" else 0.0
        prims = [
            polyline(
                [
                    (-half_w, -half_h),
                    (half_w, -half_h),
                    (half_w, half_h - roof),
                    (-half_w, half_h - roof),
                ]
            )
        ]
        if roof:
            prims.append(
                polyline(
                    [(-half_w, half_h - roof), (0, half_h), (half_w, half_h - roof)],
                    closed=False,
                )
            )
    elif symbol == "column":
        prims = _capsule_vertical(w, h)
        trays = p["trays"]
        for i in range(trays):
            y = -half_h + (i + 1) * h / (trays + 1)
            prims.append(line(-half_w + 1, y, half_w - 1, y))
    elif symbol == "reactor":
        prims = _capsule_vertical(w, h)
        if p["jacketed"]:
            body = half_h - half_w
            prims += [
                line(-half_w - JACKET, -body, -half_w - JACKET, body),
                line(half_w + JACKET, -body, half_w + JACKET, body),
            ]
            extra = JACKET
    else:  # hopper: flat top, walls converging to a narrow outlet
        prims = [
            polyline([(-half_w, half_h), (half_w, half_h), (w / 6, -half_h), (-w / 6, -half_h)])
        ]
    nozzle_prims, ports = _nozzles(p, half_w, half_h, extra)
    prims += nozzle_prims
    xs = [-half_w - extra, half_w + extra] + [pt.x for pt in ports]
    ys = [-half_h, half_h] + [pt.y for pt in ports]
    box = (min(xs), min(ys), max(xs), max(ys))
    return make_spec(
        f"PID_VESSEL_{symbol.upper()}_{params_hash(p)}",
        "vessel",
        symbol,
        None,
        prims,
        tag_attdefs(box[3]),
        ports,
        "ISO 10628-2:2012 vessels",
        bbox=box,
        parametric=True,
        params=p,
    )


def _vessel_builder(symbol: str):
    def build(params: dict | None = None) -> SymbolSpec:
        return build_vessel(symbol, params)

    return build


for _symbol in VESSELS:
    register(_symbol, "vessel", _vessel_builder(_symbol), params_schema=PARAMS_SCHEMA)
