"""ISO 5457 sheet frames: sizes, margins, the zone grid, centring and trim marks.

The frame is the one thing every drawing this server makes carries, so a wrong
margin or a wrong division count is visible on all of them. Every number
asserted here is transcribed in `engineering/sheet/frames.py` with its clause.
"""

from __future__ import annotations

import pytest

from engineering.sheet.frames import (
    CENTRING_OVERSHOOT,
    EDGE_MARGIN,
    FILING_MARGIN,
    SHEETS,
    TRIM_MARK_LONG,
    TRIM_MARK_SHORT,
    ZONE_DIVISIONS,
    ZONE_MODULE,
    frame_metrics,
    sheet_size,
    zone_divisions,
)

EPS = 1e-9


def test_sheet_sizes_are_the_iso_216_a_series():
    assert SHEETS == {
        "A4": (210.0, 297.0),
        "A3": (297.0, 420.0),
        "A2": (420.0, 594.0),
        "A1": (594.0, 841.0),
        "A0": (841.0, 1189.0),
    }


def test_sheet_sizes_agree_with_the_page_setup_table():
    """Two tables of one standard in one repository is one too many unless
    they are pinned to each other."""
    from engineering.standards.papers import PAPER_SIZES

    for name, (w, h) in SHEETS.items():
        assert PAPER_SIZES[f"ISO_{name}"] == (int(w), int(h))


def test_landscape_swaps_the_portrait_pair():
    assert sheet_size("A3", "landscape") == (420.0, 297.0)
    assert sheet_size("A3", "portrait") == (297.0, 420.0)
    assert sheet_size("a3") == (420.0, 297.0)


def test_an_unknown_sheet_is_refused_by_name_with_the_coverage():
    with pytest.raises(ValueError) as excinfo:
        sheet_size("A5")
    message = str(excinfo.value)
    assert "A5" in message
    for name in SHEETS:
        assert name in message


def test_an_unknown_orientation_is_refused():
    with pytest.raises(ValueError) as excinfo:
        sheet_size("A3", "diagonal")
    assert "diagonal" in str(excinfo.value)


def test_margins_are_the_iso_5457_filing_20_and_edge_10():
    assert (FILING_MARGIN, EDGE_MARGIN) == (20.0, 10.0)
    m = frame_metrics("A3")
    assert m["margins"] == {"left": 20.0, "right": 10.0, "top": 10.0, "bottom": 10.0}
    assert m["frame"] == [20.0, 10.0, 410.0, 287.0]
    assert m["drawing_area"] == [390.0, 277.0]
    assert m["centre"] == [210.0, 148.5]


def test_a4_portrait_drawing_area_is_exactly_the_titleblock_width():
    """ISO 7200's 180 mm title block has to fit the smallest sheet."""
    m = frame_metrics("A4", orientation="portrait")
    assert m["drawing_area"][0] == pytest.approx(180.0, abs=EPS)


@pytest.mark.parametrize(
    ("size", "orientation", "columns", "rows"),
    [
        ("A4", "portrait", 4, 6),
        ("A4", "landscape", 6, 4),
        ("A3", "landscape", 8, 6),
        ("A3", "portrait", 6, 8),
        ("A2", "landscape", 12, 8),
        ("A2", "portrait", 8, 12),
        ("A1", "landscape", 16, 12),
        ("A0", "landscape", 24, 16),
        ("A0", "portrait", 16, 24),
    ],
)
def test_zone_division_counts_match_the_authored_table(size, orientation, columns, rows):
    z = frame_metrics(size, orientation=orientation)["zones"]
    assert (z["columns"], z["rows"]) == (columns, rows)
    long_side, short_side = ZONE_DIVISIONS[size]
    if orientation == "landscape":
        assert (z["columns"], z["rows"]) == (long_side, short_side)
    else:
        assert (z["columns"], z["rows"]) == (short_side, long_side)


def test_zone_divisions_follow_the_even_rule_over_50_mm():
    assert ZONE_MODULE == 50.0
    assert zone_divisions(390.0) == 8  # 7.80 -> 8
    assert zone_divisions(277.0) == 6  # 5.54 -> 6
    assert zone_divisions(564.0) == 12  # 11.28 -> 12
    assert zone_divisions(1159.0) == 24  # 23.18 -> 24
    assert zone_divisions(821.0) == 16  # 16.42 -> 16


def test_zone_labels_cover_every_cell_and_letters_start_at_the_top():
    z = frame_metrics("A0")["zones"]
    assert z["numbers"][0] == "1" and z["numbers"][-1] == "24"
    assert len(z["numbers"]) == 24
    assert z["letters"][0] == "A" and z["letters"][-1] == "P"
    assert len(z["letters"]) == 16
    assert z["column_width"] == pytest.approx(1159.0 / 24.0, abs=EPS)
    assert z["row_height"] == pytest.approx(821.0 / 16.0, abs=EPS)


def test_the_mark_constants_are_the_standard_s():
    assert CENTRING_OVERSHOOT == 5.0
    assert (TRIM_MARK_LONG, TRIM_MARK_SHORT) == (10.0, 5.0)


# ── primitives ──────────────────────────────────────────────────────────────

from engineering.mech.primitives import Line, Poly, Text  # noqa: E402
from engineering.sheet.frames import frame_prims  # noqa: E402


def _polys(prims):
    return [p for p in prims if isinstance(p, Poly)]


def _lines(prims):
    return [p for p in prims if isinstance(p, Line)]


def _texts(prims):
    return [p for p in prims if isinstance(p, Text)]


def test_the_first_two_primitives_are_the_trimmed_sheet_and_the_frame():
    prims = frame_prims("A3", zones=False, marks=False)
    assert len(prims) == 2
    sheet, frame = prims
    assert isinstance(sheet, Poly) and sheet.closed and sheet.role == "visible"
    assert sheet.points == ((0.0, 0.0), (420.0, 0.0), (420.0, 297.0), (0.0, 297.0))
    assert frame.points == ((20.0, 10.0), (410.0, 10.0), (410.0, 287.0), (20.0, 287.0))


def test_the_zone_grid_has_a_tick_pair_per_interior_boundary_and_a_label_pair_per_cell():
    prims = frame_prims("A3", zones=True, marks=False)
    # 7 interior column boundaries x 2 edges + 5 interior row boundaries x 2 edges
    assert len(_lines(prims)) == (8 - 1) * 2 + (6 - 1) * 2
    # 8 columns x 2 edges + 6 rows x 2 edges
    assert len(_texts(prims)) == 8 * 2 + 6 * 2
    assert {t.text for t in _texts(prims)} == {
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
    }


def test_zone_ticks_span_the_margin_band_only():
    prims = frame_prims("A3", zones=True, marks=False)
    for line in [ln for ln in _lines(prims) if ln.p1[0] == ln.p2[0]]:
        ys = sorted((line.p1[1], line.p2[1]))
        assert ys in ([0.0, 10.0], [287.0, 297.0]), f"tick {line} leaves the margin band"
    for line in [ln for ln in _lines(prims) if ln.p1[1] == ln.p2[1]]:
        xs = sorted((line.p1[0], line.p2[0]))
        assert xs in ([0.0, 20.0], [410.0, 420.0]), f"tick {line} leaves the margin band"


def test_the_first_zone_letter_is_at_the_top_of_the_sheet():
    prims = frame_prims("A3", zones=True, marks=False)
    a_labels = [t for t in _texts(prims) if t.text == "A"]
    f_labels = [t for t in _texts(prims) if t.text == "F"]
    assert len(a_labels) == 2 and len(f_labels) == 2
    assert min(t.at[1] for t in a_labels) > max(t.at[1] for t in f_labels)


def test_centring_marks_run_from_the_trimmed_edge_to_5_mm_inside_the_frame():
    prims = frame_prims("A3", zones=False, marks=True)
    centre_lines = [p for p in _lines(prims) if p.role == "center"]
    assert len(centre_lines) == 4
    spans = {(tuple(c.p1), tuple(c.p2)) for c in centre_lines}
    assert ((0.0, 148.5), (25.0, 148.5)) in spans
    assert ((420.0, 148.5), (405.0, 148.5)) in spans
    assert ((210.0, 0.0), (210.0, 15.0)) in spans
    assert ((210.0, 297.0), (210.0, 282.0)) in spans


def test_each_corner_carries_two_overlapping_10_by_5_trim_rectangles():
    prims = frame_prims("A3", zones=False, marks=True)
    rects = _polys(prims)[2:]  # after the trimmed sheet and the frame
    assert len(rects) == 8
    for rect in rects:
        xs = [p[0] for p in rect.points]
        ys = [p[1] for p in rect.points]
        span = (abs(max(xs) - min(xs)), abs(max(ys) - min(ys)))
        assert span in ((10.0, 5.0), (5.0, 10.0)), span
        assert rect.closed


# ── drawing ─────────────────────────────────────────────────────────────────

from engineering.sheet.frames import draw_sheet_frame  # noqa: E402


@pytest.mark.asyncio
async def test_draw_sheet_frame_puts_the_border_on_titleblock_and_the_zones_on_text(backend):
    result = await draw_sheet_frame(backend, "A3", zones=True, marks=True)
    assert result["ok"] is True
    assert result["size"] == "A3"
    assert result["bbox"] == {"min": [0.0, 0.0], "max": [420.0, 297.0]}
    outer = await backend.entity_get(result["outer_border"])
    inner = await backend.entity_get(result["inner_border"])
    assert outer.layer == "TITLEBLOCK" and inner.layer == "TITLEBLOCK"
    texts = [
        e for e in await backend.entity_list(type_filter="TEXT") if e.properties["text"] == "A"
    ]
    assert texts and all(e.layer == "TEXT" for e in texts)


@pytest.mark.asyncio
async def test_draw_sheet_frame_offsets_every_primitive_by_the_origin(backend):
    result = await draw_sheet_frame(
        backend, "A4", orientation="portrait", zones=False, marks=False, origin=(100.0, 50.0)
    )
    assert result["bbox"] == {"min": [100.0, 50.0], "max": [310.0, 347.0]}
    assert len(result["handles"]) == 2


@pytest.mark.asyncio
async def test_a_bad_size_is_refused_before_anything_is_drawn(backend):
    before = len(await backend.entity_list())
    with pytest.raises(ValueError):
        await draw_sheet_frame(backend, "A5")
    assert len(await backend.entity_list()) == before


@pytest.mark.asyncio
async def test_a_missing_layout_is_refused_before_anything_is_drawn(backend):
    before = len(await backend.entity_list())
    with pytest.raises(RuntimeError) as excinfo:
        await draw_sheet_frame(backend, "A3", layout="NoSuchSheet")
    assert "NoSuchSheet" in str(excinfo.value)
    assert "layout_create" in str(excinfo.value)
    assert len(await backend.entity_list()) == before


@pytest.mark.asyncio
async def test_the_frame_puts_the_caller_back_on_the_tab_it_found_them_on(backend):
    await backend.layout_create("SheetA")
    await backend.layout_create("SheetB")
    await backend.layout_set_current("SheetB")
    await draw_sheet_frame(backend, "A3", zones=False, marks=False, layout="SheetA")
    assert (await backend.layout_list())["current"] == "SheetB"


@pytest.mark.asyncio
async def test_the_caller_s_tab_is_read_from_the_listing_not_a_private_attribute(
    backend, backend_without_private_state
):
    """`_current_space` exists on EzdxfBackend only. ComBackend keeps the live
    tab in AutoCAD, so reading the private attribute there fell through to the
    ``getattr`` default and dropped a live caller onto Model after every
    border -- measured on AutoCAD 2026. `layout_list()["current"]` is the value
    both engines really report, and `enter_layout` already had it in hand.
    """
    await backend.layout_create("SheetA")
    await backend.layout_create("SheetB")
    await backend.layout_set_current("SheetB")
    with pytest.raises(AttributeError):
        _ = backend_without_private_state._current_space

    result = await draw_sheet_frame(
        backend_without_private_state, "A3", zones=False, marks=False, layout="SheetA"
    )

    assert result["layout"] == "SheetA"
    assert (await backend.layout_list())["current"] == "SheetB"
