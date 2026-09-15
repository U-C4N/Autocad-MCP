# engineering/pid/symbols_valves.py
"""Valve bodies (ISO 10628-2) composed with actuators (ISA-5.1 Table 5.5).

Every body is authored once in symbol-local mm (reference size 8 × 4, process
ports on the ``in`` / ``out`` faces) and any actuator stacks on top of it along
the stem at x = 0: the stem starts where the body's own geometry on that axis
ends (``axis_top``) and the actuator glyph sits ``STEM_CLEARANCE`` above the
body's top edge, so a tall body (needle, diaphragm) pushes the actuator up
instead of being overprinted by it. An actuated valve gains a ``signal`` port
on its topmost edge and a ``FAIL`` attribute; check and relief valves take no
actuator.
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
# Gap between a body's top edge and the actuator glyph — the visible stem on the
# reference 8 × 4 body (stem 0..4, glyph from y = 4).
STEM_CLEARANCE = 2.0
# Extra stem (the yoke) between the clearance point and a diaphragm dome's base.
DIAPHRAGM_YOKE = 2.0

# (primitives, process ports, declared bbox, axis_top) — ``axis_top`` is the y
# where the body's own geometry on the x = 0 axis ends, i.e. where an actuator
# stem may start without overprinting it.
Body = tuple[list[dict], tuple[Port, ...], tuple[float, float, float, float], float]


def _body(body: str) -> Body:
    """Primitives, process ports, declared bbox and axis top of a bare valve body."""
    std = [_LEFT, _RIGHT]
    if body == "gate":
        return std, (_IN, _OUT), _BOX, 0.0
    if body == "globe":
        return std + [circle(0, 0, 1.0)], (_IN, _OUT), _BOX, 1.0
    if body == "ball":
        return std + [circle(0, 0, 1.8)], (_IN, _OUT), _BOX, 1.8
    if body == "butterfly":
        return std + [line(0, -2, 0, 2), circle(0, 0, 0.8)], (_IN, _OUT), _BOX, 2.0
    if body == "check":
        return std + [arc(0, 0, 2.5, 20, 160)], (_IN, _OUT), (-4.0, -2.0, 4.0, 2.5), 2.5
    if body == "needle":
        prims = std + [line(0, 0, 0, 5), circle(0, 5, 0.6)]
        return prims, (_IN, _OUT), (-4.0, -2.0, 4.0, 5.6), 5.6
    if body == "plug":
        return std + [polyline([(-1, -1), (1, -1), (1, 1), (-1, 1)])], (_IN, _OUT), _BOX, 1.0
    if body == "diaphragm":
        return std + [arc(0, 2, 2, 0, 180)], (_IN, _OUT), (-4.0, -2.0, 4.0, 4.0), 4.0
    if body == "three_way":
        ports = (_IN, _OUT, Port("branch", 0.0, -4.0, 270.0))
        return std + [_BOTTOM], ports, (-4.0, -4.0, 4.0, 2.0), 0.0
    if body == "angle":
        ports = (_IN, Port("out", 0.0, -4.0, 270.0))
        return [_LEFT, _BOTTOM], ports, (-4.0, -4.0, 2.0, 2.0), 0.0
    if body == "relief":
        spring = polyline(
            [(0, 0), (-1, 1.5), (1, 2.5), (-1, 3.5), (1, 4.5), (0, 6)],
            closed=False,
        )
        ports = (Port("in", 0.0, -4.0, 270.0), _OUT)
        return [_BOTTOM, _RIGHT, spring], ports, (-2.0, -4.0, 4.0, 6.0), 6.0
    raise ValueError(f"unknown valve body {body!r}; valid: {', '.join(VALVE_BODIES)}")


def _actuator(
    actuator: str, body_top: float, axis_top: float = 0.0
) -> tuple[list[dict], Port | None, float]:
    """Primitives above the body, the signal port (or None), and the new top y.

    The stem (always the first primitive) runs from ``axis_top`` — where the
    body's axial geometry ends — to the glyph base at
    ``body_top + STEM_CLEARANCE``; every glyph's lowest edge sits exactly on
    the stem's top, so an actuator is never drawn detached from, or over, its
    body.
    """
    if actuator == "none":
        return [], None, body_top
    base = body_top + STEM_CLEARANCE
    if actuator == "hand":
        return [line(0, axis_top, 0, base), line(-2.5, base, 2.5, base)], None, base
    if actuator == "diaphragm":
        dome = base + DIAPHRAGM_YOKE
        prims = [line(0, axis_top, 0, dome), arc(0, dome, 3, 0, 180), line(-3, dome, 3, dome)]
        return prims, Port("signal", 0.0, dome + 3.0, 90.0, "signal"), dome + 3.0
    stem = line(0, axis_top, 0, base)
    if actuator == "piston":
        top = base + 4.0
        prims = [stem, polyline([(-2, base), (2, base), (2, top), (-2, top)])]
        return prims, Port("signal", 0.0, top, 90.0, "signal"), top
    if actuator == "motor":
        top = base + 4.0
        prims = [stem, circle(0, base + 2.0, 2), text("M", 0, base + 2.0, 2.0)]
        return prims, Port("signal", 0.0, top, 90.0, "signal"), top
    if actuator == "solenoid":
        top = base + 3.0
        box = polyline([(-1.5, base), (1.5, base), (1.5, top), (-1.5, top)])
        prims = [stem, box, text("S", 0, base + 1.5, 1.8)]
        return prims, Port("signal", 0.0, top, 90.0, "signal"), top
    raise ValueError(f"unknown actuator {actuator!r}; valid: {', '.join(ACTUATORS)}")


def build_valve(body: str = "gate", actuator: str = "none") -> SymbolSpec:
    """Compose ``body`` with ``actuator`` into a ``SymbolSpec``.

    Raises ``ValueError`` for an unknown body or actuator, and for an actuator
    on a check or relief valve (they are self-acting).
    """
    prims, ports, box, axis_top = _body(body)
    if actuator not in ACTUATORS:
        raise ValueError(f"unknown actuator {actuator!r}; valid: {', '.join(ACTUATORS)}")
    if actuator != "none" and body in NO_ACTUATOR:
        raise ValueError(f"{body} valves take no actuator (got {actuator!r})")
    glyph_base = box[3] + STEM_CLEARANCE
    extra, signal, top = _actuator(actuator, box[3], axis_top)
    prims = list(prims) + extra
    ports = tuple(ports) + ((signal,) if signal else ())
    if extra:
        # An actuator can be wider than a narrow body (angle: xmax 2 vs. a hand
        # bar at ±2.5 or a diaphragm at ±3), so union its real extent, not just y.
        ax0, ay0, ax1, ay1 = bbox_of(extra)
        box = (min(box[0], ax0), min(box[1], ay0), max(box[2], ax1), max(box[3], top, ay1))
    attdefs = list(tag_attdefs(box[3]))
    if signal is not None:
        # Beside the actuator glyph (spec 4.7): right of its widest extent (±3
        # for a dome), 1.5 above the glyph base.
        attdefs.append(attdef("FAIL", 3.5, glyph_base + 1.5, DESC_HEIGHT, align="left"))
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
