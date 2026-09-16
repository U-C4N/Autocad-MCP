"""Track A hardening (v1.6 track E, wave 0): the block-tool defects the Track A
review found, fixed on both engines.

Headless tests run against a real ezdxf document; the live engine runs against
a fake ActiveX surface that records every call, so the tests assert the call
shape (what is dispatched, in which order, and — for a refusal — that nothing
is dispatched at all).
"""

from __future__ import annotations

import types

import pytest

from backends.base import EntityInfo

pytestmark = pytest.mark.asyncio


def _define_tagged_block(backend, name: str = "TB") -> None:
    blk = backend._doc.blocks.new(name=name)
    blk.add_line((0, 0), (10, 0))
    blk.add_attdef("TAG", insert=(0, 3), text="", dxfattribs={"height": 2.5})


# ── fake ActiveX surface ──────────────────────────────────────────────────────


class _FakeObject:
    """An ActiveX object with only the members it is given; property writes are recorded."""

    def __init__(self, object_name: str, handle: str, **members):
        object.__setattr__(self, "ObjectName", object_name)
        object.__setattr__(self, "Handle", handle)
        object.__setattr__(self, "writes", [])
        object.__setattr__(self, "deleted", False)
        for name, value in members.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        self.writes.append((name, value))
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        raise AttributeError(name)

    def Delete(self):
        object.__setattr__(self, "deleted", True)


class _FakeBlock:
    def __init__(self, name: str, is_layout: bool = False):
        self.Name = name
        self.IsLayout = is_layout
        self.Count = 0
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, attr):
        if attr.startswith("Add"):

            def _call(*args):
                self.calls.append((attr, args))
                return _FakeObject("AcDbEntity", f"M{len(self.calls)}")

            return _call
        raise AttributeError(attr)


class _FakeBlocks:
    def __init__(self, *names: str):
        self.blocks: dict[str, _FakeBlock] = {
            "*Model_Space": _FakeBlock("*Model_Space", is_layout=True),
        }
        for name in names:
            self.blocks[name] = _FakeBlock(name)

    def Add(self, base_point, name):
        self.blocks[name] = _FakeBlock(name)
        self.blocks[name].base_point = tuple(base_point.value)
        return self.blocks[name]

    def Item(self, name):
        if name not in self.blocks:
            raise RuntimeError(f"no block {name}")
        return self.blocks[name]


class _FakeLayers:
    def __init__(self, *names: str):
        self.names = {"0", *names}
        self.added: list[str] = []

    def Item(self, name):
        if name not in self.names:
            raise RuntimeError(f"no layer {name}")
        return types.SimpleNamespace(Name=name)

    def Add(self, name):
        self.names.add(name)
        self.added.append(name)
        return types.SimpleNamespace(Name=name)


class _FakeSpace:
    """The active layout block: records InsertBlock / AddText and hands back styled objects."""

    def __init__(self, attributes: dict[str, str] | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self.attributes = attributes or {}
        self.inserted: list[_FakeObject] = []

    def InsertBlock(self, point, name, sx, sy, sz, rotation):
        self.calls.append(("InsertBlock", (tuple(point.value), name, sx, sy, sz, rotation)))
        attrs = tuple(
            _FakeObject("AcDbAttribute", f"A{i}", TagString=tag, TextString=value)
            for i, (tag, value) in enumerate(self.attributes.items())
        )
        ref = _FakeObject(
            "AcDbBlockReference", f"R{len(self.inserted)}", Name=name, GetAttributes=lambda: attrs
        )
        self.inserted.append(ref)
        return ref

    def AddText(self, text, point, height):
        self.calls.append(("AddText", (text, tuple(point.value), height)))
        return _FakeObject("AcDbText", f"T{len(self.calls)}")


@pytest.fixture
def com(monkeypatch):
    """A ComBackend over a recording fake: ``document`` and ``space`` are inspectable."""
    from backends import com_backend as module

    document = types.SimpleNamespace(Blocks=_FakeBlocks("TB"), Layers=_FakeLayers(), objects={})
    document.HandleToObject = lambda handle: document.objects[handle]
    space = _FakeSpace()
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    monkeypatch.setattr(module, "_msp", lambda: space)
    monkeypatch.setattr(module, "_regen", lambda: None)
    # The real converter reads a dozen ActiveX members (bounding box, block
    # geometry, ...); the fixture only needs the handle and the name back.
    monkeypatch.setattr(
        module,
        "_entity_info",
        lambda ent: EntityInfo(
            handle=ent.Handle,
            type="INSERT",
            layer="0",
            color=256,
            linetype="ByLayer",
            visible=True,
            properties={"block_name": ent.Name},
        ),
    )
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, document, space


# ── Task 1: entity_create_block_ref refuses an undefined block ────────────────


async def test_block_ref_of_an_undefined_block_is_refused_before_writing(backend):
    before = len(list(backend._msp()))
    with pytest.raises(ValueError, match="'NO_SUCH_BLOCK' is not defined"):
        await backend.entity_create_block_ref("NO_SUCH_BLOCK", 1, 2)
    assert len(list(backend._msp())) == before, "a refused insert must not write"
    assert "NO_SUCH_BLOCK" not in backend._doc.blocks


async def test_block_ref_of_a_defined_block_still_inserts(backend):
    _define_tagged_block(backend)
    ref = await backend.entity_create_block_ref("TB", 5, 5, rotation=90)
    info = await backend.entity_get(ref.handle)
    assert info.type == "INSERT" and info.properties["block_name"] == "TB"
    assert info.properties["rotation_deg"] == pytest.approx(90.0)


@pytest.mark.parametrize("name", ["*Model_Space", "*Paper_Space"])
async def test_layout_blocks_are_refused_by_both_placing_tools(backend, name):
    with pytest.raises(ValueError, match="layout block"):
        await backend.entity_create_block_ref(name, 0, 0)
    with pytest.raises(ValueError, match="layout block"):
        await backend.block_insert(name, 0, 0)
    assert backend._doc.audit().errors == [], "nothing was written, nothing to fix"


async def test_a_non_string_block_name_is_a_type_error_on_both_placing_tools(backend):
    with pytest.raises(TypeError, match="non-empty string"):
        await backend.entity_create_block_ref(None, 0, 0)
    with pytest.raises(TypeError, match="non-empty string"):
        await backend.block_insert("", 0, 0)


async def test_com_block_ref_probes_the_block_table_before_insert_block(com):
    backend, document, space = com
    with pytest.raises(ValueError, match="'NO_SUCH_BLOCK' is not defined"):
        await backend.entity_create_block_ref("NO_SUCH_BLOCK", 1, 2)
    assert space.calls == [], "InsertBlock must not be dispatched for an unknown name"
    with pytest.raises(ValueError, match="layout block"):
        await backend.entity_create_block_ref("*Model_Space", 0, 0)
    assert space.calls == []

    ref = await backend.entity_create_block_ref("TB", 1, 2, 2.0, 2.0, 90.0)
    assert ref.properties["block_name"] == "TB"
    assert space.calls == [
        ("InsertBlock", ((1.0, 2.0, 0.0), "TB", 2.0, 2.0, 1.0, pytest.approx(1.5708, abs=1e-4)))
    ]


async def test_com_block_insert_shares_the_same_gate(com):
    backend, document, space = com
    with pytest.raises(ValueError, match="'NO_SUCH_BLOCK' is not defined"):
        await backend.block_insert("NO_SUCH_BLOCK", 1, 2, attributes={"TAG": "P-1"})
    assert space.calls == []
