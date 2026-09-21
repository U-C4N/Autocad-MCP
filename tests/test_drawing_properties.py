"""drawing_properties_get / set: DWG summary and custom properties on both engines.

Headlessly the five summary fields live in the DWG SummaryInfo stream, which
ezdxf cannot write, so any non-null summary field is refused with
`capability: dwgprops` before anything is touched; custom properties are
`$CUSTOMPROPERTYTAG` / `$CUSTOMPROPERTY` header pairs and work on both
engines (measured: they survive save + reopen).
"""

from __future__ import annotations

import types

import pytest

from backends.base import UnsupportedCapabilityError
from backends.contracts.settings import SUMMARY_FIELDS, validate_drawing_properties

pytestmark = pytest.mark.asyncio

EMPTY_SUMMARY = {field: None for field in SUMMARY_FIELDS}


# ── shared validation ───────────────────────────────────────────────────────


def test_validation_splits_writes_and_deletes():
    written, to_write, to_delete = validate_drawing_properties(
        {"title": "Gearbox", "author": None}, {"PROJECT": "X-1", "OLD": None}
    )
    assert written == {"title": "Gearbox"}
    assert to_write == {"PROJECT": "X-1"} and to_delete == ["OLD"]
    assert validate_drawing_properties(None, None) == ({}, {}, [])


@pytest.mark.parametrize(
    "summary, custom, exc, fragment",
    [
        ({"edition": "1"}, None, ValueError, "edition"),
        ({"title": 3}, None, TypeError, "summary.title"),
        (None, {"N": 3}, TypeError, "custom['N']"),
        (None, {"": "x"}, TypeError, "non-empty"),
        ("Gearbox", None, TypeError, "summary"),
        (None, ["PROJECT"], TypeError, "custom"),
    ],
)
def test_validation_refuses_by_name(summary, custom, exc, fragment):
    with pytest.raises(exc) as excinfo:
        validate_drawing_properties(summary, custom)
    assert fragment in str(excinfo.value)


# ── headless engine ─────────────────────────────────────────────────────────


async def test_get_reports_no_summary_and_empty_custom(backend):
    assert await backend.drawing_properties_get() == {
        "summary": EMPTY_SUMMARY,
        "summary_available": False,
        "custom": {},
        "backend": "ezdxf",
    }


async def test_custom_properties_write_replace_delete_and_survive_reopen(backend, tmp_path):
    first = await backend.drawing_properties_set(custom={"PROJECT": "X-1", "REV": "B"})
    assert first == {
        "ok": True,
        "summary_written": [],
        "custom_written": ["PROJECT", "REV"],
        "custom_deleted": [],
        "backend": "ezdxf",
    }
    second = await backend.drawing_properties_set(
        custom={"REV": None, "NOPE": None, "PROJECT": "X-2"}
    )
    assert second["custom_written"] == ["PROJECT"]
    assert second["custom_deleted"] == ["REV"], "a key that was never there is not 'deleted'"
    assert (await backend.drawing_properties_get())["custom"] == {"PROJECT": "X-2"}
    assert [t for t, _ in backend._doc.header.custom_vars] == ["PROJECT"], "no duplicate tags"

    path = str(tmp_path / "props.dxf")
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)
    assert (await backend.drawing_properties_get())["custom"] == {"PROJECT": "X-2"}


async def test_summary_is_refused_headlessly_before_any_write(backend):
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.drawing_properties_set(summary={"title": "T"}, custom={"LATE": "no"})
    assert excinfo.value.capability == "dwgprops"
    assert "title" in str(excinfo.value)
    assert (await backend.drawing_properties_get())["custom"] == {}


async def test_all_null_summary_is_not_a_summary_write(backend):
    res = await backend.drawing_properties_set(summary={"title": None, "author": None})
    assert res["ok"] is True and res["summary_written"] == []


async def test_malformed_custom_is_refused_by_name_before_any_write(backend):
    with pytest.raises(TypeError, match=r"custom\['N'\]"):
        await backend.drawing_properties_set(custom={"OK": "1", "N": 3})
    assert (await backend.drawing_properties_get())["custom"] == {}


def test_both_capability_maps_declare_dwgprops():
    from backends.com_backend import ComBackend
    from backends.ezdxf_backend import EzdxfBackend

    assert EzdxfBackend().capabilities().to_dict()["features"]["dwgprops"]["supported"] is False
    assert ComBackend().capabilities().to_dict()["features"]["dwgprops"]["supported"] is True


# ── server tools ────────────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, *a, **k):
        pass


async def test_server_tools_round_trip_custom_and_refuse_summary(backend):
    import server

    ctx = _FakeCtx(backend)
    res = await server.drawing_properties_set(custom={"PROJECT": "X-9"}, ctx=ctx)
    assert res["custom_written"] == ["PROJECT"]
    got = await server.drawing_properties_get(ctx=ctx)
    assert got["custom"] == {"PROJECT": "X-9"} and got["summary_available"] is False
    with pytest.raises(UnsupportedCapabilityError):
        await server.drawing_properties_set(title="Gearbox", ctx=ctx)


# ── live engine, against a fake SummaryInfo ─────────────────────────────────


class _FakeSummaryInfo:
    def __init__(self):
        self.Title = "Old title"
        self.Subject = ""
        self.Author = ""
        self.Keywords = "gear"
        self.Comments = ""
        self._custom: list[tuple[str, str]] = [("PROJECT", "X-1"), ("OLD", "1")]
        self.calls: list[tuple] = []

    def NumCustomInfo(self):
        self.calls.append(("NumCustomInfo",))
        return len(self._custom)

    def GetCustomByIndex(self, index):
        self.calls.append(("GetCustomByIndex", index))
        return self._custom[index]  # pywin32 hands [out] BSTR pairs back as a tuple

    def AddCustomInfo(self, key, value):
        self.calls.append(("AddCustomInfo", key, value))
        self._custom.append((key, value))

    def SetCustomByKey(self, key, value):
        self.calls.append(("SetCustomByKey", key, value))
        self._custom = [(k, value if k == key else v) for k, v in self._custom]

    def RemoveCustomByKey(self, key):
        self.calls.append(("RemoveCustomByKey", key))
        self._custom = [(k, v) for k, v in self._custom if k != key]


@pytest.fixture
def com_backend(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    info = _FakeSummaryInfo()
    document = types.SimpleNamespace(SummaryInfo=info)
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, info


async def test_com_get_reads_summary_and_custom(com_backend):
    backend, info = com_backend
    assert await backend.drawing_properties_get() == {
        "summary": {
            "title": "Old title",
            "subject": "",
            "author": "",
            "keywords": "gear",
            "comments": "",
        },
        "summary_available": True,
        "custom": {"PROJECT": "X-1", "OLD": "1"},
        "backend": "com",
    }


async def test_com_set_writes_summary_and_routes_custom_by_existence(com_backend):
    backend, info = com_backend
    res = await backend.drawing_properties_set(
        summary={"title": "Gearbox", "author": "U. Can", "subject": None},
        custom={"PROJECT": "X-2", "REV": "B", "OLD": None, "NOPE": None},
    )
    assert res == {
        "ok": True,
        "summary_written": ["author", "title"],
        "custom_written": ["PROJECT", "REV"],
        "custom_deleted": ["OLD"],
        "backend": "com",
    }
    assert info.Title == "Gearbox" and info.Author == "U. Can" and info.Keywords == "gear"
    mutations = [c for c in info.calls if c[0] not in ("NumCustomInfo", "GetCustomByIndex")]
    assert mutations == [
        ("SetCustomByKey", "PROJECT", "X-2"),  # existed → set
        ("AddCustomInfo", "REV", "B"),  # new → add
        ("RemoveCustomByKey", "OLD"),  # existed → remove; NOPE never touched
    ]


async def test_com_validates_before_touching_activex(com_backend):
    backend, info = com_backend
    with pytest.raises(TypeError):
        await backend.drawing_properties_set(summary={"title": "T"}, custom={"N": 3})
    with pytest.raises(ValueError):
        await backend.drawing_properties_set(summary={"edition": "1"})
    assert info.calls == [] and info.Title == "Old title"
