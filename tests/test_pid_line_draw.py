"""pid_line_draw: port-to-port orthogonal lines with class, number, markers, arrows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backends.com_backend import ComBackend
from engineering.pid.drawlines import draw_line, existing_pid_lines, resolve_endpoint
from engineering.pid.insert import place_symbol
from engineering.pid.xdata import (
    APP_NAME,
    encode_payload,
    line_payload,
    read_payload,
    write_payload,
)

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


async def test_mirrored_symbol_ports_follow_the_mirrored_geometry(backend):
    """``entity_mirror`` on an INSERT writes ``y_scale = -1``; the port has to be
    on the mirrored pump (discharge stub at (200, 104)->(200, 106)), not at the
    unmirrored offset — a line attached at y=94 runs through the pump body."""
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-1")
    about_vertical = await backend.entity_mirror(pump["handle"], 150, 0, 150, 200)
    info = await backend.entity_get(about_vertical.handle)
    assert info.properties["y_scale"] == -1.0, "the fixture must exercise a real mirror"
    ep = await resolve_endpoint(backend, {"handle": about_vertical.handle, "port": "discharge"})
    assert (ep["x"], ep["y"], ep["direction_deg"]) == (200.0, 106.0, 90.0)
    suction = await resolve_endpoint(backend, {"handle": about_vertical.handle, "port": "suction"})
    assert (suction["x"], suction["y"], suction["direction_deg"]) == (204.0, 100.0, 0.0)
    result = await draw_line(
        backend, {"handle": about_vertical.handle, "port": "discharge"}, {"x": 200, "y": 160}
    )
    assert result["vertices"][0] == [200.0, 106.0]

    about_horizontal = await backend.entity_mirror(pump["handle"], 0, 150, 300, 150)
    ep = await resolve_endpoint(backend, {"handle": about_horizontal.handle, "port": "discharge"})
    assert (ep["x"], ep["y"], ep["direction_deg"]) == (100.0, 194.0, 270.0)


async def test_an_extrusion_mirrored_symbol_resolves_its_port_on_the_mirror_image(backend):
    """MIRROR3D (and foreign DXFs) store a mirror as extrusion -Z rather than
    a negative scale; the port is where the mirrored geometry is."""
    from ezdxf.math import Vec3

    pump = await place_symbol(backend, "centrifugal_pump", 50, 20, rotation=30.0, tag="P-1")
    ent = backend._doc.entitydb.get(pump["handle"])
    ent.dxf.extrusion = (0, 0, -1)
    ent.dxf.insert = (-50, 20, 0)  # the same WCS insertion point
    truth = ent.matrix44().transform(Vec3(0, 6, 0))
    ep = await resolve_endpoint(backend, {"handle": pump["handle"], "port": "discharge"})
    assert (ep["x"], ep["y"]) == pytest.approx((truth.x, truth.y))
    assert (ep["x"], ep["y"]) == pytest.approx((53.0, 25.196), abs=1e-3)
    assert ep["direction_deg"] == pytest.approx(60.0)
    # unrotated: the suction that faced -x at (46, 20) now faces +x at (54, 20)
    pump = await place_symbol(backend, "centrifugal_pump", 150, 20, tag="P-2")
    ent = backend._doc.entitydb.get(pump["handle"])
    ent.dxf.extrusion = (0, 0, -1)
    ent.dxf.insert = (-150, 20, 0)
    ep = await resolve_endpoint(backend, {"handle": pump["handle"], "port": "suction"})
    assert (ep["x"], ep["y"], ep["direction_deg"]) == pytest.approx((154.0, 20.0, 0.0))
    result = await draw_line(
        backend, {"handle": pump["handle"], "port": "suction"}, {"x": 200, "y": 20}
    )
    assert result["vertices"][0] == pytest.approx([154.0, 20.0])


async def test_stretched_symbol_is_refused_not_resolved_off_the_geometry(backend):
    pump = await place_symbol(backend, "centrifugal_pump", 100, 100, tag="P-1")
    payload = await read_payload(backend, pump["handle"])
    stretched = await backend.block_insert(pump["block_name"], 300, 100, 2.0, 1.0, 0.0)
    await write_payload(backend, stretched.handle, payload)
    with pytest.raises(ValueError, match="non-uniform"):
        await resolve_endpoint(backend, {"handle": stretched.handle, "port": "discharge"})
    with pytest.raises(ValueError, match="non-uniform"):
        await draw_line(
            backend, {"handle": stretched.handle, "port": "discharge"}, {"x": 0, "y": 0}
        )


# ── the COM engine names a lightweight polyline ``AcDbPolyline`` ─────────────


class _ComEntity:
    """An ActiveX entity with only the members it is given."""

    def __init__(self, object_name, **members):
        self.ObjectName = object_name
        for name, value in members.items():
            setattr(self, name, value)

    def __getattr__(self, name):
        raise AttributeError(name)


def _com_pid_polyline(handle, coords, payload):
    tags = encode_payload(payload)
    codes = [1001] + [1000] * len(tags)
    values = [APP_NAME] + tags
    return _ComEntity(
        "AcDbPolyline",
        Handle=handle,
        Layer="PROCESS-PIPING-MAIN",
        Color=256,
        Linetype="ByLayer",
        Visible=True,
        Closed=False,
        Length=1.0,
        Coordinates=tuple(coords),
        Normal=(0.0, 0.0, 1.0),
        Elevation=0.0,
        GetBoundingBox=lambda: ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        GetXData=lambda app: (codes, values),
    )


def _fake_com(monkeypatch, entities):
    backend = ComBackend()

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", run_inline)
    by_handle = {ent.Handle: ent for ent in entities}
    space = SimpleNamespace(Count=len(entities), Item=lambda i: entities[i])
    doc = SimpleNamespace(HandleToObject=lambda handle: by_handle[handle])
    monkeypatch.setattr("backends.com_backend._msp", lambda: space)
    monkeypatch.setattr("backends.com_backend._acad_doc", lambda: doc)
    return backend


async def test_existing_lines_are_seen_on_the_com_engine(monkeypatch):
    """A live LWPOLYLINE's ObjectName is ``AcDbPolyline`` (measured on AutoCAD
    2026), which the COM engine reports as type POLYLINE. A reader that filters
    on LWPOLYLINE sees nothing there: every number restarts at seq 1, crossings
    stay 0 and port reuse is never reported."""
    payload = line_payload(
        "process_major",
        "100-P-7",
        "100",
        "P",
        None,
        None,
        7,
        {"handle": "2A", "port": "discharge"},
        None,
    )
    poly = _com_pid_polyline("3E", (100.0, 106.0, 100.0, 128.0, 207.0, 128.0), payload)
    other_layer = _com_pid_polyline("3F", (0.0, 0.0, 1.0, 1.0), payload)
    other_layer.Layer = "GEOMETRY"
    backend = _fake_com(monkeypatch, [poly, other_layer])

    assert (await backend.entity_get("3E")).type == "POLYLINE"
    lines = await existing_pid_lines(backend)

    assert [ln["handle"] for ln in lines] == ["3E"]
    assert lines[0]["vertices"] == [(100.0, 106.0), (100.0, 128.0), (207.0, 128.0)]
    assert lines[0]["payload"]["seq"] == 7
    assert lines[0]["payload"]["from"] == {"handle": "2A", "port": "discharge"}


# ── Track A hardening (track E wave 0, Task 6) ───────────────────────────────


async def _bubble_and_valve(backend):
    cv = await place_symbol(backend, "globe", 100, 100, tag="FCV-1", actuator="diaphragm")
    fic = await place_symbol(
        backend, "instrument", 100, 160, tag="FIC-1", type="dcs", location="primary"
    )
    return cv, fic


async def test_signal_lines_carry_no_fabricated_number_and_do_not_consume_the_sequence(backend):
    """A signal line has no pipe line number: no auto number, no label, and the
    process sequence is left alone."""
    cv, fic = await _bubble_and_valve(backend)
    pump, vessel = await _pump_and_vessel(backend)
    signal = await draw_line(
        backend, {"handle": fic["handle"]}, {"handle": cv["handle"], "port": "signal"}, "pneumatic"
    )
    assert signal["line_number"] is None and signal["label_handle"] is None
    payload = await read_payload(backend, signal["handle"])
    assert payload["number"] is None and payload["seq"] is None
    labels = await backend.entity_count(type_filter="TEXT", layer_filter="PROCESS-LINE-TEXT")
    assert labels == 0, "no label text was written for the signal line"

    process = await draw_line(
        backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": vessel["handle"], "port": "N3"},
        size="100",
        service="P",
    )
    assert process["line_number"] == "100-P-1", "the signal line did not consume seq 1"
    assert (await read_payload(backend, process["handle"]))["seq"] == 1

    # A signal line given size/service still gets no auto number (kind rules).
    electric = await draw_line(
        backend, {"handle": fic["handle"]}, {"x": 160, "y": 160}, "electric", size="IA", service="S"
    )
    assert electric["line_number"] is None and electric["label_handle"] is None

    # A verbatim number is honoured and labelled, seq stays None.
    named = await draw_line(
        backend, {"handle": fic["handle"]}, {"x": 40, "y": 160}, "electric", line_number="IA-7"
    )
    assert named["line_number"] == "IA-7" and named["label_handle"]
    assert (await backend.entity_get(named["label_handle"])).properties["text"] == "IA-7"
    assert (await read_payload(backend, named["handle"]))["seq"] is None

    # The next process line is 2, not 4.
    second = await draw_line(
        backend,
        {"handle": vessel["handle"], "port": "N4"},
        {"x": 300, "y": 128},
        size="80",
        service="P",
    )
    assert second["line_number"] == "80-P-2"


async def test_a_bubble_exit_aims_at_the_first_waypoint(backend):
    """With waypoints the radial port must leave towards the first waypoint;
    aiming at the far end put the exit on the wrong side and the first segment
    cut through the bubble (measured: 4.93 mm from the centre of a r=5 bubble)."""
    fic = await place_symbol(
        backend, "instrument", 100, 160, tag="FIC-1", type="dcs", location="primary"
    )
    result = await draw_line(
        backend,
        {"handle": fic["handle"]},
        {"x": 100, "y": 100},
        "electric",
        route_mode=[[130, 160], [130, 100]],
    )
    assert result["vertices"] == [[105.0, 160.0], [130.0, 160.0], [130.0, 100.0], [100.0, 100.0]]
    # Every vertex of the drawn line is on or outside the bubble.
    for x, y in result["vertices"]:
        assert ((x - 100) ** 2 + (y - 160) ** 2) ** 0.5 >= 5.0 - 1e-9


async def test_a_bubble_entry_aims_at_the_last_waypoint(backend):
    fic = await place_symbol(
        backend, "instrument", 100, 160, tag="FIC-1", type="dcs", location="primary"
    )
    result = await draw_line(
        backend,
        {"x": 100, "y": 100},
        {"handle": fic["handle"]},
        "electric",
        route_mode=[[130, 100], [130, 160]],
    )
    assert result["vertices"] == [[100.0, 100.0], [130.0, 100.0], [130.0, 160.0], [105.0, 160.0]]


async def test_dry_run_plans_the_same_bubble_exit_as_the_real_run(backend):
    from engineering.pid.spec import run_spec

    spec = {
        "instruments": [
            {
                "id": "FIC-1",
                "type": "dcs",
                "location": "primary",
                "x": 100,
                "y": 160,
                "tag": "FIC-1",
            }
        ],
        "valves": [
            {
                "id": "FCV-1",
                "symbol": "globe",
                "actuator": "diaphragm",
                "x": 100,
                "y": 100,
                "tag": "FCV-1",
            }
        ],
        "lines": [
            {
                "from": "FIC-1",
                "to": "FCV-1.signal",
                "class": "electric",
                "route": [[130, 160], [130, 109]],
            }
        ],
    }
    planned = await run_spec(backend, spec, dry_run=True)
    assert planned["lines"][0]["vertices"][0] == [105.0, 160.0]
    drawn = await run_spec(backend, spec)
    edge = drawn["graph"]["edges"][0]
    assert edge["line_number"] is None, "signal lines from a spec are unnumbered too"
