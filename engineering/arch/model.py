"""The plan model: walls with hosted openings, stairs and rooms.

A wall is an axis polyline of straight segments, a thickness, a justification
(the side of the axis the wall lies on, *looking along the axis from its first
point*) and a material. An opening is hosted in a wall by distance along that
axis: ``offset`` runs from the axis start to the opening's near jamb, so
``offset + width`` is its far jamb. A room is a name at a point; its area is
measured from the face the walls enclose and is never accepted from a caller.

Validation happens before anything is written and names its path
(``walls[2].thickness: must be greater than zero, got -200``), the repository
rule. The plan travels on the drawing as ``ACADMCP_ARCH`` XDATA through the
mechanical codec generalised in track F (``engineering/mech/xdata.py`` with
``app_id=`` / ``kinds=``), so a schedule is a *read* of the drawing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from engineering.mech import xdata as _codec

from .materials import WALL_HATCH_TABLE, WALL_MATERIALS, normalise_material

Pt = tuple[float, float]

APP_ID = "ACADMCP_ARCH"
KINDS: tuple[str, ...] = ("wall", "opening", "stair", "room")
PAYLOAD_VERSION = _codec.PAYLOAD_VERSION

JUSTIFICATIONS: tuple[str, ...] = ("center", "left", "right")
OPENING_KINDS: tuple[str, ...] = ("door", "window")
SWINGS: tuple[str, ...] = ("in", "out")
HANDS: tuple[str, ...] = ("left", "right")
STAIR_KINDS: tuple[str, ...] = ("straight", "l", "u")
TURNS: tuple[str, ...] = ("left", "right")

#: Below this, two lengths along a wall are the same length (mm).
EPS = 1e-9


@dataclass(frozen=True)
class Wall:
    id: str
    axis: tuple[Pt, ...]
    thickness: float
    justification: str = "center"
    material: str = "brick"
    closed: bool = False


@dataclass(frozen=True)
class Opening:
    id: str
    wall: str
    kind: str
    offset: float
    width: float
    swing: str = "in"
    hand: str = "left"
    sill: float | None = None
    height: float | None = None
    tag: str | None = None


@dataclass(frozen=True)
class Stair:
    id: str
    start: Pt
    direction_deg: float
    width: float
    risers: int
    riser_height: float
    going: float
    kind: str = "straight"
    turn: str = "left"


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    number: str | None
    at: Pt
    area: float = 0.0


# -- field readers -------------------------------------------------------------


def _number(value, where: str, *, positive: bool = True, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{where}: expected a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{where}: must be finite, got {value!r}")
    if positive and not allow_zero and number <= 0.0:
        raise ValueError(f"{where}: must be greater than zero, got {number:g}")
    if positive and allow_zero and number < 0.0:
        raise ValueError(f"{where}: must not be negative, got {number:g}")
    return number


def _text(value, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{where}: expected a name, got {value!r}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{where}: must not be empty")
    return text


def _choice(value, where: str, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise ValueError(f"{where}: {value!r} is not one of {', '.join(allowed)}")
    return value.strip().lower()


def _point(value, where: str) -> Pt:
    if isinstance(value, dict):
        if {"bulge", "arc", "center", "radius"} & set(value):
            raise ValueError(
                f"{where}: an arc segment is refused - a wall axis is straight segments in "
                "this build (curved walls are out of 1.6); split the curve into straight walls"
            )
        raise ValueError(f"{where}: expected [x, y], got {value!r}")
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{where}: expected [x, y], got {value!r}")
    if len(value) == 3:
        raise ValueError(
            f"{where}: a third value is a bulge, and an arc segment is refused - a wall axis "
            "is straight segments in this build (curved walls are out of 1.6)"
        )
    if len(value) != 2:
        raise ValueError(f"{where}: expected [x, y], got {value!r}")
    return (
        _number(value[0], f"{where}[0]", positive=False),
        _number(value[1], f"{where}[1]", positive=False),
    )


def _known(d, path: str, allowed: Sequence[str]) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"{path}: expected a dict, got {type(d).__name__}")
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        raise ValueError(
            f"{path}: unknown field(s) {', '.join(unknown)}; fields are {', '.join(allowed)}"
        )
    return d


def _required(d: dict, key: str, path: str):
    if key not in d or d[key] is None:
        raise ValueError(f"{path}.{key}: required")
    return d[key]


# -- builders -----------------------------------------------------------------

_WALL_FIELDS = ("id", "axis", "thickness", "justification", "material", "closed")
_OPENING_FIELDS = (
    "id",
    "wall",
    "kind",
    "offset",
    "width",
    "swing",
    "hand",
    "sill",
    "height",
    "tag",
)
_STAIR_FIELDS = (
    "id",
    "start",
    "direction_deg",
    "width",
    "risers",
    "riser_height",
    "going",
    "kind",
    "turn",
)
_ROOM_FIELDS = ("id", "name", "number", "at")


def wall_from_dict(d: dict, path: str = "wall") -> Wall:
    """A validated Wall. Refusals are ``ValueError("<path>.<field>: <reason>")``."""
    _known(d, path, _WALL_FIELDS)
    wall_id = _text(_required(d, "id", path), f"{path}.id")
    raw_axis = _required(d, "axis", path)
    if isinstance(raw_axis, (str, bytes, dict)) or not isinstance(raw_axis, (list, tuple)):
        raise ValueError(f"{path}.axis: expected a list of [x, y] points, got {raw_axis!r}")
    axis = tuple(_point(p, f"{path}.axis[{i}]") for i, p in enumerate(raw_axis))
    closed = d.get("closed", False)
    if not isinstance(closed, bool):
        raise ValueError(f"{path}.closed: expected true or false, got {closed!r}")
    need = 3 if closed else 2
    if len(axis) < need:
        raise ValueError(
            f"{path}.axis: a {'closed ' if closed else ''}wall needs at least {need} points, "
            f"got {len(axis)}"
        )
    for i in range(1, len(axis)):
        if math.dist(axis[i - 1], axis[i]) <= EPS:
            raise ValueError(
                f"{path}.axis[{i}]: repeats axis[{i - 1}] - a zero-length segment has no "
                "direction to offset"
            )
    if closed and math.dist(axis[-1], axis[0]) <= EPS:
        raise ValueError(
            f"{path}.axis[{len(axis) - 1}]: repeats axis[0] - closed=true already closes "
            "the loop; drop the repeated point"
        )
    thickness = _number(_required(d, "thickness", path), f"{path}.thickness")
    justification = _choice(
        d.get("justification", "center"), f"{path}.justification", JUSTIFICATIONS
    )
    material = d.get("material", "brick")
    if not isinstance(material, str) or normalise_material(material) not in WALL_HATCH_TABLE:
        raise ValueError(
            f"{path}.material: unknown wall material {material!r}; "
            f"wall materials are {', '.join(WALL_MATERIALS)}"
        )
    return Wall(
        id=wall_id,
        axis=axis,
        thickness=thickness,
        justification=justification,
        material=normalise_material(material),
        closed=closed,
    )


def opening_from_dict(d: dict, path: str = "opening") -> Opening:
    _known(d, path, _OPENING_FIELDS)
    sill = d.get("sill")
    height = d.get("height")
    tag = d.get("tag")
    return Opening(
        id=_text(_required(d, "id", path), f"{path}.id"),
        wall=_text(_required(d, "wall", path), f"{path}.wall"),
        kind=_choice(_required(d, "kind", path), f"{path}.kind", OPENING_KINDS),
        offset=_number(_required(d, "offset", path), f"{path}.offset", allow_zero=True),
        width=_number(_required(d, "width", path), f"{path}.width"),
        swing=_choice(d.get("swing", "in"), f"{path}.swing", SWINGS),
        hand=_choice(d.get("hand", "left"), f"{path}.hand", HANDS),
        sill=None if sill is None else _number(sill, f"{path}.sill", allow_zero=True),
        height=None if height is None else _number(height, f"{path}.height"),
        tag=_text(tag, f"{path}.tag", optional=True),
    )


def stair_from_dict(d: dict, path: str = "stair") -> Stair:
    _known(d, path, _STAIR_FIELDS)
    risers = _required(d, "risers", path)
    if isinstance(risers, bool) or not isinstance(risers, int):
        if isinstance(risers, float) and risers.is_integer():
            risers = int(risers)
        else:
            raise ValueError(f"{path}.risers: expected a whole number of risers, got {risers!r}")
    if risers < 2:
        raise ValueError(f"{path}.risers: a stair needs at least 2 risers, got {risers}")
    return Stair(
        id=_text(_required(d, "id", path), f"{path}.id"),
        start=_point(_required(d, "start", path), f"{path}.start"),
        direction_deg=_number(
            _required(d, "direction_deg", path), f"{path}.direction_deg", positive=False
        ),
        width=_number(_required(d, "width", path), f"{path}.width"),
        risers=risers,
        riser_height=_number(_required(d, "riser_height", path), f"{path}.riser_height"),
        going=_number(_required(d, "going", path), f"{path}.going"),
        kind=_choice(d.get("kind", "straight"), f"{path}.kind", STAIR_KINDS),
        turn=_choice(d.get("turn", "left"), f"{path}.turn", TURNS),
    )


def room_from_dict(d: dict, path: str = "room") -> Room:
    """A validated Room with ``area`` 0.0: the area is measured, never accepted."""
    if isinstance(d, dict) and "area" in d:
        raise ValueError(
            f"{path}.area: an area is measured from the face the walls enclose, never typed; "
            "remove it"
        )
    _known(d, path, _ROOM_FIELDS)
    return Room(
        id=_text(_required(d, "id", path), f"{path}.id"),
        name=_text(_required(d, "name", path), f"{path}.name"),
        number=_text(d.get("number"), f"{path}.number", optional=True),
        at=_point(_required(d, "at", path), f"{path}.at"),
    )


# -- walls and hosted openings -------------------------------------------------


def axis_segments(wall: Wall) -> list[tuple[Pt, Pt]]:
    """The straight axis segments, the closing one included when the wall is closed."""
    pts = list(wall.axis)
    segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    if wall.closed:
        segs.append((pts[-1], pts[0]))
    return segs


def wall_length(wall: Wall) -> float:
    """The length of the axis, closing segment included when the wall is closed."""
    return sum(math.dist(a, b) for a, b in axis_segments(wall))


def segment_of(wall: Wall, offset: float) -> tuple[int, float]:
    """``(segment index, distance along it)`` for a distance along the axis.

    A distance that lands exactly on an axis vertex belongs to the segment that
    *starts* there; the very end of the axis belongs to the last segment.
    """
    where = float(offset)
    total = wall_length(wall)
    if not math.isfinite(where) or where < -EPS or where > total + EPS:
        raise ValueError(f"offset {offset!r} is outside wall {wall.id} (axis length {total:g})")
    run = 0.0
    segs = axis_segments(wall)
    for index, (a, b) in enumerate(segs):
        length = math.dist(a, b)
        if where < run + length - EPS:
            return index, max(0.0, where - run)
        run += length
    last = len(segs) - 1
    return last, math.dist(*segs[last])


def _vertex_runs(wall: Wall) -> list[float]:
    """Axis distances of the interior vertices (where one segment turns into the next)."""
    runs: list[float] = []
    run = 0.0
    segs = axis_segments(wall)
    for a, b in segs[:-1]:
        run += math.dist(a, b)
        runs.append(run)
    return runs


def validate_openings(walls: Sequence[Wall], openings: Sequence[Opening]) -> None:
    """Refuse an opening that its wall cannot host - before anything is drawn.

    Refused: two walls or two openings with one id; an opening on a wall that
    is not in the plan; one that runs past the end of its wall's axis; one
    that straddles an axis vertex (a jamb on each side of a corner); two
    openings on one wall whose spans overlap - naming both ids.
    """
    by_id: dict[str, Wall] = {}
    for wall in walls:
        if wall.id in by_id:
            raise ValueError(f"walls: two walls share the id {wall.id!r}")
        by_id[wall.id] = wall
    seen: set[str] = set()
    spans: dict[str, list[tuple[float, float, str]]] = {}
    for opening in openings:
        if opening.id in seen:
            raise ValueError(f"openings: two openings share the id {opening.id!r}")
        seen.add(opening.id)
        wall = by_id.get(opening.wall)
        if wall is None:
            known = ", ".join(sorted(by_id)) or "none"
            raise ValueError(
                f"opening {opening.id}: wall {opening.wall!r} is not in the plan; walls are {known}"
            )
        start = float(opening.offset)
        end = start + float(opening.width)
        total = wall_length(wall)
        if end > total + EPS:
            raise ValueError(
                f"opening {opening.id}: runs past the end of wall {wall.id} - "
                f"{start:g} + {float(opening.width):g} = {end:g} > axis length {total:g}"
            )
        for run in _vertex_runs(wall):
            if start + EPS < run < end - EPS:
                raise ValueError(
                    f"opening {opening.id}: straddles the axis vertex of wall {wall.id} at "
                    f"{run:g} ({start:g}-{end:g}); an opening sits inside one straight segment"
                )
        for other_start, other_end, other_id in spans.get(wall.id, ()):
            if start < other_end - EPS and other_start < end - EPS:
                raise ValueError(
                    f"openings {other_id} and {opening.id} overlap on wall {wall.id}: "
                    f"{other_start:g}-{other_end:g} and {start:g}-{end:g}"
                )
        spans.setdefault(wall.id, []).append((start, end, opening.id))


# -- the ACADMCP_ARCH payload --------------------------------------------------

_BY_KIND = {Wall: "wall", Opening: "opening", Stair: "stair", Room: "room"}


def _jsonable(value):
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def to_payload(obj) -> dict:
    """``{"v": 1, "kind": "wall"|"opening"|"stair"|"room", **fields}`` - JSON-ready.

    ``Opening.kind`` (door / window) and ``Stair.kind`` (straight / l / u) would
    overwrite the payload's own ``kind``, so they travel as ``opening_kind`` and
    ``stair_kind``; ``from_payload`` puts them back.
    """
    kind = _BY_KIND.get(type(obj))
    if kind is None:
        raise TypeError(f"to_payload: not a plan object: {type(obj).__name__}")
    fields = {key: _jsonable(value) for key, value in asdict(obj).items()}
    if "kind" in fields:
        fields[f"{kind}_kind"] = fields.pop("kind")
    return {"v": PAYLOAD_VERSION, "kind": kind, **fields}


def from_payload(payload: dict):
    """The Wall / Opening / Stair / Room a payload describes, validated again."""
    if not isinstance(payload, dict):
        raise ValueError(f"{APP_ID}: a payload must be a dict, got {type(payload).__name__}")
    version = payload.get("v", PAYLOAD_VERSION)
    if version != PAYLOAD_VERSION:
        raise ValueError(
            f"{APP_ID}: payload version {version!r}; this build reads version {PAYLOAD_VERSION}"
        )
    kind = payload.get("kind")
    fields = {k: v for k, v in payload.items() if k not in ("v", "kind")}
    if f"{kind}_kind" in fields:
        fields["kind"] = fields.pop(f"{kind}_kind")
    where = f"{APP_ID} {kind}"
    if kind == "wall":
        return wall_from_dict(fields, where)
    if kind == "opening":
        return opening_from_dict(fields, where)
    if kind == "stair":
        return stair_from_dict(fields, where)
    if kind == "room":
        area = _number(fields.pop("area", 0.0), f"{where}.area", allow_zero=True)
        room = room_from_dict(fields, where)
        return Room(id=room.id, name=room.name, number=room.number, at=room.at, area=area)
    raise ValueError(f"{APP_ID}: unknown payload kind {kind!r}; kinds are {', '.join(KINDS)}")


def encode(payload: dict) -> list[tuple[int, str]]:
    """The mechanical codec with ``app_id=ACADMCP_ARCH`` and the four plan kinds."""
    return _codec.encode(payload, app_id=APP_ID, kinds=KINDS)


def to_values(payload: dict) -> list[str]:
    """Just the chunk strings - what ``entity_set_xdata(handle, APP_ID, values)`` takes."""
    return _codec.to_values(payload, app_id=APP_ID, kinds=KINDS)


def decode(rows: Sequence) -> dict:
    """A payload read back from ``entity_get_xdata``; anything foreign is refused."""
    return _codec.decode(rows, app_id=APP_ID, kinds=KINDS)
