"""B07 on the ``ACADMCP_PID`` payloads: backlog rows W2-T5-04, -05, -06 and -07."""

from __future__ import annotations

import pytest

from engineering.pid.symbols import Port, make_spec, resolve
from engineering.pid.xdata import decode_payload, encode_payload, symbol_payload
from engineering.validation import SpecError

# ── W2-T5-04: NaN is not JSON ───────────────────────────────────────────────


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_is_refused_before_it_is_written(bad):
    """RED: allow_nan=True wrote the non-RFC literal NaN into the drawing."""
    with pytest.raises(SpecError, match=r"^ACADMCP_PID payload: Out of range float values"):
        encode_payload({"a": bad})


# ── W2-T5-07: only an object can be read back ───────────────────────────────


@pytest.mark.parametrize("payload", [[1, 2], "text", 3, None])
def test_a_non_object_payload_is_refused(payload):
    """RED: encode_payload([1, 2]) wrote ['json', '[1,2]'], which decode_payload
    then read back as None - written, never readable."""
    with pytest.raises(SpecError, match=r"^ACADMCP_PID payload: must be an object"):
        encode_payload(payload)


def test_an_object_still_round_trips():
    payload = {"v": 1, "kind": "line", "number": "100-P-1"}
    assert decode_payload(encode_payload(payload)) == payload


# ── W2-T5-05: params={} means no params ─────────────────────────────────────


def test_explicit_empty_params_are_kept():
    """RED: ``params or spec.params`` replaced {} with the spec's full params."""
    spec = resolve("vertical_vessel")
    assert spec.params, "the vessel carries its canonical parameters"
    assert symbol_payload(spec, params={})["params"] == {}
    assert symbol_payload(spec)["params"] == spec.params


# ── W2-T5-06: duplicate port names ──────────────────────────────────────────


def test_two_ports_with_one_name_are_refused():
    """RED: both were kept on the spec and symbol_payload (keyed by name) kept one."""
    ports = [Port("shaft", 0.0, 0.0, 0.0), Port("shaft", 5.0, 0.0, 180.0)]
    with pytest.raises(SpecError) as excinfo:
        make_spec("PID_TEST", "misc", "test", None, [], (), ports, "test")
    assert str(excinfo.value) == (
        "PID_TEST: port 'shaft' is defined twice; port names must be unique"
    )


def test_distinct_ports_are_accepted():
    ports = [Port("in", 0.0, 0.0, 180.0), Port("out", 5.0, 0.0, 0.0)]
    spec = make_spec("PID_TEST", "misc", "test", None, [], (), ports, "test")
    assert [p.name for p in spec.ports] == ["in", "out"]
