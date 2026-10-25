"""entity_get returns HATCH loops and full DIMENSION fields on both engines (B16).

The gate: a HATCH's loop area equals analysis_measure_entity's area for the
same handle (islands, nesting, bulges). Triage rows W3-T13-09 (COM heavy 2D
polyline stride) and W3-T14-19 (a tilted INSERT read as mirrored) are fixed
here, each with its own test.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import ezdxf
import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends.com_backend import ComBackend, _entity_info
from backends.ezdxf_backend import EzdxfBackend
from engineering.understand.snapshot import records_from_doc


def _square(x0, y0, side):
    return [(x0, y0), (x0 + side, y0), (x0 + side, y0 + side), (x0, y0 + side)]


def _nested_hatch(backend, style):
    hatch = backend._doc.modelspace().add_hatch()
    hatch.dxf.hatch_style = style
    hatch.paths.add_polyline_path(_square(0, 0, 20), is_closed=True, flags=1)
    hatch.paths.add_polyline_path(_square(4, 4, 12), is_closed=True, flags=0)
    hatch.paths.add_polyline_path(_square(8, 8, 4), is_closed=True, flags=0)
    return hatch.dxf.handle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("style", "name", "area"),
    [(0, "normal", 400.0 - 144.0 + 16.0), (1, "outer", 400.0 - 144.0), (2, "ignore", 400.0)],
)
async def test_hatch_loop_area_equals_measure_entity_with_islands_and_nesting(
    backend, style, name, area
):
    handle = _nested_hatch(backend, style)
    info = await backend.entity_get(handle)
    measured = await backend.entity_measure(handle)
    props = info.properties
    assert props["hatch_style"] == name
    assert [loop["depth"] for loop in props["loops"]] == [0, 1, 2]
    assert props["area"] == pytest.approx(area, abs=1e-9)
    assert props["area"] == pytest.approx(measured["area"], abs=1e-9)
    assert props["area_exact"] is True and measured["exact"] is True


@pytest.mark.asyncio
async def test_a_bulged_hatch_loop_is_exact_where_measure_states_its_flattening(backend):
    hatch = backend._doc.modelspace().add_hatch()
    hatch.paths.add_polyline_path(
        [(0, 0, 0), (20, 0, 1.0), (20, 10, 0), (0, 10, 0)], is_closed=True, flags=1
    )
    hatch.paths.add_edge_path(flags=16).add_arc((5, 5), 2, 0, 360)
    info = await backend.entity_get(hatch.dxf.handle)
    measured = await backend.entity_measure(hatch.dxf.handle)
    expected = 200.0 + 12.5 * math.pi - 4.0 * math.pi
    assert info.properties["area"] == pytest.approx(expected, abs=1e-9)
    assert info.properties["area_exact"] is True
    # analysis_measure_entity flattens Bezier boundaries and says so: it agrees
    # within the ~0.028 % Bezier error it documents, never better.
    assert measured["exact"] is False
    assert measured["area"] == pytest.approx(expected, rel=3e-4)


@pytest.mark.asyncio
async def test_entity_get_reports_every_dimension_field(backend):
    msp = backend._doc.modelspace()
    dim = msp.add_linear_dim(
        base=(0, 5), p1=(0, 0), p2=(42, 0), dimstyle="Standard", override={"dimlfac": 2.0}
    )
    dim.render()
    dim.dimension.dxf.text = "40"
    info = await backend.entity_get(dim.dimension.dxf.handle)
    assert info.properties["dimension"] == {
        "dimtype": "linear",
        "dimtype_code": 0,
        "defpoints": [[0.0, 5.0], [0.0, 0.0], [42.0, 0.0], None, None],
        "measurement": 42.0,
        "text_override": "40",
        "dimstyle": "Standard",
        "dimlfac": 2.0,
        "angle": 0.0,
    }


@pytest.mark.asyncio
async def test_entity_list_rows_stay_light(backend):
    _nested_hatch(backend, 0)
    (row,) = await backend.entity_list(type_filter="HATCH")
    assert "loops" not in row.properties


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


@pytest.mark.asyncio
async def test_the_entity_get_tool_carries_the_loops_over_the_wire(client):
    made = await client.call_tool(
        "entity_create_hatch",
        {"pattern": "ANSI31", "boundary_points": [[0, 0], [10, 0], [10, 10], [0, 10]]},
    )
    handle = made.structured_content["handle"]
    got = (await client.call_tool("entity_get", {"handle": handle})).structured_content
    assert got["properties"]["loop_count"] == 1
    assert got["properties"]["area"] == pytest.approx(100.0, abs=1e-9)


# -- triage W3-T14-19: a tilted INSERT with a negative-z normal is not "mirrored" --


@pytest.mark.asyncio
async def test_ezdxf_insert_is_mirrored_only_in_a_flat_frame(backend):
    msp = backend._doc.modelspace()
    backend._doc.blocks.new("B").add_line((0, 0), (1, 0))
    flat = msp.add_blockref("B", (0, 0))
    flat.dxf.extrusion = (0.0, 0.0, -1.0)
    tilted = msp.add_blockref("B", (5, 0))
    tilted.dxf.extrusion = (0.5, 0.0, -math.sqrt(3.0) / 2.0)
    flat_props = (await backend.entity_get(flat.dxf.handle)).properties
    tilted_props = (await backend.entity_get(tilted.dxf.handle)).properties
    assert flat_props["mirrored"] is True
    assert tilted_props["mirrored"] is False
    assert tilted_props["plane_normal"] == pytest.approx([0.5, 0.0, -math.sqrt(3.0) / 2.0])


class _Fake:
    """An ActiveX entity that has only the members it is given."""

    def __init__(self, object_name, **members):
        self.ObjectName = object_name
        for name, value in members.items():
            setattr(self, name, value)

    def __getattr__(self, name):
        raise AttributeError(name)


def _com_common(handle="2A"):
    return {
        "Handle": handle,
        "Layer": "0",
        "color": 256,
        "Linetype": "ByLayer",
        "Visible": True,
        "GetBoundingBox": lambda: ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
    }


def test_com_insert_is_mirrored_only_in_a_flat_frame():
    class _NoBlocks:
        def Item(self, name):  # the definition is not read: no geometry_bbox
            raise KeyError(name)

    no_blocks = SimpleNamespace(Blocks=_NoBlocks())

    def insert(normal):
        return _Fake(
            "AcDbBlockReference",
            Name="B",
            Document=no_blocks,
            InsertionPoint=(0.0, 0.0, 0.0),
            XScaleFactor=1.0,
            YScaleFactor=1.0,
            ZScaleFactor=1.0,
            Rotation=0.0,
            Normal=normal,
            **_com_common(),
        )

    flat = _entity_info(insert((0.0, 0.0, -1.0))).properties
    tilted = _entity_info(insert((0.5, 0.0, -math.sqrt(3.0) / 2.0))).properties
    assert flat["mirrored"] is True
    assert tilted["mirrored"] is False
    assert tilted["plane_normal"] == pytest.approx([0.5, 0.0, -math.sqrt(3.0) / 2.0])


# -- triage W3-T13-09: a heavy 2D polyline's Coordinates are (x, y, z) triples --


def test_com_heavy_2d_polyline_is_read_with_stride_three():
    polyline = _Fake(
        "AcDb2dPolyline",
        Coordinates=(0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 10.0, 5.0, 0.0),
        Closed=False,
        Length=15.0,  # 10 + 5: the chord walk, so no bulge is read
        Normal=(0.0, 0.0, 1.0),
        **_com_common(),
    )
    props = _entity_info(polyline).properties
    assert props["points"] == [[0.0, 0.0], [10.0, 0.0], [10.0, 5.0]]
    assert props["bulges"] == [0.0, 0.0, 0.0]


# -- COM entity_get: the loops and the dimension come from the measured export --


def _com_backend_over(monkeypatch, entity, snapshot):
    import backends.com_backend as cb
    import engineering.understand.snapshot as snapshot_module

    backend = ComBackend()

    async def _run(func, *args, **kwargs):  # bypass the STA executor
        return func(*args, **kwargs)

    async def _take(_backend, path=None, *, block_defs=False):
        return snapshot

    backend._run = _run
    doc = SimpleNamespace(HandleToObject=lambda handle: entity)
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(snapshot_module, "take_snapshot", _take)
    return backend


@pytest.mark.asyncio
async def test_com_entity_get_reads_hatch_loops_from_the_export(monkeypatch):
    doc = ezdxf.new()
    hatch = doc.modelspace().add_hatch()
    hatch.paths.add_polyline_path(
        [(0, 0, 0), (20, 0, 1.0), (20, 10, 0), (0, 10, 0)], is_closed=True, flags=1
    )
    hatch.paths.add_edge_path(flags=16).add_arc((5, 5), 2, 0, 360)
    snapshot = records_from_doc(doc, source="live:Drawing9.dwg")
    fake = _Fake("AcDbHatch", **_com_common(hatch.dxf.handle))
    backend = _com_backend_over(monkeypatch, fake, snapshot)
    info = await backend.entity_get(hatch.dxf.handle)
    assert info.properties["detail_source"] == "dxf_export"
    assert info.properties["loop_count"] == 2
    assert info.properties["area"] == pytest.approx(
        200.0 + 12.5 * math.pi - 4.0 * math.pi, abs=1e-9
    )


@pytest.mark.asyncio
async def test_com_entity_get_reads_dimension_fields_from_the_export(monkeypatch):
    doc = ezdxf.new()
    dim = doc.modelspace().add_linear_dim(
        base=(0, 5), p1=(0, 0), p2=(42, 0), dimstyle="Standard", override={"dimlfac": 2.0}
    )
    dim.render()
    handle = dim.dimension.dxf.handle
    snapshot = records_from_doc(doc, source="live:Drawing9.dwg")
    fake = _Fake("AcDbRotatedDimension", TextPosition=(21.0, 5.0, 0.0), **_com_common(handle))
    backend = _com_backend_over(monkeypatch, fake, snapshot)
    props = (await backend.entity_get(handle)).properties
    assert props["dimension"]["measurement"] == 42.0
    assert props["dimension"]["dimlfac"] == 2.0
    assert props["dimension"]["defpoints"][2] == [42.0, 0.0]


@pytest.mark.asyncio
async def test_com_entity_get_says_when_the_export_does_not_hold_the_entity(monkeypatch):
    snapshot = records_from_doc(ezdxf.new(), source="live:Drawing9.dwg")
    fake = _Fake("AcDbHatch", **_com_common("77"))
    backend = _com_backend_over(monkeypatch, fake, snapshot)
    props = (await backend.entity_get("77")).properties
    assert "loops" not in props
    assert "block definition" in props["detail_unavailable"]


@pytest.mark.asyncio
async def test_com_entity_get_never_reports_an_unread_hatch_as_empty(monkeypatch):
    # A record whose loops could not be read carries none; that is "unknown",
    # never "a hatch that fills 0".
    from engineering.understand.snapshot import EntityRecord, Snapshot

    record = EntityRecord(handle="5C", type="HATCH", layer="0", space="Model")
    snapshot = Snapshot(
        source="live:Drawing9.dwg",
        insunits=4,
        extmin=None,
        extmax=None,
        layers={},
        layouts=("Model",),
        records=(record,),
    )
    fake = _Fake("AcDbHatch", **_com_common("5C"))
    backend = _com_backend_over(monkeypatch, fake, snapshot)
    props = (await backend.entity_get("5C")).properties
    assert "area" not in props and "loops" not in props
    assert "could not be read" in props["detail_unavailable"]


# -- the capability flips in both maps -------------------------------------------


def test_hatch_loops_are_declared_on_both_engines():
    for backend in (EzdxfBackend(), ComBackend()):
        features = backend.capabilities().features
        assert features["hatch_loops"].supported is True
        assert "hatch_boundaries_not_reported" not in (features["ocs_normalized"].reason or "")
