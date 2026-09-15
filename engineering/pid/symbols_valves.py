# engineering/pid/symbols_valves.py
"""Valve bodies (ISO 10628-2) composed with actuators (ISA-5.1 Table 5.5).

Every body is authored once in symbol-local mm (reference size 8 × 4, process
ports on the ``in`` / ``out`` faces) and any actuator stacks on top of it along
the stem at x = 0. An actuated valve gains a ``signal`` port on its topmost
edge and a ``FAIL`` attribute; check and relief valves take no actuator.
"""

from __future__ import annotations

from .geometry import arc, attdef, bbox_of, circle, line, polyline, text
from .symbols import DESC_HEIGHT, Port, SymbolSpec, make_spec, register, tag_attdefs

VALVE_BODIES = (
    "gate",
    "globe",
    "ball",
    "butterfly",
    "check",
    "needle",
    "plug",
    "diaphragm",
    "three_way",
    "angle",
    "relief",
)
ACTUATORS = ("none", "diaphragm", "piston", "motor", "solenoid", "hand")
NO_ACTUATOR = frozenset({"check", "relief"})
SOURCE = "ISO 10628-2:2012 valves; ISA-5.1-2009 Table 5.5 actuators"

_LEFT = polyline([(-4, -2), (-4, 2), (0, 0)])
_RIGHT = polyline([(4, -2), (4, 2), (0, 0)])
_BOTTOM = polyline([(-2, -4), (2, -4), (0, 0)])
_IN = Port("in", -4.0, 0.0, 180.0)
_OUT = Port("out", 4.0, 0.0, 0.0)
_BOX = (-4.0, -2.0, 4.0, 2.0)

Body = tuple[list[dict], tuple[Port, ...], tuple[float, float, float, float]]


def _body(body: str) -> Body:
    """Primitives, process ports and declared bbox of a bare valve body."""
    std = [_LEFT, _RIGHT]
    if body == "gate":
        return std, (_IN, _OUT), _BOX
    if body == "globe":
        return std + [circle(0, 0, 1.0)], (_IN, _OUT), _BOX
    if body == "ball":
        return std + [circle(0, 0, 1.8)], (_IN, _OUT), _BOX
    if body == "butterfly":
        return std + [line(0, -2, 0, 2), circle(0, 0, 0.8)], (_IN, _OUT), _BOX
    if body == "check":
        return std + [arc(0, 0, 2.5, 20, 160)], (_IN, _OUT), (-4.0, -2.0, 4.0, 2.5)
    if body == "needle":
        return std + [line(0, 0, 0, 5), circle(0, 5, 0.6)], (_IN, _OUT), (-4.0, -2.0, 4.0, 5.6)
    if body == "plug":
        return std + [polyline([(-1, -1), (1, -1), (1, 1), (-1, 1)])], (_IN, _OUT), _BOX
    if body == "diaphragm":
        return std + [arc(0, 2, 2, 0, 180)], (_IN, _OUT), (-4.0, -2.0, 4.0, 4.0)
    if body == "three_way":
        ports = (_IN, _OUT, Port("branch", 0.0, -4.0, 270.0))
        return std + [_BOTTOM], ports, (-4.0, -4.0, 4.0, 2.0)
    if body == "angle":
        return [_LEFT, _BOTTOM], (_IN, Port("out", 0.0, -4.0, 270.0)), (-4.0, -4.0, 2.0, 2.0)
    if body == "relief":
        spring = polyline(
            [(0, 0), (-1, 1.5), (1, 2.5), (-1, 3.5), (1, 4.5), (0, 6)],
            closed=False,
        )
        ports = (Port("in", 0.0, -4.0, 270.0), _OUT)
        return [_BOTTOM, _RIGHT, spring], ports, (-2.0, -4.0, 4.0, 6.0)
    raise ValueError(f"unknown valve body {body!r}; valid: {', '.join(VALVE_BODIES)}")


def _actuator(actuator: str, body_top: float) -> tuple[list[dict], Port | None, float]:
    """Primitives above the body, the signal port (or None), and the new top y."""
    stem = line(0, 0, 0, 4)
    if actuator == "none":
        return [], None, body_top
    if actuator == "hand":
        return [stem, line(-2.5, 4, 2.5, 4)], None, max(body_top, 4.0)
    if actuator == "diaphragm":
        prims = [stem, arc(0, 6, 3, 0, 180), line(-3, 6, 3, 6)]
        return prims, Port("signal", 0.0, 9.0, 90.0, "signal"), 9.0
    if actuator == "piston":
        prims = [stem, polyline([(-2, 4), (2, 4), (2, 8), (-2, 8)])]
        return prims, Port("signal", 0.0, 8.0, 90.0, "signal"), 8.0
    if actuator == "motor":
        prims = [stem, circle(0, 6, 2), text("M", 0, 6, 2.0)]
        return prims, Port("signal", 0.0, 8.0, 90.0, "signal"), 8.0
    if actuator == "solenoid":
        prims = [stem, polyline([(-1.5, 4), (1.5, 4), (1.5, 7), (-1.5, 7)]), text("S", 0, 5.5, 1.8)]
        return prims, Port("signal", 0.0, 7.0, 90.0, "signal"), 7.0
    raise ValueError(f"unknown actuator {actuator!r}; valid: {', '.join(ACTUATORS)}")


def build_valve(body: str = "gate", actuator: str = "none") -> SymbolSpec:
    """Compose ``body`` with ``actuator`` into a ``SymbolSpec``.

    Raises ``ValueError`` for an unknown body or actuator, and for an actuator
    on a check or relief valve (they are self-acting).
    """
    prims, ports, box = _body(body)
    if actuator not in ACTUATORS:
        raise ValueError(f"unknown actuator {actuator!r}; valid: {', '.join(ACTUATORS)}")
    if actuator != "none" and body in NO_ACTUATOR:
        raise ValueError(f"{body} valves take no actuator (got {actuator!r})")
    extra, signal, top = _actuator(actuator, box[3])
    prims = list(prims) + extra
    ports = tuple(ports) + ((signal,) if signal else ())
    if extra:
        # An actuator can be wider than a narrow body (angle: xmax 2 vs. a hand
        # bar at ±2.5 or a diaphragm at ±3), so union its real extent, not just y.
        ax0, ay0, ax1, ay1 = bbox_of(extra)
        box = (min(box[0], ax0), min(box[1], ay0), max(box[2], ax1), max(box[3], top, ay1))
    attdefs = list(tag_attdefs(box[3]))
    if signal is not None:
        attdefs.append(attdef("FAIL", 3.5, 5.5, DESC_HEIGHT, align="left"))
    variant = None if actuator == "none" else actuator
    name = f"PID_VALVE_{body.upper()}" + (f"_{actuator.upper()}" if variant else "")
    return make_spec(name, "valve", body, variant, prims, attdefs, ports, SOURCE, bbox=box)


def _valve_builder(body: str):
    def build(actuator: str = "none") -> SymbolSpec:
        return build_valve(body, actuator)

    return build


for _body_name in VALVE_BODIES:
    register(
        _body_name, "valve", _valve_builder(_body_name), variants={"actuator": list(ACTUATORS)}
    )
