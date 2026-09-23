"""``arch_plan_from_spec``: a whole plan from one declarative document, in one transaction.

Walls with their hosted openings, stairs, room labels (each area measured off
the walls just drawn), the exterior dimension chains and the schedules — in
that order, because each step reads what the one before it drew: a room label
measures the wall faces, and a schedule reads the ``ACADMCP_ARCH`` records.

Everything that can be refused without a drawing is refused before the
transaction opens, by path (``walls[2].thickness``, ``openings[1]: ...``): the
model, the openings against their walls, the junctions (the wall engine is
pure and is run once in memory for exactly this), every stair's plan symbol,
the rooms (a typed ``area`` included), the chain sides and the schedule kinds.
What only the drawing can refuse - a room point that lies in no closed face -
refuses inside the transaction, and the transaction is rolled back.

The rollback is shielded for the reason ``engineering/pid/spec.py`` documents
at length: the MCP SDK cancels a request by cancelling an anyio scope, anyio
cancellation is level-triggered, and a bare ``await`` in the handler would
itself be cancelled before the rollback body ran - leaving half a plan inside
an open transaction.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import anyio

from engineering.arch import draw as _draw
from engineering.arch import rooms as _rooms
from engineering.arch import schedule as _schedule

from .lang import LANGS
from .model import (
    opening_from_dict,
    room_from_dict,
    stair_from_dict,
    validate_openings,
    wall_from_dict,
)

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

log = logging.getLogger(__name__)

SPEC_KEYS = (
    "sheet",
    "lang",
    "poche",
    "walls",
    "openings",
    "stairs",
    "rooms",
    "chains",
    "schedules",
)
SHEET_KEYS = ("intent", "sheet_size", "scale")
CHAIN_SIDES = ("bottom", "top", "left", "right")
SCHEDULE_KINDS = ("doors", "windows", "rooms")

#: A two-room plan the tool docstring and the tests use: an entrance door and
#: an interior door, two windows, a straight stair, both rooms labelled, the
#: two exterior chains and all three schedules. The room numbers are "01" /
#: "02", not "R1": the finalize validator reads R-and-a-digit as a radius
#: callout typed as text.
EXAMPLE_SPEC: dict = {
    "sheet": {"intent": "Two-room ground floor plan", "sheet_size": "A3", "scale": 50},
    "lang": "en",
    "walls": [
        {
            "id": "EXT",
            "axis": [[0, 0], [9000, 0], [9000, 6000], [0, 6000]],
            "thickness": 250,
            "closed": True,
            "material": "brick",
        },
        {"id": "INT", "axis": [[5000, 0], [5000, 6000]], "thickness": 100, "material": "aac"},
    ],
    "openings": [
        {"id": "D1", "wall": "EXT", "kind": "door", "offset": 1000, "width": 1000, "tag": "D1"},
        {
            "id": "D2",
            "wall": "INT",
            "kind": "door",
            "offset": 2000,
            "width": 900,
            "hand": "right",
            "tag": "D2",
        },
        {
            "id": "W1",
            "wall": "EXT",
            "kind": "window",
            "offset": 20500,
            "width": 1500,
            "sill": 900,
            "height": 1200,
            "tag": "W1",
        },
        {
            "id": "W2",
            "wall": "EXT",
            "kind": "window",
            "offset": 11000,
            "width": 1200,
            "sill": 900,
            "height": 1200,
            "tag": "W2",
        },
    ],
    "stairs": [
        {
            "id": "S1",
            "start": [6000, 1000],
            "direction_deg": 90,
            "width": 900,
            "risers": 16,
            "riser_height": 175,
            "going": 280,
        }
    ],
    "rooms": [
        {"id": "R1", "name": "LIVING", "number": "01", "at": [2500, 3000]},
        {"id": "R2", "name": "HALL", "number": "02", "at": [7900, 3000]},
    ],
    "chains": {"sides": ["bottom", "left"]},
    "schedules": [
        {"kind": "doors", "at": [10500, 6000]},
        {"kind": "windows", "at": [10500, 4000]},
        {"kind": "rooms", "at": [10500, 2000]},
    ],
}


def _number(value, where: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{where}: expected a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected a number, got {value!r}") from None
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{where}: must be a finite number greater than zero, got {value!r}")
    return number


def _point(value, where: str) -> tuple[float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{where}: expected [x, y], got {value!r}")
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected [x, y], got {value!r}") from None
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"{where}: must be two finite numbers, got {value!r}")
    return (x, y)


def _list(spec: dict, key: str) -> list:
    value = spec.get(key)
    if value is None:
        return []
    if isinstance(value, (str, bytes, dict)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{key}: expected a list, got {type(value).__name__}")
    return list(value)


def validate_spec(spec: dict) -> dict:
    """The whole document checked and normalised - nothing is drawn here.

    Returns ``{"sheet", "lang", "scale", "poche", "walls", "openings",
    "stairs", "rooms", "chains", "schedules"}`` with model objects in place of
    dicts (rooms stay dicts: ``label_room`` takes the request, not a Room).
    """
    from engineering.arch.stairs import stair_prims
    from engineering.arch.walls import wall_geometry

    if not isinstance(spec, dict):
        raise ValueError(f"spec: expected a dict, got {type(spec).__name__}")
    unknown = sorted(set(spec) - set(SPEC_KEYS))
    if unknown:
        raise ValueError(
            f"spec: unknown key(s) {', '.join(unknown)}; keys are {', '.join(SPEC_KEYS)}"
        )
    sheet = spec.get("sheet") or {}
    if not isinstance(sheet, dict):
        raise ValueError(f"sheet: expected a dict, got {type(sheet).__name__}")
    extra = sorted(set(sheet) - set(SHEET_KEYS))
    if extra:
        raise ValueError(
            f"sheet: unknown key(s) {', '.join(extra)}; keys are {', '.join(SHEET_KEYS)}"
        )
    scale = _number(sheet.get("scale", 50), "sheet.scale")
    lang = spec.get("lang", "en")
    if lang not in LANGS:
        raise ValueError(f"lang: unknown language {lang!r}; languages are {', '.join(LANGS)}")
    poche = spec.get("poche", True)
    if not isinstance(poche, bool):
        raise ValueError(f"poche: expected true or false, got {poche!r}")

    raw_walls = _list(spec, "walls")
    if not raw_walls:
        raise ValueError("walls: name at least one wall - a plan without walls has no rooms")
    walls = [wall_from_dict(item, f"walls[{i}]") for i, item in enumerate(raw_walls)]
    openings = [
        opening_from_dict(item, f"openings[{i}]") for i, item in enumerate(_list(spec, "openings"))
    ]
    try:
        validate_openings(walls, openings)
        geometry = wall_geometry(walls, openings, poche=poche)
    except ValueError as exc:
        raise ValueError(f"walls/openings: {exc}") from exc

    stairs = []
    for i, item in enumerate(_list(spec, "stairs")):
        stair = stair_from_dict(item, f"stairs[{i}]")
        try:
            stair_prims(stair, lang=lang, scale=scale)
        except ValueError as exc:
            raise ValueError(f"stairs[{i}]: {exc}") from exc
        stairs.append(stair)

    rooms = []
    seen_rooms: set[str] = set()
    for i, item in enumerate(_list(spec, "rooms")):
        room = room_from_dict(item, f"rooms[{i}]")
        if room.id in seen_rooms:
            raise ValueError(f"rooms[{i}].id: {room.id!r} is used twice")
        seen_rooms.add(room.id)
        rooms.append(dict(item))

    chains = spec.get("chains")
    sides: tuple[str, ...] | None = None
    if chains not in (None, False):
        if chains is True:
            chains = {}
        if not isinstance(chains, dict) or set(chains) - {"sides"}:
            raise ValueError(f"chains: expected {{sides: [...]}}, got {chains!r}")
        sides = tuple(chains.get("sides") or ("bottom", "left"))
        bad = [side for side in sides if side not in CHAIN_SIDES]
        if bad or not sides:
            raise ValueError(f"chains.sides: {bad or sides!r} - sides are {', '.join(CHAIN_SIDES)}")

    schedules = []
    for i, item in enumerate(_list(spec, "schedules")):
        if not isinstance(item, dict) or set(item) - {"kind", "at"}:
            raise ValueError(f"schedules[{i}]: expected {{kind, at}}, got {item!r}")
        kind = item.get("kind")
        if kind not in SCHEDULE_KINDS:
            raise ValueError(
                f"schedules[{i}].kind: {kind!r} is not a schedule; "
                f"schedules are {', '.join(SCHEDULE_KINDS)}"
            )
        schedules.append({"kind": kind, "at": _point(item.get("at"), f"schedules[{i}].at")})

    return {
        "sheet": {
            "intent": str(sheet.get("intent") or "architectural plan"),
            "sheet_size": str(sheet.get("sheet_size") or "A3"),
        },
        "scale": scale,
        "lang": lang,
        "poche": poche,
        "walls": walls,
        "openings": openings,
        "omitted": list(geometry.omitted),
        "stairs": stairs,
        "rooms": rooms,
        "chains": sides,
        "schedules": schedules,
    }


async def draw_plan_from_spec(backend: AutoCADBackend, spec: dict) -> dict:
    """Validate, then draw the whole plan inside one transaction.

    Returns ``{"walls", "openings", "stairs", "rooms", "chains", "schedules",
    "omitted", "drawn", "critique", "backend"}``: the wall and opening ids that
    were drawn (an opening the wall engine could not cut is in ``omitted``, not
    in ``openings``), each drawing step's own result (``drawn`` is
    ``draw_walls``'), and the three ``arch_*`` critique focuses run over the
    committed drawing.
    """
    from engineering.arch.critique import ARCH_FOCUSES, build_index, issues_for

    norm = validate_spec(spec)
    scale = norm["scale"]
    lang = norm["lang"]
    if backend.get_plan_spec() is None:
        await backend.drawing_plan(
            norm["sheet"]["intent"], norm["sheet"]["sheet_size"], 1.0 / scale, "arch"
        )

    begun = await backend.transaction_begin()
    if not isinstance(begun, dict) or not begun.get("ok"):
        # a refusal, not a crash: nothing has been drawn yet
        detail = begun.get("error") if isinstance(begun, dict) else begun
        raise ValueError(
            f"arch_plan_from_spec: could not open a transaction ({detail}); nothing was drawn. "
            "If one is already open, close it with transaction_commit or "
            "transaction_rollback and retry."
        )

    result: dict = {
        "walls": None,
        "stairs": [],
        "rooms": [],
        "chains": None,
        "schedules": [],
    }
    try:
        await backend.drawing_apply_iso_layers("arch")
        result["walls"] = await _draw.draw_walls(
            backend, norm["walls"], norm["openings"], poche=norm["poche"]
        )
        for i, stair in enumerate(norm["stairs"]):
            try:
                result["stairs"].append(
                    await _draw.draw_stair(backend, stair, lang=lang, scale=scale)
                )
            except ValueError as exc:
                raise ValueError(f"stairs[{i}]: {exc}") from exc
        for i, room in enumerate(norm["rooms"]):
            try:
                result["rooms"].append(
                    await _rooms.label_room(backend, room, lang=lang, scale=scale)
                )
            except ValueError as exc:
                raise ValueError(f"rooms[{i}]: {exc}") from exc
        if norm["chains"] is not None:
            result["chains"] = await _draw.draw_chains(
                backend, norm["walls"], norm["openings"], sides=norm["chains"], scale=scale
            )
        for i, item in enumerate(norm["schedules"]):
            try:
                result["schedules"].append(
                    await _schedule.draw_schedule(
                        backend, item["kind"], at=item["at"], lang=lang, scale=scale
                    )
                )
            except ValueError as exc:
                raise ValueError(f"schedules[{i}]: {exc}") from exc
    except BaseException as exc:
        cancelled = isinstance(exc, anyio.get_cancelled_exc_class())
        with anyio.CancelScope(shield=True):
            try:
                await backend.transaction_rollback()
            except Exception as rollback_exc:
                if not cancelled:
                    raise
                log.error(
                    "arch_plan_from_spec: rollback after a cancelled request failed (%s: %s); "
                    "the plan may be half-drawn - check system_status and "
                    "transaction_rollback before retrying",
                    type(rollback_exc).__name__,
                    rollback_exc,
                )
        raise
    await backend.transaction_commit()

    index = await build_index(backend)
    critique = [issue.to_dict() for focus in ARCH_FOCUSES for issue in issues_for(focus, index)]
    drawn = result["walls"] or {}
    omitted = list(drawn.get("omitted", norm["omitted"]))
    skipped = {str(item.get("element")) for item in omitted}
    return {
        "walls": [wall.id for wall in norm["walls"]],
        "openings": [o.id for o in norm["openings"] if o.id not in skipped],
        "stairs": result["stairs"],
        "rooms": result["rooms"],
        "chains": result["chains"],
        "schedules": result["schedules"],
        "omitted": omitted,
        "drawn": drawn,
        "critique": critique,
        "backend": backend.name,
    }
