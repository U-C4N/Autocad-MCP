"""The last gaps the practice exams left: dim_scale, open joins, TTR arcs and region booleans."""

from __future__ import annotations

import importlib.util
import math
from types import SimpleNamespace

import pytest

from backends.capability import UnsupportedCapabilityError
from engineering.edge_chain import arc_edge, chain_path, line_edge, path_vertices
from engineering.measure import polygon_area_perimeter

pytestmark = pytest.mark.asyncio

needs_pywin32 = pytest.mark.skipif(
    importlib.util.find_spec("win32com") is None, reason="pywin32 not installed"
)


# ── drawing_apply_standard(dim_scale=) ───────────────────────────────────────


async def test_dim_scale_is_written_instead_of_one(backend):
    from engineering.standards.apply import apply_standard

    result = await apply_standard(backend, "iso", layers=False, dim_scale=0.2)
    assert float(await backend.system_get_variable("DIMSCALE")) == pytest.approx(0.2)
    assert result["settings"]["changed"]["DIMSCALE"][1] == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"dim_scale": 0}, ValueError),
        ({"dim_scale": -1.0}, ValueError),
        ({"dim_scale": "big"}, TypeError),
        ({"dim_scale": True}, TypeError),
        ({"dim_scale": 0.5, "units": False}, ValueError),
    ],
)
async def test_bad_dim_scale_is_refused_before_anything_is_written(backend, kwargs, error):
    from engineering.standards.apply import apply_standard

    before = len(await backend.dimstyle_list())
    with pytest.raises(error):
        await apply_standard(backend, "iso", layers=False, **kwargs)
    assert len(await backend.dimstyle_list()) == before


# ── boundary_from_entities(closed=False) ─────────────────────────────────────


def test_chain_path_orders_a_shuffled_open_run():
    edges = [
        line_edge((10, 0), (20, 0)),
        arc_edge(20, 5, 5, -math.pi / 2, math.pi / 2),
        line_edge((0, 0), (10, 0)),
    ]
    path = chain_path([edges[0], edges[1].reversed(), edges[2]])
    assert path is not None and len(path) == 3
    vertices = path_vertices(path)
    assert len(vertices) == 4
    _, length = polygon_area_perimeter(vertices, closed=False)
    assert length == pytest.approx(20 + math.pi * 5)


def test_chain_path_refuses_loops_branches_and_pieces():
    square = [
        line_edge((0, 0), (10, 0)),
        line_edge((10, 0), (10, 10)),
        line_edge((10, 10), (0, 10)),
        line_edge((0, 10), (0, 0)),
    ]
    assert chain_path(square) is None  # a loop has no free ends
    tee = [line_edge((0, 0), (10, 0)), line_edge((10, 0), (20, 0)), line_edge((10, 0), (10, 10))]
    assert chain_path(tee) is None  # a branch
    two = [line_edge((0, 0), (1, 0)), line_edge((5, 5), (6, 5))]
    assert chain_path(two) is None  # two pieces


async def test_open_join_makes_one_open_polyline_with_the_arc_as_a_bulge(backend):
    a = await backend.entity_create_line(0, 0, 10, 0)
    b = await backend.entity_create_arc(10, 5, 5, 270, 90)
    c = await backend.entity_create_line(10, 10, 0, 10)
    result = await backend.boundary_from_entities([c.handle, a.handle, b.handle], closed=False)
    assert result["ok"] and result["closed"] is False and result["vertices"] == 4
    assert result["length"] == pytest.approx(10 + math.pi * 5 + 10, abs=1e-5)
    pl = await backend.entity_get(result["handle"])
    assert pl.properties["closed"] is False
    assert sorted(round(x, 6) for x in pl.properties["bulges"]) == [0.0, 0.0, 0.0, 1.0] or any(
        abs(abs(x) - 1.0) < 1e-6 for x in pl.properties["bulges"]
    )


async def test_open_join_refuses_a_closed_loop_by_name(backend):
    pts = [(0, 0), (10, 0), (10, 10), (0, 10)]
    handles = [
        (await backend.entity_create_line(*pts[i], *pts[(i + 1) % 4])).handle for i in range(4)
    ]
    result = await backend.boundary_from_entities(handles, closed=False)
    assert result["ok"] is False and "closed loop" in result["error"]


# ── entity_create_circle_ttr(as_arc=) ────────────────────────────────────────


async def test_ttr_as_arc_draws_the_short_arc_between_the_tangent_points(backend):
    x = await backend.entity_create_line(0, 0, 50, 0)
    y = await backend.entity_create_line(0, 0, 0, 50)
    result = await backend.entity_create_circle_ttr(x.handle, y.handle, 4.0, 10, 10, as_arc=True)
    assert result["type"] == "ARC"
    arc = (await backend.entity_get(result["handle"])).properties
    assert (arc["end_angle"] - arc["start_angle"]) % 360 == pytest.approx(90.0)
    assert arc["center"] == pytest.approx([4.0, 4.0])


# ── region_boolean ───────────────────────────────────────────────────────────


async def test_region_boolean_is_refused_headlessly_with_its_capability(backend):
    a = await backend.entity_create_circle(0, 0, 10)
    b = await backend.entity_create_circle(5, 0, 3)
    with pytest.raises(UnsupportedCapabilityError) as info:
        await backend.region_boolean([a.handle, b.handle], "subtract")
    assert info.value.capability == "region_boolean"
    assert backend.capabilities().features["region_boolean"].supported is False


class _Region:
    def __init__(self, handle, area):
        self.Handle = handle
        self.Area = area
        self.calls = []
        self.deleted = False

    def Boolean(self, code, other):  # noqa: N802 - ActiveX spelling
        self.calls.append(code)
        self.Area -= other.Area

    def Delete(self):  # noqa: N802
        self.deleted = True


@needs_pywin32
async def test_com_region_boolean_subtracts_and_removes_the_sources(monkeypatch):
    from backends import com_backend
    from backends.com_backend import ComBackend

    sources = {"A": _Region("A", 314.0), "B": _Region("B", 28.0)}
    doc = SimpleNamespace(HandleToObject=lambda h: sources[h])

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    backend = ComBackend()
    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(com_backend, "_acad_doc", lambda: doc)
    monkeypatch.setattr(com_backend, "_regen", lambda: None)
    regions = {"A": _Region("RA", 314.0), "B": _Region("RB", 28.0)}
    monkeypatch.setattr(backend, "_region_from_profile", lambda d, h: regions[h])

    result = await backend.region_boolean(["A", "B"], "subtract")

    assert result["ok"] and result["operation"] == "subtract" and result["handle"] == "RA"
    assert result["area"] == pytest.approx(286.0) and result["sources_removed"] is True
    assert regions["A"].calls == [2]  # acSubtraction
    assert sources["A"].deleted and sources["B"].deleted


@needs_pywin32
async def test_com_region_boolean_validates_before_touching_the_drawing(monkeypatch):
    from backends import com_backend
    from backends.com_backend import ComBackend

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    backend = ComBackend()
    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(com_backend, "_acad_doc", lambda: pytest.fail("must not reach AutoCAD"))
    with pytest.raises(ValueError, match="Unknown boolean"):
        await backend.region_boolean(["A", "B"], "xor")
    with pytest.raises(ValueError, match="at least 2"):
        await backend.region_boolean(["A"], "union")


@needs_pywin32
async def test_com_open_join_accepts_a_polyline_source(monkeypatch):
    """Regression: a polyline source's own `closed` flag once shadowed the `closed` parameter."""
    from backends import com_backend
    from backends.com_backend import ComBackend

    class _Pl:
        ObjectName = "AcDbPolyline"
        Coordinates = (0.0, 0.0, 10.0, 0.0)
        Closed = False
        Length = 10.0

        def GetBulge(self, i):  # noqa: N802
            return 0.0

    line = SimpleNamespace(ObjectName="AcDbLine", StartPoint=(10, 0, 0), EndPoint=(10, 10, 0))
    objects = {"P": _Pl(), "L": line}
    made = []

    class _Space:
        def AddLightWeightPolyline(self, flat):  # noqa: N802
            pl = SimpleNamespace(
                Handle="N1", Closed=True, flat=list(flat), SetBulge=lambda i, b: None
            )
            made.append(pl)
            return pl

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    backend = ComBackend()
    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(
        com_backend, "_acad_doc", lambda: SimpleNamespace(HandleToObject=objects.get)
    )
    monkeypatch.setattr(com_backend, "_com_unnarrow", lambda o: o)
    monkeypatch.setattr(com_backend, "_msp", lambda: _Space())
    monkeypatch.setattr(com_backend, "_av", lambda v: list(v))
    monkeypatch.setattr(com_backend, "_regen", lambda: None)

    result = await backend.boundary_from_entities(["L", "P"], closed=False)
    assert result["ok"] and result["closed"] is False and result["vertices"] == 3
    assert result["length"] == pytest.approx(20.0)
    assert made[0].Closed is False
