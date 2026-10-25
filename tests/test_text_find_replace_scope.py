"""``text_find_replace`` searches the same places on both engines (spec section 5, Task 12).

The live engine walked ``ModelSpace`` alone: an attribute on a sheet, a title
block field or a block definition's ATTDEF default was replaced headlessly and
never found live. The headless engine had its own gap: it read the ATTRIBs of
model-space references only, so an attribute on a paper layout or on a
reference nested in a block definition was missed. Both now search
``TEXT_SEARCH_SCOPE``: model space, every paper layout, every block definition
that is not a layout block or an xref, and the ATTRIBs of every reference
found in any of them.

The live engine is exercised through fakes that carry only members measured on
AutoCAD 2026 (Task 12's probe): ``Layouts.Count / Item(i)``, ``Layout.TabOrder``
/ ``Block``, ``Blocks.Count / Item(i)``, ``Block.IsLayout / IsXRef / Count /
Item(j)``, entity ``ObjectName / Handle / Layer / TextString``,
``GetAttributes()``.
"""

from __future__ import annotations

import re

import pytest

from backends.contracts.entity_modification import TEXT_SEARCH_SCOPE

pytestmark = pytest.mark.asyncio

#: The one logical drawing both engines are given, as (DXF type, value): model
#: space TEXT, the ATTRIB of a model-space reference, paper-layout MTEXT, the
#: ATTRIB of a paper-layout reference, the ATTDEF default and a TEXT inside
#: block B, the ATTRIB of a reference to B nested inside block C.
EXPECTED = sorted(
    [
        ("TEXT", "OLD m", "NEW m"),
        ("ATTRIB", "OLD a1", "NEW a1"),
        ("MTEXT", "OLD p", "NEW p"),
        ("ATTRIB", "OLD a2", "NEW a2"),
        ("ATTDEF", "OLD d", "NEW d"),
        ("TEXT", "OLD b", "NEW b"),
        ("ATTRIB", "OLD n", "NEW n"),
    ]
)

_DXF_NAME = {
    "AcDbText": "TEXT",
    "AcDbMText": "MTEXT",
    "AcDbAttribute": "ATTRIB",
    "AcDbAttributeDefinition": "ATTDEF",
}


def _rows(result: dict, names: dict | None = None) -> list[tuple[str, str, str]]:
    names = names or {}
    return sorted(
        (names.get(row["type"], row["type"]), row["before"], row["after"])
        for row in result["entities"]
    )


# -- the headless engine --------------------------------------------------------


def _build_headless(backend):
    doc = backend._doc
    block_b = doc.blocks.new("B")
    block_b.add_attdef("TAG", (0, 0), text="OLD d")
    block_b.add_text("OLD b", dxfattribs={"insert": (0, 5)})
    model = doc.modelspace()
    model.add_text("OLD m", dxfattribs={"insert": (0, 0)})
    model.add_blockref("B", (10, 0)).add_auto_attribs({"TAG": "OLD a1"})
    paper = doc.layouts.get("Layout1")
    paper.add_mtext("OLD p", dxfattribs={"insert": (5, 5)})
    paper.add_blockref("B", (20, 20)).add_auto_attribs({"TAG": "OLD a2"})
    block_c = doc.blocks.new("C")
    block_c.add_blockref("B", (0, 0)).add_auto_attribs({"TAG": "OLD n"})


async def test_a_paper_layout_attribute_is_found_headlessly(backend):
    doc = backend._doc
    doc.blocks.new("P").add_attdef("TAG", (0, 0), text="")
    ref = doc.layouts.get("Layout1").add_blockref("P", (0, 0))
    ref.add_auto_attribs({"TAG": "OLD sheet"})

    result = await backend.text_find_replace("OLD", "NEW")

    assert result["replaced"] == 1
    assert ref.attribs[0].dxf.text == "NEW sheet"


async def test_a_nested_reference_attribute_is_found_headlessly(backend):
    doc = backend._doc
    doc.blocks.new("INNER").add_attdef("TAG", (0, 0), text="")
    nested = doc.blocks.new("OUTER").add_blockref("INNER", (0, 0))
    nested.add_auto_attribs({"TAG": "OLD nested"})

    result = await backend.text_find_replace("OLD", "NEW")

    assert result["replaced"] == 1
    assert nested.attribs[0].dxf.text == "NEW nested"


async def test_the_headless_engine_covers_the_whole_scope(backend):
    _build_headless(backend)

    result = await backend.text_find_replace("OLD", "NEW", dry_run=True)

    assert _rows(result) == EXPECTED
    assert result["searched_scope"] == list(TEXT_SEARCH_SCOPE)


async def test_an_xref_block_is_not_searched_headlessly(backend):
    doc = backend._doc
    doc.add_xref_def("other.dxf", "XR")
    xref = doc.blocks.get("XR")
    xref.add_text("OLD x", dxfattribs={"insert": (0, 0)})
    assert xref.block_record.is_xref

    result = await backend.text_find_replace("OLD", "NEW")

    assert result["replaced"] == 0


# -- the live engine, through measured fakes --------------------------------------


class _Text:
    def __init__(self, object_name, handle, text, layer="0"):
        self.ObjectName, self.Handle, self.TextString, self.Layer = (
            object_name,
            handle,
            text,
            layer,
        )


class _Reference:
    def __init__(self, handle, attributes, layer="0"):
        self.ObjectName, self.Handle, self.Layer = "AcDbBlockReference", handle, layer
        self._attributes = tuple(attributes)

    def GetAttributes(self):  # noqa: N802 - the ActiveX member name
        return self._attributes


class _Block:
    def __init__(self, name, items, *, is_layout=False, is_xref=False):
        self.Name, self.IsLayout, self.IsXRef = name, is_layout, is_xref
        self._items = list(items)

    @property
    def Count(self):  # noqa: N802
        return len(self._items)

    def Item(self, index):  # noqa: N802
        return self._items[index]


class _Layout:
    def __init__(self, tab_order, block):
        self.TabOrder, self.Block = tab_order, block


class _Collection:
    def __init__(self, items):
        self._items = list(items)

    @property
    def Count(self):  # noqa: N802
        return len(self._items)

    def Item(self, index):  # noqa: N802
        return self._items[index]


class _Doc:
    def __init__(self):
        model = _Block(
            "*Model_Space",
            [
                _Text("AcDbText", "10", "OLD m"),
                _Reference("11", [_Text("AcDbAttribute", "12", "OLD a1")]),
                _Text("AcDbLine", "13", ""),
            ],
            is_layout=True,
        )
        paper = _Block(
            "*Paper_Space",
            [
                _Text("AcDbMText", "20", "OLD p"),
                _Reference("21", [_Text("AcDbAttribute", "22", "OLD a2")]),
            ],
            is_layout=True,
        )
        block_b = _Block(
            "B",
            [_Text("AcDbAttributeDefinition", "30", "OLD d"), _Text("AcDbText", "31", "OLD b")],
        )
        block_c = _Block("C", [_Reference("40", [_Text("AcDbAttribute", "41", "OLD n")])])
        xref = _Block("XR", [_Text("AcDbText", "50", "OLD x")], is_xref=True)
        # Collection order puts Layout1 first: the walk sorts on TabOrder.
        self.Layouts = _Collection([_Layout(1, paper), _Layout(0, model)])
        self.Blocks = _Collection([model, paper, block_b, block_c, xref])


async def test_the_live_walk_covers_the_same_scope():
    from backends.com_backend import _com_text_find_replace

    doc = _Doc()
    pattern = re.compile(re.escape("OLD"))

    changed = _com_text_find_replace(doc, pattern, "NEW", None, True)

    rows = sorted((_DXF_NAME[r["type"]], r["before"], r["after"]) for r in changed)
    assert rows == EXPECTED
    assert [r["handle"] for r in changed][:2] == ["10", "12"], "model space (TabOrder 0) first"
    assert "50" not in {r["handle"] for r in changed}, "an xref's text is another file's"
    assert doc.Layouts.Item(1).Block.Item(0).TextString == "OLD m", "a dry run writes nothing"


async def test_the_live_walk_writes_through_textstring_and_honours_the_layer():
    from backends.com_backend import _com_text_find_replace

    doc = _Doc()
    doc.Blocks.Item(2).Item(1).Layer = "NOTES"
    pattern = re.compile(re.escape("OLD"))

    changed = _com_text_find_replace(doc, pattern, "NEW", "NOTES", False)

    assert [(r["handle"], r["after"]) for r in changed] == [("31", "NEW b")]
    assert doc.Blocks.Item(2).Item(1).TextString == "NEW b"
    assert doc.Blocks.Item(2).Item(0).TextString == "OLD d", "other layers untouched"


async def test_the_live_tool_reads_the_bound_document_and_reports_its_scope(monkeypatch):
    from backends import com_backend as module

    doc = _Doc()
    backend = module.ComBackend()
    monkeypatch.setattr(module, "_acad_doc", lambda: doc)

    async def _inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _inline)

    result = await backend.text_find_replace("OLD", "NEW", dry_run=True)

    assert result["ok"] is True and result["replaced"] == len(EXPECTED)
    assert _rows(result, _DXF_NAME) == EXPECTED
    assert result["searched_scope"] == list(TEXT_SEARCH_SCOPE)


async def test_both_engines_answer_the_same_rows(backend):
    from backends.com_backend import _com_text_find_replace

    _build_headless(backend)
    headless = await backend.text_find_replace("OLD", "NEW", dry_run=True)
    live = _com_text_find_replace(_Doc(), re.compile(re.escape("OLD")), "NEW", None, True)

    assert _rows(headless) == sorted((_DXF_NAME[r["type"]], r["before"], r["after"]) for r in live)
