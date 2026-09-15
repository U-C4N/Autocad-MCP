"""XDATA round trips, typing by group code, limits, removal — both engines."""

from __future__ import annotations

import types

import pytest

from backends.xdata_specs import (
    MAX_BYTES,
    MAX_STRING,
    app_size,
    check_entity_budget,
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


@pytest.mark.parametrize(
    "bad",
    [
        ["1", "2"],  # strings coerce with float() but are not numbers
        ["nan", 1],  # ... and "nan" is the silent wrong number end to end
        [1, "2", 3],
        [float("nan"), 1],
        [1, float("inf")],
        [1, 2, float("-inf")],
    ],
)
def test_point_coordinates_must_be_finite_numbers(bad):
    with pytest.raises((TypeError, ValueError), match=r"values\[0\]\[\d\]"):
        encode_values([bad])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_refused(bad):
    # ezdxf writes a 1040 real verbatim, so nan/inf would land in the file
    # (and every undo snapshot) as literal text that is not a DXF double.
    with pytest.raises(ValueError, match=r"values\[1\] must be finite"):
        encode_values([1.5, bad])


def test_integer_coordinates_still_encode_as_reals():
    assert encode_values([[1, 2], [3, 4, 5]]) == [(1010, (1.0, 2.0, 0.0)), (1010, (3.0, 4.0, 5.0))]


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


def test_entity_budget_counts_every_app_and_the_markers():
    # AutoCAD's 16 KB is per object across all apps (xdroom/xdsize), so the
    # gate must add what the entity already carries under other apps, ACAD's
    # own DSTYLE overrides included, plus each app's 1001 marker.
    big = encode_values(["x" * 255] * 40)  # 40 * 258 = 10320 bytes + marker
    assert app_size("APP_A", big) == 40 * 258 + 3 + len("APP_A")
    assert check_entity_budget("APP_A", big, {}) == app_size("APP_A", big)
    # Replacing the same app does not double-count its old payload.
    assert check_entity_budget("APP_A", big, {"APP_A": big}) == app_size("APP_A", big)
    # A second app of the same size no longer fits next to the first.
    with pytest.raises(ValueError, match=r"other applications.*16 KB per entity"):
        check_entity_budget("APP_B", big, {"APP_A": big})
    # Removal is never refused, even when what is left is already over the
    # limit (a file opened with too much XDATA must still be shrinkable).
    assert check_entity_budget("APP_B", [], {"APP_A": big}) == app_size("APP_A", big)
    assert check_entity_budget("APP_A", [], {"APP_A": big, "APP_B": big}) == app_size("APP_B", big)
    # Existing tags may carry their own 1001 marker (ezdxf stores it); it is
    # counted once either way.
    with_marker = [(1001, "APP_A"), *big]
    assert app_size("APP_A", with_marker) == app_size("APP_A", big)
    assert check_entity_budget("APP_B", [], {"APP_A": with_marker}) == app_size("APP_A", big)


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


async def test_non_finite_and_string_coordinates_are_refused_before_writing(backend):
    line = await backend.entity_create_line(0, 0, 1, 1)
    with pytest.raises(TypeError, match=r"values\[0\]\[0\] must be a number"):
        await backend.entity_set_xdata(line.handle, "APP_A", [["nan", 1]])
    with pytest.raises(ValueError, match=r"values\[0\] must be finite"):
        await backend.entity_set_xdata(line.handle, "APP_A", [float("nan")])
    with pytest.raises(ValueError, match=r"values\[1\] must be finite"):
        await backend.entity_set_xdata(line.handle, "APP_A", [1.0, float("inf")])
    assert (await backend.entity_get_xdata(line.handle))["xdata"] == {}


async def test_16k_is_per_entity_across_apps_not_per_call(backend, tmp_path):
    # Three 15 KB payloads under three apps each fit on their own; the entity
    # does not, and AutoCAD's SetXData would refuse the second one live.
    line = await backend.entity_create_line(0, 0, 1, 1)
    payload = ["x" * 255] * 60
    first = await backend.entity_set_xdata(line.handle, "APP_A", payload)
    assert first["ok"] is True and first["bytes"] == 60 * 258 + 3 + len("APP_A")
    with pytest.raises(ValueError, match=r"other applications.*16 KB per entity"):
        await backend.entity_set_xdata(line.handle, "APP_B", payload)
    with pytest.raises(ValueError, match="16 KB per entity"):
        await backend.entity_set_xdata(line.handle, "APP_C", ["y" * 255] * 5)
    got = (await backend.entity_get_xdata(line.handle))["xdata"]
    assert set(got) == {"APP_A"} and "APP_B" not in backend._doc.appids
    # Replacing APP_A wholesale is judged against the entity *without* its old
    # APP_A payload, so a same-size rewrite is fine ...
    await backend.entity_set_xdata(line.handle, "APP_A", ["z" * 255] * 60)
    # ... and once APP_A shrinks, APP_B fits.
    await backend.entity_set_xdata(line.handle, "APP_A", ["small"])
    await backend.entity_set_xdata(line.handle, "APP_B", payload)
    got = (await backend.entity_get_xdata(line.handle))["xdata"]
    assert got["APP_A"] == ["small"] and len(got["APP_B"]) == 60
    path = str(tmp_path / "budget.dxf")
    await backend.drawing_save(path)
    await backend.drawing_open(path)
    assert len((await backend.entity_get_xdata(line.handle, "APP_B"))["xdata"]["APP_B"]) == 60


async def test_budget_counts_acad_xdata_the_dimension_tools_write(backend):
    # A toleranced dimension carries DSTYLE overrides under the ACAD app; the
    # live engine counts them against the 16 KB and so must the gate.
    dim = await backend.dimension_linear(
        0, 0, 100, 0, 0, 20, tol_upper=0.1, tol_lower=-0.1, tol_mode="deviation"
    )
    acad = (await backend.entity_get_xdata(dim.handle))["xdata"]
    assert "ACAD" in acad and acad["ACAD"], acad
    ent = backend._get_entity(dim.handle)
    acad_bytes = app_size("ACAD", [(t.code, t.value) for t in ent.xdata.data["ACAD"]])
    assert acad_bytes > 0
    # A payload that fits an empty entity but not one carrying the override.
    base = ["x" * 255] * 63
    slack = MAX_BYTES - app_size("APP_A", encode_values(base))
    assert 3 < acad_bytes < slack
    filler = [*base, "y" * (slack - 3 - acad_bytes // 2)]
    assert app_size("APP_A", encode_values(filler)) <= MAX_BYTES
    assert app_size("APP_A", encode_values(filler)) + acad_bytes > MAX_BYTES
    with pytest.raises(ValueError, match=r"other applications.*16 KB per entity"):
        await backend.entity_set_xdata(dim.handle, "APP_A", filler)
    # Nothing was written and the override is untouched.
    assert (await backend.entity_get_xdata(dim.handle))["xdata"] == acad


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


async def test_com_budget_reads_the_whole_stream_before_writing(com_backend):
    backend, entity, registered = com_backend
    # Pretend AutoCAD already holds 15 KB under APP_A (and ACAD overrides).
    entity.stored = (
        [1001, 1070, 1001] + [1000] * 60,
        ["ACAD", 271, "APP_A"] + ["x" * 255] * 60,
    )
    with pytest.raises(ValueError, match=r"other applications.*16 KB per entity"):
        await backend.entity_set_xdata("2F", "APP_B", ["y" * 255] * 5)
    # Refused before RegisteredApplications.Add and SetXData.
    assert registered == [] and entity.stored[1][2] == "APP_A" and len(entity.stored[0]) == 63
    # Rewriting APP_A itself is judged without its old payload.
    result = await backend.entity_set_xdata("2F", "APP_A", ["z" * 255] * 60)
    assert result["ok"] is True and registered == ["APP_A"]
    # Removal always goes through.
    result = await backend.entity_set_xdata("2F", "APP_B", [])
    assert result["removed"] is True and result["bytes"] == 0


async def test_com_newline_is_refused_before_any_activex_call(com_backend):
    backend, entity, registered = com_backend
    with pytest.raises(ValueError, match="line separators are not allowed"):
        await backend.entity_set_xdata("2F", "NOTES", ["a\r\nb"])
    assert entity.stored is None and registered == []
