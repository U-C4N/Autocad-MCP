"""``mech_part_revise``: change a parameter of a drawn part and rebuild it in place (1.8-19).

The part model is on the drawing (``ACADMCP_MECH`` on the part's anchor), and
since 1.8.0 every entity it drew carries ``{part, feature, view, i}``. A
revision is therefore an edit of the model followed by the *smallest* edit of
the drawing that makes it the new model's drawing again:

1. The anchor payload is read (``read_part``) and the changes are applied as
   path edits on ``part_to_dict(old)`` - ``{"segments[1].length": 70,
   "features.h1.diameter": 10, "features.k1": null, "features+": {...}}``;
   ``build_part(new)`` refuses by path before anything is touched.
2. The OLD expected primitives come from the stored view records, the NEW ones
   from the same views laid out again for the new model, exactly as
   ``mech_part_draw`` / ``mech_view_add`` laid them out.
3. A primitive's key is ``(view, feature, type, k)``, ``k`` its ordinal among
   the primitives of that view, feature and type - so adding a chamfer does not
   shift the key of every line drawn after it.
4. **The drift check comes first** (``engineering/mech/drift.py``, one
   snapshot): a tagged entity whose real geometry is not the OLD expected one
   within the verification epsilon refuses the whole call
   (``hand_edited_tagged_entity``, the handles named, nothing written); a part
   drawn before 1.8.0 (no gen stamp) or whose views the engine no longer
   reproduces refuses as ``model_drift``. ``overwrite=True`` replaces the hand
   edits with the model. A layer, colour, linetype or lineweight changed by
   hand is not geometry and is kept - on an entity edited in place it is never
   touched, and an entity that has to be drawn again gets it back
   (``handle_map`` row ``kept``).
5. A key on both sides keeps its entity: LINE / CIRCLE / ARC are edited in
   place (``entity_edit_geometry``), so their handles survive; LWPOLYLINE, TEXT
   and HATCH are deleted and created again when they changed, and
   ``handle_map`` lists ``{old, new, reason}`` for every handle that did not
   survive. A key only on the old side is deleted, one only on the new side is
   drawn.
6. The dimensions of every dimensioned view are found by their tags, deleted
   and placed again by ``mech_dimension_part``'s engine with the style and fits
   each view was dimensioned with - **their handles change**. The hole table is
   rewritten; a balloon whose target was replaced is re-linked. A part drawn
   before 1.8.0 (``overwrite=True`` only) has no tags on its dimensions or its
   table: a dimension is found by its definition points - the two points a
   dimension of the OLD model's view measures, under any style - and the
   table's pieces by the box its rows lay out from the stored ``hole_table``
   line. Both are replaced; an untagged dimension on a dimensioned view that
   matches none is left and named (``dimensions.legacy.left``).
7. The payload becomes the new part, the gen stamp and each view's primitive
   digest; the change is appended to the part's model history
   (``engineering/mech/history.py``).
8. All of it runs inside one transaction with the shielded rollback of
   ``mech_part_from_spec`` (``draw.guarded_transaction``).
9. ``verify=True`` measures the requirements stored in the drawing
   (``ACADMCP_REQUIREMENTS``) on the new geometry and returns the summary.

``dry_run=True`` stops after step 5's plan: the counts and a preview whose
preconditions are the fingerprints of every entity the call would touch; an
apply carrying the preview's ``revision`` / ``preconditions`` is refused
``stale_revision`` if any of them moved.

Not in this release (1.8.1): reconciling a revision against a source spec, and
a mode that keeps a hand edit. A part with a detail view, or with two views
carrying the same view tag (a left and a right end view), is refused by name
(``refused``, reason ``detail_view`` / ``repeated_view_tag``), as are a payload
with no view (``no_views``) and a stored model that no longer builds
(``model_unbuildable``) - before anything is written.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from engineering.errors import (
    HandEditedTaggedEntityError,
    InvalidArgsError,
    ModelDriftError,
    RevisionRefusedError,
)
from engineering.mech import drift as D
from engineering.mech.primitives import layer_for

#: How many drift rows a refusal carries.
REFUSAL_ROWS = 20

_NAME = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)((?:\[\d+\])*)$")
_FEATURE_ID = re.compile(r"^([^\[\]]+)((?:\[\d+\])*)$")
_INDEX = re.compile(r"\[(\d+)\]")
_APPENDABLE = ("features", "segments", "outline")


# -- path edits ---------------------------------------------------------------------


def _bad(path: str, why: str) -> InvalidArgsError:
    return InvalidArgsError(f"invalid_args: changes[{path!r}]: {why}")


def _tokens(path: Any) -> tuple[list, bool]:
    """``(tokens, append)``: a token is a dict key (str), a list index (int) or
    a feature id (``("id", name)``, the segment after ``features.``)."""
    if not isinstance(path, str) or not path.strip():
        raise InvalidArgsError(
            "invalid_args: changes: every key is a path such as 'segments[1].length', "
            f"'features.h1.diameter' or 'features+'; got {path!r}"
        )
    text = path.strip()
    append = text.endswith("+")
    if append:
        text = text[:-1]
    tokens: list = []
    for number, part in enumerate(text.split(".")):
        by_id = number == 1 and tokens == ["features"]
        match = (_FEATURE_ID if by_id else _NAME).match(part)
        if match is None:
            raise _bad(path, f"{part!r} is not a key, a key[index] or a feature id")
        tokens.append(("id", match.group(1)) if by_id else match.group(1))
        tokens.extend(int(i) for i in _INDEX.findall(match.group(2)))
    return tokens, append


def _feature_ids(features) -> list[str]:
    return [str(f.get("id")) for f in features if isinstance(f, dict)]


def _step(node, token, path: str):
    if isinstance(token, tuple):
        if not isinstance(node, list):
            raise _bad(path, "a feature id names an entry of the features list")
        for item in node:
            if isinstance(item, dict) and str(item.get("id")) == token[1]:
                return item
        raise _bad(
            path, f"no feature {token[1]!r}; the part's features are {_feature_ids(node) or 'none'}"
        )
    if isinstance(token, int):
        if not isinstance(node, list):
            raise _bad(path, f"[{token}] indexes a list, and this is not one")
        if not 0 <= token < len(node):
            raise _bad(path, f"index {token} is out of range (0..{len(node) - 1})")
        return node[token]
    if not isinstance(node, dict):
        raise _bad(path, f"{token!r} names a key, and this is not an object")
    if token not in node:
        raise _bad(path, f"no key {token!r}; it has {sorted(node)}")
    return node[token]


def _feature_index(features: list, name: str) -> int | None:
    for index, item in enumerate(features):
        if isinstance(item, dict) and str(item.get("id")) == name:
            return index
    return None


def apply_changes(old: dict, changes: Any) -> tuple[dict, dict]:
    """``(new part dict, {path: [old, new]})``: ``changes`` applied in order.

    Paths: ``name`` / ``material`` / ``thickness``; ``segments[i].<key>``;
    ``outline[i]``; ``features.<id>.<key>``; ``features.<id>`` (``null``
    removes the feature, an object replaces it); ``segments[i]`` /
    ``outline[i]`` (``null`` removes the entry); ``features+`` /
    ``segments+`` / ``outline+`` (append one entry, or a list of feature or
    segment objects). Every refusal names its path; ``kind`` cannot change.
    """
    if not isinstance(changes, dict) or not changes:
        raise InvalidArgsError(
            "invalid_args: changes: expected a non-empty object of {path: value}, e.g. "
            '{"segments[1].length": 70, "features.bore.diameter": 24, "features.r1": null, '
            '"features+": {"kind": "chamfer", "id": "c2", "at": "end", "size": 1}}'
        )
    new = copy.deepcopy(old)
    diff: dict[str, list] = {}
    for raw_path, value in changes.items():
        tokens, append = _tokens(raw_path)
        path = str(raw_path).strip()
        top = tokens[0]
        if top == "kind":
            raise _bad(path, "a part cannot change kind; draw the other kind as a new part")
        if top not in new:
            raise _bad(path, f"the part has no {top!r}; it has {sorted(new)}")
        if append:
            if len(tokens) != 1 or top not in _APPENDABLE:
                raise _bad(path, f"'+' appends to {', '.join(a + '+' for a in _APPENDABLE)} only")
            target = new[top]
            if not isinstance(target, list):
                raise _bad(path, f"{top!r} is not a list on this part")
            many = top != "outline" and isinstance(value, list)
            items = list(value) if many else [value]
            if not items:
                raise _bad(path, "nothing to append")
            for item in items:
                if top != "outline" and not isinstance(item, dict):
                    raise _bad(path, f"expected an object for each {top[:-1]}, got {item!r}")
                target.append(copy.deepcopy(item))
                if top == "features":
                    diff[f"features.{item.get('id')}"] = [None, copy.deepcopy(item)]
                else:
                    diff[f"{top}[{len(target) - 1}]"] = [None, copy.deepcopy(item)]
            continue
        if len(tokens) == 1:
            if isinstance(new[top], list):
                raise _bad(path, f"{top!r} is a list: edit an entry, or append with '{top}+'")
            diff[path] = [copy.deepcopy(new[top]), copy.deepcopy(value)]
            new[top] = copy.deepcopy(value)
            continue
        node = new
        for token in tokens[:-1]:
            node = _step(node, token, path)
        last = tokens[-1]
        if isinstance(last, tuple):
            index = _feature_index(node, last[1]) if isinstance(node, list) else None
            if index is None:
                _step(node, last, path)  # raises, naming the ids
                continue  # pragma: no cover - _step raised
            before = copy.deepcopy(node[index])
            if value is None:
                del node[index]
            elif isinstance(value, dict):
                node[index] = copy.deepcopy(value)
            else:
                raise _bad(
                    path, f"a feature is replaced by an object or removed by null, not {value!r}"
                )
            diff[path] = [before, copy.deepcopy(value)]
        elif isinstance(last, int):
            before = copy.deepcopy(_step(node, last, path))
            if value is None:
                del node[last]
            else:
                node[last] = copy.deepcopy(value)
            diff[path] = [before, copy.deepcopy(value)]
        else:
            if not isinstance(node, dict):
                raise _bad(path, f"{last!r} names a key, and this is not an object")
            before = copy.deepcopy(node.get(last))
            node[last] = copy.deepcopy(value)
            diff[path] = [before, copy.deepcopy(value)]
    return new, diff


# -- the new layout -------------------------------------------------------------------


@dataclass
class Layout:
    records: list[dict]
    prims: list[tuple]
    omitted: list[dict]


def relayout(part, payload: dict) -> Layout:
    """Every stored view of ``part``, laid out the way it was first laid out.

    A record written by ``mech_part_draw`` (its ``kw`` carries the scale only)
    is placed on its projection axis from the first view with the default gap;
    one added by ``mech_view_add`` is placed the way ``add_view`` placed it -
    from the first view, with its recorded side and gap, stepped clear of the
    views before it. The flags (``dimensioned``) and dimension settings carry
    over; every record gains the digest of its new primitives.
    """
    from engineering.mech.draw import (
        DEFAULT_GAP,
        _clear_of,
        _offset_for,
        _placed,
        _placement,
        _to_wcs,
        _view_from_record,
        _view_record,
    )
    from engineering.mech.views import layout_origin

    origin, rotation = _placement(payload)
    projection = str((payload.get("placement") or {}).get("projection", "first"))
    records: list[dict] = []
    prims: list[tuple] = []
    omitted: list[dict] = []
    first = None
    for number, old in enumerate(payload.get("views") or []):
        kw = dict(old.get("kw") or {})
        try:
            view = _view_from_record(part, payload, old)
        except ValueError as exc:
            raise InvalidArgsError(
                f"invalid_args: changes: view {number} ({D.record_view_tag(old)}) cannot be "
                f"drawn for the revised part: {exc}; nothing was changed."
            ) from exc
        if first is None:
            offset = _offset_for(view, origin)
        else:
            parent = _placed(first[0], first[1])
            if "side" not in kw:
                lower_left = layout_origin(
                    parent, view.kind, projection=projection, gap=DEFAULT_GAP, child=view
                )
            else:
                gap = float(kw.get("gap", DEFAULT_GAP))
                lower_left = layout_origin(
                    parent,
                    view.kind,
                    side=str(kw.get("side", "right")),
                    projection=projection,
                    gap=gap,
                    child=view,
                )
                lower_left = _clear_of(
                    records,
                    lower_left,
                    view.bbox[2] - view.bbox[0],
                    view.bbox[3] - view.bbox[1],
                    parent.bbox,
                    gap,
                )
            offset = _offset_for(view, lower_left)
        wcs = _to_wcs(view.prims, offset, origin, rotation)
        record = _view_record(view, offset, kw, origin, rotation, prims=wcs)
        record["dimensioned"] = bool(old.get("dimensioned"))
        if "dim" in old:
            record["dim"] = old["dim"]
        records.append(record)
        prims.append(tuple(wcs))
        omitted.extend(view.omitted)
        if first is None:
            first = (view, offset)
    return Layout(records, prims, omitted)


# -- keys -------------------------------------------------------------------------------


def keyed(entries) -> dict[tuple, tuple[int, Any]]:
    """``{(view, feature, type, k): (index, prim)}`` for ``(view, index, prim)``
    entries in index order; ``k`` counts within (view, feature, type)."""
    counters: dict[tuple, int] = {}
    out: dict[tuple, tuple[int, Any]] = {}
    for view, index, prim in entries:
        group = (view, str(getattr(prim, "feature", "") or ""), D.prim_type(prim))
        k = counters.get(group, 0)
        counters[group] = k + 1
        out[(*group, k)] = (index, prim)
    return out


def _refuse_layouts_not_revised(part_id: str, records: list[dict]) -> None:
    """Refuse, by name and before anything is written, the layouts this release
    does not revise: a ``RevisionRefusedError`` (kind ``refused``), never a
    plain ``ValueError`` - that reaches the client as ``failed`` ("attempted
    and broke"), which this is not."""
    tags = [D.record_view_tag(r) for r in records]
    details = [D.record_view_tag(r) for r in records if r.get("kind") == "detail"]
    if details:
        raise RevisionRefusedError(
            f"{part_id}: a part with a detail view ({', '.join(details)}) is not revised in "
            "this release (1.8.1); nothing was changed. Draw the revised part with "
            "mech_part_draw and add the detail again.",
            part_id=part_id,
            reason="detail_view",
            views=details,
        )
    twice = sorted({t for t in tags if tags.count(t) > 1})
    if twice:
        raise RevisionRefusedError(
            f"{part_id}: views {twice} are drawn more than once (a left and a right end view "
            "carry the same tag), so their entities cannot be told apart; a revision of such a "
            "part is 1.8.1. Nothing was changed.",
            part_id=part_id,
            reason="repeated_view_tag",
            views=twice,
        )


# -- the plan ---------------------------------------------------------------------------


@dataclass
class Plan:
    part_id: str
    keep: list[dict] = field(default_factory=list)
    edit: list[dict] = field(default_factory=list)
    recreate: list[dict] = field(default_factory=list)
    delete: list[dict] = field(default_factory=list)
    create: list[dict] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    hole_table: list[str] = field(default_factory=list)
    redimension: list[int] = field(default_factory=list)
    balloons: list[tuple[str, dict]] = field(default_factory=list)
    #: a part drawn before 1.8.0: its untagged dimensions and hole-table pieces
    #: found by the old model's own geometry, and the untagged dimensions on a
    #: dimensioned view that matched none of them (left on the sheet, named)
    legacy_dims: list[str] = field(default_factory=list)
    legacy_table: list[str] = field(default_factory=list)
    legacy_left: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "kept": len(self.keep),
            "edited_in_place": len(self.edit),
            "recreated": len(self.recreate),
            "deleted": len(self.delete),
            "created": len(self.create),
            "dimensions_redrawn": len(self.dimensions),
            "views_redimensioned": len(self.redimension),
        }

    def touched(self) -> list[str]:
        handles = [row["handle"] for row in self.keep + self.edit + self.recreate + self.delete]
        return list(dict.fromkeys(handles + self.dimensions + self.hole_table))


#: The display properties a revision carries from an entity it redraws onto
#: the new one when they were set by hand: what ``draw_prims`` writes is the
#: role layer, and ByLayer for the rest.
_BYLAYER = {"color": 256, "linetype": "BYLAYER", "lineweight": -1}


def hand_properties(rec, role_layer: str) -> dict:
    """The layer / colour / linetype / lineweight of an entity that are not
    what the model draws (a hand edit, which is not geometry and is kept)."""
    out: dict = {}
    if str(rec.layer) != str(role_layer):
        out["layer"] = str(rec.layer)
    color = int(getattr(rec, "color", 256))
    if color != _BYLAYER["color"]:
        out["color"] = color
    linetype = str(getattr(rec, "linetype", "BYLAYER") or "BYLAYER")
    if linetype.upper() != _BYLAYER["linetype"]:
        out["linetype"] = linetype
    lineweight = int(getattr(rec, "lineweight", -1))
    if lineweight != _BYLAYER["lineweight"]:
        out["lineweight"] = lineweight
    return out


def _untagged(rec) -> bool:
    payload = D._payload_of(rec)
    return payload is None or payload.get("kind") != "feature"


def _measured_points(part, payload: dict, record: dict, thresholds) -> list[tuple]:
    """Every ``(p1, p2)`` in WCS a dimension of this stored view measures,
    under each style and hole-table threshold ``dimension_part`` could have
    used - the points a DIMENSION keeps as its definition points."""
    from engineering.mech.dimension import DIM_STYLES, dimension_intents, layout_dimensions
    from engineering.mech.draw import _placement, _turn, _view_from_record

    origin, rotation = _placement(payload)
    offset = record["offset"]
    view = _view_from_record(part, payload, record)
    pairs: list[tuple] = []
    for style in DIM_STYLES:
        for threshold in thresholds:
            try:
                intents = dimension_intents(
                    part, view, style=style, hole_table_threshold=int(threshold)
                )
                placed = layout_dimensions(view, intents)
            except ValueError:
                continue
            for intent in placed:
                pairs.append(
                    tuple(
                        _turn((p[0] + offset[0], p[1] + offset[1]), origin, rotation)
                        for p in (intent.p1, intent.p2)
                    )
                )
    return pairs


def _inside(point, box, eps: float) -> bool:
    return box[0] - eps <= point[0] <= box[2] + eps and box[1] - eps <= point[1] <= box[3] + eps


def legacy_dimensions(part, payload: dict, records, dimensioned: dict, eps: float):
    """``(found, left)`` on a part drawn before 1.8.0, whose dimensions carry
    no tag: ``found`` the untagged DIMENSIONs whose definition points are the
    two points a dimension of the OLD model's view measures (what
    ``dimension_part`` drew there), ``left`` the other untagged DIMENSIONs
    with a definition point inside a dimensioned view - not ours to delete,
    so they stay and are named."""
    stored = list(payload.get("views") or [])
    pairs: list[tuple] = []
    boxes: list = []
    for number in dimensioned.values():
        record = stored[number]
        thresholds = {8, int((record.get("dim") or {}).get("hole_table_threshold", 8))}
        pairs.extend(_measured_points(part, payload, record, sorted(thresholds)))
        boxes.append(record.get("bbox"))
    found: list[str] = []
    left: list[str] = []
    for rec in records:
        if rec.type != "DIMENSION" or not _untagged(rec) or rec.dimension is None:
            continue
        defpoints = [p for p in rec.dimension[1] if p is not None]
        hit = any(all(any(D._near(p, q, eps) for q in defpoints) for p in pair) for pair in pairs)
        if hit:
            found.append(str(rec.handle))
        elif any(box and _inside(p, box, eps) for p in defpoints for box in boxes):
            left.append(str(rec.handle))
    return found, left


def legacy_table_pieces(part, payload: dict, records, dimensioned: dict, eps: float) -> list[str]:
    """The pieces of a hole table drawn before 1.8.0: the headless engine
    draws a table as lines and texts and stored the first of them as
    ``hole_table``; the rest are the untagged lines and texts on its layer
    inside the box the OLD model's rows lay out from that line's corner.
    A live TABLE is one entity, the stored handle alone."""
    from engineering.annotation import prepare_table_layout
    from engineering.mech.dimension import hole_table_rows
    from engineering.mech.draw import _view_from_record

    stored = str(payload.get("hole_table") or "")
    first = next((rec for rec in records if str(rec.handle) == stored), None)
    if first is None or first.type != "LINE" or len(first.points or ()) != 2:
        return []
    views = list(payload.get("views") or [])
    rows = []
    for number in sorted(dimensioned.values()):
        record = views[number]
        threshold = int((record.get("dim") or {}).get("hole_table_threshold", 8))
        rows = hole_table_rows(
            part, _view_from_record(part, payload, record), hole_table_threshold=threshold
        )
        if rows:
            break
    if not rows:
        return []
    layout = prepare_table_layout(
        [[r["tag"], f"{r['d']:g}", str(r["qty"]), r["note"]] for r in rows],
        ["TAG", "DIA", "QTY", "NOTE"],
        None,
        7.0,
        2.5,
        "HOLE TABLE",
    )
    x0 = min(p[0] for p in first.points)
    y0 = max(p[1] for p in first.points)
    box = (x0, y0 - layout.height, x0 + layout.width, y0)
    return [
        str(rec.handle)
        for rec in records
        if str(rec.handle) != stored
        and rec.type in ("LINE", "MTEXT", "TEXT")
        and str(rec.layer) == str(first.layer)
        and _untagged(rec)
        and rec.points
        and all(_inside(p, box, eps) for p in rec.points)
    ]


def make_plan(report, layout: Layout, payload: dict, records, stamped: bool, old_part=None) -> Plan:
    plan = Plan(part_id=report.part_id)
    old_entries = [(e.view, e.index, e.prim) for e in report.expected if not e.marker]
    old = keyed(old_entries)
    new_entries = [
        (D.record_view_tag(record), index, prim)
        for record, prims in zip(layout.records, layout.prims, strict=True)
        for index, prim in enumerate(prims)
    ]
    new = keyed(new_entries)
    eps = max(report.epsilon, D.epsilon_for([p for _v, _i, p in new_entries]))
    hatch_eps = max(eps, D.HATCH_RELATIVE_EPSILON * eps / D.RELATIVE_EPSILON)

    claimed: set[str] = set()
    for key, (new_index, new_prim) in new.items():
        view = key[0]
        row = {"key": key, "view": view, "index": new_index, "prim": new_prim}
        found = old.get(key)
        entity = report.matched.get((view, found[0], False)) if found else None
        if entity is None:
            plan.create.append(row)
            continue
        claimed.add(entity.handle)
        old_prim = found[1]
        row.update(handle=entity.handle, old_prim=old_prim, entity=entity)
        row["retag"] = entity.index != new_index
        # what was set by hand is not geometry: a redrawn entity gets it back,
        # and a role change moves only an entity still on its old role layer
        row["kept"] = hand_properties(entity.record, layer_for(old_prim))
        role_moved = layer_for(old_prim) != layer_for(new_prim)
        row["relayer"] = layer_for(new_prim) if role_moved and "layer" not in row["kept"] else None
        in_place = D.prim_type(new_prim) in ("LINE", "CIRCLE", "ARC")
        fits = D.matches(entity.record, new_prim, eps, hatch_eps=hatch_eps)
        if in_place:
            (plan.keep if fits else plan.edit).append(row)
        elif fits and D.same_prim(old_prim, new_prim, eps) and not role_moved:
            plan.keep.append(row)
        else:
            plan.recreate.append(row)
    for key, (old_index, _old_prim) in old.items():
        if key in new:
            continue
        entity = report.matched.get((key[0], old_index, False))
        if entity is not None and entity.handle not in claimed:
            claimed.add(entity.handle)
            plan.delete.append({"handle": entity.handle, "key": key, "reason": "removed"})
    for entity in report.tagged:
        if entity.handle not in claimed:
            # only reached with overwrite=True: a copy, an untracked or an
            # unexpected tag the model does not draw
            claimed.add(entity.handle)
            plan.delete.append({"handle": entity.handle, "key": None, "reason": "not_in_model"})

    dimensioned = {
        D.record_view_tag(r): n
        for n, r in enumerate(payload.get("views") or [])
        if r.get("dimensioned")
    }
    for rec, tag in D.annotations_of(records, report.part_id, "dim"):
        if str(tag.get("view", "")) in dimensioned:
            plan.dimensions.append(str(rec.handle))
    if not stamped and old_part is not None and dimensioned:
        # drawn before 1.8.0 (reached only with overwrite=True): the dimensions
        # and the hole table carry no tag, so they are found by the old model's
        # own geometry - the points each dimension measures, the box the table
        # lays out in - and replaced like tagged ones; anything else is named
        plan.legacy_dims, plan.legacy_left = legacy_dimensions(
            old_part, payload, records, dimensioned, eps
        )
        plan.legacy_table = legacy_table_pieces(old_part, payload, records, dimensioned, eps)
        plan.dimensions.extend(h for h in plan.legacy_dims if h not in plan.dimensions)
    # every dimensioned view is dimensioned again: its old dimensions are gone
    plan.redimension = sorted(dimensioned.values())
    alive = {str(rec.handle) for rec in records}
    table = [
        str(rec.handle) for rec, _tag in D.annotations_of(records, report.part_id, "hole_table")
    ]
    stored = payload.get("hole_table")
    if stored and str(stored) in alive and str(stored) not in table:
        table.append(str(stored))
    table.extend(h for h in plan.legacy_table if h not in table)
    plan.hole_table = table

    replaced = {row["handle"] for row in plan.recreate} | {row["handle"] for row in plan.delete}
    for rec in records:
        payload_of = D._payload_of(rec)
        if payload_of is None or payload_of.get("kind") != "balloon":
            continue
        targets = [str(t) for t in payload_of.get("targets") or []]
        if replaced & set(targets):
            plan.balloons.append((str(rec.handle), payload_of))
    return plan


# -- refusals -----------------------------------------------------------------------------


def refuse_drift(report) -> None:
    """Raise ``model_drift`` or ``hand_edited_tagged_entity`` when the drawing is
    not the stored model's drawing. Nothing has been written."""
    pid = report.part_id
    if not report.stamped:
        raise ModelDriftError(
            f"model_drift: part {pid} carries no gen stamp (drawn before 1.8.0), so its "
            "entities cannot be checked against its model; nothing was changed. Pass "
            "overwrite=True to redraw it from the model (any hand edit is replaced).",
            part_id=pid,
            reason="unstamped",
            views=list(report.unstamped_views),
        )
    if report.engine_drift:
        raise ModelDriftError(
            f"model_drift: the view engine no longer draws part {pid}'s model the way view(s) "
            f"{', '.join(report.engine_drift)} were drawn, so a hand edit cannot be told from "
            "an engine change; nothing was changed. Pass overwrite=True to redraw it.",
            part_id=pid,
            reason="engine_drift",
            views=list(report.engine_drift),
        )
    drifted = report.drifted
    if drifted:
        handles = report.hand_edited_handles
        statuses = sorted({row["status"] for row in drifted})
        shown = ", ".join(handles[:10]) + (" ..." if len(handles) > 10 else "")
        raise HandEditedTaggedEntityError(
            f"hand_edited_tagged_entity: {len(drifted)} tagged entit"
            f"{'y' if len(drifted) == 1 else 'ies'} of part {pid} no longer match the model "
            f"({', '.join(statuses)}){': ' + shown if shown else ''}; nothing was changed. "
            "Undo the edit, or pass overwrite=True to replace it with the model.",
            part_id=pid,
            handles=handles,
            counts=report.counts(),
            drift=drifted[:REFUSAL_ROWS],
        )


# -- the apply ----------------------------------------------------------------------------


async def _create_rows(backend, part_id: str, rows: list[dict]) -> dict[tuple, str]:
    """Draw each row's primitive with its tag and its index in the whole view."""
    from engineering.mech.draw import draw_prims

    out: dict[tuple, str] = {}
    by_view: dict[str, list[dict]] = {}
    for row in rows:
        by_view.setdefault(row["view"], []).append(row)
    for view, group in by_view.items():
        group = sorted(group, key=lambda r: r["index"])
        drawn = await draw_prims(
            backend,
            [r["prim"] for r in group],
            feature_tag={"part": part_id, "view": view},
            indices=[r["index"] for r in group],
        )
        for r, handle in zip(group, drawn["handles"], strict=True):
            out[r["key"]] = str(handle)
    return out


async def _edit_in_place(backend, prim, handle: str) -> None:
    from engineering.mech.primitives import Arc, Circle, Line

    if isinstance(prim, Line):
        await backend.entity_edit_geometry(
            handle, x1=prim.p1[0], y1=prim.p1[1], x2=prim.p2[0], y2=prim.p2[1]
        )
    elif isinstance(prim, Arc):
        await backend.entity_edit_geometry(
            handle,
            cx=prim.center[0],
            cy=prim.center[1],
            radius=prim.radius,
            start_angle=prim.start_deg,
            end_angle=prim.end_deg,
        )
    elif isinstance(prim, Circle):
        await backend.entity_edit_geometry(
            handle, cx=prim.center[0], cy=prim.center[1], radius=prim.radius
        )
    else:  # pragma: no cover - the plan sends only these three
        raise ValueError(f"edit in place: {type(prim).__name__} is not edited in place")


async def _retag(backend, part_id: str, row: dict) -> None:
    from engineering.mech.draw import _feature_xdata
    from engineering.mech.xdata import APP_ID

    tag = _feature_xdata(row["prim"], {"part": part_id, "view": row["view"]}, row["index"])
    await backend.entity_set_xdata(row["handle"], APP_ID, tag[APP_ID])


def _fits_kept(part, payload: dict, record: dict, settings: dict) -> tuple[dict, list[str]]:
    from engineering.mech.draw import _fits_for_view, _view_from_record

    fits = dict(settings.get("fits") or {})
    if not fits:
        return {}, []
    view = _view_from_record(part, payload, record)
    kept = _fits_for_view(
        part,
        view,
        fits,
        style=str(settings.get("style", "chain")),
        threshold=int(settings.get("hole_table_threshold", 8)),
    )
    return kept, sorted(set(fits) - set(kept))


async def revise_part(
    backend,
    part_id: str,
    changes: Any,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    expected_revision: int | None = None,
    preconditions: list | None = None,
    verify: bool = True,
) -> dict:
    """Change the part's model by path edits and rebuild its drawing in place.

    See the module docstring for the steps; refusals: ``invalid_args`` (a path,
    a value the part refuses), ``model_drift``, ``hand_edited_tagged_entity``,
    ``stale_revision`` (``expected_revision`` / ``preconditions``), and
    ``refused`` (``RevisionRefusedError``) for the layouts this release does
    not revise (a detail view, a repeated view tag), a payload with no view and
    a stored model that does not build.
    """
    from backends.operations import CURRENT_OPERATION
    from engineering.changes.preview import (
        check_preconditions,
        current_revision,
        make_preview,
        parse_preconditions,
    )
    from engineering.mech.draw import (
        GEN_STAMP,
        _ensure_layers,
        dimension_part,
        guarded_transaction,
        read_part,
    )
    from engineering.mech.history import append_history
    from engineering.mech.part import build_part, part_to_dict
    from engineering.mech.xdata import APP_ID, to_values
    from engineering.understand.snapshot import take_snapshot

    rows = parse_preconditions(preconditions)
    if not isinstance(part_id, str) or not part_id.strip():
        raise InvalidArgsError(
            f"invalid_args: part_id: expected the part's anchor handle, got {part_id!r}"
        )
    try:
        payload = await read_part(backend, part_id.strip())
    except ValueError as exc:
        # a handle that carries no part payload: the argument names no part
        raise InvalidArgsError(f"invalid_args: part_id: {exc}") from exc
    pid = str(payload["part_id"])
    stored_views = list(payload.get("views") or [])
    if not stored_views:
        raise RevisionRefusedError(
            f"{pid}: the payload carries no view to revise; nothing was changed.",
            part_id=pid,
            reason="no_views",
        )
    _refuse_layouts_not_revised(pid, stored_views)
    try:
        old_part = build_part(payload.get("part") or {})
    except ValueError as exc:
        raise RevisionRefusedError(
            f"{pid}: the stored part model does not build ({exc}); nothing was changed.",
            part_id=pid,
            reason="model_unbuildable",
        ) from exc
    old_dict = part_to_dict(old_part)
    new_dict, diff = apply_changes(old_dict, changes)
    try:
        new_part = build_part(new_dict)
    except ValueError as exc:
        paths = ", ".join(str(p).strip() for p in changes)
        raise InvalidArgsError(
            f"invalid_args: changes [{paths}]: the revised part is refused - {exc} "
            "Nothing was changed."
        ) from exc
    layout = relayout(new_part, payload)

    if not dry_run and (expected_revision is not None or rows):
        await check_preconditions(backend, expected_revision=expected_revision, preconditions=rows)
    start = await current_revision(backend)
    snap = await take_snapshot(backend)
    report = D.compare(pid, old_part, payload, snap.records)
    overwritten: list[dict] = []
    if overwrite:
        overwritten = report.drifted[:REFUSAL_ROWS]
    else:
        refuse_drift(report)

    changed = part_to_dict(new_part) != old_dict
    plan = make_plan(report, layout, payload, snap.records, report.stamped, old_part)
    counts = plan.counts() if changed or overwritten else dict.fromkeys(plan.counts(), 0)

    if dry_run:
        preview = await make_preview(
            backend,
            handles=[pid, *plan.touched()] if (changed or overwritten) else [pid],
            planned=[{"part_id": pid, "changes": diff, "counts": counts}],
        )
        return {
            "dry_run": True,
            "part_id": pid,
            "changed": changed,
            "changes": diff,
            "counts": counts,
            "model_is_current": report.model_is_current,
            "overwritten": overwritten,
            "omitted": layout.omitted,
            "preview": preview.to_dict(),
            "backend": backend.name,
        }

    if not changed and not overwritten:
        return {
            "part_id": pid,
            "changed": False,
            "changes": diff,
            "counts": counts,
            "handle_map": [],
            "model_is_current": report.model_is_current,
            "revision": {"checked": start, "after": start},
            "verify": None,
            "backend": backend.name,
        }

    redim = set(plan.redimension)
    operation_id = CURRENT_OPERATION.get()
    outcome: dict = {}

    async def job() -> None:
        # 1. the dimensions and the hole table go first: they are redrawn last
        for handle in plan.dimensions + plan.hole_table:
            await backend.entity_delete(handle)
        handle_map: list[dict] = []
        for row in plan.delete:
            await backend.entity_delete(row["handle"])
            handle_map.append({"old": row["handle"], "new": None, "reason": row["reason"]})
        for row in plan.recreate:
            await backend.entity_delete(row["handle"])
        # 2. in place: geometry, then the layer a changed role needs, then the tag
        layers = {row["relayer"] for row in plan.keep + plan.edit if row.get("relayer")}
        if layers:
            await _ensure_layers(backend, sorted(layers))
        for row in plan.edit:
            await _edit_in_place(backend, row["prim"], row["handle"])
        for row in plan.keep + plan.edit:
            if row.get("relayer"):
                await backend.entity_set_properties(row["handle"], layer=row["relayer"])
            if row.get("retag"):
                await _retag(backend, pid, row)
        # 3. what is new, and what had to be drawn again
        created = await _create_rows(backend, pid, plan.create + plan.recreate)
        for row in plan.recreate:
            moved = {
                "old": row["handle"],
                "new": created[row["key"]],
                "reason": f"recreated:{D.prim_type(row['prim'])}",
            }
            if row.get("kept"):
                # the hand-set layer / colour / linetype / lineweight survives
                # the redraw, as it does on an entity edited in place
                await backend.entity_set_properties(created[row["key"]], **row["kept"])
                moved["kept"] = dict(row["kept"])
            handle_map.append(moved)
        for row in plan.create:
            handle_map.append({"old": None, "new": created[row["key"]], "reason": "added"})
        # 4. the payload: the new model, the gen stamp, each view's digest
        records = [dict(r) for r in layout.records]
        for number in redim:
            records[number]["dimensioned"] = False
        new_payload = {k: v for k, v in payload.items() if k != "part_id"}
        new_payload.update(part=part_to_dict(new_part), views=records, gen=GEN_STAMP)
        if plan.hole_table:
            new_payload.pop("hole_table", None)
        await backend.entity_set_xdata(pid, APP_ID, to_values(new_payload))
        # 5. dimensions, view by view, the way each view was dimensioned
        dims_created: list[str] = []
        dropped: dict[str, list[str]] = {}
        table = None
        for number in sorted(redim):
            settings = layout.records[number].get("dim") or {}
            fits, lost = _fits_kept(new_part, new_payload, records[number], settings)
            if lost:
                dropped[str(number)] = lost
            done = await dimension_part(
                backend,
                pid,
                style=str(settings.get("style", "chain")),
                hole_table_threshold=int(settings.get("hole_table_threshold", 8)),
                fits=fits or None,
                views=[number],
            )
            dims_created.extend(h for h in done["handles"] if h != done.get("hole_table"))
            table = table or done.get("hole_table")
        # 6. balloons whose target was replaced
        old_to_new = {row["old"]: row["new"] for row in handle_map if row["old"]}
        relinked = []
        for handle, balloon in plan.balloons:
            targets = [str(t) for t in balloon.get("targets") or []]
            mapped = [old_to_new.get(t, t) for t in targets]
            kept = [t for t in mapped if t]
            if kept != targets:
                await backend.entity_set_xdata(
                    handle, APP_ID, to_values({**balloon, "targets": kept})
                )
                relinked.append({"handle": handle, "targets": kept, "was": targets})
        # 7. the model history
        entry = await append_history(backend, pid, diff, operation_id)
        outcome.update(
            handle_map=handle_map,
            dims_created=dims_created,
            dropped_fits=dropped,
            hole_table=table,
            relinked=relinked,
            entry=entry,
        )

    await guarded_transaction(backend, job, what="mech_part_revise", done="changed")

    # measured, not claimed: the rebuilt drawing read back against the new model
    current = (await D.inspect_drift(backend, pid)).model_is_current
    summary = None
    if verify:
        from engineering.verification.report import summarize, verify_requirements
        from engineering.verification.requirements import load_requirements

        rs = await load_requirements(backend)
        if rs is not None and rs.requirements:
            summary = summarize(await verify_requirements(backend, rs))
    return {
        "part_id": pid,
        "changed": True,
        "rev": outcome["entry"]["rev"],
        "changes": diff,
        "counts": counts,
        "in_place": [row["handle"] for row in plan.keep + plan.edit],
        "handle_map": outcome["handle_map"],
        "dimensions": {
            "deleted": len(plan.dimensions),
            "created": outcome["dims_created"],
            "views": sorted(redim),
            "legacy": {
                "dimensions": len(plan.legacy_dims),
                "hole_table_pieces": len(plan.legacy_table),
                "left": list(plan.legacy_left),
            },
            "dropped_fits": outcome["dropped_fits"],
            "note": "dimension handles change: each is deleted and placed again",
        },
        "hole_table": outcome["hole_table"],
        "balloons_relinked": outcome["relinked"],
        "overwritten": overwritten,
        "omitted": layout.omitted,
        "model_is_current": current,
        "verify": summary,
        "revision": {"checked": start, "after": await current_revision(backend)},
        "backend": backend.name,
    }


# -- the read-only face ---------------------------------------------------------------------


async def inspect(backend, part_id: str | None, *, drift: bool, history: bool, param: str | None):
    """``mech_part_inspect``'s payload plus ``drift`` / ``history`` on request."""
    from engineering.mech.draw import read_part
    from engineering.mech.history import load_history, param_values

    payload = await read_part(backend, part_id)
    if not drift and not history and param is None:
        return payload
    out = dict(payload)
    pid = str(payload["part_id"])
    if drift:
        report = await D.inspect_drift(backend, pid)
        out["drift"] = report.drifted
        out["drift_counts"] = report.counts()
        out["model_is_current"] = report.model_is_current
        out["stamped"] = report.stamped
        out["engine_drift"] = list(report.engine_drift)
    if history or param is not None:
        stored = await load_history(backend, pid)
        if history:
            out["history"] = stored["entries"]
        out["history_dropped"] = stored["dropped"]
        if param is not None:
            out["param"] = {"path": str(param), "values": param_values(stored, param)}
    return out
