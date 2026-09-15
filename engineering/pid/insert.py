"""Placing catalogue symbols: define the block on first use, insert with
attributes, write the ACADMCP_PID payload, return the ports in WCS."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engineering.layers import PID_LAYERS

from .symbols import FAMILY_LAYER, SymbolSpec, resolve, transform_port
from .tags import parse_tag, split_instrument_tag
from .xdata import symbol_payload, write_payload

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

FAIL_POSITIONS = ("", "FO", "FC", "FL")
_PID_LAYER_DEFS = {name: (color, linetype, lw) for name, color, linetype, lw, _desc in PID_LAYERS}


async def ensure_layer(backend: AutoCADBackend, layer_name: str) -> bool:
    """Create ``layer_name`` if missing — from PID_LAYERS when it is one of ours."""
    existing = {lyr.name.lower() for lyr in await backend.layer_list()}
    if layer_name.lower() in existing:
        return False
    color, linetype, lineweight = _PID_LAYER_DEFS.get(layer_name, (7, "Continuous", 0.25))
    await backend.layer_create(
        name=layer_name, color=color, linetype=linetype, lineweight=lineweight
    )
    return True


async def ensure_block(backend: AutoCADBackend, spec: SymbolSpec) -> bool:
    """Define ``spec``'s block if the drawing does not have it yet."""
    names = {blk.name for blk in await backend.block_list()}
    if spec.name in names:
        return False
    await backend.block_define(
        spec.name, list(spec.primitives), list(spec.attdefs), 0.0, 0.0, False
    )
    return True


async def insert_symbol(
    backend: AutoCADBackend,
    spec: SymbolSpec,
    x: float,
    y: float,
    rotation: float = 0.0,
    scale: float = 1.0,
    attributes: dict | None = None,
    layer: str | None = None,
    payload: dict | None = None,
) -> dict:
    layer_name = layer or FAMILY_LAYER[spec.family]
    if layer_name == "line":
        raise ValueError(f"{spec.name}: markers need an explicit layer (the line's)")
    created = await ensure_layer(backend, layer_name)
    defined = await ensure_block(backend, spec)
    values = {att["tag"]: att["default"] for att in spec.attdefs}
    values.update({k: str(v) for k, v in (attributes or {}).items()})
    ref = await backend.block_insert(
        spec.name, x, y, scale, scale, rotation, values or None, layer_name
    )
    await write_payload(backend, ref.handle, payload or symbol_payload(spec))
    ports = {p.name: transform_port(p, x, y, rotation, scale) for p in spec.ports}
    return {
        "handle": ref.handle,
        "block_name": spec.name,
        "defined": defined,
        "layers_created": [layer_name] if created else [],
        "ports": ports,
        "layer": layer_name,
    }


async def place_symbol(
    backend: AutoCADBackend,
    symbol: str,
    x: float,
    y: float,
    rotation: float = 0.0,
    scale: float = 1.0,
    tag: str | None = None,
    desc: str | None = None,
    fail: str | None = None,
    link: str | None = None,
    layer: str | None = None,
    **options,
) -> dict:
    """The ``pid_symbol_insert`` tool body."""
    if scale <= 0:
        raise ValueError("scale must be > 0")
    spec = resolve(symbol, **options)
    if spec.family == "marker":
        raise ValueError(f"{symbol} is a line marker; pid_line_draw places markers and arrows")
    tags_present = {att["tag"] for att in spec.attdefs}
    attributes: dict[str, str] = {}
    warnings: list[str] = []
    tag_text: str | None = None
    if tag:
        tag_text = tag.strip()
        if spec.family == "instrument":
            parsed = parse_tag(tag_text, kind="instrument")
            func, loop = split_instrument_tag(tag_text)
            attributes.update(FUNC=func, LOOP=loop)
        else:
            parsed = parse_tag(tag_text, kind="auto")
            attributes["TAG"] = tag_text
        if not parsed["valid"]:
            warnings.extend(parsed["errors"])
    if desc is not None:
        if "DESC" not in tags_present:
            warnings.append(f"{symbol} has no DESC attribute; description ignored")
        else:
            attributes["DESC"] = desc
    if fail is not None:
        if "FAIL" not in tags_present:
            raise ValueError(f"{symbol}: 'fail' needs an actuator (no FAIL attribute)")
        if fail.upper() not in FAIL_POSITIONS:
            raise ValueError("fail must be one of FO, FC, FL (or empty)")
        attributes["FAIL"] = fail.upper()
    if link is not None:
        if "LINK" not in tags_present:
            raise ValueError(f"{symbol}: 'link' is only valid for offpage connectors")
        attributes["LINK"] = link
    result = await insert_symbol(backend, spec, x, y, rotation, scale, attributes, layer)
    result.update(tag=tag_text, tag_warnings=warnings, backend=backend.name)
    return result
