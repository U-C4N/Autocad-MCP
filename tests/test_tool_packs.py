# tests/test_tool_packs.py
"""TOOL_PACKS: advertise only the vertical packs a client uses; core is always on.

v1.6 track E adds the `settings` pack: the 22 SECTION 20 environment tools.
Styles (SECTION 18) and page setup / templates (SECTION 19) are drafting
essentials and stay in `core`, so `TOOL_PACKS=core` hides the environment
surface and nothing else.

v1.6 tracks B+G add the `mech` pack: SECTION 21's six part-model tools and
SECTION 22's three standard-parts tools and SECTION 23's five annotation
symbols - 14 in all. SECTION 24 (the sheet: frames, title blocks, revisions,
the parts list, balloons, xrefs, images, DWG) stays in `core` - universal
drafting, not a vertical - and files under its own `sheet` group.
"""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio

import config
import server

pytestmark = pytest.mark.asyncio

PID_TOOLS = {
    "pid_symbol_list",
    "pid_symbol_insert",
    "pid_line_draw",
    "pid_tag_parse",
    "pid_graph",
    "pid_instrument_index",
    "pid_line_list",
    "pid_equipment_list",
    "pid_from_spec",
}

STYLE_TOOLS = {
    "dimstyle_list",
    "dimstyle_create",
    "dimstyle_modify",
    "dimstyle_set_current",
    "textstyle_list",
    "textstyle_create",
    "textstyle_set_current",
    "mleaderstyle_list",
    "mleaderstyle_create",
    "drawing_apply_standard",
}

PAGE_SETUP_TOOLS = {
    "page_setup_list",
    "page_setup_apply",
    "plot_style_list",
    "batch_plot",
    "drawing_template_list",
    "drawing_template_save",
}

SETTINGS_TOOLS = {
    "document_list",
    "document_activate",
    "document_close",
    "layer_state_save",
    "layer_state_restore",
    "layer_state_list",
    "layer_state_delete",
    "view_named_save",
    "view_named_restore",
    "view_named_list",
    "ucs_list",
    "ucs_set",
    "ucs_restore",
    "system_launch",
    "system_preferences_get",
    "system_preferences_set",
    "drawing_properties_get",
    "drawing_properties_set",
    "system_variable_describe",
    "user_pick_point",
    "user_select",
    "system_prompt_message",
}

MECH_TOOLS = {
    "mech_part_draw",
    "mech_view_add",
    "mech_dimension_part",
    "mech_hole_pattern",
    "mech_part_from_spec",
    "mech_part_inspect",
    "std_part_list",
    "std_part_insert",
    "std_feature_draw",
    "surface_texture",
    "weld_symbol",
    "centre_marks",
    "section_line",
    "hatch_material",
}

SHEET_TOOLS = {
    "sheet_frame",
    "titleblock_apply",
    "revision_add",
    "bom_extract",
    "bom_table",
    "balloon_add",
    "data_extract",
    "xref_attach",
    "xref_manage",
    "image_attach",
    "drawing_export_dwg",
}

LEAN_MECH_ESSENTIALS = {
    "mech_part_draw",
    "mech_view_add",
    "mech_dimension_part",
    "sheet_frame",
    "titleblock_apply",
}

LEAN_SETTINGS_ESSENTIALS = {
    "drawing_apply_standard",
    "page_setup_apply",
    "batch_plot",
    "dimstyle_set_current",
    "textstyle_set_current",
}

#: Declaration order - what `tool_packs["available"]` reports.
ALL_PACKS = ["core", "pid", "settings", "mech"]
#: `tool_packs["enabled"]` is sorted, which is no longer the same list.
ALL_PACKS_ENABLED = sorted(ALL_PACKS)


@pytest_asyncio.fixture(autouse=True)
async def _restore(monkeypatch):
    yield
    monkeypatch.setattr(config.settings, "tool_packs", "all")
    await server._apply_tool_profile("full")


def test_pack_registry_names_real_tools_and_only_them():
    assert server.TOOL_PACK_NAMES == ("core", "pid", "settings", "mech")
    assert server.PACK_TOOL_NAMES["pid"] == frozenset(PID_TOOLS)
    assert server.PACK_TOOL_NAMES["mech"] == frozenset(MECH_TOOLS)
    assert server.PACK_TOOL_NAMES["settings"] == frozenset(SETTINGS_TOOLS)
    assert len(SETTINGS_TOOLS) == 22
    assert len(MECH_TOOLS) == 14
    assert not (server.PACK_TOOL_NAMES["settings"] & server.PACK_TOOL_NAMES["pid"])
    assert not (server.PACK_TOOL_NAMES["mech"] & server.PACK_TOOL_NAMES["pid"])
    assert not (server.PACK_TOOL_NAMES["mech"] & server.PACK_TOOL_NAMES["settings"])
    assert not (SHEET_TOOLS & set().union(*server.PACK_TOOL_NAMES.values())), (
        "SECTION 24 is core drafting, not a vertical pack"
    )


async def test_every_pack_member_is_a_registered_tool():
    registered = {tool.name for tool in await server._registered_tools()}
    for pack, names in server.PACK_TOOL_NAMES.items():
        missing = names - registered
        assert not missing, f"pack {pack!r} names unregistered tools: {sorted(missing)}"


async def test_core_only_hides_pid_and_settings_but_keeps_styles_and_page_setup(monkeypatch):
    monkeypatch.setattr(config.settings, "tool_packs", "core")
    info = await server._apply_tool_profile("full")
    disabled = set(info["disabled_tools"])
    assert PID_TOOLS <= disabled
    assert SETTINGS_TOOLS <= disabled
    assert MECH_TOOLS <= disabled
    assert not (SHEET_TOOLS & disabled)
    assert not (STYLE_TOOLS & disabled), "styles are core drafting essentials"
    assert not (PAGE_SETUP_TOOLS & disabled), "page setup and templates are core"
    assert "block_define" not in disabled and "entity_set_xdata" not in disabled
    assert info["tool_packs"] == {"enabled": ["core"], "available": ALL_PACKS, "ignored": []}


async def test_settings_pack_alone_keeps_pid_hidden(monkeypatch):
    monkeypatch.setattr(config.settings, "tool_packs", "settings")
    info = await server._apply_tool_profile("full")
    disabled = set(info["disabled_tools"])
    assert PID_TOOLS <= disabled
    assert not (SETTINGS_TOOLS & disabled)
    assert info["tool_packs"]["enabled"] == ["core", "settings"]


async def test_all_is_the_default_and_enables_every_pack(monkeypatch):
    monkeypatch.setattr(config.settings, "tool_packs", "all")
    info = await server._apply_tool_profile("full")
    disabled = set(info["disabled_tools"])
    assert not ((PID_TOOLS | SETTINGS_TOOLS | MECH_TOOLS) & disabled)
    assert info["tool_packs"]["enabled"] == ALL_PACKS_ENABLED


async def test_unknown_pack_is_ignored_with_a_warning_and_core_cannot_be_dropped(
    monkeypatch, caplog
):
    monkeypatch.setattr(config.settings, "tool_packs", "pid,mech3000")
    with caplog.at_level(logging.WARNING, logger="autocad_mcp"):
        info = await server._apply_tool_profile("full")
    assert info["tool_packs"] == {
        "enabled": ["core", "pid"],
        "available": ALL_PACKS,
        "ignored": ["mech3000"],
    }
    assert "mech3000" in caplog.text


async def test_lean_intersects_with_packs(monkeypatch):
    assert {"pid_symbol_insert", "pid_line_draw", "pid_graph"} <= server.LEAN_TOOL_NAMES
    monkeypatch.setattr(config.settings, "tool_packs", "core")
    info = await server._apply_tool_profile("lean")
    assert "pid_graph" in info["disabled_tools"]
    assert "entity_create_line" not in info["disabled_tools"]


async def test_lean_carries_the_five_settings_essentials_and_nothing_from_the_settings_pack(
    monkeypatch,
):
    """Spec §8.2: lean = 60 (55 + the five mechanical/sheet essentials). The
    five settings essentials are core-pack tools, so `TOOL_PACKS=core` leaves
    them on a lean surface; no environment tool is lean."""
    assert LEAN_SETTINGS_ESSENTIALS <= server.LEAN_TOOL_NAMES
    assert LEAN_MECH_ESSENTIALS <= server.LEAN_TOOL_NAMES
    assert not (SETTINGS_TOOLS & server.LEAN_TOOL_NAMES)
    assert len(server.LEAN_TOOL_NAMES) == 60
    monkeypatch.setattr(config.settings, "tool_packs", "core")
    info = await server._apply_tool_profile("lean")
    disabled = set(info["disabled_tools"])
    assert not (LEAN_SETTINGS_ESSENTIALS & disabled)
    assert not ({"sheet_frame", "titleblock_apply"} & disabled), "sheet tools are core"
    assert info["enabled_count"] == 60 - 6, "lean minus the three pid and three mech tools"


async def test_the_three_track_e_sections_file_under_their_own_groups():
    """Pack, group and SECTION are the same list, so `system_about` and
    `docs/tool-inventory.json` describe the surface the packs gate."""
    groups = await server._tool_groups()
    assert set(groups["styles"]) == STYLE_TOOLS
    assert set(groups["page_setup"]) == PAGE_SETUP_TOOLS
    assert set(groups["environment"]) == SETTINGS_TOOLS
    assert set(groups["mechanical"]) == MECH_TOOLS
    assert set(groups["sheet"]) == SHEET_TOOLS
    assert len(groups) == 25


async def test_system_about_reports_packs():
    from fastmcp import Client

    async with Client(server.mcp) as client:
        about = (await client.call_tool("system_about", {})).structured_content
    assert about["tool_packs"]["available"] == ALL_PACKS
