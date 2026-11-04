"""Dimension truth: the displayed text, the ``*D`` block and the ``dimension_truth`` focus (1.8-15).

v1.7.0 reported a dimension over 19.6 mm on a DIMDEC 0 sheet as "shows 19.6"
(``f"{value:.10g}"``); it shows "20". ``engineering/dimtext.py`` formats the
text the way the drawing does, the snapshot carries what the ``*D`` block
actually shows (``rendered_text``) and group 42, a headless
``dimstyle_modify`` / ``entity_scale`` / ``entity_edit_geometry`` redraws the
dimensions it changes, and the ``dimension_truth`` focus reports every way a
dimension can lie - one code per lie (``tests/fixtures/lying_dims.py``).
"""

from __future__ import annotations

import types

import ezdxf
import pytest
import pytest_asyncio

from backends.entity_fields import dimension_fields, dimension_rendered, dimension_style_vars
from backends.ezdxf_backend import EzdxfBackend
from engineering.critique import run_critique
from engineering.dimension_truth import CODES, REPAIRABLE, hidden_reasons
from engineering.dimtext import display, displayed_text, double_prefix, format_value
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.qa_scan import qa_snapshot
from engineering.refiner import RepairRegistry, refine_drawing
from engineering.verification.binding import bind_dimension
from engineering.verification.measure import default_epsilon
from engineering.verification.observe import observe
from tests.fixtures.lying_dims import CODES as FIXTURE_CODES
from tests.fixtures.lying_dims import build_lying_dims

FOCUS = ["dimension_truth"]


@pytest_asyncio.fixture
async def backend():
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    return b


def _fields(measurement, *, dimtype=0, override="", dimlfac=1.0):
    return (dimtype, (None,) * 5, measurement, override, "Standard", dimlfac, 0.0)


def _style(**values):
    base = {"dimdec": 2, "dimzin": 8, "dimdsep": ".", "dimrnd": 0.0, "dimpost": ""}
    base.update(values)
    return tuple(sorted(base.items()))


def _posts(backend, x0, x1):
    msp = backend._doc.modelspace()
    msp.add_line((x0, 0), (x0, 20))
    msp.add_line((x1, 0), (x1, 20))


def _shown(backend, handle):
    entity = backend._doc.entitydb[handle]
    rendered, group42 = dimension_rendered(entity)
    text = displayed_text(dimension_fields(entity), dimension_style_vars(entity))
    return rendered, text, group42


# -- the formatter -----------------------------------------------------------


def test_dimdec_0_shows_20_over_19_6_and_says_how_far_it_rounded():
    shown = display(_fields(19.6), _style(dimdec=0))
    assert shown.text == "20"
    assert shown.value == 20.0
    assert shown.display_rounding == pytest.approx(0.4, abs=1e-12)
    assert shown.tolerance == pytest.approx(0.5)
    assert displayed_text(_fields(19.6), _style(dimdec=0)) == "20"


@pytest.mark.parametrize(
    ("value", "style", "dimtype", "expected"),
    [
        (19.6, {}, 0, "19.6"),  # DIMZIN 8: trailing zeros suppressed
        (19.6, {"dimzin": 0}, 0, "19.60"),
        (0.5, {"dimzin": 12}, 0, ".5"),  # bit 4: the leading zero too
        (19.6, {"dimdsep": ","}, 0, "19,6"),
        (19.6, {"dimdsep": 44}, 0, "19,6"),  # a DXF character code
        (19.6, {"dimpost": "<> mm"}, 0, "19.6 mm"),
        (19.6, {"dimpost": " mm"}, 0, "19.6 mm"),  # no <>: a suffix
        (19.6, {"dimrnd": 0.5, "dimdec": 1}, 0, "19.5"),
        (19.6, {"dimrnd": 5.0, "dimdec": 0}, 0, "20"),
        (20.0, {}, 3, "Ø20"),  # diameter prefix
        (10.0, {}, 4, "R10"),  # radius prefix
        (45.0, {"dimadec": 0, "dimazin": 0}, 2, "45°"),
        # An exact binary tie (45.25) follows the headless renderer's
        # round-half-even; AutoCAD's tie rule is a live check (S4).
        (45.26, {"dimadec": 1, "dimazin": 2}, 2, "45.3°"),
    ],
)
def test_the_formatter_follows_the_style(value, style, dimtype, expected):
    assert format_value(value, dimtype, _style(**style)) == expected


def test_dimlfac_scales_a_length_never_an_angle():
    assert display(_fields(21.0, dimlfac=2.0), _style()).text == "42"
    angle = display(_fields(45.0, dimtype=2, dimlfac=2.0), _style(dimadec=0, dimazin=0))
    assert angle.text == "45°" and angle.value == 45.0


def test_overrides_keep_or_replace_the_value():
    assert display(_fields(42.0, override="4x <>"), _style()).text == "4x 42"
    replaced = display(_fields(40.0, override="42"), _style())
    assert (replaced.text, replaced.value, replaced.source) == ("42", 42.0, "override")
    suppressed = display(_fields(40.0, override=" "), _style())
    assert (suppressed.text, suppressed.value, suppressed.source) == ("", None, "suppressed")
    limits = display(_fields(40.0), _style(dimlim=1))
    assert limits.source == "limits" and limits.value is None


def test_a_leading_count_is_not_the_size_an_override_claims():
    assert display(_fields(13.5, override="4x ⌀13.5"), _style()).value == 13.5
    assert display(_fields(13.5, override="4x M8"), _style()).value == 8.0
    assert display(_fields(40.0, override="40x20"), _style()).value == 40.0  # a size, not a count


@pytest.mark.asyncio
async def test_a_dimension_measured_on_an_edge_binds_to_that_edge(backend):
    msp = backend._doc.modelspace()
    msp.add_line((0, -10), (0, 30))  # the measured points lie inside these edges
    msp.add_lwpolyline([(40, -10), (40, 30)])
    dim = await backend.dimension_linear(0, 5, 40, 5, 20, -20)
    snap, _ = await qa_snapshot(backend, {})
    observed = observe(snap)
    (feature,) = [f for f in observed.features if f.type == "dimension"]
    result = bind_dimension(feature, observed, epsilon=default_epsilon(observed))
    assert result["status"] == "bound" and len(result["refs"]) == 2
    assert "on an edge" in result["reason"]
    assert result["geometric_value"] == pytest.approx(40.0)
    assert await run_critique(backend, FOCUS) == [], dim.handle


def test_an_unknown_style_keeps_the_v170_full_precision():
    shown = display(_fields(19.6), None)
    assert (shown.text, shown.value, shown.style_known) == ("19.6", 19.6, False)


def test_double_prefix_is_an_own_sign_right_before_the_value():
    assert double_prefix(3, "%%c<>")
    assert double_prefix(3, "Ø <>")
    assert double_prefix(4, "R<>")
    assert not double_prefix(3, "<> H7")
    assert not double_prefix(0, "%%c<>")  # a linear dimension carries no automatic sign
    assert display(_fields(20.0, dimtype=3, override="%%c<>"), _style()).text == "ØØ20"


# -- binding reports what the sheet shows ------------------------------------


@pytest.mark.asyncio
async def test_a_dimdec_0_sheet_reports_20_over_19_6_not_19_6(backend):
    await backend.dimstyle_modify("Standard", {"DIMDEC": 0})
    _posts(backend, 0, 19.6)
    dim = await backend.dimension_linear(0, 0, 19.6, 0, 10, -10)
    snap, _ = await qa_snapshot(backend, {})
    observed = observe(snap)
    (feature,) = [f for f in observed.features if f.type == "dimension"]
    result = bind_dimension(feature, observed, epsilon=default_epsilon(observed))
    assert result["status"] == "bound"
    assert result["geometric_value"] == pytest.approx(19.6)
    assert result["displayed_text"] == "20" and result["displayed_value"] == 20.0
    assert result["display_rounding"] == pytest.approx(0.4, abs=1e-9)
    assert result["mismatch"] is False, "rounding is not a lie"
    rendered, text, _ = _shown(backend, dim.handle)
    assert rendered == text == "20"


# -- the snapshot carries what the *D block shows ----------------------------


@pytest.mark.asyncio
async def test_the_snapshot_record_carries_rendered_text_and_group42(backend):
    _posts(backend, 0, 40)
    dim = await backend.dimension_linear(0, 0, 40, 0, 20, -10)
    backend._doc.entitydb[dim.handle].dxf.actual_measurement = 40.0
    snap, _ = await qa_snapshot(backend, {})
    (record,) = [r for r in snap.records if r.type == "DIMENSION"]
    assert record.rendered_text == "40" and record.group42 == 40.0
    assert dict(record.dim_vars)["dimdec"] == 2
    assert all(not name.upper().startswith("*D") for name in (snap.blocks or {}))


# -- headless redraws --------------------------------------------------------


async def _four_dimensions(backend):
    _posts(backend, 0, 19.6)
    msp = backend._doc.modelspace()
    msp.add_circle((100, 0), 9.8)
    return [
        (await backend.dimension_linear(0, 0, 19.6, 0, 10, -10)).handle,
        (await backend.dimension_aligned(0, 0, 19.6, 0, 10, -20)).handle,
        (await backend.dimension_diameter(90.2, 0, 109.8, 0)).handle,
        (await backend.dimension_radius(100, 0, 109.8, 0)).handle,
    ]


@pytest.mark.asyncio
async def test_dimstyle_modify_redraws_every_dimension_using_the_style(backend):
    handles = await _four_dimensions(backend)
    before = {h: backend._doc.entitydb[h].dxf.geometry for h in handles}
    result = await backend.dimstyle_modify("Standard", {"DIMDEC": 0})
    assert result["changed"] == {"DIMDEC": [2, 0]}
    assert sorted(result["rerendered"]) == sorted(handles)
    assert result["rerender_required"] is False
    for handle in handles:
        rendered, text, _ = _shown(backend, handle)
        assert rendered == text, handle
        assert backend._doc.blocks.get(before[handle]) is None, "the old *D block is purged"
    assert [_shown(backend, h)[0] for h in handles] == ["20", "20", "Ø20", "R10"]
    assert await run_critique(backend, FOCUS) == []


@pytest.mark.asyncio
async def test_rerender_false_is_the_old_behaviour_and_the_focus_sees_it(backend):
    handles = await _four_dimensions(backend)
    result = await backend.dimstyle_modify("Standard", {"DIMDEC": 0}, rerender=False)
    assert result["rerendered"] == [] and result["rerender_required"] is True
    assert _shown(backend, handles[0])[:2] == ("19.6", "20")
    issues = await run_critique(backend, FOCUS)
    assert sorted(i.handles[0] for i in issues) == sorted(handles)
    assert {i.detail["code"] for i in issues} == {"dim_render_stale"}
    assert all(i.severity == "error" and i.detail["repairable"] for i in issues)

    refined = await refine_drawing(backend, focus=FOCUS, min_score=100.0)
    assert refined.remaining_issues == []
    for handle in handles:
        rendered, text, _ = _shown(backend, handle)
        assert rendered == text, handle


@pytest.mark.asyncio
async def test_a_user_placed_text_keeps_its_place_unless_forced(backend):
    msp = backend._doc.modelspace()
    msp.add_circle((100, 0), 9.8)
    dim = await backend.dimension_diameter(90.2, 0, 109.8, 0)
    entity = backend._doc.entitydb[dim.handle]
    assert int(entity.dxf.dimtype) & 128, "a located diameter text is user-placed"
    placed = entity.dxf.text_midpoint
    kept = await backend.dimstyle_modify("Standard", {"DIMDEC": 0})
    assert kept["text_kept"] == [dim.handle]
    assert entity.dxf.text_midpoint.isclose(placed)
    (mtext,) = [e for e in entity.get_geometry_block() if e.dxftype() == "MTEXT"]
    assert mtext.dxf.insert.isclose(placed)
    forced = await backend.dimension_rerender([dim.handle], force=True)
    assert forced["rerendered"] == [dim.handle] and forced["text_kept"] == []


@pytest.mark.asyncio
async def test_entity_scale_redraws_the_number_and_group_42(backend):
    _posts(backend, 0, 40)
    dim = await backend.dimension_linear(0, 0, 40, 0, 20, -10)
    result = await backend.entity_scale(dim.handle, 0, 0, 2)
    assert result["rerendered"] == [dim.handle]
    rendered, text, group42 = _shown(backend, dim.handle)
    assert rendered == text == "80"
    assert group42 == pytest.approx(80.0)
    assert backend._doc.entitydb[dim.handle].get_measurement() == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_entity_edit_geometry_moves_a_dimensions_points_and_redraws_it(backend):
    _posts(backend, 0, 40)
    dim = await backend.dimension_linear(0, 0, 40, 0, 20, -10)
    await backend.entity_edit_geometry(dim.handle, x2=50.0, y2=0.0)
    rendered, text, group42 = _shown(backend, dim.handle)
    assert rendered == text == "50" and group42 == pytest.approx(50.0)
    with pytest.raises(RuntimeError, match="takes x1 / y1 / x2 / y2"):
        await backend.entity_edit_geometry(dim.handle, radius=3.0)
    msp = backend._doc.modelspace()
    msp.add_circle((100, 0), 10)
    diameter = await backend.dimension_diameter(90, 0, 110, 0)
    with pytest.raises(RuntimeError, match="only a linear or aligned"):
        await backend.entity_edit_geometry(diameter.handle, x1=1.0)


@pytest.mark.asyncio
async def test_after_save_and_open_the_d_block_text_is_the_displayed_text(backend, tmp_path):
    await _four_dimensions(backend)
    await backend.dimstyle_modify("Standard", {"DIMDEC": 0})
    path = tmp_path / "dims.dxf"
    await backend.drawing_save_as(str(path))
    doc = ezdxf.readfile(str(path))
    dims = list(doc.modelspace().query("DIMENSION"))
    assert len(dims) == 4
    for entity in dims:
        rendered, _ = dimension_rendered(entity)
        assert rendered == displayed_text(dimension_fields(entity), dimension_style_vars(entity))
    referenced = {e.dxf.geometry for e in dims}
    assert {b.name for b in doc.blocks if b.name.startswith("*D")} == referenced


# -- the focus ---------------------------------------------------------------


def test_the_focus_is_registered():
    assert "dimension_truth" in ALL_CRITIQUE_FOCUSES
    assert RepairRegistry(defaults=True).get("dimension_truth") is not None
    assert set(FIXTURE_CODES) == set(CODES)
    assert REPAIRABLE == {"dim_render_stale", "dim_group42_stale"}


@pytest.mark.asyncio
async def test_one_lying_dimension_per_code_yields_exactly_those_codes(backend):
    made = await build_lying_dims(backend)
    issues = await run_critique(backend, FOCUS)
    got = {(i.detail["code"], i.handles[0]) for i in issues}
    assert got == {(code, handle) for code, handle in made.items()}
    severities = {i.detail["code"]: i.severity for i in issues}
    assert severities == {
        "dim_override_disagrees": "error",
        "dim_dimlfac_scaled": "warning",
        "dim_render_stale": "error",
        "dim_group42_stale": "warning",
        "dim_double_prefix": "warning",
        "dim_value_hidden": "error",
        "dim_unbound": "info",
    }


@pytest.mark.asyncio
async def test_refine_redraws_the_stale_codes_and_leaves_the_rest_to_a_person(backend):
    made = await build_lying_dims(backend)
    refined = await refine_drawing(backend, focus=FOCUS, min_score=100.0)
    left = {i["detail"]["code"] for i in refined.remaining_issues}
    assert left == set(CODES) - REPAIRABLE
    planned = await refine_drawing(backend, focus=FOCUS, dry_run=True)
    statuses = {
        a["detail"].get("code"): a["status"] for a in planned.rounds[0].to_dict()["actions"]
    }
    assert set(statuses.values()) == {"manual_required"}
    assert made["dim_render_stale"] not in {
        h for i in refined.remaining_issues for h in i["handles"]
    }


@pytest.mark.asyncio
async def test_an_nts_override_is_info_and_a_digitless_one_hides_the_value(backend):
    _posts(backend, 0, 40)
    nts = await backend.dimension_linear(0, 0, 40, 0, 20, -10, text_override="42 NTS")
    _posts(backend, 100, 140)
    typ = await backend.dimension_linear(100, 0, 140, 0, 120, -10, text_override="TYP")
    issues = {
        (i.handles[0], i.detail["code"]): i.severity for i in await run_critique(backend, FOCUS)
    }
    assert issues == {
        (nts.handle, "dim_override_disagrees"): "info",
        (typ.handle, "dim_value_hidden"): "error",
    }


def test_every_hidden_reason_is_named():
    record = types.SimpleNamespace(
        dimension=_fields(40.0),
        dim_vars=_style(dimtxt=0.0, dimtfill=2, dimtfillclr=3, dimclrt=3),
        transparency=0x02000000 | 10,
        color=256,
        layer="L",
        visible=False,
    )
    reasons = hidden_reasons(record, {"L": {"on": False, "frozen": True, "plot": False}})
    assert reasons == [
        "DIMTXT is 0",
        "96% transparent",
        "the text colour 3 is its own background fill",
        "the dimension is invisible",
        "layer 'L' is off",
        "layer 'L' is frozen",
        "layer 'L' does not plot",
    ]
    clean = types.SimpleNamespace(
        dimension=_fields(40.0),
        dim_vars=_style(),
        transparency=None,
        color=256,
        layer="L",
        visible=True,
    )
    assert hidden_reasons(clean, {"L": {"on": True, "frozen": False, "plot": True}}) == []


@pytest.mark.asyncio
async def test_a_dimension_inside_a_block_reference_binds_through_the_explosion(backend):
    block = backend._doc.blocks.new("POSTS")
    block.add_line((0, 0), (0, 20))
    block.add_line((40, 0), (40, 20))
    await backend.entity_create_block_ref("POSTS", 100.0, 0.0)
    await backend.dimension_linear(100, 0, 140, 0, 120, -10)
    assert await run_critique(backend, FOCUS) == []


# -- MTEXT codes, a DIMPOST suffix, a refused redraw (1.8-15 fix round) ------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "shown"),
    [
        ("<>\\PTYP", "40 TYP"),
        ("<>\\XTYP", "40 TYP"),
        ("{\\H0.7x;<>}", "40"),
        ("\\A1;<>", "40"),
        ("<>\\P(REF)", "40 (REF)"),
        ("<> TYP", "40 TYP"),
    ],
)
async def test_mtext_codes_in_a_value_override_are_formatting_not_a_stale_render(
    backend, override, shown
):
    # They were a dim_render_stale error no redraw could clear: the shown text
    # kept the codes, the *D block's text was read without them.
    _posts(backend, 0, 40)
    dim = await backend.dimension_linear(0, 0, 40, 0, 20, -10, text_override=override)
    rendered, text, _ = _shown(backend, dim.handle)
    assert text == shown
    assert display(_fields(40.0, override=override), _style()).value == 40.0
    assert await run_critique(backend, FOCUS) == [], (rendered, text)


def test_a_code_without_a_value_marker_is_not_the_number_the_override_claims():
    shown = display(_fields(40.0, override="{\\H0.7x;42}"), _style())
    assert (shown.text, shown.value) == ("42", 42.0)


def _dimpost_suffix(backend, suffix=" mm"):
    style = backend._doc.dimstyles.get("Standard")
    style.dxf.dimpost = suffix
    return style


@pytest.mark.asyncio
async def test_a_dimpost_suffix_without_the_value_marker_is_redrawn_as_autocad_shows_it(backend):
    # ezdxf refuses a DIMPOST without <>; AutoCAD reads it as a suffix. The
    # redraw used to raise after dimstyle_modify / entity_scale had written.
    _posts(backend, 0, 19.6)
    d1 = await backend.dimension_linear(0, 0, 19.6, 0, 10, -10)
    d2 = await backend.dimension_linear(0, 0, 19.6, 0, 10, -20)
    style = _dimpost_suffix(backend)
    result = await backend.dimstyle_modify("Standard", {"DIMDEC": 0})
    assert sorted(result["rerendered"]) == sorted([d1.handle, d2.handle])
    assert _shown(backend, d1.handle)[:2] == ("20 mm", "20 mm")
    scaled = await backend.entity_scale(d2.handle, 0, 0, 2)
    assert scaled["rerendered"] == [d2.handle]
    assert _shown(backend, d2.handle)[:2] == ("39 mm", "39 mm")
    assert style.dxf.dimpost == " mm", "the style keeps the suffix as AutoCAD stores it"
    entity = backend._doc.entitydb[d1.handle]
    assert not entity.has_xdata("ACAD"), "the render-only <> is never written as an override"
    issues = await run_critique(backend, FOCUS)
    # The scaled one measures 39.2 between no posts: unbound (info), never stale.
    assert [(i.handles[0], i.detail["code"]) for i in issues] == [(d2.handle, "dim_unbound")]


@pytest.fixture
def refused_render(monkeypatch):
    """The headless renderer refusing the DIMENSIONs whose handles are added."""
    from backends import ezdxf_backend as module

    refused: set[str] = set()
    real = module._render_override

    def _render(ent):
        if ent.dxf.handle in refused:
            raise ezdxf.DXFValueError("refused for the test")
        return real(ent)

    monkeypatch.setattr(module, "_render_override", _render)
    return refused


def _picture(entity):
    """The *D block's content: every DXF attribute but the handle and owner."""
    return [
        (
            e.dxftype(),
            sorted((k, str(v)) for k, v in e.dxfattribs(drop={"handle", "owner"}).items()),
        )
        for e in entity.get_geometry_block()
    ]


def _d_blocks(backend):
    return sorted(b.name for b in backend._doc.blocks if b.name.startswith("*D"))


@pytest.mark.asyncio
async def test_a_refused_redraw_leaves_the_style_and_every_dimension_as_they_were(
    backend, refused_render
):
    from engineering.errors import DimensionRenderError, classify

    _posts(backend, 0, 19.6)
    d1 = await backend.dimension_linear(0, 0, 19.6, 0, 10, -10)
    d2 = await backend.dimension_linear(0, 0, 19.6, 0, 10, -20)
    geometry = {h: backend._doc.entitydb[h].dxf.geometry for h in (d1.handle, d2.handle)}
    blocks = _d_blocks(backend)
    revision = backend.document_identity()["revision"]
    refused_render.add(d2.handle)  # d1 is redrawn first, then d2 is refused
    with pytest.raises(DimensionRenderError, match=f"DIMENSION {d2.handle}") as caught:
        await backend.dimstyle_modify("Standard", {"DIMDEC": 0})
    assert classify(caught.value)["kind"] == "refused"
    assert backend._doc.dimstyles.get("Standard").dxf.get("dimdec") in (None, 2)
    assert {h: backend._doc.entitydb[h].dxf.geometry for h in geometry} == geometry
    assert _d_blocks(backend) == blocks, "no new *D block left, no old one purged"
    assert _shown(backend, d1.handle)[0] == "19.6"
    assert backend.document_identity()["revision"] == revision


@pytest.mark.asyncio
async def test_a_refused_redraw_puts_a_scaled_or_edited_dimension_back(backend, refused_render):
    from engineering.errors import DimensionRenderError

    _posts(backend, 0, 40)
    dim = await backend.dimension_linear(0, 0, 40, 0, 20, -10)
    entity = backend._doc.entitydb[dim.handle]
    attribs = dict(entity.dxfattribs())
    picture = _picture(entity)
    revision = backend.document_identity()["revision"]
    refused_render.add(dim.handle)
    with pytest.raises(DimensionRenderError):
        await backend.entity_scale(dim.handle, 0, 0, 2)
    with pytest.raises(DimensionRenderError):
        await backend.entity_edit_geometry(dim.handle, x2=50.0, y2=0.0)
    assert dict(entity.dxfattribs()) == attribs
    assert _picture(entity) == picture, "the *D block's picture is not left scaled"
    assert backend.document_identity()["revision"] == revision
    assert _shown(backend, dim.handle)[:2] == ("40", "40")


@pytest.mark.asyncio
async def test_a_reflex_angle_the_headless_renderer_draws_is_not_offered_as_a_repair(backend):
    # ezdxf 1.4.4 draws 315° for the 45° angle dimension_angular asks for; a
    # redraw draws 315° again, so "drawing_refine redraws it" would be false.
    msp = backend._doc.modelspace()
    msp.add_line((0, 0), (50, 0))
    msp.add_line((0, 0), (50, 50))
    dim = await backend.dimension_angular(0, 0, 50, 0, 50, 50, 30, 10)
    (issue,) = [
        i for i in await run_critique(backend, FOCUS) if i.detail["code"] == "dim_render_stale"
    ]
    assert issue.handles == [dim.handle] and issue.severity == "error"
    assert issue.detail["repairable"] is False and issue.detail["reflex_render"] is True
    assert "redraw it in AutoCAD" in issue.message and "drawing_refine" not in issue.message
    planned = await refine_drawing(backend, focus=FOCUS, dry_run=True)
    (action,) = [
        a
        for a in planned.rounds[0].to_dict()["actions"]
        if a["detail"].get("code") == "dim_render_stale"
    ]
    assert action["status"] == "manual_required"


# -- the v9 matrix task ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_v9_dimension_lie_detect_task_passes_headlessly(tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import DEFAULT_MATRIX, MATRICES, task_by_id

    assert DEFAULT_MATRIX == "v8", "1.8-25 switches the default, not 1.8-15"
    assert task_by_id("dimension_lie_detect", "v9") in MATRICES["v9"]
    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        result = await adapter.run_task(task_by_id("dimension_lie_detect", "v9"))
    finally:
        await adapter.cleanup()
    assert result.status == "pass", result.metrics
    assert result.metrics["displayed_text"] == "20"


# -- the live engine: Update() and a read-back, never a claim ----------------


class _Dim:
    def __init__(self, handle, kind="AcDbRotatedDimension"):
        self.Handle = handle
        self.ObjectName = kind
        self.updates = 0

    def Update(self):
        self.updates += 1


class _Doc:
    def __init__(self, *entities):
        self.entities = {e.Handle: e for e in entities}

    def HandleToObject(self, handle):
        return self.entities[handle]


@pytest.fixture
def com(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    doc = _Doc(_Dim("2A"), _Dim("2B"), _Dim("3C", "AcDbLine"))
    monkeypatch.setattr(module, "_acad_doc", lambda: doc)
    monkeypatch.setattr(module, "_regen", lambda *_entity: None)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, doc


@pytest.mark.asyncio
async def test_com_rerender_updates_and_reports_only_what_the_read_back_confirms(com, monkeypatch):
    backend, doc = com
    reads = iter(
        [
            {"2A": ("19.6", False), "2B": ("20", True)},  # before Update()
            {"2A": ("20", True), "2B": ("20", True)},  # after: 2A changed, 2B already right
        ]
    )

    async def _display(handles=None):
        return next(reads)

    monkeypatch.setattr(backend, "_dimension_display", _display)
    result = await backend.dimension_rerender(["2A", "2B"])
    assert (doc.entities["2A"].updates, doc.entities["2B"].updates) == (1, 1)
    assert result["rerendered"] == ["2A"] and result["already_current"] == ["2B"]
    assert result["stale"] == [] and result["verified"] is True


@pytest.mark.asyncio
async def test_com_rerender_says_stale_when_autocad_did_not_redraw(com, monkeypatch):
    backend, _doc = com

    async def _display(handles=None):
        return {"2A": ("19.6", False)}

    monkeypatch.setattr(backend, "_dimension_display", _display)
    result = await backend.dimension_rerender(["2A"])
    assert result["rerendered"] == [] and result["stale"] == ["2A"]
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_com_entity_scale_updates_a_dimension_and_reads_it_back(com, monkeypatch):
    backend, doc = com
    dim = doc.entities["2A"]
    dim.ScaleEntity = lambda base, factor: None

    async def _display(handles=None):
        return {"2A": ("80", True)}

    monkeypatch.setattr(backend, "_dimension_display", _display)
    result = await backend.entity_scale("2A", 0, 0, 2)
    assert dim.updates == 1
    assert result["rerendered"] == ["2A"] and result["stale"] == []


@pytest.mark.asyncio
async def test_com_entity_edit_geometry_moves_a_dimensions_measured_points(com, monkeypatch):
    from backends import com_backend as module

    backend, doc = com
    monkeypatch.setattr(module, "_entity_info", lambda entity: entity)
    dim = doc.entities["2A"]
    dim.ExtLine1Point = (0.0, 0.0, 0.0)
    dim.ExtLine2Point = (40.0, 0.0, 0.0)
    edited = await backend.entity_edit_geometry("2A", x2=50.0)
    assert edited is dim and dim.updates == 1
    assert list(dim.ExtLine2Point.value)[:2] == [50.0, 0.0]  # a VARIANT point, as ActiveX takes
    assert dim.ExtLine1Point == (0.0, 0.0, 0.0), "an untouched point is not rewritten"
    doc.entities["4D"] = _Dim("4D", "AcDbDiametricDimension")
    with pytest.raises(RuntimeError, match="only a linear or aligned"):
        await backend.entity_edit_geometry("4D", x1=1.0)


@pytest.mark.asyncio
async def test_com_rerender_refuses_a_non_dimension_before_touching_any(com, monkeypatch):
    backend, doc = com

    async def _display(handles=None):
        return {}

    monkeypatch.setattr(backend, "_dimension_display", _display)
    with pytest.raises(ValueError, match="3C is a LINE, not a DIMENSION"):
        await backend.dimension_rerender(["2A", "3C"])
    assert doc.entities["2A"].updates == 0
