"""The ``property_override`` critique focus and its repair (v1.8.0, plan 1.8-01).

An entity on an engineering layer takes its colour, linetype and lineweight
from the layer; the layer set is where ISO 128 is enforced (``iso128``). An
entity that carries its own value escapes that discipline, and the commonest
case is invisible to a reader that trusts a tool's description: lineweight -3
is *Default* (the LWDEFAULT setting), not ByLayer (-1). Rival claim 4.5 was
exactly that - 22 entities left at -3, reported "by-layer", and
``drawing_critique(focus=None)`` saw nothing.

Three codes, one issue per layer and code (count + handles, at most
``MAX_HANDLES`` of them; the repair re-reads the layer, so it covers them all):

* ``lineweight_not_bylayer`` - anything but -1. An explicit value outside the
  ISO 128 set is an ``error``; -2 / -3 or an ISO value is a ``warning``.
* ``color_not_bylayer`` - an ACI other than 256 (0 is ByBlock).
* ``linetype_not_bylayer`` - a linetype other than BYLAYER.

Scope (the noise limit on a foreign drawing): the engineering layers
(``ENGINEERING_LAYERS``) plus the layer set applied to this drawing
(``drawing_apply_iso_layers`` or the plan's ``layer_set_id``). A title-block
layer (its name holds ``TITLE``) and a construction layer (``CONST`` -
``construction_left`` owns it) are exempt, and so is every member of a block
definition, layer 0 included: the snapshot holds layout entities only. Any
other layer is reported at ``info`` with ``in_scope: false``.

The read is the run's all-spaces snapshot (``engineering/qa_scan.py``); the
COM engine reaches the same reader through ``Document.Export``, where the
DXF integers are the ActiveX ones (acLnWtByLayer -1, acLnWtByBlock -2,
acLnWtByLwDefault -3).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from .plan_spec import ISO_128_LINEWEIGHTS_MM, Issue

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

#: ``backends.base.LINEWEIGHT_BYLAYER``, restated: importing ``backends.base``
#: here would close a cycle (base -> contracts -> engineering -> critique -> here).
LINEWEIGHT_BYLAYER = -1
_SENTINELS = {-1: "ByLayer", -2: "ByBlock", -3: "Default"}

FOCUS = "property_override"
LINEWEIGHT_NOT_BYLAYER = "lineweight_not_bylayer"
COLOR_NOT_BYLAYER = "color_not_bylayer"
LINETYPE_NOT_BYLAYER = "linetype_not_bylayer"
CODES = (LINEWEIGHT_NOT_BYLAYER, COLOR_NOT_BYLAYER, LINETYPE_NOT_BYLAYER)

#: Handles listed per issue; ``detail.count`` is the full number.
MAX_HANDLES = 50

#: ISO 128 lineweights in DXF hundredths of a mm.
ISO_128_HUNDREDTHS = frozenset(int(round(mm * 100)) for mm in ISO_128_LINEWEIGHTS_MM)

#: The ByLayer value each code's repair writes (``entity_set_properties`` keywords).
BYLAYER_VALUES: dict[str, dict] = {
    LINEWEIGHT_NOT_BYLAYER: {"lineweight": LINEWEIGHT_BYLAYER},
    COLOR_NOT_BYLAYER: {"color": 256},
    LINETYPE_NOT_BYLAYER: {"linetype": "ByLayer"},
}

_WHAT = {
    LINEWEIGHT_NOT_BYLAYER: "lineweight",
    COLOR_NOT_BYLAYER: "colour",
    LINETYPE_NOT_BYLAYER: "linetype",
}


def exempt_layer(name: str) -> bool:
    """A title-block or construction layer: never reported."""
    upper = (name or "").upper()
    return "TITLE" in upper or "CONST" in upper


def scope_layers(backend: AutoCADBackend) -> frozenset[str]:
    """Upper-cased names of the engineering layers plus the applied layer set."""
    from .layers import ENGINEERING_LAYERS, LAYER_SET_REGISTRY

    names = {row[0] for row in ENGINEERING_LAYERS}
    active = getattr(backend, "_active_layer_set_id", None)
    layer_set = active() if callable(active) else "mech"
    names |= {row[0] for row in LAYER_SET_REGISTRY.get(layer_set, ())}
    return frozenset(name.upper() for name in names if not exempt_layer(name))


def overrides(record) -> list[tuple[str, str, bool]]:
    """``(code, value label, off-standard)`` for each property the record sets itself."""
    found: list[tuple[str, str, bool]] = []
    weight = int(getattr(record, "lineweight", LINEWEIGHT_BYLAYER))
    if weight != LINEWEIGHT_BYLAYER:
        off_standard = weight >= 0 and weight not in ISO_128_HUNDREDTHS
        # The spelling of backends.base.lineweight_name.
        label = _SENTINELS.get(weight) or f"{weight / 100.0:.2f} mm"
        found.append((LINEWEIGHT_NOT_BYLAYER, label, off_standard))
    color = int(getattr(record, "color", 256))
    if color != 256:
        found.append((COLOR_NOT_BYLAYER, "ByBlock" if color == 0 else str(color), False))
    linetype = str(getattr(record, "linetype", "BYLAYER") or "BYLAYER")
    if linetype.upper() != "BYLAYER":
        found.append((LINETYPE_NOT_BYLAYER, linetype, False))
    return found


def property_override_issues(records, scope: frozenset[str]) -> list[Issue]:
    """One issue per (layer, code), layers in name order, codes in ``CODES`` order."""
    groups: dict[tuple[str, str], dict] = {}
    for record in records:
        layer = record.layer or "0"
        if exempt_layer(layer):
            continue
        for code, label, off_standard in overrides(record):
            group = groups.setdefault(
                (layer, code),
                {"handles": [], "values": Counter(), "error": False, "spaces": set()},
            )
            group["handles"].append(record.handle)
            group["values"][label] += 1
            group["error"] = group["error"] or off_standard
            group["spaces"].add(record.space)

    issues: list[Issue] = []
    for (layer, code), group in sorted(
        groups.items(), key=lambda item: (item[0][0].upper(), CODES.index(item[0][1]))
    ):
        in_scope = layer.upper() in scope
        severity = ("error" if group["error"] else "warning") if in_scope else "info"
        count = len(group["handles"])
        values = dict(sorted(group["values"].items(), key=lambda kv: (-kv[1], kv[0])))
        shown = ", ".join(f"{label} x{n}" for label, n in values.items())
        message = (
            f"{count} {'entity' if count == 1 else 'entities'} on layer '{layer}' "
            f"set {_WHAT[code]} themselves ({shown}) instead of ByLayer"
        )
        if group["error"]:
            message += "; an explicit lineweight outside the ISO 128 set"
        if not in_scope:
            message += " (outside the engineering layers and the applied layer set)"
        message += ". drawing_refine sets them ByLayer."
        issues.append(
            Issue(
                severity=severity,
                focus=FOCUS,
                message=message,
                handles=group["handles"][:MAX_HANDLES],
                detail={
                    "code": code,
                    "layer": layer,
                    "count": count,
                    "values": values,
                    "expected": "ByLayer",
                    "in_scope": in_scope,
                    "spaces": sorted(group["spaces"]),
                },
            )
        )
    return issues


async def check_property_override(backend: AutoCADBackend, shared: dict) -> list[Issue]:
    """The focus: every entity of every space, grouped per layer and code."""
    from .critique import _records

    records, issues = await _records(backend, shared, FOCUS)
    return issues + property_override_issues(records, scope_layers(backend))


check_property_override.needs_shared = True  # type: ignore[attr-defined]


async def repair_property_override(backend: AutoCADBackend, issue: Issue) -> dict:
    """Set every entity of the issue's layer that still overrides the issue's
    property back to ByLayer - what ``entity_batch_modify`` ``set_properties``
    does, one ``entity_set_properties`` per entity. The layer is re-read, so an
    issue that listed only ``MAX_HANDLES`` of its entities is repaired whole."""
    from .qa_scan import qa_snapshot

    code = str(issue.detail["code"])
    layer = str(issue.detail["layer"])
    if code not in BYLAYER_VALUES:
        raise RuntimeError(f"property_override repair: unknown code {code!r}")
    snap, _coverage = await qa_snapshot(backend, {})
    records = snap.records if snap is not None else ()
    targets = [
        record.handle
        for record in records
        if (record.layer or "0").upper() == layer.upper()
        and any(found == code for found, _label, _off in overrides(record))
    ]
    for handle in targets:
        await backend.entity_set_properties(handle, **BYLAYER_VALUES[code])
    return {"layer": layer, "code": code, "set": "ByLayer", "count": len(targets)}
