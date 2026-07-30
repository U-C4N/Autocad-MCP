"""Regression tests for the defects found by the full-tool audit sweep.

COM-side behaviour is exercised against mocked COM objects (the same technique as
tests/test_com_sprint3.py); ezdxf-side behaviour runs against a real headless
document, so these assertions are about observable output, not implementation.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

import backends.com_backend as cb
from backends.com_backend import ComBackend
from backends.ezdxf_backend import EzdxfBackend


def _com_backend():
    b = ComBackend()

    async def _run(func, *args, **kwargs):  # bypass the STA executor
        return func(*args, **kwargs)

    b._run = _run
    return b


async def _ezdxf_backend():
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    return b


# ── COM: ObjectName -> DXF type mapping ─────────────────────────────────────


def test_canonical_type_maps_com_names_to_dxf_names():
    # AutoCAD reports "AcDbPolyline" for a lightweight polyline and
    # "AcDbBlockReference" for an insert; callers filter on DXF names.
    assert cb._canonical_type("AcDbPolyline") == "LWPOLYLINE"
    assert cb._canonical_type("AcDbBlockReference") == "INSERT"
    assert cb._canonical_type("AcDb2dPolyline") == "POLYLINE"
    assert cb._canonical_type("AcDbLine") == "LINE"  # unmapped names still work


# ── COM: save format constants ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drawing_save_as_uses_2010_format_constants(monkeypatch):
    doc = MagicMock()
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    await _com_backend().drawing_save_as("C:/tmp/x.dwg", "dwg")
    assert doc.SaveAs.call_args[0][1] == 48  # ac2010_dwg, not ac2000_dwg(12)

    await _com_backend().drawing_save_as("C:/tmp/x.dxf", "dxf")
    assert doc.SaveAs.call_args[0][1] == 49  # ac2010_dxf, not ac2013_dxf(61)

    await _com_backend().drawing_save_as("C:/tmp/x.dwt", "dwt")
    assert doc.SaveAs.call_args[0][1] == 50  # ac2010_Template, not acR13_dxf(5)


# ── COM: audit must not drive the command line ──────────────────────────────


@pytest.mark.asyncio
async def test_drawing_audit_uses_activex_audit_not_sendcommand(monkeypatch):
    doc = MagicMock()
    doc.Audit.return_value = 3
    doc.SendCommand.side_effect = AssertionError("SendCommand hangs on fresh documents")
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    out = await _com_backend().drawing_audit()

    doc.Audit.assert_called_once_with(True)
    assert out["error_count"] == 3


# ── COM: exploding a block must remove the original ─────────────────────────


@pytest.mark.asyncio
async def test_block_explode_deletes_the_original_insert(monkeypatch):
    child_a, child_b = MagicMock(Handle="AA"), MagicMock(Handle="BB")
    ent = MagicMock()
    ent.Explode.return_value = [child_a, child_b]
    doc = MagicMock()
    doc.HandleToObject.return_value = ent
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(cb, "_regen", lambda: None)

    out = await _com_backend().block_explode("1F")

    ent.Delete.assert_called_once()  # Explode() alone leaves the INSERT behind
    assert out["inserted_handles"] == ["AA", "BB"]
    assert out["entity_count"] == 2


# ── COM: assigning an unknown layer must not be a hard error ────────────────


def test_apply_entity_attrs_creates_a_missing_layer(monkeypatch):
    doc = MagicMock()
    doc.Layers.Count = 1
    doc.Layers.Item.return_value = MagicMock(Name="0")
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    entity = MagicMock()
    cb._apply_entity_attrs(entity, "ЛВС_ТД", None, None)

    doc.Layers.Add.assert_called_once_with("ЛВС_ТД")
    assert entity.Layer == "ЛВС_ТД"


def test_apply_entity_attrs_skips_creation_for_existing_layer(monkeypatch):
    doc = MagicMock()
    doc.Layers.Count = 1
    doc.Layers.Item.return_value = MagicMock(Name="GEOMETRY")
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)

    cb._apply_entity_attrs(MagicMock(), "geometry", None, None)  # case-insensitive

    doc.Layers.Add.assert_not_called()


# ── COM: geometry edits keep the entity's elevation ─────────────────────────


@pytest.mark.asyncio
async def test_entity_edit_geometry_preserves_z(monkeypatch):
    ent = MagicMock()
    ent.ObjectName = "AcDbCircle"
    ent.Center = (10.0, 20.0, 7.5)
    doc = MagicMock()
    doc.HandleToObject.return_value = ent
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(cb, "_regen", lambda: None)
    monkeypatch.setattr(cb, "_entity_info", lambda e: "info")
    captured = {}
    monkeypatch.setattr(cb, "_apoint", lambda x, y, z=0.0: captured.setdefault("pt", (x, y, z)))

    await _com_backend().entity_edit_geometry("2A", cx=1.0)

    assert captured["pt"] == (1.0, 20.0, 7.5)  # z carried over, not reset to 0


# ── COM: bounding box measures the entities, not a stale header ─────────────


@pytest.mark.asyncio
async def test_analysis_bounding_box_measures_live_entities(monkeypatch):
    e1, e2 = MagicMock(), MagicMock()
    e1.GetBoundingBox.return_value = ((0.0, 0.0, 0.0), (10.0, 5.0, 0.0))
    e2.GetBoundingBox.return_value = ((-4.0, 2.0, 0.0), (6.0, 20.0, 0.0))
    mspace = MagicMock()
    mspace.Count = 2
    mspace.Item.side_effect = [e1, e2]
    monkeypatch.setattr(cb, "_msp", lambda: mspace)

    out = await _com_backend().analysis_bounding_box()

    assert out["min"] == [-4.0, 0.0]
    assert out["max"] == [10.0, 20.0]
    assert out["width"] == 14.0 and out["height"] == 20.0


@pytest.mark.asyncio
async def test_analysis_bounding_box_on_empty_drawing(monkeypatch):
    mspace = MagicMock()
    mspace.Count = 0
    monkeypatch.setattr(cb, "_msp", lambda: mspace)

    assert (await _com_backend().analysis_bounding_box())["empty"] is True


# ── ezdxf: aligned dimension offset ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dimension_aligned_offset_is_perpendicular_distance():
    b = await _ezdxf_backend()
    # Horizontal 100mm edge, dimension point 5mm below its midpoint.
    info = await b.dimension_aligned(0, 0, 100, 0, 50, -5)

    dim = b._get_entity(info.handle)
    defpoint = dim.dxf.defpoint
    # The dimension line must sit 5mm from the measured edge, not 50+.
    assert math.isclose(abs(defpoint.y), 5.0, abs_tol=0.01)


# ── ezdxf: closing an unsaved drawing must not swallow the work ─────────────


@pytest.mark.asyncio
async def test_drawing_close_refuses_to_discard_unsaved_work():
    b = await _ezdxf_backend()
    await b.entity_create_line(0, 0, 10, 0)

    with pytest.raises(RuntimeError, match="never been saved"):
        await b.drawing_close(save=True)

    assert (await b.drawing_close(save=False))["ok"] is True  # explicit discard is fine


# ── ezdxf: layer_delete guards ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_layer_delete_guards():
    b = await _ezdxf_backend()
    await b.layer_create("USED")
    await b.entity_create_line(0, 0, 1, 0, layer="USED")
    await b.layer_create("FREE")

    with pytest.raises(RuntimeError, match="cannot be deleted"):
        await b.layer_delete("0")
    with pytest.raises(RuntimeError, match="still holds 1 entity"):
        await b.layer_delete("USED")
    with pytest.raises(RuntimeError, match="does not exist"):
        await b.layer_delete("NOPE")

    assert (await b.layer_delete("FREE"))["ok"] is True


# ── ezdxf: region query is a crossing selection ─────────────────────────────


@pytest.mark.asyncio
async def test_find_in_region_is_a_crossing_selection():
    b = await _ezdxf_backend()
    await b.entity_create_line(-50, 5, 50, 5)  # crosses the window, not contained

    hits = await b.analysis_entities_in_region(0, 0, 10, 10)

    assert len(hits) == 1


# ── ezdxf: composites move and die as one ───────────────────────────────────


@pytest.mark.asyncio
async def test_table_delete_removes_every_child():
    b = await _ezdxf_backend()
    table = await b.entity_create_table(0, 100, [["a", "1"], ["b", "2"]], headers=["k", "v"])
    children = table.properties["child_handles"]
    assert len(children) > 1

    out = await b.entity_delete(table.handle)

    assert out["entity_count"] == len(children)
    remaining = {e.handle for e in await b.entity_list(limit=1000)}
    assert not remaining.intersection(children)


@pytest.mark.asyncio
async def test_table_move_moves_every_child():
    b = await _ezdxf_backend()
    table = await b.entity_create_table(0, 100, [["a", "1"]], headers=["k", "v"])
    children = table.properties["child_handles"]
    before = await b.analysis_bounding_box()

    out = await b.entity_move(table.handle, 25, 0)

    assert out["entity_count"] == len(children)
    after = await b.analysis_bounding_box()
    assert math.isclose(after["min"][0] - before["min"][0], 25.0, abs_tol=0.01)


# ── ezdxf: a binary template gets an honest error ───────────────────────────


@pytest.mark.asyncio
async def test_drawing_new_with_binary_template_explains_itself(tmp_path):
    fake_dwt = tmp_path / "corporate.dwt"
    fake_dwt.write_bytes(b"AC1032" + b"\x00" * 64)  # real DWG signature, not DXF

    b = EzdxfBackend()
    await b.connect()
    with pytest.raises(RuntimeError, match="not a DXF file"):
        await b.drawing_new(template=str(fake_dwt))


# ── COM: MLeader creation unwraps AddMLeader's out-parameter ────────────────


@pytest.mark.asyncio
async def test_leader_create_mleader_unwraps_addmleader_tuple(monkeypatch):
    leader = MagicMock()
    mspace = MagicMock()
    # AddMLeader's second argument is by-ref, so pywin32 returns (entity, index).
    mspace.AddMLeader.return_value = (leader, 0)
    monkeypatch.setattr(cb, "_msp", lambda: mspace)
    monkeypatch.setattr(cb, "_apply_entity_attrs", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_regen", lambda: None)
    monkeypatch.setattr(
        cb,
        "_entity_info",
        lambda e: cb.EntityInfo(
            handle="7F",
            type="MULTILEADER",
            layer="DIM",
            color=256,
            linetype="ByLayer",
            visible=True,
            properties={},
        ),
    )

    info = await _com_backend().leader_create_mleader([[0, 0], [10, 10]], "note")

    assert leader.TextString == "note"  # would raise AttributeError on the tuple
    assert info.type == "MLEADER"


# ── COM: layer/region queries do not use late-bound SelectionSets ───────────


@pytest.mark.asyncio
async def test_select_by_layer_scans_modelspace(monkeypatch):
    a = MagicMock(Layer="ЛВС_ТД")
    b = MagicMock(Layer="GEOMETRY")
    mspace = MagicMock()
    mspace.Count = 2
    mspace.Item.side_effect = lambda i: [a, b][i]
    doc = MagicMock()
    doc.SelectionSets.Add.side_effect = AssertionError("SelectionSets are not usable late-bound")
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(cb, "_msp", lambda: mspace)
    monkeypatch.setattr(cb, "_entity_info", lambda e: e.Layer)

    assert await _com_backend().analysis_select_by_layer("лвс_тд") == ["ЛВС_ТД"]


@pytest.mark.asyncio
async def test_entities_in_region_is_a_crossing_selection(monkeypatch):
    crossing = MagicMock()
    crossing.GetBoundingBox.return_value = ((-50.0, 5.0, 0.0), (50.0, 5.0, 0.0))
    outside = MagicMock()
    outside.GetBoundingBox.return_value = ((500.0, 500.0, 0.0), (510.0, 510.0, 0.0))
    mspace = MagicMock()
    mspace.Count = 2
    mspace.Item.side_effect = lambda i: [crossing, outside][i]
    doc = MagicMock()
    doc.SelectionSets.Add.side_effect = AssertionError("SelectionSets are not usable late-bound")
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(cb, "_msp", lambda: mspace)
    monkeypatch.setattr(cb, "_entity_info", lambda e: "hit")

    assert await _com_backend().analysis_entities_in_region(0, 0, 10, 10) == ["hit"]


# ── COM: corner ops must not park AutoCAD at a prompt ───────────────────────


@pytest.mark.asyncio
async def test_chamfer_sets_distances_via_sysvars_not_inline_options(monkeypatch):
    doc = MagicMock()
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(cb, "_entity_info", lambda e: "info")
    written = {}
    monkeypatch.setattr(cb, "_set_sysvar", lambda name, value: written.__setitem__(name, value))
    sent = {}
    monkeypatch.setattr(
        ComBackend,
        "_safe_send_command",
        staticmethod(lambda d, cmd, **k: sent.setdefault("cmd", cmd) or []),
    )

    await _com_backend().entity_chamfer("1A", "1B", 5, 7, trim=False)

    assert written["CHAMFERA"] == 5.0 and written["CHAMFERB"] == 7.0
    assert written["TRIMMODE"] == 0
    # Only the two selections may reach the command line: an inline "_D 5 7"
    # option sequence is what hung AutoCAD and poisoned the COM session.
    assert "_D" not in sent["cmd"] and sent["cmd"].count("handent") == 2


def test_safe_send_command_refuses_while_a_prompt_is_open():
    doc = MagicMock()
    doc.GetVariable.side_effect = lambda name: 1 if name == "CMDACTIVE" else 0

    with pytest.raises(RuntimeError, match="waiting for input"):
        ComBackend._safe_send_command(doc, "_CHAMFER\n")

    doc.SendCommand.assert_not_called()  # fail fast instead of blocking 60s
