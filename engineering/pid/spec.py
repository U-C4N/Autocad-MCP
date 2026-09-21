"""``pid_from_spec``: a whole P&ID from one declarative document, in one transaction."""

from __future__ import annotations

import copy
import math
from typing import TYPE_CHECKING

import anyio

from .critique import PID_FOCUSES, issues_for
from .drawlines import draw_line
from .graph import build_graph
from .insert import place_symbol
from .lines import AXIS, LINE_CLASSES, count_crossings, route, snap_axis
from .symbols import resolve, transform_port

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

EXAMPLE_SPEC: dict = {
    "sheet": {
        "layer_set": "pid",
        "sheet_size": "A3",
        "scale": 1.0,
        "intent": "Feed section P&ID",
    },
    "equipment": [
        {
            "id": "P-101",
            "symbol": "centrifugal_pump",
            "x": 100,
            "y": 100,
            "rotation": 0,
            "tag": "P-101",
            "desc": "Feed pump",
        },
        {
            "id": "V-201",
            "symbol": "vertical_vessel",
            "x": 220,
            "y": 120,
            "tag": "V-201",
            "params": {
                "width": 20,
                "height": 40,
                "nozzles": [
                    {"name": "N1", "side": "top", "fraction": 0.5},
                    {"name": "N2", "side": "left", "fraction": 0.3},
                ],
            },
        },
    ],
    "valves": [
        {
            "id": "FCV-101",
            "symbol": "globe",
            "actuator": "diaphragm",
            "x": 160,
            "y": 104,
            "tag": "FCV-101",
            "fail": "FC",
        },
    ],
    "instruments": [
        {
            "id": "FIC-101",
            "type": "dcs",
            "location": "primary",
            "x": 160,
            "y": 140,
            "tag": "FIC-101",
        },
    ],
    "lines": [
        {
            "from": "P-101.discharge",
            "to": "FCV-101.in",
            "class": "process_major",
            "number": "100-P-1001-CS1",
        },
        {
            "from": "FCV-101.out",
            "to": "V-201.N2",
            "class": "process_major",
            "number": "100-P-1001-CS1",
        },
        {"from": "FIC-101", "to": "FCV-101.signal", "class": "electric"},
    ],
    "connectors": [
        {
            "id": "OP-1",
            "direction": "out",
            "x": 300,
            "y": 120,
            "tag": "TO P&ID-002",
            "link": "L-17",
            "from": "V-201.N1",
            "class": "process_major",
        },
    ],
}

_SECTIONS = ("equipment", "valves", "instruments", "connectors")
_TOP_KEYS = {"sheet", "lines", *_SECTIONS}
_ITEM_OPTIONS = {
    "equipment": {"symbol", "x", "y", "rotation", "scale", "tag", "desc", "params", "shape"},
    "valves": {"symbol", "x", "y", "rotation", "scale", "tag", "desc", "actuator", "fail"},
    "instruments": {"x", "y", "rotation", "scale", "tag", "type", "location"},
    "connectors": {
        "x",
        "y",
        "rotation",
        "scale",
        "tag",
        "link",
        "direction",
        "from",
        "to",
        "class",
        "number",
    },
}
_LINE_KEYS = {
    "from",
    "to",
    "class",
    "number",
    "size",
    "service",
    "spec",
    "insulation",
    "route",
    "stub",
    "label",
    "arrow",
}


def _num(value, where: str) -> float:
    """A finite number, or a ``ValueError`` naming the path.

    ``float("nan")`` used to pass and be placed as an INSERT at ``[nan, 0]``;
    the transaction committed and ``build_graph`` then crashed on the drawing
    with no path in its message. Only in-process callers can deliver a
    non-finite (the MCP pipeline coerces them first), but ``run_spec`` is a
    public interface and its docstring promises nothing malformed reaches
    the drawing.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number")
    try:
        number = float(value)
    except OverflowError as exc:  # a 400-digit int is a number but not a float
        raise ValueError(f"{where} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{where} must be a finite number")
    return number


def _ref(value, where: str, ids: set[str]) -> tuple[str, str | None]:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be 'ID' or 'ID.port'")
    item_id, _, port = value.partition(".")
    if item_id not in ids:
        raise ValueError(f"{where}: unknown id {item_id!r}")
    return item_id, port or None


def _line_class(line: dict, where: str) -> str:
    cls = line.get("class", "process_major")
    if cls not in LINE_CLASSES:
        raise ValueError(f"{where}.class must be one of {', '.join(LINE_CLASSES)}")
    return cls


def validate_spec(spec: dict) -> dict:
    """Normalise a spec document; every error names the offending path.

    Nothing is created here, so a malformed spec never reaches the drawing.
    """
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    unknown = set(spec) - _TOP_KEYS
    if unknown:
        raise ValueError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    out = {key: copy.deepcopy(spec.get(key) or ([] if key != "sheet" else {})) for key in _TOP_KEYS}
    if not isinstance(out["sheet"], dict):
        raise ValueError("sheet must be an object")
    ids: set[str] = set()
    for section in _SECTIONS:
        items = out[section]
        if not isinstance(items, list):
            raise ValueError(f"{section} must be a list")
        for i, item in enumerate(items):
            where = f"{section}[{i}]"
            if not isinstance(item, dict):
                raise ValueError(f"{where} must be an object")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ValueError(f"{where}.id must be a non-empty string")
            if item_id in ids:
                raise ValueError(f"{where}.id {item_id!r} is repeated")
            ids.add(item_id)
            bad = set(item) - _ITEM_OPTIONS[section] - {"id"}
            if bad:
                raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(bad))}")
            for key in ("x", "y"):
                if key not in item:
                    raise ValueError(f"{where}.{key} is required")
                item[key] = _num(item[key], f"{where}.{key}")
            for key in ("rotation", "scale"):
                if key in item:
                    item[key] = _num(item[key], f"{where}.{key}")
            if section in ("equipment", "valves") and not isinstance(item.get("symbol"), str):
                raise ValueError(f"{where}.symbol is required")
            if section == "connectors":
                if item.get("direction") not in ("in", "out"):
                    raise ValueError(f"{where}.direction must be 'in' or 'out'")
                has_from, has_to = "from" in item, "to" in item
                if item["direction"] == "out" and (not has_from or has_to):
                    raise ValueError(f"{where}: an 'out' connector carries exactly 'from'")
                if item["direction"] == "in" and (not has_to or has_from):
                    raise ValueError(f"{where}: an 'in' connector carries exactly 'to'")
    lines = out["lines"]
    if not isinstance(lines, list):
        raise ValueError("lines must be a list")
    for i, line in enumerate(lines):
        where = f"lines[{i}]"
        if not isinstance(line, dict):
            raise ValueError(f"{where} must be an object")
        bad = set(line) - _LINE_KEYS
        if bad:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(bad))}")
        for key in ("from", "to"):
            if key not in line:
                raise ValueError(f"{where}.{key} is required")
            _ref(line[key], f"{where}.{key}", ids)
        line["class"] = _line_class(line, where)
        if "stub" in line:
            line["stub"] = _num(line["stub"], f"{where}.stub")
    for i, conn in enumerate(out["connectors"]):
        key = "from" if conn["direction"] == "out" else "to"
        _ref(conn[key], f"connectors[{i}].{key}", ids)
        conn["class"] = _line_class(conn, f"connectors[{i}]")
    return out


def _spec_for(section: str, item: dict):
    """Resolve the SymbolSpec an item would place (no backend)."""
    if section == "instruments":
        return resolve("instrument", type=item.get("type"), location=item.get("location"))
    if section == "connectors":
        return resolve("offpage", direction=item["direction"])
    return resolve(
        item["symbol"],
        actuator=item.get("actuator"),
        params=item.get("params"),
        shape=item.get("shape"),
    )


def _connector_lines(norm: dict) -> list[tuple[str, dict]]:
    """The implicit line each connector carries, as ``(where, line)``."""
    out = []
    for i, conn in enumerate(norm["connectors"]):
        if conn["direction"] == "out":
            line = {"from": conn["from"], "to": conn["id"]}
        else:
            line = {"from": conn["id"], "to": conn["to"]}
        line.update({"class": conn["class"], "number": conn.get("number")})
        out.append((f"connectors[{i}]", line))
    return out


def _all_lines(norm: dict) -> list[tuple[str, dict]]:
    return [(f"lines[{i}]", line) for i, line in enumerate(norm["lines"])] + _connector_lines(norm)


def _plan_lines(norm: dict) -> list[dict]:
    """Route every line in memory from catalogue ports — the dry run."""
    placed: dict[str, dict] = {}
    for section in _SECTIONS:
        for i, item in enumerate(norm[section]):
            try:
                spec = _spec_for(section, item)
            except ValueError as exc:
                raise ValueError(f"{section}[{i}]: {exc}") from exc
            ports = {
                p.name: transform_port(
                    p, item["x"], item["y"], item.get("rotation", 0.0), item.get("scale", 1.0)
                )
                for p in spec.ports
            }
            placed[item["id"]] = {"family": spec.family, "ports": ports}

    def endpoint(ref: str) -> dict:
        item_id, _, port = ref.partition(".")
        entry = placed[item_id]
        if not port:
            if len(entry["ports"]) == 1:
                port = next(iter(entry["ports"]))
            elif entry["family"] == "instrument" and "signal" in entry["ports"]:
                port = "signal"
            else:
                raise ValueError(f"{ref}: choose a port: {', '.join(entry['ports'])}")
        if port not in entry["ports"]:
            raise ValueError(f"{ref}: no port {port!r}; ports: {', '.join(entry['ports'])}")
        return entry["ports"][port]

    def anchor(ep: dict, other: dict):
        if ep["radius"] > 0:
            axis = snap_axis(other["x"] - ep["x"], other["y"] - ep["y"])
            ux, uy = AXIS[axis]
            return (ep["x"] + ux * ep["radius"], ep["y"] + uy * ep["radius"]), axis
        return (ep["x"], ep["y"]), ep["direction_deg"]

    planned: list[dict] = []
    for index, (where, line) in enumerate(_all_lines(norm)):
        try:
            start, end = endpoint(line["from"]), endpoint(line["to"])
            s, sd = anchor(start, end)
            e, ed = anchor(end, start)
            vertices = route(
                s, sd, e, ed, stub=float(line.get("stub", 5.0)), mode=line.get("route", "auto")
            )
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from exc
        crossings = count_crossings(vertices, [p["vertices"] for p in planned])
        planned.append(
            {
                "index": index,
                "where": where,
                "from": line["from"],
                "to": line["to"],
                "class": line["class"],
                "vertices": [[x, y] for x, y in vertices],
                "crossings": crossings,
            }
        )
    return planned


async def _place(backend: AutoCADBackend, section: str, item: dict) -> dict:
    common = (
        item["x"],
        item["y"],
        item.get("rotation", 0.0),
        item.get("scale", 1.0),
        item.get("tag"),
    )
    if section == "instruments":
        return await place_symbol(
            backend,
            "instrument",
            *common,
            type=item.get("type"),
            location=item.get("location"),
        )
    if section == "connectors":
        return await place_symbol(
            backend,
            "offpage",
            *common,
            link=item.get("link"),
            direction=item["direction"],
        )
    return await place_symbol(
        backend,
        item["symbol"],
        *common,
        desc=item.get("desc"),
        fail=item.get("fail"),
        actuator=item.get("actuator"),
        params=item.get("params"),
        shape=item.get("shape"),
    )


async def run_spec(backend: AutoCADBackend, spec: dict, dry_run: bool = False) -> dict:
    """Validate, then place every item and draw every line inside one transaction.

    ``dry_run`` routes from catalogue ports in memory and touches nothing.
    """
    norm = validate_spec(spec)
    if dry_run:
        planned = _plan_lines(norm)
        return {
            "dry_run": True,
            "items": sum(len(norm[s]) for s in _SECTIONS),
            "lines": planned,
            "crossings_total": sum(p["crossings"] for p in planned),
        }
    sheet = norm["sheet"]
    if backend.get_plan_spec() is None:
        await backend.drawing_plan(
            sheet.get("intent", "P&ID"),
            sheet.get("sheet_size", "A3"),
            float(sheet.get("scale", 1.0)),
            sheet.get("layer_set", "pid"),
        )
    begun = await backend.transaction_begin()
    if not isinstance(begun, dict) or not begun.get("ok"):
        raise RuntimeError(f"pid_from_spec: could not open a transaction: {begun}")
    handles: dict[str, str] = {}
    crossings_total = 0
    try:
        for section in _SECTIONS:
            for i, item in enumerate(norm[section]):
                try:
                    placed = await _place(backend, section, item)
                except ValueError as exc:
                    raise ValueError(f"{section}[{i}]: {exc}") from exc
                handles[item["id"]] = placed["handle"]

        def ref(value: str) -> dict:
            item_id, _, port = value.partition(".")
            if port:
                return {"handle": handles[item_id], "port": port}
            return {"handle": handles[item_id]}

        for where, line in _all_lines(norm):
            try:
                drawn = await draw_line(
                    backend,
                    ref(line["from"]),
                    ref(line["to"]),
                    line["class"],
                    line.get("route", "auto"),
                    float(line.get("stub", 5.0)),
                    line.get("number"),
                    line.get("size"),
                    line.get("service"),
                    line.get("spec"),
                    line.get("insulation"),
                    None,
                    bool(line.get("label", True)),
                    line.get("arrow"),
                )
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from exc
            crossings_total += drawn["crossings"]
    except BaseException:
        # ``Exception`` alone let a client cancellation (``CancelledError`` is
        # a ``BaseException``) leave the placed symbols in the drawing inside
        # an open transaction — on COM with the undo mark still open, so every
        # later ``pid_from_spec`` was refused until a manual rollback.
        #
        # The rollback is shielded because the MCP SDK cancels a request by
        # cancelling an anyio scope, and anyio cancellation is level-triggered:
        # ``task.cancel()`` is re-issued on every loop iteration while the task
        # is still inside the cancelled scope. A bare ``await`` here would be
        # cancelled at its first suspension, before the rollback body ran —
        # measured through the real transport: five symbols left in an open
        # transaction, and on COM the undo mark left open while the backend's
        # flag already said "no transaction". A native ``Task.cancel()`` is
        # delivered once and clears, so it was never the case that mattered.
        with anyio.CancelScope(shield=True):
            await backend.transaction_rollback()
        raise
    await backend.transaction_commit()
    graph = await build_graph(backend)
    critique = [issue.to_dict() for focus in PID_FOCUSES for issue in issues_for(focus, graph)]
    return {
        "handles": handles,
        "graph": graph,
        "critique": critique,
        "crossings_total": crossings_total,
    }
