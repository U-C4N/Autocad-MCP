"""SYSVAR_CATALOG: authored facts about AutoCAD system variables, pinned to the engines.

Every `engines["ezdxf"]` claim is checked against the real headless backend
(read, and - where the variable is writable - a write of a value that differs
from the fresh document's, followed by a save and a reopen, so a header slot
ezdxf holds in memory but never exports cannot pass); every `saved_in` that is
not "drawing" is checked to be exactly the set the backend refuses with
`capability: registry_sysvar`. The catalogue cannot say a thing the backend
does not do.
"""

from __future__ import annotations

import math

import pytest

from backends.base import UnsupportedCapabilityError
from backends.contracts.settings import _SETTING_MAP
from backends.ezdxf_backend import _UNSAVED_SYSVARS
from engineering.standards.sysvars import (
    SAVED_IN,
    SYSVAR_CATALOG,
    SYSVAR_TYPES,
    SysVar,
    check_sysvar_value,
    describe_sysvar,
    search_sysvars,
)

asyncio_test = pytest.mark.asyncio

DIM_WHITELIST = (
    "DIMTXT",
    "DIMASZ",
    "DIMEXE",
    "DIMEXO",
    "DIMGAP",
    "DIMTAD",
    "DIMTIH",
    "DIMTOH",
    "DIMDEC",
    "DIMDSEP",
    "DIMLUNIT",
    "DIMZIN",
    "DIMBLK",
    "DIMTXSTY",
    "DIMLWD",
    "DIMLWE",
    "DIMSCALE",
    "DIMTOL",
    "DIMTP",
    "DIMTM",
    "DIMTOLJ",
    "DIMTFAC",
    "DIMLFAC",
    "DIMRND",
    "DIMATFIT",
    "DIMTMOVE",
    "DIMCLRD",
    "DIMCLRE",
    "DIMCLRT",
    "DIMSAH",
    "DIMBLK1",
    "DIMBLK2",
    "DIMCEN",
)

SPEC_NAMES = (
    "LTSCALE PSLTSCALE INSUNITS LUNITS LUPREC AUNITS AUPREC LIMMIN LIMMAX GRIDMODE GRIDUNIT "
    "SNAPMODE SNAPUNIT ORTHOMODE POLARMODE POLARANG OSMODE CANNOSCALE TEXTSIZE TEXTSTYLE "
    "DIMSTYLE CLAYER CELTYPE CECOLOR CELWEIGHT PDMODE PDSIZE FILLETRAD MIRRTEXT ATTREQ ATTDIA "
    "TILEMODE CTAB DWGNAME DWGPREFIX SAVETIME FILEDIA CMDECHO HIGHLIGHT REGENMODE UCSNAME "
    "VIEWCTR VIEWSIZE ANGBASE ANGDIR PLINEWID HPNAME HPSCALE HPANG LWDISPLAY XREFCTL PROXYSHOW "
    "ISOLINES FACETRES DISPSILH"
).split()

#: A value that is not the fresh document's, for every writable headless entry
#: the generic rule below cannot pick one for: the two engines represent DIMDSEP
#: and CECOLOR differently (a character vs. its code; a colour name vs. its ACI
#: number), so the headless form is written; the name-valued ones must name
#: something the drawing has (`drawing_new` bootstraps GEOMETRY and CENTER;
#: `_set_annotation_scale` seeds AutoCAD's default scale list).
_PROBE_VALUES: dict[str, object] = {
    "DIMDSEP": 44,  # ',' as the character code the header holds (a fresh document has 46)
    "CECOLOR": 1,  # ACI red; the header holds the number
    "CLAYER": "GEOMETRY",
    "CELTYPE": "CENTER",
    "CANNOSCALE": "1:2",
}


def _probe_value(var: SysVar, fresh):
    """A legal value for ``var`` that differs from ``fresh``, so the write is not a no-op."""
    if var.name in _PROBE_VALUES:
        return _PROBE_VALUES[var.name]
    if var.type == "bool":
        return 0 if int(fresh) else 1
    if var.type == "enum":
        return next(code for code in var.enum if code != int(fresh))
    if var.type in ("int", "float"):
        step = 1 if var.type == "int" else 1.0
        low, high = var.range if var.range is not None else (-math.inf, math.inf)
        candidate = fresh + step
        return candidate if candidate <= high else fresh - step
    if var.type == "point":
        return (float(fresh[0]) + 1.0, float(fresh[1]) + 2.0)
    return f"{fresh}_PROBE"


def _same(var: SysVar, back, expected) -> bool:
    if var.type == "point":
        return tuple(float(v) for v in back[:2]) == tuple(float(v) for v in expected[:2])
    if var.type in ("bool", "int", "enum"):
        return int(back) == int(expected)
    if var.type == "float":
        return float(back) == pytest.approx(float(expected))
    return back == expected


# ── the data is complete ────────────────────────────────────────────────────


def test_catalogue_is_authored_not_sparse():
    assert len(SYSVAR_CATALOG) >= 60


def test_every_dim_whitelist_variable_and_every_spec_name_is_present():
    missing = [n for n in (*DIM_WHITELIST, *SPEC_NAMES) if n not in SYSVAR_CATALOG]
    assert missing == []


def test_every_entry_is_complete_and_well_typed():
    for name, var in SYSVAR_CATALOG.items():
        assert isinstance(var, SysVar) and var.name == name
        assert var.type in SYSVAR_TYPES, name
        assert var.meaning.strip(), name
        assert var.saved_in in SAVED_IN, name
        assert set(var.engines) == {"ezdxf", "com"}, name
        assert var.friendly_key is None or var.friendly_key in _SETTING_MAP, name
        if var.type == "enum":
            assert var.enum and all(isinstance(k, int) for k in var.enum), name
            assert var.default in var.enum, name
        else:
            assert var.enum is None, name
        if var.range is not None:
            low, high = var.range
            assert low < high, name


def test_friendly_keys_cover_the_whole_facade():
    covered = {var.friendly_key for var in SYSVAR_CATALOG.values() if var.friendly_key}
    assert covered == set(_SETTING_MAP)
    for key, (variable, _kind) in _SETTING_MAP.items():
        assert SYSVAR_CATALOG[variable].friendly_key == key, (key, variable)


def test_non_drawing_variables_are_exactly_the_ones_the_headless_engine_refuses():
    unsaved = {n for n, v in SYSVAR_CATALOG.items() if v.saved_in != "drawing"}
    assert unsaved == set(_UNSAVED_SYSVARS)
    for name in unsaved:
        assert SYSVAR_CATALOG[name].engines["ezdxf"] is False, name


# ── describe / search / check ───────────────────────────────────────────────


def test_describe_is_case_insensitive_and_complete():
    row = describe_sysvar("ltscale")
    assert row["known"] is True and row["name"] == "LTSCALE"
    assert row["type"] == "float" and row["range"][0] > 0
    assert row["saved_in"] == "drawing"
    assert row["engines"] == {"ezdxf": True, "com": True}
    assert row["friendly_key"] == "ltscale"
    assert describe_sysvar("$LIMMAX")["default"] == [420.0, 297.0]


def test_describe_unknown_names_the_nearest_without_raising():
    row = describe_sysvar("LTSCAL")
    assert row["known"] is False and row["name"] == "LTSCAL"
    assert "LTSCALE" in row["nearest"]
    assert describe_sysvar("")["known"] is False


def test_search_matches_names_and_meanings():
    names = [row["name"] for row in search_sysvars("scale")]
    for expected in ("LTSCALE", "DIMSCALE", "CANNOSCALE", "PSLTSCALE", "HPSCALE"):
        assert expected in names
    assert [row["name"] for row in search_sysvars("decimal separator")] == ["DIMDSEP"]
    assert search_sysvars("   ") == []


@pytest.mark.parametrize(
    "name, value, fragment",
    [
        ("DIMDEC", 9, "0..8"),
        ("dimdec", -1, "0..8"),
        ("DIMDEC", 2.5, "integer"),
        ("DIMTXT", "big", "number"),
        ("DIMTXT", 0, "out of range"),
        ("LUNITS", 7, "architectural"),
        ("ORTHOMODE", 2, "0 or 1"),
        ("LIMMAX", 5, "[x, y]"),
        ("DIMSTYLE", "ANSI", "read-only"),
        ("CANNOSCALEVALUE", 0.5, "read-only"),
        ("POLARANG", 0, "out of range"),
    ],
)
def test_check_refuses_with_a_message_naming_the_variable(name, value, fragment):
    message = check_sysvar_value(name, value)
    assert message is not None
    assert name.upper() in message and fragment in message


@pytest.mark.parametrize(
    "name, value",
    [
        ("DIMDEC", 3),
        ("DIMTXT", "3.5"),
        ("LUNITS", 4),
        ("ORTHOMODE", "on"),
        ("LIMMAX", [297, 210]),
        ("DIMDSEP", ","),
        ("NOSUCHVARIABLE", 12345),
    ],
)
def test_check_passes_good_values_and_unknown_names(name, value):
    assert check_sysvar_value(name, value) is None


# ── the engine claims are true ──────────────────────────────────────────────


@asyncio_test
async def test_every_ezdxf_true_entry_reads_on_a_fresh_document(backend):
    unreadable = []
    for name, var in SYSVAR_CATALOG.items():
        if var.engines["ezdxf"] and await backend.system_get_variable(name) is None:
            unreadable.append(name)
    assert unreadable == []


@asyncio_test
async def test_every_writable_ezdxf_true_entry_survives_a_save_and_reopen(backend, tmp_path):
    """The write must land in the file, not only in memory.

    The earlier form of this gate wrote each entry's *default* and read it back
    without saving: 56 of its 66 writes were no-ops (default == fresh value)
    and the rest could not tell a header slot ezdxf exports from one it holds
    in memory and drops at save (`$OSMODE`, R12-only). Every write here moves
    the value, and every read-back is from a reopened file.
    """
    writable = {
        name: var
        for name, var in SYSVAR_CATALOG.items()
        if var.engines["ezdxf"] and not var.read_only
    }
    probes: dict[str, object] = {}
    for name, var in writable.items():
        fresh = await backend.system_get_variable(name)
        probe = _probe_value(var, fresh)
        assert not _same(var, fresh, probe), f"{name}: the probe is a no-op write"
        out = await backend.system_set_variable(name, probe)
        assert out["ok"] is True, name
        probes[name] = probe

    path = str(tmp_path / "sysvars.dxf")
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)

    lost = {}
    for name, probe in probes.items():
        back = await backend.system_get_variable(name)
        if back is None or not _same(writable[name], back, probe):
            lost[name] = (probe, back)
    assert lost == {}, f"engines.ezdxf is True but the value did not survive the file: {lost}"


@asyncio_test
async def test_osmode_is_registry_saved_and_the_headless_engine_says_so(backend):
    """`$OSMODE` is an R12 header variable; ezdxf keeps it in memory and never
    exports it for R2000+, so a write used to report `ok: True` and vanish at
    save. It is registry-saved in AutoCAD and refused headlessly like POLARANG;
    the read reports `None` instead of the template's memory."""
    row = describe_sysvar("OSMODE")
    assert row["saved_in"] == "registry" and row["engines"] == {"ezdxf": False, "com": True}
    assert await backend.system_get_variable("OSMODE") is None
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.system_set_variable("OSMODE", 4134)
    assert excinfo.value.capability == "registry_sysvar"
    # the facade follows: no guess in the snapshot, a per-key refusal on write
    assert (await backend.drawing_settings())["settings"]["osmode"] is None
    res = await backend.drawing_settings({"osmode": 4134})
    assert "registry" in res["errors"]["osmode"]


@asyncio_test
async def test_angbase_is_radians_at_the_boundary_and_degrees_in_the_file(backend, tmp_path):
    """ActiveX `GetVariable("ANGBASE")` and AutoLISP `getvar` hold radians
    (measured: `SETVAR ANGBASE 90` reads back 1.5707963267948966), while the
    DXF header stores `$ANGBASE` in degrees (the same drawing saved by AutoCAD
    carries 90.0). The headless engine translates, so both engines report the
    same number and the catalogue's "radians" is true on both."""
    assert "radians" in describe_sysvar("ANGBASE")["meaning"]
    assert "radians" in describe_sysvar("HPANG")["meaning"]
    await backend.system_set_variable("ANGBASE", math.pi / 2)
    assert backend._doc.header["$ANGBASE"] == pytest.approx(90.0)
    assert await backend.system_get_variable("ANGBASE") == pytest.approx(math.pi / 2)

    path = str(tmp_path / "angbase.dxf")
    await backend.drawing_save_as(path)
    text = open(path, encoding="utf-8").read()
    start = text.index("$ANGBASE")
    assert "90.0" in text[start : start + 40], text[start : start + 40]
    await backend.drawing_open(path)
    assert await backend.system_get_variable("ANGBASE") == pytest.approx(math.pi / 2)


@asyncio_test
async def test_every_ezdxf_false_entry_is_refused_before_any_write(backend):
    for name, var in SYSVAR_CATALOG.items():
        if var.engines["ezdxf"]:
            continue
        if var.saved_in == "drawing":
            with pytest.raises(ValueError, match=name):  # no header slot in ezdxf
                await backend.system_set_variable(name, var.default)
        else:
            with pytest.raises(UnsupportedCapabilityError) as excinfo:
                await backend.system_set_variable(name, var.default)
            assert excinfo.value.capability == "registry_sysvar", name


# ── server tools ────────────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, *a, **k):
        pass


@asyncio_test
async def test_set_variable_tool_refuses_out_of_range_before_the_backend_sees_it(backend):
    from fastmcp.exceptions import ToolError

    import server

    ctx = _FakeCtx(backend)
    with pytest.raises(ToolError, match="DIMDEC"):
        await server.system_set_variable(name="DIMDEC", value=99, ctx=ctx)
    assert await backend.system_get_variable("DIMDEC") == 2
    with pytest.raises(ToolError, match="read-only"):
        await server.system_set_variable(name="DIMSTYLE", value="ANSI", ctx=ctx)
    # a name the catalogue does not know still passes straight through
    out = await server.system_set_variable(name="MEASUREMENT", value=0, ctx=ctx)
    assert out["ok"] is True
    assert await backend.system_get_variable("MEASUREMENT") == 0


@asyncio_test
async def test_set_variable_tool_refuses_before_a_live_backend_is_called(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from fastmcp.exceptions import ToolError

    import server
    from backends import com_backend as module

    calls: list[tuple] = []

    class _Doc:  # the sysvar host is AcadDocument; the Application has no such members
        def GetVariable(self, name):
            calls.append(("GetVariable", name))
            return 2

        def SetVariable(self, name, value):
            calls.append(("SetVariable", name, value))

    monkeypatch.setattr(module, "_acad_doc", lambda: _Doc())
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    with pytest.raises(ToolError, match="DIMDEC"):
        await server.system_set_variable(name="DIMDEC", value=40, ctx=_FakeCtx(backend))
    assert calls == []


@asyncio_test
async def test_describe_tool_returns_the_row_with_the_current_value(backend):
    import server

    ctx = _FakeCtx(backend)
    row = await server.system_variable_describe(name="dimdec", ctx=ctx)
    assert row["known"] is True and row["name"] == "DIMDEC" and row["current"] == 2
    unknown = await server.system_variable_describe(name="DIMDEK", ctx=ctx)
    assert unknown["known"] is False and "DIMDEC" in unknown["nearest"]
    found = await server.system_variable_describe(search="decimal separator", ctx=ctx)
    assert [row["name"] for row in found["matches"]] == ["DIMDSEP"]
    index = await server.system_variable_describe(ctx=ctx)
    assert index["count"] == len(SYSVAR_CATALOG) and "LTSCALE" in index["names"]


@asyncio_test
async def test_describe_tool_works_without_a_backend():
    import server

    class _NoBackendCtx:
        lifespan_context = {"backend": None}

    row = await server.system_variable_describe(name="LTSCALE", ctx=_NoBackendCtx())
    assert row["known"] is True and "current" not in row
