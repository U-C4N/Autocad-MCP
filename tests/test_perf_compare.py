"""``benchmarks/perf_compare.py`` — the 1.6.0-vs-head performance gate (spec §8.3).

The arithmetic is checked against hand-computed numbers; one real measurement
runs the cheapest suite workload and the server-level workload in a subprocess
of this tree, the way the comparison runs both trees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks import perf_compare as pc

REPO = Path(__file__).resolve().parents[1]


def test_paired_ratio_is_the_median_of_per_round_ratios():
    # rounds: 110/100 = 1.10, 96/120 = 0.80, 105/100 = 1.05 -> median 1.05
    assert pc.paired_ratio([110.0, 96.0, 105.0], [100.0, 120.0, 100.0]) == pytest.approx(
        1.05, abs=1e-9
    )


def test_paired_ratio_refuses_unpaired_samples():
    with pytest.raises(ValueError, match="same length"):
        pc.paired_ratio([1.0, 2.0], [1.0])
    with pytest.raises(ValueError, match="same length"):
        pc.paired_ratio([], [])


def test_the_bound_never_drops_below_five_percent():
    assert pc.bound_for(0.0) == pytest.approx(1.05, abs=1e-9)
    assert pc.bound_for(0.02) == pytest.approx(1.05, abs=1e-9)  # 2 x 0.02 = 0.04 < 0.05
    assert pc.bound_for(0.04) == pytest.approx(1.08, abs=1e-9)  # 2 x 0.04 = 0.08


def test_summarize_passes_and_fails_against_the_calibrated_bound():
    base = {"w": [100.0, 100.0, 100.0]}
    base_again = {"w": [104.0, 104.0, 104.0]}  # A/A spread 0.04 -> bound 1.08
    head_ok = {"w": [107.0, 107.0, 107.0]}  # 1.07 <= 1.08
    head_slow = {"w": [109.0, 109.0, 109.0]}  # 1.09 >  1.08
    kwargs = {"base_ref": "v1.6.0", "head_ref": "abc1234", "warmup": 5, "rounds": 3}

    ok = pc.summarize(base, head_ok, base_again, **kwargs)
    assert ok["aa_spread"] == pytest.approx(0.04, abs=1e-9)
    assert ok["bound"] == pytest.approx(1.08, abs=1e-9)
    assert ok["workloads"]["w"] == {"base_ms": 100.0, "head_ms": 107.0, "ratio": 1.07}
    assert ok["ok"] is True
    assert ok["aa"] is True
    assert pc.summarize(base, head_slow, base_again, **kwargs)["ok"] is False


def test_without_aa_the_bound_is_five_percent():
    report = pc.summarize(
        {"w": [100.0]}, {"w": [104.0]}, None, base_ref="b", head_ref="h", warmup=0, rounds=1
    )
    assert report["aa"] is False
    assert report["aa_spread"] == 0.0
    assert report["bound"] == pytest.approx(1.05, abs=1e-9)
    assert report["ok"] is True
    assert set(report) >= {
        "schema_version",
        "base",
        "head",
        "warmup",
        "rounds",
        "workloads",
        "aa_spread",
        "bound",
        "ok",
    }


def test_the_parser_defaults_are_the_gate_s():
    args = pc.build_parser().parse_args([])
    assert (args.base, args.warmup, args.rounds, args.aa) == ("v1.6.0", 5, 20, False)
    assert args.workloads.split(",") == list(pc.WORKLOADS)
    assert pc.WORKLOADS[-1] == "server_create_lines_2k"


def test_one_measurement_in_this_tree(tmp_path):
    measured = pc.run_in_tree(REPO, ["create_lines_2k", "server_create_lines_2k"], tmp_path)
    assert set(measured) == {"create_lines_2k", "server_create_lines_2k"}
    assert all(value > 0 for value in measured.values())
