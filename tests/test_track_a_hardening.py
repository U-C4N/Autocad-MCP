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
    """A layout block: records InsertBlock / AddText and hands back styled objects."""

    def __init__(self, attributes: dict[str, str] | None = None, name: str = "*Model_Space"):
        self.calls: list[tuple[str, tuple]] = []
        self.attributes = attributes or {}
        self.inserted: list[_FakeObject] = []
        self.texts: list[_FakeObject] = []
        self.Name = name
        self.IsLayout = True

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
        obj = _FakeObject("AcDbText", f"T{len(self.calls)}")
        self.texts.append(obj)
        return obj


@pytest.fixture
def com(monkeypatch):
    """A ComBackend over a recording fake: ``document`` and ``space`` are inspectable."""
    from backends import com_backend as module

    document = types.SimpleNamespace(
        Blocks=_FakeBlocks("TB"), Layers=_FakeLayers(), objects={}, owners={}
    )
    document.HandleToObject = lambda handle: document.objects[handle]
    # ``OwnerID`` -> block table record, the way ``ObjectIdToObject`` resolves it live.
    document.ObjectIdToObject = lambda object_id: document.owners[object_id]
    space = _FakeSpace()
    document.owners[1] = space  # the active layout; a sheet registers itself under 2
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


# ── Task 2: block_explode keeps ATTRIB text ──────────────────────────────────


async def test_explode_emits_a_text_per_attrib_at_the_attribs_placement(backend):
    from ezdxf.enums import TextEntityAlignment

    blk = backend._doc.blocks.new(name="TB2")
    blk.add_line((0, 0), (10, 0))
    tag = blk.add_attdef("TAG", insert=(0, 3), text="", dxfattribs={"height": 2.5})
    tag.set_placement((0, 3), align=TextEntityAlignment.CENTER)
    hidden = blk.add_attdef("LINK", insert=(0, -3), text="L-1", dxfattribs={"height": 2.0})
    hidden.is_invisible = True
    ref = await backend.block_insert(
        "TB2", 10, 10, scale_x=2.0, scale_y=2.0, rotation=30.0, attributes={"TAG": "P-101"}
    )
    raw_ref = backend._doc.entitydb[ref.handle]
    expected = {a.dxf.tag: (tuple(a.dxf.insert)[:2], a.dxf.height) for a in raw_ref.attribs}

    result = await backend.block_explode(ref.handle)

    assert result["ok"] is True and result["exploded_handle"] == ref.handle
    assert result["backend"] == "ezdxf"
    assert [(await backend.entity_get(h)).type for h in result["inserted_handles"]] == ["LINE"]
    assert len(result["attribute_texts"]) == 2
    texts = {}
    for handle in result["attribute_texts"]:
        info = await backend.entity_get(handle)
        assert info.type == "TEXT"
        texts[info.properties["text"]] = info
    tag_text = texts["P-101"]
    assert tuple(tag_text.properties["insertion"]) == pytest.approx(expected["TAG"][0])
    assert tag_text.properties["height"] == expected["TAG"][1] == 5.0
    assert tag_text.properties["rotation"] == pytest.approx(30.0)
    raw = backend._doc.entitydb[tag_text.handle]
    assert raw.get_align_enum().name == "CENTER", "alignment survives the conversion"
    assert raw.dxf.invisible == 0
    assert backend._doc.entitydb[texts["L-1"].handle].dxf.invisible == 1
    with pytest.raises(RuntimeError, match="not found"):
        await backend.entity_get(ref.handle)
    assert backend._doc.audit().errors == []


async def test_explode_without_attribs_reports_an_empty_list(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)
    await backend.block_create_from_entities("PLAIN", [line.handle])
    ref = await backend.block_insert("PLAIN", 0, 0)
    result = await backend.block_explode(ref.handle)
    assert result["attribute_texts"] == [] and len(result["inserted_handles"]) == 1


async def test_explode_refuses_a_non_insert_without_writing(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)
    before = len(list(backend._msp()))
    with pytest.raises(RuntimeError, match="not a block reference"):
        await backend.block_explode(line.handle)
    assert len(list(backend._msp())) == before


async def test_com_explode_deletes_attdefs_adds_text_per_attrib_and_deletes_the_reference(com):
    backend, document, space = com
    attdef = _FakeObject("AcDbAttributeDefinition", "D1")
    line = _FakeObject("AcDbLine", "L1")
    visible = _FakeObject(
        "AcDbAttribute",
        "A1",
        TagString="TAG",
        TextString="P-101",
        InsertionPoint=(7.0, 15.2, 0.0),
        Height=5.0,
        Rotation=0.5236,
        Layer="PID-TAG",
        Invisible=False,
    )
    hidden = _FakeObject(
        "AcDbAttribute",
        "A2",
        TagString="LINK",
        TextString="L-1",
        InsertionPoint=(13.0, 4.8, 0.0),
        Height=4.0,
        Rotation=0.5236,
        Layer="0",
        Invisible=True,
    )
    ref = _FakeObject(
        "AcDbBlockReference",
        "2F",
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (visible, hidden),
        Explode=lambda: (line, attdef),
    )
    document.objects["2F"] = ref

    result = await backend.block_explode("2F")

    assert result == {
        "ok": True,
        "exploded_handle": "2F",
        "inserted_handles": ["L1"],
        "attribute_texts": ["T1", "T2"],
        "backend": "com",
    }
    assert attdef.deleted is True, "the value-less ATTDEF placeholder is removed"
    assert line.deleted is False
    assert ref.deleted is True, "ActiveX Explode leaves the reference; BURST removes it"
    assert [c for c in space.calls] == [
        ("AddText", ("P-101", (7.0, 15.2, 0.0), 5.0)),
        ("AddText", ("L-1", (13.0, 4.8, 0.0), 4.0)),
    ]


async def test_com_explode_refuses_a_non_insert_before_any_call(com):
    backend, document, space = com
    document.objects["3A"] = _FakeObject("AcDbLine", "3A")
    with pytest.raises(RuntimeError, match="not a block reference"):
        await backend.block_explode("3A")
    assert space.calls == []


async def test_com_explode_adds_the_text_to_the_owner_block_not_the_active_layout(com):
    """ActiveX ``Explode()`` places the members in the owner space; the tag
    TEXT must land beside them, not in whatever tab is active."""
    backend, document, space = com
    sheet = _FakeSpace(name="Sheet")
    document.owners[2] = sheet
    title = _FakeObject(
        "AcDbAttribute",
        "A1",
        TagString="TITLE",
        TextString="GEAR-01",
        InsertionPoint=(100.0, 50.0, 0.0),
        Height=3.5,
        Rotation=0.0,
        Layer="TITLEBLOCK",
        Invisible=False,
    )
    line = _FakeObject("AcDbLine", "L1")
    ref = _FakeObject(
        "AcDbBlockReference",
        "4B",
        Name="TB",
        OwnerID=2,
        GetAttributes=lambda: (title,),
        Explode=lambda: (line,),
    )
    document.objects["4B"] = ref

    result = await backend.block_explode("4B")

    assert result["ok"] is True and result["inserted_handles"] == ["L1"]
    assert sheet.calls == [("AddText", ("GEAR-01", (100.0, 50.0, 0.0), 3.5))]
    assert space.calls == [], "nothing may be written into the active layout"
    assert ref.deleted is True


async def test_com_explode_refuses_a_nested_reference_before_explode(com):
    backend, document, space = com
    document.owners[3] = _FakeBlock("OUTER")  # a block definition, IsLayout False
    dispatched = []

    def _explode():
        dispatched.append("Explode")
        return ()

    ref = _FakeObject(
        "AcDbBlockReference",
        "5C",
        Name="TB",
        OwnerID=3,
        GetAttributes=lambda: (),
        Explode=_explode,
    )
    document.objects["5C"] = ref
    with pytest.raises(RuntimeError, match="nested inside block definition 'OUTER'"):
        await backend.block_explode("5C")
    assert dispatched == [] and space.calls == [] and ref.deleted is False


async def test_com_explode_carries_the_attrib_frame_and_style_onto_the_text(com):
    """The COM twin of the mirrored-reference test. ``AddText`` builds a +Z
    TEXT; an ATTRIB reflected to ``Normal (0, 0, -1)`` has an OCS ``Rotation``
    and mirrored glyphs, so the TEXT must take the frame *before* the angle
    and have its WCS anchor re-asserted after the frame changes it. The
    style members ezdxf carries (style, oblique, width, generation flags,
    alignment + alignment point, colour) ride along the same way."""
    backend, document, space = com
    attrib = _FakeObject(
        "AcDbAttribute",
        "A1",
        TagString="TAG",
        TextString="P-101",
        InsertionPoint=(-25.0, 13.0, 0.0),
        Height=2.5,
        Rotation=0.5236,
        Layer="PID-TAG",
        Invisible=False,
        Normal=(0.0, 0.0, -1.0),
        Thickness=0.7,
        StyleName="ISO",
        ObliqueAngle=0.2618,
        ScaleFactor=0.8,
        Backward=True,
        UpsideDown=False,
        Alignment=4,
        TextAlignmentPoint=(-20.0, 13.0, 0.0),
        Color=3,
    )
    ref = _FakeObject(
        "AcDbBlockReference",
        "6D",
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (attrib,),
        Explode=lambda: (),
    )
    document.objects["6D"] = ref

    result = await backend.block_explode("6D")

    assert result["ok"] is True and result["attribute_texts"] == ["T1"]
    assert space.calls == [("AddText", ("P-101", (-25.0, 13.0, 0.0), 2.5))]
    text = space.texts[0]
    # Point writes go through ``_apoint`` (a VARIANT); compare their payload.
    writes = [
        (name, tuple(value.value) if hasattr(value, "value") else value)
        for name, value in text.writes
    ]
    names = [name for name, _ in writes]
    assert ("Normal", (0.0, 0.0, -1.0)) in writes, "the ATTRIB's frame is carried"
    assert names.index("Normal") < names.index("Rotation"), (
        "Rotation is the OCS angle: it only means the same thing inside the same frame"
    )
    assert names.index("Normal") < names.index("InsertionPoint"), (
        "the WCS anchor is re-asserted after the frame change moves the OCS origin"
    )
    assert names.index("Alignment") < names.index("TextAlignmentPoint"), (
        "ActiveX rejects an alignment point on a left-aligned text"
    )
    assert dict(writes) == {
        "Normal": (0.0, 0.0, -1.0),
        "Thickness": 0.7,
        "StyleName": "ISO",
        "ObliqueAngle": 0.2618,
        "ScaleFactor": 0.8,
        "Backward": True,
        "UpsideDown": False,
        "Rotation": 0.5236,
        "InsertionPoint": (-25.0, 13.0, 0.0),
        "Alignment": 4,
        "TextAlignmentPoint": (-20.0, 13.0, 0.0),
        "Layer": "PID-TAG",
        "Color": 3,
    }


async def test_com_explode_skips_frame_members_the_attrib_does_not_expose(com):
    """A bare ATTRIB (the shape the earlier fakes use) still explodes: an
    absent optional member is skipped, never written as a guess, and a
    left-aligned ATTRIB never gets a TextAlignmentPoint."""
    backend, document, space = com
    attrib = _FakeObject(
        "AcDbAttribute",
        "A1",
        TagString="TAG",
        TextString="P-101",
        InsertionPoint=(7.0, 15.2, 0.0),
        Height=5.0,
        Rotation=0.0,
        Layer="0",
        Invisible=False,
        Alignment=0,
        TextAlignmentPoint=(99.0, 99.0, 0.0),
    )
    ref = _FakeObject(
        "AcDbBlockReference",
        "7E",
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (attrib,),
        Explode=lambda: (),
    )
    document.objects["7E"] = ref

    result = await backend.block_explode("7E")

    assert result["ok"] is True
    names = [name for name, _ in space.texts[0].writes]
    assert "Normal" not in names and "StyleName" not in names
    assert "InsertionPoint" not in names, "a +Z text keeps the anchor AddText was given"
    assert "TextAlignmentPoint" not in names, "left alignment has no alignment point"
    assert names == ["Rotation", "Alignment", "Layer"]


async def test_explode_of_a_mirrored_reference_keeps_the_tag_where_the_attrib_was(backend):
    """An ATTRIB is an OCS entity. Mirroring reflects it to extrusion
    ``(0, 0, -1)`` while the INSERT keeps +Z with ``yscale=-1``; the TEXT
    that replaces it must carry the frame, or it lands on the other side
    of the mirror axis with a clean audit."""
    from backends.ocs import to_wcs_2d

    blk = backend._doc.blocks.new(name="SYM")
    blk.add_line((0, 0), (10, 0))
    blk.add_attdef("TAG", insert=(5, 3), text="", dxfattribs={"height": 2.5})
    ref = await backend.block_insert("SYM", 20, 10, attributes={"TAG": "P-101"})
    mirrored = await backend.entity_mirror(ref.handle, 0, 0, 0, 1, delete_original=True)
    raw = backend._doc.entitydb[mirrored.handle]
    (attrib,) = raw.attribs
    assert tuple(attrib.dxf.extrusion) == (0.0, 0.0, -1.0), "the premise: a reflected ATTRIB"
    expected = to_wcs_2d(attrib.dxf.extrusion, *attrib.dxf.insert)
    assert expected == pytest.approx([-25.0, 13.0])

    result = await backend.block_explode(mirrored.handle)

    (line_handle,) = result["inserted_handles"]
    (text_handle,) = result["attribute_texts"]
    line = await backend.entity_get(line_handle)
    text = await backend.entity_get(text_handle)
    assert text.properties["insertion"] == pytest.approx(expected)
    assert text.properties["insertion"][0] < 0 and line.properties["start"][0] < 0, (
        "tag and geometry stay on the same side of the mirror axis"
    )
    assert backend._doc.audit().errors == []


async def test_explode_writes_into_the_inserts_owner_layout_not_the_current_one(backend):
    _define_tagged_block(backend)
    await backend.layout_create("Sheet")
    await backend.layout_set_current("Sheet")
    ref = await backend.block_insert("TB", 100, 50, attributes={"TAG": "GEAR-01"})
    await backend.layout_set_current("Model")
    model_before = len(list(backend._doc.modelspace()))

    result = await backend.block_explode(ref.handle)

    assert result["ok"] is True
    sheet = backend._doc.layouts.get("Sheet")
    assert sorted(e.dxftype() for e in sheet) == ["LINE", "TEXT"]
    assert len(list(backend._doc.modelspace())) == model_before, "model space untouched"
    for handle in result["inserted_handles"] + result["attribute_texts"]:
        assert backend._doc.entitydb[handle].get_layout().name == "Sheet"
    with pytest.raises(RuntimeError, match="not found"):
        await backend.entity_get(ref.handle)
    assert backend._doc.audit().errors == []


async def test_explode_refuses_a_nested_reference_before_writing(backend):
    _define_tagged_block(backend)
    outer = backend._doc.blocks.new(name="OUTER")
    nested = outer.add_blockref("TB", (0, 0))
    before = (len(list(backend._doc.modelspace())), len(list(outer)))
    with pytest.raises(RuntimeError, match="nested inside block definition 'OUTER'"):
        await backend.block_explode(nested.dxf.handle)
    assert (len(list(backend._doc.modelspace())), len(list(outer))) == before
    assert backend._doc.entitydb[nested.dxf.handle].is_alive
