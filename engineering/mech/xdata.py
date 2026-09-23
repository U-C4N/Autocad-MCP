"""``ACADMCP_MECH`` XDATA: the part model written onto the drawing.

The Track A codec (`engineering/pid/xdata.py`) with its limits made explicit.
XDATA strings cap at 255 characters and an entity carries at most 16 KB across
every application, so a payload is JSON-serialised ASCII-only and split into
1000 chunks under one 1001 application marker. `json.dumps(ensure_ascii=True)`
escapes every newline, so no chunk can carry a line separator into the DXF.

A payload that is not ours is *refused*, never decoded into something
plausible: a foreign application marker, a chunk stream that is not JSON, a
version this build does not read, or a kind outside `KINDS`. Three shapes are
written, and both the writer and every reader test them literally:

    {"v": 1, "kind": "part",     "id": "<hex>", "view": "front", "part": {...}}
    {"v": 1, "kind": "std_part", "designation": "ISO 4014 - M12x60",
     "standard": "ISO 4014", "size": "M12x60", "qty": 1, "material": "8.8"}
    {"v": 1, "kind": "balloon",  "item": 3, "targets": ["1A2B", ...]}

The codec is shared: ``app_id=`` / ``kinds=`` let another application write
its own payloads through the same chunking and the same refusals (track F
writes ``ACADMCP_ARCH`` with the kinds wall / opening / stair / room through
`engineering/arch/model.py`). The defaults are the mechanical ones, so every
mechanical caller is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from backends.xdata_specs import MAX_BYTES, app_size

APP_ID = "ACADMCP_MECH"
PAYLOAD_VERSION = 1
CHUNK = 255
KINDS: tuple[str, ...] = ("part", "std_part", "balloon")


def _validate(payload: dict, app_id: str, kinds: Sequence[str]) -> dict:
    if not isinstance(payload, dict):
        raise TypeError(f"{app_id}: a payload must be a dict, got {type(payload).__name__}")
    version = payload.get("v", PAYLOAD_VERSION)
    if version != PAYLOAD_VERSION:
        raise ValueError(
            f"{app_id}: payload version {version!r}; this build writes and reads "
            f"version {PAYLOAD_VERSION}"
        )
    kind = payload.get("kind")
    if kind not in kinds:
        raise ValueError(f"{app_id}: unknown payload kind {kind!r}; kinds are {', '.join(kinds)}")
    return {**payload, "v": PAYLOAD_VERSION}


def encode(
    payload: dict, *, app_id: str = APP_ID, kinds: Sequence[str] = KINDS
) -> list[tuple[int, str]]:
    """``[(1001, app_id), (1000, chunk), ...]`` — refused before anything is written."""
    checked = _validate(payload, app_id, kinds)
    text = json.dumps(checked, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    rows: list[tuple[int, str]] = [(1001, app_id)]
    rows.extend((1000, text[i : i + CHUNK]) for i in range(0, len(text), CHUNK))
    size = app_size(app_id, [(code, value) for code, value in rows if code != 1001])
    if size > MAX_BYTES:
        raise ValueError(
            f"{app_id}: payload encodes to {size} bytes; the limit is 16 KB per entity "
            "across every application"
        )
    return rows


def to_values(payload: dict, *, app_id: str = APP_ID, kinds: Sequence[str] = KINDS) -> list[str]:
    """Just the chunk strings — what ``entity_set_xdata(handle, app_id, values)`` takes."""
    return [value for code, value in encode(payload, app_id=app_id, kinds=kinds) if code == 1000]


def decode(rows: Sequence, *, app_id: str = APP_ID, kinds: Sequence[str] = KINDS) -> dict:
    """``rows`` may be ``(code, value)`` pairs or the bare value list a backend
    returns from ``entity_get_xdata``. Raises ValueError on anything foreign."""
    if not rows:
        raise ValueError(f"{app_id}: empty XDATA")
    parts: list[str] = []
    for row in rows:
        if isinstance(row, str):
            parts.append(row)
            continue
        try:
            code, value = row
        except (TypeError, ValueError):
            raise ValueError(f"{app_id}: {row!r} is not an XDATA (code, value) row") from None
        if code == 1001:
            if str(value) != app_id:
                raise ValueError(
                    f"{app_id}: this XDATA belongs to application {value!r}, not to us"
                )
            continue
        if code != 1000:
            raise ValueError(f"{app_id}: group code {code} is not a payload chunk")
        parts.append(str(value))
    try:
        payload = json.loads("".join(parts))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{app_id}: the chunk stream is not JSON ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{app_id}: the payload decoded to {type(payload).__name__}, not a dict")
    return _validate(payload, app_id, kinds)
