"""Revision rows, revision clouds and the triangular revision tags."""

from __future__ import annotations

import pytest

from engineering.mech.primitives import Poly, Text
from engineering.sheet.revision import (
    REVISION_COLUMNS,
    REVISION_LABELS,
    TAG_SIDE,
    add_revision,
    read_revisions,
    revision_block_prims,
    revision_heading_prims,
    revision_row_prims,
    revision_tag_prims,
)

EPS = 1e-9


def test_the_revision_columns_are_the_iso_7200_version_fields():
    assert REVISION_COLUMNS == ("rev", "description", "date", "by")
    for column in REVISION_COLUMNS:
        assert column in REVISION_LABELS


def test_a_tag_is_an_equilateral_triangle_carrying_the_letter():
    prims = revision_tag_prims("B", (100.0, 200.0))
    triangle = next(p for p in prims if isinstance(p, Poly))
    text = next(p for p in prims if isinstance(p, Text))
    assert triangle.closed and len(triangle.points) == 3
    xs = [p[0] for p in triangle.points]
    assert max(xs) - min(xs) == pytest.approx(TAG_SIDE, abs=EPS)
    assert text.text == "B"


def test_the_block_is_180_mm_wide_and_grows_downwards_from_its_anchor():
    rows = [
        {"rev": "A", "description": "first issue", "date": "2026-01-05", "by": "UC"},
        {"rev": "B", "description": "bore 40H7", "date": "2026-03-11", "by": "UC"},
    ]
    prims = revision_block_prims(rows, at=(230.0, 287.0))
    xs = [c[0] for p in prims if isinstance(p, Poly) for c in p.points]
    xs += [v for p in prims if hasattr(p, "p1") for v in (p.p1[0], p.p2[0])]
    assert min(xs) == pytest.approx(230.0, abs=EPS)
    assert max(xs) == pytest.approx(410.0, abs=EPS)
    ys = [v for p in prims if hasattr(p, "p1") for v in (p.p1[1], p.p2[1])]
    assert max(ys) == pytest.approx(287.0, abs=EPS)
    assert min(ys) == pytest.approx(287.0 - 3 * 7.0, abs=EPS)  # heading + 2 rows


def test_the_block_built_row_by_row_is_the_block_built_at_once():
    """`add_revision` appends one band at a time; the whole-block builder is
    what the tests read. They must be the same geometry or the second revision
    would not line up with the first."""
    rows = [
        {"rev": "A", "description": "first issue", "date": "2026-01-05", "by": "UC"},
        {"rev": "B", "description": "bore 40H7", "date": "2026-03-11", "by": "UC"},
    ]
    at = (230.0, 287.0)
    incremental = list(revision_heading_prims(at=at))
    for index, row in enumerate(rows):
        incremental.extend(revision_row_prims(row, at=at, index=index))
    assert tuple(incremental) == revision_block_prims(rows, at=at)


def test_a_rows_first_text_primitive_is_its_rev_cell():
    """`add_revision` hangs the payload on that primitive, so this is the link
    between the drawn row and the row a reader gets back."""
    band = revision_row_prims(
        {"rev": "B", "description": "", "date": "", "by": ""}, at=(230.0, 287.0), index=0
    )
    first_text = next(p for p in band if isinstance(p, Text))
    assert first_text.text == "B"


@pytest.mark.parametrize("bad", ["", "revision b", "AB CD", "b!"])
def test_a_malformed_revision_code_is_refused(bad):
    with pytest.raises(ValueError) as excinfo:
        revision_tag_prims(bad, (0.0, 0.0))
    assert "revision" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_a_revision_row_is_written_once_per_letter(backend):
    first = await add_revision(
        backend, rev="B", description="bore 40H7", date="2026-03-11", by="UC"
    )
    assert first["created"] is True
    count = len(await backend.entity_list())
    again = await add_revision(
        backend, rev="B", description="bore 40H7 (again)", date="2026-03-12", by="UC"
    )
    assert again["created"] is False
    assert again["handle"] == first["handle"]
    assert len(await backend.entity_list()) == count, "a repeated revision must draw nothing"
    rows = await read_revisions(backend)
    assert [r["rev"] for r in rows] == ["B"]
    assert rows[0]["description"] == "bore 40H7"


@pytest.mark.asyncio
async def test_two_revisions_stack_and_read_back_in_order(backend):
    await add_revision(backend, rev="A", description="first issue", date="2026-01-05", by="UC")
    await add_revision(backend, rev="B", description="bore 40H7", date="2026-03-11", by="UC")
    rows = await read_revisions(backend)
    assert [r["rev"] for r in rows] == ["A", "B"]


@pytest.mark.asyncio
async def test_clouds_and_tags_are_drawn_when_asked(backend):
    result = await add_revision(
        backend,
        rev="C",
        description="slot widened",
        clouds=[[10.0, 10.0, 60.0, 40.0]],
        tags=[[65.0, 45.0]],
    )
    assert len(result["clouds"]) == 1
    assert len(result["tags"]) == 1
    tag_texts = [
        e for e in await backend.entity_list(type_filter="TEXT") if e.properties["text"] == "C"
    ]
    assert tag_texts


@pytest.mark.asyncio
async def test_clouds_are_refused_before_any_row_when_the_engine_has_no_revcloud(
    backend, monkeypatch
):
    from backends.base import CapabilityMap, FeatureCapability

    original = backend.capabilities()
    features = dict(original.features)
    features["revcloud"] = FeatureCapability(False, reason="no_activex_member_command_only")
    monkeypatch.setattr(
        backend, "capabilities", lambda: CapabilityMap(backend=original.backend, features=features)
    )
    before = len(await backend.entity_list())
    from backends.base import UnsupportedCapabilityError

    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await add_revision(backend, rev="D", description="x", clouds=[[0.0, 0.0, 10.0, 10.0]])
    assert excinfo.value.capability == "revcloud"
    assert len(await backend.entity_list()) == before
    assert (await read_revisions(backend)) == ()
