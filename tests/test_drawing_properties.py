"""drawing_properties_get / set: DWG summary and custom properties on both engines.

Headlessly the five summary fields live in the DWG SummaryInfo stream, which
ezdxf cannot write, so any non-null summary field is refused with
`capability: dwgprops` before anything is touched; custom properties are
`$CUSTOMPROPERTYTAG` / `$CUSTOMPROPERTY` header pairs and work on both
engines (measured: they survive save + reopen on R2004 and newer; ezdxf
only emits the pairs for AC1018+, so an older document is refused headlessly
instead of losing the write at save).

Custom keys and values are validated once, by name, against what would break
*either* engine: keys AutoCAD's AddCustomInfo rejects mid-write as
`Invalid key` (measured on AutoCAD 2026 by sweeping every printable ASCII
character through a scratch document: leading/trailing whitespace and exactly
the thirteen characters `" * , / : ; < = > ? \\ ` |` anywhere in the key --
internal spaces, tabs, control characters, a no-break space and unicode are
accepted), case-variant duplicates (AutoCAD's keys are case-insensitive:
`Duplicate key`), and line breaks, which ezdxf writes unescaped and which
corrupt the whole DXF. A delete is exempt from the AddCustomInfo syntax rules
(RemoveCustomByKey never validates: `Key not found`, never `Invalid key`).
"""

from __future__ import annotations

import types

import pytest

from backends.base import UnsupportedCapabilityError
from backends.contracts.settings import (
    _CUSTOM_KEY_FORBIDDEN,
    SUMMARY_FIELDS,
    validate_drawing_properties,
)

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
        # Keys AutoCAD's AddCustomInfo rejects as 'Invalid key' (measured on
        # AutoCAD 2026) -- refused here so the live engine never fails
        # mid-write after the summary and earlier keys were already applied.
        (None, {" BAD ": "x"}, ValueError, "' BAD '"),
        (None, {"PROJECT ": "x"}, ValueError, "trailing whitespace"),
        (None, {"\tTAB": "x"}, ValueError, "leading or trailing whitespace"),
        (None, {"NB\xa0": "x"}, ValueError, "leading or trailing whitespace"),
        (None, {"A=B": "x"}, ValueError, "'A=B' contains '='"),
        (None, {"A;B": "x"}, ValueError, "'A;B' contains ';'"),
        (None, {"A:B/C": "x"}, ValueError, "'A:B/C' contains '/' ':'"),
        # Case-variant duplicates: AutoCAD raises 'Duplicate key' on the second.
        (None, {"Project": "a", "PROJECT": "b"}, ValueError, "differ only by case"),
        (None, {"Project": "a", "PROJECT": None}, ValueError, "differ only by case"),
        (None, {"Gr\u00fcn": "a", "GR\u00dcN": "b"}, ValueError, "differ only by case"),
        # A line break is written unescaped by ezdxf and corrupts the DXF.
        (None, {"NOTE": "line1\nline2"}, ValueError, "custom['NOTE']: the value"),
        (None, {"NOTE": "line1\rline2"}, ValueError, "line break"),
        (None, {"K\nEY": "x"}, ValueError, "the key contains a line break"),
        (None, {"K\nEY": None}, ValueError, "the key contains a line break"),
    ],
)
def test_validation_refuses_by_name(summary, custom, exc, fragment):
    with pytest.raises(exc) as excinfo:
        validate_drawing_properties(summary, custom)
    assert fragment in str(excinfo.value)


INVALID_KEY_CHARS = '"*,/:;<=>?\\`|'


def test_forbidden_set_is_exactly_the_thirteen_measured_characters():
    assert _CUSTOM_KEY_FORBIDDEN == frozenset(INVALID_KEY_CHARS)
    assert len(_CUSTOM_KEY_FORBIDDEN) == 13


@pytest.mark.parametrize("char", sorted(INVALID_KEY_CHARS))
def test_validation_refuses_every_addcustominfo_invalid_key_character(char):
    """Measured on AutoCAD 2026 (`AddCustomInfo(f"A{c}B", "v")` for every
    c in 0x20-0x7E on a scratch document): exactly these thirteen raise
    `Invalid key`. The earlier fix mirrored only `=` and `;`, so 'A:B',
    'REV/2', 'SIZE, mm', 'A"B' ... still half-applied on the live engine."""
    key = f"A{char}B"
    with pytest.raises(ValueError, match="Invalid key") as excinfo:
        validate_drawing_properties(None, {key: "x"})
    assert repr(key) in str(excinfo.value) and repr(char) in str(excinfo.value)


@pytest.mark.parametrize(
    "key",
    [
        "IN SP",
        "T\tAB",
        "\u00dcn\u00efcode",
        "X" * 300,
        "NB\xa0SP",
        "A\x01B",
        "A\x1fB",
        "A!#$%&'()+-.@[]^_{}~B",
        "\u4e2d\u2014\u200b",
    ],
)
def test_validation_accepts_keys_autocad_accepts(key):
    """Measured: internal spaces, tabs, control characters, a no-break space,
    unicode, every other printable ASCII character and 300-char keys pass
    AddCustomInfo; the validator must not be stricter than AutoCAD."""
    assert validate_drawing_properties(None, {key: "v"}) == ({}, {key: "v"}, [])


@pytest.mark.parametrize("key", ["A=B", " PAD ", "A:B", "NB\xa0", 'A"B'])
def test_validation_exempts_a_delete_from_the_addcustominfo_syntax_rules(key):
    """A delete never calls AddCustomInfo, and RemoveCustomByKey does not
    validate syntax (measured: 'Key not found', never 'Invalid key'), so a key
    a DXF script wrote must stay removable. The line-break rule still holds
    for both directions (a delete of such a key is pinned refused above)."""
    assert validate_drawing_properties(None, {key: None}) == ({}, {}, [key])


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


async def test_line_break_is_refused_before_any_write_and_the_file_still_opens(backend, tmp_path):
    """Measured: `custom={'NOTE': 'line1\\nline2', 'AFTER': 'ok'}` reported
    `ok: True`, and the saved DXF failed to reopen with
    `DXFStructureError: Invalid group code "line2"` -- the whole drawing lost,
    reported as success. The request is refused as a whole, so AFTER is not
    written either, and the file saved afterwards reopens."""
    with pytest.raises(ValueError, match=r"custom\['NOTE'\]: the value contains a line break"):
        await backend.drawing_properties_set(custom={"NOTE": "line1\nline2", "AFTER": "ok"})
    assert (await backend.drawing_properties_get())["custom"] == {}
    path = str(tmp_path / "clean.dxf")
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)
    assert (await backend.drawing_properties_get())["custom"] == {}


async def test_autocad_invalid_keys_are_refused_headlessly_too(backend):
    """The headless engine used to write ' BAD ', 'A=B' and 'A;B' without
    complaint (and AutoCAD loaded them from the DXF) while the live engine
    half-applied the same call; both now refuse before writing."""
    with pytest.raises(ValueError, match="' BAD '"):
        await backend.drawing_properties_set(custom={"AAA": "first", " BAD ": "x", "ZZZ": "n"})
    assert (await backend.drawing_properties_get())["custom"] == {}


async def test_legacy_invalid_syntax_keys_stay_deletable_headlessly(tmp_path):
    """Regression of 2b9791f: `_check_custom_key` ran before the `value is
    None` branch, so `{'A=B': None}` on a document carrying 'A=B' (any ezdxf
    script writes such keys; AutoCAD loads them from the DXF) was refused with
    a reason that only applies to AddCustomInfo -- the tool showed a key it
    could never remove."""
    import ezdxf

    from backends.ezdxf_backend import EzdxfBackend

    path = tmp_path / "legacy.dxf"
    doc = ezdxf.new("R2010")
    for tag in ("A=B", "NB\xa0SP", " PAD ", "A:B"):
        doc.header.custom_vars.append(tag, "legacy")
    doc.saveas(str(path))
    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_open(str(path))
        assert (await backend.drawing_properties_get())["custom"] == {
            "A=B": "legacy",
            "NB\xa0SP": "legacy",
            " PAD ": "legacy",
            "A:B": "legacy",
        }
        res = await backend.drawing_properties_set(custom={"A=B": None, " PAD ": None})
        assert res["custom_deleted"] == ["A=B", " PAD "]
        assert (await backend.drawing_properties_get())["custom"] == {
            "NB\xa0SP": "legacy",
            "A:B": "legacy",
        }
        # ...but writing such a key is still refused, before anything is touched.
        with pytest.raises(ValueError, match="'A:B' contains ':'"):
            await backend.drawing_properties_set(custom={"A:B": "new", "OK": "x"})
        assert (await backend.drawing_properties_get())["custom"] == {
            "NB\xa0SP": "legacy",
            "A:B": "legacy",
        }
    finally:
        await backend.disconnect()


async def test_custom_keys_match_case_insensitively_headlessly(backend):
    """Mirrors AutoCAD (measured on 2026): a write to 'PROJECT' over an
    existing 'Project' updates it under the stored spelling instead of adding
    a second tag AutoCAD would call a duplicate; a delete matches either
    spelling."""
    await backend.drawing_properties_set(custom={"Project": "X-1", "Rev": "A"})
    res = await backend.drawing_properties_set(custom={"PROJECT": "X-2", "rev": None})
    assert res["custom_written"] == ["PROJECT"] and res["custom_deleted"] == ["rev"]
    assert list(backend._doc.header.custom_vars) == [("Project", "X-2")]
    assert (await backend.drawing_properties_get())["custom"] == {"Project": "X-2"}


async def _backend_for_version(tmp_path, version: str):
    import ezdxf

    from backends.ezdxf_backend import EzdxfBackend

    path = tmp_path / f"{version}.dxf"
    ezdxf.new(version).saveas(str(path))
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_open(str(path))
    return backend


@pytest.mark.parametrize("version, dxfversion", [("R12", "AC1009"), ("R2000", "AC1015")])
async def test_custom_properties_before_r2004_are_refused_rather_than_lost(
    tmp_path, version, dxfversion
):
    """Measured: on an R12 or R2000 document `drawing_properties_set` reported
    `ok: True, custom_written: ['PROJECT']`, `drawing_properties_get` echoed
    it from memory, and `drawing_save_as` (version kept) wrote nothing --
    ezdxf emits $CUSTOMPROPERTYTAG / $CUSTOMPROPERTY only after $LASTSAVEDBY,
    which does not exist before R2004. The header-only write that vanishes on
    save, the class f3ff703 removed for CANNOSCALE. Refused up front, before
    custom_vars is touched; the reopened file is read back to prove it."""
    backend = await _backend_for_version(tmp_path, version)
    try:
        assert backend._doc.dxfversion == dxfversion
        with pytest.raises(ValueError, match=dxfversion) as excinfo:
            await backend.drawing_properties_set(custom={"PROJECT": "X-1", "REV": "B"})
        assert "R2004 or newer" in str(excinfo.value)
        assert "PROJECT" in str(excinfo.value)
        assert list(backend._doc.header.custom_vars) == []
        assert (await backend.drawing_properties_get())["custom"] == {}
        # A delete on the same document is refused for the same reason: it
        # would report a key removed that the file could never have held.
        with pytest.raises(ValueError, match="R2004 or newer"):
            await backend.drawing_properties_set(custom={"PROJECT": None})
        # An empty request touches nothing and is not a write to refuse.
        res = await backend.drawing_properties_set(custom={})
        assert res["ok"] is True and res["custom_written"] == []

        out = tmp_path / f"{version}_out.dxf"
        await backend.drawing_save_as(str(out))
        await backend.drawing_open(str(out))
        assert backend._doc.dxfversion == dxfversion
        assert (await backend.drawing_properties_get())["custom"] == {}
    finally:
        await backend.disconnect()


async def test_custom_properties_on_r2004_survive_save_and_reopen(tmp_path):
    """The oldest version that carries the pairs -- the boundary the refusal
    sits on -- round-trips."""
    backend = await _backend_for_version(tmp_path, "R2004")
    try:
        res = await backend.drawing_properties_set(custom={"PROJECT": "X-1"})
        assert res["custom_written"] == ["PROJECT"]
        out = tmp_path / "r2004_out.dxf"
        await backend.drawing_save_as(str(out))
        await backend.drawing_open(str(out))
        assert backend._doc.dxfversion == "AC1018"
        assert (await backend.drawing_properties_get())["custom"] == {"PROJECT": "X-1"}
    finally:
        await backend.disconnect()


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

    # Measured on AutoCAD 2026: the three mutators match keys case-insensitively,
    # an existing key keeps its stored spelling, a second Add is 'Duplicate key',
    # a Set/Remove of an absent key is 'Key not found'.

    def _index(self, key):
        for i, (k, _v) in enumerate(self._custom):
            if k.casefold() == key.casefold():
                return i
        return None

    def AddCustomInfo(self, key, value):
        self.calls.append(("AddCustomInfo", key, value))
        if self._index(key) is not None:
            raise RuntimeError("AutoCAD COM error: Duplicate key")
        self._custom.append((key, value))

    def SetCustomByKey(self, key, value):
        self.calls.append(("SetCustomByKey", key, value))
        i = self._index(key)
        if i is None:
            raise RuntimeError("AutoCAD COM error: Key not found")
        self._custom[i] = (self._custom[i][0], value)

    def RemoveCustomByKey(self, key):
        self.calls.append(("RemoveCustomByKey", key))
        i = self._index(key)
        if i is None:
            raise RuntimeError("AutoCAD COM error: Key not found")
        del self._custom[i]


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


@pytest.mark.parametrize(
    "key", ["A:B", "REV/2", "SIZE, mm", 'A"B', "A\\B", "A<B>", "A|B", "A*B", "A?B"]
)
async def test_com_refuses_every_addcustominfo_invalid_key_before_any_write(com_backend, key):
    """Re-measured on AutoCAD 2026 after 2b9791f: `summary={'subject':
    'PARTIAL?'}, custom={'AAA': 'first', 'A:B': 'x', 'ZZZ': 'never'}` still
    raised from AddCustomInfo('A:B') and left subject='PARTIAL?',
    custom={'AAA': 'first'} -- the same half-write for every character the
    first fix did not mirror. Nothing reaches SummaryInfo now."""
    backend, info = com_backend
    with pytest.raises(ValueError, match="Invalid key"):
        await backend.drawing_properties_set(
            summary={"subject": "PARTIAL?"},
            custom={"AAA": "first", key: "x", "ZZZ": "never"},
        )
    assert info.calls == [] and info.Subject == ""
    assert info._custom == [("PROJECT", "X-1"), ("OLD", "1")]


async def test_com_deletes_a_legacy_invalid_syntax_key(com_backend):
    """RemoveCustomByKey does not validate syntax (measured: 'Key not found'
    for an absent 'A=B', never 'Invalid key'), so a key AutoCAD loaded from a
    DXF is removed, not refused."""
    backend, info = com_backend
    info._custom.append(("A=B", "legacy"))
    res = await backend.drawing_properties_set(custom={"A=B": None})
    assert res["custom_deleted"] == ["A=B"]
    assert ("RemoveCustomByKey", "A=B") in info.calls
    assert info._custom == [("PROJECT", "X-1"), ("OLD", "1")]


async def test_com_routes_case_variant_keys_to_set_and_remove(com_backend):
    """Measured on AutoCAD 2026: AddCustomInfo('PROJECT') over an existing
    'Project' raises 'Duplicate key' -- a case-sensitive existence check
    routed it there and failed mid-write. Existence is matched casefolded."""
    backend, info = com_backend
    res = await backend.drawing_properties_set(custom={"project": "X-2", "old": None, "New": "n"})
    assert res["custom_written"] == ["New", "project"] and res["custom_deleted"] == ["old"]
    mutations = [c for c in info.calls if c[0] not in ("NumCustomInfo", "GetCustomByIndex")]
    assert mutations == [
        ("SetCustomByKey", "project", "X-2"),
        ("AddCustomInfo", "New", "n"),
        ("RemoveCustomByKey", "old"),
    ]
    assert info._custom == [("PROJECT", "X-2"), ("New", "n")], "stored spelling kept"


async def test_com_refuses_autocad_invalid_keys_before_any_write(com_backend):
    """Measured on AutoCAD 2026: `summary={'subject': 'PARTIAL?'},
    custom={'AAA': 'first', ' BAD ': 'x', 'ZZZ': 'never'}` raised an unnamed
    COM error from AddCustomInfo(' BAD ') and left subject and AAA written,
    ZZZ absent -- a half-applied write reported as an error. The shared
    validator now names the key and nothing reaches SummaryInfo."""
    backend, info = com_backend
    with pytest.raises(ValueError, match="' BAD '"):
        await backend.drawing_properties_set(
            summary={"subject": "PARTIAL?"},
            custom={"AAA": "first", " BAD ": "x", "ZZZ": "never"},
        )
    assert info.calls == [] and info.Subject == ""
    assert info._custom == [("PROJECT", "X-1"), ("OLD", "1")]
