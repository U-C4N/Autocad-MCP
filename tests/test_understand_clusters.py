"""Bodies of model-space content: two plan copies are two clusters."""

from __future__ import annotations

import pytest

from engineering.understand.clusters import find_clusters
from engineering.understand.snapshot import EntityRecord

EPS = 1e-9


def line(handle, a, b, layer="WALL"):
    return EntityRecord(handle=handle, type="LINE", layer=layer, space="Model", points=(a, b))


def text(handle, value, x, y, height=250.0):
    return EntityRecord(
        handle=handle,
        type="TEXT",
        layer="TEXT",
        space="Model",
        points=((x, y),),
        text=value,
        height=height,
    )


def plan(prefix, dx):
    """A 20 000 x 12 000 outline with a partition, a tag in each half and a
    title under it, shifted ``dx`` along x. The tags float 4 000 from every
    line - far more than one cell - so only the absorb step keeps them in."""
    return [
        line(f"{prefix}1", (dx, 0.0), (dx + 20000.0, 0.0)),
        line(f"{prefix}2", (dx + 20000.0, 0.0), (dx + 20000.0, 12000.0)),
        line(f"{prefix}3", (dx + 20000.0, 12000.0), (dx, 12000.0)),
        line(f"{prefix}4", (dx, 12000.0), (dx, 0.0)),
        line(f"{prefix}5", (dx + 10000.0, 0.0), (dx + 10000.0, 12000.0)),
        text(f"{prefix}T1", "T4100", dx + 4000.0, 6000.0),
        text(f"{prefix}T2", "T4200", dx + 14000.0, 6000.0),
        text(f"{prefix}T3", "PLANT LAYOUT", dx + 2000.0, 12500.0, height=500.0),
    ]


def test_two_plan_copies_side_by_side_are_two_clusters():
    # Copy A spans x 0..20 000, copy B 40 000..60 000: a 20 000 gap. The
    # content box is 60 000 x 12 500 (the title's point box ends at its
    # insertion, y 12 500), so a cell is 2 % (CELL_FRACTION) of the
    # hypot(60 000, 12 500) = 61 288 diagonal, about 1 226 units, and the gap
    # is over 16 empty cells.
    clusters, membership = find_clusters([*plan("A", 0.0), *plan("B", 40000.0)])
    assert len(clusters) == 2
    left, right = sorted(clusters, key=lambda c: c["box"][0])
    assert left["entities"] == right["entities"] == 8
    assert left["box"] == pytest.approx([0.0, 0.0, 20000.0, 12500.0], abs=EPS)
    assert right["box"] == pytest.approx([40000.0, 0.0, 60000.0, 12500.0], abs=EPS)
    assert membership["AT1"] == membership["A1"] == left["id"]
    assert membership["BT2"] == membership["B5"] == right["id"]
    # The tallest text leads the labels.
    assert left["labels"][0] == "PLANT LAYOUT"
    assert left["layers"][0] == "WALL"


def test_ids_run_largest_first_then_left_to_right():
    small = [line("S1", (100000.0, 0.0), (100500.0, 0.0))]
    clusters, _ = find_clusters([*small, *plan("A", 40000.0), *plan("B", 0.0)])
    assert [c["id"] for c in clusters] == ["C1", "C2", "C3"]
    assert [c["box"][0] for c in clusters] == pytest.approx([0.0, 40000.0, 100000.0], abs=EPS)
    assert [c["entities"] for c in clusters] == [8, 8, 1]


def test_excluded_handles_take_no_part():
    far = line("FAR", (-5_000_000_000.0, 0.0), (-4_999_999_000.0, 0.0))
    clusters, membership = find_clusters([*plan("A", 0.0), far], exclude={"FAR"})
    assert len(clusters) == 1
    assert "FAR" not in membership
    assert clusters[0]["box"] == pytest.approx([0.0, 0.0, 20000.0, 12500.0], abs=EPS)


def test_nothing_to_cluster_is_an_empty_answer():
    assert find_clusters([]) == ([], {})


def test_a_non_positive_cell_is_refused():
    with pytest.raises(ValueError, match="cell: must be greater than zero"):
        find_clusters(plan("A", 0.0), cell=-1.0)
