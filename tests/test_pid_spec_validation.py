"""B07 on ``pid_from_spec``: backlog rows W4-T17-07, -09, -10 and -11.

The dry run and the real run go through ``validate_spec``; each refusal here is
checked on both, and the real run must leave the drawing untouched.
"""

from __future__ import annotations

import copy

import pytest

import config
from engineering.pid.spec import EXAMPLE_SPEC, run_spec, validate_spec
from engineering.validation import SpecError

pytestmark = pytest.mark.asyncio


def _spec(mutate) -> dict:
    spec = copy.deepcopy(EXAMPLE_SPEC)
    mutate(spec)
    return spec


async def _refused_on_both_runs(backend, spec, pattern):
    before = await backend.entity_count()
    with pytest.raises(ValueError, match=pattern):
        await run_spec(backend, spec, dry_run=True)
    with pytest.raises(ValueError, match=pattern):
        await run_spec(backend, spec)
    assert await backend.entity_count() == before
    assert (await backend.system_status())["transaction_depth"] == 0
    assert backend.get_plan_spec() is None, "nothing - not even the plan - was written"


# ── W4-T17-07: what place_symbol refuses, the dry run refuses too ──────────


async def test_a_negative_scale_is_refused_by_the_dry_run_too(backend):
    """RED: the dry run approved scale=-1; only the real run refused it."""
    spec = _spec(lambda s: s["equipment"][0].update({"scale": -1}))
    await _refused_on_both_runs(
        backend, spec, r"^equipment\[0\]\.scale: must be greater than zero, got -1\.0\.$"
    )


async def test_a_fail_position_without_an_actuator_is_refused_by_the_dry_run(backend):
    spec = _spec(lambda s: s["valves"][0].update({"actuator": "none"}))
    await _refused_on_both_runs(backend, spec, r"^valves\[0\]\.fail: ")


async def test_an_unknown_fail_position_is_refused_by_the_dry_run(backend):
    spec = _spec(lambda s: s["valves"][0].update({"fail": "FX"}))
    await _refused_on_both_runs(backend, spec, r"^valves\[0\]\.fail: must be one of FO, FC, FL")


async def test_a_negative_stub_is_refused(backend):
    spec = _spec(lambda s: s["lines"][0].update({"stub": -2}))
    await _refused_on_both_runs(
        backend, spec, r"^lines\[0\]\.stub: must not be negative, got -2\.0\.$"
    )


# ── W4-T17-10: the sheet block ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "sheet, pattern",
    [
        ({"layer_set": "nonsense"}, r"^sheet\.layer_set: 'nonsense' is not a layer set"),
        ({"sheet_size": "Z9"}, r"^sheet\.sheet_size: 'Z9' is not a sheet size"),
        ({"bogus": 1}, r"^sheet: unknown key\(s\) bogus"),
        ({"scale": "abc"}, r"^sheet\.scale: expected a number, got 'abc'\.$"),
        ({"scale": 0}, r"^sheet\.scale: must be greater than zero"),
        ({"intent": 5}, r"^sheet\.intent: expected text, got int$"),
    ],
)
async def test_the_sheet_block_is_validated(backend, sheet, pattern):
    """RED: all of these were accepted; scale 'abc' died in float() mid-run."""
    spec = _spec(lambda s: s["sheet"].update(sheet))
    with pytest.raises(SpecError, match=pattern):
        validate_spec(spec)
    await _refused_on_both_runs(backend, spec, pattern)


# ── W4-T17-11: typed optional keys ──────────────────────────────────────────


@pytest.mark.parametrize(
    "mutate, pattern",
    [
        (lambda s: s["lines"][0].update({"arrow": "no"}), r"^lines\[0\]\.arrow: expected true"),
        (lambda s: s["lines"][0].update({"number": 123}), r"^lines\[0\]\.number: expected text"),
        (lambda s: s["lines"][0].update({"label": "yes"}), r"^lines\[0\]\.label: expected true"),
        (lambda s: s["lines"][0].update({"route": "zigzag"}), r"^lines\[0\]\.route: must be"),
        (lambda s: s["equipment"][0].update({"tag": 101}), r"^equipment\[0\]\.tag: expected text"),
        (
            lambda s: s["equipment"].append({"id": "P.1", "symbol": "fan", "x": 0, "y": 0}),
            r"^equipment\[2\]\.id: 'P\.1' contains '\.'",
        ),
    ],
)
async def test_optional_keys_are_typed(backend, mutate, pattern):
    """RED: arrow 'no' was truthy, number 123 was a bare TypeError, 'P.1' was accepted."""
    await _refused_on_both_runs(backend, _spec(mutate), pattern)


# ── W4-T17-09: an already open transaction ──────────────────────────────────


async def test_an_open_transaction_is_refused_before_the_plan_is_written(backend):
    """RED: ezdxf nested the transaction (COM refused it) after drawing_plan ran."""
    assert (await backend.transaction_begin())["ok"] is True
    before = await backend.entity_count()
    with pytest.raises(SpecError, match="a transaction is already open; nothing was drawn"):
        await run_spec(backend, copy.deepcopy(EXAMPLE_SPEC))
    assert backend.get_plan_spec() is None
    assert await backend.entity_count() == before
    assert (await backend.system_status())["transaction_depth"] == 1, "theirs, untouched"
    assert (await backend.transaction_rollback())["ok"] is True


# ── the item budget ─────────────────────────────────────────────────────────


async def test_a_section_over_the_item_budget_is_refused_naming_the_setting(backend, monkeypatch):
    monkeypatch.setattr(config.settings, "max_spec_items", 1)
    spec = copy.deepcopy(EXAMPLE_SPEC)  # two equipment items
    with pytest.raises(SpecError) as excinfo:
        validate_spec(spec)
    assert str(excinfo.value).startswith("equipment: 2 is over the limit of 1")
    assert "MAX_SPEC_ITEMS" in str(excinfo.value)
    await _refused_on_both_runs(backend, spec, "MAX_SPEC_ITEMS")


async def test_the_example_still_validates_and_draws(backend):
    result = await run_spec(backend, copy.deepcopy(EXAMPLE_SPEC))
    assert result["graph"]["stats"]["dangling"] == 0
