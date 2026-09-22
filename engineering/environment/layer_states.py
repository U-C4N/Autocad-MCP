"""Portable layer states: a layer-table snapshot <-> XRECORD string chunks.

These are the server's own layer states, not AutoCAD's. AutoCAD keeps its
Layer States Manager entries under the layer table's extension dictionary in
a layout neither engine authors; this codec stores a JSON snapshot as
1000-code string chunks in an XRECORD under the drawing's named object
dictionary ``ACADMCP_LAYERSTATES``. They live in the file and travel with it,
and they do **not** appear in AutoCAD's Layer States Manager (LAYERSTATE).
That sentence is repeated in the tool docstrings, in ``system_capabilities``
(feature ``layer_states``) and in the README.

Chunks are ASCII (``ensure_ascii``), so a 255-character boundary can never
split a code point, and 255 is AutoCAD's ``SetXRecordData`` string limit —
ezdxf itself would accept longer tags (measured: a 300-character tag
round-trips through save/reopen unchanged).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .names import validate_name

if TYPE_CHECKING:
    from backends.base import LayerInfo

DICT_NAME = "ACADMCP_LAYERSTATES"
PROPERTIES = ("on", "frozen", "locked", "color", "linetype", "lineweight", "plot", "current")
LAYER_PROPERTIES = PROPERTIES[:-1]
CHUNK_SIZE = 255
FORMAT_VERSION = 1


def validate_state_name(name) -> str:
    return validate_name(name, what="layer state name")


def validate_properties(properties) -> tuple[str, ...]:
    """The subset of ``PROPERTIES`` to apply; ``None`` means all of them."""
    if properties is None:
        return PROPERTIES
    if isinstance(properties, str) or not isinstance(properties, (list, tuple)):
        raise TypeError("properties must be a list of property names")
    out: list[str] = []
    for index, prop in enumerate(properties):
        if prop not in PROPERTIES:
            raise ValueError(f"properties[{index}]: {prop!r} is not one of {PROPERTIES}")
        if prop not in out:
            out.append(prop)
    if not out:
        raise ValueError(f"properties must name at least one of {PROPERTIES}")
    return tuple(out)


def snapshot_from_layers(
    layers: list[LayerInfo],
    current_layer: str,
    *,
    plot: dict[str, bool] | None = None,
    description: str | None = None,
) -> dict:
    """The state dict for a layer table.

    ``plot`` is per-layer plottability (``LayerInfo`` has no such field; each
    engine reads it and passes it in), defaulting to plottable.
    """
    plot = plot or {}
    return {
        "version": FORMAT_VERSION,
        "description": description,
        "current_layer": str(current_layer),
        "layers": {
            layer.name: {
                "on": bool(layer.is_on),
                "frozen": bool(layer.is_frozen),
                "locked": bool(layer.is_locked),
                "color": int(layer.color),
                "linetype": str(layer.linetype),
                "lineweight": int(round(float(layer.lineweight))),
                "plot": bool(plot.get(layer.name, True)),
            }
            for layer in layers
        },
    }


def encode_state(state: dict) -> list[str]:
    text = json.dumps(state, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    return [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)] or [""]


def decode_state(chunks) -> dict:
    text = "".join(str(chunk) for chunk in chunks)
    try:
        state = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"layer state payload is not valid JSON: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != FORMAT_VERSION:
        found = state.get("version") if isinstance(state, dict) else type(state).__name__
        raise ValueError(
            f"layer state payload is not a version {FORMAT_VERSION} state (got {found!r})"
        )
    layers = state.get("layers")
    if not isinstance(layers, dict):
        raise ValueError("layer state payload has no 'layers' mapping")
    for name, props in layers.items():
        if not isinstance(props, dict):
            raise ValueError(f"layer state entry {name!r} is not a mapping")
        missing = [prop for prop in LAYER_PROPERTIES if prop not in props]
        if missing:
            raise ValueError(f"layer state entry {name!r} lacks {missing}")
    state.setdefault("description", None)
    state.setdefault("current_layer", "0")
    return state


def diff_snapshot(state: dict, layers: list[LayerInfo]) -> tuple[list[str], list[str]]:
    """``(missing, new)``: in the state but not the drawing / in the drawing but not the state."""
    in_state = set(state["layers"])
    in_drawing = {layer.name for layer in layers}
    return sorted(in_state - in_drawing), sorted(in_drawing - in_state)
