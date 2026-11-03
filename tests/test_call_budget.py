"""``benchmarks/call_budget.py`` - the least-call oracles and their fixtures (1.8-14).

The fixtures are what the plan names: the messy-layers drawing (six misnamed
layers, colour overrides, 300 entities at lineweight -3) and the plate (a closed
LWPOLYLINE, six circles, two arcs). The ceilings are ``token_budget.json``'s; a
``ci`` cell with no oracle, an arm that does not do its job, or an oracle over
its ceiling fails - a pending oracle is reported, never passed.
"""

from __future__ import annotations

import ezdxf
import pytest

from benchmarks import call_budget as cb


def test_the_messy_layers_fixture_is_the_plans(tmp_path):
    doc = ezdxf.readfile(cb.build_messy_layers(tmp_path / "messy.dxf"))
    msp = list(doc.modelspace())
    layers = {layer.dxf.name for layer in doc.layers}
    assert len(cb.MESSY_LAYERS) == 6
    assert set(cb.MESSY_LAYERS) <= layers
    assert set(cb.MESSY_LAYERS.values()) <= layers
    assert len(msp) == 300
    assert all(entity.dxf.lineweight == -3 for entity in msp)
    assert all(entity.dxf.color not in (0, 256) for entity in msp), "colour overrides"
    assert {entity.dxf.layer for entity in msp} == set(cb.MESSY_LAYERS)


def test_the_plate_fixture_is_the_plans(tmp_path):
    doc = ezdxf.readfile(cb.build_plate(tmp_path / "plate.dxf"))
    kinds: dict[str, int] = {}
    for entity in doc.modelspace():
        kinds[entity.dxftype()] = kinds.get(entity.dxftype(), 0) + 1
    assert kinds == {"LWPOLYLINE": 1, "CIRCLE": 6, "ARC": 2}
    outline = next(iter(doc.modelspace().query("LWPOLYLINE")))
    assert outline.closed


def _row(name: str, naive: str = "measured", batched: str = "measured", calls: int = 1):
    arm = {"arm": "naive", "status": naive, "detail": "x", "calls": 9}
    oracle = {"arm": "batched", "status": batched, "detail": "y", "calls": calls}
    if batched == "pending_tool":
        oracle["missing_tools"] = ["some_verb"]
    return {"scenario": name, "arms": {"naive": arm, "batched": oracle}}


BUDGET = {
    "task": {
        "a": {"ceiling": 1, "gate": "ci", "owner": "1.8-14"},
        "b": {"ceiling": 3, "gate": "release", "owner": "1.8-12"},
    }
}


def test_within_the_ceilings_and_pending_on_a_release_cell_passes():
    checked = cb.check_calls([_row("a"), _row("b", batched="pending_tool")], BUDGET)
    assert checked["violations"] == []
    assert [item["cell"] for item in checked["pending"]] == ["task.b"]


@pytest.mark.parametrize(
    ("rows", "cell"),
    [
        ([_row("a", calls=2), _row("b")], "task.a"),  # over a ci ceiling
        ([_row("a", batched="pending_tool"), _row("b")], "task.a"),  # a ci cell with no oracle
        ([_row("a", naive="failed"), _row("b")], "task.a.naive"),  # an arm that did not do it
        ([_row("a")], "task.b"),  # a cell nothing measured
    ],
)
def test_what_fails_the_call_budget(rows, cell):
    checked = cb.check_calls(rows, BUDGET)
    assert cell in {item["cell"] for item in checked["violations"]}


def test_release_turns_a_pending_cell_into_a_failure():
    checked = cb.check_calls([_row("a"), _row("b", batched="pending_tool")], BUDGET, release=True)
    assert [item["cell"] for item in checked["violations"]] == ["task.b"]


def test_the_harness_passes_its_own_budget_on_this_tree(monkeypatch, capsys):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    assert cb.main([]) == 0
    out = capsys.readouterr().out
    assert "sketch_20_entities" in out
    assert "PENDING task.layer_cleanup_300" in out
