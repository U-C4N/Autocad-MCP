# engineering/pid/symbols_vessels.py
"""Parametric vessels (ISO 10628-2): size and nozzles come from the caller.

Unlike the fixed-size catalogue, a vessel's block definition depends on its
parameters, so the block name carries an 8-hex hash of the validated parameter
set (spec 4.5): identical parameters share one definition, different ones get
their own. Nozzles become stub lines plus named ports; the default set is
``N1`` top, ``N2`` bottom, ``N3`` left (0.7 up the wall), ``N4`` right (0.3).

A nozzle's port sits ``STUB`` outside the vessel's bounding box on the side it
was asked for; its stub is drawn from the port inward to the point where that
axis-aligned line meets the *drawn* outline (a dished head, a hopper's sloped
wall, a cone roof), so no stub ever floats off the vessel or pierces it.
"""

from __future__ import annotations

from .geometry import arc, line, polyline, scan_hits
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
#: Capsule bodies (cylinder with two dished heads) whose axis is vertical.
_VERTICAL_CAPSULES = ("vertical_vessel", "column", "reactor")
PARAMS_SCHEMA = {
    "width": "float > 0 (mm); vertical_vessel/column/reactor need width < height",
    "height": "float > 0 (mm); horizontal_vessel needs height < width; cone tank height > width/6",
    "nozzles": "[{name, side: top|bottom|left|right, fraction: 0..1}]",
    "roof": "tank only: flat | cone",
    "trays": "column only: int >= 0",
    "jacketed": "reactor only: bool",
}
#: Distance of a nozzle port beyond the vessel's bounding box (symbol-local mm);
#: the stub runs from there in to the drawn outline, so it is at least this long.
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
    w, h = merged["width"], merged["height"]
    if symbol in _VERTICAL_CAPSULES and h <= w:
        raise ValueError(
            f"{symbol}: height {h} must exceed width {w} — the dished heads are half-circles "
            "of the width, so a shorter axis folds them through each other"
        )
    if symbol == "horizontal_vessel" and w <= h:
        raise ValueError(
            f"{symbol}: width {w} must exceed height {h} — the dished heads are half-circles "
            "of the height, so a shorter axis folds them through each other"
        )
    if "roof" in merged and merged["roof"] not in ("flat", "cone"):
        raise ValueError(f"{symbol}: roof must be 'flat' or 'cone'")
    if merged.get("roof") == "cone" and h <= w / 6:
        raise ValueError(
            f"{symbol}: height {h} must exceed width/6 ({w / 6}) — the cone roof is that tall"
        )
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
        for key in n:
            if key not in ("name", "side", "fraction"):
                raise ValueError(
                    f"{symbol}: nozzles[{i}]: unknown key {key!r}; valid: fraction, name, side"
                )
        if n.get("side") not in SIDES:
            raise ValueError(f"{symbol}: nozzles[{i}].side must be one of {', '.join(SIDES)}")
        f = n.get("fraction", 0.5)
        if not _is_number(f) or not 0.0 <= f <= 1.0:
            raise ValueError(f"{symbol}: nozzles[{i}].fraction must be within 0..1")
        clean.append({"name": n["name"], "side": n["side"], "fraction": float(f)})
    merged["nozzles"] = clean
    return merged


def _nozzles(
    nozzles: list[dict], shell: list[dict], half_w: float, half_h: float, extra: float = 0.0
) -> tuple[list[dict], list[Port]]:
    """Nozzle stub primitives and ports for a vessel whose outline is ``shell``.

    The port sits ``STUB + extra`` outside the ±half_w × ±half_h box (``extra``
    is a jacketed reactor's jacket, which the stub passes through so the port
    still sits on the symbol's outer edge); the stub starts where the port's
    axis-aligned line meets the outermost point of the drawn outline.
    """
    prims: list[dict] = []
    ports: list[Port] = []
    stub = STUB + extra
    for n in nozzles:
        side, f = n["side"], n["fraction"]
        if side in ("top", "bottom"):
            x = -half_w + f * 2 * half_w
            hits = scan_hits(shell, "x", x)
            if not hits:
                raise RuntimeError(f"nozzle {n['name']!r}: x={x} meets no outline")
            if side == "top":
                y0, y1, d = max(hits), half_h + stub, 90.0
            else:
                y0, y1, d = min(hits), -half_h - stub, 270.0
            prims.append(line(x, y0, x, y1))
            ports.append(Port(n["name"], x, y1, d))
        else:
            y = -half_h + f * 2 * half_h
            hits = scan_hits(shell, "y", y)
            if not hits:
                raise RuntimeError(f"nozzle {n['name']!r}: y={y} meets no outline")
            if side == "left":
                x0, x1, d = min(hits), -half_w - stub, 180.0
            else:
                x0, x1, d = max(hits), half_w + stub, 0.0
            prims.append(line(x0, y, x1, y))
            ports.append(Port(n["name"], x1, y, d))
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
    if symbol in _VERTICAL_CAPSULES:
        shell = _capsule_vertical(w, h)
    elif symbol == "horizontal_vessel":
        shell = _capsule_horizontal(w, h)
    elif symbol == "tank":
        roof = w / 6 if p["roof"] == "cone" else 0.0
        shell = [
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
            shell.append(
                polyline(
                    [(-half_w, half_h - roof), (0, half_h), (half_w, half_h - roof)],
                    closed=False,
                )
            )
    else:  # hopper: flat top, walls converging to a narrow outlet
        shell = [
            polyline([(-half_w, half_h), (half_w, half_h), (w / 6, -half_h), (-w / 6, -half_h)])
        ]
    prims = list(shell)
    if symbol == "column":
        trays = p["trays"]
        for i in range(trays):
            y = -half_h + (i + 1) * h / (trays + 1)
            prims.append(line(-half_w + 1, y, half_w - 1, y))
    elif symbol == "reactor" and p["jacketed"]:
        body = half_h - half_w
        prims += [
            line(-half_w - JACKET, -body, -half_w - JACKET, body),
            line(half_w + JACKET, -body, half_w + JACKET, body),
        ]
        extra = JACKET
    nozzle_prims, ports = _nozzles(p["nozzles"], shell, half_w, half_h, extra)
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
