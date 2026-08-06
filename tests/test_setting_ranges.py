"""A setting the renderer cannot use must be refused, not reported applied.

Folding the DIM* header variables into the per-dimension override closed a
silent no-op and opened a louder failure: values that used to die harmlessly in
the header now reach ezdxf's formatter. Measured before this guard existed:

* ``dim_decimals=-1`` returned ``ok: True`` and the *next* dimension of four of
  the five kinds died with ``ValueError: Format specifier missing precision``,
  from inside the renderer, nowhere near the call that caused it.
* ``dim_decimals=40`` returned ``ok: True`` and printed
  ``33.3329999999999984083842718973755836486816`` onto the drawing.
* ``dim_text_height=-3.0`` returned ``ok: True`` and changed nothing.

AutoCAD's own ranges are the reference: DIMDEC 0-8, DIMZIN a 0-15 bitmask, and
the two lengths strictly positive. `applied` has to mean applied.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("value", [-1, 9, 40])
async def test_out_of_range_dim_decimals_is_refused(backend, value):
    result = await backend.drawing_settings({"dim_decimals": value})

    assert result["ok"] is False
    assert "dim_decimals" in result["errors"]
    assert "dim_decimals" not in result.get("applied", {})


async def test_a_refused_dim_decimals_cannot_break_the_next_dimension(backend):
    """The crash was the point: the bad value reached the formatter."""
    await backend.drawing_settings({"dim_decimals": -1})

    dim = await backend.dimension_linear(0, 0, 33.333, 0, 16, -30)

    assert dim.handle


@pytest.mark.parametrize("value", [0, 2, 8])
async def test_dim_decimals_in_range_is_still_accepted(backend, value):
    result = await backend.drawing_settings({"dim_decimals": value})

    assert result["ok"] is True, result.get("errors")


@pytest.mark.parametrize("key", ["dim_text_height", "dim_arrow_size"])
@pytest.mark.parametrize("value", [0.0, -3.0])
async def test_a_non_positive_length_is_refused(backend, key, value):
    result = await backend.drawing_settings({key: value})

    assert result["ok"] is False
    assert key in result["errors"]


@pytest.mark.parametrize("value", [-1, 16, 999])
async def test_zero_suppression_outside_the_bitmask_is_refused(backend, value):
    result = await backend.drawing_settings({"zero_suppression": value})

    assert result["ok"] is False
    assert "zero_suppression" in result["errors"]


async def test_one_bad_key_does_not_discard_the_good_ones(backend):
    result = await backend.drawing_settings({"dim_text_height": 5.0, "dim_decimals": -1})

    assert result["applied"]["dim_text_height"] == 5.0
    assert "dim_decimals" in result["errors"]
