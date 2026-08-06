"""F3 — boundary tracing: `boundary_trace` (BOUNDARY/BPOLY) and `boundary_from_entities`.

Two tools that turn loose edges into one closed LWPOLYLINE. Every assertion here
exists because a measured ezdxf behaviour makes the *plausible* implementation
lie:

* **A boundary with no boundary is the silent-success case.** A seed dropped in
  open space, or inside a "region" whose edges leave a gap, has no enclosing
  loop. Returning an empty or open polyline with ``ok: True`` is exactly the
  failure mode this repo spent a milestone deleting, so both are refusals — and
  the gap case must name the dangling endpoint, because "no loop here" and
  "your top edge is 5 mm short" are different problems with different fixes.

* **`edgeminer.find_all_loops` is O(n!)** — measured 63.9 s at 40 edges and a
  TimeoutError at 60. It must never be called; `find_loop_by_edge` is 0.0057 s
  at 60 edges. Nothing below asserts on runtime, but the nested-region test
  pins the *behaviour* that makes the cheap call correct: the loop returned is
  the nearest enclosing one, not the outermost.

* **`edgesmith.lwpolyline_from_chain` does not close what it chains.** Measured
  on four LINEs of a square: five points, the fifth a duplicate of the first,
  and ``closed = False``. That polyline draws like a square and behaves like an
  open one — HATCH association, area queries and `entity_offset` all see an open
  path. So the stored entity is checked for the closed *flag* and for the
  absence of the duplicate vertex, not just for looking right.

* **`edgesmith.loop_area` ignores bulges.** Measured on two semicircular ARCs
  chained into a circle: it returns **0.0** where the true area is 314.16. Use
  it and every arc-bounded boundary — every fillet, every slot, every keyway
  pocket — reports an area of roughly nothing while claiming success.

* **The reported area comes from the polyline, not from a copy of it.** The
  first fix for the bulge problem flattened the chain at a fixed 0.01 sag,
  which left `boundary_trace` saying 139.2177 and `entity_measure` saying
  139.2699 about the same figure, with nothing in either payload marking one
  as the approximation. The stored vertices carry exact bulges, so both tools
  now read those and round the same way.

Coordinates in and out are WCS, as everywhere else. ``area`` is unsigned:
the same square wound clockwise or counter-clockwise measures 1600.0 either
way, and a caller asking "how big is this pocket" never wants a negative
answer.
"""

from __future__ import annotations

import math
import re

import pytest

pytestmark = pytest.mark.asyncio

# A 40 x 40 square: area 1600.
SQUARE = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)]

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    """Every number an error message mentions, however it was formatted."""
    return [float(match) for match in _NUMBER.findall(text)]


def _points(backend, handle) -> list[tuple[float, float]]:
    entity = backend._doc.entitydb.get(handle)
    return [(round(float(p[0]), 6), round(float(p[1]), 6)) for p in entity.get_points()]


def _bulges(backend, handle) -> list[float]:
    entity = backend._doc.entitydb.get(handle)
    return [float(p[4]) for p in entity.get_points()]


async def _square_of_lines(backend, points=SQUARE, layer=None):
    """Four LINEs, corner to corner, returned as handles."""
    handles = []
    for index in range(len(points)):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % len(points)]
        line = await backend.entity_create_line(x1, y1, x2, y2, layer=layer)
        handles.append(line.handle)
    return handles


# ── boundary_trace: the happy path, measured ────────────────────────────────


async def test_a_seed_inside_a_square_traces_its_four_edges(backend):
    await _square_of_lines(backend)

    result = await backend.boundary_trace(20.0, 20.0)

    assert result["ok"] is True
    assert result["closed"] is True
    assert result["vertices"] == 4, "four edges, four corners — not five"
    assert result["area"] == pytest.approx(1600.0)


async def test_the_traced_boundary_is_a_real_lwpolyline(backend):
    await _square_of_lines(backend)

    result = await backend.boundary_trace(20.0, 20.0)

    entity = backend._doc.entitydb.get(result["handle"])
    assert entity.dxftype() == "LWPOLYLINE"
    assert set(_points(backend, result["handle"])) == set(SQUARE)


async def test_the_boundary_is_closed_by_flag_not_by_a_duplicate_vertex(backend):
    """`lwpolyline_from_chain` measured: 5 points, last == first, closed False.

    That polyline looks shut and is not. Hatching, offsetting and area queries
    all treat it as an open path.
    """
    await _square_of_lines(backend)

    result = await backend.boundary_trace(20.0, 20.0)

    entity = backend._doc.entitydb.get(result["handle"])
    assert entity.closed is True
    stored = _points(backend, result["handle"])
    assert len(stored) == 4
    assert stored[0] != stored[-1]


async def test_the_boundary_survives_save_and_reload(backend, tmp_path):
    import ezdxf

    await _square_of_lines(backend)
    result = await backend.boundary_trace(20.0, 20.0)

    path = tmp_path / "boundary.dxf"
    await backend.drawing_save_as(str(path))

    reloaded = ezdxf.readfile(str(path)).entitydb.get(result["handle"])
    assert reloaded.dxftype() == "LWPOLYLINE"
    assert reloaded.closed is True
    assert len(list(reloaded.get_points())) == 4


# ── boundary_trace: the refusals ────────────────────────────────────────────


async def test_a_seed_outside_every_closed_region_is_refused(backend):
    """A boundary with no boundary. An empty polyline reported as success is
    the exact shape of lie this repo removes tools for."""
    await _square_of_lines(backend)

    result = await backend.boundary_trace(500.0, 500.0)

    assert result["ok"] is False
    assert result.get("handle") is None
    assert result["error"]


async def test_a_seed_in_open_space_with_no_edges_at_all_is_refused(backend):
    result = await backend.boundary_trace(0.0, 0.0)

    assert result["ok"] is False
    assert result["error"]


async def test_a_region_with_a_gap_is_refused_and_the_error_names_the_gap(backend):
    """The top edge stops 5 mm short at (35, 40).

    "No loop found" sends the user hunting the whole drawing; naming the
    dangling endpoint sends them to the one line that is short.
    """
    await backend.entity_create_line(0, 0, 40, 0)
    await backend.entity_create_line(40, 0, 40, 40)
    await backend.entity_create_line(40, 40, 35, 40)  # 5 mm short of the corner
    await backend.entity_create_line(0, 40, 0, 0)

    result = await backend.boundary_trace(20.0, 20.0)

    assert result["ok"] is False
    assert "gap" in result["error"].lower()
    assert 35.0 in _numbers(result["error"]), (
        "the error must carry the dangling endpoint, not just the word 'gap'"
    )


async def test_a_gap_inside_the_tolerance_is_closed(backend):
    """Real drawings miss corners by microns; tolerance is what it is for."""
    await backend.entity_create_line(0, 0, 40, 0)
    await backend.entity_create_line(40, 0, 40, 40)
    await backend.entity_create_line(40, 40, 0.001, 40)
    await backend.entity_create_line(0, 40, 0, 0)

    result = await backend.boundary_trace(20.0, 20.0, tolerance=0.01)

    assert result["ok"] is True
    assert result["closed"] is True
    assert result["area"] == pytest.approx(1600.0, rel=1e-3)


async def test_the_same_gap_outside_the_tolerance_is_still_refused(backend):
    """Otherwise `tolerance` would be decoration."""
    await backend.entity_create_line(0, 0, 40, 0)
    await backend.entity_create_line(40, 0, 40, 40)
    await backend.entity_create_line(40, 40, 0.001, 40)
    await backend.entity_create_line(0, 40, 0, 0)

    result = await backend.boundary_trace(20.0, 20.0, tolerance=1e-9)

    assert result["ok"] is False


# ── boundary_trace: which loop, and what it is made of ──────────────────────


async def test_nested_regions_return_the_inner_loop(backend):
    """AutoCAD's BOUNDARY traces the nearest enclosing loop.

    Returning the outer square here would hatch straight over the island —
    and it is also the answer a whole-drawing loop search hands you first.
    """
    await _square_of_lines(backend)  # 40 x 40, area 1600
    inner = [(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)]
    await _square_of_lines(backend, inner)  # 20 x 20, area 400

    result = await backend.boundary_trace(20.0, 20.0)

    assert result["ok"] is True
    assert result["area"] == pytest.approx(400.0)
    assert set(_points(backend, result["handle"])) == set(inner)


async def test_a_seed_between_the_two_loops_returns_the_outer_ring(backend):
    """Same drawing, different seed: the loop that encloses *this* point."""
    await _square_of_lines(backend)
    await _square_of_lines(backend, [(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)])

    result = await backend.boundary_trace(5.0, 5.0)

    assert result["ok"] is True
    assert result["area"] == pytest.approx(1600.0)


async def test_source_handles_report_which_entities_formed_the_boundary(backend):
    """A traced boundary the caller cannot attribute is a polyline from nowhere."""
    handles = await _square_of_lines(backend)

    result = await backend.boundary_trace(20.0, 20.0)

    assert set(result["source_handles"]) == set(handles)
    assert result["handle"] not in result["source_handles"]


async def test_the_boundary_is_independent_of_its_sources(backend):
    """BOUNDARY makes a new entity, it does not alias the edges. Deleting a
    source line must not empty the polyline that was traced from it."""
    handles = await _square_of_lines(backend)
    result = await backend.boundary_trace(20.0, 20.0)

    await backend.entity_delete(handles[0])

    entity = backend._doc.entitydb.get(result["handle"])
    assert entity is not None
    assert entity.closed is True
    assert set(_points(backend, result["handle"])) == set(SQUARE)


async def test_the_layer_filter_restricts_the_candidate_edges(backend):
    """A construction line through the square halves it.

    Unfiltered, the nearest enclosing loop around (10, 20) is the left half
    (area 800). Scoped to GEOMETRY, the divider is not an edge and the loop is
    the whole square (1600). If `layer` were ignored both answers would be 800.
    """
    await _square_of_lines(backend, layer="GEOMETRY")
    await backend.entity_create_line(20, 0, 20, 40, layer="CONSTRUCTION")

    unfiltered = await backend.boundary_trace(10.0, 20.0)
    filtered = await backend.boundary_trace(10.0, 20.0, layer="GEOMETRY")

    assert unfiltered["area"] == pytest.approx(800.0)
    assert filtered["area"] == pytest.approx(1600.0)


# ── boundary_from_entities: the happy path ──────────────────────────────────


async def test_named_entities_chain_into_one_closed_polyline(backend):
    """The handles arrive in drawing order, not chain order; chaining is the
    tool's whole job."""
    handles = await _square_of_lines(backend)
    shuffled = [handles[2], handles[0], handles[3], handles[1]]

    result = await backend.boundary_from_entities(shuffled)

    assert result["ok"] is True
    assert result["closed"] is True
    assert result["vertices"] == 4
    assert result["area"] == pytest.approx(1600.0)
    assert set(_points(backend, result["handle"])) == set(SQUARE)


async def test_the_chained_polyline_is_closed_without_a_duplicate_vertex(backend):
    handles = await _square_of_lines(backend)

    result = await backend.boundary_from_entities(handles)

    entity = backend._doc.entitydb.get(result["handle"])
    assert entity.closed is True
    stored = _points(backend, result["handle"])
    assert len(stored) == 4 and stored[0] != stored[-1]


async def test_a_chained_polyline_round_trips(backend, tmp_path):
    import ezdxf

    handles = await _square_of_lines(backend)
    result = await backend.boundary_from_entities(handles)

    path = tmp_path / "chained.dxf"
    await backend.drawing_save_as(str(path))

    reloaded = ezdxf.readfile(str(path)).entitydb.get(result["handle"])
    assert reloaded.closed is True
    assert len(list(reloaded.get_points())) == 4


async def test_two_arcs_close_into_a_circle_whose_area_counts_the_bulges(backend):
    """`loop_area` returns 0.0 for this exact chain (measured).

    Two semicircles enclose 314.16 mm^2; their two *vertices* enclose nothing.
    An area taken from the vertex polygon reports ~0 for every arc-bounded
    pocket in the drawing while still saying ok.
    """
    upper = await backend.entity_create_arc(0, 0, 10, 0, 180)
    lower = await backend.entity_create_arc(0, 0, 10, 180, 360)

    result = await backend.boundary_from_entities([upper.handle, lower.handle])

    assert result["ok"] is True
    assert result["closed"] is True
    assert result["area"] == pytest.approx(math.pi * 100.0, rel=1e-3)
    assert all(abs(b) > 0.1 for b in _bulges(backend, result["handle"])), (
        "a circle stored without bulges is a straight line between two points"
    )


async def test_the_reported_area_matches_measuring_the_polyline_it_just_made(backend):
    """Two tools, one shape, one number.

    `boundary_*` used to report a curve flattened at a fixed 0.01 sag while
    `entity_measure` read the stored bulges analytically, so the same figure
    came back as 139.2177 from one tool and 139.2699 from the other with
    nothing in either payload to say which was the approximation. The polyline
    carries exact bulges either way — there was never a reason to answer from
    a coarser copy of it.
    """
    # 10 x 10 square whose top edge is a semicircle: 100 + pi*25/2.
    await backend.entity_create_line(0, 0, 10, 0)
    await backend.entity_create_line(10, 0, 10, 10)
    await backend.entity_create_line(0, 10, 0, 0)
    await backend.entity_create_arc(5, 10, 5, 0, 180)

    traced = await backend.boundary_trace(5.0, 5.0)
    measured = await backend.entity_measure(traced["handle"])

    true_area = 100.0 + math.pi * 25.0 / 2.0
    # rel=1e-8, not tighter: both tools round to six decimals on the way out.
    assert measured["area"] == pytest.approx(true_area, rel=1e-8)
    assert traced["area"] == pytest.approx(measured["area"], rel=1e-9), (
        "the boundary tool and the measure tool must not disagree about the "
        "shape the boundary tool just drew"
    )


async def test_boundary_from_entities_reports_the_same_exact_area(backend):
    upper = await backend.entity_create_arc(0, 0, 10, 0, 180)
    lower = await backend.entity_create_arc(0, 0, 10, 180, 360)

    result = await backend.boundary_from_entities([upper.handle, lower.handle])
    measured = await backend.entity_measure(result["handle"])

    assert result["area"] == pytest.approx(math.pi * 100.0, rel=1e-8)
    assert result["area"] == pytest.approx(measured["area"], rel=1e-12), "same six decimals"


# ── boundary_from_entities: the refusals ────────────────────────────────────


async def test_an_open_chain_is_refused_and_names_the_dangling_endpoint(backend):
    """Three sides of a rectangle: the ends at (0, 0) and (7, 25) never meet."""
    first = await backend.entity_create_line(0, 0, 40, 0)
    second = await backend.entity_create_line(40, 0, 40, 25)
    third = await backend.entity_create_line(40, 25, 7, 25)

    result = await backend.boundary_from_entities([first.handle, second.handle, third.handle])

    assert result["ok"] is False
    reported = _numbers(result["error"])
    assert 7.0 in reported and 25.0 in reported, (
        "'not closed' without the loose end leaves the caller measuring by hand"
    )


async def test_a_chain_broken_in_the_middle_is_refused(backend):
    """Four segments, all four endpoints paired — except two of them are 6 mm
    apart. The count is right and the chain is not."""
    first = await backend.entity_create_line(0, 0, 40, 0)
    second = await backend.entity_create_line(40, 0, 40, 40)
    third = await backend.entity_create_line(34, 40, 0, 40)  # starts 6 mm inboard
    fourth = await backend.entity_create_line(0, 40, 0, 0)

    result = await backend.boundary_from_entities(
        [first.handle, second.handle, third.handle, fourth.handle]
    )

    assert result["ok"] is False
    assert 34.0 in _numbers(result["error"])


@pytest.mark.parametrize("count", [0, 1])
async def test_fewer_than_two_handles_is_refused(backend, count):
    """One segment cannot enclose anything; zero is a no-op reported as work."""
    handles = await _square_of_lines(backend)

    result = await backend.boundary_from_entities(handles[:count])

    assert result["ok"] is False


async def test_a_non_linear_entity_is_refused_by_type(backend):
    """TEXT has no endpoints. Skipping it quietly would chain three of four
    sides and then blame the geometry for not closing."""
    handles = await _square_of_lines(backend)
    text = await backend.entity_create_text("NOTE", 20, 20)

    result = await backend.boundary_from_entities(handles + [text.handle])

    assert result["ok"] is False
    assert "TEXT" in result["error"]
    assert text.handle in result["error"]


async def test_an_unresolvable_handle_is_refused_by_name(backend):
    handles = await _square_of_lines(backend)

    result = await backend.boundary_from_entities(handles + ["NOSUCH"])

    assert result["ok"] is False
    assert "NOSUCH" in result["error"]
