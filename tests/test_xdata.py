"""XDATA round trips, typing by group code, limits, removal — both engines."""

from __future__ import annotations

import types

import pytest

from backends.xdata_specs import (
    MAX_BYTES,
    MAX_STRING,
    decode_values,
    encode_values,
    split_by_app,
    validate_app_name,
)

pytestmark = pytest.mark.asyncio


# ── pure encoding ────────────────────────────────────────────────────────────


def test_values_are_typed_by_group_code():
    assert encode_values(["json", 7, 2.5, [1, 2], [1, 2, 3]]) == [
        (1000, "json"),
        (1071, 7),
        (1040, 2.5),
        (1010, (1.0, 2.0, 0.0)),
        (1010, (1.0, 2.0, 3.0)),
    ]


def test_decode_is_the_inverse():
    tags = encode_values(["a", 1, 1.5, [3, 4]])
    assert decode_values(tags) == ["a", 1, 1.5, [3.0, 4.0, 0.0]]


@pytest.mark.parametrize("bad", [True, None, {"a": 1}, [1], [1, 2, 3, 4], ["x", "y"], 2**31])
def test_untypeable_values_are_refused(bad):
    with pytest.raises((TypeError, ValueError)):
        encode_values([bad])


def test_string_over_255_is_refused():
    encode_values(["x" * MAX_STRING])
    with pytest.raises(ValueError, match="255"):
        encode_values(["x" * (MAX_STRING + 1)])


@pytest.mark.parametrize("sep", ["\n", "\r", "\r\n"])
def test_line_separators_in_strings_are_refused(sep):
    # DXF is "code\nvalue\n" lines and ezdxf writes 1000 tags verbatim, so a
    # separator inside the value splits the tag pair and corrupts the file.
    with pytest.raises(ValueError, match=r"values\[1\].*line separators are not allowed"):
        encode_values(["fine", f"Inspect weld{sep}before paint"])


def test_other_control_characters_are_still_allowed():
    # Only LF/CR split a DXF tag; tabs and the exotic separators round-trip.
    assert encode_values(["a\tb", "a\x0bb", "a b"]) == [
        (1000, "a\tb"),
        (1000, "a\x0bb"),
        (1000, "a b"),
    ]


def test_total_size_over_16k_is_refused():
    with pytest.raises(ValueError, match="16"):
        encode_values(["x" * 255] * (MAX_BYTES // 255 + 1))


@pytest.mark.parametrize("bad", ["", "1abc", "a-b", "a" * 32, "ACAD"])
def test_bad_app_names_are_refused(bad):
    with pytest.raises(ValueError):
        validate_app_name(bad)


def test_split_by_app_groups_a_flat_activex_stream():
    codes = [1001, 1000, 1071, 1001, 1040]
    values = ["APP_A", "hello", 3, "APP_B", 1.5]
    assert split_by_app(codes, values) == {"APP_A": ["hello", 3], "APP_B": [1.5]}


# ── headless engine ─────────────────────────────────────────────────────────


async def test_roundtrip_registers_the_appid(backend):
    line = await backend.entity_create_line(0, 0, 1, 1)
    result = await backend.entity_set_xdata(line.handle, "ACADMCP_PID", ["json", 42, 1.5, [1, 2]])
    assert result["ok"] is True and result["value_count"] == 4 and result["removed"] is False
    assert "ACADMCP_PID" in backend._doc.appids
    got = await backend.entity_get_xdata(line.handle, "ACADMCP_PID")
    assert got["xdata"] == {"ACADMCP_PID": ["json", 42, 1.5, [1.0, 2.0, 0.0]]}


async def test_get_without_app_returns_every_app(backend):
    line = await backend.entity_create_line(0, 0, 1, 1)
    await backend.entity_set_xdata(line.handle, "APP_A", ["a"])
    await backend.entity_set_xdata(line.handle, "APP_B", ["b"])
    got = await backend.entity_get_xdata(line.handle)
    assert got["xdata"] == {"APP_A": ["a"], "APP_B": ["b"]}


async def test_unknown_app_is_empty_not_an_error(backend):
    line = await backend.entity_create_line(0, 0, 1, 1)
    got = await backend.entity_get_xdata(line.handle, "NOPE")
    assert got["xdata"] == {}


async def test_set_replaces_and_empty_removes(backend):
    line = await backend.entity_create_line(0, 0, 1, 1)
    await backend.entity_set_xdata(line.handle, "APP_A", ["one", "two"])
    await backend.entity_set_xdata(line.handle, "APP_A", ["three"])
    assert (await backend.entity_get_xdata(line.handle, "APP_A"))["xdata"] == {"APP_A": ["three"]}
    result = await backend.entity_set_xdata(line.handle, "APP_A", [])
    assert result["removed"] is True
    assert (await backend.entity_get_xdata(line.handle))["xdata"] == {}


async def test_limits_are_enforced_before_writing(backend):
    line = await backend.entity_create_line(0, 0, 1, 1)
    with pytest.raises(ValueError, match="255"):
        await backend.entity_set_xdata(line.handle, "APP_A", ["x" * 256])
    assert (await backend.entity_get_xdata(line.handle))["xdata"] == {}


async def test_newline_is_refused_before_writing_and_snapshots_still_load(backend, tmp_path):
    # Every undo/transaction snapshot is a DXF save, so an accepted newline
    # would break rollback, not just the file on disk.
    line = await backend.entity_create_line(0, 0, 1, 1)
    with pytest.raises(ValueError, match="line separators are not allowed"):
        await backend.entity_set_xdata(line.handle, "NOTES", ["Inspect weld\nbefore paint"])
    assert (await backend.entity_get_xdata(line.handle))["xdata"] == {}

    await backend.entity_set_xdata(line.handle, "NOTES", ["Inspect weld before paint"])
    await backend.transaction_begin()
    circle = await backend.entity_create_circle(5, 5, 1)
    await backend.transaction_rollback()
    assert (await backend.entity_get_xdata(line.handle, "NOTES"))["xdata"] == {
        "NOTES": ["Inspect weld before paint"]
    }
    with pytest.raises(RuntimeError, match="not found"):
        await backend.entity_get(circle.handle)

    path = str(tmp_path / "notes.dxf")
    await backend.drawing_save(path)
    await backend.drawing_open(path)
    assert (await backend.entity_get_xdata(line.handle, "NOTES"))["xdata"] == {
        "NOTES": ["Inspect weld before paint"]
    }


# ── live engine, fake ActiveX ────────────────────────────────────────────────


class _FakeEntity:
    def __init__(self):
        self.stored: tuple | None = None

    def SetXData(self, types_, values):
        self.stored = (list(types_.value), list(values.value))

    def GetXData(self, app):
        if self.stored is None:
            return ([], [])
        codes, values = self.stored
        if app:
            return (codes, values)
        return (codes, values)


@pytest.fixture
def com_backend(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    entity = _FakeEntity()
    registered: list[str] = []
    document = types.SimpleNamespace(
        HandleToObject=lambda handle: entity,
        RegisteredApplications=types.SimpleNamespace(Add=registered.append),
    )
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, entity, registered


async def test_com_set_writes_the_1001_marker_first(com_backend):
    backend, entity, registered = com_backend
    await backend.entity_set_xdata("2F", "ACADMCP_PID", ["json", 3, [1, 2]])
    assert registered == ["ACADMCP_PID"]
    codes, values = entity.stored
    assert codes == [1001, 1000, 1071, 1010]
    assert values[0] == "ACADMCP_PID" and values[1] == "json" and values[2] == 3
    assert tuple(values[3].value) == (1.0, 2.0, 0.0)


async def test_com_get_splits_by_app(com_backend):
    backend, entity, _ = com_backend
    await backend.entity_set_xdata("2F", "ACADMCP_PID", ["json"])
    got = await backend.entity_get_xdata("2F", "ACADMCP_PID")
    assert got["xdata"] == {"ACADMCP_PID": ["json"]}


async def test_com_empty_values_write_only_the_marker(com_backend):
    backend, entity, _ = com_backend
    result = await backend.entity_set_xdata("2F", "ACADMCP_PID", [])
    assert result["removed"] is True
    assert entity.stored == ([1001], ["ACADMCP_PID"])


async def test_com_newline_is_refused_before_any_activex_call(com_backend):
    backend, entity, registered = com_backend
    with pytest.raises(ValueError, match="line separators are not allowed"):
        await backend.entity_set_xdata("2F", "NOTES", ["a\r\nb"])
    assert entity.stored is None and registered == []
