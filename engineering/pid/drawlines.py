"""``pid_line_draw``: resolve ports, route, draw, label, mark, tag with XDATA."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .insert import ensure_layer, insert_symbol
from .lines import (
    AXIS,
    DEFAULT_NUMBER_FORMAT,
    LINE_CLASSES,
    PID_LINE_LAYERS,
    aim_points,
    count_crossings,
    format_line_number,
    label_placement,
    marker_positions,
    route,
    snap_axis,
)
from .router import NoRouteError, Obstacle, normalise_obstacles, obstacle_hits, route_avoiding
from .symbols import Port, resolve, transform_port
from .xdata import line_payload, marker_payload, read_payload, write_payload

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

TEXT_HEIGHT = 2.5
LABEL_LAYER = "PROCESS-LINE-TEXT"
_INSERT_TYPES = {"INSERT", "BLOCKREFERENCE"}
# ezdxf reports a lightweight polyline as LWPOLYLINE; the COM engine derives the
# type from the ActiveX ObjectName, and a live LWPOLYLINE is ``AcDbPolyline``
# (measured on AutoCAD 2026 — see backends/com_backend.py), so it arrives as
# POLYLINE, with the heavy kind as 2DPOLYLINE. Same set the graph reader keeps.
_POLY_TYPES = {"LWPOLYLINE", "POLYLINE", "2DPOLYLINE"}
_LINE_TYPES = {"LINE"}
_PAGE = 1000
#: ``route`` names (a waypoint list is the fourth form). ``avoid`` is 1.8-20's
#: obstacle-aware search (``engineering/pid/router.py``); ``auto`` stays the default.
ROUTE_MODES = ("auto", "direct", "avoid")
#: What ``avoid`` does when no route keeps the clearance: refuse ``no_route``,
#: or route through the fewest boxes and report them in ``obstacle_hits``.
ON_BLOCKED = ("refuse", "cross")
#: How far (mm) an ``avoid`` route keeps from every symbol box by default.
DEFAULT_CLEARANCE = 2.0


async def resolve_endpoint(backend: AutoCADBackend, ref: dict, name: str = "endpoint") -> dict:
    """``{handle, port}`` → the port in WCS (payload ports + INSERT transform);
    ``{x, y}`` → a free point. ``name`` (``from_`` / ``to``) prefixes a refusal."""
    if not isinstance(ref, dict):
        raise ValueError("endpoint must be {handle, port} or {x, y}")
    if "handle" in ref:
        handle = str(ref["handle"])
        try:
            info = await backend.entity_get(handle)
        except RuntimeError as exc:
            # Both engines say "Entity with handle 'X' not found." as a
            # RuntimeError, which reached the client as an "Internal error"
            # (backlog W3-T13-11). Any other failure is not a caller's mistake.
            if "not found" not in str(exc):
                raise
            raise ValueError(f"{name}.handle: no entity has handle {handle!r}") from None
        block_name = str(info.properties.get("block_name", ""))
        if block_name.startswith("PID_MARKER_"):
            raise ValueError(f"{handle} is a marker/arrow — decoration on a line, not an endpoint")
        payload = await read_payload(backend, handle)
        if info.type not in _INSERT_TYPES or payload is None or payload.get("kind") != "symbol":
            raise ValueError(f"{handle} is not a P&ID symbol placed by pid_symbol_insert")
        ports = payload["ports"]
        port_name = ref.get("port")
        if port_name is None:
            if len(ports) == 1:
                port_name = next(iter(ports))
            elif payload.get("family") == "instrument" and "signal" in ports:
                port_name = "signal"
            else:
                raise ValueError(f"{handle}: choose a port: {', '.join(ports)}")
        if port_name not in ports:
            raise ValueError(f"{handle} has no port {port_name!r}; ports: {', '.join(ports)}")
        lx, ly, ldir, kind, radius = ports[port_name]
        ins = info.properties["insertion"]
        x_scale = float(info.properties.get("x_scale", 1.0))
        # The full INSERT transform: a mirrored symbol (``entity_mirror`` writes
        # ``y_scale = -1``; MIRROR3D and foreign DXFs store extrusion -Z, which
        # the engines report as ``mirrored``) has its ports on the mirrored
        # geometry, not on the unmirrored one; a stretched symbol is refused
        # by ``transform_port``.
        try:
            world = transform_port(
                Port(port_name, float(lx), float(ly), ldir, kind, float(radius)),
                float(ins[0]),
                float(ins[1]),
                float(info.properties.get("rotation_deg", 0.0)),
                x_scale,
                float(info.properties.get("y_scale", x_scale)),
                bool(info.properties.get("mirrored", False)),
            )
        except ValueError as exc:
            raise ValueError(f"{handle}: {exc}") from exc
        return {**world, "handle": handle, "port": port_name, "block_name": block_name}
    if "x" in ref and "y" in ref:
        return {
            "name": None,
            "x": float(ref["x"]),
            "y": float(ref["y"]),
            "direction_deg": None,
            "kind": None,
            "radius": 0.0,
            "inferred": False,
            "handle": None,
            "port": None,
            "block_name": None,
        }
    raise ValueError("endpoint must be {handle, port} or {x, y}")


def _vertices(info) -> list[tuple[float, float]]:
    if info.type in _LINE_TYPES:
        s, e = info.properties["start"], info.properties["end"]
        return [(float(s[0]), float(s[1])), (float(e[0]), float(e[1]))]
    return [(float(p[0]), float(p[1])) for p in info.properties.get("points") or []]


def _bulges(info, count: int) -> list[float]:
    """Per-vertex bulges as the engine reports them (both engines put a
    ``bulges`` list next to ``points``); straight when it reports none."""
    raw = info.properties.get("bulges") or []
    out = []
    for value in raw[:count]:
        try:
            out.append(float(value or 0.0))
        except (TypeError, ValueError):
            out.append(0.0)
    return out + [0.0] * (count - len(out))


def payload_port_refs(payload) -> list[tuple[str, str]]:
    """``(handle, port)`` for each well-formed ``from``/``to`` in a line payload.

    A foreign or hand-edited payload can carry anything under those keys
    (``"garbage"``, ``None``, a number); reading it used to raise
    ``TypeError`` out of ``draw_line``. Reader results never raise: a
    malformed end is simply not a reference.
    """
    refs = []
    if not isinstance(payload, dict):
        return refs
    for key in ("from", "to"):
        ref = payload.get(key)
        if not isinstance(ref, dict):
            continue
        handle, port = ref.get("handle"), ref.get("port")
        if isinstance(handle, str) and isinstance(port, str):
            refs.append((handle, port))
    return refs


def payload_seq(payload) -> int | None:
    """The payload's sequence number when it is a genuine int (``True`` is not)."""
    if not isinstance(payload, dict):
        return None
    seq = payload.get("seq")
    return seq if type(seq) is int else None


async def max_seq_elsewhere(backend: AutoCADBackend) -> int:
    """The highest line sequence on a P&ID line in any space but the current one.

    ``existing_pid_lines`` reads the current space (crossings and port reuse
    belong to the sheet being drawn on); the sequence belongs to the document.
    One read of every layout through ``entity_infos_all_spaces``, which never
    switches the current layout.
    """
    from engineering.qa_scan import entity_infos_all_spaces

    current = (await backend.layout_list()).get("current") or "Model"
    highest = 0
    for info, space in await entity_infos_all_spaces(backend):
        if space == current or info.type not in _POLY_TYPES:
            continue
        if info.layer.upper() not in PID_LINE_LAYERS:
            continue
        seq = payload_seq(await read_payload(backend, info.handle))
        if seq is not None:
            highest = max(highest, seq)
    return highest


async def existing_pid_lines(backend: AutoCADBackend) -> list[dict]:
    """Every LINE/polyline on a P&ID line layer, with its payload when it has one.

    Listed without a type filter: the two engines name a lightweight polyline
    differently (``_POLY_TYPES``), and a filter spelled for one of them is blind
    on the other — every auto-built number would restart at 1 and no crossing
    or port reuse would ever be reported there. ``vertices`` are the raw
    vertices; ``bulges`` (one per vertex, DXF convention) let the crossing test
    follow an arc instead of its chord.
    """
    lines, _symbols = await scan_sheet(backend)
    return lines


def _is_symbol_block(info) -> bool:
    """A catalogue symbol INSERT (``PID_*``), not a line marker or arrow."""
    if info.type not in _INSERT_TYPES:
        return False
    name = str(info.properties.get("block_name", ""))
    return name.startswith("PID_") and not name.startswith("PID_MARKER_")


async def scan_sheet(backend: AutoCADBackend) -> tuple[list[dict], list[dict]]:
    """One read of the current space: its P&ID lines (``existing_pid_lines``)
    and its catalogue symbols as routing obstacles ``{handle, box}`` (1.8-20).

    A symbol is an INSERT of a ``PID_*`` block other than a marker; its box is
    the drawn geometry the graph reader uses (``graph._bbox``: attributes
    excluded). Tags are not read here - only a route that reports a symbol
    reads its attributes (``obstacle_rows``).
    """
    from .graph import _bbox

    lines: list[dict] = []
    symbols: list[dict] = []
    offset = 0
    while True:
        page = await backend.entity_list(limit=_PAGE, offset=offset)
        for info in page:
            if _is_symbol_block(info):
                box = _bbox(info)
                if box is not None:
                    symbols.append({"handle": info.handle, "box": box})
                continue
            if info.type not in _POLY_TYPES | _LINE_TYPES:
                continue
            if info.layer.upper() not in PID_LINE_LAYERS:
                continue
            payload = await read_payload(backend, info.handle) if info.type in _POLY_TYPES else None
            vertices = _vertices(info)
            lines.append(
                {
                    "handle": info.handle,
                    "vertices": vertices,
                    "bulges": _bulges(info, len(vertices)),
                    "payload": payload,
                }
            )
        if len(page) < _PAGE:
            break
        offset += _PAGE
    return lines, symbols


async def symbol_tag(backend: AutoCADBackend, handle: str) -> str | None:
    """The tag a symbol states: ``FUNC-LOOP`` on an instrument, else ``TAG``."""
    attributes = await backend.block_get_attributes(handle)
    if attributes.get("FUNC"):
        return f"{attributes['FUNC']}-{str(attributes.get('LOOP') or '').strip()}".rstrip("-")
    tag = str(attributes.get("TAG") or "").strip()
    return tag or None


async def obstacle_rows(backend: AutoCADBackend, refs: list[dict]) -> list[dict]:
    """``{handle, tag}`` rows for the obstacles a route reports, the tag read
    from the drawing where the ref does not carry one already."""
    rows = []
    for ref in refs:
        row = {"handle": ref.get("handle"), "tag": ref.get("tag")}
        if "tag" not in ref and ref.get("handle"):
            row["tag"] = await symbol_tag(backend, str(ref["handle"]))
        for key in ("id", "keep_out"):
            if key in ref:
                row[key] = ref[key]
        rows.append(row)
    return rows


def keep_out_obstacles(keep_out) -> list[Obstacle]:
    """The caller's ``keep_out`` boxes ``[[xmin, ymin, xmax, ymax], ...]``."""
    if keep_out is None:
        return []
    if isinstance(keep_out, (str, bytes)) or not isinstance(keep_out, (list, tuple)):
        raise ValueError("keep_out must be a list of [xmin, ymin, xmax, ymax] boxes")
    return [
        Obstacle(o.box, {"handle": None, "tag": None, "keep_out": i})
        for i, o in enumerate(normalise_obstacles(keep_out, "keep_out"))
    ]


def line_obstacles(
    obstacles: list[Obstacle],
    ends: tuple[str | None, str | None],
    anchors: tuple[tuple[float, float], tuple[float, float]],
    clearance: float,
) -> list[Obstacle]:
    """The obstacles one line routes around: never its own end symbols (by
    handle or spec id), and never a box whose inflated outline holds a free
    ``{x, y}`` end (``ends`` entry None) - the line is meant to end there."""
    out = []
    for obstacle in obstacles:
        ref = obstacle.ref
        if any(end and end in (ref.get("handle"), ref.get("id")) for end in ends):
            continue
        x0, y0, x1, y1 = obstacle.box
        if any(
            end is None
            and x0 - clearance < px < x1 + clearance
            and y0 - clearance < py < y1 + clearance
            for end, (px, py) in zip(ends, anchors, strict=True)
        ):
            continue
        out.append(obstacle)
    return out


def plan_route(
    s_point,
    s_dir,
    e_point,
    e_dir,
    route_mode,
    stub: float,
    obstacles: list[Obstacle],
    existing: list[dict],
    clearance: float,
    on_blocked: str,
) -> tuple[list[tuple[float, float]], dict | None]:
    """The route of one line and, for ``avoid``, its search summary. The one
    function ``draw_line`` and ``pid_from_spec``'s dry run both call, so the
    preview's vertices are the applied ones."""
    if route_mode == "avoid":
        found = route_avoiding(
            s_point,
            s_dir,
            e_point,
            e_dir,
            obstacles,
            existing,
            clearance=clearance,
            stub=stub,
            on_blocked=on_blocked,
        )
        summary = {
            "clearance": clearance,
            "cost": round(found.cost, 6),
            "bends": found.bends,
            "expanded": found.expanded,
        }
        return found.vertices, summary
    return route(s_point, s_dir, e_point, e_dir, stub=stub, mode=route_mode), None


def check_routing_options(route_mode, clearance, on_blocked) -> float:
    """Refuse a bad ``clearance`` / ``on_blocked`` before anything is read."""
    if isinstance(clearance, bool):
        raise ValueError(f"clearance must be a finite length >= 0, not {clearance!r}")
    try:
        value = float(clearance)
    except (TypeError, ValueError):
        raise ValueError(f"clearance must be a finite length >= 0, not {clearance!r}") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"clearance must be a finite length >= 0, not {value}")
    if on_blocked not in ON_BLOCKED:
        raise ValueError(f"on_blocked must be one of {', '.join(ON_BLOCKED)}, got {on_blocked!r}")
    if isinstance(route_mode, str) and route_mode not in ROUTE_MODES:
        raise ValueError(
            f"route must be one of {', '.join(ROUTE_MODES)} or a list of waypoints, "
            f"got {route_mode!r}"
        )
    return value


def _anchor(endpoint: dict, aim: tuple[float, float]) -> tuple[tuple[float, float], float | None]:
    """Where the line actually starts/ends: on the circle for radial ports,
    leaving along the axis towards ``aim`` (see ``aim_points``)."""
    if endpoint["radius"] > 0:
        axis = snap_axis(aim[0] - endpoint["x"], aim[1] - endpoint["y"])
        ux, uy = AXIS[axis]
        return (
            endpoint["x"] + ux * endpoint["radius"],
            endpoint["y"] + uy * endpoint["radius"],
        ), axis
    return (endpoint["x"], endpoint["y"]), endpoint["direction_deg"]


def _ref(endpoint: dict) -> dict | None:
    return {"handle": endpoint["handle"], "port": endpoint["port"]} if endpoint["handle"] else None


def _segment_angle(a: tuple[float, float], b: tuple[float, float]) -> float:
    (ax0, ay0), (ax1, ay1) = a, b
    if abs(ax1 - ax0) < 1e-9 or abs(ay1 - ay0) < 1e-9:
        return snap_axis(ax1 - ax0, ay1 - ay0)
    return math.degrees(math.atan2(ay1 - ay0, ax1 - ax0)) % 360.0


async def draw_line(
    backend: AutoCADBackend,
    from_: dict,
    to: dict,
    line_class: str = "process_major",
    route_mode="auto",
    stub: float = 5.0,
    line_number: str | None = None,
    size: str | None = None,
    service: str | None = None,
    spec: str | None = None,
    insulation: str | None = None,
    number_format: str | None = None,
    label: bool = True,
    arrow: bool | None = None,
    clearance: float = 2.0,
    keep_out=None,
    on_blocked: str = "refuse",
    obstacles: list[Obstacle] | None = None,
) -> dict:
    """``obstacles`` (``pid_from_spec`` only) are boxes that stand in for the
    drawn symbols with the same handle - the catalogue boxes its dry run
    routed around - so preview == apply; every other symbol is read here."""
    if line_class not in LINE_CLASSES:
        raise ValueError(f"line_class must be one of {', '.join(LINE_CLASSES)}, got {line_class!r}")
    clearance = check_routing_options(route_mode, clearance, on_blocked)
    keep = keep_out_obstacles(keep_out)
    cls = LINE_CLASSES[line_class]
    start = await resolve_endpoint(backend, from_, "from_")
    end = await resolve_endpoint(backend, to, "to")
    if start["handle"] and start["handle"] == end["handle"] and start["port"] == end["port"]:
        raise ValueError("from and to are the same port")
    s_aim, e_aim = aim_points((start["x"], start["y"]), (end["x"], end["y"]), route_mode)
    s_point, s_dir = _anchor(start, s_aim)
    e_point, e_dir = _anchor(end, e_aim)

    existing, symbols = await scan_sheet(backend)
    given = list(obstacles or [])
    replaced = {o.ref.get("handle") for o in given if o.ref.get("handle")}
    sheet = [
        Obstacle(tuple(sym["box"]), {"handle": sym["handle"]})
        for sym in symbols
        if sym["handle"] not in replaced
    ] + given
    around = line_obstacles(
        sheet + keep, (start["handle"], end["handle"]), (s_point, e_point), clearance
    )
    try:
        path, routing = plan_route(
            s_point,
            s_dir,
            e_point,
            e_dir,
            route_mode,
            stub,
            around,
            existing,
            clearance,
            on_blocked,
        )
    except NoRouteError as exc:
        raise exc.relabel(await obstacle_rows(backend, exc.obstacles)) from exc
    # Read before anything is written: a failed attribute read leaves no line.
    hits = await obstacle_rows(backend, obstacle_hits(path, around))

    crossings = count_crossings(
        path,
        [ln["vertices"] for ln in existing],
        bulges=[ln["bulges"] for ln in existing],
    )
    used: set[tuple[str, str]] = set()
    # The sequence is the document's (spec §6.3), not the current space's: a
    # line on Layout1 after one in Model was number 1 again (W3-T13-05).
    max_seq = await max_seq_elsewhere(backend)
    for ln in existing:
        used.update(payload_port_refs(ln["payload"]))
        seq = payload_seq(ln["payload"])
        if seq is not None:
            max_seq = max(max_seq, seq)
    port_reuse = [
        {"handle": ep["handle"], "port": ep["port"]}
        for ep in (start, end)
        if ep["handle"] and (ep["handle"], ep["port"]) in used
    ]
    if cls.kind == "signal":
        # An ISA-5.1 signal line carries no pipe line number: no fabricated
        # number, no label, and the process sequence is left alone. A
        # verbatim ``line_number`` is still honoured (and labelled).
        seq = None
    else:
        seq = max_seq + 1
        if line_number is None:
            fmt = number_format or DEFAULT_NUMBER_FORMAT
            fields = {
                "size": size,
                "service": service,
                "seq": seq,
                "spec": spec,
                "insulation": insulation,
            }
            line_number = format_line_number(fmt, **fields) or None

    await ensure_layer(backend, cls.layer)
    poly = await backend.entity_create_polyline(
        [list(p) for p in path], closed=False, layer=cls.layer
    )
    if cls.linetype:
        await backend.entity_set_properties(poly.handle, linetype=cls.linetype)
    await write_payload(
        backend,
        poly.handle,
        line_payload(
            line_class, line_number, size, service, spec, insulation, seq, _ref(start), _ref(end)
        ),
    )

    label_handle = None
    if label and line_number:
        await ensure_layer(backend, LABEL_LAYER)
        lx, ly, rot, _index = label_placement(path)
        shift = len(line_number) * TEXT_HEIGHT * 0.7 / 2.0
        lx -= math.cos(math.radians(rot)) * shift
        ly -= math.sin(math.radians(rot)) * shift
        text = await backend.entity_create_text(
            line_number, lx, ly, TEXT_HEIGHT, rot, layer=LABEL_LAYER
        )
        label_handle = text.handle

    want_arrow = cls.arrow if arrow is None else arrow
    arrow_handle = None
    if want_arrow:
        angle = _segment_angle(path[-2], path[-1])
        ax1, ay1 = path[-1]
        placed = await insert_symbol(
            backend,
            resolve("arrow_flow"),
            ax1,
            ay1,
            angle,
            1.0,
            None,
            cls.layer,
            marker_payload(poly.handle),
        )
        arrow_handle = placed["handle"]

    marker_handles: list[str] = []
    if cls.marker:
        marker_spec = resolve(cls.marker)
        for mx, my, angle in marker_positions(path):
            placed = await insert_symbol(
                backend,
                marker_spec,
                mx,
                my,
                angle,
                1.0,
                None,
                cls.layer,
                marker_payload(poly.handle),
            )
            marker_handles.append(placed["handle"])

    result = {
        "handle": poly.handle,
        "vertices": [[x, y] for x, y in path],
        "line_class": line_class,
        "layer": cls.layer,
        "line_number": line_number,
        "from": {
            "handle": start["handle"],
            "port": start["port"],
            "x": s_point[0],
            "y": s_point[1],
        },
        "to": {"handle": end["handle"], "port": end["port"], "x": e_point[0], "y": e_point[1]},
        "port_reuse": port_reuse,
        "crossings": crossings,
        "obstacle_hits": hits,
        "label_handle": label_handle,
        "arrow_handle": arrow_handle,
        "marker_handles": marker_handles,
        "backend": backend.name,
    }
    if routing is not None:
        result["routing"] = routing
    return result
