"""Stairs in plan: tread lines, outline, walking line, label, and the Blondel rule.

Every expected coordinate is hand-computed from the stair's numbers: a flight
of m risers at going g runs (m - 1) * g, a riser line spans the full width, and
the first flight of an L or U carries (risers + 1) // 2 risers.
"""

from __future__ import annotations

import pytest

from engineering.arch.lang import vocab
from engineering.arch.model import Stair
from engineering.arch.stairs import BLONDEL_RANGE, blondel, stair_prims
from engineering.mech.primitives import Line, Poly, Text


def S(x1, y1, x2, y2) -> tuple:
    return tuple(sorted(((float(x1), float(y1)), (float(x2), float(y2)))))


def segs(prims) -> list:
    out = []
    for p in prims:
        if isinstance(p, Line):
            a = (round(p.p1[0], 9) + 0.0, round(p.p1[1], 9) + 0.0)
            b = (round(p.p2[0], 9) + 0.0, round(p.p2[1], 9) + 0.0)
            out.append(tuple(sorted((a, b))))
    return out


def path(prims) -> list:
    (walk,) = [p for p in prims if isinstance(p, Poly)]
    return [(round(x, 9) + 0.0, round(y, 9) + 0.0) for x, y in walk.points]


def stair(**kw) -> Stair:
    base = {
        "id": "s1",
        "start": (1000.0, 2000.0),
        "direction_deg": 0.0,
        "width": 1000.0,
        "risers": 17,
        "riser_height": 170.0,
        "going": 290.0,
    }
    return Stair(**{**base, **kw})


# -- Blondel -----------------------------------------------------------------------


def test_blondel_reports_two_risers_plus_a_going_against_600_to_650():
    assert BLONDEL_RANGE == (600.0, 650.0)
    assert blondel(170.0, 290.0) == {"value": 630.0, "ok": True, "range": [600.0, 650.0]}
    assert blondel(200.0, 300.0) == {"value": 700.0, "ok": False, "range": [600.0, 650.0]}
    assert blondel(150.0, 280.0)["ok"] is False  # 580
    assert blondel(175.0, 250.0)["ok"] is True  # 600, the lower edge is inside


def test_blondel_refuses_a_non_positive_dimension_by_name():
    with pytest.raises(ValueError, match="riser_height"):
        blondel(0.0, 290.0)
    with pytest.raises(ValueError, match="going"):
        blondel(170.0, -1.0)


# -- straight -----------------------------------------------------------------------


def test_a_straight_stair_draws_every_riser_once_and_both_stringers():
    prims = stair_prims(stair())
    lines = segs(prims)
    risers = [S(1000 + 290 * k, 1500, 1000 + 290 * k, 2500) for k in range(17)]
    assert set(risers) <= set(lines)
    assert S(1000, 1500, 5640, 1500) in lines  # (17 - 1) * 290 = 4640
    assert S(1000, 2500, 5640, 2500) in lines
    assert len(lines) == 17 + 2 + 2  # risers, stringers, the two arrow strokes
    assert all(p.role == "stair" for p in prims)


def test_the_walking_line_runs_up_the_middle_with_an_arrow_at_the_top():
    prims = stair_prims(stair(), scale=50)
    assert path(prims) == [(1000.0, 2000.0), (5640.0, 2000.0)]
    lines = segs(prims)
    # arrow 3 mm x 50 = 150 long, its strokes 50 either side of the line
    assert S(5490, 2050, 5640, 2000) in lines
    assert S(5490, 1950, 5640, 2000) in lines


def test_the_up_label_sits_on_the_first_tread_at_the_plot_scale_height():
    (text,) = [p for p in stair_prims(stair(), scale=50) if isinstance(p, Text)]
    assert text.text == vocab("en")["up"]
    assert text.height == 125.0  # 2.5 mm x 50
    # (0.15 * 290, -0.25 * 1000 - 0.5 * 125) from the start
    assert (round(text.at[0], 9), round(text.at[1], 9)) == (1043.5, 1687.5)
    (tr,) = [p for p in stair_prims(stair(), lang="tr") if isinstance(p, Text)]
    assert tr.text == vocab("tr")["up"]


def test_direction_turns_the_whole_stair_about_its_start():
    prims = stair_prims(stair(direction_deg=90.0))
    lines = segs(prims)
    risers = [S(500, 2000 + 290 * k, 1500, 2000 + 290 * k) for k in range(17)]
    assert set(risers) <= set(lines)
    assert path(prims) == [(1000.0, 2000.0), (1000.0, 6640.0)]
    (text,) = [p for p in prims if isinstance(p, Text)]
    assert text.rotation == 90.0


# -- L and U ------------------------------------------------------------------------


def test_an_l_stair_turns_left_on_a_square_landing():
    prims = stair_prims(stair(start=(0.0, 0.0), kind="l", risers=16, going=280.0))
    lines = segs(prims)
    # 8 + 8 risers; the landing starts at 7 * 280 = 1960, the top riser at 500 + 7 * 280
    first = [S(280 * k, -500, 280 * k, 500) for k in range(8)]
    second = [S(1960, 500 + 280 * k, 2960, 500 + 280 * k) for k in range(8)]
    assert set(first + second) <= set(lines)
    assert {
        S(0, -500, 2960, -500),
        S(2960, -500, 2960, 2460),
        S(0, 500, 1960, 500),
        S(1960, 500, 1960, 2460),
    } <= set(lines)
    assert len(lines) == 16 + 4 + 2
    assert path(prims) == [(0.0, 0.0), (2460.0, 0.0), (2460.0, 2460.0)]


def test_an_l_stair_turning_right_is_the_mirror_image():
    prims = stair_prims(stair(start=(0.0, 0.0), kind="l", turn="right", risers=16, going=280.0))
    lines = segs(prims)
    assert S(1960, -500, 2960, -500) in lines
    assert S(2960, 500, 2960, -2460) in lines
    assert path(prims)[-1] == (2460.0, -2460.0)


def test_a_u_stair_returns_beside_the_first_flight():
    prims = stair_prims(stair(start=(0.0, 0.0), kind="u", risers=18, going=270.0, width=1100.0))
    lines = segs(prims)
    # 9 + 9 risers; landing at 8 * 270 = 2160; the second flight comes back to x = 0
    first = [S(270 * k, -550, 270 * k, 550) for k in range(9)]
    second = [S(2160 - 270 * k, 550, 2160 - 270 * k, 1650) for k in range(9)]
    assert set(first + second) <= set(lines)
    assert {
        S(0, -550, 3260, -550),
        S(3260, -550, 3260, 1650),
        S(3260, 1650, 0, 1650),
        S(0, 550, 2160, 550),
    } <= set(lines)
    assert len(lines) == 18 + 4 + 2
    assert path(prims) == [(0.0, 0.0), (2710.0, 0.0), (2710.0, 1100.0), (0.0, 1100.0)]


# -- refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"risers": 1}, "at least 2"),
        ({"kind": "l", "risers": 3}, "at least 4"),
        ({"risers": True}, "whole number"),
        ({"kind": "spiral"}, "kinds are straight, l, u"),
        ({"turn": "up"}, "turns are left, right"),
        ({"width": 0.0}, "width"),
        ({"going": float("nan")}, "going"),
        ({"direction_deg": "north"}, "direction_deg must be finite"),
        ({"start": (0.0, float("inf"))}, r"start\[1\] must be finite"),
    ],
)
def test_a_bad_stair_is_refused_before_any_geometry(kw, match):
    with pytest.raises(ValueError, match=match):
        stair_prims(stair(**kw))


def test_an_unknown_language_is_refused():
    with pytest.raises(ValueError):
        stair_prims(stair(), lang="de")
