"""1.8-23 on a fake seat: one display refresh per job, bulk tools in one STA job, O(new) reads.

§4.6 of the rival's report measured the live engine about 2x slower; the code
had three mechanical causes. Each is held here without AutoCAD (``_run`` is the
real one on a plain daemon thread, the seat is tests/test_com_space.py's):

1. every create path ended in ``Regen(acActiveViewport)`` - per entity. The
   request is now collected per job and flushed once: ``Update()`` on the one
   entity of a single create, one Regen for a larger job (``COM_REFRESH_*``);
2. ``entity_batch_create`` / ``entity_delete_many`` were N STA jobs (the server
   looped over the single tools); each is now ONE job, with per-item rows that
   carry the 1.8-02 kind;
3. a corner op read every layout block around its command; it now reads the
   owner block's tail only, and a raw command reads each block from its end.

The headless results of both bulk tools are the 1.8.0 server loop's, byte for
byte: the loop is transcribed below as the oracle.
"""

from __future__ import annotations

import importlib
import json
import time

import pytest

pytest.importorskip("win32com.client", reason="pywin32 builds the point VARIANTs")

import pywintypes  # noqa: E402
from fastmcp import Client  # noqa: E402

import config  # noqa: E402
import server  # noqa: E402
from backends import com_backend as cb  # noqa: E402
from backends.base import lineweight_name, parse_lineweight  # noqa: E402
from backends.ezdxf_backend import EzdxfBackend  # noqa: E402
from tests.test_com_space import _Block, _Doc, _Entity, _Seat  # noqa: E402

# ── the fake seat, counting refreshes and reads ─────────────────────────────


@pytest.fixture
def seat(monkeypatch):
    fake = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: fake)
    fake.regens = 0
    fake.updates = []
    fake.item_reads = {}

    def regen(doc, _which):
        fake.regens += 1

    def update(entity):
        fake.updates.append(entity.Handle)

    original_item = _Block.Item

    def item(block, index):
        fake.item_reads[block.ObjectID] = fake.item_reads.get(block.ObjectID, 0) + 1
        return original_item(block, index)

    def handle_to_object(doc, handle):
        for index in range(doc.Layouts.Count):
            for entity in doc.Layouts.Item(index).Block:
                if entity.Handle == handle:
                    return entity
        # what AutoCAD raises for a handle it does not know (a COM error)
        raise pywintypes.com_error(-2147467259, "Unspecified error", None, None)

    def add_circle(block, center, radius):
        if float(radius) <= 0:
            raise pywintypes.com_error(-2145386493, "Invalid input", None, None)
        center = tuple(getattr(center, "value", center))
        return block.add("AcDbCircle", Center=center, Radius=float(radius))

    monkeypatch.setattr(_Doc, "Regen", regen)
    monkeypatch.setattr(_Doc, "HandleToObject", handle_to_object)
    monkeypatch.setattr(_Entity, "Update", update, raising=False)
    monkeypatch.setattr(_Block, "Item", item)
    monkeypatch.setattr(_Block, "AddCircle", add_circle)
    return fake


@pytest.fixture
def com(seat):
    backend = cb.ComBackend()
    backend._executor = cb.DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend
    if backend._executor is not None:
        backend._executor.shutdown(wait=False)
        backend._executor = None


def _jobs(com) -> int:
    return com.run_metrics()["jobs"]


def _circles(n: int, **extra) -> list[dict]:
    return [{"type": "circle", "cx": float(i), "cy": 0.0, "radius": 1.0, **extra} for i in range(n)]


def _model(seat) -> _Block:
    return seat.doc.ModelSpace


# ── (1) one refresh per job ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_job_of_200_creates_regenerates_once_and_never_per_entity(com, seat):
    jobs = _jobs(com)
    result = await com.entity_batch_create(_circles(200))
    assert result["created"] == 200 and result["errors"] == []
    assert _model(seat).Count == 200
    assert _jobs(com) - jobs == 1, "the 200 creates are one STA job"
    assert seat.regens == 1, "one Regen for the job, none per entity"
    assert seat.updates == [], "above COM_REFRESH_UPDATE_MAX the job regenerates instead"


@pytest.mark.asyncio
async def test_a_single_create_is_one_update_and_no_regen(com, seat):
    info = await com.entity_create_circle(0.0, 0.0, 5.0)
    assert seat.updates == [info.handle]
    assert seat.regens == 0


@pytest.mark.asyncio
async def test_two_creates_in_one_job_regenerate_while_s4_is_unmeasured(com, seat):
    """Whether Update() of the last entity paints the first is S4's question:
    until it is answered the default (1) regenerates a job of two."""
    await com.entity_batch_create(_circles(2))
    assert seat.regens == 1 and seat.updates == []


@pytest.mark.asyncio
async def test_a_larger_update_budget_updates_the_last_entity(com, seat, monkeypatch):
    monkeypatch.setattr(config.settings, "com_refresh_update_max", 5)
    result = await com.entity_batch_create(_circles(3))
    assert seat.updates == [result["entities"][-1]["handle"]]
    assert seat.regens == 0


@pytest.mark.asyncio
async def test_regen_mode_is_the_plans_fallback_one_regen_per_call(com, seat, monkeypatch):
    monkeypatch.setattr(config.settings, "com_refresh_mode", "regen")
    await com.entity_create_circle(0.0, 0.0, 5.0)
    assert seat.regens == 1 and seat.updates == []


@pytest.mark.asyncio
async def test_an_update_that_raises_falls_back_to_one_regen(com, seat, monkeypatch):
    def broken(entity):
        raise pywintypes.com_error(-2147467259, "Update refused", None, None)

    monkeypatch.setattr(_Entity, "Update", broken, raising=False)
    await com.entity_create_circle(0.0, 0.0, 5.0)
    assert seat.regens == 1


@pytest.mark.asyncio
async def test_a_refresh_naming_no_entity_regenerates(com, seat):
    def job():
        cb._acad_doc()
        cb._regen()  # a layer change, an explode: nothing to Update()

    await com._run(job)
    assert seat.regens == 1 and seat.updates == []


@pytest.mark.asyncio
async def test_a_job_that_never_asked_does_not_refresh(com, seat):
    await com.system_get_variable("CMDECHO")
    assert seat.regens == 0 and seat.updates == []


def test_a_create_job_is_still_reported_under_its_tool():
    """call_outcomes / timeouts name a call by the method its closure lives in."""
    from backends.quarantine import real_call_name

    backend = cb.ComBackend()
    for kind, builder in backend._BATCH_JOBS.items():
        spec = {
            "line": (0, 0, 1, 1),
            "circle": (0, 0, 1),
            "arc": (0, 0, 1, 0, 90),
            "polyline": ([[0, 0], [1, 0]],),
            "text": ("A", 0, 0),
            "mtext": ("A", 0, 0),
            "point": (0, 0),
            "hatch": ("ANSI31", [[0, 0], [1, 0], [1, 1]]),
            "spline": ([[0, 0], [1, 1]],),
            "ellipse": (0, 0, 1, 0),
        }[kind]
        assert real_call_name(getattr(backend, builder)(*spec)) == f"entity_create_{kind}"


@pytest.mark.asyncio
async def test_the_refresh_is_booked_as_refresh_in_the_run_breakdown(com, seat):
    await com.entity_batch_create(_circles(10))
    ms = com.run_metrics()["ms"]
    assert set(ms) == {"queue", "guard", "work", "refresh", "counter"}


# ── (3) the bulk tools, one job each ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_bad_spec_in_the_middle_leaves_the_earlier_entities_written(com, seat):
    specs = [
        *_circles(2),
        {"type": "circle", "cx": 0.0, "cy": 0.0, "radius": -1.0},  # AutoCAD refuses it
        {"type": "circle", "cx": 0.0, "cy": 0.0},  # no radius: refused before the job
        {"cx": 0.0},  # no type
        {"type": "rectangle"},  # not a batch type
        {"type": "circle", "cx": 9.0, "cy": 0.0, "radius": 1.0, "lineweight": "thick"},
        *_circles(1),
    ]
    await com.system_get_variable("CMDECHO")  # binds the document
    jobs = _jobs(com)
    revision = com.document_identity()["revision"]
    result = await com.entity_batch_create(specs)
    assert _jobs(com) - jobs == 1, "still one STA job"
    assert result["created"] == 3
    assert _model(seat).Count == 3, "the specs before and after the bad one are written"
    rows = {row["index"]: row for row in result["errors"]}
    assert sorted(rows) == [2, 3, 4, 5, 6]
    assert rows[2]["kind"] == "failed" and rows[2]["type"] == "circle"
    assert rows[3]["kind"] == "invalid_args"
    assert rows[4] == {"index": 4, "error": "Missing 'type' key", "kind": "invalid_args"}
    assert rows[5]["error"] == "Unknown type: rectangle" and rows[5]["kind"] == "invalid_args"
    assert rows[6]["kind"] == "invalid_args", "a refused lineweight draws nothing"
    assert com.document_identity()["revision"] == revision + 1, "one call, one revision step"


@pytest.mark.asyncio
async def test_a_batch_lineweight_is_set_in_the_same_job(com, seat):
    result = await com.entity_batch_create(_circles(1, lineweight=35))
    assert result["entities"][0]["lineweight_name"] == "0.35 mm"
    assert _model(seat).Item(0).Lineweight == 35


@pytest.mark.asyncio
async def test_a_refused_document_switch_writes_nothing_for_the_whole_batch(com, seat):
    await com.entity_create_circle(0.0, 0.0, 1.0)  # binds Sheet.dwg
    other = seat.Documents.Add()
    seat.ActiveDocument = other
    with pytest.raises(cb.ActiveDocumentChangedError):
        await com.entity_batch_create(_circles(5))
    assert _model(seat).Count == 1 and other.ModelSpace.Count == 0


@pytest.mark.asyncio
async def test_entity_delete_many_is_one_job_and_names_the_missing_handle(com, seat):
    made = await com.entity_batch_create(_circles(4))
    handles = [row["handle"] for row in made["entities"]]
    jobs = _jobs(com)
    revision = com.document_identity()["revision"]
    result = await com.entity_delete_many([handles[0], "FFFF", handles[2]])
    assert _jobs(com) - jobs == 1, "one STA call for the whole list"
    assert result["deleted"] == 2
    assert [(row["handle"], row["kind"]) for row in result["errors"]] == [
        ("FFFF", "unknown_handle")
    ]
    assert [e.Handle for e in _model(seat)] == [handles[1], handles[3]]
    assert com.document_identity()["revision"] == revision + 1


@pytest.mark.asyncio
async def test_entity_delete_many_can_stop_at_the_first_failure(com, seat):
    made = await com.entity_batch_create(_circles(3))
    handles = [row["handle"] for row in made["entities"]]
    result = await com.entity_delete_many(
        [handles[0], "FFFF", handles[1], handles[2]], stop_on_error=True
    )
    assert result["deleted"] == 1 and len(result["errors"]) == 1
    assert _model(seat).Count == 2, "nothing after the failure ran"


# A healthy bulk call longer than COM_CALL_TIMEOUT: one deadline over the whole
# list sent it down the hung-AutoCAD path (executor abandoned, the old job still
# writing beside the new thread). The list is sliced inside the deadline instead.


def _slow(monkeypatch, cls, name, seconds: float = 0.02) -> None:
    original = getattr(cls, name)

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return original(*args, **kwargs)

    monkeypatch.setattr(cls, name, slow)


def _abandoned(com) -> list[dict]:
    return [row for row in com.call_outcomes if row.get("outcome") == "abandoned_deadline"]


@pytest.mark.asyncio
async def test_a_batch_longer_than_the_deadline_is_sliced_not_abandoned(com, seat, monkeypatch):
    monkeypatch.setattr(config.settings, "com_call_timeout", 0.5)
    _slow(monkeypatch, _Block, "AddCircle")  # 50 x 20 ms = 1 s, twice the deadline
    await com.system_get_variable("CMDECHO")  # binds the document
    jobs = _jobs(com)
    revision = com.document_identity()["revision"]
    seen: list[tuple[int, int]] = []

    async def progress(done, total):
        seen.append((done, total))

    result = await com.entity_batch_create(_circles(50), progress=progress)
    assert result["created"] == 50 and result["errors"] == []
    assert [row["handle"] for row in result["entities"]] == [e.Handle for e in _model(seat)]
    assert com._connected is True and _abandoned(com) == [], "no job overran the deadline"
    slices = _jobs(com) - jobs
    assert 2 <= slices <= 50, "sliced inside the deadline"
    assert seat.regens <= slices, "one refresh per slice at most"
    assert com.document_identity()["revision"] == revision + 1, "one call, one revision step"
    assert seen[0] == (0, 50) and len(seen) == slices
    assert [done for done, _total in seen] == sorted({done for done, _total in seen})


@pytest.mark.asyncio
async def test_a_delete_many_longer_than_the_deadline_is_sliced_not_abandoned(
    com, seat, monkeypatch
):
    made = await com.entity_batch_create(_circles(50))
    handles = [row["handle"] for row in made["entities"]]
    monkeypatch.setattr(config.settings, "com_call_timeout", 0.5)
    _slow(monkeypatch, _Entity, "Delete")
    jobs = _jobs(com)
    revision = com.document_identity()["revision"]
    result = await com.entity_delete_many([*handles[:25], "FFFF", *handles[25:]])
    assert result["deleted"] == 50 and _model(seat).Count == 0
    assert [(row["handle"], row["kind"]) for row in result["errors"]] == [
        ("FFFF", "unknown_handle")
    ]
    assert com._connected is True and _abandoned(com) == []
    assert _jobs(com) - jobs >= 2
    assert com.document_identity()["revision"] == revision + 1


@pytest.mark.asyncio
async def test_stop_on_error_still_stops_the_whole_call_across_slices(com, seat, monkeypatch):
    made = await com.entity_batch_create(_circles(30))
    handles = [row["handle"] for row in made["entities"]]
    monkeypatch.setattr(config.settings, "com_call_timeout", 0.2)
    _slow(monkeypatch, _Entity, "Delete")
    result = await com.entity_delete_many(
        [*handles[:10], "FFFF", *handles[10:]], stop_on_error=True
    )
    assert result["deleted"] == 10 and len(result["errors"]) == 1
    assert _model(seat).Count == 20, "no slice ran after the failure"


@pytest.mark.asyncio
async def test_without_a_deadline_a_bulk_call_is_one_job(com, seat, monkeypatch):
    monkeypatch.setattr(config.settings, "com_call_timeout", 0)
    jobs = _jobs(com)
    await com.entity_batch_create(_circles(20))
    assert _jobs(com) - jobs == 1


@pytest.mark.asyncio
async def test_the_server_tools_call_the_one_job_methods(com, seat, monkeypatch):
    async def make():
        return com

    monkeypatch.setattr(server, "_make_backend", make)
    async with Client(server.mcp) as client:
        created = await client.call_tool("entity_batch_create", {"entities": _circles(50)})
        jobs = _jobs(com)
        handles = [row["handle"] for row in created.structured_content["entities"]]
        deleted = await client.call_tool("entity_delete_many", {"handles": handles + ["FFFF"]})
    assert created.structured_content["created"] == 50
    assert seat.regens == 1
    payload = deleted.structured_content
    assert payload["ok"] is True and payload["deleted"] == 50
    assert payload["errors"][0]["kind"] == "unknown_handle"
    assert _jobs(com) - jobs == 1, "entity_delete_many is one STA job, not 51"


# ── (2) O(new): the owner block's tail ──────────────────────────────────────


def _seed(block: _Block, n: int) -> None:
    for _ in range(n):
        block.add("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(1, 0, 0), Length=1.0)


@pytest.mark.asyncio
async def test_a_corner_op_on_a_paper_layout_reads_only_the_owner_block(com, seat):
    doc = seat.doc
    _seed(doc.ModelSpace, 5_000)
    await com.layout_set_current("A3")
    sheet = doc.space("A3")
    a = sheet.add("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(10, 0, 0), Length=10.0)
    b = sheet.add("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(0, 10, 0), Length=10.0)

    def fillet(d, _text):
        d.space("A3").add("AcDbArc", Center=(2, 2, 0), Radius=2.0)

    doc.on_command = fillet
    seat.item_reads.clear()
    info = await com.entity_fillet(a.Handle, b.Handle, 2.0)
    assert info.type == "ARC" and sheet.Count == 3
    assert seat.item_reads.get(doc.ModelSpace.ObjectID, 0) <= 2, "5,000 lines: its mark and stop"
    assert seat.item_reads.get(doc.space("Layout1").ObjectID, 0) == 0
    assert seat.item_reads[sheet.ObjectID] <= 3, "the mark, the new arc and the stop"


@pytest.mark.asyncio
async def test_a_paper_owner_still_reports_an_entity_written_into_model_space(com, seat):
    """1.8-05's cross-space signal survives the owner-block read (1.8-23)."""
    doc = seat.doc
    await com.layout_set_current("A3")
    doc.on_command = lambda d, _t: d.ModelSpace.add("AcDbArc", Center=(2, 2, 0), Radius=2.0)
    outcome = await com._run(
        lambda: cb.ComBackend._safe_send_command(cb._acad_doc(), "_X", owner="A3")
    )
    assert outcome.created_count == 1 and outcome.created[0]["space"] == "Model"
    assert outcome.report()["warning"] == "created_outside_target_space"
    assert (outcome.entities_before, outcome.entities_after) == (0, 0), "the owner block's"


@pytest.mark.asyncio
async def test_a_corner_op_costs_the_same_reads_whatever_the_drawing_holds(com, seat):
    doc = seat.doc

    async def fillet_reads() -> int:
        space = doc.ModelSpace
        a = space.add("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(10, 0, 0), Length=10.0)
        b = space.add("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(0, 10, 0), Length=10.0)
        doc.on_command = lambda d, _t: d.ModelSpace.add("AcDbArc", Center=(2, 2, 0), Radius=2.0)
        seat.item_reads.clear()
        await com.entity_fillet(a.Handle, b.Handle, 2.0)
        return sum(seat.item_reads.values())

    small = await fillet_reads()
    _seed(doc.ModelSpace, 5_000)
    assert await fillet_reads() == small, "fillet reads are independent of N"


@pytest.mark.asyncio
async def test_a_raw_command_reads_each_block_from_its_end_only(com, seat):
    doc = seat.doc
    _seed(doc.ModelSpace, 5_000)

    def draws(d, _text):
        d.space("Layout1").add("AcDbCircle", Center=(0, 0, 0), Radius=1.0)

    doc.on_command = draws
    seat.item_reads.clear()
    result = await com.system_run_command("_CIRCLE 0,0 1")
    assert result["created_count"] == 1 and result["created"][0]["space"] == "Layout1"
    assert result["warning"] == "created_outside_target_space"
    assert seat.item_reads[doc.ModelSpace.ObjectID] <= 2, "5,000 lines, two reads"


@pytest.mark.asyncio
async def test_an_entity_added_after_an_erase_is_still_found(com, seat):
    """The tail is read from the end, not from the old Count: a command that
    erases one entity and adds another keeps the Count and shifts the indices."""
    doc = seat.doc
    _seed(doc.ModelSpace, 3)

    def erase_and_add(d, _text):
        d.ModelSpace._items.pop(0)
        d.ModelSpace.add("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(2, 0, 0), Length=2.0)

    doc.on_command = erase_and_add
    outcome = await com._run(
        lambda: cb.ComBackend._safe_send_command(cb._acad_doc(), "_X", owner="Model")
    )
    assert outcome.created_count == 1
    assert outcome.entities_before == outcome.entities_after == 3


def test_a_seat_without_layouts_reads_the_target_block_not_all_of_model_space():
    class _NoLayouts:
        def __init__(self, doc):
            self._doc = doc

        def __getattr__(self, name):
            if name == "Layouts":
                raise AttributeError("no Layouts on this seat")
            return getattr(self._doc, name)

    doc = _Doc(_Seat(), "Sheet.dwg", "C:\\work\\Sheet.dwg")
    _seed(doc.ModelSpace, 50)
    doc.on_command = lambda d, _t: d.ModelSpace.add("AcDbCircle", Center=(0, 0, 0), Radius=1.0)
    outcome = cb.ComBackend._safe_send_command(_NoLayouts(doc), "_CIRCLE", target_space="Model")
    assert outcome.created_count == 1 and outcome.created[0]["space"] == "Model"
    assert (outcome.entities_before, outcome.entities_after) == (50, 51)


# ── the headless engine: the 1.8.0 loop, byte for byte ──────────────────────


async def _loop_1_8_0_batch_create(b, entities: list[dict]) -> dict:
    """server.py's entity_batch_create body up to 1.8.0, transcribed (the oracle)."""
    from dataclasses import asdict

    results, errors = [], []
    create_map = {
        "line": b.entity_create_line,
        "circle": b.entity_create_circle,
        "arc": b.entity_create_arc,
        "polyline": b.entity_create_polyline,
        "text": b.entity_create_text,
        "mtext": b.entity_create_mtext,
        "point": b.entity_create_point,
        "hatch": b.entity_create_hatch,
        "spline": b.entity_create_spline,
        "ellipse": b.entity_create_ellipse,
    }
    for i, ent_def in enumerate(json.loads(json.dumps(entities))):
        ent_type = ent_def.pop("type", None)
        if not ent_type:
            errors.append({"index": i, "error": "Missing 'type' key"})
            continue
        creator = create_map.get(ent_type.lower())
        if not creator:
            errors.append({"index": i, "error": f"Unknown type: {ent_type}"})
            continue
        try:
            weight = parse_lineweight(ent_def.pop("lineweight", None))
            info = await creator(**ent_def)
            if weight is not None:
                await b.entity_set_properties(info.handle, lineweight=weight)
                info.lineweight_name = lineweight_name(weight)
            results.append(asdict(info))
        except Exception as exc:
            errors.append({"index": i, "type": ent_type, "error": str(exc)})
    return {"created": len(results), "errors": errors, "entities": results}


async def _loop_1_8_0_delete_many(b, handles: list[str]) -> dict:
    deleted, errors = 0, []
    for h in handles:
        try:
            await b.entity_delete(h)
            deleted += 1
        except Exception as exc:
            errors.append({"handle": h, "error": str(exc)})
    return {"ok": True, "deleted": deleted, "errors": errors}


_MIXED_SPECS = [
    {"type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0, "layer": "GEOMETRY"},
    {"type": "circle", "cx": 50, "cy": 50, "radius": 25, "lineweight": 35},
    {"type": "arc", "cx": 0, "cy": 0, "radius": 5, "start_angle": 0, "end_angle": 90},
    {"type": "polyline", "points": [[0, 0], [10, 0], [10, 10]], "closed": True},
    {"type": "text", "text": "A", "x": 1, "y": 2},
    {"type": "mtext", "text": "B", "x": 3, "y": 4},
    {"type": "point", "x": 5, "y": 6},
    {"type": "spline", "fit_points": [[0, 0], [5, 5], [10, 0]]},
    {"type": "ellipse", "cx": 0, "cy": 0, "major_x": 10, "major_y": 0, "ratio": 0.5},
    {"type": "hatch", "pattern": "ANSI31", "boundary_points": [[0, 0], [5, 0], [5, 5]]},
    {"type": "circle", "cx": 0, "cy": 0},
    {"cx": 1},
    {"type": "rectangle", "x": 0},
    {"type": "circle", "cx": 0, "cy": 0, "radius": 1, "lineweight": "thick"},
    {"type": "LINE", "x1": 0, "y1": 0, "x2": 1, "y2": 1},
]


@pytest.mark.asyncio
async def test_headless_entity_batch_create_is_the_1_8_0_loop_byte_for_byte():
    old, new = EzdxfBackend(), EzdxfBackend()
    await old.connect()
    await new.connect()
    await old.drawing_new()
    await new.drawing_new()
    specs = json.loads(json.dumps(_MIXED_SPECS))
    expected = await _loop_1_8_0_batch_create(old, _MIXED_SPECS)
    got = await new.entity_batch_create(specs)
    assert json.dumps(got, sort_keys=True, default=str) == json.dumps(
        expected, sort_keys=True, default=str
    )
    assert specs == _MIXED_SPECS, "the caller's specs are not modified"
    assert got["created"] == 11 and len(got["errors"]) == 4


@pytest.mark.asyncio
async def test_headless_entity_delete_many_is_the_1_8_0_loop_byte_for_byte():
    old, new = EzdxfBackend(), EzdxfBackend()
    for b in (old, new):
        await b.connect()
        await b.drawing_new()
        for i in range(3):
            await b.entity_create_circle(float(i), 0.0, 1.0)
    handles = [e.handle for e in await old.entity_list(None, None, 10, 0)][:2] + ["FFFF"]
    expected = await _loop_1_8_0_delete_many(old, handles)
    got = await new.entity_delete_many(handles)
    assert {"ok": True, **got} == expected


@pytest.mark.asyncio
async def test_headless_server_tools_answer_what_they_answered(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    oracle = EzdxfBackend()
    await oracle.connect()
    await oracle.drawing_new()
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        created = await client.call_tool("entity_batch_create", {"entities": _MIXED_SPECS})
        expected = await _loop_1_8_0_batch_create(oracle, _MIXED_SPECS)
        assert created.structured_content["created"] == expected["created"]
        assert created.structured_content["errors"] == expected["errors"]
        handles = [row["handle"] for row in created.structured_content["entities"]][:3]
        deleted = await client.call_tool("entity_delete_many", {"handles": handles + ["FFFF"]})
    oracle_handles = [row["handle"] for row in expected["entities"]][:3]
    assert deleted.structured_content == await _loop_1_8_0_delete_many(
        oracle, oracle_handles + ["FFFF"]
    )


# ── the scripts the gate names ──────────────────────────────────────────────


def test_the_perf_script_and_the_operator_edit_smoke_import():
    perf = importlib.import_module("benchmarks.perf_com")
    assert perf.TARGETS["bulk_speedup_min"] == 5.0
    assert perf.TARGETS["single_create_median_drop_min"] == 0.30
    importlib.import_module("scripts.smoke_operator_edits_com")


def test_the_committed_perf_result_is_pending_against_the_fixed_targets():
    """No live number exists yet: the file pins the targets before the run."""
    perf = importlib.import_module("benchmarks.perf_com")
    payload = json.loads(perf.DEFAULT_JSON.read_text(encoding="utf-8"))
    assert payload["schema"] == perf.SCHEMA and payload["targets"] == perf.TARGETS
    if payload["status"] == "pending":
        assert payload["sides"] == {"base": None, "current": None}
        assert all(row["pass"] is None for row in payload["verdict"].values())


def test_the_perf_verdict_reads_the_targets_the_way_the_plan_states_them():
    perf = importlib.import_module("benchmarks.perf_com")

    def side(**rows):
        return {"workloads": {name: dict(values) for name, values in rows.items()}}

    base = side(
        create_circle_singles={"median_ms": 40.0, "total_ms": 8000.0},
        delete_singles_200={"total_ms": 6000.0},
    )
    current = side(
        create_circle_singles={"median_ms": 26.0},
        batch_create_200={"median_ms": 1500.0},
        delete_many_200={"median_ms": 1300.0},
        fillet_50_n5000={"median_ms": 100.0},
        fillet_50_n10000={"median_ms": 130.0},
    )
    got = perf.verdict(base, current)
    assert got["bulk_create_speedup"] == {"value": 5.333, "target": 5.0, "pass": True}
    assert got["bulk_delete_speedup"]["pass"] is False  # 4.615x
    assert got["single_create_median_drop"] == {"value": 0.35, "target": 0.3, "pass": True}
    assert got["fillet_n_ratio"] == {"value": 1.3, "target": 1.2, "pass": False}
    assert perf.verdict(None, current)["bulk_create_speedup"]["pass"] is None
    assert perf.summarise([3.0, 1.0, 2.0])["median_ms"] == 2.0
