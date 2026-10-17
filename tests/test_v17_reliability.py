"""v1.7: the fixes and upgrades from the 2026-10-18 field report.

A production label sheet built through this server (AutoCAD 2026, plus the
headless engine for comparison) found fourteen defects. Each test below names
the report item it pins:

  #1/#5  a create AutoCAD half-refused left its entity behind; refused calls
         were never retried                    → atomic creates, message filter
  #2     viewport_create looked at the wrong place (live only — see smoke script)
  #3     layout_create left AutoCAD's default viewport on the sheet (live only)
  #4     a refusing seat was reported with a raw HRESULT → diagnosis + message
  #6/#7  no full-bleed media; paper units moved by a media write
  #8     viewport_list crashed on a narrowed IAcadEntity
  #9     SetVariable refused a VT_I4 for a 16-bit variable
  #10    an unknown linetype was assigned silently (headless)
  #11    a rolled-back handle came back as a SEQEND (headless)
  #12    the current layer could be deleted (headless)
  #13    entity_offset refused LWPOLYLINE (headless)
  #14    a duplicate layer escaped as an internal error; no exist_ok

and the upgrades the report asked for: true colour, bold/italic text styles,
MTEXT attachment and line spacing, DXF release selection, TOOL_INCLUDE.
"""

from __future__ import annotations

import sys
import threading
import types

import ezdxf
import pytest

import config
from backends import com_message_filter
from backends.contracts.drawing import read_acadver, resolve_dxf_version
from backends.contracts.entity_creation import resolve_mtext_layout
from backends.contracts.entity_modification import parse_true_color, true_color_hex
from engineering.standards.papers import canonical_media_name, resolve_page_setup

pytestmark = pytest.mark.asyncio


# ── #10 unknown linetype ────────────────────────────────────────────────────


async def test_unknown_linetype_is_refused_and_leaves_nothing_behind(backend):
    before = len(backend._msp())
    with pytest.raises(ValueError, match="NOPE_LT"):
        await backend.entity_create_line(0, 0, 1, 1, linetype="NOPE_LT")
    assert len(backend._msp()) == before, "the refused create must not leave its LINE"
    assert "NOPE_LT" not in backend._doc.linetypes


async def test_standard_linetypes_still_load_on_demand(backend):
    line = await backend.entity_create_line(0, 0, 10, 0, linetype="DASHED")
    assert line.linetype == "DASHED" and "DASHED" in backend._doc.linetypes


# ── #11 handles after rollback / undo ───────────────────────────────────────


async def test_rolled_back_handle_does_not_come_back_as_another_entity(backend):
    # A dimension block holds an attribute-less arrow INSERT; ezdxf writes it
    # with no SEQEND and makes one when the snapshot is read back.
    await backend.dimension_linear(0, 0, 30, 0, 15, -8)
    await backend.transaction_begin()
    circle = await backend.entity_create_circle(500, 500, 5)
    await backend.transaction_rollback()
    with pytest.raises(RuntimeError, match="not found"):
        await backend.entity_get(circle.handle)
    fresh = await backend.entity_create_circle(1, 1, 1)
    assert int(fresh.handle, 16) > int(circle.handle, 16), "no handle issued twice"


async def test_undo_does_not_hand_an_undone_handle_to_a_seqend(backend, monkeypatch):
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 5)
    backend._reset_history_baseline()
    await backend.dimension_linear(0, 0, 30, 0, 15, -8)
    circle = await backend.entity_create_circle(5, 5, 1)
    await backend.drawing_undo()
    with pytest.raises(RuntimeError, match="not found"):
        await backend.entity_get(circle.handle)


# ── #12 / #14 layers ────────────────────────────────────────────────────────


async def test_duplicate_layer_is_a_value_error_and_exist_ok_updates_it(backend):
    await backend.layer_create("T_LAYER", color=3)
    with pytest.raises(ValueError, match="already exists"):
        await backend.layer_create("T_LAYER", color=3)
    layer = await backend.layer_create("t_layer", color=4, exist_ok=True)
    assert layer.name == "T_LAYER" and layer.color == 4


@pytest.mark.parametrize(
    ("setup", "name", "match"),
    [
        ("current", "T_LAYER", "current layer"),
        (None, "0", "reserves"),
        (None, "Defpoints", "reserves"),
        ("used", "T_LAYER", "still holds 1"),
        (None, "NOPE", "not found"),
    ],
)
async def test_layer_delete_refuses_what_autocad_refuses(backend, setup, name, match):
    await backend.layer_create("T_LAYER")
    if setup == "current":
        await backend.layer_set_current("T_LAYER")
    if setup == "used":
        await backend.entity_create_line(0, 0, 1, 1, layer="T_LAYER")
    with pytest.raises(ValueError, match=match):
        await backend.layer_delete(name)


async def test_layer_delete_removes_an_empty_layer(backend):
    await backend.layer_create("EMPTY")
    assert (await backend.layer_delete("empty")) == {"ok": True, "deleted": "EMPTY"}
    assert "EMPTY" not in backend._doc.layers


# ── #13 offset ──────────────────────────────────────────────────────────────


def _points(info):
    return [tuple(round(v, 6) for v in p) for p in info.properties["points"]]


async def test_offset_closed_polyline_grows_by_default_and_shrinks_towards_inside(backend):
    rect = await backend.entity_create_polyline(
        [[10, 10], [40, 10], [40, 30], [10, 30]], closed=True
    )
    grown = await backend.entity_offset(rect.handle, 2)
    shrunk = await backend.entity_offset(rect.handle, 2, 20, 20)
    assert _points(grown) == [(8, 8), (42, 8), (42, 32), (8, 32)]
    assert _points(shrunk) == [(12, 12), (38, 12), (38, 28), (12, 28)]


async def test_offset_clockwise_outline_keeps_the_same_sides(backend):
    rect = await backend.entity_create_polyline(
        [[10, 10], [10, 30], [40, 30], [40, 10]], closed=True
    )
    grown = await backend.entity_offset(rect.handle, 1)
    xs = [p[0] for p in _points(grown)]
    assert min(xs) == 9 and max(xs) == 41


async def test_offset_open_polyline_follows_the_side_point(backend):
    path = await backend.entity_create_polyline([[0, 0], [10, 0], [10, 10]])
    left = await backend.entity_offset(path.handle, 1)
    right = await backend.entity_offset(path.handle, 1, 5, -3)
    assert _points(left) == [(0, 1), (9, 1), (9, 10)]
    assert _points(right) == [(0, -1), (11, -1), (11, 10)]


async def test_offset_refuses_a_collapse_and_an_arc_segment(backend):
    rect = await backend.entity_create_polyline(
        [[10, 10], [40, 10], [40, 30], [10, 30]], closed=True
    )
    with pytest.raises(ValueError, match="too large"):
        await backend.entity_offset(rect.handle, 50, 20, 20)
    bulged = backend._msp().add_lwpolyline([(0, 0, 0, 0, 1.0), (10, 0)], format="xyseb")
    with pytest.raises(ValueError, match="bulged"):
        await backend.entity_offset(bulged.dxf.handle, 1)


async def test_offset_arc_changes_the_radius_only(backend):
    arc = await backend.entity_create_arc(0, 0, 10, 0, 90)
    inner = await backend.entity_offset(arc.handle, 2, 1, 1)
    assert inner.properties["radius"] == pytest.approx(8.0)
    assert inner.properties["start_angle"] == pytest.approx(0.0)


# ── true colour ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["#25282A", "25282a", "37,40,42", [37, 40, 42], (37, 40, 42)])
async def test_true_color_spellings(value):
    assert parse_true_color(value) == (37, 40, 42)
    assert true_color_hex((37, 40, 42)) == "#25282A"


@pytest.mark.parametrize("value", ["#25282", "red", [1, 2], [0, 0, 256], [1.5, 2, 3], None])
async def test_true_color_refuses_non_colours(value):
    with pytest.raises(ValueError, match="true_color"):
        parse_true_color(value)


async def test_entity_true_color_round_trips_and_an_aci_write_removes_it(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)
    result = await backend.entity_set_true_color(line.handle, "#25282A")
    assert result["true_color"] == "#25282A"
    assert (await backend.entity_get(line.handle)).properties["true_color"] == "#25282A"
    await backend.entity_set_properties(line.handle, color=1)
    info = await backend.entity_get(line.handle)
    assert "true_color" not in info.properties and info.color == 1


async def test_layer_true_color(backend):
    await backend.layer_create("PMS426")
    result = await backend.layer_set_true_color("PMS426", [37, 40, 42])
    assert result == {"ok": True, "layer": "PMS426", "true_color": "#25282A"}
    assert backend._doc.layers.get("PMS426").rgb == (37, 40, 42)


# ── text style faces ────────────────────────────────────────────────────────


async def test_textstyle_bold_italic_writes_the_extended_font_data(backend):
    result = await backend.textstyle_create("LBL_BOLD", "arial.ttf", bold=True, italic=False)
    assert result["bold"] is True and result["italic"] is False
    family, italic, bold = backend._doc.styles.get("LBL_BOLD").get_extended_font_data()
    assert family and bold is True and italic is False


async def test_textstyle_without_face_flags_writes_no_font_data(backend):
    await backend.textstyle_create("LBL_PLAIN", "arial.ttf")
    assert not backend._doc.styles.get("LBL_PLAIN").has_extended_font_data


async def test_textstyle_bold_is_refused_for_an_shx_font(backend):
    with pytest.raises(ValueError, match="SHX"):
        await backend.textstyle_create("LBL_SHX", "isocp.shx", bold=True)


# ── MTEXT layout ────────────────────────────────────────────────────────────


async def test_mtext_attachment_and_line_spacing(backend):
    info = await backend.entity_create_mtext(
        "LABEL", 15, 26, width=30, height=4, attachment="middle_center", line_spacing=0.8
    )
    entity = backend._doc.entitydb.get(info.handle)
    assert entity.dxf.attachment_point == 5
    assert entity.dxf.line_spacing_factor == pytest.approx(0.8)
    assert tuple(entity.dxf.insert)[:2] == (15, 26)


@pytest.mark.parametrize(
    ("attachment", "code"), [("tl", 1), ("CENTER", 5), ("bottom-right", 9), ("top centre", 2)]
)
async def test_mtext_attachment_spellings(attachment, code):
    assert resolve_mtext_layout(attachment, None) == (code, None)


@pytest.mark.parametrize(("attachment", "spacing"), [("middle", None), (None, 5.0), (None, "x")])
async def test_mtext_layout_refusals(attachment, spacing):
    with pytest.raises(ValueError):
        resolve_mtext_layout(attachment, spacing)


# ── DXF release ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("version", "expected"),
    [("R12", "AC1009"), ("r2000", "AC1015"), ("2018", "AC1032"), ("AC1027", "AC1027")],
)
async def test_dxf_version_spellings(version, expected):
    assert resolve_dxf_version(version)[1] == expected


async def test_dxf_version_refuses_an_unknown_release():
    with pytest.raises(ValueError, match="R12"):
        resolve_dxf_version("R14")


@pytest.mark.parametrize(("version", "acadver"), [("R12", "AC1009"), ("R2000", "AC1015")])
async def test_export_dxf_in_an_older_release_is_a_copy(backend, tmp_path, version, acadver):
    await backend.entity_create_polyline([[0, 0], [30, 0], [30, 52], [0, 52]], closed=True)
    await backend.entity_create_mtext("CUT", 5, 5)
    own = backend._doc.dxfversion
    target = tmp_path / f"label_{version}.dxf"
    result = await backend.drawing_export_dxf(str(target), version)
    assert result["acadver"] == acadver == read_acadver(target)
    assert result["document_rebound"] is False
    assert backend._doc.dxfversion == own, "the open drawing keeps its release"
    reread = ezdxf.readfile(str(target))
    kinds = {e.dxftype() for e in reread.modelspace()}
    assert ("POLYLINE" in kinds) if version == "R12" else ("LWPOLYLINE" in kinds)


# ── #6 / #7 full-bleed media ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("paper", "orientation", "media"),
    [
        ("ANSI_B", "landscape", "ANSI_full_bleed_B_(17.00_x_11.00_Inches)"),
        ("A4", "portrait", "ISO_full_bleed_A4_(210.00_x_297.00_MM)"),
    ],
)
async def test_full_bleed_media_names(paper, orientation, media):
    assert canonical_media_name(paper, orientation, full_bleed=True) == media
    setup = resolve_page_setup(paper, orientation, full_bleed=True)
    assert setup["canonical_media_name"] == media
    assert setup["margins_mm"] == [0.0, 0.0, 0.0, 0.0] and setup["full_bleed"] is True


async def test_full_bleed_refuses_contradicting_margins():
    with pytest.raises(ValueError, match="full_bleed"):
        resolve_page_setup("A4", "portrait", margins_mm=[5, 5, 5, 5], full_bleed=True)


async def test_full_bleed_page_setup_headless_has_no_margins(backend):
    result = await backend.page_setup_apply(
        "Layout1", resolve_page_setup("ANSI_B", "landscape", scale="1:1", full_bleed=True)
    )
    row = (await backend.page_setup_list("Layout1"))[0]
    assert result["ok"] is True
    assert row["margins_mm"] == [0.0, 0.0, 0.0, 0.0]
    assert row["canonical_media_name"].startswith("ANSI_full_bleed_B")


# ── COM: atomic creates (#1) ────────────────────────────────────────────────


class _FakeEntity:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def Delete(self):
        self.log.append(("delete", self.name))


async def test_com_created_deletes_what_it_made_when_the_block_fails():
    from backends.com_backend import _com_created

    log: list = []
    with pytest.raises(RuntimeError, match="refused"):
        with _com_created() as made:
            made(_FakeEntity(log, "hatch"))
            made(_FakeEntity(log, "boundary"))
            raise RuntimeError("AutoCAD refused Layer =")
    assert log == [("delete", "boundary"), ("delete", "hatch")]


async def test_com_created_names_what_it_could_not_remove():
    from backends.com_backend import _com_created

    class Stuck:
        Handle = "2F"

        def Delete(self):
            raise RuntimeError("Call was rejected by callee")

    with pytest.raises(RuntimeError, match="2F could NOT be removed") as caught:
        with _com_created() as made:
            made(Stuck())
            raise RuntimeError("AutoCAD refused Layer =")
    assert "refused Layer" in str(caught.value)


async def test_com_created_keeps_entities_when_the_block_completes():
    from backends.com_backend import _com_created

    log: list = []
    with _com_created() as made:
        made(_FakeEntity(log, "line"))
    assert log == []


async def test_com_line_create_is_atomic(monkeypatch):
    """A property write refused after AddLine removes the line (no duplicate on retry)."""
    from backends import com_backend as module

    log: list = []

    class Line(_FakeEntity):
        def __setattr__(self, attr, value):
            if attr == "Layer":
                raise RuntimeError("Call was rejected by callee")
            object.__setattr__(self, attr, value)

    space = types.SimpleNamespace(AddLine=lambda a, b: Line(log, "line"))
    monkeypatch.setattr(module, "_msp", lambda: space)
    monkeypatch.setattr(module, "_apoint", lambda *a: a)
    backend = module.ComBackend()

    async def _inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _inline)
    with pytest.raises(RuntimeError, match="rejected"):
        await backend.entity_create_line(0, 0, 1, 1, layer="CUT")
    assert log == [("delete", "line")]


# ── COM: message filter (#1 / #5) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("tick", "kind", "budget", "expected"),
    [
        (0, com_message_filter.SERVERCALL_RETRYLATER, 15000, 100),
        (999, com_message_filter.SERVERCALL_REJECTED, 15000, 100),
        (1000, com_message_filter.SERVERCALL_RETRYLATER, 15000, 250),
        (15000, com_message_filter.SERVERCALL_RETRYLATER, 15000, com_message_filter.CANCEL_CALL),
        (10, com_message_filter.SERVERCALL_RETRYLATER, 0, com_message_filter.CANCEL_CALL),
        (10, com_message_filter.SERVERCALL_ISHANDLED, 15000, com_message_filter.CANCEL_CALL),
    ],
)
async def test_retry_policy(tick, kind, budget, expected):
    assert com_message_filter.retry_delay_ms(tick, kind, budget) == expected


async def test_retry_policy_never_asks_for_an_immediate_retry():
    delays = {
        com_message_filter.retry_delay_ms(t, com_message_filter.SERVERCALL_RETRYLATER, 60000)
        for t in range(0, 60000, 37)
    }
    assert all(d >= 100 for d in delays), "a delay below 100 ms means 'retry at once' to COM"


@pytest.mark.skipif(sys.platform != "win32", reason="CoRegisterMessageFilter is Windows-only")
async def test_message_filter_registers_on_an_sta_thread_and_revokes():
    pythoncom = pytest.importorskip("pythoncom")
    out: dict = {}

    def worker():
        # Other tests' COM executors may hold filters on their own threads, so
        # this one reads its own thread's state rather than the process totals.
        tid = threading.get_ident()
        pythoncom.CoInitialize()
        try:
            out["registered"] = com_message_filter.register(2.0)
            out["again"] = com_message_filter.register(2.0)
            with com_message_filter.budget_override(500):
                out["overridden"] = com_message_filter._STATES[tid].budget_ms
            out["restored"] = com_message_filter._STATES[tid].budget_ms
            com_message_filter.revoke()
            out["after"] = int(tid in com_message_filter._STATES)
        finally:
            pythoncom.CoUninitialize()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert out == {
        "registered": True,
        "again": True,
        "overridden": 500,
        "restored": 2000,
        "after": 0,
    }


async def test_message_filter_is_off_with_a_zero_budget():
    assert com_message_filter.register(0) is False


# ── COM: refusal message and diagnosis (#4) ─────────────────────────────────


async def test_rejected_call_message_names_the_causes_and_the_way_out(monkeypatch):
    from backends import com_backend as module

    monkeypatch.setattr(module, "_windows_session_locked", lambda: False)
    text = module._rejected_call_message(15)
    assert "did NOT run" in text and "press ESC" in text and "system_status" in text
    monkeypatch.setattr(module, "_windows_session_locked", lambda: True)
    assert "session is locked" in module._rejected_call_message(15)


async def test_seat_diagnosis_reports_a_seat_at_a_prompt(monkeypatch):
    from backends import com_backend as module

    monkeypatch.setattr(module, "_windows_session_locked", lambda: False)
    document = types.SimpleNamespace(GetVariable=lambda name: "PEDIT")
    app = types.SimpleNamespace(
        GetAcadState=lambda: types.SimpleNamespace(IsQuiescent=False),
        ActiveDocument=document,
    )
    diagnosis = module._seat_diagnosis(app)
    assert diagnosis["quiescent"] is False and diagnosis["active_commands"] == "PEDIT"
    assert "ESC" in diagnosis["hint"]


# ── COM: 16-bit system variables (#9) ───────────────────────────────────────


async def test_set_short_variable_retries_a_refused_int_as_16_bit(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    sent: list = []

    class Doc:
        def SetVariable(self, name, value):
            sent.append(type(value).__name__)
            if isinstance(value, int):
                raise RuntimeError("Exception occurred")

    module._com_set_short_variable(Doc(), "LAYOUTCREATEVIEWPORT", 0)
    assert sent == ["int", "VARIANT"]


async def test_set_short_variable_reports_the_first_refusal_when_both_fail():
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    class Doc:
        def SetVariable(self, name, value):
            raise RuntimeError("int refused" if isinstance(value, int) else "short refused")

    with pytest.raises(RuntimeError, match="int refused"):
        module._com_set_short_variable(Doc(), "X", 1)


# ── COM: page setup keeps the layout's units under paper_units=None (#7) ────


async def test_com_page_setup_puts_back_units_a_media_write_moved(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module
    from tests.test_page_setup import MEDIA, _FakeLayout, _FakeLayouts

    class InchResettingLayout(_FakeLayout):
        def __setattr__(self, attr, value):
            super().__setattr__(attr, value)
            if attr == "CanonicalMediaName" and "Inches" in str(value):
                self._props["PaperUnits"] = 0  # what AutoCAD does (report #7)

    layout = InchResettingLayout("Layout1", MEDIA)
    document = types.SimpleNamespace(Layouts=_FakeLayouts([_FakeLayout("Model", MEDIA), layout]))
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    backend = module.ComBackend()

    async def _inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _inline)
    await backend.page_setup_apply("Layout1", resolve_page_setup("ANSI_B", paper_units=None))
    assert layout.PaperUnits == 1, "the layout's millimetres survive the inch-named media"


async def test_com_page_setup_accepts_full_bleed_margins(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module
    from tests.test_page_setup import _FakeLayout, _FakeLayouts

    media = ("ANSI_full_bleed_B_(17.00_x_11.00_Inches)",)
    layout = _FakeLayout("Layout1", media)
    document = types.SimpleNamespace(Layouts=_FakeLayouts([_FakeLayout("Model", media), layout]))
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    backend = module.ComBackend()

    async def _inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _inline)
    result = await backend.page_setup_apply(
        "Layout1", resolve_page_setup("ANSI_B", scale="1:1", full_bleed=True)
    )
    assert result["ok"] is True
    assert layout.CanonicalMediaName == media[0] and layout.PaperUnits == 1


# ── COM: viewport rows are un-narrowed (#8) ─────────────────────────────────


async def test_com_viewports_are_redispatched_on_their_concrete_class(monkeypatch):
    from backends import com_backend as module

    seen: list = []
    monkeypatch.setattr(module, "_com_unnarrow", lambda obj: seen.append(obj) or obj)
    items = [
        types.SimpleNamespace(ObjectName="AcDbViewport", Handle="A1"),
        types.SimpleNamespace(ObjectName="AcDbLine", Handle="A2"),
    ]
    block = types.SimpleNamespace(Count=len(items), Item=lambda i: items[i])
    found = module.ComBackend._com_viewports(types.SimpleNamespace(Block=block))
    assert [vp.Handle for vp in found] == ["A1"] and seen == [items[0]]


# ── TOOL_INCLUDE ────────────────────────────────────────────────────────────


async def test_tool_include_adds_tools_to_the_lean_profile(monkeypatch):
    import server

    registered = {tool.name for tool in await server._registered_tools()}
    monkeypatch.setattr(config.settings, "tool_include", ["entity_create_hatch", "no_such_tool"])
    enabled = server._profile_enabled_names("lean", registered)
    assert "entity_create_hatch" in enabled
    assert "entity_create_hatch" not in server.LEAN_TOOL_NAMES
    report = server._tool_include_report(registered)
    assert report["ignored"] == ["no_such_tool"]


async def test_tool_include_cannot_unhide_disabled_3d_tools(monkeypatch):
    import server

    registered = {tool.name for tool in await server._registered_tools()}
    solid = sorted(server.SOLID_TOOL_NAMES)[0]
    monkeypatch.setattr(config.settings, "tool_include", [solid])
    monkeypatch.setattr(config.settings, "enable_3d", False)
    assert solid not in server._profile_enabled_names("lean", registered)
    assert server._tool_include_report(registered)["blocked"] == [solid]
