"""W1 review follow-up (Task 1, finding 4) - every pure read is ``readonly=True``.

Only ``drawing_info``, ``entity_get``, ``entity_list`` and ``entity_count``
passed the flag, so ``analysis_*``, ``layer_list``, ``entity_get_xdata``, the
``take_snapshot`` in-memory read and the critique reads still overran into a
quarantine that only a reopen lifts - for a call that cannot have changed the
drawing. The test below drives every tool whose ``@cad_tool`` card says
``cost="read"`` on a small headless drawing, with ``EzdxfBackend._async``
wrapped to record its ``readonly`` argument, and fails when one reaches
``_async`` without it.

A tool that reaches no ``_async`` at all would pass vacuously, so the ones that
legitimately never touch the document lock are named in ``NO_DOCUMENT_READ``;
every other read tool must reach ``_async`` at least once.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends import ezdxf_backend

pytestmark = pytest.mark.asyncio

#: Read tools that answer without taking the document lock: catalogues,
#: settings, process status, parsers, and the two interactive picks the
#: headless engine refuses by capability. Each is a reason, not a convenience.
NO_DOCUMENT_READ = {
    "system_status": "process state, answered without the lock on purpose",
    "system_capabilities": "the engine's capability map",
    "system_about": "the server's own surface",
    "system_variable_describe": "the static SYSVAR_CATALOG",
    "system_preferences_get": "live-only (capability preferences)",
    "user_pick_point": "live-only (capability interactive_prompt)",
    "user_select": "live-only (capability interactive_prompt)",
    "template_list": "the bundled layer templates",
    "drawing_template_list": "the bundled drawing templates",
    "plot_style_list": "the plot-style catalogue",
    "pid_symbol_list": "the P&ID catalogue authored in code",
    "pid_tag_parse": "the ISA-5.1 grammar, no drawing",
    "std_part_list": "the standard-part tables",
    "arch_catalogue_list": "the furniture/sanitary catalogue",
    "drawing_preflight": "checks an intent and its requirements, no drawing",
    "analysis_measure_distance": "arithmetic on the two points it is given",
    "analysis_measure_area": "arithmetic on the polygon it is given",
    "selection_get": "returns the backend's remembered selection",
}

#: Read tools that reach ``_async`` WITHOUT ``readonly=True`` on purpose.
#: Empty: every read-cost tool is a pure read of the document.
READONLY_EXCEPTIONS: dict[str, str] = {}


@pytest_asyncio.fixture
async def client(tmp_path):
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


async def _handle(client, tool: str, args: dict) -> str:
    result = await client.call_tool(tool, args)
    data = result.structured_content or {}
    return data.get("handle") or data.get("result", {}).get("handle")


async def test_every_read_cost_tool_reaches_the_engine_readonly(client, monkeypatch, tmp_path):
    calls: list[tuple[str, str, bool]] = []
    current = {"tool": "setup"}
    real_async = ezdxf_backend.EzdxfBackend._async

    async def recording_async(self, func, *a, readonly=False, **kw):
        calls.append((current["tool"], getattr(func, "__qualname__", repr(func)), readonly))
        return await real_async(self, func, *a, readonly=readonly, **kw)

    monkeypatch.setattr(ezdxf_backend.EzdxfBackend, "_async", recording_async)

    line = await _handle(client, "entity_create_line", {"x1": 0, "y1": 0, "x2": 100, "y2": 0})
    other = await _handle(client, "entity_create_line", {"x1": 100, "y1": 0, "x2": 100, "y2": 50})
    circle = await _handle(client, "entity_create_circle", {"cx": 50, "cy": 50, "radius": 10})
    await client.call_tool(
        "entity_create_text", {"text": "PUMP P-101", "x": 10, "y": 10, "height": 2.5}
    )
    reference = tmp_path / "reference.dxf"
    await client.call_tool("drawing_save_as", {"path": str(reference)})
    assert line and other and circle
    # The other half of the rule: a mutating call never passes readonly=True.
    mutating = {qualname.split(".")[1] for _tool, qualname, readonly in calls if not readonly}
    assert {"entity_create_line", "entity_create_circle", "drawing_save_as"} <= mutating
    assert [q for _t, q, readonly in calls if readonly and "create" in q] == []
    calls.clear()

    args: dict[str, dict] = {
        "selection_window": {"x1": -1, "y1": -1, "x2": 200, "y2": 200},
        "selection_polygon": {"points": [[-1, -1], [200, -1], [200, 200], [-1, 200]]},
        "entity_get": {"handle": line},
        "entity_get_xdata": {"handle": line},
        "block_get_attributes": {"handle": line},
        "block_find_references": {"name": "NO_SUCH_BLOCK"},
        "analysis_list_properties": {"handle": circle},
        "analysis_find_in_region": {"x1": -1, "y1": -1, "x2": 200, "y2": 200},
        "analysis_measure_distance": {"x1": 0, "y1": 0, "x2": 3, "y2": 4},
        "analysis_measure_area": {"points": [[0, 0], [1, 0], [1, 1]]},
        "analysis_measure_entity": {"handle": circle},
        "analysis_select_by_layer": {"layer_name": "0"},
        "analysis_select_by_type": {"entity_type": "LINE"},
        "system_get_variable": {"name": "INSUNITS"},
        "drawing_preflight": {"intent": "a plate"},
        "point_from_snap": {"handle": line, "snap": "end", "ref_x": 0, "ref_y": 0},
        "point_intersection": {"handle1": line, "handle2": other},
        "point_tangent": {"circle_handle": circle, "from_x": 100, "from_y": 100},
        "entity_select_smart": {"predicate": {"type": "LINE"}},
        "pid_tag_parse": {"tag": "FIC-101"},
        "user_pick_point": {"prompt": "pick"},
        "user_select": {"prompt": "select"},
        "drawing_scale_check": {"reference": str(reference)},
    }

    tools = await client.list_tools()
    read_tools = sorted(
        tool.name for tool in tools if ((tool.meta or {}).get("cad") or {}).get("cost") == "read"
    )
    assert len(read_tools) >= 60, read_tools
    outcomes: dict[str, str] = {}
    for name in read_tools:
        current["tool"] = name
        result = await client.call_tool(name, args.get(name, {}), raise_on_error=False)
        outcomes[name] = "error" if result.is_error else "ok"
    current["tool"] = None

    not_readonly = sorted(
        {
            f"{tool}: {qualname}"
            for tool, qualname, readonly in calls
            if not readonly and tool not in READONLY_EXCEPTIONS
        }
    )
    assert not_readonly == [], "read-cost tools reached _async without readonly=True:\n" + (
        "\n".join(not_readonly)
    )

    reached = {tool for tool, _qualname, _readonly in calls}
    silent = sorted(
        name for name in read_tools if name not in reached and name not in NO_DOCUMENT_READ
    )
    assert silent == [], f"read tools that never reached the engine: {silent} ({outcomes})"
    # The exemption list cannot go stale: a listed tool that does reach the
    # engine belongs under the readonly check, not beside it.
    stale = sorted(name for name in NO_DOCUMENT_READ if name in reached)
    assert stale == [], f"NO_DOCUMENT_READ names tools that do reach _async: {stale}"
    assert set(NO_DOCUMENT_READ) <= set(read_tools), "an exempt name is not a read tool"


async def test_a_grid_sysvar_read_creates_no_vport(backend):
    """``system_get_variable`` is now readonly, so it must not create the
    ``*Active`` VPORT ``_active_vport`` makes for the writers."""
    backend._doc.viewports.delete_config("*Active")
    assert await backend.system_get_variable("GRIDMODE") == 0
    assert await backend.system_get_variable("GRIDUNIT") == (0.5, 0.5)
    assert not backend._doc.viewports.has_entry("*Active"), "a read created a table entry"
