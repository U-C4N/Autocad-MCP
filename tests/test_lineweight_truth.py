"""Lineweight truth (plan 1.8-01, rival claim 4.5).

DXF stores a lineweight as hundredths of a millimetre plus three sentinels:
-1 ByLayer, -2 ByBlock, -3 Default (the LWDEFAULT setting). The schemas of
``entity_set_properties`` and ``layer_create`` used to say "-3=ByLayer", so a
model that set -3 on 22 entities reported them as "by-layer" and nothing on
the server could see the difference. These tests hold:

* no registered tool's schema or description maps -3 to ByLayer;
* one parser (``backends.base.parse_lineweight``) takes an integer or the
  names ``ByLayer`` / ``ByBlock`` / ``Default`` on every lineweight input, and
  refuses ByLayer / ByBlock on a layer by name, before anything is written;
* ``lineweight="ByLayer"`` lands as DXF -1 on both engines (COM faked);
* entity rows say which lineweight the entity carries (``lineweight_name``).
"""

from __future__ import annotations

import json
import re
import types

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends.base import (
    InvalidLineweightError,
    lineweight_name,
    parse_lineweight,
)
from backends.ezdxf_backend import EzdxfBackend

#: "-3 = ByLayer", "-3=ByLayer", "-3 (ByLayer)", "ByLayer (-3)", "ByLayer=-3" ...
_MINUS3_IS_BYLAYER = re.compile(
    r"-3\s*(?:=|:|\(|is|means)\s*\(?\s*by\s*-?\s*layer"
    r"|by\s*-?\s*layer\s*(?:=|:|\()\s*-3",
    re.IGNORECASE,
)

#: The tools whose lineweight input the plan names.
LINEWEIGHT_TOOLS = ("entity_set_properties", "layer_create", "layer_modify")


@pytest_asyncio.fixture
async def headless():
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    return backend


@pytest_asyncio.fixture
async def served(monkeypatch, headless):
    async def _make():
        return headless

    monkeypatch.setattr(server, "_make_backend", _make)
    async with Client(server.mcp) as client:
        yield client, headless


# -- the schemas ---------------------------------------------------------------


async def _registered_tools():
    return await server.mcp._list_tools()


@pytest.mark.asyncio
async def test_no_registered_tool_maps_minus_three_to_bylayer():
    offenders = []
    for tool in await _registered_tools():
        text = json.dumps(tool.parameters) + "\n" + (tool.description or "")
        for match in _MINUS3_IS_BYLAYER.finditer(text):
            offenders.append(f"{tool.name}: {match.group(0)!r}")
    assert offenders == []


@pytest.mark.asyncio
async def test_the_detector_catches_the_shipped_mistake():
    """The regex must have caught 1.7.0's two descriptions, or the scan proves nothing."""
    assert _MINUS3_IS_BYLAYER.search("Lineweight in 0.01mm units (-3=ByLayer, -2=ByBlock)")
    assert _MINUS3_IS_BYLAYER.search("Lineweight (-3=ByLayer, 0=0.00mm, 13=0.13mm)")
    assert not _MINUS3_IS_BYLAYER.search("-1 ByLayer, -2 ByBlock, -3 Default")


@pytest.mark.parametrize("name", LINEWEIGHT_TOOLS)
@pytest.mark.asyncio
async def test_lineweight_inputs_take_the_names_and_say_minus_one_is_bylayer(name):
    tools = {tool.name: tool for tool in await _registered_tools()}
    schema = tools[name].parameters["properties"]["lineweight"]
    encoded = json.dumps(schema)
    for word in ("ByLayer", "ByBlock", "Default"):
        assert f'"{word}"' in encoded, (name, word)
    assert re.search(r"-1\W+ByLayer", encoded), encoded
    assert re.search(r"-3\W+Default", encoded), encoded


@pytest.mark.asyncio
async def test_batch_tools_name_the_lineweight_words():
    tools = {tool.name: tool for tool in await _registered_tools()}
    for name in ("entity_batch_create", "entity_batch_modify"):
        text = json.dumps(tools[name].parameters) + (tools[name].description or "")
        assert "lineweight" in text and "ByLayer" in text, name


# -- the parser ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("ByLayer", -1),
        ("bylayer", -1),
        ("BYBLOCK", -2),
        ("Default", -3),
        (" default ", -3),
        (-1, -1),
        (-2, -2),
        (-3, -3),
        (0, 0),
        (25, 25),
        (0.25, 25),  # millimetres, the engineering layer sets' spelling
        ("50", 50),
    ],
)
def test_parse_lineweight_on_an_entity(value, expected):
    assert parse_lineweight(value) == expected


@pytest.mark.parametrize("value", ["thick", "by layer", "", True, [25]])
def test_parse_lineweight_refuses_an_unknown_value_by_name(value):
    with pytest.raises(InvalidLineweightError) as caught:
        parse_lineweight(value)
    message = str(caught.value)
    for word in ("ByLayer", "ByBlock", "Default"):
        assert word in message


@pytest.mark.parametrize("value", ["ByLayer", "ByBlock", -1, -2, "bylayer"])
def test_a_layer_cannot_be_bylayer_or_byblock(value):
    with pytest.raises(InvalidLineweightError) as caught:
        parse_lineweight(value, target="layer")
    message = str(caught.value)
    assert "layer" in message
    assert "ByLayer" in message or "ByBlock" in message
    assert caught.value.target == "layer"


@pytest.mark.parametrize(("value", "expected"), [("Default", -3), (-3, -3), (50, 50), (0.5, 50)])
def test_a_layer_takes_default_or_a_value(value, expected):
    assert parse_lineweight(value, target="layer") == expected


@pytest.mark.parametrize(
    ("value", "name"),
    [
        (-1, "ByLayer"),
        (-2, "ByBlock"),
        (-3, "Default"),
        (0, "0.00 mm"),
        (25, "0.25 mm"),
        (None, None),
    ],
)
def test_lineweight_name(value, name):
    assert lineweight_name(value) == name


# -- headless ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headless_entity_bylayer_name_lands_as_minus_one(headless):
    line = await headless.entity_create_line(0, 0, 10, 0)
    await headless.entity_set_properties(line.handle, lineweight=-3)
    assert headless._doc.entitydb[line.handle].dxf.lineweight == -3
    await headless.entity_set_properties(line.handle, lineweight="ByLayer")
    assert headless._doc.entitydb[line.handle].dxf.lineweight == -1


@pytest.mark.asyncio
async def test_headless_layer_refuses_bylayer_before_writing(headless):
    with pytest.raises(InvalidLineweightError):
        await headless.layer_create("LW-REFUSED", lineweight="ByLayer")
    assert "LW-REFUSED" not in headless._doc.layers
    await headless.layer_create("LW-OK", lineweight="Default")
    assert headless._doc.layers.get("LW-OK").dxf.lineweight == -3
    with pytest.raises(InvalidLineweightError):
        await headless.layer_modify("LW-OK", lineweight=-2)
    assert headless._doc.layers.get("LW-OK").dxf.lineweight == -3


@pytest.mark.asyncio
async def test_through_the_tools(served):
    client, backend = served
    line = await backend.entity_create_line(0, 0, 10, 0)
    await client.call_tool(
        "entity_set_properties", {"handle": line.handle, "lineweight": "ByLayer"}
    )
    assert backend._doc.entitydb[line.handle].dxf.lineweight == -1

    refused = await client.call_tool(
        "layer_create", {"name": "LW-TOOL", "lineweight": "ByBlock"}, raise_on_error=False
    )
    assert refused.is_error is True
    assert "ByBlock" in refused.content[0].text
    assert "LW-TOOL" not in backend._doc.layers

    await client.call_tool("layer_create", {"name": "LW-TOOL", "lineweight": "Default"})
    await client.call_tool("layer_modify", {"name": "LW-TOOL", "lineweight": 35})
    assert backend._doc.layers.get("LW-TOOL").dxf.lineweight == 35


@pytest.mark.asyncio
async def test_batch_create_and_modify_take_a_lineweight(served):
    client, backend = served
    created = (
        await client.call_tool(
            "entity_batch_create",
            {
                "entities": [
                    {"type": "line", "x1": 0, "y1": 0, "x2": 5, "y2": 0, "lineweight": "Default"},
                    {"type": "line", "x1": 0, "y1": 1, "x2": 5, "y2": 1, "lineweight": "thick"},
                ]
            },
        )
    ).structured_content
    assert created["created"] == 1
    (error,) = created["errors"]
    assert error["index"] == 1 and "ByLayer" in error["error"]
    handle = created["entities"][0]["handle"]
    assert backend._doc.entitydb[handle].dxf.lineweight == -3
    assert len(backend._doc.modelspace()) == 1  # the refused line was never drawn

    await client.call_tool(
        "entity_batch_modify",
        {"operations": [{"handle": handle, "action": "set_properties", "lineweight": "ByLayer"}]},
    )
    assert backend._doc.entitydb[handle].dxf.lineweight == -1


@pytest.mark.asyncio
async def test_entity_rows_carry_the_lineweight_name(headless):
    line = await headless.entity_create_line(0, 0, 10, 0)
    assert (await headless.entity_get(line.handle)).lineweight_name == "ByLayer"
    await headless.entity_set_properties(line.handle, lineweight=-3)
    assert (await headless.entity_get(line.handle)).lineweight_name == "Default"
    await headless.entity_set_properties(line.handle, lineweight=30)
    (row,) = await headless.entity_list()
    assert row.lineweight_name == "0.30 mm"


# -- live engine, faked --------------------------------------------------------


class _FakeLayers:
    def __init__(self):
        self.added: list[str] = []
        self.items: dict[str, types.SimpleNamespace] = {}

    def Add(self, name):  # noqa: N802 - ActiveX spelling
        self.added.append(name)
        layer = types.SimpleNamespace(
            Name=name,
            color=7,
            Linetype="Continuous",
            Lineweight=-3,
            LayerOn=True,
            Freeze=False,
            Lock=False,
        )
        self.items[name] = layer
        return layer

    def Item(self, name):  # noqa: N802
        return self.items[name]


@pytest.fixture
def com(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    entity = types.SimpleNamespace(Handle="A1", Lineweight=-3)
    layers = _FakeLayers()
    document = types.SimpleNamespace(
        Name="Part.dwg",
        FullName="C:/work/Part.dwg",
        Layers=layers,
        ActiveLayer=types.SimpleNamespace(Name="0"),
        HandleToObject=lambda handle: entity,
    )
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, document, entity


@pytest.mark.asyncio
async def test_com_entity_bylayer_name_lands_as_minus_one(com):
    backend, _document, entity = com
    await backend.entity_set_properties("A1", lineweight="ByLayer")
    assert entity.Lineweight == -1
    await backend.entity_set_properties("A1", lineweight="Default")
    assert entity.Lineweight == -3


def test_com_entity_rows_carry_the_lineweight_name():
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    entity = types.SimpleNamespace(
        ObjectName="AcDbLine",
        StartPoint=(0.0, 0.0, 0.0),
        EndPoint=(10.0, 0.0, 0.0),
        Length=10.0,
        Handle="A1",
        Layer="GEOMETRY",
        color=256,
        Linetype="ByLayer",
        Visible=True,
        Lineweight=-3,
    )
    assert module._entity_info(entity).lineweight_name == "Default"
    entity.Lineweight = -1
    assert module._entity_info(entity).lineweight_name == "ByLayer"


@pytest.mark.asyncio
async def test_com_layer_refuses_bylayer_before_any_com_call(com):
    backend, document, _entity = com
    with pytest.raises(InvalidLineweightError):
        await backend.layer_create("LW-REFUSED", lineweight="ByLayer")
    assert document.Layers.added == []
    info = await backend.layer_create("LW-OK", lineweight="Default")
    assert document.Layers.items["LW-OK"].Lineweight == -3
    assert info.lineweight == -3
    with pytest.raises(InvalidLineweightError):
        await backend.layer_modify("LW-OK", lineweight="ByBlock")
    assert document.Layers.items["LW-OK"].Lineweight == -3
