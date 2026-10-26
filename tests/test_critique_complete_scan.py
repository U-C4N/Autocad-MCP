"""Review Focus 2: the offending entity is found wherever it sits.

The four base checks read ``entity_list(limit=5000)`` of the current space and
the validator 10,000, so an offender at position 5,001, 10,001 or last, or on a
paper layout, read as "zero issues" - and a check that crashed read as clean.
Every drawing here is built straight into the ezdxf document (a 10,002-entity
drawing through the tools would take minutes) and every expected handle is
the one the test placed. The filler lines are 10 mm apart and 100 mm long, so
they make no corner, duplicate or overlap of their own.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time

import pytest

import config
from backends.quarantine import DocumentQuarantineError
from engineering import qa_scan
from engineering.critique import (
    _FOCUS_DISPATCH,
    CHECK_FAILED,
    SCAN_INCOMPLETE,
    _close_pairs,
    run_critique,
)
from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.validator import DrawingValidator

pytestmark = pytest.mark.asyncio

#: 10,002 entities, so position 10,001 and the last one are different entities.
TOTAL = 10_002
POSITIONS = (1, 4_999, 5_000, 5_001, 10_001, TOTAL)
BASE = ("construction_left", "duplicate_entities", "dim_overlap", "untrimmed_corner")


def _filler(layout, index: int):
    y = 10.0 * index
    return layout.add_line((0.0, y), (100.0, y), dxfattribs={"layer": "GEOMETRY"})


def _dimension(layout):
    """A rendered linear dimension far from the filler; two of them coincide."""
    dim = layout.add_linear_dim(base=(-500.0, 10.0), p1=(-500.0, 0.0), p2=(-480.0, 0.0))
    dim.render()
    return dim.dimension


def _offender(layout, kind: str):
    if kind == "construction_left":
        return layout.add_line((-300.0, 0.0), (-200.0, 0.0), dxfattribs={"layer": "CONSTRUCTION"})
    if kind == "duplicate_entities":
        return layout.add_line((-300.0, 50.0), (-200.0, 50.0), dxfattribs={"layer": "GEOMETRY"})
    if kind == "dim_overlap":
        return _dimension(layout)
    if kind == "hidden":
        line = layout.add_line((-300.0, 90.0), (-200.0, 90.0), dxfattribs={"layer": "GEOMETRY"})
        line.dxf.invisible = 1
        return line
    raise ValueError(kind)


def _partner(layout, kind: str):
    """The second entity of a pair finding, or None for a single-entity one."""
    if kind == "duplicate_entities":
        # the same segment drawn the other way round
        return layout.add_line((-200.0, 50.0), (-300.0, 50.0), dxfattribs={"layer": "GEOMETRY"})
    if kind == "dim_overlap":
        return _dimension(layout)
    return None


def _build(backend, kind: str, position: int):
    """``TOTAL`` model-space entities with the offender at ``position`` (1-based).

    A pair's partner is the last entity, or the first when the offender is the
    last; the rest is filler."""
    msp = backend._doc.modelspace()
    needs_partner = kind in ("duplicate_entities", "dim_overlap")
    partner_at = (TOTAL if position != TOTAL else 1) if needs_partner else None
    offender = partner = None
    for slot in range(1, TOTAL + 1):
        if slot == position:
            offender = _offender(msp, kind)
        elif slot == partner_at:
            partner = _partner(msp, kind)
        else:
            _filler(msp, slot)
    assert len(msp) == TOTAL
    return offender.dxf.handle, (partner.dxf.handle if partner is not None else None)


async def _no_screenshot(*_args, **_kwargs):
    """Step 7 renders the drawing with matplotlib (8 s at 10,002 lines) and is
    not what these tests are about."""
    return b"\x89PNG"


async def _issues_of(backend, focus):
    return [i for i in await run_critique(backend, [focus]) if i.focus == focus]


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("kind", ("construction_left", "duplicate_entities", "dim_overlap"))
async def test_the_offender_is_found_at_every_position(backend, kind, position):
    offender, partner = _build(backend, kind, position)
    issues = await _issues_of(backend, kind)
    assert len(issues) == 1, [i.message for i in issues]
    (issue,) = issues
    if partner is None:
        assert issue.handles == [offender]
        assert issue.detail["count"] == 1 and issue.detail["spaces"] == ["Model"]
    else:
        assert sorted(issue.handles) == sorted([offender, partner])
        assert issue.detail["space"] == "Model"


@pytest.mark.parametrize("position", POSITIONS)
async def test_a_hidden_entity_is_found_at_every_position(backend, position, monkeypatch):
    monkeypatch.setattr(backend, "view_screenshot", _no_screenshot)
    offender, _ = _build(backend, "hidden", position)
    result = await DrawingValidator().run(backend)
    (finding,) = [f for f in result.findings if f.code == "hidden_orphans"]
    assert finding.detail["sample_handles"] == [offender]
    assert finding.detail["spaces"] == ["Model"]
    assert result.ok is False


@pytest.mark.parametrize("kind", ("construction_left", "duplicate_entities", "dim_overlap"))
async def test_the_offender_is_found_on_a_paper_layout(backend, kind):
    """The model is filler; the finding is on Layout1, which entity_list never read."""
    for index in range(1, 6):
        _filler(backend._doc.modelspace(), index)
    paper = backend._doc.layouts.get("Layout1")
    offender = _offender(paper, kind).dxf.handle
    partner = _partner(paper, kind)
    (issue,) = await _issues_of(backend, kind)
    expected = [offender] if partner is None else [offender, partner.dxf.handle]
    assert sorted(issue.handles) == sorted(expected)
    spaces = issue.detail.get("spaces") or [issue.detail["space"]]
    assert spaces == ["Layout1"]


async def test_a_hidden_entity_on_a_paper_layout_is_found(backend, monkeypatch):
    monkeypatch.setattr(backend, "view_screenshot", _no_screenshot)
    offender = _offender(backend._doc.layouts.get("Layout1"), "hidden").dxf.handle
    result = await DrawingValidator().run(backend)
    (finding,) = [f for f in result.findings if f.code == "hidden_orphans"]
    assert finding.detail == {"sample_handles": [offender], "spaces": ["Layout1"]}


async def test_fake_dimension_text_on_a_paper_layout_is_found(backend):
    paper = backend._doc.layouts.get("Layout1")
    text = paper.add_text("Ø78", dxfattribs={"layer": "TEXT", "height": 3.5})
    result = await DrawingValidator().run(backend)
    (finding,) = [f for f in result.findings if f.code == "fake_dimension_text"]
    assert finding.detail["sample_handles"] == [text.dxf.handle]


# -- pairs are compared only within one space -------------------------------------------


async def test_a_pair_split_across_spaces_is_not_a_finding(backend):
    """Every layout starts at the origin: the same coordinates on two sheets are
    two places, not one."""
    msp = backend._doc.modelspace()
    paper = backend._doc.layouts.get("Layout1")
    msp.add_line((0.0, 0.0), (100.0, 0.0), dxfattribs={"layer": "GEOMETRY"})
    paper.add_line((0.0, 0.0), (100.0, 0.0), dxfattribs={"layer": "GEOMETRY"})  # same segment
    msp.add_line((200.0, 0.0), (300.0, 0.0), dxfattribs={"layer": "GEOMETRY"})
    paper.add_line((300.1875, 0.25), (300.1875, 50.0), dxfattribs={"layer": "GEOMETRY"})  # near end
    _dimension(msp)
    _dimension(paper)  # the same text point, other sheet
    assert (
        await run_critique(backend, ["duplicate_entities", "dim_overlap", "untrimmed_corner"]) == []
    )


async def test_an_untrimmed_corner_at_position_5001_and_on_paper(backend):
    """The gap is a 3-4-5 triangle scaled by 1/16: (0.1875, 0.25) -> 0.3125 mm,
    every number exact in binary."""
    msp = backend._doc.modelspace()
    for index in range(1, 5_001):
        _filler(msp, index)
    late = msp.add_line((-100.0, 0.0), (-50.0, 0.0), dxfattribs={"layer": "GEOMETRY"})
    over = msp.add_line((-49.8125, 0.25), (-49.8125, 40.0), dxfattribs={"layer": "GEOMETRY"})
    assert late.dxf.handle == list(msp)[5_000].dxf.handle  # position 5,001
    paper = backend._doc.layouts.get("Layout1")
    p1 = paper.add_line((0.0, 0.0), (10.0, 0.0))
    p2 = paper.add_line((10.1875, 0.25), (10.1875, 20.0))

    issues = await _issues_of(backend, "untrimmed_corner")

    assert [(sorted(i.handles), i.detail["space"]) for i in issues] == [
        (sorted([late.dxf.handle, over.dxf.handle]), "Model"),
        (sorted([p1.dxf.handle, p2.dxf.handle]), "Layout1"),
    ]
    for issue in issues:
        assert abs(issue.detail["gap_mm"] - 0.3125) <= 1e-9


# -- the grid hash returns exactly the all-pairs answer -----------------------------------


def _all_pairs(points, tol):
    """The oracle: the O(n^2) loop the checks used to run."""
    out = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if points[i][0] != points[j][0]:
                continue
            d = math.hypot(points[i][1] - points[j][1], points[i][2] - points[j][2])
            if d <= tol:
                out.append((i, j, d))
    return out


async def test_the_grid_hash_equals_the_all_pairs_loop():
    points = []
    # A lattice at half the tolerance, so pairs sit exactly on cell edges and
    # exactly at the tolerance; a second space shares every coordinate.
    for space in ("Model", "Layout1"):
        for ix in range(12):
            for iy in range(12):
                points.append((space, ix * 0.25, iy * 0.25))
    points.append(("Model", 0.5, 0.0))  # exactly 0.5 from (0, 0)
    points.append(("Model", 1e6 + 0.5, -1e6))  # far from the origin
    points.append(("Model", 1e6, -1e6))
    for tol in (0.5, 5.0, 0.25):
        assert _close_pairs(points, tol) == _all_pairs(points, tol), tol


# -- failures are findings, never silence ----------------------------------------------


async def test_a_check_that_raises_is_reported_as_check_failed(backend, monkeypatch):
    async def boom(_backend, _shared):
        raise RuntimeError("index exploded")

    boom.needs_shared = True
    monkeypatch.setitem(_FOCUS_DISPATCH, "duplicate_entities", boom)
    msp = backend._doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "CONSTRUCTION"})

    issues = await run_critique(backend, ["duplicate_entities", "construction_left"])

    failed, construction = issues
    assert failed.focus == "duplicate_entities" and failed.severity == "error"
    assert failed.detail == {"code": "check_failed", "error": "RuntimeError: index exploded"}
    assert construction.focus == "construction_left"  # the run went on


async def test_the_two_codes_are_not_focuses():
    assert CHECK_FAILED == "check_failed" and SCAN_INCOMPLETE == "scan_incomplete"
    assert CHECK_FAILED not in ALL_CRITIQUE_FOCUSES
    assert SCAN_INCOMPLETE not in ALL_CRITIQUE_FOCUSES


async def test_a_partial_read_is_scan_incomplete_for_every_base_check(backend, monkeypatch):
    msp = backend._doc.modelspace()
    for index in range(1, 5):
        _filler(msp, index)
    monkeypatch.setattr(config.settings, "max_qa_entities", 3)

    issues = await run_critique(backend, list(BASE))

    assert [i.focus for i in issues] == list(BASE)
    for issue in issues:
        assert issue.severity == "error"
        assert issue.detail["code"] == "scan_incomplete"
        assert issue.detail["complete"] is False
        assert "MAX_QA_ENTITIES=3" in issue.detail["reason"]


async def test_the_validator_reports_a_partial_read(backend, monkeypatch):
    for index in range(1, 5):
        _filler(backend._doc.modelspace(), index)
    monkeypatch.setattr(config.settings, "max_qa_entities", 3)
    result = await DrawingValidator().run(backend)
    (finding,) = [f for f in result.findings if f.code == "scan_incomplete"]
    assert finding.severity == "error" and result.ok is False
    assert finding.detail["records"] == 0


async def test_a_failed_validator_step_is_a_finding(backend, monkeypatch):
    async def broken_info():
        raise RuntimeError("info unavailable")

    monkeypatch.setattr(backend, "drawing_info", broken_info)
    result = await DrawingValidator().run(backend)
    codes = {f.code: f.message for f in result.findings}
    assert codes["save_check_failed"] == "info unavailable"
    assert codes["path_check_failed"] == "info unavailable"  # step 8 used to pass silently


async def _quarantine(backend, monkeypatch):
    """Abandon a real write, the way a real timeout does."""
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.2)
    entered = threading.Event()
    release = threading.Event()

    def _writer():
        entered.set()
        release.wait(5.0)
        return "too late"

    with pytest.raises(RuntimeError):
        await backend._async(_writer)
    assert entered.is_set()
    assert backend._quarantine is not None
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 10)
    return release


async def test_the_validator_does_not_swallow_a_quarantine(backend, monkeypatch):
    """Every step caught Exception, and DocumentQuarantineError is one: the
    validator reported a quarantined document as a list of warnings."""
    release = await _quarantine(backend, monkeypatch)
    try:
        with pytest.raises(DocumentQuarantineError):
            await DrawingValidator().run(backend)
    finally:
        release.set()


async def test_the_validator_and_the_critique_share_one_read(backend, monkeypatch):
    calls = []
    real = qa_scan._read_all_spaces

    async def counting(b):
        calls.append(1)
        return await real(b)

    monkeypatch.setattr(qa_scan, "_read_all_spaces", counting)
    shared: dict = {}
    await DrawingValidator().run(backend, shared=shared)
    await run_critique(backend, None, shared=shared)
    assert len(calls) == 1


class _FakeCtx:
    """drawing_finalize needs only lifespan_context and warning."""

    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def warning(self, message):
        pass


async def _clean(backend):
    from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes

    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    await backend.entity_create_line(0, 0, 40, 0, layer="GEOMETRY")


async def test_drawing_finalize_reads_the_drawing_once(backend, tmp_path, monkeypatch):
    import server

    await _clean(backend)
    calls = []
    real = qa_scan._read_all_spaces

    async def counting(b):
        calls.append(1)
        return await real(b)

    monkeypatch.setattr(qa_scan, "_read_all_spaces", counting)
    payload = await server.drawing_finalize(
        save_path=str(tmp_path / "part.dxf"), ctx=_FakeCtx(backend)
    )
    assert payload["ok"] is True
    assert len(calls) == 1


async def test_drawing_deliver_reads_the_drawing_once(backend, tmp_path, monkeypatch):
    from engineering.delivery import deliver_drawing

    await _clean(backend)
    calls = []
    real = qa_scan._read_all_spaces

    async def counting(b):
        calls.append(1)
        return await real(b)

    monkeypatch.setattr(qa_scan, "_read_all_spaces", counting)
    result = await deliver_drawing(backend, tmp_path, formats=["dxf"], min_score=0)
    assert result.status == "success"
    assert len(calls) == 1


# -- a big drawing does not stall the server -----------------------------------------------

#: The stated bound for a 50,000-line focus=None critique. Measured at 9.3 s on
#: the maintainer's machine (2026-10-24, inside the suite); the O(n^2) pair loops
#: alone took 8.9 s at 5,000 lines. Sixty seconds is the regression line.
CRITIQUE_BOUND_S = 60.0
#: How long a concurrent system_status may take to answer. The defect this
#: guards was an event loop stalled for the whole critique (8.9 s at 5,000
#: lines); an answering loop measured 0.52-0.59 s worst under a loaded machine
#: (the full suite beside perf and A/B runs), so 0.5 s flaked without a stall.
STATUS_BOUND_S = 2.0


async def test_a_50000_line_critique_leaves_system_status_answering(backend):
    msp = backend._doc.modelspace()
    for index in range(1, 50_000):
        _filler(msp, index)
    last = msp.add_line((-300.0, 0.0), (-200.0, 0.0), dxfattribs={"layer": "CONSTRUCTION"})
    worst = 0.0
    answers = 0

    async def poll():
        nonlocal worst, answers
        while True:
            started = time.perf_counter()
            await asyncio.sleep(0.02)
            status = await backend.system_status()
            worst = max(worst, time.perf_counter() - started - 0.02)
            answers += 1
            assert status["backend"] == "ezdxf"

    poller = asyncio.create_task(poll())
    started = time.perf_counter()
    try:
        issues = await run_critique(backend, None)
    finally:
        poller.cancel()
    elapsed = time.perf_counter() - started

    (construction,) = [i for i in issues if i.focus == "construction_left"]
    assert construction.handles == [last.dxf.handle]  # the 50,000th entity
    assert not [i for i in issues if i.detail.get("code") in ("check_failed", "scan_incomplete")]
    assert elapsed < CRITIQUE_BOUND_S, elapsed
    assert answers > 0
    assert worst < STATUS_BOUND_S, worst


# -- matrix v7's zero-issue tasks stay at zero now that paper space is read -------------------


@pytest.mark.parametrize("task_id", ("mech_assembly", "arch_roundtrip", "pid_roundtrip"))
async def test_the_v7_zero_issue_tasks_stay_at_zero(task_id, tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        await adapter._reset()
        passed, metrics, _artifacts = await getattr(adapter, f"_task_{task_id}")()
        everything = await run_critique(adapter.backend, None)
    finally:
        await adapter.cleanup()
    assert passed, metrics
    assert metrics["critique_issues"] == 0
    assert everything == [], [(i.focus, i.message) for i in everything]


# -- a domain index that cannot read is check_failed, not an empty index -------------------


async def test_a_mechanical_index_that_cannot_read_fails_every_mech_focus(backend, monkeypatch):
    from engineering.mech.critique import MECH_FOCUSES

    calls = []

    async def broken_list(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("entity_list down")

    monkeypatch.setattr(backend, "entity_list", broken_list)
    issues = await run_critique(backend, list(MECH_FOCUSES))

    assert [i.focus for i in issues] == list(MECH_FOCUSES)
    for issue in issues:
        assert issue.detail == {"code": "check_failed", "error": "RuntimeError: entity_list down"}
    assert len(calls) == 1  # the failed build is cached for the run, not retried six times


async def test_an_architectural_index_that_cannot_read_fails_every_arch_focus(backend, monkeypatch):
    from engineering.arch.critique import ARCH_FOCUSES

    async def broken_list(*_args, **_kwargs):
        raise RuntimeError("entity_list down")

    monkeypatch.setattr(backend, "entity_list", broken_list)
    issues = await run_critique(backend, list(ARCH_FOCUSES))

    assert [i.focus for i in issues] == list(ARCH_FOCUSES)
    assert {i.detail["code"] for i in issues} == {"check_failed"}


async def test_a_pid_attribute_read_that_fails_is_not_an_untagged_symbol(backend, monkeypatch):
    from engineering.pid.critique import PID_FOCUSES
    from engineering.pid.insert import place_symbol

    await place_symbol(backend, "instrument", 0, 0, tag="FIC-101", type="dcs", location="primary")

    async def broken_attributes(_handle):
        raise RuntimeError("attributes unreadable")

    monkeypatch.setattr(backend, "block_get_attributes", broken_attributes)
    issues = await run_critique(backend, list(PID_FOCUSES))

    assert [i.focus for i in issues] == list(PID_FOCUSES)
    for issue in issues:
        assert issue.detail == {
            "code": "check_failed",
            "error": "RuntimeError: attributes unreadable",
        }


async def test_a_drawing_without_any_insert_never_builds_the_pid_graph(backend, monkeypatch):
    import engineering.pid.critique as pid_critique
    from engineering.pid.critique import PID_FOCUSES

    async def never(*_args, **_kwargs):
        raise AssertionError("the P&ID graph was built for a drawing with no INSERT")

    monkeypatch.setattr(pid_critique, "build_graph", never)
    backend._doc.modelspace().add_line((0, 0), (10, 0))
    assert await run_critique(backend, list(PID_FOCUSES)) == []


async def test_a_layer_table_that_cannot_be_read_fails_its_checks(backend, monkeypatch):
    from engineering.understand.critique import TOPO_FOCUSES

    async def broken_layers():
        raise RuntimeError("layer table unreadable")

    monkeypatch.setattr(backend, "layer_list", broken_layers)
    foci = ["iso128", "layer_color", *TOPO_FOCUSES]
    issues = await run_critique(backend, foci)

    assert [i.focus for i in issues] == foci
    for issue in issues:
        assert issue.severity == "error"
        assert issue.detail == {
            "code": "check_failed",
            "error": "RuntimeError: layer table unreadable",
        }
