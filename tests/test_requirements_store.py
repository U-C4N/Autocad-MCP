"""B15 residue - drawing_plan(persist_requirements=True) and the ACADMCP_REQUIREMENTS record.

The set is written into the drawing (an XRECORD ``CURRENT`` inside the
``ACADMCP_REQUIREMENTS`` dictionary, the layer-states pattern), so a saved and
reopened file carries its requirements. The live path runs against a fake
ActiveX surface that models only members measured on AutoCAD 2026 (the
layer-states fakes: ``Dictionaries.Item`` / ``Add``, ``AddXRecord``,
``SetXRecordData`` / ``GetXRecordData``, ``Count`` / ``Item`` / ``GetName``,
and the makepy narrowing of ``Dictionaries.Item``).
"""

from __future__ import annotations

import ezdxf
import pytest
from fastmcp import Client

import server
from engineering.verification.requirements import (
    load_requirements,
    parse_requirements,
    store_requirements,
)
from engineering.verification.types import thaw

pytestmark = pytest.mark.asyncio

_ITEMS = [
    {
        "id": "D1",
        "quantity": "diameter",
        "target": {"nominal": 40},
        "features": [{"kind": "feature_tag", "part": "flange", "feature": "bore"}],
        "provenance": "explicit",
        "manufacturing_tolerance": {"fit": "H7", "source": "drawing note 2"},
    },
    {
        "id": "N1",
        "quantity": "count",
        "target": {"exact": 6},
        "features": [{"kind": "predicate", "type": "circle", "radius_min": 9, "radius_max": 11}],
        "provenance": "explicit",
    },
]
_RAW = {"units": "mm", "part_type": "flange", "tolerance_policy": "ISO 2768-m", "items": _ITEMS}


async def test_plan_accepts_the_hash_of_a_typed_preflight(backend):
    preflight = await backend.drawing_preflight("flange", _RAW)
    plan = await backend.drawing_plan(
        "flange",
        requirements=preflight.normalized_spec["requirements"],
        spec_hash=preflight.spec_hash,
    )
    assert plan.preflight_status == "ready"
    expected = parse_requirements({"units": "mm", "items": _ITEMS})
    assert plan.requirement_digest == expected.digest()
    assert plan.requirements_persisted is False
    assert await load_requirements(backend) is None, "nothing is written without the flag"


async def test_a_persisted_set_travels_with_the_saved_file(backend, tmp_path):
    preflight = await backend.drawing_preflight("flange", _RAW)
    assert preflight.ready is True
    before = backend.document_identity()["revision"]
    plan = await backend.drawing_plan(
        "flange",
        requirements=preflight.normalized_spec["requirements"],
        spec_hash=preflight.spec_hash,
        persist_requirements=True,
    )
    expected = parse_requirements({"units": "mm", "items": _ITEMS})
    assert plan.requirements_persisted is True
    assert plan.requirement_digest == expected.digest()
    assert backend.document_identity()["revision"] == before + 1, "the record is content"

    path = tmp_path / "flange.dxf"
    await backend.drawing_save_as(str(path))
    await backend.drawing_open(str(path))
    assert await load_requirements(backend) == expected

    record = ezdxf.readfile(str(path)).rootdict.get("ACADMCP_REQUIREMENTS").get("CURRENT")
    chunks = [tag.value for tag in record.tags if tag.code == 1000]
    assert chunks and all(len(chunk) <= 255 and chunk.isascii() for chunk in chunks)


async def test_storing_again_replaces_the_one_record(backend):
    rs = parse_requirements({"units": "mm", "items": _ITEMS})
    first = await store_requirements(backend, rs)
    second = await store_requirements(backend, rs)
    assert (first["replaced"], second["replaced"]) == (False, True)
    assert (first["digest"], first["items"], first["backend"]) == (rs.digest(), 2, "ezdxf")
    holder = backend._doc.rootdict.get("ACADMCP_REQUIREMENTS")
    assert list(holder.keys()) == ["CURRENT"]


async def test_persisting_nothing_typed_is_refused_by_name(backend):
    with pytest.raises(ValueError, match="nothing to read"):
        await backend.drawing_plan(
            "flange", requirements={"units": "mm"}, persist_requirements=True
        )
    assert backend.get_plan_spec() is None
    assert await load_requirements(backend) is None


async def test_a_legacy_only_set_is_stored_as_given(backend):
    plan = await backend.drawing_plan(
        "bracket",
        requirements={"units": "mm", "dimensions": {"width": 80.0}},
        persist_requirements=True,
    )
    loaded = await load_requirements(backend)
    assert loaded.requirements == () and thaw(loaded.legacy) == {"width": 80.0}
    assert plan.requirement_digest == loaded.digest()


# ── live engine, against a fake ActiveX surface ──────────────────────────────


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
    """``IAcadDictionaries.Item`` / ``IAcadDictionary.Item`` with a makepy cache:
    the declared ``IAcadObject`` wrapper, the live object only via ``_oleobj_``."""

    def __init__(self, obj):
        self._oleobj_ = obj

    def __getattr__(self, name):
        raise AttributeError(f"'IAcadObject' object has no attribute {name!r}")


class _FakeDict:
    def __init__(self, name):
        self.Name = name
        self.entries: list[_FakeXRecord] = []
        self.calls: list[tuple[str, tuple]] = []

    @property
    def Count(self):
        return len(self.entries)

    def Item(self, index):
        return _Narrowed(self.entries[index])

    def GetName(self, obj):
        return obj.name

    def AddXRecord(self, keyword):
        self.calls.append(("AddXRecord", (keyword,)))
        record = _FakeXRecord(keyword)
        self.entries.append(record)
        return record


class _FakeDictionaries:
    def __init__(self):
        self.dicts: dict[str, _FakeDict] = {}
        self.calls: list[tuple[str, tuple]] = []

    def Item(self, name):
        self.calls.append(("Item", (name,)))
        if name not in self.dicts:
            raise RuntimeError(f"no dictionary {name}")
        return _Narrowed(self.dicts[name])

    def Add(self, name):
        self.calls.append(("Add", (name,)))
        self.dicts[name] = _FakeDict(name)
        return self.dicts[name]


class _FakeDocument:
    def __init__(self):
        self.Dictionaries = _FakeDictionaries()


@pytest.fixture
def com_store(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    document = _FakeDocument()
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    monkeypatch.setattr(module, "_ai", lambda values: list(values))
    monkeypatch.setattr(module, "_avar", lambda values: list(values))
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    backend._bind_key(("Part.dwg", "C:/work/Part.dwg"))
    return backend, document


async def test_the_live_engine_writes_one_xrecord_and_reads_it_back(com_store):
    backend, document = com_store
    rs = parse_requirements({"units": "mm", "items": _ITEMS})
    stored = await store_requirements(backend, rs)
    assert (stored["replaced"], stored["backend"]) == (False, "com")
    assert document.Dictionaries.calls == [
        ("Item", ("ACADMCP_REQUIREMENTS",)),
        ("Add", ("ACADMCP_REQUIREMENTS",)),
    ]
    holder = document.Dictionaries.dicts["ACADMCP_REQUIREMENTS"]
    (record,) = holder.entries
    assert record.name == "CURRENT" and record.types == [1000] * len(record.values)
    assert all(len(value) <= 255 for value in record.values)
    assert backend.document_identity()["revision"] == 1, "a stored record is a content change"
    assert await load_requirements(backend) == rs
    again = await store_requirements(backend, rs)
    assert again["replaced"] is True and len(holder.entries) == 1


async def test_the_tool_takes_persist_requirements():
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        plan = (
            await client.call_tool(
                "drawing_plan",
                {
                    "intent": "flange",
                    "requirements": {"units": "mm", "items": _ITEMS},
                    "persist_requirements": True,
                },
            )
        ).structured_content
    expected = parse_requirements({"units": "mm", "items": _ITEMS})
    assert plan["requirements_persisted"] is True
    assert plan["requirement_digest"] == expected.digest()
