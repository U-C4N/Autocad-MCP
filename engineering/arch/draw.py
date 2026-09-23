"""The async layer of the architectural track - the only module here that touches a backend.

Everything else under `engineering/arch/` is pure; this module turns its
primitives into entities through the generic contract members (so it runs on
both engines as written) and writes the plan onto the drawing:

* each wall's ``ACADMCP_ARCH`` record on its first face line, each opening's
  on its door leaf or its window frame, each stair's on its first riser line;
* next to each record, under ``ACADMCP_ARCH_H``, the handles of every entity
  that element owns - faces, jambs, poche, the symbol - so a later call can
  take exactly those entities back out and redraw them.

`read_plan` reads the records back. `add_walls` and `add_opening` build on
it: they compute the whole network before and after the change, redraw only
the walls whose outline the change touches, and say which.

Every refusal fires before the first entity is created or erased.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace

from engineering.arch.dimension import exterior_chains
from engineering.arch.lang import tag_prefix
from engineering.arch.layers import ARCH_LAYERS, ARCH_ROLE_LAYER
from engineering.arch.model import (
    APP_ID,
    Opening,
    Room,
    Stair,
    Wall,
    decode,
    from_payload,
    opening_from_dict,
    stair_from_dict,
    to_payload,
    to_values,
    wall_from_dict,
)
from engineering.arch.openings import opening_prims
from engineering.arch.stairs import blondel, stair_prims
from engineering.arch.walls import WallGeometry, wall_geometry_by_wall
from engineering.mech.draw import draw_prims

log = logging.getLogger(__name__)

#: The application that carries an element's owned handles beside its record.
HANDLES_APP = "ACADMCP_ARCH_H"

#: The layers `read_plan` scans for records - never the poche or the dimensions.
RECORD_ROLES: tuple[str, ...] = ("wall", "door", "window", "stair", "room")

#: Model class -> the record kind, read off the object rather than the payload,
#: so the reader does not depend on how a payload spells its own kind.
_KIND = {Wall: "wall", Opening: "opening", Stair: "stair", Room: "room"}


# -- layers -----------------------------------------------------------------------


async def ensure_arch_layers(backend, names) -> tuple[str, ...]:
    """Create the named layers the drawing lacks, with their ``ARCH_LAYERS`` row.

    A live seat refuses ``entity.Layer = name`` on a missing layer (measured
    by the mechanical track), so every draw path ensures its layers first.
    """
    rows = {row[0]: row for row in ARCH_LAYERS}
    existing = {layer.name.lower() for layer in await backend.layer_list()}
    created: list[str] = []
    for name in dict.fromkeys(names):
        if name.lower() in existing:
            continue
        _name, color, linetype, lineweight, _description = rows.get(
            name, (name, 7, "Continuous", 0.25, "")
        )
        await backend.layer_create(name=name, color=color, linetype=linetype, lineweight=lineweight)
        created.append(name)
    return tuple(created)


# -- records --------------------------------------------------------------------


async def _write_record(backend, handle: str, element, owned: list[str]) -> None:
    await backend.entity_set_xdata(handle, APP_ID, to_values(to_payload(element)))
    await backend.entity_set_xdata(handle, HANDLES_APP, list(owned))


async def read_plan(backend) -> dict:
    """Every ``ACADMCP_ARCH`` record on the drawing, rebuilt into model objects.

    ``carriers`` maps ``"<kind>:<id>"`` to the entity that carries the record.
    Reads only; the drawing is never modified.
    """
    plan: dict = {"walls": [], "openings": [], "stairs": [], "rooms": [], "carriers": {}}
    for role in RECORD_ROLES:
        for info in await backend.entity_list(layer_filter=ARCH_ROLE_LAYER[role], limit=100000):
            raw = await backend.entity_get_xdata(info.handle, APP_ID)
            values = (raw.get("xdata") or {}).get(APP_ID) or []
            if not values:
                continue
            element = from_payload(decode(values))
            kind = _KIND[type(element)]
            plan[f"{kind}s"].append(element)
            plan["carriers"][f"{kind}:{element.id}"] = info.handle
    return plan


async def _owned(backend, carrier: str) -> list[str]:
    raw = await backend.entity_get_xdata(carrier, HANDLES_APP)
    return [str(h) for h in ((raw.get("xdata") or {}).get(HANDLES_APP) or [])]


async def _erase(backend, plan: dict, wall_id: str) -> list[str]:
    """Take a wall, its openings and their poche back out. Returns handles already gone."""
    carriers = [plan["carriers"][f"wall:{wall_id}"]]
    carriers += [
        plan["carriers"][f"opening:{op.id}"] for op in plan["openings"] if op.wall == wall_id
    ]
    missing: list[str] = []
    for carrier in carriers:
        for handle in await _owned(backend, carrier) or [carrier]:
            try:
                await backend.entity_delete(handle)
            except Exception as exc:  # erased by hand since it was drawn: nothing to take back
                log.info("arch: %s was already gone (%s)", handle, exc)
                missing.append(handle)
    return missing


# -- walls ------------------------------------------------------------------------


def _signature(geometry: WallGeometry) -> tuple:
    """What a wall looks like, rounded - equal signatures mean nothing to redraw."""

    def seg(line):
        a = (round(line.p1[0], 6), round(line.p1[1], 6))
        b = (round(line.p2[0], 6), round(line.p2[1], 6))
        return tuple(sorted((a, b)))

    def ring(points):
        pts = [(round(x, 6), round(y, 6)) for x, y in points]
        k = pts.index(min(pts))
        return tuple(pts[k:] + pts[:k])

    return (
        frozenset(seg(line) for line in geometry.faces),
        frozenset(seg(line) for line in geometry.jambs),
        frozenset(ring(region) for region in geometry.regions),
        frozenset(entry["element"] for entry in geometry.omitted),
    )


async def _draw_wall(backend, wall, geometry: WallGeometry, openings) -> dict:
    """One wall's faces, jambs, poche and hosted symbols, with their records."""
    skipped = {entry["element"] for entry in geometry.omitted}
    symbols = [(op, opening_prims(wall, op)) for op in openings if op.id not in skipped]
    # Faces first, so owned[0] - the record carrier - is the wall's first face line;
    # the poche follows on the wall-hatch layer, in each material's pattern.
    drawn = await draw_prims(
        backend,
        [*geometry.faces, *geometry.jambs, *geometry.hatches],
        role_layer=ARCH_ROLE_LAYER,
    )
    owned = list(drawn["handles"])
    done: list[dict] = []
    for op, prims in symbols:
        placed = await draw_prims(backend, prims, role_layer=ARCH_ROLE_LAYER)
        await _write_record(backend, placed["handles"][0], op, placed["handles"])
        done.append(
            {
                "id": op.id,
                "tag": op.tag,
                "record": placed["handles"][0],
                "handles": placed["handles"],
            }
        )
    await _write_record(backend, owned[0], wall, owned)
    return {
        "wall": {"id": wall.id, "record": owned[0], "handles": owned},
        "openings": done,
        "omitted": list(geometry.omitted),
    }


def _collect(results: list[dict]) -> dict:
    return {
        "walls": [r["wall"] for r in results],
        "openings": [op for r in results for op in r["openings"]],
        "handles": [
            h
            for r in results
            for h in (*r["wall"]["handles"], *(x for op in r["openings"] for x in op["handles"]))
        ],
        "omitted": [entry for r in results for entry in r["omitted"]],
    }


async def draw_walls(backend, walls, openings=(), *, poche=True) -> dict:
    """Draw a wall network and its openings, writing every record. Refusals first."""
    walls = list(walls)
    openings = list(openings)
    geometry = wall_geometry_by_wall(walls, openings, poche=poche)
    for wall in walls:  # every symbol is validated before the first entity
        for op in openings:
            if op.wall == wall.id:
                opening_prims(wall, op)
    results = [
        await _draw_wall(
            backend, wall, geometry[wall.id], [op for op in openings if op.wall == wall.id]
        )
        for wall in walls
    ]
    return {**_collect(results), "backend": backend.name}


async def add_walls(backend, walls, *, poche=True) -> dict:
    """Add walls to the plan on the drawing, redrawing the drawn walls they join."""
    if isinstance(walls, dict) or not isinstance(walls, (list, tuple)) or not walls:
        raise ValueError("walls: give a list of at least one wall {id, axis, thickness, ...}")
    new = [wall_from_dict(item, f"walls[{i}]") for i, item in enumerate(walls)]
    plan = await read_plan(backend)
    drawn = {wall.id for wall in plan["walls"]}
    for i, wall in enumerate(new):
        if wall.id in drawn:
            raise ValueError(
                f"walls[{i}].id: {wall.id!r} is already drawn; choose a new id "
                f"(drawn: {', '.join(sorted(drawn))})"
            )
    before = (
        wall_geometry_by_wall(plan["walls"], plan["openings"], poche=poche) if plan["walls"] else {}
    )
    network = [*plan["walls"], *new]
    after = wall_geometry_by_wall(network, plan["openings"], poche=poche)
    changed = [
        wall.id
        for wall in plan["walls"]
        if _signature(before[wall.id]) != _signature(after[wall.id])
    ]
    for wall_id in changed:
        was = {entry["element"] for entry in before[wall_id].omitted}
        for entry in after[wall_id].omitted:
            if entry["element"] not in was:
                raise ValueError(
                    f"the new walls would leave opening {entry['element']!r} of wall "
                    f"{wall_id!r} uncuttable ({entry['reason']}); move the opening or the wall"
                )
    by_id = {wall.id: wall for wall in network}
    targets = [*changed, *(wall.id for wall in new)]
    for wall_id in targets:
        for op in plan["openings"]:
            if op.wall == wall_id:
                opening_prims(by_id[wall_id], op)

    missing: list[str] = []
    for wall_id in changed:
        missing += await _erase(backend, plan, wall_id)
    results = [
        await _draw_wall(
            backend,
            by_id[wall_id],
            after[wall_id],
            [op for op in plan["openings"] if op.wall == wall_id],
        )
        for wall_id in targets
    ]
    return {
        **_collect(results),
        "added": [wall.id for wall in new],
        "redrawn": changed,
        "already_erased": missing,
        "backend": backend.name,
    }


# -- openings ---------------------------------------------------------------------


def assign_tags(openings, existing=(), *, lang: str = "en") -> tuple[Opening, ...]:
    """Give every untagged opening the next free tag of its kind: D1, W2 (EN) / K1, P2 (TR)."""
    used = {op.tag for op in (*existing, *openings) if op.tag}
    out: list[Opening] = []
    for op in openings:
        if op.tag:
            out.append(op)
            continue
        prefix = tag_prefix(op.kind, lang)
        number = 1
        while f"{prefix}{number}" in used:
            number += 1
        used.add(f"{prefix}{number}")
        out.append(replace(op, tag=f"{prefix}{number}"))
    return tuple(out)


async def add_opening(backend, opening: dict, *, lang: str = "en", poche: bool = True) -> dict:
    """Host a door or a window in a drawn wall, found by id, and redraw that wall cut."""
    if not isinstance(opening, dict):
        raise ValueError(f"opening: expected a dict, got {type(opening).__name__}")
    raw = dict(opening)
    auto_id = not raw.get("id")
    if auto_id:
        raw["id"] = "__new__"
    op = opening_from_dict(raw, "opening")
    plan = await read_plan(backend)
    walls = {wall.id: wall for wall in plan["walls"]}
    if op.wall not in walls:
        names = ", ".join(sorted(walls)) or "none - draw one with arch_wall"
        raise ValueError(f"opening.wall: {op.wall!r} is not a wall on this drawing; walls: {names}")
    (op,) = assign_tags([op], plan["openings"], lang=lang)
    if auto_id:
        op = replace(op, id=op.tag)
    if op.id in {other.id for other in plan["openings"]}:
        raise ValueError(f"opening.id: {op.id!r} is already drawn; choose a new id")
    hosted = [other for other in plan["openings"] if other.wall == op.wall] + [op]
    geometry = wall_geometry_by_wall(plan["walls"], [*plan["openings"], op], poche=poche)
    for entry in geometry[op.wall].omitted:
        if entry["element"] == op.id:
            raise ValueError(f"opening {op.id!r} cannot be cut: {entry['reason']}")
    wall = walls[op.wall]
    for other in hosted:
        opening_prims(wall, other)
    missing = await _erase(backend, plan, op.wall)
    result = await _draw_wall(backend, wall, geometry[op.wall], hosted)
    return {
        **_collect([result]),
        "opening": op.id,
        "tag": op.tag,
        "redrawn": [op.wall],
        "already_erased": missing,
        "backend": backend.name,
    }


# -- stairs -----------------------------------------------------------------------


async def draw_stair(backend, stair: Stair, *, lang: str = "en", scale=50) -> dict:
    """A stair in plan with its record; the Blondel value is reported, never enforced."""
    prims = stair_prims(stair, lang=lang, scale=scale)
    check = blondel(stair.riser_height, stair.going)
    drawn = await draw_prims(backend, prims, role_layer=ARCH_ROLE_LAYER)
    await _write_record(backend, drawn["handles"][0], stair, drawn["handles"])
    flights = 1 if stair.kind == "straight" else 2
    return {
        "stair": stair.id,
        "record": drawn["handles"][0],
        "handles": drawn["handles"],
        "risers": stair.risers,
        "treads": stair.risers - flights,
        "blondel": check,
        "backend": backend.name,
    }


async def add_stair(backend, stair: dict, *, lang: str = "en", scale=50) -> dict:
    """`draw_stair` for a stair given as a dict, refusing an id already on the drawing."""
    if not isinstance(stair, dict):
        raise ValueError(f"stair: expected a dict, got {type(stair).__name__}")
    model = stair_from_dict(stair, "stair")
    stair_prims(model, lang=lang, scale=scale)
    plan = await read_plan(backend)
    if model.id in {other.id for other in plan["stairs"]}:
        raise ValueError(f"stair.id: {model.id!r} is already drawn; choose a new id")
    return await draw_stair(backend, model, lang=lang, scale=scale)


# -- dimensions ---------------------------------------------------------------------


async def draw_chains(
    backend, walls, openings, *, sides=("bottom", "left"), scale=50, first_offset=None, step=None
) -> dict:
    """The exterior chains as real linear DIMENSIONs on the dimension layer."""
    intents = exterior_chains(
        walls, openings, sides=sides, first_offset=first_offset, step=step, scale=scale
    )
    layer = ARCH_ROLE_LAYER["dim"]
    await ensure_arch_layers(backend, [layer])
    handles: list[str] = []
    rows: list[dict] = []
    for intent in intents:
        horizontal = abs(intent.p2[1] - intent.p1[1]) <= abs(intent.p2[0] - intent.p1[0])
        info = await backend.dimension_linear(
            intent.p1[0],
            intent.p1[1],
            intent.p2[0],
            intent.p2[1],
            intent.text_at[0],
            intent.text_at[1],
            0.0 if horizontal else 90.0,
            layer,
        )
        handles.append(info.handle)
        rows.append(
            {
                "row": intent.feature,
                "from": list(intent.p1),
                "to": list(intent.p2),
                "value": math.dist(intent.p1, intent.p2),
            }
        )
    return {
        "count": len(handles),
        "handles": handles,
        "dimensions": rows,
        "text_height": (
            "set by the current dimension style; at 1:{0:g} set DIMSCALE={0:g} with "
            "dimstyle_modify so the text plots at its paper height".format(float(scale))
        ),
        "backend": backend.name,
    }
