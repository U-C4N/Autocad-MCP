"""Obstacle-aware P&ID routing (plan 1.8-20).

The gate: property tests of ``route_avoiding`` over random obstacle fields
(orthogonal, never inside an inflated box, stubs honoured, deterministic, no
dearer than any of ``lines.route``'s fixed candidates that are clean), a sheet
where ``auto`` runs through a vessel and ``avoid`` goes round it with
``obstacle_hits == []`` and no new dangling end, the dry run's vertices equal
to the drawn ones, the ``no_route`` / ``routing_budget_exceeded`` refusals
and the ``pid_line_through_symbol`` focus.
"""

from __future__ import annotations

import copy
import math
import random
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends.ezdxf_backend import EzdxfBackend
from engineering import errors
from engineering.critique import run_critique
from engineering.critique_catalog import FOCUS_CATALOG
from engineering.layers import apply_layer_set
from engineering.pid.critique import PID_FOCUSES
from engineering.pid.drawlines import draw_line, scan_sheet
from engineering.pid.graph import build_graph
from engineering.pid.insert import place_symbol
from engineering.pid.lines import AXIS, _validate, count_crossings, route
from engineering.pid.router import (
    NoRouteError,
    RoutingBudgetError,
    box_entered,
    obstacle_hits,
    route_avoiding,
    route_cost,
)
from engineering.pid.spec import run_spec, validate_spec
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.qa_coverage import LEDGER_KEY

FOCUS = "pid_line_through_symbol"
AXES = (0.0, 90.0, 180.0, 270.0)


# ── the pure router: properties over random fields ──────────────────────────


def _field(rng: random.Random, count: int) -> list[dict]:
    boxes = []
    for k in range(count):
        x, y = rng.uniform(-120, 120), rng.uniform(-120, 120)
        w, h = rng.uniform(3, 35), rng.uniform(3, 35)
        boxes.append({"box": (x, y, x + w, y + h), "tag": f"T-{k}"})
    return boxes


def _inflated(box, by):
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def _free(point, obstacles, clearance) -> bool:
    return not any(
        _inflated(o["box"], clearance)[0] - 1 <= point[0] <= _inflated(o["box"], clearance)[2] + 1
        and _inflated(o["box"], clearance)[1] - 1
        <= point[1]
        <= _inflated(o["box"], clearance)[3] + 1
        for o in obstacles
    )


def _ports(rng: random.Random, obstacles, clearance, stub):
    """Two ports whose stubs lie clear of every inflated box."""
    while True:
        s = (rng.uniform(-150, 150), rng.uniform(-150, 150))
        e = (rng.uniform(-150, 150), rng.uniform(-150, 150))
        ds, de = rng.choice(AXES), rng.choice(AXES)
        s1 = (s[0] + AXIS[ds][0] * stub, s[1] + AXIS[ds][1] * stub)
        e1 = (e[0] + AXIS[de][0] * stub, e[1] + AXIS[de][1] * stub)
        if math.dist(s, e) > 1 and all(_free(p, obstacles, clearance) for p in (s, s1, e, e1)):
            return s, ds, e, de


def _existing(rng: random.Random, count: int) -> list[list[tuple[float, float]]]:
    out = []
    for _ in range(count):
        x, y = rng.uniform(-150, 150), rng.uniform(-150, 150)
        x2, y2 = rng.uniform(-150, 150), rng.uniform(-150, 150)
        out.append([(x, y), (x2, y), (x2, y2)])
    return out


def _cases(seed: int, n: int):
    rng = random.Random(seed)
    for _ in range(n):
        obstacles = _field(rng, rng.randint(0, 14))
        clearance, stub = rng.choice((0.0, 1.0, 2.0)), rng.choice((0.0, 3.0, 5.0))
        s, ds, e, de = _ports(rng, obstacles, clearance, stub)
        yield obstacles, _existing(rng, rng.randint(0, 3)), clearance, stub, s, ds, e, de


def _try(*args, **kwargs):
    try:
        return route_avoiding(*args, **kwargs)
    except NoRouteError:
        return None


def test_every_route_is_orthogonal_clear_and_leaves_along_its_stubs():
    routed = 0
    for obstacles, existing, clearance, stub, s, ds, e, de in _cases(11, 120):
        found = _try(s, ds, e, de, obstacles, existing, clearance=clearance, stub=stub)
        if found is None:
            continue
        routed += 1
        pts = found.vertices
        assert pts[0] == s and pts[-1] == e
        for a, b in zip(pts, pts[1:], strict=False):
            assert abs(a[0] - b[0]) < 1e-9 or abs(a[1] - b[1]) < 1e-9, pts
            for o in obstacles:
                assert not box_entered(a, b, _inflated(o["box"], clearance)), (o, pts)
        first = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
        last = (pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
        assert math.isclose(first[0], AXIS[ds][0] * math.hypot(*first), abs_tol=1e-9)
        assert math.isclose(first[1], AXIS[ds][1] * math.hypot(*first), abs_tol=1e-9)
        assert math.isclose(last[0], -AXIS[de][0] * math.hypot(*last), abs_tol=1e-9)
        assert math.isclose(last[1], -AXIS[de][1] * math.hypot(*last), abs_tol=1e-9)
        if len(pts) > 2:
            assert math.hypot(*first) >= stub - 1e-9 and math.hypot(*last) >= stub - 1e-9
    assert routed >= 100, "the random fields must mostly be routable"


def test_the_same_field_gives_the_same_route_whatever_the_obstacle_order():
    for obstacles, existing, clearance, stub, s, ds, e, de in _cases(23, 40):
        one = _try(s, ds, e, de, obstacles, existing, clearance=clearance, stub=stub)
        two = _try(s, ds, e, de, obstacles, existing, clearance=clearance, stub=stub)
        shuffled = list(reversed(obstacles))
        three = _try(s, ds, e, de, shuffled, existing, clearance=clearance, stub=stub)
        if one is None:
            assert two is None and three is None
            continue
        assert one.vertices == two.vertices == three.vertices
        assert one.cost == two.cost == three.cost


def _fixed_candidates(s, ds, e, de, stub):
    """``lines.route``'s seven candidates, each kept when ``_validate`` keeps it."""
    us, ue = AXIS[ds], AXIS[de]
    s1 = (s[0] + us[0] * stub, s[1] + us[1] * stub)
    e1 = (e[0] + ue[0] * stub, e[1] + ue[1] * stub)
    xm, ym = (s1[0] + e1[0]) / 2.0, (s1[1] + e1[1]) / 2.0
    raw = [
        [s, e],
        [s, (e[0], s[1]), e],
        [s, (s[0], e[1]), e],
        [s, (xm, s[1]), (xm, e[1]), e],
        [s, (s[0], ym), (e[0], ym), e],
        [s, s1, (s1[0], e1[1]), e1, e],
        [s, s1, (e1[0], s1[1]), e1, e],
    ]
    return [p for p in (_validate(c, ds, de, stub) for c in raw) if p]


def test_the_search_is_never_dearer_than_a_clean_fixed_candidate():
    compared = 0
    for obstacles, existing, clearance, stub, s, ds, e, de in _cases(37, 150):
        clean = [
            c
            for c in _fixed_candidates(s, ds, e, de, stub)
            if not any(
                box_entered(a, b, _inflated(o["box"], clearance))
                for a, b in zip(c, c[1:], strict=False)
                for o in obstacles
            )
        ]
        if not clean:
            continue
        found = route_avoiding(s, ds, e, de, obstacles, existing, clearance=clearance, stub=stub)
        assert math.isclose(found.cost, route_cost(found.vertices, existing), abs_tol=1e-6)
        best = min(route_cost(c, existing) for c in clean)
        assert found.cost <= best + 1e-6, (found.vertices, clean)
        compared += 1
    assert compared >= 60


def test_on_a_bare_field_the_search_agrees_with_auto():
    """No obstacle, no line: the cheapest route is the fixed one's cost."""
    rng = random.Random(5)
    for _ in range(60):
        s = (rng.uniform(-100, 100), rng.uniform(-100, 100))
        e = (rng.uniform(-100, 100), rng.uniform(-100, 100))
        ds, de = rng.choice(AXES), rng.choice(AXES)
        try:
            auto = route(s, ds, e, de, stub=5.0)
        except ValueError:
            continue
        found = route_avoiding(s, ds, e, de, [], [], stub=5.0)
        assert found.cost <= route_cost(auto) + 1e-6


def test_a_crossing_is_priced_and_counted_on_the_final_route():
    wall = [[(50.0, -100.0), (50.0, 100.0)]]
    found = route_avoiding((0, 0), 0.0, (100, 0), 180.0, [], wall)
    assert found.crossings == 1 == count_crossings(found.vertices, wall)
    assert found.cost == pytest.approx(100.0 + 25.0)


def test_a_line_already_on_the_way_is_not_run_along():
    """An overlap costs what a crossing costs: the route steps off it."""
    along = [[(10.0, 0.0), (90.0, 0.0)]]
    found = route_avoiding((0, 0), 0.0, (100, 0), 180.0, [{"box": (40, 30, 60, 40)}], along)
    assert found.overlaps <= 1
    assert found.cost == pytest.approx(route_cost(found.vertices, along))


# ── refusals ────────────────────────────────────────────────────────────────


_RING = [
    {"box": (-20, -20, 20, -10), "tag": "A-1"},
    {"box": (-20, 10, 20, 20), "tag": "B-1"},
    {"box": (-20, -20, -10, 20), "tag": "C-1"},
    {"box": (10, -20, 20, 20), "tag": "D-1"},
    {"box": (300, 300, 310, 310), "tag": "FAR-1"},
]


def test_an_enclosed_port_is_no_route_naming_the_ring():
    with pytest.raises(NoRouteError) as caught:
        route_avoiding((0, 0), 0.0, (100, 0), 180.0, _RING)
    exc = caught.value
    assert exc.kind == "no_route"
    assert sorted(o["tag"] for o in exc.obstacles) == ["A-1", "B-1", "C-1", "D-1"]
    for tag in ("A-1", "B-1", "C-1", "D-1"):
        assert tag in str(exc)
    assert "FAR-1" not in str(exc)
    row = errors.classify(exc)
    assert row["kind"] == "no_route" and row["clearance"] == 2.0
    assert sorted(o["tag"] for o in row["obstacles"]) == ["A-1", "B-1", "C-1", "D-1"]
    assert errors.describe("no_route").category == "geometry"


def test_on_blocked_cross_goes_through_the_fewest_boxes_and_says_so():
    found = route_avoiding((0, 0), 0.0, (100, 0), 180.0, _RING, on_blocked="cross")
    assert [r["tag"] for r in obstacle_hits(found.vertices, _RING)] == ["D-1"]


def test_a_search_past_its_budget_is_refused_by_kind():
    obstacles = _field(random.Random(3), 12)
    with pytest.raises(RoutingBudgetError) as caught:
        route_avoiding((-200, -200), 0.0, (200, 200), 180.0, obstacles, max_nodes=3)
    assert caught.value.kind == "routing_budget_exceeded"
    assert errors.classify(caught.value)["kind"] == "routing_budget_exceeded"
    assert errors.classify(caught.value)["max_nodes"] == 3


def test_the_routing_kinds_are_declared_and_survive_a_path_prefix():
    for kind in ("no_route", "routing_budget_exceeded"):
        assert kind in errors.ERROR_KINDS and kind in errors.ERROR_TABLE
    with pytest.raises(NoRouteError) as caught:
        route_avoiding((0, 0), 0.0, (100, 0), 180.0, _RING)
    prefixed = caught.value.prefixed("lines[2]")
    wrapped = ValueError("outer")
    wrapped.__cause__ = prefixed
    row = errors.classify(wrapped)
    assert row["kind"] == "no_route" and row["message"].startswith("lines[2]: no route")


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"clearance": -1}, "clearance"),
        ({"stub": math.nan}, "stub"),
        ({"max_nodes": 0}, "max_nodes"),
        ({"on_blocked": "ignore"}, "on_blocked"),
    ],
)
def test_bad_options_are_refused_by_name(kwargs, match):
    with pytest.raises(ValueError, match=match):
        route_avoiding((0, 0), 0.0, (100, 0), 180.0, [], **kwargs)


# ── the drawing: auto through a vessel, avoid around it ─────────────────────


SHEET = {
    "equipment": [
        {"id": "P-1", "symbol": "centrifugal_pump", "x": 0, "y": -20, "tag": "P-101"},
        {"id": "V-1", "symbol": "vertical_vessel", "x": 80, "y": 0, "tag": "V-101"},
        {"id": "V-2", "symbol": "vertical_vessel", "x": 160, "y": 0, "tag": "V-102"},
    ],
    "lines": [{"from": "P-1.discharge", "to": "V-2.N3", "class": "process_major"}],
}


def _sheet(routing: str | None) -> dict:
    spec = copy.deepcopy(SHEET)
    if routing is not None:
        spec["sheet"] = {"routing": routing}
    return spec


@pytest_asyncio.fixture
async def pid_backend():
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    await apply_layer_set(b, "pid")
    yield b
    await b.disconnect()


async def _polyline_points(backend, handle):
    info = await backend.entity_get(handle)
    return [[float(x), float(y)] for x, y in info.properties["points"]]


@pytest.mark.asyncio
async def test_auto_runs_through_the_vessel_and_the_focus_names_it(pid_backend):
    result = await run_spec(pid_backend, _sheet(None))
    assert result["routing"] == "auto"
    assert [(h["id"], h["tag"]) for h in result["obstacle_hits"]] == [("V-1", "V-101")]
    assert result["obstacle_hits"][0]["handle"] == result["handles"]["V-1"]
    through = [i for i in result["critique"] if i["focus"] == FOCUS]
    assert len(through) == 1 and through[0]["severity"] == "error"
    assert result["handles"]["V-1"] in through[0]["handles"]
    issues = await run_critique(pid_backend, [FOCUS])
    assert [(i.focus, i.detail["tag"]) for i in issues] == [(FOCUS, "V-101")]
    # The returned graph keeps its 1.7 shape: no geometry unless asked for.
    assert all("vertices" not in e for e in result["graph"]["edges"])
    assert all("bbox" not in n for n in result["graph"]["nodes"])


@pytest.mark.asyncio
async def test_avoid_goes_round_with_no_hit_and_no_new_dangling_end(pid_backend):
    auto_backend = EzdxfBackend()
    await auto_backend.connect()
    await auto_backend.drawing_new()
    await apply_layer_set(auto_backend, "pid")
    auto = await run_spec(auto_backend, _sheet("auto"))
    result = await run_spec(pid_backend, _sheet("avoid"))
    assert result["routing"] == "avoid" and result["obstacle_hits"] == []
    assert [i for i in result["critique"] if i["focus"] == FOCUS] == []
    graph = await build_graph(pid_backend)
    assert graph["stats"]["dangling"] == auto["graph"]["stats"]["dangling"] == 0
    assert await run_critique(pid_backend, [FOCUS, "pid_dangling_line"]) == []
    line = next(e for e in await pid_backend.entity_list() if e.type == "LWPOLYLINE")
    pts = await _polyline_points(pid_backend, line.handle)
    vessel = (67.0, -23.0, 93.0, 23.0)
    for a, b in zip(pts, pts[1:], strict=False):
        assert not box_entered(tuple(a), tuple(b), _inflated(vessel, 2.0))
    await auto_backend.disconnect()


@pytest.mark.parametrize("routing", ["auto", "avoid"])
@pytest.mark.asyncio
async def test_the_dry_run_vertices_are_the_drawn_ones(pid_backend, routing):
    # A line already on the sheet: the crossings and the search see it on both runs.
    await draw_line(pid_backend, {"x": 120, "y": -60}, {"x": 120, "y": 60}, "process_minor")
    dry = await run_spec(pid_backend, _sheet(routing), dry_run=True)
    applied = await run_spec(pid_backend, _sheet(routing))
    planned = dry["lines"][0]
    assert planned["route"] == routing
    drawn = [
        e
        for e in await pid_backend.entity_list()
        if e.type == "LWPOLYLINE" and e.layer == "PROCESS-PIPING-MAIN"
    ]
    assert len(drawn) == 1
    assert await _polyline_points(pid_backend, drawn[0].handle) == planned["vertices"]
    assert dry["crossings_total"] == applied["crossings_total"]
    strip = [{k: v for k, v in h.items() if k != "handle"} for h in applied["obstacle_hits"]]
    assert strip == [{k: v for k, v in h.items() if k != "handle"} for h in dry["obstacle_hits"]]


@pytest.mark.asyncio
async def test_pid_line_draw_avoid_keeps_clear_of_symbols_and_keep_out(pid_backend):
    pump = await place_symbol(pid_backend, "centrifugal_pump", 0, -20, tag="P-101")
    vessel = await place_symbol(pid_backend, "vertical_vessel", 80, 0, tag="V-101")
    target = await place_symbol(pid_backend, "vertical_vessel", 160, 0, tag="V-102")
    ends = (
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": target["handle"], "port": "N3"},
    )
    auto = await draw_line(pid_backend, *ends)
    assert auto["obstacle_hits"] == [{"handle": vessel["handle"], "tag": "V-101"}]
    keep = [[20, 20, 40, 60]]
    avoid = await draw_line(pid_backend, *ends, route_mode="avoid", keep_out=keep, clearance=3.0)
    assert avoid["obstacle_hits"] == [] and avoid["routing"]["clearance"] == 3.0
    for a, b in zip(avoid["vertices"], avoid["vertices"][1:], strict=False):
        assert not box_entered(tuple(a), tuple(b), _inflated((67, -23, 93, 23), 3.0))
        assert not box_entered(tuple(a), tuple(b), _inflated((20, 20, 40, 60), 3.0))
    # auto reports a keep_out box it runs through, never refuses it
    hit = await draw_line(pid_backend, *ends, keep_out=[[-10, 0, 10, 4]])
    assert {"handle": None, "tag": None, "keep_out": 0} in hit["obstacle_hits"]


@pytest.mark.asyncio
async def test_a_label_over_a_symbol_is_reported_never_moved(pid_backend):
    """Labels stand after routing (the plan's risk note): one that overlaps a
    symbol is named in ``label_hits``, the line itself stays clear."""
    vessel = await place_symbol(pid_backend, "vertical_vessel", 50, 25, tag="V-101")
    over = await draw_line(pid_backend, {"x": 0, "y": 0}, {"x": 100, "y": 0}, line_number="L-1")
    assert over["obstacle_hits"] == []
    assert over["label_hits"] == [{"handle": vessel["handle"], "tag": "V-101"}]
    clear = await draw_line(
        pid_backend, {"x": 0, "y": -40}, {"x": 100, "y": -40}, line_number="L-2"
    )
    assert clear["label_hits"] == []
    unlabelled = await draw_line(
        pid_backend, {"x": 0, "y": 0.5}, {"x": 100, "y": 0.5}, line_number="L-3", label=False
    )
    assert unlabelled["label_hits"] == [] and unlabelled["label_handle"] is None


@pytest.mark.asyncio
async def test_a_spec_reports_its_label_hits_by_path(pid_backend):
    result = await run_spec(pid_backend, _sheet("avoid"))
    assert isinstance(result["label_hits"], list)
    for hit in result["label_hits"]:
        assert hit["where"].startswith("lines[") and set(hit) >= {"handle", "tag"}


@pytest.mark.asyncio
async def test_an_enclosed_port_refuses_no_route_with_tags_and_draws_nothing(pid_backend):
    pump = await place_symbol(pid_backend, "centrifugal_pump", 0, 0, tag="P-101")
    for k, (x, y, rot) in enumerate(((0, 30, 0), (-25, 10, 0), (25, 10, 0), (0, -20, 0))):
        await place_symbol(pid_backend, "horizontal_vessel", x, y, rot, tag=f"TK-{k + 1}")
    far = await place_symbol(pid_backend, "vertical_vessel", 200, 0, tag="V-900")
    before = len(await pid_backend.entity_list())
    with pytest.raises(NoRouteError) as caught:
        await draw_line(
            pid_backend,
            {"handle": pump["handle"], "port": "discharge"},
            {"handle": far["handle"], "port": "N3"},
            route_mode="avoid",
        )
    tags = {o["tag"] for o in caught.value.obstacles}
    assert tags and tags <= {"TK-1", "TK-2", "TK-3", "TK-4"}
    assert "V-900" not in str(caught.value)
    assert len(await pid_backend.entity_list()) == before
    crossed = await draw_line(
        pid_backend,
        {"handle": pump["handle"], "port": "discharge"},
        {"handle": far["handle"], "port": "N3"},
        route_mode="avoid",
        on_blocked="cross",
    )
    assert crossed["obstacle_hits"] and {h["tag"] for h in crossed["obstacle_hits"]} <= tags


@pytest.mark.asyncio
async def test_a_spec_that_cannot_avoid_is_refused_by_path_and_rolled_back(pid_backend):
    spec = {
        "sheet": {"routing": "avoid"},
        "equipment": [
            {"id": "P-1", "symbol": "centrifugal_pump", "x": 0, "y": 0, "tag": "P-101"},
            {"id": "TK-1", "symbol": "horizontal_vessel", "x": 0, "y": 30, "tag": "TK-1"},
            {"id": "TK-2", "symbol": "horizontal_vessel", "x": -25, "y": 10, "tag": "TK-2"},
            {"id": "TK-3", "symbol": "horizontal_vessel", "x": 25, "y": 10, "tag": "TK-3"},
            {"id": "TK-4", "symbol": "horizontal_vessel", "x": 0, "y": -20, "tag": "TK-4"},
            {"id": "V-9", "symbol": "vertical_vessel", "x": 200, "y": 0, "tag": "V-900"},
        ],
        "lines": [{"from": "P-1.discharge", "to": "V-9.N3"}],
    }
    with pytest.raises(NoRouteError, match=r"^lines\[0\]: no route") as dry:
        await run_spec(pid_backend, spec, dry_run=True)
    assert {o["tag"] for o in dry.value.obstacles} <= {"TK-1", "TK-2", "TK-3", "TK-4"}
    with pytest.raises(NoRouteError, match=r"^lines\[0\]: no route"):
        await run_spec(pid_backend, spec)
    assert await pid_backend.entity_list() == []


def test_sheet_routing_is_auto_or_avoid():
    assert validate_spec({"sheet": {"routing": "avoid"}})["sheet"]["routing"] == "avoid"
    with pytest.raises(ValueError, match="sheet.routing"):
        validate_spec({"sheet": {"routing": "around"}})


# ── the focus ───────────────────────────────────────────────────────────────


def test_the_focus_is_registered_like_every_pid_focus():
    assert FOCUS in ALL_CRITIQUE_FOCUSES and FOCUS in PID_FOCUSES
    assert FOCUS_CATALOG[FOCUS][0] == "pid"


@pytest.mark.asyncio
async def test_the_focus_is_silent_without_pid_symbols(backend):
    backend._doc.modelspace().add_line(
        (0, 0), (100, 0), dxfattribs={"layer": "PROCESS-PIPING-MAIN"}
    )
    shared: dict = {}
    assert await run_critique(backend, [FOCUS], shared=shared) == []
    rows = [r for r in shared[LEDGER_KEY].rows if r.focus == FOCUS]
    assert rows and all(r.status == "not_checked" and r.reason == "domain_absent" for r in rows)


@pytest.mark.asyncio
async def test_a_line_ending_on_the_symbol_or_running_along_it_is_not_through(pid_backend):
    vessel = await place_symbol(pid_backend, "vertical_vessel", 80, 0, tag="V-101")
    # Along the top edge, and ending on a nozzle: neither is "through".
    await draw_line(pid_backend, {"x": 40, "y": 23}, {"x": 120, "y": 23}, "process_minor")
    await draw_line(pid_backend, {"x": 40, "y": 8}, {"handle": vessel["handle"], "port": "N3"})
    assert await run_critique(pid_backend, [FOCUS]) == []
    await draw_line(pid_backend, {"x": 40, "y": -5}, {"x": 120, "y": -5}, "process_minor")
    issues = await run_critique(pid_backend, [FOCUS])
    assert [i.detail["symbol"] for i in issues] == [vessel["handle"]]


# ── the other engine's names, and the served tool ───────────────────────────


@pytest.mark.asyncio
async def test_the_sheet_scan_reads_the_live_engines_type_names():
    """On COM an INSERT is BLOCKREFERENCE and a lightweight polyline POLYLINE."""

    def info(handle, type_, layer, **props):
        return SimpleNamespace(handle=handle, type=type_, layer=layer, properties=props)

    rows = [
        info(
            "A1",
            "BLOCKREFERENCE",
            "EQUIPMENT",
            block_name="PID_VESSEL_VERTICAL",
            geometry_bbox={"min": [67, -23], "max": [93, 23]},
        ),
        info("A2", "BLOCKREFERENCE", "PROCESS-PIPING-MAIN", block_name="PID_MARKER_ARROW_FLOW"),
        info(
            "A3",
            "BLOCKREFERENCE",
            "0",
            block_name="TITLE",
            bounding_box={"min": [0, 0], "max": [9, 9]},
        ),
        info("L1", "POLYLINE", "PROCESS-PIPING-MAIN", points=[[0, 0], [10, 0]]),
    ]

    class Fake:
        async def entity_list(self, limit=1000, offset=0, **_kw):
            return rows[offset : offset + limit]

        async def get_xdata(self, *_a, **_k):  # pragma: no cover - not reached
            return {}

    async def no_payload(_backend, _handle):
        return None

    import engineering.pid.drawlines as drawlines

    original = drawlines.read_payload
    drawlines.read_payload = no_payload
    try:
        lines, symbols = await scan_sheet(Fake())
    finally:
        drawlines.read_payload = original
    assert symbols == [{"handle": "A1", "box": (67.0, -23.0, 93.0, 23.0)}]
    assert [ln["handle"] for ln in lines] == ["L1"]


@pytest_asyncio.fixture
async def served(monkeypatch):
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    await apply_layer_set(backend, "pid")

    async def _make():
        return backend

    monkeypatch.setattr(server, "_make_backend", _make)
    async with Client(server.mcp) as client:
        yield client, backend


@pytest.mark.asyncio
async def test_the_tool_refuses_no_route_in_the_error_envelope(served):
    client, backend = served
    pump = await place_symbol(backend, "centrifugal_pump", 0, 0, tag="P-101")
    for k, (x, y) in enumerate(((0, 30), (-25, 10), (25, 10), (0, -20))):
        await place_symbol(backend, "horizontal_vessel", x, y, tag=f"TK-{k + 1}")
    far = await place_symbol(backend, "vertical_vessel", 200, 0, tag="V-900")
    result = await client.call_tool(
        "pid_line_draw",
        {
            "from_": {"handle": pump["handle"], "port": "discharge"},
            "to": {"handle": far["handle"], "port": "N3"},
            "route": "avoid",
        },
        raise_on_error=False,
    )
    assert result.is_error
    payload = result.structured_content
    assert payload["kind"] == "no_route" and payload["category"] == "geometry"
    assert {o["tag"] for o in payload["obstacles"]} <= {"TK-1", "TK-2", "TK-3", "TK-4"}
    other = await place_symbol(backend, "centrifugal_pump", 100, -60, tag="P-102")
    ok = await client.call_tool(
        "pid_line_draw",
        {
            "from_": {"handle": other["handle"], "port": "discharge"},
            "to": {"handle": far["handle"], "port": "N2"},
            "route": "avoid",
            "clearance": 1.0,
        },
    )
    assert ok.structured_content["obstacle_hits"] == []
