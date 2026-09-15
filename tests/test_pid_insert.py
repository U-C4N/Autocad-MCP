"""pid_symbol_insert: define on first use, insert with attributes, WCS ports, XDATA."""

from __future__ import annotations

import pytest

from engineering.pid.insert import ensure_layer, place_symbol
from engineering.pid.symbols import resolve
from engineering.pid.xdata import read_payload

pytestmark = pytest.mark.asyncio


async def test_first_insert_defines_the_block_and_creates_its_layer(backend):
    result = await place_symbol(backend, "gate", 100.0, 50.0, tag="HV-101")
    assert result["defined"] is True and result["layers_created"] == ["PROCESS-VALVES"]
    assert result["block_name"] == "PID_VALVE_GATE" and result["layer"] == "PROCESS-VALVES"
    assert result["ports"]["in"] == {
        "name": "in",
        "x": 96.0,
        "y": 50.0,
        "direction_deg": 180.0,
        "kind": "process",
        "radius": 0.0,
        "inferred": False,
    }
    assert result["tag"] == "HV-101" and result["tag_warnings"] == []
    assert await backend.block_get_attributes(result["handle"]) == {"TAG": "HV-101", "DESC": ""}
    again = await place_symbol(backend, "gate", 200.0, 50.0, tag="HV-102")
    assert again["defined"] is False and again["layers_created"] == []


async def test_rotation_and_scale_move_the_ports(backend):
    result = await place_symbol(
        backend, "centrifugal_pump", 0.0, 0.0, rotation=90.0, scale=2.0, tag="P-101"
    )
    discharge = result["ports"]["discharge"]
    assert (discharge["x"], discharge["y"], discharge["direction_deg"]) == pytest.approx(
        (-12.0, 0.0, 180.0)
    )


async def test_instrument_tag_splits_into_func_and_loop(backend):
    result = await place_symbol(
        backend, "instrument", 10, 10, tag="10-FIC-101A", type="dcs", location="primary"
    )
    assert await backend.block_get_attributes(result["handle"]) == {"FUNC": "FIC", "LOOP": "101A"}
    assert result["block_name"] == "PID_INST_DCS_PRIMARY" and result["layer"] == "INSTRUMENT-SYMBOL"
    assert result["ports"]["signal"]["radius"] == 5.0
    assert result["ports"]["signal"]["direction_deg"] is None


async def test_invalid_tag_is_written_and_warned_not_refused(backend):
    result = await place_symbol(backend, "instrument", 0, 0, tag="FCI-9")
    assert await backend.block_get_attributes(result["handle"]) == {"FUNC": "FCI", "LOOP": "9"}
    assert result["tag_warnings"] and "I" in result["tag_warnings"][0]


async def test_actuator_fail_and_offpage_link_attributes(backend):
    cv = await place_symbol(backend, "globe", 0, 0, tag="FCV-105", actuator="diaphragm", fail="FC")
    assert (await backend.block_get_attributes(cv["handle"]))["FAIL"] == "FC"
    assert "signal" in cv["ports"]
    with pytest.raises(ValueError, match="FO, FC, FL"):
        await place_symbol(backend, "globe", 0, 0, actuator="diaphragm", fail="XX")
    off = await place_symbol(
        backend, "offpage", 0, 0, tag="TO P&ID-002", link="L-17", direction="out"
    )
    assert (await backend.block_get_attributes(off["handle"]))["LINK"] == "L-17"
    with pytest.raises(ValueError, match="link"):
        await place_symbol(backend, "gate", 0, 0, link="L-1")


async def test_xdata_payload_carries_ports_and_params(backend):
    result = await place_symbol(
        backend, "vertical_vessel", 0, 0, tag="V-201", params={"width": 20, "height": 40}
    )
    payload = await read_payload(backend, result["handle"])
    assert payload["kind"] == "symbol" and payload["catalog"] == "1"
    assert payload["params"]["width"] == 20.0
    assert set(payload["ports"]) == {"N1", "N2", "N3", "N4"}


async def test_refusals(backend):
    with pytest.raises(ValueError, match="scale"):
        await place_symbol(backend, "gate", 0, 0, scale=0)
    with pytest.raises(ValueError, match="pid_line_draw"):
        await place_symbol(backend, "arrow_flow", 0, 0)
    with pytest.raises(ValueError, match="params"):
        await place_symbol(backend, "gate", 0, 0, params={"width": 1})
    with pytest.raises(ValueError, match="actuator"):
        await place_symbol(backend, "check", 0, 0, actuator="motor")


async def test_layer_override_and_ensure_layer(backend):
    assert await ensure_layer(backend, "PROCESS-LINE-TEXT") is True
    assert await ensure_layer(backend, "PROCESS-LINE-TEXT") is False
    layers = {lyr.name: lyr for lyr in await backend.layer_list()}
    assert layers["PROCESS-LINE-TEXT"].lineweight == 25.0, (
        "LayerInfo reports DXF hundredths of a millimetre"
    )
    result = await place_symbol(backend, "gate", 0, 0, layer="MY-VALVES")
    assert result["layer"] == "MY-VALVES" and result["layers_created"] == ["MY-VALVES"]
    spec = resolve("gate")
    assert spec.name in {b.name for b in await backend.block_list()}
