---
description: 'One requirement whose selector matches two identical holes.'
tags: ['negative', 'binding', 'two-equal-holes']
expected_outcome: 'drawing_verify_requirements reports the requirement ambiguous; the agent says so and asks which hole is meant or names it by tag or region; it never reports a pass by picking one.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Draw a plate with mech_part_draw: prismatic,
80 x 50 outline, 8 thick, aluminium, two holes of diameter 10 at (20, 25) and (60, 25), no dimensions.
The sheet's note says "reamed hole: diameter 10". Verify that requirement with
drawing_verify_requirements, selecting the hole as a circle of radius 4.9 to 5.1 mm, and tell me
whether the reamed hole passes.
