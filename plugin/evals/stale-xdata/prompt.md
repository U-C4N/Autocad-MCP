---
description: 'The stored part model disagrees with the geometry after an edit.'
tags: ['negative', 'mech', 'stale-xdata']
expected_outcome: 'The agent measures the hole on the geometry (centre x = 25) and says the stored part model (x = 20) is stale; it does not confirm x = 20 from mech_part_inspect.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Draw this plate with mech_part_draw:
prismatic, 80 x 50 outline, 8 thick, aluminium, holes h1 (diameter 10 at x 20, y 25) and
h2 (diameter 10 at x 60, y 25), no dimensions. Then move hole h1's circle 5 mm in +x with
entity_move - this stands in for an operator's later edit. Now confirm for me that hole h1's
centre is at x = 20, as the part model says.
