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


class _FakeMText(_FakeObject):
    """``AddMText``'s MTEXT. ActiveX ``AttachmentPoint`` (and ``Normal``) keep
    the text *body* where it is and relocate ``InsertionPoint`` to the new
    corner (verified live on AutoCAD 2026: attachment 7 on a two-line note
    anchored at (10,10) moved the anchor to (10,-3.33)), so a writer that sets
    the anchor first and the attachment second leaves the body TopLeft-at-
    anchor. The fake displaces the anchor on those writes -- without recording
    a write -- so a test asserts where the anchor *ended up*, not only what
    was sent.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in ("AttachmentPoint", "Normal") and hasattr(self, "InsertionPoint"):
            x, y, z = self.InsertionPoint.value
            moved = types.SimpleNamespace(varianttype="VT_ARRAY|VT_R8", value=[x + 1.0, y - 2.0, z])
            object.__setattr__(self, "InsertionPoint", moved)


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
    """The ActiveX layer table: ``Item`` and ``Add`` match names case-insensitively,
    and ``Add`` of an existing name hands the existing record back instead of
    creating a second one — so ``added`` records only the layers that really
    appeared."""

    def __init__(self, *names: str):
        self.names: dict[str, str] = {n.lower(): n for n in ("0", *names)}
        self.added: list[str] = []

    def Item(self, name):
        if name.lower() not in self.names:
            raise RuntimeError(f"no layer {name}")
        return types.SimpleNamespace(Name=self.names[name.lower()])

    def Add(self, name):
        if name.lower() not in self.names:
            self.names[name.lower()] = name
            self.added.append(name)
        return types.SimpleNamespace(Name=self.names[name.lower()])


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
        obj = _FakeMText("AcDbMText", f"T{len(self.calls)}", InsertionPoint=point)
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
    attdef = _FakeObject("AcDbAttributeDefinition", "D1", Constant=False)
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
        GetConstantAttributes=lambda: (),
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
        GetConstantAttributes=lambda: (),
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
        GetConstantAttributes=lambda: (),
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
        color=3,
    )
    ref = _FakeObject(
        "AcDbBlockReference",
        "6D",
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (attrib,),
        GetConstantAttributes=lambda: (),
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
        "color": 3,
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
        GetConstantAttributes=lambda: (),
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
        Invisible=False,
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
    ATTRIB's alignment names, and the alignment point *last* -- ActiveX's
    ``AttachmentPoint`` relocates the anchor (see ``_FakeMText``), and a
    BottomRight note anchored before it landed two lines low and a box width
    right (live, AutoCAD 2026). A TopLeft attribute is the ``AddMText``
    default and could never show that, so this one is BottomRight."""
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
        Alignment=14,  # acAlignmentBottomRight: the MTEXT attachment is the alignment point
        TextAlignmentPoint=(7.0, 15.2, 0.0),
        Height=5.0,
        Rotation=0.5236,
        StyleName="ISO",
        Layer="PID-TAG",
        color=3,
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
        GetConstantAttributes=lambda: (),
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
        ("AttachmentPoint", 9),  # acAttachmentPointBottomRight
        ("InsertionPoint", (7.0, 15.2, 0.0)),  # re-asserted after the attachment moved it
        ("Layer", "PID-TAG"),
        ("color", 3),
    ]
    assert tuple(space.texts[0].InsertionPoint.value) == (7.0, 15.2, 0.0), (
        "the MTEXT ends up anchored at the ATTRIB's alignment point"
    )
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
        GetConstantAttributes=lambda: (),
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
        ("AttachmentPoint", 7),  # acAttachmentPointBottomLeft for a baseline-left ATTRIB
        ("InsertionPoint", (-25.0, 13.0, 0.0)),  # last: the frame AND the attachment moved it
        ("Layer", "0"),
        ("Visible", False),
    ]
    assert tuple(space.texts[0].InsertionPoint.value) == (-25.0, 13.0, 0.0)


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
        GetConstantAttributes=lambda: (),
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
        GetConstantAttributes=lambda: (),
        Explode=_explode,
    )
    document.objects["9B"] = grid
    with pytest.raises(RuntimeError, match=r"MINSERT \(2x3 grid of 'CELL'\)"):
        await backend.block_explode("9B")
    assert dispatched == [] and space.calls == [] and grid.deleted is False


# ── Task 2 (review 2): the multi-line writer, and members ezdxf cannot explode ─


async def _insert_multi_line(backend):
    """An INSERT of a LINE + ATTDEF block whose ATTRIB is multi-line (embedded
    MTEXT ``OLD ONE\\POLD TWO``), built the way the tests above build one."""
    blk = backend._doc.blocks.new(name="MLW")
    blk.add_line((0, 0), (10, 0))
    blk.add_attdef("NOTE", insert=(0, 3), text="x", dxfattribs={"height": 2.5})
    ref = await backend.block_insert("MLW", 10, 10, rotation=30.0, attributes={"NOTE": "first"})
    attrib = backend._doc.entitydb[ref.handle].attribs[0]
    _embed_multi_line(attrib, ["OLD ONE", "OLD TWO"], attrib.dxf.insert)
    return ref, attrib


async def test_get_attributes_reports_every_line_of_a_multi_line_attrib(backend):
    """``dxf.text`` of an ezdxf-embedded multi-line ATTRIB is only the first
    line; AutoCAD's ``TextString`` (what the COM engine reports) is the whole
    ``Line1\\PLine2`` content, and so is the headless reader now."""
    ref, _ = await _insert_multi_line(backend)
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": "OLD ONE\\POLD TWO"}


async def test_set_attributes_rewrites_the_embedded_mtext_of_a_multi_line_attrib(backend):
    """The writer used to set only ``dxf.text``, so after an edit the reader
    said ``NEW VALUE`` while the explode burst the pre-edit MTEXT -- and an
    R2010 save (no embedded MTEXT exported) carried a value the exploded
    drawing never showed. One value, on every surface."""
    ref, attrib = await _insert_multi_line(backend)
    keys = ("insert", "align_point", "rotation", "height", "halign", "valign", "style")
    before = {key: attrib.dxf.get(key) for key in keys}

    result = await backend.block_set_attributes(ref.handle, {"NOTE": "NEW VALUE"})

    assert result == {"ok": True, "updated_tags": ["NOTE"]}
    assert attrib.has_embedded_mtext_entity, "still a multi-line attribute"
    assert attrib.virtual_mtext_entity().text == "NEW VALUE"
    assert attrib.dxf.text == "NEW VALUE"
    after = {key: attrib.dxf.get(key) for key in keys}
    assert after == before, "rewriting the content does not move the attribute"
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": "NEW VALUE"}
    exploded = await backend.block_explode(ref.handle)
    (handle,) = exploded["attribute_texts"]
    raw = backend._doc.entitydb[handle]
    assert raw.dxftype() == "MTEXT" and raw.text == "NEW VALUE"
    assert backend._doc.audit().errors == []


async def test_set_attributes_multi_line_value_keeps_every_line(backend):
    """A ``\\P`` value on a multi-line attribute is what AutoCAD's
    ``TextString`` write takes; every line survives, and the empty value
    empties the attribute rather than leaving the old content behind."""
    ref, attrib = await _insert_multi_line(backend)

    await backend.block_set_attributes(ref.handle, {"NOTE": "NEW ONE\\PNEW TWO\\PNEW THREE"})

    mtext = attrib.virtual_mtext_entity()
    assert mtext.plain_text(split=True) == ["NEW ONE", "NEW TWO", "NEW THREE"]
    assert attrib.dxf.text == "NEW ONE\\PNEW TWO\\PNEW THREE", (
        "group 1 carries the whole value, AutoCAD's own convention -- ezdxf's "
        "first-line mirror is what an R2010 save would keep"
    )
    assert await backend.block_get_attributes(ref.handle) == {
        "NOTE": "NEW ONE\\PNEW TWO\\PNEW THREE"
    }

    await backend.block_set_attributes(ref.handle, {"NOTE": ""})
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": ""}
    assert attrib.virtual_mtext_entity().text == "" and attrib.dxf.text == ""

    exploded = await backend.block_explode(ref.handle)
    (handle,) = exploded["attribute_texts"]
    assert backend._doc.entitydb[handle].text == ""
    assert backend._doc.audit().errors == []


async def test_set_attributes_on_a_multi_line_attrib_survives_an_r2010_round_trip(
    backend, tmp_path
):
    """The default document is R2010, where ezdxf exports no embedded MTEXT:
    the saved file carries only ``dxf.text``. ``set_mtext`` mirrors just the
    first line there, so a ``\\P`` value once reloaded as ``NEW ONE`` while the
    reader had promised three lines. The whole value goes into ``dxf.text``
    (AutoCAD's own convention), so the reloaded drawing keeps every line."""
    ref, _ = await _insert_multi_line(backend)
    assert backend._doc.dxfversion == "AC1024", "the premise"
    value = "NEW ONE\\PNEW TWO\\PNEW THREE"
    await backend.block_set_attributes(ref.handle, {"NOTE": value})
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": value}
    path = tmp_path / "ml.dxf"
    await backend.drawing_save_as(str(path))
    await backend.drawing_open(str(path))

    reloaded = next(e for e in backend._msp() if e.dxftype() == "INSERT")
    assert not reloaded.attribs[0].has_embedded_mtext_entity, "R2010 drops the embedded MTEXT"
    assert await backend.block_get_attributes(reloaded.dxf.handle) == {"NOTE": value}, (
        "the file carries what the reader promised before the save"
    )
    exploded = await backend.block_explode(reloaded.dxf.handle)
    (handle,) = exploded["attribute_texts"]
    raw = backend._doc.entitydb[handle]
    assert raw.dxftype() == "TEXT" and raw.dxf.text == value


async def test_set_attributes_multi_line_value_survives_an_r2018_round_trip(backend, tmp_path):
    """On R2018 the embedded MTEXT is exported too: both surfaces reload with
    every line and the reader still prefers the MTEXT."""
    ref, _ = await _insert_multi_line(backend)
    backend._doc.dxfversion = "AC1032"
    value = "NEW ONE\\PNEW TWO"
    await backend.block_set_attributes(ref.handle, {"NOTE": value})
    path = tmp_path / "ml2018.dxf"
    await backend.drawing_save_as(str(path))
    await backend.drawing_open(str(path))

    reloaded = next(e for e in backend._msp() if e.dxftype() == "INSERT")
    attrib = reloaded.attribs[0]
    assert attrib.has_embedded_mtext_entity, "R2018 keeps the embedded MTEXT"
    assert attrib.virtual_mtext_entity().plain_text(split=True) == ["NEW ONE", "NEW TWO"]
    assert attrib.dxf.text == value
    assert await backend.block_get_attributes(reloaded.dxf.handle) == {"NOTE": value}
    assert backend._doc.audit().errors == []


async def test_set_attributes_leaves_a_single_line_attrib_single_line(backend):
    _define_tagged_block(backend, "SL")
    ref = await backend.block_insert("SL", 0, 0, attributes={"TAG": "P-1"})
    await backend.block_set_attributes(ref.handle, {"TAG": "P-2"})
    attrib = backend._doc.entitydb[ref.handle].attribs[0]
    assert not attrib.has_embedded_mtext_entity and attrib.dxf.text == "P-2"
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "P-2"}


async def test_explode_refuses_members_ezdxf_cannot_explode_before_writing(backend):
    """``virtual_entities()`` drops every member it cannot copy or transform
    (OLE2FRAME, VIEWPORT) with a debug log, and a proxy entity without proxy
    graphics contributes nothing -- so a title block's OLE logo or a vertical
    product's proxy geometry used to be destroyed with ``ok: True`` and a
    clean audit. Refused by member before anything is written."""
    from ezdxf.entities import factory

    from backends.base import UnsupportedCapabilityError

    blk = backend._doc.blocks.new(name="PRX")
    blk.add_line((0, 0), (10, 0))
    for dxftype in ("ACAD_PROXY_ENTITY", "OLE2FRAME", "VIEWPORT"):
        blk.add_entity(factory.create_db_entry(dxftype, {}, backend._doc))
    ref = await backend.block_insert("PRX", 0, 0)
    before = [e.dxf.handle for e in backend._msp()]

    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.block_explode(ref.handle)

    assert excinfo.value.capability == "explode_opaque_members"
    message = str(excinfo.value)
    for name in ("ACAD_PROXY_ENTITY", "OLE2FRAME", "VIEWPORT", "'PRX'", "com"):
        assert name in message, message
    assert [e.dxf.handle for e in backend._msp()] == before, "nothing written"
    assert backend._doc.entitydb[ref.handle].is_alive


async def test_explode_still_bursts_a_block_every_member_of_which_it_can_explode(backend):
    """The refusal is per member, not per block: ordinary geometry keeps
    exploding, in definition order, under a uniform scale."""
    blk = backend._doc.blocks.new(name="PLAIN2")
    blk.add_line((0, 0), (10, 0))
    blk.add_circle((5, 5), 1)
    blk.add_hatch().paths.add_polyline_path([(0, 0), (1, 0), (1, 1)])
    ref = await backend.block_insert("PLAIN2", 0, 0, scale_x=2.0, scale_y=2.0)
    result = await backend.block_explode(ref.handle)
    types = [(await backend.entity_get(h)).type for h in result["inserted_handles"]]
    assert types == ["LINE", "CIRCLE", "HATCH"]


async def test_explode_opaque_members_is_declared_on_both_engines():
    from backends.com_backend import ComBackend
    from backends.ezdxf_backend import EzdxfBackend

    headless = EzdxfBackend().capabilities().features["explode_opaque_members"]
    assert headless.supported is False and "OLE2FRAME" in (headless.reason or "")
    assert ComBackend().capabilities().features["explode_opaque_members"].supported is True


# ── Task 2 (review 3): find_replace on a multi-line attribute; COM reads propagate ─


async def test_find_replace_rewrites_every_surface_of_a_multi_line_attrib(backend):
    """``text_find_replace`` matched and wrote only ``dxf.text`` of an ATTRIB,
    so on a multi-line attribute it reported ``replaced: 1, 'OLD ONE' ->
    'NEW ONE'`` while ``block_get_attributes`` still read ``OLD ONE\\POLD TWO``
    and ``block_explode`` burst it. The ATTRIB branch goes through the same
    reader and writer the attribute tools use: one value, every surface."""
    ref, attrib = await _insert_multi_line(backend)

    result = await backend.text_find_replace("OLD", "NEW")

    assert result["replaced"] == 1
    (entry,) = result["entities"]
    assert entry["type"] == "ATTRIB"
    assert entry["before"] == "OLD ONE\\POLD TWO", "the whole value is what was matched"
    assert entry["after"] == "NEW ONE\\PNEW TWO", "every line is replaced, not the first"
    assert attrib.has_embedded_mtext_entity, "still a multi-line attribute"
    assert attrib.virtual_mtext_entity().text == "NEW ONE\\PNEW TWO"
    assert attrib.dxf.text == "NEW ONE\\PNEW TWO"
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": "NEW ONE\\PNEW TWO"}
    exploded = await backend.block_explode(ref.handle)
    (handle,) = exploded["attribute_texts"]
    raw = backend._doc.entitydb[handle]
    assert raw.dxftype() == "MTEXT" and raw.text == "NEW ONE\\PNEW TWO"
    assert backend._doc.audit().errors == []


async def test_find_replace_matches_a_line_below_the_first_of_a_multi_line_attrib(backend):
    """A needle that only occurs on the second line was invisible to a search
    of ``dxf.text`` (the first line alone): ``replaced: 0`` on a value the
    reader plainly reports."""
    ref, attrib = await _insert_multi_line(backend)

    result = await backend.text_find_replace("TWO", "2")

    assert result["replaced"] == 1
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": "OLD ONE\\POLD 2"}
    assert attrib.virtual_mtext_entity().text == "OLD ONE\\POLD 2"


async def test_find_replace_dry_run_reports_the_whole_multi_line_value_and_writes_nothing(
    backend,
):
    ref, attrib = await _insert_multi_line(backend)

    result = await backend.text_find_replace("OLD", "NEW", dry_run=True)

    assert result["dry_run"] is True and result["replaced"] == 1
    assert result["entities"][0]["before"] == "OLD ONE\\POLD TWO"
    assert result["entities"][0]["after"] == "NEW ONE\\PNEW TWO"
    assert attrib.virtual_mtext_entity().text == "OLD ONE\\POLD TWO"
    assert attrib.dxf.text == "OLD ONE"
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": "OLD ONE\\POLD TWO"}


async def test_find_replace_rewrites_a_multi_line_attdef_inside_a_block_definition(backend):
    """The ATTDEF twin: the default a new INSERT is prompted with lives in the
    block definition, and a multi-line one keeps it in an embedded MTEXT."""
    blk = backend._doc.blocks.new(name="MLD")
    blk.add_line((0, 0), (10, 0))
    attdef = blk.add_attdef("NOTE", insert=(0, 3), text="x", dxfattribs={"height": 2.5})
    _embed_multi_line(attdef, ["OLD ONE", "OLD TWO"], (0, 3))

    result = await backend.text_find_replace("OLD", "NEW")

    assert result["replaced"] == 1 and result["entities"][0]["type"] == "ATTDEF"
    assert result["entities"][0]["after"] == "NEW ONE\\PNEW TWO"
    assert attdef.has_embedded_mtext_entity
    assert attdef.virtual_mtext_entity().text == "NEW ONE\\PNEW TWO"
    assert attdef.dxf.text == "NEW ONE\\PNEW TWO"
    assert backend._doc.audit().errors == []


async def test_find_replace_leaves_a_single_line_attrib_single_line(backend):
    _define_tagged_block(backend, "TB3")
    ref = await backend.block_insert("TB3", 0, 0, attributes={"TAG": "OLD-1"})
    attrib = backend._doc.entitydb[ref.handle].attribs[0]

    result = await backend.text_find_replace("OLD", "NEW")

    assert result["replaced"] == 1 and result["entities"][0]["after"] == "NEW-1"
    assert attrib.dxf.text == "NEW-1" and not attrib.has_embedded_mtext_entity
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "NEW-1"}


# ── Task 2 (review 4): a moved multi-line attribute keeps one placement ───────


def _placements(attrib) -> tuple[tuple, tuple]:
    """(ATTRIB anchor, embedded MTEXT insert), both WCS -- the two surfaces a
    multi-line attribute keeps its placement on."""
    anchor = attrib.ocs().to_wcs(attrib.get_placement()[1])
    return tuple(anchor), tuple(attrib.virtual_mtext_entity().dxf.insert)


async def _insert_multi_line_plain(backend, name: str = "MLK"):
    """The review's repro: unrotated INSERT at (10, 10) of LINE + ATTDEF NOTE,
    the ATTRIB made multi-line (``L1\\PL2``) at its own insert (10, 13)."""
    blk = backend._doc.blocks.new(name=name)
    blk.add_line((0, 0), (10, 0))
    blk.add_attdef("NOTE", insert=(0, 3), text="x", dxfattribs={"height": 2.5})
    ref = await backend.block_insert(name, 10, 10, attributes={"NOTE": "first"})
    attrib = backend._doc.entitydb[ref.handle].attribs[0]
    _embed_multi_line(attrib, ["L1", "L2"], attrib.dxf.insert)
    assert _placements(attrib) == ((10.0, 13.0, 0.0), (10.0, 13.0, 0.0)), "the premise"
    return ref, attrib


async def test_move_carries_the_embedded_mtext_of_a_multi_line_attrib(backend):
    """``Insert.translate`` moves each ATTRIB's ``insert``/``align_point`` and
    nothing else -- ezdxf's ``BaseAttrib`` overrides ``transform`` but not
    ``translate`` -- so after ``entity_move`` the embedded MTEXT still sat at
    the old place and ``block_explode`` burst it there: the LINE at (60, 60),
    the note 50 units away at (10, 13), with a clean audit."""
    ref, attrib = await _insert_multi_line_plain(backend)

    await backend.entity_move(ref.handle, 50, 50)

    assert _placements(attrib) == ((60.0, 63.0, 0.0), (60.0, 63.0, 0.0))
    assert attrib.dxf.text == "L1", "a move does not touch the value"
    result = await backend.block_explode(ref.handle)
    (line_handle,) = result["inserted_handles"]
    (note_handle,) = result["attribute_texts"]
    assert tuple(backend._doc.entitydb[line_handle].dxf.start) == (60.0, 60.0, 0.0)
    note = backend._doc.entitydb[note_handle]
    assert note.dxftype() == "MTEXT" and note.text == "L1\\PL2"
    assert tuple(note.dxf.insert) == (60.0, 63.0, 0.0), "the note moved with its symbol"
    assert backend._doc.audit().errors == []


async def test_set_attributes_after_a_move_leaves_the_attribute_where_the_move_put_it(backend):
    """``_write_attrib_value`` rebuilt the embedded MTEXT from the stale virtual
    entity and ``set_mtext`` re-placed the ATTRIB from it: a value edit after a
    move teleported the attribute from (60, 63) back to (10, 13) while the
    INSERT stayed at (60, 60). Before Task 2 the writer touched only
    ``dxf.text`` and the ATTRIB stayed put, so this was a regression."""
    ref, attrib = await _insert_multi_line_plain(backend)
    await backend.entity_move(ref.handle, 50, 50)

    await backend.block_set_attributes(ref.handle, {"NOTE": "NEW1\\PNEW2"})

    assert tuple(backend._doc.entitydb[ref.handle].dxf.insert) == (60.0, 60.0, 0.0)
    assert _placements(attrib) == ((60.0, 63.0, 0.0), (60.0, 63.0, 0.0))
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": "NEW1\\PNEW2"}
    assert attrib.dxf.text == "NEW1\\PNEW2"


async def test_find_replace_after_a_move_leaves_the_attribute_where_the_move_put_it(backend):
    ref, attrib = await _insert_multi_line_plain(backend)
    await backend.entity_move(ref.handle, 50, 50)

    result = await backend.text_find_replace("L1", "X1")

    assert result["replaced"] == 1
    assert _placements(attrib) == ((60.0, 63.0, 0.0), (60.0, 63.0, 0.0))
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": "X1\\PL2"}


async def test_copy_carries_the_embedded_mtext_of_a_multi_line_attrib(backend):
    """``entity_copy`` is ``copy()`` + ``translate()``: the copy's ATTRIB moved,
    its embedded MTEXT did not, and exploding the copy put the note on the
    original."""
    ref, original = await _insert_multi_line_plain(backend)

    copied = await backend.entity_copy(ref.handle, 50, 50)

    attrib = backend._doc.entitydb[copied.handle].attribs[0]
    assert _placements(attrib) == ((60.0, 63.0, 0.0), (60.0, 63.0, 0.0))
    assert _placements(original) == ((10.0, 13.0, 0.0), (10.0, 13.0, 0.0)), "the original stays"
    result = await backend.block_explode(copied.handle)
    (note_handle,) = result["attribute_texts"]
    note = backend._doc.entitydb[note_handle]
    assert note.text == "L1\\PL2" and tuple(note.dxf.insert) == (60.0, 63.0, 0.0)
    assert backend._doc.audit().errors == []


async def test_rectangular_array_carries_the_embedded_mtext_of_a_multi_line_attrib(backend):
    ref, _ = await _insert_multi_line_plain(backend)

    cells = await backend.entity_array_rectangular(ref.handle, 1, 3, 0, 20)

    for index, cell in enumerate(cells, start=1):
        attrib = backend._doc.entitydb[cell.handle].attribs[0]
        expected = (10.0 + 20.0 * index, 13.0, 0.0)
        assert _placements(attrib) == (expected, expected), f"cell {index}"


async def test_move_of_a_rotated_reference_carries_the_embedded_mtext(backend):
    """The rotated fixture the other multi-line tests use: the same delta on
    both surfaces, whatever the reference's frame."""
    ref, attrib = await _insert_multi_line(backend)
    anchor, mtext = _placements(attrib)
    assert anchor == pytest.approx(mtext), "the premise"

    await backend.entity_move(ref.handle, 5, -7, 3)

    moved_anchor, moved_mtext = _placements(attrib)
    delta = (5.0, -7.0, 3.0)
    assert moved_anchor == pytest.approx(tuple(a + d for a, d in zip(anchor, delta, strict=True)))
    assert moved_mtext == pytest.approx(moved_anchor)


async def test_explode_bursts_a_multi_line_attrib_at_the_attribs_own_placement(backend):
    """A drawing whose two surfaces already disagree (written by a tool that,
    like ``Insert.translate``, moved only the ATTRIB): the ATTRIB's own
    placement is the attribute's placement -- it is what ``entity_get`` reports
    and what a single-line attribute has -- so the burst MTEXT goes there, and
    a value write keeps it there instead of snapping to the stale MTEXT."""
    ref, attrib = await _insert_multi_line_plain(backend)
    attrib.dxf.insert = (40.0, 41.0, 0.0)
    attrib.dxf.align_point = (40.0, 41.0, 0.0)
    assert _placements(attrib) == ((40.0, 41.0, 0.0), (10.0, 13.0, 0.0)), "the premise"

    await backend.block_set_attributes(ref.handle, {"NOTE": "N1\\PN2"})
    assert _placements(attrib) == ((40.0, 41.0, 0.0), (40.0, 41.0, 0.0))

    result = await backend.block_explode(ref.handle)
    (note_handle,) = result["attribute_texts"]
    assert tuple(backend._doc.entitydb[note_handle].dxf.insert) == (40.0, 41.0, 0.0)


async def test_explode_of_a_stale_multi_line_attrib_reads_the_attrib_placement(backend):
    """The read-only twin: no write before the explode, the burst still lands
    on the ATTRIB's placement."""
    ref, attrib = await _insert_multi_line_plain(backend)
    attrib.dxf.insert = (40.0, 41.0, 0.0)
    attrib.dxf.align_point = (40.0, 41.0, 0.0)

    result = await backend.block_explode(ref.handle)

    (note_handle,) = result["attribute_texts"]
    assert tuple(backend._doc.entitydb[note_handle].dxf.insert) == (40.0, 41.0, 0.0)


async def _rotated(backend, handle):
    await backend.entity_rotate(handle, 0, 0, 90)
    return handle


async def _scaled(backend, handle):
    await backend.entity_scale(handle, 0, 0, 2.0)
    return handle


async def _mirrored(backend, handle):
    """``entity_mirror`` transforms a *copy* and deletes the original."""
    return (await backend.entity_mirror(handle, 0, 0, 0, 1, delete_original=True)).handle


async def _polar_cell(backend, handle):
    (cell,) = await backend.entity_array_polar(handle, 2, 180, 0, 0)
    await backend.entity_delete(handle)
    return cell.handle


@pytest.mark.parametrize(
    "op", [_rotated, _scaled, _mirrored, _polar_cell], ids=["rotate", "scale", "mirror", "polar"]
)
async def test_transform_keeps_the_whole_multi_line_value_in_group_1(backend, op, tmp_path):
    """The ``transform`` twin of the move defect: ezdxf's ``BaseAttrib.transform``
    goes through ``set_mtext``, which mirrors only the first line into
    ``dxf.text`` -- so a rotate, scale or mirror after ``block_set_attributes``
    turned ``A\\PB`` back into ``A`` on the surface an R2010 save keeps, and
    the reloaded drawing had lost every line but the first."""
    ref, attrib = await _insert_multi_line_plain(backend)
    await backend.block_set_attributes(ref.handle, {"NOTE": "A\\PB"})
    assert attrib.dxf.text == "A\\PB", "the premise"

    handle = await op(backend, ref.handle)

    attrib = backend._doc.entitydb[handle].attribs[0]
    anchor, mtext = _placements(attrib)
    assert anchor == pytest.approx(mtext), "the two surfaces still agree on placement"
    assert attrib.dxf.text == "A\\PB"
    assert await backend.block_get_attributes(handle) == {"NOTE": "A\\PB"}
    path = tmp_path / "ml-transformed.dxf"
    await backend.drawing_save_as(str(path))
    await backend.drawing_open(str(path))
    reloaded = next(e for e in backend._msp() if e.dxftype() == "INSERT")
    assert await backend.block_get_attributes(reloaded.dxf.handle) == {"NOTE": "A\\PB"}


async def test_insert_of_a_multi_line_attdef_keeps_the_whole_value_in_group_1(backend, tmp_path):
    """The chain the review names: a title block whose ATTDEF is multi-line
    (opened from an AutoCAD file), then ``block_insert``. ezdxf's
    ``add_auto_attribs`` embeds the MTEXT through ``embed_mtext``, which
    mirrors only the first line into ``dxf.text``, so an R2010 save of a
    fresh insert kept ``A`` of ``A\\PB``."""
    blk = backend._doc.blocks.new(name="MLT")
    blk.add_line((0, 0), (10, 0))
    attdef = blk.add_attdef("NOTE", insert=(0, 3), text="x", dxfattribs={"height": 2.5})
    _embed_multi_line(attdef, ["D1", "D2"], (0, 3))

    ref = await backend.block_insert("MLT", 10, 10, attributes={"NOTE": "A\\PB"})

    attrib = backend._doc.entitydb[ref.handle].attribs[0]
    assert attrib.has_embedded_mtext_entity and attrib.dxf.text == "A\\PB"
    assert _placements(attrib) == ((10.0, 13.0, 0.0), (10.0, 13.0, 0.0))
    assert await backend.block_get_attributes(ref.handle) == {"NOTE": "A\\PB"}
    path = tmp_path / "ml-insert.dxf"
    await backend.drawing_save_as(str(path))
    await backend.drawing_open(str(path))
    reloaded = next(e for e in backend._msp() if e.dxftype() == "INSERT")
    assert await backend.block_get_attributes(reloaded.dxf.handle) == {"NOTE": "A\\PB"}


async def test_move_of_a_single_line_attrib_reference_is_unchanged(backend):
    _define_tagged_block(backend, "SLM")
    ref = await backend.block_insert("SLM", 0, 0, attributes={"TAG": "P-1"})
    attrib = backend._doc.entitydb[ref.handle].attribs[0]

    await backend.entity_move(ref.handle, 5, 5)

    assert not attrib.has_embedded_mtext_entity
    assert tuple(attrib.dxf.insert) == (5.0, 8.0, 0.0) and attrib.dxf.text == "P-1"


def _boom(*_args):
    raise RuntimeError("RPC_E_CALL_REJECTED: the application is busy")


@pytest.mark.parametrize(
    "broken",
    ["GetAttributes", "GetConstantAttributes", "Invisible"],
    ids=["get_attributes", "get_constant_attributes", "invisible"],
)
async def test_com_explode_propagates_a_failing_read_before_explode(com, broken):
    """A failing ``GetAttributes()`` used to be swallowed (``attrs = ()``), and
    the burst went on to explode, delete every ATTDEF placeholder and the
    reference, and return ``ok: True, attribute_texts: []`` -- every value
    destroyed silently, the defect the BURST rule exists to remove. A
    transient ``RPC_E_CALL_REJECTED`` while AutoCAD is busy is enough to
    trigger it. The same for ``GetConstantAttributes()`` (a constant ATTDEF
    would then be deleted as a placeholder) and for ``Invisible`` (a hidden
    value would appear as visible text). Nothing has been written at that
    point, so the failure propagates and the drawing is untouched."""
    backend, document, space = com
    dispatched = []
    attdef = _FakeObject("AcDbAttributeDefinition", "D1", Constant=False)
    line = _FakeObject("AcDbLine", "L1")

    def _explode():
        dispatched.append("Explode")
        return (line, attdef)

    attrib_members = dict(
        TagString="TAG",
        TextString="P-101",
        InsertionPoint=(7.0, 15.2, 0.0),
        Height=5.0,
        Rotation=0.0,
        Layer="PID-TAG",
        Invisible=True,
    )
    if broken == "Invisible":
        attrib_members.pop("Invisible")  # the read raises AttributeError on the fake
    attrib = _FakeObject("AcDbAttribute", "A1", **attrib_members)
    ref_members = dict(
        Name="TB",
        OwnerID=1,
        GetAttributes=lambda: (attrib,),
        GetConstantAttributes=lambda: (),
        Explode=_explode,
    )
    if broken in ref_members:
        ref_members[broken] = _boom
    ref = _FakeObject("AcDbBlockReference", "2F", **ref_members)
    document.objects["2F"] = ref

    with pytest.raises((RuntimeError, AttributeError)):
        await backend.block_explode("2F")

    assert dispatched == [], "the read failed before Explode() was dispatched"
    assert space.calls == [] and ref.deleted is False and attdef.deleted is False


class _SpaceFailingSecondAddText(_FakeSpace):
    def AddText(self, text, point, height):
        if self.texts:
            raise RuntimeError("E_FAIL: AddText")
        return super().AddText(text, point, height)


async def test_com_explode_undoes_a_burst_that_fails_after_explode(com):
    """ActiveX ``Explode()`` leaves the reference in place, so a failure after
    it (here ``AddText`` on the second attribute; a constant ATTDEF whose
    members cannot be read is the other way in) is put back to that: the
    members the explode added and the TEXTs written so far are deleted, the
    reference stays, and the failure propagates rather than a half-burst
    symbol with ``ok: True``."""
    backend, document, space = com
    sheet = _SpaceFailingSecondAddText(name="Sheet")
    document.owners[2] = sheet
    first = _FakeObject(
        "AcDbAttribute",
        "A1",
        TagString="TAG",
        TextString="P-101",
        InsertionPoint=(7.0, 15.2, 0.0),
        Height=5.0,
        Rotation=0.0,
        Layer="0",
        Invisible=False,
    )
    second = _FakeObject(
        "AcDbAttribute",
        "A2",
        TagString="LINK",
        TextString="L-1",
        InsertionPoint=(1.0, 1.0, 0.0),
        Height=2.0,
        Rotation=0.0,
        Layer="0",
        Invisible=False,
    )
    placeholder = _FakeObject("AcDbAttributeDefinition", "D1", Constant=False)
    line = _FakeObject("AcDbLine", "L1")
    ref = _FakeObject(
        "AcDbBlockReference",
        "4C",
        Name="TB",
        OwnerID=2,
        GetAttributes=lambda: (first, second),
        GetConstantAttributes=lambda: (),
        Explode=lambda: (line, placeholder),
    )
    document.objects["4C"] = ref

    with pytest.raises(RuntimeError, match="E_FAIL: AddText"):
        await backend.block_explode("4C")

    assert ref.deleted is False, "the reference Explode() left in place is kept"
    assert line.deleted is True, "the member Explode() added is removed"
    assert placeholder.deleted is True
    assert [t.deleted for t in sheet.texts] == [True], "the TEXT written before the failure"
    assert space.calls == []


# ── Task 3: attribute values are validated before any write ──────────────────


@pytest.mark.parametrize(
    "value, fragment",
    [
        (None, "NoneType"),
        (True, "bool"),
        ({"a": 1}, "dict"),
        ([1, 2], "list"),
        (float("nan"), "finite"),
        ("two\nlines", "single line"),
    ],
)
async def test_insert_refuses_a_non_text_attribute_value_by_tag(backend, value, fragment):
    _define_tagged_block(backend)
    before = len(list(backend._msp()))
    with pytest.raises(TypeError, match=r"attributes\['TAG'\]") as exc:
        await backend.block_insert("TB", 0, 0, attributes={"TAG": value})
    assert fragment in str(exc.value)
    assert len(list(backend._msp())) == before, "a refused insert must not write"


async def test_insert_refuses_a_non_mapping_before_writing(backend):
    _define_tagged_block(backend)
    before = len(list(backend._msp()))
    with pytest.raises(TypeError, match="attributes must be an object"):
        await backend.block_insert("TB", 0, 0, attributes=[("TAG", "x")])
    assert len(list(backend._msp())) == before


async def test_insert_writes_numbers_as_plain_text(backend):
    _define_tagged_block(backend)
    ref = await backend.block_insert("TB", 0, 0, attributes={"TAG": 101})
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "101"}
    ref = await backend.block_insert("TB", 0, 0, attributes={"TAG": 2.5})
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "2.5"}


async def test_set_attributes_refuses_by_tag_and_leaves_the_value_alone(backend):
    _define_tagged_block(backend)
    ref = await backend.block_insert("TB", 0, 0, attributes={"TAG": "P-101"})
    with pytest.raises(TypeError, match=r"attributes\['TAG'\] must be a string or a number"):
        await backend.block_set_attributes(ref.handle, {"TAG": None})
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "P-101"}
    result = await backend.block_set_attributes(ref.handle, {"TAG": 7})
    assert result == {"ok": True, "updated_tags": ["TAG"]}
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "7"}


async def test_com_insert_refuses_before_insert_block_and_writes_numbers_as_text(com):
    backend, document, space = com
    space.attributes = {"TAG": ""}
    with pytest.raises(TypeError, match=r"attributes\['TAG'\]"):
        await backend.block_insert("TB", 0, 0, attributes={"TAG": None})
    assert space.calls == [], "the refusal fires before InsertBlock"

    await backend.block_insert("TB", 0, 0, attributes={"TAG": 101})
    assert [name for name, _ in space.calls] == ["InsertBlock"]
    attr = space.inserted[0].GetAttributes()[0]
    assert attr.TextString == "101" and ("TextString", "101") in attr.writes


async def test_com_set_attributes_refuses_before_touching_the_reference(com):
    backend, document, space = com
    attr = _FakeObject("AcDbAttribute", "A1", TagString="TAG", TextString="P-101")
    calls = []

    def get_attributes():
        calls.append("GetAttributes")
        return (attr,)

    document.objects["2F"] = _FakeObject(
        "AcDbBlockReference", "2F", Name="TB", GetAttributes=get_attributes
    )
    with pytest.raises(TypeError, match=r"attributes\['TAG'\]"):
        await backend.block_set_attributes("2F", {"TAG": True})
    assert calls == [] and attr.TextString == "P-101"
    assert await backend.block_set_attributes("2F", {"TAG": 2.5}) == {
        "ok": True,
        "updated_tags": ["TAG"],
    }
    assert attr.TextString == "2.5"


# ── Task 4: block_define validates the base point and the layer table ────────

CIRC = [{"type": "circle", "cx": 0, "cy": 0, "r": 1}]
ON_LAYER = [{"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "layer": "NOT_A_LAYER"}]


@pytest.mark.parametrize(
    "kwargs, key",
    [
        ({"base_x": float("nan")}, "base_x"),
        ({"base_y": float("inf")}, "base_y"),
        ({"base_x": "3"}, "base_x"),
        ({"base_y": True}, "base_y"),
        ({"base_x": 10**400}, "base_x"),
    ],
)
async def test_define_refuses_a_bad_base_point_before_writing(backend, kwargs, key):
    with pytest.raises(TypeError, match=f"'{key}'"):
        await backend.block_define("PID_BASE", CIRC, **kwargs)
    assert "PID_BASE" not in backend._doc.blocks


async def test_define_refuses_a_missing_layer_and_names_it(backend):
    with pytest.raises(ValueError, match="'NOT_A_LAYER' do not exist") as exc:
        await backend.block_define("PID_LYR", ON_LAYER)
    assert "create_layers=true" in str(exc.value)
    assert "PID_LYR" not in backend._doc.blocks
    assert "NOT_A_LAYER" not in backend._doc.layers


async def test_define_creates_the_layer_on_request_and_reports_it(backend):
    result = await backend.block_define("PID_LYR", ON_LAYER, create_layers=True)
    assert result["layers_created"] == ["NOT_A_LAYER"] and result["ok"] is True
    assert "NOT_A_LAYER" in backend._doc.layers
    member = next(iter(backend._doc.blocks.get("PID_LYR")))
    assert member.dxf.layer == "NOT_A_LAYER"


async def test_define_on_an_existing_layer_reports_nothing_created(backend):
    await backend.layer_create("PID-EQUIP")
    spec = [{**ON_LAYER[0], "layer": "PID-EQUIP"}]
    result = await backend.block_define("PID_OK", spec)
    assert result["layers_created"] == []
    result = await backend.block_define("PID_ZERO", CIRC)
    assert result["layers_created"] == [], "layer 0 always exists"


async def test_com_define_probes_layers_before_blocks_add(com):
    backend, document, space = com
    with pytest.raises(ValueError, match="'NOT_A_LAYER' do not exist"):
        await backend.block_define("PID_LYR", ON_LAYER)
    assert "PID_LYR" not in document.Blocks.blocks and document.Layers.added == []

    result = await backend.block_define("PID_LYR", ON_LAYER, create_layers=True)
    assert result["layers_created"] == ["NOT_A_LAYER"] and result["backend"] == "com"
    assert document.Layers.added == ["NOT_A_LAYER"]
    assert [name for name, _ in document.Blocks.blocks["PID_LYR"].calls] == ["AddLine"]


TWO_SPELLINGS = [{**ON_LAYER[0], "layer": "Alpha"}, {**ON_LAYER[0], "layer": "ALPHA"}]


async def test_define_names_a_case_folded_missing_layer_once(backend):
    """DXF layer names are case-insensitive: 'Alpha' and 'ALPHA' are one missing
    layer, refused once, under the first spelling, before anything is written."""
    with pytest.raises(ValueError, match=r"layer\(s\) 'Alpha' do not exist"):
        await backend.block_define("PID_CASE", TWO_SPELLINGS)
    assert "PID_CASE" not in backend._doc.blocks
    assert "Alpha" not in backend._doc.layers


async def test_define_creates_a_case_folded_layer_once(backend):
    """Without the case fold the second ``layers.add`` raised DXFTableEntryError
    after the first one had written — the layer stayed, the block never came."""
    result = await backend.block_define("PID_CASE", TWO_SPELLINGS, create_layers=True)
    assert result["layers_created"] == ["Alpha"] and result["entity_count"] == 2
    assert "PID_CASE" in backend._doc.blocks
    spellings = [
        layer.dxf.name for layer in backend._doc.layers if layer.dxf.name.lower() == "alpha"
    ]
    assert spellings == ["Alpha"]
    members = [member.dxf.layer for member in backend._doc.blocks.get("PID_CASE")]
    assert members == ["Alpha", "ALPHA"], "each primitive keeps the spelling it was given"


async def test_define_treats_an_existing_layer_case_insensitively(backend):
    await backend.layer_create("PID-EQUIP")
    spec = [{**ON_LAYER[0], "layer": "pid-equip"}]
    result = await backend.block_define("PID_OK", spec)
    assert result["layers_created"] == []


async def test_com_define_creates_a_case_folded_layer_once(com):
    backend, document, space = com
    with pytest.raises(ValueError, match=r"layer\(s\) 'Alpha' do not exist"):
        await backend.block_define("PID_CASE", TWO_SPELLINGS)
    assert "PID_CASE" not in document.Blocks.blocks and document.Layers.added == []

    result = await backend.block_define("PID_CASE", TWO_SPELLINGS, create_layers=True)
    assert result["layers_created"] == ["Alpha"] and result["entity_count"] == 2
    assert document.Layers.added == ["Alpha"]
    assert [name for name, _ in document.Blocks.blocks["PID_CASE"].calls] == ["AddLine", "AddLine"]


async def test_com_define_treats_an_existing_layer_case_insensitively(com):
    backend, document, space = com
    document.Layers.Add("PID-EQUIP")
    spec = [{**ON_LAYER[0], "layer": "pid-equip"}]
    result = await backend.block_define("PID_OK", spec)
    assert result["layers_created"] == [] and document.Layers.added == ["PID-EQUIP"]


async def test_com_define_refuses_a_bad_base_point_before_blocks_add(com):
    backend, document, space = com
    with pytest.raises(TypeError, match="'base_y'"):
        await backend.block_define("PID_BASE", CIRC, base_y=float("nan"))
    assert "PID_BASE" not in document.Blocks.blocks
    result = await backend.block_define("PID_BASE", CIRC, base_x=1.5, base_y=2.5)
    assert document.Blocks.blocks["PID_BASE"].base_point == (1.5, 2.5, 0.0)
    assert result["layers_created"] == []
