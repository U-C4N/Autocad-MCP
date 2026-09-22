"""The ACADMCP_MECH chunking codec: Track A's shape, one app, 255-char chunks."""

from __future__ import annotations

import pytest

from backends.xdata_specs import encode_values
from engineering.mech.xdata import APP_ID, KINDS, PAYLOAD_VERSION, decode, encode, to_values

PART = {
    "v": 1,
    "kind": "part",
    "id": "1A2B",
    "view": "front",
    "part": {"name": "shaft", "segments": [{"length": 60.0, "d_outer": 40.0}]},
}


def test_app_id_version_and_kinds_are_the_pinned_ones():
    assert APP_ID == "ACADMCP_MECH"
    assert PAYLOAD_VERSION == 1
    assert KINDS == ("part", "std_part", "balloon")


def test_encode_opens_with_the_app_id_and_chunks_at_255():
    rows = encode({"v": 1, "kind": "balloon", "item": 3, "targets": ["1A2B"] * 60})
    assert rows[0] == (1001, APP_ID)
    assert all(code == 1000 for code, _value in rows[1:])
    assert len(rows) > 2, "the payload must actually have been split"
    assert all(len(value) <= 255 for _code, value in rows[1:])


def test_roundtrip_is_exact():
    assert decode(encode(PART)) == PART


def test_decode_reads_the_bare_value_list_a_backend_returns():
    """`entity_get_xdata` hands back decoded values, not (code, value) rows."""
    assert decode(to_values(PART)) == PART


def test_the_version_is_injected_when_absent_and_a_wrong_one_is_refused():
    payload = {
        "kind": "std_part",
        "designation": "ISO 4014 - M12x60",
        "standard": "ISO 4014",
        "size": "M12x60",
        "qty": 1,
        "material": "8.8",
    }
    assert decode(encode(payload))["v"] == PAYLOAD_VERSION
    with pytest.raises(ValueError, match="version"):
        encode({**payload, "v": 99})


def test_a_foreign_application_is_refused():
    with pytest.raises(ValueError, match="ACADMCP_PID"):
        decode([(1001, "ACADMCP_PID"), (1000, '{"v":1,"kind":"part"}')])


def test_a_corrupt_payload_is_refused_not_silently_dropped():
    with pytest.raises(ValueError, match="not JSON"):
        decode([(1001, APP_ID), (1000, '{"v":1,"kind":')])


def test_an_unknown_kind_is_refused_on_both_sides():
    with pytest.raises(ValueError, match="kind"):
        encode({"v": 1, "kind": "sheet_frame"})
    with pytest.raises(ValueError, match="kind"):
        decode([(1001, APP_ID), (1000, '{"v":1,"kind":"sheet_frame"}')])


def test_the_16_kb_entity_ceiling_is_enforced_before_writing():
    huge = {"v": 1, "kind": "part", "id": "1", "view": "front", "part": {"n": "x" * 20000}}
    with pytest.raises(ValueError, match="16 KB"):
        encode(huge)


def test_every_chunk_is_a_value_the_backend_will_accept():
    """`entity_set_xdata` runs the values through `encode_values`; ASCII-only
    JSON means no line separator can reach a 1000 tag and split the DXF."""
    rows = encode(PART)
    tags = encode_values(to_values(PART))
    assert all(code == 1000 for code, _value in tags)
    assert len(tags) == len(rows) - 1
