"""The ``property_override`` critique focus and its repair (plan 1.8-01).

An entity on an engineering layer is expected to take its colour, linetype and
lineweight from the layer. Rival claim 4.5: 22 entities left at lineweight -3
(Default, *not* ByLayer) and ``drawing_critique(focus=None)`` could not see it.
The focus reports one issue per layer and code (``lineweight_not_bylayer``,
``color_not_bylayer``, ``linetype_not_bylayer``) with the count and the handles;
an explicit lineweight outside the ISO 128 set is an error. It reads the run's
all-spaces snapshot, so it covers both engines with one reader.

Scope: the engineering layers plus the applied layer set; the title-block layer
and block definitions (layer-0 members included) are exempt; any other layer is
reported at ``info``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from backends.ezdxf_backend import EzdxfBackend
from engineering.critique import run_critique
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.qa_scan import qa_snapshot
from engineering.refiner import RepairRegistry, refine_drawing

FOCUS = ["property_override"]


@pytest_asyncio.fixture
async def backend():
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    await b.drawing_apply_iso_layers("mech")
    return b


def _by(issues, layer, code):
    found = [i for i in issues if i.detail.get("layer") == layer and i.detail.get("code") == code]
    assert len(found) <= 1, found
    return found[0] if found else None


async def _line(backend, y, layer="GEOMETRY", **props):
    line = await backend.entity_create_line(0, y, 10, y, layer=layer)
    if props:
        await backend.entity_set_properties(line.handle, **props)
    return line.handle


def test_the_focus_is_registered():
    assert "property_override" in ALL_CRITIQUE_FOCUSES
    assert RepairRegistry(defaults=True).get("property_override") is not None


@pytest.mark.asyncio
async def test_a_clean_drawing_reports_nothing(backend):
    await _line(backend, 0)
    await _line(backend, 1, layer="CENTER")
    # ezdxf's add_hatch / set_pattern_fill default to an explicit ACI 7; the
    # headless hatch is created ByLayer, as ActiveX AddHatch leaves it.
    hatch = await backend.entity_create_hatch(
        "ANSI31", [(0, 10), (10, 10), (10, 20), (0, 20)], layer="HATCH"
    )
    assert backend._doc.entitydb[hatch.handle].dxf.color == 256
    assert await run_critique(backend, FOCUS) == []


@pytest.mark.asyncio
async def test_default_lineweight_is_not_bylayer_grouped_per_layer(backend):
    default = [await _line(backend, y, lineweight=-3) for y in range(22)]
    clean = await _line(backend, 30, lineweight=-1)

    issues = await run_critique(backend, FOCUS)
    assert len(issues) == 1
    (issue,) = issues
    assert issue.focus == "property_override"
    assert issue.severity == "warning"  # -3 is a sentinel, not an off-standard width
    assert issue.detail["code"] == "lineweight_not_bylayer"
    assert issue.detail["layer"] == "GEOMETRY"
    assert issue.detail["count"] == 22
    assert issue.detail["values"] == {"Default": 22}
    assert sorted(issue.handles) == sorted(default)
    assert clean not in issue.handles


@pytest.mark.asyncio
async def test_an_off_standard_explicit_lineweight_is_an_error(backend):
    default = await _line(backend, 0, lineweight=-3)
    thirty = await _line(backend, 1, lineweight=30)  # 0.30 mm is not ISO 128

    (issue,) = await run_critique(backend, FOCUS)
    assert issue.severity == "error"
    assert issue.detail["values"] == {"Default": 1, "0.30 mm": 1}
    assert issue.detail["count"] == 2
    assert sorted(issue.handles) == sorted([default, thirty])


@pytest.mark.asyncio
async def test_an_iso_explicit_lineweight_is_still_an_override(backend):
    handle = await _line(backend, 0, lineweight=50)
    (issue,) = await run_critique(backend, FOCUS)
    assert issue.severity == "warning"
    assert issue.handles == [handle]


@pytest.mark.asyncio
async def test_colour_and_linetype_overrides(backend):
    red = await _line(backend, 0, color=1)
    hidden = await _line(backend, 1, linetype="HIDDEN")
    byblock = await _line(backend, 2, color=0)

    issues = await run_critique(backend, FOCUS)
    colour = _by(issues, "GEOMETRY", "color_not_bylayer")
    linetype = _by(issues, "GEOMETRY", "linetype_not_bylayer")
    assert colour is not None and linetype is not None
    assert sorted(colour.handles) == sorted([red, byblock])
    assert colour.detail["values"] == {"1": 1, "ByBlock": 1}
    assert linetype.handles == [hidden]
    assert linetype.detail["values"] == {"HIDDEN": 1}
    assert len(issues) == 2


@pytest.mark.asyncio
async def test_titleblock_and_block_members_are_exempt(backend):
    await _line(backend, 0, layer="TITLEBLOCK", lineweight=-3, color=1)
    doc = backend._doc
    block = doc.blocks.new("LW_PART")
    block.add_line((0, 0), (5, 0), dxfattribs={"layer": "0", "lineweight": -3, "color": 3})
    doc.modelspace().add_blockref("LW_PART", (0, 50), dxfattribs={"layer": "GEOMETRY"})
    assert await run_critique(backend, FOCUS) == []


@pytest.mark.asyncio
async def test_a_foreign_layer_is_info(backend):
    await backend.layer_create("XREF-SURVEY", color=5)
    handle = await _line(backend, 0, layer="XREF-SURVEY", lineweight=-3)

    (issue,) = await run_critique(backend, FOCUS)
    assert issue.severity == "info"
    assert issue.detail["layer"] == "XREF-SURVEY"
    assert issue.detail["in_scope"] is False
    assert issue.handles == [handle]


@pytest.mark.asyncio
async def test_the_applied_layer_set_is_in_scope(backend):
    await backend.drawing_apply_iso_layers("pid")
    await _line(backend, 0, layer="PROCESS-PIPING-MAIN", lineweight=-3)
    (issue,) = await run_critique(backend, FOCUS)
    assert issue.severity == "warning"
    assert issue.detail["in_scope"] is True


@pytest.mark.asyncio
async def test_runs_under_focus_none(backend):
    await _line(backend, 0, lineweight=-3)
    issues = await run_critique(backend, None)
    assert [i.detail["code"] for i in issues if i.focus == "property_override"] == [
        "lineweight_not_bylayer"
    ]


@pytest.mark.asyncio
async def test_refine_restores_bylayer_and_the_focus_comes_back_empty(backend):
    await backend.layer_create("XREF-SURVEY", color=5)
    handles = [await _line(backend, y, lineweight=-3) for y in range(3)]
    handles.append(await _line(backend, 5, lineweight=30))
    handles.append(await _line(backend, 6, color=1, linetype="HIDDEN"))
    handles.append(await _line(backend, 7, layer="XREF-SURVEY", lineweight=-1, color=2))
    keep = await _line(backend, 8)

    result = await refine_drawing(backend, focus=FOCUS, min_score=100.0)
    assert result.status == "threshold_met", result.to_dict()
    assert await run_critique(backend, FOCUS) == []
    for handle in handles + [keep]:
        entity = backend._doc.entitydb[handle]
        assert entity.dxf.get("lineweight", -1) == -1
        assert entity.dxf.get("color", 256) == 256
        assert entity.dxf.get("linetype", "BYLAYER").upper() == "BYLAYER"
        assert entity.dxf.layer in ("GEOMETRY", "XREF-SURVEY")  # nothing moved layers


@pytest.mark.asyncio
async def test_refine_covers_more_entities_than_the_issue_lists(backend):
    """The issue lists at most 50 handles; the repair re-reads the layer."""
    for y in range(60):
        await _line(backend, y, lineweight=-3)
    (issue,) = await run_critique(backend, FOCUS)
    assert issue.detail["count"] == 60 and len(issue.handles) == 50
    await refine_drawing(backend, focus=FOCUS, min_score=100.0)
    assert await run_critique(backend, FOCUS) == []


# -- the snapshot carries what the focus reads -----------------------------------


@pytest.mark.asyncio
async def test_snapshot_records_and_layers_carry_the_properties(backend):
    line = await _line(backend, 0, lineweight=-3, color=1, linetype="HIDDEN")
    plain = await _line(backend, 1)
    backend._doc.entitydb[line].transparency = 0.5
    await backend.layer_modify("CENTER", lineweight=35)
    await backend.layer_hide("HIDDEN")

    snap, coverage = await qa_snapshot(backend, {})
    assert coverage.complete
    records = {r.handle: r for r in snap.records}
    assert records[line].lineweight == -3
    assert records[line].color == 1
    assert records[line].linetype.upper() == "HIDDEN"
    assert records[line].transparency is not None
    assert records[plain].lineweight == -1
    assert records[plain].color == 256
    assert records[plain].linetype.upper() == "BYLAYER"
    assert records[plain].transparency is None

    center = snap.layers["CENTER"]
    assert center["lineweight"] == 35
    assert center["plot"] is True
    assert center["on"] is True and center["frozen"] is False
    assert snap.layers["HIDDEN"]["on"] is False
