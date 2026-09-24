"""Pipe takeoff rows: topology from the P&ID, lengths from the layout (spec §8).

A P&ID says which equipment a line joins, what it carries and how big it is;
it does not say how long the pipe is unless the scale check calls it to scale.
So each run found by `engineering.understand.network` is measured one of two
ways, and the rows say which:

* **layout** - the positions of the tags the run joins, on the layout, joined
  by the rectilinear minimum spanning tree (`route.rmst`), plus
  `vertical_allowance` metres per tree edge. The P&ID's drawn length is kept
  beside it as `schematic_m`, a comparison column, never the answer.
* **pid** - the drawn length on the P&ID: divided by the scale factor when the
  scale check calls it `to_scale`, taken in the P&ID's own unit when there is
  no layout (every row then carries `scale_verified: false`), and on a
  schematic P&ID only with `force=True`.

Units, plan copies, scale and rooms are read through group U's readers
(`describe.drawing_units`, `describe.prepare`, `scale.scale_check`,
`describe.find_rooms`), so a takeoff sees exactly what `drawing_understand`
and `drawing_scale_check` report for the same drawing.

Every length leaves this module in metres, converted from the unit the
geometry implies - never blindly from `INSUNITS`. Nothing here touches a
drawing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from engineering.arch.faces import face_containing
from engineering.understand.describe import FACE_TOL_FRACTION, drawing_units, find_rooms, prepare
from engineering.understand.labels import normalize_panel, parse_electrical, wiring_target
from engineering.understand.network import (
    MODEL,
    TEXT_TYPES,
    label_search_default,
    tag_occurrences,
)
from engineering.understand.route import manhattan, rmst, route_legs
from engineering.understand.scale import scale_check
from engineering.understand.snapshot import Pt, Snapshot
from engineering.understand.vocab import SERVICES

__all__ = [
    "CABLE_SEARCH_SHARE",
    "DEFAULT_SERVICES",
    "LENGTH_SOURCES",
    "UNIT_METRES",
    "apportion",
    "cable_rows",
    "check_allowance",
    "check_pipe_options",
    "check_section_rules",
    "layout_tags",
    "pipe_rows",
    "room_at",
    "room_regions",
    "roundup_m",
    "section_for",
    "unit_of",
]

#: The user's own takeoff scope; utilities only on request (spec §8).
DEFAULT_SERVICES = ("product", "cip_supply", "cip_return")
LENGTH_SOURCES = ("auto", "pid", "layout")
UNASSIGNED = "unassigned"

#: Metres per unit of each unit a takeoff converts.
UNIT_METRES = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}

# -- options -----------------------------------------------------------------


def _number(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: expected a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{name}: must be finite, got {value!r}")
    return number


def check_allowance(allowance) -> float:
    number = _number(allowance, "allowance")
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"allowance: must lie between 0 and 1 (0.20 is 20 %), got {number:g}")
    return number


def check_pipe_options(*, services, allowance, vertical_allowance, length_source) -> dict:
    """Refuse bad takeoff options by name, before a drawing is read."""
    chosen = DEFAULT_SERVICES if services is None else tuple(dict.fromkeys(services))
    if not chosen:
        raise ValueError("services: an empty list takes nothing off; omit it for the default")
    unknown = [s for s in chosen if s not in SERVICES]
    if unknown:
        raise ValueError(
            f"services: {', '.join(map(str, unknown))} unknown; choose from {', '.join(SERVICES)}"
        )
    vertical = _number(vertical_allowance, "vertical_allowance")
    if vertical < 0.0:
        raise ValueError(f"vertical_allowance: must be zero or more metres, got {vertical:g}")
    if length_source not in LENGTH_SOURCES:
        raise ValueError(
            f"length_source: {length_source!r} unknown; choose from {', '.join(LENGTH_SOURCES)}"
        )
    return {
        "services": chosen,
        "allowance": check_allowance(allowance),
        "vertical_allowance": vertical,
        "length_source": length_source,
    }


# -- units -------------------------------------------------------------------


def unit_of(snap: Snapshot, *, prep: dict | None = None) -> dict:
    """The unit a drawing's geometry implies - `describe.drawing_units`, the reading
    `drawing_understand` and `drawing_scale_check` give - in the shape a takeoff reads.

    ``{"declared", "inferred", "m_per_unit", "warning", "evidence"}``: ``declared``
    is the INSUNITS abbreviation (None when it names none of mm / cm / m / in /
    ft), ``evidence`` is `infer_units`' own list. When no measurement settles
    the unit the declared one stands, else mm, and the warning says which; a
    takeoff never leaves a length in unknown units.
    """
    found = drawing_units(snap, prep=prep)
    name = found["declared"]["name"]
    declared = name if name in UNIT_METRES else None
    inferred, warning = found["inferred"], found["warning"]
    if inferred is None:
        inferred = declared or "mm"
        warning = (
            f"INSUNITS declares {found['declared']['label']} ({found['declared']['code']}) and "
            f"no measurement settles the unit; the takeoff reads it as {inferred}."
        )
    return {
        "declared": declared,
        "inferred": inferred,
        "m_per_unit": UNIT_METRES[inferred],
        "warning": f"{snap.source}: {warning}" if warning else None,
        "evidence": found["evidence"],
    }


# -- plan copies and tag positions ---------------------------------------------


def layout_tags(snap: Snapshot, wanted: Iterable[str] = (), *, prep: dict | None = None) -> dict:
    """Tag positions on a drawing, using only tags that are unique where they are read.

    When no tag is written twice the whole model space is read. Otherwise the
    drawing's clusters - `describe.prepare`'s, the plan copies, sheets and
    outliers `drawing_understand` reports - are read, and the cluster holding
    the most `wanted` tags is chosen (ties: the one furthest left, then lowest); a tag written more than once inside it is `ambiguous`,
    never picked arbitrarily. ``{"positions": {tag: Pt}, "ambiguous": [...],
    "cluster": box | None, "clusters": n}``.
    """
    occurrences = tag_occurrences(snap)
    wanted = set(wanted)
    if all(len(places) == 1 for places in occurrences.values()):
        return {
            "positions": {tag: places[0]["at"] for tag, places in occurrences.items()},
            "ambiguous": [],
            "cluster": None,
            "clusters": 1,
        }
    prep = prep or prepare(snap)
    boxes = [tuple(cluster["box"]) for cluster in prep["clusters"]]

    def inside(p: Pt, box) -> bool:
        return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]

    def weight(box) -> tuple:
        count = sum(
            1
            for tag, places in occurrences.items()
            if tag in wanted and any(inside(place["at"], box) for place in places)
        )
        return (-count, box[0], box[1])

    chosen = min(boxes, key=weight) if boxes else None
    positions, ambiguous = {}, []
    for tag, places in sorted(occurrences.items()):
        here = [place for place in places if chosen is None or inside(place["at"], chosen)]
        if len(here) == 1:
            positions[tag] = here[0]["at"]
        elif len(here) > 1:
            ambiguous.append(tag)
    return {
        "positions": positions,
        "ambiguous": ambiguous,
        "cluster": chosen,
        "clusters": len(boxes),
    }


# -- rooms -------------------------------------------------------------------


def room_regions(snap: Snapshot, cluster=None, *, prep: dict | None = None) -> dict:
    """Room faces and labels - `describe.find_rooms`, the rooms `drawing_understand`
    reports - inside ``cluster`` (a box) when one is given.

    ``{"faces": [(key, Face)], "labels": [(key, Pt)]}``; the key is the room
    number, else its name. A record belongs to the cluster when every point
    it has lies in the box.
    """
    prep = prep or prepare(snap)

    def keep(rec) -> bool:
        return cluster is None or all(
            cluster[0] <= p[0] <= cluster[2] and cluster[1] <= p[1] <= cluster[3]
            for p in rec.points
        )

    tol = prep["diag"] * FACE_TOL_FRACTION if prep["diag"] > 0 else 1.0
    found = find_rooms(
        [rec for rec in prep["kept"] if keep(rec)], tol=tol, membership=prep["membership"]
    )

    def key(row: dict) -> str:
        return str(row["number"] or row["name"] or row["text"])

    # without a face a piece goes to the nearest label: when the drawing numbers
    # its rooms, an unnumbered mention ('to room storage') is a note, not a room
    numbered = [row for row in found["rooms"] if row["number"]]
    candidates = numbered or found["rooms"]
    return {
        "faces": [(key(row), face) for row, face in found["labelled"]],
        "labels": sorted((key(row), (row["at"][0], row["at"][1])) for row in candidates),
    }


def _rooms_mode(regions: dict) -> str:
    if regions["faces"]:
        return "polygons"
    return "nearest_label" if regions["labels"] else "none"


def room_at(regions: dict, p: Pt) -> str:
    """The room a point stands in; else the nearest room label; else ''."""
    face = face_containing([face for _key, face in regions["faces"]], p)
    if face is not None:
        return next(key for key, f in regions["faces"] if f is face)
    if regions["labels"]:
        return min(regions["labels"], key=lambda item: (math.dist(item[1], p), item[0]))[0]
    return ""


def apportion(legs, regions: dict) -> dict[str, float]:
    """Length of each orthogonal leg inside each room polygon (drawing units).

    A leg is cut at every crossing with a room outline or hole; each piece goes
    to the room its midpoint stands in, or to '' outside every room. Nothing is
    counted twice: the pieces add up to the leg.
    """
    faces = [face for _key, face in regions["faces"]]
    key_of = {id(face): key for key, face in regions["faces"]}
    out: dict[str, float] = {}
    for a, b in legs:
        length = math.dist(a, b)
        if length == 0.0:
            continue
        cuts = {0.0, 1.0}
        for face in faces:
            for ring in (face.loop, *face.holes):
                for i in range(len(ring)):
                    t = _cut(a, b, ring[i], ring[(i + 1) % len(ring)])
                    if t is not None:
                        cuts.add(t)
        ts = sorted(cuts)
        for t0, t1 in zip(ts, ts[1:], strict=False):
            if t1 - t0 <= 0.0:
                continue
            tm = (t0 + t1) / 2.0
            mid = (a[0] + (b[0] - a[0]) * tm, a[1] + (b[1] - a[1]) * tm)
            face = face_containing(faces, mid)
            key = key_of[id(face)] if face is not None else ""
            out[key] = out.get(key, 0.0) + (t1 - t0) * length
    return out


def _cut(a: Pt, b: Pt, c: Pt, d: Pt) -> float | None:
    """Parameter along a->b where it crosses segment c-d, or None."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denom = rx * sy - ry * sx
    if denom == 0.0:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = (qx * sy - qy * sx) / denom
    u = (qx * ry - qy * rx) / denom
    if 0.0 < t < 1.0 and 0.0 <= u <= 1.0:
        return t
    return None


# -- pipe rows ---------------------------------------------------------------


def _choose_source(requested: str, layout, scale: dict, force: bool) -> tuple[str, str]:
    verdict = scale.get("verdict")
    if requested == "layout":
        if layout is None:
            raise ValueError("length_source='layout' needs a layout drawing; none was given")
        return "layout", "requested"
    if requested == "pid":
        if layout is not None and verdict == "schematic" and not force:
            raise ValueError(
                "length_source='pid': the scale check calls this P&ID schematic - "
                f"{scale['within_10pct']:.0%} of {scale['pair_count']} tag pairs lie within "
                f"±10 % of the median ratio {scale['median']:.6g} (IQR {scale['iqr']:.6g}); "
                "a length measured on it is drawn line, not pipe. Pass force=True to measure "
                "on it anyway, or use length_source='layout'."
            )
        return "pid", "forced" if layout is not None and verdict == "schematic" else "requested"
    if layout is None:
        return "pid", "no_layout"
    if verdict == "to_scale":
        return "pid", "auto_to_scale"
    return "layout", f"auto_{verdict}"


def _row(rows: dict, room: str, service: str, diameter: str) -> dict:
    return rows.setdefault(
        (room, service, diameter),
        {
            "room": room,
            "service": service,
            "diameter": diameter,
            "direct_m": 0.0,
            "continuity_m": 0.0,
            "unassigned_m": 0.0,
            "net_m": 0.0,
            "schematic_m": 0.0,
            "status": "A",
            "runs": [],
        },
    )


def _pid_cells(edges, regions: dict, m_per_unit: float) -> dict:
    """{(room, diameter): {net, direct, continuity, unassigned}} in metres, piece by piece.

    With room faces a piece is cut at the outlines (`apportion` on its chord,
    scaled to the piece's true length, so a bend keeps its arc length); without
    faces the whole piece goes to the room label nearest its midpoint.
    """
    cells: dict[tuple[str, str], dict] = {}
    for edge in edges:
        a, b = edge["a"], edge["b"]
        metres = edge["length"] * m_per_unit
        pieces = apportion([(a, b)], regions) if regions["faces"] else {}
        chord = sum(pieces.values())
        if chord > 0.0:
            shares = {room: value / chord for room, value in pieces.items()}
        else:
            shares = {room_at(regions, ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)): 1.0}
        diameter = edge["diameter"] or UNASSIGNED
        for room, share in shares.items():
            cell = cells.setdefault(
                (room, diameter), {"net": 0.0, "direct": 0.0, "continuity": 0.0, "unassigned": 0.0}
            )
            cell["net"] += metres * share
            cell[edge["diameter_source"]] += metres * share
    return cells


def _reasons(flagged: list[dict]) -> str:
    counts: dict[str, int] = {}
    for run in flagged:
        counts[run["reason"]] = counts.get(run["reason"], 0) + 1
    return ", ".join(f"{reason} {n}" for reason, n in sorted(counts.items()))


def pipe_rows(
    network,
    pid: Snapshot,
    layout: Snapshot | None,
    *,
    scale=None,
    services=DEFAULT_SERVICES,
    allowance=0.20,
    vertical_allowance=0.0,
    length_source="auto",
    force=False,
) -> dict:
    """Takeoff rows by room x service x diameter, with the runs, checks and method behind them.

    `network` is `build_network(pid)`'s result. `scale` is a `scale_check`
    result; omitted, `scale_check(pid, layout)` runs when a layout is given. Refused by name before anything is measured: an unknown service
    or length source, an allowance outside 0-1, a negative vertical
    allowance, `length_source='layout'` without a layout, and
    `length_source='pid'` on a P&ID the scale check calls schematic unless
    `force=True` (the refusal quotes the statistics).

    Status per run: **A** every piece's diameter read directly; **B** a
    diameter carried by continuity or unassigned; **C**, on the layout only,
    not routable - an end on no equipment, fewer than two tags, a tag missing
    from (or written twice on) the layout. A C run goes to `control` with its
    schematic length only, is counted in the totals and is flagged, and a
    warning says how much could not be routed. Measured on the P&ID a run needs
    no equipment at its ends - the drawn geometry is the length - and each
    piece goes to the P&ID room it lies in (cut at the room outlines when the
    P&ID has room faces, else the nearest room label). On the layout a run
    holding several diameters shares its routed length among them in
    proportion to their drawn length on the P&ID.
    """
    options = check_pipe_options(
        services=services,
        allowance=allowance,
        vertical_allowance=vertical_allowance,
        length_source=length_source,
    )
    allowance = options["allowance"]
    vertical = options["vertical_allowance"]
    prep_pid = prepare(pid)
    prep_layout = prepare(layout) if layout is not None else None
    pid_unit = unit_of(pid, prep=prep_pid)
    layout_unit = unit_of(layout, prep=prep_layout) if layout is not None else None
    wanted = {tag for run in network["runs"] for tag in run.tags}
    found = layout_tags(layout, wanted, prep=prep_layout) if layout is not None else None
    if scale is None:
        scale = (
            scale_check(pid, layout)
            if layout is not None
            else {"verdict": "not_checked", "pair_count": 0, "matched": [], "ambiguous": []}
        )
    source, reason = _choose_source(length_source, layout, scale, bool(force))
    scale_verified = source == "layout" or scale.get("verdict") == "to_scale"
    positions = found["positions"] if found else {}
    ambiguous = set(found["ambiguous"]) if found else set()
    regions = (
        room_regions(layout, found["cluster"], prep=prep_layout)
        if found is not None and source == "layout"
        else room_regions(pid, prep=prep_pid)
    )
    m_pid = pid_unit["m_per_unit"]
    m_layout = layout_unit["m_per_unit"] if layout_unit else None

    rows: dict[tuple[str, str, str], dict] = {}
    runs_out, control, excluded = [], [], {}
    for run in network["runs"]:
        if run.service not in options["services"]:
            excluded[run.service] = excluded.get(run.service, 0) + 1
            continue
        ends = network["attachments"].get(run.id, [])
        drawn = sum(edge["length"] for edge in run.edges)
        schematic_m = drawn * m_pid
        parts: dict[str, dict] = {}
        for edge in run.edges:
            part = parts.setdefault(
                edge["diameter"] or UNASSIGNED,
                {"drawn": 0.0, "direct": 0.0, "continuity": 0.0, "unassigned": 0.0},
            )
            part["drawn"] += edge["length"]
            part[edge["diameter_source"]] += edge["length"]
        why = None
        if source == "layout":
            if any(end["tag"] is None for end in ends) or not ends:
                why = "end_not_on_equipment"
            elif len(run.tags) < 2:
                why = "single_tag"
            else:
                missing = [tag for tag in run.tags if tag not in positions]
                if missing:
                    why = (
                        "tag_ambiguous"
                        if any(t in ambiguous for t in missing)
                        else "tag_not_on_layout"
                    )
        detail = {
            "id": run.id,
            "service": run.service,
            "tags": list(run.tags),
            "diameters": {d: p["drawn"] * m_pid for d, p in sorted(parts.items())},
            "schematic_m": schematic_m,
            "layout_m": None,
            "vertical_m": 0.0,
            "length_m": None,
            "rooms": {},
            "status": "C",
            "reason": why,
        }
        runs_out.append(detail)
        for diameter, part in sorted(parts.items()):
            if diameter == UNASSIGNED:
                control.append(
                    {
                        "run": run.id,
                        "kind": "unassigned_diameter",
                        "service": run.service,
                        "tags": list(run.tags),
                        "schematic_m": part["drawn"] * m_pid,
                        "reason": "no_diameter_label",
                    }
                )
        if why is not None:
            control.append(
                {
                    "run": run.id,
                    "kind": "unroutable",
                    "service": run.service,
                    "tags": list(run.tags),
                    "schematic_m": schematic_m,
                    "reason": why,
                }
            )
            continue
        if source == "layout":
            points = [positions[tag] for tag in run.tags]
            length_units, tree = rmst(points)
            if regions["faces"]:
                legs = [leg for i, j in tree for leg in route_legs(points[i], points[j])]
                rooms = {key: value * m_layout for key, value in apportion(legs, regions).items()}
            else:
                centre = (
                    sum(p[0] for p in points) / len(points),
                    sum(p[1] for p in points) / len(points),
                )
                rooms = {room_at(regions, centre): length_units * m_layout}
            for _i, j in tree:
                key = room_at(regions, points[j])
                rooms[key] = rooms.get(key, 0.0) + vertical
            detail["layout_m"] = length_units * m_layout
            detail["vertical_m"] = vertical * len(tree)
            length_m = detail["layout_m"] + detail["vertical_m"]
        else:
            factor = (
                scale["factor"]
                if layout is not None and scale.get("verdict") == "to_scale"
                else 1.0
            )
            length_m = schematic_m / factor
            cells = _pid_cells(run.edges, regions, m_pid / factor)
            rooms = {}
            for (room, _diameter), cell in cells.items():
                rooms[room] = rooms.get(room, 0.0) + cell["net"]
        detail["length_m"] = length_m
        detail["rooms"] = rooms
        status = (
            "A"
            if all(p["continuity"] == 0.0 and p["unassigned"] == 0.0 for p in parts.values())
            else "B"
        )
        detail["status"] = status
        if source == "pid":
            # measured piece by piece: each cell is one room and one diameter
            for (room, diameter), cell in cells.items():
                row = _row(rows, room, run.service, diameter)
                row["net_m"] += cell["net"]
                row["direct_m"] += cell["direct"]
                row["continuity_m"] += cell["continuity"]
                row["unassigned_m"] += cell["unassigned"]
                row["schematic_m"] += cell["net"] * factor
                if cell["continuity"] or cell["unassigned"]:
                    row["status"] = "B"
                if run.id not in row["runs"]:
                    row["runs"].append(run.id)
            continue
        for diameter, part in parts.items():
            share = part["drawn"] / drawn if drawn else 0.0
            for room, room_m in rooms.items():
                x = room_m * share
                row = _row(rows, room, run.service, diameter)
                row["net_m"] += x
                row["direct_m"] += x * part["direct"] / part["drawn"]
                row["continuity_m"] += x * part["continuity"] / part["drawn"]
                row["unassigned_m"] += x * part["unassigned"] / part["drawn"]
                row["schematic_m"] += schematic_m * share * (room_m / length_m if length_m else 0.0)
                if part["continuity"] or part["unassigned"]:
                    row["status"] = "B"
                if run.id not in row["runs"]:
                    row["runs"].append(run.id)

    order = {service: i for i, service in enumerate(SERVICES)}
    table = []
    for key in sorted(rows, key=lambda k: (k[0], order.get(k[1], len(order)), k[2])):
        row = rows[key]
        row["allowance"] = allowance
        row["allowance_m"] = row["net_m"] * allowance
        row["total_m"] = row["net_m"] + row["allowance_m"]
        table.append(row)

    routed = sum(row["net_m"] for row in table)
    unroutable = sum(c["schematic_m"] for c in control if c["kind"] == "unroutable")
    net_total = routed + unroutable
    summary = {
        name: [
            {
                "key": key,
                "net_m": sum(r["net_m"] for r in table if r[field] == key),
                "total_m": sum(r["total_m"] for r in table if r[field] == key),
            }
            for key in sorted({r[field] for r in table})
        ]
        for name, field in (
            ("by_service", "service"),
            ("by_diameter", "diameter"),
            ("by_room", "room"),
        )
    }
    warnings = [u["warning"] for u in (pid_unit, layout_unit) if u and u["warning"]]
    dropped = int(network.get("stats", {}).get("segments_below_tol") or 0)
    if dropped:
        warnings.append(
            f"{dropped} P&ID segment(s) shorter than the junction tolerance "
            f"({network['stats']['tol']:g} drawing units) collapsed to a point and are not "
            "counted"
        )
    flagged = [r for r in runs_out if r["status"] == "C"]
    if source == "layout" and flagged:
        warnings.append(
            f"{len(flagged)} of {len(runs_out)} runs could not be routed on the layout "
            f"({_reasons(flagged)}); their {unroutable:.1f} m drawn on the P&ID are in the "
            "control rows, not in the table. length_source='pid' with force=True measures "
            "every run on the P&ID instead, and says the scale is not verified."
        )
    if not scale_verified:
        warnings.append(
            "scale_verified: false - lengths are drawn lengths on a P&ID that no scale check "
            "has confirmed; they are not physical pipe lengths"
        )
    return {
        "kind": "pipe",
        "rows": table,
        "runs": runs_out,
        "control": control,
        "summary": summary,
        "totals": {
            "routed_m": routed,
            "unroutable_m": unroutable,
            "net_m": net_total,
            "allowance": allowance,
            "allowance_m": net_total * allowance,
            "total_m": net_total * (1.0 + allowance),
            "flagged_runs": sum(1 for r in runs_out if r["status"] == "C"),
        },
        "length_source": source,
        "scale": scale,
        "scale_verified": scale_verified,
        "units": {"pid": pid_unit, "layout": layout_unit},
        "warnings": warnings,
        "method": {
            "sources": {"pid": pid.source, "layout": layout.source if layout else None},
            "length_source": source,
            "requested": length_source,
            "reason": reason,
            "allowance": allowance,
            "vertical_allowance": vertical,
            "services": list(options["services"]),
            "excluded_services": excluded,
            "tol": network["stats"]["tol"],
            "label_search": network["stats"]["label_search"],
            "overlap_length_m": network["stats"]["overlap_length"] * m_pid,
            "route": "rectilinear MST of the tag positions; each connection x first, then y",
            "rooms": _rooms_mode(regions),
        },
    }


# -- cable rows ----------------------------------------------------------------

#: A load's power block stands under its symbol rather than on a line, so its
#: electrical texts and its wiring callout are searched twice as far as a
#: diameter label: 10 x the median text height.
CABLE_SEARCH_SHARE = 2.0
SECTION_RULE_KEYS = ("max_kw", "section", "phases")


def check_section_rules(section_rules) -> tuple[dict, ...]:
    """The caller's own cable-section rule table, validated and sorted by max_kw.

    Each rule is ``{"max_kw": > 0, "section": text, "phases": 1 | 3 (optional)}``;
    a load takes the first rule whose max_kw it does not exceed and whose
    phases, when the rule names them, equal the load's. No standard table exists
    in this repository: without rules no section is proposed.
    """
    if section_rules is None:
        return ()
    if not isinstance(section_rules, (list, tuple)):
        raise ValueError("section_rules: expected a list of {max_kw, section[, phases]}")
    rules = []
    for i, rule in enumerate(section_rules):
        where = f"section_rules[{i}]"
        if not isinstance(rule, dict):
            raise ValueError(f"{where}: expected {{max_kw, section[, phases]}}, got {rule!r}")
        unknown = sorted(set(rule) - set(SECTION_RULE_KEYS))
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(map(str, unknown))}")
        max_kw = _number(rule.get("max_kw"), f"{where}.max_kw")
        if max_kw <= 0.0:
            raise ValueError(f"{where}.max_kw: must be greater than zero, got {max_kw:g}")
        section = rule.get("section")
        if not isinstance(section, str) or not section.strip():
            raise ValueError(
                f"{where}.section: expected a non-empty text such as '5x2.5', got {section!r}"
            )
        phases = rule.get("phases")
        if phases is not None and phases not in (1, 3):
            raise ValueError(f"{where}.phases: 1 or 3, got {phases!r}")
        rules.append({"max_kw": max_kw, "section": section.strip(), "phases": phases})
    return tuple(sorted(rules, key=lambda r: (r["max_kw"], r["phases"] or 0)))


def section_for(kw: float | None, phases: int | None, rules) -> str | None:
    """The first rule the load fits, or None - never a guessed section."""
    if kw is None:
        return None
    for rule in rules:
        if kw <= rule["max_kw"] and (rule["phases"] is None or rule["phases"] == phases):
            return rule["section"]
    return None


def roundup_m(length_m: float, allowance: float) -> int:
    """ROUNDUP(length x (1 + allowance)) to the metre, on the value rounded to 1e-6 m first.

    The rounding keeps binary noise from buying a metre of cable: 50 m with a
    10 % allowance is 55.00000000000001 in floating point, and 55 m on the sheet.
    """
    return math.ceil(round(length_m * (1.0 + allowance), 6))


def _nearest_tag(at: Pt, places: dict[str, list[Pt]], search: float) -> str | None:
    found = [
        (math.dist(at, p), tag)
        for tag, points in places.items()
        for p in points
        if math.dist(at, p) <= search
    ]
    return min(found)[1] if found else None


def _inside_box(p: Pt, box) -> bool:
    return box is None or (box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3])


def cable_rows(
    pid: Snapshot, layout: Snapshot | None, *, allowance=0.20, section_rules=None
) -> dict:
    """One row per electrical load, the cable method made repeatable (spec §9).

    Loads are the P&ID tags with electrical texts near them (`parse_electrical`:
    kW, phases, voltage, + N, VFD) and the tags a wiring callout names. The
    panel comes from the layout's callouts (`wiring_target`: 'Wiring to CP1'
    -> CP-1), else the P&ID's. The length is the Manhattan distance on the
    layout from the load's tag to its panel's tag, and the cable is
    `roundup_m(length, allowance)`. **Power is never invented**: a load with no
    stated kW keeps an empty cell and an open item. With no layout, lengths
    stay empty and every row gets an open item - the P&ID is not a length
    source for cables. A load wired to a panel that itself states a power is
    part of that package: room and panel totals count the package's power and
    not its children's. Sections come only from `section_rules`.

    Refused by name before anything is read: an allowance outside 0-1 and a
    malformed section rule.
    """
    allowance = check_allowance(allowance)
    rules = check_section_rules(section_rules)
    pid_places = {tag: [p["at"] for p in places] for tag, places in tag_occurrences(pid).items()}
    pid_search = CABLE_SEARCH_SHARE * label_search_default(pid)

    loads: dict[str, list] = {}
    orphans: list[str] = []
    pid_callouts: dict[str, str] = {}
    for rec in pid.records:
        if rec.space != MODEL or rec.type not in TEXT_TYPES or not rec.text or not rec.points:
            continue
        at = rec.points[0]
        target = wiring_target(rec.text)
        if target is not None:
            tag = _nearest_tag(at, pid_places, pid_search)
            if tag is not None:
                pid_callouts.setdefault(tag, target)
            continue
        found = parse_electrical(rec.text)
        if found["kw"] is None and not (found["phases"] or found["voltage"] or found["vfd"]):
            continue
        tag = _nearest_tag(at, pid_places, pid_search)
        if tag is None:
            orphans.append(rec.handle)
            continue
        distance = min(math.dist(at, p) for p in pid_places[tag])
        loads.setdefault(tag, []).append((distance, rec.handle, found))

    positions: dict[str, Pt] = {}
    ambiguous: set[str] = set()
    panels: dict[str, tuple[str, str]] = {}
    prep_layout = prepare(layout) if layout is not None else None
    layout_unit = unit_of(layout, prep=prep_layout) if layout is not None else None
    cluster = None
    layout_search = None
    if layout is not None:
        placed = layout_tags(layout, set(loads) | set(pid_callouts), prep=prep_layout)
        positions, ambiguous, cluster = (
            placed["positions"],
            set(placed["ambiguous"]),
            placed["cluster"],
        )
        layout_search = CABLE_SEARCH_SHARE * label_search_default(layout)
        layout_places = {tag: [p] for tag, p in positions.items()}
        for rec in layout.records:
            if rec.space != MODEL or rec.type not in TEXT_TYPES or not rec.text or not rec.points:
                continue
            target = wiring_target(rec.text)
            if target is None or not _inside_box(rec.points[0], cluster):
                continue
            tag = _nearest_tag(rec.points[0], layout_places, layout_search)
            if tag is not None:
                panels.setdefault(tag, (target, "layout"))
    for tag, target in pid_callouts.items():
        panels.setdefault(tag, (target, "pid"))
    panel_at = {}
    for tag, p in positions.items():
        name = normalize_panel(tag)
        if name is not None:
            panel_at[name] = p
    regions = (
        room_regions(layout, cluster, prep=prep_layout) if layout is not None else room_regions(pid)
    )

    rows, items = [], []
    for tag in sorted(set(loads) | set(panels)):
        texts = sorted(loads.get(tag, []), key=lambda item: item[:2])
        powers = [(d, f["kw"]) for d, _h, f in texts if f["kw"] is not None]
        kw = powers[0][1] if powers else None
        if len({value for _d, value in powers}) > 1:
            items.append(
                {
                    "tag": tag,
                    "item": "conflicting_power",
                    "detail": ", ".join(f"{value:g} kW" for _d, value in powers),
                }
            )
        phases = next((f["phases"] for _d, _h, f in texts if f["phases"]), None)
        voltage = next((f["voltage"] for _d, _h, f in texts if f["voltage"]), None)
        panel, panel_source = panels.get(tag, (None, None))
        row = {
            "tag": tag,
            "room": "",
            "at": None,
            "kw": kw,
            "phases": phases,
            "voltage": voltage,
            "neutral": any(f["neutral"] for _d, _h, f in texts),
            "vfd": any(f["vfd"] for _d, _h, f in texts),
            "panel": panel,
            "panel_source": panel_source,
            "length_m": None,
            "allowance": allowance,
            "cable_m": None,
            "section": section_for(kw, phases, rules),
            "package": None,
        }
        if kw is None:
            items.append(
                {
                    "tag": tag,
                    "item": "missing_power",
                    "detail": "no power is stated near the tag on the P&ID",
                }
            )
        if panel is None:
            items.append(
                {
                    "tag": tag,
                    "item": "missing_panel",
                    "detail": "no wiring callout names a panel for this tag",
                }
            )
        if rules and kw is not None and row["section"] is None:
            items.append(
                {"tag": tag, "item": "no_section_rule", "detail": f"no rule covers {kw:g} kW"}
            )
        if layout is None:
            items.append(
                {
                    "tag": tag,
                    "item": "no_layout",
                    "detail": "no layout given: the P&ID is not a length source for cables",
                }
            )
            if pid_places.get(tag):
                row["room"] = room_at(regions, pid_places[tag][0])
        else:
            here = positions.get(tag)
            if here is None:
                items.append(
                    {
                        "tag": tag,
                        "item": "tag_ambiguous" if tag in ambiguous else "tag_not_on_layout",
                        "detail": "the load's tag cannot be placed on the layout",
                    }
                )
            else:
                row["at"] = here
                row["room"] = room_at(regions, here)
            if panel is not None and panel not in panel_at:
                items.append(
                    {
                        "tag": tag,
                        "item": "panel_not_on_layout",
                        "detail": f"{panel} is not written once on the layout",
                    }
                )
            if here is not None and panel in panel_at:
                row["length_m"] = manhattan(here, panel_at[panel]) * layout_unit["m_per_unit"]
                row["cable_m"] = roundup_m(row["length_m"], allowance)
        rows.append(row)

    stated = {
        (normalize_panel(r["tag"]) or r["tag"]): r["tag"] for r in rows if r["kw"] is not None
    }
    for row in rows:
        owner = stated.get(row["panel"]) if row["panel"] else None
        if owner is not None and owner != row["tag"]:
            row["package"] = owner

    def counted(row: dict) -> float:
        return row["kw"] if row["kw"] is not None and row["package"] is None else 0.0

    summary = {}
    for name, field in (("by_room", "room"), ("by_panel", "panel")):
        summary[name] = [
            {
                "key": key,
                "loads": sum(1 for r in rows if (r[field] or "") == key),
                "kw": sum(counted(r) for r in rows if (r[field] or "") == key),
                "cable_m": sum(r["cable_m"] or 0 for r in rows if (r[field] or "") == key),
            }
            for key in sorted({row[field] or "" for row in rows})
        ]
    units = {"pid": unit_of(pid), "layout": layout_unit}
    warnings = [unit["warning"] for unit in units.values() if unit and unit["warning"]]
    if orphans:
        warnings.append(f"electrical text(s) near no tag, not used: {', '.join(orphans)}")
    return {
        "kind": "cable",
        "rows": rows,
        "open_items": items,
        "summary": summary,
        "totals": {
            "loads": len(rows),
            "known_kw": sum(counted(r) for r in rows),
            "cable_m": sum(r["cable_m"] or 0 for r in rows),
            "open_items": len(items),
            "packages": sum(1 for r in rows if r["package"]),
        },
        "units": units,
        "warnings": warnings,
        "method": {
            "sources": {"pid": pid.source, "layout": layout.source if layout else None},
            "allowance": allowance,
            "search": {"pid": pid_search, "layout": layout_search},
            "section_rules": [dict(rule) for rule in rules],
            "rooms": _rooms_mode(regions),
        },
    }
