"""Set-based edits: ``entity_modify_where``, ``layer_rename``, ``layer_merge`` (1.8-12).

The rival's layer clean-up took 11 calls; ours took 124, because the verbs did
not exist and ``entity_batch_modify`` wants one row per entity. This module
holds the engine-neutral half of the three verbs - the argument grammar and
the headless matching and reassignment - so the two backends share one
reading of ``where`` and ``set`` and neither ``ezdxf_backend.py`` nor
``com_backend.py`` grows a second copy.

**where** - every key optional, at least one required (an empty filter would
select every entity in the space; say ``{"type": "*"}`` to mean that):

- ``layer``: a name or a list of names; ``*`` and ``?`` are the only
  wildcards, case-insensitive (layer names are);
- ``type``: a DXF type or a list (``LINE``, ``INSERT``, ``DIMENSION``),
  same wildcards;
- ``color``: an ACI number 0-256 (256 = ByLayer, 0 = ByBlock);
- ``linetype``: a name or a list, same wildcards (``ByLayer`` matches an
  entity that follows its layer);
- ``lineweight``: anything ``parse_lineweight`` takes (``"ByLayer"``, 25,
  ``0.25``, ``-3``);
- ``region``: ``[x0, y0, x1, y1]`` in WCS, x0 <= x1 and y0 <= y1; an entity
  matches when its box meets the region (closed intervals, the rule the
  preview's region fingerprint uses); an entity with no finite box never
  matches;
- ``block``: an INSERT's block name or a list, same wildcards (an anonymous
  dynamic-block reference is matched by its ``*U`` name, on both engines).

**set** - at least one of ``layer`` (created when missing and reported),
``color`` (0-256; a true colour is dropped with it), ``linetype`` (loaded
when missing), ``lineweight`` (``parse_lineweight``), ``visible`` (bool).

**space** - ``model``, ``paper`` (every paper layout) or ``all``. Block
definitions are never searched by ``entity_modify_where``; ``layer_merge``
and ``layer_rename`` reach into them because a layer's name lives there too.

An entity on a locked layer is not written - AutoCAD's own rule, which the
live engine cannot break and the headless one keeps for parity - and is
reported in ``skipped_locked``, never counted as matched.

Refusals are by name and before anything is written: a malformed ``where``
or ``set`` is ``invalid_args``; ``layer_rename`` / ``layer_merge`` refusing
a layer (``0``, ``DEFPOINTS``, an xref-dependent one, a missing source, an
existing rename target) are ``refused``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

from fastmcp.exceptions import ToolError

from backends.base import parse_lineweight
from engineering.errors import InvalidArgsError

__all__ = [
    "HANDLE_SAMPLE",
    "PROTECTED_LAYERS",
    "SPACES",
    "SetSpec",
    "Where",
    "changes_payload",
    "check_layer_name",
    "glob_matches",
    "parse_set",
    "parse_space",
    "parse_where",
    "region_meets",
    "wcmatch_any",
]

#: How many handles a result lists before it says ``truncated``.
HANDLE_SAMPLE = 20
#: The spaces ``entity_modify_where`` searches.
SPACES = ("model", "paper", "all")
#: Layers no rename or merge may take from: AutoCAD refuses both.
PROTECTED_LAYERS = frozenset({"0", "defpoints"})

WHERE_KEYS = ("layer", "type", "color", "linetype", "lineweight", "region", "block")
SET_KEYS = ("layer", "color", "linetype", "lineweight", "visible")


def _invalid(path: str, message: str) -> InvalidArgsError:
    return InvalidArgsError(f"invalid_args: {path}: {message}")


# ── globs ──────────────────────────────────────────────────────────────────


def _names(value, path: str) -> tuple[str, ...]:
    """A name or a non-empty list of names, each a non-empty string."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not items:
        raise _invalid(path, f"expected a name or a non-empty list of names, got {value!r}")
    out = []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise _invalid(f"{path}[{index}]", f"expected a non-empty name, got {item!r}")
        out.append(item.strip())
    return tuple(dict.fromkeys(out))


def _glob_regex(patterns: Iterable[str]) -> re.Pattern:
    parts = []
    for pattern in patterns:
        body = "".join(
            ".*" if char == "*" else "." if char == "?" else re.escape(char) for char in pattern
        )
        parts.append(f"(?:{body})")
    return re.compile(r"\A(?:" + "|".join(parts) + r")\Z", re.IGNORECASE | re.DOTALL)


def glob_matches(patterns: tuple[str, ...], value: str) -> bool:
    """True when ``value`` matches one of ``patterns`` (``*`` / ``?``, any case)."""
    return _glob_regex(patterns).match(str(value or "")) is not None


#: Characters AutoCAD's ``wcmatch`` grammar treats as special; everything but
#: ``*`` and ``?`` is escaped so a filter means what the headless glob means.
_WCMATCH_ESCAPE = frozenset("#@.~[]-,`")


def wcmatch_any(patterns: tuple[str, ...]) -> str:
    """``patterns`` as one ``ssget`` filter string: comma-joined alternatives,
    ``*`` and ``?`` kept as wildcards, every other special escaped with a
    backtick - so ``A-WALL`` does not read as a range and ``A.B`` matches only
    itself (the escape ``com_backend._wcmatch_literal`` measured)."""
    return ",".join(
        "".join("`" + char if char in _WCMATCH_ESCAPE else char for char in pattern)
        for pattern in patterns
    )


# ── where / set / space ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Where:
    """A parsed ``where``. ``None`` means "not filtered on"."""

    layers: tuple[str, ...] | None = None
    types: tuple[str, ...] | None = None
    color: int | None = None
    linetypes: tuple[str, ...] | None = None
    lineweight: int | None = None
    region: tuple[float, float, float, float] | None = None
    blocks: tuple[str, ...] | None = None

    def to_dict(self) -> dict:
        out: dict = {}
        if self.layers is not None:
            out["layer"] = list(self.layers)
        if self.types is not None:
            out["type"] = list(self.types)
        if self.color is not None:
            out["color"] = self.color
        if self.linetypes is not None:
            out["linetype"] = list(self.linetypes)
        if self.lineweight is not None:
            out["lineweight"] = self.lineweight
        if self.region is not None:
            out["region"] = list(self.region)
        if self.blocks is not None:
            out["block"] = list(self.blocks)
        return out


def _color(value, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 256:
        raise _invalid(path, f"expected an ACI number 0-256 (256 = ByLayer), got {value!r}")
    return int(value)


def _lineweight(value, path: str) -> int:
    try:
        code = parse_lineweight(value)
    except ValueError as exc:
        raise _invalid(path, str(exc)) from None
    if code is None:
        raise _invalid(path, "expected a lineweight, got None")
    return code


def _region(value, path: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise _invalid(path, f"expected [x0, y0, x1, y1], got {value!r}")
    numbers = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise _invalid(f"{path}[{index}]", f"expected a number, got {item!r}")
        if not math.isfinite(float(item)):
            raise _invalid(f"{path}[{index}]", f"must be finite, got {item!r}")
        numbers.append(float(item))
    x0, y0, x1, y1 = numbers
    if x0 > x1 or y0 > y1:
        raise _invalid(path, f"x0 <= x1 and y0 <= y1 are required, got {value!r}")
    return x0, y0, x1, y1


def _mapping(raw, path: str, keys: tuple[str, ...]) -> dict:
    if not isinstance(raw, dict):
        raise _invalid(path, f"expected an object, got {type(raw).__name__}")
    unknown = sorted(str(k) for k in raw if k not in keys)
    if unknown:
        raise _invalid(path, f"unknown key(s) {', '.join(unknown)}; known: {', '.join(keys)}")
    given = {k: v for k, v in raw.items() if v is not None}
    if not given:
        raise _invalid(path, f"name at least one of {', '.join(keys)}")
    return given


def parse_where(raw) -> Where:
    """A validated :class:`Where`; refused by path (``invalid_args``)."""
    given = _mapping(raw, "where", WHERE_KEYS)
    return Where(
        layers=_names(given["layer"], "where.layer") if "layer" in given else None,
        types=tuple(t.upper() for t in _names(given["type"], "where.type"))
        if "type" in given
        else None,
        color=_color(given["color"], "where.color") if "color" in given else None,
        linetypes=_names(given["linetype"], "where.linetype") if "linetype" in given else None,
        lineweight=_lineweight(given["lineweight"], "where.lineweight")
        if "lineweight" in given
        else None,
        region=_region(given["region"], "where.region") if "region" in given else None,
        blocks=_names(given["block"], "where.block") if "block" in given else None,
    )


@dataclass(frozen=True)
class SetSpec:
    """A parsed ``set``. ``None`` means "left as it is"."""

    layer: str | None = None
    color: int | None = None
    linetype: str | None = None
    lineweight: int | None = None
    visible: bool | None = None

    def to_dict(self) -> dict:
        return {
            k: v
            for k, v in (
                ("layer", self.layer),
                ("color", self.color),
                ("linetype", self.linetype),
                ("lineweight", self.lineweight),
                ("visible", self.visible),
            )
            if v is not None
        }


def parse_set(raw) -> SetSpec:
    """A validated :class:`SetSpec`; refused by path (``invalid_args``)."""
    given = _mapping(raw, "set", SET_KEYS)
    layer = None
    if "layer" in given:
        if not isinstance(given["layer"], str) or not given["layer"].strip():
            raise _invalid("set.layer", f"expected a layer name, got {given['layer']!r}")
        layer = check_layer_name(given["layer"], "set.layer", invalid_args=True)
    linetype = None
    if "linetype" in given:
        if not isinstance(given["linetype"], str) or not given["linetype"].strip():
            raise _invalid("set.linetype", f"expected a linetype name, got {given['linetype']!r}")
        linetype = given["linetype"].strip()
    visible = None
    if "visible" in given:
        if not isinstance(given["visible"], bool):
            raise _invalid("set.visible", f"expected true or false, got {given['visible']!r}")
        visible = given["visible"]
    return SetSpec(
        layer=layer,
        color=_color(given["color"], "set.color") if "color" in given else None,
        linetype=linetype,
        lineweight=_lineweight(given["lineweight"], "set.lineweight")
        if "lineweight" in given
        else None,
        visible=visible,
    )


def resolve_layer(spec: SetSpec, existing: Iterable[str]) -> tuple[SetSpec, bool]:
    """``(spec, missing)``: ``spec.layer`` spelt as the layer table stores it
    (names ignore case, so writing ``A-WALL`` onto ``a-wall`` would read back
    as a change forever), and whether the layer still has to be created."""
    if spec.layer is None:
        return spec, False
    names = {str(name).lower(): str(name) for name in existing}
    stored = names.get(spec.layer.lower())
    if stored is None:
        return spec, True
    return replace(spec, layer=stored), False


def parse_space(raw) -> str:
    space = str(raw or "").strip().lower()
    if space not in SPACES:
        raise _invalid("space", f"expected one of {', '.join(SPACES)}, got {raw!r}")
    return space


def check_layer_name(name, path: str, *, invalid_args: bool = False) -> str:
    """``name`` if a layer may carry it; refused by name otherwise.

    The DXF symbol-table rule (``security.sanitize_symbol_name``) plus ``|``,
    the xref-dependent separator no local layer may carry.
    """
    from security import sanitize_symbol_name

    try:
        cleaned = sanitize_symbol_name(str(name), kind="layer")
        if "|" in cleaned:
            raise ToolError("layer name rejected: '|' marks an xref-dependent layer.")
    except ToolError as exc:
        if invalid_args:
            raise _invalid(path, str(exc)) from None
        raise ToolError(f"{path}: {exc}") from None
    return cleaned


def region_meets(box, region) -> bool:
    """True when ``box`` and ``region`` share a point (closed intervals)."""
    if box is None:
        return False
    x0, y0, x1, y1 = box
    rx0, ry0, rx1, ry1 = region
    return not (x1 < rx0 or x0 > rx1 or y1 < ry0 or y0 > ry1)


def changes_payload(*, modified: int = 0, **counts: int) -> dict:
    """The ``changes`` block every set edit carries (1.8-10's shape).

    These verbs add and delete no entity, so ``added`` / ``deleted`` are empty
    and the set is ``complete`` on both engines; ``counts`` carries what moved
    (``modified`` entities, plus layer-table rows such as ``layers_deleted``).
    """
    return {
        "added": [],
        "deleted": [],
        "counts": {"added": 0, "deleted": 0, "modified": int(modified), **counts},
        "complete": True,
    }


# ── the headless engine ────────────────────────────────────────────────────


def ezdxf_spaces(doc, space: str):
    """``(name, layout)`` for every layout ``space`` covers, model first."""
    if space in ("model", "all"):
        yield "Model", doc.modelspace()
    if space in ("paper", "all"):
        for name in doc.layouts.names_in_taborder():
            if name.lower() != "model":
                yield name, doc.layouts.get(name)


def ezdxf_locked_layers(doc) -> set[str]:
    """Lower-cased names of the locked layers."""
    return {str(layer.dxf.name).lower() for layer in doc.layers if layer.is_locked()}


def ezdxf_matches(entity, where: Where) -> bool:
    """True when ``entity`` passes every filter of ``where``."""
    dxftype = entity.dxftype().upper()
    if where.types is not None and not glob_matches(where.types, dxftype):
        return False
    if where.layers is not None and not glob_matches(where.layers, entity.dxf.get("layer", "0")):
        return False
    if where.blocks is not None:
        if dxftype != "INSERT" or not glob_matches(where.blocks, entity.dxf.get("name", "")):
            return False
    if where.color is not None and int(entity.dxf.get("color", 256)) != where.color:
        return False
    if where.linetypes is not None and not glob_matches(
        where.linetypes, entity.dxf.get("linetype", "ByLayer")
    ):
        return False
    if where.lineweight is not None and int(entity.dxf.get("lineweight", -1)) != where.lineweight:
        return False
    if where.region is not None:
        from engineering.understand.snapshot import _box

        if not region_meets(_box(entity), where.region):
            return False
    return True


def ezdxf_select(doc, where: Where, space: str) -> tuple[list, dict]:
    """``(entities, skipped_locked)``: what ``where`` matches in ``space``,
    sorted by handle, with the entities on locked layers set aside. A paper
    layout's own overall viewport (id 1) is the sheet, not drawn content, and
    AutoCAD does not let a selection pick it - so neither does this."""
    locked = ezdxf_locked_layers(doc)
    found = []
    skipped: dict[str, int] = {}
    for _name, layout in ezdxf_spaces(doc, space):
        for entity in layout:
            if entity.dxftype() == "VIEWPORT" and int(entity.dxf.get("id", 0)) == 1:
                continue
            if not ezdxf_matches(entity, where):
                continue
            layer = str(entity.dxf.get("layer", "0"))
            if layer.lower() in locked:
                skipped[layer] = skipped.get(layer, 0) + 1
                continue
            found.append(entity)
    found.sort(key=lambda e: int(str(e.dxf.handle), 16))
    return found, skipped


def skipped_payload(skipped: dict[str, int]) -> dict:
    return {"count": sum(skipped.values()), "layers": sorted(skipped)}


def ezdxf_differs(entity, spec: SetSpec) -> bool:
    """True when writing ``spec`` would change ``entity``."""
    dxf = entity.dxf
    if spec.layer is not None and str(dxf.get("layer", "0")) != spec.layer:
        return True
    if spec.color is not None and (
        int(dxf.get("color", 256)) != spec.color or dxf.hasattr("true_color")
    ):
        return True
    if spec.linetype is not None and str(dxf.get("linetype", "ByLayer")).lower() != (
        spec.linetype.lower()
    ):
        return True
    if spec.lineweight is not None and int(dxf.get("lineweight", -1)) != spec.lineweight:
        return True
    return spec.visible is not None and bool(dxf.get("invisible", 0)) == spec.visible


def ezdxf_write(entity, spec: SetSpec) -> None:
    """Write ``spec`` onto ``entity`` (a POLYLINE's vertices follow its layer)."""
    dxf = entity.dxf
    if spec.layer is not None:
        dxf.layer = spec.layer
        if entity.dxftype() == "POLYLINE":
            for vertex in entity.vertices:
                vertex.dxf.layer = spec.layer
            if entity.seqend is not None:
                entity.seqend.dxf.layer = spec.layer
    if spec.color is not None:
        dxf.color = spec.color
        dxf.discard("true_color")  # a true colour overrides the ACI it was given
        dxf.discard("color_name")
    if spec.linetype is not None:
        dxf.linetype = spec.linetype
    if spec.lineweight is not None:
        dxf.lineweight = spec.lineweight
    if spec.visible is not None:
        dxf.invisible = 0 if spec.visible else 1


def ezdxf_containers(doc) -> dict[str, tuple[str, str]]:
    """Block-record handle -> ``("layout" | "block", name)``."""
    out: dict[str, tuple[str, str]] = {}
    for layout in doc.layouts:
        out[str(layout.block_record_handle)] = ("layout", layout.name)
    for block in doc.blocks:
        record = block.block_record
        if block.is_any_layout:
            continue
        out[str(record.dxf.handle)] = ("block", block.name)
    return out


def ezdxf_container_of(doc, entity, containers) -> tuple[str, str] | None:
    """The layout or block definition ``entity`` lives in, through its owners
    (an ATTRIB's INSERT, a VERTEX's POLYLINE); None when the chain breaks."""
    seen = 0
    current = entity
    while current is not None and seen < 16:
        owner = str(current.dxf.get("owner", "") or "")
        if owner in containers:
            return containers[owner]
        current = doc.entitydb.get(owner) if owner else None
        seen += 1
    return None


def ezdxf_layer_references(doc, keys: set[str]):
    """Every live entity whose ``layer`` is one of ``keys`` (lower-cased),
    from the entity database: layouts, block definitions, ATTRIBs, VERTEXes."""
    for entity in list(doc.entitydb.values()):
        if not entity.is_alive or entity.dxftype() == "LAYER":
            continue
        if entity.dxf.hasattr("layer") and str(entity.dxf.layer).lower() in keys:
            yield entity


def tally(doc, entities, containers) -> tuple[dict[str, int], list[str]]:
    """``(by_space, blocks_touched)`` for the entities about to be reassigned."""
    by_space: dict[str, int] = {}
    blocks: set[str] = set()
    for entity in entities:
        where = ezdxf_container_of(doc, entity, containers)
        if where is None:
            by_space["(unowned)"] = by_space.get("(unowned)", 0) + 1
        elif where[0] == "layout":
            by_space[where[1]] = by_space.get(where[1], 0) + 1
        else:
            blocks.add(where[1])
            by_space["(block definitions)"] = by_space.get("(block definitions)", 0) + 1
    return by_space, sorted(blocks)


BLOCK_NOTE = (
    "A block definition was edited: every INSERT of {names} shows the change, in every "
    "layout and in every block that nests it."
)


def block_note(blocks: list[str]) -> str | None:
    if not blocks:
        return None
    shown = ", ".join(blocks[:10]) + (f" (+{len(blocks) - 10} more)" if len(blocks) > 10 else "")
    return BLOCK_NOTE.format(names=shown)


# ── the live engine (duck-typed ActiveX objects; com_backend supplies the
# selection-set call, which builds the VARIANT filter arrays) ──────────────


def com_filter(query: Where, scope: str) -> tuple[list[int], list]:
    """The ``ssget "X"`` filter for ``query`` in ``scope``: ``(codes, values)``.

    Group 0 (type), 8 (layer), 2 (block name, with 0 INSERT when no type was
    named), 62 (colour, only for 1-255: a ByLayer or ByBlock entity carries
    no group 62 to filter on, so 0 / 256 are checked per entity) and 410
    (the layout: ``Model`` or ``~Model``). Linetype, lineweight and region are
    checked per entity, on the members that answer them.
    """
    codes: list[int] = []
    values: list = []
    if query.types is not None:
        codes.append(0)
        values.append(wcmatch_any(query.types))
    elif query.blocks is not None:
        codes.append(0)
        values.append("INSERT")
    if query.layers is not None:
        codes.append(8)
        values.append(wcmatch_any(query.layers))
    if query.blocks is not None:
        codes.append(2)
        values.append(wcmatch_any(query.blocks))
    if query.color is not None and 1 <= query.color <= 255:
        codes.append(62)
        values.append(int(query.color))
    if scope == "model":
        codes.append(410)
        values.append("Model")
    elif scope == "paper":
        codes.append(410)
        values.append("~Model")
    return codes, values


def com_box(entity):
    """``GetBoundingBox`` as ``(x0, y0, x1, y1)``; None for an unbounded entity."""
    try:
        low, high = entity.GetBoundingBox()
        return (float(low[0]), float(low[1]), float(high[0]), float(high[1]))
    except Exception:  # an XLINE / RAY has no box: it never meets a region
        return None


def com_locked_layers(doc) -> set[str]:
    out = set()
    for index in range(doc.Layers.Count):
        layer = doc.Layers.Item(index)
        if bool(layer.Lock):
            out.add(str(layer.Name).lower())
    return out


def com_layer_names(doc) -> list[str]:
    return [str(doc.Layers.Item(i).Name) for i in range(doc.Layers.Count)]


def com_select(doc, query: Where, scope: str, select_all) -> tuple[list, dict]:
    """``(entities, skipped_locked)`` on the live engine, sorted by handle.

    ``select_all(doc, codes, values)`` returns the filtered selection's
    members (com_backend's ``SelectionSet.Select(acSelectionSetAll, ...)``,
    deleted again before it returns)."""
    codes, values = com_filter(query, scope)
    locked = com_locked_layers(doc)
    found = []
    skipped: dict[str, int] = {}
    for entity in select_all(doc, codes, values):
        if query.color in (0, 256) and int(entity.color) != query.color:
            continue
        if query.linetypes is not None and not glob_matches(query.linetypes, entity.Linetype):
            continue
        if query.lineweight is not None and int(entity.Lineweight) != query.lineweight:
            continue
        if query.region is not None and not region_meets(com_box(entity), query.region):
            continue
        layer = str(entity.Layer)
        if layer.lower() in locked:
            skipped[layer] = skipped.get(layer, 0) + 1
            continue
        found.append(entity)
    found.sort(key=lambda e: int(str(e.Handle), 16))
    return found, skipped


def com_differs(entity, spec: SetSpec) -> bool:
    if spec.layer is not None and str(entity.Layer) != spec.layer:
        return True
    if spec.color is not None and int(entity.color) != spec.color:
        return True
    if spec.linetype is not None and str(entity.Linetype).lower() != spec.linetype.lower():
        return True
    if spec.lineweight is not None and int(entity.Lineweight) != spec.lineweight:
        return True
    return spec.visible is not None and bool(entity.Visible) != spec.visible


def com_write(entity, spec: SetSpec) -> None:
    if spec.layer is not None:
        entity.Layer = spec.layer
    if spec.color is not None:
        entity.color = spec.color
    if spec.linetype is not None:
        entity.Linetype = spec.linetype
    if spec.lineweight is not None:
        entity.Lineweight = spec.lineweight
    if spec.visible is not None:
        entity.Visible = spec.visible


def _com_attributes(entity) -> list:
    try:
        if str(entity.ObjectName) != "AcDbBlockReference" or not entity.HasAttributes:
            return []
        return list(entity.GetAttributes())
    except Exception:  # noqa: BLE001 - a reference without readable attributes has none to move
        return []


def com_reassign(doc, keys: set[str], goal: str, select_all) -> tuple[int, dict, list[str]]:
    """Move every entity on a layer in ``keys`` onto ``goal``, live.

    Layouts through one filtered selection (group 8), their INSERTs'
    attributes through a second (0 INSERT, 66 1), then a walk of every block
    definition that is neither a layout nor an xref (anonymous ones included:
    an entity left inside ``*D`` on a deleted layer would keep it alive).
    ``(reassigned, by_space, blocks_touched)``."""
    spaces = {}
    for index in range(doc.Layouts.Count):
        layout = doc.Layouts.Item(index)
        spaces[layout.Block.ObjectID] = str(layout.Name)
    by_space: dict[str, int] = {}
    blocks: set[str] = set()
    count = 0

    def _space(entity) -> str:
        try:
            return spaces.get(entity.OwnerID, "(unowned)")
        except Exception:  # noqa: BLE001 - the tally is evidence, never a failure
            return "(unowned)"

    names = wcmatch_any(tuple(sorted(keys)))
    for entity in select_all(doc, [8], [names]):
        entity.Layer = goal
        where = _space(entity)
        by_space[where] = by_space.get(where, 0) + 1
        count += 1
    for insert in select_all(doc, [0, 66], ["INSERT", 1]):
        for attribute in _com_attributes(insert):
            if str(attribute.Layer).lower() in keys:
                attribute.Layer = goal
                where = _space(insert)
                by_space[where] = by_space.get(where, 0) + 1
                count += 1
    for index in range(doc.Blocks.Count):
        block = doc.Blocks.Item(index)
        if bool(block.IsLayout) or bool(block.IsXRef):
            continue
        for position in range(block.Count):
            entity = block.Item(position)
            members = [entity, *_com_attributes(entity)]
            for member in members:
                if str(member.Layer).lower() in keys:
                    member.Layer = goal
                    blocks.add(str(block.Name))
                    by_space["(block definitions)"] = by_space.get("(block definitions)", 0) + 1
                    count += 1
    return count, by_space, sorted(blocks)


def merge_target(target) -> str:
    if not isinstance(target, str) or not target.strip():
        raise _invalid("target", f"expected a layer name, got {target!r}")
    return target.strip()


def merge_sources(sources, target: str) -> tuple[str, ...]:
    """The validated, de-duplicated source names of a merge (refused by name)."""
    names = [sources] if isinstance(sources, str) else sources
    if not isinstance(names, (list, tuple)) or not names:
        raise _invalid("sources", f"expected a non-empty list of layer names, got {sources!r}")
    out: list[str] = []
    for index, name in enumerate(names):
        if not isinstance(name, str) or not name.strip():
            raise _invalid(f"sources[{index}]", f"expected a layer name, got {name!r}")
        out.append(name.strip())
    unique = tuple(dict.fromkeys(out))
    if len({n.lower() for n in unique}) != len(unique):
        raise _invalid("sources", "names a layer twice (layer names ignore case)")
    if target.lower() in {n.lower() for n in unique}:
        raise ToolError(
            f"layer_merge: refused - the target {target!r} is also a source; a layer "
            "cannot merge into itself."
        )
    for name in unique:
        if name.lower() in PROTECTED_LAYERS:
            raise ToolError(
                f"layer_merge: refused - layer {name!r} cannot be merged away (AutoCAD keeps "
                "0 and DEFPOINTS in every drawing)."
            )
        if "|" in name:
            raise ToolError(
                f"layer_merge: refused - {name!r} is an xref-dependent layer; it belongs to "
                "the referenced file."
            )
    return unique


def refuse_missing(tool: str, missing: list[str], existing: Iterable[str]) -> None:
    if not missing:
        return
    known = sorted(existing, key=str.lower)
    shown = ", ".join(known[:20]) + (" ..." if len(known) > 20 else "")
    raise ToolError(
        f"{tool}: refused - no layer named {', '.join(repr(m) for m in missing)} in the "
        f"drawing; nothing was changed. Layers: {shown}"
    )


def rename_checks(old, new, existing: Iterable[str]) -> tuple[str, str]:
    """``(old as stored, new)``, or a refusal by name. ``existing`` is the
    layer table's names."""
    if not isinstance(old, str) or not old.strip():
        raise _invalid("old", f"expected a layer name, got {old!r}")
    if not isinstance(new, str) or not new.strip():
        raise _invalid("new", f"expected a layer name, got {new!r}")
    old = old.strip()
    names = {name.lower(): name for name in existing}
    if old.lower() in PROTECTED_LAYERS:
        raise ToolError(
            f"layer_rename: refused - layer {old!r} cannot be renamed (AutoCAD keeps 0 and "
            "DEFPOINTS under those names)."
        )
    if "|" in old:
        raise ToolError(
            f"layer_rename: refused - {old!r} is an xref-dependent layer; rename it in the "
            "referenced file."
        )
    if old.lower() not in names:
        refuse_missing("layer_rename", [old], names.values())
    new = check_layer_name(new, "layer_rename")
    if new.lower() in PROTECTED_LAYERS:
        raise ToolError(f"layer_rename: refused - {new!r} is a reserved layer name.")
    if new.lower() == old.lower():
        raise ToolError(
            f"layer_rename: refused - {old!r} and {new!r} differ only in case and layer "
            "names ignore case; nothing to rename."
        )
    if new.lower() in names:
        raise ToolError(
            f"layer_rename: refused - layer {names[new.lower()]!r} already exists; "
            "layer_merge moves one layer's entities onto another."
        )
    return names[old.lower()], new
