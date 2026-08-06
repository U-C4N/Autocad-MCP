"""M5 — handle grounding: connecting the picture to the handles.

A screenshot shows the model what the drawing looks like and nothing it can act
on. Every modify tool takes a handle, and there was no way to tell which of the
hex strings from `entity_list` is the circle in the top-left — so the model
either guessed or re-derived the mapping from coordinates, which is exactly what
CLAUDE.md's no-coordinate-guessing rule exists to prevent.

The overlay labels each entity with its handle at its own bounding-box centre,
so "the circle at the top-left" and "handle 2F" become the same statement.

Off by default: labels are ink on the drawing, and on a busy sheet they would
bury the geometry they are meant to explain. When there are more entities than
labels allowed, the image says so rather than labelling a subset and letting it
read as the whole picture.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")


def _png_size(data: bytes) -> tuple[int, int]:
    """Width/height straight out of the PNG IHDR — no Pillow needed."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


async def test_a_plain_screenshot_is_unchanged(backend):
    await backend.entity_create_circle(10, 10, 5)
    png = await backend.view_screenshot()

    assert png is not None
    assert _png_size(png)[0] > 0


async def test_the_overlay_changes_the_image(backend):
    """The labels have to actually be drawn, not merely computed."""
    await backend.entity_create_circle(10, 10, 5)
    await backend.entity_create_line(0, 0, 20, 0)

    plain = await backend.view_screenshot()
    labelled = await backend.view_screenshot(overlay_handles=True)

    assert labelled is not None
    assert labelled != plain, "the overlay produced a byte-identical image"


async def test_the_overlay_reports_which_handles_it_drew(backend):
    circle = await backend.entity_create_circle(10, 10, 5)
    line = await backend.entity_create_line(0, 0, 20, 0)

    report = await backend.view_screenshot_grounded()

    assert set(report["labels"]) == {circle.handle, line.handle}
    assert report["labelled"] == 2
    assert report["total"] == 2
    assert report["truncated"] is False
    assert report["png"][:8] == b"\x89PNG\r\n\x1a\n"


async def test_each_label_sits_on_its_own_entity(backend):
    """A label in the wrong place is worse than no label — it grounds a lie."""
    left = await backend.entity_create_circle(-50, 0, 5)
    right = await backend.entity_create_circle(50, 0, 5)

    report = await backend.view_screenshot_grounded()

    assert report["labels"][left.handle][0] == pytest.approx(-50, abs=1.0)
    assert report["labels"][right.handle][0] == pytest.approx(50, abs=1.0)


async def test_a_crowded_drawing_says_it_only_labelled_some(backend):
    for i in range(30):
        await backend.entity_create_circle(i * 10, 0, 2)

    report = await backend.view_screenshot_grounded(max_labels=10)

    assert report["labelled"] == 10
    assert report["total"] == 30
    assert report["truncated"] is True, (
        "labelling a subset silently would let the picture read as the whole drawing"
    )


async def test_an_empty_drawing_grounds_nothing_without_failing(backend):
    report = await backend.view_screenshot_grounded()
    assert report["labels"] == {}
    assert report["total"] == 0
    assert report["png"] is not None
