"""Backlog W4-T17-08: the dry run's crossings count what the real run counts.

``pid_from_spec(dry_run=True)`` counted each planned line against the spec's
own earlier lines only, and did not say so; the real run counts against every
P&ID line already on the current layout as well (``draw_line`` reads
``existing_pid_lines``). On a sheet that already carried a line the two totals
disagreed. Both now count in one scope and report it as ``crossings_scope``.

Oracle: the spec's one line runs (5, 0) -> (95, 0) (two ``dcs`` instruments of
radius 5 at x = 0 and x = 100, ``route: direct``); a P&ID line from (50, -20) to
(50, 20) crosses it once, at (50, 0), strictly inside both segments.
"""

from __future__ import annotations

import pytest

from engineering.pid.spec import run_spec

pytestmark = pytest.mark.asyncio

SPEC = {
    "instruments": [
        {"id": "I1", "type": "dcs", "location": "primary", "x": 0, "y": 0},
        {"id": "I2", "type": "dcs", "location": "primary", "x": 100, "y": 0},
    ],
    "lines": [{"from": "I1", "to": "I2", "class": "electric", "route": "direct", "label": False}],
}
SCOPE = "spec_lines+current_space_pid_lines"


async def test_the_dry_run_counts_a_crossing_with_a_line_already_drawn(backend):
    await backend.entity_create_polyline([[50, -20], [50, 20]], layer="UTILITY-LINE")

    dry = await run_spec(backend, SPEC, dry_run=True)

    assert dry["crossings_scope"] == SCOPE
    assert dry["lines"][0]["vertices"] == [[5.0, 0.0], [95.0, 0.0]]
    assert dry["crossings_total"] == 1


async def test_the_dry_run_and_the_real_run_agree(backend):
    await backend.entity_create_polyline([[50, -20], [50, 20]], layer="UTILITY-LINE")

    dry = await run_spec(backend, SPEC, dry_run=True)
    real = await run_spec(backend, SPEC)

    assert real["crossings_scope"] == dry["crossings_scope"] == SCOPE
    assert real["crossings_total"] == dry["crossings_total"] == 1


async def test_a_line_on_a_non_pid_layer_is_not_a_crossing(backend):
    await backend.entity_create_polyline([[50, -20], [50, 20]], layer="0")

    dry = await run_spec(backend, SPEC, dry_run=True)

    assert dry["crossings_total"] == 0
