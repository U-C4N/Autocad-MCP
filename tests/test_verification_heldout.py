"""B29: the binder on a held-out set from a different generator family.

The report gives eligible, attempted, accepted and abstained counts, precision
and coverage with Wilson 95 % intervals; the target is >= 98 % precision and
>= 80 % coverage, and abstaining on everything does not meet it.
"""

from __future__ import annotations

import inspect

import pytest

from engineering.understand.snapshot import records_from_doc
from engineering.verification.binding import bind
from engineering.verification.observe import observe
from engineering.verification.types import Binding, RequirementSet
from tests.fixtures import heldout_dims
from tests.fixtures.heldout_dims import TARGET_COVERAGE, TARGET_PRECISION, build_heldout, score


@pytest.fixture(scope="module")
def heldout():
    doc, cases = build_heldout()
    observed = observe(records_from_doc(doc, source="heldout", block_defs=True))
    bindings = bind(
        RequirementSet(requirements=tuple(c.requirement for c in cases), units="mm"), observed
    )
    return doc, cases, bindings


def test_the_held_out_family_is_not_the_development_generator():
    source = inspect.getsource(heldout_dims)
    assert "engineering.mech" not in source
    assert "draw_part" not in source and "draw_prims" not in source


def test_the_generator_s_truth_is_where_ezdxf_puts_the_hole(heldout):
    doc, cases, _bindings = heldout
    checked = 0
    for case in cases:
        if not case.case_id.endswith("-region") or not case.truth[0][0]:
            continue
        ((path, handle),) = case.truth
        insert = doc.entitydb[path[0]]
        circle = doc.entitydb[handle]
        expected = insert.matrix44().transform(circle.dxf.center)
        region = dict(case.requirement.features[0].value)["region"]
        centre = ((region[0] + region[2]) / 2.0, (region[1] + region[3]) / 2.0)
        assert centre == pytest.approx((expected.x, expected.y), abs=1e-9)
        checked += 1
    assert checked > 50


def test_the_binder_meets_the_b29_targets(heldout):
    _doc, cases, bindings = heldout
    report = score(cases, bindings)
    assert report["eligible"] == len(cases) == report["attempted"] + report["abstained"]
    assert report["must_abstain"] > 0
    # Review Focus 7: every case that names no single feature is abstained on
    assert report["abstained_correctly"] == report["must_abstain"]
    assert report["precision"]["value"] >= TARGET_PRECISION
    assert report["coverage"]["value"] >= TARGET_COVERAGE
    for key in ("precision", "coverage"):
        assert report[key]["low"] <= report[key]["value"] <= report[key]["high"]
    assert report["meets_target"] is True


def test_abstaining_on_everything_does_not_meet_the_target(heldout):
    _doc, cases, _bindings = heldout
    silent = tuple(
        Binding(
            requirement_id=c.case_id, status="unknown", refs=(), candidates=(), reason="abstain"
        )
        for c in cases
    )
    report = score(cases, silent)
    assert report["attempted"] == 0 and report["coverage"]["value"] == 0.0
    assert report["meets_target"] is False


def test_a_binder_that_picks_the_first_candidate_is_caught(heldout):
    _doc, cases, bindings = heldout
    picked = tuple(
        Binding(
            requirement_id=b.requirement_id,
            status="bound",
            refs=b.candidates[:1],
            candidates=b.candidates,
            reason="first pick",
        )
        if b.status == "ambiguous"
        else b
        for b in bindings
    )
    report = score(cases, picked)
    assert report["accepted"] < report["attempted"]
