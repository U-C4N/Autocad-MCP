"""Typed requirements: parse, validate, conflicts, the stored record (v1.7.0, B15 residue).

A requirement set arrives as::

    {"units": "mm",
     "items": [{"id": "D1", "quantity": "diameter",
                "target": {"nominal": 40, "unit": "mm"},
                "features": [{"kind": "feature_tag", "part": "flange", "feature": "bore"}],
                "provenance": "explicit",
                "source": {"document": "flange.pdf", "page": 1, "label": "Ø40"}}],
     "dimensions": {...}}          # optional legacy object, kept read-only

Every refusal names its path through ``engineering.validation``
(``items[2].target.nominal: must be greater than zero, got -1.0.``).
Conflicting provenance is kept and reported (``requirement_conflicts``), never
collapsed to one value. A legacy ``dimensions`` object is accepted as given,
stored as given and never measured.

``store_requirements`` writes the set into the drawing - an XRECORD named
``CURRENT`` inside the ``ACADMCP_REQUIREMENTS`` dictionary, JSON in
255-character 1000-code chunks: the layer-states pattern
(``engineering/environment/layer_states.py``). ActiveX can add only a
dictionary to the named object dictionary (``IAcadDictionaries`` has ``Add``
and no ``AddXRecord`` - read off the AutoCAD 2026 type library), so the record
sits one level down on both engines. It is the server's own record; AutoCAD
shows it nowhere.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import config
from engineering.validation import (
    SpecError,
    finite,
    int_in_range,
    non_negative,
    positive,
    short_repr,
    within_budget,
)
from engineering.verification.types import (
    FEATURE_TYPES,
    PROVENANCES,
    QUANTITIES,
    SELECTOR_KINDS,
    FeatureSelector,
    Requirement,
    RequirementSet,
    freeze,
)

__all__ = [
    "ANGLE_UNITS",
    "CHUNK_SIZE",
    "DICT_NAME",
    "LENGTH_UNITS",
    "RECORD_NAME",
    "RequirementError",
    "decode_requirements",
    "encode_requirements",
    "load_requirements",
    "parse_requirements",
    "requirement_conflicts",
    "store_requirements",
]

DICT_NAME = "ACADMCP_REQUIREMENTS"
RECORD_NAME = "CURRENT"
FORMAT_VERSION = 1
#: AutoCAD's ``SetXRecordData`` string limit; the chunks are ASCII, so a
#: boundary never splits a code point.
CHUNK_SIZE = 255

LENGTH_UNITS: tuple[str, ...] = ("mm", "cm", "m", "in", "ft")
ANGLE_UNITS: tuple[str, ...] = ("deg", "rad")
UNIT_ALIASES = {
    "millimetre": "mm",
    "millimeter": "mm",
    "millimetres": "mm",
    "millimeters": "mm",
    "centimetre": "cm",
    "centimeter": "cm",
    "metre": "m",
    "meter": "m",
    "inch": "in",
    "inches": "in",
    "foot": "ft",
    "feet": "ft",
    "degree": "deg",
    "degrees": "deg",
    "radian": "rad",
    "radians": "rad",
}

ITEM_KEYS: tuple[str, ...] = (
    "id",
    "quantity",
    "target",
    "features",
    "provenance",
    "source",
    "datum",
    "manufacturing_tolerance",
    "verification_epsilon",
)
SOURCE_KEYS: tuple[str, ...] = ("document", "page", "view", "label", "region")
#: A geometric predicate's optional keys: the vocabulary Task 14's binder reads
#: (a size window is a radius window for circles / arcs, a length window for lines).
PREDICATE_KEYS: tuple[str, ...] = (
    "layer",
    "space",
    "region",
    "radius_min",
    "radius_max",
    "length_min",
    "length_max",
)
#: ``(required, optional)`` keys per selector kind, besides ``kind``.
SELECTOR_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "handle": (("handle",), ("block_path",)),
    "feature_tag": (("part", "feature"), ("view", "type", "fallback")),
    "arch_element": (("element",), ("type",)),
    "predicate": (("type",), PREDICATE_KEYS),
    "dimension": (("handle",), ()),
}
#: How many feature selectors each quantity takes: ``(min, max)``, None = no maximum.
FEATURE_ARITY: dict[str, tuple[int, int | None]] = {
    "distance": (1, 2),
    "radius": (1, 1),
    "diameter": (1, 1),
    "angle": (1, 2),
    "count": (1, None),
    "absence": (1, None),
    "concentric": (2, 2),
    "coincident": (2, 2),
    "parallel": (2, 2),
    "perpendicular": (2, 2),
    "tangent": (2, 2),
    "horizontal": (1, 1),
    "vertical": (1, 1),
}
MEASURED: tuple[str, ...] = ("distance", "radius", "diameter", "angle")
MAX_COUNT = 1_000_000
_FIT = re.compile(r"[A-Za-z]{1,2}\d{1,2}")
_HEX = set("0123456789abcdefABCDEF")


class RequirementError(SpecError):
    """A requirement refused before anything is stored; the message starts with its path."""


def _mapping(value: Any, path: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise RequirementError(f"{path}: expected an object, got {short_repr(value)}.")
    return value


def _only(mapping: Mapping, path: str, allowed: tuple[str, ...]) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise RequirementError(f"{path}: unknown key(s) {unknown}; allowed: {list(allowed)}.")


def _text(value: Any, path: str, *, max_chars: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequirementError(f"{path}: expected a non-empty string, got {short_repr(value)}.")
    text = value.strip()
    if len(text) > max_chars:
        raise RequirementError(f"{path}: at most {max_chars} characters, got {len(text)}.")
    return text


def _choice(value: Any, path: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise RequirementError(f"{path}: must be one of {list(choices)}, got {short_repr(value)}.")
    return value


def _unit(value: Any, path: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        raise RequirementError(f"{path}: expected a unit name, got {short_repr(value)}.")
    folded = value.strip().lower()
    unit = UNIT_ALIASES.get(folded, folded)
    if unit not in allowed:
        raise RequirementError(f"{path}: must be one of {list(allowed)}, got {short_repr(value)}.")
    return unit


def _region(value: Any, path: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise RequirementError(f"{path}: expected [x1, y1, x2, y2], got {short_repr(value)}.")
    x1, y1, x2, y2 = (finite(item, f"{path}[{index}]") for index, item in enumerate(value))
    if x1 == x2 or y1 == y2:
        raise RequirementError(
            f"{path}: a region needs a width and a height, got {short_repr(value)}."
        )
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _handle(value: Any, path: str) -> str:
    text = _text(value, path, max_chars=16)
    if not set(text) <= _HEX:
        raise RequirementError(f"{path}: a handle is hexadecimal, got {short_repr(value)}.")
    return text.upper()


def _parse_target(value: Any, quantity: str, units: str, path: str) -> dict:
    if quantity in MEASURED:
        target = _mapping(value, path)
        _only(target, path, ("nominal", "unit"))
        if "nominal" not in target:
            raise RequirementError(f"{path}.nominal: required for a {quantity}.")
        where = f"{path}.nominal"
        if quantity == "distance":
            nominal = non_negative(target["nominal"], where)
        elif quantity == "angle":
            nominal = finite(target["nominal"], where)
        else:
            nominal = positive(target["nominal"], where)
        if quantity == "angle":
            unit = _unit(target.get("unit", "deg"), f"{path}.unit", ANGLE_UNITS)
        else:
            unit = _unit(target.get("unit", units), f"{path}.unit", LENGTH_UNITS)
        return {"nominal": nominal, "unit": unit}
    if quantity == "count":
        target = _mapping(value, path)
        _only(target, path, ("exact", "min", "max"))
        out = {
            key: int_in_range(target[key], f"{path}.{key}", 0, MAX_COUNT)
            for key in ("exact", "min", "max")
            if key in target
        }
        if not out:
            raise RequirementError(f"{path}: a count needs 'exact', 'min' or 'max'.")
        if "exact" in out and len(out) > 1:
            raise RequirementError(f"{path}: 'exact' excludes 'min' and 'max'.")
        if "min" in out and "max" in out and out["min"] > out["max"]:
            raise RequirementError(f"{path}: min {out['min']} is greater than max {out['max']}.")
        return out
    # absence and the geometric relations carry no target
    if value is None:
        return {}
    target = _mapping(value, path)
    if target:
        raise RequirementError(
            f"{path}: a {quantity} requirement takes no target, got {short_repr(dict(target))}."
        )
    return {}


def _parse_selector(value: Any, path: str) -> FeatureSelector:
    selector = _mapping(value, path)
    kind = _choice(selector.get("kind"), f"{path}.kind", SELECTOR_KINDS)
    required, optional = SELECTOR_KEYS[kind]
    _only(selector, path, ("kind", *required, *optional))
    for key in required:
        if key not in selector:
            raise RequirementError(f"{path}.{key}: required for a {kind} selector.")
    out: dict[str, Any] = {}
    if kind in ("handle", "dimension"):
        out["handle"] = _handle(selector["handle"], f"{path}.handle")
        if "block_path" in selector:
            out["block_path"] = _block_path(selector["block_path"], f"{path}.block_path")
    elif kind == "feature_tag":
        for key in ("part", "feature", "view"):
            if key in selector:
                out[key] = _text(selector[key], f"{path}.{key}")
        if "type" in selector:
            out["type"] = _choice(selector["type"], f"{path}.type", FEATURE_TYPES)
        if "fallback" in selector:
            fallback = _mapping(selector["fallback"], f"{path}.fallback")
            _only(fallback, f"{path}.fallback", ("type", *PREDICATE_KEYS))
            if "type" not in fallback:
                raise RequirementError(f"{path}.fallback.type: required for a fallback predicate.")
            out["fallback"] = _parse_predicate(fallback, f"{path}.fallback")
    elif kind == "arch_element":
        out["element"] = _text(selector["element"], f"{path}.element")
        if "type" in selector:
            out["type"] = _choice(selector["type"], f"{path}.type", FEATURE_TYPES)
    else:  # predicate
        out = _parse_predicate(selector, path)
    return FeatureSelector(kind=kind, value=freeze(out))


#: One MINSERT cell in a ``block_path``: ``"<handle>[<row>,<column>]"``
#: (``engineering/verification/observe.py::cell_path_element``).
_CELL = re.compile(r"^([0-9A-Fa-f]{1,16})\[([0-9]+),([0-9]+)\]$")


def _block_path(value: Any, path: str) -> list[str]:
    """The INSERT handles above an exploded feature, outermost first; a MINSERT
    cell is ``"<handle>[<row>,<column>]"``."""
    if not isinstance(value, (list, tuple)) or not value:
        raise RequirementError(
            f"{path}: expected a non-empty list of handles, got {short_repr(value)}."
        )
    out = []
    for index, item in enumerate(value):
        where = f"{path}[{index}]"
        cell = _CELL.match(item) if isinstance(item, str) else None
        if cell is not None:
            out.append(f"{cell.group(1).upper()}[{int(cell.group(2))},{int(cell.group(3))}]")
        else:
            out.append(_handle(item, where))
    return out


def _parse_predicate(selector: Mapping, path: str) -> dict:
    """A geometric predicate: a feature type, a layer / space, a region and a size
    window (``radius_*`` for circles and arcs, ``length_*`` for lines)."""
    out: dict[str, Any] = {"type": _choice(selector["type"], f"{path}.type", FEATURE_TYPES)}
    for key in ("layer", "space"):
        if key in selector:
            out[key] = _text(selector[key], f"{path}.{key}")
    for low, high in (("radius_min", "radius_max"), ("length_min", "length_max")):
        for key in (low, high):
            if key in selector:
                out[key] = non_negative(selector[key], f"{path}.{key}")
        if low in out and high in out and out[low] > out[high]:
            raise RequirementError(f"{path}: {low} {out[low]} is greater than {high} {out[high]}.")
    if "region" in selector:
        out["region"] = _region(selector["region"], f"{path}.region")
    return out


def _parse_source(value: Any, path: str) -> dict:
    if value is None:
        return {}
    source = _mapping(value, path)
    _only(source, path, SOURCE_KEYS)
    out: dict[str, Any] = {}
    for key in ("document", "view", "label"):
        if key in source:
            out[key] = _text(source[key], f"{path}.{key}")
    if "page" in source:
        out["page"] = int_in_range(source["page"], f"{path}.page", 1, 100_000)
    if "region" in source:
        out["region"] = _region(source["region"], f"{path}.region")
    return out


def _parse_tolerance(value: Any, path: str) -> dict | None:
    if value is None:
        return None
    tolerance = _mapping(value, path)
    _only(tolerance, path, ("upper", "lower", "fit", "source"))
    if "source" not in tolerance:
        raise RequirementError(
            f"{path}.source: a manufacturing tolerance names its own source (a drawing note, "
            "a standard, a fit table); it is never derived."
        )
    out: dict[str, Any] = {"source": _text(tolerance["source"], f"{path}.source")}
    has_fit = "fit" in tolerance
    has_band = "upper" in tolerance or "lower" in tolerance
    if has_fit and has_band:
        raise RequirementError(f"{path}: give a fit or an upper/lower band, not both.")
    if has_fit:
        fit = _text(tolerance["fit"], f"{path}.fit", max_chars=8)
        if not _FIT.fullmatch(fit):
            raise RequirementError(
                f"{path}.fit: an ISO 286 fit such as H7 or g6, got {short_repr(tolerance['fit'])}."
            )
        out["fit"] = fit
    elif has_band:
        if "upper" not in tolerance or "lower" not in tolerance:
            raise RequirementError(f"{path}: a band needs both 'upper' and 'lower'.")
        upper = finite(tolerance["upper"], f"{path}.upper")
        lower = finite(tolerance["lower"], f"{path}.lower")
        if lower > upper:
            raise RequirementError(f"{path}: lower {lower} is greater than upper {upper}.")
        out["upper"] = upper
        out["lower"] = lower
    else:
        raise RequirementError(f"{path}: give a 'fit' or an 'upper'/'lower' band.")
    return out


def _parse_item(value: Any, index: int, units: str) -> Requirement:
    path = f"items[{index}]"
    item = _mapping(value, path)
    _only(item, path, ITEM_KEYS)
    for key in ("id", "quantity", "features", "provenance"):
        if key not in item:
            raise RequirementError(f"{path}.{key}: required.")
    requirement_id = _text(item["id"], f"{path}.id", max_chars=64)
    quantity = _choice(item["quantity"], f"{path}.quantity", QUANTITIES)
    provenance = _choice(item["provenance"], f"{path}.provenance", PROVENANCES)
    target = _parse_target(item.get("target"), quantity, units, f"{path}.target")
    raw_features = item["features"]
    if not isinstance(raw_features, (list, tuple)) or not raw_features:
        raise RequirementError(
            f"{path}.features: expected a non-empty list of selectors, got {short_repr(raw_features)}."
        )
    features = tuple(
        _parse_selector(selector, f"{path}.features[{position}]")
        for position, selector in enumerate(raw_features)
    )
    low, high = FEATURE_ARITY[quantity]
    if len(features) < low or (high is not None and len(features) > high):
        wanted = (
            str(low) if low == high else (f"at least {low}" if high is None else f"{low} to {high}")
        )
        raise RequirementError(
            f"{path}.features: a {quantity} takes {wanted} selector(s), got {len(features)}."
        )
    datum = (
        None if item.get("datum") is None else _text(item["datum"], f"{path}.datum", max_chars=16)
    )
    epsilon = item.get("verification_epsilon")
    if epsilon is not None:
        epsilon = positive(epsilon, f"{path}.verification_epsilon")
    tolerance = _parse_tolerance(
        item.get("manufacturing_tolerance"), f"{path}.manufacturing_tolerance"
    )
    return Requirement(
        id=requirement_id,
        quantity=quantity,
        target=freeze(target),
        features=features,
        provenance=provenance,
        source=freeze(_parse_source(item.get("source"), f"{path}.source")),
        datum=datum,
        manufacturing_tolerance=None if tolerance is None else freeze(tolerance),
        verification_epsilon=epsilon,
    )


def parse_requirements(raw: Any) -> RequirementSet:
    """A ``RequirementSet`` from typed ``items`` and/or a legacy ``dimensions`` object.

    Keys other than ``units``, ``items``, ``dimensions`` / ``legacy`` (the
    preflight's ``part_type``, ``tolerance_policy``, ``constraints``) are left
    to their own readers. Items come back sorted by id. Refusals are
    ``RequirementError`` (a ``SpecError``, so a ``ValueError``) naming the path.
    """
    if isinstance(raw, RequirementSet):
        return raw
    data = _mapping(raw, "requirements")
    if "dimensions" in data and "legacy" in data:
        raise RequirementError(
            "requirements: give 'dimensions' or 'legacy', not both (they are the same legacy object)."
        )
    legacy_raw = data.get("dimensions", data.get("legacy"))
    items_raw = data.get("items")
    if items_raw is None and legacy_raw is None:
        raise RequirementError(
            "requirements: nothing to read - give typed 'items' (a list of requirements) or a "
            "legacy 'dimensions' object."
        )
    if data.get("units") is None:
        raise RequirementError(
            f"units: required - a length means nothing without it ({', '.join(LENGTH_UNITS)})."
        )
    units = _unit(data["units"], "units", LENGTH_UNITS)
    requirements: tuple[Requirement, ...] = ()
    if items_raw is not None:
        if not isinstance(items_raw, (list, tuple)):
            raise RequirementError(f"items: expected a list, got {short_repr(items_raw)}.")
        within_budget(len(items_raw), config.settings.max_spec_items, "items", "MAX_SPEC_ITEMS")
        parsed = [_parse_item(value, index, units) for index, value in enumerate(items_raw)]
        seen: dict[str, int] = {}
        for index, requirement in enumerate(parsed):
            if requirement.id in seen:
                raise RequirementError(
                    f"items[{index}].id: {requirement.id!r} is already items[{seen[requirement.id]}].id; "
                    "an id names one requirement."
                )
            seen[requirement.id] = index
        requirements = tuple(sorted(parsed, key=lambda requirement: requirement.id))
    legacy = None if legacy_raw is None else freeze(_mapping(legacy_raw, "dimensions"))
    return RequirementSet(requirements=requirements, units=units, legacy=legacy)


def requirement_conflicts(rs: RequirementSet) -> list[dict]:
    """Conflicts kept in the set and reported: ``{code, field, values, message}``.

    ``CONFLICTING_PROVENANCE`` for an item marked ``conflicting``;
    ``CONFLICTING_REQUIREMENT`` for two items asking for the same quantity of
    the same feature(s) with different targets. Nothing is dropped or merged.
    """
    out: list[dict] = []
    for requirement in rs.requirements:
        if requirement.provenance == "conflicting":
            out.append(
                {
                    "code": "CONFLICTING_PROVENANCE",
                    "field": f"items[{requirement.id}]",
                    "values": [requirement.id],
                    "message": (
                        f"requirement {requirement.id!r} is marked conflicting: its sources "
                        "disagree. It is kept as given; resolve the source before it can pass."
                    ),
                }
            )
    groups: dict[tuple, list[Requirement]] = {}
    for requirement in rs.requirements:
        groups.setdefault((requirement.quantity, requirement.features), []).append(requirement)
    for (quantity, _features), members in groups.items():
        if len({member.target for member in members}) > 1:
            ids = [member.id for member in members]
            out.append(
                {
                    "code": "CONFLICTING_REQUIREMENT",
                    "field": "items",
                    "values": ids,
                    "message": (
                        f"requirements {ids} ask for the {quantity} of the same feature(s) with "
                        "different targets; all are kept - say which one controls."
                    ),
                }
            )
    return out


def encode_requirements(rs: RequirementSet) -> list[str]:
    """The stored form: ``{format, digest, set}`` as ASCII JSON in ``CHUNK_SIZE`` pieces."""
    payload = {"format": FORMAT_VERSION, "digest": rs.digest(), "set": rs.to_dict()}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)] or [""]


def decode_requirements(chunks) -> RequirementSet:
    """The set ``encode_requirements`` stored; an edited or damaged record is refused."""
    text = "".join(str(chunk) for chunk in chunks)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{DICT_NAME}: the stored record is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT_VERSION:
        found = payload.get("format") if isinstance(payload, dict) else type(payload).__name__
        raise ValueError(f"{DICT_NAME}: not a format {FORMAT_VERSION} record (got {found!r})")
    rs = parse_requirements(payload.get("set"))
    if rs.digest() != payload.get("digest"):
        raise ValueError(
            f"{DICT_NAME}: the stored digest {payload.get('digest')!r} does not match the stored "
            f"set ({rs.digest()!r}); the record was edited or damaged"
        )
    return rs


# ── the stored record (both engines) ─────────────────────────────────────────


async def store_requirements(backend, rs: RequirementSet) -> dict:
    """Write ``rs`` into the drawing as ``ACADMCP_REQUIREMENTS/CURRENT``, replacing it.

    A content change: the headless revision moves through ``_mark_dirty``, the
    live one through ``_bump_revision`` (not on an ``active_document_changed``
    refusal, which writes nothing). Returns ``{ok, dictionary, record, items,
    legacy, digest, chunks, replaced, backend}``.
    """
    chunks = encode_requirements(rs)
    if getattr(backend, "name", None) == "com":
        from backends.document_guard import ActiveDocumentChangedError

        refused = False
        try:
            replaced = await backend._run(_com_store, chunks)
        except ActiveDocumentChangedError:
            refused = True
            raise
        finally:
            if not refused:
                backend._bump_revision()
    else:
        replaced = await backend._async(_ezdxf_store, backend, chunks)
    return {
        "ok": True,
        "dictionary": DICT_NAME,
        "record": RECORD_NAME,
        "items": len(rs.requirements),
        "legacy": rs.legacy is not None,
        "digest": rs.digest(),
        "chunks": len(chunks),
        "replaced": replaced,
        "backend": backend.name,
    }


async def load_requirements(backend) -> RequirementSet | None:
    """The stored set, or None when the drawing carries no ``ACADMCP_REQUIREMENTS`` record."""
    if getattr(backend, "name", None) == "com":
        chunks = await backend._run(_com_chunks)
    else:
        chunks = await backend._async(_ezdxf_chunks, backend, readonly=True)
    return None if chunks is None else decode_requirements(chunks)


def _ezdxf_store(backend, chunks: list[str]) -> bool:
    doc = backend._require_doc()
    holder = doc.rootdict.get_required_dict(DICT_NAME)
    replaced = RECORD_NAME in holder
    if replaced:
        holder.remove(RECORD_NAME)
    record = holder.add_xrecord(RECORD_NAME)
    record.extend([(1000, chunk) for chunk in chunks])
    backend._mark_dirty()
    return replaced


def _ezdxf_chunks(backend) -> list[str] | None:
    doc = backend._require_doc()
    if DICT_NAME not in doc.rootdict:
        return None
    holder = doc.rootdict.get(DICT_NAME)
    if RECORD_NAME not in holder:
        return None
    return [str(tag.value) for tag in holder.get(RECORD_NAME).tags if tag.code == 1000]


def _com_holder(doc, *, create: bool):
    from backends import com_backend as com

    try:
        return com._com_unnarrow(doc.Dictionaries.Item(DICT_NAME))
    except Exception:  # absent: Dictionaries.Item raises for an unknown key
        if not create:
            return None
    return com._com_unnarrow(doc.Dictionaries.Add(DICT_NAME))


def _com_record(holder):
    from backends.com_backend import ComBackend

    if holder is None:
        return None
    for key, obj in ComBackend._com_dict_entries(holder):
        if key == RECORD_NAME:
            return obj
    return None


def _com_store(chunks: list[str]) -> bool:
    """COM thread: write the chunks into the bound document's record."""
    from backends import com_backend as com

    doc = com._acad_doc()
    holder = _com_holder(doc, create=True)
    record = _com_record(holder)
    replaced = record is not None
    if record is None:
        record = holder.AddXRecord(RECORD_NAME)
    record.SetXRecordData(com._ai([1000] * len(chunks)), com._avar(chunks))
    return replaced


def _com_chunks() -> list[str] | None:
    """COM thread: the record's chunks, or None."""
    from backends import com_backend as com
    from backends.com_backend import ComBackend

    record = _com_record(_com_holder(com._acad_doc(), create=False))
    return None if record is None else ComBackend._com_xrecord_chunks(record)
