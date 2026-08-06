"""Paper-space layouts, viewports, layout PDF export and opt-in 3D solids."""

from __future__ import annotations

from importlib.util import find_spec

import ezdxf
import pytest
import pytest_asyncio

import config
import server
from backends.base import UnsupportedCapabilityError
from backends.ezdxf_backend import EzdxfBackend

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def backend():
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    yield b
    await b.disconnect()


async def test_layout_list_reports_model_and_current(backend):
    result = await backend.layout_list()
    assert result["ok"] is True
    assert "Model" in result["layouts"]
    assert isinstance(result["current"], str)


async def test_layout_create_and_duplicate_rejected(backend):
    created = await backend.layout_create("A3-Sheet")
    assert created["ok"] is True
    listing = await backend.layout_list()
    assert "A3-Sheet" in listing["layouts"]
    duplicate = await backend.layout_create("A3-Sheet")
    assert duplicate["ok"] is False


async def test_layout_set_current_roundtrip(backend):
    await backend.layout_create("A3-Sheet")
    switched = await backend.layout_set_current("A3-Sheet")
    assert switched["ok"] is True
    assert (await backend.layout_list())["current"] == "A3-Sheet"
    back = await backend.layout_set_current("Model")
    assert back["ok"] is True
    missing = await backend.layout_set_current("NOPE")
    assert missing["ok"] is False


async def test_viewport_create_scaled(backend, tmp_path):
    await backend.layout_create("A3-Sheet")
    result = await backend.viewport_create(
        "A3-Sheet",
        center_x=200,
        center_y=140,
        width=180,
        height=120,
        view_center_x=50,
        view_center_y=25,
        scale=0.5,
    )
    assert result["ok"] is True
    assert result["view_height"] == pytest.approx(240.0)  # height / scale

    exported = tmp_path / "layout_roundtrip.dxf"
    save = await backend.drawing_export_dxf(str(exported))
    assert save["ok"] is True
    doc = ezdxf.readfile(str(exported))
    viewports = doc.layouts.get("A3-Sheet").query("VIEWPORT")
    assert len(viewports) >= 1
    created = [vp for vp in viewports if vp.dxf.handle == result["handle"]]
    assert created, "created viewport handle must survive the DXF roundtrip"
    assert created[0].dxf.view_height == pytest.approx(240.0)


async def test_viewport_rejects_model_and_bad_scale(backend):
    bad_layout = await backend.viewport_create("Model", 0, 0, 10, 10, 0, 0, 1.0)
    assert bad_layout["ok"] is False
    await backend.layout_create("A3-Sheet")
    bad_scale = await backend.viewport_create("A3-Sheet", 0, 0, 10, 10, 0, 0, 0.0)
    assert bad_scale["ok"] is False


@pytest.mark.skipif(find_spec("matplotlib") is None, reason="matplotlib not installed")
async def test_export_pdf_layout(backend, tmp_path):
    await backend.layout_create("A3-Sheet")
    await backend.entity_create_line(0, 0, 100, 0)
    target = tmp_path / "layout.pdf"
    result = await backend.drawing_export_pdf(str(target), layout="A3-Sheet")
    assert result["ok"] is True
    assert result["layout"] == "A3-Sheet"
    assert "note" in result  # honest viewport-projection limitation
    assert target.is_file() and target.stat().st_size > 0
    missing = await backend.drawing_export_pdf(str(tmp_path / "x.pdf"), layout="NOPE")
    assert missing["ok"] is False


async def test_solids_unsupported_headlessly(backend):
    """T0.7: these refuse by *raising*, so the refusal middleware can see them.

    They used to return ``{"ok": False, "capability": "solid_3d"}``, which left
    ``is_error`` False on a call that did nothing and put the refusal outside
    the one place that turns a capability boundary into a wire payload. The
    payload itself is unchanged — ``to_dict()`` still yields exactly what the
    old return value did.
    """
    calls = [
        backend.solid_box(0, 0, 0, 10, 10, 10),
        backend.solid_cylinder(0, 0, 0, 5, 10),
        backend.solid_extrude("FF", 10),
        backend.solid_revolve("FF", 0, 0, 0, 1),
        backend.solid_boolean("FF", "FE", "union"),
    ]
    for call in calls:
        with pytest.raises(UnsupportedCapabilityError) as excinfo:
            await call
        assert excinfo.value.capability == "solid_3d"
        assert excinfo.value.to_dict()["ok"] is False
        assert excinfo.value.to_dict()["capability"] == "solid_3d"


async def test_capability_map_reports_paper_space_and_solids(backend):
    features = backend.capabilities().to_dict()["features"]
    assert features["paper_space"]["supported"] is True
    # Not False: the headless renderer does project model content through
    # viewports. See tests/test_viewport_ops.py for the measurement.
    assert features["viewport_render"]["supported"] is True
    assert features["solid_3d"]["supported"] is False


class TestSolidToolGating:
    @pytest.mark.asyncio
    async def test_solid_tools_hidden_without_enable_3d(self, monkeypatch):
        monkeypatch.setattr(config.settings, "enable_3d", False)
        info = await server._apply_tool_profile("full")
        assert server.SOLID_TOOL_NAMES <= set(info["disabled_tools"])
        await server._apply_tool_profile("full")

    @pytest.mark.asyncio
    async def test_solid_tools_visible_with_enable_3d(self, monkeypatch):
        monkeypatch.setattr(config.settings, "enable_3d", True)
        info = await server._apply_tool_profile("full")
        assert not (server.SOLID_TOOL_NAMES & set(info["disabled_tools"]))
        monkeypatch.setattr(config.settings, "enable_3d", False)
        await server._apply_tool_profile("full")
