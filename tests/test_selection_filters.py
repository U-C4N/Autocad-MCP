"""F1 — selection filters: window, polygon and property selection.

Three tools that answer "which entities do you mean?" without the caller having
to memorise handles. Each carries a failure mode that is silent unless it is
pinned here:

* **window vs crossing** is the whole point of the family. AutoCAD's *window*
  returns only entities that are *fully contained*; *crossing* also returns the
  ones that merely straddle the edge. Under the hood these are
  ``select.bbox_inside`` and ``select.bbox_overlap`` — one character apart in a
  call, and getting them backwards produces a plausible-looking handle list that
  is quietly the wrong set. Every mode test below builds one entity inside, one
  straddling and one outside, and pins exactly which of the three comes back.
* **a zero-area box** (``x1 == x2`` or ``y1 == y2``) can never contain anything.
  ``select.Window((0, 0), (0, 100))`` returns ``[]`` — measured — so a tool that
  passed it through would answer "nothing is there" to a question that was
  really "you asked for nothing". It is refused instead.
* **a polygon is not its bounding box.** A triangle and a square with identical
  bounding boxes must not select the same entities; the triangle test uses
  exactly that pair so a lazy bbox implementation cannot pass.
* **mirrored geometry** is where stored coordinates and real position diverge. A
  mirrored CIRCLE keeps ``center = (30, 20)`` on disk and carries
  ``extrusion = (0, 0, -1)``; it is *drawn* at (-30, 20), which is what
  ``entity_get`` reports. Selection must agree with the reported position, or
  the caller would have to window an entity where nothing is visible.
* **``selection_filter`` takes named parameters, not a query string,** on
  purpose. ``layout.query("LINE[nosuchattr=='x']")`` returns ``[]`` silently —
  measured — so a typo is indistinguishable from "no matches". Named parameters
  make the typo a ``TypeError`` at the tool boundary. The response carries
  ``filtered_by`` for the same reason: a caller must be able to tell "I filtered
  by layer and found none" from "I never filtered by layer at all".
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

# The selection box every window/polygon test uses.
BOX = (0.0, 0.0, 100.0, 100.0)
SQUARE_POLY = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
# Same bounding box as SQUARE_POLY, half the area.
TRIANGLE_POLY = [[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]]


async def _three_positions(backend):
    """One entity fully inside BOX, one straddling its edge, one fully outside.

    Plus a second contained entity on another layer, so a layer filter has
    something to remove without emptying the result.
    """
    contained = await backend.entity_create_circle(50, 50, 10, layer="GEOMETRY")
    contained_other_layer = await backend.entity_create_circle(20, 80, 5, layer="HIDDEN")
    straddling = await backend.entity_create_line(90, 50, 110, 50, layer="GEOMETRY")
    outside = await backend.entity_create_circle(200, 200, 5, layer="HIDDEN")
    return {
        "contained": contained.handle,
        "contained_other_layer": contained_other_layer.handle,
        "straddling": straddling.handle,
        "outside": outside.handle,
    }


async def _mixed_properties(backend):
    """Three entities that differ in type, layer, colour, linetype and area."""
    circle = await backend.entity_create_circle(50, 50, 10, layer="GEOMETRY", color=1)
    square = await backend.entity_create_polyline(
        [[0, 0], [20, 0], [20, 20], [0, 20]], closed=True, layer="GEOMETRY", color=3
    )
    line = await backend.entity_create_line(0, 0, 50, 0, layer="HIDDEN", color=0, linetype="HIDDEN")
    return {"circle": circle.handle, "square": square.handle, "line": line.handle}


# ── selection_window: the mode contract ─────────────────────────────────────


async def test_a_window_returns_only_fully_contained_entities(backend):
    """The straddling line is the one that must NOT come back."""
    handles = await _three_positions(backend)

    result = await backend.selection_window(*BOX, mode="window")

    assert result["ok"] is True
    assert set(result["handles"]) == {
        handles["contained"],
        handles["contained_other_layer"],
    }


async def test_a_crossing_also_returns_entities_that_straddle_the_edge(backend):
    """Crossing = window + the straddlers. Never fewer, never the outside one."""
    handles = await _three_positions(backend)

    result = await backend.selection_window(*BOX, mode="crossing")

    assert set(result["handles"]) == {
        handles["contained"],
        handles["contained_other_layer"],
        handles["straddling"],
    }


async def test_neither_mode_reaches_an_entity_outside_the_box(backend):
    handles = await _three_positions(backend)

    window = await backend.selection_window(*BOX, mode="window")
    crossing = await backend.selection_window(*BOX, mode="crossing")

    assert handles["outside"] not in window["handles"]
    assert handles["outside"] not in crossing["handles"]


async def test_crossing_is_a_strict_superset_of_window(backend):
    """If these two ever come back equal for this drawing, one of them is wrong."""
    await _three_positions(backend)

    window = set((await backend.selection_window(*BOX, mode="window"))["handles"])
    crossing = set((await backend.selection_window(*BOX, mode="crossing"))["handles"])

    assert window < crossing
    assert len(crossing) - len(window) == 1


async def test_window_is_the_default_mode(backend):
    """The safer of the two: contained-only never over-selects."""
    handles = await _three_positions(backend)

    result = await backend.selection_window(*BOX)

    assert result["mode"] == "window"
    assert handles["straddling"] not in result["handles"]


async def test_the_reported_count_matches_the_handles(backend):
    await _three_positions(backend)

    result = await backend.selection_window(*BOX, mode="crossing")

    assert result["count"] == len(result["handles"]) == 3
    assert result["mode"] == "crossing"


async def test_the_corners_may_be_given_in_any_order(backend):
    """(x2, y2) top-right is a convention, not a requirement of the geometry."""
    await _three_positions(backend)

    normal = await backend.selection_window(0, 0, 100, 100, mode="crossing")
    flipped = await backend.selection_window(100, 100, 0, 0, mode="crossing")

    assert set(flipped["handles"]) == set(normal["handles"])
    assert flipped["count"] == normal["count"] == 3


# ── selection_window: refusals ──────────────────────────────────────────────


@pytest.mark.parametrize("bad_mode", ["inside", "fence", "WINDOWS", "", "all"])
async def test_an_unknown_window_mode_is_refused(backend, bad_mode):
    """Falling back to a default mode would answer a question nobody asked."""
    await _three_positions(backend)

    result = await backend.selection_window(*BOX, mode=bad_mode)

    assert result["ok"] is False
    assert "mode" in result["error"]


@pytest.mark.parametrize(
    "box",
    [
        (10.0, 0.0, 10.0, 100.0),  # zero width
        (0.0, 40.0, 100.0, 40.0),  # zero height
        (5.0, 5.0, 5.0, 5.0),  # a point
    ],
)
async def test_a_zero_area_window_is_refused(backend, box):
    """It can never contain anything, so [] would read as "nothing is there"."""
    await _three_positions(backend)

    result = await backend.selection_window(*box, mode="crossing")

    assert result["ok"] is False
    assert "area" in result["error"] or "zero" in result["error"]


# ── selection_window: filters compose with the mode ─────────────────────────


async def test_an_entity_type_filter_narrows_the_crossing_set(backend):
    """The type filter must cut the crossing set, not replace it."""
    handles = await _three_positions(backend)

    result = await backend.selection_window(*BOX, mode="crossing", entity_type="CIRCLE")

    assert set(result["handles"]) == {
        handles["contained"],
        handles["contained_other_layer"],
    }
    assert handles["straddling"] not in result["handles"]


async def test_an_entity_type_filter_can_keep_the_straddler_alone(backend):
    """Same box, same mode, LINE instead of CIRCLE: the complement comes back."""
    handles = await _three_positions(backend)

    result = await backend.selection_window(*BOX, mode="crossing", entity_type="LINE")

    assert set(result["handles"]) == {handles["straddling"]}


async def test_a_layer_filter_composes_with_crossing(backend):
    handles = await _three_positions(backend)

    result = await backend.selection_window(*BOX, mode="crossing", layer="GEOMETRY")

    assert set(result["handles"]) == {handles["contained"], handles["straddling"]}


async def test_a_layer_filter_composes_with_window(backend):
    """Same layer, window mode: the straddler drops out and only one is left."""
    handles = await _three_positions(backend)

    result = await backend.selection_window(*BOX, mode="window", layer="GEOMETRY")

    assert set(result["handles"]) == {handles["contained"]}


async def test_type_and_layer_filters_compose_with_each_other(backend):
    handles = await _three_positions(backend)

    result = await backend.selection_window(
        *BOX, mode="crossing", entity_type="CIRCLE", layer="HIDDEN"
    )

    assert set(result["handles"]) == {handles["contained_other_layer"]}


async def test_an_empty_selection_is_ok_with_zero_handles(backend):
    """Nothing there is an answer, not an error — unlike a zero-area box."""
    await _three_positions(backend)

    result = await backend.selection_window(500, 500, 600, 600, mode="crossing")

    assert result["ok"] is True
    assert result["handles"] == []
    assert result["count"] == 0


# ── selection_window: mirrored geometry ─────────────────────────────────────


async def _mirrored_circle(backend):
    """A CIRCLE stored at (30, 20) but drawn at (-30, 20).

    Mirroring about the Y axis gives extrusion (0, 0, -1) and leaves the stored
    centre untouched — measured. entity_get resolves it to WCS (-30, 20).
    """
    source = await backend.entity_create_circle(30, 20, 5, layer="GEOMETRY")
    return (await backend.entity_mirror(source.handle, 0, 0, 0, 10, delete_original=True)).handle


async def test_a_mirrored_entity_is_selected_where_entity_get_reports_it(backend):
    handle = await _mirrored_circle(backend)
    reported = await backend.entity_get(handle)
    assert reported.properties["center"] == [-30.0, 20.0]

    result = await backend.selection_window(-40, 10, -20, 30, mode="window")

    assert set(result["handles"]) == {handle}


async def test_a_mirrored_entity_is_not_selected_at_its_stored_coordinates(backend):
    """Selecting at (30, 20) would select an entity nobody can see there."""
    handle = await _mirrored_circle(backend)

    result = await backend.selection_window(20, 10, 40, 30, mode="crossing")

    assert handle not in result["handles"]
    assert result["count"] == 0


async def test_the_mirrored_coordinates_really_do_diverge_on_disk(backend, tmp_path):
    """Proof the divergence is stored, not an artefact of the in-memory copy."""
    import ezdxf

    handle = await _mirrored_circle(backend)
    path = tmp_path / "mirrored.dxf"
    await backend.drawing_save_as(str(path))

    reloaded = ezdxf.readfile(str(path)).entitydb.get(handle)
    assert tuple(reloaded.dxf.extrusion) == (0.0, 0.0, -1.0)
    assert tuple(reloaded.dxf.center)[:2] == (30.0, 20.0)

    selected = await backend.selection_window(-40, 10, -20, 30, mode="window")
    assert selected["handles"] == [handle]


# ── selection_polygon ───────────────────────────────────────────────────────


async def test_a_polygon_window_returns_only_fully_contained_entities(backend):
    handles = await _three_positions(backend)

    result = await backend.selection_polygon(SQUARE_POLY, mode="window")

    assert result["ok"] is True
    assert set(result["handles"]) == {
        handles["contained"],
        handles["contained_other_layer"],
    }


async def test_a_polygon_crossing_also_returns_the_straddler(backend):
    handles = await _three_positions(backend)

    result = await backend.selection_polygon(SQUARE_POLY, mode="crossing")

    assert set(result["handles"]) == {
        handles["contained"],
        handles["contained_other_layer"],
        handles["straddling"],
    }
    assert result["mode"] == "crossing"


async def test_a_polygon_is_not_its_bounding_box(backend):
    """The triangle has exactly SQUARE_POLY's bounding box and half its area.

    Measured: both contained circles sit inside the square but poke out of the
    triangle's hypotenuse, so a bbox-only implementation returns two here and
    fails.
    """
    await _three_positions(backend)

    square = await backend.selection_polygon(SQUARE_POLY, mode="window")
    triangle = await backend.selection_polygon(TRIANGLE_POLY, mode="window")

    assert square["count"] == 2
    assert triangle["handles"] == []


async def test_the_triangle_still_reaches_them_in_crossing_mode(backend):
    """They poke out of it — which is exactly what crossing is for."""
    handles = await _three_positions(backend)

    result = await backend.selection_polygon(TRIANGLE_POLY, mode="crossing")

    assert set(result["handles"]) == {
        handles["contained"],
        handles["contained_other_layer"],
    }


@pytest.mark.parametrize(
    "bad",
    [[], [[0.0, 0.0]], [[0.0, 0.0], [100.0, 100.0]]],
)
async def test_a_polygon_needs_at_least_three_points(backend, bad):
    """Two points enclose no area; a zero-area lasso catches nothing."""
    await _three_positions(backend)

    result = await backend.selection_polygon(bad, mode="crossing")

    assert result["ok"] is False
    assert "3" in result["error"] or "three" in result["error"].lower()


@pytest.mark.parametrize("bad_mode", ["inside", "fence", ""])
async def test_an_unknown_polygon_mode_is_refused(backend, bad_mode):
    await _three_positions(backend)

    result = await backend.selection_polygon(SQUARE_POLY, mode=bad_mode)

    assert result["ok"] is False
    assert "mode" in result["error"]


async def test_a_polygon_filter_composes_with_the_mode(backend):
    handles = await _three_positions(backend)

    result = await backend.selection_polygon(SQUARE_POLY, mode="crossing", entity_type="LINE")

    assert set(result["handles"]) == {handles["straddling"]}


async def test_a_polygon_layer_filter_composes_with_the_mode(backend):
    handles = await _three_positions(backend)

    result = await backend.selection_polygon(SQUARE_POLY, mode="window", layer="HIDDEN")

    assert set(result["handles"]) == {handles["contained_other_layer"]}


# ── selection_filter: what was actually filtered ────────────────────────────


async def test_no_filters_returns_everything_and_says_it_filtered_by_nothing(backend):
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter()

    assert result["ok"] is True
    assert set(result["handles"]) == set(handles.values())
    assert result["count"] == 3
    assert list(result["filtered_by"]) == []


async def test_filtering_by_layer_names_the_layer_filter_in_the_response(backend):
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(layer="GEOMETRY")

    assert set(result["handles"]) == {handles["circle"], handles["square"]}
    assert set(result["filtered_by"]) == {"layer"}


async def test_an_empty_result_is_distinguishable_from_an_unfiltered_one(backend):
    """The honesty case: "found none" and "never looked" must not both be [].

    DIM exists and holds nothing, so the handle list is empty either way; only
    ``filtered_by`` tells the caller a layer filter ran at all.
    """
    await _mixed_properties(backend)
    await backend.layer_create("DIM")

    filtered = await backend.selection_filter(layer="DIM")
    unfiltered = await backend.selection_filter()

    assert filtered["ok"] is True
    assert filtered["count"] == 0
    assert set(filtered["filtered_by"]) == {"layer"}
    assert list(unfiltered["filtered_by"]) == []
    assert unfiltered["count"] == 3


async def test_a_typo_in_a_filter_name_is_a_type_error_not_an_empty_list(backend):
    """Why this tool takes named parameters instead of a query string.

    ``query("LINE[nosuchattr=='x']")`` answers [] — measured — and the caller
    reads it as "no matches". A wrong keyword must fail loudly instead.
    """
    await _mixed_properties(backend)

    with pytest.raises(TypeError):
        await backend.selection_filter(layar="GEOMETRY")


async def test_every_applied_filter_is_reported_not_just_the_first(backend):
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(layer="GEOMETRY", entity_type="CIRCLE")

    assert set(result["handles"]) == {handles["circle"]}
    assert set(result["filtered_by"]) == {"layer", "entity_type"}


# ── selection_filter: the individual filters ────────────────────────────────


async def test_filtering_by_entity_type(backend):
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(entity_type="LWPOLYLINE")

    assert set(result["handles"]) == {handles["square"]}
    assert set(result["filtered_by"]) == {"entity_type"}


async def test_filtering_by_colour(backend):
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(color=3)

    assert set(result["handles"]) == {handles["square"]}
    assert set(result["filtered_by"]) == {"color"}


async def test_colour_zero_is_byblock_not_no_filter(backend):
    """ACI 0 is ByBlock, a real colour. ``if color:`` would drop this filter.

    The failure it guards is the worst kind: the caller asks for ByBlock and
    gets the entire drawing back, reported as a successful colour filter.
    """
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(color=0)

    assert set(result["handles"]) == {handles["line"]}
    assert set(result["filtered_by"]) == {"color"}


async def test_filtering_by_linetype(backend):
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(linetype="HIDDEN")

    assert set(result["handles"]) == {handles["line"]}
    assert set(result["filtered_by"]) == {"linetype"}


async def test_min_area_keeps_only_the_shapes_that_are_big_enough(backend):
    """Measured: the square encloses 400, the circle 314.16."""
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(min_area=350.0)

    assert set(result["handles"]) == {handles["square"]}
    assert set(result["filtered_by"]) == {"min_area"}


async def test_min_area_admits_both_shapes_when_the_threshold_is_low(backend):
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(min_area=100.0)

    assert set(result["handles"]) == {handles["circle"], handles["square"]}


async def test_min_area_excludes_entities_that_enclose_no_area(backend):
    """A LINE has no area at all — it is not an entity of area zero to be kept
    by a zero threshold, it is an entity the question does not apply to."""
    handles = await _mixed_properties(backend)

    result = await backend.selection_filter(min_area=0.0)

    assert handles["line"] not in result["handles"]
    assert set(result["filtered_by"]) == {"min_area"}


async def test_a_negative_min_area_is_refused(backend):
    """No area is negative, so the threshold is either a typo or a unit error."""
    await _mixed_properties(backend)

    result = await backend.selection_filter(min_area=-1.0)

    assert result["ok"] is False
    assert "min_area" in result["error"]


async def test_the_filter_count_matches_the_handles(backend):
    await _mixed_properties(backend)

    result = await backend.selection_filter(layer="GEOMETRY")

    assert result["count"] == len(result["handles"]) == 2
