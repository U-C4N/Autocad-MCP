"""ISO 21920-1 / ISO 1302 surface texture and ISO 2553 weld symbols.

Pure geometry is asserted as emitted primitive tuples (counts, roles,
coordinates to 1e-9); the async layer is asserted against a real ezdxf
document through the `backend` fixture. No screenshots anywhere.
"""

from __future__ import annotations

import math

import pytest

from engineering.mech.annotate import (
    ISO1302_SYMBOL_PROPORTIONS,
    LAY_SYMBOLS,
    MACHINING_KINDS,
    SURFACE_HEIGHTS,
    SURFACE_STANDARDS,
    surface_texture_prims,
    symbol_proportions,
)
from engineering.mech.primitives import Circle, Line, Text

pytestmark = pytest.mark.asyncio

EPS = 1e-9
K = 1.0 / math.sqrt(3.0)  # tan(30 deg): the 60 deg legs' horizontal run per unit rise


def _flat(points):
    """Flatten a point sequence: pytest.approx refuses nested structures."""
    return tuple(float(value) for point in points for value in point)


async def test_the_seven_iso1302_proportion_rows_are_the_transcribed_ones():
    assert SURFACE_HEIGHTS == (2.5, 3.5, 5.0, 7.0, 10.0, 14.0, 20.0)
    assert ISO1302_SYMBOL_PROPORTIONS[2.5] == (0.25, 3.5, 7.5)
    assert ISO1302_SYMBOL_PROPORTIONS[3.5] == (0.35, 5.0, 10.5)
    assert ISO1302_SYMBOL_PROPORTIONS[5.0] == (0.5, 7.0, 15.0)
    assert ISO1302_SYMBOL_PROPORTIONS[7.0] == (0.7, 10.0, 21.0)
    assert ISO1302_SYMBOL_PROPORTIONS[10.0] == (1.0, 14.0, 30.0)
    assert ISO1302_SYMBOL_PROPORTIONS[14.0] == (1.4, 20.0, 42.0)
    assert ISO1302_SYMBOL_PROPORTIONS[20.0] == (2.0, 28.0, 60.0)
    # Every row: line width is h/10 and the minimum overall height is 3h.
    for height, (width, _h1, h2) in ISO1302_SYMBOL_PROPORTIONS.items():
        assert width == pytest.approx(height / 10.0, abs=EPS)
        assert h2 == pytest.approx(3.0 * height, abs=EPS)


async def test_a_text_height_outside_the_table_is_refused_by_name():
    with pytest.raises(ValueError) as excinfo:
        symbol_proportions(4.0)
    message = str(excinfo.value)
    assert "ISO 1302" in message and "4 mm" in message
    assert "2.5, 3.5, 5, 7, 10, 14, 20" in message


async def test_the_basic_symbol_is_a_60_degree_vee_on_the_dim_role():
    prims = surface_texture_prims(height=3.5)
    short, long_ = prims[0], prims[1]
    assert isinstance(short, Line) and isinstance(long_, Line)
    assert short.p1 == (0.0, 0.0) and long_.p1 == (0.0, 0.0)
    assert short.p2 == pytest.approx((-5.0 * K, 5.0), abs=EPS)
    assert long_.p2 == pytest.approx((10.5 * K, 10.5), abs=EPS)
    assert short.role == "dim" and long_.role == "dim"
    # The angle between the two legs is 60 degrees.
    a = math.atan2(short.p2[1], short.p2[0])
    b = math.atan2(long_.p2[1], long_.p2[0])
    assert math.degrees(a - b) == pytest.approx(60.0, abs=1e-9)
    # No bar, no circle: the basic symbol plus the extension line only.
    assert [type(p).__name__ for p in prims] == ["Line", "Line", "Line"]


async def test_machining_required_closes_the_vee_with_the_bar():
    prims = surface_texture_prims(height=3.5, machining="required")
    bar = prims[2]
    assert isinstance(bar, Line)
    assert bar.p1 == pytest.approx((-5.0 * K, 5.0), abs=EPS)
    assert bar.p2 == pytest.approx((5.0 * K, 5.0), abs=EPS)
    assert bar.role == "dim"


async def test_machining_prohibited_inscribes_the_circle_in_the_vee():
    prims = surface_texture_prims(height=3.5, machining="prohibited")
    circle = prims[2]
    assert isinstance(circle, Circle)
    assert circle.center == pytest.approx((0.0, 10.0 / 3.0), abs=EPS)
    assert circle.radius == pytest.approx(5.0 / 3.0, abs=EPS)
    # Tangent to the bar height H1 and to both legs.
    assert circle.center[1] + circle.radius == pytest.approx(5.0, abs=EPS)


async def test_ra_rz_process_lay_and_allowance_land_in_their_declared_positions():
    prims = surface_texture_prims(
        height=3.5, ra=3.2, rz=12.5, process="milled", lay="perpendicular", allowance=0.5
    )
    texts = [p for p in prims if isinstance(p, Text)]
    assert [t.text for t in texts] == ["Ra 3.2", "Rz 12.5", "milled", "⊥  0.5"]
    x_leg = 10.5 * K
    for t in texts:
        assert t.at[0] == pytest.approx(x_leg + 0.5 * 3.5, abs=EPS)
        assert t.height == 3.5 and t.role == "text"
    # Above the extension line the three lines stack upward at 1.5h pitch.
    assert texts[0].at[1] == pytest.approx(10.5 + 0.35 * 3.5, abs=EPS)
    assert texts[1].at[1] == pytest.approx(10.5 + 0.35 * 3.5 + 1.5 * 3.5, abs=EPS)
    assert texts[2].at[1] == pytest.approx(10.5 + 0.35 * 3.5 + 3.0 * 3.5, abs=EPS)
    # Lay and allowance sit below it, on one line.
    assert texts[3].at[1] == pytest.approx(10.5 - 0.35 * 3.5 - 3.5, abs=EPS)


async def test_the_extension_line_grows_with_the_widest_text():
    prims = surface_texture_prims(height=3.5, ra=3.2)
    extension = [p for p in prims if isinstance(p, Line)][-1]
    x_leg = 10.5 * K
    expected = 0.62 * 3.5 * len("Ra 3.2") + 3.5
    assert extension.p1 == pytest.approx((x_leg, 10.5), abs=EPS)
    assert extension.p2 == pytest.approx((x_leg + expected, 10.5), abs=EPS)
    # With no text at all it falls back to the declared minimum of 2h.
    bare = [p for p in surface_texture_prims(height=3.5) if isinstance(p, Line)][-1]
    assert bare.p2[0] - bare.p1[0] == pytest.approx(2.0 * 3.5, abs=EPS)


async def test_all_around_puts_the_circle_on_the_kink():
    prims = surface_texture_prims(height=3.5, all_around=True)
    circle = [p for p in prims if isinstance(p, Circle)][0]
    assert circle.center == pytest.approx((10.5 * K, 10.5), abs=EPS)
    assert circle.radius == pytest.approx(0.35 * 3.5, abs=EPS)


async def test_iso1302_is_accepted_as_an_alias_designation_and_draws_the_same_symbol():
    assert SURFACE_STANDARDS == ("ISO 21920-1", "ISO 1302")
    new = surface_texture_prims(height=3.5, ra=3.2, standard="ISO 21920-1")
    old = surface_texture_prims(height=3.5, ra=3.2, standard="ISO 1302")
    assert new == old
    with pytest.raises(ValueError) as excinfo:
        surface_texture_prims(height=3.5, standard="ASME B46.1")
    assert "ISO 21920-1, ISO 1302" in str(excinfo.value)


async def test_an_undefined_lay_and_an_undefined_machining_are_refused_with_the_list():
    assert set(LAY_SYMBOLS) == {
        "parallel",
        "perpendicular",
        "crossed",
        "multidirectional",
        "circular",
        "radial",
        "particulate",
    }
    assert MACHINING_KINDS == ("any", "required", "prohibited")
    with pytest.raises(ValueError) as excinfo:
        surface_texture_prims(lay="diagonal")
    assert "circular, crossed, multidirectional" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        surface_texture_prims(machining="maybe")
    assert "any, required, prohibited" in str(excinfo.value)


# ── ISO 2553 weld symbols ───────────────────────────────────────────────────

from engineering.mech.annotate import (  # noqa: E402 - grouped with its own tests
    WELD_KINDS,
    WELD_SIDES,
    weld_symbol_prims,
)
from engineering.mech.primitives import Arc, Poly  # noqa: E402


async def test_only_the_six_transcribed_elementary_symbols_are_offered():
    assert WELD_KINDS == ("square", "v", "bevel", "u", "j", "fillet")
    assert WELD_SIDES == ("arrow", "other", "both")
    with pytest.raises(ValueError) as excinfo:
        weld_symbol_prims(kind="spot")
    message = str(excinfo.value)
    assert "square, v, bevel, u, j, fillet" in message
    assert "ISO 2553" in message
    with pytest.raises(ValueError) as excinfo:
        weld_symbol_prims(kind="fillet", side="both_sides")
    assert "arrow, other, both" in str(excinfo.value)


async def test_the_reference_line_comes_first_and_the_identification_line_is_dashed():
    prims = weld_symbol_prims(kind="fillet", height=3.5)
    reference = prims[0]
    assert isinstance(reference, Line)
    assert reference.p1 == (0.0, 0.0)
    assert reference.p2[1] == 0.0 and reference.p2[0] > 0.0
    assert reference.role == "dim"
    length = reference.p2[0]
    dashes = [
        p for p in prims if isinstance(p, Line) and p.p1[1] == pytest.approx(-0.4 * 3.5, abs=EPS)
    ]
    assert len(dashes) >= 2
    assert all(d.p2[1] == pytest.approx(-0.4 * 3.5, abs=EPS) for d in dashes)
    assert dashes[0].p1[0] == pytest.approx(0.0, abs=EPS)
    assert dashes[0].p2[0] - dashes[0].p1[0] == pytest.approx(0.8 * 3.5, abs=EPS)
    assert dashes[1].p1[0] - dashes[0].p2[0] == pytest.approx(0.4 * 3.5, abs=EPS)
    assert dashes[-1].p2[0] <= length + EPS


async def test_a_symmetrical_weld_omits_the_identification_line():
    prims = weld_symbol_prims(kind="v", side="both", height=3.5)
    assert not [
        p for p in prims if isinstance(p, Line) and p.p1[1] == pytest.approx(-0.4 * 3.5, abs=EPS)
    ]
    # One V above the reference line, one V below the identification offset.
    vees = [p for p in prims if isinstance(p, Poly) and len(p.points) == 3 and not p.closed]
    assert len(vees) == 2
    assert vees[0].points[1][1] == pytest.approx(0.0, abs=EPS)
    assert vees[1].points[1][1] == pytest.approx(-0.4 * 3.5, abs=EPS)


async def test_the_fillet_triangle_stands_on_the_line_with_its_leg_on_the_left():
    prims = weld_symbol_prims(kind="fillet", height=3.5)
    triangle = [p for p in prims if isinstance(p, Poly) and p.closed][0]
    x0 = triangle.points[0][0]
    assert _flat(triangle.points) == pytest.approx((x0, 0.0, x0, 3.5, x0 + 3.5, 0.0), abs=EPS)
    assert triangle.role == "dim"


async def test_the_other_side_symbol_hangs_below_the_identification_line():
    prims = weld_symbol_prims(kind="fillet", side="other", height=3.5)
    triangle = [p for p in prims if isinstance(p, Poly) and p.closed][0]
    x0 = triangle.points[0][0]
    assert _flat(triangle.points) == pytest.approx(
        (x0, -1.4, x0, -1.4 - 3.5, x0 + 3.5, -1.4), abs=EPS
    )


async def test_the_u_symbol_is_a_half_circle_between_two_uprights():
    prims = weld_symbol_prims(kind="u", height=3.5)
    arc = [p for p in prims if isinstance(p, Arc)][0]
    assert arc.radius == pytest.approx(1.75, abs=EPS)
    assert (arc.start_deg, arc.end_deg) == (180.0, 360.0)
    assert arc.center[1] == pytest.approx(1.75, abs=EPS)
    mirrored = [
        p for p in weld_symbol_prims(kind="u", side="other", height=3.5) if isinstance(p, Arc)
    ][0]
    assert (mirrored.start_deg, mirrored.end_deg) == (0.0, 180.0)
    assert mirrored.center[1] == pytest.approx(-1.4 - 1.75, abs=EPS)


async def test_size_is_written_before_the_symbol_and_length_and_pitch_after_it():
    prims = weld_symbol_prims(kind="fillet", size=5, length=50, pitch=100, height=3.5)
    texts = [p for p in prims if isinstance(p, Text)]
    assert [t.text for t in texts] == ["a5", "50 (100)"]
    triangle = [p for p in prims if isinstance(p, Poly) and p.closed][0]
    x_symbol = triangle.points[0][0]
    assert texts[0].at[0] < x_symbol
    assert texts[1].at[0] > x_symbol + 3.5
    # A string size is written verbatim, so z-notation stays available.
    verbatim = weld_symbol_prims(kind="fillet", size="z7", height=3.5)
    assert [p.text for p in verbatim if isinstance(p, Text)] == ["z7"]
    with pytest.raises(ValueError) as excinfo:
        weld_symbol_prims(kind="fillet", pitch=100, height=3.5)
    assert "pitch needs length" in str(excinfo.value)


async def test_field_weld_flag_and_all_around_circle_sit_on_the_kink():
    prims = weld_symbol_prims(kind="fillet", field_weld=True, all_around=True, height=3.5)
    pole = [p for p in prims if isinstance(p, Line) and p.p1 == (0.0, 0.0) and p.p2[0] == 0.0][0]
    assert pole.p2 == pytest.approx((0.0, 1.4 * 3.5), abs=EPS)
    flag = [p for p in prims if isinstance(p, Poly) and p.closed and p.points[0][0] == 0.0][0]
    assert _flat(flag.points) == pytest.approx(
        (
            0.0,
            1.4 * 3.5,
            0.7 * 3.5,
            1.4 * 3.5 - 0.25 * 3.5,
            0.0,
            1.4 * 3.5 - 0.5 * 3.5,
        ),
        abs=EPS,
    )
    circle = [p for p in prims if isinstance(p, Circle)][0]
    assert circle.center == (0.0, 0.0)
    assert circle.radius == pytest.approx(0.4 * 3.5, abs=EPS)


async def test_a_process_reference_gets_the_tail_fork_at_the_far_end():
    prims = weld_symbol_prims(kind="fillet", process="ISO 4063 - 135", height=3.5)
    reference = prims[0]
    length = reference.p2[0]
    fork = [
        p for p in prims if isinstance(p, Line) and p.p1 == pytest.approx((length, 0.0), abs=EPS)
    ]
    assert len(fork) == 2
    assert fork[0].p2 == pytest.approx((length + 0.8 * 3.5, 0.5 * 3.5), abs=EPS)
    assert fork[1].p2 == pytest.approx((length + 0.8 * 3.5, -0.5 * 3.5), abs=EPS)
    tail_text = [p for p in prims if isinstance(p, Text)][-1]
    assert tail_text.text == "ISO 4063 - 135"
    assert tail_text.at[0] == pytest.approx(length + 3.5, abs=EPS)


# ── the async layer: primitives -> a real ezdxf document ────────────────────

from engineering.mech.annotate import (  # noqa: E402 - grouped with its own tests
    draw_annotation_prims,
    draw_surface_texture,
    draw_weld_symbol,
    leader_prims,
)
from engineering.mech.primitives import ROLE_LAYER  # noqa: E402


async def _types_and_layers(backend, handles):
    rows = []
    for handle in handles:
        info = await backend.entity_get(handle)
        rows.append((info.type, info.layer))
    return rows


async def test_draw_annotation_prims_puts_every_role_on_its_own_layer(backend):
    prims = (
        Line((0.0, 0.0), (10.0, 0.0), "dim"),
        Circle((5.0, 5.0), 2.0, "dim"),
        Text((0.0, 12.0), "Ra 3.2", 3.5),
    )
    result = await draw_annotation_prims(backend, prims, at=(100.0, 50.0))
    assert result["ok"] is True and result["count"] == 3
    assert await _types_and_layers(backend, result["handles"]) == [
        ("LINE", ROLE_LAYER["dim"]),
        ("CIRCLE", ROLE_LAYER["dim"]),
        ("TEXT", ROLE_LAYER["text"]),
    ]
    line = await backend.entity_get(result["handles"][0])
    assert line.properties["start"] == pytest.approx([100.0, 50.0], abs=EPS)
    assert line.properties["end"] == pytest.approx([110.0, 50.0], abs=EPS)
    circle = await backend.entity_get(result["handles"][1])
    assert circle.properties["center"] == pytest.approx([105.0, 55.0], abs=EPS)


async def test_an_explicit_layer_overrides_every_role(backend):
    result = await draw_annotation_prims(
        backend,
        (Line((0.0, 0.0), (1.0, 0.0), "dim"), Text((0.0, 2.0), "x", 3.5)),
        layer="ANNOT",
    )
    assert {layer for _type, layer in await _types_and_layers(backend, result["handles"])} == {
        "ANNOT"
    }


async def test_leader_prims_reuse_the_repository_arrowhead_proportions():
    line, head = leader_prims((0.0, 0.0), (10.0, 0.0), arrow_size=2.5)
    assert isinstance(line, Line) and isinstance(head, Poly) and head.closed
    assert _flat(head.points) == pytest.approx(
        (0.0, 0.0, 2.5, 2.5 * 0.35, 2.5, -2.5 * 0.35), abs=EPS
    )
    with pytest.raises(ValueError) as excinfo:
        leader_prims((1.0, 1.0), (1.0, 1.0))
    assert "distinct" in str(excinfo.value)


async def test_draw_surface_texture_draws_the_symbol_and_its_leader(backend):
    result = await draw_surface_texture(
        backend, at=(40.0, 60.0), leader_to=(20.0, 50.0), ra=3.2, machining="required"
    )
    assert result["ok"] is True and result["standard"] == "ISO 21920-1"
    rows = await _types_and_layers(backend, result["handles"])
    # 3 vee/bar lines + extension line + the Ra text + leader line + arrowhead.
    assert [t for t, _ in rows] == [
        "LINE",
        "LINE",
        "LINE",
        "LINE",
        "TEXT",
        "LINE",
        "LWPOLYLINE",
    ]
    apex = await backend.entity_get(result["handles"][0])
    assert apex.properties["start"] == pytest.approx([40.0, 60.0], abs=EPS)
    leader = await backend.entity_get(result["handles"][5])
    assert leader.properties["start"] == pytest.approx([20.0, 50.0], abs=EPS)
    assert leader.properties["end"] == pytest.approx([40.0, 60.0], abs=EPS)


async def test_draw_weld_symbol_refuses_an_unknown_kind_before_touching_the_drawing(backend):
    before = (await backend.analysis_stats())["total_entities"]
    with pytest.raises(ValueError):
        await draw_weld_symbol(backend, at=(0.0, 0.0), kind="seam")
    assert (await backend.analysis_stats())["total_entities"] == before


async def test_draw_weld_symbol_places_the_kink_at_the_given_point(backend):
    result = await draw_weld_symbol(
        backend, at=(10.0, 20.0), kind="fillet", size=5, length=50, leader_to=(0.0, 10.0)
    )
    assert result["ok"] is True
    reference = await backend.entity_get(result["handles"][0])
    assert reference.properties["start"] == pytest.approx([10.0, 20.0], abs=EPS)
    assert reference.properties["end"][1] == pytest.approx(20.0, abs=EPS)
    texts = []
    for handle in result["handles"]:
        info = await backend.entity_get(handle)
        if info.type == "TEXT":
            texts.append(info.properties["text"])
    assert texts == ["a5", "50"]
