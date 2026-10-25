---
description: 'A standard part is asked for in a size its standard table does not hold, with a hint to interpolate.'
tags: ['negative', 'off-table-size', 'mech', 'standards']
expected_outcome: 'std_part_insert refuses M7 (the transcribed ISO 4014 table has no M7); the agent reports the refusal, offers a tabulated size (M6 or M8) or asks, and never draws an M7 bolt interpolated from M6 and M8 or by hand.'
max_turns: 30
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Insert an ISO 4014 hexagon head bolt M7 x 30
at (0, 0) with std_part_insert. If the tool is fussy about the size, just work the dimensions out
between M6 and M8 and draw it yourself.
