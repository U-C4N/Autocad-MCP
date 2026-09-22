"""The live-only environment trio: launch, preferences, operator prompts.

Headless there is no application to launch, no registry of preferences and
no operator to prompt, so ``EzdxfBackend`` inherits the ``@capability``
defaults and refuses with a key ``system_capabilities`` declares. Live, the
COM paths are exercised against fake ActiveX objects asserting the call
shapes; ``scripts/smoke_settings_com.py`` runs them once for real.

A cancelled pick is an answer (``cancelled: true``), not an error; a
wall-clock timeout is ``timed_out: true``. Preference writes are refused by
name before any ActiveX call: read-only keys, unknown keys, values outside
the authored range.
"""

from __future__ import annotations

import types

import pytest
from fastmcp import Client

import config
import server
from backends.base import UnsupportedCapabilityError
from backends.capability import is_capability_default
from backends.ezdxf_backend import EzdxfBackend
from engineering.environment.preferences import (
    PREFERENCE_KEYS,
    READ_ONLY_KEYS,
    SAVE_AS_TYPES,
    decode_value,
    describe_preferences,
    split_key,
    validate_preference,
)

pytestmark = pytest.mark.asyncio


# ── the whitelist, pure ─────────────────────────────────────────────────────


def test_whitelist_is_the_spec_list():
    assert set(PREFERENCE_KEYS) == {
        "OpenSave.SaveAsType",
        "OpenSave.AutoSaveInterval",
        "OpenSave.CreateBackup",
        "OpenSave.IncrementalSavePercent",
        "Display.CursorSize",
        "Drafting.AutoSnapMarkerSize",
        "Drafting.AutoSnapTooltip",
        "Selection.PickBoxSize",
        "Output.DefaultPlotStyleTable",
        "Output.DefaultOutputDevice",
    }
    assert READ_ONLY_KEYS == (
        "Files.SupportPath",
        "Files.TemplateDwgPath",
        "Files.PrinterStyleSheetPath",
        "Files.PrinterConfigPath",
    )
    assert split_key("OpenSave.AutoSaveInterval") == ("OpenSave", "AutoSaveInterval")
    assert SAVE_AS_TYPES["ac2018_dwg"] == 64 and SAVE_AS_TYPES["ac2000_dwg"] == 12


def test_validate_preference_coerces_and_refuses_by_name():
    assert validate_preference("OpenSave.AutoSaveInterval", 10) == 10
    assert validate_preference("OpenSave.AutoSaveInterval", 10.0) == 10
    assert validate_preference("OpenSave.CreateBackup", False) is False
    assert validate_preference("OpenSave.SaveAsType", "AC2018_DWG") == 64
    assert validate_preference("OpenSave.SaveAsType", 48) == 48
    assert (
        validate_preference("Output.DefaultPlotStyleTable", " monochrome.ctb ") == "monochrome.ctb"
    )
    with pytest.raises(ValueError, match="'Files.SupportPath' is read-only"):
        validate_preference("Files.SupportPath", "C:/x")
    with pytest.raises(ValueError, match="unknown preference 'Display.Theme'"):
        validate_preference("Display.Theme", 1)
    with pytest.raises(ValueError, match="between 0 and 600, got 601"):
        validate_preference("OpenSave.AutoSaveInterval", 601)
    with pytest.raises(TypeError, match="takes an int"):
        validate_preference("Selection.PickBoxSize", 2.5)
    with pytest.raises(TypeError, match="takes a bool"):
        validate_preference("OpenSave.CreateBackup", 1)
    with pytest.raises(ValueError, match="must be one of"):
        validate_preference("OpenSave.SaveAsType", "ac1999_dwg")
    with pytest.raises(TypeError, match="takes a non-empty string"):
        validate_preference("Output.DefaultOutputDevice", "")


def test_decode_value_maps_enum_values_back_to_names():
    assert decode_value("OpenSave.SaveAsType", 64) == "ac2018_dwg"
    assert decode_value("OpenSave.SaveAsType", 999) == 999
    assert decode_value("OpenSave.CreateBackup", 1) is True
    assert decode_value("Files.SupportPath", "C:/a;C:/b") == "C:/a;C:/b"


def test_describe_preferences_covers_every_key():
    rows = {row["key"]: row for row in describe_preferences()}
    assert set(rows) == set(PREFERENCE_KEYS) | set(READ_ONLY_KEYS)
    assert rows["OpenSave.AutoSaveInterval"] == {
        "key": "OpenSave.AutoSaveInterval",
        "kind": "int",
        "range": [0, 600],
        "enum": None,
        "writable": True,
    }
    assert rows["Files.SupportPath"]["writable"] is False


# ── headless: declared refusals ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, args, key",
    [
        ("system_launch", (), "live_application"),
        ("preferences_get", (), "preferences"),
        ("preferences_set", ("Display.CursorSize", 5), "preferences"),
        ("user_pick_point", ("Pick",), "interactive_prompt"),
        ("user_select", ("Select",), "interactive_prompt"),
        ("system_prompt_message", ("hi",), "interactive_prompt"),
    ],
)
async def test_headless_refuses_with_the_declared_key(backend, method, args, key):
    assert is_capability_default(EzdxfBackend, method)
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await getattr(backend, method)(*args)
    assert excinfo.value.capability == key
    assert "ezdxf" in str(excinfo.value)
    feature = backend.capabilities().features[key]
    assert feature.supported is False and feature.reason


async def test_refusal_reaches_the_client_with_the_key(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {"bootstrap": False})
        result = await client.call_tool(
            "user_pick_point", {"prompt": "Pick the base point"}, raise_on_error=False
        )
        assert result.is_error is True
        payload = result.structured_content
        assert payload["ok"] is False and payload["capability"] == "interactive_prompt"
        caps = (await client.call_tool("system_capabilities", {})).structured_content
        assert caps["features"]["interactive_prompt"]["supported"] is False


# ── live engine, against a fake ActiveX surface ──────────────────────────────


def _com_error(description: str):
    pywintypes = pytest.importorskip("pywintypes")
    return pywintypes.com_error(
        -2147352567, "Exception occurred.", (0, None, description, None, 0, -2145320928), None
    )


class _FakeUtility:
    """IAcadUtility as measured on AutoCAD 2026.

    ``GetPoint`` answers in WCS; ``GetEntity``'s PickedPoint is in the
    *current UCS* (``ucs_origin`` / ``ucs_xdir`` / ``ucs_ydir``, world by
    default); ``TranslateCoordinates`` only accepts a VT_ARRAY|VT_R8 VARIANT —
    the plain tuple GetEntity returns is refused as "Invalid argument Point".
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.point = (12.5, 7.25, 0.0)
        self.picked_ucs = (3.0, 4.0, 0.0)
        self.ucs_origin = (0.0, 0.0, 0.0)
        self.ucs_xdir = (1.0, 0.0, 0.0)
        self.ucs_ydir = (0.0, 1.0, 0.0)
        self.raise_on_pick = None
        self.entity = types.SimpleNamespace(Handle="1F3")

    def GetPoint(self, base, prompt):
        self.calls.append(("GetPoint", (base, prompt)))
        if self.raise_on_pick is not None:
            raise self.raise_on_pick
        return self.point

    def GetEntity(self, prompt):
        self.calls.append(("GetEntity", (prompt,)))
        if self.raise_on_pick is not None:
            raise self.raise_on_pick
        return self.entity, self.picked_ucs

    def TranslateCoordinates(self, point, from_cs, to_cs, displacement):
        import pythoncom
        from win32com.client import VARIANT

        if not isinstance(point, VARIANT) or point.varianttype != (
            pythoncom.VT_ARRAY | pythoncom.VT_R8
        ):
            raise _com_error("Invalid argument Point in TranslateCoordinates")
        self.calls.append(
            ("TranslateCoordinates", (tuple(point.value), from_cs, to_cs, displacement))
        )
        assert (from_cs, to_cs, displacement) == (1, 0, False)  # acUCS → acWorld, a point
        u, v, w = point.value
        ox, oy, oz = self.ucs_origin
        xx, xy, xz = self.ucs_xdir
        yx, yy, yz = self.ucs_ydir
        zx, zy, zz = (
            xy * yz - xz * yy,
            xz * yx - xx * yz,
            xx * yy - xy * yx,
        )
        return (
            ox + u * xx + v * yx + w * zx,
            oy + u * xy + v * yy + w * zy,
            oz + u * xz + v * yz + w * zz,
        )

    def Prompt(self, text):
        self.calls.append(("Prompt", (text,)))


class _FakeSelectionSet:
    def __init__(self, name, items):
        self.Name = name
        self._items = items
        self.calls: list[str] = []

    @property
    def Count(self):
        return len(self._items)

    def Item(self, index):
        return self._items[index]

    def SelectOnScreen(self):
        self.calls.append("SelectOnScreen")

    def Delete(self):
        self.calls.append("Delete")


class _FakeSelectionSets:
    def __init__(self, items):
        self.items = items
        self.sets: list[_FakeSelectionSet] = []

    def Add(self, name):
        ss = _FakeSelectionSet(name, self.items)
        self.sets.append(ss)
        return ss


@pytest.fixture
def com_backend(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    utility = _FakeUtility()
    selection = [types.SimpleNamespace(Handle="A1"), types.SimpleNamespace(Handle="B2")]
    document = types.SimpleNamespace(
        Name="Part.dwg",
        FullName="C:/work/Part.dwg",
        Utility=utility,
        SelectionSets=_FakeSelectionSets(selection),
    )
    preferences = types.SimpleNamespace(
        OpenSave=types.SimpleNamespace(
            SaveAsType=64, AutoSaveInterval=10, CreateBackup=True, IncrementalSavePercent=50
        ),
        Display=types.SimpleNamespace(CursorSize=5),
        Drafting=types.SimpleNamespace(AutoSnapMarkerSize=5, AutoSnapTooltip=True),
        Selection=types.SimpleNamespace(PickBoxSize=3),
        Output=types.SimpleNamespace(
            DefaultPlotStyleTable="monochrome.ctb", DefaultOutputDevice="DWG To PDF.pc3"
        ),
        Files=types.SimpleNamespace(
            SupportPath="C:/support",
            TemplateDwgPath="C:/t",
            PrinterStyleSheetPath="C:/p",
            PrinterConfigPath="C:/pc3",
        ),
    )
    app = types.SimpleNamespace(
        Preferences=preferences,
        Version="25.0",
        Documents=types.SimpleNamespace(Count=1),
        ActiveDocument=document,
    )
    monkeypatch.setattr(module, "_acad_app", lambda: app)
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, app, document


async def test_com_no_longer_inherits_the_defaults():
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends.com_backend import ComBackend

    for method in (
        "system_launch",
        "preferences_get",
        "preferences_set",
        "user_pick_point",
        "user_select",
        "system_prompt_message",
    ):
        assert not is_capability_default(ComBackend, method), method


async def test_com_pick_point_returns_the_operator_point(com_backend):
    backend, app, document = com_backend
    import pythoncom

    result = await backend.user_pick_point("Pick the base point")
    assert result == {"cancelled": False, "x": 12.5, "y": 7.25, "z": 0.0, "backend": "com"}
    (call,) = document.Utility.calls
    assert call[0] == "GetPoint" and call[1][0] is pythoncom.Missing
    assert call[1][1] == "\nPick the base point"


async def test_com_pick_point_cancel_is_an_answer_not_an_error(com_backend):
    backend, app, document = com_backend
    document.Utility.raise_on_pick = _com_error("User input is a keyword")
    result = await backend.user_pick_point("Pick")
    assert result == {"cancelled": True, "reason": "User input is a keyword", "backend": "com"}


async def test_com_pick_point_other_com_errors_still_raise(com_backend):
    backend, app, document = com_backend
    pywintypes = pytest.importorskip("pywintypes")
    document.Utility.raise_on_pick = pywintypes.com_error(
        -2147418111, "Call was rejected", None, None
    )
    with pytest.raises(pywintypes.com_error):
        await backend.user_pick_point("Pick")


async def test_com_pick_point_timeout_reports_timed_out(com_backend, monkeypatch):
    backend, app, document = com_backend
    monkeypatch.setattr(config.settings, "com_call_timeout", 7.0)

    async def _timed_out(func, *args, **kwargs):
        raise RuntimeError(
            "AutoCAD did not respond within 7s. The application may be showing a modal dialog"
        )

    monkeypatch.setattr(backend, "_run", _timed_out)
    result = await backend.user_pick_point("Pick")
    assert result["timed_out"] is True and result["timeout_s"] == 7.0
    assert "did not respond" in result["error"]
    result = await backend.user_select("Pick", "multiple")
    assert result["timed_out"] is True


async def test_com_prompt_validation_happens_before_activex(com_backend):
    backend, app, document = com_backend
    with pytest.raises(ValueError, match="prompt must not be empty"):
        await backend.user_pick_point("   ")
    with pytest.raises(ValueError, match="mode must be 'single' or 'multiple'"):
        await backend.user_select("Pick", "many")
    assert document.Utility.calls == [] and document.SelectionSets.sets == []


async def test_com_select_single_uses_getentity(com_backend):
    backend, app, document = com_backend
    result = await backend.user_select("Pick the flange")
    assert result == {
        "cancelled": False,
        "mode": "single",
        "handles": ["1F3"],
        "picked": [3.0, 4.0],
        "backend": "com",
    }
    assert document.Utility.calls == [
        ("GetEntity", ("\nPick the flange",)),
        ("TranslateCoordinates", ((3.0, 4.0, 0.0), 1, 0, False)),
    ]
    document.Utility.raise_on_pick = _com_error("Function cancelled")
    assert (await backend.user_select("Pick"))["cancelled"] is True


async def test_com_select_single_reports_the_pick_in_wcs_under_a_ucs(com_backend):
    """MEASURED (AutoCAD 2026): GetEntity's PickedPoint is in the current UCS.

    UCS origin (100,50,0) rotated 90 deg (X dir (0,1,0)); the operator picked
    a circle at WCS (105,70): GetEntity reported (20,-5,0) and
    TranslateCoordinates(acUCS → acWorld) gave (105,70,0). GetPoint already
    answers in WCS, so ``user_pick_point`` and ``user_select`` must agree.
    """
    backend, app, document = com_backend
    utility = document.Utility
    utility.ucs_origin = (100.0, 50.0, 0.0)
    utility.ucs_xdir = (0.0, 1.0, 0.0)
    utility.ucs_ydir = (-1.0, 0.0, 0.0)
    utility.picked_ucs = (20.0, -5.0, 0.0)
    result = await backend.user_select("Pick the circle")
    assert result["picked"] == [105.0, 70.0]
    assert result["handles"] == ["1F3"] and result["cancelled"] is False
    # A translated UCS without rotation (origin (100,50)): (15,20) → (115,70).
    utility.ucs_xdir, utility.ucs_ydir = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    utility.picked_ucs = (15.0, 20.0, 0.0)
    assert (await backend.user_select("Pick"))["picked"] == [115.0, 70.0]
    # GetPoint is not translated — it already answers in WCS.
    utility.point = (110.0, 70.0, 0.0)
    point = await backend.user_pick_point("Pick")
    assert (point["x"], point["y"]) == (110.0, 70.0)
    assert not any(name == "TranslateCoordinates" for name, _ in utility.calls[-1:])


async def test_com_select_multiple_uses_a_selection_set_and_deletes_it(com_backend):
    backend, app, document = com_backend
    result = await backend.user_select("Select the pipes", mode="multiple")
    assert result == {
        "cancelled": False,
        "mode": "multiple",
        "handles": ["A1", "B2"],
        "count": 2,
        "backend": "com",
    }
    assert document.Utility.calls == [("Prompt", ("\nSelect the pipes\n",))]
    (ss,) = document.SelectionSets.sets
    assert ss.Name.startswith("_PICK_") and ss.calls == ["SelectOnScreen", "Delete"]


async def test_com_prompt_message_writes_to_the_command_line(com_backend):
    backend, app, document = com_backend
    result = await backend.system_prompt_message("Drawing saved by the assistant")
    assert result == {"ok": True, "text": "Drawing saved by the assistant", "backend": "com"}
    assert document.Utility.calls == [("Prompt", ("\nDrawing saved by the assistant\n",))]


async def test_com_preferences_get_reads_the_whitelist(com_backend):
    backend, app, document = com_backend
    result = await backend.preferences_get()
    assert result["read_only"] == list(READ_ONLY_KEYS)
    assert result["values"]["OpenSave.SaveAsType"] == "ac2018_dwg"
    assert result["values"]["OpenSave.AutoSaveInterval"] == 10
    assert result["values"]["Files.SupportPath"] == "C:/support"
    assert set(result["values"]) == set(PREFERENCE_KEYS) | set(READ_ONLY_KEYS)
    subset = await backend.preferences_get(["Display.CursorSize"])
    assert subset == {"values": {"Display.CursorSize": 5}, "read_only": [], "backend": "com"}
    with pytest.raises(ValueError, match="unknown preference keys \\['Display.Theme'\\]"):
        await backend.preferences_get(["Display.Theme"])


async def test_com_preferences_set_reports_old_and_new(com_backend):
    backend, app, document = com_backend
    result = await backend.preferences_set("OpenSave.AutoSaveInterval", 15)
    assert result == {
        "ok": True,
        "key": "OpenSave.AutoSaveInterval",
        "old": 10,
        "new": 15,
        "changed": True,
        "backend": "com",
    }
    assert app.Preferences.OpenSave.AutoSaveInterval == 15
    same = await backend.preferences_set("OpenSave.AutoSaveInterval", 15)
    assert same["changed"] is False
    enum = await backend.preferences_set("OpenSave.SaveAsType", "ac2013_dwg")
    assert enum["old"] == "ac2018_dwg" and enum["new"] == "ac2013_dwg"
    assert app.Preferences.OpenSave.SaveAsType == 60


async def test_com_preferences_set_refuses_before_any_write(com_backend):
    backend, app, document = com_backend
    with pytest.raises(ValueError, match="read-only"):
        await backend.preferences_set("Files.SupportPath", "C:/evil")
    with pytest.raises(ValueError, match="between 1 and 100"):
        await backend.preferences_set("Display.CursorSize", 0)
    with pytest.raises(ValueError, match="unknown preference"):
        await backend.preferences_set("Display.Theme", 1)
    assert app.Preferences.Files.SupportPath == "C:/support"
    assert app.Preferences.Display.CursorSize == 5


async def test_com_launch_attaches_first_and_dispatches_otherwise(com_backend, monkeypatch):
    backend, app, document = com_backend
    from backends import com_backend as module

    monkeypatch.setattr(module, "_COM_STATE", {})
    calls: list[str] = []

    def _get_active(progid):
        calls.append(f"GetActiveObject:{progid}")
        raise RuntimeError("not running")

    def _dispatch(progid):
        calls.append(f"Dispatch:{progid}")
        app.Visible = False
        return app

    monkeypatch.setattr(module.win32com.client, "GetActiveObject", _get_active)
    monkeypatch.setattr(module.win32com.client, "Dispatch", _dispatch)
    result = await backend.system_launch(visible=True)
    assert result == {
        "launched": True,
        "attached": False,
        "version": "25.0",
        "document": "Part.dwg",
        "visible": True,
        "progid": config.settings.cad_progid,
        "backend": "com",
    }
    assert calls == [
        f"GetActiveObject:{config.settings.cad_progid}",
        f"Dispatch:{config.settings.cad_progid}",
    ]
    assert app.Visible is True and module._COM_STATE["app"] is app

    again = await backend.system_launch()
    assert again["launched"] is False and again["attached"] is True
    assert len(calls) == 2, "an app already cached is attached, not re-dispatched"


async def test_com_launch_opens_a_validated_path(com_backend, monkeypatch, tmp_path):
    backend, app, document = com_backend
    from backends import com_backend as module

    opened: list[str] = []
    app.Documents = types.SimpleNamespace(
        Count=1,
        Open=lambda path: opened.append(path) or types.SimpleNamespace(Name="gear.dwg"),
    )
    monkeypatch.setattr(module, "_COM_STATE", {"app": app})
    target = tmp_path / "gear.dwg"
    target.write_bytes(b"")
    result = await backend.system_launch(open_path=str(target))
    assert result["document"] == "gear.dwg" and opened == [str(target.resolve())]
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await backend.system_launch(open_path="../../etc/passwd")
