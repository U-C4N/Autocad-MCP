"""``drawing_diff``: two revisions of one drawing, matched by geometric signature.

Spec §10. Each revision is read once into ``EntityRecord``s
(``engineering.understand.snapshot``) and the two are compared in memory.
Nothing in this module writes to a drawing; the revision clouds of
``markup=True`` are drawn by the tool through
``engineering.sheet.revision.add_revision``.

Matching runs in three stages, in this order, and a record leaves the pool the
moment it is matched:

1. **Exact.** Every record has a signature: its type, layer and space, its
   geometry rounded to ``tol`` (points, bulges, closed flag, radius, arc
   angles; the bounding box when it has no points), its text and text height,
   its block name, rotation, scale and attributes. Equal signatures are the
   same entity whatever their handles; where several old records share a
   signature the one with the same handle is taken first. A LINE's two ends
   are sorted, so a line drawn backwards is the same line.
2. **Handle.** A record left over on both sides with the same handle and the
   same type is the same entity, edited. A save that renumbers handles would
   make this stage pair strangers, so it runs only while handles look stable:
   when at least half of the exact matches kept their handle. The report says
   which way it went (``handles_stable``).
3. **Position.** Within one (type, layer, space), a leftover new record is
   paired with the nearest leftover old record whose anchor (the mean of its
   points, else the centre of its box) lies within ``move_tol``; the closest
   pairs are taken first. ``move_tol`` defaults to 2 % of the drawing's robust
   diagonal - the diagonal of the 1st-99th percentile box of every model-space
   anchor of both revisions - so one stray entity kilometres away cannot
   inflate it.

What is left is added (new side) or removed (old side). A matched pair that
is not identical is *changed*, and the row says what changed: ``moved_by``
(dx, dy) when the geometry is the old geometry translated, else ``geometry``
from -> to; ``text``, ``attribs`` (by tag), ``radius``, ``angles``,
``height``, ``rotation``, ``scale``, ``block``, ``layer``, ``space``,
``bulges``, ``closed`` from -> to.

The changes are grouped into clusters for the revision clouds: every changed,
added and removed record contributes its box (a changed one its old box and
its new box), boxes within ``move_tol`` of each other join one cluster, and a
cluster's box is padded by a quarter of ``move_tol`` (never less than ten
``tol``). ``count`` is the number of distinct changes in the cluster.

Block definitions are compared apart (``block_signatures`` /
``block_changes``): the entity records carry INSERTs, not the blocks they
reference, so a redrawn definition changes every drawn copy while every INSERT
record stays identical.

The 2 % move tolerance, the 1st/99th percentiles, the half-of-the-matches
handle rule and the cluster padding are this module's declared defaults, not
values from a standard.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections import defaultdict
from collections.abc import Iterable, Sequence

import config
from engineering.understand.snapshot import EntityRecord, Pt, Snapshot, records_from_doc

__all__ = [
    "CATEGORIES",
    "DEFAULT_LIMIT",
    "DEFAULT_TOL",
    "MOVE_FRACTION",
    "PAD_FRACTION",
    "ROBUST_QUANTILE",
    "anchor",
    "block_changes",
    "block_signatures",
    "cap_report",
    "default_move_tol",
    "diff_snapshots",
    "read_revision",
    "robust_box",
    "signature",
]

DEFAULT_TOL = 0.01
#: The default move tolerance, as a share of the robust diagonal.
MOVE_FRACTION = 0.02
#: The robust box runs from this quantile of the anchors to its complement.
ROBUST_QUANTILE = 0.01
#: A cluster's box is padded by this share of ``move_tol``.
PAD_FRACTION = 0.25
DEFAULT_LIMIT = 200
CATEGORIES = ("changed", "added", "removed")

Box = tuple[float, float, float, float]


def _positive(name: str, value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number; got {value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive number; got {value!r}")
    return number


def _q(value, tol: float):
    """``value`` on the ``tol`` grid; a non-finite value is kept as its repr."""
    number = float(value)
    if not math.isfinite(number):
        return repr(number)
    return int(round(number / tol))


def _qp(point, tol: float) -> tuple:
    return (_q(point[0], tol), _q(point[1], tol))


def signature(rec: EntityRecord, tol: float = DEFAULT_TOL) -> tuple:
    """Everything that makes two records the same entity, rounded to ``tol``."""
    points = tuple(_qp(p, tol) for p in rec.points)
    if rec.type == "LINE" and len(points) == 2:
        points = tuple(sorted(points))
    if points:
        geometry = points
    elif rec.bbox is not None:
        geometry = tuple(_q(v, tol) for v in rec.bbox)
    else:
        geometry = ()
    return (
        rec.type,
        rec.layer,
        rec.space,
        geometry,
        tuple(_q(b, tol) for b in rec.bulges),
        bool(rec.closed),
        None if rec.radius is None else _q(rec.radius, tol),
        None
        if rec.angles is None
        else (_q(rec.angles[0] % 360.0, tol), _q(rec.angles[1] % 360.0, tol)),
        rec.text,
        None if rec.height is None else _q(rec.height, tol),
        rec.block,
        tuple(sorted(rec.attribs)),
        _q(rec.rotation % 360.0, tol),
        (_q(rec.scale[0], tol), _q(rec.scale[1], tol)),
    )


def anchor(rec: EntityRecord) -> Pt | None:
    """The mean of the record's points, else the centre of its box, else None."""
    if rec.points:
        n = len(rec.points)
        point = (sum(p[0] for p in rec.points) / n, sum(p[1] for p in rec.points) / n)
    elif rec.bbox is not None:
        x0, y0, x1, y1 = rec.bbox
        point = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    else:
        return None
    if not (math.isfinite(point[0]) and math.isfinite(point[1])):
        return None
    return point


def _box(rec: EntityRecord) -> Box | None:
    if rec.bbox is not None:
        return tuple(float(v) for v in rec.bbox)
    if rec.points:
        xs = [p[0] for p in rec.points]
        ys = [p[1] for p in rec.points]
        return (min(xs), min(ys), max(xs), max(ys))
    return None


def _quantile_pair(values: list[float], q: float) -> tuple[float, float]:
    values = sorted(values)
    last = len(values) - 1
    low = int(math.floor(q * last))
    return values[low], values[last - low]


def robust_box(*snaps: Snapshot) -> Box | None:
    """The 1st-99th percentile box of the model-space anchors of ``snaps``
    (every space when model space is empty); None when there is no anchor."""
    records = [r for s in snaps for r in s.records]
    anchors = [a for r in records if r.space == "Model" and (a := anchor(r)) is not None]
    if not anchors:
        anchors = [a for r in records if (a := anchor(r)) is not None]
    if not anchors:
        return None
    x0, x1 = _quantile_pair([a[0] for a in anchors], ROBUST_QUANTILE)
    y0, y1 = _quantile_pair([a[1] for a in anchors], ROBUST_QUANTILE)
    return (x0, y0, x1, y1)


def default_move_tol(old: Snapshot, new: Snapshot, tol: float = DEFAULT_TOL) -> float:
    """2 % of the robust diagonal of both revisions, never less than ``tol``."""
    box = robust_box(old, new)
    diagonal = 0.0 if box is None else math.hypot(box[2] - box[0], box[3] - box[1])
    return max(MOVE_FRACTION * diagonal, tol)


def _nearest_pairs(
    olds: Sequence[EntityRecord],
    news: Sequence[EntityRecord],
    left_old: Iterable[int],
    left_new: Iterable[int],
    move_tol: float,
) -> list[tuple[int, int]]:
    """Stage 3: closest pairs first, within one (type, layer, space)."""
    groups: dict[tuple, tuple[list[int], list[int]]] = defaultdict(lambda: ([], []))
    for i in left_old:
        groups[(olds[i].type, olds[i].layer, olds[i].space)][0].append(i)
    for j in left_new:
        groups[(news[j].type, news[j].layer, news[j].space)][1].append(j)
    candidates: list[tuple[float, str, str, int, int]] = []
    for old_ids, new_ids in groups.values():
        if not old_ids or not new_ids:
            continue
        grid: dict[tuple[int, int], list[tuple[int, Pt]]] = defaultdict(list)
        for i in old_ids:
            a = anchor(olds[i])
            if a is not None:
                cell = (math.floor(a[0] / move_tol), math.floor(a[1] / move_tol))
                grid[cell].append((i, a))
        for j in new_ids:
            b = anchor(news[j])
            if b is None:
                continue
            cx, cy = math.floor(b[0] / move_tol), math.floor(b[1] / move_tol)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for i, a in grid.get((cx + dx, cy + dy), ()):
                        distance = math.dist(a, b)
                        if distance <= move_tol:
                            candidates.append((distance, olds[i].handle, news[j].handle, i, j))
    candidates.sort()
    used_old: set[int] = set()
    used_new: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _distance, _old_handle, _new_handle, i, j in candidates:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        pairs.append((i, j))
    return pairs


def _number_changed(a, b, tol: float, *, modulo: float | None = None) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    if modulo is not None:
        a, b = a % modulo, b % modulo
    return _q(a, tol) != _q(b, tol)


def _movement(o: EntityRecord, n: EntityRecord, tol: float) -> dict:
    """``moved_by`` for a pure translation, ``geometry`` for anything else."""
    if o.points and n.points:
        before, after = list(o.points), list(n.points)
    elif not o.points and not n.points:
        if o.bbox is None or n.bbox is None:
            if (o.bbox is None) != (n.bbox is None):
                return {"geometry": {"from": o.bbox, "to": n.bbox}}
            return {}
        before = [(o.bbox[0], o.bbox[1]), (o.bbox[2], o.bbox[3])]
        after = [(n.bbox[0], n.bbox[1]), (n.bbox[2], n.bbox[3])]
    else:
        return {
            "geometry": {"from": [list(p) for p in o.points], "to": [list(p) for p in n.points]}
        }
    a, b = anchor(o), anchor(n)
    if a is None or b is None:
        return {"geometry": {"from": [list(p) for p in before], "to": [list(p) for p in after]}}
    dx, dy = b[0] - a[0], b[1] - a[1]
    half = tol / 2.0
    translated = len(before) == len(after) and all(
        abs(p[0] + dx - q[0]) <= half and abs(p[1] + dy - q[1]) <= half
        for p, q in zip(before, after, strict=True)
    )
    if not translated:
        return {"geometry": {"from": [list(p) for p in before], "to": [list(p) for p in after]}}
    if abs(dx) > half or abs(dy) > half:
        return {"moved_by": [dx, dy]}
    return {}


def _attrib_changes(o: EntityRecord, n: EntityRecord) -> list[dict]:
    before, after = dict(o.attribs), dict(n.attribs)
    return [
        {"tag": tag, "from": before.get(tag), "to": after.get(tag)}
        for tag in sorted(set(before) | set(after))
        if before.get(tag) != after.get(tag)
    ]


def _changes(o: EntityRecord, n: EntityRecord, tol: float) -> dict:
    out: dict = {}
    if o.layer != n.layer:
        out["layer"] = {"from": o.layer, "to": n.layer}
    if o.space != n.space:
        out["space"] = {"from": o.space, "to": n.space}
    out.update(_movement(o, n, tol))
    if tuple(_q(b, tol) for b in o.bulges) != tuple(_q(b, tol) for b in n.bulges):
        out["bulges"] = {"from": list(o.bulges), "to": list(n.bulges)}
    if bool(o.closed) != bool(n.closed):
        out["closed"] = {"from": bool(o.closed), "to": bool(n.closed)}
    if _number_changed(o.radius, n.radius, tol):
        out["radius"] = {"from": o.radius, "to": n.radius}
    if (o.angles is None) != (n.angles is None) or (
        o.angles is not None
        and n.angles is not None
        and any(
            _number_changed(a, b, tol, modulo=360.0)
            for a, b in zip(o.angles, n.angles, strict=True)
        )
    ):
        out["angles"] = {
            "from": None if o.angles is None else list(o.angles),
            "to": None if n.angles is None else list(n.angles),
        }
    if o.text != n.text:
        out["text"] = {"from": o.text, "to": n.text}
    if _number_changed(o.height, n.height, tol):
        out["height"] = {"from": o.height, "to": n.height}
    if o.block != n.block:
        out["block"] = {"from": o.block, "to": n.block}
    attribs = _attrib_changes(o, n)
    if attribs:
        out["attribs"] = attribs
    if _number_changed(o.rotation, n.rotation, tol, modulo=360.0):
        out["rotation"] = {"from": o.rotation, "to": n.rotation}
    if any(_number_changed(a, b, tol) for a, b in zip(o.scale, n.scale, strict=True)):
        out["scale"] = {"from": list(o.scale), "to": list(n.scale)}
    return out


def _row(rec: EntityRecord) -> dict:
    at = anchor(rec)
    return {
        "handle": rec.handle,
        "type": rec.type,
        "layer": rec.layer,
        "space": rec.space,
        "at": None if at is None else [at[0], at[1]],
    }


def _tally(changed: list[dict], added: list[dict], removed: list[dict]) -> tuple[dict, dict]:
    by_layer: dict[str, dict[str, int]] = {}
    by_type: dict[str, dict[str, int]] = {}
    for category, rows in (("changed", changed), ("added", added), ("removed", removed)):
        for row in rows:
            for table, key in ((by_layer, row["layer"]), (by_type, row["type"])):
                counts = table.setdefault(key, {"changed": 0, "added": 0, "removed": 0})
                counts[category] += 1
    return dict(sorted(by_layer.items())), dict(sorted(by_type.items()))


def _box_gap(a: Box, b: Box) -> float:
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return math.hypot(dx, dy)


def _clusters(items: list[tuple[str, Box, int]], *, link: float, pad: float) -> list[dict]:
    """Single-linkage clusters of change boxes, per space, swept along x."""
    parent = list(range(len(items)))

    def find(k: int) -> int:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    by_space: dict[str, list[int]] = defaultdict(list)
    for k, (space, _box_value, _key) in enumerate(items):
        by_space[space].append(k)
    for members in by_space.values():
        members.sort(key=lambda k: items[k][1][0])
        active: list[int] = []
        for k in members:
            box = items[k][1]
            active = [m for m in active if items[m][1][2] + link >= box[0]]
            for m in active:
                if _box_gap(items[m][1], box) <= link:
                    parent[find(k)] = find(m)
            active.append(k)
    grouped: dict[int, list[int]] = defaultdict(list)
    for k in range(len(items)):
        grouped[find(k)].append(k)
    clusters = []
    for members in grouped.values():
        boxes = [items[k][1] for k in members]
        clusters.append(
            {
                "space": items[members[0]][0],
                "box": [
                    min(b[0] for b in boxes) - pad,
                    min(b[1] for b in boxes) - pad,
                    max(b[2] for b in boxes) + pad,
                    max(b[3] for b in boxes) + pad,
                ],
                "count": len({items[k][2] for k in members}),
            }
        )
    clusters.sort(key=lambda c: (c["space"], c["box"]))
    return clusters


def diff_snapshots(
    old: Snapshot, new: Snapshot, *, tol: float = DEFAULT_TOL, move_tol: float | None = None
) -> dict:
    """Match ``new`` against ``old`` and report every change (spec §10).

    Refuses a non-positive ``tol`` or ``move_tol`` before any work.
    """
    tol = _positive("tol", tol)
    move_tol = (
        default_move_tol(old, new, tol) if move_tol is None else _positive("move_tol", move_tol)
    )
    olds = list(old.records)
    news = list(new.records)

    # 1. exact
    pools: dict[tuple, list[int]] = defaultdict(list)
    for i, rec in enumerate(olds):
        pools[signature(rec, tol)].append(i)
    used_old: set[int] = set()
    left_new: list[int] = []
    exact = kept_handle = 0
    for j, rec in enumerate(news):
        pool = pools.get(signature(rec, tol))
        if not pool:
            left_new.append(j)
            continue
        at = next((k for k, i in enumerate(pool) if olds[i].handle == rec.handle), 0)
        i = pool.pop(at)
        used_old.add(i)
        exact += 1
        kept_handle += int(olds[i].handle == rec.handle)
    left_old = [i for i in range(len(olds)) if i not in used_old]
    handles_stable = exact == 0 or 2 * kept_handle >= exact

    pairs: list[tuple[int, int, str]] = []
    # 2. handle
    if handles_stable:
        by_handle = {olds[i].handle: i for i in left_old}
        still_new: list[int] = []
        for j in left_new:
            i = by_handle.get(news[j].handle)
            if i is not None and olds[i].type == news[j].type:
                pairs.append((i, j, "handle"))
                del by_handle[news[j].handle]
            else:
                still_new.append(j)
        left_new = still_new
        taken = {i for i, _j, _how in pairs}
        left_old = [i for i in left_old if i not in taken]

    # 3. position
    pairs.extend(
        (i, j, "position") for i, j in _nearest_pairs(olds, news, left_old, left_new, move_tol)
    )
    taken_old = {i for i, _j, _how in pairs}
    taken_new = {j for _i, j, _how in pairs}

    changed: list[dict] = []
    items: list[tuple[str, Box, int]] = []
    for key, (i, j, how) in enumerate(
        sorted(pairs, key=lambda p: (olds[p[0]].handle, news[p[1]].handle))
    ):
        o, n = olds[i], news[j]
        at = anchor(n)
        changed.append(
            {
                "old_handle": o.handle,
                "new_handle": n.handle,
                "type": n.type,
                "layer": n.layer,
                "space": n.space,
                "matched_by": how,
                "at": None if at is None else [at[0], at[1]],
                "changes": _changes(o, n, tol),
            }
        )
        for rec in (o, n):
            box = _box(rec)
            if box is not None:
                items.append((rec.space, box, key))
    base = len(changed)
    removed_recs = sorted((olds[i] for i in left_old if i not in taken_old), key=lambda r: r.handle)
    added_recs = sorted((news[j] for j in left_new if j not in taken_new), key=lambda r: r.handle)
    for offset, rec in enumerate(removed_recs + added_recs):
        box = _box(rec)
        if box is not None:
            items.append((rec.space, box, base + offset))
    removed = [_row(rec) for rec in removed_recs]
    added = [_row(rec) for rec in added_recs]
    by_layer, by_type = _tally(changed, added, removed)
    pad = max(PAD_FRACTION * move_tol, 10.0 * tol)
    return {
        "tol": tol,
        "move_tol": move_tol,
        "handles_stable": handles_stable,
        "summary": {
            "old_entities": len(olds),
            "new_entities": len(news),
            "unchanged": exact,
            "changed": len(changed),
            "added": len(added),
            "removed": len(removed),
        },
        "by_layer": by_layer,
        "by_type": by_type,
        "changed": changed,
        "added": added,
        "removed": removed,
        "clusters": _clusters(items, link=move_tol, pad=pad),
    }


def cap_report(result: dict, limit: int = DEFAULT_LIMIT) -> dict:
    """``result`` with each list of ``CATEGORIES`` cut to ``limit`` rows and
    ``truncated`` saying how many rows each cut dropped."""
    if int(limit) < 1:
        raise ValueError(f"limit must be at least 1; got {limit!r}")
    capped = dict(result)
    capped["truncated"] = {}
    for category in CATEGORIES:
        rows = list(result.get(category) or [])
        capped[category] = rows[: int(limit)]
        capped["truncated"][category] = max(0, len(rows) - int(limit))
    return capped


# -- block definitions -----------------------------------------------------------------

_BLOCK_ATTRS = (
    "start",
    "end",
    "center",
    "insert",
    "radius",
    "start_angle",
    "end_angle",
    "rotation",
    "height",
    "text",
    "tag",
    "name",
    "xscale",
    "yscale",
    "major_axis",
    "ratio",
)


def _q_value(value, tol: float):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _q(value, tol)
    if isinstance(value, str):
        return value
    try:
        return tuple(_q(v, tol) for v in value)
    except TypeError:
        return repr(value)


def _block_entity_key(entity, tol: float) -> str:
    key: list = [entity.dxftype(), entity.dxf.get("layer", "0")]
    for attr in _BLOCK_ATTRS:
        if entity.dxf.hasattr(attr):
            key.append((attr, _q_value(entity.dxf.get(attr), tol)))
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        key.append(("points", tuple(_q_value(p, tol) for p in entity.get_points("xyb"))))
        key.append(("closed", bool(entity.closed)))
    elif kind == "POLYLINE":
        key.append(("points", tuple(_q_value(v.dxf.location, tol) for v in entity.vertices)))
    elif kind == "MTEXT":
        key.append(("mtext", entity.text))
    return repr(tuple(key))


def block_signatures(doc, *, tol: float = DEFAULT_TOL) -> dict[str, str]:
    """``{block name: digest}`` for every named block definition of an ezdxf
    document: its base point and, per entity, its type, layer and main DXF
    attributes rounded to ``tol``. Layout blocks and anonymous blocks
    (``*U``, ``*D`` ...) are left out - their names are not stable across saves."""
    tol = _positive("tol", tol)
    out: dict[str, str] = {}
    for block in doc.blocks:
        name = block.name
        if block.block_record.is_any_layout or name.startswith("*"):
            continue
        base = _q_value(block.block.dxf.base_point, tol)
        rows = sorted(_block_entity_key(entity, tol) for entity in block)
        out[name] = hashlib.sha256(repr((base, rows)).encode("utf-8")).hexdigest()
    return dict(sorted(out.items()))


def block_changes(old: dict[str, str] | None, new: dict[str, str] | None) -> dict:
    """Block definitions added, removed and redrawn between two revisions."""
    if old is None or new is None:
        return {
            "compared": False,
            "reason": "block definitions are compared only when both revisions are DXF files "
            "or the current document of the headless engine",
        }
    return {
        "compared": True,
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(name for name in set(old) & set(new) if old[name] != new[name]),
    }


def read_revision(path: str, *, tol: float = DEFAULT_TOL) -> tuple[Snapshot, dict[str, str]]:
    """One read of a DXF revision: its records and its block signatures.

    Refused before reading: a file that cannot be stat'ed and one over
    ``MAX_DXF_BYTES`` (the size and the variable named); a file ezdxf cannot
    parse is refused with ezdxf's reason.
    """
    import ezdxf

    limit = int(config.settings.max_dxf_bytes)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ValueError(f"{path}: cannot be read ({exc.strerror or exc})") from exc
    if limit > 0 and size > limit:
        raise ValueError(
            f"{path} is {size} bytes, over the MAX_DXF_BYTES limit of {limit}; "
            "raise MAX_DXF_BYTES to read it"
        )
    try:
        doc = ezdxf.readfile(path)
    except (OSError, ezdxf.DXFError) as exc:
        raise ValueError(f"{path}: not a readable DXF ({exc})") from exc
    return records_from_doc(doc, source=str(path)), block_signatures(doc, tol=tol)
