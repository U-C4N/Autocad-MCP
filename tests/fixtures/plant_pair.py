"""The synthetic plant pair: a schematic P&ID and its layout, with known answers.

Every track H reader is measured on this pair and never on a client drawing
(spec §14). It reproduces each field finding of the design spec (§1, §15) with
invented names and invented numbers:

* **Tags in both drawings** - T101..T104, M11, CP-1 in room 1; T201..T203, M12,
  CP-2 in room 2. T105 is on the P&ID only, so the run to it is not routable.
* **The P&ID is not to scale** - room 1 is the layout stretched 1.3x about the
  origin, room 2 is rearranged outright. The generator computes the scale
  statistics of its own placement (pairs at least 2 m apart, the ratio of their
  distances, the share within ±10 % of the median) and refuses to write a pair
  that would not read as ``schematic``.
* **Service layers in English and Dutch** - ``P_product piping``,
  ``P_cipsupplyline``, ``P_cipreturnline``, ``P_ijswater`` (ice water, a utility
  outside the default takeoff scope), valves and the reducer on ``KLEPPEN``.
* **Russian supply / return words** - ПОДАЧА beside the CIP supply runs,
  ВОЗВРАТ beside the returns, and one return (``CR2``) drawn on the *supply*
  layer: only the word says which way it runs.
* **Size labels as drafters write them** - ``{\\fArial|b0;Ø51}`` (an MTEXT format
  run), ``Ø51``, ``%%c38``, ``\\U+2205 38``, ``⌀38``, ``ø38``, ``SMS51``, ``DN20``,
  ``DN25``, ``20x27``; each 300 mm from the segment it names and at least 600 mm
  from every other run (the generator checks it).
* **Fittings on runs** - a reducer (``VERLOOPSTUK``) between Ø51 and Ø38 on
  ``PR3``, inline valves (``KLEP_VLINDER``) on ``PR2`` and ``CS2``; the pipe stops
  at each fitting's box and continues on its far side.
* **A double-drawn segment** - on ``PR2`` a LINE lies exactly over an LWPOLYLINE
  segment; it must count once.
* **Electrical texts in Russian** - power, phases, voltage, N and PE, ЧРП (VFD)
  1500 mm right of each load's tag; the heater on T202 states no power.
* **The layout lies about its units** - ``$INSUNITS = 1`` (inches) over
  millimetre geometry; one stray LINE 5,000 km away; two copies of the plan
  100 m apart, so every tag is on the layout twice and is unique only within a
  copy.
* **Rooms** - wall outlines (200 mm walls) and labels ``ROOM 1 PROCESS HALL`` /
  ``ПОМЕЩЕНИЕ 2 МОЙКА``; ``Wiring to CP1`` style MULTILEADER callouts from each
  load to its panel, in four spellings.

Truth values are **computed here from the positions placed here** - Manhattan
distances, the rectilinear minimum spanning tree of each run's tags, the cable
lengths rounded up after the allowance - never typed. A position on the layout
is the insertion point of the tag's TEXT, which is also the centre of the
equipment circle it labels, so "the tag's position" and "the equipment's
position" are one point by construction.
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.math import Vec2
from ezdxf.render.mleader import ConnectionSide

Pt = tuple[float, float]

__all__ = ["ALLOWANCE", "LABEL_OFFSET", "build_plant_pair"]

#: The takeoffs' default allowance; the truth's ROUNDUP metres use it.
ALLOWANCE = 0.20
#: How far a size label or a service word sits from the segment it names.
LABEL_OFFSET = 300.0
#: The stretch of room 1 on the P&ID.
ROOM1_FACTOR = 1.3
#: The second plan copy on the layout is this far to the right of the first.
COPY_OFFSET: Pt = (100_000.0, 0.0)
#: The stray entity on the layout.
OUTLIER_START: Pt = (-5.0e9, 3.0e9)
#: Tank, pump and panel sizes (P&ID radii / layout radii / panel box).
PID_TANK_R, PID_PUMP_R = 600.0, 300.0
LAYOUT_TANK_R, LAYOUT_PUMP_R = 1000.0, 400.0
PANEL_W, PANEL_H = 1000.0, 400.0
TEXT_H = 250.0

#: Tag positions on the layout (first copy), mm. The P&ID positions follow.
LAYOUT_TAGS: dict[str, Pt] = {
    "T101": (3000.0, 3000.0),
    "T102": (8000.0, 3500.0),
    "T103": (13500.0, 3000.0),
    "T104": (17000.0, 8500.0),
    "M11": (5500.0, 8200.0),
    "CP-1": (1200.0, 10800.0),
    "T201": (23500.0, 3200.0),
    "T202": (28700.0, 3000.0),
    "T203": (33000.0, 8800.0),
    "M12": (26300.0, 8400.0),
    "CP-2": (35000.0, 1300.0),
}
ROOM1_TAGS = ("T101", "T102", "T103", "T104", "M11", "CP-1")
#: Room 2 on the P&ID: rearranged, not scaled.
PID_ROOM2: dict[str, Pt] = {
    "T201": (25000.0, 3000.0),
    "T202": (29000.0, 14000.0),
    "T203": (25500.0, 11000.0),
    "M12": (32000.0, 4000.0),
    "CP-2": (28000.0, 7500.0),
}
#: On the P&ID only: the run to it cannot be routed on the layout.
PID_ONLY = {"T105": (22100.0, 16500.0)}

ROOMS = (
    {"number": "1", "name": "PROCESS HALL", "box": (0.0, 0.0, 20000.0, 12000.0)},
    {
        "number": "2",
        "name": "МОЙКА",
        "box": (20200.0, 0.0, 36000.0, 12000.0),
    },
)
ROOM_LABELS = (
    ("ROOM 1 PROCESS HALL", (10000.0, 11000.0)),
    (
        "ПОМЕЩЕНИЕ 2 МОЙКА",
        (28000.0, 11000.0),
    ),
)
OUTER_WALL = (-200.0, -200.0, 36200.0, 12200.0)

SUPPLY_WORD = "ПОДАЧА"
RETURN_WORD = "ВОЗВРАТ"

#: (tag, the Russian electrical text beside it, stated kW or None, VFD).
LOADS = (
    ("M11", "Насос P = 5,5 кВт\\P3 ф. 400 В + N + PE", 5.5, False),
    ("T102", "Мешалка P = 2,2 кВт\\P3 ф. 400 В + N + PE", 2.2, False),
    ("M12", "Насос CIP P = 7,5 кВт\\P3 ф. 400 В + N + PE\\PЧРП", 7.5, True),
    ("T202", "Нагреватель\\P3 ф. 400 В + N + PE", None, False),
)
#: (load tag, callout text on the layout, the panel it names).
CALLOUTS = (
    ("M11", "Wiring to CP1", "CP-1"),
    ("T102", "Wiring to CP 1", "CP-1"),
    ("M12", "Wiring to CP-2", "CP-2"),
    ("T202", "WIRING TO CP2", "CP-2"),
)
ELECTRICAL_DX = 1500.0
CALLOUT_DX, CALLOUT_DY = 1500.0, 1500.0


@dataclass(frozen=True)
class _Run:
    """One pipe run of the P&ID and what the takeoffs must find for it."""

    id: str
    layer: str
    service: str
    diameter: str
    tags: tuple[str, ...]
    status: str
    paths: tuple[tuple[Pt, ...], ...]
    labels: tuple[tuple[str, str, Pt, str], ...]  # (TEXT | MTEXT, as drawn, insert, as read)
    arcs: tuple[tuple[Pt, float, float, float], ...] = ()  # centre, radius, start, end (deg)
    fittings: tuple[tuple[str, Pt, float], ...] = ()  # block, centre, rotation
    words: tuple[tuple[str, Pt], ...] = ()
    duplicate: tuple[Pt, Pt] | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


RUNS: tuple[_Run, ...] = (
    _Run(
        "PR1",
        "P_product piping",
        "product",
        "Ø51",
        ("T101", "T102"),
        "A",
        paths=(((4500.0, 4200.0), (9800.0, 4200.0)),),
        labels=(("MTEXT", "{\\fArial|b0;Ø51}", (7150.0, 4500.0), "Ø51"),),
    ),
    _Run(
        "PR2",
        "P_product piping",
        "product",
        "SMS51",
        ("M11", "T102", "T103"),
        "B",
        paths=(
            ((10400.0, 5150.0), (10400.0, 7000.0)),
            ((7150.0, 7000.0), (13850.0, 7000.0)),
            ((14150.0, 7000.0), (17250.0, 7000.0)),
            ((17550.0, 6700.0), (17550.0, 4500.0)),
            ((7150.0, 7000.0), (7150.0, 10360.0)),
        ),
        labels=(("TEXT", "SMS51", (10700.0, 6075.0), "SMS51"),),
        arcs=(((17250.0, 6700.0), 300.0, 0.0, 90.0),),
        fittings=(("KLEP_VLINDER", (14000.0, 7000.0), 0.0),),
        duplicate=((14150.0, 7000.0), (17250.0, 7000.0)),
        notes=("T-junction at (10400, 7000)", "valve continued", "one segment drawn twice"),
    ),
    _Run(
        "PR3",
        "P_product piping",
        "product",
        "Ø51",
        ("T103", "T104"),
        "A",
        paths=(
            ((18150.0, 3900.0), (20000.0, 3900.0), (20000.0, 7000.0)),
            ((20000.0, 7300.0), (20000.0, 11050.0), (21500.0, 11050.0)),
        ),
        labels=(
            ("TEXT", "Ø51", (19075.0, 4200.0), "Ø51"),
            ("TEXT", "Ø51", (20300.0, 5450.0), "Ø51"),
            ("TEXT", "%%c38", (20300.0, 9175.0), "Ø38"),
            ("TEXT", "%%c38", (20750.0, 11350.0), "Ø38"),
        ),
        fittings=(("VERLOOPSTUK", (20000.0, 7150.0), 90.0),),
        notes=("reducer between the two sizes",),
    ),
    _Run(
        "PR4",
        "P_product piping",
        "product",
        "DN20",
        ("T104", "T105"),
        "C",
        paths=(((22100.0, 11650.0), (22100.0, 15900.0)),),
        labels=(("TEXT", "DN20", (22400.0, 13775.0), "DN20"),),
        notes=("T105 is not on the layout",),
    ),
    _Run(
        "CS1",
        "P_cipsupplyline",
        "cip_supply",
        "Ø38",
        ("T101", "T201"),
        "A",
        paths=(((25000.0, 2400.0), (25000.0, 1500.0), (3900.0, 1500.0), (3900.0, 3300.0)),),
        labels=(
            ("MTEXT", "\\U+2205 38", (25300.0, 1950.0), "Ø38"),
            ("MTEXT", "\\U+2205 38", (14450.0, 1800.0), "Ø38"),
            ("MTEXT", "\\U+2205 38", (4200.0, 2400.0), "Ø38"),
        ),
        words=((SUPPLY_WORD, (14450.0, 1200.0)),),
    ),
    _Run(
        "CS2",
        "P_cipsupplyline",
        "cip_supply",
        "20x27",
        ("M12", "T201"),
        "B",
        paths=(
            ((25600.0, 3000.0), (27850.0, 3000.0)),
            ((28150.0, 3000.0), (32000.0, 3000.0), (32000.0, 3700.0)),
        ),
        labels=(("TEXT", "20x27", (26725.0, 3300.0), "20x27"),),
        fittings=(("KLEP_VLINDER", (28000.0, 3000.0), 0.0),),
        words=((SUPPLY_WORD, (30075.0, 2700.0)),),
    ),
    _Run(
        "CR1",
        "P_cipreturnline",
        "cip_return",
        "Ø38",
        ("T101", "T202"),
        "A",
        paths=(
            (
                (3300.0, 3900.0),
                (2500.0, 3900.0),
                (2500.0, 19000.0),
                (29000.0, 19000.0),
                (29000.0, 14600.0),
            ),
        ),
        labels=(
            ("TEXT", "⌀38", (2900.0, 4200.0), "Ø38"),
            ("TEXT", "⌀38", (2200.0, 11450.0), "Ø38"),
            ("TEXT", "⌀38", (15750.0, 19300.0), "Ø38"),
            ("TEXT", "⌀38", (29300.0, 16800.0), "Ø38"),
        ),
        words=((RETURN_WORD, (15750.0, 18700.0)),),
    ),
    _Run(
        "CR2",
        "P_cipsupplyline",
        "cip_return",
        "Ø38",
        ("T202", "T203"),
        "A",
        paths=(((25500.0, 11600.0), (25500.0, 14000.0), (28400.0, 14000.0)),),
        labels=(
            ("TEXT", "ø38", (25200.0, 12800.0), "Ø38"),
            ("TEXT", "ø38", (26950.0, 14300.0), "Ø38"),
        ),
        words=((RETURN_WORD, (26950.0, 13700.0)),),
        notes=("a return drawn on the supply layer",),
    ),
    _Run(
        "UT1",
        "P_ijswater",
        "ice_water",
        "DN25",
        ("M12", "T203"),
        "A",
        paths=(((32000.0, 4300.0), (32000.0, 11000.0), (26100.0, 11000.0)),),
        labels=(
            ("TEXT", "DN25", (32300.0, 7650.0), "DN25"),
            ("TEXT", "DN25", (29050.0, 11300.0), "DN25"),
        ),
        notes=("a utility: outside the default takeoff scope",),
    ),
)

LAYER_SERVICES = {
    "P_product piping": "product",
    "P_cipsupplyline": "cip_supply",
    "P_cipreturnline": "cip_return",
    "P_ijswater": "ice_water",
    "E_power": "electrical",
}


def pid_tags() -> dict[str, Pt]:
    """Tag positions on the P&ID: room 1 stretched, room 2 rearranged, T105 alone."""
    out = {
        tag: (ROOM1_FACTOR * x, ROOM1_FACTOR * y)
        for tag, (x, y) in LAYOUT_TAGS.items()
        if tag in ROOM1_TAGS
    }
    out.update(PID_ROOM2)
    out.update(PID_ONLY)
    return out


def manhattan(a: Pt, b: Pt) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def rmst_length(points: list[Pt]) -> float:
    """Rectilinear minimum spanning tree length (Prim, Manhattan weights)."""
    if len(points) < 2:
        return 0.0
    inside = {0}
    total = 0.0
    while len(inside) < len(points):
        best = min(
            (manhattan(points[i], points[j]), j)
            for i in inside
            for j in range(len(points))
            if j not in inside
        )
        total += best[0]
        inside.add(best[1])
    return total


def scale_statistics(pid: dict[str, Pt], layout: dict[str, Pt], tags) -> dict:
    """Spec §6 over ``tags``: pairs at least 2 m apart on both drawings."""
    ratios = []
    for a, b in itertools.combinations(sorted(tags), 2):
        on_layout = math.dist(layout[a], layout[b])
        on_pid = math.dist(pid[a], pid[b])
        if on_layout >= 2000.0 and on_pid >= 2000.0:
            ratios.append(on_pid / on_layout)
    median = statistics.median(ratios)
    within = sum(1 for r in ratios if abs(r - median) <= 0.1 * median) / len(ratios)
    return {"pairs": len(ratios), "median": median, "within_10pct": within}


def _segments(run: _Run) -> list[tuple[Pt, Pt]]:
    out = []
    for path in run.paths:
        out.extend(zip(path, path[1:], strict=False))
    return out


def _segment_distance(p: Pt, a: Pt, b: Pt) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _arc_length(arc) -> float:
    _centre, radius, start, end = arc
    return radius * math.radians((end - start) % 360.0)


def _check_labels() -> None:
    """Every label names its own segment at LABEL_OFFSET and no other run within 2x."""
    for run in RUNS:
        own = _segments(run)
        others = [s for other in RUNS if other is not run for s in _segments(other)]
        for _kind, _raw, at, _read in run.labels:
            mine = min(_segment_distance(at, a, b) for a, b in own)
            theirs = min(_segment_distance(at, a, b) for a, b in others)
            if abs(mine - LABEL_OFFSET) > 1e-9 or theirs <= 2 * LABEL_OFFSET:
                raise AssertionError(f"{run.id} label at {at}: own {mine}, other run {theirs}")
        if run.status == "A":
            for a, b in own:
                near = min(_segment_distance(lab[2], a, b) for lab in run.labels)
                if abs(near - LABEL_OFFSET) > 1e-9:
                    raise AssertionError(f"{run.id}: segment {a}-{b} has no direct label")


def _segment_diameters(run: _Run) -> dict[str, float]:
    """Drawn length per size: a segment takes the label 300 mm from it (direct),
    else the run's size (continuity); arcs are continuity."""
    lengths: dict[str, float] = {}
    for a, b in _segments(run):
        size = run.diameter
        for _kind, _raw, at, read in run.labels:
            if abs(_segment_distance(at, a, b) - LABEL_OFFSET) <= 1e-9:
                size = read
                break
        lengths[size] = lengths.get(size, 0.0) + math.dist(a, b)
    for arc in run.arcs:
        lengths[run.diameter] = lengths.get(run.diameter, 0.0) + _arc_length(arc)
    return lengths


def _blocks(doc) -> None:
    tank = doc.blocks.new("TANK_V")
    tank.add_circle((0.0, 0.0), PID_TANK_R)
    pump = doc.blocks.new("POMP")
    pump.add_circle((0.0, 0.0), PID_PUMP_R)
    pump.add_lwpolyline([(-150.0, -150.0), (150.0, 0.0), (-150.0, 150.0)], close=True)
    panel = doc.blocks.new("SCHAKELKAST")
    panel.add_lwpolyline(
        [
            (-PANEL_W / 2, -PANEL_H / 2),
            (PANEL_W / 2, -PANEL_H / 2),
            (PANEL_W / 2, PANEL_H / 2),
            (-PANEL_W / 2, PANEL_H / 2),
        ],
        close=True,
    )
    valve = doc.blocks.new("KLEP_VLINDER")
    valve.add_lwpolyline(
        [(-150.0, -100.0), (150.0, 100.0), (150.0, -100.0), (-150.0, 100.0)], close=True
    )
    reducer = doc.blocks.new("VERLOOPSTUK")
    reducer.add_lwpolyline(
        [(-150.0, -100.0), (150.0, -60.0), (150.0, 60.0), (-150.0, 100.0)], close=True
    )


def _write_pid(path: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    for name in (*LAYER_SERVICES, "KLEPPEN", "TEKST", "TAGS", "EQUIPMENT"):
        if name not in doc.layers:
            doc.layers.add(name)
    _blocks(doc)
    msp = doc.modelspace()
    for tag, at in pid_tags().items():
        block = (
            "SCHAKELKAST" if tag.startswith("CP") else "POMP" if tag.startswith("M") else "TANK_V"
        )
        msp.add_blockref(block, at, dxfattribs={"layer": "EQUIPMENT"})
        msp.add_text(tag, dxfattribs={"insert": at, "height": TEXT_H, "layer": "TAGS"})
    for run in RUNS:
        attribs = {"layer": run.layer}
        for pts in run.paths:
            # a two-point path is a LINE - except the one drawn twice, which is
            # an LWPOLYLINE, so its duplicate is a line lying over a polyline
            if len(pts) == 2 and tuple(pts) != run.duplicate:
                msp.add_line(pts[0], pts[1], dxfattribs=attribs)
            else:
                msp.add_lwpolyline(pts, dxfattribs=attribs)
        for centre, radius, start, end in run.arcs:
            msp.add_arc(centre, radius, start, end, dxfattribs=attribs)
        if run.duplicate is not None:
            msp.add_line(run.duplicate[0], run.duplicate[1], dxfattribs=attribs)
        for block, at, rotation in run.fittings:
            msp.add_blockref(block, at, dxfattribs={"layer": "KLEPPEN", "rotation": rotation})
        for kind, raw, at, _read in run.labels:
            if kind == "MTEXT":
                msp.add_mtext(
                    raw, dxfattribs={"insert": at, "char_height": TEXT_H, "layer": "TEKST"}
                )
            else:
                msp.add_text(raw, dxfattribs={"insert": at, "height": TEXT_H, "layer": "TEKST"})
        for word, at in run.words:
            msp.add_text(word, dxfattribs={"insert": at, "height": TEXT_H, "layer": "TEKST"})
    tags = pid_tags()
    for tag, text, _kw, _vfd in LOADS:
        x, y = tags[tag]
        msp.add_mtext(
            text,
            dxfattribs={
                "insert": (x + ELECTRICAL_DX, y),
                "char_height": TEXT_H,
                "layer": "E_power",
            },
        )
    _set_extents(doc)
    doc.saveas(path)


def _set_extents(doc) -> None:
    """Declare the model-space extents the way AutoCAD would: over everything.

    ezdxf copies the model-space layout's ``extmin`` / ``extmax`` into
    ``$EXTMIN`` / ``$EXTMAX`` when it saves (``Drawing.update_extents``), so the
    layout's attributes are what is set; a header write alone is overwritten.
    """
    msp = doc.modelspace()
    box = ezbbox.extents(msp, fast=True)
    msp.dxf.extmin = (box.extmin.x, box.extmin.y, 0.0)
    msp.dxf.extmax = (box.extmax.x, box.extmax.y, 0.0)


def _rect(msp, box, layer) -> None:
    x0, y0, x1, y1 = box
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
        msp.add_line(a, b, dxfattribs={"layer": layer})


def _write_layout(path: Path) -> str:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 1  # inches - the geometry is millimetres
    for name in ("WALLS", "ROOMS", "EQUIPMENT", "TAGS", "E-PANEL", "E-CALLOUT"):
        doc.layers.add(name)
    msp = doc.modelspace()
    for copy in range(2):
        ox, oy = COPY_OFFSET[0] * copy, COPY_OFFSET[1] * copy

        def at(p: Pt, ox=ox, oy=oy) -> Pt:
            return (p[0] + ox, p[1] + oy)

        x0, y0, x1, y1 = OUTER_WALL
        _rect(msp, (x0 + ox, y0 + oy, x1 + ox, y1 + oy), "WALLS")
        for room in ROOMS:
            bx0, by0, bx1, by1 = room["box"]
            _rect(msp, (bx0 + ox, by0 + oy, bx1 + ox, by1 + oy), "WALLS")
        for text, where in ROOM_LABELS:
            msp.add_text(text, dxfattribs={"insert": at(where), "height": TEXT_H, "layer": "ROOMS"})
        for tag, where in LAYOUT_TAGS.items():
            centre = at(where)
            if tag.startswith("CP"):
                cx, cy = centre
                msp.add_lwpolyline(
                    [
                        (cx - PANEL_W / 2, cy - PANEL_H / 2),
                        (cx + PANEL_W / 2, cy - PANEL_H / 2),
                        (cx + PANEL_W / 2, cy + PANEL_H / 2),
                        (cx - PANEL_W / 2, cy + PANEL_H / 2),
                    ],
                    close=True,
                    dxfattribs={"layer": "E-PANEL"},
                )
            else:
                radius = LAYOUT_PUMP_R if tag.startswith("M") else LAYOUT_TANK_R
                msp.add_circle(centre, radius, dxfattribs={"layer": "EQUIPMENT"})
            msp.add_text(tag, dxfattribs={"insert": centre, "height": TEXT_H, "layer": "TAGS"})
        for tag, text, _panel in CALLOUTS:
            tip = at(LAYOUT_TAGS[tag])
            builder = msp.add_multileader_mtext("Standard", dxfattribs={"layer": "E-CALLOUT"})
            builder.set_content(text, char_height=TEXT_H)
            builder.add_leader_line(ConnectionSide.left, [Vec2(tip)])
            builder.build(insert=Vec2(tip[0] + CALLOUT_DX, tip[1] + CALLOUT_DY))
    stray = msp.add_line(OUTLIER_START, (OUTLIER_START[0] + 1000.0, OUTLIER_START[1]))
    _set_extents(doc)
    doc.saveas(path)
    return str(stray.dxf.handle)


def build_plant_pair(directory: str | Path) -> dict:
    """Write ``pid.dxf`` and ``layout.dxf`` into ``directory``; return the truth.

    Refuses (``AssertionError``) to write a pair that would not test what it
    claims: a label nearer another run than its own, a status-A segment with no
    direct label, a P&ID that is not schematic overall or not 1.3x in room 1,
    or a cable length whose rounding sits on an integer.
    """
    _check_labels()
    layout_tags = dict(LAYOUT_TAGS)
    pid = pid_tags()
    overall = scale_statistics(pid, layout_tags, LAYOUT_TAGS)
    room1 = scale_statistics(pid, layout_tags, ROOM1_TAGS)
    room2 = scale_statistics(pid, layout_tags, [t for t in LAYOUT_TAGS if t not in ROOM1_TAGS])
    if overall["within_10pct"] > 0.5 or room1["within_10pct"] != 1.0:
        raise AssertionError(f"the pair does not read as schematic: {overall}, {room1}")

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    pid_path, layout_path = out / "pid.dxf", out / "layout.dxf"
    _write_pid(pid_path)
    outlier = _write_layout(layout_path)

    pipe_runs = []
    for run in RUNS:
        routable = all(tag in layout_tags for tag in run.tags)
        pieces = sum(math.dist(a, b) for a, b in _segments(run)) + sum(
            _arc_length(a) for a in run.arcs
        )
        pipe_runs.append(
            {
                "id": run.id,
                "layer": run.layer,
                "service": run.service,
                "diameter": run.diameter,
                "diameters": _segment_diameters(run),
                "tags": sorted(run.tags),
                "status": run.status,
                "pid_length_mm": pieces,
                "layout_length_mm": rmst_length([layout_tags[t] for t in run.tags])
                if routable
                else None,
            }
        )

    cable_rows = []
    for (tag, _text, kw, vfd), (callout_tag, _callout, panel) in zip(LOADS, CALLOUTS, strict=True):
        assert tag == callout_tag
        length = manhattan(layout_tags[tag], layout_tags[panel])
        metres = length / 1000.0 * (1.0 + ALLOWANCE)
        if abs(metres - round(metres)) < 1e-6:
            raise AssertionError(f"{tag}: {metres} m sits on an integer; move the tag")
        cable_rows.append(
            {
                "tag": tag,
                "panel": panel,
                "kw": kw,
                "phases": 3,
                "voltage": 400,
                "vfd": vfd,
                "length_mm": length,
                "roundup_m": math.ceil(metres),
            }
        )

    rooms = []
    for room in ROOMS:
        x0, y0, x1, y1 = room["box"]
        rooms.append(
            {
                "number": room["number"],
                "name": room["name"],
                "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
            }
        )

    return {
        "pid": str(pid_path),
        "layout": str(layout_path),
        "scale_verdict": "schematic",
        "layout_insunits": 1,
        "layout_true_unit": "mm",
        "pid_insunits": 4,
        "outlier_handle": outlier,
        "cluster_count": 2,
        "copy_offset": list(COPY_OFFSET),
        "layer_services": dict(LAYER_SERVICES),
        "pipe_runs": pipe_runs,
        "cable_rows": cable_rows,
        "rooms": rooms,
        "allowance": ALLOWANCE,
        "label_offset_mm": LABEL_OFFSET,
        "tag_positions": {"layout": layout_tags, "pid": pid},
        "scale": {
            "overall": overall,
            "room_1": room1,
            "room_2": room2,
            "room_1_factor": ROOM1_FACTOR,
        },
    }
