"""In-place edit primitives (v1.8.0, plan item 1.8-06).

A polyline's vertices, a label's position and a polyline's arcs change without
the entity being deleted and redrawn: the handle, the layer and the XDATA a
generator wrote stay where they were. That is the whole point - a revision that
deletes and redraws loses every reference anybody kept to the old handle.

Headless tests run on the real engine; the live engine is covered by a fake
COM polyline that models the three answers AutoCAD might give to a
``Coordinates`` write with a different vertex count (S4, unmeasured): accept
it, refuse it, or ignore it. The last two are decided at runtime and fall back
to delete + recreate, reported as ``replaced``.
"""

from __future__ import annotations

import importlib.util
import math
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends.base import UnsupportedCapabilityError

pytestmark = pytest.mark.asyncio

needs_pywin32 = pytest.mark.skipif(
    importlib.util.find_spec("win32com") is None, reason="pywin32 not installed"
)

FOUR = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]
SIX = [[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [20.0, 30.0], [0.0, 20.0], [0.0, 10.0]]
SIX_BULGES = [1.0, 0.0, 0.5, 0.0, 0.0, 0.0]
TAG = {"ACADMCP_MECH": ['{"feature":"","kind":"feature","part":"P","v":1,"view":"front"}']}


def _arc_length(p1, p2, bulge: float) -> float:
    chord = math.dist(p1, p2)
    if bulge == 0.0:
        return chord
    sweep = 4.0 * math.atan(abs(bulge))
    radius = chord / (2.0 * math.sin(sweep / 2.0))
    return radius * sweep


def _perimeter(points, bulges, closed=True) -> float:
    pairs = list(zip(points, points[1:] + (points[:1] if closed else []), strict=False))
    return sum(_arc_length(a, b, bulges[i]) for i, (a, b) in enumerate(pairs))


def _close(a, b, tol=1e-9) -> bool:
    return all(math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b, strict=True))


# ── headless: entity_set_vertices ────────────────────────────────────────────


async def test_four_to_six_vertices_with_two_bulges_keeps_handle_layer_and_xdata(backend):
    pline = await backend.entity_create_polyline(FOUR, True, layer="GEOMETRY", xdata=TAG)
    updated = await backend.entity_set_vertices(pline.handle, SIX, SIX_BULGES)

    assert updated.handle == pline.handle
    assert updated.layer == "GEOMETRY"
    assert [list(p) for p in updated.properties["points"]] == SIX
    assert updated.properties["bulges"] == SIX_BULGES
    assert updated.properties["closed"] is True
    xdata = await backend.entity_get_xdata(pline.handle, "ACADMCP_MECH")
    assert xdata["xdata"]["ACADMCP_MECH"] == TAG["ACADMCP_MECH"]

    measured = await backend.entity_measure(pline.handle)
    assert math.isclose(measured["perimeter"], _perimeter(SIX, SIX_BULGES), rel_tol=1e-9)
    assert measured["exact"] is True
    got = await backend.entity_get(pline.handle)
    assert math.isclose(got.properties["length"], _perimeter(SIX, SIX_BULGES), rel_tol=1e-6)


async def test_closed_is_applied_only_when_given(backend):
    pline = await backend.entity_create_polyline(FOUR, True)
    kept = await backend.entity_set_vertices(pline.handle, FOUR[:3])
    assert kept.properties["closed"] is True
    opened = await backend.entity_set_vertices(pline.handle, FOUR, closed=False)
    assert opened.properties["closed"] is False


async def test_omitted_bulges_keep_the_arcs_when_the_count_is_unchanged(backend):
    pline = await backend.entity_create_polyline(SIX, True, bulges=SIX_BULGES)
    moved = [[x + 5.0, y] for x, y in SIX]
    same = await backend.entity_set_vertices(pline.handle, moved)
    assert same.properties["bulges"] == SIX_BULGES
    fewer = await backend.entity_set_vertices(pline.handle, FOUR)
    assert fewer.properties["bulges"] == [0.0] * 4


async def test_a_mirrored_polyline_takes_wcs_points_and_wcs_bulges(backend):
    pline = await backend.entity_create_polyline(FOUR, True)
    entity = backend._doc.entitydb[pline.handle]
    entity.dxf.extrusion = (0.0, 0.0, -1.0)

    updated = await backend.entity_set_vertices(pline.handle, SIX, SIX_BULGES)
    assert all(
        _close(got, want) for got, want in zip(updated.properties["points"], SIX, strict=True)
    )
    assert updated.properties["bulges"] == SIX_BULGES
    stored = [(x, y, b) for x, y, *_rest, b in entity.get_points("xyseb")]
    assert math.isclose(stored[1][0], -40.0)  # the frame stores x unnegated: -WCS x
    assert stored[0][2] == -1.0  # a reflection reverses the stored arc's sense


async def test_a_tilted_polyline_is_refused_and_left_alone(backend):
    pline = await backend.entity_create_polyline(FOUR, True)
    entity = backend._doc.entitydb[pline.handle]
    entity.dxf.extrusion = (0.0, 1.0, 1.0)
    before = [tuple(p) for p in entity.get_points("xyb")]
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.entity_set_vertices(pline.handle, SIX)
    assert excinfo.value.capability == "ocs_tilted_plane"
    assert [tuple(p) for p in entity.get_points("xyb")] == before


async def test_set_vertices_refuses_what_is_not_a_lightweight_polyline(backend):
    circle = await backend.entity_create_circle(0.0, 0.0, 5.0)
    with pytest.raises(RuntimeError, match="LWPOLYLINE"):
        await backend.entity_set_vertices(circle.handle, FOUR)


@pytest.mark.parametrize(
    ("points", "bulges", "match"),
    [
        ([[0.0, 0.0]], None, "at least 2"),
        (FOUR, [0.0, 1.0], "one per vertex"),
        ([[0.0, "a"], [1.0, 1.0]], None, "expected a number"),
        (FOUR, [0.0, 0.0, float("nan"), 0.0], "finite"),
    ],
)
async def test_bad_vertex_lists_are_refused_before_anything_changes(backend, points, bulges, match):
    pline = await backend.entity_create_polyline(FOUR, True)
    with pytest.raises(ValueError, match=match):
        await backend.entity_set_vertices(pline.handle, points, bulges)
    got = await backend.entity_get(pline.handle)
    assert [list(p) for p in got.properties["points"]] == FOUR


async def test_create_polyline_takes_bulges(backend):
    pline = await backend.entity_create_polyline(SIX, True, layer="GEOMETRY", bulges=SIX_BULGES)
    assert pline.properties["bulges"] == SIX_BULGES
    measured = await backend.entity_measure(pline.handle)
    assert math.isclose(measured["perimeter"], _perimeter(SIX, SIX_BULGES), rel_tol=1e-9)


# ── headless: entity_edit_geometry / entity_edit_text gain positions ─────────


async def test_edit_geometry_rewrites_a_polyline_in_place(backend):
    pline = await backend.entity_create_polyline(FOUR, True, xdata=TAG)
    updated = await backend.entity_edit_geometry(pline.handle, points=SIX, bulges=SIX_BULGES)
    assert updated.handle == pline.handle
    assert updated.properties["bulges"] == SIX_BULGES
    only_bulges = await backend.entity_edit_geometry(pline.handle, bulges=[0.0] * 6)
    assert [list(p) for p in only_bulges.properties["points"]] == SIX
    assert only_bulges.properties["bulges"] == [0.0] * 6


async def test_edit_geometry_refuses_mixed_vocabularies(backend):
    pline = await backend.entity_create_polyline(FOUR, True)
    circle = await backend.entity_create_circle(0.0, 0.0, 5.0)
    with pytest.raises(RuntimeError, match="points"):
        await backend.entity_edit_geometry(pline.handle, cx=1.0)
    with pytest.raises(RuntimeError, match="LWPOLYLINE"):
        await backend.entity_edit_geometry(circle.handle, points=FOUR)
    with pytest.raises(RuntimeError, match="TEXT"):
        await backend.entity_edit_geometry(circle.handle, x=1.0)


async def test_text_moves_keep_the_handle(backend):
    text = await backend.entity_create_text("A", 1.0, 2.0, xdata=TAG)
    moved = await backend.entity_edit_text(text.handle, x=50.0, y=60.0)
    assert moved.handle == text.handle
    assert _close(moved.properties["insertion"], [50.0, 60.0])
    again = await backend.entity_edit_geometry(text.handle, y=70.0)
    assert again.handle == text.handle
    assert _close(again.properties["insertion"], [50.0, 70.0])
    assert (await backend.entity_get_xdata(text.handle, "ACADMCP_MECH"))["xdata"]


async def test_an_aligned_text_moves_with_its_alignment_point(backend):
    text = await backend.entity_create_text("A", 0.0, 0.0)
    entity = backend._doc.entitydb[text.handle]
    entity.dxf.halign = 1
    entity.dxf.align_point = (10.0, 0.0, 0.0)
    await backend.entity_edit_text(text.handle, x=100.0, y=5.0)
    assert _close(tuple(entity.dxf.insert)[:2], (100.0, 5.0))
    assert _close(tuple(entity.dxf.align_point)[:2], (110.0, 5.0))


async def test_mtext_moves_keep_the_handle(backend):
    mtext = await backend.entity_create_mtext("note", 0.0, 0.0)
    moved = await backend.entity_edit_text(mtext.handle, x=-5.0, y=12.5, text="moved")
    assert moved.handle == mtext.handle
    assert _close(moved.properties["insertion"], [-5.0, 12.5])
    assert "moved" in moved.properties["text"]


async def test_a_tilted_text_position_is_refused(backend):
    text = await backend.entity_create_text("A", 0.0, 0.0)
    backend._doc.entitydb[text.handle].dxf.extrusion = (1.0, 0.0, 1.0)
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.entity_edit_text(text.handle, x=5.0)
    assert excinfo.value.capability == "ocs_tilted_plane"


# ── headless: xdata= on the dimension and table contracts ────────────────────


async def test_the_four_dimension_contracts_write_xdata_on_the_dimension(backend):
    made = [
        await backend.dimension_linear(0, 0, 10, 0, 5, 5, xdata=TAG),
        await backend.dimension_aligned(0, 0, 10, 10, 0, 5, xdata=TAG),
        await backend.dimension_radius(0, 0, 10, 0, xdata=TAG),
        await backend.dimension_diameter(-10, 0, 10, 0, xdata=TAG),
    ]
    for info in made:
        entity = backend._doc.entitydb[info.handle]
        assert entity.dxftype() == "DIMENSION"  # the dimension, never its *D block
        assert (await backend.entity_get_xdata(info.handle, "ACADMCP_MECH"))["xdata"] == TAG


async def test_the_table_contract_writes_xdata_on_every_child(backend):
    table = await backend.entity_create_table(0, 0, [["A", "1"]], ["K", "V"], xdata=TAG)
    for handle in table.properties["child_handles"]:
        assert (await backend.entity_get_xdata(handle, "ACADMCP_MECH"))["xdata"] == TAG


async def test_a_bad_xdata_tag_creates_no_dimension(backend):
    before = await backend.entity_count()
    with pytest.raises(ValueError):
        await backend.dimension_linear(0, 0, 10, 0, 5, 5, xdata={"ACADMCP_MECH": []})
    assert await backend.entity_count() == before


# ── the MCP tools carry the new parameters ───────────────────────────────────


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf"
        await connected.call_tool("drawing_new", {})
        yield connected


async def test_the_tools_edit_in_place(client):
    created = (
        await client.call_tool(
            "entity_create_polyline", {"points": FOUR, "closed": True, "bulges": [0.0, 1.0, 0, 0]}
        )
    ).structured_content
    assert created["properties"]["bulges"] == [0.0, 1.0, 0.0, 0.0]
    edited = (
        await client.call_tool(
            "entity_edit_geometry",
            {"handle": created["handle"], "points": SIX, "bulges": SIX_BULGES},
        )
    ).structured_content
    assert edited["handle"] == created["handle"]
    assert edited["properties"]["bulges"] == SIX_BULGES

    text = (
        await client.call_tool("entity_create_text", {"text": "A", "x": 0, "y": 0})
    ).structured_content
    moved = (
        await client.call_tool("entity_edit_text", {"handle": text["handle"], "x": 9, "y": 8})
    ).structured_content
    assert moved["handle"] == text["handle"]
    assert _close(moved["properties"]["insertion"], [9.0, 8.0])


# ── live engine, fake COM ────────────────────────────────────────────────────


class _Log(list):
    pass


class _FakePline:
    """An AcDbPolyline as ActiveX shows it: OCS Coordinates as flat (x, y) pairs.

    ``on_count_change`` models S4: ``"accept"`` resizes, ``"raise"`` answers
    E_INVALIDARG, ``"ignore"`` keeps the old vertices without a word.
    """

    ObjectName = "AcDbPolyline"
    OwnerID = 7

    def __init__(self, log, coords, *, handle="2B", on_count_change="accept", normal=None):
        self._log = log
        self._coords = [float(v) for v in coords]
        self._bulges = [0.0] * (len(self._coords) // 2)
        self._on_count_change = on_count_change
        self.Handle = handle
        self.Closed = True
        self.Normal = normal or (0.0, 0.0, 1.0)
        self.Elevation = 0.0
        self.Layer = "GEOMETRY"
        self.color = 3
        self.Linetype = "HIDDEN"
        self.LinetypeScale = 2.0
        self.Lineweight = 35
        self.Visible = True
        self._xdata = ((1001, 1000), ("ACADMCP_MECH", "payload"))

    @property
    def Coordinates(self):  # noqa: N802 - the ActiveX spelling
        return tuple(self._coords)

    @Coordinates.setter
    def Coordinates(self, value):  # noqa: N802
        values = [float(v) for v in getattr(value, "value", value)]
        self._log.append(("Coordinates", self.Handle, len(values) // 2))
        if len(values) != len(self._coords):
            if self._on_count_change == "raise":
                raise RuntimeError("E_INVALIDARG")
            if self._on_count_change == "ignore":
                return
        self._coords = values
        self._bulges = (self._bulges + [0.0] * len(values))[: len(values) // 2]

    @property
    def Length(self):  # noqa: N802
        from engineering import measure

        verts = [
            (self._coords[i], self._coords[i + 1], self._bulges[i // 2])
            for i in range(0, len(self._coords), 2)
        ]
        return measure.polygon_area_perimeter(verts, bool(self.Closed))[1]

    def GetBulge(self, index):  # noqa: N802
        return self._bulges[index]

    def SetBulge(self, index, value):  # noqa: N802
        self._log.append(("SetBulge", self.Handle, index, float(value)))
        self._bulges[index] = float(value)

    def GetBoundingBox(self):  # noqa: N802
        xs, ys = self._coords[0::2], self._coords[1::2]
        return (min(xs), min(ys), 0.0), (max(xs), max(ys), 0.0)

    def GetXData(self, app):  # noqa: N802
        self._log.append(("GetXData", self.Handle, app))
        return self._xdata

    def SetXData(self, codes, values):  # noqa: N802
        self._xdata = (
            tuple(getattr(codes, "value", codes)),
            tuple(getattr(values, "value", values)),
        )
        self._log.append(("SetXData", self.Handle))

    def Delete(self):  # noqa: N802
        self._log.append(("Delete", self.Handle))


class _FakeSpace:
    def __init__(self, log):
        self._log = log
        self.made: list[_FakePline] = []

    def AddLightWeightPolyline(self, flat):  # noqa: N802
        values = list(getattr(flat, "value", flat))
        self._log.append(("AddLightWeightPolyline", len(values) // 2))
        made = _FakePline(self._log, values, handle=f"N{len(self.made)}")
        made.Closed = False
        made.Layer = "0"
        made.color = 256
        made.Linetype = "ByLayer"
        made.LinetypeScale = 1.0
        made.Lineweight = -1
        made._xdata = ((), ())
        self.made.append(made)
        return made


class _FakeText:
    def __init__(self, log, name="AcDbText", normal=None):
        self._log = log
        self.ObjectName = name
        self.Handle = "3C"
        self.InsertionPoint = (1.0, 2.0, 0.0)
        self.Normal = normal or (0.0, 0.0, 1.0)
        self.TextString = "A"
        self.Contents = "A"
        self.Height = 2.5
        self.Rotation = 0.0
        self.Layer = "TEXT"
        self.color = 256
        self.Linetype = "ByLayer"
        self.Visible = True

    def Move(self, a, b):  # noqa: N802
        a = list(getattr(a, "value", a))
        b = list(getattr(b, "value", b))
        delta = [b[i] - a[i] for i in range(3)]
        self._log.append(("Move", self.Handle, tuple(delta)))
        x, y, z = self.InsertionPoint
        self.InsertionPoint = (x + delta[0], y + delta[1], z + delta[2])


@pytest.fixture
def seat(monkeypatch):
    import backends.com_backend as cb

    log = _Log()
    space = _FakeSpace(log)
    objects: dict = {}
    doc = SimpleNamespace(
        HandleToObject=lambda h: objects[h],
        ObjectIdToObject=lambda oid: space,
        RegisteredApplications=SimpleNamespace(Add=lambda name: log.append(("RegApp", name))),
    )
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(cb, "_msp", lambda: space)
    monkeypatch.setattr(cb, "_regen", lambda *_entity: None)
    backend = cb.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return SimpleNamespace(backend=backend, log=log, space=space, objects=objects)


def _flat(points):
    return [c for p in points for c in p]


@needs_pywin32
async def test_com_same_count_writes_coordinates_bulges_and_closed_in_place(seat):
    pline = _FakePline(seat.log, _flat(FOUR))
    seat.objects["2B"] = pline
    moved = [[x + 1.0, y] for x, y in FOUR]
    info = await seat.backend.entity_set_vertices("2B", moved, [0.0, 0.5, 0.0, 0.0], closed=False)
    assert info.handle == "2B"
    assert "replaced" not in info.properties
    assert pline._coords == _flat(moved)
    assert pline._bulges == [0.0, 0.5, 0.0, 0.0]
    assert pline.Closed is False
    assert not [e for e in seat.log if e[0] in ("Delete", "AddLightWeightPolyline")]


@needs_pywin32
async def test_com_a_changed_count_that_activex_accepts_keeps_the_handle(seat):
    pline = _FakePline(seat.log, _flat(FOUR), on_count_change="accept")
    seat.objects["2B"] = pline
    info = await seat.backend.entity_set_vertices("2B", SIX, SIX_BULGES)
    assert info.handle == "2B" and "replaced" not in info.properties
    assert info.properties["bulges"] == SIX_BULGES
    assert math.isclose(info.properties["length"], _perimeter(SIX, SIX_BULGES), rel_tol=1e-9)


@needs_pywin32
@pytest.mark.parametrize("answer", ["raise", "ignore"])
async def test_com_a_changed_count_activex_will_not_take_is_replaced_and_reported(seat, answer):
    pline = _FakePline(seat.log, _flat(FOUR), on_count_change=answer)
    seat.objects["2B"] = pline
    info = await seat.backend.entity_set_vertices("2B", SIX, SIX_BULGES)

    new = seat.space.made[0]
    assert info.handle == new.Handle
    assert info.properties["replaced"] == [{"old": "2B", "new": new.Handle}]
    assert ("Delete", "2B") in seat.log
    assert new._coords == _flat(SIX) and new._bulges == SIX_BULGES
    assert new.Closed is True  # the old polyline's, since closed was not given
    assert (new.Layer, new.color, new.Linetype, new.Lineweight) == ("GEOMETRY", 3, "HIDDEN", 35)
    assert new.LinetypeScale == 2.0
    assert new._xdata == ((1001, 1000), ("ACADMCP_MECH", "payload"))
    assert "not_copied" not in info.properties["replaced"][0]


class _LockedPline(_FakePline):
    """A polyline whose every Coordinates write fails (a locked layer, say)."""

    @property
    def Coordinates(self):  # noqa: N802
        return tuple(self._coords)

    @Coordinates.setter
    def Coordinates(self, value):  # noqa: N802
        raise RuntimeError("E_FAIL")


@needs_pywin32
async def test_com_a_same_count_failure_is_raised_not_replaced(seat):
    seat.objects["2B"] = _LockedPline(seat.log, _flat(FOUR))
    with pytest.raises(RuntimeError, match="E_FAIL"):
        await seat.backend.entity_set_vertices("2B", [[x, y + 1] for x, y in FOUR])
    assert not seat.space.made
    assert ("Delete", "2B") not in seat.log


@needs_pywin32
async def test_com_a_tilted_polyline_is_refused_before_any_write(seat):
    pline = _FakePline(seat.log, _flat(FOUR), normal=(0.0, 0.7071, 0.7071))
    seat.objects["2B"] = pline
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await seat.backend.entity_set_vertices("2B", SIX)
    assert excinfo.value.capability == "ocs_tilted_plane"
    assert not [e for e in seat.log if e[0] == "Coordinates"]


@needs_pywin32
async def test_com_a_mirrored_polyline_is_written_in_its_own_frame(seat):
    pline = _FakePline(seat.log, _flat(FOUR), normal=(0.0, 0.0, -1.0))
    seat.objects["2B"] = pline
    info = await seat.backend.entity_set_vertices("2B", SIX, SIX_BULGES)
    assert pline._coords[2] == -40.0  # stored OCS x is -WCS x in a -Z frame
    assert pline._bulges[0] == -1.0
    assert all(_close(got, want) for got, want in zip(info.properties["points"], SIX, strict=True))
    assert info.properties["bulges"] == SIX_BULGES


@needs_pywin32
async def test_com_edit_geometry_routes_points_to_the_vertex_writer(seat):
    pline = _FakePline(seat.log, _flat(FOUR))
    seat.objects["2B"] = pline
    info = await seat.backend.entity_edit_geometry("2B", bulges=[0.0, 1.0, 0.0, 0.0])
    assert info.handle == "2B" and pline._bulges == [0.0, 1.0, 0.0, 0.0]
    with pytest.raises(RuntimeError, match="points"):
        await seat.backend.entity_edit_geometry("2B", radius=3.0)


@needs_pywin32
async def test_com_text_moves_by_activex_move_keeping_the_handle(seat):
    text = _FakeText(seat.log)
    seat.objects["3C"] = text
    info = await seat.backend.entity_edit_text("3C", x=11.0, y=22.0)
    assert info.handle == "3C"
    assert ("Move", "3C", (10.0, 20.0, 0.0)) in seat.log
    assert _close(info.properties["insertion"], [11.0, 22.0])
    again = await seat.backend.entity_edit_geometry("3C", x=1.0)
    assert _close(again.properties["insertion"], [1.0, 22.0])


@needs_pywin32
async def test_com_a_tilted_text_position_is_refused(seat):
    text = _FakeText(seat.log, normal=(0.7071, 0.0, 0.7071))
    seat.objects["3C"] = text
    with pytest.raises(UnsupportedCapabilityError):
        await seat.backend.entity_edit_text("3C", x=5.0)
    assert not [e for e in seat.log if e[0] == "Move"]


@needs_pywin32
async def test_com_create_polyline_sets_bulges_in_the_same_job(seat):
    info = await seat.backend.entity_create_polyline(SIX, True, bulges=SIX_BULGES, xdata=TAG)
    made = seat.space.made[0]
    assert made._bulges == SIX_BULGES and made.Closed is True
    assert info.properties["bulges"] == SIX_BULGES
    assert ("SetXData", made.Handle) in seat.log


class _FakeDim:
    def __init__(self, log, name):
        self._log = log
        self.ObjectName = name
        self.Handle = "D1"
        self.Layer = "0"
        self.color = 256
        self.Linetype = "ByLayer"
        self.Visible = True

    def SetXData(self, codes, values):  # noqa: N802
        self._log.append(("SetXData", self.ObjectName, tuple(getattr(values, "value", values))))


class _DimSpace:
    def __init__(self, log):
        self._log = log

    def AddDimRotated(self, *a):  # noqa: N802
        return _FakeDim(self._log, "AcDbRotatedDimension")

    def AddDimAligned(self, *a):  # noqa: N802
        return _FakeDim(self._log, "AcDbAlignedDimension")

    def AddDimRadial(self, *a):  # noqa: N802
        return _FakeDim(self._log, "AcDbRadialDimension")

    def AddDimDiametric(self, *a):  # noqa: N802
        return _FakeDim(self._log, "AcDbDiametricDimension")


@needs_pywin32
async def test_com_the_four_dimension_contracts_tag_the_dimension_in_the_same_job(
    seat, monkeypatch
):
    import backends.com_backend as cb

    monkeypatch.setattr(cb, "_msp", lambda: _DimSpace(seat.log))
    monkeypatch.setattr(cb, "_apply_dim_tolerance", lambda *a, **k: None)
    await seat.backend.dimension_linear(0, 0, 10, 0, 5, 5, xdata=TAG)
    await seat.backend.dimension_aligned(0, 0, 10, 0, 5, 5, xdata=TAG)
    await seat.backend.dimension_radius(0, 0, 10, 0, xdata=TAG)
    await seat.backend.dimension_diameter(0, 0, 10, 0, xdata=TAG)
    tagged = [entry[1] for entry in seat.log if entry[0] == "SetXData"]
    assert tagged == [
        "AcDbRotatedDimension",
        "AcDbAlignedDimension",
        "AcDbRadialDimension",
        "AcDbDiametricDimension",
    ]


def _smoke_report(**six_overrides):
    from scripts.smoke_edit_com import SIX as SMOKE_SIX
    from scripts.smoke_edit_com import SIX_BULGES as SMOKE_BULGES
    from scripts.smoke_edit_com import TAG as SMOKE_TAG
    from scripts.smoke_edit_com import _perimeter as smoke_perimeter

    six = {
        "old": "2B",
        "handle": "2B",
        "handle_kept": True,
        "replaced": [],
        "points": SMOKE_SIX,
        "bulges": SMOKE_BULGES,
        "points_ok": True,
        "bulges_ok": True,
        "layer": "GEOMETRY",
        "xdata": SMOKE_TAG["ACADMCP_MECH"],
        "perimeter": smoke_perimeter(SMOKE_SIX, SMOKE_BULGES),
        "perimeter_hand": smoke_perimeter(SMOKE_SIX, SMOKE_BULGES),
    }
    six.update(six_overrides)
    return {
        "same_count": {"handle_kept": True, "points_ok": True},
        "four_to_six": six,
        "text": {"handle_kept": True, "insertion": [50.0, 60.0], "target": [50.0, 60.0]},
        "mtext": {"handle_kept": True, "insertion": [-5.0, 12.5], "target": [-5.0, 12.5]},
        "dimension_xdata": SMOKE_TAG["ACADMCP_MECH"],
        "table_xdata": SMOKE_TAG["ACADMCP_MECH"],
        "mech_dimensions": 5,
        "mech_roles": ["dim"],
    }


@pytest.mark.parametrize(
    ("overrides", "passes"),
    [
        ({}, True),
        ({"handle": "N0", "handle_kept": False, "replaced": [{"old": "2B", "new": "N0"}]}, True),
        ({"handle": "N0", "handle_kept": False}, False),  # replaced without saying so
        ({"xdata": []}, False),
        ({"bulges_ok": False}, False),
    ],
)
async def test_the_edit_smoke_accepts_either_s4_answer_but_never_a_silent_one(overrides, passes):
    from scripts.live.harness import LiveHarness
    from scripts.smoke_edit_com import assess

    h = LiveHarness("edit")
    assess(h, _smoke_report(**overrides))
    assert all(row["status"] == "pass" for row in h.checks) is passes


async def test_entity_set_vertices_is_a_live_mutator():
    import backends.com_backend as cb

    assert "entity_set_vertices" in cb._MUTATING_METHODS
    assert getattr(vars(cb.ComBackend)["entity_set_vertices"], "__acadmcp_mutating__", False)
