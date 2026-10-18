"""engineering.edge_chain and the live engine's ``boundary_from_entities``.

The headless engine chains edges with ezdxf's edgeminer. ActiveX has no edge graph, so the
live engine reads each entity over COM and chains it with ``engineering.edge_chain``. These
tests pin the chain maths (order, direction, bulge sign, gaps) and the COM wiring with a
fake document, so neither needs AutoCAD.
"""

from __future__ import annotations

import importlib.util
import math
from types import SimpleNamespace

import pytest

from engineering.edge_chain import (
    arc_edge,
    chain_loop,
    dangling_endpoint,
    line_edge,
    loop_vertices,
    polyline_edges,
)
from engineering.measure import polygon_area_perimeter

needs_pywin32 = pytest.mark.skipif(
    importlib.util.find_spec("win32com") is None, reason="pywin32 not installed"
)


def _stadium():
    """Two 20-long lines closed by two semicircles of radius 5: area 200 + 25*pi."""
    return [
        line_edge((0, 0), (20, 0), "L1"),
        arc_edge(20, 5, 5, -math.pi / 2, math.pi / 2, "A1"),
        line_edge((20, 10), (0, 10), "L2"),
        arc_edge(0, 5, 5, math.pi / 2, 3 * math.pi / 2, "A2"),
    ]


def test_arc_edge_bulge_is_tan_quarter_sweep_and_positive_for_ccw():
    e = arc_edge(0, 0, 10, 0.0, math.pi / 2)
    assert e.bulge == pytest.approx(math.tan(math.pi / 8))
    assert e.start == pytest.approx((10, 0)) and e.end == pytest.approx((0, 10))
    assert e.reversed().bulge == pytest.approx(-e.bulge)


def test_chain_orders_and_orients_a_shuffled_loop():
    edges = _stadium()
    shuffled = [edges[2], edges[0].reversed(), edges[3], edges[1]]
    chain = chain_loop(shuffled)
    assert chain is not None and len(chain) == 4
    for a, b in zip(chain, chain[1:] + chain[:1], strict=True):
        assert math.dist(a.end, b.start) < 1e-9
    area, perimeter = polygon_area_perimeter(loop_vertices(chain), closed=True)
    assert abs(area) == pytest.approx(200 + 25 * math.pi, rel=1e-9)
    assert perimeter == pytest.approx(40 + 10 * math.pi, rel=1e-9)


def test_a_gap_is_refused_and_named():
    edges = [line_edge((0, 0), (10, 0)), line_edge((10, 0), (10, 10)), line_edge((10, 10), (0, 9))]
    assert chain_loop(edges) is None
    start, end = dangling_endpoint(edges)
    assert {tuple(round(v, 6) for v in start), tuple(round(v, 6) for v in end)} == {
        (0.0, 0.0),
        (0.0, 9.0),
    }


def test_all_edges_must_be_used():
    square = [
        line_edge((0, 0), (10, 0)),
        line_edge((10, 0), (10, 10)),
        line_edge((10, 10), (0, 10)),
        line_edge((0, 10), (0, 0)),
    ]
    stray = line_edge((50, 50), (60, 60))
    assert chain_loop(square + [stray]) is None


def test_a_spur_cannot_be_part_of_a_single_loop():
    edges = [
        line_edge((0, 0), (10, 0)),
        line_edge((10, 0), (20, 0)),  # spur off the corner (10, 0)
        line_edge((10, 0), (10, 10)),
        line_edge((10, 10), (0, 10)),
        line_edge((0, 10), (0, 0)),
    ]
    assert chain_loop(edges) is None
    assert chain_loop([e for e in edges if e.end != (20.0, 0.0)]) is not None


def test_polyline_edges_follow_closed_flag_and_bulges():
    coords = [0, 0, 10, 0, 10, 10]
    open_edges = polyline_edges(coords, [0.0, 0.5, 0.0], closed=False)
    closed_edges = polyline_edges(coords, [0.0, 0.5, 0.0], closed=True)
    assert len(open_edges) == 2 and len(closed_edges) == 3
    assert open_edges[1].bulge == 0.5 and closed_edges[2].end == (0.0, 0.0)


class _FakeEntity:
    def __init__(self, name, **members):
        self.ObjectName = name
        self.__dict__.update(members)


class _FakePolyline:
    def __init__(self, handle="F1"):
        self.Handle = handle
        self.Closed = False
        self.bulges = {}

    def SetBulge(self, i, b):  # noqa: N802 - ActiveX spelling
        self.bulges[i] = b


def _patched_backend(monkeypatch, objects, space=None):
    from backends import com_backend
    from backends.com_backend import ComBackend

    doc = SimpleNamespace(HandleToObject=lambda h: objects[h])

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    backend = ComBackend()
    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(com_backend, "_acad_doc", lambda: doc)
    monkeypatch.setattr(com_backend, "_com_unnarrow", lambda obj: obj)
    if space is not None:
        monkeypatch.setattr(com_backend, "_msp", lambda: space)
        monkeypatch.setattr(com_backend, "_av", lambda values: list(values))
        monkeypatch.setattr(com_backend, "_regen", lambda: None)
    return backend


@needs_pywin32
@pytest.mark.asyncio
async def test_com_boundary_from_entities_stores_one_closed_bulged_polyline(monkeypatch):
    objects = {
        "A": _FakeEntity("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(20, 0, 0)),
        "B": _FakeEntity(
            "AcDbArc", Center=(20, 5, 0), Radius=5.0, StartAngle=-math.pi / 2, EndAngle=math.pi / 2
        ),
        "C": _FakeEntity("AcDbLine", StartPoint=(20, 10, 0), EndPoint=(0, 10, 0)),
        "D": _FakeEntity(
            "AcDbArc",
            Center=(0, 5, 0),
            Radius=5.0,
            StartAngle=math.pi / 2,
            EndAngle=3 * math.pi / 2,
        ),
    }
    created: list[_FakePolyline] = []

    class _Space:
        def AddLightWeightPolyline(self, flat):  # noqa: N802
            pl = _FakePolyline()
            pl.flat = list(flat)
            created.append(pl)
            return pl

    backend = _patched_backend(monkeypatch, objects, _Space())
    result = await backend.boundary_from_entities(["D", "A", "C", "B"])

    assert result["ok"] is True and result["closed"] is True and result["vertices"] == 4
    assert result["area"] == pytest.approx(200 + 25 * math.pi, abs=1e-5)
    (pl,) = created
    assert pl.Closed is True
    assert sorted(round(b, 6) for b in pl.bulges.values()) == [
        1.0,
        1.0,
    ]  # two semicircles: tan(pi/4)


@needs_pywin32
@pytest.mark.asyncio
async def test_com_boundary_from_entities_names_the_gap(monkeypatch):
    objects = {
        "A": _FakeEntity("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(10, 0, 0)),
        "B": _FakeEntity("AcDbLine", StartPoint=(10, 0, 0), EndPoint=(10, 10, 0)),
        "C": _FakeEntity("AcDbLine", StartPoint=(10, 10, 0), EndPoint=(0, 9, 0)),
    }
    backend = _patched_backend(monkeypatch, objects)
    result = await backend.boundary_from_entities(["A", "B", "C"])
    assert result["ok"] is False and "do not close" in result["error"]


@needs_pywin32
@pytest.mark.asyncio
async def test_com_boundary_from_entities_refuses_a_text(monkeypatch):
    backend = _patched_backend(
        monkeypatch, {"A": _FakeEntity("AcDbText"), "B": _FakeEntity("AcDbText")}
    )
    result = await backend.boundary_from_entities(["A", "B"])
    assert result["ok"] is False and "TEXT" in result["error"]
