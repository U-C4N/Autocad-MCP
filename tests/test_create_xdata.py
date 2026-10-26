"""Create calls that carry their own XDATA (B17): one call, one lock, one undo step.

The mechanical generators tag every entity they draw; doing it with a second
entity_set_xdata per entity would double the COM round trips and the headless
undo snapshots. The tag rides in the create call instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backends.com_backend import ComBackend
from backends.xdata_specs import prepare_xdata

TAG = {"ACADMCP_MECH": ['{"kind":"feature","v":1}']}


def test_prepare_xdata_validates_before_anything_is_written():
    assert prepare_xdata(None) == []
    assert prepare_xdata(TAG) == [("ACADMCP_MECH", [(1000, '{"kind":"feature","v":1}')])]
    with pytest.raises(TypeError, match="app_name"):
        prepare_xdata(["ACADMCP_MECH"])
    with pytest.raises(ValueError, match="app name"):
        prepare_xdata({"bad name!": ["x"]})
    with pytest.raises(ValueError, match="no values"):
        prepare_xdata({"ACADMCP_MECH": []})
    with pytest.raises(ValueError, match="16 KB"):
        prepare_xdata({"ACADMCP_MECH": ["x" * 255] * 70})  # 17,850 characters


@pytest.mark.asyncio
async def test_every_tagged_create_call_writes_its_xdata_in_one_step(backend, monkeypatch):
    marks = []
    original = backend._mark_dirty

    def counted():
        marks.append(1)
        original()

    monkeypatch.setattr(backend, "_mark_dirty", counted)
    made = [
        await backend.entity_create_line(0, 0, 1, 0, xdata=TAG),
        await backend.entity_create_circle(0, 0, 1, xdata=TAG),
        await backend.entity_create_arc(0, 0, 1, 0, 90, xdata=TAG),
        await backend.entity_create_polyline([[0, 0], [1, 0], [1, 1]], xdata=TAG),
        await backend.entity_create_text("A", 0, 0, xdata=TAG),
        await backend.entity_create_hatch("ANSI31", [[0, 0], [1, 0], [1, 1]], xdata=TAG),
    ]
    assert len(marks) == len(made)  # one undo step per create, the tag included
    for info in made:
        got = await backend.entity_get_xdata(info.handle, "ACADMCP_MECH")
        assert got["xdata"]["ACADMCP_MECH"] == ['{"kind":"feature","v":1}']


@pytest.mark.asyncio
async def test_a_refused_tag_creates_nothing(backend):
    before = len(await backend.entity_list(limit=1000))
    with pytest.raises(ValueError, match="16 KB"):
        await backend.entity_create_line(0, 0, 1, 0, xdata={"ACADMCP_MECH": ["x" * 255] * 70})
    assert len(await backend.entity_list(limit=1000)) == before


@pytest.mark.asyncio
async def test_an_untagged_create_call_is_unchanged(backend):
    info = await backend.entity_create_line(0, 0, 1, 0)
    assert (await backend.entity_get_xdata(info.handle))["xdata"] == {}


class _FakeEntity:
    def __init__(self, log, object_name, **members):
        self._log = log
        self.ObjectName = object_name
        self.Handle = "2A"
        self.Layer = "0"
        self.color = 256
        self.Linetype = "ByLayer"
        self.Visible = True
        for name, value in members.items():
            setattr(self, name, value)

    def GetBoundingBox(self):
        return ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0))

    def SetXData(self, codes, values):
        self._log.append(
            (
                "SetXData",
                list(getattr(codes, "value", codes)),
                list(getattr(values, "value", values)),
            )
        )


def _com_seat(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    import backends.com_backend as cb

    log: list = []
    space = SimpleNamespace(
        AddLine=lambda start, end: _FakeEntity(
            log, "AcDbLine", StartPoint=(0.0, 0.0, 0.0), EndPoint=(1.0, 0.0, 0.0), Length=1.0
        ),
        AddCircle=lambda centre, radius: _FakeEntity(
            log, "AcDbCircle", Center=(0.0, 0.0, 0.0), Radius=float(radius)
        ),
    )
    doc = SimpleNamespace(
        RegisteredApplications=SimpleNamespace(Add=lambda name: log.append(("Add", name))),
        Regen=lambda viewport: None,
    )
    monkeypatch.setattr(cb, "_msp", lambda: space)
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    backend = ComBackend()
    jobs: list = []

    async def _run(func, *args, **kwargs):  # one call here is one COM job
        jobs.append(func)
        return func(*args, **kwargs)

    backend._run = _run
    return backend, log, jobs


@pytest.mark.asyncio
async def test_com_create_writes_the_tag_inside_the_same_job(monkeypatch):
    backend, log, jobs = _com_seat(monkeypatch)
    await backend.entity_create_line(0, 0, 1, 0, xdata=TAG)
    await backend.entity_create_circle(0, 0, 2, xdata=TAG)
    assert len(jobs) == 2
    expected = ("SetXData", [1001, 1000], ["ACADMCP_MECH", '{"kind":"feature","v":1}'])
    assert log == [("Add", "ACADMCP_MECH"), expected, ("Add", "ACADMCP_MECH"), expected]


@pytest.mark.asyncio
async def test_com_create_without_a_tag_writes_no_xdata(monkeypatch):
    backend, log, jobs = _com_seat(monkeypatch)
    await backend.entity_create_line(0, 0, 1, 0)
    assert log == [] and len(jobs) == 1
