"""block_define: a definition with ATTDEFs from typed specs, on both engines."""

from __future__ import annotations

import types

import pytest

pytestmark = pytest.mark.asyncio

VALVE = [
    {"type": "polyline", "points": [[-4, -2], [-4, 2], [0, 0]], "closed": True},
    {"type": "polyline", "points": [[4, -2], [4, 2], [0, 0]], "closed": True},
    {"type": "circle", "cx": 0, "cy": 0, "r": 1.5},
    {"type": "arc", "cx": 0, "cy": 2, "r": 2, "start_deg": 0, "end_deg": 180},
    {"type": "line", "x1": 0, "y1": 0, "x2": 0, "y2": 4},
    {"type": "text", "text": "M", "x": 0, "y": 6, "height": 2, "align": "middle_center"},
    {"type": "solid", "points": [[0, 0], [-3, 1.2], [-3, -1.2]]},
]
ATTDEFS = [
    {"tag": "TAG", "x": 0, "y": 5.5, "height": 2.5, "align": "center"},
    {"tag": "LINK", "x": 0, "y": -5, "height": 2.0, "invisible": True, "default": "L-1"},
]


# ── headless engine ─────────────────────────────────────────────────────────


async def test_defines_geometry_and_attdefs(backend):
    result = await backend.block_define("PID_VALVE_TEST", VALVE, ATTDEFS)
    assert result == {
        "ok": True,
        "name": "PID_VALVE_TEST",
        "entity_count": 7,
        "attdef_count": 2,
        "replaced": False,
        "layers_created": [],
        "backend": "ezdxf",
    }
    blk = backend._doc.blocks.get("PID_VALVE_TEST")
    types_ = sorted(e.dxftype() for e in blk)
    assert types_ == [
        "ARC",
        "ATTDEF",
        "ATTDEF",
        "CIRCLE",
        "LINE",
        "LWPOLYLINE",
        "LWPOLYLINE",
        "SOLID",
        "TEXT",
    ]
    attdefs = {e.dxf.tag: e for e in blk if e.dxftype() == "ATTDEF"}
    assert attdefs["LINK"].is_invisible is True
    assert attdefs["LINK"].dxf.text == "L-1"
    assert attdefs["TAG"].is_invisible is False
    for e in blk:
        assert e.dxf.layer == "0"
        assert e.dxf.color == 0, "ByBlock colour so the INSERT's layer drives it"


async def test_insert_after_define_carries_the_attributes(backend):
    await backend.block_define("PID_VALVE_TEST", VALVE, ATTDEFS)
    ref = await backend.block_insert("PID_VALVE_TEST", 10, 10, attributes={"TAG": "HV-101"})
    assert await backend.block_get_attributes(ref.handle) == {"TAG": "HV-101", "LINK": "L-1"}


async def test_name_clash_is_refused_unless_overwrite(backend):
    await backend.block_define("PID_X", VALVE)
    with pytest.raises(ValueError, match="overwrite"):
        await backend.block_define("PID_X", VALVE)
    result = await backend.block_define("PID_X", VALVE[:1], overwrite=True)
    assert result["replaced"] is True and result["entity_count"] == 1
    assert len(list(backend._doc.blocks.get("PID_X"))) == 1


async def test_overwrite_keeps_existing_inserts_pointing_at_the_name(backend):
    await backend.block_define("PID_X", VALVE)
    ref = await backend.block_insert("PID_X", 0, 0)
    await backend.block_define(
        "PID_X", [{"type": "circle", "cx": 0, "cy": 0, "r": 9}], overwrite=True
    )
    info = await backend.entity_get(ref.handle)
    assert info.properties["block_name"] == "PID_X"
    virtual = list(backend._doc.entitydb[ref.handle].virtual_entities())
    assert [e.dxftype() for e in virtual] == ["CIRCLE"]


async def test_invalid_request_writes_nothing(backend):
    with pytest.raises(TypeError, match=r"entities\[1\]"):
        await backend.block_define("PID_BAD", [VALVE[0], {"type": "circle", "cx": 0, "cy": 0}])
    assert "PID_BAD" not in backend._doc.blocks
    with pytest.raises(ValueError, match="empty"):
        await backend.block_define("PID_EMPTY", [])
    assert "PID_EMPTY" not in backend._doc.blocks


async def test_anonymous_names_are_refused(backend):
    with pytest.raises(ValueError, match="anonymous"):
        await backend.block_define("*U9", VALVE)


async def test_solid_uses_bowtie_order(backend):
    await backend.block_define(
        "PID_SOLID", [{"type": "solid", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}]
    )
    solid = next(e for e in backend._doc.blocks.get("PID_SOLID") if e.dxftype() == "SOLID")
    assert tuple(solid.dxf.vtx2)[:2] == (0.0, 1.0)
    assert tuple(solid.dxf.vtx3)[:2] == (1.0, 1.0)


# ── live engine, against a fake ActiveX surface ──────────────────────────────


class _Recorder:
    """Records every method call made on it, returns a styled child."""

    def __init__(self, name):
        self.Name = name
        self.calls: list[tuple[str, tuple]] = []
        self.Count = 0

    def __getattr__(self, attr):
        if attr.startswith("Add"):

            def _call(*args):
                self.calls.append((attr, args))
                return types.SimpleNamespace()

            return _call
        raise AttributeError(attr)


class _FakeBlocks:
    def __init__(self):
        self.blocks: dict[str, _Recorder] = {}

    def Add(self, base_point, name):
        self.blocks[name] = _Recorder(name)
        self.blocks[name].base_point = tuple(base_point.value)
        return self.blocks[name]

    def Item(self, name):
        if name not in self.blocks:
            raise RuntimeError(f"no block {name}")
        return self.blocks[name]


@pytest.fixture
def com_backend(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    document = types.SimpleNamespace(Blocks=_FakeBlocks())
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    monkeypatch.setattr(module, "_regen", lambda: None)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, document


async def test_com_issues_one_activex_call_per_primitive_and_attdef(com_backend):
    backend, document = com_backend
    result = await backend.block_define("PID_VALVE_TEST", VALVE, ATTDEFS, 1.0, 2.0)
    assert result["ok"] is True and result["backend"] == "com" and result["replaced"] is False
    block = document.Blocks.blocks["PID_VALVE_TEST"]
    assert block.base_point == (1.0, 2.0, 0.0)
    names = [name for name, _ in block.calls]
    assert names == [
        "AddLightWeightPolyline",
        "AddLightWeightPolyline",
        "AddCircle",
        "AddArc",
        "AddLine",
        "AddText",
        "AddSolid",
        "AddAttribute",
        "AddAttribute",
    ]
    tag_call = block.calls[7][1]
    link_call = block.calls[8][1]
    assert tag_call[0] == 2.5 and tag_call[1] == 0 and tag_call[4] == "TAG"
    assert link_call[1] == 1, "invisible ATTDEF uses acAttributeModeInvisible"
    assert link_call[5] == "L-1"


async def test_com_refuses_a_clash_without_overwrite(com_backend):
    backend, document = com_backend
    await backend.block_define("PID_X", VALVE[:1])
    with pytest.raises(ValueError, match="overwrite"):
        await backend.block_define("PID_X", VALVE[:1])


async def test_com_validates_before_touching_activex(com_backend):
    backend, document = com_backend
    with pytest.raises(TypeError):
        await backend.block_define("PID_BAD", [{"type": "circle", "cx": 0}])
    assert document.Blocks.blocks == {}
