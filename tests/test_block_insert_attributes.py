"""block_insert(attributes=...) must reference the named block, not an anonymous wrapper.

ezdxf's `add_auto_blockref` wraps the INSERT in an anonymous `*U` block. The
handle it returns then reports block_name `*U1`, and `block_get_attributes`
finds no ATTRIBs on it — an insert with attributes silently lost them.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _define_tagged_block(backend, name: str = "TB") -> None:
    blk = backend._doc.blocks.new(name=name)
    blk.add_line((0, 0), (10, 0))
    blk.add_attdef("TAG", insert=(0, 3), text="", dxfattribs={"height": 2.5})


async def test_insert_with_attributes_references_the_named_block(backend):
    _define_tagged_block(backend)
    ref = await backend.block_insert("TB", 10, 10, attributes={"TAG": "P-101"})

    info = await backend.entity_get(ref.handle)
    assert info.properties["block_name"] == "TB"
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "P-101"}
    anonymous = [b.name for b in backend._doc.blocks if b.name.startswith("*U")]
    assert anonymous == [], "no anonymous wrapper block may be created"


async def test_insert_ignores_values_for_tags_the_block_does_not_define(backend):
    _define_tagged_block(backend)
    ref = await backend.block_insert("TB", 0, 0, attributes={"TAG": "V-1", "NOPE": "x"})
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "V-1"}


async def test_insert_without_attributes_is_unchanged(backend):
    _define_tagged_block(backend)
    ref = await backend.block_insert("TB", 0, 0)
    info = await backend.entity_get(ref.handle)
    assert info.properties["block_name"] == "TB"
    assert await backend.block_get_attributes(ref.handle) == {}
