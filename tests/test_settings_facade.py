"""drawing_settings, track E: limits, grid/snap, ortho/polar, PSLTSCALE, annotation
scale, unit formats and the current styles — on both engines.

Measured before writing (ezdxf 1.4.4, 2026-09-16):

* `doc.header["$GRIDMODE"]` (and GRIDUNIT / SNAPMODE / SNAPUNIT / POLARMODE /
  POLARANG / CANNOSCALE / CANNOSCALEVALUE) raises `DXFKeyError: Invalid header
  variable` — none of them is a DXF header variable. Grid and snap live on the
  active VPORT; CANNOSCALE is a DICTIONARYVAR in AcDbVariableDictionary; the
  polar pair is registry-saved and has no home in a file at all.
* A `$LIMMAX` written to the header is overwritten at save by
  `Drawing.update_limits()`, which copies the model-space LAYOUT's limits into
  the header — so a header-only write reported success and vanished on reload.
"""

from __future__ import annotations

import math

import pytest

from backends.base import UnsupportedCapabilityError
from backends.contracts.settings import _SETTING_MAP, parse_scale, scale_value

pytestmark = pytest.mark.asyncio

NEW_KEYS = (
    "limits",
    "grid",
    "grid_spacing",
    "snap",
    "snap_spacing",
    "ortho",
    "polar",
    "polar_angle",
    "psltscale",
    "annotation_scale",
    "linear_units",
    "angular_units",
    "dimstyle",
    "textstyle",
)


def _active_vport(backend):
    return backend._doc.viewports.get("*Active")[0]


# ── pure helpers ────────────────────────────────────────────────────────────


def test_parse_scale_canonicalises_and_refuses():
    assert parse_scale("1:50") == ("1:50", 1.0, 50.0)
    assert parse_scale(" 2 : 1 ") == ("2:1", 2.0, 1.0)
    assert parse_scale("1:2.5") == ("1:2.5", 1.0, 2.5)
    assert scale_value("1:50") == pytest.approx(0.02)
    for bad in ("fifty", "1:0", "0:1", "1-50", "", True):
        with pytest.raises(ValueError, match="annotation_scale"):
            parse_scale(bad)


def test_every_new_key_is_in_the_map():
    for key in NEW_KEYS:
        assert key in _SETTING_MAP, key


# ── headless engine ─────────────────────────────────────────────────────────


async def test_snapshot_reports_every_key_including_the_unavailable_ones(backend):
    snap = (await backend.drawing_settings())["settings"]
    for key in NEW_KEYS:
        assert key in snap, key
    assert snap["limits"] == [[0.0, 0.0], [420.0, 297.0]]
    assert snap["grid"] is False and snap["snap"] is False and snap["ortho"] is False
    assert snap["psltscale"] is True
    assert snap["annotation_scale"] == {"name": "1:1", "value": 1.0}
    assert snap["linear_units"] == {"code": 2, "name": "decimal"}
    assert snap["angular_units"] == {"code": 0, "name": "degrees"}
    assert snap["dimstyle"] == "ISO-25"
    assert snap["textstyle"] == "Standard"
    # registry-saved: a file holds no value, and None says so rather than a guess
    assert snap["polar"] is None
    assert snap["polar_angle"] is None


async def test_limits_survive_a_save_and_reopen(backend, tmp_path):
    res = await backend.drawing_settings({"limits": [[0, 0], [500, 350]]})
    assert res["ok"] is True, res.get("errors")
    assert res["changed"]["limits"] == [
        [[0.0, 0.0], [420.0, 297.0]],
        [[0.0, 0.0], [500.0, 350.0]],
    ]
    layout = backend._doc.modelspace().dxf_layout.dxf
    assert tuple(layout.limmax)[:2] == (500.0, 350.0), "the layout is what survives write()"

    path = str(tmp_path / "limits.dxf")
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)
    assert (await backend.drawing_settings())["settings"]["limits"] == [
        [0.0, 0.0],
        [500.0, 350.0],
    ]


async def test_grid_and_snap_live_on_the_active_vport(backend, tmp_path):
    res = await backend.drawing_settings(
        {"grid": True, "grid_spacing": 5, "snap": "on", "snap_spacing": [2.5, 5]}
    )
    assert res["ok"] is True, res.get("errors")
    vport = _active_vport(backend).dxf
    assert vport.grid_on == 1 and vport.snap_on == 1
    assert tuple(vport.grid_spacing)[:2] == (5.0, 5.0)
    assert tuple(vport.snap_spacing)[:2] == (2.5, 5.0)

    snap = (await backend.drawing_settings())["settings"]
    assert snap["grid"] is True and snap["grid_spacing"] == 5.0
    assert snap["snap"] is True and snap["snap_spacing"] == [2.5, 5.0]

    path = str(tmp_path / "grid.dxf")
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)
    again = (await backend.drawing_settings())["settings"]
    assert again["grid"] is True and again["grid_spacing"] == 5.0
    assert again["snap_spacing"] == [2.5, 5.0]


async def test_ortho_and_psltscale_are_header_variables(backend):
    res = await backend.drawing_settings({"ortho": True, "psltscale": False})
    assert res["ok"] is True, res.get("errors")
    assert backend._doc.header["$ORTHOMODE"] == 1
    assert backend._doc.header["$PSLTSCALE"] == 0
    snap = (await backend.drawing_settings())["settings"]
    assert snap["ortho"] is True and snap["psltscale"] is False


async def test_polar_is_refused_headlessly_by_capability(backend):
    res = await backend.drawing_settings({"polar": True, "polar_angle": 45, "ortho": True})
    assert res["ok"] is False
    assert res["applied"] == {"ortho": True}, "the key that has a home is still applied"
    assert "registry" in res["errors"]["polar"]
    assert "registry" in res["errors"]["polar_angle"]
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.system_set_variable("POLARANG", 0.5)
    assert excinfo.value.capability == "registry_sysvar"


async def test_annotation_scale_writes_the_variable_dictionary_and_scale_list(backend, tmp_path):
    res = await backend.drawing_settings({"annotation_scale": "1:50"})
    assert res["ok"] is True, res.get("errors")
    assert res["changed"]["annotation_scale"] == [
        {"name": "1:1", "value": 1.0},
        {"name": "1:50", "value": 0.02},
    ]
    vardict = backend._doc.rootdict.get("AcDbVariableDictionary")
    assert vardict["CANNOSCALE"].dxf.value == "1:50"
    scales = backend._doc.rootdict.get("ACAD_SCALELIST")
    assert scales.get("1:50") is not None, "AutoCAD only honours a scale that is in the list"
    assert await backend.system_get_variable("CANNOSCALEVALUE") == pytest.approx(0.02)

    # re-setting the same scale is not a change and adds no second list entry
    again = await backend.drawing_settings({"annotation_scale": "1:50"})
    assert again["changed"] == {}
    assert len(list(scales.keys())) == 1

    path = str(tmp_path / "scale.dxf")
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)
    assert (await backend.drawing_settings())["settings"]["annotation_scale"] == {
        "name": "1:50",
        "value": 0.02,
    }
    assert backend._doc.audit().has_errors is False


def _seed_autocad_style_scale_list(doc, names):
    """ACAD_SCALELIST the way AutoCAD saves it: keyed A0, A1, ... with the scale
    name only in the SCALE object's group 300 (the default metric list already
    carries 1:1, 1:2, 1:5, 1:10, 1:20, 1:50, 1:100, 2:1 ...)."""
    from ezdxf.entities import factory
    from ezdxf.lldxf.extendedtags import ExtendedTags

    scales = doc.rootdict.get("ACAD_SCALELIST")
    if scales is None:
        scales = doc.rootdict.add_new_dict("ACAD_SCALELIST")
    for index, name in enumerate(names):
        paper, drawing = (float(part) for part in name.split(":"))
        handle = doc.entitydb.next_handle()
        text = (
            f"  0\nSCALE\n  5\n{handle}\n330\n{scales.dxf.handle}\n100\nAcDbScale\n"
            f" 70\n0\n300\n{name}\n140\n{paper}\n141\n{drawing}\n290\n{int(paper == drawing)}\n"
        )
        entry = factory.load(ExtendedTags.from_text(text), doc)
        doc.entitydb.add(entry)
        doc.objects.add_object(entry)
        scales.add(f"A{index}", entry)
    return scales


def _scale_names(scales):
    return [
        tag.value
        for _key, entry in scales.items()
        for subclass in entry.xtags.subclasses
        for tag in subclass
        if tag.code == 300
    ]


async def test_annotation_scale_matches_an_autocad_keyed_scale_list_by_name(backend, tmp_path):
    """Regression: the presence check used to look the scale up by dictionary
    *key*, so on any AutoCAD-authored drawing (keys A0, A1, ...) every write
    appended a second SCALE object with the same name."""
    scales = _seed_autocad_style_scale_list(backend._doc, ["1:1", "1:2", "1:50"])
    assert list(scales.keys()) == ["A0", "A1", "A2"]

    res = await backend.drawing_settings({"annotation_scale": "1:50"})
    assert res["ok"] is True, res.get("errors")
    assert res["changed"]["annotation_scale"][1] == {"name": "1:50", "value": 0.02}
    assert list(scales.keys()) == ["A0", "A1", "A2"], "the existing entry is reused, not duplicated"
    assert _scale_names(scales).count("1:50") == 1
    assert backend._doc.rootdict.get("AcDbVariableDictionary")["CANNOSCALE"].dxf.value == "1:50"

    # a scale the list does not carry is still appended, exactly once, under a
    # key that does not collide with AutoCAD's A<n> keys
    res = await backend.drawing_settings({"annotation_scale": "1:25"})
    assert res["ok"] is True, res.get("errors")
    assert _scale_names(scales).count("1:25") == 1
    assert len(list(scales.keys())) == 4
    await backend.drawing_settings({"annotation_scale": "1:25"})
    assert len(list(scales.keys())) == 4

    path = str(tmp_path / "autocad_keyed.dxf")
    await backend.drawing_save_as(path)
    await backend.drawing_open(path)
    reopened = backend._doc.rootdict.get("ACAD_SCALELIST")
    assert sorted(_scale_names(reopened)) == ["1:1", "1:2", "1:25", "1:50"]
    assert backend._doc.audit().has_errors is False


async def test_unit_formats_by_name_and_by_code(backend):
    res = await backend.drawing_settings({"linear_units": "architectural", "angular_units": "dms"})
    assert res["ok"] is True, res.get("errors")
    assert backend._doc.header["$LUNITS"] == 4
    assert backend._doc.header["$AUNITS"] == 1
    snap = (await backend.drawing_settings())["settings"]
    assert snap["linear_units"] == {"code": 4, "name": "architectural"}
    assert snap["angular_units"] == {"code": 1, "name": "dms"}

    by_code = await backend.drawing_settings({"linear_units": 1, "angular_units": 3})
    assert by_code["ok"] is True
    assert backend._doc.header["$LUNITS"] == 1 and backend._doc.header["$AUNITS"] == 3

    bad = await backend.drawing_settings({"linear_units": "furlongs", "angular_units": 9})
    assert "architectural" in bad["errors"]["linear_units"]
    assert "angular_units" in bad["errors"]["angular_units"]
    assert backend._doc.header["$LUNITS"] == 1, "a refused value writes nothing"


async def test_current_styles_delegate_to_the_styles_contract(backend, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _dimstyle(name):
        calls.append(("dimstyle_set_current", name))
        backend._doc.header["$DIMSTYLE"] = name
        return {"ok": True, "current": name, "previous": "ISO-25"}

    async def _textstyle(name):
        calls.append(("textstyle_set_current", name))
        backend._doc.header["$TEXTSTYLE"] = name
        return {"ok": True, "current": name, "previous": "Standard"}

    monkeypatch.setattr(backend, "dimstyle_set_current", _dimstyle, raising=False)
    monkeypatch.setattr(backend, "textstyle_set_current", _textstyle, raising=False)
    res = await backend.drawing_settings({"dimstyle": " ANSI ", "textstyle": "ISOCP"})
    assert res["ok"] is True, res.get("errors")
    assert calls == [("dimstyle_set_current", "ANSI"), ("textstyle_set_current", "ISOCP")]
    assert res["changed"]["dimstyle"] == ["ISO-25", "ANSI"]
    snap = (await backend.drawing_settings())["settings"]
    assert snap["dimstyle"] == "ANSI" and snap["textstyle"] == "ISOCP"


async def test_current_styles_are_refused_when_no_styles_contract_exists(backend, monkeypatch):
    # Group S may or may not have merged: force the "absent" branch either way.
    monkeypatch.setattr(backend, "dimstyle_set_current", None, raising=False)
    res = await backend.drawing_settings({"dimstyle": "ANSI"})
    assert res["ok"] is False
    assert "dimstyle_set_current" in res["errors"]["dimstyle"]
    assert (await backend.drawing_settings())["settings"]["dimstyle"] == "ISO-25"


async def test_changed_reports_only_values_that_moved(backend):
    same = await backend.drawing_settings({"psltscale": True, "ortho": False})
    assert same["ok"] is True and same["changed"] == {}
    moved = await backend.drawing_settings({"psltscale": False, "ortho": False})
    assert moved["changed"] == {"psltscale": [True, False]}


async def test_malformed_values_are_refused_before_any_write(backend):
    before_limmax = tuple(backend._doc.header["$LIMMAX"])
    res = await backend.drawing_settings(
        {
            "limits": [[0, 0], [0, 5]],
            "annotation_scale": "fifty",
            "grid_spacing": 0,
            "snap_spacing": [1, 2, 3],
            "polar": "maybe",
            "dimstyle": "",
        }
    )
    assert res["ok"] is False and res["applied"] == {}
    assert set(res["errors"]) == {
        "limits",
        "annotation_scale",
        "grid_spacing",
        "snap_spacing",
        "polar",
        "dimstyle",
    }
    assert "upper-right" in res["errors"]["limits"]
    assert "paper:drawing" in res["errors"]["annotation_scale"]
    assert "greater than 0" in res["errors"]["grid_spacing"]
    assert tuple(backend._doc.header["$LIMMAX"]) == before_limmax
    assert backend._doc.rootdict.get("AcDbVariableDictionary") is None
    assert tuple(_active_vport(backend).dxf.grid_spacing)[:2] != (0.0, 0.0)


async def test_raw_gridmode_routes_to_the_vport_and_the_old_error_is_gone(backend):
    assert await backend.system_get_variable("GRIDMODE") == 0
    out = await backend.system_set_variable("GRIDMODE", 1)
    assert out["ok"] is True
    assert _active_vport(backend).dxf.grid_on == 1
    assert await backend.system_get_variable("GRIDMODE") == 1


async def test_raw_unknown_header_name_is_a_named_value_error(backend):
    with pytest.raises(ValueError, match=r"\$HPNAME2"):
        await backend.system_set_variable("HPNAME2", "x")


async def test_cannoscalevalue_is_read_only(backend):
    with pytest.raises(ValueError, match="CANNOSCALEVALUE is read-only"):
        await backend.system_set_variable("CANNOSCALEVALUE", 0.5)


def test_both_capability_maps_declare_registry_sysvar():
    from backends.com_backend import ComBackend
    from backends.ezdxf_backend import EzdxfBackend

    ezdxf_features = EzdxfBackend().capabilities().to_dict()["features"]
    com_features = ComBackend().capabilities().to_dict()["features"]
    assert ezdxf_features["registry_sysvar"]["supported"] is False
    assert com_features["registry_sysvar"]["supported"] is True


# ── server tool wiring ──────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, *a, **k):
        pass


async def test_server_tool_carries_the_new_keys(backend):
    import server

    ctx = _FakeCtx(backend)
    write = await server.drawing_settings(
        settings={"limits": [[0, 0], [297, 210]], "grid": True, "annotation_scale": "1:2"},
        ctx=ctx,
    )
    assert write["ok"] is True, write.get("errors")
    read = await server.drawing_settings(ctx=ctx)
    assert read["settings"]["limits"] == [[0.0, 0.0], [297.0, 210.0]]
    assert read["settings"]["annotation_scale"] == {"name": "1:2", "value": 0.5}


# ── live engine, against a fake ActiveX application ─────────────────────────


class _FakeApp:
    """Records GetVariable / SetVariable; stores what AutoCAD would store."""

    def __init__(self):
        self.store = {
            "INSUNITS": 4,
            "LIMMIN": (0.0, 0.0),
            "LIMMAX": (420.0, 297.0),
            "GRIDMODE": 0,
            "GRIDUNIT": (10.0, 10.0),
            "SNAPMODE": 0,
            "SNAPUNIT": (10.0, 10.0),
            "ORTHOMODE": 0,
            "AUTOSNAP": 63,
            "POLARANG": math.pi / 2,
            "PSLTSCALE": 1,
            "CANNOSCALE": "1:1",
            "LUNITS": 2,
            "AUNITS": 0,
            "DIMSTYLE": "ISO-25",
            "TEXTSTYLE": "Standard",
            "DIMSCALE": 1.0,
        }
        self.calls: list[tuple] = []

    def GetVariable(self, name):
        self.calls.append(("GetVariable", name))
        return self.store[name]

    def SetVariable(self, name, value):
        stored = getattr(value, "value", value)  # a VARIANT array → its Python sequence
        if isinstance(stored, list):
            stored = tuple(stored)
        self.calls.append(("SetVariable", name, stored))
        self.store[name] = stored


@pytest.fixture
def com_backend(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    app = _FakeApp()
    monkeypatch.setattr(module, "_acad_app", lambda: app)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, app


async def test_com_facade_sends_autocad_shaped_values(com_backend):
    backend, app = com_backend
    res = await backend.drawing_settings(
        {
            "limits": [[0, 0], [500, 350]],
            "grid": True,
            "grid_spacing": 5,
            "polar": False,
            "polar_angle": 45,
            "annotation_scale": "1:50",
            "linear_units": "architectural",
            "psltscale": 0,
        }
    )
    assert res["ok"] is True, res.get("errors")
    sets = [call[1:] for call in app.calls if call[0] == "SetVariable"]
    assert sets == [
        ("LIMMIN", (0.0, 0.0)),
        ("LIMMAX", (500.0, 350.0)),
        ("GRIDMODE", 1),
        ("GRIDUNIT", (5.0, 5.0)),
        ("AUTOSNAP", 55),  # 63 with bit 8 cleared: polar off, everything else kept
        ("POLARANG", pytest.approx(math.radians(45))),
        ("CANNOSCALE", "1:50"),
        ("LUNITS", 4),
        ("PSLTSCALE", 0),
    ]
    assert res["changed"]["polar"] == [True, False]
    assert res["changed"]["polar_angle"] == [90.0, 45.0]


async def test_com_point_variables_travel_as_variant_double_arrays(com_backend):
    import pythoncom

    backend, app = com_backend
    seen: dict = {}
    real_set = app.SetVariable

    def _capture(name, value):
        seen[name] = value
        real_set(name, value)

    app.SetVariable = _capture
    await backend.system_set_variable("LIMMAX", (300, 200))
    variant = seen["LIMMAX"]
    assert variant.varianttype == pythoncom.VT_ARRAY | pythoncom.VT_R8
    assert tuple(variant.value) == (300.0, 200.0)


async def test_com_refuses_before_touching_activex(com_backend):
    backend, app = com_backend
    res = await backend.drawing_settings({"limits": [[0, 0], [0, 0]], "annotation_scale": "x"})
    assert res["ok"] is False
    assert not [call for call in app.calls if call[0] == "SetVariable"]
