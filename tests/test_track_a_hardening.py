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


#: Members AutoCAD's ActiveX takes as a ``VT_ARRAY|VT_R8`` VARIANT. A plain
#: Python tuple marshals as ``VT_ARRAY|VT_VARIANT`` and is refused with
#: ``E_INVALIDARG`` (verified live on AutoCAD 2026: ``text.Normal = (0, 0, -1)``
#: raises, ``text.Normal = _apoint(0, 0, -1)`` succeeds); the fake refuses it
#: the same way so a test cannot pin the wrong call shape.
_POINT_MEMBERS = frozenset({"Normal", "InsertionPoint", "TextAlignmentPoint"})


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
        if name in _POINT_MEMBERS and not hasattr(value, "varianttype"):
            raise RuntimeError(
                f"E_INVALIDARG: {name} takes a VT_ARRAY|VT_R8 VARIANT, not {type(value).__name__}"
            )
        self.writes.append((name, value))
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        raise AttributeError(name)

    def Delete(self):
        object.__setattr__(self, "deleted", True)


class _FakeBlock:
    def __init__(self, name: str, is_layout: bool = False, is_xref: bool = False):
        self.Name = name
        self.IsLayout = is_layout
        self.IsXRef = is_xref
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

    def AddMText(self, point, width, text):
        self.calls.append(("AddMText", (tuple(point.value), width, text)))
        obj = _FakeObject("AcDbMText", f"T{len(self.calls)}")
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
    import pythoncom

    for name, value in text.writes:
        if name in _POINT_MEMBERS:
            assert value.varianttype == pythoncom.VT_ARRAY | pythoncom.VT_R8, (
                f"{name} must be a double-array VARIANT, not a tuple (E_INVALIDARG live)"
            )
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


async def test_com_explode_turns_a_constant_attdef_into_text_instead_of_deleting_it(com):
    """``GetAttributes()`` excludes constant attributes (``GetConstantAttributes()``
    lists them) and ``Explode()`` returns each as an ``AcDbAttributeDefinition``
    already at its WCS placement showing its value. Deleting every ATTDEF
    destroyed that text with ``ok: True``; BURST converts it to TEXT."""
    backend, document, space = com
    tag = _FakeObject(
        "AcDbAttribute",
        "A1",
        TagString="TAG",
        TextString="P-101",
        InsertionPoint=(7.0, 15.2, 0.0),
        Height=5.0,
        Rotation=0.0,
        Layer="PID-TAG",
        Invisible=False,
    )
    placeholder = _FakeObject("AcDbAttributeDefinition", "D1", TagString="TAG", Constant=False)
    constant = _FakeObject(
        "AcDbAttributeDefinition",
        "D2",
        TagString="CONST",
        TextString="ACME",
        Constant=True,
        InsertionPoint=(25.0, 7.0, 0.0),
        Height=2.0,
        Rotation=0.5236,
        Layer="TITLEBLOCK",
        Invisible=False,
        Alignment=4,
        TextAlignmentPoint=(25.0, 7.0, 0.0),
    )
    line = _FakeObject("AcDbLine", "L1")
    ref = _FakeObject(
        "AcDbBlockReference",
        "8F",
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (tag,),
        GetConstantAttributes=lambda: (constant,),
        Explode=lambda: (line, placeholder, constant),
    )
    document.objects["8F"] = ref

    result = await backend.block_explode("8F")

    assert result["inserted_handles"] == ["L1"]
    assert result["attribute_texts"] == ["T1", "T2"], "the constant value is a TEXT too"
    assert placeholder.deleted is True and constant.deleted is True and ref.deleted is True
    assert space.calls == [
        ("AddText", ("P-101", (7.0, 15.2, 0.0), 5.0)),
        ("AddText", ("ACME", (25.0, 7.0, 0.0), 2.0)),
    ]
    writes = [
        (name, tuple(value.value) if hasattr(value, "value") else value)
        for name, value in space.texts[1].writes
    ]
    assert writes == [
        ("Rotation", 0.5236),
        ("Alignment", 4),
        ("TextAlignmentPoint", (25.0, 7.0, 0.0)),
        ("Layer", "TITLEBLOCK"),
    ], "the constant ATTDEF's placement, angle and alignment ride onto its TEXT"


async def test_com_explode_falls_back_to_the_constant_tag_list_when_constant_is_absent(com):
    """An object that does not expose ``Constant`` is still recognised through
    ``GetConstantAttributes()``; a value-less placeholder is still just deleted."""
    backend, document, space = com
    constant = _FakeObject(
        "AcDbAttributeDefinition",
        "D2",
        TagString="CONST",
        TextString="ACME",
        InsertionPoint=(1.0, 2.0, 0.0),
        Height=2.0,
        Rotation=0.0,
        Layer="0",
    )
    placeholder = _FakeObject("AcDbAttributeDefinition", "D1", TagString="TAG")
    ref = _FakeObject(
        "AcDbBlockReference",
        "9A",
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (),
        GetConstantAttributes=lambda: (constant,),
        Explode=lambda: (placeholder, constant),
    )
    document.objects["9A"] = ref

    result = await backend.block_explode("9A")

    assert result["attribute_texts"] == ["T1"]
    assert space.calls == [("AddText", ("ACME", (1.0, 2.0, 0.0), 2.0))]
    assert placeholder.deleted is True and constant.deleted is True


def _define_block_with_constant_attdef(backend, name: str = "TBC") -> None:
    from ezdxf.enums import TextEntityAlignment

    blk = backend._doc.blocks.new(name=name)
    blk.add_line((0, 0), (10, 0))
    blk.add_attdef("TAG", insert=(0, 3), text="", dxfattribs={"height": 2.5})
    const = blk.add_attdef("CONST", insert=(5, -3), text="ACME", dxfattribs={"height": 2.0})
    const.set_placement((5, -3), align=TextEntityAlignment.MIDDLE_CENTER)
    const.dxf.flags |= 2  # ATTRIB_CONST
    assert const.is_const


async def test_explode_keeps_a_constant_attdef_that_has_no_attrib_as_text(backend):
    """The AutoCAD-authored shape: a constant attribute has no ATTRIB on the
    reference, and ``virtual_entities()`` skips ATTDEF, so its text used to
    vanish with a clean audit. The TEXT lands where ``add_auto_attribs``
    would have put the ATTRIB (mirrored + rotated reference)."""
    _define_block_with_constant_attdef(backend)
    ref = backend._msp().add_blockref(
        "TBC", (20, 10), dxfattribs={"xscale": -1.0, "yscale": 1.0, "rotation": 30.0}
    )
    ref.add_auto_attribs({"TAG": "P-101"})
    expected = {a.dxf.tag: a.dxfattribs() for a in ref.attribs}["CONST"]
    ref.delete_attrib("CONST")  # AutoCAD writes no ATTRIB for a constant attribute
    assert [a.dxf.tag for a in ref.attribs] == ["TAG"], "the premise"

    result = await backend.block_explode(ref.dxf.handle)

    assert [(await backend.entity_get(h)).type for h in result["inserted_handles"]] == ["LINE"]
    texts = {}
    for handle in result["attribute_texts"]:
        raw = backend._doc.entitydb[handle]
        assert raw.dxftype() == "TEXT"
        texts[raw.dxf.text] = raw
    assert set(texts) == {"P-101", "ACME"}
    acme = texts["ACME"]
    for key in ("insert", "align_point", "extrusion"):
        assert tuple(acme.dxf.get(key)) == pytest.approx(tuple(expected[key])), key
    assert acme.dxf.rotation == pytest.approx(expected["rotation"])
    assert acme.dxf.height == expected["height"] == 2.0
    assert acme.get_align_enum().name == "MIDDLE_CENTER"
    assert acme.dxf.invisible == 0
    assert backend._doc.audit().errors == []


async def test_explode_does_not_duplicate_a_constant_attdef_that_already_has_an_attrib(backend):
    """The MCP-authored shape: ``block_insert`` attaches an ATTRIB for the
    constant ATTDEF too, so it is converted once, not once per source."""
    _define_block_with_constant_attdef(backend)
    ref = await backend.block_insert("TBC", 20, 10, attributes={"TAG": "P-101"})
    raw_ref = backend._doc.entitydb[ref.handle]
    assert sorted(a.dxf.tag for a in raw_ref.attribs) == ["CONST", "TAG"], "the premise"

    result = await backend.block_explode(ref.handle)

    values = sorted(backend._doc.entitydb[h].dxf.text for h in result["attribute_texts"])
    assert values == ["ACME", "P-101"]
    assert backend._doc.audit().errors == []


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


# ── Task 2 (review): multi-line attributes, xrefs and MINSERTs ───────────────


def _embed_multi_line(attrib, lines: list[str], insert, char_height: float = 2.5):
    """Give ``attrib`` (ATTRIB or ATTDEF) an embedded MTEXT the way ezdxf's
    ``embed_mtext`` does; ``dxf.text`` then holds only the first line."""
    from ezdxf.entities import MText

    mtext = MText.new(dxfattribs={"char_height": char_height, "insert": insert})
    mtext.text = "\\P".join(lines)
    attrib.embed_mtext(mtext)
    assert attrib.has_embedded_mtext_entity and attrib.dxf.text == lines[0], "the premise"


async def test_explode_keeps_a_multi_line_attrib_as_mtext_with_every_line(backend):
    """A multi-line ATTRIB carries its content in an embedded MTEXT; ``dxf.text``
    is only the first line (ezdxf) or ``Line1\\PLine2`` (AutoCAD), so a TEXT
    either drops the other lines or renders literal ``\\P``. BURST emits an
    MTEXT for a multi-line attribute."""
    blk = backend._doc.blocks.new(name="MLB")
    blk.add_line((0, 0), (10, 0))
    blk.add_attdef("NOTE", insert=(0, 3), text="x", dxfattribs={"height": 2.5})
    blk.add_attdef("HIDDEN", insert=(0, -3), text="x", dxfattribs={"height": 2.0})
    ref = await backend.block_insert(
        "MLB",
        10,
        10,
        scale_x=2.0,
        scale_y=2.0,
        rotation=30.0,
        attributes={"NOTE": "line one", "HIDDEN": "h"},
    )
    raw_ref = backend._doc.entitydb[ref.handle]
    attribs = {a.dxf.tag: a for a in raw_ref.attribs}
    lines = ["LINE ONE", "LINE TWO", "LINE THREE"]
    _embed_multi_line(attribs["NOTE"], lines, attribs["NOTE"].dxf.insert, char_height=5.0)
    attribs["NOTE"].dxf.layer = "PID-TAG"
    _embed_multi_line(attribs["HIDDEN"], ["H1", "H2"], attribs["HIDDEN"].dxf.insert)
    attribs["HIDDEN"].is_invisible = True
    expected = attribs["NOTE"].virtual_mtext_entity()

    result = await backend.block_explode(ref.handle)

    assert [(await backend.entity_get(h)).type for h in result["inserted_handles"]] == ["LINE"]
    assert len(result["attribute_texts"]) == 2
    by_text = {}
    for handle in result["attribute_texts"]:
        raw = backend._doc.entitydb[handle]
        assert raw.dxftype() == "MTEXT", "a multi-line attribute bursts into an MTEXT"
        by_text[raw.text] = raw
    note = by_text["\\P".join(lines)]
    assert note.plain_text(split=True) == lines
    assert tuple(note.dxf.insert) == pytest.approx(tuple(expected.dxf.insert))
    assert note.dxf.char_height == expected.dxf.char_height == 5.0
    assert note.dxf.attachment_point == expected.dxf.attachment_point
    assert note.dxf.layer == "PID-TAG", "the ATTRIB's graphic properties ride along"
    assert note.dxf.invisible == 0
    assert note.get_layout() is backend._doc.modelspace()
    assert by_text["H1\\PH2"].dxf.invisible == 1, "an invisible attribute stays invisible"
    with pytest.raises(RuntimeError, match="not found"):
        await backend.entity_get(ref.handle)
    assert backend._doc.audit().errors == []


async def test_explode_keeps_a_multi_line_constant_attdef_as_mtext(backend):
    """The constant twin: no ATTRIB on the reference, the value lives in the
    definition's ATTDEF, and that ATTDEF is multi-line."""
    blk = backend._doc.blocks.new(name="MLC")
    blk.add_line((0, 0), (10, 0))
    const = blk.add_attdef("CONST", insert=(5, -3), text="x", dxfattribs={"height": 2.0})
    _embed_multi_line(const, ["ACME", "LTD"], (5, -3, 0), char_height=2.0)
    const.dxf.flags |= 2  # ATTRIB_CONST
    ref = backend._msp().add_blockref("MLC", (20, 10), dxfattribs={"rotation": 90.0})
    assert list(ref.attribs) == [], "the premise: AutoCAD writes no ATTRIB for a constant"
    expected = const.virtual_mtext_entity().transform(ref.matrix44())

    result = await backend.block_explode(ref.dxf.handle)

    (handle,) = result["attribute_texts"]
    raw = backend._doc.entitydb[handle]
    assert raw.dxftype() == "MTEXT"
    assert raw.plain_text(split=True) == ["ACME", "LTD"]
    assert tuple(raw.dxf.insert) == pytest.approx(tuple(expected.dxf.insert))
    assert raw.get_rotation() == pytest.approx(90.0)
    assert backend._doc.audit().errors == []


async def test_explode_refuses_an_xref_before_writing(backend):
    """``virtual_entities()`` of an xref INSERT yields nothing (the geometry is
    in the other file), so exploding it deleted the reference with ``ok: True``
    and an empty ``inserted_handles``. AutoCAD refuses to explode an xref."""
    backend._doc.add_xref_def("C:/x/detail.dwg", "XREF_DETAIL")
    xref = backend._msp().add_blockref("XREF_DETAIL", (5, 5))
    before = len(list(backend._msp()))
    with pytest.raises(RuntimeError, match="external reference 'XREF_DETAIL'"):
        await backend.block_explode(xref.dxf.handle)
    assert len(list(backend._msp())) == before
    assert backend._doc.entitydb[xref.dxf.handle].is_alive


async def test_explode_refuses_a_minsert_before_writing(backend):
    """``virtual_entities()`` resolves only the first cell of a MINSERT grid
    (``multi_insert()`` yields all of them), so a 2x3 grid exploded into one
    cell and five vanished with a clean audit. AutoCAD refuses to explode a
    MINSERT; so does this."""
    cell = backend._doc.blocks.new(name="CELL")
    cell.add_circle((0, 0), 1)
    grid = backend._msp().add_blockref(
        "CELL",
        (0, 0),
        dxfattribs={"row_count": 2, "column_count": 3, "row_spacing": 5, "column_spacing": 5},
    )
    assert grid.mcount == 6, "the premise"
    before = len(list(backend._msp()))
    with pytest.raises(RuntimeError, match=r"MINSERT \(2x3 grid of 'CELL'\)"):
        await backend.block_explode(grid.dxf.handle)
    assert len(list(backend._msp())) == before
    assert backend._doc.entitydb[grid.dxf.handle].is_alive


async def test_explode_of_a_single_cell_grid_is_still_an_ordinary_insert(backend):
    """A ``row_count``/``column_count`` of 1 is ``mcount`` 1 -- a plain INSERT
    wearing MINSERT fields -- and must still explode."""
    cell = backend._doc.blocks.new(name="CELL1")
    cell.add_circle((0, 0), 1)
    single = backend._msp().add_blockref(
        "CELL1",
        (0, 0),
        dxfattribs={"row_count": 1, "column_count": 1, "row_spacing": 5, "column_spacing": 5},
    )
    assert single.mcount == 1
    result = await backend.block_explode(single.dxf.handle)
    assert [(await backend.entity_get(h)).type for h in result["inserted_handles"]] == ["CIRCLE"]


async def test_com_explode_turns_a_multi_line_attrib_into_mtext(com):
    """``TextString`` of a multi-line attribute is one flattened string; the
    content is ``MTextAttributeContent`` and the box ``MTextBoundaryWidth``.
    The TEXT path would render ``\\P`` literally, so BURST adds an MTEXT:
    height, style and frame first, then the angle, then the attachment the
    ATTRIB's alignment names, anchored at its alignment point."""
    backend, document, space = com
    note = _FakeObject(
        "AcDbAttribute",
        "A1",
        TagString="NOTE",
        TextString="LINE ONE LINE TWO",
        MTextAttribute=True,
        MTextAttributeContent="LINE ONE\\PLINE TWO",
        MTextBoundaryWidth=40.0,
        InsertionPoint=(7.0, 12.7, 0.0),
        Alignment=6,  # acAlignmentTopLeft: the MTEXT attachment is the alignment point
        TextAlignmentPoint=(7.0, 15.2, 0.0),
        Height=5.0,
        Rotation=0.5236,
        StyleName="ISO",
        Layer="PID-TAG",
        Color=3,
        Invisible=False,
    )
    plain = _FakeObject(
        "AcDbAttribute",
        "A2",
        TagString="TAG",
        TextString="P-101",
        MTextAttribute=False,
        InsertionPoint=(1.0, 2.0, 0.0),
        Height=2.5,
        Rotation=0.0,
        Layer="0",
        Invisible=False,
    )
    line = _FakeObject("AcDbLine", "L1")
    ref = _FakeObject(
        "AcDbBlockReference",
        "6D",
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (note, plain),
        Explode=lambda: (line,),
    )
    document.objects["6D"] = ref

    result = await backend.block_explode("6D")

    assert result["inserted_handles"] == ["L1"]
    assert result["attribute_texts"] == ["T1", "T2"]
    assert space.calls == [
        ("AddMText", ((7.0, 15.2, 0.0), 40.0, "LINE ONE\\PLINE TWO")),
        ("AddText", ("P-101", (1.0, 2.0, 0.0), 2.5)),
    ]
    assert space.texts[0].ObjectName == "AcDbMText"
    writes = [
        (name, tuple(value.value) if hasattr(value, "value") else value)
        for name, value in space.texts[0].writes
    ]
    assert writes == [
        ("Height", 5.0),
        ("StyleName", "ISO"),
        ("Rotation", 0.5236),
        ("AttachmentPoint", 1),  # acAttachmentPointTopLeft
        ("Layer", "PID-TAG"),
        ("Color", 3),
    ]
    assert ref.deleted is True


async def test_com_explode_multi_line_attrib_takes_the_frame_before_the_angle(com):
    backend, document, space = com
    note = _FakeObject(
        "AcDbAttribute",
        "A1",
        TagString="NOTE",
        TextString="A B",
        MTextAttribute=True,
        MTextAttributeContent="A\\PB",
        InsertionPoint=(-25.0, 13.0, 0.0),
        Alignment=0,
        Normal=(0.0, 0.0, -1.0),
        Height=2.5,
        Rotation=3.1416,
        Layer="0",
        Invisible=True,
    )
    ref = _FakeObject(
        "AcDbBlockReference",
        "7E",
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (note,),
        Explode=lambda: (),
    )
    document.objects["7E"] = ref

    result = await backend.block_explode("7E")

    assert result["attribute_texts"] == ["T1"]
    assert space.calls == [("AddMText", ((-25.0, 13.0, 0.0), 0.0, "A\\PB"))], (
        "no MTextBoundaryWidth exposed: a zero (unbounded) box"
    )
    writes = [
        (name, tuple(value.value) if hasattr(value, "value") else value)
        for name, value in space.texts[0].writes
    ]
    assert writes == [
        ("Height", 2.5),
        ("Normal", (0.0, 0.0, -1.0)),
        ("Rotation", 3.1416),
        ("InsertionPoint", (-25.0, 13.0, 0.0)),
        ("AttachmentPoint", 7),  # acAttachmentPointBottomLeft for a baseline-left ATTRIB
        ("Layer", "0"),
        ("Visible", False),
    ]


async def test_com_explode_refuses_an_xref_before_explode(com):
    """ActiveX ``Explode()`` raises on an xref; the headless engine used to
    delete it. Both name the refusal before anything is dispatched."""
    backend, document, space = com
    document.Blocks.blocks["XREF_DETAIL"] = _FakeBlock("XREF_DETAIL", is_xref=True)
    dispatched = []

    def _explode():
        dispatched.append("Explode")
        return ()

    ref = _FakeObject(
        "AcDbBlockReference",
        "8A",
        Name="XREF_DETAIL",
        OwnerID=1,
        GetAttributes=lambda: (),
        Explode=_explode,
    )
    document.objects["8A"] = ref
    with pytest.raises(RuntimeError, match="external reference 'XREF_DETAIL'"):
        await backend.block_explode("8A")
    assert dispatched == [] and space.calls == [] and ref.deleted is False


async def test_com_explode_refuses_a_minsert_by_name_before_explode(com):
    """A MINSERT is ``AcDbMInsertBlock``; it used to fail the INSERT check with
    the misleading 'not a block reference'."""
    backend, document, space = com
    dispatched = []

    def _explode():
        dispatched.append("Explode")
        return ()

    grid = _FakeObject(
        "AcDbMInsertBlock",
        "9B",
        Name="CELL",
        OwnerID=1,
        Rows=2,
        Columns=3,
        GetAttributes=lambda: (),
        Explode=_explode,
    )
    document.objects["9B"] = grid
    with pytest.raises(RuntimeError, match=r"MINSERT \(2x3 grid of 'CELL'\)"):
        await backend.block_explode("9B")
    assert dispatched == [] and space.calls == [] and grid.deleted is False
