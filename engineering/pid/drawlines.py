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
    flatten_bulges,
    format_line_number,
    label_placement,
    marker_positions,
    route,
    snap_axis,
)
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


async def resolve_endpoint(backend: AutoCADBackend, ref: dict) -> dict:
    """``{handle, port}`` → the port in WCS (payload ports + INSERT transform);
    ``{x, y}`` → a free point."""
    if not isinstance(ref, dict):
        raise ValueError("endpoint must be {handle, port} or {x, y}")
    if "handle" in ref:
        handle = str(ref["handle"])
        info = await backend.entity_get(handle)
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


async def existing_pid_lines(backend: AutoCADBackend) -> list[dict]:
    """Every LINE/polyline on a P&ID line layer, with its payload when it has one.

    Listed without a type filter: the two engines name a lightweight polyline
    differently (``_POLY_TYPES``), and a filter spelled for one of them is blind
    on the other — every auto-built number would restart at 1 and no crossing
    or port reuse would ever be reported there. ``vertices`` are the raw
    vertices; ``bulges`` (one per vertex, DXF convention) let the crossing test
    follow an arc instead of its chord.
    """
    out = []
    offset = 0
    while True:
        page = await backend.entity_list(limit=_PAGE, offset=offset)
        for info in page:
            if info.type not in _POLY_TYPES | _LINE_TYPES:
                continue
            if info.layer.upper() not in PID_LINE_LAYERS:
                continue
            payload = await read_payload(backend, info.handle) if info.type in _POLY_TYPES else None
            vertices = _vertices(info)
            out.append(
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
    return out


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
) -> dict:
    if line_class not in LINE_CLASSES:
        raise ValueError(f"line_class must be one of {', '.join(LINE_CLASSES)}, got {line_class!r}")
    cls = LINE_CLASSES[line_class]
    start = await resolve_endpoint(backend, from_)
    end = await resolve_endpoint(backend, to)
    if start["handle"] and start["handle"] == end["handle"] and start["port"] == end["port"]:
        raise ValueError("from and to are the same port")
    s_aim, e_aim = aim_points((start["x"], start["y"]), (end["x"], end["y"]), route_mode)
    s_point, s_dir = _anchor(start, s_aim)
    e_point, e_dir = _anchor(end, e_aim)
    path = route(s_point, s_dir, e_point, e_dir, stub=stub, mode=route_mode)

    existing = await existing_pid_lines(backend)
    crossings = count_crossings(
        path, [flatten_bulges(ln["vertices"], ln["bulges"]) for ln in existing]
    )
    used: set[tuple[str, str]] = set()
    max_seq = 0
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

    return {
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
        "label_handle": label_handle,
        "arrow_handle": arrow_handle,
        "marker_handles": marker_handles,
        "backend": backend.name,
    }
