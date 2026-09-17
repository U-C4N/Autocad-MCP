"""SYSVAR_CATALOG: authored facts about AutoCAD system variables, pinned to the engines.

Every `engines["ezdxf"]` claim is checked against the real headless backend
(read, and write of the default where the variable is writable); every
`saved_in` that is not "drawing" is checked to be exactly the set the backend
refuses with `capability: registry_sysvar`. The catalogue cannot say a thing
the backend does not do.
"""

from __future__ import annotations

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

#: The two engines legitimately represent these differently (a character vs.
#: its code; a colour name vs. its ACI number), so their default cannot be
#: written verbatim headlessly. Everything else round-trips its default.
_REPRESENTATION_DIFFERS = {"DIMDSEP", "CECOLOR"}


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
async def test_every_writable_ezdxf_true_entry_round_trips_its_default(backend):
    for name, var in SYSVAR_CATALOG.items():
        if not var.engines["ezdxf"] or var.read_only or name in _REPRESENTATION_DIFFERS:
            continue
        await backend.system_set_variable(name, var.default)
        back = await backend.system_get_variable(name)
        if var.type == "point":
            assert tuple(float(v) for v in back[:2]) == tuple(float(v) for v in var.default), name
        elif var.type in ("bool", "int", "enum"):
            assert int(back) == int(var.default), name
        elif var.type == "float":
            assert float(back) == pytest.approx(float(var.default)), name
        else:
            assert back == var.default, name


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

    class _App:
        def GetVariable(self, name):
            calls.append(("GetVariable", name))
            return 2

        def SetVariable(self, name, value):
            calls.append(("SetVariable", name, value))

    monkeypatch.setattr(module, "_acad_app", lambda: _App())
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
