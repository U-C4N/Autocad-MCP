"""pid_line_draw: port-to-port orthogonal lines with class, number, markers, arrows."""

from __future__ import annotations

import pytest

from engineering.pid.drawlines import draw_line
from engineering.pid.insert import place_symbol
from engineering.pid.xdata import read_payload

pytestmark = pytest.mark.asyncio


async def _pump_and_vessel(backend):
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-101")
    vessel = await place_symbol(backend, "vertical_vessel", 220, 120, tag="V-201")
    return pump, vessel


async def test_process_line_between_ports(backend):
    pump, vessel = await _pump_and_vessel(backend)
    result = await draw_line(
        backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": vessel["handle"], "port": "N3"},
        line_class="process_major",
        size="100",
        service="P",
        spec="CS1",
    )
    assert result["layer"] == "PROCESS-PIPING-MAIN" and result["line_class"] == "process_major"
    assert result["vertices"][0] == [100.0, 106.0] and result["vertices"][-1] == [207.0, 128.0]
    assert result["line_number"] == "100-P-1-CS1" and result["label_handle"]
    assert result["arrow_handle"] and result["marker_handles"] == []
    assert result["crossings"] == 0 and result["port_reuse"] == []
    info = await backend.entity_get(result["handle"])
    assert info.type == "LWPOLYLINE" and info.layer == "PROCESS-PIPING-MAIN"
    payload = await read_payload(backend, result["handle"])
    assert payload["kind"] == "line" and payload["from"] == {
        "handle": pump["handle"],
        "port": "discharge",
    }
    assert payload["seq"] == 1
    label = await backend.entity_get(result["label_handle"])
    assert label.layer == "PROCESS-LINE-TEXT" and label.properties["text"] == "100-P-1-CS1"
    arrow = await backend.entity_get(result["arrow_handle"])
    assert arrow.properties["block_name"] == "PID_MARKER_ARROW_FLOW"
    assert arrow.layer == "PROCESS-PIPING-MAIN"


async def test_sequence_increments_and_number_can_be_given(backend):
    pump, vessel = await _pump_and_vessel(backend)
    first = await draw_line(
        backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": vessel["handle"], "port": "N3"},
        size="100",
        service="P",
    )
    second = await draw_line(
        backend,
        {"handle": vessel["handle"], "port": "N4"},
        {"x": 300, "y": 128},
        size="80",
        service="P",
    )
    assert (first["line_number"], second["line_number"]) == ("100-P-1", "80-P-2")
    third = await draw_line(
        backend,
        {"handle": vessel["handle"], "port": "N1"},
        {"x": 210, "y": 200},
        line_number="VENT-01",
    )
    assert third["line_number"] == "VENT-01"


async def test_signal_line_from_a_bubble_gets_markers_and_no_arrow(backend):
    cv = await place_symbol(backend, "globe", 100, 100, tag="FCV-1", actuator="diaphragm")
    fic = await place_symbol(
        backend, "instrument", 100, 160, tag="FIC-1", type="dcs", location="primary"
    )
    result = await draw_line(
        backend,
        {"handle": fic["handle"]},
        {"handle": cv["handle"], "port": "signal"},
        line_class="pneumatic",
    )
    assert result["vertices"][0] == [100.0, 155.0], "the line starts on the bubble's circle"
    assert result["vertices"][-1] == [100.0, 109.0]
    assert result["arrow_handle"] is None and len(result["marker_handles"]) == 1
    mark = await backend.entity_get(result["marker_handles"][0])
    assert mark.properties["block_name"] == "PID_MARKER_MARK_PNEUMATIC"
    assert mark.layer == "INSTRUMENT-LINE-SIGNAL"
    line = await backend.entity_get(result["handle"])
    assert line.linetype == "Continuous"
    electric = await draw_line(
        backend, {"handle": fic["handle"]}, {"x": 160, "y": 160}, line_class="electric"
    )
    assert (await backend.entity_get(electric["handle"])).linetype == "ByLayer"


async def test_port_reuse_and_crossings_are_reported_not_refused(backend):
    pump, vessel = await _pump_and_vessel(backend)
    await draw_line(
        backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": vessel["handle"], "port": "N3"},
    )
    again = await draw_line(
        backend, {"handle": pump["handle"], "port": "discharge"}, {"x": 100, "y": 200}
    )
    assert again["port_reuse"] == [{"handle": pump["handle"], "port": "discharge"}]
    crossing = await draw_line(
        backend, {"x": 150, "y": 90}, {"x": 150, "y": 140}, line_class="utility"
    )
    assert crossing["crossings"] == 1


async def test_refusals(backend):
    pump, vessel = await _pump_and_vessel(backend)
    with pytest.raises(ValueError, match="same"):
        await draw_line(
            backend,
            {"handle": pump["handle"], "port": "discharge"},
            {"handle": pump["handle"], "port": "discharge"},
        )
    with pytest.raises(ValueError, match="discharge, suction"):  # payload ports are JSON-sorted
        await draw_line(backend, {"handle": pump["handle"], "port": "outlet"}, {"x": 0, "y": 0})
    with pytest.raises(ValueError, match="choose a port"):
        await draw_line(backend, {"handle": pump["handle"]}, {"x": 0, "y": 0})
    with pytest.raises(ValueError, match="line_class"):
        await draw_line(backend, {"x": 0, "y": 0}, {"x": 10, "y": 0}, line_class="steam")
    first = await draw_line(
        backend, {"handle": pump["handle"], "port": "discharge"}, {"x": 100, "y": 200}
    )
    with pytest.raises(ValueError, match="decoration"):
        await draw_line(backend, {"handle": first["arrow_handle"], "port": "in"}, {"x": 0, "y": 0})
    with pytest.raises(ValueError, match="not a P&ID symbol"):
        line = await backend.entity_create_line(0, 0, 1, 1)
        await draw_line(backend, {"handle": line.handle, "port": "in"}, {"x": 0, "y": 0})
