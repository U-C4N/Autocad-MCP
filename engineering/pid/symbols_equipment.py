# engineering/pid/symbols_equipment.py
"""Rotating equipment, heat transfer and inline miscellany (ISO 10628-2).

Three families, each authored once in symbol-local mm and registered with the
catalogue on import:

* ``rotating`` — centrifugal / positive-displacement pumps (reference Ø8),
  compressor, fan, agitator (shaft port on the bottom edge, motor bubble on
  top), ejector (motive + suction + out).
* ``heat`` — shell-and-tube (four ports: tube pair on the ends, shell pair on
  the long faces), plate exchanger, air cooler (fan below the bundle), fired
  heater (flue port on top of the stack).
* ``misc`` — inline fittings a process line runs through: filter, Y-strainer,
  reducer (``shape`` = concentric | eccentric, the eccentric outlet dropped
  1 mm), flange pair, spectacle blind, orifice plate (a ``signal`` port on top
  for the DP instrument), drain/vent stub (a single ``in`` port).

Every symbol carries the ``TAG`` / ``DESC`` attribute pair above its bbox and
every port sits on the declared bbox boundary, so a line can leave it along
the port's outward direction without crossing the glyph.
"""

from __future__ import annotations

from .geometry import circle, line, polyline, solid, text
from .symbols import Port, SymbolSpec, make_spec, register, tag_attdefs

ROTATING = ("centrifugal_pump", "pd_pump", "compressor", "fan", "agitator", "ejector")
HEAT = ("shell_tube_hx", "plate_hx", "air_cooler", "fired_heater")
MISC = (
    "filter",
    "strainer_y",
    "reducer",
    "flange_pair",
    "spectacle_blind",
    "orifice_plate",
    "drain_vent",
)
REDUCER_SHAPES = ("concentric", "eccentric")
SOURCE = "ISO 10628-2:2012"

_IN = Port("in", -4.0, 0.0, 180.0)
_OUT = Port("out", 4.0, 0.0, 0.0)

# (primitives, ports, declared bbox)
Body = tuple[list[dict], tuple[Port, ...], tuple[float, float, float, float]]


def _rotating(symbol: str) -> Body:
    if symbol == "centrifugal_pump":
        # Ø8 casing, tangential discharge nozzle on top, impeller arrow inside.
        return (
            [circle(0, 0, 4), line(0, 4, 0, 6), polyline([(-2, -1), (-2, 1), (1, 0)])],
            (Port("suction", -4.0, 0.0, 180.0), Port("discharge", 0.0, 6.0, 90.0)),
            (-4, -4, 4, 6),
        )
    if symbol == "pd_pump":
        return (
            [circle(0, 0, 4), polyline([(-2, -1.5), (2, -1.5), (0, 2)])],
            (_IN, _OUT),
            (-4, -4, 4, 4),
        )
    if symbol == "compressor":
        # Trapezoid narrowing toward the discharge.
        return ([polyline([(-4, -3), (4, -1.5), (4, 1.5), (-4, 3)])], (_IN, _OUT), (-4, -3, 4, 3))
    if symbol == "fan":
        return (
            [circle(0, 0, 4), line(-2.8, -2.8, 2.8, 2.8), line(-2.8, 2.8, 2.8, -2.8)],
            (_IN, _OUT),
            (-4, -4, 4, 4),
        )
    if symbol == "agitator":
        # Shaft from the bottom edge up to an "M" motor bubble; paddle at the shaft foot.
        return (
            [line(0, 0, 0, 8), circle(0, 10, 2), text("M", 0, 10, 2.0), line(-3, 0, 3, 0)],
            (Port("shaft", 0.0, 0.0, 270.0),),
            (-3, 0, 3, 12),
        )
    if symbol == "ejector":
        # Motive fluid enters on the left, suction from below, mixed flow leaves right.
        return (
            [
                polyline([(-4, -1.5), (0, -1.5), (4, -0.75), (4, 0.75), (0, 1.5), (-4, 1.5)]),
                line(0, -1.5, 0, -4),
            ],
            (Port("motive", -4.0, 0.0, 180.0), Port("suction", 0.0, -4.0, 270.0), _OUT),
            (-4, -4, 4, 1.5),
        )
    raise ValueError(f"unknown rotating equipment {symbol!r}")


def _heat(symbol: str) -> Body:
    if symbol == "shell_tube_hx":
        # Shell 24 × 8 with tube sheets 3 mm in from each end.
        return (
            [
                polyline([(-12, -4), (12, -4), (12, 4), (-12, 4)]),
                line(-9, -4, -9, 4),
                line(9, -4, 9, 4),
            ],
            (
                Port("tube_in", -12.0, 0.0, 180.0),
                Port("tube_out", 12.0, 0.0, 0.0),
                Port("shell_in", -4.0, 4.0, 90.0),
                Port("shell_out", 4.0, -4.0, 270.0),
            ),
            (-12, -4, 12, 4),
        )
    if symbol == "plate_hx":
        return (
            [
                polyline([(-4, -6), (4, -6), (4, 6), (-4, 6)]),
                line(-4, -6, 4, 6),
                line(-4, 6, 4, -6),
            ],
            (
                Port("hot_in", -4.0, 3.0, 180.0),
                Port("hot_out", 4.0, -3.0, 0.0),
                Port("cold_in", 4.0, 3.0, 0.0),
                Port("cold_out", -4.0, -3.0, 180.0),
            ),
            (-4, -6, 4, 6),
        )
    if symbol == "air_cooler":
        # Tube bundle 20 × 6 with the fan (circle + cross) hung underneath.
        return (
            [
                polyline([(-10, -3), (10, -3), (10, 3), (-10, 3)]),
                circle(0, -6, 2.5),
                line(-1.8, -7.8, 1.8, -4.2),
                line(-1.8, -4.2, 1.8, -7.8),
            ],
            (Port("in", -10.0, 0.0, 180.0), Port("out", 10.0, 0.0, 0.0)),
            (-10, -8.5, 10, 3),
        )
    if symbol == "fired_heater":
        # Firebox 12 × 12 with a 4-wide stack; the coil zigzags inside the box.
        return (
            [
                polyline([(-6, -8), (6, -8), (6, 4), (2, 4), (2, 10), (-2, 10), (-2, 4), (-6, 4)]),
                polyline([(-4, -6), (4, -4), (-4, -2), (4, 0), (-4, 2)], closed=False),
            ],
            (
                Port("in", -6.0, -4.0, 180.0),
                Port("out", 6.0, 2.0, 0.0),
                Port("flue", 0.0, 10.0, 90.0),
            ),
            (-6, -8, 6, 10),
        )
    raise ValueError(f"unknown heat-transfer equipment {symbol!r}")


def _misc(symbol: str, shape: str = "concentric") -> Body:
    if symbol == "filter":
        return (
            [polyline([(-4, -4), (4, -4), (4, 4), (-4, 4)]), line(-4, 4, 4, -4)],
            (_IN, _OUT),
            (-4, -4, 4, 4),
        )
    if symbol == "strainer_y":
        # The run is the top edge (y = 0); the strainer leg hangs below it.
        return (
            [line(-4, 0, 4, 0), line(0, 0, 3, -4), line(2, -4.7, 4, -3.3)],
            (_IN, _OUT),
            (-4, -5, 4, 0),
        )
    if symbol == "reducer":
        if shape == "eccentric":
            # Flat bottom, the outlet centreline dropped to y = -1.
            return (
                [polyline([(-3, -2), (3, -2), (3, 0), (-3, 2)])],
                (Port("in", -3.0, 0.0, 180.0), Port("out", 3.0, -1.0, 0.0)),
                (-3, -2, 3, 2),
            )
        if shape != "concentric":
            raise ValueError(
                f"reducer: shape must be one of {', '.join(REDUCER_SHAPES)}, got {shape!r}"
            )
        return (
            [polyline([(-3, -2), (3, -1), (3, 1), (-3, 2)])],
            (Port("in", -3.0, 0.0, 180.0), Port("out", 3.0, 0.0, 0.0)),
            (-3, -2, 3, 2),
        )
    if symbol == "flange_pair":
        return (
            [line(-0.6, -2.5, -0.6, 2.5), line(0.6, -2.5, 0.6, 2.5)],
            (Port("in", -0.6, 0.0, 180.0), Port("out", 0.6, 0.0, 0.0)),
            (-0.6, -2.5, 0.6, 2.5),
        )
    if symbol == "spectacle_blind":
        # Open ring on top, filled (blind) disc below, joined by the web line.
        return (
            [
                line(0, -3.5, 0, 3.5),
                circle(0, 2, 1.5),
                solid([(0, -0.5), (1.5, -2), (0, -3.5), (-1.5, -2)]),
            ],
            (Port("in", -1.5, 0.0, 180.0), Port("out", 1.5, 0.0, 0.0)),
            (-1.5, -3.5, 1.5, 3.5),
        )
    if symbol == "orifice_plate":
        return (
            [line(-0.5, -3, -0.5, 3), line(0.5, -3, 0.5, 3)],
            (
                Port("in", -0.5, 0.0, 180.0),
                Port("out", 0.5, 0.0, 0.0),
                Port("signal", 0.0, 3.0, 90.0, "signal"),
            ),
            (-0.5, -3, 0.5, 3),
        )
    if symbol == "drain_vent":
        # A stub hanging off a line: the single port is where the line meets it.
        return (
            [line(0, 0, 0, -4), line(-1.5, -4, 1.5, -4)],
            (Port("in", 0.0, 0.0, 90.0),),
            (-1.5, -4, 1.5, 0),
        )
    raise ValueError(f"unknown inline equipment {symbol!r}")


def build_equipment(symbol: str, shape: str = "concentric") -> SymbolSpec:
    """The SymbolSpec of one equipment symbol; ``shape`` applies to ``reducer`` only."""
    if symbol in ROTATING:
        family, (prims, ports, box) = "rotating", _rotating(symbol)
    elif symbol in HEAT:
        family, (prims, ports, box) = "heat", _heat(symbol)
    elif symbol in MISC:
        family, (prims, ports, box) = "misc", _misc(symbol, shape)
    else:
        raise ValueError(
            f"unknown equipment {symbol!r}; valid: {', '.join(ROTATING + HEAT + MISC)}"
        )
    variant = shape if symbol == "reducer" else None
    name = f"PID_{family.upper()}_{symbol.upper()}" + (f"_{variant.upper()}" if variant else "")
    return make_spec(
        name, family, symbol, variant, prims, tag_attdefs(box[3]), ports, SOURCE, bbox=box
    )


def _equipment_builder(symbol: str):
    def build() -> SymbolSpec:
        return build_equipment(symbol)

    return build


def _reducer_builder(shape: str = "concentric") -> SymbolSpec:
    return build_equipment("reducer", shape)


for _symbol in ROTATING + HEAT + MISC:
    if _symbol == "reducer":
        register(_symbol, "misc", _reducer_builder, variants={"shape": list(REDUCER_SHAPES)})
    elif _symbol in ROTATING:
        register(_symbol, "rotating", _equipment_builder(_symbol))
    elif _symbol in HEAT:
        register(_symbol, "heat", _equipment_builder(_symbol))
    else:
        register(_symbol, "misc", _equipment_builder(_symbol))
