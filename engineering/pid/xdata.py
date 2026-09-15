"""``ACADMCP_PID`` XDATA payloads.

XDATA strings are capped at 255 characters, so a JSON payload is stored as
``["json", chunk, chunk, ...]`` and re-joined on read. Every symbol INSERT and
every P&ID line carries one, so a reader without the catalogue (another tool,
a later catalogue version) still sees ports, classes and numbers.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

APP_NAME = "ACADMCP_PID"
MARKER = "json"
CHUNK = 255
PAYLOAD_VERSION = 1


def encode_payload(payload: dict) -> list[str]:
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    return [MARKER] + [text[i : i + CHUNK] for i in range(0, len(text), CHUNK)]


def decode_payload(values: list) -> dict | None:
    if not values or values[0] != MARKER:
        return None
    parts = [v for v in values[1:] if isinstance(v, str)]
    try:
        decoded = json.loads("".join(parts))
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


async def write_payload(backend: AutoCADBackend, handle: str, payload: dict) -> dict:
    return await backend.entity_set_xdata(handle, APP_NAME, encode_payload(payload))


async def read_payload(backend: AutoCADBackend, handle: str) -> dict | None:
    result = await backend.entity_get_xdata(handle, APP_NAME)
    return decode_payload(result.get("xdata", {}).get(APP_NAME, []))


def symbol_payload(spec: Any, params: dict | None = None) -> dict:
    """``spec`` is an ``engineering.pid.symbols.SymbolSpec`` (typed loosely to
    avoid an import cycle). Ports are symbol-local ``[x, y, direction, kind, radius]``."""
    from .symbols import CATALOG_VERSION

    return {
        "v": PAYLOAD_VERSION,
        "kind": "symbol",
        "catalog": CATALOG_VERSION,
        "name": spec.name,
        "family": spec.family,
        "symbol": spec.symbol,
        "variant": spec.variant,
        "params": dict(params or spec.params),
        "ports": {p.name: [p.x, p.y, p.direction_deg, p.kind, p.radius] for p in spec.ports},
    }


def line_payload(
    line_class: str,
    number: str | None,
    size: str | None,
    service: str | None,
    spec: str | None,
    insulation: str | None,
    seq: int | None,
    from_ref: dict | None,
    to_ref: dict | None,
) -> dict:
    return {
        "v": PAYLOAD_VERSION,
        "kind": "line",
        "class": line_class,
        "number": number,
        "size": size,
        "service": service,
        "spec": spec,
        "insulation": insulation,
        "seq": seq,
        "from": from_ref,
        "to": to_ref,
    }


def marker_payload(line_handle: str) -> dict:
    return {"v": PAYLOAD_VERSION, "kind": "marker", "line": line_handle}
