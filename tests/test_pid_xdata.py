"""ACADMCP_PID payloads survive the 255-character XDATA string limit."""

from __future__ import annotations

import pytest

from engineering.pid.xdata import (
    APP_NAME,
    MARKER,
    decode_payload,
    encode_payload,
    line_payload,
    marker_payload,
    read_payload,
    write_payload,
)

pytestmark = pytest.mark.asyncio


def test_short_payload_is_marker_plus_one_chunk():
    values = encode_payload({"v": 1, "kind": "marker", "line": "3A"})
    assert values[0] == MARKER and len(values) == 2
    assert decode_payload(values) == {"kind": "marker", "line": "3A", "v": 1}


def test_long_payload_is_chunked_at_255_and_rejoined():
    payload = {"v": 1, "kind": "symbol", "ports": {f"N{i}": [i, i, 90] for i in range(60)}}
    values = encode_payload(payload)
    assert all(len(v) <= 255 for v in values)
    assert len(values) > 2
    assert decode_payload(values) == payload


def test_decode_rejects_foreign_or_broken_values():
    assert decode_payload([]) is None
    assert decode_payload(["not-json", "{}"]) is None
    assert decode_payload([MARKER, "{not json"]) is None


def test_builders_carry_the_documented_keys():
    line = line_payload(
        "process_major",
        "100-P-1001",
        "100",
        "P",
        "CS1",
        "IH",
        1001,
        {"handle": "2F", "port": "discharge"},
        {"handle": "31", "port": "N1"},
    )
    assert line["kind"] == "line" and line["class"] == "process_major"
    assert line["from"] == {"handle": "2F", "port": "discharge"} and line["seq"] == 1001
    assert marker_payload("3A") == {"v": 1, "kind": "marker", "line": "3A"}


async def test_roundtrip_through_a_backend(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)
    payload = {"v": 1, "kind": "line", "class": "electric", "number": None}
    await write_payload(backend, line.handle, payload)
    assert await read_payload(backend, line.handle) == payload
    got = await backend.entity_get_xdata(line.handle, APP_NAME)
    assert got["xdata"][APP_NAME][0] == MARKER


async def test_read_payload_is_none_without_xdata(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)
    assert await read_payload(backend, line.handle) is None
