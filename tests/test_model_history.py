"""The model history a revision writes into the drawing (plan item 1.8-19).

Five revisions of the bore; the value at revision 2 is read back by its path,
in one call, from the ``ACADMCP_MODEL_HISTORY`` XRECORD - which survives a
save and a reopen, and keeps the newest 200 entries.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

import server
from backends.ezdxf_backend import EzdxfBackend
from backends.operations import CURRENT_OPERATION
from engineering.mech import history as H
from engineering.mech.draw import draw_part
from engineering.mech.part import build_part
from engineering.mech.revise import inspect, revise_part

pytestmark = pytest.mark.asyncio

SHAFT = {
    "kind": "revolved",
    "name": "bushing",
    "material": "steel",
    "segments": [{"length": 30.0, "d_outer": 60.0}],
    "features": [{"kind": "axial_bore", "id": "bore", "diameter": 20.0}],
}
ROUNDS = (21.0, 22.0, 23.0, 24.0, 25.0)


async def _drawn():
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    drawn = await draw_part(b, build_part(SHAFT), views=("front", "side"))
    return b, drawn["part_id"]


async def test_five_rounds_and_the_round_two_value_by_param(outside_repo_dir):
    b, pid = await _drawn()
    for number, value in enumerate(ROUNDS, start=1):
        token = CURRENT_OPERATION.set(f"op-{number}")
        try:
            result = await revise_part(b, pid, {"features.bore.diameter": value})
        finally:
            CURRENT_OPERATION.reset(token)
        assert result["rev"] == number

    read = await inspect(b, pid, drift=False, history=False, param="features.bore.diameter")
    values = read["param"]["values"]
    assert [row["rev"] for row in values] == [1, 2, 3, 4, 5]
    assert [row["new"] for row in values] == list(ROUNDS)
    assert [row["old"] for row in values] == [20.0, *ROUNDS[:-1]]
    round_two = next(row for row in values if row["rev"] == 2)
    assert round_two["new"] == 22.0 and round_two["operation_id"] == "op-2"
    assert round_two["at"].endswith("Z")

    full = await inspect(b, pid, drift=False, history=True, param=None)
    assert [e["rev"] for e in full["history"]] == [1, 2, 3, 4, 5]
    assert full["history"][0]["changes"] == {"features.bore.diameter": [20.0, 21.0]}
    both = await inspect(b, pid, drift=False, history=True, param="features.bore")
    assert len(both["history"]) == 5 and len(both["param"]["values"]) == 5

    # the record travels with the file
    path = outside_repo_dir / "history.dxf"
    await b.drawing_save_as(str(path))
    reopened = EzdxfBackend()
    await reopened.connect()
    await reopened.drawing_open(str(path))
    again = await H.load_history(reopened, pid)
    assert [e["rev"] for e in again["entries"]] == [1, 2, 3, 4, 5]
    assert H.param_values(again, "features.bore.diameter")[1]["new"] == 22.0
    # and a later revision on the reopened file continues the numbering
    result = await revise_part(reopened, pid, {"features.bore.diameter": 26.0})
    assert result["rev"] == 6


async def test_a_part_with_no_revision_has_an_empty_history():
    b, pid = await _drawn()
    read = await inspect(b, pid, drift=False, history=True, param=None)
    assert read["history"] == [] and read["history_dropped"] == 0
    assert (await inspect(b, pid, drift=False, history=False, param="x"))["param"]["values"] == []


async def test_the_history_keeps_the_newest_two_hundred_entries():
    b, pid = await _drawn()
    for number in range(H.MAX_ENTRIES + 5):
        await H.append_history(b, pid, {"name": [str(number), str(number + 1)]}, None)
    stored = await H.load_history(b, pid)
    assert len(stored["entries"]) == H.MAX_ENTRIES
    assert stored["dropped"] == 5
    assert stored["entries"][0]["rev"] == 6 and stored["entries"][-1]["rev"] == H.MAX_ENTRIES + 5


async def test_a_refused_revision_writes_no_history():
    b, pid = await _drawn()
    with pytest.raises(Exception):  # noqa: B017 - any refusal; the point is what is stored
        await revise_part(b, pid, {"features.bore.diameter": 90.0})
    assert (await H.load_history(b, pid))["entries"] == []


async def test_the_history_rides_inside_the_revision_transaction(monkeypatch):
    b, pid = await _drawn()

    async def broken(*_args, **_kwargs):
        raise RuntimeError("the history write broke")

    monkeypatch.setattr(H, "append_history", broken)
    with pytest.raises(RuntimeError, match="history write broke"):
        await revise_part(b, pid, {"features.bore.diameter": 24.0})
    monkeypatch.undo()
    read = await inspect(b, pid, drift=True, history=True, param=None)
    assert read["part"]["features"][0]["diameter"] == 20.0, "rolled back with the geometry"
    assert read["history"] == [] and read["model_is_current"] is True


# -- the live engine, against a fake ActiveX surface (tests/test_requirements_store.py's) --


class _FakeXRecord:
    def __init__(self, name):
        self.name = name
        self.types = None
        self.values = None

    def SetXRecordData(self, types_, values):
        self.types, self.values = list(types_), list(values)

    def GetXRecordData(self):
        return self.types, self.values


class _Narrowed:
    def __init__(self, obj):
        self._oleobj_ = obj

    def __getattr__(self, name):
        raise AttributeError(f"'IAcadObject' object has no attribute {name!r}")


class _FakeDict:
    def __init__(self, name):
        self.Name = name
        self.entries: list[_FakeXRecord] = []

    @property
    def Count(self):
        return len(self.entries)

    def Item(self, index):
        return _Narrowed(self.entries[index])

    def GetName(self, obj):
        return obj.name

    def AddXRecord(self, keyword):
        record = _FakeXRecord(keyword)
        self.entries.append(record)
        return record


class _FakeDictionaries:
    def __init__(self):
        self.dicts: dict[str, _FakeDict] = {}

    def Item(self, name):
        if name not in self.dicts:
            raise RuntimeError(f"no dictionary {name}")
        return _Narrowed(self.dicts[name])

    def Add(self, name):
        self.dicts[name] = _FakeDict(name)
        return self.dicts[name]


class _FakeDocument:
    def __init__(self):
        self.Dictionaries = _FakeDictionaries()


async def test_the_live_engine_keeps_the_history_in_one_xrecord_per_part(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    document = _FakeDocument()
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    monkeypatch.setattr(module, "_ai", lambda values: list(values))
    monkeypatch.setattr(module, "_avar", lambda values: list(values))
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        module._note_reached()
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    backend._bind_key(("Part.dwg", "C:/work/Part.dwg"))

    assert (await H.load_history(backend, "2A"))["entries"] == []
    for value in (22.0, 24.0):
        await H.append_history(backend, "2A", {"features.bore.diameter": [20.0, value]}, "op")
    holder = document.Dictionaries.dicts[H.DICT_NAME]
    (record,) = holder.entries
    assert record.name == "2A" and record.types == [1000] * len(record.values)
    assert all(len(value) <= 255 for value in record.values)
    assert backend.document_identity()["revision"] == 2, "each entry is a content change"
    stored = await H.load_history(backend, "2A")
    assert [e["rev"] for e in stored["entries"]] == [1, 2]
    assert H.param_values(stored, "features.bore.diameter")[1]["new"] == 24.0


async def test_revise_reaches_only_contract_members_and_the_shared_readers():
    """Both engines by construction: what revise_part touches on its backend is
    the generic contract, plus the private seams the snapshot, preview and
    history readers use on the headless engine - each with a live twin
    (``Document.Export`` / ``_run`` + ``_acad_doc``)."""
    from backends.base import AutoCADBackend

    b, pid = await _drawn()
    seen: set[str] = set()

    class Recording:
        def __getattr__(self, name):
            seen.add(name)
            return getattr(b, name)

    await revise_part(Recording(), pid, {"features.bore.diameter": 24.0})
    private = {name for name in seen if name.startswith("_")}
    public = seen - private
    contract = {name for name in dir(AutoCADBackend) if not name.startswith("_")}
    # refresh_document_identity: the live engine's optional hook, asked for by
    # getattr in engineering/changes/preview.py::read_identity
    assert public <= contract | {"name", "refresh_document_identity"}, public - contract
    assert private <= {"_async", "_require_doc", "_mark_dirty", "_doc", "_doc_path"}, private


async def test_the_inspect_tool_serves_the_history_and_the_operation_id(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        drawn = (await client.call_tool("mech_part_draw", {"spec": SHAFT})).structured_content
        pid = drawn["part_id"]
        for value in (22, 24):
            await client.call_tool(
                "mech_part_revise", {"part_id": pid, "changes": {"features.bore.diameter": value}}
            )
        read = (
            await client.call_tool("mech_part_inspect", {"part_id": pid, "history": True})
        ).structured_content
    entries = read["history"]
    assert [e["changes"]["features.bore.diameter"][1] for e in entries] == [22, 24]
    assert all(isinstance(e["operation_id"], str) and e["operation_id"] for e in entries)
