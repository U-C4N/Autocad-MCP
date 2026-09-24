"""The three topo_* focuses: a positive and a negative fixture each, and silence
on every drawing this server makes.

The network layer is the synthetic plant's product-piping layer, read from the
fixture's truth dict (``tests/fixtures/plant_pair.py``), so the test draws on a
layer the vocabulary classifies the way a foreign drawing's layer would be
classified.
"""

from __future__ import annotations

import copy

import pytest

from engineering.arch.layers import ARCH_ROLE_LAYER
from engineering.critique import run_critique
from engineering.pid.lines import PID_LINE_LAYERS
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.understand.critique import (
    SEMANTIC_LAYERS,
    TOPO_DISPATCH,
    TOPO_FOCUSES,
    build_index,
    focus_layers,
    issues_for,
)
from engineering.understand.snapshot import EntityRecord, Snapshot
from engineering.understand.vocab import classify_layer
from tests.fixtures.plant_pair import build_plant_pair

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def pipe_layer(tmp_path_factory) -> str:
    truth = build_plant_pair(tmp_path_factory.mktemp("plant"))
    layer = next(
        name for name, service in sorted(truth["layer_services"].items()) if service == "product"
    )
    meaning = classify_layer(layer)
    assert meaning["discipline"] == "piping" and meaning["confidence"] >= 0.9, meaning
    return layer


async def _lines(backend, layer, *segments):
    handles = []
    for x1, y1, x2, y2 in segments:
        entity = await backend.entity_create_line(x1, y1, x2, y2, layer=layer)
        handles.append(entity.handle)
    return handles


async def _issues(backend, focus):
    return issues_for(focus, await build_index(backend))


# -- the closed enum and the exclusions ------------------------------------------------


async def test_the_three_focuses_are_the_spec_ones_and_are_in_the_closed_enum():
    assert TOPO_FOCUSES == ("topo_dangling_endpoint", "topo_near_miss", "topo_interior_crossing")
    for focus in TOPO_FOCUSES:
        assert focus in ALL_CRITIQUE_FOCUSES
        assert TOPO_DISPATCH[focus].needs_shared is True


async def test_pid_line_layers_and_arch_layers_are_excluded():
    assert set(PID_LINE_LAYERS) <= SEMANTIC_LAYERS
    assert ARCH_ROLE_LAYER["wall"] in SEMANTIC_LAYERS
    assert "0" not in SEMANTIC_LAYERS
    snap = Snapshot(
        source="test",
        insunits=4,
        extmin=None,
        extmax=None,
        layers={"PROCESS-PIPING-MAIN": {}, "A-WALL-E-N": {}},
        layouts=(),
        records=(
            EntityRecord("1", "LINE", "PROCESS-PIPING-MAIN", "Model", ((0, 0), (10, 0))),
            EntityRecord("2", "LINE", "A-WALL-E-N", "Model", ((0, 5), (10, 5))),
        ),
    )
    assert focus_layers(snap) == []


# -- topo_dangling_endpoint ------------------------------------------------------------


async def test_dangling_fires_on_a_lone_pipe(backend, pipe_layer):
    (handle,) = await _lines(backend, pipe_layer, (0, 0, 1000, 0))
    issues = await _issues(backend, "topo_dangling_endpoint")
    assert [(i.severity, i.handles, i.detail["end"]) for i in issues] == [
        ("warning", [handle], "end"),
        ("warning", [handle], "start"),
    ]


async def test_dangling_is_quiet_on_a_closed_loop(backend, pipe_layer):
    await _lines(
        backend,
        pipe_layer,
        (0, 0, 1000, 0),
        (1000, 0, 1000, 1000),
        (1000, 1000, 0, 1000),
        (0, 1000, 0, 0),
    )
    assert await _issues(backend, "topo_dangling_endpoint") == []


# -- topo_near_miss --------------------------------------------------------------------


async def test_near_miss_fires_on_a_3_unit_gap(backend, pipe_layer):
    # box 0..2003 wide: default gap 0.002 x 2003 = 4.006 > 3
    first, second = await _lines(backend, pipe_layer, (0, 0, 1000, 0), (1003, 0, 2003, 0))
    (issue,) = await _issues(backend, "topo_near_miss")
    assert issue.severity == "error"
    assert issue.handles == sorted([first, second])
    assert issue.detail["kind"] == "end_end"
    assert issue.detail["gap"] == pytest.approx(3.0)
    assert "stops 3 short of the end of" in issue.message


async def test_near_miss_is_quiet_when_the_lines_join(backend, pipe_layer):
    await _lines(backend, pipe_layer, (0, 0, 1000, 0), (1000, 0, 2000, 0))
    assert await _issues(backend, "topo_near_miss") == []


# -- topo_interior_crossing ------------------------------------------------------------


async def test_crossing_fires_on_two_pipes_crossing_mid_span(backend, pipe_layer):
    await _lines(backend, pipe_layer, (0, 0, 1000, 0), (500, -500, 500, 500))
    (issue,) = await _issues(backend, "topo_interior_crossing")
    assert issue.severity == "info"
    assert issue.detail["at"] == [500.0, 0.0]


async def test_crossing_is_quiet_on_a_t_junction(backend, pipe_layer):
    await _lines(backend, pipe_layer, (0, 0, 1000, 0), (500, 0, 500, 500))
    assert await _issues(backend, "topo_interior_crossing") == []


# -- silence on this server's own drawings ---------------------------------------------


async def test_a_pid_from_spec_sheet_gets_no_topo_issue(backend):
    from engineering.pid.spec import EXAMPLE_SPEC, run_spec

    await run_spec(backend, copy.deepcopy(EXAMPLE_SPEC))
    assert await run_critique(backend, list(TOPO_FOCUSES)) == []
    everything = await run_critique(backend, None)
    assert not [i for i in everything if i.focus.startswith("topo_")]


async def test_an_arch_plan_from_spec_gets_no_topo_issue(backend):
    from engineering.arch.spec import EXAMPLE_SPEC, draw_plan_from_spec

    await draw_plan_from_spec(backend, copy.deepcopy(EXAMPLE_SPEC))
    assert await run_critique(backend, list(TOPO_FOCUSES)) == []


async def test_a_drawing_without_a_network_layer_is_never_snapshotted(backend, monkeypatch):
    import engineering.understand.snapshot as snapshot

    async def refuse(*_args, **_kwargs):
        raise AssertionError("the snapshot was taken")

    monkeypatch.setattr(snapshot, "take_snapshot", refuse)
    await backend.entity_create_line(0, 0, 100, 0, layer="GEOMETRY")
    assert await build_index(backend) == {"layers": [], "findings": None, "error": None}


async def test_a_snapshot_that_fails_is_reported_not_passed(backend, pipe_layer, monkeypatch):
    import engineering.understand.snapshot as snapshot

    async def broken(*_args, **_kwargs):
        raise RuntimeError("export failed")

    monkeypatch.setattr(snapshot, "take_snapshot", broken)
    await _lines(backend, pipe_layer, (0, 0, 1000, 0))
    (issue,) = await _issues(backend, "topo_near_miss")
    assert issue.severity == "info" and "export failed" in issue.message


# -- the dispatch ----------------------------------------------------------------------


async def test_the_three_focuses_share_one_index_per_run(backend, pipe_layer):
    await _lines(backend, pipe_layer, (0, 0, 1000, 0))
    shared: dict = {}
    for focus in TOPO_FOCUSES:
        await TOPO_DISPATCH[focus](backend, shared)
    assert list(shared) == ["topo_index"]


async def test_run_critique_reaches_the_topo_focuses(backend, pipe_layer):
    await _lines(backend, pipe_layer, (0, 0, 1000, 0), (1003, 0, 2003, 0))
    (issue,) = await run_critique(backend, ["topo_near_miss"])
    assert issue.focus == "topo_near_miss" and issue.severity == "error"
